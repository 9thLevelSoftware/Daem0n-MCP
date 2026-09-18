"""Canonical append-only memory event primitives.

This module intentionally depends only on the Python standard library.  SQLite
is the v7 source of truth; retrieval, model, and vector packages must never be
needed to encode or verify an event.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import unicodedata
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from typing import Any

_ID_PREFIXES = frozenset({"mem", "fact", "rel", "evt", "prj", "enr", "job", "mig"})
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_PROJECTION_REBUILD_GRACE_US = 5_000_000


class CanonicalEncodingError(ValueError):
    """Raised when a value cannot be represented by the v7 canonical format."""


class EventStreamConflict(RuntimeError):  # noqa: N818 -- retained public Python API name
    """Raised when an append would fork or overwrite an event stream."""

    code = "EVENT_STREAM_CONFLICT"

    def __init__(self, detail: str = "event stream head does not match") -> None:
        super().__init__(f"{self.code}: {detail}")


class EventBundleError(ValueError):
    """Raised when a portable v7 event bundle fails closed validation."""

    def __init__(self, code: str, detail: str) -> None:
        self.code = code
        super().__init__(f"{code}: {detail}")


class CompatibilityStreamError(RuntimeError):
    """Raised when a retained v6 identity cannot safely select one v7 stream."""

    def __init__(self, code: str, detail: str) -> None:
        self.code = code
        super().__init__(f"{code}: {detail}")


@dataclass(frozen=True, slots=True)
class EventCommand:
    """Complete provenance and projection input for one semantic event."""

    workspace_id: str
    stream_id: str
    stream_kind: str
    event_type: str
    occurred_at_us: int
    recorded_at_us: int
    actor_type: str
    payload: Mapping[str, Any]
    actor_id: str | None = None
    causation_event_id: str | None = None
    correlation_id: str | None = None
    event_schema_version: int = 1
    expected_stream_version: int | None = None


@dataclass(frozen=True, slots=True)
class AppendedEvent:
    event_id: str
    event_hash: str
    payload_hash: str
    stream_version: int
    previous_event_hash: str | None


@dataclass(frozen=True, slots=True)
class GovernanceEventCommand:
    """Complete input for one append-only governance-domain event."""

    workspace_id: str
    stream_id: str
    stream_kind: str
    event_type: str
    occurred_at_us: int
    recorded_at_us: int
    actor_type: str
    payload: Mapping[str, Any]
    actor_id: str | None = None
    causation_event_id: str | None = None
    correlation_id: str | None = None
    event_schema_version: int = 1
    expected_stream_version: int | None = None


@dataclass(frozen=True, slots=True)
class EventBundleImportResult:
    events_imported: int
    events_existing: int
    root_hash: str


def _normalize_string(value: str) -> str:
    normalized = unicodedata.normalize(
        "NFC", value.replace("\r\n", "\n").replace("\r", "\n")
    )
    try:
        normalized.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise CanonicalEncodingError(
            "strings must contain valid Unicode scalar values"
        ) from exc
    return normalized


def normalize_canonical(value: Any) -> Any:
    """Recursively normalize one JSON-compatible value without losing whitespace."""
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise CanonicalEncodingError("non-finite numbers are not canonical")
        return value
    if isinstance(value, str):
        return _normalize_string(value)
    if isinstance(value, list):
        return [normalize_canonical(item) for item in value]
    if isinstance(value, Mapping):
        normalized: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise CanonicalEncodingError("object keys must be strings")
            normalized_key = _normalize_string(key)
            if normalized_key in normalized:
                raise CanonicalEncodingError("object keys collide after normalization")
            normalized[normalized_key] = normalize_canonical(item)
        return normalized
    raise CanonicalEncodingError(f"unsupported canonical value: {type(value).__name__}")


def canonical_json_bytes(value: Any) -> bytes:
    """Return exact compact, sorted, NFC-normalized UTF-8 JSON bytes."""
    try:
        encoded = json.dumps(
            normalize_canonical(value),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
    except (TypeError, ValueError, RecursionError) as exc:
        if isinstance(exc, CanonicalEncodingError):
            raise
        raise CanonicalEncodingError("value is not canonical JSON") from exc
    return encoded.encode("utf-8")


def sha256_json(value: Any) -> str:
    """Hash one canonical JSON value using full lower-case SHA-256."""
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def deterministic_id(prefix: str, kind: str, *parts: Any) -> str:
    """Derive one opaque v7 identifier from an explicit idempotency domain."""
    if prefix not in _ID_PREFIXES:
        raise ValueError(f"unsupported v7 identifier prefix: {prefix}")
    if not isinstance(kind, str) or not kind:
        raise ValueError("identifier kind must be a non-empty string")
    return f"{prefix}_{sha256_json(['daem0nmcp', 'v7', kind, *parts])}"


def event_hash_for(event_columns: Mapping[str, Any]) -> str:
    """Hash the complete persisted event envelope excluding ID/hash/payload text."""
    forbidden = {"event_id", "event_hash", "payload_json"} & set(event_columns)
    if forbidden:
        raise ValueError(
            "event hash input contains database-only/excluded fields: "
            + ", ".join(sorted(forbidden))
        )
    return sha256_json(dict(event_columns))


def event_id_for_hash(event_hash: str) -> str:
    """Return the exact event identifier for a verified SHA-256 event hash."""
    if not isinstance(event_hash, str) or not _SHA256_RE.fullmatch(event_hash):
        raise ValueError("event hash must be 64 lower-case hexadecimal characters")
    return f"evt_{event_hash}"


_RECORD_TYPES = frozenset(
    {"decision", "pattern", "warning", "learning", "procedure", "observation", "legacy"}
)
_ACTOR_TYPES = frozenset({"user", "client", "system", "migration", "import"})
_STREAM_KINDS = frozenset({"memory", "fact", "relationship"})
_RECORD_FIELDS = frozenset(
    {
        "record_type",
        "legacy_type",
        "content",
        "rationale",
        "context",
        "tags",
        "file_path",
        "file_path_relative",
        "keywords",
        "is_permanent",
        "pinned",
        "archived",
        "outcome",
        "worked",
        "recall_count",
        "surprise_score",
        "importance_score",
        "source_client",
        "source_model",
        "deleted_at_us",
    }
)
_COMPATIBILITY_MEMORY_FIELDS = frozenset(
    {
        "category",
        "content",
        "rationale",
        "context",
        "tags",
        "file_path",
        "file_path_relative",
        "keywords",
        "is_permanent",
        "outcome",
        "worked",
        "pinned",
        "archived",
        "source_client",
        "source_model",
        "updated_at",
    }
)


def compatibility_memory_record(
    memory: Any, *, deleted_at_us: int | None = None
) -> dict[str, Any]:
    """Translate one retained v6 Memory row into the canonical typed state."""

    category = memory.category
    known_categories = {
        "decision",
        "pattern",
        "warning",
        "learning",
        "procedure",
        "observation",
    }
    record_type = category if category in known_categories else "legacy"
    legacy_type = (
        None
        if record_type != "legacy"
        else ("<null>" if category is None else str(category))
    )
    return {
        "record_type": record_type,
        "legacy_type": legacy_type,
        "content": memory.content,
        "rationale": memory.rationale,
        "context": memory.context or {},
        "tags": memory.tags or [],
        "file_path": memory.file_path,
        "file_path_relative": memory.file_path_relative,
        "keywords": memory.keywords,
        "is_permanent": bool(memory.is_permanent),
        "pinned": bool(memory.pinned),
        "archived": bool(memory.archived),
        "outcome": memory.outcome,
        "worked": None if memory.worked is None else bool(memory.worked),
        "recall_count": max(0, int(memory.recall_count or 0)),
        "surprise_score": memory.surprise_score,
        "importance_score": memory.importance_score,
        "source_client": memory.source_client,
        "source_model": memory.source_model,
        "deleted_at_us": deleted_at_us,
    }


def apply_compatibility_memory_update(memory: Any, **changes: Any) -> None:
    """Apply an event-authorized mutation to the retained v6 projection."""

    unsupported = set(changes) - _COMPATIBILITY_MEMORY_FIELDS
    if unsupported:
        raise ValueError(
            f"unsupported compatibility memory fields: {sorted(unsupported)}"
        )
    for name, value in changes.items():
        setattr(memory, name, value)


async def delete_compatibility_memory(session: Any, memory: Any) -> None:
    """Delete only the retained v6 row after a canonical tombstone exists."""

    await session.delete(memory)


def invalidate_compatibility_memory_version(
    version: Any,
    *,
    valid_to: Any,
    invalidated_by_version_id: int,
) -> None:
    """Apply event-authorized invalidation to the retained bitemporal row."""

    version.valid_to = valid_to
    version.invalidated_by_version_id = invalidated_by_version_id


def memory_content_hash(record: Mapping[str, Any]) -> str:
    """Hash the portable, content-defining fields of a typed memory record."""

    return sha256_json(
        {
            "record_type": record.get("record_type"),
            "legacy_type": record.get("legacy_type"),
            "content": record.get("content"),
            "rationale": record.get("rationale"),
            "context": record.get("context", {}),
            "tags": record.get("tags", []),
            "file_path": record.get("file_path"),
            "file_path_relative": record.get("file_path_relative"),
            "outcome": record.get("outcome"),
            "worked": record.get("worked"),
        }
    )


def memory_state_hash(record: Mapping[str, Any]) -> str:
    """Hash canonical semantic state while excluding telemetry and timestamps."""

    return sha256_json(
        {
            "content": {
                "record_type": record.get("record_type"),
                "legacy_type": record.get("legacy_type"),
                "content": record.get("content"),
                "rationale": record.get("rationale"),
                "context": record.get("context", {}),
                "tags": record.get("tags", []),
                "file_path": record.get("file_path"),
                "file_path_relative": record.get("file_path_relative"),
                "outcome": record.get("outcome"),
                "worked": record.get("worked"),
            },
            "is_permanent": bool(record.get("is_permanent", False)),
            "pinned": bool(record.get("pinned", False)),
            "archived": bool(record.get("archived", False)),
            "deleted": record.get("deleted_at_us") is not None,
            "source_client": record.get("source_client"),
            "source_model": record.get("source_model"),
        }
    )


def _plain_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _signed_int64(value: object) -> bool:
    return _plain_int(value) and -(2**63) <= value <= 2**63 - 1


def _validate_command(command: EventCommand) -> dict[str, Any]:
    if not isinstance(command.workspace_id, str) or not command.workspace_id.startswith(
        "ws_"
    ):
        raise ValueError("workspace_id must be an opaque ws_ identifier")
    if command.stream_kind not in _STREAM_KINDS:
        raise ValueError("unsupported event stream kind")
    if not isinstance(command.stream_id, str) or not command.stream_id:
        raise ValueError("stream_id must be non-empty")
    if (
        not isinstance(command.event_type, str)
        or not 3 <= len(command.event_type) <= 80
    ):
        raise ValueError("event_type length is invalid")
    if command.actor_type not in _ACTOR_TYPES:
        raise ValueError("unsupported actor type")
    for name, value in (
        ("occurred_at_us", command.occurred_at_us),
        ("recorded_at_us", command.recorded_at_us),
    ):
        if not _signed_int64(value):
            raise ValueError(f"{name} must be a signed 64-bit integer")
    if not _plain_int(command.event_schema_version) or command.event_schema_version < 1:
        raise ValueError("event_schema_version must be positive")
    if command.expected_stream_version is not None and (
        not _plain_int(command.expected_stream_version)
        or command.expected_stream_version < 1
    ):
        raise ValueError("expected_stream_version must be positive")
    payload = normalize_canonical(command.payload)
    if not isinstance(payload, dict):
        raise ValueError("event payload must be an object")
    return payload


class EventStore:
    """Synchronous SQLite canonical append and projection repository.

    The caller owns commit/rollback.  Each invocation uses a savepoint, ensuring
    an event insert and its typed projection either both survive or neither does.
    """

    def __init__(self, connection: Any, *, assume_transaction: bool = False) -> None:
        self.connection = connection
        self.assume_transaction = assume_transaction
        self._savepoint_number = 0

    def append_and_project(self, command: EventCommand) -> AppendedEvent:
        payload = _validate_command(command)
        if not self.assume_transaction and not getattr(
            self.connection, "in_transaction", False
        ):
            self.connection.execute("BEGIN IMMEDIATE")
        self._savepoint_number += 1
        savepoint = f"v7_append_{self._savepoint_number}"
        self.connection.execute(f"SAVEPOINT {savepoint}")
        try:
            result = self._append(command, payload)
            self.connection.execute(f"RELEASE SAVEPOINT {savepoint}")
            return result
        except Exception:
            self.connection.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
            self.connection.execute(f"RELEASE SAVEPOINT {savepoint}")
            raise

    def _append(self, command: EventCommand, payload: dict[str, Any]) -> AppendedEvent:
        head = self.connection.execute(
            """
            SELECT stream_version, event_hash FROM memory_events
            WHERE workspace_id=? AND stream_id=?
            ORDER BY stream_version DESC LIMIT 1
            """,
            (command.workspace_id, command.stream_id),
        ).fetchone()
        head_version = int(head[0]) if head is not None else 0
        if command.expected_stream_version is None:
            stream_version = head_version + 1
        else:
            stream_version = command.expected_stream_version

        if stream_version == 1:
            previous_hash = None
        else:
            previous = self.connection.execute(
                """
                SELECT event_hash FROM memory_events
                WHERE workspace_id=? AND stream_id=? AND stream_version=?
                """,
                (command.workspace_id, command.stream_id, stream_version - 1),
            ).fetchone()
            if previous is None:
                raise EventStreamConflict("event stream would contain a gap")
            previous_hash = str(previous[0])

        payload_text = canonical_json_bytes(payload).decode("utf-8")
        payload_hash = hashlib.sha256(payload_text.encode("utf-8")).hexdigest()
        columns = {
            "actor_id": command.actor_id,
            "actor_type": command.actor_type,
            "causation_event_id": command.causation_event_id,
            "correlation_id": command.correlation_id,
            "event_schema_version": command.event_schema_version,
            "event_type": command.event_type,
            "occurred_at_us": command.occurred_at_us,
            "payload_hash": payload_hash,
            "previous_event_hash": previous_hash,
            "recorded_at_us": command.recorded_at_us,
            "stream_id": command.stream_id,
            "stream_kind": command.stream_kind,
            "stream_version": stream_version,
            "workspace_id": command.workspace_id,
        }
        event_hash = event_hash_for(columns)
        event_id = event_id_for_hash(event_hash)
        values = (
            event_id,
            command.workspace_id,
            command.stream_id,
            command.stream_kind,
            stream_version,
            command.event_type,
            command.event_schema_version,
            command.occurred_at_us,
            command.recorded_at_us,
            command.actor_type,
            command.actor_id,
            command.causation_event_id,
            command.correlation_id,
            payload_text,
            payload_hash,
            previous_hash,
            event_hash,
        )
        existing = self.connection.execute(
            """
            SELECT event_id, workspace_id, stream_id, stream_kind, stream_version,
                   event_type, event_schema_version, occurred_at_us, recorded_at_us,
                   actor_type, actor_id, causation_event_id, correlation_id,
                   payload_json, payload_hash, previous_event_hash, event_hash
            FROM memory_events
            WHERE workspace_id=? AND stream_id=? AND stream_version=?
            """,
            (command.workspace_id, command.stream_id, stream_version),
        ).fetchone()
        if existing is not None:
            if tuple(existing) != values:
                raise EventStreamConflict(
                    "different event already occupies stream version"
                )
            return AppendedEvent(
                event_id, event_hash, payload_hash, stream_version, previous_hash
            )
        if stream_version != head_version + 1:
            raise EventStreamConflict("expected version is not the current stream head")
        try:
            self.connection.execute(
                """
                INSERT INTO memory_events (
                    event_id, workspace_id, stream_id, stream_kind, stream_version,
                    event_type, event_schema_version, occurred_at_us, recorded_at_us,
                    actor_type, actor_id, causation_event_id, correlation_id,
                    payload_json, payload_hash, previous_event_hash, event_hash
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                values,
            )
        except Exception as exc:
            raise EventStreamConflict(
                "event identity or stream version is occupied"
            ) from exc
        self._project(command, payload, event_id, stream_version)
        self._invalidate_retrieval_projections(command, event_id)
        return AppendedEvent(
            event_id, event_hash, payload_hash, stream_version, previous_hash
        )

    def _invalidate_retrieval_projections(
        self, command: EventCommand, event_id: str
    ) -> None:
        required = {
            "background_jobs",
            "projection_manifests",
            "retrieval_documents",
        }
        available = {
            str(row[0])
            for row in _bounded_cursor_rows(
                self.connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' "
                    "AND name IN ('background_jobs','projection_manifests',"
                    "'retrieval_documents')"
                )
            )
        }
        if available != required:
            return
        invalidated_projection_names = (
            "dense",
            "graph",
            "lexical",
            "outcome",
            "procedure",
            "temporal",
        )
        placeholders = ",".join("?" for _ in invalidated_projection_names)
        active_manifests = self.connection.execute(
            "SELECT manifest_id,projection_name,details_json "
            "FROM projection_manifests "
            "WHERE workspace_id=? AND status='active' "
            f"AND projection_name IN ({placeholders})",
            (command.workspace_id, *invalidated_projection_names),
        ).fetchall()
        if not active_manifests and command.stream_kind != "memory":
            return
        lexical_delta_applied = False
        if command.stream_kind == "memory" and any(
            str(row[1]) == "lexical" for row in active_manifests
        ):
            from .retrieval.projections import LexicalProjectionBuilder

            lexical_delta_applied = LexicalProjectionBuilder(
                self.connection
            ).apply_active_delta(
                command.workspace_id,
                command.stream_id,
            )
        for manifest_id, _projection_name, details_text in active_manifests:
            try:
                details = json.loads(str(details_text))
            except (TypeError, ValueError, RecursionError) as exc:
                raise CanonicalEncodingError(
                    "active retrieval manifest details are invalid"
                ) from exc
            if not isinstance(details, Mapping):
                raise CanonicalEncodingError(
                    "active retrieval manifest details are invalid"
                )
            stale_details = dict(details)
            stale_details["rebuild_required_at_us"] = command.recorded_at_us
            stale_details["rebuild_required_event_id"] = event_id
            self.connection.execute(
                "UPDATE projection_manifests SET details_json=? "
                "WHERE manifest_id=? AND status='active'",
                (
                    canonical_json_bytes(stale_details).decode("utf-8"),
                    manifest_id,
                ),
            )
        projection_names = {str(row[1]) for row in active_manifests}
        if command.stream_kind == "memory":
            # Lexical retrieval is core.  A cold or incompatible workspace must
            # still receive a durable immediate rebuild; only a successfully
            # applied active-generation delta makes delayed coalescing safe.
            projection_names.add("lexical")
        for projection_name in sorted(projection_names):
            from .retrieval.job_queue import enqueue_projection_rebuild

            enqueue_projection_rebuild(
                self.connection,
                workspace_id=command.workspace_id,
                projection_name=projection_name,
                source_event_id=event_id,
                recorded_at_us=command.recorded_at_us,
                available_at_us=(
                    min(
                        command.recorded_at_us + _PROJECTION_REBUILD_GRACE_US,
                        9_223_372_036_854_775_807,
                    )
                    if lexical_delta_applied
                    else command.recorded_at_us
                ),
            )

    def _project(
        self,
        command: EventCommand,
        payload: dict[str, Any],
        event_id: str,
        stream_version: int,
    ) -> None:
        if command.stream_kind == "memory":
            self._project_memory(command, payload, event_id, stream_version)
            return
        if command.stream_kind == "fact":
            self._project_fact(command, payload, event_id, stream_version)
            return
        if command.stream_kind == "relationship":
            self._project_relationship(command, payload, event_id, stream_version)
            return
        raise ValueError("unsupported stream kind")

    def _project_memory(
        self,
        command: EventCommand,
        payload: dict[str, Any],
        event_id: str,
        stream_version: int,
    ) -> None:
        record = payload.get("record")
        if not isinstance(record, dict) or not _RECORD_FIELDS.issuperset(record):
            raise ValueError("memory event requires a supported record object")
        record_type = record.get("record_type")
        legacy_type = record.get("legacy_type")
        if record_type not in _RECORD_TYPES:
            raise ValueError("unsupported memory record type")
        if (record_type == "legacy") != (legacy_type is not None):
            raise ValueError("legacy_type must be present exactly for legacy records")
        if not isinstance(record.get("content"), str):
            raise ValueError("memory content must be a string")
        for name in (
            "rationale",
            "file_path",
            "file_path_relative",
            "keywords",
            "outcome",
            "source_client",
            "source_model",
        ):
            value = record.get(name)
            if value is not None and not isinstance(value, str):
                raise ValueError(f"{name} must be text or null")
        context = record.get("context", {})
        tags = record.get("tags", [])
        if not isinstance(context, dict) or not isinstance(tags, list):
            raise ValueError("memory context/tags have invalid shape")
        for name in ("is_permanent", "pinned", "archived"):
            if not isinstance(record.get(name, False), bool):
                raise ValueError(f"{name} must be boolean")
        worked = record.get("worked")
        if worked is not None and not isinstance(worked, bool):
            raise ValueError("worked must be boolean or null")
        recall_count = record.get("recall_count", 0)
        if not _plain_int(recall_count) or recall_count < 0:
            raise ValueError("recall_count must be non-negative")
        for name in ("surprise_score", "importance_score"):
            value = record.get(name)
            if value is not None and (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(float(value))
                or not 0.0 <= float(value) <= 1.0
            ):
                raise ValueError(f"{name} must be between zero and one")
        deleted_at_us = record.get("deleted_at_us")
        if deleted_at_us is not None and not _signed_int64(deleted_at_us):
            raise ValueError("deleted_at_us must be a signed 64-bit integer or null")
        content_hash = memory_content_hash(record)
        state_hash = memory_state_hash(record)
        existing = self.connection.execute(
            "SELECT created_at_us FROM memory_records WHERE record_id=?",
            (command.stream_id,),
        ).fetchone()
        created_at = (
            int(existing[0]) if existing is not None else command.occurred_at_us
        )
        values = (
            command.stream_id,
            command.workspace_id,
            record_type,
            legacy_type,
            record["content"],
            content_hash,
            record.get("rationale"),
            canonical_json_bytes(context).decode("utf-8"),
            canonical_json_bytes(tags).decode("utf-8"),
            record.get("file_path"),
            record.get("file_path_relative"),
            record.get("keywords"),
            int(record.get("is_permanent", False)),
            int(record.get("pinned", False)),
            int(record.get("archived", False)),
            record.get("outcome"),
            None if worked is None else int(worked),
            recall_count,
            record.get("surprise_score"),
            record.get("importance_score"),
            record.get("source_client"),
            record.get("source_model"),
            stream_version,
            event_id,
            created_at,
            command.recorded_at_us,
            record.get("deleted_at_us"),
            state_hash,
        )
        self.connection.execute(
            """
            INSERT INTO memory_records (
                record_id, workspace_id, record_type, legacy_type, content,
                content_hash, rationale, context_json, tags_json, file_path,
                file_path_relative, keywords, is_permanent, pinned, archived,
                outcome, worked, recall_count, surprise_score, importance_score,
                source_client, source_model, stream_version, source_event_id,
                created_at_us, updated_at_us, deleted_at_us, state_hash
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(record_id) DO UPDATE SET
                workspace_id=excluded.workspace_id,
                record_type=excluded.record_type,
                legacy_type=excluded.legacy_type,
                content=excluded.content,
                content_hash=excluded.content_hash,
                rationale=excluded.rationale,
                context_json=excluded.context_json,
                tags_json=excluded.tags_json,
                file_path=excluded.file_path,
                file_path_relative=excluded.file_path_relative,
                keywords=excluded.keywords,
                is_permanent=excluded.is_permanent,
                pinned=excluded.pinned,
                archived=excluded.archived,
                outcome=excluded.outcome,
                worked=excluded.worked,
                recall_count=excluded.recall_count,
                surprise_score=excluded.surprise_score,
                importance_score=excluded.importance_score,
                source_client=excluded.source_client,
                source_model=excluded.source_model,
                stream_version=excluded.stream_version,
                source_event_id=excluded.source_event_id,
                updated_at_us=excluded.updated_at_us,
                deleted_at_us=excluded.deleted_at_us,
                state_hash=excluded.state_hash
            """,
            values,
        )

    def _project_fact(self, command, payload, event_id, stream_version) -> None:
        fact = payload.get("fact")
        if not isinstance(fact, dict):
            raise ValueError("fact event requires a fact object")
        predicate = fact.get("predicate")
        object_kind = fact.get("object_kind")
        legacy_type = fact.get("legacy_type")
        if not isinstance(predicate, str) or not 1 <= len(predicate) <= 120:
            raise ValueError("fact predicate length is invalid")
        if object_kind not in {
            "text",
            "number",
            "boolean",
            "json",
            "record_ref",
            "legacy",
        }:
            raise ValueError("fact object kind is invalid")
        if (object_kind == "legacy") != (legacy_type is not None):
            raise ValueError("fact legacy_type is inconsistent")
        confidence = fact.get("confidence", 1.0)
        if (
            isinstance(confidence, bool)
            or not isinstance(confidence, (int, float))
            or not 0 <= confidence <= 1
        ):
            raise ValueError("fact confidence is invalid")
        verification_count = fact.get("verification_count", 0)
        if not _plain_int(verification_count) or verification_count < 0:
            raise ValueError("fact verification_count is invalid")
        is_verified = fact.get("is_verified", False)
        if not isinstance(is_verified, bool):
            raise ValueError("fact is_verified must be boolean")
        evidence = fact.get("evidence", [])
        metadata = fact.get("metadata", {})
        if not isinstance(evidence, list) or not isinstance(metadata, dict):
            raise ValueError("fact evidence/metadata shape is invalid")
        valid_from = fact.get("valid_from_us")
        valid_to = fact.get("valid_to_us")
        if not _signed_int64(valid_from) or (
            valid_to is not None
            and (not _signed_int64(valid_to) or valid_to <= valid_from)
        ):
            raise ValueError("fact valid interval is invalid")
        subject = fact.get("subject_record_id")
        if subject is not None and not isinstance(subject, str):
            raise ValueError("fact subject is invalid")
        content_hash = sha256_json(
            {
                "subject_record_id": subject,
                "predicate": predicate,
                "object_kind": object_kind,
                "object": fact.get("object"),
                "legacy_type": legacy_type,
                "confidence": confidence,
                "verification_count": verification_count,
                "is_verified": is_verified,
                "evidence": evidence,
                "metadata": metadata,
                "valid_from_us": valid_from,
                "valid_to_us": valid_to,
            }
        )
        retracted = command.event_type == "fact.retracted" or valid_to is not None
        if stream_version > 1:
            self.connection.execute(
                """
                UPDATE memory_fact_versions
                SET transaction_to_us=?, retracted_by_event_id=?
                WHERE fact_id=? AND transaction_to_us IS NULL
                """,
                (command.recorded_at_us, event_id, command.stream_id),
            )
        self.connection.execute(
            """
            INSERT INTO memory_fact_versions (
                fact_version_id, fact_id, workspace_id, version,
                subject_record_id, predicate, object_kind, object_json,
                legacy_type, content_hash, confidence, verification_count,
                is_verified, evidence_json, metadata_json, valid_from_us,
                valid_to_us, transaction_from_us, transaction_to_us,
                asserted_by_event_id, retracted_by_event_id
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                deterministic_id(
                    "fact", "fact-version", command.stream_id, stream_version
                ),
                command.stream_id,
                command.workspace_id,
                stream_version,
                subject,
                predicate,
                object_kind,
                canonical_json_bytes(fact.get("object")).decode("utf-8"),
                legacy_type,
                content_hash,
                float(confidence),
                verification_count,
                int(is_verified),
                canonical_json_bytes(evidence).decode("utf-8"),
                canonical_json_bytes(metadata).decode("utf-8"),
                valid_from,
                valid_to,
                command.recorded_at_us,
                None,
                event_id,
                event_id if retracted else None,
            ),
        )

    def _project_relationship(self, command, payload, event_id, stream_version) -> None:
        relation = payload.get("relationship")
        if not isinstance(relation, dict):
            raise ValueError("relationship event requires a relationship object")
        source = relation.get("source_record_id")
        target = relation.get("target_record_id")
        relation_type = relation.get("relationship_type")
        legacy_type = relation.get("legacy_type")
        if not isinstance(source, str) or not isinstance(target, str):
            raise ValueError("relationship endpoints are invalid")
        if source == target and command.actor_type not in {"migration", "import"}:
            raise ValueError("runtime relationships may not be self-edges")
        if relation_type not in {
            "led_to",
            "supersedes",
            "depends_on",
            "conflicts_with",
            "related_to",
            "evidence_for",
            "derived_from",
            "invalidates",
            "legacy",
        }:
            raise ValueError("relationship type is invalid")
        if (relation_type == "legacy") != (legacy_type is not None):
            raise ValueError("relationship legacy_type is inconsistent")
        confidence = relation.get("confidence", 1.0)
        if (
            isinstance(confidence, bool)
            or not isinstance(confidence, (int, float))
            or not 0 <= confidence <= 1
        ):
            raise ValueError("relationship confidence is invalid")
        metadata = relation.get("metadata", {})
        if not isinstance(metadata, dict):
            raise ValueError("relationship metadata must be an object")
        valid_from = relation.get("valid_from_us")
        valid_to = relation.get("valid_to_us")
        if not _signed_int64(valid_from) or (
            valid_to is not None
            and (not _signed_int64(valid_to) or valid_to <= valid_from)
        ):
            raise ValueError("relationship valid interval is invalid")
        content_hash = sha256_json(
            {
                "source_record_id": source,
                "target_record_id": target,
                "relationship_type": relation_type,
                "legacy_type": legacy_type,
                "description": relation.get("description"),
                "confidence": confidence,
                "metadata": metadata,
                "valid_from_us": valid_from,
                "valid_to_us": valid_to,
            }
        )
        removed = command.event_type == "relationship.removed" or valid_to is not None
        if stream_version > 1:
            changed = self.connection.execute(
                """
                UPDATE memory_relationship_versions
                SET transaction_to_us=?, retracted_by_event_id=?
                WHERE relationship_id=? AND transaction_to_us IS NULL
                """,
                (command.recorded_at_us, event_id, command.stream_id),
            ).rowcount
            if changed != 1:
                raise EventStreamConflict("relationship has no single open version")
        self.connection.execute(
            """
            INSERT INTO memory_relationship_versions (
                relationship_version_id, relationship_id, workspace_id, version,
                source_record_id, target_record_id, relationship_type,
                legacy_type, description, confidence, metadata_json,
                content_hash, valid_from_us, valid_to_us, transaction_from_us,
                transaction_to_us, asserted_by_event_id, retracted_by_event_id
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                deterministic_id(
                    "rel", "relationship-version", command.stream_id, stream_version
                ),
                command.stream_id,
                command.workspace_id,
                stream_version,
                source,
                target,
                relation_type,
                legacy_type,
                relation.get("description"),
                float(confidence),
                canonical_json_bytes(metadata).decode("utf-8"),
                content_hash,
                valid_from,
                valid_to,
                command.recorded_at_us,
                None,
                event_id,
                event_id if removed else None,
            ),
        )


