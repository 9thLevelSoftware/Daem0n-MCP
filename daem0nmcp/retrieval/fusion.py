"""Deterministic weighted reciprocal-rank fusion for v7 candidates."""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from types import MappingProxyType

from .types import (
    EvidenceRef,
    FusedCandidate,
    ProviderResult,
    _provider,
    evidence_identity,
)

DEFAULT_RRF_K = 60
MAX_RRF_K = 1_000_000
DEFAULT_RRF_WEIGHTS: Mapping[str, float] = MappingProxyType(
    {
        "lexical": 1.0,
        "dense": 1.0,
        "graph": 0.7,
        "temporal": 0.85,
        "procedure": 0.8,
        "outcome": 0.9,
    }
)


@dataclass(slots=True)
class _Accumulator:
    score: float = 0.0
    evidence_refs: set[EvidenceRef] = field(default_factory=set)
    channels: set[str] = field(default_factory=set)
    channel_ranks: dict[str, int] = field(default_factory=dict)
    manifest_generations: dict[str, int | None] = field(default_factory=dict)
    highlights: set[str] = field(default_factory=set)
    policy_notes: set[str] = field(default_factory=set)
    transaction_time: datetime | None = None


def _validated_weights(weights: Mapping[str, float]) -> dict[str, float]:
    if not isinstance(weights, Mapping) or not weights:
        raise ValueError("RRF weights must be a non-empty mapping")
    validated: dict[str, float] = {}
    for channel, weight in weights.items():
        _provider(channel, "RRF weight channel")
        if isinstance(weight, bool) or not isinstance(weight, (int, float)):
            raise ValueError("RRF weights must be positive finite numbers")
        try:
            numeric = float(weight)
        except (OverflowError, ValueError) as exc:
            raise ValueError("RRF weights must be positive finite numbers") from exc
        if not math.isfinite(numeric) or numeric <= 0:
            raise ValueError("RRF weights must be positive finite numbers")
        validated[channel] = numeric
    return validated


def _evidence_order(evidence: EvidenceRef) -> tuple[object, ...]:
    return (
        0 if evidence.provider == "lexical" else 1,
        evidence.provider,
        evidence.record_id,
        evidence.event_id,
        evidence.content_hash,
        evidence.version_id or "",
        evidence.relation_path,
    )


def _newer(current: datetime | None, candidate: datetime | None) -> datetime | None:
    if current is None:
        return candidate
    if candidate is None:
        return current
    return max(current, candidate)


def _transaction_order(value: datetime | None) -> tuple[int, int, int, int]:
    if value is None:
        return (1, 0, 0, 0)
    utc = value.astimezone(timezone.utc)
    seconds = utc.hour * 3600 + utc.minute * 60 + utc.second
    return (0, -utc.toordinal(), -seconds, -utc.microsecond)


def _fused_order(candidate: FusedCandidate) -> tuple[object, ...]:
    lexical_rank = candidate.lexical_rank
    return (
        -candidate.score,
        -len(candidate.channels),
        lexical_rank is None,
        lexical_rank if lexical_rank is not None else 0,
        *_transaction_order(candidate.transaction_time),
        candidate.record_id,
        candidate.version_id or "",
    )


def weighted_reciprocal_rank_fusion(
    provider_results: Iterable[ProviderResult],
    *,
    weights: Mapping[str, float] = DEFAULT_RRF_WEIGHTS,
    k: int = DEFAULT_RRF_K,
) -> tuple[FusedCandidate, ...]:
    """Fuse provider rank lists without comparing provider-native scores.

    Providers are sorted before accumulation, which makes floating-point
    addition, provenance ordering, and output stable regardless of scheduling
    order.  A provider contributes at most once for each record/version key.
    """

    if isinstance(k, bool) or not isinstance(k, int) or k < 1 or k > MAX_RRF_K:
        raise ValueError("RRF k must be a bounded positive integer")
    channel_weights = _validated_weights(weights)
    results = tuple(provider_results)
    if not all(isinstance(result, ProviderResult) for result in results):
        raise ValueError("provider_results must contain ProviderResult values")
    provider_names = [result.provider for result in results]
    if len(provider_names) != len(set(provider_names)):
        raise ValueError("each provider may contribute at most one result")

    accumulators: dict[tuple[str, str | None], _Accumulator] = {}
    for result in sorted(results, key=lambda item: item.provider):
        if not result.candidates:
            continue
        if result.provider not in channel_weights:
            raise ValueError(f"no RRF weight configured for {result.provider}")
        weight = channel_weights[result.provider]
        for candidate in result.candidates:
            identity = evidence_identity(candidate.evidence)
            accumulator = accumulators.setdefault(identity, _Accumulator())
            accumulator.score += weight / (k + candidate.rank)
            accumulator.evidence_refs.add(candidate.evidence)
            accumulator.channels.add(result.provider)
            accumulator.channel_ranks[result.provider] = candidate.rank
            accumulator.manifest_generations[result.provider] = (
                result.manifest_generation
            )
            accumulator.highlights.update(candidate.highlights)
            accumulator.policy_notes.update(candidate.policy_notes)
            accumulator.transaction_time = _newer(
                accumulator.transaction_time, candidate.transaction_time
            )

    fused: list[FusedCandidate] = []
    for accumulator in accumulators.values():
        evidence_refs = tuple(sorted(accumulator.evidence_refs, key=_evidence_order))
        fused.append(
            FusedCandidate(
                evidence=evidence_refs[0],
                evidence_refs=evidence_refs,
                score=accumulator.score,
                channels=frozenset(accumulator.channels),
                channel_ranks=tuple(sorted(accumulator.channel_ranks.items())),
                manifest_generations=tuple(
                    sorted(accumulator.manifest_generations.items())
                ),
                highlights=tuple(sorted(accumulator.highlights)),
                policy_notes=tuple(sorted(accumulator.policy_notes)),
                transaction_time=accumulator.transaction_time,
            )
        )
    return tuple(sorted(fused, key=_fused_order))


def fused_candidate_sort_key(candidate: FusedCandidate) -> tuple[object, ...]:
    """Expose the mandated deterministic ordering to later pure stages."""

    if not isinstance(candidate, FusedCandidate):
        raise ValueError("candidate must be a FusedCandidate")
    return _fused_order(candidate)
