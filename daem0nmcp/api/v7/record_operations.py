"""Canonical v7 record operations backed by Task 7 and Task 8 seams.

All SQLite access is scoped to a registered workspace's active format-7
generation.  Mutations append complete semantic state through ``EventStore``;
the canonical ``memory_records`` projection is never updated directly here.
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
from pathlib import Path
from types import MappingProxyType
from typing import Any, Protocol, cast

from ...bounded_workers import BoundedWorkerBusyError, BoundedWorkerPool
from ...event_store import (
    AppendedEvent,
    EventCommand,
    EventStore,
    EventStreamConflict,
    canonical_json_bytes,
    deterministic_id,
    sha256_json,
)
from ...retrieval import (
    LexicalProvider,
    ProviderResult,
    RetrievalProvider,
    RetrievalQuery,
    sqlite_read_connection_factory,
)
from ...schema_version import CURRENT_SCHEMA_VERSION
from ...workspace import Workspace, WorkspaceRegistry
from .application import AdmittedRequest
from .errors import STABLE_ERROR_CODE_SET
from .models import (
    MIGRATED_EMPTY_CONTENT,
    EvidenceRef,
    MutationReceipt,
    Page,
    RecordSummary,
    parse_wire_datetime,
    stored_relative_path,
)
from .pinned import IdempotencyConflict
from .runtime_services import WorkspaceStorageResolver
from .tasks import await_task_terminal
from .tools import (
    HighlightSpan,
    MemoryStoreBatchData,
    SessionUpdatesData,
    TextSearchHit,
    UpdateKind,
    UpdateSummary,
)

_SCHEMA_VERSION = CURRENT_SCHEMA_VERSION
_MAX_LEXICAL_CANDIDATES = 1_000
_MAX_SESSION_EVENTS = 200
_EVENT_CURSOR_RE = re.compile(r"^cur_v1_([0-9a-f]{64})_([0-9a-f]{64})$")
_ORIGIN_CURSOR_RE = re.compile(r"^cur_v1_origin_([0-9a-f]{64})$")
_EVENT_ID_RE = re.compile(r"^evt_([0-9a-f]{64})$")
_QUERY_TOKEN_RE = re.compile(r"[A-Za-z0-9_]+")
_FTS_OPERATORS = frozenset({"and", "or", "not", "near"})
_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
_RECORD_COLUMNS = (
    "record_id,workspace_id,record_type,legacy_type,content,content_hash,"
    "rationale,context_json,tags_json,file_path,file_path_relative,keywords,"
    "is_permanent,pinned,archived,outcome,worked,recall_count,surprise_score,"
    "importance_score,source_client,source_model,stream_version,"
    "source_event_id,created_at_us,updated_at_us,deleted_at_us"
)


_RECORD_OPERATION_WORKERS = BoundedWorkerPool(
    max_workers=4,
    thread_name_prefix="daem0nmcp-v7-record",
)


class RecordOperationError(RuntimeError):
    """Stable, path-free business failure understood by the v7 router."""

    def __init__(self, code: str) -> None:
        if code not in STABLE_ERROR_CODE_SET:
            raise ValueError("record operation error code is not stable")
        self.code = code
        super().__init__(code)


class ProjectionScheduler(Protocol):
    def __call__(self, database_path: Path) -> object: ...


class LexicalProviderFactory(Protocol):
    def __call__(self, database_path: Path) -> RetrievalProvider: ...


def _default_clock() -> datetime:
    return datetime.now(timezone.utc)


def _default_projection_scheduler(database_path: Path) -> None:
    from ...retrieval.runtime import schedule_projection_job_drain

    schedule_projection_job_drain(database_path, max_jobs=5)


def _default_lexical_provider(database_path: Path) -> RetrievalProvider:
    return LexicalProvider(
        connection_factory=sqlite_read_connection_factory(
            database_path,
            busy_timeout_seconds=5.0,
        ),
        timeout_seconds=5.0,
    )


@dataclass(frozen=True, slots=True)
class RecordOperationDependencies:
    """Reviewed dependencies for the canonical record-operation slice."""

    storage_resolver: WorkspaceStorageResolver = field(
        default_factory=WorkspaceStorageResolver
    )
    clock: Callable[[], datetime] = field(default=_default_clock)
    projection_scheduler: ProjectionScheduler = _default_projection_scheduler
    lexical_provider_factory: LexicalProviderFactory = _default_lexical_provider
    poll_interval_seconds: float = 0.1
    cursor_secret: bytes = field(default_factory=lambda: secrets.token_bytes(32))

    def __post_init__(self) -> None:
        if not hasattr(self.storage_resolver, "locked_active"):
            raise TypeError("storage_resolver must provide locked_active")
        for name in ("clock", "projection_scheduler", "lexical_provider_factory"):
            if not callable(getattr(self, name)):
                raise TypeError(f"{name} must be callable")
        interval = self.poll_interval_seconds
        if (
            isinstance(interval, bool)
            or not isinstance(interval, (int, float))
            or not 0.001 <= float(interval) <= 1.0
        ):
            raise ValueError("poll_interval_seconds must be between 0.001 and 1")
        if not isinstance(self.cursor_secret, bytes) or len(self.cursor_secret) < 32:
            raise ValueError("cursor_secret must contain at least 32 bytes")


class _WorkerCancelledError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class CanonicalBatchStoreRequest:
    """Explicit, already-authorized input for one atomic event-store batch."""

    records: tuple[Mapping[str, Any], ...]
    idempotency_key: str
    semantic_namespace: str = "memory-store-batch"
    provenance: Mapping[str, Any] | None = None

    def __post_init__(self) -> None:
        if not self.records or len(self.records) > 100:
            raise ValueError("records must contain between 1 and 100 items")
        if not isinstance(self.idempotency_key, str) or not self.idempotency_key:
            raise ValueError("idempotency_key is required")
        if not re.fullmatch(r"[a-z][a-z0-9-]{0,63}", self.semantic_namespace):
            raise ValueError("semantic_namespace is invalid")
        if self.provenance is not None and not isinstance(self.provenance, Mapping):
            raise ValueError("provenance must be a mapping")


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
        raise RecordOperationError("UNAUTHORIZED_WORKSPACE")
    try:
        canonical = workspace.root.resolve(strict=True)
        registered = WorkspaceRegistry([canonical], default_root=canonical).default
        exact_root = os.path.normcase(str(workspace.root)) == os.path.normcase(
            str(canonical)
        )
    except (OSError, RuntimeError, TypeError, ValueError):
        raise RecordOperationError("UNAUTHORIZED_WORKSPACE") from None
    if registered.workspace_id != workspace.workspace_id or not exact_root:
        raise RecordOperationError("UNAUTHORIZED_WORKSPACE")


def _open_database(path: Path, *, writable: bool) -> sqlite3.Connection:
    connection: sqlite3.Connection | None = None
    try:
        mode = "rw" if writable else "ro"
        connection = sqlite3.connect(
            f"{path.resolve().as_uri()}?mode={mode}",
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
        required = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name IN ('memory_events','memory_records',"
                "'retrieval_documents','projection_manifests',"
                "'governance_events','session_update_sequence')"
            )
        }
        if (
            version is None
            or int(version[0]) < _SCHEMA_VERSION
            or required
            != {
                "memory_events",
                "memory_records",
                "retrieval_documents",
                "projection_manifests",
                "governance_events",
                "session_update_sequence",
            }
        ):
            raise RecordOperationError("CAPABILITY_DEGRADED")
        return connection
    except RecordOperationError:
        if connection is not None:
            connection.close()
        raise
    except Exception:
        if connection is not None:
            connection.close()
        raise RecordOperationError("CAPABILITY_DEGRADED") from None


def _datetime_us(value: object) -> int:
    try:
        value = parse_wire_datetime(value)
        delta = value.astimezone(timezone.utc) - _EPOCH
        result = (delta.days * 86_400 + delta.seconds) * 1_000_000 + delta.microseconds
    except (OverflowError, ValueError):
        raise RecordOperationError("INVALID_ARGUMENT") from None
    if not -(2**63) <= result <= 2**63 - 1:
        raise RecordOperationError("INVALID_ARGUMENT")
    return result


def _datetime_from_us(value: object) -> datetime:
    if isinstance(value, bool) or not isinstance(value, int):
        raise RecordOperationError("CAPABILITY_DEGRADED")
    try:
        return _EPOCH + timedelta(microseconds=value)
    except (OverflowError, ValueError):
        raise RecordOperationError("CAPABILITY_DEGRADED") from None


def _now_us(dependencies: RecordOperationDependencies) -> int:
    try:
        value = dependencies.clock()
    except Exception:
        raise RecordOperationError("CAPABILITY_DEGRADED") from None
    return _datetime_us(value)


def _parse_json(value: object, expected: type) -> Any:
    try:
        result = json.loads(str(value))
    except (TypeError, ValueError, RecursionError):
        raise RecordOperationError("CAPABILITY_DEGRADED") from None
    if not isinstance(result, expected):
        raise RecordOperationError("CAPABILITY_DEGRADED")
    return result


def _load_record(
    connection: sqlite3.Connection,
    workspace_id: str,
    record_id: str,
) -> sqlite3.Row:
    rows = connection.execute(
        f"SELECT {_RECORD_COLUMNS} FROM memory_records "
        "WHERE workspace_id=? AND record_id=? LIMIT 2",
        (workspace_id, record_id),
    ).fetchall()
    if not rows:
        raise RecordOperationError("NOT_FOUND")
    if len(rows) != 1:
        raise RecordOperationError("CAPABILITY_DEGRADED")
    # A migrated v6 row keeps the host-absolute ``file_path`` v6 wrote; it is
    # legacy provenance, never emitted, so it must not deny the read.
    return rows[0]


def _record_status(row: sqlite3.Row) -> str:
    if row["deleted_at_us"] is not None:
        return "invalidated"
    if bool(row["archived"]):
        return "archived"
    return "current"


def _summary(
    row: sqlite3.Row,
    *,
    include_metadata: bool = True,
) -> RecordSummary:
    # v6 accepted empty content; the bounded summary renders it as the
    # migration's marker rather than denying the whole read.
    content = row["content"]
    if not isinstance(content, str):
        raise RecordOperationError("CAPABILITY_DEGRADED")
    tags = _parse_json(row["tags_json"], list)
    # A future valid-time may be recorded now; ``RecordSummary`` exposes the
    # transaction-time creation while the event retains the exact valid time.
    created_at = _datetime_from_us(row["created_at_us"])
    updated_at = _datetime_from_us(row["updated_at_us"])
    try:
        return RecordSummary.model_validate(
            {
                "record_id": str(row["record_id"]),
                "record_type": str(row["record_type"]),
                "excerpt": content[:4000],
                "tags": tags if include_metadata else [],
                "relative_file_path": stored_relative_path(row["file_path_relative"])
                if include_metadata
                else None,
                "current_status": _record_status(row),
                "content_hash": str(row["content_hash"]),
                "created_at": created_at,
                "updated_at": updated_at,
            }
        )
    except RecordOperationError:
        raise
    except Exception:
        raise RecordOperationError("CAPABILITY_DEGRADED") from None


def _record_state(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "record_type": row["record_type"],
        "legacy_type": row["legacy_type"],
        "content": row["content"],
        "rationale": row["rationale"],
        "context": _parse_json(row["context_json"], dict),
        "tags": _parse_json(row["tags_json"], list),
        "file_path": None,
        "file_path_relative": stored_relative_path(row["file_path_relative"]),
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


def _memory_record(
    record: object,
) -> tuple[dict[str, Any], dict[str, Any], int | None]:
    if not isinstance(record, Mapping):
        raise RecordOperationError("INVALID_ARGUMENT")
    try:
        record_type = record["record_type"]
        content = record["content"]
        rationale = record.get("rationale")
        context = dict(record.get("context", {}))
        original_context = dict(context)
        tags = list(record.get("tags", []))
        relative_file_path = record.get("relative_file_path")
        happened_at = record.get("happened_at")
        steps = list(record.get("procedure_steps", []))
    except (KeyError, TypeError, ValueError):
        raise RecordOperationError("INVALID_ARGUMENT") from None
    if record_type == "procedure":
        if "steps" in context and context["steps"] != steps:
            raise RecordOperationError("INVALID_ARGUMENT")
        context["steps"] = steps
    happened_at_us = None if happened_at is None else _datetime_us(happened_at)
    request_item = {
        "record_type": record_type,
        "content": content,
        "rationale": rationale,
        "context": original_context,
        "tags": tags,
        "relative_file_path": relative_file_path,
        "happened_at_us": happened_at_us,
        "procedure_steps": steps,
    }
    state = {
        "record_type": record_type,
        "legacy_type": None,
        "content": content,
        "rationale": rationale,
        "context": context,
        "tags": tags,
        "file_path": None,
        "file_path_relative": relative_file_path,
        "keywords": None,
        "is_permanent": False,
        "pinned": False,
        "archived": False,
        "outcome": None,
        "worked": None,
        "recall_count": 0,
        "surprise_score": None,
        "importance_score": None,
        "source_client": None,
        "source_model": None,
        "deleted_at_us": None,
    }
    return request_item, state, happened_at_us


def _event_receipt(row: sqlite3.Row) -> AppendedEvent:
    try:
        return AppendedEvent(
            event_id=str(row["event_id"]),
            event_hash=str(row["event_hash"]),
            payload_hash=str(row["payload_hash"]),
            stream_version=int(row["stream_version"]),
            previous_event_hash=(
                None
                if row["previous_event_hash"] is None
                else str(row["previous_event_hash"])
            ),
        )
    except (KeyError, TypeError, ValueError):
        raise RecordOperationError("CAPABILITY_DEGRADED") from None


def _verified_payload(row: sqlite3.Row) -> dict[str, Any]:
    payload = _parse_json(row["payload_json"], dict)
    try:
        if canonical_json_bytes(payload).decode("utf-8") != str(
            row["payload_json"]
        ) or sha256_json(payload) != str(row["payload_hash"]):
            raise RecordOperationError("CAPABILITY_DEGRADED")
    except RecordOperationError:
        raise
    except Exception:
        raise RecordOperationError("CAPABILITY_DEGRADED") from None
    return payload


def _correlation(workspace_id: str, operation: str, key: str) -> str:
    return deterministic_id(
        "job",
        f"{operation}-idempotency",
        workspace_id,
        key,
    )


def _operation_id(*parts: object) -> str:
    return "op_" + sha256_json(["daem0nmcp", "v7", "record-operation", *parts])


async def _run_read(operation: Callable[[], Any]) -> Any:
    worker = asyncio.create_task(_RECORD_OPERATION_WORKERS.run(operation))
    try:
        return await asyncio.shield(worker)
    except asyncio.CancelledError as cancellation:
        with suppress(asyncio.CancelledError, Exception):
            await await_task_terminal(worker)
        raise cancellation
    except BoundedWorkerBusyError as exc:
        raise RecordOperationError("TASK_REQUIRED") from exc


async def _run_mutation(
    operation: Callable[[threading.Event], tuple[Any, Path, bool]],
) -> tuple[Any, Path, bool]:
    cancelled = threading.Event()
    worker = asyncio.create_task(
        _RECORD_OPERATION_WORKERS.run(lambda: operation(cancelled))
    )
    try:
        return await asyncio.shield(worker)
    except asyncio.CancelledError as cancellation:
        cancelled.set()
        try:
            result = await await_task_terminal(worker)
        except (_WorkerCancelledError, BoundedWorkerBusyError):
            raise cancellation from None
        except Exception:
            raise cancellation from None
        return result
    except BoundedWorkerBusyError as exc:
        raise RecordOperationError("TASK_REQUIRED") from exc


async def _schedule_after_commit(
    dependencies: RecordOperationDependencies,
    path: Path,
    changed: bool,
) -> None:
    if not changed:
        return
    try:
        dependencies.projection_scheduler(path)
    except asyncio.CancelledError:
        raise
    except Exception:
        # EventStore durably enqueues projection work in the committed database.
        return


def _translate_storage_error(error: Exception) -> RecordOperationError:
    if isinstance(error, RecordOperationError):
        return error
    code = getattr(error, "code", None)
    if code in STABLE_ERROR_CODE_SET:
        return RecordOperationError(str(code))
    return RecordOperationError("CAPABILITY_DEGRADED")


def _canonical_batch_sync(
    dependencies: RecordOperationDependencies,
    workspace: Workspace,
    request: CanonicalBatchStoreRequest,
    cancelled: threading.Event,
) -> tuple[MemoryStoreBatchData, Path, bool]:
    recorded_at_us = _now_us(dependencies)
    if cancelled.is_set():
        raise _WorkerCancelledError()
    prepared = [_memory_record(item) for item in request.records]
    provenance = None if request.provenance is None else dict(request.provenance)
    legacy_batch = (
        request.semantic_namespace == "memory-store-batch" and provenance is None
    )
    request_hash = sha256_json(
        [item[0] for item in prepared]
        if legacy_batch
        else {
            "semantic_namespace": request.semantic_namespace,
            "records": [item[0] for item in prepared],
            "provenance": provenance,
        }
    )
    correlation = _correlation(
        workspace.workspace_id,
        request.semantic_namespace,
        request.idempotency_key,
    )
    record_ids = [
        deterministic_id(
            "mem",
            request.semantic_namespace,
            workspace.workspace_id,
            request.idempotency_key,
            index,
        )
        for index in range(len(prepared))
    ]
    try:
        with dependencies.storage_resolver.locked_active(workspace) as active:
            connection = _open_database(active.path, writable=True)
            try:
                connection.execute("BEGIN IMMEDIATE")
                if cancelled.is_set():
                    raise _WorkerCancelledError()
                existing = connection.execute(
                    "SELECT event_id,event_hash,payload_hash,stream_version,"
                    "previous_event_hash,stream_id,payload_json "
                    "FROM memory_events WHERE workspace_id=? "
                    "AND event_type='memory.created' AND correlation_id=?",
                    (workspace.workspace_id, correlation),
                ).fetchall()
                events: list[AppendedEvent]
                changed = False
                if existing:
                    indexed: dict[int, sqlite3.Row] = {}
                    for row in existing:
                        payload = _verified_payload(row)
                        if payload.get("idempotency_request_hash") != request_hash:
                            raise IdempotencyConflict()
                        index = payload.get("batch_index")
                        if (
                            isinstance(index, bool)
                            or not isinstance(index, int)
                            or not 0 <= index < len(prepared)
                            or payload.get("batch_size") != len(prepared)
                            or (
                                not legacy_batch
                                and payload.get("semantic_namespace")
                                != request.semantic_namespace
                            )
                            or index in indexed
                            or str(row["stream_id"]) != record_ids[index]
                        ):
                            raise RecordOperationError("CAPABILITY_DEGRADED")
                        indexed[index] = row
                    if set(indexed) != set(range(len(prepared))):
                        raise RecordOperationError("CAPABILITY_DEGRADED")
                    events = [
                        _event_receipt(indexed[index]) for index in range(len(prepared))
                    ]
                else:
                    store = EventStore(connection, assume_transaction=True)
                    events = []
                    for index, ((_, state, happened_at_us), record_id) in enumerate(
                        zip(prepared, record_ids, strict=True)
                    ):
                        if cancelled.is_set():
                            raise _WorkerCancelledError()
                        payload = {
                            "record": state,
                            "idempotency_request_hash": request_hash,
                            "batch_index": index,
                            "batch_size": len(prepared),
                        }
                        if not legacy_batch:
                            payload["semantic_namespace"] = request.semantic_namespace
                            payload["provenance"] = provenance
                        event = store.append_and_project(
                            EventCommand(
                                workspace_id=workspace.workspace_id,
                                stream_id=record_id,
                                stream_kind="memory",
                                event_type="memory.created",
                                occurred_at_us=(
                                    recorded_at_us
                                    if happened_at_us is None
                                    else happened_at_us
                                ),
                                recorded_at_us=recorded_at_us,
                                actor_type="client",
                                payload=payload,
                                correlation_id=correlation,
                                expected_stream_version=1,
                            )
                        )
                        events.append(event)
                    changed = True
                summaries = [
                    _summary(
                        _load_record(connection, workspace.workspace_id, record_id)
                    )
                    for record_id in record_ids
                ]
                if cancelled.is_set():
                    raise _WorkerCancelledError()
                connection.commit()
                return (
                    MemoryStoreBatchData(
                        records=summaries,
                        event_ids=[event.event_id for event in events],
                        idempotent_replay=not changed,
                    ),
                    active.path,
                    changed,
                )
            except (
                IdempotencyConflict,
                RecordOperationError,
                EventStreamConflict,
                _WorkerCancelledError,
            ):
                if connection.in_transaction:
                    connection.rollback()
                raise
            except Exception:
                if connection.in_transaction:
                    connection.rollback()
                raise RecordOperationError("CAPABILITY_DEGRADED") from None
            finally:
                if connection.in_transaction:
                    connection.rollback()
                connection.close()
    except (
        IdempotencyConflict,
        RecordOperationError,
        EventStreamConflict,
        _WorkerCancelledError,
    ):
        raise
    except Exception as exc:
        raise _translate_storage_error(exc) from None


async def store_canonical_batch(
    dependencies: RecordOperationDependencies,
    *,
    workspace: Workspace,
    request: CanonicalBatchStoreRequest,
) -> MemoryStoreBatchData:
    """Commit a validated semantic batch atomically without authorizing it."""
    result, path, changed = await _run_mutation(
        lambda cancelled: _canonical_batch_sync(
            dependencies, workspace, request, cancelled
        )
    )
    await _schedule_after_commit(dependencies, path, changed)
    return result


def _matching_state_event(
    connection: sqlite3.Connection,
    *,
    workspace_id: str,
    record_id: str,
    event_type: str,
    request_hash: str,
) -> AppendedEvent | None:
    rows = connection.execute(
        "SELECT event_id,event_hash,payload_hash,stream_version,"
        "previous_event_hash,stream_id,payload_json FROM memory_events "
        "WHERE workspace_id=? AND stream_id=? AND event_type=? "
        "ORDER BY rowid DESC",
        (workspace_id, record_id, event_type),
    ).fetchall()
    for row in rows:
        payload = _verified_payload(row)
        if payload.get("operation_request_hash") == request_hash:
            return _event_receipt(row)
    return None


def _state_set_sync(
    dependencies: RecordOperationDependencies,
    workspace: Workspace,
    request: AdmittedRequest,
    cancelled: threading.Event,
    *,
    field_name: str,
    event_type: str,
) -> tuple[MutationReceipt, Path, bool]:
    recorded_at_us = _now_us(dependencies)
    if cancelled.is_set():
        raise _WorkerCancelledError()
    desired = bool(getattr(request, field_name))
    request_hash = sha256_json({"record_id": request.record_id, field_name: desired})
    try:
        with dependencies.storage_resolver.locked_active(workspace) as active:
            connection = _open_database(active.path, writable=True)
            try:
                connection.execute("BEGIN IMMEDIATE")
                row = _load_record(
                    connection,
                    workspace.workspace_id,
                    request.record_id,
                )
                existing = None
                if bool(row[field_name]) == desired:
                    existing = _matching_state_event(
                        connection,
                        workspace_id=workspace.workspace_id,
                        record_id=request.record_id,
                        event_type=event_type,
                        request_hash=request_hash,
                    )
                if existing is None:
                    if cancelled.is_set():
                        raise _WorkerCancelledError()
                    state = _record_state(row)
                    state[field_name] = desired
                    event = EventStore(
                        connection, assume_transaction=True
                    ).append_and_project(
                        EventCommand(
                            workspace_id=workspace.workspace_id,
                            stream_id=request.record_id,
                            stream_kind="memory",
                            event_type=event_type,
                            occurred_at_us=recorded_at_us,
                            recorded_at_us=recorded_at_us,
                            actor_type="client",
                            payload={
                                "record": state,
                                "operation_request_hash": request_hash,
                            },
                            correlation_id=_correlation(
                                workspace.workspace_id,
                                event_type,
                                request_hash,
                            ),
                            expected_stream_version=int(row["stream_version"]) + 1,
                        )
                    )
                    changed = True
                else:
                    event = existing
                    changed = False
                if cancelled.is_set():
                    raise _WorkerCancelledError()
                connection.commit()
                return (
                    MutationReceipt(
                        operation_id=_operation_id(
                            workspace.workspace_id,
                            event_type,
                            request.record_id,
                            desired,
                        ),
                        affected_ids=[request.record_id],
                        event_ids=[event.event_id],
                        counts={"selected": 1, "changed": int(changed)},
                        idempotent_replay=not changed,
                    ),
                    active.path,
                    changed,
                )
            except (
                RecordOperationError,
                EventStreamConflict,
                _WorkerCancelledError,
            ):
                if connection.in_transaction:
                    connection.rollback()
                raise
            except Exception:
                if connection.in_transaction:
                    connection.rollback()
                raise RecordOperationError("CAPABILITY_DEGRADED") from None
            finally:
                if connection.in_transaction:
                    connection.rollback()
                connection.close()
    except (RecordOperationError, EventStreamConflict, _WorkerCancelledError):
        raise
    except Exception as exc:
        raise _translate_storage_error(exc) from None


def _cursor_for(
    secret: bytes,
    domain: str,
    workspace_id: str,
    selector: object,
    event_id: str,
    content_hash: str,
) -> str:
    event_match = _EVENT_ID_RE.fullmatch(event_id)
    if event_match is None:
        raise RecordOperationError("CAPABILITY_DEGRADED")
    signature = hmac.new(
        secret,
        canonical_json_bytes(
            [
                "daem0nmcp",
                "v7",
                "cursor",
                domain,
                workspace_id,
                selector,
                event_id,
                content_hash,
            ]
        ),
        hashlib.sha256,
    ).hexdigest()
    return f"cur_v1_{event_match.group(1)}_{signature}"


def _cursor_event_id(cursor: str) -> str:
    if not isinstance(cursor, str):
        raise RecordOperationError("INVALID_ARGUMENT")
    match = _EVENT_CURSOR_RE.fullmatch(cursor)
    if match is None:
        raise RecordOperationError("INVALID_ARGUMENT")
    return "evt_" + match.group(1)


def _file_recall_sync(
    dependencies: RecordOperationDependencies,
    workspace: Workspace,
    request: AdmittedRequest,
) -> Page[RecordSummary]:
    selector = {"relative_file_path": request.relative_file_path}
    try:
        with dependencies.storage_resolver.locked_active(workspace) as active:
            connection = _open_database(active.path, writable=False)
            try:
                cursor_values: tuple[int, str] | None = None
                if request.cursor is not None:
                    event_id = _cursor_event_id(request.cursor)
                    rows = connection.execute(
                        f"SELECT {_RECORD_COLUMNS} FROM memory_records "
                        "WHERE workspace_id=? AND source_event_id=? "
                        "AND file_path_relative=? AND archived=0 "
                        "AND deleted_at_us IS NULL LIMIT 2",
                        (
                            workspace.workspace_id,
                            event_id,
                            request.relative_file_path,
                        ),
                    ).fetchall()
                    if len(rows) != 1:
                        raise RecordOperationError("INVALID_ARGUMENT")
                    cursor_row = rows[0]
                    expected = _cursor_for(
                        dependencies.cursor_secret,
                        "memory-recall-file",
                        workspace.workspace_id,
                        selector,
                        event_id,
                        str(cursor_row["content_hash"]),
                    )
                    if not hmac.compare_digest(request.cursor, expected):
                        raise RecordOperationError("INVALID_ARGUMENT")
                    cursor_values = (
                        int(cursor_row["updated_at_us"]),
                        str(cursor_row["record_id"]),
                    )
                where = (
                    "workspace_id=? AND file_path_relative=? "
                    "AND archived=0 AND deleted_at_us IS NULL"
                )
                parameters: list[object] = [
                    workspace.workspace_id,
                    request.relative_file_path,
                ]
                if cursor_values is not None:
                    where += (
                        " AND (updated_at_us<? OR (updated_at_us=? AND record_id>?))"
                    )
                    parameters.extend(
                        [cursor_values[0], cursor_values[0], cursor_values[1]]
                    )
                parameters.append(request.limit + 1)
                rows = connection.execute(
                    f"SELECT {_RECORD_COLUMNS} FROM memory_records "
                    f"WHERE {where} ORDER BY updated_at_us DESC,record_id ASC "
                    "LIMIT ?",
                    parameters,
                ).fetchall()
                more = len(rows) > request.limit
                selected = rows[: request.limit]
                next_cursor = None
                if more and selected:
                    last = selected[-1]
                    next_cursor = _cursor_for(
                        dependencies.cursor_secret,
                        "memory-recall-file",
                        workspace.workspace_id,
                        selector,
                        str(last["source_event_id"]),
                        str(last["content_hash"]),
                    )
                return Page[RecordSummary](
                    items=[_summary(row) for row in selected],
                    next_cursor=next_cursor,
                    truncated=more,
                )
            finally:
                connection.close()
    except RecordOperationError:
        raise
    except Exception as exc:
        raise _translate_storage_error(exc) from None


def _highlight_spans(excerpt: str, query: str) -> list[HighlightSpan]:
    tokens: list[str] = []
    seen: set[str] = set()
    for token in _QUERY_TOKEN_RE.findall(query):
        folded = token.casefold()
        if folded in _FTS_OPERATORS or folded in seen:
            continue
        seen.add(folded)
        tokens.append(token)
    offsets: set[tuple[int, int]] = set()
    for token in tokens:
        for match in re.finditer(re.escape(token), excerpt, flags=re.IGNORECASE):
            offsets.add((match.start(), match.end()))
            if len(offsets) >= 100:
                break
        if len(offsets) >= 100:
            break
    return [HighlightSpan(start=start, end=end) for start, end in sorted(offsets)[:100]]


def _text_search_sync(
    dependencies: RecordOperationDependencies,
    workspace: Workspace,
    request: AdmittedRequest,
) -> Page[TextSearchHit]:
    selector = {
        "query": request.query,
        "include_metadata": request.include_metadata,
        "highlight": request.highlight,
    }
    try:
        with dependencies.storage_resolver.locked_active(workspace) as active:
            validation = _open_database(active.path, writable=False)
            validation.close()
            provider = dependencies.lexical_provider_factory(active.path)
            result = asyncio.run(
                provider.search(
                    RetrievalQuery(
                        workspace_id=workspace.workspace_id,
                        text=request.query,
                        limit=100,
                        candidate_limit=_MAX_LEXICAL_CANDIDATES,
                        include_archived=False,
                    ),
                    _MAX_LEXICAL_CANDIDATES,
                )
            )
            if not isinstance(result, ProviderResult):
                raise RecordOperationError("LEXICAL_UNAVAILABLE")
            if result.status in {"unavailable", "failed"}:
                raise RecordOperationError("LEXICAL_UNAVAILABLE")
            candidates = list(result.candidates)
            start = 0
            if request.cursor is not None:
                event_id = _cursor_event_id(request.cursor)
                matching = [
                    (index, candidate)
                    for index, candidate in enumerate(candidates)
                    if candidate.evidence.event_id == event_id
                ]
                if len(matching) != 1:
                    raise RecordOperationError("INVALID_ARGUMENT")
                index, candidate = matching[0]
                expected = _cursor_for(
                    dependencies.cursor_secret,
                    "memory-search-text",
                    workspace.workspace_id,
                    selector,
                    event_id,
                    candidate.evidence.content_hash,
                )
                if not hmac.compare_digest(request.cursor, expected):
                    raise RecordOperationError("INVALID_ARGUMENT")
                start = index + 1
            selected = candidates[start : start + request.limit]
            more = start + len(selected) < len(candidates)
            connection = _open_database(active.path, writable=False)
            try:
                hits: list[TextSearchHit] = []
                for candidate in selected:
                    row = _load_record(
                        connection,
                        workspace.workspace_id,
                        candidate.evidence.record_id,
                    )
                    if (
                        str(row["source_event_id"]) != candidate.evidence.event_id
                        or str(row["content_hash"]) != candidate.evidence.content_hash
                        or row["deleted_at_us"] is not None
                        or bool(row["archived"])
                    ):
                        raise RecordOperationError("LEXICAL_UNAVAILABLE")
                    # v6 accepted empty content; the bounded excerpt shows
                    # the migration marker rather than failing the search.
                    excerpt = str(row["content"])[:8000] or MIGRATED_EMPTY_CONTENT
                    evidence = candidate.evidence
                    hits.append(
                        TextSearchHit(
                            record=_summary(
                                row,
                                include_metadata=request.include_metadata,
                            ),
                            bounded_excerpt=excerpt,
                            highlights=(
                                _highlight_spans(excerpt, request.query)
                                if request.highlight
                                else []
                            ),
                            evidence_refs=[
                                EvidenceRef(
                                    record_id=evidence.record_id,
                                    event_id=evidence.event_id,
                                    content_hash=evidence.content_hash,
                                    version_id=None,
                                    relation_path=list(evidence.relation_path),
                                    provider="lexical",
                                )
                            ],
                        )
                    )
            finally:
                connection.close()
            next_cursor = None
            if more and selected:
                last = selected[-1].evidence
                next_cursor = _cursor_for(
                    dependencies.cursor_secret,
                    "memory-search-text",
                    workspace.workspace_id,
                    selector,
                    last.event_id,
                    last.content_hash,
                )
            return Page[TextSearchHit](
                items=hits,
                next_cursor=next_cursor,
                truncated=more,
            )
    except RecordOperationError:
        raise
    except Exception as exc:
        raise _translate_storage_error(exc) from None


def _session_selector(request: AdmittedRequest) -> dict[str, int | None]:
    return {
        "since_at_us": (None if request.since is None else _datetime_us(request.since))
    }


def _session_origin_cursor(
    secret: bytes,
    workspace_id: str,
    selector: object,
) -> str:
    signature = hmac.new(
        secret,
        canonical_json_bytes(
            [
                "daem0nmcp",
                "v7",
                "cursor",
                "session-updates-origin",
                workspace_id,
                selector,
            ]
        ),
        hashlib.sha256,
    ).hexdigest()
    return f"cur_v1_origin_{signature}"


def _session_cursor(
    secret: bytes,
    workspace_id: str,
    selector: object,
    event_id: str,
    event_hash: str,
) -> str:
    return _cursor_for(
        secret,
        "session-updates",
        workspace_id,
        selector,
        event_id,
        event_hash,
    )


def _session_position(
    connection: sqlite3.Connection,
    secret: bytes,
    workspace_id: str,
    selector: object,
    cursor: str,
) -> int:
    expected_origin = _session_origin_cursor(secret, workspace_id, selector)
    if hmac.compare_digest(cursor, expected_origin):
        return 0
    if _ORIGIN_CURSOR_RE.fullmatch(cursor) is not None:
        raise RecordOperationError("INVALID_ARGUMENT")
    event_id = _cursor_event_id(cursor)
    rows = connection.execute(
        "SELECT sequence.update_sequence,sequence.event_source,"
        "COALESCE(memory.event_hash,governance.event_hash) AS event_hash "
        "FROM session_update_sequence AS sequence "
        "LEFT JOIN memory_events AS memory "
        "ON sequence.event_source='memory' "
        "AND memory.event_id=sequence.event_id AND memory.stream_kind='memory' "
        "LEFT JOIN governance_events AS governance "
        "ON sequence.event_source='governance' "
        "AND governance.event_id=sequence.event_id "
        "WHERE sequence.workspace_id=? AND sequence.event_id=? LIMIT 2",
        (workspace_id, event_id),
    ).fetchall()
    if len(rows) != 1:
        raise RecordOperationError("INVALID_ARGUMENT")
    source = str(rows[0]["event_source"])
    if source not in {"memory", "governance"} or rows[0]["event_hash"] is None:
        raise RecordOperationError("CAPABILITY_DEGRADED")
    if not hmac.compare_digest(
        cursor,
        _session_cursor(
            secret,
            workspace_id,
            selector,
            event_id,
            str(rows[0]["event_hash"]),
        ),
    ):
        raise RecordOperationError("INVALID_ARGUMENT")
    return int(rows[0]["update_sequence"])


def _event_summary(kind: str, event_type: str) -> str:
    summaries = {
        "memory.created": "Memory record created.",
        "memory.pin_set": "Memory pin state changed.",
        "memory.archive_set": "Memory archive state changed.",
        "memory.outcome_recorded": "Memory outcome recorded.",
        "rule.created": "Rule created.",
        "rule.updated": "Rule updated.",
        "context_trigger.created": "Context trigger created.",
        "context_trigger.deleted": "Context trigger deleted.",
        "active_context.added": "Active context added.",
        "active_context.updated": "Active context updated.",
        "active_context.removed": "Active context removed.",
        "active_context.cleared": "Active context cleared.",
    }
    return summaries.get(event_type, f"{kind.replace('_', ' ').title()} updated.")


def _session_updates_once_sync(
    dependencies: RecordOperationDependencies,
    workspace: Workspace,
    request: AdmittedRequest,
) -> SessionUpdatesData:
    try:
        with dependencies.storage_resolver.locked_active(workspace) as active:
            connection = _open_database(active.path, writable=False)
            try:
                selector = _session_selector(request)
                position = 0
                if request.after_cursor is not None:
                    position = _session_position(
                        connection,
                        dependencies.cursor_secret,
                        workspace.workspace_id,
                        selector,
                        request.after_cursor,
                    )
                where = "sequence.workspace_id=? AND sequence.update_sequence>?"
                parameters: list[object] = [workspace.workspace_id, position]
                if request.since is not None:
                    where += (
                        " AND COALESCE(memory.recorded_at_us,"
                        "governance.recorded_at_us)>?"
                    )
                    parameters.append(_datetime_us(request.since))
                parameters.append(_MAX_SESSION_EVENTS + 1)
                rows = connection.execute(
                    "SELECT sequence.update_sequence,sequence.event_id,"
                    "COALESCE(memory.event_hash,governance.event_hash) "
                    "AS event_hash,"
                    "COALESCE(memory.stream_id,governance.stream_id) AS stream_id,"
                    "COALESCE(memory.event_type,governance.event_type) "
                    "AS event_type,"
                    "COALESCE(memory.occurred_at_us,governance.occurred_at_us) "
                    "AS occurred_at_us,"
                    "CASE WHEN sequence.event_source='memory' THEN 'record' "
                    "ELSE governance.stream_kind END AS update_kind "
                    "FROM session_update_sequence AS sequence "
                    "LEFT JOIN memory_events AS memory "
                    "ON sequence.event_source='memory' "
                    "AND memory.event_id=sequence.event_id "
                    "AND memory.stream_kind='memory' "
                    "LEFT JOIN governance_events AS governance "
                    "ON sequence.event_source='governance' "
                    "AND governance.event_id=sequence.event_id "
                    f"WHERE {where} "
                    "ORDER BY sequence.update_sequence ASC LIMIT ?",
                    parameters,
                ).fetchall()
                selected = rows[:_MAX_SESSION_EVENTS]
                events: list[UpdateSummary] = []
                for row in selected:
                    if any(
                        row[name] is None
                        for name in (
                            "event_hash",
                            "stream_id",
                            "event_type",
                            "occurred_at_us",
                            "update_kind",
                        )
                    ):
                        raise RecordOperationError("CAPABILITY_DEGRADED")
                    raw_kind = str(row["update_kind"])
                    if raw_kind not in {
                        "record",
                        "rule",
                        "trigger",
                        "active_context",
                    }:
                        raise RecordOperationError("CAPABILITY_DEGRADED")
                    kind = cast(UpdateKind, raw_kind)
                    events.append(
                        UpdateSummary(
                            event_id=str(row["event_id"]),
                            kind=kind,
                            object_id=str(row["stream_id"]),
                            occurred_at=_datetime_from_us(row["occurred_at_us"]),
                            summary=_event_summary(kind, str(row["event_type"])),
                        )
                    )
                if selected:
                    last = selected[-1]
                    cursor = _session_cursor(
                        dependencies.cursor_secret,
                        workspace.workspace_id,
                        selector,
                        str(last["event_id"]),
                        str(last["event_hash"]),
                    )
                elif request.after_cursor is not None:
                    cursor = request.after_cursor
                else:
                    latest = connection.execute(
                        "SELECT sequence.event_id,"
                        "COALESCE(memory.event_hash,governance.event_hash) "
                        "AS event_hash "
                        "FROM session_update_sequence AS sequence "
                        "LEFT JOIN memory_events AS memory "
                        "ON sequence.event_source='memory' "
                        "AND memory.event_id=sequence.event_id "
                        "AND memory.stream_kind='memory' "
                        "LEFT JOIN governance_events AS governance "
                        "ON sequence.event_source='governance' "
                        "AND governance.event_id=sequence.event_id "
                        "WHERE sequence.workspace_id=? "
                        "ORDER BY sequence.update_sequence DESC LIMIT 1",
                        (workspace.workspace_id,),
                    ).fetchone()
                    cursor = (
                        _session_origin_cursor(
                            dependencies.cursor_secret,
                            workspace.workspace_id,
                            selector,
                        )
                        if latest is None
                        else _session_cursor(
                            dependencies.cursor_secret,
                            workspace.workspace_id,
                            selector,
                            str(latest["event_id"]),
                            str(latest["event_hash"]),
                        )
                    )
                return SessionUpdatesData(
                    changed=bool(events),
                    cursor=cursor,
                    events=events,
                )
            finally:
                connection.close()
    except RecordOperationError:
        raise
    except Exception as exc:
        raise _translate_storage_error(exc) from None


def build_record_operations(
    dependencies: RecordOperationDependencies,
) -> Mapping[str, Callable[..., Any]]:
    """Return the exact immutable canonical record-operation registry."""

    if not isinstance(dependencies, RecordOperationDependencies):
        raise TypeError("dependencies must be RecordOperationDependencies")

    async def session_updates_get(
        *, workspace: Workspace, request: AdmittedRequest
    ) -> SessionUpdatesData:
        _authorize(workspace, request, "session_updates_get")
        loop = asyncio.get_running_loop()
        deadline = loop.time() + request.wait_seconds
        while True:
            result = await _run_read(
                lambda: _session_updates_once_sync(
                    dependencies,
                    workspace,
                    request,
                )
            )
            if result.changed or loop.time() >= deadline:
                return result
            await asyncio.sleep(
                min(
                    float(dependencies.poll_interval_seconds),
                    max(0.0, deadline - loop.time()),
                )
            )

    async def memory_recall_file(
        *, workspace: Workspace, request: AdmittedRequest
    ) -> Page[RecordSummary]:
        _authorize(workspace, request, "memory_recall_file")
        return await _run_read(
            lambda: _file_recall_sync(dependencies, workspace, request)
        )

    async def memory_search_text(
        *, workspace: Workspace, request: AdmittedRequest
    ) -> Page[TextSearchHit]:
        _authorize(workspace, request, "memory_search_text")
        return await _run_read(
            lambda: _text_search_sync(dependencies, workspace, request)
        )

    async def memory_store_batch(
        *, workspace: Workspace, request: AdmittedRequest
    ) -> MemoryStoreBatchData:
        _authorize(workspace, request, "memory_store_batch")
        return await store_canonical_batch(
            dependencies,
            workspace=workspace,
            request=CanonicalBatchStoreRequest(
                records=tuple(request.records),
                idempotency_key=request.idempotency_key,
            ),
        )

    async def memory_pin_set(
        *, workspace: Workspace, request: AdmittedRequest
    ) -> MutationReceipt:
        _authorize(workspace, request, "memory_pin_set")
        result, path, changed = await _run_mutation(
            lambda cancelled: _state_set_sync(
                dependencies,
                workspace,
                request,
                cancelled,
                field_name="pinned",
                event_type="memory.pin_set",
            )
        )
        await _schedule_after_commit(dependencies, path, changed)
        return result

    async def memory_archive_set(
        *, workspace: Workspace, request: AdmittedRequest
    ) -> MutationReceipt:
        _authorize(workspace, request, "memory_archive_set")
        result, path, changed = await _run_mutation(
            lambda cancelled: _state_set_sync(
                dependencies,
                workspace,
                request,
                cancelled,
                field_name="archived",
                event_type="memory.archive_set",
            )
        )
        await _schedule_after_commit(dependencies, path, changed)
        return result

    return MappingProxyType(
        {
            "memory_archive_set": memory_archive_set,
            "memory_pin_set": memory_pin_set,
            "memory_recall_file": memory_recall_file,
            "memory_search_text": memory_search_text,
            "memory_store_batch": memory_store_batch,
            "session_updates_get": session_updates_get,
        }
    )


__all__ = [
    "CanonicalBatchStoreRequest",
    "RecordOperationDependencies",
    "RecordOperationError",
    "build_record_operations",
    "store_canonical_batch",
]
