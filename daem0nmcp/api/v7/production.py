"""Production composition root for the exact Daem0nMCP v7 surface."""

from __future__ import annotations

import importlib.util
import inspect
import ipaddress
import json
import logging
import os
import re
import secrets
from collections.abc import Mapping, MutableMapping
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from types import MappingProxyType
from typing import Any, Literal, TypeVar

from ...capabilities import CapabilityRegistry
from ...capture_candidates import CaptureCandidateStore
from ...config import Settings
from ...covenant import (
    CovenantGate,
    CovenantStateStore,
    InvocationScope,
    authority_from_environment,
    invocation_scope_var,
)
from ...dreaming.v7_runtime import V7DreamingCoordinator
from ...edit_bridge import BridgeIdentity, EditApprovalBroker
from ...edit_bridge_transport import (
    LocalBridgeServer,
    RemoteBridgeHTTPSServer,
    build_edit_bridge_service,
    local_authority_principal,
)
from ...storage_activation import resolve_active_database
from ...transport_security import (
    build_fastmcp_auth,
    validate_transport_security,
)
from ...workspace import Workspace, WorkspaceRegistry
from ...workspace_access import WorkspaceAccessPolicy
from .code_entity_operations import (
    CodeEntityOperationDependencies,
    build_code_entity_operations,
)
from .composition import V7Surface, build_v7_surface
from .consolidation_operations import (
    ConsolidationOperationDependencies,
    build_consolidation_operations,
)
from .discovery_operations import (
    DiscoveryOperationDependencies,
    build_discovery_operations,
)
from .edit_capture_operations import (
    EditCaptureOperationDependencies,
    build_edit_capture_operations,
)
from .external_operations import (
    ExternalOperationDependencies,
    build_external_operations,
)
from .federation_operations import (
    FederationOperationDependencies,
    build_federation_operations,
)
from .graph_operations import GraphOperationDependencies, build_graph_operations
from .health_diagnostics import RuntimeHealthDiagnostics
from .intelligence_operations import (
    IntelligenceOperationDependencies,
    build_intelligence_operations,
)
from .local_state_operations import (
    LocalStateOperationDependencies,
    build_local_state_operations,
)
from .maintenance_operations import (
    MaintenanceOperationDependencies,
    build_maintenance_operations,
)
from .models import CapabilityState, RecordSummary, WireModel
from .opaque_capabilities import OpaqueCapabilityAuthority
from .operations import CoreOperationDependencies, build_core_operations
from .pinned import PinnedDependencies
from .policy import V7_COVENANT_POLICY
from .record_operations import (
    RecordOperationDependencies,
    build_record_operations,
)
from .relationship_operations import (
    RelationshipOperationDependencies,
    build_relationship_operations,
)
from .resource_repository import (
    ResourceRepositoryReaders,
    build_sqlite_resource_readers,
)
from .resources import (
    ActiveContextItem,
    ResourceReader,
    ResourceReadRequest,
    ResourceRow,
    RuleView,
)
from .responses import ResponseFactory
from .rule_trigger_operations import (
    RuleTriggerOperationDependencies,
    build_rule_trigger_operations,
)
from .runtime_services import (
    BasicBriefingService,
    BasicHealthService,
    BasicPreflightService,
    RuntimeServiceError,
    SQLiteMemoryEventWriter,
    Task8RecallService,
    WorkspaceStorageResolver,
    resolve_workspace_storage,
)
from .task_dispatcher import DurableTaskDispatcher, validate_task_redis_url
from .tools import SessionBriefInput, build_argument_normalizer
from .utility_operations import (
    UtilityOperationDependencies,
    build_utility_operations,
)
from .workspace_bootstrap import WorkspaceBootstrapLifecycle

TransportMode = Literal["stdio", "streamable-http"]


