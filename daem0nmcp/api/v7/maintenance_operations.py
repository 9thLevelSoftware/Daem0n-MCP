"""Canonical v7 maintenance previews and append-only mutations."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import os
import secrets
import sqlite3
import threading
from collections.abc import Callable, Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import MappingProxyType
from typing import Any

from ...bounded_workers import BoundedWorkerBusyError, BoundedWorkerPool
from ...event_store import (
    EventCommand,
    EventStore,
    EventStreamConflict,
    canonical_json_bytes,
    deterministic_id,
    memory_content_hash,
    memory_state_hash,
    sha256_json,
)
from ...schema_version import CURRENT_SCHEMA_VERSION
from ...storage_activation import ResolvedActiveDatabase
from ...workspace import Workspace, WorkspaceRegistry
from .application import AdmittedRequest
from .errors import STABLE_ERROR_CODE_SET
from .models import (
    DestructiveMutationReceipt,
    Preview,
    RecordSummary,
    stored_relative_path,
)
from .runtime_protocols import ActiveStorageResolver, WorkerPool
from .runtime_services import WorkspaceStorageResolver
from .tasks import await_task_terminal
from .tools import MemoryCompactData

_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
_MAX_SELECTION = 500
_MAX_GROUP_SCAN = 2_000
_REQUIRED_TABLES = frozenset({"memory_events", "memory_records", "schema_version"})
_RECORD_COLUMNS = (
    "record.record_id,record.workspace_id,record.record_type,"
    "record.legacy_type,record.content,record.content_hash,record.rationale,"
    "record.context_json,record.tags_json,record.file_path,"
    "record.file_path_relative,record.keywords,record.is_permanent,"
    "record.pinned,record.archived,record.outcome,record.worked,"
    "record.recall_count,record.surprise_score,record.importance_score,"
    "record.source_client,record.source_model,record.stream_version,"
    "record.source_event_id,record.created_at_us,record.updated_at_us,"
    "record.deleted_at_us,record.state_hash,event.payload_json,"
    "event.payload_hash,event.event_type,event.stream_id AS event_stream_id,"
    "event.workspace_id AS event_workspace_id"
)


class MaintenanceOperationError(RuntimeError):
    """Stable, path-free failure understood by the shared v7 router."""

    def __init__(self, code: str) -> None:
        if code not in STABLE_ERROR_CODE_SET:
            raise ValueError("maintenance operation error code is not stable")
        self.code = code
        super().__init__(code)


class _WorkerCancelledError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class _RecordSnapshot:
    record_id: str
    state_hash: str
    content_hash: str
    source_event_id: str
    stream_version: int
    created_at_us: int
    updated_at_us: int
    state: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class _SelectionClaims:
    workspace_id: str
    target_tool: str
    criteria_hash: str
    selection_hash: str
    selected_count: int
    source_high_water: str
    evaluated_at_us: int
    expires_at_us: int


@dataclass(frozen=True, slots=True)
class _DuplicateGroup:
    keeper: _RecordSnapshot
    candidates: tuple[_RecordSnapshot, ...]


def _default_clock() -> datetime:
    return datetime.now(timezone.utc)


def _default_worker_pool() -> BoundedWorkerPool:
    return BoundedWorkerPool(
        max_workers=4,
        thread_name_prefix="daem0nmcp-v7-maintenance",
    )


@dataclass(frozen=True, slots=True)
class MaintenanceOperationDependencies:
    """Owned dependencies for canonical maintenance operations."""

    storage_resolver: ActiveStorageResolver = field(
        default_factory=WorkspaceStorageResolver
    )
    clock: Callable[[], datetime] = field(default=_default_clock)
    selection_secret: bytes = field(default_factory=lambda: secrets.token_bytes(32))
    selection_ttl_seconds: int = 300
    worker_pool: WorkerPool = field(default_factory=_default_worker_pool)

    def __post_init__(self) -> None:
        if not callable(getattr(self.storage_resolver, "locked_active", None)):
            raise TypeError("storage_resolver must provide locked_active")
        if not callable(self.clock):
            raise TypeError("clock must be callable")
        if (
            not isinstance(self.selection_secret, bytes)
            or len(self.selection_secret) < 32
        ):
            raise ValueError("selection_secret must contain at least 32 bytes")
        ttl = self.selection_ttl_seconds
        if isinstance(ttl, bool) or not isinstance(ttl, int) or not 1 <= ttl <= 3600:
            raise ValueError("selection_ttl_seconds must be between 1 and 3600")
        if not callable(getattr(self.worker_pool, "run", None)) or not callable(
            getattr(self.worker_pool, "shutdown", None)
        ):
            raise TypeError("worker_pool must provide run and shutdown")

    def close(self) -> None:
        """Join and release the dependency-owned bounded worker pool."""

        self.worker_pool.shutdown()


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
        raise MaintenanceOperationError("UNAUTHORIZED_WORKSPACE")
    try:
        canonical = workspace.root.resolve(strict=True)
        registered = WorkspaceRegistry([canonical], default_root=canonical).default
        exact_root = os.path.normcase(str(workspace.root)) == os.path.normcase(
            str(canonical)
        )
    except (OSError, RuntimeError, TypeError, ValueError):
        raise MaintenanceOperationError("UNAUTHORIZED_WORKSPACE") from None
    if registered.workspace_id != workspace.workspace_id or not exact_root:
        raise MaintenanceOperationError("UNAUTHORIZED_WORKSPACE")


def _database_path(workspace: Workspace, active: ResolvedActiveDatabase) -> Path:
    try:
        root = workspace.root.resolve(strict=True)
        candidate = Path(active.path)
        if candidate.is_symlink():
            raise ValueError
        resolved = candidate.resolve(strict=True)
        resolved.relative_to(root)
        if not resolved.is_file():
            raise ValueError
        return resolved
    except (OSError, RuntimeError, TypeError, ValueError):
        raise MaintenanceOperationError("WORKSPACE_PATH_ESCAPE") from None


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
            or version[0] < CURRENT_SCHEMA_VERSION
            or not _REQUIRED_TABLES.issubset(tables)
        ):
            raise MaintenanceOperationError("CAPABILITY_DEGRADED")
        return connection
    except MaintenanceOperationError:
        if connection is not None:
            connection.close()
        raise
    except Exception:
        if connection is not None:
            connection.close()
        raise MaintenanceOperationError("CAPABILITY_DEGRADED") from None


def _datetime_us(value: datetime) -> int:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise MaintenanceOperationError("CAPABILITY_DEGRADED")
    try:
        delta = value.astimezone(timezone.utc) - _EPOCH
        result = (delta.days * 86_400 + delta.seconds) * 1_000_000 + delta.microseconds
    except (OverflowError, TypeError, ValueError):
        raise MaintenanceOperationError("CAPABILITY_DEGRADED") from None
    if not -(2**63) <= result <= 2**63 - 1:
        raise MaintenanceOperationError("CAPABILITY_DEGRADED")
    return result


def _datetime_from_us(value: int) -> datetime:
    try:
        return _EPOCH + timedelta(microseconds=value)
    except (OverflowError, TypeError, ValueError):
        raise MaintenanceOperationError("CAPABILITY_DEGRADED") from None


def _now_us(dependencies: MaintenanceOperationDependencies) -> int:
    try:
        return _datetime_us(dependencies.clock())
    except MaintenanceOperationError:
        raise
    except Exception:
        raise MaintenanceOperationError("CAPABILITY_DEGRADED") from None


def _parse_json(value: object, expected: type) -> Any:
    try:
        parsed = json.loads(str(value))
    except (TypeError, ValueError, RecursionError):
        raise MaintenanceOperationError("CAPABILITY_DEGRADED") from None
    if not isinstance(parsed, expected):
        raise MaintenanceOperationError("CAPABILITY_DEGRADED")
    return parsed


def _record_state(row: sqlite3.Row) -> dict[str, Any]:
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


def _verified_snapshot(row: sqlite3.Row) -> _RecordSnapshot:
    try:
        state = _record_state(row)
        payload = _parse_json(row["payload_json"], dict)
        if (
            row["file_path"] is not None
            or row["workspace_id"] != row["event_workspace_id"]
            or row["record_id"] != row["event_stream_id"]
            or payload.get("record") != state
            or canonical_json_bytes(payload).decode("utf-8") != str(row["payload_json"])
            or sha256_json(payload) != str(row["payload_hash"])
            or memory_content_hash(state) != str(row["content_hash"])
            or memory_state_hash(state) != str(row["state_hash"])
        ):
            raise ValueError
        return _RecordSnapshot(
            record_id=str(row["record_id"]),
            state_hash=str(row["state_hash"]),
            content_hash=str(row["content_hash"]),
            source_event_id=str(row["source_event_id"]),
            stream_version=int(row["stream_version"]),
            created_at_us=int(row["created_at_us"]),
            updated_at_us=int(row["updated_at_us"]),
            state=MappingProxyType(state),
        )
    except MaintenanceOperationError:
        raise
    except Exception:
        raise MaintenanceOperationError("CAPABILITY_DEGRADED") from None


def _source_high_water(connection: sqlite3.Connection, workspace_id: str) -> str:
    row = connection.execute(
        "SELECT count(*),COALESCE(max(recorded_at_us),0),"
        "COALESCE(max(event_id),'') FROM memory_events WHERE workspace_id=?",
        (workspace_id,),
    ).fetchone()
    if row is None:
        raise MaintenanceOperationError("CAPABILITY_DEGRADED")
    return sha256_json(
        {
            "event_count": int(row[0]),
            "max_event_id": str(row[2]),
            "max_recorded_at_us": int(row[1]),
        }
    )


def _selection_pairs(records: Sequence[_RecordSnapshot]) -> list[list[str]]:
    return [[record.record_id, record.state_hash] for record in records]


def _b64encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _b64decode(value: str) -> bytes:
    try:
        return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
    except Exception:
        raise MaintenanceOperationError("TOKEN_TAMPERED") from None


def _issue_selection_token(
    dependencies: MaintenanceOperationDependencies,
    *,
    workspace_id: str,
    target_tool: str,
    criteria: Mapping[str, Any],
    records: Sequence[_RecordSnapshot],
    source_high_water: str,
    evaluated_at_us: int,
) -> tuple[str, datetime]:
    expires_at_us = evaluated_at_us + dependencies.selection_ttl_seconds * 1_000_000
    payload = {
        "criteria_hash": sha256_json(criteria),
        "evaluated_at_us": evaluated_at_us,
        "expires_at_us": expires_at_us,
        "selected_count": len(records),
        "selection_hash": sha256_json(_selection_pairs(records)),
        "source_high_water": source_high_water,
        "target_tool": target_tool,
        "workspace_id": workspace_id,
    }
    encoded = _b64encode(canonical_json_bytes(payload))
    signature = hmac.new(
        dependencies.selection_secret,
        encoded.encode("ascii"),
        hashlib.sha256,
    ).hexdigest()
    return f"sel_v1.{encoded}.{signature}", _datetime_from_us(expires_at_us)


def _verify_selection_token(
    dependencies: MaintenanceOperationDependencies,
    token: object,
    *,
    workspace_id: str,
    target_tool: str,
    criteria: Mapping[str, Any],
) -> _SelectionClaims:
    if not isinstance(token, str):
        raise MaintenanceOperationError("TOKEN_TAMPERED")
    parts = token.split(".")
    if not token.startswith("sel_v1."):
        if token.startswith("sel_"):
            raise MaintenanceOperationError("TOKEN_LEGACY_UNSUPPORTED")
        raise MaintenanceOperationError("TOKEN_TAMPERED")
    if len(parts) != 3 or parts[0] != "sel_v1" or len(parts[2]) != 64:
        raise MaintenanceOperationError("TOKEN_TAMPERED")
    expected = hmac.new(
        dependencies.selection_secret,
        parts[1].encode("ascii"),
        hashlib.sha256,
    ).hexdigest()
    if not hmac.compare_digest(expected, parts[2]):
        raise MaintenanceOperationError("TOKEN_TAMPERED")
    try:
        raw = _b64decode(parts[1])
        payload = json.loads(raw.decode("utf-8"))
        expected_keys = {
            "criteria_hash",
            "evaluated_at_us",
            "expires_at_us",
            "selected_count",
            "selection_hash",
            "source_high_water",
            "target_tool",
            "workspace_id",
        }
        if (
            not isinstance(payload, dict)
            or set(payload) != expected_keys
            or canonical_json_bytes(payload) != raw
        ):
            raise ValueError
        claims = _SelectionClaims(
            workspace_id=payload["workspace_id"],
            target_tool=payload["target_tool"],
            criteria_hash=payload["criteria_hash"],
            selection_hash=payload["selection_hash"],
            selected_count=payload["selected_count"],
            source_high_water=payload["source_high_water"],
            evaluated_at_us=payload["evaluated_at_us"],
            expires_at_us=payload["expires_at_us"],
        )
        if (
            not all(
                isinstance(value, str) and len(value) == 64
                for value in (
                    claims.criteria_hash,
                    claims.selection_hash,
                    claims.source_high_water,
                )
            )
            or isinstance(claims.selected_count, bool)
            or not isinstance(claims.selected_count, int)
            or not 0 <= claims.selected_count <= _MAX_SELECTION
            or isinstance(claims.evaluated_at_us, bool)
            or not isinstance(claims.evaluated_at_us, int)
            or isinstance(claims.expires_at_us, bool)
            or not isinstance(claims.expires_at_us, int)
            or claims.expires_at_us <= claims.evaluated_at_us
        ):
            raise ValueError
    except MaintenanceOperationError:
        raise
    except Exception:
        raise MaintenanceOperationError("TOKEN_TAMPERED") from None
    if claims.workspace_id != workspace_id:
        raise MaintenanceOperationError("TOKEN_SCOPE_MISMATCH")
    if claims.target_tool != target_tool:
        raise MaintenanceOperationError("TOKEN_OPERATION_MISMATCH")
    if claims.criteria_hash != sha256_json(criteria):
        raise MaintenanceOperationError("TOKEN_ARGUMENT_MISMATCH")
    if _now_us(dependencies) >= claims.expires_at_us:
        raise MaintenanceOperationError("TOKEN_EXPIRED")
    return claims


def _prune_criteria(request: AdmittedRequest) -> dict[str, Any]:
    categories = (
        ["decision", "learning"]
        if request.categories is None
        else sorted(request.categories)
    )
    return {
        "categories": categories,
        "min_recall_count": request.min_recall_count,
        "older_than_days": request.older_than_days,
        "protect_successful": request.protect_successful,
    }


def _prune_candidates(
    connection: sqlite3.Connection,
    workspace_id: str,
    criteria: Mapping[str, Any],
    evaluated_at_us: int,
) -> tuple[list[_RecordSnapshot], int]:
    cutoff_us = evaluated_at_us - int(criteria["older_than_days"]) * 86_400_000_000
    categories = list(criteria["categories"])
    placeholders = ",".join("?" for _ in categories)
    where = (
        "record.workspace_id=? AND record.record_type IN ("
        + placeholders
        + ") AND record.created_at_us<? AND record.deleted_at_us IS NULL "
        "AND record.is_permanent=0 AND record.pinned=0 AND record.archived=0 "
        "AND record.outcome IS NULL AND record.recall_count<?"
    )
    parameters: list[Any] = [
        workspace_id,
        *categories,
        cutoff_us,
        criteria["min_recall_count"],
    ]
    if criteria["protect_successful"]:
        where += " AND (record.worked IS NULL OR record.worked=0)"
    total_row = connection.execute(
        f"SELECT count(*) FROM memory_records AS record WHERE {where}",
        parameters,
    ).fetchone()
    if total_row is None:
        raise MaintenanceOperationError("CAPABILITY_DEGRADED")
    rows = connection.execute(
        f"SELECT {_RECORD_COLUMNS} FROM memory_records AS record "
        "JOIN memory_events AS event ON event.event_id=record.source_event_id "
        f"WHERE {where} ORDER BY record.created_at_us,record.record_id LIMIT ?",
        (*parameters, _MAX_SELECTION),
    ).fetchall()
    return [_verified_snapshot(row) for row in rows], int(total_row[0])


def _duplicate_criteria(request: AdmittedRequest) -> dict[str, Any]:
    return {"merge_duplicates": request.merge_duplicates}


def _normalized_duplicate_content(value: object) -> str:
    if not isinstance(value, str):
        raise MaintenanceOperationError("CAPABILITY_DEGRADED")
    return " ".join(value.casefold().split())


def _duplicate_groups(
    connection: sqlite3.Connection,
    workspace_id: str,
    criteria: Mapping[str, Any],
) -> tuple[list[_DuplicateGroup], int, int]:
    rows = connection.execute(
        f"SELECT {_RECORD_COLUMNS} FROM memory_records AS record "
        "JOIN memory_events AS event ON event.event_id=record.source_event_id "
        "WHERE record.workspace_id=? AND record.deleted_at_us IS NULL "
        "ORDER BY record.record_id LIMIT ?",
        (workspace_id, _MAX_GROUP_SCAN + 1),
    ).fetchall()
    if len(rows) > _MAX_GROUP_SCAN:
        raise MaintenanceOperationError("TASK_REQUIRED")
    grouped: dict[tuple[str, str, str], list[_RecordSnapshot]] = {}
    for row in rows:
        snapshot = _verified_snapshot(row)
        key = (
            str(snapshot.state["record_type"]),
            _normalized_duplicate_content(snapshot.state["content"]),
            ""
            if snapshot.state["file_path_relative"] is None
            else str(snapshot.state["file_path_relative"]),
        )
        grouped.setdefault(key, []).append(snapshot)
    duplicate_items = [
        (key, members) for key, members in grouped.items() if len(members) > 1
    ]
    duplicate_items.sort(key=lambda item: item[0])
    eligible = sum(len(members) - 1 for _key, members in duplicate_items)
    if not criteria["merge_duplicates"]:
        return [], eligible, len(duplicate_items)
    selected: list[_DuplicateGroup] = []
    event_capacity = _MAX_SELECTION
    for _key, members in duplicate_items:
        keeper = max(
            members,
            key=lambda record: (record.created_at_us, record.record_id),
        )
        candidates = sorted(
            (record for record in members if record.record_id != keeper.record_id),
            key=lambda record: (record.created_at_us, record.record_id),
        )
        take = min(len(candidates), max(0, event_capacity - 1))
        if take == 0:
            break
        chosen = tuple(candidates[:take])
        selected.append(_DuplicateGroup(keeper=keeper, candidates=chosen))
        event_capacity -= 1 + len(chosen)
    return selected, eligible, len(duplicate_items)


def _duplicate_candidates(
    groups: Sequence[_DuplicateGroup],
) -> list[_RecordSnapshot]:
    return [candidate for group in groups for candidate in group.candidates]


def _compaction_criteria(request: AdmittedRequest) -> dict[str, Any]:
    return {
        "limit": request.limit,
        "query": request.query,
        "summary": request.summary,
    }


def _compaction_candidates(
    connection: sqlite3.Connection,
    workspace_id: str,
    criteria: Mapping[str, Any],
) -> tuple[list[_RecordSnapshot], int]:
    rows = connection.execute(
        f"SELECT {_RECORD_COLUMNS} FROM memory_records AS record "
        "JOIN memory_events AS event ON event.event_id=record.source_event_id "
        "WHERE record.workspace_id=? AND record.deleted_at_us IS NULL "
        "AND record.archived=0 AND record.pinned=0 AND record.is_permanent=0 "
        "AND record.record_type IN ('decision','learning') "
        "ORDER BY record.created_at_us,record.record_id LIMIT ?",
        (workspace_id, _MAX_GROUP_SCAN + 1),
    ).fetchall()
    if len(rows) > _MAX_GROUP_SCAN:
        raise MaintenanceOperationError("TASK_REQUIRED")
    query = criteria["query"]
    query_text = None if query is None else str(query).casefold()
    eligible: list[_RecordSnapshot] = []
    for row in rows:
        record = _verified_snapshot(row)
        if (
            record.state["record_type"] == "decision"
            and record.state["outcome"] is None
            and record.state["worked"] is None
        ):
            continue
        tags = record.state["tags"]
        if not isinstance(tags, list) or not all(isinstance(tag, str) for tag in tags):
            raise MaintenanceOperationError("CAPABILITY_DEGRADED")
        if query_text is not None:
            searchable = [
                str(record.state["content"]),
                (
                    ""
                    if record.state["rationale"] is None
                    else str(record.state["rationale"])
                ),
                *tags,
            ]
            if not any(query_text in value.casefold() for value in searchable):
                continue
        eligible.append(record)
    return eligible[: int(criteria["limit"])], len(eligible)


def _record_summary(record: _RecordSnapshot) -> RecordSummary:
    content = record.state["content"]
    tags = record.state["tags"]
    if not isinstance(content, str) or not content or not isinstance(tags, list):
        raise MaintenanceOperationError("CAPABILITY_DEGRADED")
    status = (
        "invalidated"
        if record.state["deleted_at_us"] is not None
        else ("archived" if record.state["archived"] else "current")
    )
    created_at = _datetime_from_us(record.created_at_us)
    updated_at = _datetime_from_us(record.updated_at_us)
    if created_at > updated_at:
        created_at = updated_at
    try:
        return RecordSummary.model_validate(
            {
                "record_id": record.record_id,
                "record_type": record.state["record_type"],
                "excerpt": content[:4000],
                "tags": tags,
                "relative_file_path": stored_relative_path(
                    record.state["file_path_relative"]
                ),
                "current_status": status,
                "content_hash": record.content_hash,
                "created_at": created_at,
                "updated_at": updated_at,
            }
        )
    except Exception:
        raise MaintenanceOperationError("CAPABILITY_DEGRADED") from None


def _load_snapshot(
    connection: sqlite3.Connection,
    workspace_id: str,
    record_id: str,
) -> _RecordSnapshot:
    rows = connection.execute(
        f"SELECT {_RECORD_COLUMNS} FROM memory_records AS record "
        "JOIN memory_events AS event ON event.event_id=record.source_event_id "
        "WHERE record.workspace_id=? AND record.record_id=? LIMIT 2",
        (workspace_id, record_id),
    ).fetchall()
    if len(rows) != 1:
        raise MaintenanceOperationError(
            "NOT_FOUND" if not rows else "CAPABILITY_DEGRADED"
        )
    return _verified_snapshot(rows[0])


def _dream_criteria(_request: AdmittedRequest) -> dict[str, Any]:
    return {}


def _dream_candidates(
    connection: sqlite3.Connection,
    workspace_id: str,
) -> tuple[list[_RecordSnapshot], int, int, int]:
    rows = connection.execute(
        f"SELECT {_RECORD_COLUMNS} FROM memory_records AS record "
        "JOIN memory_events AS event ON event.event_id=record.source_event_id "
        "WHERE record.workspace_id=? AND record.record_type='learning' "
        "AND record.deleted_at_us IS NULL ORDER BY record.record_id LIMIT ?",
        (workspace_id, _MAX_GROUP_SCAN + 1),
    ).fetchall()
    if len(rows) > _MAX_GROUP_SCAN:
        raise MaintenanceOperationError("TASK_REQUIRED")
    grouped: dict[tuple[str, str], list[_RecordSnapshot]] = {}
    for row in rows:
        record = _verified_snapshot(row)
        tags = record.state["tags"]
        if not isinstance(tags, list) or not all(isinstance(tag, str) for tag in tags):
            raise MaintenanceOperationError("CAPABILITY_DEGRADED")
        tag_set = set(tags)
        if "dream" not in tag_set:
            continue
        group_key: tuple[str, str] | None = None
        if "re-evaluation" in tag_set:
            source_tags = sorted(
                tag for tag in tag_set if tag.startswith("source-decision:")
            )
            if len(source_tags) > 1:
                raise MaintenanceOperationError("CAPABILITY_DEGRADED")
            if source_tags and source_tags[0].split(":", 1)[1]:
                group_key = ("reevaluation", source_tags[0].split(":", 1)[1])
        elif "dream-summary" in tag_set:
            day = _datetime_from_us(record.created_at_us).date().isoformat()
            group_key = ("summary", day)
        if group_key is not None:
            grouped.setdefault(group_key, []).append(record)
    candidates: list[_RecordSnapshot] = []
    reevaluation_duplicates = 0
    summary_duplicates = 0
    for key in sorted(grouped):
        members = grouped[key]
        if len(members) <= 1:
            continue
        keeper = max(
            members,
            key=lambda record: (record.created_at_us, record.record_id),
        )
        duplicates = sorted(
            (record for record in members if record.record_id != keeper.record_id),
            key=lambda record: (record.created_at_us, record.record_id),
        )
        candidates.extend(duplicates)
        if key[0] == "reevaluation":
            reevaluation_duplicates += len(duplicates)
        else:
            summary_duplicates += len(duplicates)
    return (
        candidates[:_MAX_SELECTION],
        len(candidates),
        reevaluation_duplicates,
        summary_duplicates,
    )


def _selection_correlation(workspace_id: str, claims: _SelectionClaims) -> str:
    return deterministic_id(
        "job",
        "maintenance-selection",
        workspace_id,
        claims.target_tool,
        claims.criteria_hash,
        claims.selection_hash,
    )


def _operation_id(workspace_id: str, claims: _SelectionClaims) -> str:
    return "op_" + sha256_json(
        [
            "daem0nmcp",
            "v7",
            "maintenance-operation",
            workspace_id,
            claims.target_tool,
            claims.criteria_hash,
            claims.selection_hash,
        ]
    )


def _verified_payload(row: sqlite3.Row) -> dict[str, Any]:
    try:
        payload = json.loads(str(row["payload_json"]))
        if (
            not isinstance(payload, dict)
            or canonical_json_bytes(payload).decode("utf-8") != str(row["payload_json"])
            or sha256_json(payload) != str(row["payload_hash"])
        ):
            raise ValueError
        return payload
    except Exception:
        raise MaintenanceOperationError("CAPABILITY_DEGRADED") from None


def _prune_replay(
    connection: sqlite3.Connection,
    workspace_id: str,
    claims: _SelectionClaims,
    correlation_id: str,
) -> DestructiveMutationReceipt | None:
    rows = connection.execute(
        "SELECT event_id,stream_id,event_type,payload_json,payload_hash "
        "FROM memory_events WHERE workspace_id=? AND correlation_id=? "
        "ORDER BY rowid",
        (workspace_id, correlation_id),
    ).fetchall()
    if not rows:
        return None
    pairs: list[list[str]] = []
    affected_ids: list[str] = []
    event_ids: list[str] = []
    for row in rows:
        payload = _verified_payload(row)
        maintenance = payload.get("maintenance")
        if (
            row["event_type"] != "memory.deleted"
            or not isinstance(maintenance, dict)
            or maintenance.get("operation") != claims.target_tool
            or maintenance.get("criteria_hash") != claims.criteria_hash
            or maintenance.get("selection_hash") != claims.selection_hash
            or maintenance.get("selected_count") != claims.selected_count
            or not isinstance(maintenance.get("selected_state_hash"), str)
        ):
            raise MaintenanceOperationError("CAPABILITY_DEGRADED")
        record_id = str(row["stream_id"])
        pairs.append([record_id, maintenance["selected_state_hash"]])
        affected_ids.append(record_id)
        event_ids.append(str(row["event_id"]))
    if (
        len(rows) != claims.selected_count
        or sha256_json(pairs) != claims.selection_hash
    ):
        raise MaintenanceOperationError("CAPABILITY_DEGRADED")
    return DestructiveMutationReceipt(
        operation_id=_operation_id(workspace_id, claims),
        affected_ids=affected_ids,
        event_ids=event_ids,
        counts={"changed": len(rows), "selected": claims.selected_count},
        idempotent_replay=True,
        selected_count=claims.selected_count,
        changed_count=len(rows),
        skipped_count=0,
    )


def _translate_error(error: Exception) -> MaintenanceOperationError:
    if isinstance(error, MaintenanceOperationError):
        return error
    if isinstance(error, EventStreamConflict):
        return MaintenanceOperationError("EVENT_STREAM_CONFLICT")
    code = getattr(error, "code", None)
    if isinstance(code, str) and code in STABLE_ERROR_CODE_SET:
        return MaintenanceOperationError(code)
    return MaintenanceOperationError("CAPABILITY_DEGRADED")


async def _run_read(
    dependencies: MaintenanceOperationDependencies,
    operation: Callable[[], Any],
) -> Any:
    worker = asyncio.create_task(dependencies.worker_pool.run(operation))
    try:
        return await asyncio.shield(worker)
    except asyncio.CancelledError as cancellation:
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
    except BoundedWorkerBusyError:
        raise MaintenanceOperationError("TASK_REQUIRED") from None


async def _run_mutation(
    dependencies: MaintenanceOperationDependencies,
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
            result = await await_task_terminal(worker)
        except (_WorkerCancelledError, BoundedWorkerBusyError):
            raise cancellation from None
        except Exception:
            raise cancellation from None
        return result
    except BoundedWorkerBusyError:
        raise MaintenanceOperationError("TASK_REQUIRED") from None


def _prune_preview_sync(
    dependencies: MaintenanceOperationDependencies,
    workspace: Workspace,
    request: AdmittedRequest,
) -> Preview:
    evaluated_at_us = _now_us(dependencies)
    criteria = _prune_criteria(request)
    try:
        with dependencies.storage_resolver.locked_active(workspace) as active:
            path = _database_path(workspace, active)
            connection = _open_database(path, writable=False)
            try:
                connection.execute("BEGIN")
                high_water = _source_high_water(connection, workspace.workspace_id)
                records, eligible = _prune_candidates(
                    connection,
                    workspace.workspace_id,
                    criteria,
                    evaluated_at_us,
                )
                token, expires_at = _issue_selection_token(
                    dependencies,
                    workspace_id=workspace.workspace_id,
                    target_tool="memory_prune",
                    criteria=criteria,
                    records=records,
                    source_high_water=high_water,
                    evaluated_at_us=evaluated_at_us,
                )
                connection.rollback()
                return Preview(
                    selection_token=token,
                    counts={
                        "eligible": eligible,
                        "remaining": max(0, eligible - len(records)),
                        "selected": len(records),
                    },
                    sample_ids=[record.record_id for record in records[:20]],
                    expires_at=expires_at,
                )
            finally:
                if connection.in_transaction:
                    connection.rollback()
                connection.close()
    except MaintenanceOperationError:
        raise
    except Exception as exc:
        raise _translate_error(exc) from None


def _prune_sync(
    dependencies: MaintenanceOperationDependencies,
    workspace: Workspace,
    request: AdmittedRequest,
    claims: _SelectionClaims,
    cancelled: threading.Event,
) -> DestructiveMutationReceipt:
    if cancelled.is_set():
        raise _WorkerCancelledError()
    criteria = _prune_criteria(request)
    correlation_id = _selection_correlation(workspace.workspace_id, claims)
    recorded_at_us = _now_us(dependencies)
    try:
        with dependencies.storage_resolver.locked_active(workspace) as active:
            path = _database_path(workspace, active)
            connection = _open_database(path, writable=True)
            try:
                connection.execute("BEGIN IMMEDIATE")
                if cancelled.is_set():
                    raise _WorkerCancelledError()
                replay = _prune_replay(
                    connection,
                    workspace.workspace_id,
                    claims,
                    correlation_id,
                )
                if replay is not None:
                    connection.commit()
                    return replay
                if (
                    _source_high_water(connection, workspace.workspace_id)
                    != claims.source_high_water
                ):
                    raise MaintenanceOperationError("CONFLICT")
                records, _eligible = _prune_candidates(
                    connection,
                    workspace.workspace_id,
                    criteria,
                    claims.evaluated_at_us,
                )
                if (
                    len(records) != claims.selected_count
                    or sha256_json(_selection_pairs(records)) != claims.selection_hash
                ):
                    raise MaintenanceOperationError("CONFLICT")
                store = EventStore(connection, assume_transaction=True)
                event_ids: list[str] = []
                affected_ids: list[str] = []
                for record in records:
                    if cancelled.is_set():
                        raise _WorkerCancelledError()
                    state = dict(record.state)
                    state["deleted_at_us"] = recorded_at_us
                    event = store.append_and_project(
                        EventCommand(
                            workspace_id=workspace.workspace_id,
                            stream_id=record.record_id,
                            stream_kind="memory",
                            event_type="memory.deleted",
                            occurred_at_us=recorded_at_us,
                            recorded_at_us=recorded_at_us,
                            actor_type="client",
                            causation_event_id=record.source_event_id,
                            correlation_id=correlation_id,
                            expected_stream_version=record.stream_version + 1,
                            payload={
                                "record": state,
                                "maintenance": {
                                    "criteria_hash": claims.criteria_hash,
                                    "operation": claims.target_tool,
                                    "selected_count": claims.selected_count,
                                    "selected_state_hash": record.state_hash,
                                    "selection_hash": claims.selection_hash,
                                },
                            },
                        )
                    )
                    affected_ids.append(record.record_id)
                    event_ids.append(event.event_id)
                if cancelled.is_set():
                    raise _WorkerCancelledError()
                connection.commit()
                return DestructiveMutationReceipt(
                    operation_id=_operation_id(workspace.workspace_id, claims),
                    affected_ids=affected_ids,
                    event_ids=event_ids,
                    counts={
                        "changed": len(records),
                        "selected": claims.selected_count,
                    },
                    idempotent_replay=False,
                    selected_count=claims.selected_count,
                    changed_count=len(records),
                    skipped_count=0,
                )
            except (
                EventStreamConflict,
                MaintenanceOperationError,
                _WorkerCancelledError,
            ):
                if connection.in_transaction:
                    connection.rollback()
                raise
            except Exception:
                if connection.in_transaction:
                    connection.rollback()
                raise MaintenanceOperationError("CAPABILITY_DEGRADED") from None
            finally:
                if connection.in_transaction:
                    connection.rollback()
                connection.close()
    except (EventStreamConflict, MaintenanceOperationError, _WorkerCancelledError):
        raise
    except Exception as exc:
        raise _translate_error(exc) from None


def _duplicates_preview_sync(
    dependencies: MaintenanceOperationDependencies,
    workspace: Workspace,
    request: AdmittedRequest,
) -> Preview:
    evaluated_at_us = _now_us(dependencies)
    criteria = _duplicate_criteria(request)
    try:
        with dependencies.storage_resolver.locked_active(workspace) as active:
            path = _database_path(workspace, active)
            connection = _open_database(path, writable=False)
            try:
                connection.execute("BEGIN")
                high_water = _source_high_water(connection, workspace.workspace_id)
                groups, eligible, duplicate_group_count = _duplicate_groups(
                    connection,
                    workspace.workspace_id,
                    criteria,
                )
                records = _duplicate_candidates(groups)
                token, expires_at = _issue_selection_token(
                    dependencies,
                    workspace_id=workspace.workspace_id,
                    target_tool="memory_duplicates_cleanup",
                    criteria=criteria,
                    records=records,
                    source_high_water=high_water,
                    evaluated_at_us=evaluated_at_us,
                )
                connection.rollback()
                return Preview(
                    selection_token=token,
                    counts={
                        "duplicate_groups": duplicate_group_count,
                        "eligible": eligible,
                        "remaining": max(0, eligible - len(records)),
                        "selected": len(records),
                    },
                    sample_ids=[record.record_id for record in records[:20]],
                    expires_at=expires_at,
                )
            finally:
                if connection.in_transaction:
                    connection.rollback()
                connection.close()
    except MaintenanceOperationError:
        raise
    except Exception as exc:
        raise _translate_error(exc) from None


def _duplicates_replay(
    connection: sqlite3.Connection,
    workspace_id: str,
    claims: _SelectionClaims,
    correlation_id: str,
) -> DestructiveMutationReceipt | None:
    rows = connection.execute(
        "SELECT event_id,stream_id,event_type,payload_json,payload_hash "
        "FROM memory_events WHERE workspace_id=? AND correlation_id=? "
        "ORDER BY rowid",
        (workspace_id, correlation_id),
    ).fetchall()
    if not rows:
        return None
    candidate_pairs: list[list[str]] = []
    affected_ids: list[str] = []
    event_ids: list[str] = []
    keeper_count = 0
    for row in rows:
        payload = _verified_payload(row)
        maintenance = payload.get("maintenance")
        if (
            not isinstance(maintenance, dict)
            or maintenance.get("operation") != claims.target_tool
            or maintenance.get("criteria_hash") != claims.criteria_hash
            or maintenance.get("selection_hash") != claims.selection_hash
            or maintenance.get("selected_count") != claims.selected_count
        ):
            raise MaintenanceOperationError("CAPABILITY_DEGRADED")
        role = maintenance.get("role")
        record_id = str(row["stream_id"])
        if role == "keeper":
            if row["event_type"] != "memory.duplicates_merged":
                raise MaintenanceOperationError("CAPABILITY_DEGRADED")
            keeper_count += 1
        elif role == "candidate":
            state_hash = maintenance.get("selected_state_hash")
            if row["event_type"] != "memory.deleted" or not isinstance(state_hash, str):
                raise MaintenanceOperationError("CAPABILITY_DEGRADED")
            candidate_pairs.append([record_id, state_hash])
        else:
            raise MaintenanceOperationError("CAPABILITY_DEGRADED")
        if record_id not in affected_ids:
            affected_ids.append(record_id)
        event_ids.append(str(row["event_id"]))
    if (
        len(candidate_pairs) != claims.selected_count
        or sha256_json(candidate_pairs) != claims.selection_hash
        or len(rows) != claims.selected_count + keeper_count
    ):
        raise MaintenanceOperationError("CAPABILITY_DEGRADED")
    return DestructiveMutationReceipt(
        operation_id=_operation_id(workspace_id, claims),
        affected_ids=affected_ids,
        event_ids=event_ids,
        counts={
            "changed": claims.selected_count,
            "groups": keeper_count,
            "selected": claims.selected_count,
        },
        idempotent_replay=True,
        selected_count=claims.selected_count,
        changed_count=claims.selected_count,
        skipped_count=0,
    )


def _merged_keeper_state(group: _DuplicateGroup) -> dict[str, Any]:
    members = [group.keeper, *group.candidates]
    state = dict(group.keeper.state)
    tags: set[str] = set()
    for member in members:
        member_tags = member.state["tags"]
        if not isinstance(member_tags, list) or not all(
            isinstance(tag, str) for tag in member_tags
        ):
            raise MaintenanceOperationError("CAPABILITY_DEGRADED")
        tags.update(member_tags)
    state["tags"] = sorted(tags)
    state["pinned"] = any(bool(member.state["pinned"]) for member in members)
    state["is_permanent"] = any(
        bool(member.state["is_permanent"]) for member in members
    )
    state["archived"] = all(bool(member.state["archived"]) for member in members)
    outcome_sources = [
        member
        for member in members
        if member.state["outcome"] is not None or member.state["worked"] is not None
    ]
    if outcome_sources:
        outcome_source = max(
            outcome_sources,
            key=lambda record: (record.updated_at_us, record.record_id),
        )
        state["outcome"] = outcome_source.state["outcome"]
        state["worked"] = outcome_source.state["worked"]
    return state


def _duplicates_sync(
    dependencies: MaintenanceOperationDependencies,
    workspace: Workspace,
    request: AdmittedRequest,
    claims: _SelectionClaims,
    cancelled: threading.Event,
) -> DestructiveMutationReceipt:
    if cancelled.is_set():
        raise _WorkerCancelledError()
    criteria = _duplicate_criteria(request)
    correlation_id = _selection_correlation(workspace.workspace_id, claims)
    recorded_at_us = _now_us(dependencies)
    try:
        with dependencies.storage_resolver.locked_active(workspace) as active:
            path = _database_path(workspace, active)
            connection = _open_database(path, writable=True)
            try:
                connection.execute("BEGIN IMMEDIATE")
                if cancelled.is_set():
                    raise _WorkerCancelledError()
                replay = _duplicates_replay(
                    connection,
                    workspace.workspace_id,
                    claims,
                    correlation_id,
                )
                if replay is not None:
                    connection.commit()
                    return replay
                if (
                    _source_high_water(connection, workspace.workspace_id)
                    != claims.source_high_water
                ):
                    raise MaintenanceOperationError("CONFLICT")
                groups, _eligible, _duplicate_group_count = _duplicate_groups(
                    connection,
                    workspace.workspace_id,
                    criteria,
                )
                records = _duplicate_candidates(groups)
                if (
                    len(records) != claims.selected_count
                    or sha256_json(_selection_pairs(records)) != claims.selection_hash
                ):
                    raise MaintenanceOperationError("CONFLICT")
                store = EventStore(connection, assume_transaction=True)
                affected_ids: list[str] = []
                event_ids: list[str] = []
                for group in groups:
                    if cancelled.is_set():
                        raise _WorkerCancelledError()
                    keeper_state = _merged_keeper_state(group)
                    keeper_event = store.append_and_project(
                        EventCommand(
                            workspace_id=workspace.workspace_id,
                            stream_id=group.keeper.record_id,
                            stream_kind="memory",
                            event_type="memory.duplicates_merged",
                            occurred_at_us=recorded_at_us,
                            recorded_at_us=recorded_at_us,
                            actor_type="client",
                            causation_event_id=group.keeper.source_event_id,
                            correlation_id=correlation_id,
                            expected_stream_version=group.keeper.stream_version + 1,
                            payload={
                                "record": keeper_state,
                                "maintenance": {
                                    "criteria_hash": claims.criteria_hash,
                                    "operation": claims.target_tool,
                                    "role": "keeper",
                                    "selected_count": claims.selected_count,
                                    "selection_hash": claims.selection_hash,
                                },
                            },
                        )
                    )
                    affected_ids.append(group.keeper.record_id)
                    event_ids.append(keeper_event.event_id)
                    for candidate in group.candidates:
                        if cancelled.is_set():
                            raise _WorkerCancelledError()
                        candidate_state = dict(candidate.state)
                        candidate_state["deleted_at_us"] = recorded_at_us
                        candidate_event = store.append_and_project(
                            EventCommand(
                                workspace_id=workspace.workspace_id,
                                stream_id=candidate.record_id,
                                stream_kind="memory",
                                event_type="memory.deleted",
                                occurred_at_us=recorded_at_us,
                                recorded_at_us=recorded_at_us,
                                actor_type="client",
                                causation_event_id=candidate.source_event_id,
                                correlation_id=correlation_id,
                                expected_stream_version=candidate.stream_version + 1,
                                payload={
                                    "record": candidate_state,
                                    "maintenance": {
                                        "criteria_hash": claims.criteria_hash,
                                        "keeper_record_id": group.keeper.record_id,
                                        "operation": claims.target_tool,
                                        "role": "candidate",
                                        "selected_count": claims.selected_count,
                                        "selected_state_hash": candidate.state_hash,
                                        "selection_hash": claims.selection_hash,
                                    },
                                },
                            )
                        )
                        affected_ids.append(candidate.record_id)
                        event_ids.append(candidate_event.event_id)
                if cancelled.is_set():
                    raise _WorkerCancelledError()
                connection.commit()
                return DestructiveMutationReceipt(
                    operation_id=_operation_id(workspace.workspace_id, claims),
                    affected_ids=affected_ids,
                    event_ids=event_ids,
                    counts={
                        "changed": len(records),
                        "groups": len(groups),
                        "selected": claims.selected_count,
                    },
                    idempotent_replay=False,
                    selected_count=claims.selected_count,
                    changed_count=len(records),
                    skipped_count=0,
                )
            except (
                EventStreamConflict,
                MaintenanceOperationError,
                _WorkerCancelledError,
            ):
                if connection.in_transaction:
                    connection.rollback()
                raise
            except Exception:
                if connection.in_transaction:
                    connection.rollback()
                raise MaintenanceOperationError("CAPABILITY_DEGRADED") from None
            finally:
                if connection.in_transaction:
                    connection.rollback()
                connection.close()
    except (EventStreamConflict, MaintenanceOperationError, _WorkerCancelledError):
        raise
    except Exception as exc:
        raise _translate_error(exc) from None


def _compaction_preview_sync(
    dependencies: MaintenanceOperationDependencies,
    workspace: Workspace,
    request: AdmittedRequest,
) -> Preview:
    evaluated_at_us = _now_us(dependencies)
    criteria = _compaction_criteria(request)
    try:
        with dependencies.storage_resolver.locked_active(workspace) as active:
            path = _database_path(workspace, active)
            connection = _open_database(path, writable=False)
            try:
                connection.execute("BEGIN")
                high_water = _source_high_water(connection, workspace.workspace_id)
                records, eligible = _compaction_candidates(
                    connection,
                    workspace.workspace_id,
                    criteria,
                )
                token, expires_at = _issue_selection_token(
                    dependencies,
                    workspace_id=workspace.workspace_id,
                    target_tool="memory_compact",
                    criteria=criteria,
                    records=records,
                    source_high_water=high_water,
                    evaluated_at_us=evaluated_at_us,
                )
                connection.rollback()
                return Preview(
                    selection_token=token,
                    counts={
                        "eligible": eligible,
                        "remaining": max(0, eligible - len(records)),
                        "selected": len(records),
                    },
                    sample_ids=[record.record_id for record in records[:20]],
                    expires_at=expires_at,
                )
            finally:
                if connection.in_transaction:
                    connection.rollback()
                connection.close()
    except MaintenanceOperationError:
        raise
    except Exception as exc:
        raise _translate_error(exc) from None


def _compaction_correlation(workspace_id: str, idempotency_key: str) -> str:
    return deterministic_id(
        "job",
        "memory-compact-idempotency",
        workspace_id,
        idempotency_key,
    )


def _compaction_request_hash(claims: _SelectionClaims) -> str:
    return sha256_json(
        {
            "criteria_hash": claims.criteria_hash,
            "selected_count": claims.selected_count,
            "selection_hash": claims.selection_hash,
        }
    )


def _compaction_replay(
    connection: sqlite3.Connection,
    workspace_id: str,
    claims: _SelectionClaims,
    correlation_id: str,
) -> MemoryCompactData | None:
    rows = connection.execute(
        "SELECT event_id,stream_id,stream_kind,event_type,occurred_at_us,"
        "recorded_at_us,payload_json,payload_hash FROM memory_events "
        "WHERE workspace_id=? AND correlation_id=? ORDER BY rowid",
        (workspace_id, correlation_id),
    ).fetchall()
    if not rows:
        return None
    request_hash = _compaction_request_hash(claims)
    archive_pairs: list[list[str]] = []
    affected_ids: list[str] = []
    event_ids: list[str] = []
    source_event_ids: list[str] | None = None
    summary_id: str | None = None
    summary_count = 0
    archive_count = 0
    supersession_count = 0
    for row in rows:
        payload = _verified_payload(row)
        maintenance = payload.get("maintenance")
        if not isinstance(maintenance, dict):
            raise MaintenanceOperationError("CAPABILITY_DEGRADED")
        existing_request_hash = maintenance.get("idempotency_request_hash")
        if existing_request_hash != request_hash:
            raise MaintenanceOperationError("IDEMPOTENCY_CONFLICT")
        if (
            maintenance.get("operation") != claims.target_tool
            or maintenance.get("criteria_hash") != claims.criteria_hash
            or maintenance.get("selection_hash") != claims.selection_hash
            or maintenance.get("selected_count") != claims.selected_count
        ):
            raise MaintenanceOperationError("CAPABILITY_DEGRADED")
        role = maintenance.get("role")
        stream_id = str(row["stream_id"])
        if role == "summary":
            if (
                summary_count
                or row["stream_kind"] != "memory"
                or row["event_type"] != "memory.created"
            ):
                raise MaintenanceOperationError("CAPABILITY_DEGRADED")
            raw_source_ids = maintenance.get("source_event_ids")
            if not isinstance(raw_source_ids, list) or not all(
                isinstance(item, str) for item in raw_source_ids
            ):
                raise MaintenanceOperationError("CAPABILITY_DEGRADED")
            source_event_ids = list(raw_source_ids)
            summary_id = stream_id
            summary_count += 1
        elif role == "archive":
            state_hash = maintenance.get("selected_state_hash")
            if (
                row["stream_kind"] != "memory"
                or row["event_type"] != "memory.compaction_archived"
                or not isinstance(state_hash, str)
            ):
                raise MaintenanceOperationError("CAPABILITY_DEGRADED")
            archive_pairs.append([stream_id, state_hash])
            archive_count += 1
        elif role == "supersession":
            if (
                row["stream_kind"] != "relationship"
                or row["event_type"] != "relationship.created"
            ):
                raise MaintenanceOperationError("CAPABILITY_DEGRADED")
            supersession_count += 1
        else:
            raise MaintenanceOperationError("CAPABILITY_DEGRADED")
        affected_ids.append(stream_id)
        event_ids.append(str(row["event_id"]))
    if (
        summary_count != 1
        or summary_id is None
        or source_event_ids is None
        or archive_count != claims.selected_count
        or supersession_count != claims.selected_count
        or len(source_event_ids) != claims.selected_count
        or sha256_json(archive_pairs) != claims.selection_hash
        or len(rows) != 1 + claims.selected_count * 2
    ):
        raise MaintenanceOperationError("CAPABILITY_DEGRADED")
    summary = _record_summary(_load_snapshot(connection, workspace_id, summary_id))
    receipt = DestructiveMutationReceipt(
        operation_id=_operation_id(workspace_id, claims),
        affected_ids=affected_ids,
        event_ids=event_ids,
        counts={
            "archived": archive_count,
            "selected": claims.selected_count,
            "supersessions": supersession_count,
        },
        idempotent_replay=True,
        selected_count=claims.selected_count,
        changed_count=archive_count,
        skipped_count=0,
    )
    return MemoryCompactData(
        summary_record=summary,
        source_event_ids=source_event_ids,
        receipt=receipt,
    )


def _summary_state(
    request: AdmittedRequest,
    records: Sequence[_RecordSnapshot],
) -> dict[str, Any]:
    return {
        "record_type": "learning",
        "legacy_type": None,
        "content": request.summary,
        "rationale": f"Compacted summary of {len(records)} canonical memories.",
        "context": {
            "compacted_record_ids": [record.record_id for record in records],
            "query": request.query,
        },
        "tags": ["checkpoint", "compacted"],
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
        "source_client": "v7-maintenance",
        "source_model": None,
        "deleted_at_us": None,
    }


def _compaction_sync(
    dependencies: MaintenanceOperationDependencies,
    workspace: Workspace,
    request: AdmittedRequest,
    claims: _SelectionClaims,
    cancelled: threading.Event,
) -> MemoryCompactData:
    if cancelled.is_set():
        raise _WorkerCancelledError()
    criteria = _compaction_criteria(request)
    correlation_id = _compaction_correlation(
        workspace.workspace_id, request.idempotency_key
    )
    request_hash = _compaction_request_hash(claims)
    recorded_at_us = _now_us(dependencies)
    try:
        with dependencies.storage_resolver.locked_active(workspace) as active:
            path = _database_path(workspace, active)
            connection = _open_database(path, writable=True)
            try:
                connection.execute("BEGIN IMMEDIATE")
                if cancelled.is_set():
                    raise _WorkerCancelledError()
                replay = _compaction_replay(
                    connection,
                    workspace.workspace_id,
                    claims,
                    correlation_id,
                )
                if replay is not None:
                    connection.commit()
                    return replay
                if claims.selected_count == 0:
                    raise MaintenanceOperationError("NOT_FOUND")
                if (
                    _source_high_water(connection, workspace.workspace_id)
                    != claims.source_high_water
                ):
                    raise MaintenanceOperationError("CONFLICT")
                records, _eligible = _compaction_candidates(
                    connection,
                    workspace.workspace_id,
                    criteria,
                )
                if (
                    len(records) != claims.selected_count
                    or sha256_json(_selection_pairs(records)) != claims.selection_hash
                ):
                    raise MaintenanceOperationError("CONFLICT")
                source_event_ids = [record.source_event_id for record in records]
                summary_id = deterministic_id(
                    "mem",
                    "memory-compaction-summary",
                    workspace.workspace_id,
                    request.idempotency_key,
                )
                common = {
                    "criteria_hash": claims.criteria_hash,
                    "idempotency_request_hash": request_hash,
                    "operation": claims.target_tool,
                    "selected_count": claims.selected_count,
                    "selection_hash": claims.selection_hash,
                }
                store = EventStore(connection, assume_transaction=True)
                summary_event = store.append_and_project(
                    EventCommand(
                        workspace_id=workspace.workspace_id,
                        stream_id=summary_id,
                        stream_kind="memory",
                        event_type="memory.created",
                        occurred_at_us=recorded_at_us,
                        recorded_at_us=recorded_at_us,
                        actor_type="client",
                        correlation_id=correlation_id,
                        expected_stream_version=1,
                        payload={
                            "record": _summary_state(request, records),
                            "maintenance": {
                                **common,
                                "role": "summary",
                                "source_event_ids": source_event_ids,
                            },
                        },
                    )
                )
                affected_ids = [summary_id]
                event_ids = [summary_event.event_id]
                for record in records:
                    if cancelled.is_set():
                        raise _WorkerCancelledError()
                    archived_state = dict(record.state)
                    archived_state["archived"] = True
                    archive_event = store.append_and_project(
                        EventCommand(
                            workspace_id=workspace.workspace_id,
                            stream_id=record.record_id,
                            stream_kind="memory",
                            event_type="memory.compaction_archived",
                            occurred_at_us=recorded_at_us,
                            recorded_at_us=recorded_at_us,
                            actor_type="client",
                            causation_event_id=record.source_event_id,
                            correlation_id=correlation_id,
                            expected_stream_version=record.stream_version + 1,
                            payload={
                                "record": archived_state,
                                "maintenance": {
                                    **common,
                                    "role": "archive",
                                    "selected_state_hash": record.state_hash,
                                },
                            },
                        )
                    )
                    relationship_id = deterministic_id(
                        "rel",
                        "memory-compaction-supersession",
                        workspace.workspace_id,
                        request.idempotency_key,
                        record.record_id,
                    )
                    relationship_event = store.append_and_project(
                        EventCommand(
                            workspace_id=workspace.workspace_id,
                            stream_id=relationship_id,
                            stream_kind="relationship",
                            event_type="relationship.created",
                            occurred_at_us=recorded_at_us,
                            recorded_at_us=recorded_at_us,
                            actor_type="client",
                            causation_event_id=summary_event.event_id,
                            correlation_id=correlation_id,
                            expected_stream_version=1,
                            payload={
                                "relationship": {
                                    "source_record_id": summary_id,
                                    "target_record_id": record.record_id,
                                    "relationship_type": "supersedes",
                                    "legacy_type": None,
                                    "description": "Canonical memory compaction.",
                                    "confidence": 1.0,
                                    "metadata": {"operation": "memory_compact"},
                                    "valid_from_us": recorded_at_us,
                                    "valid_to_us": None,
                                },
                                "maintenance": {
                                    **common,
                                    "role": "supersession",
                                    "source_record_id": summary_id,
                                    "target_record_id": record.record_id,
                                },
                            },
                        )
                    )
                    affected_ids.extend([record.record_id, relationship_id])
                    event_ids.extend(
                        [archive_event.event_id, relationship_event.event_id]
                    )
                if cancelled.is_set():
                    raise _WorkerCancelledError()
                summary = _record_summary(
                    _load_snapshot(connection, workspace.workspace_id, summary_id)
                )
                connection.commit()
                return MemoryCompactData(
                    summary_record=summary,
                    source_event_ids=source_event_ids,
                    receipt=DestructiveMutationReceipt(
                        operation_id=_operation_id(workspace.workspace_id, claims),
                        affected_ids=affected_ids,
                        event_ids=event_ids,
                        counts={
                            "archived": len(records),
                            "selected": claims.selected_count,
                            "supersessions": len(records),
                        },
                        idempotent_replay=False,
                        selected_count=claims.selected_count,
                        changed_count=len(records),
                        skipped_count=0,
                    ),
                )
            except (
                EventStreamConflict,
                MaintenanceOperationError,
                _WorkerCancelledError,
            ):
                if connection.in_transaction:
                    connection.rollback()
                raise
            except Exception:
                if connection.in_transaction:
                    connection.rollback()
                raise MaintenanceOperationError("CAPABILITY_DEGRADED") from None
            finally:
                if connection.in_transaction:
                    connection.rollback()
                connection.close()
    except (EventStreamConflict, MaintenanceOperationError, _WorkerCancelledError):
        raise
    except Exception as exc:
        raise _translate_error(exc) from None


def _dream_preview_sync(
    dependencies: MaintenanceOperationDependencies,
    workspace: Workspace,
    request: AdmittedRequest,
) -> Preview:
    evaluated_at_us = _now_us(dependencies)
    criteria = _dream_criteria(request)
    try:
        with dependencies.storage_resolver.locked_active(workspace) as active:
            path = _database_path(workspace, active)
            connection = _open_database(path, writable=False)
            try:
                connection.execute("BEGIN")
                high_water = _source_high_water(connection, workspace.workspace_id)
                (
                    records,
                    eligible,
                    reevaluation_duplicates,
                    summary_duplicates,
                ) = _dream_candidates(connection, workspace.workspace_id)
                token, expires_at = _issue_selection_token(
                    dependencies,
                    workspace_id=workspace.workspace_id,
                    target_tool="dream_duplicates_purge",
                    criteria=criteria,
                    records=records,
                    source_high_water=high_water,
                    evaluated_at_us=evaluated_at_us,
                )
                connection.rollback()
                return Preview(
                    selection_token=token,
                    counts={
                        "eligible": eligible,
                        "reevaluation_duplicates": reevaluation_duplicates,
                        "remaining": max(0, eligible - len(records)),
                        "selected": len(records),
                        "summary_duplicates": summary_duplicates,
                    },
                    sample_ids=[record.record_id for record in records[:20]],
                    expires_at=expires_at,
                )
            finally:
                if connection.in_transaction:
                    connection.rollback()
                connection.close()
    except MaintenanceOperationError:
        raise
    except Exception as exc:
        raise _translate_error(exc) from None


def _dream_sync(
    dependencies: MaintenanceOperationDependencies,
    workspace: Workspace,
    request: AdmittedRequest,
    claims: _SelectionClaims,
    cancelled: threading.Event,
) -> DestructiveMutationReceipt:
    if cancelled.is_set():
        raise _WorkerCancelledError()
    _dream_criteria(request)
    correlation_id = _selection_correlation(workspace.workspace_id, claims)
    recorded_at_us = _now_us(dependencies)
    try:
        with dependencies.storage_resolver.locked_active(workspace) as active:
            path = _database_path(workspace, active)
            connection = _open_database(path, writable=True)
            try:
                connection.execute("BEGIN IMMEDIATE")
                if cancelled.is_set():
                    raise _WorkerCancelledError()
                replay = _prune_replay(
                    connection,
                    workspace.workspace_id,
                    claims,
                    correlation_id,
                )
                if replay is not None:
                    connection.commit()
                    return replay
                if (
                    _source_high_water(connection, workspace.workspace_id)
                    != claims.source_high_water
                ):
                    raise MaintenanceOperationError("CONFLICT")
                records, _eligible, _reevaluations, _summaries = _dream_candidates(
                    connection, workspace.workspace_id
                )
                if (
                    len(records) != claims.selected_count
                    or sha256_json(_selection_pairs(records)) != claims.selection_hash
                ):
                    raise MaintenanceOperationError("CONFLICT")
                store = EventStore(connection, assume_transaction=True)
                affected_ids: list[str] = []
                event_ids: list[str] = []
                for record in records:
                    if cancelled.is_set():
                        raise _WorkerCancelledError()
                    state = dict(record.state)
                    state["deleted_at_us"] = recorded_at_us
                    event = store.append_and_project(
                        EventCommand(
                            workspace_id=workspace.workspace_id,
                            stream_id=record.record_id,
                            stream_kind="memory",
                            event_type="memory.deleted",
                            occurred_at_us=recorded_at_us,
                            recorded_at_us=recorded_at_us,
                            actor_type="client",
                            causation_event_id=record.source_event_id,
                            correlation_id=correlation_id,
                            expected_stream_version=record.stream_version + 1,
                            payload={
                                "record": state,
                                "maintenance": {
                                    "criteria_hash": claims.criteria_hash,
                                    "operation": claims.target_tool,
                                    "selected_count": claims.selected_count,
                                    "selected_state_hash": record.state_hash,
                                    "selection_hash": claims.selection_hash,
                                },
                            },
                        )
                    )
                    affected_ids.append(record.record_id)
                    event_ids.append(event.event_id)
                if cancelled.is_set():
                    raise _WorkerCancelledError()
                connection.commit()
                return DestructiveMutationReceipt(
                    operation_id=_operation_id(workspace.workspace_id, claims),
                    affected_ids=affected_ids,
                    event_ids=event_ids,
                    counts={
                        "changed": len(records),
                        "selected": claims.selected_count,
                    },
                    idempotent_replay=False,
                    selected_count=claims.selected_count,
                    changed_count=len(records),
                    skipped_count=0,
                )
            except (
                EventStreamConflict,
                MaintenanceOperationError,
                _WorkerCancelledError,
            ):
                if connection.in_transaction:
                    connection.rollback()
                raise
            except Exception:
                if connection.in_transaction:
                    connection.rollback()
                raise MaintenanceOperationError("CAPABILITY_DEGRADED") from None
            finally:
                if connection.in_transaction:
                    connection.rollback()
                connection.close()
    except (EventStreamConflict, MaintenanceOperationError, _WorkerCancelledError):
        raise
    except Exception as exc:
        raise _translate_error(exc) from None


def build_maintenance_operations(
    dependencies: MaintenanceOperationDependencies,
) -> Mapping[str, Callable[..., Any]]:
    """Return the exact immutable canonical maintenance registry."""

    if not isinstance(dependencies, MaintenanceOperationDependencies):
        raise TypeError("dependencies must be MaintenanceOperationDependencies")

    async def memory_prune_preview(
        *, workspace: Workspace, request: AdmittedRequest
    ) -> Preview:
        _authorize(workspace, request, "memory_prune_preview")
        return await _run_read(
            dependencies,
            lambda: _prune_preview_sync(dependencies, workspace, request),
        )

    async def memory_prune(
        *, workspace: Workspace, request: AdmittedRequest
    ) -> DestructiveMutationReceipt:
        _authorize(workspace, request, "memory_prune")
        criteria = _prune_criteria(request)
        claims = _verify_selection_token(
            dependencies,
            request.selection_token,
            workspace_id=workspace.workspace_id,
            target_tool="memory_prune",
            criteria=criteria,
        )
        return await _run_mutation(
            dependencies,
            lambda cancelled: _prune_sync(
                dependencies,
                workspace,
                request,
                claims,
                cancelled,
            ),
        )

    async def memory_duplicates_preview(
        *, workspace: Workspace, request: AdmittedRequest
    ) -> Preview:
        _authorize(workspace, request, "memory_duplicates_preview")
        return await _run_read(
            dependencies,
            lambda: _duplicates_preview_sync(dependencies, workspace, request),
        )

    async def memory_duplicates_cleanup(
        *, workspace: Workspace, request: AdmittedRequest
    ) -> DestructiveMutationReceipt:
        _authorize(workspace, request, "memory_duplicates_cleanup")
        criteria = _duplicate_criteria(request)
        claims = _verify_selection_token(
            dependencies,
            request.selection_token,
            workspace_id=workspace.workspace_id,
            target_tool="memory_duplicates_cleanup",
            criteria=criteria,
        )
        return await _run_mutation(
            dependencies,
            lambda cancelled: _duplicates_sync(
                dependencies,
                workspace,
                request,
                claims,
                cancelled,
            ),
        )

    async def memory_compaction_preview(
        *, workspace: Workspace, request: AdmittedRequest
    ) -> Preview:
        _authorize(workspace, request, "memory_compaction_preview")
        return await _run_read(
            dependencies,
            lambda: _compaction_preview_sync(dependencies, workspace, request),
        )

    async def memory_compact(
        *, workspace: Workspace, request: AdmittedRequest
    ) -> MemoryCompactData:
        _authorize(workspace, request, "memory_compact")
        criteria = _compaction_criteria(request)
        claims = _verify_selection_token(
            dependencies,
            request.selection_token,
            workspace_id=workspace.workspace_id,
            target_tool="memory_compact",
            criteria=criteria,
        )
        return await _run_mutation(
            dependencies,
            lambda cancelled: _compaction_sync(
                dependencies,
                workspace,
                request,
                claims,
                cancelled,
            ),
        )

    async def dream_duplicates_preview(
        *, workspace: Workspace, request: AdmittedRequest
    ) -> Preview:
        _authorize(workspace, request, "dream_duplicates_preview")
        return await _run_read(
            dependencies,
            lambda: _dream_preview_sync(dependencies, workspace, request),
        )

    async def dream_duplicates_purge(
        *, workspace: Workspace, request: AdmittedRequest
    ) -> DestructiveMutationReceipt:
        _authorize(workspace, request, "dream_duplicates_purge")
        criteria = _dream_criteria(request)
        claims = _verify_selection_token(
            dependencies,
            request.selection_token,
            workspace_id=workspace.workspace_id,
            target_tool="dream_duplicates_purge",
            criteria=criteria,
        )
        return await _run_mutation(
            dependencies,
            lambda cancelled: _dream_sync(
                dependencies,
                workspace,
                request,
                claims,
                cancelled,
            ),
        )

    return MappingProxyType(
        {
            "dream_duplicates_preview": dream_duplicates_preview,
            "dream_duplicates_purge": dream_duplicates_purge,
            "memory_compact": memory_compact,
            "memory_compaction_preview": memory_compaction_preview,
            "memory_duplicates_cleanup": memory_duplicates_cleanup,
            "memory_duplicates_preview": memory_duplicates_preview,
            "memory_prune": memory_prune,
            "memory_prune_preview": memory_prune_preview,
        }
    )


__all__ = [
    "MaintenanceOperationDependencies",
    "MaintenanceOperationError",
    "build_maintenance_operations",
]
