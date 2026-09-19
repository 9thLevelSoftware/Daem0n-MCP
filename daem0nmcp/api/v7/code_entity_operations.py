"""Canonical v7 TODO-storage and entity-evolution operations."""

# ruff: noqa: I001 -- preserve the repository's CRLF import block.

from __future__ import annotations

import asyncio
import json
import os
import re
import sqlite3
import threading
import unicodedata
from collections.abc import Callable, Mapping
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import MappingProxyType
from typing import Any

from ...bounded_workers import (
    BoundedWorkerBusyError,
    BoundedWorkerPool,
)
from ...event_store import (
    EventCommand,
    EventStore,
    EventStreamConflict,
    canonical_json_bytes,
    deterministic_id,
    event_hash_for,
    event_id_for_hash,
    memory_content_hash,
    memory_state_hash,
    parse_canonical_json,
    sha256_json,
)
from ...schema_version import CURRENT_SCHEMA_VERSION
from ...workspace import Workspace, WorkspaceRegistry
from .application import AdmittedRequest
from .discovery_operations import (
    _active_projection,
    _code_summary,
    _validate_code_partition,
    _validate_entity_partition,
)
from .errors import STABLE_ERROR_CODE_SET
from .models import (
    EvidenceRef,
    RecordSummary,
)
from .public_ids import (
    PublicObjectIdNotFound,
    PublicObjectIdRepository,
    PublicObjectKind,
)
from .runtime_protocols import ActiveStorageResolver, WorkerPool
from .runtime_services import WorkspaceStorageResolver
from .tools import (
    CodeImpactData,
    CodeImpactPath,
    CodeTodosStoreData,
    EntityEvolutionData,
    EntityEvolutionItem,
    EntitySummary,
    TodoFinding,
)
from .utility_operations import _cursor_offset as _todo_cursor_offset
from .utility_operations import _findings as _todo_findings
from .utility_operations import _relative_target as _todo_relative_target
from .utility_operations import _selector_digest as _todo_selector_digest


_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
_MAX_TODO_RECORDS = 500
_MAX_ENTITY_RECORDS = 200
_MAX_ENTITY_EVENTS = 200
_MAX_IMPACT_ENTITIES = 500
_MAX_IMPACT_EDGES = 5_000
_EVENT_TYPE_RE = re.compile(r"^[a-z][a-z0-9_.-]{2,79}$")
_EVENT_COLUMNS = (
    "event_id,workspace_id,stream_id,stream_kind,stream_version,event_type,"
    "event_schema_version,occurred_at_us,recorded_at_us,actor_type,actor_id,"
    "causation_event_id,correlation_id,payload_json,payload_hash,"
    "previous_event_hash,event_hash"
)
_REQUIRED_TABLES = frozenset(
    {
        "discovery_entities",
        "discovery_code_edges",
        "discovery_code_entities",
        "discovery_entity_records",
        "discovery_projection_partitions",
        "memory_events",
        "memory_records",
        "projection_manifests",
        "public_object_ids",
        "schema_version",
    }
)


class CodeEntityOperationError(RuntimeError):
    """Stable, path-free failure understood by the shared v7 router."""

    def __init__(self, code: str) -> None:
        if code not in STABLE_ERROR_CODE_SET:
            raise ValueError("code/entity operation error code is not stable")
        self.code = code
        super().__init__(code)


class _WorkerCancelledError(RuntimeError):
    pass


def _default_clock() -> datetime:
    return datetime.now(timezone.utc)


def _default_worker_pool() -> BoundedWorkerPool:
    return BoundedWorkerPool(
        max_workers=4,
        thread_name_prefix="daem0nmcp-v7-code-entity",
    )


@dataclass(frozen=True, slots=True)
class CodeEntityOperationDependencies:
    """Owned dependencies for the focused canonical operation slice."""

    operation_secret: bytes
    storage_resolver: ActiveStorageResolver = field(
        default_factory=WorkspaceStorageResolver
    )
    clock: Callable[[], datetime] = _default_clock
    worker_pool: WorkerPool = field(default_factory=_default_worker_pool)

    def __post_init__(self) -> None:
        if (
            not isinstance(self.operation_secret, bytes)
            or len(self.operation_secret) < 32
        ):
            raise ValueError("operation_secret must contain at least 32 bytes")
        if not callable(getattr(self.storage_resolver, "locked_active", None)):
            raise TypeError("storage_resolver must provide locked_active")
        if not callable(self.clock):
            raise TypeError("clock must be callable")
        if not callable(getattr(self.worker_pool, "run", None)) or not callable(
            getattr(self.worker_pool, "shutdown", None)
        ):
            raise TypeError("worker_pool must provide run and shutdown")

    def close(self) -> None:
        self.worker_pool.shutdown()


def _authorize(
    workspace: Workspace,
    request: AdmittedRequest,
    tool_name: str,
) -> Path:
    if (
        not isinstance(workspace, Workspace)
        or not isinstance(request, AdmittedRequest)
        or request.tool_name != tool_name
        or request.workspace_id != workspace.workspace_id
    ):
        raise CodeEntityOperationError("UNAUTHORIZED_WORKSPACE")
    try:
        root = workspace.root.resolve(strict=True)
        registered = WorkspaceRegistry([root], default_root=root).default
        exact = os.path.normcase(str(root)) == os.path.normcase(str(workspace.root))
    except (OSError, RuntimeError, TypeError, ValueError):
        raise CodeEntityOperationError("UNAUTHORIZED_WORKSPACE") from None
    if registered.workspace_id != workspace.workspace_id or not exact:
        raise CodeEntityOperationError("UNAUTHORIZED_WORKSPACE")
    return root


