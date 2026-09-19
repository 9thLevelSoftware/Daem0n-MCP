"""Production, dependency-injected services for the six pinned v7 tools.

The module deliberately stays framework-neutral.  It binds the pinned service
protocols to the canonical v7 SQLite event store and Task 8 retrieval runtime
without importing FastMCP or the legacy SQLAlchemy application state.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import os
import re
import sqlite3
import threading
import time
from collections.abc import Awaitable, Callable, Iterator, Mapping
from contextlib import ExitStack, contextmanager, suppress
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal, Protocol

from ... import __version__
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
from ...retrieval import RetrievalQuery, RetrievalResult
from ...retrieval.composer import SelectedEvidence
from ...retrieval.service import RetrievalCandidateResult
from ...retrieval.types import EvidenceRef
from ...schema_version import CURRENT_SCHEMA_VERSION
from ...storage_activation import (
    DatabaseFileLock,
    ResolvedActiveDatabase,
    resolve_active_database,
)
from ...workspace import (
    Workspace,
    WorkspaceRegistry,
    normalize_resolved_path,
    resolve_derived_path,
)
from .errors import is_database_busy
from .federated_retrieval import (
    FederatedCandidate,
    FederatedRetrievalError,
    FederatedSourceResult,
    compose_federated_results,
    federation_access_lock,
    sliced_queries,
    validate_directional_links,
)
from .models import (
    CapabilityState,
    CitationManifestEntry,
    RecordSummary,
    RetrievalData,
    TokenUsage,
)
from .models import (
    EvidenceItem as PublicEvidenceItem,
)
from .models import (
    EvidenceRef as PublicEvidenceRef,
)
from .models import (
    ProviderDiagnostic as PublicProviderDiagnostic,
)
from .pinned import (
    IdempotencyConflict,
    MemoryOutcomeCommand,
    MemoryStoreCommand,
    RecordedOutcome,
    StoredMemory,
)
from .tasks import await_task_terminal
from .tools import (
    HealthData,
    PreflightGuidance,
    SessionBriefData,
    SessionBriefInput,
)

_WINDOWS_ABSOLUTE_PATH = re.compile(r"(?<![A-Za-z0-9])(?:[A-Za-z]:[\\/]|\\\\)")
_POSIX_ABSOLUTE_PATH = re.compile(r"(?:^|[\s\"'=(])/(?!/)[A-Za-z0-9_.-]")
_SCHEMA_VERSION = CURRENT_SCHEMA_VERSION
_FORMAT_VERSION = 7
_PROTOCOL_VERSION = "2025-11-25"
_SUPPORTED_TRANSPORTS: frozenset[Literal["stdio", "streamable-http"]] = frozenset(
    {"stdio", "streamable-http"}
)
_MAX_EVIDENCE_REFS = 1_600
_REQUIRED_V7_TABLES = frozenset(
    {
        "active_context_entries",
        "memory_events",
        "memory_records",
        "public_object_ids",
        "retrieval_documents",
    }
)


class RuntimeServiceError(RuntimeError):
    """A stable, path-free service failure safe for internal error handling."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class _WorkerCancelledError(RuntimeError):
    pass


def _contains_raw_path(value: object) -> bool:
    if isinstance(value, str):
        return bool(
            _WINDOWS_ABSOLUTE_PATH.search(value) or _POSIX_ABSOLUTE_PATH.search(value)
        )
    if isinstance(value, Mapping):
        return any(
            _contains_raw_path(key) or _contains_raw_path(item)
            for key, item in value.items()
        )
    if isinstance(value, (list, tuple, set, frozenset)):
        return any(_contains_raw_path(item) for item in value)
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        return _contains_raw_path(model_dump(mode="python"))
    return False


def _validated_workspace(workspace: Workspace) -> Workspace:
    if not isinstance(workspace, Workspace):
        raise RuntimeServiceError("INVALID_WORKSPACE")
    try:
        canonical = normalize_resolved_path(workspace.root.resolve(strict=True))
        registered = WorkspaceRegistry([canonical], default_root=canonical).default
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        raise RuntimeServiceError("INVALID_WORKSPACE") from exc
    if (
        registered.workspace_id != workspace.workspace_id
        or os.path.normcase(str(registered.root)) != os.path.normcase(str(canonical))
        or os.path.normcase(str(normalize_resolved_path(workspace.root)))
        != os.path.normcase(str(canonical))
    ):
        raise RuntimeServiceError("INVALID_WORKSPACE")
    return registered


def resolve_workspace_storage(workspace: Workspace) -> Path:
    """Resolve the fixed storage directory below one registered workspace."""

    registered = _validated_workspace(workspace)
    try:
        storage = resolve_derived_path(
            registered.root,
            ".daem0nmcp",
            "storage",
        )
    except Exception as exc:
        raise RuntimeServiceError("ACTIVE_V7_UNAVAILABLE") from exc
    if not storage.is_dir() or storage.is_symlink():
        raise RuntimeServiceError("ACTIVE_V7_UNAVAILABLE")
    return storage


class WorkspaceStorageResolver:
    """Hold the activation lock while a caller uses one active v7 generation."""

    @contextmanager
    def locked_current(
        self,
        workspace: Workspace,
    ) -> Iterator[ResolvedActiveDatabase]:
        """Resolve one active generation without asserting its format version."""

        storage = resolve_workspace_storage(workspace)
        lock = DatabaseFileLock(storage, "shared")
        try:
            lock.acquire()
            active = resolve_active_database(storage)
            if active.path.is_symlink() or not active.path.is_file():
                raise RuntimeServiceError("ACTIVE_V7_UNAVAILABLE")
        except RuntimeServiceError:
            lock.release()
            raise
        except Exception as exc:
            lock.release()
            # DatabaseInUseError: another process holds the storage lock
            # exclusively (fresh bootstrap, migration) - transient, retryable.
            if is_database_busy(exc):
                raise RuntimeServiceError("DATABASE_IN_USE") from exc
            raise RuntimeServiceError("ACTIVE_V7_UNAVAILABLE") from exc
        try:
            yield active
        finally:
            lock.release()

    @contextmanager
    def locked_active(
        self,
        workspace: Workspace,
    ) -> Iterator[ResolvedActiveDatabase]:
        with self.locked_current(workspace) as active:
            if (
                active.pointer is None
                or active.format_version != _FORMAT_VERSION
                or active.generation < 1
            ):
                raise RuntimeServiceError("ACTIVE_V7_UNAVAILABLE")
            yield active


def _open_database(path: Path, *, writable: bool) -> sqlite3.Connection:
    mode = "rw" if writable else "ro"
    try:
        connection = sqlite3.connect(
            f"file:{path.as_posix()}?mode={mode}",
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
        version_table = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_version'"
        ).fetchone()
        version = (
            0
            if version_table is None
            else int(
                connection.execute(
                    "SELECT COALESCE(MAX(version),0) FROM schema_version"
                ).fetchone()[0]
            )
        )
        required = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name IN ('active_context_entries','memory_events',"
                "'memory_records','retrieval_documents','public_object_ids')"
            )
        }
        if (
            foreign_keys is None
            or int(foreign_keys[0]) != 1
            or version < _SCHEMA_VERSION
            or required != _REQUIRED_V7_TABLES
        ):
            raise RuntimeServiceError("ACTIVE_V7_UNAVAILABLE")
        return connection
    except RuntimeServiceError:
        with suppress(NameError, sqlite3.Error):
            connection.close()
        raise
    except Exception as exc:
        with suppress(NameError, sqlite3.Error):
            connection.close()
        if is_database_busy(exc):
            raise RuntimeServiceError("DATABASE_IN_USE") from exc
        raise RuntimeServiceError("ACTIVE_V7_UNAVAILABLE") from exc


