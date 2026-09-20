"""Framework-neutral v7 adapters for canonical storage operations.

The adapters in this module deliberately stop at the reviewed v7 seams: the
Task 7 event bundle/store, the Task 8 projection builders, the active format-7
database pointer, and the in-memory Covenant gate.  They never call a retained
v6 tool implementation.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
import os
import re
import secrets
import sqlite3
import threading
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager, suppress
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import MappingProxyType
from typing import Any

from ...bounded_workers import BoundedWorkerBusyError, BoundedWorkerPool
from ...covenant import CovenantGate, InvocationScope
from ...event_store import (
    EventBundleError,
    EventCommand,
    EventStore,
    EventStreamConflict,
    canonical_json_bytes,
    deterministic_id,
    event_hash_for,
    event_id_for_hash,
    import_event_bundle,
    memory_content_hash,
    parse_canonical_json,
    sha256_json,
)
from ...retrieval.operations import ProjectionOperationError, rebuild_projection
from ...schema_version import CURRENT_SCHEMA_VERSION
from ...storage_activation import (
    DatabaseFileLock,
    DatabaseInUseError,
    PointerValidationError,
    resolve_active_database,
)
from ...workspace import Workspace
from .application import AdmittedRequest
from .errors import STABLE_ERROR_CODE_SET, is_database_busy
from .models import EvidenceRef, Page, RecordSummary
from .portable_projections import (
    ImportFinalizationLease,
    PortableTransferError,
    activate_vector_candidate,
    claim_import_finalization,
    cleanup_import_attempt,
    complete_import_session,
    create_export_session,
    prepare_import,
    prepare_vector_candidate,
    read_export_page,
    reconstruct_legacy_mappings,
    release_import_finalization,
    renew_import_finalization,
    stage_import_page,
    validate_legacy_projection,
)
from .tasks import await_task_terminal
from .tools import (
    CovenantNextStep,
    CovenantStatusData,
    DiagnosticSummary,
    ExportBundle,
    ExportEvent,
    MemoryAtTimeData,
    MemoryAtTimeGetInput,
    MemoryVersionView,
    ProjectionManifest,
    ProjectionRebuildData,
    WorkspaceImportData,
)

_SCHEMA_VERSION = CURRENT_SCHEMA_VERSION
_FORMAT_VERSION = 7
_MAX_EXPORT_EVENTS = 10_000
_IMPORT_LEASE_RENEW_MARGIN_US = 5 * 60 * 1_000_000
_CURSOR_RE = re.compile(r"^cur_([0-9a-f]{64})$")
_WINDOWS_ABSOLUTE_PATH = re.compile(r"(?<![A-Za-z0-9])(?:[A-Za-z]:[\\/]|\\\\)")
_POSIX_ABSOLUTE_PATH = re.compile(r"(?:^|[\s\"'=(])/(?!/)[A-Za-z0-9_.-]")
_EVENT_COLUMNS = (
    "event_id,workspace_id,stream_id,stream_kind,stream_version,event_type,"
    "event_schema_version,occurred_at_us,recorded_at_us,actor_type,actor_id,"
    "causation_event_id,correlation_id,payload_json,payload_hash,"
    "previous_event_hash,event_hash"
)
_INTERNAL_EVENT_KEYS = frozenset(
    {
        "event_id",
        "workspace_id",
        "stream_id",
        "stream_kind",
        "stream_version",
        "event_type",
        "event_schema_version",
        "occurred_at_us",
        "recorded_at_us",
        "actor_type",
        "actor_id",
        "causation_event_id",
        "correlation_id",
        "payload",
        "payload_hash",
        "previous_event_hash",
        "event_hash",
    }
)
_PUBLIC_ENVELOPE_KEYS = frozenset(
    {
        "actor_id",
        "actor_type",
        "causation_event_id",
        "correlation_id",
        "data",
        "event_schema_version",
        "previous_event_hash",
        "recorded_at_us",
        "stream_id",
        "stream_kind",
        "stream_version",
    }
)
_UNIX_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)

# This is intentionally not the event loop's default executor.  The semaphore
# remains owned by the concurrent future after an asyncio waiter is cancelled,
# so cancellation cannot release capacity while SQLite still holds a lock.
_LOGGER = logging.getLogger(__name__)

_CORE_OPERATION_WORKERS = BoundedWorkerPool(
    max_workers=4,
    thread_name_prefix="daem0nmcp-v7-core",
)


class CoreOperationError(RuntimeError):
    """Sanitized operation failure understood by the shared v7 router."""

    def __init__(self, code: str) -> None:
        if code not in STABLE_ERROR_CODE_SET:
            raise ValueError("core operation error code is not stable")
        self.code = code
        super().__init__(code)


class _WorkerCancelledError(RuntimeError):
    """Internal signal proving a mutation rolled back before commit."""


def _default_storage_path(workspace: Workspace) -> Path:
    return workspace.root / ".daem0nmcp" / "storage"


def _default_clock() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True, slots=True)
class CoreOperationDependencies:
    """Reviewed dependencies for the six canonical granular adapters."""

    covenant_gate: CovenantGate
    scope_provider: Callable[[], InvocationScope | None]
    storage_path_resolver: Callable[[Workspace], str | os.PathLike[str]] = (
        _default_storage_path
    )
    clock: Callable[[], datetime] = field(default=_default_clock)
    projection_config: object | None = None
    projection_capability_statuses: Mapping[str, str] | None = None
    cursor_secret: bytes = field(default_factory=lambda: secrets.token_bytes(32))

    def __post_init__(self) -> None:
        if not isinstance(self.covenant_gate, CovenantGate):
            raise TypeError("covenant_gate must be a CovenantGate")
        for name in ("scope_provider", "storage_path_resolver", "clock"):
            if not callable(getattr(self, name)):
                raise TypeError(f"{name} must be callable")
        if not isinstance(self.cursor_secret, bytes) or len(self.cursor_secret) < 32:
            raise ValueError("cursor_secret must contain at least 32 bytes")


def _canonical_root(workspace: Workspace) -> str:
    return os.path.normcase(str(workspace.root.resolve()))


def _authorize_workspace(workspace: Workspace, request: AdmittedRequest) -> None:
    if (
        not isinstance(workspace, Workspace)
        or request.workspace_id != workspace.workspace_id
    ):
        raise CoreOperationError("UNAUTHORIZED_WORKSPACE")


def _validated_storage_path(
    dependencies: CoreOperationDependencies,
    workspace: Workspace,
) -> Path:
    try:
        root = workspace.root.resolve(strict=True)
        storage = Path(dependencies.storage_path_resolver(workspace))
        if storage.is_symlink():
            raise ValueError("storage link")
        resolved = storage.resolve(strict=True)
        resolved.relative_to(root)
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        raise CoreOperationError("WORKSPACE_PATH_ESCAPE") from exc
    return resolved


def _portable_failure_code(error: BaseException, operation: str) -> str:
    """Name a transfer failure the caller can act on, and log its cause.

    A concurrent writer (a projection drain or a dreaming job) can hold the
    SQLite write lock past the busy timeout while a transfer does its session
    bookkeeping. That is a transient, retryable condition, not an invalid
    bundle, and reporting it as one leaves the caller with no next step.
    Only the explicit ``raise ... from`` chain counts: a bundle rejected while
    an unrelated busy error is in flight is still an invalid bundle.
    """

    seen: set[int] = set()
    cause: BaseException | None = error
    while cause is not None and id(cause) not in seen:
        seen.add(id(cause))
        if is_database_busy(cause):
            _LOGGER.warning("%s deferred: %s", operation, cause)
            return "DATABASE_IN_USE"
        cause = cause.__cause__
    _LOGGER.warning(
        "%s rejected the bundle: %s", operation, type(error).__name__, exc_info=error
    )
    return "IMPORT_INVALID"


def _verify_schema(connection: sqlite3.Connection) -> None:
    try:
        row = connection.execute(
            "SELECT COALESCE(MAX(version),0) FROM schema_version"
        ).fetchone()
    except sqlite3.Error as exc:
        raise CoreOperationError("CAPABILITY_DEGRADED") from exc
    if row is None or type(row[0]) is not int or row[0] != _SCHEMA_VERSION:
        raise CoreOperationError("CAPABILITY_DEGRADED")


@contextmanager
def _active_connection(
    dependencies: CoreOperationDependencies,
    workspace: Workspace,
) -> Iterator[sqlite3.Connection]:
    """Hold one shared generation lock from pointer resolution through I/O."""

    storage = _validated_storage_path(dependencies, workspace)
    try:
        with DatabaseFileLock(storage, "shared"):
            active = resolve_active_database(storage)
            if active.format_version != _FORMAT_VERSION:
                raise CoreOperationError("CAPABILITY_DEGRADED")
            connection = sqlite3.connect(active.path, timeout=5.0)
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys=ON")
            try:
                _verify_schema(connection)
                yield connection
            finally:
                connection.close()
    except CoreOperationError:
        raise
    except DatabaseInUseError as exc:
        raise CoreOperationError("DATABASE_IN_USE") from exc
    except PointerValidationError as exc:
        raise CoreOperationError("CAPABILITY_DEGRADED") from exc
    except sqlite3.Error as exc:
        raise CoreOperationError("CAPABILITY_DEGRADED") from exc


async def _run_blocking(operation: Callable[[], Any]) -> Any:
    worker = asyncio.create_task(_CORE_OPERATION_WORKERS.run(operation))
    try:
        return await asyncio.shield(worker)
    except asyncio.CancelledError as cancellation:
        # A thread-pool future cannot be cancelled once it has started.  Keep
        # the coroutine alive until that work reaches a terminal state so a
        # caller never observes cancellation while detached SQLite work can
        # still commit later.
        while not worker.done():
            try:
                await asyncio.shield(worker)
            except asyncio.CancelledError:
                continue
            except Exception:
                break
        if worker.done():
            with suppress(Exception):
                worker.result()
        raise cancellation
    except BoundedWorkerBusyError as exc:
        raise CoreOperationError("TASK_REQUIRED") from exc


async def _run_mutation(
    operation: Callable[[threading.Event], Any],
) -> Any:
    cancelled = threading.Event()
    worker = asyncio.create_task(
        _CORE_OPERATION_WORKERS.run(lambda: operation(cancelled))
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
        # Once the worker has committed, its receipt wins over late
        # cancellation so callers are never told a durable mutation failed.
        return result
    except BoundedWorkerBusyError as exc:
        raise CoreOperationError("TASK_REQUIRED") from exc


def _raise_if_cancelled(cancelled: threading.Event) -> None:
    if cancelled.is_set():
        raise _WorkerCancelledError()


def _utc_from_us(value: object) -> datetime:
    if isinstance(value, bool) or not isinstance(value, int):
        raise CoreOperationError("IMPORT_INVALID")
    try:
        return _UNIX_EPOCH + timedelta(microseconds=value)
    except (OSError, OverflowError, ValueError) as exc:
        raise CoreOperationError("IMPORT_INVALID") from exc


def _us_from_datetime(value: object) -> int:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise CoreOperationError("IMPORT_INVALID")
    try:
        delta = value.astimezone(timezone.utc) - _UNIX_EPOCH
        return (
            delta.days * 86_400_000_000 + delta.seconds * 1_000_000 + delta.microseconds
        )
    except (OSError, OverflowError, ValueError) as exc:
        raise CoreOperationError("IMPORT_INVALID") from exc


def _row_event(row: sqlite3.Row) -> dict[str, Any]:
    event = dict(row)
    try:
        event["payload"] = parse_canonical_json(event.pop("payload_json"))
    except Exception as exc:
        raise CoreOperationError("IMPORT_INVALID") from exc
    return event


def _verify_event(
    event: Mapping[str, Any],
    workspace_id: str,
    *,
    expected_previous_hash: str | None | object = ...,
) -> None:
    if set(event) != _INTERNAL_EVENT_KEYS or event.get("workspace_id") != workspace_id:
        raise CoreOperationError("IMPORT_INVALID")
    payload = event.get("payload")
    if not isinstance(payload, Mapping):
        raise CoreOperationError("IMPORT_INVALID")
    try:
        payload_hash = sha256_json(payload)
        envelope = {
            key: event[key]
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
        calculated_hash = event_hash_for(envelope)
    except Exception as exc:
        raise CoreOperationError("IMPORT_INVALID") from exc
    if (
        payload_hash != event.get("payload_hash")
        or calculated_hash != event.get("event_hash")
        or event_id_for_hash(calculated_hash) != event.get("event_id")
    ):
        raise CoreOperationError("IMPORT_INVALID")
    if (
        expected_previous_hash is not ...
        and event.get("previous_event_hash") != expected_previous_hash
    ):
        raise CoreOperationError("IMPORT_INVALID")


def _validate_bundle(bundle: Mapping[str, Any], workspace_id: str) -> None:
    if (
        set(bundle) != {"workspace_id", "event_schema_version", "events", "root_hash"}
        or bundle.get("workspace_id") != workspace_id
        or bundle.get("event_schema_version") != 1
        or not isinstance(bundle.get("events"), list)
    ):
        raise CoreOperationError("IMPORT_INVALID")
    events = bundle["events"]
    if len(events) > _MAX_EXPORT_EVENTS:
        raise CoreOperationError("TASK_REQUIRED")
    event_ids: set[str] = set()
    event_hashes: set[str] = set()
    streams: dict[str, list[Mapping[str, Any]]] = {}
    for event in events:
        if not isinstance(event, Mapping):
            raise CoreOperationError("IMPORT_INVALID")
        _verify_event(event, workspace_id)
        event_id = event["event_id"]
        event_hash = event["event_hash"]
        if event_id in event_ids or event_hash in event_hashes:
            raise CoreOperationError("IMPORT_INVALID")
        event_ids.add(event_id)
        event_hashes.add(event_hash)
        streams.setdefault(str(event["stream_id"]), []).append(event)
    if [event["event_id"] for event in events] != sorted(event_ids):
        raise CoreOperationError("IMPORT_INVALID")
    for stream in streams.values():
        ordered = sorted(stream, key=lambda item: item["stream_version"])
        previous: str | None = None
        for version, event in enumerate(ordered, 1):
            if event["stream_version"] != version:
                raise CoreOperationError("IMPORT_INVALID")
            _verify_event(
                event,
                workspace_id,
                expected_previous_hash=previous,
            )
            previous = str(event["event_hash"])
    if any(
        event.get("causation_event_id") is not None
        and event.get("causation_event_id") not in event_ids
        for event in events
    ):
        raise CoreOperationError("IMPORT_INVALID")
    digest = hashlib.sha256()
    try:
        for event in events:
            digest.update(bytes.fromhex(str(event["event_hash"])))
    except ValueError as exc:
        raise CoreOperationError("IMPORT_INVALID") from exc
    if bundle.get("root_hash") != digest.hexdigest():
        raise CoreOperationError("IMPORT_INVALID")


def _reject_raw_paths(value: object) -> None:
    if isinstance(value, str):
        if (
            _WINDOWS_ABSOLUTE_PATH.search(value) is not None
            or _POSIX_ABSOLUTE_PATH.search(value) is not None
        ):
            raise CoreOperationError("WORKSPACE_PATH_ESCAPE")
    elif isinstance(value, Mapping):
        for key, item in value.items():
            if (
                key in {"file_path", "project_path", "database_path"}
                and item is not None
                and item != ""
            ):
                raise CoreOperationError("WORKSPACE_PATH_ESCAPE")
            _reject_raw_paths(item)
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        for item in value:
            _reject_raw_paths(item)
    elif isinstance(value, (bytes, bytearray, memoryview)):
        raise CoreOperationError("CAPABILITY_DISABLED")


def _public_event(event: Mapping[str, Any]) -> ExportEvent:
    _reject_raw_paths(event["payload"])
    record_id = event["stream_id"] if event["stream_kind"] == "memory" else None
    return ExportEvent(
        event_id=event["event_id"],
        record_id=record_id,
        event_type=event["event_type"],
        happened_at=_utc_from_us(event["occurred_at_us"]),
        content_hash=event["event_hash"],
        payload={
            "actor_id": event["actor_id"],
            "actor_type": event["actor_type"],
            "causation_event_id": event["causation_event_id"],
            "correlation_id": event["correlation_id"],
            "data": event["payload"],
            "event_schema_version": event["event_schema_version"],
            "previous_event_hash": event["previous_event_hash"],
            "recorded_at_us": event["recorded_at_us"],
            "stream_id": event["stream_id"],
            "stream_kind": event["stream_kind"],
            "stream_version": event["stream_version"],
        },
    )


def _internal_bundle(bundle: ExportBundle) -> dict[str, Any]:
    if bundle.vectors_included:
        raise CoreOperationError("CAPABILITY_DISABLED")
    events: list[dict[str, Any]] = []
    for public in bundle.events:
        envelope = public.payload
        if set(envelope) != _PUBLIC_ENVELOPE_KEYS:
            raise CoreOperationError("IMPORT_INVALID")
        data = envelope.get("data")
        if not isinstance(data, Mapping):
            raise CoreOperationError("IMPORT_INVALID")
        _reject_raw_paths(data)
        event = {
            "event_id": public.event_id,
            "workspace_id": bundle.workspace_id,
            "stream_id": envelope["stream_id"],
            "stream_kind": envelope["stream_kind"],
            "stream_version": envelope["stream_version"],
            "event_type": public.event_type,
            "event_schema_version": envelope["event_schema_version"],
            "occurred_at_us": _us_from_datetime(public.happened_at),
            "recorded_at_us": envelope["recorded_at_us"],
            "actor_type": envelope["actor_type"],
            "actor_id": envelope["actor_id"],
            "causation_event_id": envelope["causation_event_id"],
            "correlation_id": envelope["correlation_id"],
            "payload": dict(data),
            "payload_hash": sha256_json(data),
            "previous_event_hash": envelope["previous_event_hash"],
            "event_hash": public.content_hash,
        }
        expected_record_id = (
            event["stream_id"] if event["stream_kind"] == "memory" else None
        )
        if public.record_id != expected_record_id:
            raise CoreOperationError("IMPORT_INVALID")
        events.append(event)
    internal = {
        "workspace_id": bundle.workspace_id,
        "event_schema_version": 1,
        "events": events,
        "root_hash": bundle.root_hash,
    }
    _validate_bundle(internal, bundle.workspace_id)
    return internal


def _cursor_for(
    secret: bytes,
    workspace_id: str,
    record_id: str,
    event_id: str,
    event_hash: str,
) -> str:
    binding = hmac.new(
        secret,
        canonical_json_bytes(
            [
                "v7-memory-version-cursor",
                workspace_id,
                record_id,
                event_id,
                event_hash,
            ]
        ),
        hashlib.sha256,
    ).hexdigest()
    return f"cur_{binding}"


def _cursor_event(
    connection: sqlite3.Connection,
    secret: bytes,
    workspace_id: str,
    record_id: str,
    cursor: str,
) -> dict[str, Any]:
    match = _CURSOR_RE.fullmatch(cursor)
    if match is None:
        raise CoreOperationError("INVALID_ARGUMENT")
    rows = connection.execute(
        f"SELECT {_EVENT_COLUMNS} FROM memory_events "
        "WHERE workspace_id=? AND stream_id=? AND stream_kind='memory' "
        "ORDER BY stream_version ASC",
        (workspace_id, record_id),
    ).fetchall()
    for row in rows:
        event = _row_event(row)
        _verify_event(event, workspace_id)
        expected = _cursor_for(
            secret,
            workspace_id,
            record_id,
            str(event["event_id"]),
            str(event["event_hash"]),
        )
        if hmac.compare_digest(cursor, expected):
            return event
    raise CoreOperationError("INVALID_ARGUMENT")


def _record_from_event(event: Mapping[str, Any]) -> Mapping[str, Any]:
    payload = event.get("payload")
    record = payload.get("record") if isinstance(payload, Mapping) else None
    if not isinstance(record, Mapping):
        raise CoreOperationError("IMPORT_INVALID")
    if record.get("record_type") == "legacy":
        raise CoreOperationError("CAPABILITY_DEGRADED")
    _reject_raw_paths(record)
    return record


def _version_id(event: Mapping[str, Any]) -> str:
    return "ver_" + sha256_json(
        [
            "v7-memory-version",
            event["workspace_id"],
            event["stream_id"],
            event["event_id"],
            event["stream_version"],
            event["event_hash"],
        ]
    )


def _summary(
    event: Mapping[str, Any],
    *,
    created_at_us: int,
    superseded: bool,
) -> RecordSummary:
    record = _record_from_event(event)
    content = record.get("content")
    tags = record.get("tags", [])
    relative = record.get("file_path_relative")
    if not isinstance(content, str) or not content or not isinstance(tags, list):
        raise CoreOperationError("IMPORT_INVALID")
    if record.get("deleted_at_us") is not None:
        status = "invalidated"
    elif record.get("archived") is True:
        status = "archived"
    elif superseded:
        status = "superseded"
    else:
        status = "current"
    try:
        return RecordSummary.model_validate(
            {
                "record_id": event["stream_id"],
                "record_type": record["record_type"],
                "excerpt": content[:4000],
                "tags": tags,
                "relative_file_path": relative,
                "current_status": status,
                "content_hash": memory_content_hash(record),
                "created_at": _utc_from_us(created_at_us),
                "updated_at": _utc_from_us(event["recorded_at_us"]),
            }
        )
    except CoreOperationError:
        raise
    except Exception as exc:
        raise CoreOperationError("IMPORT_INVALID") from exc


def _first_occurred_at(
    connection: sqlite3.Connection,
    workspace_id: str,
    record_id: str,
) -> int:
    row = connection.execute(
        "SELECT occurred_at_us FROM memory_events WHERE workspace_id=? "
        "AND stream_id=? AND stream_kind='memory' AND stream_version=1",
        (workspace_id, record_id),
    ).fetchone()
    if row is None or type(row[0]) is not int:
        raise CoreOperationError("NOT_FOUND")
    return row[0]


def _versions_sync(
    dependencies: CoreOperationDependencies,
    workspace: Workspace,
    request: AdmittedRequest,
) -> Page[MemoryVersionView]:
    with _active_connection(dependencies, workspace) as connection:
        created_at_us = _first_occurred_at(
            connection, workspace.workspace_id, request.record_id
        )
        start_version = 0
        previous_hash: str | None = None
        if request.cursor is not None:
            cursor_event = _cursor_event(
                connection,
                dependencies.cursor_secret,
                workspace.workspace_id,
                request.record_id,
                request.cursor,
            )
            start_version = int(cursor_event["stream_version"])
            previous_hash = str(cursor_event["event_hash"])
        rows = connection.execute(
            f"SELECT {_EVENT_COLUMNS} FROM memory_events "
            "WHERE workspace_id=? AND stream_id=? AND stream_kind='memory' "
            "AND stream_version>? ORDER BY stream_version ASC LIMIT ?",
            (
                workspace.workspace_id,
                request.record_id,
                start_version,
                request.limit + 1,
            ),
        ).fetchall()
        events = [_row_event(row) for row in rows]
        expected_version = start_version + 1
        for event in events:
            if event["stream_version"] != expected_version:
                raise CoreOperationError("IMPORT_INVALID")
            _verify_event(
                event,
                workspace.workspace_id,
                expected_previous_hash=previous_hash,
            )
            previous_hash = str(event["event_hash"])
            expected_version += 1
        truncated = len(events) > request.limit
        selected = events[: request.limit]
        items: list[MemoryVersionView] = []
        for index, event in enumerate(selected):
            next_event = events[index + 1] if index + 1 < len(events) else None
            valid_to = None
            if next_event is not None:
                if next_event["occurred_at_us"] <= event["occurred_at_us"]:
                    raise CoreOperationError("IMPORT_INVALID")
                valid_to = _utc_from_us(next_event["occurred_at_us"])
            items.append(
                MemoryVersionView(
                    version_id=_version_id(event),
                    record=_summary(
                        event,
                        created_at_us=created_at_us,
                        superseded=next_event is not None,
                    ),
                    event_id=event["event_id"],
                    valid_from=_utc_from_us(event["occurred_at_us"]),
                    valid_to=valid_to,
                    transaction_time=_utc_from_us(event["recorded_at_us"]),
                )
            )
        next_cursor = None
        if truncated and selected:
            final = selected[-1]
            next_cursor = _cursor_for(
                dependencies.cursor_secret,
                workspace.workspace_id,
                request.record_id,
                str(final["event_id"]),
                str(final["event_hash"]),
            )
        return Page[MemoryVersionView](
            items=items,
            next_cursor=next_cursor,
            truncated=truncated,
        )


def _at_time_sync(
    dependencies: CoreOperationDependencies,
    workspace: Workspace,
    request: AdmittedRequest,
) -> MemoryAtTimeData:
    # Admission deliberately retains JSON-safe normalized arguments. Restore
    # schema types here; ISO timestamps on the MCP wire are not datetime objects.
    arguments = MemoryAtTimeGetInput.model_validate(request.model_dump())
    with _active_connection(dependencies, workspace) as connection:
        created_at_us = _first_occurred_at(
            connection, workspace.workspace_id, request.record_id
        )
        valid_at = _us_from_datetime(arguments.valid_time)
        transaction_time = arguments.transaction_time or dependencies.clock()
        transaction_at = _us_from_datetime(transaction_time)
        rows = connection.execute(
            f"SELECT {_EVENT_COLUMNS} FROM memory_events "
            "WHERE workspace_id=? AND stream_id=? AND stream_kind='memory' "
            "AND occurred_at_us<=? AND recorded_at_us<=? "
            "ORDER BY occurred_at_us DESC,stream_version DESC LIMIT 1",
            (
                workspace.workspace_id,
                request.record_id,
                valid_at,
                transaction_at,
            ),
        ).fetchall()
        if not rows:
            raise CoreOperationError("NOT_FOUND")
        event = _row_event(rows[0])
        _verify_event(event, workspace.workspace_id)
        version_id = _version_id(event)
        summary = _summary(event, created_at_us=created_at_us, superseded=False)
        evidence = EvidenceRef(
            record_id=request.record_id,
            event_id=event["event_id"],
            content_hash=summary.content_hash,
            version_id=version_id,
            relation_path=[],
            provider="temporal",
        )
        return MemoryAtTimeData(
            record=summary,
            version_id=version_id,
            evidence_refs=[evidence],
        )


def _manifest_from_row(row: sqlite3.Row | None) -> ProjectionManifest | None:
    if row is None:
        return None
    built_at = row[3] if row[3] is not None else row[4]
    if built_at is None:
        raise CoreOperationError("CAPABILITY_DEGRADED")
    try:
        return ProjectionManifest(
            projection=row[0],
            generation=row[1],
            source_root_hash=row[2],
            built_at=_utc_from_us(built_at),
        )
    except CoreOperationError:
        raise
    except Exception as exc:
        raise CoreOperationError("CAPABILITY_DEGRADED") from exc


def _active_manifest(
    connection: sqlite3.Connection,
    workspace_id: str,
    projection: str,
) -> sqlite3.Row | None:
    return connection.execute(
        "SELECT projection_name,generation,source_event_root_hash,"
        "activated_at_us,completed_at_us,row_count,source_event_count "
        "FROM projection_manifests WHERE workspace_id=? AND projection_name=? "
        "AND status='active' LIMIT 1",
        (workspace_id, projection),
    ).fetchone()


def _projection_sync(
    dependencies: CoreOperationDependencies,
    workspace: Workspace,
    request: AdmittedRequest,
    cancelled: threading.Event,
) -> ProjectionRebuildData:
    _raise_if_cancelled(cancelled)
    with _active_connection(dependencies, workspace) as connection:
        _raise_if_cancelled(cancelled)
        previous_row = _active_manifest(
            connection, workspace.workspace_id, request.projection
        )
        builders: Mapping[str, object] | None = None
        try:
            from ...retrieval.runtime import create_projection_builders

            builders = create_projection_builders(
                connection,
                connection.execute("PRAGMA database_list").fetchone()[2],
                config=dependencies.projection_config,
                include_optional=request.projection != "lexical",
                capability_statuses=dependencies.projection_capability_statuses,
            )
            if previous_row is not None and not request.force:
                preview = rebuild_projection(
                    connection,
                    workspace_id=workspace.workspace_id,
                    projection=request.projection,
                    dry_run=True,
                    builders=builders,
                )
                if (
                    preview.get("capability_status") == "ready"
                    and previous_row[2] == preview.get("source_event_root_hash")
                    and previous_row[5] == preview.get("row_count")
                    and previous_row[6] == preview.get("source_event_count")
                ):
                    current = _manifest_from_row(previous_row)
                    if current is None:
                        raise CoreOperationError("CAPABILITY_DEGRADED")
                    _raise_if_cancelled(cancelled)
                    return ProjectionRebuildData(
                        manifest=current,
                        previous_manifest=None,
                        counts={
                            "rows": int(preview["row_count"]),
                            "source_events": int(preview["source_event_count"]),
                            "previous_rows": int(previous_row[5]),
                        },
                        diagnostics=[
                            DiagnosticSummary(
                                code="PROJECTION_CURRENT",
                                message=(
                                    "The active projection already matches the "
                                    "canonical event snapshot."
                                ),
                            )
                        ],
                    )
            _raise_if_cancelled(cancelled)
            result = rebuild_projection(
                connection,
                workspace_id=workspace.workspace_id,
                projection=request.projection,
                dry_run=False,
                builders=builders,
            )
        except ProjectionOperationError as exc:
            if exc.code == "PROJECTION_BUILDER_UNAVAILABLE":
                raise CoreOperationError("CAPABILITY_DISABLED") from exc
            if exc.code in {"LEXICAL_UNAVAILABLE", "FTS5_UNAVAILABLE"}:
                raise CoreOperationError("LEXICAL_UNAVAILABLE") from exc
            raise CoreOperationError("CAPABILITY_DEGRADED") from exc
        except CoreOperationError:
            raise
        except Exception as exc:
            raise CoreOperationError("CAPABILITY_DEGRADED") from exc
        finally:
            close = getattr(builders, "close", None)
            if callable(close):
                with suppress(Exception):
                    close()
        active_row = _active_manifest(
            connection, workspace.workspace_id, request.projection
        )
        manifest = _manifest_from_row(active_row)
        if manifest is None:
            raise CoreOperationError("CAPABILITY_DEGRADED")
        previous = _manifest_from_row(previous_row)
        diagnostics: list[DiagnosticSummary] = []
        if result.get("capability_status") != "ready":
            diagnostics.append(
                DiagnosticSummary(
                    code="PROJECTION_DEGRADED",
                    message="The projection provider reported a degraded build.",
                )
            )
        return ProjectionRebuildData(
            manifest=manifest,
            previous_manifest=previous,
            counts={
                "rows": int(result["row_count"]),
                "source_events": int(result["source_event_count"]),
                "previous_rows": int(result.get("active_row_count", 0)),
            },
            diagnostics=diagnostics,
        )


def _export_sync(
    dependencies: CoreOperationDependencies,
    workspace: Workspace,
    request: AdmittedRequest,
    cancelled: threading.Event,
) -> ExportBundle:
    with _active_connection(dependencies, workspace) as connection:
        try:
            storage = _validated_storage_path(dependencies, workspace)
            if request.export_session_id is None:
                page = create_export_session(
                    connection,
                    storage,
                    workspace.workspace_id,
                    include_legacy_projection=request.include_legacy_projection,
                    include_vectors=request.include_vectors,
                    config=dependencies.projection_config,
                    clock=dependencies.clock(),
                    cursor_secret=dependencies.cursor_secret,
                    public_event=lambda event: _public_event(event).model_dump(
                        mode="json"
                    ),
                    page_byte_limit=request.page_byte_limit,
                    local_capability_ready=(
                        dependencies.projection_capability_statuses is not None
                        and dependencies.projection_capability_statuses.get("local")
                        == "ready"
                    ),
                    cancelled=cancelled.is_set,
                )
            else:
                page = read_export_page(
                    connection,
                    storage,
                    workspace.workspace_id,
                    session_id=request.export_session_id,
                    page_index=request.page_index,
                    cursor=request.cursor,
                    cursor_secret=dependencies.cursor_secret,
                    now=dependencies.clock(),
                )
            content = page["content"]
            manifest = page["manifest"]
            items = content["items"]
            descriptor = page.get(
                "page_descriptor",
                {
                    "page_index": page["page_index"],
                    "page_kind": page["page_kind"],
                    "item_count": len(items),
                    "byte_count": len(canonical_json_bytes(content)),
                    "page_hash": page["page_hash"],
                },
            )
            return ExportBundle(
                bundle_version=2,
                workspace_id=workspace.workspace_id,
                exported_at=dependencies.clock(),
                root_hash=manifest["event_root_hash"],
                events=items if page["page_kind"] == "events" else [],
                legacy_projection_included=manifest["legacy_projection"] is not None,
                vectors_included=manifest["vectors"] is not None,
                export_session_id=page["export_session_id"],
                manifest_hash=page["manifest_hash"],
                manifest=manifest,
                page_index=page["page_index"],
                page_count=page["page_count"],
                page_kind=page["page_kind"],
                page_hash=page["page_hash"],
                page_descriptor=descriptor,
                page_proof=page.get("page_proof", []),
                next_cursor=page["next_cursor"],
                complete=page["complete"],
                legacy_rows=items if page["page_kind"] == "legacy" else [],
                vector_points=items if page["page_kind"] == "vectors" else [],
            )
        except CoreOperationError:
            raise
        except PortableTransferError as exc:
            code = exc.code
            if code == "IMPORT_INVALID":
                code = _portable_failure_code(exc, "workspace_export")
            raise CoreOperationError(code) from exc
        except Exception as exc:
            raise CoreOperationError(
                _portable_failure_code(exc, "workspace_export")
            ) from exc


def _journal_payload(bundle: ExportBundle, merge: bool) -> dict[str, Any]:
    return {
        "api_version": "7",
        "bundle_hash": sha256_json(bundle.model_dump(mode="json")),
        "event_count": len(bundle.events),
        "merge": merge,
        "root_hash": bundle.root_hash,
    }


def _portable_page(bundle: ExportBundle) -> dict[str, Any]:
    if bundle.bundle_version != 2 or bundle.page_kind is None:
        raise CoreOperationError("IMPORT_INVALID")
    items: list[dict[str, Any]]
    if bundle.page_kind == "events":
        items = [event.model_dump(mode="json") for event in bundle.events]
    elif bundle.page_kind == "legacy":
        items = list(bundle.legacy_rows)
    else:
        items = [point.model_dump(mode="json") for point in bundle.vector_points]
    return {
        "manifest_hash": bundle.manifest_hash,
        "manifest": bundle.manifest,
        "page_index": bundle.page_index,
        "page_count": bundle.page_count,
        "page_kind": bundle.page_kind,
        "page_hash": bundle.page_hash,
        "page_descriptor": bundle.page_descriptor,
        "page_proof": bundle.page_proof,
        "content": {"kind": bundle.page_kind, "items": items},
    }


def _decode_portable_event(
    public_value: Mapping[str, Any], workspace_id: str
) -> dict[str, Any]:
    try:
        public = ExportEvent.model_validate(public_value)
        envelope = public.payload
        if set(envelope) != _PUBLIC_ENVELOPE_KEYS:
            raise CoreOperationError("IMPORT_INVALID")
        data = envelope.get("data")
        if not isinstance(data, Mapping):
            raise CoreOperationError("IMPORT_INVALID")
        _reject_raw_paths(data)
        event = {
            "event_id": public.event_id,
            "workspace_id": workspace_id,
            "stream_id": envelope["stream_id"],
            "stream_kind": envelope["stream_kind"],
            "stream_version": envelope["stream_version"],
            "event_type": public.event_type,
            "event_schema_version": envelope["event_schema_version"],
            "occurred_at_us": _us_from_datetime(public.happened_at),
            "recorded_at_us": envelope["recorded_at_us"],
            "actor_type": envelope["actor_type"],
            "actor_id": envelope["actor_id"],
            "causation_event_id": envelope["causation_event_id"],
            "correlation_id": envelope["correlation_id"],
            "payload": dict(data),
            "payload_hash": sha256_json(data),
            "previous_event_hash": envelope["previous_event_hash"],
            "event_hash": public.content_hash,
        }
        expected_record_id = (
            event["stream_id"] if event["stream_kind"] == "memory" else None
        )
        if public.record_id != expected_record_id:
            raise CoreOperationError("IMPORT_INVALID")
        _verify_event(event, workspace_id)
        return event
    except CoreOperationError:
        raise
    except Exception as exc:
        raise CoreOperationError("IMPORT_INVALID") from exc


def _event_command(event: Mapping[str, Any]) -> EventCommand:
    return EventCommand(
        workspace_id=str(event["workspace_id"]),
        stream_id=str(event["stream_id"]),
        stream_kind=str(event["stream_kind"]),
        event_type=str(event["event_type"]),
        occurred_at_us=int(event["occurred_at_us"]),
        recorded_at_us=int(event["recorded_at_us"]),
        actor_type=str(event["actor_type"]),
        actor_id=event["actor_id"],
        causation_event_id=event["causation_event_id"],
        correlation_id=event["correlation_id"],
        event_schema_version=int(event["event_schema_version"]),
        expected_stream_version=int(event["stream_version"]),
        payload=dict(event["payload"]),
    )


def _import_v2_sync(
    dependencies: CoreOperationDependencies,
    workspace: Workspace,
    request: AdmittedRequest,
    cancelled: threading.Event,
) -> WorkspaceImportData:
    storage = _validated_storage_path(dependencies, workspace)
    vector_candidate = None
    lease: ImportFinalizationLease | None = None
    committed = False
    session_id: str | None = request.import_session_id
    with _active_connection(dependencies, workspace) as connection:
        try:
            if request.bundle is not None:
                bundle = ExportBundle.model_validate(request.bundle)
                if bundle.workspace_id != workspace.workspace_id:
                    raise CoreOperationError("CROSS_WORKSPACE_IMPORT_UNSUPPORTED")
                if bundle.bundle_version != 2:
                    raise CoreOperationError("IMPORT_INVALID")
                _raise_if_cancelled(cancelled)
                connection.execute("BEGIN IMMEDIATE")
                session_id, ready = stage_import_page(
                    connection,
                    storage,
                    workspace.workspace_id,
                    _portable_page(bundle),
                    import_session_id=request.import_session_id,
                    now=dependencies.clock(),
                )
                _raise_if_cancelled(cancelled)
                connection.commit()
                staged = connection.execute(
                    "SELECT staged_page_count,page_count "
                    "FROM portable_transfer_sessions "
                    "WHERE session_id=?",
                    (session_id,),
                ).fetchone()
                if not (ready and request.finalize):
                    return WorkspaceImportData(
                        root_hash=bundle.root_hash,
                        imported=0,
                        skipped=0,
                        import_session_id=session_id,
                        status="ready" if ready else "staging",
                        staged_pages=int(staged[0]) if staged is not None else 0,
                        page_count=int(staged[1]) if staged is not None else 0,
                    )
            if not isinstance(session_id, str):
                raise CoreOperationError("IMPORT_INVALID")
            _raise_if_cancelled(cancelled)
            session_row = connection.execute(
                "SELECT manifest_json,manifest_hash FROM portable_transfer_sessions "
                "WHERE session_id=? AND workspace_id=? AND direction='import'",
                (session_id, workspace.workspace_id),
            ).fetchone()
            if session_row is None:
                raise CoreOperationError("IMPORT_INVALID")
            session_manifest = parse_canonical_json(str(session_row[0]))
            if not isinstance(session_manifest, Mapping) or sha256_json(
                session_manifest
            ) != str(session_row[1]):
                raise CoreOperationError("IMPORT_INVALID")
            manifest_hash = str(session_row[1])
            journal = {
                "api_version": "7",
                "bundle_version": 2,
                "manifest_hash": manifest_hash,
                "merge": request.merge,
                "root_hash": session_manifest.get("event_root_hash"),
            }
            payload_text = canonical_json_bytes(journal).decode("utf-8")
            payload_hash = sha256_json(journal)
            existing_receipt = connection.execute(
                "SELECT payload_hash,status,result_json FROM background_jobs "
                "WHERE workspace_id=? AND job_type='v7.workspace_import' "
                "AND idempotency_key=?",
                (workspace.workspace_id, request.idempotency_key),
            ).fetchone()
            if existing_receipt is not None:
                if (
                    existing_receipt[0] != payload_hash
                    or existing_receipt[1] != "succeeded"
                    or existing_receipt[2] is None
                ):
                    raise CoreOperationError("IDEMPOTENCY_CONFLICT")
                receipt = WorkspaceImportData.model_validate(
                    parse_canonical_json(str(existing_receipt[2]))
                )
                return receipt.model_copy(
                    update={
                        "imported": 0,
                        "skipped": receipt.imported + receipt.skipped,
                    }
                )
            lease = claim_import_finalization(
                connection,
                storage,
                workspace.workspace_id,
                session_id,
                now=dependencies.clock(),
            )
            prepared = prepare_import(
                connection,
                storage,
                workspace.workspace_id,
                session_id,
                lease=lease,
                decode_event=lambda value: _decode_portable_event(
                    value, workspace.workspace_id
                ),
                clock=dependencies.clock,
                cancelled=cancelled.is_set,
            )
            if prepared.lease is None:
                raise CoreOperationError("IMPORT_INVALID")
            lease = prepared.lease
            connection.commit()

            def finalization_checkpoint() -> None:
                nonlocal lease
                _raise_if_cancelled(cancelled)
                if lease is None:
                    raise PortableTransferError("IMPORT_INVALID")
                current = dependencies.clock()
                if (
                    _us_from_datetime(current)
                    >= lease.expires_at_us - _IMPORT_LEASE_RENEW_MARGIN_US
                ):
                    lease = renew_import_finalization(
                        connection,
                        workspace.workspace_id,
                        lease,
                        now=current,
                    )

            vector_candidate, vector_diagnostic = prepare_vector_candidate(
                prepared,
                connection,
                workspace.workspace_id,
                dependencies.projection_config,
                local_capability_ready=(
                    dependencies.projection_capability_statuses is not None
                    and dependencies.projection_capability_statuses.get("local")
                    == "ready"
                ),
                checkpoint=finalization_checkpoint,
            )
            lease = renew_import_finalization(
                connection,
                workspace.workspace_id,
                lease,
                now=dependencies.clock(),
            )
            connection.execute("BEGIN IMMEDIATE")
            existing_job = connection.execute(
                "SELECT payload_hash,status,result_json FROM background_jobs "
                "WHERE workspace_id=? AND job_type='v7.workspace_import' "
                "AND idempotency_key=?",
                (workspace.workspace_id, request.idempotency_key),
            ).fetchone()
            if existing_job is not None:
                if existing_job[0] != payload_hash or existing_job[1] != "succeeded":
                    raise CoreOperationError("IDEMPOTENCY_CONFLICT")
                result = parse_canonical_json(str(existing_job[2]))
                connection.commit()
                if vector_candidate is not None:
                    vector_candidate.discard(connection, workspace.workspace_id)
                    vector_candidate = None
                receipt = WorkspaceImportData.model_validate(result)
                return receipt.model_copy(
                    update={
                        "imported": 0,
                        "skipped": receipt.imported + receipt.skipped,
                    }
                )
            if not request.merge:
                occupied = connection.execute(
                    "SELECT 1 FROM memory_events WHERE workspace_id=? LIMIT 1",
                    (workspace.workspace_id,),
                ).fetchone()
                if occupied is not None:
                    raise CoreOperationError("CONFLICT")
            store = EventStore(connection, assume_transaction=True)
            imported = 0
            skipped = 0
            event_ids: list[str] = []
            total = 0
            for event in prepared.iter_events():
                finalization_checkpoint()
                present = connection.execute(
                    "SELECT event_hash FROM memory_events WHERE workspace_id=? "
                    "AND stream_id=? AND stream_version=?",
                    (
                        workspace.workspace_id,
                        event["stream_id"],
                        event["stream_version"],
                    ),
                ).fetchone()
                try:
                    appended = store.append_and_project(_event_command(event))
                except EventStreamConflict as exc:
                    raise CoreOperationError("EVENT_STREAM_CONFLICT") from exc
                if appended.event_hash != event["event_hash"]:
                    raise CoreOperationError("IMPORT_INVALID")
                if present is None:
                    imported += 1
                else:
                    if str(present[0]) != event["event_hash"]:
                        raise CoreOperationError("EVENT_STREAM_CONFLICT")
                    skipped += 1
                if len(event_ids) < 4096:
                    event_ids.append(str(event["event_id"]))
                total += 1
            now = dependencies.clock()
            reconstruct_legacy_mappings(
                connection,
                workspace.workspace_id,
                event_root_hash=str(prepared.manifest["event_root_hash"]),
                manifest_hash=manifest_hash,
                now_us=_us_from_datetime(now),
            )
            validate_legacy_projection(prepared, connection, workspace.workspace_id)
            diagnostics: list[DiagnosticSummary] = []
            if vector_candidate is not None:
                try:
                    activate_vector_candidate(
                        vector_candidate,
                        connection,
                        workspace.workspace_id,
                        now_us=_us_from_datetime(now),
                        checkpoint=finalization_checkpoint,
                    )
                except PortableTransferError as exc:
                    if exc.code != "VECTOR_REBUILD_REQUIRED":
                        raise
                    vector_candidate.discard(connection, workspace.workspace_id)
                    vector_candidate = None
                    vector_diagnostic = exc.code
            elif vector_diagnostic is not None:
                pass
            if vector_diagnostic is not None:
                diagnostics.append(
                    DiagnosticSummary(
                        code=vector_diagnostic,
                        message=(
                            "The imported vectors use a different model contract; "
                            "the dense projection must be rebuilt."
                        ),
                    )
                )
            result_model = WorkspaceImportData(
                root_hash=prepared.manifest["event_root_hash"],
                imported=imported,
                skipped=skipped,
                event_ids=event_ids,
                import_session_id=session_id,
                status="succeeded",
                staged_pages=prepared.page_count,
                page_count=prepared.page_count,
                event_ids_truncated=total > len(event_ids),
                diagnostics=diagnostics,
            )
            now_us = _us_from_datetime(now)
            connection.execute(
                "INSERT INTO background_jobs(job_id,workspace_id,job_type,"
                "idempotency_key,payload_json,payload_hash,status,priority,attempts,"
                "max_attempts,available_at_us,result_json,created_at_us,updated_at_us,"
                "started_at_us,finished_at_us) VALUES (?,?,?,?,?,?,'succeeded',0,1,1,"
                "?,?,?,?,?,?)",
                (
                    deterministic_id(
                        "job",
                        "v7.workspace_import",
                        workspace.workspace_id,
                        request.idempotency_key,
                    ),
                    workspace.workspace_id,
                    "v7.workspace_import",
                    request.idempotency_key,
                    payload_text,
                    payload_hash,
                    now_us,
                    canonical_json_bytes(result_model.model_dump(mode="json")).decode(
                        "utf-8"
                    ),
                    now_us,
                    now_us,
                    now_us,
                    now_us,
                ),
            )
            finalization_checkpoint()
            complete_import_session(
                connection,
                workspace.workspace_id,
                lease,
                now=dependencies.clock(),
            )
            connection.commit()
            committed = True
            if vector_candidate is not None:
                vector_candidate.close()
                vector_candidate = None
            with suppress(Exception):
                cleanup_import_attempt(
                    connection,
                    storage,
                    workspace.workspace_id,
                    lease,
                    now=dependencies.clock(),
                )
            return result_model
        except PortableTransferError as exc:
            if connection.in_transaction:
                connection.rollback()
            if vector_candidate is not None:
                vector_candidate.discard(connection, workspace.workspace_id)
                vector_candidate = None
            code = exc.code
            if code == "IMPORT_INVALID":
                code = _portable_failure_code(exc, "workspace_import")
            raise CoreOperationError(code) from exc
        except CoreOperationError:
            if connection.in_transaction:
                connection.rollback()
            if vector_candidate is not None:
                vector_candidate.discard(connection, workspace.workspace_id)
                vector_candidate = None
            raise
        except sqlite3.IntegrityError as exc:
            if connection.in_transaction:
                connection.rollback()
            if vector_candidate is not None:
                vector_candidate.discard(connection, workspace.workspace_id)
                vector_candidate = None
            raise CoreOperationError("IDEMPOTENCY_CONFLICT") from exc
        except _WorkerCancelledError:
            if connection.in_transaction:
                connection.rollback()
            if vector_candidate is not None:
                vector_candidate.discard(connection, workspace.workspace_id)
                vector_candidate = None
            raise
        except Exception as exc:
            if connection.in_transaction:
                connection.rollback()
            if vector_candidate is not None:
                vector_candidate.discard(connection, workspace.workspace_id)
            vector_candidate = None
            raise CoreOperationError(
                _portable_failure_code(exc, "workspace_import")
            ) from exc
        finally:
            if lease is not None and not committed:
                with suppress(Exception):
                    release_import_finalization(
                        connection,
                        storage,
                        lease,
                        now=dependencies.clock(),
                    )


def _import_sync(
    dependencies: CoreOperationDependencies,
    workspace: Workspace,
    request: AdmittedRequest,
    cancelled: threading.Event,
) -> WorkspaceImportData:
    _raise_if_cancelled(cancelled)
    if request.bundle is None:
        return _import_v2_sync(dependencies, workspace, request, cancelled)
    try:
        bundle = ExportBundle.model_validate(request.bundle)
    except Exception as exc:
        raise CoreOperationError("IMPORT_INVALID") from exc
    if bundle.bundle_version == 2:
        return _import_v2_sync(dependencies, workspace, request, cancelled)
    if bundle.workspace_id != workspace.workspace_id:
        raise CoreOperationError("CROSS_WORKSPACE_IMPORT_UNSUPPORTED")
    internal = _internal_bundle(bundle)
    payload = _journal_payload(bundle, request.merge)
    payload_text = canonical_json_bytes(payload).decode("utf-8")
    payload_hash = sha256_json(payload)
    event_ids = [event.event_id for event in bundle.events]
    with _active_connection(dependencies, workspace) as connection:
        try:
            _raise_if_cancelled(cancelled)
            connection.execute("BEGIN IMMEDIATE")
            _raise_if_cancelled(cancelled)
            existing = connection.execute(
                "SELECT job_id,payload_json,payload_hash,status FROM background_jobs "
                "WHERE workspace_id=? AND job_type='v7.workspace_import' "
                "AND idempotency_key=?",
                (workspace.workspace_id, request.idempotency_key),
            ).fetchone()
            if existing is not None:
                expected_job_id = deterministic_id(
                    "job",
                    "v7.workspace_import",
                    workspace.workspace_id,
                    request.idempotency_key,
                )
                if (
                    existing[0] != expected_job_id
                    or existing[1] != payload_text
                    or existing[2] != payload_hash
                    or existing[3] != "succeeded"
                ):
                    raise CoreOperationError("IDEMPOTENCY_CONFLICT")
                _raise_if_cancelled(cancelled)
                connection.commit()
                return WorkspaceImportData(
                    root_hash=bundle.root_hash,
                    imported=0,
                    skipped=len(event_ids),
                    event_ids=event_ids,
                )
            if not request.merge:
                occupied = connection.execute(
                    "SELECT 1 FROM memory_events WHERE workspace_id=? LIMIT 1",
                    (workspace.workspace_id,),
                ).fetchone()
                if occupied is not None:
                    raise CoreOperationError("CONFLICT")
            _raise_if_cancelled(cancelled)
            try:
                result = import_event_bundle(
                    connection,
                    internal,
                    workspace.workspace_id,
                    assume_transaction=True,
                )
            except EventBundleError as exc:
                if exc.code == "CROSS_WORKSPACE_IMPORT_UNSUPPORTED":
                    raise CoreOperationError(
                        "CROSS_WORKSPACE_IMPORT_UNSUPPORTED"
                    ) from exc
                raise CoreOperationError("IMPORT_INVALID") from exc
            except EventStreamConflict as exc:
                raise CoreOperationError("EVENT_STREAM_CONFLICT") from exc
            _raise_if_cancelled(cancelled)
            now = dependencies.clock()
            now_us = _us_from_datetime(now)
            reconstruct_legacy_mappings(
                connection,
                workspace.workspace_id,
                event_root_hash=result.root_hash,
                manifest_hash=None,
                now_us=now_us,
            )
            result_payload = {
                "event_ids": event_ids,
                "imported": result.events_imported,
                "root_hash": result.root_hash,
                "skipped": result.events_existing,
            }
            connection.execute(
                "INSERT INTO background_jobs ("
                "job_id,workspace_id,job_type,idempotency_key,payload_json,"
                "payload_hash,status,priority,attempts,max_attempts,available_at_us,"
                "lease_owner,lease_token,lease_expires_at_us,cancel_requested_at_us,"
                "last_error_json,result_json,source_event_id,created_at_us,updated_at_us,"
                "started_at_us,finished_at_us) VALUES (?,?,?,?,?,?,'succeeded',0,1,1,"
                "?,NULL,NULL,NULL,NULL,NULL,?,NULL,?,?,?,?)",
                (
                    deterministic_id(
                        "job",
                        "v7.workspace_import",
                        workspace.workspace_id,
                        request.idempotency_key,
                    ),
                    workspace.workspace_id,
                    "v7.workspace_import",
                    request.idempotency_key,
                    payload_text,
                    payload_hash,
                    now_us,
                    canonical_json_bytes(result_payload).decode("utf-8"),
                    now_us,
                    now_us,
                    now_us,
                    now_us,
                ),
            )
            _raise_if_cancelled(cancelled)
            connection.commit()
            return WorkspaceImportData(
                root_hash=result.root_hash,
                imported=result.events_imported,
                skipped=result.events_existing,
                event_ids=event_ids,
            )
        except CoreOperationError:
            connection.rollback()
            raise
        except sqlite3.IntegrityError as exc:
            connection.rollback()
            raise CoreOperationError("IDEMPOTENCY_CONFLICT") from exc
        except sqlite3.Error as exc:
            connection.rollback()
            raise CoreOperationError("IMPORT_INVALID") from exc
        except Exception:
            connection.rollback()
            raise


def _covenant_status(
    dependencies: CoreOperationDependencies,
    workspace: Workspace,
    request: AdmittedRequest,
) -> CovenantStatusData:
    _authorize_workspace(workspace, request)
    scope = dependencies.scope_provider()
    briefed = False
    briefed_at: datetime | None = None
    if isinstance(
        scope, InvocationScope
    ) and scope.canonical_workspace == _canonical_root(workspace):
        try:
            status = dependencies.covenant_gate.state_store.status(scope)
            raw_briefed_at = status.get("briefed_at")
            briefed = status.get("briefed") is True
            if briefed and type(raw_briefed_at) is int:
                briefed_at = datetime.fromtimestamp(
                    raw_briefed_at,
                    tz=timezone.utc,
                )
            elif briefed:
                briefed = False
        except Exception:
            briefed = False
            briefed_at = None
    ttl = dependencies.covenant_gate.authority.ttl_seconds
    if isinstance(ttl, bool) or not isinstance(ttl, int) or not 1 <= ttl <= 3600:
        raise CoreOperationError("CAPABILITY_DEGRADED")
    next_step = None
    if not briefed:
        next_step = CovenantNextStep(
            tool="session_brief",
            reason="Start the scoped Covenant session before protected work.",
        )
    return CovenantStatusData(
        briefed=briefed,
        briefed_at=briefed_at,
        token_ttl_seconds=ttl,
        next_step=next_step,
    )


def build_core_operations(
    dependencies: CoreOperationDependencies,
) -> Mapping[str, Callable[..., Any]]:
    """Return the exact immutable canonical-operation adapter registry."""

    if not isinstance(dependencies, CoreOperationDependencies):
        raise TypeError("dependencies must be CoreOperationDependencies")

    async def covenant_status(
        *, workspace: Workspace, request: AdmittedRequest
    ) -> CovenantStatusData:
        return _covenant_status(dependencies, workspace, request)

    async def memory_versions_list(
        *, workspace: Workspace, request: AdmittedRequest
    ) -> Page[MemoryVersionView]:
        _authorize_workspace(workspace, request)
        return await _run_blocking(
            lambda: _versions_sync(dependencies, workspace, request)
        )

    async def memory_at_time_get(
        *, workspace: Workspace, request: AdmittedRequest
    ) -> MemoryAtTimeData:
        _authorize_workspace(workspace, request)
        return await _run_blocking(
            lambda: _at_time_sync(dependencies, workspace, request)
        )

    async def projection_rebuild(
        *, workspace: Workspace, request: AdmittedRequest
    ) -> ProjectionRebuildData:
        _authorize_workspace(workspace, request)
        return await _run_mutation(
            lambda cancelled: _projection_sync(
                dependencies,
                workspace,
                request,
                cancelled,
            )
        )

    async def workspace_export(
        *, workspace: Workspace, request: AdmittedRequest
    ) -> ExportBundle:
        _authorize_workspace(workspace, request)
        return await _run_mutation(
            lambda cancelled: _export_sync(dependencies, workspace, request, cancelled)
        )

    async def workspace_import(
        *, workspace: Workspace, request: AdmittedRequest
    ) -> WorkspaceImportData:
        _authorize_workspace(workspace, request)
        return await _run_mutation(
            lambda cancelled: _import_sync(
                dependencies,
                workspace,
                request,
                cancelled,
            )
        )

    return MappingProxyType(
        {
            "covenant_status": covenant_status,
            "memory_at_time_get": memory_at_time_get,
            "memory_versions_list": memory_versions_list,
            "projection_rebuild": projection_rebuild,
            "workspace_export": workspace_export,
            "workspace_import": workspace_import,
        }
    )


__all__ = [
    "CoreOperationDependencies",
    "CoreOperationError",
    "build_core_operations",
]