def _database_path(workspace: Workspace, active: Any) -> Path:
    try:
        storage = (workspace.root / ".daem0nmcp" / "storage").resolve(strict=True)
        path = Path(active.path).resolve(strict=True)
        path.relative_to(storage)
        if path.is_symlink() or not path.is_file():
            raise ValueError
        return path
    except (OSError, RuntimeError, TypeError, ValueError):
        raise CodeEntityOperationError("CAPABILITY_DEGRADED") from None


def _open_database(path: Path, *, writable: bool) -> sqlite3.Connection:
    connection: sqlite3.Connection | None = None
    try:
        mode = "rw" if writable else "ro"
        connection = sqlite3.connect(
            f"{path.as_uri()}?mode={mode}",
            uri=True,
            timeout=5.0,
            isolation_level=None,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=5000")
        connection.execute("PRAGMA foreign_keys=ON")
        if not writable:
            connection.execute("PRAGMA query_only=ON")
        foreign_keys = connection.execute("PRAGMA foreign_keys").fetchone()
        version = connection.execute(
            "SELECT COALESCE(MAX(version),0) FROM schema_version"
        ).fetchone()
        tables = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
            if str(row[0]) in _REQUIRED_TABLES
        }
        if (
            foreign_keys is None
            or int(foreign_keys[0]) != 1
            or version is None
            or int(version[0]) != CURRENT_SCHEMA_VERSION
            or tables != _REQUIRED_TABLES
        ):
            raise CodeEntityOperationError("CAPABILITY_DEGRADED")
        return connection
    except CodeEntityOperationError:
        if connection is not None:
            connection.close()
        raise
    except Exception:
        if connection is not None:
            connection.close()
        raise CodeEntityOperationError("CAPABILITY_DEGRADED") from None


def _datetime_us(value: object) -> int:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise CodeEntityOperationError("INVALID_ARGUMENT")
    try:
        delta = value.astimezone(timezone.utc) - _EPOCH
        result = (delta.days * 86_400 + delta.seconds) * 1_000_000 + delta.microseconds
    except (OverflowError, ValueError):
        raise CodeEntityOperationError("INVALID_ARGUMENT") from None
    if not -(2**63) <= result <= 2**63 - 1:
        raise CodeEntityOperationError("INVALID_ARGUMENT")
    return result


def _datetime_from_us(value: object) -> datetime:
    if isinstance(value, bool) or not isinstance(value, int):
        raise CodeEntityOperationError("CAPABILITY_DEGRADED")
    try:
        return _EPOCH + timedelta(microseconds=value)
    except (OverflowError, ValueError):
        raise CodeEntityOperationError("CAPABILITY_DEGRADED") from None


def _now_us(dependencies: CodeEntityOperationDependencies) -> int:
    try:
        return _datetime_us(dependencies.clock())
    except CodeEntityOperationError:
        raise
    except Exception:
        raise CodeEntityOperationError("CAPABILITY_DEGRADED") from None


def _verified_event(row: sqlite3.Row) -> dict[str, Any]:
    try:
        payload_text = str(row["payload_json"])
        payload = parse_canonical_json(payload_text)
        if (
            not isinstance(payload, dict)
            or canonical_json_bytes(payload).decode("utf-8") != payload_text
            or sha256_json(payload) != row["payload_hash"]
        ):
            raise ValueError
        envelope = {
            key: row[key]
            for key in (
                "actor_id",
                "actor_type",
                "causation_event_id",
                "correlation_id",
                "event_schema_version",
                "event_type",
                "occurred_at_us",
                "payload_hash",
                "previous_event_hash",
                "recorded_at_us",
                "stream_id",
                "stream_kind",
                "stream_version",
                "workspace_id",
            )
        }
        calculated = event_hash_for(envelope)
        if (
            calculated != row["event_hash"]
            or event_id_for_hash(calculated) != row["event_id"]
        ):
            raise ValueError
        return payload
    except Exception:
        raise CodeEntityOperationError("CAPABILITY_DEGRADED") from None


def _parse_json(value: object, expected: type) -> Any:
    try:
        parsed = json.loads(str(value))
    except (TypeError, ValueError, RecursionError):
        raise CodeEntityOperationError("CAPABILITY_DEGRADED") from None
    if not isinstance(parsed, expected):
        raise CodeEntityOperationError("CAPABILITY_DEGRADED")
    return parsed