class ProductionConfigurationError(RuntimeError):
    """Path-free failure raised before a partially configured server exists."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class _ProductionAssembly:
    surface: V7Surface
    auth: object | None
    tasks_enabled: bool
    task_dispatcher: DurableTaskDispatcher | None
    services: tuple[object, ...]
    sync_timeout_seconds: float
    edit_broker: EditApprovalBroker
    capture_candidates: CaptureCandidateStore
    edit_bridge_service: LocalBridgeServer | RemoteBridgeHTTPSServer | None


def _runtime_lifespan(services: tuple[object, ...]):
    """Own and deterministically close per-server runtime services."""

    @asynccontextmanager
    async def lifespan(_server: object):
        try:
            for service in services:
                start = getattr(service, "start", None)
                if callable(start):
                    result = start()
                    if inspect.isawaitable(result):
                        await result
            yield {}
        finally:
            first_error = None
            for service in reversed(services):
                close = getattr(service, "aclose", None) or getattr(
                    service, "close", None
                )
                try:
                    if callable(close):
                        result = close()
                        if inspect.isawaitable(result):
                            await result
                except Exception as error:
                    if first_error is None:
                        first_error = error
            if first_error is not None:
                raise first_error

    return lifespan


class _ProjectionLifecycle:
    """Resume durable projection work at startup and quiesce it on shutdown."""

    def __init__(
        self,
        workspaces: tuple[Workspace, ...],
        settings: Settings,
        capability_statuses: Mapping[str, str],
    ) -> None:
        self._workspaces = workspaces
        self._settings = settings
        self._capability_statuses = dict(capability_statuses)
        self._paths: list[Path] = []

    def start(self) -> None:
        from ...retrieval.runtime import schedule_projection_job_drain

        for workspace in self._workspaces:
            try:
                active = _active_database(workspace)
                schedule_projection_job_drain(
                    active.path,
                    config=self._settings,
                    max_jobs=1,
                    capability_statuses=self._capability_statuses,
                    continuous=True,
                )
                self._paths.append(active.path)
            except Exception:
                logging.getLogger(__name__).warning(
                    "Projection startup requires available v7 storage"
                )

    async def aclose(self) -> None:
        from ...retrieval.runtime import await_projection_job_drains

        await await_projection_job_drains(tuple(self._paths))


# Only entry points the profile's own extra installs; a module from another
# profile would fail here and degrade a working one.
_NATIVE_PROFILE_MODULES = {
    "local": ("qdrant_client",),
    "models-local": ("sentence_transformers", "onnx", "onnxruntime"),
    "graph": ("networkx", "igraph", "leidenalg"),
}


class _OptionalNativeRuntimeLifecycle:
    """Initialize enabled native provider entry points before worker threads."""

    def __init__(self, capability_statuses: MutableMapping[str, str]) -> None:
        self._statuses = capability_statuses
        self.failures: dict[str, str] = {}

    def start(self) -> None:
        for profile, modules in _NATIVE_PROFILE_MODULES.items():
            if self._statuses.get(profile) != "ready":
                continue
            for module in modules:
                # Import the public model/runtime entry points on the main
                # thread. This initializes NumPy/SciPy/native DLL dependencies
                # before a dense query can race another provider's first worker.
                try:
                    importlib.import_module(module)
                except Exception as error:
                    # Installed metadata said ready, but the package cannot run
                    # here. Report that once instead of per tool call.
                    self._statuses[profile] = "degraded"
                    self.failures[profile] = (
                        f"importing {module} raised {type(error).__name__}"
                    )
                    logging.getLogger(__name__).warning(
                        "Optional %s runtime is unavailable: importing %s raised %s",
                        profile,
                        module,
                        type(error).__name__,
                    )
                    break


def _loopback_host(host: str) -> bool:
    normalized = host.strip()
    if normalized.startswith("[") and normalized.endswith("]"):
        normalized = normalized[1:-1]
    if normalized.casefold() == "localhost":
        return True
    try:
        return ipaddress.ip_address(normalized).is_loopback
    except ValueError:
        return False


def _local_authority_principal(settings: Settings) -> str:
    """Identify reconnecting local clients by their managed-storage authority."""

    return local_authority_principal(settings.get_storage_path())


def _task_configuration(
    environ: Mapping[str, str],
) -> tuple[bool, CapabilityState, str | None]:
    configured = environ.get("DAEM0NMCP_TASK_REDIS_URL")
    if configured is None:
        return (
            False,
            CapabilityState.model_validate(
                {
                    "name": "tasks",
                    "status": "disabled",
                    "reason_code": "TASKS_UNAVAILABLE",
                    "remediation": "Set an authenticated loopback DAEM0NMCP_TASK_REDIS_URL to "
                    "enable durable tasks.",
                }
            ),
            None,
        )
    try:
        redis_url = validate_task_redis_url(configured)
    except ValueError as exc:
        raise ProductionConfigurationError("TASK_CONFIGURATION_INVALID") from exc
    if importlib.util.find_spec("redis") is None:
        raise ProductionConfigurationError("TASK_PROFILE_UNAVAILABLE")
    return (
        True,
        CapabilityState.model_validate({"name": "tasks", "status": "ready"}),
        redis_url,
    )


def _remediation_text(name: str, remediation: Mapping[str, Any]) -> str:
    """Render the registry's structured remediation as one actionable line."""

    parts = [
        str(remediation[key])
        for key in ("message", "command", "environment")
        if remediation.get(key)
    ]
    missing = remediation.get("missing")
    if isinstance(missing, list) and missing:
        parts.append("Missing: " + ", ".join(str(item) for item in missing[:8]) + ".")
    return " ".join(parts) or f"Review the {name} capability profile."


