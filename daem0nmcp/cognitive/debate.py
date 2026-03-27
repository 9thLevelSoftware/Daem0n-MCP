"""Adversarial Council -- the daemon convenes an internal debate where
advocate and challenger argue from memory evidence alone, and a judge
weighs the strength of what has been inscribed.

No LLM calls are made anywhere in this module.  All "arguments" are
constructed by querying the daemon's memory via ``recall()`` and scoring
the returned evidence.  The "judge" is a pure scoring function that
compares evidence metrics -- it does not generate text via an LLM.

Convergence detection stops the debate early when position scores
stabilize across consecutive rounds (minimum 2 rounds required).
A hard cap of ``max_rounds`` (default 5) prevents unbounded processing.

When the debate concludes, the consensus is persisted as a ``learning``
memory with full provenance tags (TOOL-04).
"""

import logging
import uuid
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ..context_manager import ProjectContext

try:
    from ..config import settings
except ImportError:
    from daem0nmcp.config import settings

from . import DebateArgument, DebateResult, DebateRound

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Evidence scoring
# ---------------------------------------------------------------------------


def score_evidence(memories: list[dict[str, Any]]) -> float:
    """Score evidence strength from recalled memories.

    Returns a float in [0.0, 1.0].  Higher scores indicate stronger
    evidence -- boosted by outcome success and source diversity.

    Args:
        memories: Flat list of memory dicts from ``recall()``.

    Returns:
        Aggregate evidence score between 0.0 and 1.0.
    """
    if not memories:
        return 0.0

    score = 0.0
    seen_files: set[str] = set()

    for mem in memories:
        relevance = mem.get("relevance", 0.5)

        # Outcome boost: worked=True is strong evidence
        if mem.get("worked") is True:
            relevance *= 1.5
        elif mem.get("worked") is False:
            relevance *= 0.5

        # Diversity bonus: different files = more convincing
        file_path = mem.get("file_path")
        if file_path and file_path not in seen_files:
            relevance *= 1.1
            seen_files.add(file_path)

        score += relevance

    return min(score / max(len(memories) * 1.5, 1.0), 1.0)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _extract_memories(recall_result: dict[str, Any]) -> list[dict[str, Any]]:
    """Extract a flat list of memory dicts from a ``recall()`` result.

    Handles both result structures:

    * **Categorized** -- keys ``decisions``, ``patterns``, ``warnings``,
      ``learnings`` each mapping to a list of memory dicts.
    * **Direct** -- a ``memories`` key mapping to a list of memory dicts.
    * **Fallback** -- returns an empty list.

    Args:
        recall_result: The dict returned by ``MemoryManager.recall()``.

    Returns:
        Flat list of memory dicts.
    """
    # Direct "memories" key (often the empty-result structure)
    if "memories" in recall_result:
        memories_val = recall_result["memories"]
        if isinstance(memories_val, list):
            return memories_val

    # Categorized structure (normal recall result)
    flat: list[dict[str, Any]] = []
    for cat_key in ("decisions", "patterns", "warnings", "learnings"):
        entries = recall_result.get(cat_key)
        if isinstance(entries, list):
            flat.extend(entries)

    return flat


async def _gather_evidence(
    topic: str,
    position: str,
    perspective: str,
    ctx: "ProjectContext",
) -> DebateArgument:
    """Gather evidence for one side of the debate.

    Calls ``recall()`` with a combined topic+position query and scores
    the returned memories to construct a :class:`DebateArgument`.

    Args:
        topic: The debate topic.
        position: The position to argue (e.g. "use React").
        perspective: ``"advocate"`` or ``"challenger"``.
        ctx: The active project context (provides memory_manager).

    Returns:
        A fully populated :class:`DebateArgument`.
    """
    recall_result = await ctx.memory_manager.recall(
        topic=f"{topic} {position}",
        project_path=ctx.project_path,
        limit=10,
    )

    memories = _extract_memories(recall_result)

    evidence_ids: list[int] = [m["id"] for m in memories if "id" in m]
    evidence_summaries: list[str] = [m.get("content", "")[:100] for m in memories[:5]]
    evidence_strength = score_evidence(memories)
    outcome_support = sum(1 for m in memories if m.get("worked") is True)
    outcome_against = sum(1 for m in memories if m.get("worked") is False)

    return DebateArgument(
        perspective=perspective,
        position=position,
        evidence_ids=evidence_ids,
        evidence_summaries=evidence_summaries,
        evidence_strength=evidence_strength,
        outcome_support=outcome_support,
        outcome_against=outcome_against,
    )


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