_BUNDLE_KEYS = frozenset(
    {"workspace_id", "event_schema_version", "events", "root_hash"}
)
_BUNDLE_EVENT_KEYS = frozenset(
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
_EVENT_SELECT_COLUMNS = (
    "event_id, workspace_id, stream_id, stream_kind, stream_version, "
    "event_type, event_schema_version, occurred_at_us, recorded_at_us, "
    "actor_type, actor_id, causation_event_id, correlation_id, payload_json, "
    "payload_hash, previous_event_hash, event_hash"
)


def _reject_json_constant(value: str) -> None:
    raise CanonicalEncodingError(f"non-finite JSON number is not canonical: {value}")


def _unique_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise CanonicalEncodingError("duplicate JSON object key")
        value[key] = item
    return value


def parse_canonical_json(text: str) -> Any:
    """Parse exact canonical JSON, rejecting duplicates and alternate encodings."""

    if not isinstance(text, str):
        raise CanonicalEncodingError("canonical JSON input must be text")
    try:
        value = json.loads(
            text,
            object_pairs_hook=_unique_json_object,
            parse_constant=_reject_json_constant,
        )
    except (json.JSONDecodeError, TypeError, ValueError, RecursionError) as exc:
        if isinstance(exc, CanonicalEncodingError):
            raise
        raise CanonicalEncodingError("invalid canonical JSON") from exc
    if canonical_json_bytes(value) != text.encode("utf-8"):
        raise CanonicalEncodingError("JSON text is not in canonical form")
    return value


_COMPATIBILITY_SOURCE_KINDS = {
    "memories": "memory",
    "facts": "fact",
    "memory_relationships": "relationship",
}


def _compatibility_id_text(value: Any) -> str | None:
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        return None
    return str(value)


def _payload_legacy_claims(
    payload: Mapping[str, Any], stream_kind: str
) -> set[tuple[str, str]]:
    """Extract every retained-v6 identity claimed by one event payload."""

    claims: set[tuple[str, str]] = set()
    legacy = payload.get("legacy")
    if isinstance(legacy, Mapping):
        source_table = legacy.get("table")
        columns = legacy.get("columns")
        if source_table in _COMPATIBILITY_SOURCE_KINDS and isinstance(columns, list):
            for column in columns:
                if isinstance(column, list) and len(column) == 2 and column[0] == "id":
                    legacy_id = _compatibility_id_text(column[1])
                    if legacy_id is not None:
                        claims.add((str(source_table), legacy_id))

    compatibility = payload.get("compatibility")
    if stream_kind == "memory" and isinstance(compatibility, Mapping):
        legacy_id = _compatibility_id_text(compatibility.get("legacy_memory_id"))
        if legacy_id is not None:
            claims.add(("memories", legacy_id))

    projection_name = {
        "fact": "fact",
        "relationship": "relationship",
    }.get(stream_kind)
    projection = payload.get(projection_name) if projection_name else None
    metadata = projection.get("metadata") if isinstance(projection, Mapping) else None
    metadata_key = {
        "fact": ("facts", "legacy_fact_id"),
        "relationship": (
            "memory_relationships",
            "legacy_relationship_id",
        ),
    }.get(stream_kind)
    if metadata_key and isinstance(metadata, Mapping):
        legacy_id = _compatibility_id_text(metadata.get(metadata_key[1]))
        if legacy_id is not None:
            claims.add((metadata_key[0], legacy_id))
    return claims


def _bounded_cursor_rows(cursor: Any, *, batch_size: int = 1024) -> Iterator[Any]:
    """Yield DB-API rows without assuming cursor iteration or buffering all rows."""

    try:
        while True:
            rows = cursor.fetchmany(batch_size)
            if not rows:
                return
            yield from rows
    finally:
        close = getattr(cursor, "close", None)
        if callable(close):
            close()


def build_live_compatibility_claim_index(
    connection: Any,
    workspace_id: str,
    source_tables: set[str] | frozenset[str] | None = None,
) -> dict[tuple[str, str], str]:
    """Resolve all retained identities to their unique live typed streams.

    The index is built in linear passes so migration validation does not rescan
    the complete event log once per compatibility row.
    """

    if not isinstance(workspace_id, str) or not workspace_id.startswith("ws_"):
        raise CompatibilityStreamError(
            "COMPATIBILITY_STREAM_INVALID", "workspace_id is invalid"
        )
    selected = (
        set(_COMPATIBILITY_SOURCE_KINDS)
        if source_tables is None
        else set(source_tables)
    )
    if not selected or not selected <= set(_COMPATIBILITY_SOURCE_KINDS):
        raise CompatibilityStreamError(
            "COMPATIBILITY_STREAM_INVALID", "source table selection is invalid"
        )
    selected_kinds = {_COMPATIBILITY_SOURCE_KINDS[table] for table in selected}
    candidates: dict[tuple[str, str], set[str]] = {}
    event_streams: dict[str, set[str]] = {kind: set() for kind in selected_kinds}

    schema_cursor = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='legacy_id_map'"
    )
    try:
        has_map = schema_cursor.fetchone()
    finally:
        schema_cursor.close()
    if has_map is not None:
        for row in _bounded_cursor_rows(
            connection.execute(
                "SELECT source_table,legacy_id,target_kind,target_id "
                "FROM legacy_id_map WHERE workspace_id=?",
                (workspace_id,),
            )
        ):
            source_table = str(row[0])
            if source_table not in selected:
                continue
            expected_kind = _COMPATIBILITY_SOURCE_KINDS[source_table]
            if row[2] != expected_kind:
                raise CompatibilityStreamError(
                    "COMPATIBILITY_STREAM_INVALID",
                    "migration mapping has an invalid target kind",
                )
            candidates.setdefault((source_table, str(row[1])), set()).add(str(row[3]))

    for row in _bounded_cursor_rows(
        connection.execute(
            "SELECT stream_id,stream_kind,payload_json FROM memory_events "
            "WHERE workspace_id=? ORDER BY stream_id,stream_version",
            (workspace_id,),
        )
    ):
        stream_kind = str(row[1])
        if stream_kind not in selected_kinds:
            continue
        stream_id = str(row[0])
        event_streams[stream_kind].add(stream_id)
        try:
            payload = parse_canonical_json(str(row[2]))
        except CanonicalEncodingError as exc:
            raise CompatibilityStreamError(
                "COMPATIBILITY_STREAM_INVALID",
                "event provenance payload is not canonical",
            ) from exc
        if not isinstance(payload, Mapping):
            raise CompatibilityStreamError(
                "COMPATIBILITY_STREAM_INVALID", "event payload is not an object"
            )
        for claim in _payload_legacy_claims(payload, stream_kind):
            if claim[0] in selected:
                candidates.setdefault(claim, set()).add(stream_id)

    current_projection: dict[str, dict[str, bool]] = {}
    if "memory" in selected_kinds:
        current_projection["memory"] = {
            str(row[0]): row[1] is None
            for row in _bounded_cursor_rows(
                connection.execute(
                    "SELECT record_id,deleted_at_us FROM memory_records "
                    "WHERE workspace_id=?",
                    (workspace_id,),
                )
            )
        }
    for stream_kind, table, id_column in (
        ("fact", "memory_fact_versions", "fact_id"),
        ("relationship", "memory_relationship_versions", "relationship_id"),
    ):
        if stream_kind not in selected_kinds:
            continue
        status: dict[str, bool] = {}
        for row in _bounded_cursor_rows(
            connection.execute(
                f"SELECT {id_column},valid_to_us FROM {table} "
                "WHERE workspace_id=? AND transaction_to_us IS NULL",
                (workspace_id,),
            )
        ):
            stream_id = str(row[0])
            if stream_id in status:
                raise CompatibilityStreamError(
                    "COMPATIBILITY_STREAM_INVALID",
                    "typed stream has multiple current projections",
                )
            status[stream_id] = row[1] is None
        current_projection[stream_kind] = status

    resolved: dict[tuple[str, str], str] = {}
    for claim, streams in candidates.items():
        stream_kind = _COMPATIBILITY_SOURCE_KINDS[claim[0]]
        projection = current_projection[stream_kind]
        for stream_id in streams:
            if stream_id not in event_streams[stream_kind]:
                raise CompatibilityStreamError(
                    "COMPATIBILITY_STREAM_INVALID",
                    "compatibility claim points to a missing event stream",
                )
            if stream_id not in projection:
                raise CompatibilityStreamError(
                    "COMPATIBILITY_STREAM_INVALID",
                    "compatibility claim has no current typed projection",
                )
        live = {stream_id for stream_id in streams if projection[stream_id]}
        if len(live) > 1:
            raise CompatibilityStreamError(
                "COMPATIBILITY_STREAM_AMBIGUOUS",
                "multiple live event streams claim the retained identity",
            )
        if live:
            resolved[claim] = next(iter(live))
    return resolved


