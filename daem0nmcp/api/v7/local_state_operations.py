"""Canonical v7 active-context operation adapters.

The adapters use only the schema-20 active-context relation and canonical
memory projection. They hold the active-generation lock for every SQLite
snapshot and never expose a workspace path or retained integer identifier.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
import threading
from collections.abc import Callable, Mapping
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from types import MappingProxyType
from typing import Any

from ...bounded_workers import BoundedWorkerBusyError, BoundedWorkerPool
from ...event_store import (
    AppendedEvent,
    GovernanceEventCommand,
    GovernanceEventStore,
    canonical_json_bytes,
    deterministic_id,
    sha256_json,
)
from ...schema_version import CURRENT_SCHEMA_VERSION
from ...workspace import Workspace, WorkspaceRegistry
from .active_context_storage import active_context_id_for_record
from .application import AdmittedRequest
from .errors import STABLE_ERROR_CODE_SET
from .models import (
    DestructiveMutationReceipt,
    MutationReceipt,
    RecordSummary,
    parse_wire_datetime,
    stored_relative_path,
)
from .resources import ActiveContextItem
from .runtime_services import WorkspaceStorageResolver
from .tasks import await_task_terminal
from .tools import ActiveContextPage

_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
_MAX_ACTIVE_ENTRIES = 500
_PUBLIC_RECORD_TYPES = frozenset(
    {"decision", "pattern", "warning", "learning", "procedure", "observation"}
)
_CURSOR_RE = re.compile(r"^cur_([0-9a-f]{64})_([0-9a-f]{1,16})_([0-9a-f]{64})$")
_SELECTION_RE = re.compile(r"^sel_([0-9a-f]{64})_([0-9a-f]{1,16})_([0-9a-f]{64})$")
_RECORD_COLUMNS = (
    "record.record_id,record.workspace_id,record.record_type,record.content,"
    "record.content_hash,record.tags_json,record.file_path,"
    "record.file_path_relative,record.archived,record.created_at_us,"
    "record.updated_at_us,record.deleted_at_us"
)
_ENTRY_COLUMNS = (
    "entry.active_context_id,entry.priority,entry.reason,entry.added_at_us,"
    "entry.expires_at_us,entry.removed_at_us"
)
_REQUIRED_ENTRY_COLUMNS = frozenset(
    {
        "active_context_id",
        "workspace_id",
        "record_id",
        "priority",
        "reason",
        "added_at_us",
        "expires_at_us",
        "removed_at_us",
    }
)
_REQUIRED_RECORD_COLUMNS = frozenset(
    {
        "record_id",
        "workspace_id",
        "record_type",
        "content",
        "content_hash",
        "tags_json",
        "file_path",
        "file_path_relative",
        "archived",
        "created_at_us",
        "updated_at_us",
        "deleted_at_us",
    }
)

_LOCAL_STATE_WORKERS = BoundedWorkerPool(
    max_workers=4,
    thread_name_prefix="daem0nmcp-v7-local-state",
)


class LocalStateOperationError(RuntimeError):
    """Stable, path-free business failure understood by the v7 router."""

    def __init__(self, code: str) -> None:
        if code not in STABLE_ERROR_CODE_SET:
            raise ValueError("local-state operation error code is not stable")
        self.code = code
        super().__init__(code)


class _WorkerCancelledError(RuntimeError):
    pass


def _default_clock() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True, slots=True)
class LocalStateOperationDependencies:
    """Reviewed dependencies for canonical active-context operations."""

    storage_resolver: WorkspaceStorageResolver = field(
        default_factory=WorkspaceStorageResolver
    )
    clock: Callable[[], datetime] = field(default=_default_clock)
    token_secret: bytes = field(default_factory=lambda: secrets.token_bytes(32))
    selection_ttl_seconds: int = 300

    def __post_init__(self) -> None:
        if not callable(getattr(self.storage_resolver, "locked_active", None)):
            raise TypeError("storage_resolver must provide locked_active")
        if not callable(self.clock):
            raise TypeError("clock must be callable")
        if not isinstance(self.token_secret, bytes) or len(self.token_secret) < 32:
            raise ValueError("token_secret must contain at least 32 bytes")
        if (
            isinstance(self.selection_ttl_seconds, bool)
            or not isinstance(self.selection_ttl_seconds, int)
            or not 1 <= self.selection_ttl_seconds <= 3600
        ):
            raise ValueError("selection_ttl_seconds must be between 1 and 3600")


def _authorize(
    workspace: Workspace,
    request: AdmittedRequest,
    tool_name: str,
) -> None:
    if (
        not isinstance(workspace, Workspace)
        or not isinstance(request, AdmittedRequest)
        or request.tool_name != tool_name
        or request.workspace_id != workspace.workspace_id
    ):
        raise LocalStateOperationError("UNAUTHORIZED_WORKSPACE")
    try:
        canonical = workspace.root.resolve(strict=True)
        registered = WorkspaceRegistry([canonical], default_root=canonical).default
        exact_root = os.path.normcase(str(workspace.root)) == os.path.normcase(
            str(canonical)
        )
    except (OSError, RuntimeError, TypeError, ValueError):
        raise LocalStateOperationError("UNAUTHORIZED_WORKSPACE") from None
    if registered.workspace_id != workspace.workspace_id or not exact_root:
        raise LocalStateOperationError("UNAUTHORIZED_WORKSPACE")


def _table_columns(
    connection: sqlite3.Connection,
    table: str,
) -> frozenset[str]:
    return frozenset(
        str(row[1]) for row in connection.execute(f"PRAGMA table_info({table})")
    )


def _open_database(path: Any, *, writable: bool) -> sqlite3.Connection:
    connection: sqlite3.Connection | None = None
    try:
        resolved = path.resolve(strict=True)
        mode = "rw" if writable else "ro"
        connection = sqlite3.connect(
            f"{resolved.as_uri()}?mode={mode}",
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
                "SELECT name FROM sqlite_master WHERE type='table' AND name IN "
                "('active_context_entries','background_jobs','governance_events',"
                "'memory_records')"
            )
        }
        if (
            version is None
            or type(version[0]) is not int
            or version[0] < CURRENT_SCHEMA_VERSION
            or tables
            != {
                "active_context_entries",
                "background_jobs",
                "governance_events",
                "memory_records",
            }
            or not _table_columns(connection, "active_context_entries")
            >= _REQUIRED_ENTRY_COLUMNS
            or not _table_columns(connection, "memory_records")
            >= _REQUIRED_RECORD_COLUMNS
        ):
            raise LocalStateOperationError("CAPABILITY_DEGRADED")
        return connection
    except LocalStateOperationError:
        if connection is not None:
            connection.close()
        raise
    except Exception:
        if connection is not None:
            connection.close()
        raise LocalStateOperationError("CAPABILITY_DEGRADED") from None


def _datetime_us(value: object) -> int:
    try:
        value = parse_wire_datetime(value)
        delta = value.astimezone(timezone.utc) - _EPOCH
        result = (delta.days * 86_400 + delta.seconds) * 1_000_000 + delta.microseconds
    except (OverflowError, ValueError):
        raise LocalStateOperationError("INVALID_ARGUMENT") from None
    if not 0 <= result <= 2**63 - 1:
        raise LocalStateOperationError("INVALID_ARGUMENT")
    return result


def _datetime_from_us(value: object) -> datetime:
    if isinstance(value, bool) or not isinstance(value, int):
        raise LocalStateOperationError("CAPABILITY_DEGRADED")
    try:
        return _EPOCH + timedelta(microseconds=value)
    except (OverflowError, ValueError):
        raise LocalStateOperationError("CAPABILITY_DEGRADED") from None


def _now_us(dependencies: LocalStateOperationDependencies) -> int:
    try:
        value = dependencies.clock()
    except Exception:
        raise LocalStateOperationError("CAPABILITY_DEGRADED") from None
    try:
        return _datetime_us(value)
    except LocalStateOperationError as exc:
        raise LocalStateOperationError("CAPABILITY_DEGRADED") from exc


def _json_string_list(value: object) -> list[str]:
    if not isinstance(value, str) or len(value.encode("utf-8")) > 65_536:
        raise LocalStateOperationError("CAPABILITY_DEGRADED")
    try:
        decoded = json.loads(value)
    except (json.JSONDecodeError, RecursionError, UnicodeError):
        raise LocalStateOperationError("CAPABILITY_DEGRADED") from None
    if not isinstance(decoded, list) or not all(type(item) is str for item in decoded):
        raise LocalStateOperationError("CAPABILITY_DEGRADED")
    return decoded


def _record_summary(row: sqlite3.Row) -> RecordSummary:
    # A migrated v6 row keeps the host-absolute ``file_path`` v6 wrote; it
    # is legacy provenance, never emitted, so it must not deny the read.
    content = row["content"]
    record_type = row["record_type"]
    archived = row["archived"]
    if (
        not isinstance(content, str)
        or not content
        or record_type not in _PUBLIC_RECORD_TYPES
        or archived not in (0, 1)
    ):
        raise LocalStateOperationError("CAPABILITY_DEGRADED")
    if row["deleted_at_us"] is not None:
        _datetime_from_us(row["deleted_at_us"])
    status = (
        "invalidated"
        if row["deleted_at_us"] is not None
        else "archived"
        if archived
        else "current"
    )
    try:
        return RecordSummary.model_validate(
            {
                "record_id": row["record_id"],
                "record_type": record_type,
                "excerpt": content[:4000],
                "tags": _json_string_list(row["tags_json"]),
                "relative_file_path": stored_relative_path(row["file_path_relative"]),
                "current_status": status,
                "content_hash": row["content_hash"],
                "created_at": _datetime_from_us(row["created_at_us"]),
                "updated_at": _datetime_from_us(row["updated_at_us"]),
            }
        )
    except LocalStateOperationError:
        raise
    except Exception:
        raise LocalStateOperationError("CAPABILITY_DEGRADED") from None


def _active_item(row: sqlite3.Row) -> ActiveContextItem:
    removed = row["removed_at_us"]
    if removed is not None:
        _datetime_from_us(removed)
        raise LocalStateOperationError("CAPABILITY_DEGRADED")
    try:
        return ActiveContextItem(
            active_context_id=row["active_context_id"],
            record=_record_summary(row),
            priority=row["priority"],
            reason=row["reason"],
            added_at=_datetime_from_us(row["added_at_us"]),
            expires_at=(
                None
                if row["expires_at_us"] is None
                else _datetime_from_us(row["expires_at_us"])
            ),
        )
    except LocalStateOperationError:
        raise
    except Exception:
        raise LocalStateOperationError("CAPABILITY_DEGRADED") from None


def _active_state(row: Mapping[str, object] | sqlite3.Row) -> dict[str, object]:
    """Return the complete path-free semantic state carried by an event."""

    try:
        active_context_id = str(row["active_context_id"])
        record_id = str(row["record_id"])
        priority = row["priority"]
        reason = row["reason"]
        added_at_us = row["added_at_us"]
        expires_at_us = row["expires_at_us"]
        removed_at_us = row["removed_at_us"]
    except (IndexError, KeyError):
        raise LocalStateOperationError("CAPABILITY_DEGRADED") from None
    if (
        not active_context_id.startswith("act_")
        or not record_id.startswith("mem_")
        or isinstance(priority, bool)
        or not isinstance(priority, int)
        or not -100 <= priority <= 100
        or (reason is not None and not isinstance(reason, str))
    ):
        raise LocalStateOperationError("CAPABILITY_DEGRADED")
    _datetime_from_us(added_at_us)
    if expires_at_us is not None:
        _datetime_from_us(expires_at_us)
    if removed_at_us is not None:
        _datetime_from_us(removed_at_us)
    return {
        "active_context_id": active_context_id,
        "record_id": record_id,
        "priority": priority,
        "reason": reason,
        "added_at_us": added_at_us,
        "expires_at_us": expires_at_us,
        "removed_at_us": removed_at_us,
    }


def _append_active_event(
    connection: sqlite3.Connection,
    workspace_id: str,
    event_type: str,
    state: Mapping[str, object],
    now_us: int,
) -> AppendedEvent:
    try:
        return GovernanceEventStore(
            connection,
            assume_transaction=True,
        ).append_and_project(
            GovernanceEventCommand(
                workspace_id=workspace_id,
                stream_id=str(state["active_context_id"]),
                stream_kind="active_context",
                event_type=event_type,
                occurred_at_us=now_us,
                recorded_at_us=now_us,
                actor_type="client",
                payload=state,
            )
        )
    except Exception as exc:
        raise _translate_error(exc) from None


def _latest_active_event_id(
    connection: sqlite3.Connection,
    workspace_id: str,
    active_context_id: str,
    event_type: str,
) -> str | None:
    rows = connection.execute(
        "SELECT event_id,event_type FROM governance_events "
        "WHERE workspace_id=? AND stream_id=? "
        "AND stream_kind='active_context' "
        "ORDER BY stream_version DESC LIMIT 2",
        (workspace_id, active_context_id),
    ).fetchall()
    if not rows:
        return None
    if len(rows) > 1 and rows[0]["event_id"] == rows[1]["event_id"]:
        raise LocalStateOperationError("CAPABILITY_DEGRADED")
    return str(rows[0]["event_id"]) if rows[0]["event_type"] == event_type else None


def _snapshot_rows(
    connection: sqlite3.Connection,
    workspace_id: str,
    now_us: int,
) -> list[sqlite3.Row]:
    rows = connection.execute(
        f"SELECT {_ENTRY_COLUMNS},{_RECORD_COLUMNS} "
        "FROM active_context_entries entry JOIN memory_records record "
        "ON record.workspace_id=entry.workspace_id "
        "AND record.record_id=entry.record_id "
        "WHERE entry.workspace_id=? AND entry.removed_at_us IS NULL "
        "AND (entry.expires_at_us IS NULL OR entry.expires_at_us>?) "
        "ORDER BY entry.priority DESC,entry.added_at_us DESC,"
        "entry.active_context_id ASC LIMIT ?",
        (workspace_id, now_us, _MAX_ACTIVE_ENTRIES + 1),
    ).fetchall()
    if len(rows) > _MAX_ACTIVE_ENTRIES:
        raise LocalStateOperationError("CAPABILITY_DEGRADED")
    return rows


def _snapshot_hash(workspace_id: str, rows: list[sqlite3.Row]) -> str:
    return sha256_json(
        [
            "daem0nmcp",
            "v7",
            "active-context-snapshot",
            workspace_id,
            [
                [
                    row["active_context_id"],
                    row["priority"],
                    row["reason"],
                    row["added_at_us"],
                    row["expires_at_us"],
                ]
                for row in rows
            ],
        ]
    )


def _signature(
    dependencies: LocalStateOperationDependencies,
    payload: object,
) -> str:
    return hmac.new(
        dependencies.token_secret,
        canonical_json_bytes(payload),
        hashlib.sha256,
    ).hexdigest()


def _selection_token(
    dependencies: LocalStateOperationDependencies,
    workspace_id: str,
    snapshot_hash: str,
    expires_at_us: int,
) -> str:
    signature = _signature(
        dependencies,
        [
            "daem0nmcp",
            "v7",
            "active-context-clear",
            workspace_id,
            snapshot_hash,
            expires_at_us,
        ],
    )
    return f"sel_{snapshot_hash}_{expires_at_us:x}_{signature}"


def _authenticate_selection(
    dependencies: LocalStateOperationDependencies,
    workspace_id: str,
    token: object,
    now_us: int,
) -> str:
    if not isinstance(token, str):
        raise LocalStateOperationError("TOKEN_TAMPERED")
    match = _SELECTION_RE.fullmatch(token)
    if match is None:
        raise LocalStateOperationError("TOKEN_TAMPERED")
    snapshot_hash = match.group(1)
    try:
        expires_at_us = int(match.group(2), 16)
    except ValueError:
        raise LocalStateOperationError("TOKEN_TAMPERED") from None
    signature = _signature(
        dependencies,
        [
            "daem0nmcp",
            "v7",
            "active-context-clear",
            workspace_id,
            snapshot_hash,
            expires_at_us,
        ],
    )
    expected = f"sel_{snapshot_hash}_{expires_at_us:x}_{signature}"
    if not hmac.compare_digest(token, expected):
        raise LocalStateOperationError("TOKEN_TAMPERED")
    if expires_at_us <= now_us:
        raise LocalStateOperationError("TOKEN_EXPIRED")
    return snapshot_hash


def _cursor(
    dependencies: LocalStateOperationDependencies,
    workspace_id: str,
    snapshot_hash: str,
    active_context_id: str,
    selection_expires_at_us: int,
) -> str:
    signature = _signature(
        dependencies,
        [
            "daem0nmcp",
            "v7",
            "active-context-cursor",
            workspace_id,
            snapshot_hash,
            active_context_id,
            selection_expires_at_us,
        ],
    )
    return f"cur_{active_context_id[4:]}_{selection_expires_at_us:x}_{signature}"


def _cursor_start(
    dependencies: LocalStateOperationDependencies,
    workspace_id: str,
    snapshot_hash: str,
    cursor: object,
    rows: list[sqlite3.Row],
    now_us: int,
) -> tuple[int, int]:
    if cursor is None:
        expires_at_us = now_us + dependencies.selection_ttl_seconds * 1_000_000
        if expires_at_us > 2**63 - 1:
            raise LocalStateOperationError("CAPABILITY_DEGRADED")
        return 0, expires_at_us
    if not isinstance(cursor, str):
        raise LocalStateOperationError("INVALID_ARGUMENT")
    match = _CURSOR_RE.fullmatch(cursor)
    if match is None:
        raise LocalStateOperationError("INVALID_ARGUMENT")
    active_context_id = f"act_{match.group(1)}"
    try:
        expires_at_us = int(match.group(2), 16)
    except ValueError:
        raise LocalStateOperationError("INVALID_ARGUMENT") from None
    expected = _cursor(
        dependencies,
        workspace_id,
        snapshot_hash,
        active_context_id,
        expires_at_us,
    )
    if not hmac.compare_digest(cursor, expected):
        raise LocalStateOperationError("INVALID_ARGUMENT")
    if expires_at_us <= now_us:
        raise LocalStateOperationError("INVALID_ARGUMENT")
    for index, row in enumerate(rows):
        if row["active_context_id"] == active_context_id:
            return index + 1, expires_at_us
    raise LocalStateOperationError("INVALID_ARGUMENT")


def _translate_error(error: Exception) -> LocalStateOperationError:
    if isinstance(error, LocalStateOperationError):
        return error
    code = getattr(error, "code", None)
    if isinstance(code, str) and code in STABLE_ERROR_CODE_SET:
        return LocalStateOperationError(code)
    if isinstance(error, sqlite3.OperationalError) and any(
        word in str(error).casefold() for word in ("busy", "locked")
    ):
        return LocalStateOperationError("DATABASE_IN_USE")
    return LocalStateOperationError("CAPABILITY_DEGRADED")


async def _run_read(operation: Callable[[], Any]) -> Any:
    worker = asyncio.create_task(_LOCAL_STATE_WORKERS.run(operation))
    try:
        return await asyncio.shield(worker)
    except asyncio.CancelledError as cancellation:
        with suppress(asyncio.CancelledError, Exception):
            await await_task_terminal(worker)
        raise cancellation
    except BoundedWorkerBusyError as exc:
        raise LocalStateOperationError("TASK_REQUIRED") from exc


async def _run_mutation(
    operation: Callable[[threading.Event], Any],
) -> Any:
    cancelled = threading.Event()
    worker = asyncio.create_task(_LOCAL_STATE_WORKERS.run(lambda: operation(cancelled)))
    try:
        return await asyncio.shield(worker)
    except asyncio.CancelledError as cancellation:
        cancelled.set()
        try:
            result = await await_task_terminal(worker)
        except Exception:
            raise cancellation from None
        return result
    except BoundedWorkerBusyError as exc:
        raise LocalStateOperationError("TASK_REQUIRED") from exc


def _require_active_capacity(
    connection: sqlite3.Connection,
    workspace_id: str,
    now_us: int,
) -> None:
    current_count = connection.execute(
        "SELECT count(*) FROM active_context_entries "
        "WHERE workspace_id=? AND removed_at_us IS NULL "
        "AND (expires_at_us IS NULL OR expires_at_us>?)",
        (workspace_id, now_us),
    ).fetchone()
    if (
        current_count is None
        or type(current_count[0]) is not int
        or current_count[0] >= _MAX_ACTIVE_ENTRIES
    ):
        raise LocalStateOperationError("CAPABILITY_DEGRADED")


def _add_sync(
    dependencies: LocalStateOperationDependencies,
    workspace: Workspace,
    request: AdmittedRequest,
    cancelled: threading.Event,
) -> ActiveContextItem:
    now_us = _now_us(dependencies)
    expires_at_us = (
        None if request.expires_at is None else _datetime_us(request.expires_at)
    )
    if expires_at_us is not None and expires_at_us <= now_us:
        raise LocalStateOperationError("INVALID_ARGUMENT")
    if cancelled.is_set():
        raise _WorkerCancelledError()
    active_context_id = active_context_id_for_record(
        workspace.workspace_id,
        request.record_id,
    )
    try:
        with dependencies.storage_resolver.locked_active(workspace) as active:
            connection = _open_database(active.path, writable=True)
            try:
                connection.execute("BEGIN IMMEDIATE")
                record_rows = connection.execute(
                    f"SELECT {_RECORD_COLUMNS} FROM memory_records record "
                    "WHERE record.workspace_id=? AND record.record_id=? LIMIT 2",
                    (workspace.workspace_id, request.record_id),
                ).fetchall()
                if not record_rows:
                    raise LocalStateOperationError("NOT_FOUND")
                if len(record_rows) != 1:
                    raise LocalStateOperationError("CAPABILITY_DEGRADED")
                _record_summary(record_rows[0])
                entry_rows = connection.execute(
                    "SELECT active_context_id,priority,reason,added_at_us,"
                    "expires_at_us,removed_at_us FROM active_context_entries "
                    "WHERE workspace_id=? AND record_id=? LIMIT 2",
                    (workspace.workspace_id, request.record_id),
                ).fetchall()
                if len(entry_rows) > 1:
                    raise LocalStateOperationError("CAPABILITY_DEGRADED")
                event_type: str | None = None
                if not entry_rows:
                    _require_active_capacity(
                        connection,
                        workspace.workspace_id,
                        now_us,
                    )
                    connection.execute(
                        "INSERT INTO active_context_entries "
                        "(active_context_id,workspace_id,record_id,priority,"
                        "reason,added_at_us,expires_at_us,removed_at_us) "
                        "VALUES (?,?,?,?,?,?,?,NULL)",
                        (
                            active_context_id,
                            workspace.workspace_id,
                            request.record_id,
                            request.priority,
                            request.reason,
                            now_us,
                            expires_at_us,
                        ),
                    )
                    event_type = "active_context.added"
                else:
                    entry = entry_rows[0]
                    if entry["active_context_id"] != active_context_id:
                        raise LocalStateOperationError("CAPABILITY_DEGRADED")
                    was_current = entry["removed_at_us"] is None and (
                        entry["expires_at_us"] is None
                        or entry["expires_at_us"] > now_us
                    )
                    expected = (
                        request.priority,
                        request.reason,
                        expires_at_us,
                        None,
                    )
                    actual = (
                        entry["priority"],
                        entry["reason"],
                        entry["expires_at_us"],
                        entry["removed_at_us"],
                    )
                    if actual != expected:
                        if not was_current:
                            _require_active_capacity(
                                connection,
                                workspace.workspace_id,
                                now_us,
                            )
                        connection.execute(
                            "UPDATE active_context_entries SET priority=?,"
                            "reason=?,added_at_us=?,expires_at_us=?,"
                            "removed_at_us=NULL WHERE active_context_id=? "
                            "AND workspace_id=?",
                            (
                                request.priority,
                                request.reason,
                                now_us,
                                expires_at_us,
                                active_context_id,
                                workspace.workspace_id,
                            ),
                        )
                        event_type = (
                            "active_context.added"
                            if not was_current
                            else "active_context.updated"
                        )
                row = connection.execute(
                    f"SELECT {_ENTRY_COLUMNS},{_RECORD_COLUMNS} "
                    "FROM active_context_entries entry JOIN memory_records record "
                    "ON record.workspace_id=entry.workspace_id "
                    "AND record.record_id=entry.record_id "
                    "WHERE entry.workspace_id=? "
                    "AND entry.active_context_id=? LIMIT 1",
                    (workspace.workspace_id, active_context_id),
                ).fetchone()
                if row is None:
                    raise LocalStateOperationError("CAPABILITY_DEGRADED")
                result = _active_item(row)
                if event_type is not None:
                    _append_active_event(
                        connection,
                        workspace.workspace_id,
                        event_type,
                        _active_state(row),
                        now_us,
                    )
                if cancelled.is_set():
                    raise _WorkerCancelledError()
                connection.commit()
                return result
            except (LocalStateOperationError, _WorkerCancelledError):
                if connection.in_transaction:
                    connection.rollback()
                raise
            except Exception as exc:
                if connection.in_transaction:
                    connection.rollback()
                raise _translate_error(exc) from None
            finally:
                if connection.in_transaction:
                    connection.rollback()
                connection.close()
    except (LocalStateOperationError, _WorkerCancelledError):
        raise
    except Exception as exc:
        raise _translate_error(exc) from None


def _list_sync(
    dependencies: LocalStateOperationDependencies,
    workspace: Workspace,
    request: AdmittedRequest,
) -> ActiveContextPage:
    now_us = _now_us(dependencies)
    try:
        with dependencies.storage_resolver.locked_active(workspace) as active:
            connection = _open_database(active.path, writable=False)
            try:
                rows = _snapshot_rows(connection, workspace.workspace_id, now_us)
                snapshot_hash = _snapshot_hash(workspace.workspace_id, rows)
                start, selection_expires_at_us = _cursor_start(
                    dependencies,
                    workspace.workspace_id,
                    snapshot_hash,
                    request.cursor,
                    rows,
                    now_us,
                )
                selected = rows[start : start + request.limit]
                truncated = start + len(selected) < len(rows)
                next_cursor = None
                if truncated and selected:
                    next_cursor = _cursor(
                        dependencies,
                        workspace.workspace_id,
                        snapshot_hash,
                        selected[-1]["active_context_id"],
                        selection_expires_at_us,
                    )
                return ActiveContextPage(
                    items=[_active_item(row) for row in selected],
                    next_cursor=next_cursor,
                    truncated=truncated,
                    selection_token=_selection_token(
                        dependencies,
                        workspace.workspace_id,
                        snapshot_hash,
                        selection_expires_at_us,
                    ),
                )
            finally:
                connection.close()
    except LocalStateOperationError:
        raise
    except Exception as exc:
        raise _translate_error(exc) from None


def _remove_sync(
    dependencies: LocalStateOperationDependencies,
    workspace: Workspace,
    request: AdmittedRequest,
    cancelled: threading.Event,
) -> MutationReceipt:
    now_us = _now_us(dependencies)
    if cancelled.is_set():
        raise _WorkerCancelledError()
    try:
        with dependencies.storage_resolver.locked_active(workspace) as active:
            connection = _open_database(active.path, writable=True)
            try:
                connection.execute("BEGIN IMMEDIATE")
                rows = connection.execute(
                    "SELECT active_context_id,record_id,priority,reason,"
                    "added_at_us,expires_at_us,removed_at_us "
                    "FROM active_context_entries "
                    "WHERE workspace_id=? AND active_context_id=? LIMIT 2",
                    (workspace.workspace_id, request.active_context_id),
                ).fetchall()
                if not rows:
                    raise LocalStateOperationError("NOT_FOUND")
                if len(rows) != 1:
                    raise LocalStateOperationError("CAPABILITY_DEGRADED")
                replay = rows[0]["removed_at_us"] is not None
                event_ids: list[str] = []
                if not replay:
                    connection.execute(
                        "UPDATE active_context_entries SET removed_at_us=? "
                        "WHERE workspace_id=? AND active_context_id=? "
                        "AND removed_at_us IS NULL",
                        (
                            now_us,
                            workspace.workspace_id,
                            request.active_context_id,
                        ),
                    )
                    removed_state = dict(_active_state(rows[0]))
                    removed_state["removed_at_us"] = now_us
                    event = _append_active_event(
                        connection,
                        workspace.workspace_id,
                        "active_context.removed",
                        removed_state,
                        now_us,
                    )
                    event_ids.append(event.event_id)
                else:
                    previous_event_id = _latest_active_event_id(
                        connection,
                        workspace.workspace_id,
                        request.active_context_id,
                        "active_context.removed",
                    )
                    if previous_event_id is not None:
                        event_ids.append(previous_event_id)
                result = MutationReceipt(
                    operation_id="op_"
                    + sha256_json(
                        [
                            "daem0nmcp",
                            "v7",
                            "active-context-remove",
                            workspace.workspace_id,
                            request.active_context_id,
                        ]
                    ),
                    affected_ids=[request.active_context_id],
                    event_ids=event_ids,
                    counts={"removed": 0 if replay else 1},
                    idempotent_replay=replay,
                )
                if cancelled.is_set():
                    raise _WorkerCancelledError()
                connection.commit()
                return result
            except (LocalStateOperationError, _WorkerCancelledError):
                if connection.in_transaction:
                    connection.rollback()
                raise
            except Exception as exc:
                if connection.in_transaction:
                    connection.rollback()
                raise _translate_error(exc) from None
            finally:
                if connection.in_transaction:
                    connection.rollback()
                connection.close()
    except (LocalStateOperationError, _WorkerCancelledError):
        raise
    except Exception as exc:
        raise _translate_error(exc) from None


def _journal_key(selection_token: str) -> str:
    return sha256_json(
        ["daem0nmcp", "v7", "active-context-clear-journal", selection_token]
    )


def _journal_replay(
    row: sqlite3.Row,
    expected_job_id: str,
    expected_payload: str,
    expected_payload_hash: str,
) -> DestructiveMutationReceipt:
    if (
        row["job_id"] != expected_job_id
        or row["payload_json"] != expected_payload
        or row["payload_hash"] != expected_payload_hash
        or row["status"] != "succeeded"
        or not isinstance(row["result_json"], str)
    ):
        raise LocalStateOperationError("IDEMPOTENCY_CONFLICT")
    try:
        result = DestructiveMutationReceipt.model_validate_json(row["result_json"])
    except Exception:
        raise LocalStateOperationError("CAPABILITY_DEGRADED") from None
    return result.model_copy(update={"idempotent_replay": True})


def _clear_sync(
    dependencies: LocalStateOperationDependencies,
    workspace: Workspace,
    request: AdmittedRequest,
    cancelled: threading.Event,
) -> DestructiveMutationReceipt:
    now_us = _now_us(dependencies)
    selected_hash = _authenticate_selection(
        dependencies,
        workspace.workspace_id,
        request.selection_token,
        now_us,
    )
    if cancelled.is_set():
        raise _WorkerCancelledError()
    journal_key = _journal_key(request.selection_token)
    payload = {
        "selection_token_hash": journal_key,
        "snapshot_hash": selected_hash,
    }
    payload_json = canonical_json_bytes(payload).decode("utf-8")
    payload_hash = sha256_json(payload)
    job_id = deterministic_id(
        "job",
        "v7.active_context_clear",
        workspace.workspace_id,
        journal_key,
    )
    try:
        with dependencies.storage_resolver.locked_active(workspace) as active:
            connection = _open_database(active.path, writable=True)
            try:
                connection.execute("BEGIN IMMEDIATE")
                journal = connection.execute(
                    "SELECT job_id,payload_json,payload_hash,status,result_json "
                    "FROM background_jobs WHERE workspace_id=? "
                    "AND job_type='v7.active_context_clear' "
                    "AND idempotency_key=? LIMIT 2",
                    (workspace.workspace_id, journal_key),
                ).fetchall()
                if len(journal) > 1:
                    raise LocalStateOperationError("CAPABILITY_DEGRADED")
                if journal:
                    result = _journal_replay(
                        journal[0],
                        job_id,
                        payload_json,
                        payload_hash,
                    )
                    if cancelled.is_set():
                        raise _WorkerCancelledError()
                    connection.commit()
                    return result
                rows = _snapshot_rows(connection, workspace.workspace_id, now_us)
                current_hash = _snapshot_hash(workspace.workspace_id, rows)
                if not hmac.compare_digest(selected_hash, current_hash):
                    raise LocalStateOperationError("CONFLICT")
                affected_ids = [str(row["active_context_id"]) for row in rows]
                changed = 0
                event_ids: list[str] = []
                if affected_ids:
                    placeholders = ",".join("?" for _ in affected_ids)
                    cursor = connection.execute(
                        "UPDATE active_context_entries SET removed_at_us=? "
                        "WHERE workspace_id=? AND removed_at_us IS NULL "
                        f"AND active_context_id IN ({placeholders})",
                        (now_us, workspace.workspace_id, *affected_ids),
                    )
                    changed = cursor.rowcount
                    if changed != len(affected_ids):
                        raise LocalStateOperationError("CONFLICT")
                    for row in rows:
                        removed_state = _active_state(row)
                        removed_state["removed_at_us"] = now_us
                        event = _append_active_event(
                            connection,
                            workspace.workspace_id,
                            "active_context.removed",
                            removed_state,
                            now_us,
                        )
                        event_ids.append(event.event_id)
                result = DestructiveMutationReceipt(
                    operation_id="op_"
                    + sha256_json(
                        [
                            "daem0nmcp",
                            "v7",
                            "active-context-clear",
                            workspace.workspace_id,
                            journal_key,
                        ]
                    ),
                    affected_ids=affected_ids,
                    event_ids=event_ids,
                    counts={"removed": changed},
                    idempotent_replay=False,
                    selected_count=len(affected_ids),
                    changed_count=changed,
                    skipped_count=len(affected_ids) - changed,
                )
                result_json = canonical_json_bytes(
                    result.model_dump(mode="json")
                ).decode("utf-8")
                connection.execute(
                    "INSERT INTO background_jobs ("
                    "job_id,workspace_id,job_type,idempotency_key,payload_json,"
                    "payload_hash,status,priority,attempts,max_attempts,"
                    "available_at_us,lease_owner,lease_token,"
                    "lease_expires_at_us,cancel_requested_at_us,last_error_json,"
                    "result_json,source_event_id,created_at_us,updated_at_us,"
                    "started_at_us,finished_at_us) VALUES (?,?,?,?,?,?,'succeeded',"
                    "0,1,1,?,NULL,NULL,NULL,NULL,NULL,?,NULL,?,?,?,?)",
                    (
                        job_id,
                        workspace.workspace_id,
                        "v7.active_context_clear",
                        journal_key,
                        payload_json,
                        payload_hash,
                        now_us,
                        result_json,
                        now_us,
                        now_us,
                        now_us,
                        now_us,
                    ),
                )
                if cancelled.is_set():
                    raise _WorkerCancelledError()
                connection.commit()
                return result
            except (LocalStateOperationError, _WorkerCancelledError):
                if connection.in_transaction:
                    connection.rollback()
                raise
            except Exception as exc:
                if connection.in_transaction:
                    connection.rollback()
                raise _translate_error(exc) from None
            finally:
                if connection.in_transaction:
                    connection.rollback()
                connection.close()
    except (LocalStateOperationError, _WorkerCancelledError):
        raise
    except Exception as exc:
        raise _translate_error(exc) from None


def build_local_state_operations(
    dependencies: LocalStateOperationDependencies,
) -> Mapping[str, Callable[..., Any]]:
    """Return the exact immutable canonical active-context registry."""

    if not isinstance(dependencies, LocalStateOperationDependencies):
        raise TypeError("dependencies must be LocalStateOperationDependencies")

    async def active_context_add(
        *, workspace: Workspace, request: AdmittedRequest
    ) -> ActiveContextItem:
        _authorize(workspace, request, "active_context_add")
        return await _run_mutation(
            lambda cancelled: _add_sync(
                dependencies,
                workspace,
                request,
                cancelled,
            )
        )

    async def active_context_list(
        *, workspace: Workspace, request: AdmittedRequest
    ) -> ActiveContextPage:
        _authorize(workspace, request, "active_context_list")
        return await _run_read(lambda: _list_sync(dependencies, workspace, request))

    async def active_context_remove(
        *, workspace: Workspace, request: AdmittedRequest
    ) -> MutationReceipt:
        _authorize(workspace, request, "active_context_remove")
        return await _run_mutation(
            lambda cancelled: _remove_sync(
                dependencies,
                workspace,
                request,
                cancelled,
            )
        )

    async def active_context_clear(
        *, workspace: Workspace, request: AdmittedRequest
    ) -> DestructiveMutationReceipt:
        _authorize(workspace, request, "active_context_clear")
        return await _run_mutation(
            lambda cancelled: _clear_sync(
                dependencies,
                workspace,
                request,
                cancelled,
            )
        )

    return MappingProxyType(
        {
            "active_context_add": active_context_add,
            "active_context_clear": active_context_clear,
            "active_context_list": active_context_list,
            "active_context_remove": active_context_remove,
        }
    )


__all__ = [
    "LocalStateOperationDependencies",
    "LocalStateOperationError",
    "build_local_state_operations",
]
