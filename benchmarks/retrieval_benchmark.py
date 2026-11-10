"""Deterministic, dependency-free evaluation for the v7 retrieval contract.

The module deliberately accepts plain dictionaries at the retrieval boundary.
That keeps the checked-in corpus useful while the production service evolves,
and lets CI run without a model, Qdrant, network access, or optional packages.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib
import json
import math
import re
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

WARMUP_ITERATIONS = 5
TIMED_ITERATIONS = 30
BENCHMARK_MODES = ("fully_enabled", "lexical_only")
FIXTURE_FILES = ("events.jsonl", "qdrant_fake.py", "queries.jsonl", "records.jsonl")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_RECORD_ID_RE = re.compile(r"^mem_[0-9a-f]{64}$")
_EVENT_ID_RE = re.compile(r"^evt_[0-9a-f]{64}$")
_WORKSPACE_ID_RE = re.compile(r"^ws_[0-9a-f]{24}$")
_PROVIDER_STATES = frozenset({"degraded", "failed", "ready", "unavailable"})
_CITATION_STATES = frozenset({"current", "superseded"})

_RECORD_FIELDS = frozenset(
    {
        "archived",
        "content",
        "content_hash",
        "fixture_traits",
        "outcome",
        "rationale",
        "record_id",
        "record_type",
        "source_event_id",
        "structured_steps",
        "tags",
        "transaction_from_us",
        "transaction_to_us",
        "valid_from_us",
        "valid_to_us",
        "visibility",
        "workspace_id",
    }
)
_EVENT_FIELDS = frozenset(
    {
        "actor_id",
        "actor_type",
        "causation_event_id",
        "correlation_id",
        "event_hash",
        "event_id",
        "event_schema_version",
        "event_type",
        "occurred_at_us",
        "payload",
        "payload_hash",
        "previous_event_hash",
        "recorded_at_us",
        "stream_id",
        "stream_kind",
        "stream_version",
        "workspace_id",
    }
)
_QUERY_FIELDS = frozenset(
    {
        "as_of_transaction_time_us",
        "as_of_valid_time_us",
        "contradiction",
        "expected_abstention",
        "expected_excluded_citations",
        "expected_provider_degradation",
        "expected_relevant",
        "filters",
        "query_id",
        "required_citations",
        "temporal_expectation",
        "text",
        "token_budget",
    }
)
_FILTER_FIELDS = frozenset(
    {
        "categories",
        "include_archived",
        "include_invalidated",
        "record_ids",
        "tags",
    }
)
_EXPECTED_TRAIT_COUNTS = {
    "archived": 1,
    "current_fact": 1,
    "dense_semantic_match": 1,
    "duplicate_content": 2,
    "failed_outcome_warning": 1,
    "lexical_synonym_miss": 1,
    "procedure": 2,
    "rationale_match": 1,
    "successful_decision": 1,
    "superseded_fact": 1,
    "tag_match": 1,
}
_EXPECTED_EVENT_TYPE_COUNTS = {
    "fact.asserted": 2,
    "fact.retracted": 1,
    "memory.created": 12,
    "memory.outcome_recorded": 2,
    "relationship.created": 1,
}
_EVENT_TYPE_STREAM_KIND = {
    "fact.asserted": "fact",
    "fact.retracted": "fact",
    "memory.created": "memory",
    "memory.outcome_recorded": "memory",
    "relationship.created": "relationship",
}


class FixtureValidationError(ValueError):
    """Raised when checked-in benchmark data fails closed validation."""

    def __init__(self, code: str, detail: str) -> None:
        self.code = code
        super().__init__(f"{code}: {detail}")


class BenchmarkInputError(ValueError):
    """Raised when a benchmark adapter violates the plain-result contract."""

    def __init__(self, code: str, detail: str) -> None:
        self.code = code
        super().__init__(f"{code}: {detail}")


@dataclass(frozen=True, slots=True)
class RetrievalFixtures:
    records: tuple[dict[str, Any], ...]
    events: tuple[dict[str, Any], ...]
    queries: tuple[dict[str, Any], ...]
    digest: str


def _canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _reject_constant(value: str) -> None:
    raise FixtureValidationError("INVALID_JSON", f"non-finite number {value}")


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise FixtureValidationError("DUPLICATE_KEY", key)
        result[key] = value
    return result


def _load_jsonl(path: Path) -> tuple[dict[str, Any], ...]:
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise FixtureValidationError("FIXTURE_READ_FAILED", path.name) from exc
    if not text.endswith("\n") or "\r" in text:
        raise FixtureValidationError(
            "NON_CANONICAL_JSONL", f"{path.name} must use LF and end with LF"
        )
    values: list[dict[str, Any]] = []
    for line_number, line in enumerate(text.splitlines(), 1):
        if not line:
            raise FixtureValidationError(
                "EMPTY_JSONL_LINE", f"{path.name}:{line_number}"
            )
        try:
            value = json.loads(
                line,
                object_pairs_hook=_unique_object,
                parse_constant=_reject_constant,
            )
        except FixtureValidationError:
            raise
        except (json.JSONDecodeError, RecursionError, TypeError, ValueError) as exc:
            raise FixtureValidationError(
                "INVALID_JSON", f"{path.name}:{line_number}"
            ) from exc
        if not isinstance(value, dict):
            raise FixtureValidationError(
                "INVALID_JSONL_ROW", f"{path.name}:{line_number} is not an object"
            )
        values.append(value)
    return tuple(values)


def _fixture_digest(root: Path) -> str:
    digest = hashlib.sha256()
    for name in FIXTURE_FILES:
        try:
            payload = (root / name).read_bytes().replace(b"\r\n", b"\n")
        except OSError as exc:
            raise FixtureValidationError("FIXTURE_READ_FAILED", name) from exc
        digest.update(name.encode("utf-8"))
        digest.update(b"\0")
        digest.update(payload)
        digest.update(b"\0")
    return digest.hexdigest()


def _plain_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _require_fields(
    value: Mapping[str, Any], expected: frozenset[str], *, location: str
) -> None:
    actual = set(value)
    unknown = actual - expected
    if unknown:
        raise FixtureValidationError(
            "UNKNOWN_FIELDS", f"{location}: {','.join(sorted(unknown))}"
        )
    missing = expected - actual
    if missing:
        raise FixtureValidationError(
            "MISSING_FIELDS", f"{location}: {','.join(sorted(missing))}"
        )


def _is_text_list(value: object, *, allow_empty: bool = True) -> bool:
    return (
        isinstance(value, list)
        and (allow_empty or bool(value))
        and all(isinstance(item, str) and bool(item) for item in value)
        and len(value) == len(set(value))
    )


def _record_hash_input(record: Mapping[str, Any]) -> dict[str, Any]:
    steps = record["structured_steps"]
    outcome = record["outcome"]
    return {
        "content": record["content"],
        "context": {"steps": steps} if steps else {},
        "file_path": None,
        "file_path_relative": None,
        "legacy_type": None,
        "outcome": None if outcome is None else outcome["outcome_text"],
        "rationale": record["rationale"],
        "record_type": record["record_type"],
        "tags": record["tags"],
        "worked": None if outcome is None else outcome["worked"],
    }


def _validate_record_trait_contract(record: Mapping[str, Any], location: str) -> None:
    traits = set(record["fixture_traits"])
    outcome = record["outcome"]
    checks = (
        (("archived" in traits) == record["archived"]),
        (("procedure" in traits) == (record["record_type"] == "procedure")),
        ("procedure" not in traits or bool(record["structured_steps"])),
        ("tag_match" not in traits or bool(record["tags"])),
        ("rationale_match" not in traits or bool(record["rationale"])),
        (("dense_semantic_match" in traits) == ("lexical_synonym_miss" in traits)),
        (
            "current_fact" not in traits
            or (record["valid_to_us"] is None and record["transaction_to_us"] is None)
        ),
        (
            "superseded_fact" not in traits
            or (
                record["valid_to_us"] is not None
                and record["transaction_to_us"] is not None
            )
        ),
        (
            "successful_decision" not in traits
            or (
                record["record_type"] == "decision"
                and outcome is not None
                and outcome["worked"] is True
            )
        ),
        (
            "failed_outcome_warning" not in traits
            or (
                record["record_type"] == "warning"
                and outcome is not None
                and outcome["worked"] is False
            )
        ),
    )
    if not all(checks):
        raise FixtureValidationError("TRAIT_CONTRACT", location)


def _validate_records(records: Sequence[dict[str, Any]]) -> None:
    if len(records) != 12:
        raise FixtureValidationError(
            "RECORD_COUNT", "records.jsonl must contain 12 rows"
        )
    ids: set[str] = set()
    trait_counts: dict[str, int] = {}
    workspaces: set[str] = set()
    for index, record in enumerate(records, 1):
        location = f"records.jsonl:{index}"
        _require_fields(record, _RECORD_FIELDS, location=location)
        record_id = record["record_id"]
        workspace_id = record["workspace_id"]
        if not isinstance(record_id, str) or not _RECORD_ID_RE.fullmatch(record_id):
            raise FixtureValidationError("INVALID_RECORD_ID", location)
        if record_id in ids:
            raise FixtureValidationError("DUPLICATE_RECORD_ID", record_id)
        ids.add(record_id)
        if not isinstance(workspace_id, str) or not _WORKSPACE_ID_RE.fullmatch(
            workspace_id
        ):
            raise FixtureValidationError("INVALID_WORKSPACE_ID", location)
        workspaces.add(workspace_id)
        if record["record_type"] not in {
            "decision",
            "learning",
            "observation",
            "pattern",
            "procedure",
            "warning",
        }:
            raise FixtureValidationError("INVALID_RECORD_TYPE", location)
        if not isinstance(record["content"], str) or not record["content"]:
            raise FixtureValidationError("INVALID_CONTENT", location)
        if record["rationale"] is not None and not isinstance(record["rationale"], str):
            raise FixtureValidationError("INVALID_RATIONALE", location)
        if not _is_text_list(record["tags"]):
            raise FixtureValidationError("INVALID_TAGS", location)
        if not _is_text_list(record["structured_steps"]):
            raise FixtureValidationError("INVALID_STEPS", location)
        if not _is_text_list(record["fixture_traits"], allow_empty=False):
            raise FixtureValidationError("INVALID_TRAITS", location)
        for trait in record["fixture_traits"]:
            trait_counts[trait] = trait_counts.get(trait, 0) + 1
        if (
            not isinstance(record["archived"], bool)
            or record["visibility"] != "workspace"
        ):
            raise FixtureValidationError("INVALID_POLICY_METADATA", location)
        for field in ("valid_from_us", "transaction_from_us"):
            if not _plain_int(record[field]):
                raise FixtureValidationError("INVALID_TIME", f"{location}:{field}")
        for start, end in (
            ("valid_from_us", "valid_to_us"),
            ("transaction_from_us", "transaction_to_us"),
        ):
            end_value = record[end]
            if end_value is not None and (
                not _plain_int(end_value) or end_value <= record[start]
            ):
                raise FixtureValidationError("INVALID_TIME", f"{location}:{end}")
        source_event_id = record["source_event_id"]
        if not isinstance(source_event_id, str) or not _EVENT_ID_RE.fullmatch(
            source_event_id
        ):
            raise FixtureValidationError("INVALID_EVENT_ID", location)
        outcome = record["outcome"]
        if outcome is not None:
            if not isinstance(outcome, dict):
                raise FixtureValidationError("INVALID_OUTCOME", location)
            _require_fields(
                outcome,
                frozenset({"event_id", "outcome_text", "worked"}),
                location=f"{location}:outcome",
            )
            if (
                not isinstance(outcome["event_id"], str)
                or not _EVENT_ID_RE.fullmatch(outcome["event_id"])
                or not isinstance(outcome["outcome_text"], str)
                or not outcome["outcome_text"]
                or not isinstance(outcome["worked"], bool)
            ):
                raise FixtureValidationError("INVALID_OUTCOME", location)
        _validate_record_trait_contract(record, location)
        expected_hash = hashlib.sha256(
            _canonical_json_bytes(_record_hash_input(record))
        ).hexdigest()
        if record["content_hash"] != expected_hash:
            raise FixtureValidationError("CONTENT_HASH_MISMATCH", record_id)
    if len(workspaces) != 1:
        raise FixtureValidationError("MULTIPLE_WORKSPACES", "records")
    if trait_counts != _EXPECTED_TRAIT_COUNTS:
        raise FixtureValidationError(
            "TRAIT_COVERAGE", json.dumps(trait_counts, sort_keys=True)
        )
    duplicate_hashes = {
        record["content_hash"]
        for record in records
        if "duplicate_content" in record["fixture_traits"]
    }
    if len(duplicate_hashes) != 1:
        raise FixtureValidationError(
            "TRAIT_CONTRACT", "duplicate content hashes differ"
        )


def _validate_events(
    events: Sequence[dict[str, Any]], records: Sequence[dict[str, Any]]
) -> None:
    if len(events) != 18:
        raise FixtureValidationError("EVENT_COUNT", "events.jsonl must contain 18 rows")
    event_ids: set[str] = set()
    record_by_id = {record["record_id"]: record for record in records}
    stream_heads: dict[str, tuple[int, str]] = {}
    created_streams: set[str] = set()
    event_type_counts: dict[str, int] = {}
    last_recorded_at = -1
    for index, event in enumerate(events, 1):
        location = f"events.jsonl:{index}"
        _require_fields(event, _EVENT_FIELDS, location=location)
        event_id = event["event_id"]
        if not isinstance(event_id, str) or not _EVENT_ID_RE.fullmatch(event_id):
            raise FixtureValidationError("INVALID_EVENT_ID", location)
        if event_id in event_ids:
            raise FixtureValidationError("DUPLICATE_EVENT_ID", event_id)
        if event["event_hash"] != event_id[4:]:
            raise FixtureValidationError("EVENT_HASH_MISMATCH", event_id)
        if event["event_schema_version"] != 1:
            raise FixtureValidationError("EVENT_SCHEMA_VERSION", location)
        if event["workspace_id"] != records[0]["workspace_id"]:
            raise FixtureValidationError("EVENT_WORKSPACE_MISMATCH", location)
        if event["stream_kind"] not in {"fact", "memory", "relationship"}:
            raise FixtureValidationError("INVALID_STREAM_KIND", location)
        expected_kind = _EVENT_TYPE_STREAM_KIND.get(event["event_type"])
        if expected_kind is None or expected_kind != event["stream_kind"]:
            raise FixtureValidationError("INVALID_EVENT_TYPE", location)
        event_type_counts[event["event_type"]] = (
            event_type_counts.get(event["event_type"], 0) + 1
        )
        if not _plain_int(event["stream_version"]) or event["stream_version"] < 1:
            raise FixtureValidationError("INVALID_STREAM_VERSION", location)
        if not _plain_int(event["occurred_at_us"]) or not _plain_int(
            event["recorded_at_us"]
        ):
            raise FixtureValidationError("INVALID_TIME", location)
        if event["recorded_at_us"] < event["occurred_at_us"]:
            raise FixtureValidationError("INVALID_TIME", location)
        if event["recorded_at_us"] < last_recorded_at:
            raise FixtureValidationError("EVENT_ORDER", location)
        last_recorded_at = event["recorded_at_us"]
        if event["actor_type"] != "import" or event["actor_id"] != "retrieval-fixture":
            raise FixtureValidationError("INVALID_ACTOR", location)
        if not isinstance(event["payload"], dict):
            raise FixtureValidationError("INVALID_PAYLOAD", location)
        payload_hash = hashlib.sha256(
            _canonical_json_bytes(event["payload"])
        ).hexdigest()
        if event["payload_hash"] != payload_hash:
            raise FixtureValidationError("PAYLOAD_HASH_MISMATCH", event_id)
        event_columns = {
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
        if (
            hashlib.sha256(_canonical_json_bytes(event_columns)).hexdigest()
            != event["event_hash"]
        ):
            raise FixtureValidationError("EVENT_HASH_MISMATCH", event_id)
        causation = event["causation_event_id"]
        if causation is not None and causation not in event_ids:
            raise FixtureValidationError("FORWARD_CAUSATION", event_id)
        event_ids.add(event_id)
        previous = stream_heads.get(event["stream_id"])
        expected_version = 1 if previous is None else previous[0] + 1
        expected_hash = None if previous is None else previous[1]
        if (
            event["stream_version"] != expected_version
            or event["previous_event_hash"] != expected_hash
        ):
            raise FixtureValidationError("BROKEN_EVENT_CHAIN", event["stream_id"])
        stream_heads[event["stream_id"]] = (
            event["stream_version"],
            event["event_hash"],
        )
        if event["event_type"] == "memory.created":
            if (
                event["stream_kind"] != "memory"
                or event["stream_id"] not in record_by_id
            ):
                raise FixtureValidationError("INVALID_MEMORY_CREATE", location)
            created_streams.add(event["stream_id"])
    if created_streams != set(record_by_id):
        raise FixtureValidationError(
            "MISSING_MEMORY_CREATE", "records are not constructed"
        )
    if event_type_counts != _EXPECTED_EVENT_TYPE_COUNTS:
        raise FixtureValidationError(
            "EVENT_TYPE_COVERAGE", json.dumps(event_type_counts, sort_keys=True)
        )
    for record in records:
        source_event = next(
            (
                event
                for event in events
                if event["event_id"] == record["source_event_id"]
            ),
            None,
        )
        if source_event is None or source_event["stream_id"] != record["record_id"]:
            raise FixtureValidationError("SOURCE_EVENT_MISMATCH", record["record_id"])
        record_payload = source_event["payload"].get("record")
        if not isinstance(record_payload, dict):
            raise FixtureValidationError("SOURCE_EVENT_MISMATCH", record["record_id"])
        if record["outcome"] is not None and (
            record["outcome"]["event_id"] != source_event["event_id"]
            or source_event["event_type"] != "memory.outcome_recorded"
        ):
            raise FixtureValidationError("SOURCE_EVENT_MISMATCH", record["record_id"])
        payload_hash_input = {
            "content": record_payload.get("content"),
            "context": record_payload.get("context", {}),
            "file_path": record_payload.get("file_path"),
            "file_path_relative": record_payload.get("file_path_relative"),
            "legacy_type": record_payload.get("legacy_type"),
            "outcome": record_payload.get("outcome"),
            "rationale": record_payload.get("rationale"),
            "record_type": record_payload.get("record_type"),
            "tags": record_payload.get("tags", []),
            "worked": record_payload.get("worked"),
        }
        if (
            hashlib.sha256(_canonical_json_bytes(payload_hash_input)).hexdigest()
            != record["content_hash"]
        ):
            raise FixtureValidationError(
                "SOURCE_EVENT_CONTENT_MISMATCH", record["record_id"]
            )


def _validate_queries(
    queries: Sequence[dict[str, Any]], records: Sequence[dict[str, Any]]
) -> None:
    if len(queries) != 12:
        raise FixtureValidationError(
            "QUERY_COUNT", "queries.jsonl must contain 12 rows"
        )
    record_ids = {record["record_id"] for record in records}
    query_ids: set[str] = set()
    contradiction_count = 0
    for index, query in enumerate(queries, 1):
        location = f"queries.jsonl:{index}"
        _require_fields(query, _QUERY_FIELDS, location=location)
        query_id = query["query_id"]
        if not isinstance(query_id, str) or not re.fullmatch(
            r"q[0-9]{2}_[a-z0-9_]+", query_id
        ):
            raise FixtureValidationError("INVALID_QUERY_ID", location)
        if query_id in query_ids:
            raise FixtureValidationError("DUPLICATE_QUERY_ID", query_id)
        query_ids.add(query_id)
        if not isinstance(query["text"], str) or not query["text"].strip():
            raise FixtureValidationError("INVALID_QUERY_TEXT", query_id)
        if not _plain_int(query["token_budget"]) or query["token_budget"] < 1:
            raise FixtureValidationError("INVALID_TOKEN_BUDGET", query_id)
        if not isinstance(query["expected_abstention"], bool):
            raise FixtureValidationError("INVALID_ABSTENTION", query_id)
        filters = query["filters"]
        if not isinstance(filters, dict):
            raise FixtureValidationError("INVALID_FILTERS", query_id)
        _require_fields(filters, _FILTER_FIELDS, location=f"{location}:filters")
        if not isinstance(filters["include_archived"], bool) or not isinstance(
            filters["include_invalidated"], bool
        ):
            raise FixtureValidationError("INVALID_FILTERS", query_id)
        for name in ("categories", "record_ids", "tags"):
            if not _is_text_list(filters[name]):
                raise FixtureValidationError("INVALID_FILTERS", f"{query_id}:{name}")
        if not set(filters["record_ids"]).issubset(record_ids):
            raise FixtureValidationError("UNKNOWN_RECORD_REFERENCE", query_id)
        relevant = query["expected_relevant"]
        if not isinstance(relevant, list):
            raise FixtureValidationError("INVALID_RELEVANCE", query_id)
        relevant_ids: list[str] = []
        grades: list[int] = []
        for item in relevant:
            if not isinstance(item, dict) or set(item) != {"grade", "record_id"}:
                raise FixtureValidationError("INVALID_RELEVANCE", query_id)
            if (
                item["record_id"] not in record_ids
                or not _plain_int(item["grade"])
                or not 0 <= item["grade"] <= 3
            ):
                raise FixtureValidationError("INVALID_RELEVANCE", query_id)
            relevant_ids.append(item["record_id"])
            grades.append(item["grade"])
        if len(relevant_ids) != len(set(relevant_ids)) or grades != sorted(
            grades, reverse=True
        ):
            raise FixtureValidationError("INVALID_RELEVANCE_ORDER", query_id)
        required = query["required_citations"]
        excluded = query["expected_excluded_citations"]
        if not _is_text_list(required) or not _is_text_list(excluded):
            raise FixtureValidationError("INVALID_CITATIONS", query_id)
        if not (set(required) | set(excluded)).issubset(record_ids):
            raise FixtureValidationError("UNKNOWN_RECORD_REFERENCE", query_id)
        if set(required) & set(excluded):
            raise FixtureValidationError("CONFLICTING_CITATIONS", query_id)
        degradation = query["expected_provider_degradation"]
        if not isinstance(degradation, dict) or set(degradation) != set(
            BENCHMARK_MODES
        ):
            raise FixtureValidationError("INVALID_DEGRADATION", query_id)
        if any(not _is_text_list(degradation[mode]) for mode in BENCHMARK_MODES):
            raise FixtureValidationError("INVALID_DEGRADATION", query_id)
        for field in ("as_of_transaction_time_us", "as_of_valid_time_us"):
            if query[field] is not None and not _plain_int(query[field]):
                raise FixtureValidationError("INVALID_TIME", f"{query_id}:{field}")
        temporal = query["temporal_expectation"]
        if temporal is not None:
            if not isinstance(temporal, dict) or set(temporal) != {
                "include_invalidated",
                "known_invalidated_record_ids",
                "valid_record_ids",
            }:
                raise FixtureValidationError("INVALID_TEMPORAL_EXPECTATION", query_id)
            if not isinstance(temporal["include_invalidated"], bool):
                raise FixtureValidationError("INVALID_TEMPORAL_EXPECTATION", query_id)
            for name in ("known_invalidated_record_ids", "valid_record_ids"):
                if not _is_text_list(temporal[name]) or not set(
                    temporal[name]
                ).issubset(record_ids):
                    raise FixtureValidationError(
                        "INVALID_TEMPORAL_EXPECTATION", query_id
                    )
        contradiction = query["contradiction"]
        if contradiction is not None:
            contradiction_count += 1
            if (
                not isinstance(contradiction, dict)
                or set(contradiction) != {"mode", "record_ids"}
                or contradiction["mode"] not in {"exclude", "label"}
                or not _is_text_list(contradiction["record_ids"], allow_empty=False)
                or not set(contradiction["record_ids"]).issubset(record_ids)
            ):
                raise FixtureValidationError("INVALID_CONTRADICTION", query_id)
    if contradiction_count != 2:
        raise FixtureValidationError(
            "CONTRADICTION_COVERAGE", "exactly two queries required"
        )
    if sum(bool(query["expected_abstention"]) for query in queries) != 2:
        raise FixtureValidationError(
            "ABSTENTION_COVERAGE", "exactly two queries required"
        )


def load_retrieval_fixtures(root: str | Path) -> RetrievalFixtures:
    """Load and cross-validate the checked-in v7 retrieval corpus."""
    fixture_root = Path(root)
    records = _load_jsonl(fixture_root / "records.jsonl")
    events = _load_jsonl(fixture_root / "events.jsonl")
    queries = _load_jsonl(fixture_root / "queries.jsonl")
    _validate_records(records)
    _validate_events(events, records)
    _validate_queries(queries, records)
    return RetrievalFixtures(records, events, queries, _fixture_digest(fixture_root))


def _results_by_query(
    queries: Sequence[Mapping[str, Any]], results: Mapping[str, Mapping[str, Any]]
) -> None:
    expected = {query["query_id"] for query in queries}
    if set(results) != expected:
        raise BenchmarkInputError("RESULT_QUERY_SET", "result IDs do not match queries")


def calculate_ranking_metrics(
    queries: Sequence[Mapping[str, Any]],
    results: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    """Calculate the exact Task 8 Recall/MRR/nDCG definitions."""
    _results_by_query(queries, results)
    cutoffs = (1, 3, 5, 10)
    recall_values = {cutoff: [] for cutoff in cutoffs}
    reciprocal_ranks: list[float] = []
    ndcg_values: list[float] = []
    without_relevant = 0
    for query in queries:
        relevant_items = [
            item for item in query["expected_relevant"] if item["grade"] > 0
        ]
        if not relevant_items:
            without_relevant += 1
            continue
        relevant_ids = [item["record_id"] for item in relevant_items]
        grades = {item["record_id"]: item["grade"] for item in relevant_items}
        returned = list(results[query["query_id"]]["returned_record_ids"])
        for cutoff in cutoffs:
            relevant_at_cutoff = set(relevant_ids[:cutoff])
            found = relevant_at_cutoff.intersection(returned[:cutoff])
            recall_values[cutoff].append(len(found) / len(relevant_at_cutoff))
        first_rank = next(
            (
                rank
                for rank, record_id in enumerate(returned[:10], 1)
                if record_id in grades
            ),
            None,
        )
        reciprocal_ranks.append(0.0 if first_rank is None else 1.0 / first_rank)
        dcg = sum(
            (2 ** grades.get(record_id, 0) - 1) / math.log2(rank + 1)
            for rank, record_id in enumerate(returned[:10], 1)
        )
        ideal_grades = sorted(grades.values(), reverse=True)[:10]
        idcg = sum(
            (2**grade - 1) / math.log2(rank + 1)
            for rank, grade in enumerate(ideal_grades, 1)
        )
        ndcg_values.append(dcg / idcg)
    evaluated = len(queries) - without_relevant
    return {
        "evaluated_queries": evaluated,
        "mrr_at_10": _mean(reciprocal_ranks),
        "ndcg_at_10": _mean(ndcg_values),
        "queries_without_relevant": without_relevant,
        "recall_at": {str(cutoff): _mean(recall_values[cutoff]) for cutoff in cutoffs},
    }


def _safe_ratio(numerator: int, denominator: int) -> float:
    return 1.0 if denominator == 0 else numerator / denominator


def _mean(values: Sequence[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def calculate_quality_metrics(
    queries: Sequence[Mapping[str, Any]],
    results: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    """Calculate temporal, contradiction, abstention, and token evidence metrics."""
    _results_by_query(queries, results)
    temporal_returned = 0
    temporal_valid = 0
    invalidated_checks = 0
    invalidated_excluded = 0
    contradiction_checks = 0
    contradiction_successes = 0
    excluded_citation_checks = 0
    excluded_citations_absent = 0
    tp = fp = tn = fn = 0
    total_tokens = 0
    required_citations = 0
    retained_required = 0
    per_query_tokens: dict[str, int] = {}
    for query in queries:
        query_id = query["query_id"]
        result = results[query_id]
        returned = list(result["returned_record_ids"])
        citations = set(result["citation_record_ids"])
        returned_evidence = set(returned) | citations
        for record_id in query.get("expected_excluded_citations", ()):
            excluded_citation_checks += 1
            excluded_citations_absent += record_id not in returned_evidence
        statuses = result.get("citation_statuses", {})
        temporal = query.get("temporal_expectation")
        if temporal is not None:
            valid = set(temporal["valid_record_ids"])
            temporal_returned += len(returned)
            temporal_valid += sum(record_id in valid for record_id in returned)
            if not temporal["include_invalidated"]:
                for record_id in temporal["known_invalidated_record_ids"]:
                    invalidated_checks += 1
                    invalidated_excluded += record_id not in returned_evidence
        contradiction = query.get("contradiction")
        if contradiction is not None:
            for record_id in contradiction["record_ids"]:
                contradiction_checks += 1
                if contradiction["mode"] == "exclude":
                    contradiction_successes += record_id not in returned_evidence
                else:
                    contradiction_successes += (
                        record_id in returned
                        and statuses.get(record_id) == "superseded"
                    )
        expected_abstention = bool(query["expected_abstention"])
        predicted_abstention = bool(result["abstained"])
        if expected_abstention and predicted_abstention:
            tp += 1
        elif not expected_abstention and predicted_abstention:
            fp += 1
        elif expected_abstention:
            fn += 1
        else:
            tn += 1
        tokens = result["rendered_tokens"]
        total_tokens += tokens
        per_query_tokens[query_id] = tokens
        required = set(query["required_citations"])
        required_citations += len(required)
        retained_required += len(required.intersection(citations))
    return {
        "abstention": {
            "false_negative": fn,
            "false_positive": fp,
            "precision": 0.0 if tp + fp == 0 else tp / (tp + fp),
            "recall": _safe_ratio(tp, tp + fn),
            "true_negative": tn,
            "true_positive": tp,
        },
        "contradiction_handling": _safe_ratio(
            contradiction_successes, contradiction_checks
        ),
        "excluded_citation_exclusion_rate": _safe_ratio(
            excluded_citations_absent, excluded_citation_checks
        ),
        "temporal": {
            "invalidated_exclusion_rate": _safe_ratio(
                invalidated_excluded, invalidated_checks
            ),
            "invalidated_records_checked": invalidated_checks,
            "temporal_records_returned": temporal_returned,
            "valid_return_ratio": _safe_ratio(temporal_valid, temporal_returned),
        },
        "tokens": {
            "evidence_coverage": _safe_ratio(retained_required, required_citations),
            "per_query_rendered_tokens": dict(sorted(per_query_tokens.items())),
            "relevant_citations_retained": retained_required,
            "rendered_tokens": total_tokens,
            "required_citations": required_citations,
            "tokens_per_relevant_citation": (
                None if retained_required == 0 else total_tokens / retained_required
            ),
        },
    }


def _validate_result(
    query: Mapping[str, Any],
    raw: Mapping[str, Any],
    *,
    allowed_record_ids: frozenset[str] | None = None,
) -> dict[str, Any]:
    required_fields = {
        "abstained",
        "citation_record_ids",
        "citation_statuses",
        "provider_statuses",
        "provider_timings_ns",
        "rendered_tokens",
        "returned_record_ids",
    }
    if not isinstance(raw, Mapping) or not required_fields.issubset(raw):
        raise BenchmarkInputError("INVALID_RESULT", query["query_id"])
    returned = raw["returned_record_ids"]
    citations = raw["citation_record_ids"]
    if not _is_text_list(returned) or not _is_text_list(citations):
        raise BenchmarkInputError("INVALID_RESULT_IDS", query["query_id"])
    if allowed_record_ids is not None and not (set(returned) | set(citations)).issubset(
        allowed_record_ids
    ):
        raise BenchmarkInputError("UNKNOWN_RESULT_RECORD", query["query_id"])
    abstained = raw["abstained"]
    if not isinstance(abstained, bool):
        raise BenchmarkInputError("INVALID_ABSTENTION_RESULT", query["query_id"])
    tokens = raw["rendered_tokens"]
    if not _plain_int(tokens) or not 0 <= tokens <= query["token_budget"]:
        raise BenchmarkInputError("TOKEN_BUDGET_EXCEEDED", query["query_id"])
    statuses = raw["citation_statuses"]
    if (
        not isinstance(statuses, Mapping)
        or not set(statuses).issubset(citations)
        or any(value not in _CITATION_STATES for value in statuses.values())
    ):
        raise BenchmarkInputError("INVALID_CITATION_STATUS", query["query_id"])
    if abstained and (returned or citations or statuses or tokens != 0):
        raise BenchmarkInputError("INVALID_ABSTENTION_RESULT", query["query_id"])
    provider_statuses = raw["provider_statuses"]
    if (
        not isinstance(provider_statuses, Mapping)
        or not provider_statuses
        or any(
            not isinstance(key, str) or value not in _PROVIDER_STATES
            for key, value in provider_statuses.items()
        )
    ):
        raise BenchmarkInputError("INVALID_PROVIDER_STATUS", query["query_id"])
    lexical_status = provider_statuses.get("lexical")
    if lexical_status is None:
        raise BenchmarkInputError("LEXICAL_STATUS_REQUIRED", query["query_id"])
    if lexical_status in {"failed", "unavailable"} and not abstained:
        raise BenchmarkInputError("LEXICAL_UNAVAILABLE_MUST_ABSTAIN", query["query_id"])
    timings = raw["provider_timings_ns"]
    if not isinstance(timings, Mapping) or any(
        not isinstance(key, str) or not _plain_int(value) or value < 0
        for key, value in timings.items()
    ):
        raise BenchmarkInputError("INVALID_PROVIDER_TIMING", query["query_id"])
    if set(timings) != set(provider_statuses):
        raise BenchmarkInputError("PROVIDER_TIMING_STATUS_MISMATCH", query["query_id"])
    return {
        "abstained": abstained,
        "citation_record_ids": list(citations),
        "citation_statuses": dict(sorted(statuses.items())),
        "provider_statuses": dict(sorted(provider_statuses.items())),
        "provider_timings_ns": dict(sorted(timings.items())),
        "rendered_tokens": tokens,
        "returned_record_ids": list(returned),
    }


def _quality_payload(result: Mapping[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in result.items() if key != "provider_timings_ns"}


def _nearest_rank_percentiles(samples: Sequence[int]) -> dict[str, int | None]:
    if not samples:
        return {"p50": None, "p95": None, "p99": None}
    ordered = sorted(samples)

    def percentile(fraction: float) -> int:
        return ordered[max(0, math.ceil(fraction * len(ordered)) - 1)]

    return {"p50": percentile(0.50), "p95": percentile(0.95), "p99": percentile(0.99)}


def _text_mapping(name: str, value: Mapping[str, str]) -> dict[str, str]:
    if not isinstance(value, Mapping) or any(
        not isinstance(key, str) or not key or not isinstance(item, str) or not item
        for key, item in value.items()
    ):
        raise BenchmarkInputError("INVALID_METADATA", name)
    return dict(sorted(value.items()))


def _provider_degradation_diagnostics(
    queries: Sequence[Mapping[str, Any]],
    results: Mapping[str, Mapping[str, Any]],
    mode: str,
) -> dict[str, Any]:
    degraded_states = {"degraded", "failed", "unavailable"}
    expected: set[str] = set()
    observed: set[str] = set()
    for query in queries:
        query_id = query["query_id"]
        expected.update(
            f"{query_id}:{provider}"
            for provider in query["expected_provider_degradation"][mode]
        )
        observed.update(
            f"{query_id}:{provider}"
            for provider, status in results[query_id]["provider_statuses"].items()
            if status in degraded_states
        )
    return {
        "expected": len(expected),
        "missing": sorted(expected - observed),
        "observed_expected": len(expected & observed),
        "unexpected": sorted(observed - expected),
    }


def run_benchmark(
    fixtures: RetrievalFixtures,
    retrievers: Mapping[str, Callable[[dict[str, Any]], Mapping[str, Any]]],
    *,
    version_identifier: str,
    manifests: Mapping[str, str],
    config_hashes: Mapping[str, str],
    provider_availability: Mapping[str, str],
    clock_ns: Callable[[], int] = time.perf_counter_ns,
) -> dict[str, Any]:
    """Run both benchmark modes for five warmups and thirty timed iterations."""
    if set(retrievers) != set(BENCHMARK_MODES) or any(
        not callable(retrievers[mode]) for mode in BENCHMARK_MODES
    ):
        raise BenchmarkInputError(
            "RETRIEVER_MODES", "both benchmark modes are required"
        )
    if not isinstance(version_identifier, str) or not version_identifier:
        raise BenchmarkInputError("INVALID_METADATA", "version_identifier")
    manifest_values = _text_mapping("manifests", manifests)
    config_values = _text_mapping("config_hashes", config_hashes)
    if any(not _SHA256_RE.fullmatch(value) for value in config_values.values()):
        raise BenchmarkInputError("INVALID_METADATA", "config_hashes")
    availability = _text_mapping("provider_availability", provider_availability)
    if any(value not in _PROVIDER_STATES for value in availability.values()):
        raise BenchmarkInputError("INVALID_METADATA", "provider_availability")
    mode_reports: dict[str, Any] = {}
    allowed_record_ids = frozenset(record["record_id"] for record in fixtures.records)
    for mode in BENCHMARK_MODES:
        retrieve = retrievers[mode]
        for _ in range(WARMUP_ITERATIONS):
            for query in fixtures.queries:
                _validate_result(
                    query, retrieve(query), allowed_record_ids=allowed_record_ids
                )
        baselines: dict[str, dict[str, Any]] = {}
        end_to_end_samples: list[int] = []
        provider_samples: dict[str, list[int]] = {}
        for _ in range(TIMED_ITERATIONS):
            for query in fixtures.queries:
                started = clock_ns()
                raw_result = retrieve(query)
                finished = clock_ns()
                if (
                    not _plain_int(started)
                    or not _plain_int(finished)
                    or finished < started
                ):
                    raise BenchmarkInputError("INVALID_CLOCK", query["query_id"])
                result = _validate_result(
                    query, raw_result, allowed_record_ids=allowed_record_ids
                )
                end_to_end_samples.append(finished - started)
                for provider, elapsed in result["provider_timings_ns"].items():
                    provider_samples.setdefault(provider, []).append(elapsed)
                quality = _quality_payload(result)
                previous = baselines.setdefault(query["query_id"], quality)
                if quality != previous:
                    raise BenchmarkInputError(
                        "NONDETERMINISTIC_RESULT", f"{mode}:{query['query_id']}"
                    )
        ranking = calculate_ranking_metrics(fixtures.queries, baselines)
        quality_metrics = calculate_quality_metrics(fixtures.queries, baselines)
        per_query = [
            {"query_id": query["query_id"], **baselines[query["query_id"]]}
            for query in fixtures.queries
        ]
        mode_reports[mode] = {
            "latency_ns": {
                "end_to_end": _nearest_rank_percentiles(end_to_end_samples),
                "providers": {
                    provider: _nearest_rank_percentiles(samples)
                    for provider, samples in sorted(provider_samples.items())
                },
                "sample_count": len(end_to_end_samples),
            },
            "metrics": {"quality": quality_metrics, "ranking": ranking},
            "per_query": per_query,
            "provider_degradation": _provider_degradation_diagnostics(
                fixtures.queries, baselines, mode
            ),
        }
    return {
        "benchmark": {
            "timed_iterations": TIMED_ITERATIONS,
            "warmup_iterations": WARMUP_ITERATIONS,
        },
        "metadata": {
            "config_hashes": config_values,
            "fixture_digest": fixtures.digest,
            "manifests": manifest_values,
            "provider_availability": availability,
            "version_identifier": version_identifier,
        },
        "modes": mode_reports,
    }


def serialize_benchmark_report(report: Mapping[str, Any]) -> str:
    """Return the stable JSON representation emitted by benchmark automation."""
    try:
        return json.dumps(
            report,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
    except (TypeError, ValueError, RecursionError) as exc:
        raise BenchmarkInputError(
            "REPORT_NOT_JSON", "report is not canonical JSON"
        ) from exc


def _assignment_mapping(values: Sequence[str], option: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for value in values:
        key, separator, item = value.partition("=")
        if not separator or not key or not item or key in result:
            raise BenchmarkInputError(
                "INVALID_CLI_ASSIGNMENT", f"{option} requires unique KEY=VALUE entries"
            )
        result[key] = item
    return result


def _load_retriever(
    specification: str,
) -> Callable[[dict[str, Any]], Mapping[str, Any]]:
    module_name, separator, attribute_name = specification.partition(":")
    if not separator or not module_name or not attribute_name:
        raise BenchmarkInputError(
            "INVALID_ADAPTER", "adapter must use module:callable syntax"
        )
    try:
        value: Any = importlib.import_module(module_name)
        for component in attribute_name.split("."):
            value = getattr(value, component)
    except (AttributeError, ImportError) as exc:
        raise BenchmarkInputError("INVALID_ADAPTER", specification) from exc
    if not callable(value):
        raise BenchmarkInputError("INVALID_ADAPTER", specification)
    return value


def main(
    argv: Sequence[str] | None = None,
    *,
    retrievers: Mapping[str, Callable[[dict[str, Any]], Mapping[str, Any]]]
    | None = None,
    clock_ns: Callable[[], int] = time.perf_counter_ns,
) -> int:
    """Run the benchmark and emit one deterministic JSON document."""
    default_fixtures = (
        Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "retrieval"
    )
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixture-root", type=Path, default=default_fixtures)
    parser.add_argument("--version-identifier", required=True)
    parser.add_argument(
        "--manifest", action="append", required=True, metavar="KEY=VALUE"
    )
    parser.add_argument(
        "--config-hash", action="append", required=True, metavar="KEY=SHA256"
    )
    parser.add_argument(
        "--provider-availability",
        action="append",
        required=True,
        metavar="PROVIDER=STATUS",
    )
    parser.add_argument("--lexical-adapter", metavar="MODULE:CALLABLE")
    parser.add_argument("--fully-enabled-adapter", metavar="MODULE:CALLABLE")
    parser.add_argument("--output", type=Path)
    arguments = parser.parse_args(argv)
    if retrievers is None:
        if not arguments.lexical_adapter or not arguments.fully_enabled_adapter:
            parser.error("--lexical-adapter and --fully-enabled-adapter are required")
        retrievers = {
            "fully_enabled": _load_retriever(arguments.fully_enabled_adapter),
            "lexical_only": _load_retriever(arguments.lexical_adapter),
        }
    report = run_benchmark(
        load_retrieval_fixtures(arguments.fixture_root),
        retrievers,
        version_identifier=arguments.version_identifier,
        manifests=_assignment_mapping(arguments.manifest, "--manifest"),
        config_hashes=_assignment_mapping(arguments.config_hash, "--config-hash"),
        provider_availability=_assignment_mapping(
            arguments.provider_availability, "--provider-availability"
        ),
        clock_ns=clock_ns,
    )
    rendered = serialize_benchmark_report(report)
    if arguments.output is None:
        print(rendered)
    else:
        arguments.output.write_text(rendered + "\n", encoding="utf-8", newline="\n")
    return 0


__all__ = [
    "BENCHMARK_MODES",
    "BenchmarkInputError",
    "FixtureValidationError",
    "RetrievalFixtures",
    "TIMED_ITERATIONS",
    "WARMUP_ITERATIONS",
    "calculate_quality_metrics",
    "calculate_ranking_metrics",
    "load_retrieval_fixtures",
    "main",
    "run_benchmark",
    "serialize_benchmark_report",
]


if __name__ == "__main__":
    raise SystemExit(main())