def resolve_compatibility_stream(
    connection: Any,
    workspace_id: str,
    stream_kind: str,
    source_table: str,
    legacy_id: int | str,
) -> str | None:
    """Resolve a retained v6 identity to exactly one canonical v7 stream.

    Native migrations have an indexed ``legacy_id_map``.  Portable event
    bundles intentionally contain only immutable events and projections, so
    the event provenance is the authoritative fallback after restoration.
    """

    if _COMPATIBILITY_SOURCE_KINDS.get(source_table) != stream_kind:
        raise CompatibilityStreamError(
            "COMPATIBILITY_STREAM_INVALID", "source table and stream kind disagree"
        )
    if not isinstance(workspace_id, str) or not workspace_id.startswith("ws_"):
        raise CompatibilityStreamError(
            "COMPATIBILITY_STREAM_INVALID", "workspace_id is invalid"
        )
    legacy_text = _compatibility_id_text(legacy_id)
    if legacy_text is None:
        raise CompatibilityStreamError(
            "COMPATIBILITY_STREAM_INVALID", "legacy identity is invalid"
        )

    index = build_live_compatibility_claim_index(
        connection, workspace_id, {source_table}
    )
    return index.get((source_table, legacy_text))


def _event_root_hash(events: list[Mapping[str, Any]]) -> str:
    digest = hashlib.sha256()
    for event in events:
        event_hash = event.get("event_hash")
        if not isinstance(event_hash, str) or not _SHA256_RE.fullmatch(event_hash):
            raise EventBundleError("INVALID_EVENT_BUNDLE", "event_hash is malformed")
        digest.update(bytes.fromhex(event_hash))
    return digest.hexdigest()


