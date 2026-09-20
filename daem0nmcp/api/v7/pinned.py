"""Policy-first, framework-neutral handlers for the six pinned v7 tools."""

from __future__ import annotations

import inspect
import os
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from types import MappingProxyType
from typing import Any, Protocol

from ...covenant import (
    ArgumentNormalizationError,
    CovenantGate,
    InvocationScope,
    invocation_scope_var,
)
from ...event_store import AppendedEvent, EventStreamConflict
from ...retrieval import RetrievalQuery
from ...workspace import Workspace
from .errors import MIGRATION_REQUIRED_MESSAGE, ErrorCode
from .models import ApiResponse, CapabilityState, RecordSummary, RetrievalData
from .responses import ResponseContext, ResponseFactory
from .tasks import (
    durable_task_execution_var,
    is_durable_task_execution,
    task_admission_only_var,
)
from .tools import (
    HealthData,
    MemoryPreflightInput,
    MemoryPreflightOutput,
    MemoryRecallInput,
    MemoryRecallOutput,
    MemoryRecordOutcomeInput,
    MemoryRecordOutcomeOutput,
    MemoryStoreData,
    MemoryStoreInput,
    MemoryStoreOutput,
    OutcomeData,
    PreflightData,
    PreflightGuidance,
    SessionBriefData,
    SessionBriefInput,
    SessionBriefOutput,
    SystemHealthInput,
    SystemHealthOutput,
)

PINNED_HANDLER_NAMES = frozenset(
    {
        "session_brief",
        "memory_preflight",
        "memory_recall",
        "memory_store",
        "memory_record_outcome",
        "system_health",
    }
)


class IdempotencyConflictError(RuntimeError):
    """A key was already bound to a different canonical mutation request."""

    code = ErrorCode.IDEMPOTENCY_CONFLICT.value


IdempotencyConflict = IdempotencyConflictError


