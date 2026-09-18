"""Immutable, dependency-free contracts for the v7 retrieval pipeline.

The identifiers validated here are the opaque identifiers published by the
v7 event store.  These types carry provenance between projections without
making a retrieval projection another source of truth.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Literal, Protocol, runtime_checkable

ProviderStatus = Literal["ready", "degraded", "unavailable", "failed"]

_WORKSPACE_ID = re.compile(r"^ws_[0-9a-f]{24}$")
_RECORD_ID = re.compile(r"^mem_[0-9a-f]{64}$")
_EVENT_ID = re.compile(r"^evt_[0-9a-f]{64}$")
_VERSION_ID = re.compile(r"^(?:fact_[0-9a-f]{64}|rel_[0-9a-f]{64})$")
_RELATION_ID = re.compile(r"^(?:rel|fact)_[0-9a-f]{64}$")
_CONTENT_HASH = re.compile(r"^[0-9a-f]{64}$")
_PROVIDER_NAME = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")
_SAFE_REASON = re.compile(r"^[A-Z][A-Z0-9_]{0,79}$")
_CITATION = re.compile(r"^\[E[1-9][0-9]*\]$")
_CITATION_IN_TEXT = re.compile(r"\[E[1-9][0-9]*\]")
_MAX_QUERY_CHARS = 16_384
_MAX_RESULT_LIMIT = 100
_MAX_CANDIDATE_LIMIT = 1_000
MAX_TOKEN_BUDGET = 131_072
_MAX_FILTER_VALUES = 256
_MAX_FILTER_VALUE_CHARS = 256
MAX_LEGACY_RATIONALE_CHARS = 4_096
MAX_LEGACY_TAGS = 32
MAX_LEGACY_TAG_CHARS = 128


def _plain_positive_int(
    value: object,
    field_name: str,
    *,
    maximum: int | None = None,
) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{field_name} must be a positive integer")
    if maximum is not None and value > maximum:
        raise ValueError(f"{field_name} exceeds its supported maximum")
    return value


def _finite_nonnegative(value: object, field_name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{field_name} must be a finite non-negative number")
    try:
        result = float(value)
    except (OverflowError, ValueError) as exc:
        raise ValueError(f"{field_name} must be a finite non-negative number") from exc
    if not math.isfinite(result) or result < 0:
        raise ValueError(f"{field_name} must be a finite non-negative number")
    return result


def _aware_datetime(value: datetime | None, field_name: str) -> None:
    if value is None:
        return
    if not isinstance(value, datetime):
        raise ValueError(f"{field_name} must be a datetime")
    try:
        offset = value.utcoffset()
    except (OverflowError, ValueError) as exc:
        raise ValueError(f"{field_name} must be timezone-aware") from exc
    if offset is None:
        raise ValueError(f"{field_name} must be timezone-aware")


def _opaque(value: object, pattern: re.Pattern[str], field_name: str) -> str:
    if not isinstance(value, str) or pattern.fullmatch(value) is None:
        raise ValueError(f"{field_name} must be an opaque v7 identifier")
    return value


def _provider(value: object, field_name: str = "provider") -> str:
    if not isinstance(value, str) or _PROVIDER_NAME.fullmatch(value) is None:
        raise ValueError(f"{field_name} is invalid")
    return value


def _reason(value: str | None, field_name: str = "reason") -> None:
    if value is not None and (
        not isinstance(value, str) or _SAFE_REASON.fullmatch(value) is None
    ):
        raise ValueError(f"{field_name} must be a sanitized reason code")


def _string_filter(
    value: frozenset[str] | None,
    field_name: str,
    *,
    pattern: re.Pattern[str] | None = None,
) -> None:
    if value is None:
        return
    if not isinstance(value, frozenset):
        raise ValueError(f"{field_name} must be a frozenset")
    if len(value) > _MAX_FILTER_VALUES:
        raise ValueError(f"{field_name} contains too many values")
    for item in value:
        if (
            not isinstance(item, str)
            or not item
            or item != item.strip()
            or len(item) > _MAX_FILTER_VALUE_CHARS
        ):
            raise ValueError(f"{field_name} contains an invalid value")
        if pattern is not None and pattern.fullmatch(item) is None:
            raise ValueError(f"{field_name} contains a non-opaque identifier")


def _legacy_metadata(
    rationale: object,
    tags: object,
    worked: object,
) -> None:
    if rationale is not None and (
        not isinstance(rationale, str) or len(rationale) > MAX_LEGACY_RATIONALE_CHARS
    ):
        raise ValueError("rationale must be bounded text or null")
    if (
        not isinstance(tags, tuple)
        or len(tags) > MAX_LEGACY_TAGS
        or not all(
            isinstance(tag, str)
            and tag
            and tag == tag.strip()
            and len(tag) <= MAX_LEGACY_TAG_CHARS
            for tag in tags
        )
        or len(tags) != len(set(tags))
    ):
        raise ValueError("tags must contain unique bounded strings")
    if worked is not None and not isinstance(worked, bool):
        raise ValueError("worked must be boolean or null")


@dataclass(frozen=True, slots=True)
class RetrievalQuery:
    """One policy-scoped retrieval request.

    Empty or whitespace-only query text is deliberately permitted.  The
    lexical provider reports it as an explicit no-results result instead of
    changing ranking semantics through a hidden fallback.
    """

    workspace_id: str
    text: str
    limit: int = 10
    candidate_limit: int = 50
    as_of_valid_time: datetime | None = None
    as_of_transaction_time: datetime | None = None
    categories: frozenset[str] | None = None
    tags: frozenset[str] | None = None
    record_ids: frozenset[str] | None = None
    include_invalidated: bool = False
    include_archived: bool = False
    token_budget: int = 2400
    rerank: bool = False

    def __post_init__(self) -> None:
        _opaque(self.workspace_id, _WORKSPACE_ID, "workspace_id")
        if not isinstance(self.text, str) or len(self.text) > _MAX_QUERY_CHARS:
            raise ValueError("text must be a bounded string")
        _plain_positive_int(self.limit, "limit", maximum=_MAX_RESULT_LIMIT)
        _plain_positive_int(
            self.candidate_limit,
            "candidate_limit",
            maximum=_MAX_CANDIDATE_LIMIT,
        )
        if self.candidate_limit < self.limit:
            raise ValueError("candidate_limit must be greater than or equal to limit")
        _plain_positive_int(
            self.token_budget,
            "token_budget",
            maximum=MAX_TOKEN_BUDGET,
        )
        _aware_datetime(self.as_of_valid_time, "as_of_valid_time")
        _aware_datetime(self.as_of_transaction_time, "as_of_transaction_time")
        _string_filter(self.categories, "categories")
        _string_filter(self.tags, "tags")
        _string_filter(self.record_ids, "record_ids", pattern=_RECORD_ID)
        for field_name in (
            "include_invalidated",
            "include_archived",
            "rerank",
        ):
            if not isinstance(getattr(self, field_name), bool):
                raise ValueError(f"{field_name} must be boolean")


@dataclass(frozen=True, slots=True)
class EvidenceRef:
    """Opaque provenance for one candidate evidence unit."""

    record_id: str
    event_id: str
    content_hash: str
    version_id: str | None
    relation_path: tuple[str, ...] = ()
    provider: str = ""

    def __post_init__(self) -> None:
        _opaque(self.record_id, _RECORD_ID, "record_id")
        _opaque(self.event_id, _EVENT_ID, "event_id")
        _opaque(self.content_hash, _CONTENT_HASH, "content_hash")
        if self.version_id is not None:
            _opaque(self.version_id, _VERSION_ID, "version_id")
        if not isinstance(self.relation_path, tuple):
            raise ValueError("relation_path must be a tuple")
        for relation_id in self.relation_path:
            _opaque(relation_id, _RELATION_ID, "relation_path")
        if self.provider:
            _provider(self.provider)
        elif not isinstance(self.provider, str):
            raise ValueError("provider is invalid")


@dataclass(frozen=True, slots=True)
class Candidate:
    """One provider-ranked candidate; raw scores remain diagnostic only."""

    evidence: EvidenceRef
    rank: int
    raw_score: float | None
    channels: frozenset[str]
    highlights: tuple[str, ...] = ()
    policy_notes: tuple[str, ...] = ()
    transaction_time: datetime | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.evidence, EvidenceRef):
            raise ValueError("evidence must be an EvidenceRef")
        _plain_positive_int(self.rank, "rank", maximum=_MAX_CANDIDATE_LIMIT)
        if self.raw_score is not None:
            if isinstance(self.raw_score, bool) or not isinstance(
                self.raw_score, (int, float)
            ):
                raise ValueError("raw_score must be finite when supplied")
            try:
                numeric_score = float(self.raw_score)
            except (OverflowError, ValueError) as exc:
                raise ValueError("raw_score must be finite when supplied") from exc
            if not math.isfinite(numeric_score):
                raise ValueError("raw_score must be finite when supplied")
            object.__setattr__(self, "raw_score", numeric_score)
        if not isinstance(self.channels, frozenset) or not self.channels:
            raise ValueError("channels must be a non-empty frozenset")
        for channel in self.channels:
            _provider(channel, "channel")
        if self.evidence.provider and self.evidence.provider not in self.channels:
            raise ValueError("the evidence provider must be one candidate channel")
        for field_name in ("highlights", "policy_notes"):
            values = getattr(self, field_name)
            if not isinstance(values, tuple) or not all(
                isinstance(item, str) and item for item in values
            ):
                raise ValueError(f"{field_name} must contain non-empty strings")
        _aware_datetime(self.transaction_time, "transaction_time")


@dataclass(frozen=True, slots=True)
class ProviderResult:
    """Ordered output and operational status from one retrieval provider."""

    provider: str
    candidates: tuple[Candidate, ...] = ()
    status: ProviderStatus = "ready"
    manifest_generation: int | None = None
    elapsed_ms: float = 0.0
    reason: str | None = None

    def __post_init__(self) -> None:
        _provider(self.provider)
        if self.status not in {"ready", "degraded", "unavailable", "failed"}:
            raise ValueError("status is invalid")
        if not isinstance(self.candidates, tuple) or not all(
            isinstance(candidate, Candidate) for candidate in self.candidates
        ):
            raise ValueError("candidates must be a tuple of Candidate values")
        ranks = [candidate.rank for candidate in self.candidates]
        if ranks != sorted(ranks) or len(ranks) != len(set(ranks)):
            raise ValueError("provider candidate ranks must be ordered and unique")
        identities = [
            (candidate.evidence.record_id, candidate.evidence.version_id)
            for candidate in self.candidates
        ]
        if len(identities) != len(set(identities)):
            raise ValueError("a provider cannot emit the same evidence twice")
        for candidate in self.candidates:
            if self.provider not in candidate.channels:
                raise ValueError("provider must be one candidate channel")
            if candidate.evidence.provider not in {"", self.provider}:
                raise ValueError("evidence provider does not match its emitter")
        if self.manifest_generation is not None:
            _plain_positive_int(self.manifest_generation, "manifest_generation")
        object.__setattr__(
            self, "elapsed_ms", _finite_nonnegative(self.elapsed_ms, "elapsed_ms")
        )
        _reason(self.reason)
        if self.status != "ready" and self.reason is None:
            raise ValueError("non-ready providers require a sanitized reason")
        if self.status in {"unavailable", "failed"} and self.candidates:
            raise ValueError("unavailable or failed providers cannot emit candidates")


@runtime_checkable
class RetrievalProvider(Protocol):
    """Structural interface implemented by every retrieval projection."""

    name: str

    async def search(self, query: RetrievalQuery, limit: int) -> ProviderResult:
        """Return one ordered provider rank list and sanitized diagnostics."""
        ...


@dataclass(frozen=True, slots=True)
class ProviderDiagnostic:
    """Candidate-free provider telemetry safe to expose in final results."""

    provider: str
    status: ProviderStatus
    manifest_generation: int | None
    elapsed_ms: float
    reason: str | None
    returned_count: int

    def __post_init__(self) -> None:
        _provider(self.provider)
        if self.status not in {"ready", "degraded", "unavailable", "failed"}:
            raise ValueError("status is invalid")
        if self.manifest_generation is not None:
            _plain_positive_int(self.manifest_generation, "manifest_generation")
        object.__setattr__(
            self, "elapsed_ms", _finite_nonnegative(self.elapsed_ms, "elapsed_ms")
        )
        if (
            isinstance(self.returned_count, bool)
            or not isinstance(self.returned_count, int)
            or self.returned_count < 0
        ):
            raise ValueError("returned_count must be a non-negative integer")
        _reason(self.reason)
        if self.status != "ready" and self.reason is None:
            raise ValueError("non-ready providers require a sanitized reason")
        if self.status in {"unavailable", "failed"} and self.returned_count:
            raise ValueError(
                "unavailable or failed providers cannot report returned evidence"
            )

    @classmethod
    def from_result(cls, result: ProviderResult) -> ProviderDiagnostic:
        if not isinstance(result, ProviderResult):
            raise ValueError("result must be a ProviderResult")
        return cls(
            provider=result.provider,
            status=result.status,
            manifest_generation=result.manifest_generation,
            elapsed_ms=result.elapsed_ms,
            reason=result.reason,
            returned_count=len(result.candidates),
        )


@dataclass(frozen=True, slots=True)
class FusedCandidate:
    """Rank-fused evidence with every contributing provenance reference."""

    evidence: EvidenceRef
    evidence_refs: tuple[EvidenceRef, ...]
    score: float
    channels: frozenset[str]
    channel_ranks: tuple[tuple[str, int], ...]
    manifest_generations: tuple[tuple[str, int | None], ...]
    highlights: tuple[str, ...] = ()
    policy_notes: tuple[str, ...] = ()
    transaction_time: datetime | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.evidence, EvidenceRef):
            raise ValueError("evidence must be an EvidenceRef")
        if (
            not isinstance(self.evidence_refs, tuple)
            or not self.evidence_refs
            or not all(isinstance(item, EvidenceRef) for item in self.evidence_refs)
            or self.evidence_refs[0] != self.evidence
        ):
            raise ValueError("evidence_refs must begin with primary evidence")
        if len(self.evidence_refs) != len(set(self.evidence_refs)):
            raise ValueError("evidence_refs must be unique")
        numeric_score = _finite_nonnegative(self.score, "score")
        object.__setattr__(self, "score", numeric_score)
        if not isinstance(self.channels, frozenset) or not self.channels:
            raise ValueError("channels must be a non-empty frozenset")
        for channel in self.channels:
            _provider(channel, "channel")
        for evidence in self.evidence_refs:
            if evidence.provider and evidence.provider not in self.channels:
                raise ValueError("every evidence provider must be a fused channel")
        if not isinstance(self.channel_ranks, tuple):
            raise ValueError("channel_ranks must be an ordered tuple")
        rank_channels: set[str] = set()
        previous_channel: str | None = None
        for channel, rank in self.channel_ranks:
            _provider(channel, "rank channel")
            _plain_positive_int(rank, "channel rank")
            if channel in rank_channels or (
                previous_channel is not None and channel < previous_channel
            ):
                raise ValueError("channel_ranks must be unique and sorted")
            rank_channels.add(channel)
            previous_channel = channel
        if rank_channels != set(self.channels):
            raise ValueError("every contributing channel requires one rank")
        if not isinstance(self.manifest_generations, tuple):
            raise ValueError("manifest_generations must be an ordered tuple")
        manifest_channels: set[str] = set()
        previous_channel = None
        for channel, generation in self.manifest_generations:
            _provider(channel, "manifest channel")
            if generation is not None:
                _plain_positive_int(generation, "manifest generation")
            if channel in manifest_channels or (
                previous_channel is not None and channel < previous_channel
            ):
                raise ValueError("manifest_generations must be unique and sorted")
            manifest_channels.add(channel)
            previous_channel = channel
        if manifest_channels != set(self.channels):
            raise ValueError("every contributing channel requires a manifest entry")
        for field_name in ("highlights", "policy_notes"):
            values = getattr(self, field_name)
            if not isinstance(values, tuple) or not all(
                isinstance(item, str) and item for item in values
            ):
                raise ValueError(f"{field_name} must contain non-empty strings")
        _aware_datetime(self.transaction_time, "transaction_time")

    @property
    def record_id(self) -> str:
        return self.evidence.record_id

    @property
    def version_id(self) -> str | None:
        return self.evidence.version_id

    @property
    def content_hash(self) -> str:
        return self.evidence.content_hash

    @property
    def lexical_rank(self) -> int | None:
        for channel, rank in self.channel_ranks:
            if channel == "lexical":
                return rank
        return None


@dataclass(frozen=True, slots=True)
class EvidenceItem:
    """One selected, policy-valid evidence unit ready for composition."""

    citation: str
    excerpt: str
    category: str
    status: Literal["current", "superseded"]
    score: float
    channels: frozenset[str]
    token_count: int
    evidence_refs: tuple[EvidenceRef, ...]
    rationale: str | None = None
    tags: tuple[str, ...] = ()
    worked: bool | None = None
    superseded_by_version_id: str | None = None
    outcome: str | None = None
    outcome_failed: bool = False
    procedure_steps: tuple[str, ...] = ()
    relation_path: tuple[str, ...] = ()
    relation_paths: tuple[tuple[str, ...], ...] = ()

    def __post_init__(self) -> None:
        if (
            not isinstance(self.citation, str)
            or _CITATION.fullmatch(self.citation) is None
        ):
            raise ValueError("citation must be a stable [E#] marker")
        for field_name in ("excerpt", "category"):
            value = getattr(self, field_name)
            if not isinstance(value, str) or not value:
                raise ValueError(f"{field_name} must be a non-empty string")
        if self.status not in {"current", "superseded"}:
            raise ValueError("evidence status is invalid")
        object.__setattr__(self, "score", _finite_nonnegative(self.score, "score"))
        if not isinstance(self.channels, frozenset) or not self.channels:
            raise ValueError("channels must be a non-empty frozenset")
        for channel in self.channels:
            _provider(channel, "channel")
        _plain_positive_int(self.token_count, "token_count")
        if (
            not isinstance(self.evidence_refs, tuple)
            or not self.evidence_refs
            or not all(
                isinstance(evidence, EvidenceRef) for evidence in self.evidence_refs
            )
        ):
            raise ValueError("evidence_refs must be a non-empty tuple")
        if len(self.evidence_refs) != len(set(self.evidence_refs)):
            raise ValueError("evidence_refs must be unique")
        for evidence in self.evidence_refs:
            if evidence.provider and evidence.provider not in self.channels:
                raise ValueError("every evidence provider must be a selected channel")
        _legacy_metadata(self.rationale, self.tags, self.worked)
        if self.superseded_by_version_id is not None:
            _opaque(
                self.superseded_by_version_id,
                _VERSION_ID,
                "superseded_by_version_id",
            )
        if (self.status == "superseded") != (self.superseded_by_version_id is not None):
            raise ValueError(
                "superseded evidence requires its invalidating opaque version"
            )
        if self.outcome is not None and (
            not isinstance(self.outcome, str) or not self.outcome
        ):
            raise ValueError("outcome must be non-empty when supplied")
        if not isinstance(self.outcome_failed, bool):
            raise ValueError("outcome_failed must be boolean")
        if self.outcome_failed and self.outcome is None:
            raise ValueError("a failed outcome requires outcome text")
        if not isinstance(self.procedure_steps, tuple) or not all(
            isinstance(step, str) and step for step in self.procedure_steps
        ):
            raise ValueError("procedure_steps must contain non-empty strings")
        if not isinstance(self.relation_path, tuple):
            raise ValueError("relation_path must be a tuple")
        for relation_id in self.relation_path:
            _opaque(relation_id, _RELATION_ID, "relation_path")
        if not isinstance(self.relation_paths, tuple) or not all(
            isinstance(path, tuple) and path for path in self.relation_paths
        ):
            raise ValueError("relation_paths must contain non-empty tuples")
        normalized_paths = self.relation_paths
        if not normalized_paths and self.relation_path:
            normalized_paths = (self.relation_path,)
            object.__setattr__(self, "relation_paths", normalized_paths)
        if (
            len(normalized_paths) != len(set(normalized_paths))
            or tuple(sorted(normalized_paths)) != normalized_paths
        ):
            raise ValueError("relation_paths must be unique and sorted")
        for path in normalized_paths:
            for relation_id in path:
                _opaque(relation_id, _RELATION_ID, "relation_paths")
        if normalized_paths and self.relation_path != normalized_paths[0]:
            raise ValueError("relation_path must be the first retained graph path")


@dataclass(frozen=True, slots=True)
class CitationEntry:
    """Opaque citation-manifest entry with source excerpt offsets."""

    marker: str
    evidence_refs: tuple[EvidenceRef, ...]
    channels: frozenset[str]
    excerpt_start: int
    excerpt_end: int

    def __post_init__(self) -> None:
        if not isinstance(self.marker, str) or _CITATION.fullmatch(self.marker) is None:
            raise ValueError("marker must be a stable [E#] citation")
        if (
            not isinstance(self.evidence_refs, tuple)
            or not self.evidence_refs
            or not all(
                isinstance(evidence, EvidenceRef) for evidence in self.evidence_refs
            )
        ):
            raise ValueError("evidence_refs must be a non-empty tuple")
        if len(self.evidence_refs) != len(set(self.evidence_refs)):
            raise ValueError("evidence_refs must be unique")
        if not isinstance(self.channels, frozenset) or not self.channels:
            raise ValueError("channels must be a non-empty frozenset")
        for channel in self.channels:
            _provider(channel, "channel")
        for evidence in self.evidence_refs:
            if evidence.provider and evidence.provider not in self.channels:
                raise ValueError("every evidence provider must be a citation channel")
        if (
            isinstance(self.excerpt_start, bool)
            or not isinstance(self.excerpt_start, int)
            or self.excerpt_start < 0
            or isinstance(self.excerpt_end, bool)
            or not isinstance(self.excerpt_end, int)
            or self.excerpt_end <= self.excerpt_start
        ):
            raise ValueError("excerpt offsets must form a non-empty range")


@dataclass(frozen=True, slots=True)
class ContextPackage:
    """Rendered context and a complete immutable citation manifest."""

    text: str
    citations: tuple[CitationEntry, ...]
    token_budget: int
    requested_tokens: int
    selected_tokens: int
    rendered_tokens: int
    dropped_tokens: int
    drop_reasons: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.text, str):
            raise ValueError("text must be a string")
        if not isinstance(self.citations, tuple) or not all(
            isinstance(citation, CitationEntry) for citation in self.citations
        ):
            raise ValueError("citations must contain CitationEntry values")
        markers = [citation.marker for citation in self.citations]
        if len(markers) != len(set(markers)):
            raise ValueError("citation markers must be unique")
        if markers != _CITATION_IN_TEXT.findall(self.text):
            raise ValueError(
                "rendered citation markers must exactly match the manifest"
            )
        for citation in self.citations:
            if citation.excerpt_end > len(self.text):
                raise ValueError("citation excerpt offsets exceed rendered text")
            if self.text.find(citation.marker) >= citation.excerpt_start:
                raise ValueError("each citation marker must precede its excerpt")
        _plain_positive_int(
            self.token_budget,
            "token_budget",
            maximum=MAX_TOKEN_BUDGET,
        )
        for field_name in (
            "requested_tokens",
            "selected_tokens",
            "rendered_tokens",
            "dropped_tokens",
        ):
            value = getattr(self, field_name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{field_name} must be a non-negative integer")
        if self.selected_tokens > self.requested_tokens:
            raise ValueError("selected_tokens cannot exceed requested_tokens")
        if self.rendered_tokens > self.token_budget:
            raise ValueError("rendered context exceeds its token budget")
        if not isinstance(self.drop_reasons, tuple):
            raise ValueError("drop_reasons must be a tuple")
        for drop_reason in self.drop_reasons:
            _reason(drop_reason, "drop reason")


@dataclass(frozen=True, slots=True)
class RetrievalResult:
    """Final retrieval envelope; raw provider candidate lists are excluded."""

    items: tuple[EvidenceItem, ...] = ()
    context: ContextPackage | None = None
    providers: tuple[ProviderDiagnostic, ...] = ()
    weights: tuple[tuple[str, float], ...] = ()
    policy_rejection_counts: tuple[tuple[str, int], ...] = ()
    abstained: bool = False
    reason: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.items, tuple) or not all(
            isinstance(item, EvidenceItem) for item in self.items
        ):
            raise ValueError("items must contain selected EvidenceItem values")
        if self.context is not None and not isinstance(self.context, ContextPackage):
            raise ValueError("context must be a ContextPackage")
        if not isinstance(self.providers, tuple) or not all(
            isinstance(item, ProviderDiagnostic) for item in self.providers
        ):
            raise ValueError("providers must contain candidate-free diagnostics")
        if not isinstance(self.weights, tuple):
            raise ValueError("weights must be an ordered tuple")
        seen_weights: set[str] = set()
        for channel, weight in self.weights:
            _provider(channel, "weight channel")
            if channel in seen_weights:
                raise ValueError("weight channels must be unique")
            seen_weights.add(channel)
            numeric_weight = _finite_nonnegative(weight, "weight")
            if numeric_weight == 0:
                raise ValueError("weights must be positive")
        if not isinstance(self.policy_rejection_counts, tuple):
            raise ValueError("policy_rejection_counts must be an ordered tuple")
        rejection_reasons: set[str] = set()
        for rejection_reason, count in self.policy_rejection_counts:
            _reason(rejection_reason, "policy rejection reason")
            if rejection_reason in rejection_reasons:
                raise ValueError("policy rejection reasons must be unique")
            rejection_reasons.add(rejection_reason)
            if isinstance(count, bool) or not isinstance(count, int) or count < 1:
                raise ValueError("policy rejection counts must be positive integers")
        if not isinstance(self.abstained, bool):
            raise ValueError("abstained must be boolean")
        _reason(self.reason)
        if self.abstained:
            if self.reason is None:
                raise ValueError("abstention requires a sanitized reason")
            if self.items or self.context is not None:
                raise ValueError("an abstention cannot contain fabricated evidence")
            if (
                self.reason == "ALL_CANDIDATES_FILTERED"
                and not self.policy_rejection_counts
            ):
                raise ValueError(
                    "all-filtered abstention requires policy rejection counts"
                )
        elif self.reason is not None:
            raise ValueError("non-abstaining results cannot have an abstention reason")
        elif not self.items or self.context is None:
            raise ValueError("non-abstaining results require evidence and context")
        if not self.abstained:
            item_markers = [item.citation for item in self.items]
            if len(item_markers) != len(set(item_markers)):
                raise ValueError("evidence item citations must be unique")
            item_citations = set(item_markers)
            manifest_by_marker = {
                citation.marker: citation for citation in self.context.citations
            }
            manifest_citations = set(manifest_by_marker)
            if item_citations != manifest_citations:
                raise ValueError(
                    "every evidence item must resolve in the citation manifest"
                )
            if tuple(item_markers) != tuple(
                citation.marker for citation in self.context.citations
            ):
                raise ValueError("evidence items must follow rendered citation order")
            for item in self.items:
                citation = manifest_by_marker[item.citation]
                if (
                    set(item.evidence_refs) != set(citation.evidence_refs)
                    or item.channels != citation.channels
                    or self.context.text[citation.excerpt_start : citation.excerpt_end]
                    != item.excerpt
                ):
                    raise ValueError(
                        "citation manifest provenance must match selected evidence"
                    )


def evidence_identity(evidence: EvidenceRef) -> tuple[str, str | None]:
    """Return the stable record/version key used through fusion and policy."""

    if not isinstance(evidence, EvidenceRef):
        raise ValueError("evidence must be an EvidenceRef")
    return evidence.record_id, evidence.version_id
