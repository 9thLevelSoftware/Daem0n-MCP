"""Cognitive tools -- the daemon's instruments of deeper reasoning.

This package provides the shared result types and orchestration logic
for the daemon's cognitive capabilities:

- **Temporal Scrying** (simulate): Reconstruct historical knowledge states
  and compare with current understanding to reveal what has changed.
- **Rule Entropy Analysis** (evolve): Detect staleness in the daemon's
  rule engine by cross-referencing code drift and outcome correlation.
- **Adversarial Council** (debate): Run structured advocate/challenger
  debates grounded entirely in memory evidence, with convergence detection.

All result dataclasses are defined here as the shared foundation used
by the individual cognitive modules (simulate.py, evolve.py, debate.py).
"""

from dataclasses import asdict, dataclass
from typing import Any

__all__ = [
    "SimulationResult",
    "StalenessReport",
    "DebateArgument",
    "DebateRound",
    "DebateResult",
]


# ---------------------------------------------------------------------------
# Temporal Scrying -- counterfactual decision replay
# ---------------------------------------------------------------------------


@dataclass
class SimulationResult:
    """Result of counterfactual decision simulation.

    Captures the full context diff between what was known at the time of
    a past decision and what is known now, enabling the daemon to assess
    whether a decision would be made differently with current knowledge.
    """

    decision_id: int
    decision_content: str
    decision_time: str  # ISO timestamp of the original decision
    historical_context: dict[str, Any]  # What was known at decision time
    current_context: dict[str, Any]  # What is known now
    knowledge_diff: dict[str, Any]  # Structured diff (new, invalidated, changed)
    counterfactual_assessment: str  # Text summary of what changed
    confidence: float  # 0.0-1.0 -- how much the evidence landscape shifted

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a plain dict for MCP tool return values."""
        return asdict(self)


# ---------------------------------------------------------------------------
# Rule Entropy Analysis -- staleness detection and evolution suggestions
# ---------------------------------------------------------------------------


@dataclass
class StalenessReport:
    """Report on rule staleness with evolution suggestions.

    Quantifies how much a rule's trigger text has drifted from the
    current codebase and outcome history, producing a composite
    staleness score and concrete suggestions for rule evolution.
    """

    rule_id: int
    rule_trigger: str
    staleness_score: float  # 0.0 (fresh) to 1.0 (fully stale)
    code_drift_score: float  # How much referenced code has changed
    outcome_correlation_score: float  # Worked/failed ratio quality
    age_factor: float  # Time-based decay contribution
    referenced_entities: list[dict[str, Any]]  # Code entities mentioned in trigger
    missing_entities: list[str]  # Entities no longer in codebase
    outcome_summary: dict[str, int]  # {"worked": N, "failed": M, "unknown": K}
    evolution_suggestions: list[dict[str, Any]]  # Concrete proposed changes

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a plain dict for MCP tool return values."""
        return asdict(self)


# ---------------------------------------------------------------------------
# Adversarial Council -- memory-grounded debate
# ---------------------------------------------------------------------------


@dataclass
class DebateArgument:
    """A single argument in the debate, grounded in memory evidence.

    Each argument MUST cite retrieved memory evidence -- the daemon
    does not fabricate reasoning.  Arguments are scored by the quality
    and quantity of their supporting memories.
    """

    perspective: str  # "advocate" or "challenger"
    position: str
    evidence_ids: list[int]  # Memory IDs supporting this argument
    evidence_summaries: list[str]  # Content previews of cited memories
    evidence_strength: float  # Aggregate evidence score (0.0-1.0)
    outcome_support: int  # Count of worked=True evidence
    outcome_against: int  # Count of worked=False evidence

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a plain dict for MCP tool return values."""
        return asdict(self)


@dataclass
class DebateRound:
    """A single round of adversarial debate.

    Each round pits the advocate's evidence against the challenger's
    evidence, with a judge assessment determining running scores.
    """

    round_number: int
    advocate_argument: DebateArgument
    challenger_argument: DebateArgument
    judge_assessment: str  # Which side had stronger evidence this round
    advocate_score: float  # Running cumulative score
    challenger_score: float  # Running cumulative score

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a plain dict for MCP tool return values."""
        result = asdict(self)
        # asdict handles nested dataclasses automatically
        return result


@dataclass
class DebateResult:
    """Complete debate result with synthesis.

    Captures the full adversarial council session: all rounds, convergence
    information, and the final consensus synthesis.  Optionally stores the
    consensus as a new memory for future reference.
    """

    debate_id: str  # UUID for the debate session
    topic: str
    advocate_position: str
    challenger_position: str
    rounds: list[DebateRound]
    total_rounds: int
    converged: bool  # Did positions stabilize before max rounds?
    convergence_round: int | None  # Round at which convergence was detected
    synthesis: str  # Consensus statement
    confidence: float  # How strong the consensus is (0.0-1.0)
    winning_perspective: str  # "advocate", "challenger", or "balanced"
    all_evidence_ids: list[int]  # All memory IDs cited across all rounds
    consensus_memory_id: int | None  # ID of persisted consensus memory

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a plain dict for MCP tool return values."""
        result = asdict(self)
        # asdict recursively converts nested dataclasses (DebateRound,
        # DebateArgument) into dicts, so no special handling needed.
        return result
