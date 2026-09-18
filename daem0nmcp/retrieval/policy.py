"""Ordered, fail-closed policy gates for v7 retrieval evidence."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Literal

from .fusion import fused_candidate_sort_key
from .types import (
    _CONTENT_HASH,
    _EVENT_ID,
    _RECORD_ID,
    _VERSION_ID,
    _WORKSPACE_ID,
    FusedCandidate,
    RetrievalQuery,
    _aware_datetime,
    _opaque,
    _plain_positive_int,
    _provider,
    _reason,
)

PolicyGate = Literal["scope", "visibility", "filters", "temporal", "manifest"]
DedupKind = Literal["exact", "near"]


@dataclass(frozen=True, slots=True)
class PolicyRecord:
    """Ephemeral canonical/projection state used to authorize one candidate.

    This is a read snapshot, not a persistence model.  Visibility is resolved
    by the caller's authorization layer into ``visibility_allowed`` before the
    pure policy pipeline receives it.
    """

    workspace_id: str
    record_id: str
    version_id: str | None
    content_hash: str
    source_event_ids: frozenset[str]
    visibility: str
    visibility_allowed: bool
    archived: bool
    category: str
    tags: frozenset[str]
    valid_from: datetime | None
    valid_to: datetime | None
    transaction_from: datetime | None
    transaction_to: datetime | None
    superseded_by_version_id: str | None
    has_unresolved_contradiction: bool
    projection_content_hashes: tuple[tuple[str, str], ...]
    active_manifest_generations: tuple[tuple[str, int], ...]

    def __post_init__(self) -> None:
        _opaque(self.workspace_id, _WORKSPACE_ID, "workspace_id")
        _opaque(self.record_id, _RECORD_ID, "record_id")
        if self.version_id is not None:
            _opaque(self.version_id, _VERSION_ID, "version_id")
        _opaque(self.content_hash, _CONTENT_HASH, "content_hash")
        if (
            not isinstance(self.source_event_ids, frozenset)
            or not self.source_event_ids
        ):
            raise ValueError("source_event_ids must be a non-empty frozenset")
        for event_id in self.source_event_ids:
            _opaque(event_id, _EVENT_ID, "source_event_id")
        for field_name in ("visibility", "category"):
            value = getattr(self, field_name)
            if not isinstance(value, str) or not value or value != value.strip():
                raise ValueError(f"{field_name} must be a non-empty string")
        for field_name in (
            "visibility_allowed",
            "archived",
            "has_unresolved_contradiction",
        ):
            if not isinstance(getattr(self, field_name), bool):
                raise ValueError(f"{field_name} must be boolean")
        if not isinstance(self.tags, frozenset) or not all(
            isinstance(tag, str) and tag and tag == tag.strip() for tag in self.tags
        ):
            raise ValueError("tags must be a frozenset of non-empty strings")
        for field_name in (
            "valid_from",
            "valid_to",
            "transaction_from",
            "transaction_to",
        ):
            _aware_datetime(getattr(self, field_name), field_name)
        if (
            self.valid_from is not None
            and self.valid_to is not None
            and self.valid_to <= self.valid_from
        ):
            raise ValueError("valid interval must be half-open and increasing")
        if (
            self.transaction_from is not None
            and self.transaction_to is not None
            and self.transaction_to <= self.transaction_from
        ):
            raise ValueError("transaction interval must be half-open and increasing")
        if self.superseded_by_version_id is not None:
            _opaque(
                self.superseded_by_version_id,
                _VERSION_ID,
                "superseded_by_version_id",
            )
        self._validate_projection_hashes()
        self._validate_manifest_generations()

    def _validate_projection_hashes(self) -> None:
        if not isinstance(self.projection_content_hashes, tuple):
            raise ValueError("projection_content_hashes must be an ordered tuple")
        previous: str | None = None
        seen: set[str] = set()
        for channel, content_hash in self.projection_content_hashes:
            _provider(channel, "projection channel")
            _opaque(content_hash, _CONTENT_HASH, "projection content hash")
            if channel in seen or (previous is not None and channel < previous):
                raise ValueError("projection_content_hashes must be unique and sorted")
            seen.add(channel)
            previous = channel

    def _validate_manifest_generations(self) -> None:
        if not isinstance(self.active_manifest_generations, tuple):
            raise ValueError("active_manifest_generations must be an ordered tuple")
        previous: str | None = None
        seen: set[str] = set()
        for channel, generation in self.active_manifest_generations:
            _provider(channel, "manifest channel")
            _plain_positive_int(generation, "active manifest generation")
            if channel in seen or (previous is not None and channel < previous):
                raise ValueError(
                    "active_manifest_generations must be unique and sorted"
                )
            seen.add(channel)
            previous = channel

    @property
    def identity(self) -> tuple[str, str | None]:
        return self.record_id, self.version_id


@dataclass(frozen=True, slots=True)
class PolicyRejection:
    record_id: str
    version_id: str | None
    gate: PolicyGate
    reason: str

    def __post_init__(self) -> None:
        _opaque(self.record_id, _RECORD_ID, "record_id")
        if self.version_id is not None:
            _opaque(self.version_id, _VERSION_ID, "version_id")
        if self.gate not in {"scope", "visibility", "filters", "temporal", "manifest"}:
            raise ValueError("policy gate is invalid")
        _reason(self.reason)


@dataclass(frozen=True, slots=True)
class PolicyMerge:
    retained_record_id: str
    retained_version_id: str | None
    merged_record_id: str
    merged_version_id: str | None
    kind: DedupKind

    def __post_init__(self) -> None:
        _opaque(self.retained_record_id, _RECORD_ID, "retained_record_id")
        _opaque(self.merged_record_id, _RECORD_ID, "merged_record_id")
        for field_name in ("retained_version_id", "merged_version_id"):
            value = getattr(self, field_name)
            if value is not None:
                _opaque(value, _VERSION_ID, field_name)
        if self.kind not in {"exact", "near"}:
            raise ValueError("deduplication kind is invalid")


@dataclass(frozen=True, slots=True)
class PolicyResult:
    candidates: tuple[FusedCandidate, ...]
    rejections: tuple[PolicyRejection, ...]
    merges: tuple[PolicyMerge, ...]
    rejection_counts: tuple[tuple[str, int], ...]
    abstained: bool
    reason: str | None

    def __post_init__(self) -> None:
        if not isinstance(self.candidates, tuple) or not all(
            isinstance(item, FusedCandidate) for item in self.candidates
        ):
            raise ValueError("candidates must contain FusedCandidate values")
        if not isinstance(self.rejections, tuple) or not all(
            isinstance(item, PolicyRejection) for item in self.rejections
        ):
            raise ValueError("rejections must contain PolicyRejection values")
        if not isinstance(self.merges, tuple) or not all(
            isinstance(item, PolicyMerge) for item in self.merges
        ):
            raise ValueError("merges must contain PolicyMerge values")
        if not isinstance(self.rejection_counts, tuple):
            raise ValueError("rejection_counts must be an ordered tuple")
        for rejection_reason, count in self.rejection_counts:
            _reason(rejection_reason, "rejection reason")
            if isinstance(count, bool) or not isinstance(count, int) or count < 1:
                raise ValueError("rejection counts must be positive integers")
        if not isinstance(self.abstained, bool):
            raise ValueError("abstained must be boolean")
        _reason(self.reason)
        if self.abstained != (not self.candidates):
            raise ValueError("abstention must exactly reflect empty policy evidence")
        if self.abstained != (self.reason is not None):
            raise ValueError("only abstentions have a safe reason code")


def _reject(
    candidate: FusedCandidate, gate: PolicyGate, reason: str
) -> PolicyRejection:
    return PolicyRejection(
        record_id=candidate.record_id,
        version_id=candidate.version_id,
        gate=gate,
        reason=reason,
    )


def _temporal_decision(
    query: RetrievalQuery,
    state: PolicyRecord,
    snapshot_time: datetime,
) -> tuple[str | None, str | None]:
    invalidated = False

    valid_at = query.as_of_valid_time or snapshot_time
    if state.valid_from is not None and valid_at < state.valid_from:
        return "NOT_YET_VALID", None
    if state.valid_to is not None and valid_at >= state.valid_to:
        invalidated = True

    transaction_at = query.as_of_transaction_time or snapshot_time
    if state.transaction_from is not None and transaction_at < state.transaction_from:
        return "NOT_YET_RECORDED", None
    if state.transaction_to is not None and transaction_at >= state.transaction_to:
        invalidated = True

    if state.has_unresolved_contradiction:
        return "UNRESOLVED_CONTRADICTION", None
    if (
        state.superseded_by_version_id is not None
        and state.valid_to is None
        and state.transaction_to is None
    ):
        invalidated = True
    if not invalidated:
        return None, None
    if not query.include_invalidated:
        return "INVALIDATED_VERSION", None
    if state.superseded_by_version_id is None:
        return "INVALIDATION_PROVENANCE_MISSING", None
    return None, f"SUPERSEDED_BY:{state.superseded_by_version_id}"


def _manifest_decision(candidate: FusedCandidate, state: PolicyRecord) -> str | None:
    if any(
        evidence.record_id != state.record_id or evidence.version_id != state.version_id
        for evidence in candidate.evidence_refs
    ):
        return "EVIDENCE_IDENTITY_MISMATCH"
    if any(
        evidence.event_id not in state.source_event_ids
        for evidence in candidate.evidence_refs
    ):
        return "SOURCE_EVENT_MISMATCH"
    if any(
        evidence.content_hash != state.content_hash
        for evidence in candidate.evidence_refs
    ):
        return "CONTENT_HASH_MISMATCH"
    projection_hashes = dict(state.projection_content_hashes)
    for channel in candidate.channels:
        if projection_hashes.get(channel) != state.content_hash:
            return "CONTENT_HASH_MISMATCH"
    active_manifests = dict(state.active_manifest_generations)
    for channel, generation in candidate.manifest_generations:
        if generation is None or active_manifests.get(channel) != generation:
            return "MANIFEST_NOT_ACTIVE"
    return None


def _with_policy_note(candidate: FusedCandidate, note: str | None) -> FusedCandidate:
    if note is None or note in candidate.policy_notes:
        return candidate
    return replace(
        candidate,
        policy_notes=tuple(sorted((*candidate.policy_notes, note))),
    )


def _evidence_order(evidence) -> tuple[object, ...]:
    return (
        0 if evidence.provider == "lexical" else 1,
        evidence.provider,
        evidence.record_id,
        evidence.event_id,
        evidence.version_id or "",
        evidence.relation_path,
    )


def _merge_candidates(
    retained: FusedCandidate,
    merged: FusedCandidate,
    *,
    merge_policy_notes: bool = True,
) -> FusedCandidate:
    ranks = dict(retained.channel_ranks)
    for channel, rank in merged.channel_ranks:
        ranks[channel] = min(rank, ranks.get(channel, rank))
    manifests = dict(retained.manifest_generations)
    for channel, generation in merged.manifest_generations:
        existing = manifests.get(channel)
        if existing is not None and generation is not None and existing != generation:
            raise ValueError(
                "cannot merge evidence from conflicting manifest generations"
            )
        if channel not in manifests or existing is None:
            manifests[channel] = generation
    merged_refs = {*retained.evidence_refs, *merged.evidence_refs}
    merged_refs.discard(retained.evidence)
    evidence_refs = (
        retained.evidence,
        *sorted(merged_refs, key=_evidence_order),
    )
    transaction_time = retained.transaction_time
    if transaction_time is None or (
        merged.transaction_time is not None
        and merged.transaction_time > transaction_time
    ):
        transaction_time = merged.transaction_time
    return FusedCandidate(
        evidence=retained.evidence,
        evidence_refs=evidence_refs,
        score=max(retained.score, merged.score),
        channels=retained.channels | merged.channels,
        channel_ranks=tuple(sorted(ranks.items())),
        manifest_generations=tuple(sorted(manifests.items())),
        highlights=tuple(sorted({*retained.highlights, *merged.highlights})),
        policy_notes=(
            tuple(sorted({*retained.policy_notes, *merged.policy_notes}))
            if merge_policy_notes
            else retained.policy_notes
        ),
        transaction_time=transaction_time,
    )


def _rejection_counts(
    rejections: Iterable[PolicyRejection],
) -> tuple[tuple[str, int], ...]:
    counts: dict[str, int] = {}
    for rejection in rejections:
        counts[rejection.reason] = counts.get(rejection.reason, 0) + 1
    return tuple(counts.items())


def _deduplicate(
    candidates: tuple[FusedCandidate, ...],
    records: dict[tuple[str, str | None], PolicyRecord],
) -> tuple[tuple[FusedCandidate, ...], tuple[PolicyMerge, ...]]:
    exact: list[FusedCandidate] = []
    exact_positions: dict[tuple[str, str | None], int] = {}
    merges: list[PolicyMerge] = []
    for candidate in candidates:
        identity = (candidate.record_id, candidate.version_id)
        position = exact_positions.get(identity)
        if position is None:
            exact_positions[identity] = len(exact)
            exact.append(candidate)
            continue
        retained = exact[position]
        exact[position] = _merge_candidates(retained, candidate)
        merges.append(
            PolicyMerge(
                retained_record_id=retained.record_id,
                retained_version_id=retained.version_id,
                merged_record_id=candidate.record_id,
                merged_version_id=candidate.version_id,
                kind="exact",
            )
        )

    near: list[FusedCandidate] = []
    hash_positions: dict[str, int] = {}
    for candidate in exact:
        state = records[(candidate.record_id, candidate.version_id)]
        position = hash_positions.get(state.content_hash)
        if position is None:
            hash_positions[state.content_hash] = len(near)
            near.append(candidate)
            continue
        retained = near[position]
        retained_state = records[(retained.record_id, retained.version_id)]
        if (
            retained.record_id == candidate.record_id
            and retained.version_id != candidate.version_id
        ):
            if state.superseded_by_version_id == retained.version_id:
                retained, candidate = candidate, retained
            elif retained_state.superseded_by_version_id != candidate.version_id:
                near.append(candidate)
                continue
        near[position] = _merge_candidates(
            retained,
            candidate,
            merge_policy_notes=False,
        )
        merges.append(
            PolicyMerge(
                retained_record_id=retained.record_id,
                retained_version_id=retained.version_id,
                merged_record_id=candidate.record_id,
                merged_version_id=candidate.version_id,
                kind="near",
            )
        )
    return tuple(near), tuple(merges)


def apply_retrieval_policy(
    query: RetrievalQuery,
    candidates: Iterable[FusedCandidate],
    records: Iterable[PolicyRecord],
    *,
    snapshot_time: datetime,
) -> PolicyResult:
    """Apply the mandated policy gates, then exact and content-hash dedup."""

    if not isinstance(query, RetrievalQuery):
        raise ValueError("query must be a RetrievalQuery")
    if snapshot_time is None:
        raise ValueError("snapshot_time is required")
    _aware_datetime(snapshot_time, "snapshot_time")
    candidate_values = tuple(candidates)
    if not all(isinstance(item, FusedCandidate) for item in candidate_values):
        raise ValueError("candidates must contain FusedCandidate values")
    record_values = tuple(records)
    if not all(isinstance(item, PolicyRecord) for item in record_values):
        raise ValueError("records must contain PolicyRecord values")
    record_map = {record.identity: record for record in record_values}
    if len(record_map) != len(record_values):
        raise ValueError("policy records must have unique record/version identities")

    ordered = tuple(sorted(candidate_values, key=fused_candidate_sort_key))
    accepted: list[FusedCandidate] = []
    rejections: list[PolicyRejection] = []
    for candidate in ordered:
        identity = (candidate.record_id, candidate.version_id)
        state = record_map.get(identity)

        # Gate 1: workspace and explicit record-ID scope.
        if state is None:
            rejections.append(_reject(candidate, "scope", "RECORD_NOT_FOUND"))
            continue
        if state.workspace_id != query.workspace_id:
            rejections.append(_reject(candidate, "scope", "WORKSPACE_SCOPE"))
            continue
        if query.record_ids is not None and candidate.record_id not in query.record_ids:
            rejections.append(_reject(candidate, "scope", "RECORD_ID_SCOPE"))
            continue

        # Gate 2: caller visibility authorization, then archive policy.
        if not state.visibility_allowed:
            rejections.append(_reject(candidate, "visibility", "VISIBILITY_DENIED"))
            continue
        if state.archived and not query.include_archived:
            rejections.append(_reject(candidate, "visibility", "ARCHIVED_EXCLUDED"))
            continue

        # Gate 3: category and conjunctive tag filters.
        if query.categories is not None and state.category not in query.categories:
            rejections.append(_reject(candidate, "filters", "CATEGORY_MISMATCH"))
            continue
        if query.tags is not None and not query.tags.issubset(state.tags):
            rejections.append(_reject(candidate, "filters", "TAG_MISMATCH"))
            continue

        # Gate 4: valid time, transaction time, and contradiction state.
        temporal_reason, policy_note = _temporal_decision(query, state, snapshot_time)
        if temporal_reason is not None:
            rejections.append(_reject(candidate, "temporal", temporal_reason))
            continue

        # Gate 5: canonical/projected hashes and active integer generations.
        manifest_reason = _manifest_decision(candidate, state)
        if manifest_reason is not None:
            rejections.append(_reject(candidate, "manifest", manifest_reason))
            continue
        accepted.append(_with_policy_note(candidate, policy_note))

    deduplicated, merges = _deduplicate(tuple(accepted), record_map)
    if deduplicated:
        abstention_reason = None
    elif not candidate_values:
        abstention_reason = "NO_CANDIDATES"
    else:
        abstention_reason = "ALL_CANDIDATES_FILTERED"
    return PolicyResult(
        candidates=deduplicated,
        rejections=tuple(rejections),
        merges=merges,
        rejection_counts=_rejection_counts(rejections),
        abstained=not deduplicated,
        reason=abstention_reason,
    )
