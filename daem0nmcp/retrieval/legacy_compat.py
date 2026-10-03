"""Pure rendering for the retained v7-to-legacy recall envelope.

Every field consumed here was authenticated and loaded by the retrieval
repository in its policy-consistent, bounded SQLite worker transaction.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..event_store import (
    event_hash_for,
    event_id_for_hash,
    memory_content_hash,
    parse_canonical_json,
    sha256_json,
)
from .types import EvidenceItem, EvidenceRef

_CATEGORY_KEYS = {
    "decision": "decisions",
    "pattern": "patterns",
    "warning": "warnings",
    "learning": "learnings",
}
_MAX_LEGACY_ITEMS = 100
_MAX_LEGACY_CONTEXT_BYTES = 4_096
_LEGACY_CONTEXT_KEYS = frozenset({"alternatives", "reason"})


@dataclass(frozen=True, slots=True)
class LegacyEvidenceMetadata:
    """Bounded compatibility data bound to one selected canonical event."""

    record_id: str
    event_id: str
    content_hash: str
    legacy_id: int | None
    context: dict[str, Any] | None


def _safe_legacy_context(value: object) -> dict[str, Any] | None:
    if not isinstance(value, dict) or not set(value) <= _LEGACY_CONTEXT_KEYS:
        return None
    try:
        encoded = json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    except (TypeError, ValueError, RecursionError):
        return None
    # Context is user text and may mention paths (UD-3).
    if len(encoded) > _MAX_LEGACY_CONTEXT_BYTES:
        return None
    # Detach the value from the parsed event payload before it reaches a cache.
    copied = json.loads(encoded.decode("utf-8"))
    return copied if isinstance(copied, dict) else None


def load_legacy_evidence_metadata(
    database_path: Path,
    workspace_id: str,
    items: tuple[EvidenceItem, ...],
) -> dict[tuple[str, str, str], LegacyEvidenceMetadata]:
    """Load exact event-bound legacy fields for already selected evidence.

    The immutable event is used rather than the retained ``memories`` table or
    the current ``memory_records`` row.  That keeps historical recall from
    receiving context written after the selected event.
    """

    if len(items) > _MAX_LEGACY_ITEMS:
        raise ValueError("items must contain at most 100 values")
    database_path = Path(database_path)
    if not database_path.is_file():
        return {}
    refs = tuple(item.evidence_refs[0] for item in items)
    if not refs:
        return {}
    event_ids = tuple(dict.fromkeys(ref.event_id for ref in refs))
    placeholders = ",".join("?" for _ in event_ids)
    connection = sqlite3.connect(database_path)
    connection.row_factory = sqlite3.Row
    try:
        rows = connection.execute(
            "SELECT event_id,workspace_id,stream_id,stream_kind,stream_version,"
            "event_type,event_schema_version,occurred_at_us,recorded_at_us,"
            "actor_type,actor_id,causation_event_id,correlation_id,payload_json,"
            "payload_hash,previous_event_hash,event_hash FROM memory_events "
            f"WHERE workspace_id=? AND stream_kind='memory' AND event_id IN ({placeholders})",
            (workspace_id, *event_ids),
        ).fetchall()
    finally:
        connection.close()
    by_event = {str(row["event_id"]): row for row in rows}
    hydrated: dict[tuple[str, str, str], LegacyEvidenceMetadata] = {}
    for ref in refs:
        selected = by_event.get(ref.event_id)
        if selected is None or str(selected["stream_id"]) != ref.record_id:
            continue
        key = (ref.record_id, ref.event_id, ref.content_hash)
        try:
            payload = parse_canonical_json(str(selected["payload_json"]))
            payload_hash = sha256_json(payload)
            envelope = {
                name: selected[name]
                for name in (
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
        except Exception:
            payload = None
            payload_hash = ""
            calculated_hash = ""
        authenticated = (
            isinstance(payload, dict)
            and payload_hash == selected["payload_hash"]
            and calculated_hash == selected["event_hash"]
            and ref.event_id == event_id_for_hash(calculated_hash)
        )
        if not authenticated:
            hydrated[key] = LegacyEvidenceMetadata(
                record_id=ref.record_id,
                event_id=ref.event_id,
                content_hash=ref.content_hash,
                legacy_id=None,
                context=None,
            )
            continue
        assert isinstance(payload, dict)
        record = payload.get("record")
        if (
            not isinstance(record, dict)
            or memory_content_hash(record) != ref.content_hash
        ):
            continue
        compatibility = payload.get("compatibility")
        legacy_id: int | None = None
        if isinstance(compatibility, dict):
            candidate = compatibility.get("legacy_memory_id")
            if (
                isinstance(candidate, int)
                and not isinstance(candidate, bool)
                and candidate > 0
            ):
                legacy_id = candidate
        hydrated[key] = LegacyEvidenceMetadata(
            record_id=ref.record_id,
            event_id=ref.event_id,
            content_hash=ref.content_hash,
            legacy_id=legacy_id,
            context=_safe_legacy_context(record.get("context", {})),
        )
    claimed_ids = sorted(
        {item.legacy_id for item in hydrated.values() if item.legacy_id is not None}
    )
    if not claimed_ids:
        return hydrated

    # A legacy integer is only safe when every durable claim in this origin
    # workspace resolves to the selected canonical stream. Context can still
    # be returned when an integer claim is ambiguous; only the alias is hidden.
    claim_placeholders = ",".join("?" for _ in claimed_ids)
    targets: dict[int, set[str]] = {legacy_id: set() for legacy_id in claimed_ids}
    connection = sqlite3.connect(database_path)
    try:
        for legacy_id, stream_id in connection.execute(
            "SELECT CAST(json_extract(event.payload_json,'$.compatibility.legacy_memory_id') AS INTEGER),"
            "event.stream_id FROM memory_events AS event "
            "JOIN memory_records AS record ON record.workspace_id=event.workspace_id "
            "AND record.record_id=event.stream_id AND record.deleted_at_us IS NULL "
            "WHERE event.workspace_id=? AND event.stream_kind='memory' "
            "AND json_type(event.payload_json,'$.compatibility.legacy_memory_id')='integer' "
            f"AND json_extract(event.payload_json,'$.compatibility.legacy_memory_id') IN ({claim_placeholders}) "
            "GROUP BY 1,event.stream_id",
            (workspace_id, *claimed_ids),
        ):
            targets[int(legacy_id)].add(str(stream_id))
        for legacy_id, target_id in connection.execute(
            "SELECT CAST(legacy_id AS INTEGER),target_id FROM legacy_id_map "
            "WHERE workspace_id=? AND source_table='memories' "
            "AND target_kind='memory' "
            f"AND CAST(legacy_id AS INTEGER) IN ({claim_placeholders})",
            (workspace_id, *claimed_ids),
        ):
            targets[int(legacy_id)].add(str(target_id))
    finally:
        connection.close()
    return {
        key: LegacyEvidenceMetadata(
            record_id=value.record_id,
            event_id=value.event_id,
            content_hash=value.content_hash,
            legacy_id=(
                value.legacy_id
                if value.legacy_id is not None
                and targets[value.legacy_id] == {value.record_id}
                else None
            ),
            context=value.context,
        )
        for key, value in hydrated.items()
    }


@dataclass(frozen=True, slots=True)
class _SelectedItem:
    item: EvidenceItem
    primary: EvidenceRef
    category_key: str


def _selected_items(
    items: tuple[EvidenceItem, ...],
    per_category_limit: int,
) -> tuple[_SelectedItem, ...]:
    if not isinstance(items, tuple) or not all(
        isinstance(item, EvidenceItem) for item in items
    ):
        raise ValueError("items must contain EvidenceItem values")
    if len(items) > _MAX_LEGACY_ITEMS:
        raise ValueError("items must contain at most 100 values")
    if (
        isinstance(per_category_limit, bool)
        or not isinstance(per_category_limit, int)
        or per_category_limit < 1
        or per_category_limit > _MAX_LEGACY_ITEMS
    ):
        raise ValueError("per_category_limit must be between 1 and 100")
    counts = dict.fromkeys(_CATEGORY_KEYS.values(), 0)
    selected: list[_SelectedItem] = []
    for item in items:
        category_key = _CATEGORY_KEYS.get(item.category, "learnings")
        if counts[category_key] == per_category_limit:
            continue
        counts[category_key] += 1
        selected.append(
            _SelectedItem(
                item=item,
                primary=item.evidence_refs[0],
                category_key=category_key,
            )
        )
    return tuple(selected)


def _render_selection(
    selection: _SelectedItem,
    *,
    condensed: bool,
    metadata: LegacyEvidenceMetadata | None,
    origin_workspace_id: str | None,
) -> dict[str, Any]:
    item = selection.item
    content = (
        item.excerpt
        if not condensed or len(item.excerpt) <= 150
        else item.excerpt[:150] + "..."
    )
    rendered: dict[str, Any] = {
        "id": metadata.legacy_id
        if metadata and metadata.legacy_id is not None
        else selection.primary.record_id,
        "record_id": selection.primary.record_id,
        "content": content,
        "rationale": None if condensed else item.rationale,
        # v7 contexts are schemaless and may contain paths, prompts, or secrets.
        # A typed allowlist is required before this legacy field can be exposed.
        "context": None if condensed or metadata is None else metadata.context,
        "tags": list(item.tags),
        "relevance": round(item.score, 6),
        # Retained names for callers of the legacy Python API.  v7 exposes one
        # policy-composed score and does not apply the old post-ranking decay.
        "semantic_match": round(item.score, 6),
        "recency_weight": 1.0,
        "outcome": item.outcome,
        "worked": item.worked,
        "citation": item.citation,
        "status": item.status,
        "channels": sorted(item.channels),
        "evidence_refs": [
            {
                "record_id": ref.record_id,
                "event_id": ref.event_id,
                "content_hash": ref.content_hash,
                "version_id": ref.version_id,
                "relation_path": list(ref.relation_path),
                "provider": ref.provider,
            }
            for ref in item.evidence_refs
        ],
    }
    if origin_workspace_id is not None:
        rendered["origin_workspace_id"] = origin_workspace_id
    if item.outcome_failed:
        rendered["_warning"] = (
            f"This approach FAILED: {item.outcome or 'no details recorded'}"
        )
    return rendered


def build_legacy_recall_categories(
    items: tuple[EvidenceItem, ...],
    *,
    per_category_limit: int,
    condensed: bool,
    metadata: dict[tuple[str, str, str], LegacyEvidenceMetadata] | None = None,
    origin_workspace_id: str | None = None,
) -> dict[str, list[dict[str, Any]]]:
    """Render only repository-authenticated post-policy evidence."""

    if not isinstance(condensed, bool):
        raise ValueError("condensed must be boolean")
    selections = _selected_items(items, per_category_limit)
    categories: dict[str, list[dict[str, Any]]] = {
        category: [] for category in _CATEGORY_KEYS.values()
    }
    for selection in selections:
        primary = selection.primary
        selected_metadata = (metadata or {}).get(
            (primary.record_id, primary.event_id, primary.content_hash)
        )
        categories[selection.category_key].append(
            _render_selection(
                selection,
                condensed=condensed,
                metadata=selected_metadata,
                origin_workspace_id=origin_workspace_id,
            )
        )
    return categories


__all__ = [
    "LegacyEvidenceMetadata",
    "build_legacy_recall_categories",
    "load_legacy_evidence_metadata",
]