async def run_debate(
    topic: str,
    advocate_position: str,
    challenger_position: str,
    ctx: "ProjectContext",
    project_path: str = "",
) -> DebateResult:
    """Run a structured advocate/challenger/judge debate.

    All argumentation is grounded in memory evidence retrieved via
    ``recall()`` -- **no LLM calls are made**.  The judge is a pure
    scoring function comparing evidence metrics.

    The debate runs for up to ``max_rounds`` rounds (default 5).
    Convergence detection stops early (after a minimum of 2 rounds)
    when both sides' scores stabilize within the configured threshold.

    If both sides have fewer than ``min_evidence`` supporting memories,
    the debate exits early with ``confidence=0.0``.

    On completion, the consensus synthesis is persisted as a ``learning``
    memory with full provenance tags (debate ID, topic, positions,
    round count, and all cited evidence IDs).

    Args:
        topic: What the debate is about.
        advocate_position: The position the advocate argues for.
        challenger_position: The position the challenger argues for.
        ctx: The active project context.
        project_path: Project path for memory persistence.

    Returns:
        A :class:`DebateResult` with synthesis, confidence, and all
        round data.
    """
    try:
        return await _run_debate_inner(
            topic, advocate_position, challenger_position, ctx, project_path
        )
    except Exception as exc:
        logger.error("Unexpected error during debate on '%s': %s", topic, exc)
        return DebateResult(
            debate_id="error",
            topic=topic,
            advocate_position=advocate_position,
            challenger_position=challenger_position,
            rounds=[],
            total_rounds=0,
            converged=False,
            convergence_round=None,
            synthesis=f"Debate could not be completed due to an unexpected error: {exc}",
            confidence=0.0,
            winning_perspective="error",
            all_evidence_ids=[],
            consensus_memory_id=None,
        )