def _capability_states(
    environ: Mapping[str, str],
    native_failures: Mapping[str, str] = MappingProxyType({}),
) -> tuple[CapabilityState, ...]:
    values: list[CapabilityState] = []
    for name, capability in CapabilityRegistry(environ=environ).all().items():
        status = str(capability["status"])
        failure = native_failures.get(name)
        if status == "ready" and failure is None:
            values.append(
                CapabilityState.model_validate({"name": name, "status": "ready"})
            )
            continue
        if failure is not None:
            status = "degraded"
        reason = {
            "disabled": "CAPABILITY_DISABLED",
            "degraded": "CAPABILITY_DEGRADED",
            "failed": "CAPABILITY_CONFIGURATION_INVALID",
        }[status]
        remediation = capability.get("remediation")
        text = (
            f"The {name} profile is installed but cannot run here: {failure}. "
            "Reinstall or repair it."
            if failure is not None
            else _remediation_text(
                name, remediation if isinstance(remediation, Mapping) else {}
            )
        )
        values.append(
            CapabilityState.model_validate(
                {
                    "name": name,
                    "status": status,
                    "reason_code": reason,
                    "remediation": text,
                }
            )
        )
    return tuple(values)


def _active_database(workspace: Workspace):
    storage = resolve_workspace_storage(workspace)
    active = resolve_active_database(storage)
    if active.format_version == 6:
        # The store is intact but still v6; retrying never helps, so say so
        # and name the offline command that migrates it.
        raise ProductionConfigurationError("MIGRATION_REQUIRED")
    if active.format_version != 7:
        raise ProductionConfigurationError("ACTIVE_V7_UNAVAILABLE")
    return active


PublicItem = TypeVar("PublicItem", bound=WireModel)


def _public_items(rows: object, expected: type[PublicItem]) -> list[PublicItem]:
    if not isinstance(rows, list):
        raise TypeError("resource reader returned an invalid result")
    values: list[PublicItem] = []
    for row in rows:
        if isinstance(row, ResourceRow):
            if row.deleted:
                continue
            row = row.item
        if not isinstance(row, expected):
            raise TypeError("resource reader returned an invalid item")
        values.append(row)
    return values


async def _read_items(
    reader: ResourceReader,
    workspace: Workspace,
    request: ResourceReadRequest,
    expected: type[PublicItem],
) -> list[PublicItem]:
    result = reader(workspace, request)
    if inspect.isawaitable(result):
        result = await result
    return _public_items(result, expected)


def _merge_operations(
    *operation_maps: Mapping[str, Any],
) -> Mapping[str, Any]:
    merged: dict[str, Any] = {}
    for operations in operation_maps:
        overlap = set(merged) & set(operations)
        if overlap:
            raise ProductionConfigurationError("DUPLICATE_OPERATION")
        merged.update(operations)
    return MappingProxyType(merged)


_RELEVANCE_TOKEN = re.compile(r"[a-z0-9][a-z0-9_-]{1,79}")


