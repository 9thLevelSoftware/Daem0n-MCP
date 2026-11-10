"""Canonical workspace federation links backed by an immutable v7 ledger.

Only opaque registered workspace identifiers cross this boundary.  The
retained ``project_links`` table is deliberately neither read nor written: it
stores path authority and cannot satisfy the v7 workspace isolation contract.

Cross-workspace consolidation is intentionally absent from this factory.  The
canonical bundle importer rejects workspace rebinding, while the filesystem
has no atomic multi-workspace archive primitive.  A composition root can keep
those two capabilities disabled until both seams are available.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import os
import re
import secrets
import sqlite3
import threading
from collections.abc import Callable, Iterator, Mapping
from contextlib import ExitStack, contextmanager, suppress
from contextvars import copy_context
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from types import MappingProxyType
from typing import Any

from ...bounded_workers import BoundedWorkerBusyError, BoundedWorkerPool
from ...event_store import canonical_json_bytes, sha256_json
from ...schema_version import CURRENT_SCHEMA_VERSION
from ...workspace import Workspace, WorkspaceRegistry
from .application import AdmittedRequest
from .errors import STABLE_ERROR_CODE_SET
from .federated_retrieval import federation_access_lock
from .models import MutationReceipt, Page
from .runtime_protocols import ActiveStorageResolver, WorkerPool, WorkspaceResolver
from .runtime_services import WorkspaceStorageResolver
from .tasks import await_task_terminal
from .tools import WorkspaceLinkView

_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
_CURSOR_RE = re.compile(r"^cur_v1_link_(ws_[0-9a-f]{24})_([0-9a-f]{64})$")
_REQUIRED_TABLES = frozenset({"schema_version", "workspace_link_events"})


class FederationOperationError(RuntimeError):
    """Stable, path-free federation failure understood by the v7 router."""

    def __init__(self, code: str) -> None:
        if code not in STABLE_ERROR_CODE_SET:
            raise ValueError("federation operation error code is not stable")
        self.code = code
        super().__init__(code)


class _WorkerCancelledError(RuntimeError):
    pass


def _default_clock() -> datetime:
    return datetime.now(timezone.utc)


def _default_worker_pool() -> BoundedWorkerPool:
    return BoundedWorkerPool(
        max_workers=4,
        thread_name_prefix="daem0nmcp-v7-federation",
    )


@dataclass(frozen=True, slots=True)
class FederationOperationDependencies:
    """Reviewed dependencies for the canonical workspace-link lifecycle."""

    workspace_resolver: WorkspaceResolver
    storage_resolver: ActiveStorageResolver = field(
        default_factory=WorkspaceStorageResolver
    )
    clock: Callable[[], datetime] = field(default=_default_clock)
    cursor_secret: bytes = field(default_factory=lambda: secrets.token_bytes(32))
    worker_pool: WorkerPool = field(default_factory=_default_worker_pool)
    workspace_authorizer: Callable[[Workspace], bool] | None = None

    def __post_init__(self) -> None:
        if not callable(getattr(self.workspace_resolver, "resolve", None)):
            raise TypeError("workspace_resolver must provide resolve")
        if not callable(getattr(self.storage_resolver, "locked_active", None)):
            raise TypeError("storage_resolver must provide locked_active")
        if not callable(self.clock):
            raise TypeError("clock must be callable")
        if not isinstance(self.cursor_secret, bytes) or len(self.cursor_secret) < 32:
            raise ValueError("cursor_secret must contain at least 32 bytes")
        if not callable(getattr(self.worker_pool, "run", None)) or not callable(
            getattr(self.worker_pool, "shutdown", None)
        ):
            raise TypeError("worker_pool must provide run and shutdown")

    def close(self) -> None:
        """Join and release the dependency-owned bounded worker pool."""

        self.worker_pool.shutdown()


def _exact_workspace(value: object, expected_id: str) -> Workspace:
    if not isinstance(value, Workspace) or value.workspace_id != expected_id:
        raise FederationOperationError("UNAUTHORIZED_WORKSPACE")
    try:
        canonical = value.root.resolve(strict=True)
        registered = WorkspaceRegistry([canonical], default_root=canonical).default
        exact_root = os.path.normcase(str(value.root)) == os.path.normcase(
            str(canonical)
        )
    except (OSError, RuntimeError, TypeError, ValueError):
        raise FederationOperationError("UNAUTHORIZED_WORKSPACE") from None
    if registered.workspace_id != expected_id or not exact_root:
        raise FederationOperationError("UNAUTHORIZED_WORKSPACE")
    return value


def _authorize(
    workspace: Workspace,
    request: AdmittedRequest,
    tool_name: str,
) -> None:
    if (
        not isinstance(request, AdmittedRequest)
        or request.tool_name != tool_name
        or request.workspace_id != getattr(workspace, "workspace_id", None)
    ):
        raise FederationOperationError("UNAUTHORIZED_WORKSPACE")
    _exact_workspace(workspace, request.workspace_id)


def _resolve_linked(
    dependencies: FederationOperationDependencies,
    linked_workspace_id: str,
) -> Workspace:
    try:
        resolved = dependencies.workspace_resolver.resolve(linked_workspace_id)
    except Exception:
        raise FederationOperationError("UNAUTHORIZED_WORKSPACE") from None
    workspace = _exact_workspace(resolved, linked_workspace_id)
    _authorize_workspace_access(dependencies, workspace)
    return workspace


def _authorize_workspace_access(
    dependencies: FederationOperationDependencies, *workspaces: Workspace
) -> None:
    if dependencies.workspace_authorizer is None:
        return
    try:
        allowed = all(
            dependencies.workspace_authorizer(workspace) is True
            for workspace in workspaces
        )
    except Exception:
        allowed = False
    if not allowed:
        raise FederationOperationError("UNAUTHORIZED_WORKSPACE")


def _database_path(workspace: Workspace, active: object) -> Path:
    try:
        root = workspace.root.resolve(strict=True)
        active_path = getattr(active, "path", None)
        if not isinstance(active_path, (str, Path)):
            raise ValueError("active database path is unavailable")
        candidate = Path(active_path)
        if candidate.is_symlink():
            raise ValueError
        resolved = candidate.resolve(strict=True)
        resolved.relative_to(root)
        if not resolved.is_file():
            raise ValueError
        return resolved
    except (OSError, RuntimeError, TypeError, ValueError):
        raise FederationOperationError("WORKSPACE_PATH_ESCAPE") from None


@contextmanager
def _locked_active_databases(
    dependencies: FederationOperationDependencies,
    workspaces: tuple[Workspace, ...],
) -> Iterator[Mapping[str, object]]:
    """Hold shared activation locks in one global opaque-ID order."""

    unique = {workspace.workspace_id: workspace for workspace in workspaces}
    active_by_id: dict[str, object] = {}
    with ExitStack() as stack:
        for workspace_id in sorted(unique):
            active_by_id[workspace_id] = stack.enter_context(
                dependencies.storage_resolver.locked_active(unique[workspace_id])
            )
        yield MappingProxyType(active_by_id)


def _open_database(path: Path, *, writable: bool) -> sqlite3.Connection:
    connection: sqlite3.Connection | None = None
    try:
        connection = sqlite3.connect(
            f"{path.as_uri()}?mode={'rw' if writable else 'ro'}",
            uri=True,
            timeout=5.0,
            isolation_level=None,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=5000")
        connection.execute("PRAGMA foreign_keys=ON")
        if not writable:
            connection.execute("PRAGMA query_only=ON")
        version = connection.execute(
            "SELECT COALESCE(MAX(version),0) FROM schema_version"
        ).fetchone()
        tables = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        if (
            version is None
            or isinstance(version[0], bool)
            or not isinstance(version[0], int)
            or version[0] != CURRENT_SCHEMA_VERSION
            or not _REQUIRED_TABLES.issubset(tables)
        ):
            raise FederationOperationError("CAPABILITY_DEGRADED")
        return connection
    except FederationOperationError:
        if connection is not None:
            connection.close()
        raise
    except Exception:
        if connection is not None:
            connection.close()
        raise FederationOperationError("CAPABILITY_DEGRADED") from None


def _timestamp_us(value: datetime) -> int:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise FederationOperationError("CAPABILITY_DEGRADED")
    try:
        delta = value.astimezone(timezone.utc) - _EPOCH
        result = (delta.days * 86_400 + delta.seconds) * 1_000_000 + delta.microseconds
    except (OverflowError, TypeError, ValueError):
        raise FederationOperationError("CAPABILITY_DEGRADED") from None
    if not 0 <= result <= 2**63 - 1:
        raise FederationOperationError("CAPABILITY_DEGRADED")
    return result


def _raise_if_cancelled(cancelled: threading.Event) -> None:
    if cancelled.is_set():
        raise _WorkerCancelledError()


async def _run_read(
    dependencies: FederationOperationDependencies,
    operation: Callable[[], Any],
) -> Any:
    context = copy_context()
    task = asyncio.create_task(
        dependencies.worker_pool.run(lambda: context.run(operation))
    )
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError as cancellation:
        with suppress(Exception):
            await await_task_terminal(task)
        raise cancellation
    except BoundedWorkerBusyError as exc:
        raise FederationOperationError("TASK_REQUIRED") from exc


async def _run_mutation(
    dependencies: FederationOperationDependencies,
    operation: Callable[[threading.Event], Any],
) -> Any:
    cancelled = threading.Event()
    context = copy_context()
    task = asyncio.create_task(
        dependencies.worker_pool.run(lambda: context.run(operation, cancelled))
    )
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError as cancellation:
        cancelled.set()
        try:
            result = await await_task_terminal(task)
        except (_WorkerCancelledError, BoundedWorkerBusyError):
            raise cancellation from None
        except Exception:
            raise cancellation from None
        return result
    except BoundedWorkerBusyError as exc:
        raise FederationOperationError("TASK_REQUIRED") from exc


def _current_link(
    connection: sqlite3.Connection,
    workspace_id: str,
    linked_workspace_id: str,
) -> sqlite3.Row | None:
    rows = connection.execute(
        "SELECT event_id,workspace_id,linked_workspace_id,stream_version,"
        "event_type,relationship,label,occurred_at_us,recorded_at_us,"
        "previous_event_hash,event_hash "
        "FROM workspace_link_events WHERE workspace_id=? "
        "AND linked_workspace_id=? ORDER BY stream_version LIMIT 10001",
        (workspace_id, linked_workspace_id),
    ).fetchall()
    if len(rows) > 10_000:
        raise FederationOperationError("CAPABILITY_DEGRADED")
    previous_hash: str | None = None
    previous_type: str | None = None
    for expected_version, row in enumerate(rows, 1):
        try:
            if (
                row[1] != workspace_id
                or row[2] != linked_workspace_id
                or row[3] != expected_version
                or row[8] != row[7]
                or row[9] != previous_hash
                or (expected_version == 1 and row[4] != "workspace.linked")
                or (
                    previous_type == "workspace.unlinked"
                    and row[4] == "workspace.unlinked"
                )
            ):
                raise ValueError
            expected = _event_values(
                workspace_id=workspace_id,
                linked_workspace_id=linked_workspace_id,
                stream_version=expected_version,
                event_type=str(row[4]),
                relationship=str(row[5]),
                label=row[6],
                timestamp_us=int(row[7]),
                previous_event_hash=previous_hash,
            )
            if tuple(row) != expected:
                raise ValueError
        except (TypeError, ValueError):
            raise FederationOperationError("CAPABILITY_DEGRADED") from None
        previous_hash = str(row[10])
        previous_type = str(row[4])
    return None if not rows else rows[-1]


def _event_values(
    *,
    workspace_id: str,
    linked_workspace_id: str,
    stream_version: int,
    event_type: str,
    relationship: str,
    label: str | None,
    timestamp_us: int,
    previous_event_hash: str | None,
) -> tuple[object, ...]:
    envelope = {
        "workspace_id": workspace_id,
        "linked_workspace_id": linked_workspace_id,
        "stream_version": stream_version,
        "event_type": event_type,
        "relationship": relationship,
        "label": label,
        "occurred_at_us": timestamp_us,
        "recorded_at_us": timestamp_us,
        "previous_event_hash": previous_event_hash,
    }
    event_hash = sha256_json(envelope)
    return (
        f"evt_{event_hash}",
        workspace_id,
        linked_workspace_id,
        stream_version,
        event_type,
        relationship,
        label,
        timestamp_us,
        timestamp_us,
        previous_event_hash,
        event_hash,
    )


def _insert_event(
    connection: sqlite3.Connection,
    values: tuple[object, ...],
) -> None:
    try:
        connection.execute(
            "INSERT INTO workspace_link_events (event_id,workspace_id,"
            "linked_workspace_id,stream_version,event_type,relationship,label,"
            "occurred_at_us,recorded_at_us,previous_event_hash,event_hash) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            values,
        )
    except sqlite3.IntegrityError as exc:
        raise FederationOperationError("EVENT_STREAM_CONFLICT") from exc


def _link_sync(
    dependencies: FederationOperationDependencies,
    workspace: Workspace,
    request: AdmittedRequest,
    cancelled: threading.Event,
) -> WorkspaceLinkView:
    _raise_if_cancelled(cancelled)
    linked = _resolve_linked(dependencies, request.linked_workspace_id)
    _authorize_workspace_access(dependencies, workspace, linked)
    _raise_if_cancelled(cancelled)
    with _locked_active_databases(dependencies, (workspace, linked)) as active_by_id:
        _raise_if_cancelled(cancelled)
        origin_path = _database_path(workspace, active_by_id[workspace.workspace_id])
        with federation_access_lock(origin_path, "exclusive"):
            linked_connection = _open_database(
                _database_path(linked, active_by_id[linked.workspace_id]),
                writable=False,
            )
            linked_connection.close()
            connection = _open_database(origin_path, writable=True)
            try:
                connection.execute("BEGIN IMMEDIATE")
                _raise_if_cancelled(cancelled)
                current = _current_link(
                    connection,
                    workspace.workspace_id,
                    request.linked_workspace_id,
                )
                if (
                    current is not None
                    and current[4] == "workspace.linked"
                    and current[5] == request.relationship
                    and current[6] == request.label
                ):
                    _raise_if_cancelled(cancelled)
                    connection.commit()
                    return WorkspaceLinkView(
                        workspace_id=workspace.workspace_id,
                        linked_workspace_id=request.linked_workspace_id,
                        relationship=request.relationship,
                        label=request.label,
                    )
                version = 1 if current is None else int(current[3]) + 1
                previous_hash = None if current is None else str(current[10])
                values = _event_values(
                    workspace_id=workspace.workspace_id,
                    linked_workspace_id=request.linked_workspace_id,
                    stream_version=version,
                    event_type="workspace.linked",
                    relationship=request.relationship,
                    label=request.label,
                    timestamp_us=_timestamp_us(dependencies.clock()),
                    previous_event_hash=previous_hash,
                )
                _raise_if_cancelled(cancelled)
                _insert_event(connection, values)
                _raise_if_cancelled(cancelled)
                _authorize_workspace_access(dependencies, workspace, linked)
                connection.commit()
                return WorkspaceLinkView(
                    workspace_id=workspace.workspace_id,
                    linked_workspace_id=request.linked_workspace_id,
                    relationship=request.relationship,
                    label=request.label,
                )
            except FederationOperationError:
                connection.rollback()
                raise
            except sqlite3.Error:
                connection.rollback()
                raise FederationOperationError("CAPABILITY_DEGRADED") from None
            except Exception:
                connection.rollback()
                raise
            finally:
                connection.close()


def _unlink_receipt(
    workspace_id: str,
    linked_workspace_id: str,
    *,
    event_id: str | None,
    replay: bool,
    changed: int,
) -> MutationReceipt:
    return MutationReceipt(
        operation_id="op_"
        + sha256_json(["v7-workspace-unlink", workspace_id, linked_workspace_id]),
        affected_ids=[linked_workspace_id] if event_id is not None else [],
        event_ids=[] if event_id is None else [event_id],
        counts={"unlinked": changed},
        idempotent_replay=replay,
    )


def _unlink_sync(
    dependencies: FederationOperationDependencies,
    workspace: Workspace,
    request: AdmittedRequest,
    cancelled: threading.Event,
) -> MutationReceipt:
    _raise_if_cancelled(cancelled)
    linked = _resolve_linked(dependencies, request.linked_workspace_id)
    _authorize_workspace_access(dependencies, workspace, linked)
    _raise_if_cancelled(cancelled)
    with _locked_active_databases(dependencies, (workspace, linked)) as active_by_id:
        _raise_if_cancelled(cancelled)
        origin_path = _database_path(workspace, active_by_id[workspace.workspace_id])
        with federation_access_lock(origin_path, "exclusive"):
            linked_connection = _open_database(
                _database_path(linked, active_by_id[linked.workspace_id]),
                writable=False,
            )
            linked_connection.close()
            connection = _open_database(origin_path, writable=True)
            try:
                connection.execute("BEGIN IMMEDIATE")
                _raise_if_cancelled(cancelled)
                current = _current_link(
                    connection,
                    workspace.workspace_id,
                    request.linked_workspace_id,
                )
                if current is None:
                    _raise_if_cancelled(cancelled)
                    connection.commit()
                    return _unlink_receipt(
                        workspace.workspace_id,
                        request.linked_workspace_id,
                        event_id=None,
                        replay=True,
                        changed=0,
                    )
                if current[4] == "workspace.unlinked":
                    _raise_if_cancelled(cancelled)
                    connection.commit()
                    return _unlink_receipt(
                        workspace.workspace_id,
                        request.linked_workspace_id,
                        event_id=str(current[0]),
                        replay=True,
                        changed=0,
                    )
                values = _event_values(
                    workspace_id=workspace.workspace_id,
                    linked_workspace_id=request.linked_workspace_id,
                    stream_version=int(current[3]) + 1,
                    event_type="workspace.unlinked",
                    relationship=str(current[5]),
                    label=current[6],
                    timestamp_us=_timestamp_us(dependencies.clock()),
                    previous_event_hash=str(current[10]),
                )
                _raise_if_cancelled(cancelled)
                _insert_event(connection, values)
                _raise_if_cancelled(cancelled)
                _authorize_workspace_access(dependencies, workspace, linked)
                connection.commit()
                return _unlink_receipt(
                    workspace.workspace_id,
                    request.linked_workspace_id,
                    event_id=str(values[0]),
                    replay=False,
                    changed=1,
                )
            except FederationOperationError:
                connection.rollback()
                raise
            except sqlite3.Error:
                connection.rollback()
                raise FederationOperationError("CAPABILITY_DEGRADED") from None
            except Exception:
                connection.rollback()
                raise
            finally:
                connection.close()


def _cursor_for(
    secret: bytes,
    workspace_id: str,
    linked_workspace_id: str,
) -> str:
    digest = hmac.new(
        secret,
        canonical_json_bytes(
            ["v7-workspace-links-cursor", workspace_id, linked_workspace_id]
        ),
        hashlib.sha256,
    ).hexdigest()
    return f"cur_v1_link_{linked_workspace_id}_{digest}"


def _cursor_anchor(
    dependencies: FederationOperationDependencies,
    workspace_id: str,
    cursor: str | None,
) -> str:
    if cursor is None:
        return ""
    match = _CURSOR_RE.fullmatch(cursor)
    if match is None:
        raise FederationOperationError("INVALID_ARGUMENT")
    anchor = match.group(1)
    expected = _cursor_for(
        dependencies.cursor_secret,
        workspace_id,
        anchor,
    )
    if not hmac.compare_digest(cursor, expected):
        raise FederationOperationError("INVALID_ARGUMENT")
    return anchor


def _list_sync(
    dependencies: FederationOperationDependencies,
    workspace: Workspace,
    request: AdmittedRequest,
) -> Page[WorkspaceLinkView]:
    _authorize_workspace_access(dependencies, workspace)
    anchor = _cursor_anchor(
        dependencies,
        workspace.workspace_id,
        request.cursor,
    )
    with dependencies.storage_resolver.locked_active(workspace) as active:
        database_path = _database_path(workspace, active)
        with federation_access_lock(database_path, "shared"):
            connection = _open_database(database_path, writable=False)
            try:
                rows = connection.execute(
                    "SELECT event.linked_workspace_id,event.relationship,event.label "
                    "FROM workspace_link_events AS event JOIN ("
                    "SELECT linked_workspace_id,MAX(stream_version) AS version "
                    "FROM workspace_link_events WHERE workspace_id=? "
                    "GROUP BY linked_workspace_id) AS latest "
                    "ON latest.linked_workspace_id=event.linked_workspace_id "
                    "AND latest.version=event.stream_version "
                    "WHERE event.workspace_id=? "
                    "AND event.event_type='workspace.linked' "
                    "AND event.linked_workspace_id>? "
                    "ORDER BY event.linked_workspace_id LIMIT ?",
                    (
                        workspace.workspace_id,
                        workspace.workspace_id,
                        anchor,
                        request.limit + 1,
                    ),
                ).fetchall()
                for row in rows:
                    current = _current_link(
                        connection,
                        workspace.workspace_id,
                        str(row[0]),
                    )
                    if (
                        current is None
                        or current[4] != "workspace.linked"
                        or current[5] != row[1]
                        or current[6] != row[2]
                    ):
                        raise FederationOperationError("CAPABILITY_DEGRADED")
            except sqlite3.Error:
                raise FederationOperationError("CAPABILITY_DEGRADED") from None
            finally:
                connection.close()
    selected = rows[: request.limit]
    items: list[WorkspaceLinkView] = []
    for row in selected:
        linked_workspace_id = str(row[0])
        _resolve_linked(dependencies, linked_workspace_id)
        items.append(
            WorkspaceLinkView(
                workspace_id=workspace.workspace_id,
                linked_workspace_id=linked_workspace_id,
                relationship=row[1],
                label=row[2],
            )
        )
    truncated = len(rows) > request.limit
    next_cursor = None
    if truncated and items:
        next_cursor = _cursor_for(
            dependencies.cursor_secret,
            workspace.workspace_id,
            items[-1].linked_workspace_id,
        )
    return Page[WorkspaceLinkView](
        items=items,
        next_cursor=next_cursor,
        truncated=truncated,
    )


def build_federation_operations(
    dependencies: FederationOperationDependencies,
) -> Mapping[str, Callable[..., object]]:
    """Return the three safe, framework-neutral workspace-link handlers."""

    if not isinstance(dependencies, FederationOperationDependencies):
        raise TypeError("dependencies must be FederationOperationDependencies")

    async def workspace_link(
        *, workspace: Workspace, request: AdmittedRequest
    ) -> WorkspaceLinkView:
        _authorize(workspace, request, "workspace_link")
        return await _run_mutation(
            dependencies,
            lambda cancelled: _link_sync(dependencies, workspace, request, cancelled),
        )

    async def workspace_unlink(
        *, workspace: Workspace, request: AdmittedRequest
    ) -> MutationReceipt:
        _authorize(workspace, request, "workspace_unlink")
        return await _run_mutation(
            dependencies,
            lambda cancelled: _unlink_sync(dependencies, workspace, request, cancelled),
        )

    async def workspace_links_list(
        *, workspace: Workspace, request: AdmittedRequest
    ) -> Page[WorkspaceLinkView]:
        _authorize(workspace, request, "workspace_links_list")
        return await _run_read(
            dependencies,
            lambda: _list_sync(dependencies, workspace, request),
        )

    return MappingProxyType(
        {
            "workspace_link": workspace_link,
            "workspace_links_list": workspace_links_list,
            "workspace_unlink": workspace_unlink,
        }
    )


__all__ = [
    "FederationOperationDependencies",
    "FederationOperationError",
    "build_federation_operations",
]