def _row_state(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "record_type": row["record_type"],
        "legacy_type": row["legacy_type"],
        "content": row["content"],
        "rationale": row["rationale"],
        "context": _parse_json(row["context_json"], dict),
        "tags": _parse_json(row["tags_json"], list),
        "file_path": row["file_path"],
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


def _validated_record_row(
    connection: sqlite3.Connection,
    workspace_id: str,
    record_id: str,
    event_id: str,
    expected_state: Mapping[str, Any],
    expected_version: int,
) -> sqlite3.Row:
    rows = connection.execute(
        "SELECT * FROM memory_records WHERE workspace_id=? AND record_id=? LIMIT 2",
        (workspace_id, record_id),
    ).fetchall()
    if len(rows) != 1:
        raise CodeEntityOperationError("CAPABILITY_DEGRADED")
    row = rows[0]
    state = _row_state(row)
    if (
        state != dict(expected_state)
        or row["source_event_id"] != event_id
        or row["stream_version"] != expected_version
        or row["state_hash"] != memory_state_hash(state)
        or row["content_hash"] != memory_content_hash(state)
        or row["file_path"] is not None
    ):
        raise CodeEntityOperationError("CAPABILITY_DEGRADED")
    return row


def _event_record_summary(
    record_id: str,
    expected_state: Mapping[str, Any],
    occurred_at_us: object,
    recorded_at_us: object,
) -> RecordSummary:
    state = dict(expected_state)
    content = state["content"]
    if not isinstance(content, str) or not content:
        raise CodeEntityOperationError("CAPABILITY_DEGRADED")
    created = _datetime_from_us(occurred_at_us)
    updated = _datetime_from_us(recorded_at_us)
    if created > updated:
        raise CodeEntityOperationError("CAPABILITY_DEGRADED")
    try:
        return RecordSummary(
            record_id=record_id,
            record_type=state["record_type"],
            excerpt=content[:4000],
            tags=state["tags"],
            relative_file_path=state["file_path_relative"],
            current_status=(
                "invalidated"
                if state["deleted_at_us"] is not None
                else "archived"
                if state["archived"]
                else "current"
            ),
            content_hash=memory_content_hash(state),
            created_at=created,
            updated_at=updated,
        )
    except Exception:
        raise CodeEntityOperationError("CAPABILITY_DEGRADED") from None


def _todo_state(finding: TodoFinding, record_type: str) -> dict[str, Any]:
    content = (
        f"{finding.todo_type.upper()} at {finding.relative_file_path}:"
        f"{finding.line}: {finding.text}"
    )
    return {
        "record_type": record_type,
        "legacy_type": None,
        "content": content,
        "rationale": "Recorded from a bounded canonical v7 TODO scan.",
        "context": {
            "code_todo": finding.model_dump(mode="json"),
        },
        "tags": ["code-todo", finding.todo_type],
        "file_path": None,
        "file_path_relative": finding.relative_file_path,
        "keywords": None,
        "is_permanent": False,
        "pinned": False,
        "archived": False,
        "outcome": None,
        "worked": None,
        "recall_count": 0,
        "surprise_score": None,
        "importance_score": None,
        "source_client": "daem0nmcp-v7",
        "source_model": None,
        "deleted_at_us": None,
    }


def _todo_empty_state(
    request: AdmittedRequest,
    request_hash: str,
) -> dict[str, Any]:
    selected_types = sorted(request.types or {"todo", "fixme", "hack", "xxx", "note"})
    return {
        "record_type": request.record_type,
        "legacy_type": None,
        "content": (
            f"Archived TODO scan receipt for {request.relative_root}: "
            f"no {', '.join(selected_types)} findings."
        ),
        "rationale": ("Binds an empty canonical v7 TODO scan to its idempotency key."),
        "context": {
            "code_todo_scan_receipt": {
                "finding_count": 0,
                "request_hash": request_hash,
            }
        },
        "tags": ["code-todo-scan", "receipt"],
        "file_path": None,
        "file_path_relative": None,
        "keywords": None,
        "is_permanent": False,
        "pinned": False,
        "archived": True,
        "outcome": None,
        "worked": None,
        "recall_count": 0,
        "surprise_score": None,
        "importance_score": None,
        "source_client": "daem0nmcp-v7",
        "source_model": None,
        "deleted_at_us": None,
    }


def _todo_request_hash(request: AdmittedRequest) -> str:
    return sha256_json(
        {
            "relative_root": request.relative_root,
            "types": (None if request.types is None else sorted(request.types)),
            "cursor": request.cursor,
            "limit": request.limit,
            "record_type": request.record_type,
        }
    )


def _todo_correlation(workspace_id: str, key: str) -> str:
    return deterministic_id(
        "job",
        "code-todos-scan-and-store-idempotency",
        workspace_id,
        key,
    )


def _todo_record_id(workspace_id: str, key: str, index: int) -> str:
    return deterministic_id(
        "mem",
        "code-todos-scan-and-store",
        workspace_id,
        key,
        index,
    )


def _todo_empty_record_id(workspace_id: str, key: str) -> str:
    return deterministic_id(
        "mem",
        "code-todos-scan-and-store-empty-receipt",
        workspace_id,
        key,
    )


def _todo_page(
    dependencies: CodeEntityOperationDependencies,
    root: Path,
    workspace_id: str,
    request: AdmittedRequest,
) -> list[TodoFinding]:
    try:
        scan_root = _todo_relative_target(root, request.relative_root, directory=True)
        selected_types = frozenset(
            request.types or {"todo", "fixme", "hack", "xxx", "note"}
        )
        selector = _todo_selector_digest(
            workspace_id,
            request.relative_root,
            selected_types,
        )
        offset = (
            0
            if request.cursor is None
            else _todo_cursor_offset(
                dependencies.operation_secret,
                request.cursor,
                selector,
            )
        )
        findings = _todo_findings(root, scan_root, selected_types)
        if offset > len(findings):
            raise CodeEntityOperationError("INVALID_ARGUMENT")
        selected = findings[offset : offset + request.limit]
        if len(selected) > _MAX_TODO_RECORDS:
            raise CodeEntityOperationError("TASK_REQUIRED")
        return selected
    except CodeEntityOperationError:
        raise
    except Exception as error:
        code = getattr(error, "code", None)
        if isinstance(code, str) and code in STABLE_ERROR_CODE_SET:
            raise CodeEntityOperationError(code) from None
        raise CodeEntityOperationError("CAPABILITY_DEGRADED") from None


def _existing_todo_result(
    connection: sqlite3.Connection,
    workspace_id: str,
    request: AdmittedRequest,
    request_hash: str,
    correlation: str,
) -> CodeTodosStoreData | None:
    rows = connection.execute(
        f"SELECT {_EVENT_COLUMNS} FROM memory_events WHERE workspace_id=? "
        "AND correlation_id=? LIMIT ?",
        (workspace_id, correlation, _MAX_TODO_RECORDS + 1),
    ).fetchall()
    if not rows:
        return None
    if len(rows) > _MAX_TODO_RECORDS:
        raise CodeEntityOperationError("CAPABILITY_DEGRADED")
    verified = [(row, _verified_event(row)) for row in rows]
    for _row, payload in verified:
        if payload.get("idempotency_request_hash") != request_hash:
            raise CodeEntityOperationError("IDEMPOTENCY_CONFLICT")
    empty_rows = [
        (row, payload) for row, payload in verified if payload.get("empty_scan") is True
    ]
    if empty_rows:
        if len(verified) != 1 or len(empty_rows) != 1:
            raise CodeEntityOperationError("CAPABILITY_DEGRADED")
        row, payload = empty_rows[0]
        state = _todo_empty_state(request, request_hash)
        if (
            set(payload)
            != {
                "batch_index",
                "batch_size",
                "empty_scan",
                "idempotency_request_hash",
                "record",
                "scan_snapshot_hash",
            }
            or payload.get("batch_index") is not None
            or payload.get("batch_size") != 0
            or payload.get("record") != state
            or payload.get("scan_snapshot_hash") != sha256_json([])
            or row["stream_kind"] != "memory"
            or row["stream_version"] != 1
            or row["event_type"] != "memory.created"
            or row["previous_event_hash"] is not None
            or row["stream_id"]
            != _todo_empty_record_id(
                workspace_id,
                request.idempotency_key,
            )
        ):
            raise CodeEntityOperationError("CAPABILITY_DEGRADED")
        event_id = str(row["event_id"])
        return CodeTodosStoreData(
            findings=[],
            stored_records=[
                _event_record_summary(
                    str(row["stream_id"]),
                    state,
                    row["occurred_at_us"],
                    row["recorded_at_us"],
                )
            ],
            event_ids=[event_id],
        )
    indexed: dict[int, tuple[sqlite3.Row, TodoFinding, dict[str, Any]]] = {}
    expected_size: int | None = None
    expected_snapshot: str | None = None
    for row, payload in verified:
        index = payload.get("batch_index")
        size = payload.get("batch_size")
        snapshot = payload.get("scan_snapshot_hash")
        try:
            finding = TodoFinding.model_validate(payload.get("todo_finding"))
        except Exception:
            raise CodeEntityOperationError("CAPABILITY_DEGRADED") from None
        if (
            set(payload)
            != {
                "batch_index",
                "batch_size",
                "empty_scan",
                "idempotency_request_hash",
                "record",
                "scan_snapshot_hash",
                "todo_finding",
            }
            or payload.get("empty_scan") is not False
            or isinstance(index, bool)
            or not isinstance(index, int)
            or isinstance(size, bool)
            or not isinstance(size, int)
            or not 1 <= size <= _MAX_TODO_RECORDS
            or not 0 <= index < size
            or index in indexed
            or row["stream_kind"] != "memory"
            or row["stream_version"] != 1
            or row["event_type"] != "memory.created"
            or row["previous_event_hash"] is not None
            or row["stream_id"]
            != _todo_record_id(workspace_id, request.idempotency_key, index)
            or not isinstance(snapshot, str)
        ):
            raise CodeEntityOperationError("CAPABILITY_DEGRADED")
        if expected_size is None:
            expected_size = size
            expected_snapshot = snapshot
        elif expected_size != size or expected_snapshot != snapshot:
            raise CodeEntityOperationError("CAPABILITY_DEGRADED")
        record_state = payload.get("record")
        expected_state = _todo_state(finding, request.record_type)
        if record_state != expected_state:
            raise CodeEntityOperationError("CAPABILITY_DEGRADED")
        indexed[index] = (row, finding, expected_state)
    if expected_size is None or set(indexed) != set(range(expected_size)):
        raise CodeEntityOperationError("CAPABILITY_DEGRADED")
    findings = [indexed[index][1] for index in range(expected_size)]
    if sha256_json([item.model_dump(mode="json") for item in findings]) != (
        expected_snapshot
    ):
        raise CodeEntityOperationError("CAPABILITY_DEGRADED")
    summaries: list[RecordSummary] = []
    event_ids: list[str] = []
    for index in range(expected_size):
        row, _finding, state = indexed[index]
        event_id = str(row["event_id"])
        summaries.append(
            _event_record_summary(
                str(row["stream_id"]),
                state,
                row["occurred_at_us"],
                row["recorded_at_us"],
            )
        )
        event_ids.append(event_id)
    return CodeTodosStoreData(
        findings=findings,
        stored_records=summaries,
        event_ids=event_ids,
    )


def _todo_store_sync(
    dependencies: CodeEntityOperationDependencies,
    workspace: Workspace,
    request: AdmittedRequest,
    cancelled: threading.Event,
) -> CodeTodosStoreData:
    root = _authorize(workspace, request, "code_todos_scan_and_store")
    request_hash = _todo_request_hash(request)
    correlation = _todo_correlation(workspace.workspace_id, request.idempotency_key)
    recorded_at_us = _now_us(dependencies)
    if cancelled.is_set():
        raise _WorkerCancelledError()
    try:
        with dependencies.storage_resolver.locked_active(workspace) as active:
            connection = _open_database(
                _database_path(workspace, active), writable=True
            )
            try:
                connection.execute("BEGIN")
                existing = _existing_todo_result(
                    connection,
                    workspace.workspace_id,
                    request,
                    request_hash,
                    correlation,
                )
                connection.rollback()
                if existing is not None:
                    return existing
                if cancelled.is_set():
                    raise _WorkerCancelledError()
                findings = _todo_page(
                    dependencies,
                    root,
                    workspace.workspace_id,
                    request,
                )
                if cancelled.is_set():
                    raise _WorkerCancelledError()
                snapshot_hash = sha256_json(
                    [item.model_dump(mode="json") for item in findings]
                )
                connection.execute("BEGIN IMMEDIATE")
                existing = _existing_todo_result(
                    connection,
                    workspace.workspace_id,
                    request,
                    request_hash,
                    correlation,
                )
                if existing is not None:
                    connection.rollback()
                    return existing
                store = EventStore(connection, assume_transaction=True)
                if not findings:
                    state = _todo_empty_state(request, request_hash)
                    record_id = _todo_empty_record_id(
                        workspace.workspace_id,
                        request.idempotency_key,
                    )
                    event = store.append_and_project(
                        EventCommand(
                            workspace_id=workspace.workspace_id,
                            stream_id=record_id,
                            stream_kind="memory",
                            event_type="memory.created",
                            occurred_at_us=recorded_at_us,
                            recorded_at_us=recorded_at_us,
                            actor_type="client",
                            correlation_id=correlation,
                            expected_stream_version=1,
                            payload={
                                "record": state,
                                "idempotency_request_hash": request_hash,
                                "batch_index": None,
                                "batch_size": 0,
                                "scan_snapshot_hash": snapshot_hash,
                                "empty_scan": True,
                            },
                        )
                    )
                    result = CodeTodosStoreData(
                        findings=[],
                        stored_records=[
                            _event_record_summary(
                                record_id,
                                state,
                                recorded_at_us,
                                recorded_at_us,
                            )
                        ],
                        event_ids=[event.event_id],
                    )
                    if cancelled.is_set():
                        raise _WorkerCancelledError()
                    connection.commit()
                    return result
                event_ids: list[str] = []
                states: list[dict[str, Any]] = []
                for index, finding in enumerate(findings):
                    if cancelled.is_set():
                        raise _WorkerCancelledError()
                    record_id = _todo_record_id(
                        workspace.workspace_id,
                        request.idempotency_key,
                        index,
                    )
                    state = _todo_state(finding, request.record_type)
                    event = store.append_and_project(
                        EventCommand(
                            workspace_id=workspace.workspace_id,
                            stream_id=record_id,
                            stream_kind="memory",
                            event_type="memory.created",
                            occurred_at_us=recorded_at_us,
                            recorded_at_us=recorded_at_us,
                            actor_type="client",
                            correlation_id=correlation,
                            expected_stream_version=1,
                            payload={
                                "record": state,
                                "idempotency_request_hash": request_hash,
                                "batch_index": index,
                                "batch_size": len(findings),
                                "scan_snapshot_hash": snapshot_hash,
                                "todo_finding": finding.model_dump(mode="json"),
                                "empty_scan": False,
                            },
                        )
                    )
                    event_ids.append(event.event_id)
                    states.append(state)
                summaries = [
                    _event_record_summary(
                        _todo_record_id(
                            workspace.workspace_id,
                            request.idempotency_key,
                            index,
                        ),
                        states[index],
                        recorded_at_us,
                        recorded_at_us,
                    )
                    for index in range(len(findings))
                ]
                if cancelled.is_set():
                    raise _WorkerCancelledError()
                connection.commit()
                return CodeTodosStoreData(
                    findings=findings,
                    stored_records=summaries,
                    event_ids=event_ids,
                )
            except (
                CodeEntityOperationError,
                EventStreamConflict,
                _WorkerCancelledError,
            ):
                if connection.in_transaction:
                    connection.rollback()
                raise
            except Exception:
                if connection.in_transaction:
                    connection.rollback()
                raise CodeEntityOperationError("CAPABILITY_DEGRADED") from None
            finally:
                if connection.in_transaction:
                    connection.rollback()
                connection.close()
    except (CodeEntityOperationError, _WorkerCancelledError):
        raise
    except EventStreamConflict:
        raise CodeEntityOperationError("EVENT_STREAM_CONFLICT") from None
    except Exception as error:
        code = getattr(error, "code", None)
        if isinstance(code, str) and code in STABLE_ERROR_CODE_SET:
            raise CodeEntityOperationError(code) from None
        raise CodeEntityOperationError("CAPABILITY_DEGRADED") from None


def _entity_row(
    connection: sqlite3.Connection,
    workspace_id: str,
    generation: int,
    request: AdmittedRequest,
) -> sqlite3.Row:
    if request.entity_id is not None:
        try:
            PublicObjectIdRepository(connection).resolve_public_id(
                workspace_id,
                PublicObjectKind.ENTITY,
                request.entity_id,
            )
        except PublicObjectIdNotFound:
            raise CodeEntityOperationError("NOT_FOUND") from None
        except Exception:
            raise CodeEntityOperationError("CAPABILITY_DEGRADED") from None
        rows = connection.execute(
            "SELECT entity_id,name,entity_type,mention_count "
            "FROM discovery_entities WHERE workspace_id=? "
            "AND graph_generation=? AND entity_id=? LIMIT 2",
            (workspace_id, generation, request.entity_id),
        ).fetchall()
        if not rows:
            historical = connection.execute(
                "SELECT 1 FROM discovery_entities WHERE workspace_id=? "
                "AND entity_id=? AND graph_generation<>? LIMIT 1",
                (workspace_id, request.entity_id, generation),
            ).fetchone()
            raise CodeEntityOperationError(
                "STALE_PROJECTION_ID" if historical is not None else "NOT_FOUND"
            )
    else:
        if not isinstance(request.entity_name, str):
            raise CodeEntityOperationError("INVALID_ARGUMENT")
        normalized = unicodedata.normalize("NFC", request.entity_name.casefold())
        where = "workspace_id=? AND graph_generation=? AND normalized_name=?"
        parameters: list[object] = [workspace_id, generation, normalized]
        if request.entity_type is not None:
            where += " AND entity_type=?"
            parameters.append(request.entity_type)
        rows = connection.execute(
            "SELECT entity_id,name,entity_type,mention_count "
            "FROM discovery_entities WHERE " + where + " ORDER BY entity_id LIMIT 2",
            parameters,
        ).fetchall()
    if not rows:
        raise CodeEntityOperationError("NOT_FOUND")
    if len(rows) != 1:
        raise CodeEntityOperationError("CONFLICT")
    return rows[0]


def _safe_event_content(state: Mapping[str, Any]) -> str:
    content = state.get("content")
    # Stored user text reads back as stored (UD-3).
    if not isinstance(content, str) or not content:
        raise CodeEntityOperationError("CAPABILITY_DEGRADED")
    return content


def _event_summary(
    row: sqlite3.Row,
    state: Mapping[str, Any],
    previous: Mapping[str, Any] | None,
) -> str:
    event_type = row["event_type"]
    if not isinstance(event_type, str) or _EVENT_TYPE_RE.fullmatch(event_type) is None:
        raise CodeEntityOperationError("CAPABILITY_DEGRADED")
    content = _safe_event_content(state)
    if previous is None:
        change = "created"
    else:
        fields = (
            "record_type",
            "content",
            "rationale",
            "context",
            "tags",
            "archived",
            "outcome",
            "worked",
            "deleted_at_us",
        )
        changed = [field for field in fields if state.get(field) != previous.get(field)]
        change = ", ".join(changed) if changed else "state"
    return f"{event_type} ({change}): {content[:1000]}"


@dataclass(frozen=True, slots=True)
class _EntityEvent:
    row: sqlite3.Row
    state: Mapping[str, Any]
    previous: Mapping[str, Any] | None


def _entity_events(
    connection: sqlite3.Connection,
    workspace_id: str,
    generation: int,
    entity_id: str,
) -> list[_EntityEvent]:
    members = connection.execute(
        "SELECT record_id FROM discovery_entity_records WHERE workspace_id=? "
        "AND graph_generation=? AND entity_id=? ORDER BY record_id LIMIT ?",
        (workspace_id, generation, entity_id, _MAX_ENTITY_RECORDS + 1),
    ).fetchall()
    if len(members) > _MAX_ENTITY_RECORDS:
        raise CodeEntityOperationError("TASK_REQUIRED")
    record_ids = [str(row[0]) for row in members]
    if not record_ids:
        return []
    placeholders = ",".join("?" for _ in record_ids)
    rows = connection.execute(
        f"SELECT {_EVENT_COLUMNS} FROM memory_events WHERE workspace_id=? "
        f"AND stream_id IN ({placeholders}) AND stream_kind='memory' "
        "ORDER BY stream_id,stream_version LIMIT ?",
        (workspace_id, *record_ids, _MAX_ENTITY_EVENTS + 1),
    ).fetchall()
    if len(rows) > _MAX_ENTITY_EVENTS:
        raise CodeEntityOperationError("TASK_REQUIRED")
    events: list[_EntityEvent] = []
    previous_hash: dict[str, str] = {}
    previous_version: dict[str, int] = {}
    previous_state: dict[str, Mapping[str, Any]] = {}
    latest: dict[str, tuple[sqlite3.Row, Mapping[str, Any]]] = {}
    for row in rows:
        stream_id = str(row["stream_id"])
        version = row["stream_version"]
        if (
            stream_id not in record_ids
            or isinstance(version, bool)
            or not isinstance(version, int)
            or version != previous_version.get(stream_id, 0) + 1
            or row["previous_event_hash"] != previous_hash.get(stream_id)
        ):
            raise CodeEntityOperationError("CAPABILITY_DEGRADED")
        payload = _verified_event(row)
        state = payload.get("record")
        if not isinstance(state, Mapping):
            raise CodeEntityOperationError("CAPABILITY_DEGRADED")
        _safe_event_content(state)
        previous = previous_state.get(stream_id)
        events.append(_EntityEvent(row=row, state=dict(state), previous=previous))
        previous_hash[stream_id] = str(row["event_hash"])
        previous_version[stream_id] = version
        previous_state[stream_id] = dict(state)
        latest[stream_id] = (row, dict(state))
    if set(latest) != set(record_ids):
        raise CodeEntityOperationError("CAPABILITY_DEGRADED")
    for record_id, (row, state) in latest.items():
        _validated_record_row(
            connection,
            workspace_id,
            record_id,
            str(row["event_id"]),
            state,
            int(row["stream_version"]),
        )
    return events


def _entity_evolution_sync(
    dependencies: CodeEntityOperationDependencies,
    workspace: Workspace,
    request: AdmittedRequest,
) -> EntityEvolutionData:
    _authorize(workspace, request, "entity_evolution_trace")
    try:
        with dependencies.storage_resolver.locked_active(workspace) as active:
            connection = _open_database(
                _database_path(workspace, active), writable=False
            )
            try:
                connection.execute("BEGIN")
                manifest = _active_projection(
                    connection, workspace.workspace_id, "graph"
                )
                _validate_entity_partition(
                    connection,
                    workspace.workspace_id,
                    manifest.generation,
                )
                row = _entity_row(
                    connection,
                    workspace.workspace_id,
                    manifest.generation,
                    request,
                )
                events = _entity_events(
                    connection,
                    workspace.workspace_id,
                    manifest.generation,
                    str(row["entity_id"]),
                )
                if request.include_invalidated:
                    selected = events
                else:
                    latest: dict[str, _EntityEvent] = {}
                    for item in events:
                        latest[str(item.row["stream_id"])] = item
                    selected = [
                        item
                        for item in latest.values()
                        if item.state.get("deleted_at_us") is None
                    ]
                selected.sort(
                    key=lambda item: (
                        item.row["occurred_at_us"],
                        item.row["recorded_at_us"],
                        item.row["event_id"],
                    )
                )
                timeline = [
                    EntityEvolutionItem(
                        happened_at=_datetime_from_us(item.row["occurred_at_us"]),
                        summary=_event_summary(item.row, item.state, item.previous),
                        event_id=str(item.row["event_id"]),
                    )
                    for item in selected
                ]
                evidence_refs = [
                    EvidenceRef(
                        record_id=str(item.row["stream_id"]),
                        event_id=str(item.row["event_id"]),
                        content_hash=memory_content_hash(item.state),
                        provider="canonical-events",
                    )
                    for item in selected
                ]
                result = EntityEvolutionData(
                    entity=EntitySummary(
                        entity_id=str(row["entity_id"]),
                        name=str(row["name"]),
                        entity_type=str(row["entity_type"]),
                        mention_count=row["mention_count"],
                        manifest_generation=manifest.generation,
                    ),
                    timeline=timeline,
                    evidence_refs=evidence_refs,
                )
                connection.rollback()
                return result
            except CodeEntityOperationError:
                if connection.in_transaction:
                    connection.rollback()
                raise
            except Exception as error:
                if connection.in_transaction:
                    connection.rollback()
                code = getattr(error, "code", None)
                if isinstance(code, str) and code in STABLE_ERROR_CODE_SET:
                    raise CodeEntityOperationError(code) from None
                raise CodeEntityOperationError("CAPABILITY_DEGRADED") from None
            finally:
                if connection.in_transaction:
                    connection.rollback()
                connection.close()
    except CodeEntityOperationError:
        raise
    except Exception as error:
        code = getattr(error, "code", None)
        if isinstance(code, str) and code in STABLE_ERROR_CODE_SET:
            raise CodeEntityOperationError(code) from None
        raise CodeEntityOperationError("CAPABILITY_DEGRADED") from None


def _code_impact_sync(
    dependencies: CodeEntityOperationDependencies,
    workspace: Workspace,
    request: AdmittedRequest,
) -> CodeImpactData:
    try:
        with dependencies.storage_resolver.locked_active(workspace) as active:
            connection = _open_database(
                _database_path(workspace, active), writable=False
            )
            try:
                connection.execute("BEGIN")
                manifest = _active_projection(
                    connection, workspace.workspace_id, "code"
                )
                _validate_code_partition(connection, workspace.workspace_id, manifest)
                if request.code_entity_id is not None:
                    rows = connection.execute(
                        "SELECT code_entity_id,kind,qualified_name,"
                        "relative_file_path,start_line,end_line "
                        "FROM discovery_code_entities WHERE workspace_id=? "
                        "AND code_generation=? AND code_entity_id=? LIMIT 2",
                        (
                            workspace.workspace_id,
                            manifest.generation,
                            request.code_entity_id,
                        ),
                    ).fetchall()
                    if not rows:
                        historical = connection.execute(
                            "SELECT 1 FROM discovery_code_entities "
                            "WHERE workspace_id=? AND code_entity_id=? LIMIT 1",
                            (workspace.workspace_id, request.code_entity_id),
                        ).fetchone()
                        raise CodeEntityOperationError(
                            "STALE_PROJECTION_ID"
                            if historical is not None
                            else "NOT_FOUND"
                        )
                else:
                    normalized = unicodedata.normalize(
                        "NFC", request.qualified_name.casefold()
                    )
                    rows = connection.execute(
                        "SELECT code_entity_id,kind,qualified_name,"
                        "relative_file_path,start_line,end_line "
                        "FROM discovery_code_entities WHERE workspace_id=? "
                        "AND code_generation=? AND normalized_name=? "
                        "ORDER BY code_entity_id LIMIT 2",
                        (workspace.workspace_id, manifest.generation, normalized),
                    ).fetchall()
                    if not rows:
                        raise CodeEntityOperationError("NOT_FOUND")
                    if len(rows) != 1:
                        raise CodeEntityOperationError("CONFLICT")
                if len(rows) != 1:
                    raise CodeEntityOperationError("CAPABILITY_DEGRADED")
                subject_row = rows[0]
                subject_id = str(subject_row["code_entity_id"])
                paths: dict[str, tuple[str, ...]] = {subject_id: (subject_id,)}
                frontier = [subject_id]
                traversed_edges = 0
                for _depth in range(request.max_depth):
                    if not frontier:
                        break
                    placeholders = ",".join("?" for _ in frontier)
                    edge_rows = connection.execute(
                        "SELECT target_code_entity_id,source_code_entity_id "
                        "FROM discovery_code_edges WHERE workspace_id=? "
                        "AND code_generation=? AND target_code_entity_id IN ("
                        + placeholders
                        + ") ORDER BY target_code_entity_id,source_code_entity_id,"
                        "edge_kind LIMIT ?",
                        (
                            workspace.workspace_id,
                            manifest.generation,
                            *frontier,
                            _MAX_IMPACT_EDGES - traversed_edges + 1,
                        ),
                    ).fetchall()
                    traversed_edges += len(edge_rows)
                    if traversed_edges > _MAX_IMPACT_EDGES:
                        raise CodeEntityOperationError("TASK_REQUIRED")
                    next_frontier: list[str] = []
                    for edge in edge_rows:
                        target_id = str(edge[0])
                        source_id = str(edge[1])
                        if target_id not in paths:
                            raise CodeEntityOperationError("CAPABILITY_DEGRADED")
                        if source_id in paths:
                            continue
                        path = (*paths[target_id], source_id)
                        if len(path) > 32:
                            raise CodeEntityOperationError("TASK_REQUIRED")
                        paths[source_id] = path
                        next_frontier.append(source_id)
                        if len(paths) - 1 > _MAX_IMPACT_ENTITIES:
                            raise CodeEntityOperationError("TASK_REQUIRED")
                    frontier = sorted(set(next_frontier))
                affected_ids = sorted(
                    (item for item in paths if item != subject_id),
                    key=lambda item: (len(paths[item]), item),
                )
                summaries: dict[str, Any] = {}
                if affected_ids:
                    placeholders = ",".join("?" for _ in affected_ids)
                    affected_rows = connection.execute(
                        "SELECT code_entity_id,kind,qualified_name,"
                        "relative_file_path,start_line,end_line "
                        "FROM discovery_code_entities WHERE workspace_id=? "
                        "AND code_generation=? AND code_entity_id IN ("
                        + placeholders
                        + ")",
                        (
                            workspace.workspace_id,
                            manifest.generation,
                            *affected_ids,
                        ),
                    ).fetchall()
                    summaries = {
                        str(row["code_entity_id"]): _code_summary(
                            row, manifest.generation
                        )
                        for row in affected_rows
                    }
                    if set(summaries) != set(affected_ids):
                        raise CodeEntityOperationError("CAPABILITY_DEGRADED")
                result = CodeImpactData(
                    subject=_code_summary(subject_row, manifest.generation),
                    affected=[summaries[item] for item in affected_ids],
                    paths=[
                        CodeImpactPath(entities=list(paths[item]))
                        for item in affected_ids
                    ],
                    evidence_refs=[],
                )
                connection.rollback()
                return result
            except CodeEntityOperationError:
                if connection.in_transaction:
                    connection.rollback()
                raise
            except Exception as error:
                if connection.in_transaction:
                    connection.rollback()
                code = getattr(error, "code", None)
                if isinstance(code, str) and code in STABLE_ERROR_CODE_SET:
                    raise CodeEntityOperationError(code) from None
                raise CodeEntityOperationError("CAPABILITY_DEGRADED") from None
            finally:
                if connection.in_transaction:
                    connection.rollback()
                connection.close()
    except CodeEntityOperationError:
        raise
    except Exception as error:
        code = getattr(error, "code", None)
        if isinstance(code, str) and code in STABLE_ERROR_CODE_SET:
            raise CodeEntityOperationError(code) from None
        raise CodeEntityOperationError("CAPABILITY_DEGRADED") from None


async def _await_worker_uninterruptibly(worker: asyncio.Task[Any]) -> Any:
    while True:
        try:
            return await asyncio.shield(worker)
        except asyncio.CancelledError:
            if worker.done():
                return worker.result()


async def _run_mutation(
    dependencies: CodeEntityOperationDependencies,
    operation: Callable[[threading.Event], Any],
) -> Any:
    cancelled = threading.Event()
    worker = asyncio.create_task(
        dependencies.worker_pool.run(lambda: operation(cancelled))
    )
    try:
        return await asyncio.shield(worker)
    except asyncio.CancelledError as cancellation:
        cancelled.set()
        try:
            result = await _await_worker_uninterruptibly(worker)
        except asyncio.CancelledError:
            raise cancellation from None
        except (_WorkerCancelledError, BoundedWorkerBusyError):
            raise cancellation from None
        except Exception:
            raise cancellation from None
        return result
    except BoundedWorkerBusyError as error:
        raise CodeEntityOperationError("TASK_REQUIRED") from error


async def _run_read(
    dependencies: CodeEntityOperationDependencies,
    operation: Callable[[], Any],
) -> Any:
    worker = asyncio.create_task(dependencies.worker_pool.run(operation))
    try:
        return await asyncio.shield(worker)
    except asyncio.CancelledError as cancellation:
        with suppress(asyncio.CancelledError, Exception):
            await _await_worker_uninterruptibly(worker)
        raise cancellation from None
    except BoundedWorkerBusyError as error:
        raise CodeEntityOperationError("TASK_REQUIRED") from error


def build_code_entity_operations(
    dependencies: CodeEntityOperationDependencies,
) -> Mapping[str, Callable[..., Any]]:
    """Return the exact immutable handler registry."""

    if not isinstance(dependencies, CodeEntityOperationDependencies):
        raise TypeError("code/entity operation dependencies are required")

    async def code_todos_scan_and_store(
        *, workspace: Workspace, request: AdmittedRequest
    ) -> CodeTodosStoreData:
        _authorize(workspace, request, "code_todos_scan_and_store")
        return await _run_mutation(
            dependencies,
            lambda cancelled: _todo_store_sync(
                dependencies,
                workspace,
                request,
                cancelled,
            ),
        )

    async def entity_evolution_trace(
        *, workspace: Workspace, request: AdmittedRequest
    ) -> EntityEvolutionData:
        _authorize(workspace, request, "entity_evolution_trace")
        return await _run_read(
            dependencies,
            lambda: _entity_evolution_sync(
                dependencies,
                workspace,
                request,
            ),
        )

    async def code_impact_analyze(
        *, workspace: Workspace, request: AdmittedRequest
    ) -> CodeImpactData:
        _authorize(workspace, request, "code_impact_analyze")
        return await _run_read(
            dependencies,
            lambda: _code_impact_sync(
                dependencies,
                workspace,
                request,
            ),
        )

    return MappingProxyType(
        {
            "code_impact_analyze": code_impact_analyze,
            "code_todos_scan_and_store": code_todos_scan_and_store,
            "entity_evolution_trace": entity_evolution_trace,
        }
    )


__all__ = [
    "CodeEntityOperationDependencies",
    "CodeEntityOperationError",
    "build_code_entity_operations",
]
