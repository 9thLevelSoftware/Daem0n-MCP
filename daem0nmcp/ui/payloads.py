"""Closed, bounded presentation projections for Daem0n MCP Apps."""

from __future__ import annotations

import math
import re
from collections.abc import Collection
from datetime import datetime
from typing import Any, Final

APP_IDS: Final[tuple[str, ...]] = (
    "test",
    "search",
    "briefing",
    "covenant",
    "community",
    "graph",
)

MAX_LABEL_CHARS: Final = 256
MAX_CONTENT_CHARS: Final = 16_384
MAX_PATH_CHARS: Final = 4_096
MAX_TAG_CHARS: Final = 128
MAX_TAGS: Final = 32
MAX_SEARCH_ITEMS_PER_CATEGORY: Final = 100
MAX_BRIEFING_ITEMS: Final = 20
MAX_COMMUNITIES: Final = 1_000
MAX_COMMUNITY_PATH: Final = 32
MAX_GRAPH_NODES: Final = 1_000
MAX_GRAPH_EDGES: Final = 5_000
MAX_GRAPH_PATH: Final = 1_000
JS_MAX_SAFE_INTEGER: Final = 9_007_199_254_740_991

_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_SEARCH_CATEGORIES = ("decisions", "warnings", "patterns", "learnings")
_GRAPH_CATEGORIES = {"decision", "warning", "pattern", "learning"}
_RELATIONSHIPS = {
    "led_to",
    "supersedes",
    "conflicts_with",
    "relates_to",
    "depends_on",
}


class InvalidAppPayload(ValueError):  # noqa: N818 -- retained public Python API name
    """Raised when a root payload cannot be represented safely."""


def _text(value: Any, limit: int = MAX_LABEL_CHARS) -> str:
    if not isinstance(value, str):
        return ""
    bounded = value[:limit]
    try:
        bounded.encode("utf-8")
    except UnicodeEncodeError:
        return ""
    return bounded


def _enum(value: Any, allowed: Collection[str], fallback: str) -> str:
    """Return only a string from a closed presentation allowlist."""
    return value if isinstance(value, str) and value in allowed else fallback


