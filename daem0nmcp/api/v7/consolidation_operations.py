"""Preview-bound, recoverable canonical workspace consolidation.

The target owns the durable preview and run ledger. Source workspaces remain
independent canonical authorities: consolidation copies complete live memory
state, and the destructive variant appends archive events only after the
target transaction has committed.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import secrets
import sqlite3
import threading
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import ExitStack, suppress
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from ...bounded_workers import BoundedWorkerBusyError, BoundedWorkerPool
from ...covenant import CovenantGate, InvocationScope, invocation_scope_var
from ...event_store import (
    EventCommand,
    EventStore,
    canonical_json_bytes,
    deterministic_id,
    memory_state_hash,
    sha256_json,
)
from ...schema_version import CURRENT_SCHEMA_VERSION
from ...storage_activation import ResolvedActiveDatabase
from ...workspace import Workspace, WorkspaceRegistry
from .application import AdmittedRequest
from .errors import STABLE_ERROR_CODE_SET
from .federated_retrieval import federation_access_lock, validate_directional_links
from .runtime_protocols import ActiveStorageResolver, WorkerPool, WorkspaceResolver
from .runtime_services import WorkspaceStorageResolver
from .tasks import await_task_terminal, durable_task_execution_var
from .tools import (
    WorkspaceConsolidateArchiveData,
    WorkspaceConsolidateData,
    WorkspaceConsolidationPreviewData,
)

_MAX_RECORDS = 10_000
_MAX_BYTES = 64 * 1024 * 1024
_PREVIEW_TTL = timedelta(minutes=10)
_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
_RECORD_COLUMNS = (
    "record_id,record_type,legacy_type,content,content_hash,rationale,"
    "context_json,tags_json,file_path,file_path_relative,keywords,is_permanent,"
    "pinned,archived,outcome,worked,recall_count,surprise_score,importance_score,"
    "source_client,source_model,stream_version,source_event_id,created_at_us,"
    "updated_at_us,deleted_at_us"
)
_REQUIRED_TABLES = frozenset(
    {
        "memory_events",
        "memory_records",
        "workspace_link_events",
        "consolidation_previews",
        "consolidation_preview_records",
        "consolidation_runs",
        "consolidation_mappings",
        "consolidation_archive_progress",
    }
)


class ConsolidationOperationError(RuntimeError):
    def __init__(self, code: str) -> None:
        if code not in STABLE_ERROR_CODE_SET:
            raise ValueError("consolidation error code is not stable")
        self.code = code
        super().__init__(code)


class _CancelledError(RuntimeError):
    pass


def _clock() -> datetime:
    return datetime.now(timezone.utc)


def _pool() -> BoundedWorkerPool:
    return BoundedWorkerPool(
        max_workers=2, thread_name_prefix="daem0nmcp-v7-consolidate"
    )


@dataclass(frozen=True, slots=True)
class ConsolidationOperationDependencies:
    workspace_resolver: WorkspaceResolver
    covenant_gate: CovenantGate
    scope_provider: Callable[[], InvocationScope | None] = invocation_scope_var.get
    storage_resolver: ActiveStorageResolver = field(
        default_factory=WorkspaceStorageResolver
    )
    signing_key: bytes = field(default_factory=lambda: secrets.token_bytes(32))
    clock: Callable[[], datetime] = _clock
    worker_pool: WorkerPool = field(default_factory=_pool)
    projection_scheduler: Callable[[Path], object] | None = None

    def __post_init__(self) -> None:
        if not callable(getattr(self.workspace_resolver, "resolve", None)):
            raise TypeError("workspace_resolver must provide resolve")
        if not callable(getattr(self.storage_resolver, "locked_active", None)):
            raise TypeError("storage_resolver must provide locked_active")
        if not callable(self.scope_provider) or not callable(self.clock):
            raise TypeError("scope_provider and clock must be callable")
        if len(self.signing_key) < 32:
            raise ValueError("signing_key must contain at least 32 bytes")

    def close(self) -> None:
        self.worker_pool.shutdown()


def _now_us(deps: ConsolidationOperationDependencies) -> int:
    value = deps.clock().astimezone(timezone.utc)
    delta = value - _EPOCH
    return (delta.days * 86_400 + delta.seconds) * 1_000_000 + delta.microseconds


def _datetime_us(value: int) -> datetime:
    return _EPOCH + timedelta(microseconds=value)


def _exact_workspace(value: object, workspace_id: str) -> Workspace:
    if not isinstance(value, Workspace) or value.workspace_id != workspace_id:
        raise ConsolidationOperationError("UNAUTHORIZED_WORKSPACE")
    try:
        canonical = value.root.resolve(strict=True)
        if (
            WorkspaceRegistry([canonical], default_root=canonical).default.workspace_id
            != workspace_id
        ):
            raise ValueError
        if os.path.normcase(str(value.root)) != os.path.normcase(str(canonical)):
            raise ValueError
    except (OSError, RuntimeError, TypeError, ValueError):
        raise ConsolidationOperationError("UNAUTHORIZED_WORKSPACE") from None
    return value


def _resolve_all(
    deps: ConsolidationOperationDependencies,
    target: Workspace,
    source_ids: Sequence[str],
) -> tuple[Workspace, ...]:
    target = _exact_workspace(target, target.workspace_id)
    result = [target]
    for workspace_id in sorted(source_ids):
        try:
            resolved = deps.workspace_resolver.resolve(workspace_id)
        except Exception:
            raise ConsolidationOperationError("UNAUTHORIZED_WORKSPACE") from None
        result.append(_exact_workspace(resolved, workspace_id))
    return tuple(result)


def _authorize_all(
    deps: ConsolidationOperationDependencies,
    workspaces: Sequence[Workspace],
    scope: InvocationScope | None = None,
) -> InvocationScope:
    scope = deps.scope_provider() if scope is None else scope
    if not isinstance(scope, InvocationScope):
        raise ConsolidationOperationError("IDENTITY_UNAVAILABLE")
    for workspace in workspaces:
        candidate = InvocationScope(
            scope.principal_id,
            scope.transport_session_id,
            os.path.normcase(str(workspace.root.resolve(strict=True))),
        )
        if not deps.covenant_gate.workspace_authorized(candidate):
            raise ConsolidationOperationError("UNAUTHORIZED_WORKSPACE")
    return scope


def _execution_scope(
    deps: ConsolidationOperationDependencies,
    workspace: Workspace,
) -> InvocationScope:
    scope = deps.scope_provider()
    if isinstance(scope, InvocationScope):
        return scope
    execution = durable_task_execution_var.get()
    if (
        execution is None
        or execution.workspace_id != workspace.workspace_id
        or not execution.principal_id
        or not execution.transport_session_id
    ):
        raise ConsolidationOperationError("IDENTITY_UNAVAILABLE")
    return InvocationScope(
        execution.principal_id,
        execution.transport_session_id,
        os.path.normcase(str(workspace.root.resolve(strict=True))),
    )


def _database_path(workspace: Workspace, active: ResolvedActiveDatabase) -> Path:
    try:
        result = Path(active.path).resolve(strict=True)
        result.relative_to(workspace.root.resolve(strict=True))
        if not result.is_file() or result.is_symlink():
            raise ValueError
        return result
    except (OSError, RuntimeError, TypeError, ValueError):
        raise ConsolidationOperationError("WORKSPACE_PATH_ESCAPE") from None


def _open(path: Path, *, writable: bool) -> sqlite3.Connection:
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
            or int(version[0]) != CURRENT_SCHEMA_VERSION
            or not tables >= _REQUIRED_TABLES
        ):
            raise ConsolidationOperationError("CAPABILITY_DEGRADED")
        return connection
    except ConsolidationOperationError:
        if connection is not None:
            connection.close()
        raise
    except Exception:
        if connection is not None:
            connection.close()
        raise ConsolidationOperationError("CAPABILITY_DEGRADED") from None


def _event_root(connection: sqlite3.Connection, workspace_id: str) -> str:
    digest = hashlib.sha256()
    count = 0
    for row in connection.execute(
        "SELECT event_hash FROM memory_events WHERE workspace_id=? ORDER BY event_id",
        (workspace_id,),
    ):
        digest.update(str(row[0]).encode("ascii"))
        digest.update(b"\n")
        count += 1
    return sha256_json({"count": count, "hash": digest.hexdigest()})


def _parse_json(value: object, expected: type) -> Any:
    try:
        parsed = json.loads(str(value))
    except (TypeError, ValueError, RecursionError):
        raise ConsolidationOperationError("CAPABILITY_DEGRADED") from None
    if not isinstance(parsed, expected):
        raise ConsolidationOperationError("CAPABILITY_DEGRADED")
    return parsed


def _state(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "record_type": row["record_type"],
        "legacy_type": row["legacy_type"],
        "content": row["content"],
        "rationale": row["rationale"],
        "context": _parse_json(row["context_json"], dict),
        "tags": _parse_json(row["tags_json"], list),
        # Absolute source paths are authority and never cross workspaces.
        "file_path": None,
        "file_path_relative": row["file_path_relative"],
        "keywords": row["keywords"],
        "is_permanent": bool(row["is_permanent"]),
        "pinned": bool(row["pinned"]),
        "archived": bool(row["archived"]),
        "outcome": row["outcome"],
        "worked": None if row["worked"] is None else bool(row["worked"]),
        "recall_count": int(row["recall_count"]),
        "surprise_score": row["surprise_score"],
        "importance_score": row["importance_score"],
        "source_client": row["source_client"],
        "source_model": row["source_model"],
        "deleted_at_us": row["deleted_at_us"],
    }


def _rows(connection: sqlite3.Connection, workspace_id: str) -> Iterator[sqlite3.Row]:
    cursor = connection.execute(
        f"SELECT {_RECORD_COLUMNS} FROM memory_records WHERE workspace_id=? "
        "AND deleted_at_us IS NULL AND archived=0 ORDER BY record_id LIMIT ?",
        (workspace_id, _MAX_RECORDS + 1),
    )
    seen = 0
    while True:
        batch = cursor.fetchmany(64)
        if not batch:
            return
        for row in batch:
            seen += 1
            if seen > _MAX_RECORDS:
                raise ConsolidationOperationError("TASK_REQUIRED")
            yield row


def _token(preview_id: str) -> str:
    return f"sel_v1.{preview_id}.{secrets.token_urlsafe(32)}"


def _claims(token: str) -> str:
    try:
        prefix, preview_id, nonce = token.split(".")
        if (
            prefix != "sel_v1"
            or len(preview_id) != 68
            or not preview_id.startswith("cpr_")
            or len(nonce) < 32
        ):
            raise ValueError
        return preview_id
    except Exception:
        raise ConsolidationOperationError("TOKEN_TAMPERED") from None


def _identity_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _verify_selection_rows(
    workspace_id: str,
    source_ids: Sequence[str],
    preview: sqlite3.Row,
    records: Sequence[sqlite3.Row],
) -> None:
    if len(records) != int(preview["selected_count"]):
        raise ConsolidationOperationError("CAPABILITY_DEGRADED")
    selected: list[dict[str, Any]] = []
    allowed_sources = set(source_ids)
    for ordinal, record in enumerate(records):
        source_id = str(record["source_workspace_id"])
        source_record_id = str(record["source_record_id"])
        target_record_id = deterministic_id(
            "mem",
            "workspace-consolidation",
            workspace_id,
            source_id,
            source_record_id,
        )
        if (
            int(record["ordinal"]) != ordinal
            or source_id not in allowed_sources
            or str(record["target_record_id"]) != target_record_id
        ):
            raise ConsolidationOperationError("CAPABILITY_DEGRADED")
        selected.append(
            {
                "source_workspace_id": source_id,
                "source_record_id": source_record_id,
                "source_event_id": str(record["source_event_id"]),
                "source_state_hash": str(record["source_state_hash"]),
                "source_content_hash": str(record["source_content_hash"]),
                "target_record_id": target_record_id,
            }
        )
    if sha256_json(selected) != preview["selection_hash"]:
        raise ConsolidationOperationError("CAPABILITY_DEGRADED")


def _preview_sync(
    deps: ConsolidationOperationDependencies,
    workspace: Workspace,
    request: AdmittedRequest,
    scope: InvocationScope,
) -> WorkspaceConsolidationPreviewData:
    workspaces = _resolve_all(deps, workspace, tuple(request.source_workspace_ids))
    scope = _authorize_all(deps, workspaces, scope)
    now = _now_us(deps)
    expires = now + int(_PREVIEW_TTL.total_seconds() * 1_000_000)
    with ExitStack() as stack:
        active = {
            item.workspace_id: stack.enter_context(
                deps.storage_resolver.locked_active(item)
            )
            for item in sorted(workspaces, key=lambda value: value.workspace_id)
        }
        paths = {
            item.workspace_id: _database_path(item, active[item.workspace_id])
            for item in workspaces
        }
        stack.enter_context(
            federation_access_lock(paths[workspace.workspace_id], "shared")
        )
        validate_directional_links(
            paths[workspace.workspace_id],
            workspace.workspace_id,
            sorted(request.source_workspace_ids),
        )
        connections = {
            identifier: _open(path, writable=identifier == workspace.workspace_id)
            for identifier, path in paths.items()
        }
        for connection in connections.values():
            stack.callback(connection.close)
        snapshots = {
            identifier: {
                "generation": int(active[identifier].generation),
                "event_root": _event_root(connections[identifier], identifier),
            }
            for identifier in sorted(connections)
        }
        selected: list[dict[str, Any]] = []
        total_bytes = 0
        for source_id in sorted(request.source_workspace_ids):
            for row in _rows(connections[source_id], source_id):
                state = _state(row)
                size = len(canonical_json_bytes(state))
                total_bytes += size
                if len(selected) >= _MAX_RECORDS or total_bytes > _MAX_BYTES:
                    raise ConsolidationOperationError("TASK_REQUIRED")
                selected.append(
                    {
                        "source_workspace_id": source_id,
                        "source_record_id": str(row["record_id"]),
                        "source_event_id": str(row["source_event_id"]),
                        "source_state_hash": memory_state_hash(state),
                        "source_content_hash": str(row["content_hash"]),
                        "target_record_id": deterministic_id(
                            "mem",
                            "workspace-consolidation",
                            workspace.workspace_id,
                            source_id,
                            str(row["record_id"]),
                        ),
                    }
                )
        if not selected:
            raise ConsolidationOperationError("NOT_FOUND")
        selection_hash = sha256_json(selected)
        preview_id = "cpr_" + sha256_json(
            [
                workspace.workspace_id,
                scope.principal_id,
                scope.transport_session_id,
                snapshots,
                selection_hash,
                now,
            ]
        )
        selection_token = _token(preview_id)
        target = connections[workspace.workspace_id]
        target.execute("BEGIN IMMEDIATE")
        try:
            target.execute(
                "INSERT INTO consolidation_previews("
                "preview_id,target_workspace_id,principal_hash,session_hash,selection_token_hash,sources_json,"
                "snapshots_json,selection_hash,selected_count,total_bytes,created_at_us,"
                "expires_at_us,applied_run_id) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,NULL)",
                (
                    preview_id,
                    workspace.workspace_id,
                    _identity_hash(scope.principal_id),
                    _identity_hash(scope.transport_session_id),
                    _identity_hash(selection_token),
                    canonical_json_bytes(sorted(request.source_workspace_ids)).decode(),
                    canonical_json_bytes(snapshots).decode(),
                    selection_hash,
                    len(selected),
                    total_bytes,
                    now,
                    expires,
                ),
            )
            target.executemany(
                "INSERT INTO consolidation_preview_records VALUES(?,?,?,?,?,?,?,?)",
                [
                    (
                        preview_id,
                        ordinal,
                        item["source_workspace_id"],
                        item["source_record_id"],
                        item["source_event_id"],
                        item["source_state_hash"],
                        item["source_content_hash"],
                        item["target_record_id"],
                    )
                    for ordinal, item in enumerate(selected)
                ],
            )
            target.commit()
        except Exception:
            target.rollback()
            raise
        _authorize_all(deps, workspaces, scope)
        return WorkspaceConsolidationPreviewData(
            preview_id=preview_id,
            sources=sorted(request.source_workspace_ids),
            selected=len(selected),
            total_bytes=total_bytes,
            expires_at=_datetime_us(expires),
            selection_token=selection_token,
        )


def _verified_preview(
    deps: ConsolidationOperationDependencies,
    target: sqlite3.Connection,
    workspace_id: str,
    source_ids: Sequence[str],
    token: str,
    scope: InvocationScope,
    now: int,
    *,
    require_fresh: bool,
) -> tuple[sqlite3.Row, list[sqlite3.Row], Mapping[str, Any]]:
    preview_id = _claims(token)
    row = target.execute(
        "SELECT * FROM consolidation_previews WHERE preview_id=?", (preview_id,)
    ).fetchone()
    if row is None:
        raise ConsolidationOperationError("TOKEN_TAMPERED")
    if row["selection_token_hash"] != _identity_hash(token):
        raise ConsolidationOperationError("TOKEN_TAMPERED")
    if (
        row["target_workspace_id"] != workspace_id
        or row["principal_hash"] != _identity_hash(scope.principal_id)
        or row["session_hash"] != _identity_hash(scope.transport_session_id)
    ):
        raise ConsolidationOperationError("TOKEN_SCOPE_MISMATCH")
    if require_fresh and int(row["expires_at_us"]) < now:
        raise ConsolidationOperationError("TOKEN_EXPIRED")
    if _parse_json(row["sources_json"], list) != sorted(source_ids):
        raise ConsolidationOperationError("TOKEN_ARGUMENT_MISMATCH")
    records = target.execute(
        "SELECT * FROM consolidation_preview_records WHERE preview_id=? ORDER BY ordinal",
        (row["preview_id"],),
    ).fetchall()
    _verify_selection_rows(workspace_id, source_ids, row, records)
    return row, records, _parse_json(row["snapshots_json"], dict)


def _current_row(
    connection: sqlite3.Connection, workspace_id: str, record_id: str
) -> sqlite3.Row | None:
    rows = connection.execute(
        f"SELECT {_RECORD_COLUMNS} FROM memory_records WHERE workspace_id=? AND record_id=? LIMIT 2",
        (workspace_id, record_id),
    ).fetchall()
    if len(rows) > 1:
        raise ConsolidationOperationError("CAPABILITY_DEGRADED")
    return None if not rows else rows[0]


def _raise_if_cancelled(cancelled: threading.Event) -> None:
    if cancelled.is_set():
        raise _CancelledError()


def _verify_committed_target(
    target: sqlite3.Connection,
    workspace_id: str,
    records: Sequence[sqlite3.Row],
) -> None:
    for record in records:
        mapping = target.execute(
            "SELECT target_record_id,source_event_id,source_state_hash "
            "FROM consolidation_mappings WHERE target_workspace_id=? "
            "AND source_workspace_id=? AND source_record_id=? LIMIT 2",
            (
                workspace_id,
                record["source_workspace_id"],
                record["source_record_id"],
            ),
        ).fetchall()
        current = _current_row(target, workspace_id, str(record["target_record_id"]))
        if (
            len(mapping) != 1
            or current is None
            or str(mapping[0]["target_record_id"]) != record["target_record_id"]
            or str(mapping[0]["source_event_id"]) != record["source_event_id"]
            or str(mapping[0]["source_state_hash"]) != record["source_state_hash"]
            or memory_state_hash(_state(current)) != record["source_state_hash"]
            or str(current["content_hash"]) != record["source_content_hash"]
        ):
            raise ConsolidationOperationError("CAPABILITY_DEGRADED")


def _schedule_projections(
    deps: ConsolidationOperationDependencies,
    paths: Sequence[Path],
) -> None:
    if deps.projection_scheduler is None:
        return
    for path in paths:
        with suppress(Exception):
            deps.projection_scheduler(path)


def _archive_sources(
    deps: ConsolidationOperationDependencies | None,
    target_workspace: Workspace,
    target_path: Path,
    run_id: str,
    source_workspaces: Mapping[str, Workspace],
    sources: Mapping[str, sqlite3.Connection],
    records: Sequence[sqlite3.Row],
    now: int,
    scope: InvocationScope | None,
    cancelled: threading.Event,
) -> int:
    archived = 0
    by_source: dict[str, list[sqlite3.Row]] = {}
    for record in records:
        by_source.setdefault(str(record["source_workspace_id"]), []).append(record)
    for source_id in sorted(by_source):
        _raise_if_cancelled(cancelled)
        connection = sources[source_id]
        store = EventStore(connection, assume_transaction=True)
        for record in by_source[source_id]:
            _raise_if_cancelled(cancelled)
            current = _current_row(
                connection, source_id, str(record["source_record_id"])
            )
            if current is None:
                raise ConsolidationOperationError("CONFLICT")
            state = _state(current)
            if state["archived"]:
                archived_event = connection.execute(
                    "SELECT event_type,correlation_id FROM memory_events WHERE event_id=?",
                    (current["source_event_id"],),
                ).fetchone()
                if archived_event is not None and tuple(archived_event) == (
                    "memory.archive_set",
                    run_id,
                ):
                    continue
                raise ConsolidationOperationError("CONFLICT")
            if (
                str(current["source_event_id"]) != record["source_event_id"]
                or memory_state_hash(state) != record["source_state_hash"]
                or str(current["content_hash"]) != record["source_content_hash"]
            ):
                raise ConsolidationOperationError("CONFLICT")
            state["archived"] = True
            store.append_and_project(
                EventCommand(
                    workspace_id=source_id,
                    stream_id=str(record["source_record_id"]),
                    stream_kind="memory",
                    event_type="memory.archive_set",
                    occurred_at_us=now,
                    recorded_at_us=now,
                    actor_type="system",
                    actor_id="workspace-consolidation",
                    correlation_id=run_id,
                    payload={
                        "record": state,
                        "provenance": {"consolidation_run_id": run_id},
                    },
                    expected_stream_version=int(current["stream_version"]) + 1,
                )
            )
            archived += 1
        _raise_if_cancelled(cancelled)
        if deps is not None:
            _authorize_all(
                deps,
                (target_workspace, source_workspaces[source_id]),
                scope,
            )
        connection.commit()
        target = _open(target_path, writable=True)
        try:
            target.execute("BEGIN IMMEDIATE")
            for record in by_source[source_id]:
                current = _current_row(
                    connection, source_id, str(record["source_record_id"])
                )
                assert current is not None
                changed = target.execute(
                    "UPDATE consolidation_archive_progress SET status='archived',archive_event_id=? WHERE run_id=? AND source_workspace_id=? AND source_record_id=? AND status='pending'",
                    (
                        current["source_event_id"],
                        run_id,
                        source_id,
                        record["source_record_id"],
                    ),
                ).rowcount
                if changed not in {0, 1}:
                    raise ConsolidationOperationError("CAPABILITY_DEGRADED")
            changed = target.execute(
                "UPDATE consolidation_runs SET status='archiving',archived_count=(SELECT COUNT(*) FROM consolidation_archive_progress WHERE run_id=? AND status='archived'),updated_at_us=? WHERE run_id=?",
                (run_id, now, run_id),
            ).rowcount
            if changed != 1:
                raise ConsolidationOperationError("CAPABILITY_DEGRADED")
            target.commit()
        finally:
            target.close()
    return archived


def _complete_archive(
    deps: ConsolidationOperationDependencies,
    target_workspace: Workspace,
    target_path: Path,
    run_id: str,
    source_workspaces: Mapping[str, Workspace],
    sources: Mapping[str, sqlite3.Connection],
    records: Sequence[sqlite3.Row],
    now: int,
    scope: InvocationScope,
    cancelled: threading.Event,
) -> tuple[int, int]:
    for connection in sources.values():
        if not connection.in_transaction:
            connection.execute("BEGIN IMMEDIATE")
    try:
        newly_archived = _archive_sources(
            deps,
            target_workspace,
            target_path,
            run_id,
            source_workspaces,
            sources,
            records,
            now,
            scope,
            cancelled,
        )
    except Exception:
        recovery = _open(target_path, writable=True)
        try:
            changed = recovery.execute(
                "UPDATE consolidation_runs SET status='recovery_required',"
                "updated_at_us=? WHERE run_id=? AND status<>'completed'",
                (now, run_id),
            ).rowcount
            if changed != 1:
                raise ConsolidationOperationError("CAPABILITY_DEGRADED")
            recovery.commit()
        finally:
            recovery.close()
        raise
    final = _open(target_path, writable=True)
    try:
        final.execute("BEGIN IMMEDIATE")
        pending = int(
            final.execute(
                "SELECT COUNT(*) FROM consolidation_archive_progress "
                "WHERE run_id=? AND status='pending'",
                (run_id,),
            ).fetchone()[0]
        )
        if pending != 0:
            raise ConsolidationOperationError("CAPABILITY_DEGRADED")
        changed = final.execute(
            "UPDATE consolidation_runs SET status='completed',"
            "archived_count=(SELECT COUNT(*) FROM consolidation_archive_progress "
            "WHERE run_id=? AND status='archived'),updated_at_us=? "
            "WHERE run_id=? AND status<>'completed'",
            (run_id, now, run_id),
        ).rowcount
        if changed != 1:
            raise ConsolidationOperationError("CAPABILITY_DEGRADED")
        final.commit()
        row = final.execute(
            "SELECT archived_count FROM consolidation_runs WHERE run_id=?",
            (run_id,),
        ).fetchone()
        if row is None:
            raise ConsolidationOperationError("CAPABILITY_DEGRADED")
        return int(row[0]), newly_archived
    except Exception:
        if final.in_transaction:
            final.rollback()
        raise
    finally:
        final.close()


def _apply_sync(
    deps: ConsolidationOperationDependencies,
    workspace: Workspace,
    request: AdmittedRequest,
    *,
    archive: bool,
    cancelled: threading.Event,
    scope: InvocationScope,
    projection_paths: set[Path],
) -> WorkspaceConsolidateData:
    source_ids = sorted(request.source_workspace_ids)
    workspaces = _resolve_all(deps, workspace, source_ids)
    scope = _authorize_all(deps, workspaces, scope)
    now = _now_us(deps)
    operation = "consolidate_archive" if archive else "consolidate"
    with ExitStack() as stack:
        active = {
            item.workspace_id: stack.enter_context(
                deps.storage_resolver.locked_active(item)
            )
            for item in sorted(workspaces, key=lambda item: item.workspace_id)
        }
        paths = {
            item.workspace_id: _database_path(item, active[item.workspace_id])
            for item in workspaces
        }
        stack.enter_context(
            federation_access_lock(paths[workspace.workspace_id], "shared")
        )
        validate_directional_links(
            paths[workspace.workspace_id], workspace.workspace_id, source_ids
        )
        connections = {
            identifier: _open(paths[identifier], writable=True)
            for identifier in sorted(paths)
        }
        for connection in connections.values():
            stack.callback(connection.close)
        for connection in connections.values():
            connection.execute("BEGIN IMMEDIATE")
        target = connections[workspace.workspace_id]
        preview, records, snapshots = _verified_preview(
            deps,
            target,
            workspace.workspace_id,
            source_ids,
            request.selection_token,
            scope,
            now,
            require_fresh=False,
        )
        request_hash = sha256_json(
            {
                "operation": operation,
                "target": workspace.workspace_id,
                "sources": source_ids,
                "preview": preview["preview_id"],
            }
        )
        run_id = "con_" + sha256_json(
            [workspace.workspace_id, operation, request.idempotency_key]
        )
        existing = target.execute(
            "SELECT * FROM consolidation_runs WHERE target_workspace_id=? AND operation=? AND idempotency_key=?",
            (workspace.workspace_id, operation, request.idempotency_key),
        ).fetchone()
        if existing is not None:
            if existing["request_hash"] != request_hash:
                raise ConsolidationOperationError("IDEMPOTENCY_CONFLICT")
            _verify_committed_target(target, workspace.workspace_id, records)
            projection_paths.add(paths[workspace.workspace_id])
            if archive:
                projection_paths.update(paths[identifier] for identifier in source_ids)
            for connection in connections.values():
                connection.rollback()
            replay_event_ids = _parse_json(existing["event_ids_json"], list)
            if not archive:
                if existing["status"] != "completed":
                    raise ConsolidationOperationError("CAPABILITY_DEGRADED")
                result: WorkspaceConsolidateData = WorkspaceConsolidateData(
                    sources=source_ids,
                    imported=int(existing["imported_count"]),
                    event_ids=replay_event_ids,
                )
                stack.close()
                return result
            progress = target.execute(
                "SELECT COUNT(*) AS total,"
                "SUM(CASE WHEN status='archived' THEN 1 ELSE 0 END) AS archived "
                "FROM consolidation_archive_progress WHERE run_id=?",
                (run_id,),
            ).fetchone()
            if progress is None or int(progress["total"]) != len(records):
                raise ConsolidationOperationError("CAPABILITY_DEGRADED")
            if existing["status"] == "completed":
                archived_count = int(progress["archived"] or 0)
                if archived_count != len(records) or archived_count != int(
                    existing["archived_count"]
                ):
                    raise ConsolidationOperationError("CAPABILITY_DEGRADED")
            elif existing["status"] in {
                "target_committed",
                "archiving",
                "recovery_required",
            }:
                source_workspaces = {item.workspace_id: item for item in workspaces[1:]}
                archived_count, _newly_archived = _complete_archive(
                    deps,
                    workspace,
                    paths[workspace.workspace_id],
                    run_id,
                    source_workspaces,
                    {identifier: connections[identifier] for identifier in source_ids},
                    records,
                    now,
                    scope,
                    cancelled,
                )
            else:
                raise ConsolidationOperationError("CAPABILITY_DEGRADED")
            result = WorkspaceConsolidateArchiveData(
                sources=source_ids,
                imported=int(existing["imported_count"]),
                archived=archived_count,
                event_ids=replay_event_ids,
            )
            stack.close()
            return result
        if int(preview["expires_at_us"]) < now:
            raise ConsolidationOperationError("TOKEN_EXPIRED")
        for identifier in sorted(connections):
            expected = snapshots.get(identifier)
            if (
                not isinstance(expected, dict)
                or int(expected.get("generation", -1))
                != int(active[identifier].generation)
                or expected.get("event_root")
                != _event_root(connections[identifier], identifier)
            ):
                raise ConsolidationOperationError("CONFLICT")
        _authorize_all(deps, workspaces, scope)
        validate_directional_links(
            paths[workspace.workspace_id], workspace.workspace_id, source_ids
        )
        if preview["applied_run_id"] is not None:
            raise ConsolidationOperationError("TOKEN_REPLAYED")
        if cancelled.is_set():
            raise _CancelledError()
        target.execute(
            "INSERT INTO consolidation_runs VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                run_id,
                workspace.workspace_id,
                operation,
                request.idempotency_key,
                preview["preview_id"],
                request_hash,
                "target_committed" if archive else "completed",
                0,
                0,
                "[]",
                now,
                now,
            ),
        )
        store = EventStore(target, assume_transaction=True)
        event_ids: list[str] = []
        imported = 0
        for record in records:
            _raise_if_cancelled(cancelled)
            source = _current_row(
                connections[str(record["source_workspace_id"])],
                str(record["source_workspace_id"]),
                str(record["source_record_id"]),
            )
            if source is None:
                raise ConsolidationOperationError("CONFLICT")
            state = _state(source)
            if (
                str(source["source_event_id"]) != record["source_event_id"]
                or memory_state_hash(state) != record["source_state_hash"]
                or str(source["content_hash"]) != record["source_content_hash"]
            ):
                raise ConsolidationOperationError("CONFLICT")
            current = _current_row(
                target, workspace.workspace_id, str(record["target_record_id"])
            )
            mapping = target.execute(
                "SELECT * FROM consolidation_mappings WHERE target_workspace_id=? AND source_workspace_id=? AND source_record_id=?",
                (
                    workspace.workspace_id,
                    record["source_workspace_id"],
                    record["source_record_id"],
                ),
            ).fetchone()
            if current is not None and mapping is None:
                raise ConsolidationOperationError("CONFLICT")
            if (
                mapping is not None
                and mapping["source_state_hash"] == record["source_state_hash"]
            ):
                continue
            event = store.append_and_project(
                EventCommand(
                    workspace_id=workspace.workspace_id,
                    stream_id=str(record["target_record_id"]),
                    stream_kind="memory",
                    event_type="memory.created"
                    if current is None
                    else "memory.updated",
                    occurred_at_us=now,
                    recorded_at_us=now,
                    actor_type="import",
                    actor_id="workspace-consolidation",
                    correlation_id=run_id,
                    payload={
                        "record": state,
                        "provenance": {
                            "consolidation_run_id": run_id,
                            "source_workspace_id": record["source_workspace_id"],
                            "source_record_id": record["source_record_id"],
                            "source_event_id": record["source_event_id"],
                            "source_state_hash": record["source_state_hash"],
                        },
                    },
                    expected_stream_version=1
                    if current is None
                    else int(current["stream_version"]) + 1,
                )
            )
            target.execute(
                "INSERT OR REPLACE INTO consolidation_mappings VALUES(?,?,?,?,?,?,?,?)",
                (
                    workspace.workspace_id,
                    record["source_workspace_id"],
                    record["source_record_id"],
                    record["target_record_id"],
                    record["source_event_id"],
                    record["source_state_hash"],
                    event.event_id,
                    run_id,
                ),
            )
            event_ids.append(event.event_id)
            imported += 1
        if archive:
            target.executemany(
                "INSERT INTO consolidation_archive_progress VALUES(?,?,?,?,?,'pending',NULL)",
                [
                    (
                        run_id,
                        record["source_workspace_id"],
                        record["source_record_id"],
                        record["source_event_id"],
                        record["source_state_hash"],
                    )
                    for record in records
                ],
            )
        changed = target.execute(
            "UPDATE consolidation_runs SET imported_count=?,event_ids_json=?,updated_at_us=? WHERE run_id=?",
            (imported, canonical_json_bytes(event_ids).decode(), now, run_id),
        ).rowcount
        if changed != 1:
            raise ConsolidationOperationError("CAPABILITY_DEGRADED")
        changed = target.execute(
            "UPDATE consolidation_previews SET applied_run_id=? WHERE preview_id=? AND applied_run_id IS NULL",
            (run_id, preview["preview_id"]),
        ).rowcount
        if changed != 1:
            raise ConsolidationOperationError("TOKEN_REPLAYED")
        _raise_if_cancelled(cancelled)
        _authorize_all(deps, workspaces, scope)
        validate_directional_links(
            paths[workspace.workspace_id], workspace.workspace_id, source_ids
        )
        target.commit()
        projection_paths.add(paths[workspace.workspace_id])
        if archive:
            # A source may commit before a later archive step fails. Wake all
            # participating source queues after the worker releases its locks.
            projection_paths.update(paths[identifier] for identifier in source_ids)
        if archive:
            source_workspaces = {item.workspace_id: item for item in workspaces[1:]}
            archived, _newly_archived = _complete_archive(
                deps,
                workspace,
                paths[workspace.workspace_id],
                run_id,
                source_workspaces,
                {identifier: connections[identifier] for identifier in source_ids},
                records,
                now,
                scope,
                cancelled,
            )
        else:
            archived = 0
        for identifier, connection in connections.items():
            if identifier != workspace.workspace_id and connection.in_transaction:
                connection.rollback()
        if archive:
            result = WorkspaceConsolidateArchiveData(
                sources=source_ids,
                imported=imported,
                archived=archived,
                event_ids=event_ids,
            )
        else:
            result = WorkspaceConsolidateData(
                sources=source_ids, imported=imported, event_ids=event_ids
            )
        stack.close()
        return result


def recover_consolidation(
    workspace_registry: WorkspaceRegistry,
    target_workspace: Workspace,
    *,
    run_id: str | None = None,
    storage_resolver: ActiveStorageResolver | None = None,
    clock: Callable[[], datetime] = _clock,
) -> list[dict[str, Any]]:
    """Resume target-committed source archives for a local operator.

    The command never deletes workspace directories or canonical source data.
    It only completes deterministic ``memory.archive_set`` events whose exact
    pre-archive event and state hashes are retained in the target run ledger.
    """

    resolver = storage_resolver or WorkspaceStorageResolver()
    with resolver.locked_active(target_workspace) as target_active:
        discovered_generation = int(target_active.generation)
        discovered_path = _database_path(target_workspace, target_active)
        target = _open(discovered_path, writable=False)
        try:
            parameters: tuple[object, ...] = (target_workspace.workspace_id,)
            query = (
                "SELECT run_id FROM consolidation_runs WHERE target_workspace_id=? "
                "AND operation='consolidate_archive' AND status<>'completed'"
            )
            if run_id is not None:
                query += " AND run_id=?"
                parameters += (run_id,)
            query += " ORDER BY created_at_us,run_id"
            runs = [str(row[0]) for row in target.execute(query, parameters)]
        finally:
            target.close()
    if run_id is not None and not runs:
        raise ConsolidationOperationError("NOT_FOUND")

    results: list[dict[str, Any]] = []
    for selected_run in runs:
        with resolver.locked_active(target_workspace) as target_active:
            if (
                int(target_active.generation) != discovered_generation
                or _database_path(target_workspace, target_active) != discovered_path
            ):
                raise ConsolidationOperationError("CONFLICT")
            target = _open(discovered_path, writable=False)
            try:
                run = target.execute(
                    "SELECT * FROM consolidation_runs WHERE run_id=? AND "
                    "target_workspace_id=? AND operation='consolidate_archive' LIMIT 2",
                    (selected_run, target_workspace.workspace_id),
                ).fetchall()
                if len(run) != 1:
                    raise ConsolidationOperationError("CAPABILITY_DEGRADED")
                if run[0]["status"] not in {
                    "target_committed",
                    "archiving",
                    "recovery_required",
                }:
                    raise ConsolidationOperationError("CAPABILITY_DEGRADED")
                preview = target.execute(
                    "SELECT * FROM consolidation_previews WHERE preview_id=?",
                    (run[0]["preview_id"],),
                ).fetchone()
                if preview is None:
                    raise ConsolidationOperationError("CAPABILITY_DEGRADED")
                parsed_source_ids = _parse_json(preview["sources_json"], list)
                if not all(isinstance(item, str) for item in parsed_source_ids):
                    raise ConsolidationOperationError("CAPABILITY_DEGRADED")
                source_ids = sorted(str(item) for item in parsed_source_ids)
                records = target.execute(
                    "SELECT * FROM consolidation_preview_records "
                    "WHERE preview_id=? ORDER BY ordinal",
                    (preview["preview_id"],),
                ).fetchall()
                _verify_selection_rows(
                    target_workspace.workspace_id, source_ids, preview, records
                )
                expected_request_hash = sha256_json(
                    {
                        "operation": "consolidate_archive",
                        "target": target_workspace.workspace_id,
                        "sources": source_ids,
                        "preview": preview["preview_id"],
                    }
                )
                if run[0]["request_hash"] != expected_request_hash:
                    raise ConsolidationOperationError("CAPABILITY_DEGRADED")
                _verify_committed_target(target, target_workspace.workspace_id, records)
            finally:
                target.close()
        sources = tuple(
            _exact_workspace(workspace_registry.resolve(identifier), identifier)
            for identifier in source_ids
        )
        now = int(
            (clock().astimezone(timezone.utc) - _EPOCH).total_seconds() * 1_000_000
        )
        with ExitStack() as stack:
            all_workspaces = (target_workspace, *sources)
            active = {
                item.workspace_id: stack.enter_context(resolver.locked_active(item))
                for item in sorted(all_workspaces, key=lambda value: value.workspace_id)
            }
            if (
                int(active[target_workspace.workspace_id].generation)
                != discovered_generation
                or _database_path(
                    target_workspace, active[target_workspace.workspace_id]
                )
                != discovered_path
            ):
                raise ConsolidationOperationError("CONFLICT")
            target = _open(discovered_path, writable=False)
            try:
                run_row = target.execute(
                    "SELECT status FROM consolidation_runs WHERE run_id=?",
                    (selected_run,),
                ).fetchall()
                progress = target.execute(
                    "SELECT progress.source_workspace_id,progress.source_record_id,"
                    "progress.expected_event_id AS source_event_id,"
                    "progress.expected_state_hash AS source_state_hash,"
                    "preview.source_content_hash FROM consolidation_archive_progress "
                    "AS progress JOIN consolidation_runs AS run ON "
                    "run.run_id=progress.run_id JOIN consolidation_preview_records "
                    "AS preview ON preview.preview_id=run.preview_id AND "
                    "preview.source_workspace_id=progress.source_workspace_id AND "
                    "preview.source_record_id=progress.source_record_id "
                    "WHERE progress.run_id=? AND progress.status='pending' "
                    "ORDER BY progress.source_workspace_id,progress.source_record_id",
                    (selected_run,),
                ).fetchall()
                total_progress = int(
                    target.execute(
                        "SELECT COUNT(*) FROM consolidation_archive_progress "
                        "WHERE run_id=?",
                        (selected_run,),
                    ).fetchone()[0]
                )
                if len(run_row) != 1 or total_progress != len(records):
                    raise ConsolidationOperationError("CAPABILITY_DEGRADED")
                if run_row[0]["status"] not in {
                    "target_committed",
                    "archiving",
                    "recovery_required",
                }:
                    raise ConsolidationOperationError("CAPABILITY_DEGRADED")
            finally:
                target.close()
            source_workspaces = {item.workspace_id: item for item in sources}
            connections = {
                source.workspace_id: _open(
                    _database_path(source, active[source.workspace_id]),
                    writable=True,
                )
                for source in sources
            }
            for connection in connections.values():
                stack.callback(connection.close)
                connection.execute("BEGIN IMMEDIATE")
            newly_archived = _archive_sources(
                None,
                target_workspace,
                discovered_path,
                selected_run,
                source_workspaces,
                connections,
                progress,
                now,
                None,
                threading.Event(),
            )
            final = _open(discovered_path, writable=True)
            try:
                pending = int(
                    final.execute(
                        "SELECT COUNT(*) FROM consolidation_archive_progress "
                        "WHERE run_id=? AND status='pending'",
                        (selected_run,),
                    ).fetchone()[0]
                )
                if pending != 0:
                    raise ConsolidationOperationError("CAPABILITY_DEGRADED")
                changed = final.execute(
                    "UPDATE consolidation_runs SET status='completed',"
                    "archived_count=(SELECT COUNT(*) FROM "
                    "consolidation_archive_progress WHERE run_id=? AND "
                    "status='archived'),updated_at_us=? WHERE run_id=? "
                    "AND status<>'completed'",
                    (selected_run, now, selected_run),
                ).rowcount
                if changed != 1:
                    raise ConsolidationOperationError("CAPABILITY_DEGRADED")
                final.commit()
                total = int(
                    final.execute(
                        "SELECT archived_count FROM consolidation_runs WHERE run_id=?",
                        (selected_run,),
                    ).fetchone()[0]
                )
            finally:
                final.close()
        results.append(
            {
                "run_id": selected_run,
                "status": "completed",
                "archived": total,
                "newly_archived": newly_archived,
            }
        )
    return results


async def _run(
    deps: ConsolidationOperationDependencies, operation: Callable[[], Any]
) -> Any:
    task = asyncio.create_task(deps.worker_pool.run(operation))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError as cancellation:
        try:
            return await await_task_terminal(task)
        except Exception:
            raise cancellation from None
    except BoundedWorkerBusyError as exc:
        raise ConsolidationOperationError("TASK_REQUIRED") from exc


async def _run_mutation(
    deps: ConsolidationOperationDependencies,
    cancelled: threading.Event,
    operation: Callable[[], Any],
    projection_paths: set[Path],
) -> Any:
    task = asyncio.create_task(deps.worker_pool.run(operation))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError as cancellation:
        cancelled.set()
        try:
            return await await_task_terminal(task)
        except _CancelledError:
            raise cancellation from None
        except Exception:
            raise cancellation from None
    except BoundedWorkerBusyError as exc:
        raise ConsolidationOperationError("TASK_REQUIRED") from exc
    finally:
        # The shield/terminal wait above keeps this after worker completion,
        # including cancellation and errors after the target committed. The
        # production scheduler owns asyncio tasks and must run on this loop.
        _schedule_projections(deps, tuple(sorted(projection_paths)))


def build_consolidation_operations(
    dependencies: ConsolidationOperationDependencies,
) -> Mapping[str, Callable[..., Any]]:
    async def preview(
        *, workspace: Workspace, request: AdmittedRequest
    ) -> WorkspaceConsolidationPreviewData:
        if (
            request.tool_name != "workspace_consolidation_preview"
            or request.workspace_id != workspace.workspace_id
        ):
            raise ConsolidationOperationError("UNAUTHORIZED_WORKSPACE")
        scope = _execution_scope(dependencies, workspace)
        return await _run(
            dependencies, lambda: _preview_sync(dependencies, workspace, request, scope)
        )

    async def consolidate(
        *, workspace: Workspace, request: AdmittedRequest
    ) -> WorkspaceConsolidateData:
        if (
            request.tool_name != "workspace_consolidate"
            or request.workspace_id != workspace.workspace_id
        ):
            raise ConsolidationOperationError("UNAUTHORIZED_WORKSPACE")
        cancelled = threading.Event()
        scope = _execution_scope(dependencies, workspace)
        projection_paths: set[Path] = set()
        return await _run_mutation(
            dependencies,
            cancelled,
            lambda: _apply_sync(
                dependencies,
                workspace,
                request,
                archive=False,
                cancelled=cancelled,
                scope=scope,
                projection_paths=projection_paths,
            ),
            projection_paths,
        )

    async def consolidate_archive(
        *, workspace: Workspace, request: AdmittedRequest
    ) -> WorkspaceConsolidateArchiveData:
        if (
            request.tool_name != "workspace_consolidate_and_archive_sources"
            or request.workspace_id != workspace.workspace_id
        ):
            raise ConsolidationOperationError("UNAUTHORIZED_WORKSPACE")
        cancelled = threading.Event()
        scope = _execution_scope(dependencies, workspace)
        projection_paths: set[Path] = set()
        return await _run_mutation(
            dependencies,
            cancelled,
            lambda: _apply_sync(
                dependencies,
                workspace,
                request,
                archive=True,
                cancelled=cancelled,
                scope=scope,
                projection_paths=projection_paths,
            ),
            projection_paths,
        )

    return {
        "workspace_consolidation_preview": preview,
        "workspace_consolidate": consolidate,
        "workspace_consolidate_and_archive_sources": consolidate_archive,
    }


__all__ = [
    "ConsolidationOperationDependencies",
    "ConsolidationOperationError",
    "build_consolidation_operations",
    "recover_consolidation",
]
