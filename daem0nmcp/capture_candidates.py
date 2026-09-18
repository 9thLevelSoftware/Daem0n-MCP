"""Durable, review-only capture candidates and atomic canonical promotion."""

from __future__ import annotations

import asyncio
import hmac
import json
import re
import sqlite3
import threading
from _thread import LockType
from collections.abc import Callable, Mapping, Sequence
from contextlib import nullcontext
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal, cast, get_args

from .api.v7.models import (
    RecordSummary,
    RecordType,
    contains_absolute_filesystem_path,
)
from .api.v7.runtime_services import WorkspaceStorageResolver
from .bounded_workers import BoundedWorkerBusyError, BoundedWorkerPool
from .event_store import (
    EventCommand,
    EventStore,
    EventStreamConflict,
    canonical_json_bytes,
    deterministic_id,
    sha256_json,
)
from .schema_version import CURRENT_SCHEMA_VERSION
from .workspace import Workspace

CaptureSourceKind = Literal["native_edit", "tool_result", "dreaming", "system"]

_CAPTURE_WORKERS = BoundedWorkerPool(
    max_workers=2,
    thread_name_prefix="daem0nmcp-v7-capture",
)
_IDEMPOTENCY_KEY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._~-]{7,127}$")
_RECORD_TYPES = frozenset(get_args(RecordType))
_DISALLOWED_PROVENANCE_KEYS = frozenset(
    {
        "conversation",
        "messages",
        "prompt",
        "raw",
        "raw_input",
        "raw_output",
        "raw_transcript",
        "response_body",
        "transcript",
    }
)
_MAX_CANDIDATE_RECORD_BYTES = 16_384
_MAX_PROVENANCE_BYTES = 16_384
_MAX_CAPTURE_ITEMS = 100