def _inspect_database_health(
    path: Path,
) -> tuple[int | None, frozenset[str]]:
    """Read path-free schema facts from one already-resolved database."""

    connection: sqlite3.Connection | None = None
    try:
        connection = sqlite3.connect(
            f"file:{path.as_posix()}?mode=ro",
            uri=True,
            timeout=2.0,
            isolation_level=None,
        )
        connection.execute("PRAGMA query_only=ON")
        connection.execute("PRAGMA busy_timeout=2000")
        deadline = time.monotonic() + 2.0
        connection.set_progress_handler(
            lambda: 1 if time.monotonic() >= deadline else 0,
            1_000,
        )
        connection.execute("BEGIN")
        required_names = (*sorted(_REQUIRED_V7_TABLES), "schema_version")
        placeholders = ",".join("?" for _ in required_names)
        tables = frozenset(
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                f"AND name IN ({placeholders}) LIMIT ?",
                (*required_names, len(required_names)),
            )
        )
        schema_version: int | None = None
        if "schema_version" in tables:
            raw_version = connection.execute(
                "SELECT MAX(version) FROM schema_version"
            ).fetchone()[0]
            if raw_version is not None:
                schema_version = int(raw_version)
                if schema_version < 1:
                    schema_version = None
        # Online health is an availability probe.  SQLite's ``quick_check``
        # scans the complete database (the argument bounds reported errors,
        # not work), so running it here can block the event loop for tens of
        # seconds on a healthy multi-gigabyte workspace.  Opening the active
        # file, reading its schema marker, and inventorying the required tables
        # bounds this request while still failing closed for unreadable or
        # structurally incomplete storage.  Full-page corruption certification
        # remains the explicit offline verify-v7 operation.
        return schema_version, tables
    except Exception as exc:
        raise RuntimeServiceError("ACTIVE_V7_UNAVAILABLE") from exc
    finally:
        if connection is not None:
            with suppress(sqlite3.Error):
                connection.close()


def _datetime_us(value: datetime) -> int:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise RuntimeServiceError("INVALID_TIMESTAMP")
    try:
        utc = value.astimezone(timezone.utc)
        epoch = datetime(1970, 1, 1, tzinfo=timezone.utc)
        delta = utc - epoch
        result = (delta.days * 86_400 + delta.seconds) * 1_000_000 + delta.microseconds
    except (OverflowError, ValueError) as exc:
        raise RuntimeServiceError("INVALID_TIMESTAMP") from exc
    if not -(2**63) <= result <= 2**63 - 1:
        raise RuntimeServiceError("INVALID_TIMESTAMP")
    return result


def _datetime_from_us(value: object) -> datetime:
    if isinstance(value, bool) or not isinstance(value, int):
        raise RuntimeServiceError("MEMORY_RECORD_INTEGRITY_FAILED")
    try:
        return datetime.fromtimestamp(value / 1_000_000, timezone.utc)
    except (OverflowError, OSError, ValueError) as exc:
        raise RuntimeServiceError("MEMORY_RECORD_INTEGRITY_FAILED") from exc


def _record_status(row: sqlite3.Row, evidence_status: str = "current") -> str:
    if row["deleted_at_us"] is not None:
        return "invalidated"
    if bool(row["archived"]):
        return "archived"
    if evidence_status == "superseded":
        return "superseded"
    return "current"


def _parse_json(value: object, expected_type: type, code: str) -> Any:
    try:
        parsed = json.loads(str(value))
    except (TypeError, ValueError, RecursionError) as exc:
        raise RuntimeServiceError(code) from exc
    if not isinstance(parsed, expected_type):
        raise RuntimeServiceError(code)
    return parsed


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
    except (KeyError, TypeError, ValueError) as exc:
        raise RuntimeServiceError("MEMORY_EVENT_INTEGRITY_FAILED") from exc


def _idempotency_correlation(
    workspace_id: str,
    operation: str,
    key: str,
) -> str:
    return deterministic_id(
        "job",
        f"{operation}-idempotency",
        workspace_id,
        key,
    )


def _existing_idempotent_event(
    connection: sqlite3.Connection,
    *,
    workspace_id: str,
    event_type: str,
    correlation_id: str,
    stream_id: str,
    request_hash: str,
) -> AppendedEvent | None:
    rows = connection.execute(
        "SELECT event_id,event_hash,payload_hash,stream_version,"
        "previous_event_hash,stream_id,payload_json FROM memory_events "
        "WHERE workspace_id=? AND event_type=? AND correlation_id=? "
        "ORDER BY event_id LIMIT 2",
        (workspace_id, event_type, correlation_id),
    ).fetchall()
    if not rows:
        return None
    if len(rows) != 1:
        raise RuntimeServiceError("MEMORY_EVENT_INTEGRITY_FAILED")
    row = rows[0]
    if str(row["stream_id"]) != stream_id:
        raise IdempotencyConflict()
    payload = _parse_json(row["payload_json"], dict, "MEMORY_EVENT_INTEGRITY_FAILED")
    try:
        canonical = canonical_json_bytes(payload).decode("utf-8")
    except Exception as exc:
        raise RuntimeServiceError("MEMORY_EVENT_INTEGRITY_FAILED") from exc
    if canonical != str(row["payload_json"]) or sha256_json(payload) != str(
        row["payload_hash"]
    ):
        raise RuntimeServiceError("MEMORY_EVENT_INTEGRITY_FAILED")
    if payload.get("idempotency_request_hash") != request_hash:
        raise IdempotencyConflict()
    return _event_receipt(row)


_RECORD_COLUMNS = (
    "record_id,workspace_id,record_type,legacy_type,content,content_hash,"
    "rationale,context_json,tags_json,file_path,file_path_relative,keywords,"
    "is_permanent,pinned,archived,outcome,worked,recall_count,surprise_score,"
    "importance_score,source_client,source_model,stream_version,"
    "source_event_id,created_at_us,updated_at_us,deleted_at_us"
)
_QUALIFIED_RECORD_COLUMNS = ",".join(
    f"record.{name}" for name in _RECORD_COLUMNS.split(",")
)


def _load_record_row(
    connection: sqlite3.Connection,
    workspace_id: str,
    record_id: str,
) -> sqlite3.Row:
    rows = connection.execute(
        f"SELECT {_RECORD_COLUMNS} FROM memory_records "
        "WHERE workspace_id=? AND record_id=? LIMIT 2",
        (workspace_id, record_id),
    ).fetchall()
    if len(rows) != 1:
        raise RuntimeServiceError("NOT_FOUND")
    if rows[0]["file_path"] is not None:
        raise RuntimeServiceError("MEMORY_RECORD_INTEGRITY_FAILED")
    return rows[0]