def _row_mapping(cursor: Any, row: Any) -> dict[str, Any]:
    if hasattr(row, "keys"):
        # sqlite3.Row iterates values, so its named columns must be explicit.
        keys = row.keys()
        return {key: row[key] for key in keys}
    return {
        column[0]: value for column, value in zip(cursor.description, row, strict=True)
    }


def export_event_bundle(connection: Any, workspace_id: str) -> dict[str, Any]:
    """Export a deterministic portable bundle from one workspace event log."""

    if not isinstance(workspace_id, str) or not workspace_id.startswith("ws_"):
        raise EventBundleError("INVALID_EVENT_BUNDLE", "workspace_id is invalid")
    cursor = connection.execute(
        f"SELECT {_EVENT_SELECT_COLUMNS} FROM memory_events "
        "WHERE workspace_id=? ORDER BY event_id",
        (workspace_id,),
    )
    events: list[dict[str, Any]] = []
    for raw_row in cursor.fetchall():
        row = _row_mapping(cursor, raw_row)
        try:
            payload = parse_canonical_json(row.pop("payload_json"))
        except CanonicalEncodingError as exc:
            raise EventBundleError(
                "INVALID_EVENT_BUNDLE", "stored event payload is not canonical"
            ) from exc
        row["payload"] = payload
        events.append(row)
    return {
        "workspace_id": workspace_id,
        "event_schema_version": 1,
        "events": events,
        "root_hash": _event_root_hash(events),
    }