async def _run_debate_inner(
    topic: str,
    advocate_position: str,
    challenger_position: str,
    ctx: "ProjectContext",
    project_path: str,
) -> DebateResult:
    """Internal implementation of the debate loop.

    Separated from :func:`run_debate` so that the outer function can
    wrap the entire logic in a try/except for robustness.
    """
    debate_id = str(uuid.uuid4())[:12]
    max_rounds: int = getattr(settings, "cognitive_debate_max_rounds", 5)
    convergence_threshold: float = getattr(
        settings, "cognitive_debate_convergence_threshold", 0.05
    )
    min_evidence: int = getattr(settings, "cognitive_debate_min_evidence", 2)

    rounds: list[DebateRound] = []
    prev_advocate_score: float = 0.0
    prev_challenger_score: float = 0.0

    # ------------------------------------------------------------------
    # Early exit on insufficient evidence
    # ------------------------------------------------------------------
    preliminary_advocate = await _gather_evidence(
        topic, advocate_position, "advocate", ctx
    )
    preliminary_challenger = await _gather_evidence(
        topic, challenger_position, "challenger", ctx
    )

    if (
        len(preliminary_advocate.evidence_ids) < min_evidence
        and len(preliminary_challenger.evidence_ids) < min_evidence
    ):
        logger.info(
            "Debate '%s': insufficient evidence (advocate=%d, challenger=%d, min=%d)",
            topic,
            len(preliminary_advocate.evidence_ids),
            len(preliminary_challenger.evidence_ids),
            min_evidence,
        )
        return DebateResult(
            debate_id=debate_id,
            topic=topic,
            advocate_position=advocate_position,
            challenger_position=challenger_position,
            rounds=[],
            total_rounds=0,
            converged=False,
            convergence_round=None,
            synthesis=(
                "Insufficient evidence inscribed in the daemon's memory to "
                "conduct meaningful deliberation on this topic."
            ),
            confidence=0.0,
            winning_perspective="insufficient_evidence",
            all_evidence_ids=[],
            consensus_memory_id=None,
        )

    # ------------------------------------------------------------------
    # Debate rounds
    # ------------------------------------------------------------------
    converged = False
    convergence_round: int | None = None

    for round_num in range(1, max_rounds + 1):
        advocate = await _gather_evidence(topic, advocate_position, "advocate", ctx)
        challenger = await _gather_evidence(
            topic, challenger_position, "challenger", ctx
        )

        # Judge assessment (pure scoring -- no LLM)
        adv_str = advocate.evidence_strength
        chl_str = challenger.evidence_strength
        diff = abs(adv_str - chl_str)

        if diff <= 0.1:
            judge_assessment = (
                "The evidence is closely matched. Neither position commands "
                "decisive support from the daemon's inscribed memories."
            )
        elif adv_str > chl_str:
            judge_assessment = (
                f"The advocate's evidence holds greater weight -- "
                f"{adv_str:.2f} vs {chl_str:.2f}. "
                f"{len(advocate.evidence_ids)} memories support the "
                f"advocate's position."
            )
        else:
            judge_assessment = (
                f"The challenger's evidence holds greater weight -- "
                f"{chl_str:.2f} vs {adv_str:.2f}. "
                f"{len(challenger.evidence_ids)} memories support the "
                f"challenger's position."
            )

        debate_round = DebateRound(
            round_number=round_num,
            advocate_argument=advocate,
            challenger_argument=challenger,
            judge_assessment=judge_assessment,
            advocate_score=adv_str,
            challenger_score=chl_str,
        )
        rounds.append(debate_round)

        # Convergence detection (only after round 2)
        if round_num >= 2:
            advocate_delta = abs(adv_str - prev_advocate_score)
            challenger_delta = abs(chl_str - prev_challenger_score)
            if (
                advocate_delta < convergence_threshold
                and challenger_delta < convergence_threshold
            ):
                converged = True
                convergence_round = round_num
                logger.info(
                    "Debate '%s': converged at round %d "
                    "(advocate_delta=%.4f, challenger_delta=%.4f)",
                    topic,
                    round_num,
                    advocate_delta,
                    challenger_delta,
                )
                break

        prev_advocate_score = adv_str
        prev_challenger_score = chl_str

    # ------------------------------------------------------------------
    # Determine winner
    # ------------------------------------------------------------------
    total_rounds = len(rounds)
    final_advocate_score = rounds[-1].advocate_score
    final_challenger_score = rounds[-1].challenger_score

    if abs(final_advocate_score - final_challenger_score) < 0.1:
        winning_perspective = "balanced"
        winning_position = "both positions"
    elif final_advocate_score > final_challenger_score:
        winning_perspective = "advocate"
        winning_position = advocate_position
    else:
        winning_perspective = "challenger"
        winning_position = challenger_position

    # ------------------------------------------------------------------
    # Collect all evidence IDs (deduplicated)
    # ------------------------------------------------------------------
    all_evidence_ids_set: set[int] = set()
    for rnd in rounds:
        all_evidence_ids_set.update(rnd.advocate_argument.evidence_ids)
        all_evidence_ids_set.update(rnd.challenger_argument.evidence_ids)
    all_evidence_ids = sorted(all_evidence_ids_set)

    # ------------------------------------------------------------------
    # Compute confidence
    # ------------------------------------------------------------------
    total_evidence = len(all_evidence_ids)
    score_diff = abs(final_advocate_score - final_challenger_score)
    confidence = min(1.0, score_diff * 2.0 + (total_evidence / 20.0))
    confidence = max(0.0, confidence)

    # ------------------------------------------------------------------
    # Generate synthesis
    # ------------------------------------------------------------------
    synthesis_parts: list[str] = [
        f"The Adversarial Council has deliberated on '{topic}' "
        f"for {total_rounds} round{'s' if total_rounds != 1 else ''}.",
    ]
    if converged and convergence_round is not None:
        synthesis_parts.append(
            f"Positions stabilized at round {convergence_round} -- "
            f"further debate would yield no new insight."
        )
    if winning_perspective == "balanced":
        synthesis_parts.append(
            "Neither position commands decisive authority. "
            "The daemon holds both perspectives in tension."
        )
    else:
        synthesis_parts.append(
            f"The {winning_perspective} position prevails: '{winning_position}'."
        )
    synthesis_parts.append(
        f"Confidence: {confidence:.2f} based on "
        f"{total_evidence} piece{'s' if total_evidence != 1 else ''} "
        f"of inscribed evidence."
    )
    synthesis = " ".join(synthesis_parts)

    # ------------------------------------------------------------------
    # Persist consensus memory (TOOL-04)
    # ------------------------------------------------------------------
    consensus_memory_id: int | None = None
    try:
        remember_result = await ctx.memory_manager.remember(
            category="learning",
            content=f"Debate consensus: {synthesis}"[:1000],
            rationale=(
                f"Internal debate on '{topic}' "
                f"({total_rounds} rounds, confidence={confidence:.2f})"
            ),
            tags=[
                "debate",
                "consensus",
                f"debate-id:{debate_id}",
                f"topic:{topic[:50]}",
            ],
            context={
                "debate_id": debate_id,
                "topic": topic,
                "advocate_position": advocate_position,
                "challenger_position": challenger_position,
                "total_rounds": total_rounds,
                "converged": converged,
                "confidence": confidence,
                "winning_perspective": winning_perspective,
                "all_evidence_ids": all_evidence_ids,
            },
            project_path=project_path,
        )
        # Extract consensus memory ID from the remember() result
        if isinstance(remember_result, dict):
            consensus_memory_id = remember_result.get(
                "id", remember_result.get("memory_id")
            )
        logger.info(
            "Debate '%s': consensus persisted as memory %s",
            topic,
            consensus_memory_id,
        )
    except Exception as exc:
        logger.error(
            "Debate '%s': failed to persist consensus memory: %s",
            topic,
            exc,
        )

    # ------------------------------------------------------------------
    # Return result
    # ------------------------------------------------------------------
    return DebateResult(
        debate_id=debate_id,
        topic=topic,
        advocate_position=advocate_position,
        challenger_position=challenger_position,
        rounds=rounds,
        total_rounds=total_rounds,
        converged=converged,
        convergence_round=convergence_round,
        synthesis=synthesis,
        confidence=confidence,
        winning_perspective=winning_perspective,
        all_evidence_ids=all_evidence_ids,
        consensus_memory_id=consensus_memory_id,
    )
