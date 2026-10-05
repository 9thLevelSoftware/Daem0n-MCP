"""Path-private federation validation, budget slicing, and global composition."""

from __future__ import annotations

import os
import sqlite3
import threading
from collections.abc import Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass, replace
from pathlib import Path
from time import perf_counter_ns
from typing import Literal

from ...event_store import sha256_json
from ...retrieval.composer import (
    EvidenceComposer,
    RetentionPolicy,
    SelectedEvidence,
    _normalize_evidence_text,
    query_identifiers,
)
from ...retrieval.runtime import CoreTokenizer
from ...retrieval.types import (
    EvidenceRef as InternalEvidenceRef,
)
from ...retrieval.types import FusedCandidate, RetrievalQuery
from ...storage_activation import DatabaseFileLock
from .models import (
    CitationManifestEntry,
    EvidenceItem,
    EvidenceRef,
    ProviderDiagnostic,
    RecordSummary,
    RetrievalData,
    TokenUsage,
)


class FederatedRetrievalError(RuntimeError):
    """Stable path-free federation failure."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(slots=True)
class _LocalAccessState:
    readers: int = 0
    writer: bool = False
    waiting_writers: int = 0


_ACCESS_MUTEX = threading.Condition(threading.RLock())
_ACCESS_STATES: dict[str, _LocalAccessState] = {}


class FederationAccessLock:
    """Cross-thread/process read-write guard for one origin link ledger."""

    def __init__(
        self,
        database_path: Path,
        mode: Literal["shared", "exclusive"],
    ) -> None:
        if mode not in {"shared", "exclusive"}:
            raise ValueError("federation lock mode is invalid")
        database_path = Path(database_path).resolve(strict=True)
        lock_root = database_path.parent / (f".{database_path.name}.federation-access")
        self._key = os.path.normcase(str(lock_root.resolve(strict=False)))
        self._mode = mode
        self._file_lock = DatabaseFileLock(
            lock_root,
            mode,
            nonblocking=False,
        )
        self._acquired = False

    def acquire(self) -> FederationAccessLock:
        if self._acquired:
            return self
        with _ACCESS_MUTEX:
            state = _ACCESS_STATES.setdefault(self._key, _LocalAccessState())
            if self._mode == "exclusive":
                state.waiting_writers += 1
                try:
                    while state.writer or state.readers:
                        _ACCESS_MUTEX.wait()
                    state.writer = True
                finally:
                    state.waiting_writers -= 1
            else:
                while state.writer or state.waiting_writers:
                    _ACCESS_MUTEX.wait()
                state.readers += 1
        try:
            self._file_lock.acquire()
        except Exception:
            self._release_local()
            raise
        self._acquired = True
        return self

    def _release_local(self) -> None:
        with _ACCESS_MUTEX:
            state = _ACCESS_STATES.get(self._key)
            if state is None:
                return
            if self._mode == "exclusive":
                state.writer = False
            else:
                state.readers -= 1
            if not state.writer and not state.readers and not state.waiting_writers:
                _ACCESS_STATES.pop(self._key, None)
            _ACCESS_MUTEX.notify_all()

    def release(self) -> None:
        if not self._acquired:
            return
        try:
            self._file_lock.release()
        finally:
            self._acquired = False
            self._release_local()

    def __enter__(self) -> FederationAccessLock:
        return self.acquire()

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.release()


def federation_access_lock(
    database_path: Path,
    mode: Literal["shared", "exclusive"],
) -> FederationAccessLock:
    """Return the private authorization/link guard for an active database."""

    return FederationAccessLock(database_path, mode)


@dataclass(frozen=True, slots=True)
class FederatedCandidate:
    """One canonical, authenticated candidate before global composition."""

    record: RecordSummary
    content: str
    channels: tuple[str, ...]
    status: Literal["current", "superseded"]
    evidence_refs: tuple[EvidenceRef, ...]
    applicability: Literal["current", "needs_revalidation", "unverifiable"] | None = (
        None
    )
    changed_bindings: tuple[str, ...] = ()
    utility: float | None = None
    procedure_steps: tuple[str, ...] = ()
    code_bindings: tuple[tuple[str, str | None], ...] = ()
    outcome: str | None = None
    outcome_failed: bool = False
    superseded_by_version_id: str | None = None


@dataclass(frozen=True, slots=True)
class FederatedSourceResult:
    """Bounded candidates and diagnostics from one admitted source."""

    candidates: tuple[FederatedCandidate, ...] = ()
    diagnostics: tuple[ProviderDiagnostic, ...] = ()


def _event_values(row: sqlite3.Row, previous_hash: str | None) -> tuple[object, ...]:
    envelope = {
        "workspace_id": row[1],
        "linked_workspace_id": row[2],
        "stream_version": row[3],
        "event_type": row[4],
        "relationship": row[5],
        "label": row[6],
        "occurred_at_us": row[7],
        "recorded_at_us": row[8],
        "previous_event_hash": previous_hash,
    }
    event_hash = sha256_json(envelope)
    return (f"evt_{event_hash}", *tuple(row)[1:9], previous_hash, event_hash)


def validate_directional_links(
    database_path: Path,
    origin_workspace_id: str,
    linked_workspace_ids: Sequence[str],
) -> None:
    """Require intact, currently-linked origin ledger streams."""

    try:
        connection = sqlite3.connect(
            f"file:{database_path.as_posix()}?mode=ro", uri=True, timeout=5.0
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only=ON")
        for linked_workspace_id in linked_workspace_ids:
            rows = connection.execute(
                "SELECT event_id,workspace_id,linked_workspace_id,stream_version,"
                "event_type,relationship,label,occurred_at_us,recorded_at_us,"
                "previous_event_hash,event_hash FROM workspace_link_events "
                "WHERE workspace_id=? AND linked_workspace_id=? "
                "ORDER BY stream_version LIMIT 10001",
                (origin_workspace_id, linked_workspace_id),
            ).fetchall()
            if not rows or len(rows) > 10_000:
                raise FederatedRetrievalError("UNAUTHORIZED_WORKSPACE")
            previous_hash: str | None = None
            previous_type: str | None = None
            for version, row in enumerate(rows, 1):
                if (
                    row[1] != origin_workspace_id
                    or row[2] != linked_workspace_id
                    or row[3] != version
                    or row[8] != row[7]
                    or row[9] != previous_hash
                    or (version == 1 and row[4] != "workspace.linked")
                    or (
                        previous_type == "workspace.unlinked"
                        and row[4] == "workspace.unlinked"
                    )
                    or tuple(row) != _event_values(row, previous_hash)
                ):
                    raise FederatedRetrievalError("CAPABILITY_DEGRADED")
                previous_hash = str(row[10])
                previous_type = str(row[4])
            if previous_type != "workspace.linked":
                raise FederatedRetrievalError("UNAUTHORIZED_WORKSPACE")
    except FederatedRetrievalError:
        raise
    except sqlite3.Error:
        raise FederatedRetrievalError("CAPABILITY_DEGRADED") from None
    finally:
        with suppress(NameError, sqlite3.Error):
            connection.close()


def sliced_queries(
    query: RetrievalQuery,
    origin_workspace_id: str,
    linked_workspace_ids: Sequence[str],
) -> dict[str, RetrievalQuery]:
    """Allocate the shared candidate budget across every admitted source."""

    ordered = (origin_workspace_id, *sorted(linked_workspace_ids))
    if query.candidate_limit < len(ordered):
        raise FederatedRetrievalError("INVALID_ARGUMENT")

    def allocate(total: int, count: int) -> list[int]:
        values = [1] * count
        remaining = total - sum(values)
        index = 0
        while remaining:
            values[index % len(values)] += 1
            index += 1
            remaining -= 1
        return values

    candidates = allocate(query.candidate_limit, len(ordered))
    return {
        workspace_id: replace(
            query,
            workspace_id=workspace_id,
            limit=candidates[index],
            candidate_limit=candidates[index],
        )
        for index, workspace_id in enumerate(ordered)
    }


def _merged_diagnostics(
    results: Mapping[str, FederatedSourceResult],
) -> list[ProviderDiagnostic]:
    diagnostics: dict[str, ProviderDiagnostic] = {}
    order = {"ready": 0, "degraded": 1, "unavailable": 2, "failed": 3}
    for workspace_id in sorted(results):
        for diagnostic in results[workspace_id].diagnostics:
            current = diagnostics.get(diagnostic.provider)
            if current is None:
                diagnostics[diagnostic.provider] = diagnostic
                continue
            worst = max((current, diagnostic), key=lambda item: order[item.status])
            returned_count = min(
                200, current.returned_count + diagnostic.returned_count
            )
            if worst.status in {"unavailable", "failed"}:
                returned_count = 0
            diagnostics[diagnostic.provider] = ProviderDiagnostic(
                provider=diagnostic.provider,
                status=worst.status,
                manifest_generation=(
                    current.manifest_generation
                    if current.manifest_generation == diagnostic.manifest_generation
                    else None
                ),
                elapsed_ms=current.elapsed_ms + diagnostic.elapsed_ms,
                reason=worst.reason,
                returned_count=returned_count,
            )
    return list(diagnostics.values())


def _fit_excerpt(
    *,
    citation: str,
    content: str,
    prior_lines: Sequence[str],
    token_budget: int,
    tokenizer: CoreTokenizer,
    applicability_label: str = "",
) -> tuple[str, int] | None:
    normalized = _normalize_evidence_text(content)[:8000].strip()
    if not normalized:
        return None

    def rendered(prefix: str) -> tuple[str, int]:
        line = f"{citation} {prefix}" + applicability_label
        text = "\n".join((*prior_lines, line))
        return line, tokenizer.count_tokens(text)

    line, tokens = rendered(normalized)
    if tokens <= token_budget:
        return line, tokens
    low = 1
    high = len(normalized)
    fitted: tuple[str, int] | None = None
    while low <= high:
        middle = (low + high) // 2
        excerpt = normalized[:middle].rstrip()
        if not excerpt:
            low = middle + 1
            continue
        candidate_line, candidate_tokens = rendered(excerpt)
        if candidate_tokens <= token_budget:
            fitted = candidate_line, candidate_tokens
            low = middle + 1
        else:
            high = middle - 1
    return fitted


def _compose_retained(
    ranked: Sequence[tuple[float, str, str, FederatedCandidate]],
    query: RetrievalQuery,
    retention: RetentionPolicy,
    label_applicability: bool,
) -> RetrievalData:
    composer = EvidenceComposer(tokenizer=CoreTokenizer())
    sources = []
    originals = {}
    highest = ranked[0][0] if ranked else 1.0
    for index, (score, _workspace_id, _record_id, item) in enumerate(ranked, 1):
        refs = tuple(
            InternalEvidenceRef(
                record_id=ref.record_id,
                event_id=ref.event_id,
                content_hash=ref.content_hash,
                version_id=ref.version_id,
                relation_path=tuple(ref.relation_path),
                provider=ref.provider,
            )
            for ref in item.evidence_refs
        )
        candidate = FusedCandidate(
            evidence=refs[0],
            evidence_refs=refs,
            score=score / highest,
            channels=frozenset(item.channels),
            channel_ranks=tuple((channel, index) for channel in sorted(item.channels)),
            manifest_generations=tuple(
                (channel, None) for channel in sorted(item.channels)
            ),
        )
        source = SelectedEvidence(
            candidate=candidate,
            content=item.content,
            category=item.record.record_type,
            status=item.status,
            superseded_by_version_id=item.superseded_by_version_id,
            procedure_steps=item.procedure_steps,
            code_bindings=item.code_bindings,
            outcome=item.outcome,
            outcome_failed=item.outcome_failed,
            utility=item.utility,
            applicability=item.applicability,
            changed_bindings=item.changed_bindings,
        )
        sources.append(source)
        originals[id(source)] = item
    ordered = sorted(
        sources, key=lambda source: not composer._priority(source, retention)
    )[: query.limit]
    retained_ids = {id(source) for source in ordered}
    sources = [source for source in sources if id(source) in retained_ids]
    composition = composer.compose(
        sources,
        token_budget=query.token_budget,
        retention=retention,
        label_applicability=label_applicability,
    )
    by_ref: dict[InternalEvidenceRef, list[FederatedCandidate]] = {}
    for source in sources:
        by_ref.setdefault(source.candidate.evidence, []).append(originals[id(source)])
    items = []
    manifest = []
    for evidence in composition.items:
        original = by_ref[evidence.evidence_refs[0]].pop(0)
        evidence_item = EvidenceItem(
            citation=evidence.citation,
            record=original.record,
            bounded_excerpt=evidence.excerpt,
            channels=sorted(evidence.channels),
            score=evidence.score,
            status=evidence.status,
            evidence_refs=list(original.evidence_refs),
            utility=evidence.utility,
            applicability=evidence.applicability,
            changed_bindings=list(evidence.changed_bindings),
        )
        items.append(evidence_item)
        manifest.append(
            CitationManifestEntry(
                citation=evidence_item.citation,
                evidence_refs=evidence_item.evidence_refs,
                channels=evidence_item.channels,
            )
        )
    context = composition.context
    return RetrievalData(
        items=items,
        rendered_context=context.text,
        citation_manifest=manifest,
        abstained=not items,
        abstention_reason=None if items else "NO_POLICY_VALID_EVIDENCE",
        token_usage=TokenUsage(
            budget=query.token_budget,
            requested=min(1_000_000, context.requested_tokens),
            selected=context.selected_tokens,
            rendered=context.rendered_tokens,
            dropped=min(1_000_000, context.dropped_tokens),
        ),
    )


def compose_federated_results(
    results: Mapping[str, FederatedSourceResult],
    query: RetrievalQuery,
    *,
    label_applicability: bool = False,
    retention_mode: Literal["off", "shadow", "apply"] = "off",
) -> RetrievalData:
    """Fuse all authenticated candidates and compose the context exactly once."""

    ranked: list[tuple[float, str, str, FederatedCandidate]] = []
    for workspace_id in sorted(results):
        source_result = results[workspace_id]
        for rank, item in enumerate(source_result.candidates, 1):
            ranked.append(
                (1.0 / (60.0 + rank), workspace_id, item.record.record_id, item)
            )
    ranked.sort(key=lambda value: (-value[0], value[1], value[2]))
    if query.intent is not None and retention_mode != "off":
        started = perf_counter_ns()
        retention_failed = False
        try:
            retained = (
                compose_federated_results(
                    results, query, label_applicability=label_applicability
                )
                if query.intent == "explore"
                else _compose_retained(
                    ranked,
                    query,
                    RetentionPolicy(query.intent, query_identifiers(query.text)),
                    label_applicability,
                )
            )
        except Exception:
            if retention_mode == "apply":
                raise
            retention_failed = True
            retained = None
        result = (
            retained
            if retained is not None
            and (retention_mode == "apply" or query.intent == "explore")
            else compose_federated_results(
                results, query, label_applicability=label_applicability
            )
        )
        return result.model_copy(
            update={
                "provider_diagnostics": [
                    *_merged_diagnostics(results),
                    ProviderDiagnostic(
                        provider="retention",
                        status="degraded" if retention_failed else "ready",
                        manifest_generation=None,
                        elapsed_ms=(perf_counter_ns() - started) / 1_000_000,
                        reason=(
                            "RETENTION_FAILED"
                            if retention_failed
                            else "RETENTION_APPLIED"
                            if retention_mode == "apply"
                            else "RETENTION_SHADOW"
                        ),
                        returned_count=len(retained.items)
                        if retained is not None
                        else 0,
                    ),
                ],
            }
        )
    tokenizer = CoreTokenizer()
    selected: list[EvidenceItem] = []
    manifest: list[CitationManifestEntry] = []
    lines: list[str] = []
    rendered_tokens = 0

    def applicability_label(item: FederatedCandidate) -> str:
        if label_applicability and item.applicability == "needs_revalidation":
            return "\nNeeds revalidation: " + "; ".join(
                _normalize_evidence_text(path) for path in item.changed_bindings
            )
        return ""

    requested_lines = [
        f"[E{index}] {_normalize_evidence_text(item.content)[:8000].strip()}"
        + applicability_label(item)
        for index, (_score, _workspace, _record, item) in enumerate(ranked, 1)
    ]
    requested = min(1_000_000, tokenizer.count_tokens("\n".join(requested_lines)))
    highest = ranked[0][0] if ranked else 1.0
    for index, (score, _workspace_id, _record_id, item) in enumerate(ranked):
        if len(selected) == query.limit:
            break
        citation = f"[E{len(selected) + 1}]"
        future_count = min(
            query.limit - len(selected) - 1,
            len(ranked) - index - 1,
        )
        reserved_tokens = sum(
            tokenizer.count_tokens(f"[E{len(selected) + offset + 2}] x")
            for offset in range(future_count)
        )
        fitted = _fit_excerpt(
            citation=citation,
            content=item.content,
            prior_lines=lines,
            token_budget=max(0, query.token_budget - reserved_tokens),
            tokenizer=tokenizer,
            applicability_label=applicability_label(item),
        )
        if fitted is None:
            continue
        line, rendered_tokens = fitted
        excerpt = line[len(citation) + 1 :].split("\n", 1)[0]
        copied = EvidenceItem.model_validate(
            {
                "citation": citation,
                "record": item.record,
                "bounded_excerpt": excerpt,
                "channels": list(item.channels),
                "score": score / highest,
                "status": item.status,
                "evidence_refs": list(item.evidence_refs),
                "applicability": item.applicability,
                "changed_bindings": list(item.changed_bindings),
                "utility": item.utility,
            }
        )
        selected.append(copied)
        manifest.append(
            CitationManifestEntry(
                citation=citation,
                evidence_refs=copied.evidence_refs,
                channels=copied.channels,
            )
        )
        lines.append(line)
    if not selected:
        return RetrievalData(
            abstained=True,
            abstention_reason="NO_POLICY_VALID_EVIDENCE",
            provider_diagnostics=_merged_diagnostics(results),
            token_usage=TokenUsage(
                budget=query.token_budget,
                requested=requested,
                selected=0,
                rendered=0,
                dropped=requested,
            ),
        )
    return RetrievalData(
        items=selected,
        rendered_context="\n".join(lines),
        citation_manifest=manifest,
        provider_diagnostics=_merged_diagnostics(results),
        abstained=False,
        token_usage=TokenUsage(
            budget=query.token_budget,
            requested=requested,
            selected=rendered_tokens,
            rendered=rendered_tokens,
            dropped=max(0, requested - rendered_tokens),
        ),
    )


__all__ = [
    "FederatedCandidate",
    "FederatedRetrievalError",
    "FederatedSourceResult",
    "compose_federated_results",
    "sliced_queries",
    "validate_directional_links",
]