def _validated_bundle_commands(
    bundle: Mapping[str, Any], target_workspace_id: str
) -> tuple[list[EventCommand], str]:
    if not isinstance(bundle, Mapping) or set(bundle) != _BUNDLE_KEYS:
        raise EventBundleError("INVALID_EVENT_BUNDLE", "bundle fields are invalid")
    workspace_id = bundle.get("workspace_id")
    if workspace_id != target_workspace_id:
        raise EventBundleError(
            "CROSS_WORKSPACE_IMPORT_UNSUPPORTED",
            "direct event restore requires the same workspace",
        )
    if bundle.get("event_schema_version") != 1:
        raise EventBundleError("INVALID_EVENT_BUNDLE", "event schema is unsupported")
    raw_events = bundle.get("events")
    if not isinstance(raw_events, list):
        raise EventBundleError("INVALID_EVENT_BUNDLE", "events must be an array")
    if any(not isinstance(event, Mapping) for event in raw_events):
        raise EventBundleError("INVALID_EVENT_BUNDLE", "each event must be an object")
    events = [dict(event) for event in raw_events]
    if any(set(event) != _BUNDLE_EVENT_KEYS for event in events):
        raise EventBundleError("INVALID_EVENT_BUNDLE", "event fields are invalid")
    event_ids = [event["event_id"] for event in events]
    if any(
        not isinstance(event_id, str) or not re.fullmatch(r"evt_[0-9a-f]{64}", event_id)
        for event_id in event_ids
    ):
        raise EventBundleError("INVALID_EVENT_BUNDLE", "event_id is malformed")
    if event_ids != sorted(event_ids):
        raise EventBundleError(
            "INVALID_EVENT_BUNDLE", "events are not ordered by event_id"
        )
    if len(set(event_ids)) != len(events):
        raise EventBundleError("INVALID_EVENT_BUNDLE", "event_id is duplicated")

    commands: list[EventCommand] = []
    by_stream: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for event in events:
        if event.get("workspace_id") != workspace_id:
            raise EventBundleError("INVALID_EVENT_BUNDLE", "event workspace differs")
        payload = event.get("payload")
        try:
            payload_hash = sha256_json(payload)
        except (CanonicalEncodingError, RecursionError) as exc:
            raise EventBundleError(
                "INVALID_EVENT_BUNDLE", "payload is invalid"
            ) from exc
        if payload_hash != event.get("payload_hash"):
            raise EventBundleError("INVALID_EVENT_BUNDLE", "payload hash differs")
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
        try:
            calculated_hash = event_hash_for(envelope)
        except (CanonicalEncodingError, ValueError, RecursionError) as exc:
            raise EventBundleError(
                "INVALID_EVENT_BUNDLE", "event envelope is invalid"
            ) from exc
        if calculated_hash != event.get("event_hash"):
            raise EventBundleError("INVALID_EVENT_BUNDLE", "event hash differs")
        if event.get("event_id") != event_id_for_hash(calculated_hash):
            raise EventBundleError("INVALID_EVENT_BUNDLE", "event identity differs")
        command = EventCommand(
            workspace_id=workspace_id,
            stream_id=event["stream_id"],
            stream_kind=event["stream_kind"],
            event_type=event["event_type"],
            occurred_at_us=event["occurred_at_us"],
            recorded_at_us=event["recorded_at_us"],
            actor_type=event["actor_type"],
            actor_id=event["actor_id"],
            causation_event_id=event["causation_event_id"],
            correlation_id=event["correlation_id"],
            event_schema_version=event["event_schema_version"],
            expected_stream_version=event["stream_version"],
            payload=payload,
        )
        try:
            _validate_command(command)
        except (CanonicalEncodingError, ValueError, RecursionError) as exc:
            raise EventBundleError(
                "INVALID_EVENT_BUNDLE", "event command is invalid"
            ) from exc
        commands.append(command)
        by_stream.setdefault((workspace_id, event["stream_id"]), []).append(event)

    for stream_events in by_stream.values():
        ordered = sorted(stream_events, key=lambda event: event["stream_version"])
        for expected_version, event in enumerate(ordered, 1):
            if event["stream_version"] != expected_version:
                raise EventBundleError("INVALID_EVENT_BUNDLE", "event stream has a gap")
            expected_previous = (
                None
                if expected_version == 1
                else ordered[-2 + expected_version]["event_hash"]
            )
            if event["previous_event_hash"] != expected_previous:
                raise EventBundleError("INVALID_EVENT_BUNDLE", "event chain differs")

    calculated_root = _event_root_hash(events)
    if bundle.get("root_hash") != calculated_root:
        raise EventBundleError("INVALID_EVENT_BUNDLE", "event root hash differs")

    # Export order is event_id, while insertion must respect both stream chains
    # and cross-stream causation foreign keys. Produce a deterministic
    # topological order only after the entire bundle has validated.
    command_by_id = {
        event["event_id"]: command
        for event, command in zip(events, commands, strict=True)
    }
    first_memory_event: dict[str, str] = {}
    for event in events:
        if event["stream_kind"] != "memory":
            continue
        current = first_memory_event.get(event["stream_id"])
        if (
            current is None
            or event["stream_version"] < command_by_id[current].expected_stream_version
        ):
            first_memory_event[event["stream_id"]] = event["event_id"]
    event_id_by_hash = {event["event_hash"]: event["event_id"] for event in events}
    dependencies: dict[str, set[str]] = {}
    for event in events:
        required: set[str] = set()
        previous_hash = event["previous_event_hash"]
        if previous_hash is not None:
            previous_id = event_id_by_hash.get(previous_hash)
            if previous_id is None:
                raise EventBundleError(
                    "INVALID_EVENT_BUNDLE", "previous event is absent from bundle"
                )
            required.add(previous_id)
        causation_id = event["causation_event_id"]
        if causation_id is not None:
            if causation_id not in command_by_id:
                raise EventBundleError(
                    "INVALID_EVENT_BUNDLE", "causation event is absent from bundle"
                )
            required.add(causation_id)
        command = command_by_id[event["event_id"]]
        projection = None
        if command.stream_kind == "fact":
            projection = command.payload.get("fact")
            references = (
                [projection.get("subject_record_id")]
                if isinstance(projection, Mapping)
                else []
            )
        elif command.stream_kind == "relationship":
            projection = command.payload.get("relationship")
            references = (
                [
                    projection.get("source_record_id"),
                    projection.get("target_record_id"),
                ]
                if isinstance(projection, Mapping)
                else []
            )
        else:
            references = []
        for reference in references:
            dependency = (
                first_memory_event.get(reference)
                if isinstance(reference, str)
                else None
            )
            if dependency is not None:
                required.add(dependency)
        dependencies[event["event_id"]] = required

    ordered_commands: list[EventCommand] = []
    completed: set[str] = set()
    while len(completed) < len(events):
        ready = sorted(
            [
                event_id
                for event_id, required in dependencies.items()
                if event_id not in completed and required <= completed
            ],
            key=lambda event_id: (
                command_by_id[event_id].recorded_at_us,
                event_id,
            ),
        )
        if not ready:
            raise EventBundleError(
                "INVALID_EVENT_BUNDLE", "event dependencies are cyclic"
            )
        for event_id in ready:
            ordered_commands.append(command_by_id[event_id])
            completed.add(event_id)
    return ordered_commands, calculated_root


