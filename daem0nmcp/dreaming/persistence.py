"""Dream session and result data models with persistence helpers.

DreamSession and DreamResult capture full provenance metadata for
autonomous re-evaluation of failed decisions during idle periods.
Dream results are stored as 'learning' category memories with
structured context dicts for traceability.
"""

import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class DreamResult:
    """Result of re-evaluating a single failed decision during dreaming."""

    source_decision_id: int  # Memory.id of the failed decision being re-evaluated
    original_content: str  # First 200 chars of the original decision content
    original_outcome: str | None  # The outcome text from the original decision
    insight: str  # The re-evaluation insight text
    result_type: str  # One of "revised", "confirmed_failure", "needs_more_data"
    evidence_ids: list[int] = field(
        default_factory=list
    )  # Memory.ids of evidence found


@dataclass
class DreamSession:
    """Tracks a complete dream session with provenance metadata."""

    session_id: str = field(default_factory=lambda: str(uuid.uuid4())[:12])
    project_path: str = ""
    started_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    ended_at: datetime | None = None
    decisions_reviewed: int = 0
    insights_generated: int = 0
    outcomes_resolved: int = 0
    results: list[DreamResult] = field(default_factory=list)
    interrupted: bool = False  # True if user returned before completion
    strategies_run: list[str] = field(default_factory=list)


async def persist_dream_result(
    memory_manager,
    result: DreamResult,
    session: DreamSession,
) -> dict[str, Any]:
    """Store a dream re-evaluation result as a learning memory.

    Args:
        memory_manager: MemoryManager instance for storing memories.
        result: The DreamResult to persist.
        session: The DreamSession this result belongs to.

    Returns:
        The result from memory_manager.remember(), or {"error": str} on failure.
    """
    try:
        # Build tags -- pending-resolution types get an extra tag for cooldown tracking
        tags = [
            "dream",
            "re-evaluation",
            f"dream-session:{session.session_id}",
            f"source-decision:{result.source_decision_id}",
        ]
        _pending_types = {
            "auto_resolved_success",
            "auto_resolved_failure",
            "flagged_for_review",
            "insufficient_evidence",
        }
        if result.result_type in _pending_types:
            tags.append("pending-resolution")

        return await memory_manager.remember(
            category="learning",
            content=f"Dream re-evaluation: {result.insight}",
            rationale=(
                f"Autonomous re-evaluation of decision #{result.source_decision_id} "
                f"during idle period (session {session.session_id})"
            ),
            tags=tags,
            context={
                "source_decision_id": result.source_decision_id,
                "dream_session_id": session.session_id,
                "re_evaluation_result": result.result_type,
                "original_content": result.original_content,
                "original_outcome": result.original_outcome,
                "evidence_ids": result.evidence_ids,
                "dream_timestamp": datetime.now(timezone.utc).isoformat(),
            },
            project_path=session.project_path,
        )
    except Exception as e:
        logger.warning(
            "Failed to persist dream result for decision #%d: %s",
            result.source_decision_id,
            e,
        )
        return {"error": str(e)}


async def persist_session_summary(
    memory_manager,
    session: DreamSession,
) -> dict[str, Any] | None:
    """Store a dream session summary as a learning memory.

    Only persists if the session generated at least one insight.

    Args:
        memory_manager: MemoryManager instance for storing memories.
        session: The completed DreamSession to summarize.

    Returns:
        The result from memory_manager.remember(), None if no insights,
        or {"error": str} on failure.
    """
    if session.insights_generated == 0:
        return None

    try:
        # Build content string with optional outcomes_resolved mention
        content = (
            f"Dream session summary: Reviewed {session.decisions_reviewed} "
            f"decisions, generated {session.insights_generated} insights"
            f" (strategies: {', '.join(session.strategies_run) or 'none'})"
        )
        if session.outcomes_resolved > 0:
            content += f", auto-resolved {session.outcomes_resolved} pending outcome(s)"

        return await memory_manager.remember(
            category="learning",
            content=content,
            rationale=f"Dream session {session.session_id} summary",
            tags=[
                "dream",
                "dream-summary",
                f"dream-session:{session.session_id}",
            ],
            context={
                "dream_session_id": session.session_id,
                "decisions_reviewed": session.decisions_reviewed,
                "insights_generated": session.insights_generated,
                "outcomes_resolved": session.outcomes_resolved,
                "strategies_run": session.strategies_run,
                "interrupted": session.interrupted,
                "started_at": session.started_at.isoformat(),
                "ended_at": (
                    session.ended_at.isoformat() if session.ended_at else None
                ),
                "dream_timestamp": datetime.now(timezone.utc).isoformat(),
            },
            project_path=session.project_path,
        )
    except Exception as e:
        logger.warning(
            "Failed to persist dream session summary %s: %s",
            session.session_id,
            e,
        )
        return {"error": str(e)}