class FederationAuthorizationError(RuntimeError):
    """A linked workspace failed independent invocation authorization."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


_EXPECTED_SERVICE_ERRORS = MappingProxyType(
    {
        "NOT_FOUND": (
            ErrorCode.NOT_FOUND,
            "The requested object was not found.",
            False,
        ),
        "INVALID_ARGUMENT": (
            ErrorCode.INVALID_ARGUMENT,
            "The request is invalid.",
            False,
        ),
        "INVALID_TIMESTAMP": (
            ErrorCode.INVALID_ARGUMENT,
            "The timestamp is invalid.",
            False,
        ),
        "INVALID_WORKSPACE": (
            ErrorCode.UNAUTHORIZED_WORKSPACE,
            "The workspace is unavailable.",
            False,
        ),
        "UNAUTHORIZED_WORKSPACE": (
            ErrorCode.UNAUTHORIZED_WORKSPACE,
            "The workspace is unavailable.",
            False,
        ),
        "COMMUNION_REQUIRED": (
            ErrorCode.COMMUNION_REQUIRED,
            "A session briefing is required for this workspace.",
            False,
        ),
        "ACTIVE_V7_UNAVAILABLE": (
            ErrorCode.CAPABILITY_DEGRADED,
            "The active v7 workspace is unavailable.",
            True,
        ),
        "MIGRATION_REQUIRED": (
            ErrorCode.MIGRATION_REQUIRED,
            MIGRATION_REQUIRED_MESSAGE,
            False,
        ),
        "FEDERATION_UNAVAILABLE": (
            ErrorCode.CAPABILITY_DISABLED,
            "Linked-workspace retrieval is unavailable.",
            False,
        ),
        "RETRIEVAL_UNAVAILABLE": (
            ErrorCode.CAPABILITY_DEGRADED,
            "Retrieval is temporarily unavailable.",
            True,
        ),
        "CLOCK_UNAVAILABLE": (
            ErrorCode.CAPABILITY_DEGRADED,
            "The service clock is unavailable.",
            True,
        ),
        "CAPABILITY_DEGRADED": (
            ErrorCode.CAPABILITY_DEGRADED,
            "The requested capability is temporarily unavailable.",
            True,
        ),
        "DATABASE_IN_USE": (
            ErrorCode.DATABASE_IN_USE,
            "The workspace database is currently in use.",
            True,
        ),
        "TASK_REQUIRED": (
            ErrorCode.TASK_REQUIRED,
            "This operation requires background task support.",
            False,
        ),
    }
)


def _expected_service_failure(
    response: ResponseContext,
    error: BaseException,
) -> ApiResponse[Any] | None:
    error_code = getattr(error, "code", None)
    if not isinstance(error_code, str):
        return None
    mapped = _EXPECTED_SERVICE_ERRORS.get(error_code)
    if mapped is None:
        return None
    code, message, retryable = mapped
    return response.failure(code, message, retryable=retryable)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class PinnedWorkspaceResolver(Protocol):
    def resolve(self, workspace_id: str) -> Workspace | Awaitable[Workspace]: ...


class ArgumentNormalizer(Protocol):
    def __call__(
        self,
        operation: str,
        arguments: Mapping[str, Any] | None,
        workspace: str,
    ) -> dict[str, Any]: ...


class BriefingService(Protocol):
    def assemble(
        self,
        workspace: Workspace,
        request: SessionBriefInput,
    ) -> object | Awaitable[object]: ...


class PreflightService(Protocol):
    def guidance(
        self,
        workspace: Workspace,
        target_tool: str,
        normalized_arguments: Mapping[str, Any],
        description: str | None,
    ) -> object | Awaitable[object]: ...


class RecallService(Protocol):
    def retrieve(
        self,
        workspace: Workspace,
        query: RetrievalQuery,
        linked_workspace_ids: frozenset[str],
        federation_authorizer: Callable[[Workspace], object | Awaitable[object]]
        | None = None,
    ) -> object | Awaitable[object]: ...


class HealthService(Protocol):
    def inspect(
        self,
        workspace: Workspace | None,
        include_components: bool,
    ) -> object | Awaitable[object]: ...


@dataclass(frozen=True, slots=True)
class PinnedDependencies:
    """Runtime services needed by the pinned, framework-neutral handlers."""

    workspace_resolver: PinnedWorkspaceResolver
    covenant_gate: CovenantGate
    argument_normalizer: ArgumentNormalizer
    briefing_service: BriefingService
    preflight_service: PreflightService
    recall_service: RecallService
    memory_event_writer: MemoryEventWriter
    health_service: HealthService
    response_factory: ResponseFactory = field(default_factory=ResponseFactory)
    scope_provider: Callable[[], InvocationScope | None] = invocation_scope_var.get
    clock: Callable[[], datetime] = _utc_now


@dataclass(frozen=True, slots=True)
class MemoryStoreCommand:
    """Token-free canonical input for an injected Task 7 event writer."""

    record_type: str
    content: str
    rationale: str | None
    context: Mapping[str, Any]
    tags: tuple[str, ...]
    relative_file_path: str | None
    happened_at: datetime | None
    procedure_steps: tuple[str, ...]
    idempotency_key: str


@dataclass(frozen=True, slots=True)
class StoredMemory:
    """Task 7 append receipt plus the projected public record summary."""

    record: RecordSummary
    event: AppendedEvent
    idempotent_replay: bool


@dataclass(frozen=True, slots=True)
class MemoryOutcomeCommand:
    """Opaque, token-free input for one outcome append."""

    record_id: str
    outcome_text: str
    worked: bool
    happened_at: datetime | None
    idempotency_key: str


@dataclass(frozen=True, slots=True)
class RecordedOutcome:
    """Task 7 outcome append receipt mapped to the public result."""

    record_id: str
    event: AppendedEvent
    worked: bool
    idempotent_replay: bool


class MemoryEventWriter(Protocol):
    def store(
        self,
        workspace: Workspace,
        command: MemoryStoreCommand,
    ) -> StoredMemory | Awaitable[StoredMemory]: ...

    def record_outcome(
        self,
        workspace: Workspace,
        command: MemoryOutcomeCommand,
    ) -> RecordedOutcome | Awaitable[RecordedOutcome]: ...


class PinnedHandlers:
    """The six business handlers bound to one immutable dependency set."""

    def __init__(self, dependencies: PinnedDependencies) -> None:
        self._dependencies = dependencies

    async def _resolve_workspace(
        self,
        workspace_id: str,
        response: ResponseContext,
    ) -> tuple[Workspace | None, ApiResponse[Any] | None]:
        try:
            workspace_value = self._dependencies.workspace_resolver.resolve(
                workspace_id
            )
            workspace = (
                await workspace_value
                if inspect.isawaitable(workspace_value)
                else workspace_value
            )
        except Exception:
            return None, response.failure(
                ErrorCode.UNAUTHORIZED_WORKSPACE,
                "The requested workspace is unavailable.",
            )
        if (
            not isinstance(workspace, Workspace)
            or workspace.workspace_id != workspace_id
        ):
            return None, response.internal_error()
        return workspace, None

    def _scope_for_workspace(
        self,
        workspace: Workspace,
        response: ResponseContext,
    ) -> tuple[InvocationScope | None, ApiResponse[Any] | None]:
        scope = self._dependencies.scope_provider()
        if scope is None:
            return None, response.failure(
                ErrorCode.IDENTITY_UNAVAILABLE,
                "A transport-derived invocation identity is required.",
            )
        try:
            canonical_root = os.path.normcase(str(workspace.root.resolve()))
        except (OSError, RuntimeError, ValueError):
            return None, response.internal_error()
        if scope.canonical_workspace != canonical_root:
            return None, response.failure(
                ErrorCode.TOKEN_SCOPE_MISMATCH,
                "The invocation scope does not match the requested workspace.",
            )
        if not self._dependencies.covenant_gate.workspace_authorized(scope):
            return None, response.failure(
                ErrorCode.UNAUTHORIZED_WORKSPACE,
                "The requested workspace is unavailable.",
            )
        return scope, None

    async def session_brief(
        self,
        workspace_id: str,
        focus_areas: list[str] | None = None,
        warning_limit: int = 10,
        failure_limit: int = 10,
    ) -> SessionBriefOutput:
        payload: dict[str, object] = {
            "workspace_id": workspace_id,
            "warning_limit": warning_limit,
            "failure_limit": failure_limit,
        }
        if focus_areas is not None:
            payload["focus_areas"] = focus_areas
        request = SessionBriefInput.model_validate(payload)
        response = self._dependencies.response_factory.begin(request.workspace_id)

        workspace, failure = await self._resolve_workspace(
            request.workspace_id,
            response,
        )
        if failure is not None:
            return failure
        assert workspace is not None
        scope, failure = self._scope_for_workspace(workspace, response)
        if failure is not None:
            return failure
        assert scope is not None

        violation = self._dependencies.covenant_gate.authorize(
            "session_brief",
            request.model_dump(),
            scope,
        )
        if violation is not None:
            return response.failure(
                ErrorCode.UNAUTHORIZED_WORKSPACE,
                "The requested workspace is unavailable.",
            )

        try:
            assembled_value = self._dependencies.briefing_service.assemble(
                workspace,
                request,
            )
            assembled = (
                await assembled_value
                if inspect.isawaitable(assembled_value)
                else assembled_value
            )
            data = SessionBriefData.model_validate(assembled)
            if data.workspace_id != request.workspace_id:
                raise ValueError("briefing service returned a mismatched workspace")
            result = response.success(data)
            if result.ok:
                self._dependencies.covenant_gate.record_briefing(scope)
            return result
        except Exception as exc:
            return _expected_service_failure(response, exc) or response.internal_error(
                exc
            )

    async def memory_preflight(
        self,
        workspace_id: str,
        target_tool: str,
        target_arguments: dict[str, Any],
        description: str | None = None,
    ) -> MemoryPreflightOutput:
        request = MemoryPreflightInput.model_validate(
            {
                "workspace_id": workspace_id,
                "target_tool": target_tool,
                "target_arguments": target_arguments,
                "description": description,
            }
        )
        response = self._dependencies.response_factory.begin(request.workspace_id)
        workspace, failure = await self._resolve_workspace(
            request.workspace_id,
            response,
        )
        if failure is not None:
            return failure
        assert workspace is not None
        scope, failure = self._scope_for_workspace(workspace, response)
        if failure is not None:
            return failure
        assert scope is not None
        call_arguments = request.model_dump()
        violation = self._dependencies.covenant_gate.authorize(
            "memory_preflight",
            call_arguments,
            scope,
        )
        if violation is not None:
            code = violation.get("violation")
            if code == ErrorCode.COMMUNION_REQUIRED.value:
                return response.failure(
                    ErrorCode.COMMUNION_REQUIRED,
                    "A session briefing is required for this workspace.",
                    remedy_tool="session_brief",
                    remedy_arguments={"workspace_id": request.workspace_id},
                )
            try:
                stable_code = ErrorCode(code)
            except (TypeError, ValueError):
                return response.internal_error()
            return response.failure(
                stable_code,
                "The Covenant admission request was rejected.",
            )

        if {"workspace_id", "preflight_token"} & set(request.target_arguments):
            return response.failure(
                ErrorCode.INVALID_ARGUMENT,
                "Target arguments cannot contain workspace_id or preflight_token.",
            )
        target_call_arguments = {
            "workspace_id": request.workspace_id,
            **request.target_arguments,
        }
        target_is_complete = True
        try:
            normalized_arguments = self._dependencies.argument_normalizer(
                request.target_tool,
                target_call_arguments,
                scope.canonical_workspace,
            )
        except ArgumentNormalizationError:
            if request.description is None:
                return response.failure(
                    ErrorCode.INVALID_ARGUMENT,
                    "Target arguments do not match the target tool schema.",
                )
            # Description-only counsel is useful while a protected mutation is
            # still being planned.  The unissuable draft stays strict JSON and
            # can never become capability material until full normalization
            # succeeds.
            target_is_complete = False
            normalized_arguments = dict(request.target_arguments)
        try:
            guidance_value = self._dependencies.preflight_service.guidance(
                workspace,
                request.target_tool,
                normalized_arguments,
                request.description,
            )
            guidance_result = (
                await guidance_value
                if inspect.isawaitable(guidance_value)
                else guidance_value
            )
            guidance = PreflightGuidance.model_validate(guidance_result)
            if not target_is_complete:
                return response.success(
                    PreflightData(
                        guidance=guidance,
                        preflight_token=None,
                        target_tool=request.target_tool,
                        expires_at=None,
                    ),
                )
            token = self._dependencies.covenant_gate.issue_preflight(
                scope,
                request.target_tool,
                target_call_arguments,
            )
            claims = self._dependencies.covenant_gate.authority.verify(token)
            expires_at = datetime.fromtimestamp(claims["exp"], timezone.utc)
            data = PreflightData(
                guidance=guidance,
                preflight_token=token,
                target_tool=request.target_tool,
                expires_at=expires_at,
            )
            return response.success(data)
        except Exception as exc:
            return _expected_service_failure(response, exc) or response.internal_error(
                exc
            )

    async def memory_recall(
        self,
        workspace_id: str,
        query: str,
        limit: int = 10,
        candidate_limit: int = 50,
        categories: set[str] | None = None,
        tags: set[str] | None = None,
        record_ids: set[str] | None = None,
        linked_workspace_ids: set[str] | None = None,
        as_of_valid_time: datetime | None = None,
        as_of_transaction_time: datetime | None = None,
        include_invalidated: bool = False,
        include_archived: bool = False,
        token_budget: int = 2400,
        rerank: bool = False,
    ) -> MemoryRecallOutput:
        payload: dict[str, object] = {
            "workspace_id": workspace_id,
            "query": query,
            "limit": limit,
            "candidate_limit": candidate_limit,
            "categories": categories,
            "tags": tags,
            "record_ids": record_ids,
            "as_of_valid_time": as_of_valid_time,
            "as_of_transaction_time": as_of_transaction_time,
            "include_invalidated": include_invalidated,
            "include_archived": include_archived,
            "token_budget": token_budget,
            "rerank": rerank,
        }
        if linked_workspace_ids is not None:
            payload["linked_workspace_ids"] = linked_workspace_ids
        request = MemoryRecallInput.model_validate(payload)
        response = self._dependencies.response_factory.begin(request.workspace_id)
        workspace, failure = await self._resolve_workspace(
            request.workspace_id,
            response,
        )
        if failure is not None:
            return failure
        assert workspace is not None
        effective = request.model_dump(mode="json")
        durable_execution = is_durable_task_execution(
            "memory_recall",
            effective,
        )
        scope: InvocationScope | None
        if durable_execution:
            execution = durable_task_execution_var.get()
            if (
                execution is None
                or not execution.principal_id
                or not execution.transport_session_id
            ):
                return response.failure(
                    ErrorCode.COMMUNION_REQUIRED,
                    "A renewed session briefing is required for this task.",
                    remedy_tool="session_brief",
                    remedy_arguments={"workspace_id": request.workspace_id},
                )
            scope = InvocationScope(
                execution.principal_id,
                execution.transport_session_id,
                str(workspace.root),
            )
        else:
            scope, failure = self._scope_for_workspace(workspace, response)
            if failure is not None:
                return failure
            assert scope is not None
        violation = self._dependencies.covenant_gate.authorize(
            "memory_recall",
            effective,
            scope,
        )
        if violation is not None:
            code = violation.get("violation")
            if code == ErrorCode.COMMUNION_REQUIRED.value:
                return response.failure(
                    ErrorCode.COMMUNION_REQUIRED,
                    "A session briefing is required for this workspace.",
                    remedy_tool="session_brief",
                    remedy_arguments={"workspace_id": request.workspace_id},
                )
            return response.internal_error()
        if task_admission_only_var.get() and not durable_execution:
            return response.failure(
                ErrorCode.TASKS_UNAVAILABLE,
                "Task execution is unavailable.",
                capability_states=(
                    CapabilityState(
                        name="tasks",
                        status="disabled",
                        reason_code="TASKS_UNAVAILABLE",
                        remediation=("Use a reviewed synchronous fallback profile."),
                    ),
                ),
            )
        try:
            retrieval_query = RetrievalQuery(
                workspace_id=request.workspace_id,
                text=request.query,
                limit=request.limit,
                candidate_limit=request.candidate_limit,
                categories=(
                    None
                    if request.categories is None
                    else frozenset(request.categories)
                ),
                tags=None if request.tags is None else frozenset(request.tags),
                record_ids=(
                    None
                    if request.record_ids is None
                    else frozenset(request.record_ids)
                ),
                as_of_valid_time=request.as_of_valid_time,
                as_of_transaction_time=request.as_of_transaction_time,
                include_invalidated=request.include_invalidated,
                include_archived=request.include_archived,
                token_budget=request.token_budget,
                rerank=request.rerank,
            )
            linked_ids = frozenset(request.linked_workspace_ids)
            if linked_ids:

                async def authorize_linked(selected: Workspace) -> None:
                    try:
                        selected_scope = InvocationScope(
                            scope.principal_id,
                            scope.transport_session_id,
                            str(selected.root),
                        )
                        selected_request = request.model_copy(
                            update={
                                "workspace_id": selected.workspace_id,
                                "linked_workspace_ids": set(),
                            }
                        )
                        selected_violation = self._dependencies.covenant_gate.authorize(
                            "memory_recall",
                            selected_request.model_dump(),
                            selected_scope,
                        )
                    except Exception:
                        raise FederationAuthorizationError(
                            "UNAUTHORIZED_WORKSPACE"
                        ) from None
                    if selected_violation is None:
                        return
                    if (
                        selected_violation.get("violation")
                        == ErrorCode.COMMUNION_REQUIRED.value
                    ):
                        raise FederationAuthorizationError("COMMUNION_REQUIRED")
                    raise FederationAuthorizationError("UNAUTHORIZED_WORKSPACE")

                result_value = self._dependencies.recall_service.retrieve(
                    workspace,
                    retrieval_query,
                    linked_ids,
                    authorize_linked,
                )
            else:
                result_value = self._dependencies.recall_service.retrieve(
                    workspace,
                    retrieval_query,
                    linked_ids,
                )
            result = (
                await result_value
                if inspect.isawaitable(result_value)
                else result_value
            )
            return response.success(
                RetrievalData.model_validate(result),
            )
        except Exception as exc:
            return _expected_service_failure(response, exc) or response.internal_error(
                exc
            )

    async def memory_store(
        self,
        *,
        workspace_id: str,
        record_type: str,
        content: str,
        rationale: str | None = None,
        context: dict[str, Any] | None = None,
        tags: list[str] | None = None,
        relative_file_path: str | None = None,
        happened_at: datetime | None = None,
        procedure_steps: list[str] | None = None,
        idempotency_key: str,
        preflight_token: str,
    ) -> MemoryStoreOutput:
        payload: dict[str, object] = {
            "workspace_id": workspace_id,
            "record_type": record_type,
            "content": content,
            "rationale": rationale,
            "relative_file_path": relative_file_path,
            "happened_at": happened_at,
            "idempotency_key": idempotency_key,
            "preflight_token": preflight_token,
        }
        if context is not None:
            payload["context"] = context
        if tags is not None:
            payload["tags"] = tags
        if procedure_steps is not None:
            payload["procedure_steps"] = procedure_steps
        request = MemoryStoreInput.model_validate(payload)
        response = self._dependencies.response_factory.begin(request.workspace_id)
        workspace, failure = await self._resolve_workspace(
            request.workspace_id,
            response,
        )
        if failure is not None:
            return failure
        assert workspace is not None
        scope, failure = self._scope_for_workspace(workspace, response)
        if failure is not None:
            return failure
        assert scope is not None
        violation = self._dependencies.covenant_gate.authorize(
            "memory_store",
            request.model_dump(),
            scope,
            preflight_token=request.preflight_token,
        )
        if violation is not None:
            code = violation.get("violation")
            try:
                stable_code = ErrorCode(code)
            except (TypeError, ValueError):
                return response.internal_error()
            remedy_tool = None
            remedy_arguments = None
            if stable_code in {
                ErrorCode.COUNSEL_REQUIRED,
                ErrorCode.TOKEN_MISSING,
                ErrorCode.TOKEN_TAMPERED,
                ErrorCode.TOKEN_EXPIRED,
                ErrorCode.TOKEN_SCOPE_MISMATCH,
                ErrorCode.TOKEN_OPERATION_MISMATCH,
                ErrorCode.TOKEN_ARGUMENT_MISMATCH,
                ErrorCode.TOKEN_REPLAYED,
                ErrorCode.TOKEN_LEGACY_UNSUPPORTED,
            }:
                remedy_tool = "memory_preflight"
                remedy_arguments = {
                    "workspace_id": request.workspace_id,
                    "target_tool": "memory_store",
                    "target_arguments": request.model_dump(
                        mode="json",
                        exclude={"workspace_id", "preflight_token"},
                    ),
                }
            return response.failure(
                stable_code,
                "The preflight capability was rejected.",
                remedy_tool=remedy_tool,
                remedy_arguments=remedy_arguments,
            )
        command = MemoryStoreCommand(
            record_type=request.record_type,
            content=request.content,
            rationale=request.rationale,
            context=MappingProxyType(dict(request.context)),
            tags=tuple(request.tags),
            relative_file_path=request.relative_file_path,
            happened_at=request.happened_at,
            procedure_steps=tuple(request.procedure_steps),
            idempotency_key=request.idempotency_key,
        )
        try:
            stored_value = self._dependencies.memory_event_writer.store(
                workspace,
                command,
            )
            stored = (
                await stored_value
                if inspect.isawaitable(stored_value)
                else stored_value
            )
            if not isinstance(stored, StoredMemory):
                raise TypeError("memory writer returned an invalid store result")
            data = MemoryStoreData(
                record=stored.record,
                event_id=stored.event.event_id,
                stream_version=stored.event.stream_version,
                idempotent_replay=stored.idempotent_replay,
            )
            return response.success(data)
        except IdempotencyConflict:
            return response.failure(
                ErrorCode.IDEMPOTENCY_CONFLICT,
                "The idempotency key is already bound to a different request.",
            )
        except EventStreamConflict:
            return response.failure(
                ErrorCode.EVENT_STREAM_CONFLICT,
                "The memory stream changed before the event could be appended.",
                retryable=True,
            )
        except Exception as exc:
            return _expected_service_failure(response, exc) or response.internal_error(
                exc
            )

    async def memory_record_outcome(
        self,
        *,
        workspace_id: str,
        record_id: str,
        outcome_text: str,
        worked: bool,
        happened_at: datetime | None = None,
        idempotency_key: str,
    ) -> MemoryRecordOutcomeOutput:
        request = MemoryRecordOutcomeInput.model_validate(
            {
                "workspace_id": workspace_id,
                "record_id": record_id,
                "outcome_text": outcome_text,
                "worked": worked,
                "happened_at": happened_at,
                "idempotency_key": idempotency_key,
            }
        )
        response = self._dependencies.response_factory.begin(request.workspace_id)
        workspace, failure = await self._resolve_workspace(
            request.workspace_id,
            response,
        )
        if failure is not None:
            return failure
        assert workspace is not None
        scope, failure = self._scope_for_workspace(workspace, response)
        if failure is not None:
            return failure
        assert scope is not None
        violation = self._dependencies.covenant_gate.authorize(
            "memory_record_outcome",
            request.model_dump(),
            scope,
        )
        if violation is not None:
            code = violation.get("violation")
            if code == ErrorCode.COMMUNION_REQUIRED.value:
                return response.failure(
                    ErrorCode.COMMUNION_REQUIRED,
                    "A session briefing is required for this workspace.",
                    remedy_tool="session_brief",
                    remedy_arguments={"workspace_id": request.workspace_id},
                )
            return response.internal_error()
        command = MemoryOutcomeCommand(
            record_id=request.record_id,
            outcome_text=request.outcome_text,
            worked=request.worked,
            happened_at=request.happened_at,
            idempotency_key=request.idempotency_key,
        )
        try:
            recorded_value = self._dependencies.memory_event_writer.record_outcome(
                workspace,
                command,
            )
            recorded = (
                await recorded_value
                if inspect.isawaitable(recorded_value)
                else recorded_value
            )
            if not isinstance(recorded, RecordedOutcome):
                raise TypeError("memory writer returned an invalid outcome result")
            if (
                recorded.record_id != request.record_id
                or recorded.worked is not request.worked
            ):
                raise ValueError("outcome writer changed the admitted mutation")
            data = OutcomeData(
                record_id=recorded.record_id,
                outcome_event_id=recorded.event.event_id,
                stream_version=recorded.event.stream_version,
                worked=recorded.worked,
                idempotent_replay=recorded.idempotent_replay,
            )
            return response.success(data)
        except IdempotencyConflict:
            return response.failure(
                ErrorCode.IDEMPOTENCY_CONFLICT,
                "The idempotency key is already bound to a different request.",
            )
        except EventStreamConflict:
            return response.failure(
                ErrorCode.EVENT_STREAM_CONFLICT,
                "The memory stream changed before the event could be appended.",
                retryable=True,
            )
        except Exception as exc:
            return _expected_service_failure(response, exc) or response.internal_error(
                exc
            )

    async def system_health(
        self,
        workspace_id: str | None = None,
        include_components: bool = True,
    ) -> SystemHealthOutput:
        request = SystemHealthInput.model_validate(
            {
                "workspace_id": workspace_id,
                "include_components": include_components,
            }
        )
        response = self._dependencies.response_factory.begin(request.workspace_id)
        workspace: Workspace | None = None
        if request.workspace_id is not None:
            workspace, failure = await self._resolve_workspace(
                request.workspace_id,
                response,
            )
            if failure is not None:
                return failure
            assert workspace is not None
            _, failure = self._scope_for_workspace(workspace, response)
            if failure is not None:
                return failure
        violation = self._dependencies.covenant_gate.authorize(
            "system_health",
            request.model_dump(),
            self._dependencies.scope_provider(),
        )
        if violation is not None:
            return response.failure(
                ErrorCode.UNAUTHORIZED_WORKSPACE,
                "The requested workspace is unavailable.",
            )
        try:
            health_value = self._dependencies.health_service.inspect(
                workspace,
                request.include_components,
            )
            health = (
                await health_value
                if inspect.isawaitable(health_value)
                else health_value
            )
            data = HealthData.model_validate(health)
            if workspace is not None:
                _, failure = self._scope_for_workspace(workspace, response)
                if failure is not None:
                    return failure
            if not request.include_components and data.capability_states:
                data = data.model_copy(update={"capability_states": []})
            return response.success(data)
        except Exception as exc:
            return _expected_service_failure(response, exc) or response.internal_error(
                exc
            )


def build_pinned_handlers(
    dependencies: PinnedDependencies,
) -> Mapping[str, Callable[..., object]]:
    """Bind the pinned vertical handlers to injected runtime dependencies."""

    handlers = PinnedHandlers(dependencies)
    PinnedHandlers.memory_recall.__dict__["__daem0nmcp_admission_aware__"] = True
    return MappingProxyType(
        {
            "session_brief": handlers.session_brief,
            "memory_preflight": handlers.memory_preflight,
            "memory_recall": handlers.memory_recall,
            "memory_store": handlers.memory_store,
            "memory_record_outcome": handlers.memory_record_outcome,
            "system_health": handlers.system_health,
        }
    )


__all__ = [
    "ArgumentNormalizer",
    "BriefingService",
    "HealthService",
    "IdempotencyConflict",
    "MemoryEventWriter",
    "MemoryOutcomeCommand",
    "MemoryStoreCommand",
    "PINNED_HANDLER_NAMES",
    "PinnedDependencies",
    "PinnedHandlers",
    "PinnedWorkspaceResolver",
    "PreflightService",
    "RecallService",
    "RecordedOutcome",
    "StoredMemory",
    "build_pinned_handlers",
]