def import_event_bundle(
    connection: Any,
    bundle: Mapping[str, Any],
    target_workspace_id: str,
    *,
    assume_transaction: bool = False,
) -> EventBundleImportResult:
    """Validate a bundle completely, then restore it atomically and idempotently."""

    commands, root_hash = _validated_bundle_commands(bundle, target_workspace_id)
    if not assume_transaction and not getattr(connection, "in_transaction", False):
        connection.execute("BEGIN IMMEDIATE")
    connection.execute("SAVEPOINT v7_bundle_import")
    imported = 0
    existing = 0
    store = EventStore(connection, assume_transaction=True)
    try:
        for command in commands:
            present = connection.execute(
                "SELECT 1 FROM memory_events WHERE workspace_id=? AND stream_id=? "
                "AND stream_version=?",
                (
                    command.workspace_id,
                    command.stream_id,
                    command.expected_stream_version,
                ),
            ).fetchone()
            store.append_and_project(command)
            if present is None:
                imported += 1
            else:
                existing += 1
        connection.execute("RELEASE SAVEPOINT v7_bundle_import")
    except Exception:
        connection.execute("ROLLBACK TO SAVEPOINT v7_bundle_import")
        connection.execute("RELEASE SAVEPOINT v7_bundle_import")
        raise
    return EventBundleImportResult(imported, existing, root_hash)


_GOVERNANCE_STREAM_PREFIXES = {
    "rule": ("rule_", 69),
    "trigger": ("trg_", 68),
    "active_context": ("act_", 68),
}
_GOVERNANCE_EVENT_TYPES = {
    "rule": frozenset({"rule.created", "rule.updated"}),
    "trigger": frozenset({"context_trigger.created", "context_trigger.deleted"}),
}
_GOVERNANCE_RECORD_TYPES = frozenset(
    {"decision", "pattern", "warning", "learning", "procedure", "observation"}
)
_RULE_STATE_FIELDS = frozenset(
    {
        "rule_id",
        "trigger",
        "must_do",
        "must_not",
        "ask_first",
        "warnings",
        "priority",
        "enabled",
        "created_at_us",
        "updated_at_us",
    }
)
_TRIGGER_STATE_FIELDS = frozenset(
    {
        "trigger_id",
        "trigger_type",
        "pattern",
        "recall_query",
        "categories",
        "enabled",
        "priority",
        "created_at_us",
        "updated_at_us",
        "deleted_at_us",
    }
)


def _opaque_governance_id(value: object, prefix: str, length: int) -> bool:
    return (
        isinstance(value, str)
        and len(value) == length
        and value.startswith(prefix)
        and all(character in "0123456789abcdef" for character in value[len(prefix) :])
    )


def _governance_timestamp(value: object, name: str) -> int:
    if not _plain_int(value) or value < 0 or value > 9_223_372_036_854_775_807:
        raise ValueError(f"{name} must be a non-negative signed integer")
    return value


def _governance_text_list(
    payload: Mapping[str, Any], name: str, *, maximum: int
) -> list[str]:
    value = payload.get(name)
    if (
        not isinstance(value, list)
        or len(value) > maximum
        or any(
            not isinstance(item, str) or not 1 <= len(item) <= 2_000 for item in value
        )
    ):
        raise ValueError(f"{name} must be a bounded text list")
    return value


