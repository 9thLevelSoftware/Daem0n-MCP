"""Temporal Scrying -- reconstruct and compare the knowledge that existed
at a past decision's moment of inscription.

Given a decision memory ID, this module:
1. Looks up the decision and determines when it was inscribed
2. Reconstructs the historical context (what was known THEN) via
   bi-temporal recall with ``as_of_time``
3. Retrieves the current context (what is known NOW)
4. Computes a structured knowledge diff (new, invalidated, changed evidence)
5. Returns a ``SimulationResult`` with counterfactual assessment

Falls back to ``Memory.created_at`` when no ``MemoryVersion`` records
exist (pre-v4.0 memories).
"""

import logging
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from sqlalchemy import select

if TYPE_CHECKING:
    from ..context_manager import ProjectContext

try:
    from ..models import Memory, MemoryVersion
except ImportError:
    from daem0nmcp.models import Memory, MemoryVersion

try:
    from . import SimulationResult
except ImportError:
    from daem0nmcp.cognitive import SimulationResult

logger = logging.getLogger(__name__)

# Category keys returned by MemoryManager.recall()
_RECALL_CATEGORIES = ("decisions", "patterns", "warnings", "learnings")


def _extract_memories_from_recall(
    recall_result: dict[str, Any],
) -> list[dict[str, Any]]:
    """Flatten the categorized recall result into a single memory list.

    Handles both the standard category-keyed dict (``decisions``,
    ``patterns``, ``warnings``, ``learnings``) and any future
    ``categorized_memories`` or ``memories`` wrapper key.
    """
    memories: list[dict[str, Any]] = []

    # Check for a top-level wrapper key first
    if "memories" in recall_result and isinstance(recall_result["memories"], list):
        return recall_result["memories"]
    if "categorized_memories" in recall_result and isinstance(
        recall_result["categorized_memories"], dict
    ):
        for cat_list in recall_result["categorized_memories"].values():
            if isinstance(cat_list, list):
                memories.extend(cat_list)
        return memories

    # Standard path: category keys at the top level
    for cat_key in _RECALL_CATEGORIES:
        cat_list = recall_result.get(cat_key)
        if isinstance(cat_list, list):
            for mem in cat_list:
                # Tag the category onto each memory dict for downstream use
                if "category" not in mem:
                    mem["category"] = cat_key.rstrip("s")  # decisions -> decision
                memories.append(mem)

    return memories


def _build_context_dict(
    memories: list[dict[str, Any]],
    recall_time: str,
) -> dict[str, Any]:
    """Build a context summary dict from a flat list of memory dicts."""
    return {
        "memory_count": len(memories),
        "memories": [
            {
                "id": m.get("id"),
                "content": (m.get("content") or "")[:100],
                "category": m.get("category", "unknown"),
                "worked": m.get("worked"),
            }
            for m in memories
        ],
        "recall_time": recall_time,
    }


async def run_simulation(
    decision_id: int,
    ctx: "ProjectContext",
) -> SimulationResult:
    """Perform temporal scrying on a past decision.

    Reconstructs the knowledge state at the decision's valid_time,
    compares it with the current knowledge state, and produces a
    structured diff revealing what has changed.

    Args:
        decision_id: The ``Memory.id`` of the decision to scry.
        ctx: The active ``ProjectContext`` providing database and
            memory manager access.

    Returns:
        A fully populated ``SimulationResult``.

    Raises:
        ValueError: If the decision memory does not exist.
    """
    try:
        return await _run_simulation_inner(decision_id, ctx)
    except ValueError:
        # Let explicit validation errors propagate
        raise
    except Exception as exc:
        logger.error(
            "Temporal scrying failed for decision #%d: %s",
            decision_id,
            exc,
            exc_info=True,
        )
        return SimulationResult(
            decision_id=decision_id,
            decision_content="<scrying failed>",
            decision_time=datetime.now(timezone.utc).isoformat(),
            historical_context={},
            current_context={},
            knowledge_diff={
                "new_evidence": [],
                "invalidated_evidence": [],
                "outcome_changes": [],
                "new_count": 0,
                "invalidated_count": 0,
                "changed_count": 0,
            },
            counterfactual_assessment=(f"The daemon's scrying was disrupted: {exc}"),
            confidence=0.0,
        )