def _relevance_tokens(*values: object) -> frozenset[str]:
    encoded = json.dumps(
        values,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
    ).casefold()
    tokens: set[str] = set()
    for value in _RELEVANCE_TOKEN.findall(encoded):
        tokens.add(value)
        tokens.update(
            component
            for component in value.replace("-", "_").split("_")
            if len(component) >= 2
        )
    return frozenset(tokens)


def _rank_relevant(
    values: list[PublicItem],
    query_tokens: frozenset[str],
    *,
    limit: int,
) -> list[PublicItem]:
    if not query_tokens:
        return values[:limit]
    ranked: list[tuple[int, int, PublicItem]] = []
    for index, value in enumerate(values):
        searchable: object
        if isinstance(value, RecordSummary):
            searchable = {
                "record_type": value.record_type,
                "excerpt": value.excerpt,
                "tags": value.tags,
                "relative_file_path": value.relative_file_path,
            }
        elif isinstance(value, RuleView):
            searchable = {
                "trigger": value.trigger,
                "must_do": value.must_do,
                "must_not": value.must_not,
                "ask_first": value.ask_first,
                "warnings": value.warnings,
            }
        elif hasattr(value, "model_dump"):
            searchable = value.model_dump(mode="json")
        else:
            searchable = value
        score = len(query_tokens & _relevance_tokens(searchable))
        if score:
            ranked.append((-score, index, value))
    ranked.sort(key=lambda item: (item[0], item[1]))
    return [item[2] for item in ranked[:limit]]


def _unique_text(values: list[str], *, limit: int) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        if value not in seen:
            result.append(value)
            seen.add(value)
        if len(result) == limit:
            break
    return result


def _briefing_reader(readers: ResourceRepositoryReaders):
    async def read(
        workspace: Workspace, request: SessionBriefInput
    ) -> dict[str, object]:
        try:
            _active_database(workspace)
        except ProductionConfigurationError as exc:
            raise RuntimeServiceError(exc.code) from None
        except Exception:
            raise RuntimeServiceError("ACTIVE_V7_UNAVAILABLE") from None
        warning_limit = request.warning_limit
        failure_limit = request.failure_limit
        focus_areas = list(request.focus_areas)
        focus_tokens = _relevance_tokens(focus_areas)
        snapshot_reader = readers.briefing_snapshot_reader
        if snapshot_reader is not None:
            snapshot = await snapshot_reader(
                workspace,
                warning_limit=warning_limit,
                failure_limit=failure_limit,
                rule_limit=50,
                active_context_limit=50,
            )
            warnings = _public_items(snapshot.warnings, RecordSummary)
            failures = _public_items(snapshot.failures, RecordSummary)
            rules = _public_items(snapshot.rules, RuleView)
            active_context = _public_items(snapshot.active_context, ActiveContextItem)
            decisions = _rank_relevant(
                _public_items(snapshot.decisions, RecordSummary),
                focus_tokens,
                limit=50,
            )
            git_changes = list(snapshot.git_changes)[:200]
            projection_freshness = list(snapshot.projection_freshness)[:7]
            statistics = dict(snapshot.workspace_statistics)
            stale_projection_count = snapshot.stale_projection_count
        else:
            warnings = []
            if warning_limit:
                warnings = await _read_items(
                    readers.warning_reader,
                    workspace,
                    ResourceReadRequest("warnings", warning_limit, "updated_at_desc"),
                    RecordSummary,
                )
            failures = []
            if failure_limit:
                failures = await _read_items(
                    readers.failure_reader,
                    workspace,
                    ResourceReadRequest("failures", failure_limit, "updated_at_desc"),
                    RecordSummary,
                )
            rules = await _read_items(
                readers.rule_reader,
                workspace,
                ResourceReadRequest("rules", 50, "priority_desc", enabled_only=True),
                RuleView,
            )
            active_context = await _read_items(
                readers.active_context_reader,
                workspace,
                ResourceReadRequest("active_context", 50, "priority_desc"),
                ActiveContextItem,
            )
            decisions = []
            git_changes = []
            projection_freshness = []
            statistics = {
                "warnings": len(warnings),
                "failed_outcomes": len(failures),
                "rules": len(rules),
                "active_context": len(active_context),
            }
            stale_projection_count = 0
        failed_outcomes = [
            {
                "record_id": item.record_id,
                "outcome_excerpt": item.excerpt,
                "worked": False,
                "happened_at": item.updated_at,
            }
            for item in failures
        ]
        next_steps: list[dict[str, str]] = []
        if warnings or failures or rules:
            next_steps.append(
                {
                    "tool": "memory_preflight",
                    "reason": (
                        "Review bound warnings, failed approaches, and rules "
                        "before a protected operation."
                    ),
                }
            )
        if focus_areas:
            next_steps.append(
                {
                    "tool": "memory_recall",
                    "reason": "Retrieve evidence for the requested focus areas.",
                }
            )
        if stale_projection_count:
            next_steps.append(
                {
                    "tool": "projection_rebuild",
                    "reason": "One or more retrieval projections require rebuilding.",
                }
            )
        if not next_steps:
            next_steps.append(
                {
                    "tool": "memory_recall",
                    "reason": "Retrieve task-specific evidence before acting.",
                }
            )
        return {
            "workspace_id": workspace.workspace_id,
            "briefed_at": datetime.now(timezone.utc),
            "workspace_statistics": statistics,
            "recent_decisions": decisions,
            "warnings": warnings,
            "failed_outcomes": failed_outcomes,
            "applicable_rules": rules,
            "active_context": active_context,
            "git_changes": git_changes,
            "projection_freshness": projection_freshness,
            "covenant_next_steps": next_steps,
        }

    return read