class CaptureCandidateError(RuntimeError):
    """Stable operation failure without storage or content disclosure."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class CaptureCandidateRequest:
    """Bounded candidate proposal accepted only from trusted producers."""

    source_kind: CaptureSourceKind
    record: Mapping[str, Any]
    provenance: Mapping[str, Any]
    idempotency_key: str

    def __post_init__(self) -> None:
        if self.source_kind not in {
            "native_edit",
            "tool_result",
            "dreaming",
            "system",
        }:
            raise ValueError("capture source kind is invalid")
        if not _IDEMPOTENCY_KEY.fullmatch(self.idempotency_key):
            raise ValueError("capture idempotency key is invalid")
        record = _validated_record(self.record)
        provenance = _validated_provenance(self.provenance)
        object.__setattr__(self, "record", record)
        object.__setattr__(self, "provenance", provenance)


@dataclass(frozen=True, slots=True)
class CaptureCandidate:
    candidate_id: str
    workspace_id: str
    source_kind: CaptureSourceKind
    record: Mapping[str, Any]
    provenance: Mapping[str, Any]
    status: Literal["pending", "promoted"]
    created_at: datetime
    promoted_event_id: str | None = None


@dataclass(frozen=True, slots=True)
class CapturePromotion:
    candidate: CaptureCandidate
    record: RecordSummary
    event_id: str
    idempotent_replay: bool


def _json_object(value: Mapping[str, Any], *, max_bytes: int) -> dict[str, Any]:
    try:
        copied = dict(value)
        encoded = canonical_json_bytes(copied)
    except (TypeError, ValueError, RecursionError):
        raise ValueError("capture value is not canonical JSON") from None
    if len(encoded) > max_bytes:
        raise ValueError("capture value exceeds its byte limit")
    decoded = json.loads(encoded)
    if not isinstance(decoded, dict):
        raise ValueError("capture value must be an object")
    return decoded


def _validated_record(value: Mapping[str, Any]) -> dict[str, Any]:
    record = _json_object(value, max_bytes=_MAX_CANDIDATE_RECORD_BYTES)
    if set(record) - {"record_type", "content", "rationale", "context", "tags"}:
        raise ValueError("capture record contains unsupported fields")
    if record.get("record_type") not in _RECORD_TYPES:
        raise ValueError("capture record type is invalid")
    content = record.get("content")
    if not isinstance(content, str) or not 1 <= len(content) <= 8_000:
        raise ValueError("capture content is invalid")
    rationale = record.get("rationale")
    if rationale is not None and (
        not isinstance(rationale, str) or not 1 <= len(rationale) <= 2_000
    ):
        raise ValueError("capture rationale is invalid")
    context = record.get("context", {})
    tags = record.get("tags", [])
    if not isinstance(context, dict) or not isinstance(tags, list):
        raise ValueError("capture record metadata is invalid")
    if len(tags) > 16 or any(
        not isinstance(tag, str) or not 1 <= len(tag) <= 80 for tag in tags
    ):
        raise ValueError("capture tags are invalid")
    if len(tags) != len(set(tags)):
        raise ValueError("capture tags must be unique")
    if contains_absolute_filesystem_path(record):
        raise ValueError("capture record contains a public absolute path")
    return {
        "record_type": record["record_type"],
        "content": content,
        "rationale": rationale,
        "context": context,
        "tags": tags,
    }


def _walk_keys(value: object) -> Sequence[str]:
    keys: list[str] = []
    if isinstance(value, dict):
        for key, item in value.items():
            keys.append(str(key).casefold())
            keys.extend(_walk_keys(item))
    elif isinstance(value, list):
        for item in value:
            keys.extend(_walk_keys(item))
    return keys


def _validated_provenance(value: Mapping[str, Any]) -> dict[str, Any]:
    provenance = _json_object(value, max_bytes=_MAX_PROVENANCE_BYTES)
    if _DISALLOWED_PROVENANCE_KEYS & set(_walk_keys(provenance)):
        raise ValueError("raw conversation data is forbidden in capture provenance")
    if contains_absolute_filesystem_path(provenance):
        raise ValueError("capture provenance contains a public absolute path")
    return provenance


def _datetime_us(value: datetime) -> int:
    if value.tzinfo is None:
        raise ValueError("capture clock must be timezone aware")
    return int(value.astimezone(timezone.utc).timestamp() * 1_000_000)


def _datetime_from_us(value: int) -> datetime:
    return datetime.fromtimestamp(value / 1_000_000, tz=timezone.utc)


def _open_database(path: Path, *, writable: bool) -> sqlite3.Connection:
    connection: sqlite3.Connection | None = None
    try:
        connection = sqlite3.connect(
            f"{path.resolve().as_uri()}?mode={'rw' if writable else 'ro'}",
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
                "('memory_capture_candidates','memory_events','memory_records')"
            )
        }
        if (
            version is None
            or int(version[0]) < CURRENT_SCHEMA_VERSION
            or tables
            != {
                "memory_capture_candidates",
                "memory_events",
                "memory_records",
            }
        ):
            raise CaptureCandidateError("CAPABILITY_DEGRADED")
        return connection
    except CaptureCandidateError:
        if connection is not None:
            connection.close()
        raise
    except Exception:
        if connection is not None:
            connection.close()
        raise CaptureCandidateError("CAPABILITY_DEGRADED") from None


def _candidate_from_row(row: sqlite3.Row) -> CaptureCandidate:
    try:
        record = json.loads(str(row["proposed_record_json"]))
        provenance = json.loads(str(row["provenance_json"]))
        return CaptureCandidate(
            candidate_id=str(row["candidate_id"]),
            workspace_id=str(row["workspace_id"]),
            source_kind=str(row["source_kind"]),  # type: ignore[arg-type]
            record=_validated_record(record),
            provenance=_validated_provenance(provenance),
            status=str(row["status"]),  # type: ignore[arg-type]
            created_at=_datetime_from_us(int(row["created_at_us"])),
            promoted_event_id=(
                None
                if row["promoted_event_id"] is None
                else str(row["promoted_event_id"])
            ),
        )
    except CaptureCandidateError:
        raise
    except Exception:
        raise CaptureCandidateError("CAPABILITY_DEGRADED") from None


def _record_state(record: Mapping[str, Any]) -> dict[str, Any]:
    validated = _validated_record(record)
    return {
        "record_type": validated["record_type"],
        "legacy_type": None,
        "content": validated["content"],
        "rationale": validated["rationale"],
        "context": validated["context"],
        "tags": validated["tags"],
        "file_path": None,
        "file_path_relative": None,
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


def _summary(
    connection: sqlite3.Connection, workspace_id: str, record_id: str
) -> RecordSummary:
    row = connection.execute(
        "SELECT record_id,record_type,content,tags_json,content_hash,created_at_us,"
        "updated_at_us FROM memory_records WHERE workspace_id=? AND record_id=?",
        (workspace_id, record_id),
    ).fetchone()
    if row is None:
        raise CaptureCandidateError("CAPABILITY_DEGRADED")
    try:
        record_type = str(row["record_type"])
        if record_type not in _RECORD_TYPES:
            raise ValueError("stored record type is invalid")
        return RecordSummary(
            record_id=str(row["record_id"]),
            record_type=cast(RecordType, record_type),
            excerpt=str(row["content"])[:4000],
            tags=json.loads(str(row["tags_json"])),
            relative_file_path=None,
            current_status="current",
            content_hash=str(row["content_hash"]),
            created_at=_datetime_from_us(int(row["created_at_us"])),
            updated_at=_datetime_from_us(int(row["updated_at_us"])),
        )
    except Exception:
        raise CaptureCandidateError("CAPABILITY_DEGRADED") from None


@dataclass(frozen=True, slots=True)
class CaptureCandidateStore:
    """Workspace-scoped candidate authority reusable by adapters and P7."""

    storage_resolver: WorkspaceStorageResolver = field(
        default_factory=WorkspaceStorageResolver
    )
    clock: Callable[[], datetime] = field(
        default_factory=lambda: lambda: datetime.now(timezone.utc)
    )
    projection_scheduler: Callable[[Path], object] | None = None

    def _stage_sync(
        self,
        workspace: Workspace,
        request: CaptureCandidateRequest,
        *,
        source_versions: Sequence[tuple[str, str]] = (),
        cancelled: threading.Event | None = None,
        publication_lock: LockType | None = None,
    ) -> CaptureCandidate:
        now_us = _datetime_us(self.clock())
        identity = {
            "source_kind": request.source_kind,
            "record": request.record,
            "provenance": request.provenance,
        }
        candidate_hash = sha256_json(identity)
        candidate_id = "cap_" + sha256_json(
            [
                "daem0nmcp",
                "v7",
                "capture-candidate",
                workspace.workspace_id,
                candidate_hash,
            ]
        )
        with self.storage_resolver.locked_active(workspace) as active:
            connection = _open_database(active.path, writable=True)
            try:
                connection.execute("BEGIN IMMEDIATE")
                if cancelled is not None and cancelled.is_set():
                    raise CaptureCandidateError("CAPTURE_CANCELLED")
                for record_id, source_event_id in source_versions:
                    row = connection.execute(
                        "SELECT source_event_id FROM memory_records WHERE "
                        "workspace_id=? AND record_id=? AND archived=0 "
                        "AND deleted_at_us IS NULL",
                        (workspace.workspace_id, record_id),
                    ).fetchone()
                    if row is None or not hmac.compare_digest(
                        str(row[0]), source_event_id
                    ):
                        raise CaptureCandidateError("CAPTURE_SOURCE_STALE")
                if cancelled is not None and cancelled.is_set():
                    raise CaptureCandidateError("CAPTURE_CANCELLED")
                existing = connection.execute(
                    "SELECT * FROM memory_capture_candidates WHERE workspace_id=? "
                    "AND idempotency_key=?",
                    (workspace.workspace_id, request.idempotency_key),
                ).fetchone()
                if existing is None:
                    connection.execute(
                        "INSERT INTO memory_capture_candidates(candidate_id,workspace_id,"
                        "idempotency_key,candidate_hash,source_kind,proposed_record_json,"
                        "provenance_json,status,created_at_us) VALUES (?,?,?,?,?,?,?,?,?)",
                        (
                            candidate_id,
                            workspace.workspace_id,
                            request.idempotency_key,
                            candidate_hash,
                            request.source_kind,
                            canonical_json_bytes(dict(request.record)).decode("utf-8"),
                            canonical_json_bytes(dict(request.provenance)).decode(
                                "utf-8"
                            ),
                            "pending",
                            now_us,
                        ),
                    )
                    existing = connection.execute(
                        "SELECT * FROM memory_capture_candidates WHERE candidate_id=?",
                        (candidate_id,),
                    ).fetchone()
                elif not hmac.compare_digest(
                    str(existing["candidate_hash"]), candidate_hash
                ):
                    raise CaptureCandidateError("IDEMPOTENCY_CONFLICT")
                with publication_lock or nullcontext():
                    if cancelled is not None and cancelled.is_set():
                        raise CaptureCandidateError("CAPTURE_CANCELLED")
                    connection.commit()
                if existing is None:
                    raise CaptureCandidateError("CAPABILITY_DEGRADED")
                return _candidate_from_row(existing)
            except CaptureCandidateError:
                if connection.in_transaction:
                    connection.rollback()
                raise
            except Exception:
                if connection.in_transaction:
                    connection.rollback()
                raise CaptureCandidateError("CAPABILITY_DEGRADED") from None
            finally:
                connection.close()

    async def stage(
        self, workspace: Workspace, request: CaptureCandidateRequest
    ) -> CaptureCandidate:
        try:
            return await _CAPTURE_WORKERS.run(
                lambda: self._stage_sync(workspace, request)
            )
        except BoundedWorkerBusyError as exc:
            raise CaptureCandidateError("TASK_REQUIRED") from exc

    async def stage_validated(
        self,
        workspace: Workspace,
        request: CaptureCandidateRequest,
        *,
        source_versions: Sequence[tuple[str, str]],
        cancelled: threading.Event | None = None,
        publication_lock: LockType | None = None,
    ) -> CaptureCandidate:
        """Stage only while exact authoritative source events remain current."""

        try:
            return await _CAPTURE_WORKERS.run(
                lambda: self._stage_sync(
                    workspace,
                    request,
                    source_versions=tuple(source_versions),
                    cancelled=cancelled,
                    publication_lock=publication_lock,
                )
            )
        except BoundedWorkerBusyError as exc:
            raise CaptureCandidateError("TASK_REQUIRED") from exc

    def _list_sync(
        self,
        workspace: Workspace,
        *,
        limit: int,
        before: tuple[int, str] | None,
    ) -> tuple[list[CaptureCandidate], tuple[int, str] | None]:
        if not 1 <= limit <= _MAX_CAPTURE_ITEMS:
            raise CaptureCandidateError("INVALID_ARGUMENT")
        with self.storage_resolver.locked_active(workspace) as active:
            connection = _open_database(active.path, writable=False)
            try:
                arguments: list[object] = [workspace.workspace_id]
                boundary = ""
                if before is not None:
                    boundary = (
                        "AND (created_at_us<? OR (created_at_us=? AND candidate_id<?)) "
                    )
                    arguments.extend([before[0], before[0], before[1]])
                arguments.append(limit + 1)
                rows = connection.execute(
                    "SELECT * FROM memory_capture_candidates WHERE workspace_id=? "
                    "AND status='pending' "
                    + boundary
                    + "ORDER BY created_at_us DESC,candidate_id DESC LIMIT ?",
                    tuple(arguments),
                ).fetchall()
                selected = rows[:limit]
                next_boundary = (
                    None
                    if len(rows) <= limit
                    else (
                        int(selected[-1]["created_at_us"]),
                        str(selected[-1]["candidate_id"]),
                    )
                )
                return [_candidate_from_row(row) for row in selected], next_boundary
            finally:
                connection.close()

    async def list_pending(
        self,
        workspace: Workspace,
        *,
        limit: int,
        before: tuple[int, str] | None,
    ) -> tuple[list[CaptureCandidate], tuple[int, str] | None]:
        try:
            return await _CAPTURE_WORKERS.run(
                lambda: self._list_sync(workspace, limit=limit, before=before)
            )
        except BoundedWorkerBusyError as exc:
            raise CaptureCandidateError("TASK_REQUIRED") from exc

    def _promote_sync(
        self,
        workspace: Workspace,
        *,
        candidate_id: str,
        record: Mapping[str, Any],
        idempotency_key: str,
    ) -> tuple[CapturePromotion, Path, bool]:
        if not _IDEMPOTENCY_KEY.fullmatch(idempotency_key):
            raise CaptureCandidateError("INVALID_ARGUMENT")
        state = _record_state(record)
        request_hash = sha256_json(
            {
                "candidate_id": candidate_id,
                "record": _validated_record(record),
                "idempotency_key": idempotency_key,
            }
        )
        now_us = _datetime_us(self.clock())
        record_id = deterministic_id(
            "mem", "capture-promotion", workspace.workspace_id, idempotency_key
        )
        correlation_id = deterministic_id(
            "job", "capture-promotion", workspace.workspace_id, idempotency_key
        )
        with self.storage_resolver.locked_active(workspace) as active:
            connection = _open_database(active.path, writable=True)
            try:
                connection.execute("BEGIN IMMEDIATE")
                row = connection.execute(
                    "SELECT * FROM memory_capture_candidates WHERE workspace_id=? "
                    "AND candidate_id=?",
                    (workspace.workspace_id, candidate_id),
                ).fetchone()
                if row is None:
                    raise CaptureCandidateError("NOT_FOUND")
                candidate = _candidate_from_row(row)
                if candidate.status == "promoted":
                    if (
                        str(row["promotion_idempotency_key"]) != idempotency_key
                        or str(row["promotion_request_hash"]) != request_hash
                        or candidate.promoted_event_id is None
                    ):
                        raise CaptureCandidateError("IDEMPOTENCY_CONFLICT")
                    summary = _summary(connection, workspace.workspace_id, record_id)
                    connection.commit()
                    return (
                        CapturePromotion(
                            candidate=candidate,
                            record=summary,
                            event_id=candidate.promoted_event_id,
                            idempotent_replay=True,
                        ),
                        active.path,
                        False,
                    )
                collision = connection.execute(
                    "SELECT candidate_id,promotion_request_hash FROM "
                    "memory_capture_candidates WHERE workspace_id=? AND "
                    "promotion_idempotency_key=?",
                    (workspace.workspace_id, idempotency_key),
                ).fetchone()
                if collision is not None:
                    raise CaptureCandidateError("IDEMPOTENCY_CONFLICT")
                event = EventStore(
                    connection, assume_transaction=True
                ).append_and_project(
                    EventCommand(
                        workspace_id=workspace.workspace_id,
                        stream_id=record_id,
                        stream_kind="memory",
                        event_type="memory.created",
                        occurred_at_us=now_us,
                        recorded_at_us=now_us,
                        actor_type="client",
                        payload={
                            "record": state,
                            "capture_candidate_id": candidate_id,
                            "idempotency_request_hash": request_hash,
                        },
                        correlation_id=correlation_id,
                        expected_stream_version=1,
                    )
                )
                updated = connection.execute(
                    "UPDATE memory_capture_candidates SET status='promoted',"
                    "promoted_at_us=?,promoted_event_id=?,promotion_idempotency_key=?,"
                    "promotion_request_hash=? WHERE workspace_id=? AND candidate_id=? "
                    "AND status='pending'",
                    (
                        now_us,
                        event.event_id,
                        idempotency_key,
                        request_hash,
                        workspace.workspace_id,
                        candidate_id,
                    ),
                )
                if updated.rowcount != 1:
                    raise CaptureCandidateError("CONFLICT")
                promoted_row = connection.execute(
                    "SELECT * FROM memory_capture_candidates WHERE candidate_id=?",
                    (candidate_id,),
                ).fetchone()
                if promoted_row is None:
                    raise CaptureCandidateError("CAPABILITY_DEGRADED")
                summary = _summary(connection, workspace.workspace_id, record_id)
                connection.commit()
                return (
                    CapturePromotion(
                        candidate=_candidate_from_row(promoted_row),
                        record=summary,
                        event_id=event.event_id,
                        idempotent_replay=False,
                    ),
                    active.path,
                    True,
                )
            except CaptureCandidateError:
                if connection.in_transaction:
                    connection.rollback()
                raise
            except EventStreamConflict:
                if connection.in_transaction:
                    connection.rollback()
                raise CaptureCandidateError("IDEMPOTENCY_CONFLICT") from None
            except Exception:
                if connection.in_transaction:
                    connection.rollback()
                raise CaptureCandidateError("CAPABILITY_DEGRADED") from None
            finally:
                connection.close()

    async def promote(
        self,
        workspace: Workspace,
        *,
        candidate_id: str,
        record: Mapping[str, Any],
        idempotency_key: str,
    ) -> CapturePromotion:
        try:
            result, path, changed = await _CAPTURE_WORKERS.run(
                lambda: self._promote_sync(
                    workspace,
                    candidate_id=candidate_id,
                    record=record,
                    idempotency_key=idempotency_key,
                )
            )
        except BoundedWorkerBusyError as exc:
            raise CaptureCandidateError("TASK_REQUIRED") from exc
        if changed and self.projection_scheduler is not None:
            try:
                self.projection_scheduler(path)
            except asyncio.CancelledError:
                raise
            except Exception:
                pass
        return result


__all__ = [
    "CaptureCandidate",
    "CaptureCandidateError",
    "CaptureCandidateRequest",
    "CaptureCandidateStore",
    "CapturePromotion",
    "CaptureSourceKind",
]