async def _run_simulation_inner(
    decision_id: int,
    ctx: "ProjectContext",
) -> SimulationResult:
    """Core simulation logic, separated for clean error handling."""
    mm = ctx.memory_manager

    # ------------------------------------------------------------------
    # 1. Look up the decision memory
    # ------------------------------------------------------------------
    async with ctx.db_manager.get_session() as session:
        result = await session.execute(select(Memory).where(Memory.id == decision_id))
        decision = result.scalar_one_or_none()

    if not decision:
        raise ValueError(
            f"The daemon cannot scry a decision that was never inscribed "
            f"(memory #{decision_id} not found)"
        )

    decision_content = decision.content
    query_topic = decision_content[:200]

    # ------------------------------------------------------------------
    # 2. Determine decision time from version history
    # ------------------------------------------------------------------
    async with ctx.db_manager.get_session() as session:
        version_result = await session.execute(
            select(MemoryVersion)
            .where(MemoryVersion.memory_id == decision_id)
            .order_by(MemoryVersion.version_number.asc())
            .limit(1)
        )
        first_version = version_result.scalar_one_or_none()

    if first_version and first_version.changed_at:
        decision_time_iso = first_version.changed_at.isoformat()
        decision_time_dt = first_version.changed_at
    else:
        # Fallback for pre-v4.0 memories without versions
        decision_time_iso = decision.created_at.isoformat()
        decision_time_dt = decision.created_at

    # Normalize to UTC-aware datetime for recall
    if decision_time_dt.tzinfo is None:
        decision_time_dt = decision_time_dt.replace(tzinfo=timezone.utc)

    # ------------------------------------------------------------------
    # 3. Reconstruct historical context (what was known THEN)
    # ------------------------------------------------------------------
    historical_recall = await mm.recall(
        topic=query_topic,
        as_of_time=decision_time_dt,
        project_path=ctx.project_path,
    )
    historical_memories = _extract_memories_from_recall(historical_recall)
    historical_context = _build_context_dict(historical_memories, decision_time_iso)

    # ------------------------------------------------------------------
    # 4. Get current context (what is known NOW)
    # ------------------------------------------------------------------
    current_recall = await mm.recall(
        topic=query_topic,
        project_path=ctx.project_path,
    )
    current_memories = _extract_memories_from_recall(current_recall)
    current_time_iso = datetime.now(timezone.utc).isoformat()
    current_context = _build_context_dict(current_memories, current_time_iso)

    # ------------------------------------------------------------------
    # 5. Compute structured knowledge diff
    # ------------------------------------------------------------------
    historical_ids = {
        m.get("id") for m in historical_memories if m.get("id") is not None
    }
    current_ids = {m.get("id") for m in current_memories if m.get("id") is not None}

    # Index memories by ID for easy lookup
    historical_by_id = {
        m["id"]: m for m in historical_memories if m.get("id") is not None
    }
    current_by_id = {m["id"]: m for m in current_memories if m.get("id") is not None}

    # New evidence: memories present now but not at decision time
    new_ids = current_ids - historical_ids
    new_evidence = [
        {
            "id": mid,
            "content": (current_by_id[mid].get("content") or "")[:100],
            "category": current_by_id[mid].get("category", "unknown"),
        }
        for mid in sorted(new_ids)
    ]

    # Invalidated evidence: memories present then but not now
    invalidated_ids = historical_ids - current_ids
    invalidated_evidence = [
        {
            "id": mid,
            "content": (historical_by_id[mid].get("content") or "")[:100],
            "category": historical_by_id[mid].get("category", "unknown"),
        }
        for mid in sorted(invalidated_ids)
    ]

    # Outcome changes: overlapping memories whose worked status changed
    overlap_ids = historical_ids & current_ids
    outcome_changes: list[dict[str, Any]] = []
    for mid in sorted(overlap_ids):
        old_worked = historical_by_id[mid].get("worked")
        new_worked = current_by_id[mid].get("worked")
        if old_worked != new_worked:
            outcome_changes.append(
                {
                    "id": mid,
                    "content": (current_by_id[mid].get("content") or "")[:100],
                    "old_worked": old_worked,
                    "new_worked": new_worked,
                }
            )

    new_count = len(new_evidence)
    invalidated_count = len(invalidated_evidence)
    changed_count = len(outcome_changes)

    knowledge_diff = {
        "new_evidence": new_evidence,
        "invalidated_evidence": invalidated_evidence,
        "outcome_changes": outcome_changes,
        "new_count": new_count,
        "invalidated_count": invalidated_count,
        "changed_count": changed_count,
    }

    # ------------------------------------------------------------------
    # 6. Generate counterfactual assessment
    # ------------------------------------------------------------------
    historical_count = len(historical_memories)
    parts = [
        f"Temporal Scrying of Decision #{decision_id}: "
        f"At the time of inscription, {historical_count} memories informed "
        f"this decision. Since then, {new_count} new memories have emerged "
        f"and {invalidated_count} have faded. "
        f"{changed_count} outcomes have shifted."
    ]

    if new_count > 0:
        parts.append(
            "The daemon now possesses knowledge that did not exist "
            "when this decision was made."
        )
    if invalidated_count > 0:
        parts.append(
            "Some evidence that supported the original decision has "
            "since been invalidated or forgotten."
        )
    if changed_count > 0:
        parts.append(
            "The outcomes of related decisions have shifted, potentially "
            "altering the calculus of this choice."
        )
    if new_count == 0 and invalidated_count == 0 and changed_count == 0:
        parts.append(
            "The context remains unchanged -- the original decision "
            "stands on the same ground."
        )

    counterfactual_assessment = " ".join(parts)

    # ------------------------------------------------------------------
    # 7. Compute confidence (evidence landscape change magnitude)
    # ------------------------------------------------------------------
    total_changes = new_count + invalidated_count + changed_count
    confidence = min(
        1.0,
        total_changes / max(historical_count, 1) * 0.5,
    )

    return SimulationResult(
        decision_id=decision_id,
        decision_content=decision_content,
        decision_time=decision_time_iso,
        historical_context=historical_context,
        current_context=current_context,
        knowledge_diff=knowledge_diff,
        counterfactual_assessment=counterfactual_assessment,
        confidence=confidence,
    )