def _validate_governance_command(
    command: GovernanceEventCommand,
) -> dict[str, Any]:
    if not _opaque_governance_id(command.workspace_id, "ws_", 27):
        raise ValueError("workspace_id must be an opaque v7 workspace identifier")
    stream_shape = _GOVERNANCE_STREAM_PREFIXES.get(command.stream_kind)
    if stream_shape is None or not _opaque_governance_id(
        command.stream_id, *stream_shape
    ):
        raise ValueError("stream_id does not match governance stream kind")
    if command.stream_kind == "active_context":
        valid_event_type = (
            isinstance(command.event_type, str)
            and command.event_type.startswith("active_context.")
            and 3 <= len(command.event_type) <= 80
        )
    else:
        valid_event_type = (
            command.event_type in _GOVERNANCE_EVENT_TYPES[command.stream_kind]
        )
    if not valid_event_type:
        raise ValueError("unsupported governance event type")
    if command.actor_type not in _ACTOR_TYPES:
        raise ValueError("unsupported actor type")
    if command.actor_id is not None and (
        not isinstance(command.actor_id, str) or not 1 <= len(command.actor_id) <= 200
    ):
        raise ValueError("actor_id length is invalid")
    if command.correlation_id is not None and (
        not isinstance(command.correlation_id, str)
        or not 1 <= len(command.correlation_id) <= 200
    ):
        raise ValueError("correlation_id length is invalid")
    if command.causation_event_id is not None and not _opaque_governance_id(
        command.causation_event_id, "evt_", 68
    ):
        raise ValueError("causation_event_id must be an opaque event identifier")
    _governance_timestamp(command.occurred_at_us, "occurred_at_us")
    _governance_timestamp(command.recorded_at_us, "recorded_at_us")
    if not _plain_int(command.event_schema_version) or command.event_schema_version < 1:
        raise ValueError("event_schema_version must be positive")
    if command.expected_stream_version is not None and (
        not _plain_int(command.expected_stream_version)
        or command.expected_stream_version < 1
    ):
        raise ValueError("expected_stream_version must be positive")
    payload = normalize_canonical(command.payload)
    if not isinstance(payload, dict):
        raise ValueError("event payload must be an object")
    if command.stream_kind == "rule":
        _validate_governance_rule_state(command, payload)
    elif command.stream_kind == "trigger":
        _validate_governance_trigger_state(command, payload)
    return payload


def _validate_governance_rule_state(
    command: GovernanceEventCommand, payload: Mapping[str, Any]
) -> None:
    if (
        set(payload) != _RULE_STATE_FIELDS
        or payload.get("rule_id") != command.stream_id
    ):
        raise ValueError("rule event must contain one complete canonical state")
    trigger = payload.get("trigger")
    if not isinstance(trigger, str) or not 1 <= len(trigger) <= 2_000:
        raise ValueError("rule trigger is invalid")
    for name in ("must_do", "must_not", "ask_first", "warnings"):
        _governance_text_list(payload, name, maximum=50)
    priority = payload.get("priority")
    if not _plain_int(priority) or not -1_000 <= priority <= 1_000:
        raise ValueError("rule priority is invalid")
    if not isinstance(payload.get("enabled"), bool):
        raise ValueError("rule enabled state is invalid")
    created = _governance_timestamp(payload.get("created_at_us"), "created_at_us")
    updated = _governance_timestamp(payload.get("updated_at_us"), "updated_at_us")
    if updated < created:
        raise ValueError("rule updated_at_us precedes creation")


def _validate_governance_trigger_state(
    command: GovernanceEventCommand, payload: Mapping[str, Any]
) -> None:
    if (
        set(payload) != _TRIGGER_STATE_FIELDS
        or payload.get("trigger_id") != command.stream_id
    ):
        raise ValueError("trigger event must contain one complete canonical state")
    if payload.get("trigger_type") not in {"file", "tag", "entity"}:
        raise ValueError("trigger type is invalid")
    for name, maximum in (("pattern", 2_000), ("recall_query", 2_000)):
        value = payload.get(name)
        if not isinstance(value, str) or not 1 <= len(value) <= maximum:
            raise ValueError(f"{name} is invalid")
    categories = _governance_text_list(payload, "categories", maximum=32)
    if not set(categories) <= _GOVERNANCE_RECORD_TYPES:
        raise ValueError("trigger categories are invalid")
    if not isinstance(payload.get("enabled"), bool):
        raise ValueError("trigger enabled state is invalid")
    if not _signed_int64(payload.get("priority")):
        raise ValueError("trigger priority is invalid")
    created = _governance_timestamp(payload.get("created_at_us"), "created_at_us")
    updated = _governance_timestamp(payload.get("updated_at_us"), "updated_at_us")
    deleted = payload.get("deleted_at_us")
    if updated < created:
        raise ValueError("trigger updated_at_us precedes creation")
    if deleted is not None:
        deleted = _governance_timestamp(deleted, "deleted_at_us")
        if deleted < created:
            raise ValueError("trigger deletion precedes creation")
    if command.event_type == "context_trigger.created" and deleted is not None:
        raise ValueError("created trigger cannot be deleted")
    if command.event_type == "context_trigger.deleted" and deleted is None:
        raise ValueError("deleted trigger requires deleted_at_us")


