"""Bounded runtime health without record contents, credentials or host paths."""

from __future__ import annotations

import json
import os
import re
from collections.abc import Callable, Mapping
from typing import Any, Protocol

from ...bounded_workers import BoundedWorkerBusyError, BoundedWorkerPool
from ...covenant import InvocationScope
from ...workspace import Workspace
from .runtime_protocols import ActiveStorageResolver
from .runtime_services import _open_database

_PROJECTIONS = ("lexical", "dense", "graph", "temporal", "procedure", "outcome", "code")
_ERROR_CODE = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")


class TaskHealthSource(Protocol):
    @property
    def is_ready(self) -> bool: ...
    def scoped_health_counts(self, scope: InvocationScope) -> dict[str, int]: ...


class BridgeHealthSource(Protocol):
    @property
    def is_ready(self) -> bool: ...


class RuntimeHealthDiagnostics:
    def __init__(
        self,
        *,
        storage_resolver: ActiveStorageResolver,
        capability_statuses: Mapping[str, str],
        scope_provider: Callable[[], InvocationScope | None],
        workspace_authorizer: Callable[[InvocationScope], bool],
        task_provider: Callable[[], TaskHealthSource | None],
        bridge_provider: Callable[[], BridgeHealthSource | None],
    ) -> None:
        self._storage = storage_resolver
        self._capabilities = dict(capability_statuses)
        self._scope = scope_provider
        self._authorize = workspace_authorizer
        self._tasks = task_provider
        self._bridge = bridge_provider
        self._workers = BoundedWorkerPool(
            max_workers=2, thread_name_prefix="daem0nmcp-health"
        )

    def close(self) -> None:
        self._workers.shutdown()

    async def inspect(self, workspace: Workspace | None) -> list[Mapping[str, Any]]:
        scope = self._scope()
        if workspace is not None and (
            scope is None
            or scope.canonical_workspace != os.path.normcase(str(workspace.root))
            or not self._authorize(scope)
        ):
            return []
        try:
            result = await self._workers.run(
                lambda: self._inspect_sync(workspace, scope)
            )
            if (
                workspace is not None
                and scope is not None
                and not self._authorize(scope)
            ):
                return []
            return result
        except BoundedWorkerBusyError:
            return [
                {
                    "component": "migration",
                    "status": "degraded",
                    "stable_error_code": "HEALTH_CAPACITY_UNAVAILABLE",
                }
            ]

    def _inspect_sync(
        self, workspace: Workspace | None, scope: InvocationScope | None
    ) -> list[Mapping[str, Any]]:
        result: list[Mapping[str, Any]] = []
        task = self._tasks()
        task_ready = task is not None and task.is_ready
        task_row: dict[str, Any] = {
            "component": "tasks",
            "status": "disabled"
            if task is None
            else "ready"
            if task_ready
            else "degraded",
        }
        if task is not None:
            if not task_ready:
                task_row["stable_error_code"] = "TASK_RUNTIME_UNAVAILABLE"
            if workspace is not None and scope is not None:
                try:
                    task_row["counts"] = task.scoped_health_counts(scope)
                except Exception:
                    task_row = {
                        "component": "tasks",
                        "status": "degraded",
                        "stable_error_code": "TASK_DIAGNOSTICS_UNAVAILABLE",
                    }
        result.append(task_row)
        bridge = self._bridge()
        bridge_ready = bridge is not None and bridge.is_ready
        result.append(
            {
                "component": "edit_bridge",
                "status": "disabled"
                if bridge is None
                else "ready"
                if bridge_ready
                else "degraded",
                "stable_error_code": "EDIT_BRIDGE_UNAVAILABLE"
                if bridge is not None and not bridge_ready
                else None,
            }
        )
        if workspace is not None:
            try:
                result.extend(self._projections(workspace))
            except Exception:
                result.append(
                    {
                        "component": "migration",
                        "status": "degraded",
                        "stable_error_code": "STORAGE_DIAGNOSTICS_UNAVAILABLE",
                    }
                )
        return result

    def _enabled(self, projection: str) -> bool:
        required = {
            "dense": ("local", "models-local"),
            "graph": ("graph",),
            "code": ("apps",),
        }.get(projection, ())
        return all(self._capabilities.get(name) == "ready" for name in required)

    def _projections(self, workspace: Workspace) -> list[Mapping[str, Any]]:
        with self._storage.locked_active(workspace) as active:
            connection = _open_database(active.path, writable=False)
            try:
                connection.execute("BEGIN")
                event_count = int(
                    connection.execute(
                        "SELECT COUNT(*) FROM memory_events WHERE workspace_id=?",
                        (workspace.workspace_id,),
                    ).fetchone()[0]
                )
                manifests = {
                    str(row[0]): row
                    for row in connection.execute(
                        "SELECT projection_name,generation,source_event_count,row_count "
                        "FROM projection_manifests WHERE workspace_id=? AND status='active' LIMIT 8",
                        (workspace.workspace_id,),
                    )
                }
                result: list[Mapping[str, Any]] = []
                for name in _PROJECTIONS:
                    if not self._enabled(name):
                        result.append({"component": name, "status": "disabled"})
                        continue
                    manifest = manifests.get(name)
                    row: dict[str, Any] = {
                        "component": name,
                        "status": "ready" if manifest else "degraded",
                        "counts": {
                            "pending_jobs": 0,
                            "running_jobs": 0,
                            "dead_letters": 0,
                        },
                    }
                    if manifest is not None:
                        row["counts"].update(
                            generation=int(manifest[1]), projected_rows=int(manifest[3])
                        )
                        if name != "code":
                            lag = max(0, event_count - int(manifest[2]))
                            row["counts"]["event_lag"] = lag
                            if lag:
                                row.update(
                                    status="degraded",
                                    stable_error_code="PROJECTION_LAGGING",
                                )
                    else:
                        row["stable_error_code"] = "PROJECTION_NOT_BUILT"
                    job = connection.execute(
                        "SELECT status,substr(last_error_json,1,1024) FROM background_jobs "
                        "WHERE workspace_id=? AND job_type='retrieval.projection_rebuild' "
                        "AND idempotency_key=? LIMIT 1",
                        (workspace.workspace_id, f"active-projection:{name}"),
                    ).fetchone()
                    if job is not None:
                        metric = {
                            "queued": "pending_jobs",
                            "running": "running_jobs",
                            "dead_letter": "dead_letters",
                        }.get(str(job[0]))
                        if metric:
                            row["counts"][metric] = 1
                        if job[0] == "dead_letter":
                            row.update(
                                status="degraded",
                                stable_error_code="PROJECTION_JOB_FAILED",
                            )
                        if isinstance(job[1], str):
                            try:
                                error = json.loads(job[1])
                                code = (
                                    error.get("code")
                                    if isinstance(error, dict)
                                    else None
                                )
                            except (ValueError, TypeError):
                                code = None
                            if isinstance(code, str) and _ERROR_CODE.fullmatch(code):
                                row["stable_error_code"] = code
                    result.append(row)
                # Health's bounded structural inspection cannot certify the
                # complete authoritative history. Never imply a verified store
                # merely because its pointer/schema and projections can be read.
                result.append(
                    {
                        "component": "migration",
                        "status": "not_verified",
                        "counts": {"active_generation": active.generation},
                        "stable_error_code": "OFFLINE_VERIFICATION_REQUIRED",
                    }
                )
                return result
            finally:
                connection.close()
