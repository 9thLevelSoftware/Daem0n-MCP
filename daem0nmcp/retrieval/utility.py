"""Deterministic outcome evidence weighting and provenance credit folding."""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Literal

ALPHA = 0.3
GAMMA = 0.8
LAMBDA = 0.7
MAX_DEPTH = 4
MAX_DESCENDANTS = 64
RANK_PRIOR_K = 61
RELEVANCE_BAND = 0.95


@dataclass(frozen=True, slots=True)
class UtilityContribution:
    recorded_at_us: int
    event_id: str
    depth: int
    reward: float
    weight: float


@dataclass(frozen=True, slots=True)
class UtilityEstimate:
    q_single: float
    q_trace: float
    evidence_count: int
    single_evidence_count: int


def verification_weight(
    worked: bool, verification: Mapping[str, object] | None
) -> float:
    if verification is None:
        return 0.5
    kind = verification.get("kind")
    if kind in {"test", "command"}:
        exit_code = verification.get("exit_code")
        if exit_code is None:
            return 0.75
        return 1.0 if (exit_code == 0) == worked else 0.25
    if kind == "review":
        return 0.75
    return 0.5


def fold_utility(contributions: Iterable[UtilityContribution]) -> UtilityEstimate:
    q_single = q_trace = 0.5
    evidence_count = single_evidence_count = 0
    for contribution in sorted(
        contributions, key=lambda item: (item.recorded_at_us, item.event_id)
    ):
        if contribution.depth <= 1:
            q_single += ALPHA * contribution.weight * (contribution.reward - q_single)
            single_evidence_count += 1
        factor = (
            1.0
            if contribution.depth <= 1
            else (GAMMA * LAMBDA) ** (contribution.depth - 1)
        )
        q_trace += (
            ALPHA * contribution.weight * factor * (contribution.reward - q_trace)
        )
        evidence_count += 1
    return UtilityEstimate(
        round(q_single, 6), round(q_trace, 6), evidence_count, single_evidence_count
    )


def utility_signal(
    estimate: UtilityEstimate, credit: Literal["single_step", "trace"]
) -> float:
    # Support counts only the evidence that moved the selected estimate.
    if credit == "single_step":
        q, support = estimate.q_single, estimate.single_evidence_count
    else:
        q, support = estimate.q_trace, estimate.evidence_count
    return (q - 0.5) * 2 * min(1.0, support / 3)


def utility_order(
    relevance: Sequence[float], signals: Sequence[float], weight: float
) -> tuple[int, ...]:
    """Order ranked candidates by rank prior plus weighted utility.

    ``relevance`` holds each candidate's fused score in the incoming rank
    order.  Utility only breaks near-ties: a candidate overtakes the one
    directly above it when its combined score is higher and both its rank
    prior and its fused score are within ``RELEVANCE_BAND`` of that
    candidate's, so outcome history cannot lift a clearly weaker match over
    a clearly stronger one, nor sink a strong match below distant ones.
    """

    priors = tuple(
        RANK_PRIOR_K / (RANK_PRIOR_K + index) for index in range(len(relevance))
    )
    combined = tuple(
        prior + weight * signal for prior, signal in zip(priors, signals, strict=True)
    )
    order: list[int] = []
    for index in range(len(relevance)):
        position = len(order)
        while position:
            above = order[position - 1]
            if (
                combined[index] <= combined[above]
                or priors[index] < RELEVANCE_BAND * priors[above]
                or relevance[index] < RELEVANCE_BAND * relevance[above]
            ):
                break
            position -= 1
        order.insert(position, index)
    return tuple(order)