def _guidance_reader(readers: ResourceRepositoryReaders):
    async def read(
        workspace: Workspace,
        target_tool: str,
        normalized_arguments: Mapping[str, Any],
        description: str | None,
    ) -> dict[str, object]:
        query_tokens = _relevance_tokens(
            target_tool,
            normalized_arguments,
            description,
        )
        snapshot_reader = readers.briefing_snapshot_reader
        if snapshot_reader is not None:
            snapshot = await snapshot_reader(
                workspace,
                warning_limit=20,
                failure_limit=20,
                rule_limit=50,
                active_context_limit=1,
                include_git_changes=False,
            )
            candidate_records = [
                *_public_items(snapshot.failures, RecordSummary),
                *_public_items(snapshot.warnings, RecordSummary),
            ]
            candidate_rules = _public_items(snapshot.rules, RuleView)
        else:
            warnings = await _read_items(
                readers.warning_reader,
                workspace,
                ResourceReadRequest("warnings", 20, "updated_at_desc"),
                RecordSummary,
            )
            failures = await _read_items(
                readers.failure_reader,
                workspace,
                ResourceReadRequest("failures", 20, "updated_at_desc"),
                RecordSummary,
            )
            candidate_records = [*failures, *warnings]
            candidate_rules = await _read_items(
                readers.rule_reader,
                workspace,
                ResourceReadRequest("rules", 20, "priority_desc", enabled_only=True),
                RuleView,
            )
        records = _rank_relevant(
            candidate_records,
            query_tokens,
            limit=20,
        )
        rules = _rank_relevant(
            candidate_rules,
            query_tokens,
            limit=20,
        )
        return {
            "records": records,
            "rules": rules,
            "must_do": _unique_text(
                [value for rule in rules for value in rule.must_do],
                limit=50,
            ),
            "must_not": _unique_text(
                [value for rule in rules for value in rule.must_not],
                limit=50,
            ),
            "ask_first": _unique_text(
                [value for rule in rules for value in rule.ask_first],
                limit=50,
            ),
            "warnings": _unique_text(
                [
                    *[value for rule in rules for value in rule.warnings],
                    *[
                        record.excerpt
                        for record in records
                        if record.record_type == "warning"
                    ],
                ],
                limit=50,
            ),
        }

    return read