def _record_summary(
    row: sqlite3.Row,
    *,
    evidence_status: str = "current",
) -> RecordSummary:
    tags = _parse_json(row["tags_json"], list, "MEMORY_RECORD_INTEGRITY_FAILED")
    content = row["content"]
    if not isinstance(content, str) or not content:
        raise RuntimeServiceError("MEMORY_RECORD_INTEGRITY_FAILED")
    try:
        return RecordSummary.model_validate(
            {
                "record_id": str(row["record_id"]),
                "record_type": str(row["record_type"]),
                "excerpt": content[:4000],
                "tags": tags,
                "relative_file_path": None
                if row["file_path_relative"] is None
                else str(row["file_path_relative"]),
                "current_status": _record_status(row, evidence_status),
                "content_hash": str(row["content_hash"]),
                "created_at": _datetime_from_us(row["created_at_us"]),
                "updated_at": _datetime_from_us(row["updated_at_us"]),
            }
        )
    except RuntimeServiceError:
        raise
    except Exception as exc:
        raise RuntimeServiceError("MEMORY_RECORD_INTEGRITY_FAILED") from exc


def _record_state(row: sqlite3.Row) -> dict[str, Any]:
    context = _parse_json(row["context_json"], dict, "MEMORY_RECORD_INTEGRITY_FAILED")
    tags = _parse_json(row["tags_json"], list, "MEMORY_RECORD_INTEGRITY_FAILED")
    return {
        "record_type": row["record_type"],
        "legacy_type": row["legacy_type"],
        "content": row["content"],
        "rationale": row["rationale"],
        "context": context,
        "tags": tags,
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


class ProjectionScheduler(Protocol):
    def __call__(self, database_path: Path) -> object: ...


def _default_projection_scheduler(
    database_path: Path,
    config: object | None = None,
    capability_statuses: Mapping[str, str] | None = None,
) -> None:
    from ...retrieval.runtime import schedule_projection_job_drain

    schedule_projection_job_drain(
        database_path,
        config=config,
        max_jobs=5,
        capability_statuses=capability_statuses,
    )


class SQLiteMemoryEventWriter:
    """Append pinned memory mutations through the canonical Task 7 store."""

    def __init__(
        self,
        *,
        storage_resolver: WorkspaceStorageResolver | None = None,
        clock: Callable[[], datetime] | None = None,
        projection_scheduler: ProjectionScheduler = _default_projection_scheduler,
        projection_config: object | None = None,
        capability_statuses: Mapping[str, str] | None = None,
        max_workers: int = 2,
    ) -> None:
        if not callable(clock or datetime.now) or not callable(projection_scheduler):
            raise ValueError("runtime writer dependencies must be callable")
        self._storage_resolver = storage_resolver or WorkspaceStorageResolver()
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._projection_scheduler = projection_scheduler
        self._projection_config = projection_config
        self._capability_statuses = dict(capability_statuses or {})
        self._uses_default_scheduler = (
            projection_scheduler is _default_projection_scheduler
        )
        self._workers = BoundedWorkerPool(
            max_workers=max_workers,
            thread_name_prefix="daem0nmcp-v7-memory",
        )

    def close(self) -> None:
        """Release the writer's private bounded worker pool."""

        self._workers.shutdown()

    async def _run_mutation(
        self,
        operation: Callable[[threading.Event], tuple[Any, Path, bool]],
    ) -> tuple[Any, Path, bool]:
        cancelled = threading.Event()
        worker = asyncio.create_task(self._workers.run(lambda: operation(cancelled)))
        try:
            return await asyncio.shield(worker)
        except BoundedWorkerBusyError as exc:
            raise RuntimeServiceError("DATABASE_IN_USE") from exc
        except asyncio.CancelledError as cancellation:
            cancelled.set()
            try:
                result = await await_task_terminal(worker)
            except _WorkerCancelledError:
                raise cancellation from None
            except Exception:
                raise cancellation from None
            return result

    def _now_us(self) -> int:
        try:
            value = self._clock()
        except Exception as exc:
            raise RuntimeServiceError("CLOCK_UNAVAILABLE") from exc
        return _datetime_us(value)

    def _schedule_after_commit(self, path: Path, changed: bool) -> None:
        if not changed:
            return
        try:
            if self._uses_default_scheduler:
                _default_projection_scheduler(
                    path,
                    self._projection_config,
                    self._capability_statuses,
                )
            else:
                self._projection_scheduler(path)
        except Exception:
            # Projection work is durable in background_jobs.  A refresh wake-up
            # failure must not hide an already committed semantic receipt.
            return

    async def store(
        self,
        workspace: Workspace,
        command: MemoryStoreCommand,
    ) -> StoredMemory:
        if not isinstance(command, MemoryStoreCommand):
            raise RuntimeServiceError("INVALID_ARGUMENT")
        result, path, changed = await self._run_mutation(
            lambda cancelled: self._store_sync(workspace, command, cancelled)
        )
        self._schedule_after_commit(path, changed)
        if not isinstance(result, StoredMemory):
            raise RuntimeServiceError("MEMORY_STORE_FAILED")
        return result

    def _store_sync(
        self,
        workspace: Workspace,
        command: MemoryStoreCommand,
        cancelled: threading.Event,
    ) -> tuple[StoredMemory, Path, bool]:
        recorded_at_us = self._now_us()
        if cancelled.is_set():
            raise _WorkerCancelledError()
        happened_at_us = (
            recorded_at_us
            if command.happened_at is None
            else _datetime_us(command.happened_at)
        )
        context = dict(command.context)
        if command.record_type == "procedure":
            steps = list(command.procedure_steps)
            if "steps" in context and context["steps"] != steps:
                raise RuntimeServiceError("INVALID_ARGUMENT")
            context["steps"] = steps
        request_hash = sha256_json(
            {
                "record_type": command.record_type,
                "content": command.content,
                "rationale": command.rationale,
                "context": dict(command.context),
                "tags": list(command.tags),
                "relative_file_path": command.relative_file_path,
                "happened_at_us": (
                    None if command.happened_at is None else happened_at_us
                ),
                "procedure_steps": list(command.procedure_steps),
            }
        )
        record_id = deterministic_id(
            "mem",
            "memory-store",
            workspace.workspace_id,
            command.idempotency_key,
        )
        correlation = _idempotency_correlation(
            workspace.workspace_id,
            "memory-store",
            command.idempotency_key,
        )
        record = {
            "record_type": command.record_type,
            "legacy_type": None,
            "content": command.content,
            "rationale": command.rationale,
            "context": context,
            "tags": list(command.tags),
            "file_path": None,
            "file_path_relative": command.relative_file_path,
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
        payload = {
            "record": record,
            "idempotency_request_hash": request_hash,
        }
        with self._storage_resolver.locked_active(workspace) as active:
            connection = _open_database(active.path, writable=True)
            try:
                connection.execute("BEGIN IMMEDIATE")
                if cancelled.is_set():
                    raise _WorkerCancelledError()
                existing = _existing_idempotent_event(
                    connection,
                    workspace_id=workspace.workspace_id,
                    event_type="memory.created",
                    correlation_id=correlation,
                    stream_id=record_id,
                    request_hash=request_hash,
                )
                if existing is None:
                    event = EventStore(
                        connection, assume_transaction=True
                    ).append_and_project(
                        EventCommand(
                            workspace_id=workspace.workspace_id,
                            stream_id=record_id,
                            stream_kind="memory",
                            event_type="memory.created",
                            occurred_at_us=happened_at_us,
                            recorded_at_us=recorded_at_us,
                            actor_type="client",
                            payload=payload,
                            correlation_id=correlation,
                            expected_stream_version=1,
                        )
                    )
                    changed = True
                else:
                    event = existing
                    changed = False
                summary = _record_summary(
                    _load_record_row(connection, workspace.workspace_id, record_id)
                )
                if cancelled.is_set():
                    raise _WorkerCancelledError()
                connection.commit()
                return (
                    StoredMemory(summary, event, not changed),
                    active.path,
                    changed,
                )
            except (
                IdempotencyConflict,
                EventStreamConflict,
                RuntimeServiceError,
                _WorkerCancelledError,
            ):
                if connection.in_transaction:
                    connection.rollback()
                raise
            except Exception as exc:
                if connection.in_transaction:
                    connection.rollback()
                if is_database_busy(exc):
                    raise RuntimeServiceError("DATABASE_IN_USE") from exc
                raise RuntimeServiceError("MEMORY_STORE_FAILED") from exc
            finally:
                if connection.in_transaction:
                    connection.rollback()
                connection.close()

    async def record_outcome(
        self,
        workspace: Workspace,
        command: MemoryOutcomeCommand,
    ) -> RecordedOutcome:
        if not isinstance(command, MemoryOutcomeCommand):
            raise RuntimeServiceError("INVALID_ARGUMENT")
        result, path, changed = await self._run_mutation(
            lambda cancelled: self._record_outcome_sync(workspace, command, cancelled)
        )
        self._schedule_after_commit(path, changed)
        if not isinstance(result, RecordedOutcome):
            raise RuntimeServiceError("MEMORY_OUTCOME_FAILED")
        return result

    def _record_outcome_sync(
        self,
        workspace: Workspace,
        command: MemoryOutcomeCommand,
        cancelled: threading.Event,
    ) -> tuple[RecordedOutcome, Path, bool]:
        recorded_at_us = self._now_us()
        if cancelled.is_set():
            raise _WorkerCancelledError()
        happened_at_us = (
            recorded_at_us
            if command.happened_at is None
            else _datetime_us(command.happened_at)
        )
        request_hash = sha256_json(
            {
                "record_id": command.record_id,
                "outcome_text": command.outcome_text,
                "worked": command.worked,
                "happened_at_us": (
                    None if command.happened_at is None else happened_at_us
                ),
            }
        )
        correlation = _idempotency_correlation(
            workspace.workspace_id,
            "memory-record-outcome",
            command.idempotency_key,
        )
        with self._storage_resolver.locked_active(workspace) as active:
            connection = _open_database(active.path, writable=True)
            try:
                connection.execute("BEGIN IMMEDIATE")
                if cancelled.is_set():
                    raise _WorkerCancelledError()
                row = _load_record_row(
                    connection, workspace.workspace_id, command.record_id
                )
                existing = _existing_idempotent_event(
                    connection,
                    workspace_id=workspace.workspace_id,
                    event_type="memory.outcome_recorded",
                    correlation_id=correlation,
                    stream_id=command.record_id,
                    request_hash=request_hash,
                )
                if existing is None:
                    record = _record_state(row)
                    record["outcome"] = command.outcome_text
                    record["worked"] = command.worked
                    payload = {
                        "record": record,
                        "idempotency_request_hash": request_hash,
                    }
                    event = EventStore(
                        connection, assume_transaction=True
                    ).append_and_project(
                        EventCommand(
                            workspace_id=workspace.workspace_id,
                            stream_id=command.record_id,
                            stream_kind="memory",
                            event_type="memory.outcome_recorded",
                            occurred_at_us=happened_at_us,
                            recorded_at_us=recorded_at_us,
                            actor_type="client",
                            payload=payload,
                            correlation_id=correlation,
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
                    RecordedOutcome(
                        command.record_id,
                        event,
                        command.worked,
                        not changed,
                    ),
                    active.path,
                    changed,
                )
            except (
                IdempotencyConflict,
                EventStreamConflict,
                RuntimeServiceError,
                _WorkerCancelledError,
            ):
                if connection.in_transaction:
                    connection.rollback()
                raise
            except Exception as exc:
                if connection.in_transaction:
                    connection.rollback()
                if is_database_busy(exc):
                    raise RuntimeServiceError("DATABASE_IN_USE") from exc
                raise RuntimeServiceError("MEMORY_OUTCOME_FAILED") from exc
            finally:
                if connection.in_transaction:
                    connection.rollback()
                connection.close()


class RetrievalServiceFactory(Protocol):
    def __call__(self, database_path: Path) -> object: ...


def _default_retrieval_service_factory(
    database_path: Path,
    config: object | None = None,
    capability_statuses: Mapping[str, str] | None = None,
) -> object:
    from ...retrieval.runtime import create_retrieval_service

    return create_retrieval_service(
        database_path,
        config=config,
        capability_statuses=capability_statuses,
    )


_RETRIEVAL_CONFIG_FIELDS = (
    "embedding_backend",
    "embedding_dimension",
    "embedding_document_prefix",
    "embedding_model",
    "embedding_query_prefix",
    "qdrant_collection_prefix",
    "qdrant_api_key",
    "qdrant_path",
    "qdrant_timeout_seconds",
    "qdrant_url",
    "retrieval_candidate_limit",
    "retrieval_graph_max_branching",
    "retrieval_graph_max_depth",
    "retrieval_rerank_candidate_limit",
    "retrieval_rerank_enabled",
    "retrieval_rrf_weights",
    "rrf_k",
)


def _retrieval_config_fingerprint(
    config: object, capability_statuses: Mapping[str, str]
) -> str:
    values = {name: getattr(config, name, None) for name in _RETRIEVAL_CONFIG_FIELDS}
    extra = getattr(config, "__dict__", None)
    if not any(value is not None for value in values.values()) and isinstance(
        extra, dict
    ):
        values = {
            str(name): value
            for name, value in sorted(extra.items())
            if not str(name).startswith("_")
        }
    try:
        return sha256_json(
            {
                "config": values,
                "capabilities": dict(sorted(capability_statuses.items())),
            }
        )
    except (TypeError, ValueError) as exc:
        raise RuntimeServiceError("RETRIEVAL_CONFIG_INVALID") from exc


@dataclass(slots=True)
class _CachedRetrievalService:
    key: tuple[str, int, str, str]
    service: object
    users: int = 0
    retired: bool = False
    closed: bool = False


class Task8RecallService:
    """Adapt Task 8 retrieval to strict, reauthenticated public v7 models."""

    def __init__(
        self,
        *,
        storage_resolver: WorkspaceStorageResolver | None = None,
        workspace_resolver: object | None = None,
        service_factory: RetrievalServiceFactory = _default_retrieval_service_factory,
        config: object | None = None,
        capability_statuses: Mapping[str, str] | None = None,
        max_workers: int = 4,
    ) -> None:
        if not callable(service_factory):
            raise ValueError("service_factory must be callable")
        self._storage_resolver = storage_resolver or WorkspaceStorageResolver()
        self._workspace_resolver = workspace_resolver
        self._service_factory = service_factory
        self._config = config
        self._capability_statuses = dict(capability_statuses or {})
        self._uses_default_factory = (
            service_factory is _default_retrieval_service_factory
        )
        self._cache_lock = threading.RLock()
        self._cache: dict[tuple[str, int, str, str], _CachedRetrievalService] = {}
        self._closed = False
        self._workers = BoundedWorkerPool(
            max_workers=max_workers,
            thread_name_prefix="daem0nmcp-v7-recall",
        )

    def close(self) -> None:
        """Retire cached retrieval resources and stop private hydration workers."""

        resources: list[object] = []
        with self._cache_lock:
            if self._closed:
                return
            self._closed = True
            entries = tuple(self._cache.values())
            self._cache.clear()
            for entry in entries:
                entry.retired = True
                if entry.users == 0 and not entry.closed:
                    entry.closed = True
                    resources.append(entry.service)
        self._workers.shutdown()
        self._close_services(resources)

    def _effective_config(self) -> object:
        if self._config is not None:
            return self._config
        from ...config import settings

        return settings

    def _acquire_service(
        self, workspace_id: str, active: ResolvedActiveDatabase
    ) -> _CachedRetrievalService:
        config = self._effective_config()
        key = (
            workspace_id,
            int(active.generation),
            os.path.normcase(str(Path(active.path).resolve())),
            _retrieval_config_fingerprint(config, self._capability_statuses),
        )
        resources: list[object] = []
        with self._cache_lock:
            if self._closed:
                raise RuntimeServiceError("RETRIEVAL_UNAVAILABLE")
            entry = self._cache.get(key)
            if entry is None:
                if self._uses_default_factory:
                    service = _default_retrieval_service_factory(
                        active.path,
                        config,
                        self._capability_statuses,
                    )
                else:
                    service = self._service_factory(active.path)
                entry = _CachedRetrievalService(key=key, service=service)
                self._cache[key] = entry
            entry.users += 1
            for other_key, other in tuple(self._cache.items()):
                if other_key == key or other_key[0] != workspace_id:
                    continue
                self._cache.pop(other_key, None)
                other.retired = True
                if other.users == 0 and not other.closed:
                    other.closed = True
                    resources.append(other.service)
        self._close_services(resources)
        return entry

    def _release_service(self, entry: _CachedRetrievalService) -> None:
        resource: object | None = None
        with self._cache_lock:
            entry.users -= 1
            if entry.users < 0:
                raise RuntimeError("retrieval service cache usage underflow")
            if (
                (entry.retired or self._closed)
                and entry.users == 0
                and not entry.closed
            ):
                entry.closed = True
                resource = entry.service
        if resource is not None:
            self._close_services([resource])

    @staticmethod
    def _close_services(services: list[object]) -> None:
        for service in services:
            close = getattr(service, "close", None)
            if callable(close):
                with suppress(Exception):
                    close()

    async def _run_snapshot(self, operation: Callable[[], Any]) -> Any:
        worker = asyncio.create_task(self._workers.run(operation))
        try:
            return await asyncio.shield(worker)
        except BoundedWorkerBusyError as exc:
            raise RuntimeServiceError("RETRIEVAL_UNAVAILABLE") from exc
        except asyncio.CancelledError:
            with suppress(asyncio.CancelledError, Exception):
                await await_task_terminal(worker)
            raise

    async def retrieve(
        self,
        workspace: Workspace,
        query: RetrievalQuery,
        linked_workspace_ids: frozenset[str],
        federation_authorizer: Callable[[Workspace], object | Awaitable[object]]
        | None = None,
    ) -> RetrievalData:
        if not isinstance(query, RetrievalQuery) or not isinstance(
            linked_workspace_ids, frozenset
        ):
            raise RuntimeServiceError("INVALID_ARGUMENT")
        if query.workspace_id != workspace.workspace_id:
            raise RuntimeServiceError("INVALID_WORKSPACE")
        if linked_workspace_ids:
            return await self._retrieve_federated(
                workspace,
                query,
                linked_workspace_ids,
                federation_authorizer,
            )
        try:
            with self._storage_resolver.locked_active(workspace) as active:
                entry = self._acquire_service(workspace.workspace_id, active)
                try:
                    retrieve = getattr(entry.service, "retrieve", None)
                    if not callable(retrieve):
                        raise RuntimeServiceError("RETRIEVAL_UNAVAILABLE")
                    value = retrieve(query)
                    result = await value if inspect.isawaitable(value) else value
                    if not isinstance(result, RetrievalResult):
                        raise RuntimeServiceError("RETRIEVAL_FAILED")
                    hydrated = await self._run_snapshot(
                        lambda: self._hydrate(active.path, query, result)
                    )
                finally:
                    self._release_service(entry)
        except asyncio.CancelledError:
            raise
        except RuntimeServiceError:
            raise
        except Exception as exc:
            raise RuntimeServiceError("RETRIEVAL_FAILED") from exc
        if _contains_raw_path(hydrated):
            raise RuntimeServiceError("UNSAFE_SERVICE_OUTPUT")
        return hydrated

    async def _retrieve_source(
        self,
        workspace: Workspace,
        active: ResolvedActiveDatabase,
        query: RetrievalQuery,
    ) -> FederatedSourceResult:
        entry = self._acquire_service(workspace.workspace_id, active)
        try:
            retrieve = getattr(entry.service, "retrieve_candidates", None)
            if not callable(retrieve):
                raise RuntimeServiceError("RETRIEVAL_UNAVAILABLE")
            value = retrieve(query)
            result = await value if inspect.isawaitable(value) else value
            if not isinstance(result, RetrievalCandidateResult):
                raise RuntimeServiceError("RETRIEVAL_FAILED")
            return await self._run_snapshot(
                lambda: self._hydrate_candidates(active.path, query, result)
            )
        finally:
            self._release_service(entry)

    def _resolve_federated_workspace(self, workspace_id: str) -> Workspace:
        resolver = self._workspace_resolver
        resolve = getattr(resolver, "resolve", None)
        if not callable(resolve):
            raise RuntimeServiceError("FEDERATION_UNAVAILABLE")
        try:
            workspace = resolve(workspace_id)
            validated = _validated_workspace(workspace)
        except Exception as exc:
            raise RuntimeServiceError("INVALID_WORKSPACE") from exc
        if validated.workspace_id != workspace_id:
            raise RuntimeServiceError("INVALID_WORKSPACE")
        return validated

    async def _retrieve_federated(
        self,
        origin: Workspace,
        query: RetrievalQuery,
        linked_workspace_ids: frozenset[str],
        authorizer: Callable[[Workspace], object | Awaitable[object]] | None,
    ) -> RetrievalData:
        if authorizer is None or len(linked_workspace_ids) > 32:
            raise RuntimeServiceError("FEDERATION_UNAVAILABLE")
        workspaces = {origin.workspace_id: _validated_workspace(origin)}
        for workspace_id in sorted(linked_workspace_ids):
            if workspace_id == origin.workspace_id:
                raise RuntimeServiceError("INVALID_ARGUMENT")
            workspaces[workspace_id] = self._resolve_federated_workspace(workspace_id)
        try:
            with ExitStack() as stack:
                active_by_id = {
                    workspace_id: stack.enter_context(
                        self._storage_resolver.locked_active(workspaces[workspace_id])
                    )
                    for workspace_id in sorted(workspaces)
                }
                linked = tuple(sorted(linked_workspace_ids))
                validate_directional_links(
                    active_by_id[origin.workspace_id].path,
                    origin.workspace_id,
                    linked,
                )
                for selected in workspaces.values():
                    value = authorizer(selected)
                    if inspect.isawaitable(value):
                        await value
                queries = sliced_queries(query, origin.workspace_id, linked)
                semaphore = asyncio.Semaphore(2)

                async def retrieve_one(
                    workspace_id: str,
                ) -> tuple[str, FederatedSourceResult]:
                    async with semaphore:
                        return (
                            workspace_id,
                            await self._retrieve_source(
                                workspaces[workspace_id],
                                active_by_id[workspace_id],
                                queries[workspace_id],
                            ),
                        )

                timeout = float(
                    getattr(self._effective_config(), "sync_timeout_seconds", 15.0)
                )
                pairs = await asyncio.wait_for(
                    asyncio.gather(
                        *(retrieve_one(workspace_id) for workspace_id in queries)
                    ),
                    timeout=timeout,
                )
                guard = federation_access_lock(
                    active_by_id[origin.workspace_id].path,
                    "shared",
                )
                acquire = asyncio.create_task(asyncio.to_thread(guard.acquire))
                try:
                    try:
                        await asyncio.shield(acquire)
                    except asyncio.CancelledError:
                        # The blocking OS lock acquisition cannot be cancelled.
                        # Release it if it completes after its waiter is gone.
                        await asyncio.shield(acquire)
                        guard.release()
                        raise
                    validate_directional_links(
                        active_by_id[origin.workspace_id].path,
                        origin.workspace_id,
                        linked,
                    )
                    for selected in workspaces.values():
                        value = authorizer(selected)
                        if inspect.isawaitable(value):
                            await value
                    hydrated = compose_federated_results(dict(pairs), query)
                    if _contains_raw_path(hydrated):
                        raise RuntimeServiceError("UNSAFE_SERVICE_OUTPUT")
                finally:
                    guard.release()
        except asyncio.TimeoutError as exc:
            raise RuntimeServiceError("DEADLINE_EXCEEDED") from exc
        except FederatedRetrievalError as exc:
            raise RuntimeServiceError(exc.code) from exc
        except asyncio.CancelledError:
            raise
        except RuntimeServiceError:
            raise
        except Exception as exc:
            authorization_code = getattr(exc, "code", None)
            if authorization_code in {
                "COMMUNION_REQUIRED",
                "UNAUTHORIZED_WORKSPACE",
            }:
                raise RuntimeServiceError(authorization_code) from exc
            raise RuntimeServiceError("RETRIEVAL_FAILED") from exc
        # `hydrated` is immutable and there is no await between releasing the
        # read guard above and committing this response to the caller.
        return hydrated

    def _hydrate_candidates(
        self,
        path: Path,
        query: RetrievalQuery,
        result: RetrievalCandidateResult,
    ) -> FederatedSourceResult:
        diagnostics = tuple(
            PublicProviderDiagnostic(
                provider=item.provider,
                status=item.status,
                manifest_generation=item.manifest_generation,
                elapsed_ms=item.elapsed_ms,
                reason=item.reason,
                returned_count=item.returned_count,
            )
            for item in result.providers
        )
        if result.abstained:
            return FederatedSourceResult(diagnostics=diagnostics)
        if len(result.selected) > query.limit:
            raise RuntimeServiceError("EVIDENCE_AUTHENTICATION_FAILED")
        refs = tuple(
            ref
            for selected in result.selected
            for ref in selected.candidate.evidence_refs
        )
        if len(refs) > _MAX_EVIDENCE_REFS:
            raise RuntimeServiceError("EVIDENCE_AUTHENTICATION_FAILED")
        connection = _open_database(path, writable=False)
        try:
            connection.execute("BEGIN")
            hydrated: dict[tuple[str, str, str], sqlite3.Row] = {}
            for ref in refs:
                provider = getattr(ref, "provider", None)
                if not isinstance(provider, str) or not provider:
                    raise RuntimeServiceError("EVIDENCE_AUTHENTICATION_FAILED")
                key = (ref.record_id, ref.event_id, ref.content_hash)
                if key in hydrated:
                    continue
                rows = connection.execute(
                    f"SELECT {_QUALIFIED_RECORD_COLUMNS} "
                    "FROM memory_records AS record "
                    "JOIN memory_events AS event "
                    "ON event.event_id=record.source_event_id "
                    "WHERE record.workspace_id=? AND record.record_id=? "
                    "AND record.source_event_id=? AND record.content_hash=? "
                    "AND event.workspace_id=record.workspace_id "
                    "AND event.stream_id=record.record_id LIMIT 2",
                    (query.workspace_id, *key),
                ).fetchall()
                if len(rows) != 1 or rows[0]["file_path"] is not None:
                    raise RuntimeServiceError("EVIDENCE_AUTHENTICATION_FAILED")
                hydrated[key] = rows[0]

            candidates: list[FederatedCandidate] = []
            for selected in result.selected:
                if not isinstance(selected, SelectedEvidence):
                    raise RuntimeServiceError("EVIDENCE_AUTHENTICATION_FAILED")
                primary = selected.candidate.evidence_refs[0]
                row = hydrated.get(
                    (primary.record_id, primary.event_id, primary.content_hash)
                )
                if row is None:
                    raise RuntimeServiceError("EVIDENCE_AUTHENTICATION_FAILED")
                tags = _parse_json(
                    row["tags_json"], list, "EVIDENCE_AUTHENTICATION_FAILED"
                )
                if (
                    str(row["record_type"]) != selected.category
                    or str(row["content"]) != selected.content
                    or row["rationale"] != selected.rationale
                    or tuple(tags) != selected.tags
                ):
                    raise RuntimeServiceError("EVIDENCE_AUTHENTICATION_FAILED")
                public_refs = tuple(
                    self._public_ref(ref, query.workspace_id)
                    for ref in selected.candidate.evidence_refs
                )
                candidates.append(
                    FederatedCandidate(
                        record=_record_summary(row, evidence_status=selected.status),
                        content=selected.content,
                        channels=tuple(sorted(selected.candidate.channels)),
                        status=selected.status,
                        evidence_refs=public_refs,
                    )
                )
            connection.commit()
            return FederatedSourceResult(
                candidates=tuple(candidates), diagnostics=diagnostics
            )
        except RuntimeServiceError:
            if connection.in_transaction:
                connection.rollback()
            raise
        except Exception as exc:
            if connection.in_transaction:
                connection.rollback()
            raise RuntimeServiceError("EVIDENCE_AUTHENTICATION_FAILED") from exc
        finally:
            if connection.in_transaction:
                connection.rollback()
            connection.close()

    @staticmethod
    def _all_refs(result: RetrievalResult) -> tuple[object, ...]:
        refs: list[object] = []
        for item in result.items:
            refs.extend(item.evidence_refs)
        if result.context is not None:
            for entry in result.context.citations:
                refs.extend(entry.evidence_refs)
        if len(refs) > _MAX_EVIDENCE_REFS:
            raise RuntimeServiceError("EVIDENCE_AUTHENTICATION_FAILED")
        return tuple(refs)

    def _hydrate(
        self,
        path: Path,
        query: RetrievalQuery,
        result: RetrievalResult,
    ) -> RetrievalData:
        if result.abstained:
            diagnostics = [
                PublicProviderDiagnostic(
                    provider=item.provider,
                    status=item.status,
                    manifest_generation=item.manifest_generation,
                    elapsed_ms=item.elapsed_ms,
                    reason=item.reason,
                    returned_count=item.returned_count,
                )
                for item in result.providers
            ]
            try:
                return RetrievalData(
                    items=[],
                    rendered_context=None,
                    citation_manifest=[],
                    provider_diagnostics=diagnostics,
                    abstained=True,
                    abstention_reason=result.reason,
                    token_usage=TokenUsage(
                        budget=query.token_budget,
                        requested=0,
                        selected=0,
                        rendered=0,
                        dropped=0,
                    ),
                )
            except Exception as exc:
                raise RuntimeServiceError("RETRIEVAL_FAILED") from exc

        if result.context is None:
            raise RuntimeServiceError("EVIDENCE_AUTHENTICATION_FAILED")
        refs = self._all_refs(result)
        connection = _open_database(path, writable=False)
        try:
            connection.execute("BEGIN")
            hydrated: dict[tuple[str, str, str], sqlite3.Row] = {}
            for ref in refs:
                provider = getattr(ref, "provider", None)
                if not isinstance(provider, str) or not provider:
                    raise RuntimeServiceError("EVIDENCE_AUTHENTICATION_FAILED")
                key = (
                    str(getattr(ref, "record_id", "")),
                    str(getattr(ref, "event_id", "")),
                    str(getattr(ref, "content_hash", "")),
                )
                if key in hydrated:
                    continue
                rows = connection.execute(
                    f"SELECT {_QUALIFIED_RECORD_COLUMNS} "
                    "FROM memory_records AS record "
                    "JOIN memory_events AS event "
                    "ON event.event_id=record.source_event_id "
                    "WHERE record.workspace_id=? AND record.record_id=? "
                    "AND record.source_event_id=? AND record.content_hash=? "
                    "AND event.workspace_id=record.workspace_id "
                    "AND event.stream_id=record.record_id LIMIT 2",
                    (query.workspace_id, *key),
                ).fetchall()
                if len(rows) != 1 or rows[0]["file_path"] is not None:
                    raise RuntimeServiceError("EVIDENCE_AUTHENTICATION_FAILED")
                hydrated[key] = rows[0]

            items: list[PublicEvidenceItem] = []
            for item in result.items:
                primary = item.evidence_refs[0]
                primary_key = (
                    primary.record_id,
                    primary.event_id,
                    primary.content_hash,
                )
                row = hydrated.get(primary_key)
                if row is None:
                    raise RuntimeServiceError("EVIDENCE_AUTHENTICATION_FAILED")
                tags = _parse_json(
                    row["tags_json"],
                    list,
                    "EVIDENCE_AUTHENTICATION_FAILED",
                )
                if str(row["record_type"]) != item.category or tuple(tags) != item.tags:
                    raise RuntimeServiceError("EVIDENCE_AUTHENTICATION_FAILED")
                public_refs = [
                    self._public_ref(ref, query.workspace_id)
                    for ref in item.evidence_refs
                ]
                items.append(
                    PublicEvidenceItem(
                        citation=item.citation,
                        record=_record_summary(row, evidence_status=item.status),
                        bounded_excerpt=item.excerpt,
                        channels=sorted(item.channels),
                        score=item.score,
                        status=item.status,
                        evidence_refs=public_refs,
                    )
                )

            manifest = [
                CitationManifestEntry(
                    citation=entry.marker,
                    evidence_refs=[
                        self._public_ref(ref, query.workspace_id)
                        for ref in entry.evidence_refs
                    ],
                    channels=sorted(entry.channels),
                )
                for entry in result.context.citations
            ]
            diagnostics = [
                PublicProviderDiagnostic(
                    provider=item.provider,
                    status=item.status,
                    manifest_generation=item.manifest_generation,
                    elapsed_ms=item.elapsed_ms,
                    reason=item.reason,
                    returned_count=item.returned_count,
                )
                for item in result.providers
            ]
            data = RetrievalData(
                items=items,
                rendered_context=result.context.text,
                citation_manifest=manifest,
                provider_diagnostics=diagnostics,
                abstained=False,
                abstention_reason=None,
                token_usage=TokenUsage(
                    budget=result.context.token_budget,
                    requested=result.context.requested_tokens,
                    selected=result.context.selected_tokens,
                    rendered=result.context.rendered_tokens,
                    dropped=result.context.dropped_tokens,
                ),
            )
            connection.commit()
            return data
        except RuntimeServiceError:
            if connection.in_transaction:
                connection.rollback()
            raise
        except Exception as exc:
            if connection.in_transaction:
                connection.rollback()
            raise RuntimeServiceError("EVIDENCE_AUTHENTICATION_FAILED") from exc
        finally:
            if connection.in_transaction:
                connection.rollback()
            connection.close()

    @staticmethod
    def _public_ref(ref: EvidenceRef, origin_workspace_id: str) -> PublicEvidenceRef:
        provider = getattr(ref, "provider", None)
        if not isinstance(provider, str) or not provider:
            raise RuntimeServiceError("EVIDENCE_AUTHENTICATION_FAILED")
        try:
            return PublicEvidenceRef(
                origin_workspace_id=origin_workspace_id,
                record_id=ref.record_id,
                event_id=ref.event_id,
                content_hash=ref.content_hash,
                version_id=ref.version_id,
                relation_path=list(ref.relation_path),
                provider=provider,
            )
        except Exception as exc:
            raise RuntimeServiceError("EVIDENCE_AUTHENTICATION_FAILED") from exc


class BriefingReader(Protocol):
    def __call__(
        self,
        workspace: Workspace,
        request: SessionBriefInput,
    ) -> object | Awaitable[object]: ...


class BasicBriefingService:
    """Assemble strict briefing data from an injected, workspace-scoped reader."""

    def __init__(
        self,
        *,
        reader: BriefingReader | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if reader is not None and not callable(reader):
            raise ValueError("reader must be callable")
        self._reader = reader
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    async def assemble(
        self,
        workspace: Workspace,
        request: SessionBriefInput,
    ) -> SessionBriefData:
        _validated_workspace(workspace)
        if (
            not isinstance(request, SessionBriefInput)
            or request.workspace_id != workspace.workspace_id
        ):
            raise RuntimeServiceError("INVALID_ARGUMENT")
        try:
            if self._reader is None:
                value: object = {
                    "workspace_id": workspace.workspace_id,
                    "briefed_at": self._clock(),
                    "workspace_statistics": {},
                }
            else:
                value = self._reader(workspace, request)
                if inspect.isawaitable(value):
                    value = await value
            if _contains_raw_path(value):
                raise RuntimeServiceError("UNSAFE_SERVICE_OUTPUT")
            data = SessionBriefData.model_validate(value)
        except asyncio.CancelledError:
            raise
        except RuntimeServiceError:
            raise
        except Exception as exc:
            if is_database_busy(exc):
                raise RuntimeServiceError("DATABASE_IN_USE") from exc
            raise RuntimeServiceError("BRIEFING_FAILED") from exc
        if data.workspace_id != workspace.workspace_id:
            raise RuntimeServiceError("BRIEFING_FAILED")
        if _contains_raw_path(data):
            raise RuntimeServiceError("UNSAFE_SERVICE_OUTPUT")
        return data


class GuidanceReader(Protocol):
    def __call__(
        self,
        workspace: Workspace,
        target_tool: str,
        normalized_arguments: Mapping[str, Any],
        description: str | None,
    ) -> object | Awaitable[object]: ...


class BasicPreflightService:
    """Load counsel from an injected reader without changing Covenant state."""

    def __init__(self, *, reader: GuidanceReader | None = None) -> None:
        if reader is not None and not callable(reader):
            raise ValueError("reader must be callable")
        self._reader = reader

    async def guidance(
        self,
        workspace: Workspace,
        target_tool: str,
        normalized_arguments: Mapping[str, Any],
        description: str | None,
    ) -> PreflightGuidance:
        _validated_workspace(workspace)
        if (
            not isinstance(target_tool, str)
            or not target_tool
            or not isinstance(normalized_arguments, Mapping)
        ):
            raise RuntimeServiceError("INVALID_ARGUMENT")
        try:
            if self._reader is None:
                value: object = {}
            else:
                value = self._reader(
                    workspace,
                    target_tool,
                    normalized_arguments,
                    description,
                )
                if inspect.isawaitable(value):
                    value = await value
            data = PreflightGuidance.model_validate(value)
        except asyncio.CancelledError:
            raise
        except RuntimeServiceError:
            raise
        except Exception as exc:
            if is_database_busy(exc):
                raise RuntimeServiceError("DATABASE_IN_USE") from exc
            raise RuntimeServiceError("PREFLIGHT_FAILED") from exc
        if _contains_raw_path(data):
            raise RuntimeServiceError("UNSAFE_SERVICE_OUTPUT")
        return data


def _storage_capability(
    resolver: WorkspaceStorageResolver,
    workspace: Workspace,
) -> tuple[int | None, int | None, CapabilityState]:
    try:
        with resolver.locked_current(workspace) as active:
            schema_version, tables = _inspect_database_health(active.path)
            format_version = active.format_version
    except Exception:
        return (
            None,
            None,
            CapabilityState(
                name="storage",
                status="failed",
                reason_code="STORAGE_UNAVAILABLE",
                remediation="Initialize or repair the workspace storage.",
            ),
        )

    if schema_version is None:
        return (
            None,
            None,
            CapabilityState(
                name="storage",
                status="failed",
                reason_code="STORAGE_INVALID",
                remediation="Repair or recreate the workspace storage.",
            ),
        )
    if format_version != _FORMAT_VERSION:
        return (
            format_version,
            schema_version,
            CapabilityState(
                name="storage",
                status="degraded",
                reason_code="STORAGE_FORMAT_UNSUPPORTED",
                remediation="Migrate the workspace storage to format 7.",
            ),
        )
    if schema_version < _SCHEMA_VERSION:
        return (
            format_version,
            schema_version,
            CapabilityState(
                name="storage",
                status="degraded",
                reason_code="STORAGE_SCHEMA_OUTDATED",
                remediation=("Migrate the workspace storage to the current schema."),
            ),
        )
    if schema_version > _SCHEMA_VERSION:
        return (
            format_version,
            schema_version,
            CapabilityState(
                name="storage",
                status="degraded",
                reason_code="STORAGE_SCHEMA_UNSUPPORTED",
                remediation="Use a server compatible with the workspace schema.",
            ),
        )
    if not tables >= _REQUIRED_V7_TABLES:
        return (
            None,
            None,
            CapabilityState(
                name="storage",
                status="failed",
                reason_code="STORAGE_INVALID",
                remediation="Repair or recreate the workspace storage.",
            ),
        )
    return (
        format_version,
        schema_version,
        CapabilityState(name="storage", status="ready"),
    )


class BasicHealthService:
    """Report reviewed v7 capability state without reading global legacy state."""

    def __init__(
        self,
        *,
        auth_mode: Literal["process", "loopback", "jwt"],
        task_support: CapabilityState,
        capability_states: tuple[CapabilityState, ...] = (),
        package_version: str = __version__,
        protocol_version: str = _PROTOCOL_VERSION,
        storage_resolver: WorkspaceStorageResolver | None = None,
        dreaming_provider: Callable[
            [Workspace | None], Awaitable[Mapping[str, Any] | None]
        ]
        | None = None,
        runtime_diagnostics_provider: Callable[
            [Workspace | None], Awaitable[list[Mapping[str, Any]]]
        ]
        | None = None,
    ) -> None:
        if auth_mode not in {"process", "loopback", "jwt"}:
            raise ValueError("auth_mode is invalid")
        if storage_resolver is not None and not isinstance(
            storage_resolver, WorkspaceStorageResolver
        ):
            raise ValueError("storage_resolver is invalid")
        try:
            self._task_support = CapabilityState.model_validate(task_support)
            self._capability_states = tuple(
                CapabilityState.model_validate(item) for item in capability_states
            )
            if any(item.name == "storage" for item in self._capability_states):
                raise ValueError("storage capability is reserved")
            if len(self._capability_states) > 63:
                raise ValueError("too many capability states")
            self._storage_resolver = storage_resolver or WorkspaceStorageResolver()
            if dreaming_provider is not None and not callable(dreaming_provider):
                raise ValueError("dreaming provider is invalid")
            self._dreaming_provider = dreaming_provider
            if runtime_diagnostics_provider is not None and not callable(
                runtime_diagnostics_provider
            ):
                raise ValueError("runtime diagnostics provider is invalid")
            self._runtime_diagnostics_provider = runtime_diagnostics_provider
            self._data = HealthData(
                package_version=package_version,
                protocol_version=protocol_version,
                storage_format_version=_FORMAT_VERSION,
                storage_schema_version=_SCHEMA_VERSION,
                supported_transports=set(_SUPPORTED_TRANSPORTS),
                task_support=self._task_support,
                auth_mode=auth_mode,
                capability_states=list(self._capability_states),
            )
        except Exception:
            raise ValueError("health service configuration is invalid") from None
        if _contains_raw_path(self._data):
            raise ValueError("health service configuration is unsafe")
        self._workers = BoundedWorkerPool(
            max_workers=2,
            thread_name_prefix="daem0nmcp-v7-health",
        )

    def close(self) -> None:
        """Wait for admitted probes and release the owned worker threads."""

        self._workers.shutdown()

    async def aclose(self) -> None:
        """Drain health workers without blocking the server event loop."""

        await asyncio.to_thread(self.close)

    async def inspect(
        self,
        workspace: Workspace | None,
        include_components: bool,
    ) -> HealthData:
        if workspace is not None:
            _validated_workspace(workspace)
        if not isinstance(include_components, bool):
            raise RuntimeServiceError("INVALID_ARGUMENT")
        if workspace is None:
            data = self._data.model_copy(deep=True)
        else:
            try:
                format_version, schema_version, storage_state = await self._workers.run(
                    lambda: _storage_capability(
                        self._storage_resolver,
                        workspace,
                    )
                )
            except BoundedWorkerBusyError:
                format_version = None
                schema_version = None
                storage_state = CapabilityState(
                    name="storage",
                    status="degraded",
                    reason_code="STORAGE_UNAVAILABLE",
                    remediation="Retry workspace storage health inspection.",
                )
            data = HealthData.model_validate(
                {
                    **self._data.model_dump(mode="python"),
                    "storage_format_version": format_version,
                    "storage_schema_version": schema_version,
                    "capability_states": [
                        *self._capability_states,
                        storage_state,
                    ],
                }
            )
        if (
            include_components
            and workspace is not None
            and self._dreaming_provider is not None
        ):
            try:
                dreaming = await self._dreaming_provider(workspace)
                data = HealthData.model_validate(
                    {**data.model_dump(mode="python"), "dreaming": dreaming}
                )
            except Exception:
                data = data.model_copy(update={"dreaming": None}, deep=True)
        if not include_components:
            data = data.model_copy(
                update={"capability_states": [], "dreaming": None}, deep=True
            )
        elif self._runtime_diagnostics_provider is not None:
            diagnostics = await self._runtime_diagnostics_provider(workspace)
            data = HealthData.model_validate(
                {
                    **data.model_dump(mode="python"),
                    "runtime_diagnostics": diagnostics,
                }
            )
        if _contains_raw_path(data):
            raise RuntimeServiceError("UNSAFE_SERVICE_OUTPUT")
        return data


__all__ = [
    "BasicBriefingService",
    "BasicHealthService",
    "BasicPreflightService",
    "RuntimeServiceError",
    "SQLiteMemoryEventWriter",
    "Task8RecallService",
    "WorkspaceStorageResolver",
    "resolve_workspace_storage",
]