class GovernanceEventStore:
    """Atomic append and canonical projection for v7 governance streams."""

    def __init__(self, connection: Any, *, assume_transaction: bool = False) -> None:
        self.connection = connection
        self.assume_transaction = assume_transaction
        self._savepoint_number = 0

    def append_and_project(self, command: GovernanceEventCommand) -> AppendedEvent:
        payload = _validate_governance_command(command)
        if not self.assume_transaction and not getattr(
            self.connection, "in_transaction", False
        ):
            self.connection.execute("BEGIN IMMEDIATE")
        self._savepoint_number += 1
        savepoint = f"v7_governance_append_{self._savepoint_number}"
        self.connection.execute(f"SAVEPOINT {savepoint}")
        try:
            result = self._append(command, payload)
            self.connection.execute(f"RELEASE SAVEPOINT {savepoint}")
            return result
        except Exception:
            self.connection.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
            self.connection.execute(f"RELEASE SAVEPOINT {savepoint}")
            raise

    def _append(
        self, command: GovernanceEventCommand, payload: dict[str, Any]
    ) -> AppendedEvent:
        head = self.connection.execute(
            "SELECT stream_version,event_hash FROM governance_events "
            "WHERE workspace_id=? AND stream_id=? "
            "ORDER BY stream_version DESC LIMIT 1",
            (command.workspace_id, command.stream_id),
        ).fetchone()
        head_version = 0 if head is None else int(head[0])
        stream_version = (
            head_version + 1
            if command.expected_stream_version is None
            else command.expected_stream_version
        )
        if stream_version == 1:
            previous_hash = None
        else:
            previous = self.connection.execute(
                "SELECT event_hash FROM governance_events "
                "WHERE workspace_id=? AND stream_id=? AND stream_version=?",
                (command.workspace_id, command.stream_id, stream_version - 1),
            ).fetchone()
            if previous is None:
                raise EventStreamConflict("governance stream would contain a gap")
            previous_hash = str(previous[0])
        payload_text = canonical_json_bytes(payload).decode("utf-8")
        payload_hash = hashlib.sha256(payload_text.encode("utf-8")).hexdigest()
        columns = {
            "actor_id": command.actor_id,
            "actor_type": command.actor_type,
            "causation_event_id": command.causation_event_id,
            "correlation_id": command.correlation_id,
            "event_schema_version": command.event_schema_version,
            "event_type": command.event_type,
            "occurred_at_us": command.occurred_at_us,
            "payload_hash": payload_hash,
            "previous_event_hash": previous_hash,
            "recorded_at_us": command.recorded_at_us,
            "stream_id": command.stream_id,
            "stream_kind": command.stream_kind,
            "stream_version": stream_version,
            "workspace_id": command.workspace_id,
        }
        event_hash = event_hash_for(columns)
        event_id = event_id_for_hash(event_hash)
        values = (
            event_id,
            command.workspace_id,
            command.stream_id,
            command.stream_kind,
            stream_version,
            command.event_type,
            command.event_schema_version,
            command.occurred_at_us,
            command.recorded_at_us,
            command.actor_type,
            command.actor_id,
            command.causation_event_id,
            command.correlation_id,
            payload_text,
            payload_hash,
            previous_hash,
            event_hash,
        )
        existing = self.connection.execute(
            "SELECT event_id,workspace_id,stream_id,stream_kind,stream_version,"
            "event_type,event_schema_version,occurred_at_us,recorded_at_us,"
            "actor_type,actor_id,causation_event_id,correlation_id,payload_json,"
            "payload_hash,previous_event_hash,event_hash FROM governance_events "
            "WHERE workspace_id=? AND stream_id=? AND stream_version=?",
            (command.workspace_id, command.stream_id, stream_version),
        ).fetchone()
        if existing is not None:
            if tuple(existing) != values:
                raise EventStreamConflict(
                    "different event already occupies governance stream version"
                )
            return AppendedEvent(
                event_id,
                event_hash,
                payload_hash,
                stream_version,
                previous_hash,
            )
        if stream_version != head_version + 1:
            raise EventStreamConflict(
                "expected version is not the current governance stream head"
            )
        try:
            self.connection.execute(
                "INSERT INTO governance_events "
                "(event_id,workspace_id,stream_id,stream_kind,stream_version,"
                "event_type,event_schema_version,occurred_at_us,recorded_at_us,"
                "actor_type,actor_id,causation_event_id,correlation_id,payload_json,"
                "payload_hash,previous_event_hash,event_hash) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                values,
            )
        except Exception as exc:
            raise EventStreamConflict(
                "governance event identity or stream version is occupied"
            ) from exc
        self._project(command, payload, event_id, stream_version)
        return AppendedEvent(
            event_id,
            event_hash,
            payload_hash,
            stream_version,
            previous_hash,
        )

    def _project(
        self,
        command: GovernanceEventCommand,
        payload: dict[str, Any],
        event_id: str,
        stream_version: int,
    ) -> None:
        if command.stream_kind == "rule":
            self._project_rule(command, payload, event_id, stream_version)
        elif command.stream_kind == "trigger":
            self._project_trigger(command, payload, event_id, stream_version)

    def _project_rule(
        self,
        command: GovernanceEventCommand,
        payload: dict[str, Any],
        event_id: str,
        stream_version: int,
    ) -> None:
        values = (
            payload["trigger"],
            canonical_json_bytes(payload["must_do"]).decode("utf-8"),
            canonical_json_bytes(payload["must_not"]).decode("utf-8"),
            canonical_json_bytes(payload["ask_first"]).decode("utf-8"),
            canonical_json_bytes(payload["warnings"]).decode("utf-8"),
            payload["priority"],
            int(payload["enabled"]),
            stream_version,
            event_id,
            payload["updated_at_us"],
            sha256_json(payload),
        )
        existing = self.connection.execute(
            "SELECT workspace_id,stream_version,created_at_us "
            "FROM governance_rules WHERE rule_id=?",
            (command.stream_id,),
        ).fetchone()
        if command.event_type == "rule.created":
            if existing is not None or stream_version != 1:
                raise EventStreamConflict("rule creation projection already exists")
            self.connection.execute(
                "INSERT INTO governance_rules "
                "(rule_id,workspace_id,trigger,must_do_json,must_not_json,"
                "ask_first_json,warnings_json,priority,enabled,stream_version,"
                "source_event_id,created_at_us,updated_at_us,state_hash) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    command.stream_id,
                    command.workspace_id,
                    *values[:9],
                    payload["created_at_us"],
                    *values[9:],
                ),
            )
            return
        if (
            existing is None
            or str(existing[0]) != command.workspace_id
            or int(existing[1]) + 1 != stream_version
            or int(existing[2]) != payload["created_at_us"]
        ):
            raise EventStreamConflict("rule projection head does not match")
        cursor = self.connection.execute(
            "UPDATE governance_rules SET trigger=?,must_do_json=?,must_not_json=?,"
            "ask_first_json=?,warnings_json=?,priority=?,enabled=?,stream_version=?,"
            "source_event_id=?,updated_at_us=?,state_hash=? WHERE rule_id=? "
            "AND workspace_id=? AND stream_version=?",
            (
                *values,
                command.stream_id,
                command.workspace_id,
                stream_version - 1,
            ),
        )
        if cursor.rowcount != 1:
            raise EventStreamConflict("rule projection update was not singular")

    def _project_trigger(
        self,
        command: GovernanceEventCommand,
        payload: dict[str, Any],
        event_id: str,
        stream_version: int,
    ) -> None:
        values = (
            payload["trigger_type"],
            payload["pattern"],
            payload["recall_query"],
            canonical_json_bytes(payload["categories"]).decode("utf-8"),
            int(payload["enabled"]),
            payload["priority"],
            stream_version,
            event_id,
            payload["updated_at_us"],
            payload["deleted_at_us"],
            sha256_json(payload),
        )
        existing = self.connection.execute(
            "SELECT workspace_id,stream_version,created_at_us,deleted_at_us "
            "FROM governance_context_triggers WHERE trigger_id=?",
            (command.stream_id,),
        ).fetchone()
        if command.event_type == "context_trigger.created":
            if existing is not None or stream_version != 1:
                raise EventStreamConflict("trigger creation projection already exists")
            self.connection.execute(
                "INSERT INTO governance_context_triggers "
                "(trigger_id,workspace_id,trigger_type,pattern,recall_query,"
                "categories_json,enabled,priority,stream_version,source_event_id,"
                "created_at_us,updated_at_us,deleted_at_us,state_hash) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    command.stream_id,
                    command.workspace_id,
                    *values[:8],
                    payload["created_at_us"],
                    *values[8:],
                ),
            )
            return
        if (
            existing is None
            or str(existing[0]) != command.workspace_id
            or int(existing[1]) + 1 != stream_version
            or int(existing[2]) != payload["created_at_us"]
            or existing[3] is not None
        ):
            raise EventStreamConflict("trigger projection head does not match")
        cursor = self.connection.execute(
            "UPDATE governance_context_triggers SET trigger_type=?,pattern=?,"
            "recall_query=?,categories_json=?,enabled=?,priority=?,stream_version=?,"
            "source_event_id=?,updated_at_us=?,deleted_at_us=?,state_hash=? "
            "WHERE trigger_id=? AND workspace_id=? AND stream_version=? "
            "AND deleted_at_us IS NULL",
            (
                *values,
                command.stream_id,
                command.workspace_id,
                stream_version - 1,
            ),
        )
        if cursor.rowcount != 1:
            raise EventStreamConflict("trigger projection update was not singular")


class _SyncExecuteConnection:
    """Give SQLAlchemy's adapted async DB-API connection sqlite semantics."""

    def __init__(self, connection: Any, *, in_transaction: bool) -> None:
        self._connection = connection
        self._in_transaction = in_transaction

    @property
    def in_transaction(self) -> bool:
        return self._in_transaction

    def execute(self, statement: str, parameters: Any = ()) -> Any:
        cursor = self._connection.cursor()
        try:
            cursor.execute(statement, parameters)
        except Exception:
            cursor.close()
            raise
        return cursor

    def __getattr__(self, name: str) -> Any:
        return getattr(self._connection, name)


def _sync_execute_connection(sync_connection: Any) -> _SyncExecuteConnection:
    pooled = sync_connection.connection
    physical_transaction = bool(
        getattr(pooled.driver_connection, "in_transaction", False)
    )
    return _SyncExecuteConnection(
        pooled.dbapi_connection,
        in_transaction=physical_transaction,
    )


async def append_and_project_async(
    session: Any, command: EventCommand
) -> AppendedEvent:
    """Run the stdlib projector on an AsyncSession's current DBAPI transaction."""

    async_connection = await session.connection()

    def append(sync_connection):
        dbapi_connection = _sync_execute_connection(sync_connection)
        assume_transaction = bool(getattr(dbapi_connection, "in_transaction", False))
        return EventStore(
            dbapi_connection, assume_transaction=assume_transaction
        ).append_and_project(command)

    result = await async_connection.run_sync(append)
    session.info["daem0nmcp_v7_event_appended"] = True
    return result


async def resolve_compatibility_stream_async(
    session: Any,
    workspace_id: str,
    stream_kind: str,
    source_table: str,
    legacy_id: int | str,
) -> str | None:
    """AsyncSession bridge for exact retained-v6 identity resolution."""

    async_connection = await session.connection()

    def resolve(sync_connection):
        return resolve_compatibility_stream(
            _sync_execute_connection(sync_connection),
            workspace_id,
            stream_kind,
            source_table,
            legacy_id,
        )

    return await async_connection.run_sync(resolve)


async def export_event_bundle_async(session: Any, workspace_id: str) -> dict[str, Any]:
    """AsyncSession bridge for deterministic event export."""

    async_connection = await session.connection()

    def export(sync_connection):
        return export_event_bundle(
            _sync_execute_connection(sync_connection), workspace_id
        )

    return await async_connection.run_sync(export)


async def import_event_bundle_async(
    session: Any,
    bundle: Mapping[str, Any],
    workspace_id: str,
) -> EventBundleImportResult:
    """AsyncSession bridge for validated same-workspace event restoration."""

    async_connection = await session.connection()

    def restore(sync_connection):
        dbapi_connection = _sync_execute_connection(sync_connection)
        return import_event_bundle(
            dbapi_connection,
            bundle,
            workspace_id,
            assume_transaction=bool(getattr(dbapi_connection, "in_transaction", False)),
        )

    result = await async_connection.run_sync(restore)
    if result.events_imported:
        session.info["daem0nmcp_v7_event_appended"] = True
    return result


__all__ = [
    "AppendedEvent",
    "CanonicalEncodingError",
    "CompatibilityStreamError",
    "EventBundleError",
    "EventBundleImportResult",
    "EventCommand",
    "EventStore",
    "EventStreamConflict",
    "GovernanceEventCommand",
    "GovernanceEventStore",
    "canonical_json_bytes",
    "compatibility_memory_record",
    "export_event_bundle",
    "export_event_bundle_async",
    "import_event_bundle",
    "import_event_bundle_async",
    "invalidate_compatibility_memory_version",
    "parse_canonical_json",
    "resolve_compatibility_stream",
    "resolve_compatibility_stream_async",
    "append_and_project_async",
    "apply_compatibility_memory_update",
    "build_live_compatibility_claim_index",
    "delete_compatibility_memory",
    "deterministic_id",
    "event_hash_for",
    "event_id_for_hash",
    "normalize_canonical",
    "memory_content_hash",
    "memory_state_hash",
    "sha256_json",
]
