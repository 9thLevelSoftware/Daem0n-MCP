"""Deterministic, evidence-backed v7 intelligence operations."""

from __future__ import annotations

import asyncio
import os
import re
import sqlite3
import threading
from collections.abc import Callable, Mapping
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
    deterministic_id,
    event_hash_for,
    event_id_for_hash,
    memory_content_hash,
    parse_canonical_json,
    sha256_json,
)
from ...schema_version import CURRENT_SCHEMA_VERSION
from ...storage_activation import ResolvedActiveDatabase
from ...workspace import Workspace, WorkspaceRegistry
from .application import AdmittedRequest
from .errors import STABLE_ERROR_CODE_SET
from .models import (
    EvidenceRef,
    RecordSummary,
    parse_wire_datetime,
    stored_relative_path,
)
from .public_ids import PublicObjectIdNotFound, PublicObjectIdRepository
from .resources import RuleView
from .runtime_protocols import ActiveStorageResolver, WorkerPool
from .runtime_services import WorkspaceStorageResolver
from .tools import (
    CommunitySummary,
    Contradiction,
    DebateRound,
    DecisionDebateData,
    DecisionSimulationData,
    HierarchicalRecallData,
    HierarchyLayer,
    MemoryVerifyData,
    RuleEvolutionData,
    RuleEvolutionReport,
    VerifiedClaim,
)

_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
_MAX_SCAN_EVENTS = 20_000
_MAX_EVIDENCE_RECORDS = 10_000
_MAX_GOVERNANCE_EVENTS = 5_000
_MAX_COMMUNITIES = 1_000
_MAX_COMMUNITY_MEMBERS = 20_000
_CLAIM_SPLIT_RE = re.compile(r"(?<=[.!?])\s+|[\r\n]+")
_WORD_RE = re.compile(r"[A-Za-z0-9]+(?:[-_][A-Za-z0-9]+)*")
_NEGATIONS = frozenset({"no", "not", "never", "none", "without"})
_STOP_WORDS = frozenset(
    {
        "a",
        "an",
        "and",
        "are",
        "as",
        "at",
        "be",
        "been",
        "by",
        "for",
        "from",
        "has",
        "have",
        "in",
        "is",
        "it",
        "of",
        "on",
        "or",
        "that",
        "the",
        "this",
        "to",
        "was",
        "were",
        "will",
        "with",
    }
)
_EVENT_COLUMNS = (
    "event_id,workspace_id,stream_id,stream_kind,stream_version,event_type,"
    "event_schema_version,occurred_at_us,recorded_at_us,actor_type,actor_id,"
    "causation_event_id,correlation_id,payload_json,payload_hash,"
    "previous_event_hash,event_hash"
)
_CORE_REQUIRED_TABLES = frozenset({"memory_events", "memory_records", "schema_version"})


class IntelligenceOperationError(RuntimeError):
    """Stable, path-free failure understood by the shared v7 router."""

    def __init__(self, code: str) -> None:
        if code not in STABLE_ERROR_CODE_SET:
            raise ValueError("intelligence operation error code is not stable")
        self.code = code
        super().__init__(code)


class _WorkerCancelledError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class _RecordEvidence:
    record: RecordSummary
    content: str
    worked: bool | None
    event_id: str
    created_at_us: int
    recorded_at_us: int


def _default_clock() -> datetime:
    return datetime.now(timezone.utc)


def _default_worker_pool() -> BoundedWorkerPool:
    return BoundedWorkerPool(
        max_workers=4,
        thread_name_prefix="daem0nmcp-v7-intelligence",
    )


@dataclass(frozen=True, slots=True)
class IntelligenceOperationDependencies:
    """Owned dependencies for canonical deterministic intelligence."""

    storage_resolver: ActiveStorageResolver = field(
        default_factory=WorkspaceStorageResolver
    )
    clock: Callable[[], datetime] = field(default=_default_clock)
    worker_pool: WorkerPool = field(default_factory=_default_worker_pool)

    def __post_init__(self) -> None:
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
) -> None:
    if (
        not isinstance(workspace, Workspace)
        or not isinstance(request, AdmittedRequest)
        or request.tool_name != tool_name
        or request.workspace_id != workspace.workspace_id
    ):
        raise IntelligenceOperationError("UNAUTHORIZED_WORKSPACE")
    try:
        canonical = workspace.root.resolve(strict=True)
        registered = WorkspaceRegistry([canonical], default_root=canonical).default
        exact = os.path.normcase(str(canonical)) == os.path.normcase(
            str(workspace.root)
        )
    except (OSError, RuntimeError, TypeError, ValueError):
        raise IntelligenceOperationError("UNAUTHORIZED_WORKSPACE") from None
    if registered.workspace_id != workspace.workspace_id or not exact:
        raise IntelligenceOperationError("UNAUTHORIZED_WORKSPACE")


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
        raise IntelligenceOperationError("WORKSPACE_PATH_ESCAPE") from None


def _open_database(path: Path, *, writable: bool = False) -> sqlite3.Connection:
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
        if not writable:
            connection.execute("PRAGMA query_only=ON")
        connection.execute("PRAGMA foreign_keys=ON")
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
            or not _CORE_REQUIRED_TABLES.issubset(tables)
        ):
            raise IntelligenceOperationError("CAPABILITY_DEGRADED")
        return connection
    except IntelligenceOperationError:
        if connection is not None:
            connection.close()
        raise
    except Exception:
        if connection is not None:
            connection.close()
        raise IntelligenceOperationError("CAPABILITY_DEGRADED") from None


async def _run_read(
    dependencies: IntelligenceOperationDependencies,
    operation: Callable[[], Any],
) -> Any:
    worker = asyncio.create_task(dependencies.worker_pool.run(operation))
    try:
        return await asyncio.shield(worker)
    except asyncio.CancelledError as cancellation:
        try:
            await _await_worker_uninterruptibly(worker)
        except asyncio.CancelledError:
            pass
        except Exception:
            pass
        raise cancellation
    except BoundedWorkerBusyError as exc:
        raise IntelligenceOperationError("TASK_REQUIRED") from exc