def _assemble(
    transport_mode: str,
    *,
    host: str | None,
    settings: Settings | None,
    environ: Mapping[str, str] | None,
) -> _ProductionAssembly:
    if transport_mode not in {"stdio", "streamable-http"}:
        raise ValueError("v7 supports stdio or streamable-http")
    env = os.environ if environ is None else environ
    loaded_settings = settings or Settings()
    if not isinstance(loaded_settings, Settings):
        raise TypeError("settings must be Settings")

    auth: object | None = None
    loopback = False
    if transport_mode == "streamable-http":
        selected_host = host or "127.0.0.1"
        auth = build_fastmcp_auth(env)
        validate_transport_security(
            selected_host,
            auth_provider=auth,
            environ=env,
        )
        loopback = _loopback_host(selected_host)

    authority = authority_from_environment(
        local_stdio=transport_mode == "stdio" or loopback,
        environ=env,
    )
    if authority is None:
        raise ProductionConfigurationError("CAPABILITY_AUTHORITY_UNAVAILABLE")
    tasks_enabled, task_state, task_redis_url = _task_configuration(env)
    capability_statuses = {
        name: str(capability["status"])
        for name, capability in CapabilityRegistry(environ=env).all().items()
    }
    # Preload before anything snapshots the statuses, so an installed but
    # broken extra degrades everywhere instead of only failing per call.
    native_runtimes = _OptionalNativeRuntimeLifecycle(capability_statuses)
    native_runtimes.start()
    normalizer = build_argument_normalizer()
    registry = WorkspaceRegistry.from_settings(loaded_settings)
    workspaces = {registry.default.workspace_id: registry.default}
    for root in loaded_settings.workspace_roots:
        workspace = registry.resolve(root)
        workspaces[workspace.workspace_id] = workspace
    access_policy = WorkspaceAccessPolicy(
        workspaces={
            str(workspace.root): workspace.workspace_id
            for workspace in workspaces.values()
        },
        local_principal=_local_authority_principal(loaded_settings),
        path=Path(
            env.get("DAEM0NMCP_WORKSPACE_ACCESS_FILE")
            or (Path(loaded_settings.get_storage_path()) / "v7-workspace-access.json")
        ),
    )
    gate = CovenantGate(
        state_store=CovenantStateStore(),
        authority=OpaqueCapabilityAuthority(authority),
        policy=V7_COVENANT_POLICY,
        argument_normalizer=normalizer,
        workspace_authorizer=access_policy,
    )
    resource_readers = build_sqlite_resource_readers(_active_database)
    storage_resolver = WorkspaceStorageResolver()
    operation_secret = secrets.token_bytes(32)
    writer = SQLiteMemoryEventWriter(
        storage_resolver=storage_resolver,
        projection_config=loaded_settings,
        capability_statuses=capability_statuses,
    )
    recall = Task8RecallService(
        storage_resolver=storage_resolver,
        workspace_resolver=registry,
        config=loaded_settings,
        capability_statuses=capability_statuses,
    )
    discovery_dependencies = DiscoveryOperationDependencies(
        storage_resolver=storage_resolver,
        cursor_secret=operation_secret,
        recall_service=recall,
        capability_statuses=capability_statuses,
    )
    relationship_dependencies = RelationshipOperationDependencies(
        storage_resolver=storage_resolver,
    )
    utility_dependencies = UtilityOperationDependencies(
        cursor_secret=operation_secret,
    )
    maintenance_dependencies = MaintenanceOperationDependencies(
        storage_resolver=storage_resolver,
        selection_secret=operation_secret,
    )
    intelligence_dependencies = IntelligenceOperationDependencies(
        storage_resolver=storage_resolver,
    )
    code_entity_dependencies = CodeEntityOperationDependencies(
        operation_secret=operation_secret,
        storage_resolver=storage_resolver,
    )

    def authorize_current_workspace(workspace: Workspace) -> bool:
        from .tasks import durable_task_execution_var

        scope = invocation_scope_var.get()
        execution = durable_task_execution_var.get()
        if execution is not None:
            scope = InvocationScope(
                execution.principal_id,
                execution.transport_session_id or "durable-admission",
                str(workspace.root),
            )
        if scope is None:
            return False
        return gate.workspace_authorized(
            InvocationScope(
                scope.principal_id, scope.transport_session_id, str(workspace.root)
            )
        )

    federation_dependencies = FederationOperationDependencies(
        workspace_resolver=registry,
        storage_resolver=storage_resolver,
        cursor_secret=operation_secret,
        workspace_authorizer=authorize_current_workspace,
    )

    def schedule_capture_projection(path: Path) -> None:
        from ...retrieval.runtime import schedule_projection_job_drain

        schedule_projection_job_drain(
            path,
            config=loaded_settings,
            capability_statuses=capability_statuses,
        )

    capture_candidates = CaptureCandidateStore(
        storage_resolver=storage_resolver,
        projection_scheduler=schedule_capture_projection,
    )
    dreaming = V7DreamingCoordinator(
        workspaces=tuple(workspaces.values()),
        settings=loaded_settings,
        candidate_store=capture_candidates,
        storage_resolver=storage_resolver,
        capability_statuses=capability_statuses,
        projection_scheduler=schedule_capture_projection,
    )
    task_dispatcher: DurableTaskDispatcher | None = None
    edit_bridge_service: LocalBridgeServer | RemoteBridgeHTTPSServer | None = None
    runtime_health = RuntimeHealthDiagnostics(
        storage_resolver=storage_resolver,
        capability_statuses=capability_statuses,
        scope_provider=invocation_scope_var.get,
        workspace_authorizer=gate.workspace_authorized,
        task_provider=lambda: task_dispatcher,
        bridge_provider=lambda: edit_bridge_service,
    )
    health = BasicHealthService(
        auth_mode=(
            "process"
            if transport_mode == "stdio"
            else "loopback"
            if auth is None
            else "jwt"
        ),
        task_support=task_state,
        capability_states=_capability_states(env, native_runtimes.failures),
        storage_resolver=storage_resolver,
        dreaming_provider=dreaming.health,
        runtime_diagnostics_provider=runtime_health.inspect,
    )
    pinned = PinnedDependencies(
        workspace_resolver=registry,
        covenant_gate=gate,
        argument_normalizer=normalizer,
        briefing_service=BasicBriefingService(
            reader=_briefing_reader(resource_readers)
        ),
        preflight_service=BasicPreflightService(
            reader=_guidance_reader(resource_readers)
        ),
        recall_service=recall,
        memory_event_writer=writer,
        health_service=health,
        response_factory=ResponseFactory(),
    )
    record_dependencies = RecordOperationDependencies(
        storage_resolver=storage_resolver,
        cursor_secret=operation_secret,
    )
    consolidation_dependencies = ConsolidationOperationDependencies(
        workspace_resolver=registry,
        covenant_gate=gate,
        scope_provider=invocation_scope_var.get,
        storage_resolver=storage_resolver,
        signing_key=operation_secret,
        projection_scheduler=schedule_capture_projection,
    )
    edit_broker = EditApprovalBroker(
        storage_resolver=storage_resolver,
        signing_key=operation_secret,
    )
    try:

        def authorize_bridge_workspace(
            workspace: Workspace, identity: BridgeIdentity
        ) -> bool:
            return gate.workspace_authorized(
                InvocationScope(
                    identity.principal_id,
                    "bridge-access-check",
                    str(workspace.root),
                )
            )

        edit_bridge_service = build_edit_bridge_service(
            broker=edit_broker,
            candidates=capture_candidates,
            workspace_resolver=registry.resolve,
            workspace_authorizer=authorize_bridge_workspace,
            environ=env,
        )
    except (OSError, TypeError, ValueError) as exc:
        raise ProductionConfigurationError("EDIT_BRIDGE_CONFIGURATION_INVALID") from exc
    graph_dependencies = GraphOperationDependencies(
        storage_resolver=storage_resolver,
        capability_statuses=capability_statuses,
    )
    operations = _merge_operations(
        build_core_operations(
            CoreOperationDependencies(
                covenant_gate=gate,
                scope_provider=invocation_scope_var.get,
                storage_path_resolver=resolve_workspace_storage,
                projection_config=loaded_settings,
                projection_capability_statuses=capability_statuses,
            )
        ),
        build_record_operations(record_dependencies),
        build_edit_capture_operations(
            EditCaptureOperationDependencies(
                broker=edit_broker,
                candidates=capture_candidates,
                cursor_secret=operation_secret,
                scope_provider=invocation_scope_var.get,
            )
        ),
        build_external_operations(
            ExternalOperationDependencies(
                record_dependencies=record_dependencies,
                environment=env,
            )
        ),
        build_local_state_operations(
            LocalStateOperationDependencies(
                storage_resolver=storage_resolver,
                token_secret=operation_secret,
            )
        ),
        build_rule_trigger_operations(
            RuleTriggerOperationDependencies(
                storage_resolver=storage_resolver,
                recall_service=recall,
                cursor_secret=operation_secret,
            )
        ),
        build_discovery_operations(discovery_dependencies),
        build_graph_operations(graph_dependencies),
        build_relationship_operations(relationship_dependencies),
        build_utility_operations(utility_dependencies),
        build_maintenance_operations(maintenance_dependencies),
        build_intelligence_operations(intelligence_dependencies),
        build_code_entity_operations(code_entity_dependencies),
        build_federation_operations(federation_dependencies),
        build_consolidation_operations(consolidation_dependencies),
    )
    surface = build_v7_surface(
        pinned_dependencies=pinned,
        operations=operations,
        warning_reader=resource_readers.warning_reader,
        failure_reader=resource_readers.failure_reader,
        rule_reader=resource_readers.rule_reader,
        active_context_reader=resource_readers.active_context_reader,
        transport_mode=transport_mode,
        process_principal=_local_authority_principal(loaded_settings),
        allow_unauthenticated_loopback=(
            transport_mode == "streamable-http" and auth is None
        ),
        activity_callback=dreaming.record_activity,
    )
    if tasks_enabled:
        assert task_redis_url is not None
        task_dispatcher = DurableTaskDispatcher(
            database_path=(
                Path(loaded_settings.get_storage_path()) / "v7-task-dispatcher.sqlite3"
            ),
            redis_url=task_redis_url,
            manifest=surface.manifest,
            covenant_gate=gate,
            workspace_resolver=registry,
        )
    return _ProductionAssembly(
        surface=surface,
        auth=auth,
        tasks_enabled=tasks_enabled,
        task_dispatcher=task_dispatcher,
        sync_timeout_seconds=loaded_settings.sync_timeout_seconds,
        edit_broker=edit_broker,
        capture_candidates=capture_candidates,
        edit_bridge_service=edit_bridge_service,
        services=(
            WorkspaceBootstrapLifecycle(tuple(workspaces.values())),
            writer,
            recall,
            runtime_health,
            health,
            dreaming,
            discovery_dependencies,
            graph_dependencies,
            relationship_dependencies,
            utility_dependencies,
            maintenance_dependencies,
            intelligence_dependencies,
            code_entity_dependencies,
            federation_dependencies,
            consolidation_dependencies,
            _ProjectionLifecycle(
                tuple(workspaces.values()),
                loaded_settings,
                capability_statuses,
            ),
            *(() if edit_bridge_service is None else (edit_bridge_service,)),
        )
        + (() if task_dispatcher is None else (task_dispatcher,)),
    )


def build_production_surface(
    transport_mode: str,
    *,
    host: str | None = None,
    settings: Settings | None = None,
    environ: Mapping[str, str] | None = None,
) -> V7Surface:
    """Build the inspectable production surface without registering FastMCP."""

    return _assemble(
        transport_mode,
        host=host,
        settings=settings,
        environ=environ,
    ).surface


def create_v7_server(
    transport_mode: str,
    *,
    host: str | None = None,
    settings: Settings | None = None,
    environ: Mapping[str, str] | None = None,
) -> Any:
    """Create one fresh production FastMCP server from the v7 manifest."""

    assembly = _assemble(
        transport_mode,
        host=host,
        settings=settings,
        environ=environ,
    )
    server = assembly.surface.build_server(
        auth=assembly.auth,
        tasks_enabled=assembly.tasks_enabled,
        task_dispatcher=assembly.task_dispatcher,
        lifespan=_runtime_lifespan(assembly.services),
        sync_timeout_seconds=assembly.sync_timeout_seconds,
    )
    with suppress(AttributeError, TypeError):
        server._daem0nmcp_v7_services = assembly.services
    return server


__all__ = [
    "ProductionConfigurationError",
    "build_production_surface",
    "create_v7_server",
]
