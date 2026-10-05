"""Deterministic outcome evidence weighting and provenance credit folding."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Literal

ALPHA = 0.3
GAMMA = 0.8
LAMBDA = 0.7
MAX_DEPTH = 4
MAX_DESCENDANTS = 64


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
    evidence_count = 0
    for contribution in sorted(
        contributions, key=lambda item: (item.recorded_at_us, item.event_id)
    ):
        if contribution.depth <= 1:
            q_single += ALPHA * contribution.weight * (contribution.reward - q_single)
        factor = (
            1.0
            if contribution.depth <= 1
            else (GAMMA * LAMBDA) ** (contribution.depth - 1)
        )
        q_trace += (
            ALPHA * contribution.weight * factor * (contribution.reward - q_trace)
        )
        evidence_count += 1
    return UtilityEstimate(round(q_single, 6), round(q_trace, 6), evidence_count)


def utility_signal(
    estimate: UtilityEstimate, credit: Literal["single_step", "trace"]
) -> float:
    q = estimate.q_single if credit == "single_step" else estimate.q_trace
    return (q - 0.5) * 2 * min(1.0, estimate.evidence_count / 3)