async def _await_worker_uninterruptibly(worker: asyncio.Task[Any]) -> Any:
    """Drain owned work despite any number of caller cancellation requests."""

    while True:
        try:
            return await asyncio.shield(worker)
        except asyncio.CancelledError:
            if worker.done():
                return worker.result()


async def _run_mutation(
    dependencies: IntelligenceOperationDependencies,
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
    except BoundedWorkerBusyError as exc:
        raise IntelligenceOperationError("TASK_REQUIRED") from exc


def _datetime_us(value: object) -> int:
    try:
        value = parse_wire_datetime(value)
        delta = value.astimezone(timezone.utc) - _EPOCH
        result = (delta.days * 86_400 + delta.seconds) * 1_000_000 + delta.microseconds
    except (OverflowError, ValueError):
        raise IntelligenceOperationError("INVALID_ARGUMENT") from None
    if not -(2**63) <= result <= 2**63 - 1:
        raise IntelligenceOperationError("INVALID_ARGUMENT")
    return result


def _datetime_from_us(value: object) -> datetime:
    if isinstance(value, bool) or not isinstance(value, int):
        raise IntelligenceOperationError("CAPABILITY_DEGRADED")
    try:
        return _EPOCH + timedelta(microseconds=value)
    except (OverflowError, ValueError):
        raise IntelligenceOperationError("CAPABILITY_DEGRADED") from None


def _now(dependencies: IntelligenceOperationDependencies) -> datetime:
    try:
        value = dependencies.clock()
        if not isinstance(value, datetime) or value.tzinfo is None:
            raise ValueError
        return value.astimezone(timezone.utc)
    except Exception:
        raise IntelligenceOperationError("CAPABILITY_DEGRADED") from None


def _verified_event(row: sqlite3.Row) -> dict[str, Any]:
    try:
        payload = parse_canonical_json(str(row["payload_json"]))
        if not isinstance(payload, dict):
            raise ValueError
        if sha256_json(payload) != row["payload_hash"]:
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
        raise IntelligenceOperationError("CAPABILITY_DEGRADED") from None


def _safe_text(value: object) -> str:
    # Stored user text reads back as stored (UD-3).
    if not isinstance(value, str) or not value:
        raise IntelligenceOperationError("CAPABILITY_DEGRADED")
    return value


def _record_evidence_from_events(
    connection: sqlite3.Connection,
    workspace_id: str,
    *,
    categories: frozenset[str] | None = None,
    valid_at_us: int | None = None,
    transaction_at_us: int | None = None,
) -> list[_RecordEvidence]:
    parameters: list[object] = [workspace_id]
    transaction_filter = ""
    if transaction_at_us is not None:
        transaction_filter = " AND recorded_at_us<=?"
        parameters.append(transaction_at_us)
    rows = connection.execute(
        f"SELECT {_EVENT_COLUMNS} FROM memory_events WHERE workspace_id=? "
        "AND stream_kind='memory'"
        + transaction_filter
        + " ORDER BY stream_id,stream_version LIMIT ?",
        (*parameters, _MAX_SCAN_EVENTS + 1),
    ).fetchall()
    if len(rows) > _MAX_SCAN_EVENTS:
        raise IntelligenceOperationError("TASK_REQUIRED")

    selected: dict[str, tuple[sqlite3.Row, dict[str, Any], int]] = {}
    first_recorded: dict[str, int] = {}
    previous_hash: dict[str, str] = {}
    previous_version: dict[str, int] = {}
    for row in rows:
        stream_id = str(row["stream_id"])
        version = row["stream_version"]
        if isinstance(version, bool) or not isinstance(version, int):
            raise IntelligenceOperationError("CAPABILITY_DEGRADED")
        expected_version = previous_version.get(stream_id, 0) + 1
        expected_hash = previous_hash.get(stream_id)
        if version != expected_version or row["previous_event_hash"] != expected_hash:
            raise IntelligenceOperationError("CAPABILITY_DEGRADED")
        payload = _verified_event(row)
        previous_version[stream_id] = version
        previous_hash[stream_id] = str(row["event_hash"])
        first_recorded.setdefault(stream_id, int(row["recorded_at_us"]))
        occurred_at = row["occurred_at_us"]
        if valid_at_us is not None and (
            not isinstance(occurred_at, int) or occurred_at > valid_at_us
        ):
            continue
        selected[stream_id] = (row, payload, first_recorded[stream_id])

    records: list[_RecordEvidence] = []
    for stream_id in sorted(selected):
        row, payload, created_at_us = selected[stream_id]
        state = payload.get("record")
        if not isinstance(state, dict):
            raise IntelligenceOperationError("CAPABILITY_DEGRADED")
        record_type = state.get("record_type")
        if record_type == "legacy":
            continue
        if categories is not None and record_type not in categories:
            continue
        if state.get("archived") is True or state.get("deleted_at_us") is not None:
            continue
        content = _safe_text(state.get("content"))
        try:
            tags = state.get("tags", [])
            relative_path = stored_relative_path(state.get("file_path_relative"))
            content_hash = memory_content_hash(state)
            recorded_at_us = int(row["recorded_at_us"])
            created = _datetime_from_us(created_at_us)
            updated = _datetime_from_us(recorded_at_us)
            if created > updated:
                created = updated
            summary = RecordSummary.model_validate(
                {
                    "record_id": stream_id,
                    "record_type": record_type,
                    "excerpt": content[:4000],
                    "tags": tags,
                    "relative_file_path": relative_path,
                    "current_status": "current",
                    "content_hash": content_hash,
                    "created_at": created,
                    "updated_at": updated,
                }
            )
            worked = state.get("worked")
            if worked is not None and not isinstance(worked, bool):
                raise ValueError
        except Exception:
            raise IntelligenceOperationError("CAPABILITY_DEGRADED") from None
        records.append(
            _RecordEvidence(
                record=summary,
                content=content,
                worked=worked,
                event_id=str(row["event_id"]),
                created_at_us=created_at_us,
                recorded_at_us=recorded_at_us,
            )
        )
        if len(records) > _MAX_EVIDENCE_RECORDS:
            raise IntelligenceOperationError("TASK_REQUIRED")
    return records


def _evidence_ref(item: _RecordEvidence, provider: str = "canonical") -> EvidenceRef:
    return EvidenceRef(
        record_id=item.record.record_id,
        event_id=item.event_id,
        content_hash=item.record.content_hash,
        provider=provider,
    )


def _claims(text: str) -> list[str]:
    raw_claims = [item.strip() for item in _CLAIM_SPLIT_RE.split(text)]
    claims: list[str] = []
    for raw in raw_claims:
        if not raw:
            continue
        remaining = raw
        while len(remaining) > 2_000 and len(claims) < 100:
            boundary = remaining.rfind(" ", 0, 2_001)
            if boundary < 1:
                boundary = 2_000
            claims.append(remaining[:boundary].strip())
            remaining = remaining[boundary:].strip()
        if remaining and len(claims) < 100:
            claims.append(remaining)
        if len(claims) == 100:
            break
    return claims or [text[:2_000]]


def _semantic_signature(text: str) -> tuple[frozenset[str], bool]:
    tokens = [token.casefold() for token in _WORD_RE.findall(text)]
    semantic = frozenset(
        token
        for token in tokens
        if token not in _STOP_WORDS and token not in _NEGATIONS
    )
    return semantic, any(token in _NEGATIONS for token in tokens)


def _match_score(claim_terms: frozenset[str], evidence: str) -> tuple[float, bool]:
    evidence_terms, evidence_negated = _semantic_signature(evidence)
    if not claim_terms:
        return 0.0, evidence_negated
    overlap = len(claim_terms & evidence_terms)
    minimum = 1 if len(claim_terms) == 1 else 2
    if overlap < minimum:
        return 0.0, evidence_negated
    return overlap / len(claim_terms), evidence_negated


def _memory_verify_sync(
    dependencies: IntelligenceOperationDependencies,
    workspace: Workspace,
    request: AdmittedRequest,
) -> MemoryVerifyData:
    valid_at = (
        None
        if request.as_of_valid_time is None
        else _datetime_us(request.as_of_valid_time)
    )
    transaction_at = (
        None
        if request.as_of_transaction_time is None
        else _datetime_us(request.as_of_transaction_time)
    )
    categories = None if request.categories is None else frozenset(request.categories)
    try:
        with dependencies.storage_resolver.locked_active(workspace) as active:
            connection = _open_database(_database_path(workspace, active))
            try:
                connection.execute("BEGIN")
                evidence = _record_evidence_from_events(
                    connection,
                    workspace.workspace_id,
                    categories=categories,
                    valid_at_us=valid_at,
                    transaction_at_us=transaction_at,
                )
                verified_claims: list[VerifiedClaim] = []
                contradictions: list[Contradiction] = []
                all_refs: dict[tuple[str, str], EvidenceRef] = {}
                for claim in _claims(request.text):
                    claim_terms, claim_negated = _semantic_signature(claim)
                    matches: list[tuple[float, bool, _RecordEvidence]] = []
                    for item in evidence:
                        score, evidence_negated = _match_score(
                            claim_terms, item.content
                        )
                        if score >= 0.75:
                            matches.append((score, evidence_negated, item))
                    matches.sort(
                        key=lambda entry: (
                            -entry[0],
                            entry[2].record.record_id,
                        )
                    )
                    selected = matches[:32]
                    contradictory = [
                        entry for entry in selected if entry[1] != claim_negated
                    ]
                    supporting = [
                        entry for entry in selected if entry[1] == claim_negated
                    ]
                    if contradictory:
                        status = "contradicted"
                        used = contradictory
                    elif supporting:
                        status = "supported"
                        used = supporting
                    else:
                        status = "unknown"
                        used = []
                    refs = [_evidence_ref(entry[2]) for entry in used]
                    for ref in refs:
                        all_refs[(ref.record_id, ref.event_id)] = ref
                    verified_claims.append(
                        VerifiedClaim.model_validate(
                            {"claim": claim, "status": status, "evidence_refs": refs}
                        )
                    )
                    if status == "contradicted":
                        contradictions.append(
                            Contradiction(
                                claim=claim,
                                explanation=(
                                    "Canonical memory evidence matches the claim "
                                    "terms but differs in negation."
                                ),
                                evidence_refs=refs,
                            )
                        )
                statuses = {claim.status for claim in verified_claims}
                if statuses == {"supported"}:
                    overall = "supported"
                elif statuses == {"contradicted"}:
                    overall = "contradicted"
                elif statuses == {"unknown"}:
                    overall = "unknown"
                else:
                    overall = "mixed"
                return MemoryVerifyData.model_validate(
                    {
                        "claims": verified_claims,
                        "evidence_refs": list(all_refs.values())[:200],
                        "contradictions": contradictions,
                        "overall_status": overall,
                    }
                )
            finally:
                if connection.in_transaction:
                    connection.rollback()
                connection.close()
    except IntelligenceOperationError:
        raise
    except Exception as exc:
        code = getattr(exc, "code", None)
        if isinstance(code, str) and code in STABLE_ERROR_CODE_SET:
            raise IntelligenceOperationError(code) from None
        raise IntelligenceOperationError("CAPABILITY_DEGRADED") from None


def _relevant_records(
    subject: _RecordEvidence,
    records: list[_RecordEvidence],
    *,
    limit: int = 100,
) -> list[_RecordEvidence]:
    terms, _negated = _semantic_signature(subject.content)
    scored: list[tuple[float, _RecordEvidence]] = []
    for item in records:
        if item.record.record_id == subject.record.record_id:
            continue
        score, _ = _match_score(terms, item.content)
        if score >= 0.4:
            scored.append((score, item))
    scored.sort(key=lambda entry: (-entry[0], entry[1].record.record_id))
    return [entry[1] for entry in scored[:limit]]


def _decision_simulate_sync(
    dependencies: IntelligenceOperationDependencies,
    workspace: Workspace,
    request: AdmittedRequest,
) -> DecisionSimulationData:
    try:
        with dependencies.storage_resolver.locked_active(workspace) as active:
            connection = _open_database(_database_path(workspace, active))
            try:
                connection.execute("BEGIN")
                current = _record_evidence_from_events(
                    connection,
                    workspace.workspace_id,
                )
                current_by_id = {item.record.record_id: item for item in current}
                decision = current_by_id.get(request.record_id)
                if decision is None:
                    raise IntelligenceOperationError("NOT_FOUND")
                if decision.record.record_type != "decision":
                    raise IntelligenceOperationError("INVALID_ARGUMENT")
                comparison_at = (
                    decision.created_at_us
                    if request.as_of_transaction_time is None
                    else _datetime_us(request.as_of_transaction_time)
                )
                then = _record_evidence_from_events(
                    connection,
                    workspace.workspace_id,
                    transaction_at_us=comparison_at,
                )
                then_by_id = {item.record.record_id: item for item in then}
                historical_decision = then_by_id.get(request.record_id)
                if historical_decision is None:
                    raise IntelligenceOperationError("NOT_FOUND")
                then_context = _relevant_records(historical_decision, then)
                current_context = _relevant_records(decision, current)

                then_context_by_id = {
                    item.record.record_id: item for item in then_context
                }
                current_context_by_id = {
                    item.record.record_id: item for item in current_context
                }
                differences: list[str] = []
                for record_id in sorted(
                    set(current_context_by_id) - set(then_context_by_id)
                ):
                    differences.append(
                        f"New evidence after comparison point: {record_id}."
                    )
                for record_id in sorted(
                    set(then_context_by_id) - set(current_context_by_id)
                ):
                    differences.append(
                        f"Evidence absent from current context: {record_id}."
                    )
                for record_id in sorted(
                    set(then_context_by_id) & set(current_context_by_id)
                ):
                    if (
                        then_context_by_id[record_id].record.content_hash
                        != current_context_by_id[record_id].record.content_hash
                    ):
                        differences.append(
                            f"Evidence changed after comparison point: {record_id}."
                        )
                references: dict[tuple[str, str], EvidenceRef] = {}
                for item in [decision, *then_context, *current_context]:
                    reference = _evidence_ref(item, "temporal")
                    references[(reference.record_id, reference.event_id)] = reference
                return DecisionSimulationData(
                    decision=decision.record,
                    then_context=[item.record for item in then_context],
                    current_context=[item.record for item in current_context],
                    differences=differences[:100],
                    evidence_refs=list(references.values())[:200],
                )
            finally:
                if connection.in_transaction:
                    connection.rollback()
                connection.close()
    except IntelligenceOperationError:
        raise
    except Exception as exc:
        code = getattr(exc, "code", None)
        if isinstance(code, str) and code in STABLE_ERROR_CODE_SET:
            raise IntelligenceOperationError(code) from None
        raise IntelligenceOperationError("CAPABILITY_DEGRADED") from None


def _json_text_list(value: object) -> list[str]:
    try:
        decoded = parse_canonical_json(str(value))
    except Exception:
        raise IntelligenceOperationError("CAPABILITY_DEGRADED") from None
    if (
        not isinstance(decoded, list)
        or len(decoded) > 50
        or any(not isinstance(item, str) for item in decoded)
    ):
        raise IntelligenceOperationError("CAPABILITY_DEGRADED")
    return decoded


def _rule_view(row: sqlite3.Row) -> RuleView:
    try:
        enabled = row["enabled"]
        if enabled not in (0, 1):
            raise ValueError
        return RuleView(
            rule_id=row["rule_id"],
            trigger=_safe_text(row["trigger"]),
            must_do=_json_text_list(row["must_do_json"]),
            must_not=_json_text_list(row["must_not_json"]),
            ask_first=_json_text_list(row["ask_first_json"]),
            warnings=_json_text_list(row["warnings_json"]),
            priority=row["priority"],
            enabled=bool(enabled),
            created_at=_datetime_from_us(row["created_at_us"]),
        )
    except IntelligenceOperationError:
        raise
    except Exception:
        raise IntelligenceOperationError("CAPABILITY_DEGRADED") from None


def _governance_revision_count(
    connection: sqlite3.Connection,
    workspace_id: str,
    row: sqlite3.Row,
) -> int:
    events = connection.execute(
        f"SELECT {_EVENT_COLUMNS} FROM governance_events "
        "WHERE workspace_id=? AND stream_id=? "
        "ORDER BY stream_version LIMIT ?",
        (workspace_id, row["rule_id"], _MAX_GOVERNANCE_EVENTS + 1),
    ).fetchall()
    if len(events) > _MAX_GOVERNANCE_EVENTS:
        raise IntelligenceOperationError("TASK_REQUIRED")
    previous_hash: str | None = None
    for expected_version, event in enumerate(events, start=1):
        if (
            event["stream_version"] != expected_version
            or event["previous_event_hash"] != previous_hash
            or event["stream_kind"] != "rule"
        ):
            raise IntelligenceOperationError("CAPABILITY_DEGRADED")
        payload = _verified_event(event)
        previous_hash = str(event["event_hash"])
        if expected_version == len(events):
            expected_state = {
                "rule_id": row["rule_id"],
                "trigger": row["trigger"],
                "must_do": _json_text_list(row["must_do_json"]),
                "must_not": _json_text_list(row["must_not_json"]),
                "ask_first": _json_text_list(row["ask_first_json"]),
                "warnings": _json_text_list(row["warnings_json"]),
                "priority": row["priority"],
                "enabled": bool(row["enabled"]),
                "created_at_us": row["created_at_us"],
                "updated_at_us": row["updated_at_us"],
            }
            if payload != expected_state or sha256_json(payload) != row["state_hash"]:
                raise IntelligenceOperationError("CAPABILITY_DEGRADED")
    if not events or len(events) != row["stream_version"]:
        raise IntelligenceOperationError("CAPABILITY_DEGRADED")
    return len(events)


def _rule_rows(
    connection: sqlite3.Connection,
    workspace_id: str,
    rule_id: str | None,
) -> list[sqlite3.Row]:
    columns = (
        "rule_id,trigger,must_do_json,must_not_json,ask_first_json,"
        "warnings_json,priority,enabled,stream_version,source_event_id,"
        "created_at_us,updated_at_us,state_hash"
    )
    if rule_id is not None:
        try:
            PublicObjectIdRepository(connection).resolve_public_id(
                workspace_id, "rule", rule_id
            )
        except PublicObjectIdNotFound:
            raise IntelligenceOperationError("NOT_FOUND") from None
        except Exception:
            raise IntelligenceOperationError("CAPABILITY_DEGRADED") from None
        rows = connection.execute(
            f"SELECT {columns} FROM governance_rules "
            "WHERE workspace_id=? AND rule_id=? LIMIT 2",
            (workspace_id, rule_id),
        ).fetchall()
        if not rows:
            raise IntelligenceOperationError("NOT_FOUND")
        if len(rows) != 1:
            raise IntelligenceOperationError("CAPABILITY_DEGRADED")
        return rows
    rows = connection.execute(
        f"SELECT {columns} FROM governance_rules WHERE workspace_id=? "
        "ORDER BY priority DESC,created_at_us DESC,rule_id LIMIT 101",
        (workspace_id,),
    ).fetchall()
    return rows[:100]


def _rule_evolution_sync(
    dependencies: IntelligenceOperationDependencies,
    workspace: Workspace,
    request: AdmittedRequest,
) -> RuleEvolutionData:
    now = _now(dependencies)
    try:
        with dependencies.storage_resolver.locked_active(workspace) as active:
            connection = _open_database(_database_path(workspace, active))
            try:
                connection.execute("BEGIN")
                rows = _rule_rows(
                    connection,
                    workspace.workspace_id,
                    request.rule_id,
                )
                memory_evidence = _record_evidence_from_events(
                    connection, workspace.workspace_id
                )
                reports: list[RuleEvolutionReport] = []
                all_references: dict[tuple[str, str], EvidenceRef] = {}
                for row in rows:
                    rule = _rule_view(row)
                    revisions = _governance_revision_count(
                        connection, workspace.workspace_id, row
                    )
                    terms, _ = _semantic_signature(rule.trigger)
                    matches: list[tuple[float, _RecordEvidence]] = []
                    for item in memory_evidence:
                        score, _ = _match_score(terms, item.content)
                        if score >= 0.4:
                            matches.append((score, item))
                    matches.sort(
                        key=lambda entry: (-entry[0], entry[1].record.record_id)
                    )
                    worked = sum(entry[1].worked is True for entry in matches)
                    failed = sum(entry[1].worked is False for entry in matches)
                    unresolved = sum(entry[1].worked is None for entry in matches)
                    age_days = max(0, (now - rule.created_at).days)
                    if failed and worked:
                        signal = "mixed outcome evidence"
                    elif failed:
                        signal = "failed outcome evidence"
                    elif worked:
                        signal = "worked outcome evidence"
                    else:
                        signal = "no resolved outcome evidence"
                    summary = (
                        f"Rule has {revisions} canonical revision(s) and is "
                        f"{age_days} day(s) old. Bounded outcome evidence: "
                        f"{worked} worked, {failed} failed, {unresolved} unresolved. "
                        f"Review signal: {signal}."
                    )
                    references = [
                        _evidence_ref(entry[1], "canonical") for entry in matches[:2]
                    ]
                    for reference in references:
                        all_references[(reference.record_id, reference.event_id)] = (
                            reference
                        )
                    reports.append(
                        RuleEvolutionReport(
                            rule=rule,
                            summary=summary,
                            evidence_refs=references,
                        )
                    )
                return RuleEvolutionData(
                    reports=reports,
                    analyzed=len(reports),
                    evidence_refs=list(all_references.values())[:200],
                )
            finally:
                if connection.in_transaction:
                    connection.rollback()
                connection.close()
    except IntelligenceOperationError:
        raise
    except Exception as exc:
        code = getattr(exc, "code", None)
        if isinstance(code, str) and code in STABLE_ERROR_CODE_SET:
            raise IntelligenceOperationError(code) from None
        raise IntelligenceOperationError("CAPABILITY_DEGRADED") from None


def _active_graph_generation(
    connection: sqlite3.Connection,
    workspace_id: str,
) -> int:
    rows = connection.execute(
        "SELECT generation FROM projection_manifests WHERE workspace_id=? "
        "AND projection_name='graph' AND status='active' LIMIT 2",
        (workspace_id,),
    ).fetchall()
    if len(rows) != 1:
        raise IntelligenceOperationError("CAPABILITY_DEGRADED")
    generation = rows[0][0]
    if (
        isinstance(generation, bool)
        or not isinstance(generation, int)
        or generation < 1
    ):
        raise IntelligenceOperationError("CAPABILITY_DEGRADED")
    try:
        from ...retrieval.specialized_projection import (
            SpecializedProjectionBuilder,
        )

        if not SpecializedProjectionBuilder(connection).active_is_current(
            workspace_id, "graph"
        ):
            raise IntelligenceOperationError("CAPABILITY_DEGRADED")
    except IntelligenceOperationError:
        raise
    except Exception:
        raise IntelligenceOperationError("CAPABILITY_DEGRADED") from None
    return generation


def _community_rows(
    connection: sqlite3.Connection,
    workspace_id: str,
    generation: int,
) -> tuple[list[sqlite3.Row], list[sqlite3.Row]]:
    try:
        from .discovery_operations import _validate_community_partition

        _validate_community_partition(connection, workspace_id, generation)
    except IntelligenceOperationError:
        raise
    except Exception as exc:
        code = getattr(exc, "code", None)
        if isinstance(code, str) and code in STABLE_ERROR_CODE_SET:
            raise IntelligenceOperationError(code) from None
        raise IntelligenceOperationError("CAPABILITY_DEGRADED") from None
    partition = connection.execute(
        "SELECT row_count,content_hash FROM discovery_projection_partitions "
        "WHERE workspace_id=? AND projection_name='graph' AND generation=? "
        "AND partition_name='communities' LIMIT 2",
        (workspace_id, generation),
    ).fetchall()
    if len(partition) != 1:
        raise IntelligenceOperationError("CAPABILITY_DEGRADED")
    expected_count, content_hash = partition[0]
    if (
        isinstance(expected_count, bool)
        or not isinstance(expected_count, int)
        or expected_count < 0
        or expected_count > _MAX_COMMUNITIES
        or not isinstance(content_hash, str)
        or re.fullmatch(r"[0-9a-f]{64}", content_hash) is None
    ):
        raise IntelligenceOperationError("TASK_REQUIRED")
    communities = connection.execute(
        "SELECT community.community_id,community.label,community.level,"
        "community.member_count,community.parent_community_id,"
        "mapping.source_key,mapping.projection_generation "
        "FROM discovery_communities AS community "
        "LEFT JOIN public_object_ids AS mapping "
        "ON mapping.public_id=community.community_id "
        "AND mapping.workspace_id=community.workspace_id "
        "AND mapping.object_kind='community' "
        "WHERE community.workspace_id=? AND community.graph_generation=? "
        "ORDER BY community.level,community.community_id LIMIT ?",
        (workspace_id, generation, _MAX_COMMUNITIES + 1),
    ).fetchall()
    if len(communities) > _MAX_COMMUNITIES or len(communities) != expected_count:
        raise IntelligenceOperationError("CAPABILITY_DEGRADED")
    community_ids = {str(row["community_id"]) for row in communities}
    if len(community_ids) != len(communities):
        raise IntelligenceOperationError("CAPABILITY_DEGRADED")
    for row in communities:
        if (
            row["source_key"] is None
            or row["projection_generation"] != generation
            or (
                row["parent_community_id"] is not None
                and str(row["parent_community_id"]) not in community_ids
            )
        ):
            raise IntelligenceOperationError("CAPABILITY_DEGRADED")
    members = connection.execute(
        "SELECT community_id,record_id FROM discovery_community_members "
        "WHERE workspace_id=? AND graph_generation=? "
        "ORDER BY community_id,record_id LIMIT ?",
        (workspace_id, generation, _MAX_COMMUNITY_MEMBERS + 1),
    ).fetchall()
    if len(members) > _MAX_COMMUNITY_MEMBERS:
        raise IntelligenceOperationError("TASK_REQUIRED")
    counts = dict.fromkeys(community_ids, 0)
    for member in members:
        community_id = str(member["community_id"])
        if community_id not in counts:
            raise IntelligenceOperationError("CAPABILITY_DEGRADED")
        counts[community_id] += 1
    if any(
        counts[str(row["community_id"])] != row["member_count"] for row in communities
    ):
        raise IntelligenceOperationError("CAPABILITY_DEGRADED")
    return communities, members


def _community_summary(row: sqlite3.Row, generation: int) -> CommunitySummary:
    try:
        return CommunitySummary(
            community_id=row["community_id"],
            label=_safe_text(row["label"]),
            level=row["level"],
            member_count=row["member_count"],
            parent_community_id=row["parent_community_id"],
            manifest_generation=generation,
        )
    except IntelligenceOperationError:
        raise
    except Exception:
        raise IntelligenceOperationError("CAPABILITY_DEGRADED") from None


def _hierarchical_recall_sync(
    dependencies: IntelligenceOperationDependencies,
    workspace: Workspace,
    request: AdmittedRequest,
) -> HierarchicalRecallData:
    try:
        with dependencies.storage_resolver.locked_active(workspace) as active:
            connection = _open_database(_database_path(workspace, active))
            try:
                connection.execute("BEGIN")
                generation = _active_graph_generation(
                    connection, workspace.workspace_id
                )
                communities, members = _community_rows(
                    connection, workspace.workspace_id, generation
                )
                records = _record_evidence_from_events(
                    connection, workspace.workspace_id
                )
                by_id = {item.record.record_id: item for item in records}
                members_by_community: dict[str, list[_RecordEvidence]] = {
                    str(row["community_id"]): [] for row in communities
                }
                for member in members:
                    record = by_id.get(str(member["record_id"]))
                    if record is None:
                        raise IntelligenceOperationError("CAPABILITY_DEGRADED")
                    members_by_community[str(member["community_id"])].append(record)

                query_terms, _ = _semantic_signature(request.query)
                direct: list[tuple[float, sqlite3.Row]] = []
                fallback: list[tuple[float, sqlite3.Row]] = []
                for row in communities:
                    label_score, _ = _match_score(query_terms, str(row["label"]))
                    if label_score > 0:
                        direct.append((label_score, row))
                    member_score = max(
                        (
                            _match_score(query_terms, item.content)[0]
                            for item in members_by_community[str(row["community_id"])]
                        ),
                        default=0.0,
                    )
                    if member_score > 0:
                        fallback.append((member_score, row))
                candidates = direct if direct else fallback
                candidates.sort(
                    key=lambda entry: (
                        -entry[0],
                        entry[1]["level"],
                        entry[1]["community_id"],
                    )
                )
                selected_rows = [entry[1] for entry in candidates[: request.limit]]
                selected_communities = [
                    _community_summary(row, generation) for row in selected_rows
                ]
                layered: dict[int, list[RecordSummary]] = {}
                seen: set[str] = set()
                references: dict[tuple[str, str], EvidenceRef] = {}
                remaining = request.limit
                for row in selected_rows:
                    community_members = members_by_community[str(row["community_id"])]
                    for item in community_members:
                        reference = _evidence_ref(item, "graph")
                        references[(reference.record_id, reference.event_id)] = (
                            reference
                        )
                        if (
                            not request.include_members
                            or remaining <= 0
                            or item.record.record_id in seen
                        ):
                            continue
                        seen.add(item.record.record_id)
                        layered.setdefault(int(row["level"]), []).append(item.record)
                        remaining -= 1
                layers = [
                    HierarchyLayer(level=level, records=layered[level])
                    for level in sorted(layered)
                ]
                return HierarchicalRecallData(
                    layers=layers,
                    communities=selected_communities,
                    evidence_refs=list(references.values())[:200],
                )
            finally:
                if connection.in_transaction:
                    connection.rollback()
                connection.close()
    except IntelligenceOperationError:
        raise
    except Exception as exc:
        code = getattr(exc, "code", None)
        if isinstance(code, str) and code in STABLE_ERROR_CODE_SET:
            raise IntelligenceOperationError(code) from None
        raise IntelligenceOperationError("CAPABILITY_DEGRADED") from None


def _position_evidence(
    position: str,
    records: list[_RecordEvidence],
) -> list[tuple[float, _RecordEvidence]]:
    terms, _ = _semantic_signature(position)
    selected: list[tuple[float, _RecordEvidence]] = []
    for item in records:
        score, _ = _match_score(terms, item.content)
        if score < 0.4:
            continue
        if item.worked is True:
            score = min(1.0, score + 0.2)
        elif item.worked is False:
            score = max(0.0, score - 0.2)
        selected.append((score, item))
    selected.sort(key=lambda entry: (-entry[0], entry[1].record.record_id))
    return selected[:16]


def _debate_argument(
    perspective: str,
    position: str,
    evidence: list[tuple[float, _RecordEvidence]],
    citations: Mapping[str, int],
) -> tuple[str, float]:
    score = sum(entry[0] for entry in evidence) / len(evidence) if evidence else 0.0
    labels = [f"[E{citations[entry[1].record.record_id]}]" for entry in evidence]
    rendered_labels = ", ".join(labels) if labels else "none"
    bounded_position = position[:1_200]
    argument = (
        f"{perspective} position: {bounded_position}. Canonical evidence: "
        f"{rendered_labels}. Weighted score: {score:.3f}."
    )
    return argument[:2_000], score


def _debate_result_from_state(
    state: Mapping[str, Any],
    record_id: str,
    event_id: str,
) -> DecisionDebateData:
    try:
        context = state["context"]
        if not isinstance(context, Mapping):
            raise ValueError
        debate = context["decision_debate"]
        if not isinstance(debate, Mapping):
            raise ValueError
        rounds = [DebateRound.model_validate(item) for item in debate["rounds"]]
        references = [
            EvidenceRef.model_validate(item) for item in debate["evidence_refs"]
        ]
        synthesis = state["content"]
        if synthesis != debate["synthesis"]:
            raise ValueError
        return DecisionDebateData(
            rounds=rounds,
            synthesis=synthesis,
            consensus_record_id=record_id,
            event_ids=[event_id],
            evidence_refs=references,
        )
    except Exception:
        raise IntelligenceOperationError("CAPABILITY_DEGRADED") from None


def _decision_debate_sync(
    dependencies: IntelligenceOperationDependencies,
    workspace: Workspace,
    request: AdmittedRequest,
    cancelled: threading.Event,
) -> DecisionDebateData:
    now = _now(dependencies)
    now_us = _datetime_us(now)
    request_hash = sha256_json(
        {
            "topic": request.topic,
            "advocate_position": request.advocate_position,
            "challenger_position": request.challenger_position,
            "max_rounds": request.max_rounds,
        }
    )
    correlation = deterministic_id(
        "job",
        "decision-debate-idempotency",
        workspace.workspace_id,
        request.idempotency_key,
    )
    record_id = deterministic_id(
        "mem",
        "decision-debate-consensus",
        workspace.workspace_id,
        request.idempotency_key,
    )
    if cancelled.is_set():
        raise _WorkerCancelledError()
    try:
        with dependencies.storage_resolver.locked_active(workspace) as active:
            connection = _open_database(
                _database_path(workspace, active), writable=True
            )
            try:
                connection.execute("BEGIN IMMEDIATE")
                existing = connection.execute(
                    f"SELECT {_EVENT_COLUMNS} FROM memory_events "
                    "WHERE workspace_id=? AND correlation_id=? "
                    "AND event_type='memory.created' LIMIT 2",
                    (workspace.workspace_id, correlation),
                ).fetchall()
                if len(existing) > 1:
                    raise IntelligenceOperationError("CAPABILITY_DEGRADED")
                if existing:
                    row = existing[0]
                    payload = _verified_event(row)
                    if (
                        row["stream_id"] != record_id
                        or row["stream_version"] != 1
                        or row["previous_event_hash"] is not None
                        or payload.get("idempotency_request_hash") != request_hash
                    ):
                        raise IntelligenceOperationError("IDEMPOTENCY_CONFLICT")
                    state = payload.get("record")
                    projection = connection.execute(
                        "SELECT source_event_id,state_hash FROM memory_records "
                        "WHERE workspace_id=? AND record_id=? LIMIT 2",
                        (workspace.workspace_id, record_id),
                    ).fetchall()
                    if (
                        not isinstance(state, Mapping)
                        or len(projection) != 1
                        or projection[0]["source_event_id"] != row["event_id"]
                        or projection[0]["state_hash"]
                        != sha256_json(
                            {
                                "content": {
                                    "record_type": state.get("record_type"),
                                    "legacy_type": state.get("legacy_type"),
                                    "content": state.get("content"),
                                    "rationale": state.get("rationale"),
                                    "context": state.get("context", {}),
                                    "tags": state.get("tags", []),
                                    "file_path": state.get("file_path"),
                                    "file_path_relative": state.get(
                                        "file_path_relative"
                                    ),
                                    "outcome": state.get("outcome"),
                                    "worked": state.get("worked"),
                                },
                                "is_permanent": bool(state.get("is_permanent", False)),
                                "pinned": bool(state.get("pinned", False)),
                                "archived": bool(state.get("archived", False)),
                                "deleted": state.get("deleted_at_us") is not None,
                                "source_client": state.get("source_client"),
                                "source_model": state.get("source_model"),
                            }
                        )
                    ):
                        raise IntelligenceOperationError("CAPABILITY_DEGRADED")
                    return _debate_result_from_state(
                        state, record_id, str(row["event_id"])
                    )

                records = _record_evidence_from_events(
                    connection, workspace.workspace_id
                )
                advocate_evidence = _position_evidence(
                    request.advocate_position, records
                )
                challenger_evidence = _position_evidence(
                    request.challenger_position, records
                )
                ordered_records: dict[str, _RecordEvidence] = {}
                for _score, item in [*advocate_evidence, *challenger_evidence]:
                    ordered_records.setdefault(item.record.record_id, item)
                citations = {
                    record_id_value: index
                    for index, record_id_value in enumerate(ordered_records, 1)
                }
                advocate, advocate_score = _debate_argument(
                    "Advocate",
                    request.advocate_position,
                    advocate_evidence,
                    citations,
                )
                challenger, challenger_score = _debate_argument(
                    "Challenger",
                    request.challenger_position,
                    challenger_evidence,
                    citations,
                )
                rounds = [
                    DebateRound(
                        round_number=round_number,
                        advocate=advocate,
                        challenger=challenger,
                    )
                    for round_number in range(1, request.max_rounds + 1)
                ]
                topic = request.topic[:1_000]
                advocate_position = request.advocate_position[:1_500]
                challenger_position = request.challenger_position[:1_500]
                if advocate_score > challenger_score:
                    conclusion = "the advocate has stronger canonical support"
                elif challenger_score > advocate_score:
                    conclusion = "the challenger has stronger canonical support"
                else:
                    conclusion = (
                        "the positions have balanced or insufficient canonical support"
                    )
                synthesis = (
                    f"Evidence-weighted synthesis for {topic}: {conclusion} "
                    f"({advocate_score:.3f} vs {challenger_score:.3f}). "
                    f"Advocate: {advocate_position}. Challenger: "
                    f"{challenger_position}."
                )[:50_000]
                references = [
                    _evidence_ref(item, "canonical")
                    for item in ordered_records.values()
                ][:32]
                debate_context = {
                    "request_hash": request_hash,
                    "rounds": [
                        round_item.model_dump(mode="json") for round_item in rounds
                    ],
                    "synthesis": synthesis,
                    "evidence_refs": [
                        reference.model_dump(mode="json") for reference in references
                    ],
                }
                state = {
                    "record_type": "decision",
                    "legacy_type": None,
                    "content": synthesis,
                    "rationale": (
                        "Deterministic comparison of canonical memory evidence."
                    ),
                    "context": {"decision_debate": debate_context},
                    "tags": ["decision-debate", "deterministic"],
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
                    "source_client": "daem0nmcp-v7",
                    "source_model": None,
                    "deleted_at_us": None,
                }
                if cancelled.is_set():
                    raise _WorkerCancelledError()
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
                        correlation_id=correlation,
                        expected_stream_version=1,
                        payload={
                            "record": state,
                            "idempotency_request_hash": request_hash,
                        },
                    )
                )
                if cancelled.is_set():
                    raise _WorkerCancelledError()
                connection.commit()
                return _debate_result_from_state(state, record_id, event.event_id)
            except (
                EventStreamConflict,
                IntelligenceOperationError,
                _WorkerCancelledError,
            ):
                if connection.in_transaction:
                    connection.rollback()
                raise
            except Exception:
                if connection.in_transaction:
                    connection.rollback()
                raise IntelligenceOperationError("CAPABILITY_DEGRADED") from None
            finally:
                if connection.in_transaction:
                    connection.rollback()
                connection.close()
    except (IntelligenceOperationError, _WorkerCancelledError):
        raise
    except EventStreamConflict:
        raise IntelligenceOperationError("EVENT_STREAM_CONFLICT") from None
    except Exception as exc:
        code = getattr(exc, "code", None)
        if isinstance(code, str) and code in STABLE_ERROR_CODE_SET:
            raise IntelligenceOperationError(code) from None
        raise IntelligenceOperationError("CAPABILITY_DEGRADED") from None