def _mapping(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _items(value: Any, limit: int) -> list[Any]:
    return value[:limit] if isinstance(value, list) else []


def _count(value: Any, default: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        return default
    return min(max(value, 0), JS_MAX_SAFE_INTEGER)


def _bounded_int(value: Any, minimum: int, maximum: int, default: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        return default
    return min(max(value, minimum), maximum)


def _ratio(value: Any, default: float = 0.0) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return default
    if isinstance(value, int):
        if value < 0:
            return 0.0
        if value > 100:
            return 1.0
    number = float(value)
    if not math.isfinite(number):
        return default
    if 1 < number <= 100:
        number /= 100
    return min(max(number, 0.0), 1.0)


def _boolean(value: Any, default: bool = False) -> bool:
    return value if isinstance(value, bool) else default


def _optional_boolean(value: Any) -> bool | None:
    return value if isinstance(value, bool) else None


def _safe_id(value: Any) -> int | str | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if 0 < value <= JS_MAX_SAFE_INTEGER else None
    if isinstance(value, str) and _SAFE_ID.fullmatch(value):
        return value
    return None


def _id_key(value: int | str) -> tuple[type[int] | type[str], int | str]:
    return type(value), value


def _date(value: Any) -> str:
    text = _text(value)
    if not text:
        return ""
    candidate = text[:-1] + "+00:00" if text.endswith("Z") else text
    try:
        datetime.fromisoformat(candidate)
    except (ValueError, OverflowError):
        return ""
    return text


def _search_record(value: Any) -> dict[str, Any]:
    record = _mapping(value)
    relevance = _ratio(record.get("relevance", record.get("score", 0.0)))
    return {
        "id": _safe_id(record.get("id")),
        "content": _text(record.get("content"), MAX_CONTENT_CHARS),
        "relevance": relevance,
        "semantic_match": _ratio(record.get("semantic_match"), relevance),
        "recency_weight": _ratio(record.get("recency_weight"), 1.0),
        "created_at": _date(record.get("created_at")),
        "tags": [
            _text(tag, MAX_TAG_CHARS)
            for tag in _items(record.get("tags"), MAX_TAGS)
            if isinstance(tag, str)
        ],
        "worked": _optional_boolean(record.get("worked")),
    }


def _normalize_search(data: dict[str, Any]) -> dict[str, Any]:
    categories = {
        category: [
            _search_record(item)
            for item in _items(data.get(category), MAX_SEARCH_ITEMS_PER_CATEGORY)
        ]
        for category in _SEARCH_CATEGORIES
    }
    shown = sum(len(records) for records in categories.values())
    return {
        "topic": _text(data.get("topic")),
        **categories,
        "total_count": _count(data.get("total_count"), shown),
        "offset": _count(data.get("offset")),
        "limit": _bounded_int(data.get("limit"), 1, 100, 10),
        "has_more": _boolean(data.get("has_more")),
    }


def _brief_content(value: Any) -> str:
    record = _mapping(value)
    return _text(record.get("content", record.get("summary")), MAX_CONTENT_CHARS)


def _normalize_briefing(data: dict[str, Any]) -> dict[str, Any]:
    statistics = _mapping(data.get("statistics"))
    by_category = _mapping(statistics.get("by_category"))
    outcome_rates = _mapping(statistics.get("outcome_rates"))
    decisions = []
    for item in _items(data.get("recent_decisions"), MAX_BRIEFING_ITEMS):
        record = _mapping(item)
        decisions.append(
            {
                "content": _brief_content(record),
                "worked": _optional_boolean(record.get("worked")),
                "created_at": _date(record.get("created_at")),
            }
        )
    warnings = []
    for item in _items(data.get("active_warnings"), MAX_BRIEFING_ITEMS):
        record = _mapping(item)
        severity = record.get("severity")
        warnings.append(
            {
                "content": _brief_content(record),
                "severity": _enum(severity, {"high", "medium", "low"}, "neutral"),
            }
        )
    failed = [
        {"content": _brief_content(item)}
        for item in _items(data.get("failed_approaches"), MAX_BRIEFING_ITEMS)
    ]
    git = _mapping(data.get("git_changes"))
    raw_files = git.get("files", git.get("uncommitted_changes"))
    files = []
    for item in _items(raw_files, MAX_BRIEFING_ITEMS):
        record = _mapping(item)
        status = record.get("status")
        files.append(
            {
                "status": _enum(status, {"A", "M", "D"}, "?"),
                "path": _text(record.get("path", record.get("file")), MAX_PATH_CHARS),
            }
        )
    raw_focus = data.get("focus_areas")
    if isinstance(raw_focus, dict):
        focus_values: list[Any] = [
            {"topic": key} for key in list(raw_focus)[:MAX_BRIEFING_ITEMS]
        ]
    else:
        focus_values = _items(raw_focus, MAX_BRIEFING_ITEMS)
    focus = []
    for item in focus_values:
        record = _mapping(item)
        focus.append({"topic": _text(record.get("topic"))})
    status = data.get("status")
    return {
        "status": _enum(status, {"ready", "error", "degraded"}, "neutral"),
        "statistics": {
            "total_memories": _count(statistics.get("total_memories")),
            "by_category": {
                category: _count(by_category.get(category))
                for category in ("decision", "warning", "pattern", "learning")
            },
            "outcome_rates": {
                "success_rate": _ratio(outcome_rates.get("success_rate"))
            },
        },
        "recent_decisions": decisions,
        "active_warnings": warnings,
        "failed_approaches": failed,
        "git_changes": {
            "total": _count(git.get("total"), len(files)),
            "files": files,
        },
        "focus_areas": focus,
        "message": _text(data.get("message"), MAX_CONTENT_CHARS),
    }


def _normalize_covenant(data: dict[str, Any]) -> dict[str, Any]:
    preflight = _mapping(data.get("preflight"))
    phase = data.get("phase")
    status = preflight.get("status")
    return {
        "phase": _enum(phase, {"commune", "counsel", "inscribe", "seal"}, "unknown"),
        "phase_label": _text(data.get("phase_label")),
        "phase_description": _text(data.get("phase_description"), MAX_CONTENT_CHARS),
        "preflight": {
            "status": _enum(status, {"valid", "issued", "expired", "none"}, "unknown"),
            "expires_at": _date(preflight.get("expires_at")),
            "remaining_seconds": _bounded_int(
                preflight.get("remaining_seconds"), 0, 86_400, 0
            ),
        },
        "can_mutate": _boolean(data.get("can_mutate")),
        "message": _text(data.get("message"), MAX_CONTENT_CHARS),
    }


def _normalize_community(data: dict[str, Any]) -> dict[str, Any]:
    communities = []
    seen: set[tuple[type[int] | type[str], int | str]] = set()
    for item in _items(data.get("communities"), MAX_COMMUNITIES):
        record = _mapping(item)
        community_id = _safe_id(record.get("id"))
        if community_id is None or _id_key(community_id) in seen:
            continue
        seen.add(_id_key(community_id))
        communities.append(
            {
                "id": community_id,
                "parent_community_id": _safe_id(
                    record.get("parent_community_id", record.get("parent_id"))
                ),
                "name": _text(record.get("name")),
                "summary": _text(record.get("summary"), MAX_CONTENT_CHARS),
                "member_count": _count(record.get("member_count")),
                "level": _bounded_int(record.get("level"), 0, 5, 0),
            }
        )

    by_id = {_id_key(item["id"]): item for item in communities}
    for item in communities:
        parent = item["parent_community_id"]
        if (
            parent is None
            or _id_key(parent) not in by_id
            or _id_key(parent) == _id_key(item["id"])
        ):
            item["parent_community_id"] = None

    # Break any remaining cycle by detaching the first repeated edge encountered.
    for item in communities:
        visited = {_id_key(item["id"])}
        cursor = item
        while cursor["parent_community_id"] is not None:
            parent_key = _id_key(cursor["parent_community_id"])
            if parent_key in visited:
                cursor["parent_community_id"] = None
                break
            visited.add(parent_key)
            cursor = by_id[parent_key]

    path = []
    for item in _items(data.get("path"), MAX_COMMUNITY_PATH):
        record = _mapping(item)
        community_id = _safe_id(record.get("id"))
        if community_id is not None:
            path.append({"id": community_id, "name": _text(record.get("name"))})
    return {
        "count": _count(data.get("count"), len(communities)),
        "communities": communities,
        "path": path,
    }


def _normalize_graph(data: dict[str, Any]) -> dict[str, Any]:
    nodes = []
    node_ids: set[tuple[type[int] | type[str], int | str]] = set()
    for item in _items(data.get("nodes"), MAX_GRAPH_NODES):
        record = _mapping(item)
        node_id = _safe_id(record.get("id"))
        if node_id is None or _id_key(node_id) in node_ids:
            continue
        node_ids.add(_id_key(node_id))
        category = record.get("category")
        nodes.append(
            {
                "id": node_id,
                "content": _text(record.get("content"), MAX_CONTENT_CHARS),
                "full_content": _text(
                    record.get("full_content", record.get("content")), MAX_CONTENT_CHARS
                ),
                "category": _enum(category, _GRAPH_CATEGORIES, "default"),
                "tags": [
                    _text(tag, MAX_TAG_CHARS)
                    for tag in _items(record.get("tags"), MAX_TAGS)
                    if isinstance(tag, str)
                ],
                "created_at": _date(record.get("created_at")),
                "community_id": _safe_id(record.get("community_id")),
            }
        )
    edges = []
    for item in _items(data.get("edges"), MAX_GRAPH_EDGES):
        record = _mapping(item)
        source = _safe_id(record.get("source", record.get("source_id")))
        target = _safe_id(record.get("target", record.get("target_id")))
        if source is None or target is None:
            continue
        if _id_key(source) not in node_ids or _id_key(target) not in node_ids:
            continue
        relationship = record.get("relationship")
        edges.append(
            {
                "source": source,
                "target": target,
                "relationship": _enum(relationship, _RELATIONSHIPS, "relates_to"),
                "confidence": _ratio(record.get("confidence")),
                "description": _text(record.get("description"), MAX_CONTENT_CHARS),
            }
        )
    path = []
    for value in _items(data.get("path"), MAX_GRAPH_PATH):
        node_id = _safe_id(value)
        if node_id is not None and _id_key(node_id) in node_ids:
            path.append(node_id)
    return {
        "topic": _text(data.get("topic")),
        "nodes": nodes,
        "edges": edges,
        "path": path,
    }


_PROJECTORS = {
    "test": lambda data: {},
    "search": _normalize_search,
    "briefing": _normalize_briefing,
    "covenant": _normalize_covenant,
    "community": _normalize_community,
    "graph": _normalize_graph,
}


def normalize_app_payload(app_id: str, data: dict[str, Any]) -> dict[str, Any]:
    """Return the closed, bounded presentation schema for one known app."""
    if app_id not in _PROJECTORS or type(data) is not dict:
        raise InvalidAppPayload("invalid app payload")
    return _PROJECTORS[app_id](data)


__all__ = [
    "APP_IDS",
    "InvalidAppPayload",
    "JS_MAX_SAFE_INTEGER",
    "MAX_BRIEFING_ITEMS",
    "MAX_COMMUNITIES",
    "MAX_COMMUNITY_PATH",
    "MAX_CONTENT_CHARS",
    "MAX_GRAPH_EDGES",
    "MAX_GRAPH_NODES",
    "MAX_GRAPH_PATH",
    "MAX_LABEL_CHARS",
    "MAX_PATH_CHARS",
    "MAX_SEARCH_ITEMS_PER_CATEGORY",
    "MAX_TAG_CHARS",
    "MAX_TAGS",
    "normalize_app_payload",
]