def build_intelligence_operations(
    dependencies: IntelligenceOperationDependencies,
) -> Mapping[str, Callable[..., Any]]:
    """Return the exact immutable intelligence operation registry."""

    if not isinstance(dependencies, IntelligenceOperationDependencies):
        raise TypeError("dependencies must be IntelligenceOperationDependencies")

    async def decision_debate(
        *, workspace: Workspace, request: AdmittedRequest
    ) -> DecisionDebateData:
        _authorize(workspace, request, "decision_debate")
        return await _run_mutation(
            dependencies,
            lambda cancelled: _decision_debate_sync(
                dependencies, workspace, request, cancelled
            ),
        )

    async def decision_simulate(
        *, workspace: Workspace, request: AdmittedRequest
    ) -> DecisionSimulationData:
        _authorize(workspace, request, "decision_simulate")
        return await _run_read(
            dependencies,
            lambda: _decision_simulate_sync(dependencies, workspace, request),
        )

    async def memory_recall_hierarchical(
        *, workspace: Workspace, request: AdmittedRequest
    ) -> HierarchicalRecallData:
        _authorize(workspace, request, "memory_recall_hierarchical")
        return await _run_read(
            dependencies,
            lambda: _hierarchical_recall_sync(dependencies, workspace, request),
        )

    async def memory_verify(
        *, workspace: Workspace, request: AdmittedRequest
    ) -> MemoryVerifyData:
        _authorize(workspace, request, "memory_verify")
        return await _run_read(
            dependencies,
            lambda: _memory_verify_sync(dependencies, workspace, request),
        )

    async def rule_evolution_analyze(
        *, workspace: Workspace, request: AdmittedRequest
    ) -> RuleEvolutionData:
        _authorize(workspace, request, "rule_evolution_analyze")
        return await _run_read(
            dependencies,
            lambda: _rule_evolution_sync(dependencies, workspace, request),
        )

    return MappingProxyType(
        {
            "decision_debate": decision_debate,
            "decision_simulate": decision_simulate,
            "memory_recall_hierarchical": memory_recall_hierarchical,
            "memory_verify": memory_verify,
            "rule_evolution_analyze": rule_evolution_analyze,
        }
    )


__all__ = [
    "IntelligenceOperationDependencies",
    "IntelligenceOperationError",
    "build_intelligence_operations",
]
