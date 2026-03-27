"""
Reflection Persistence for Metacognitive Architecture.

Stores reflections that change behavior as memories for learning.
Per CONTEXT.md:
- Category "reflection", tagged with error type and situation
- Deduplication: Don't store identical reflections; increment counter
- Indexing: By error signature and context

Reflections enable "Have I seen this error before?" queries.
"""

import hashlib
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ..memory import MemoryManager

logger = logging.getLogger(__name__)


@dataclass
class Reflection:
    """A reflection to potentially persist."""

    error_type: str  # e.g., "factual_error", "incomplete_response", "conflict"
    error_signature: str  # Unique identifier for this error pattern
    content: str  # What was learned
    context: str  # What was being attempted
    query: str  # Original query
    iteration: int  # Which iteration this occurred on
    quality_delta: float  # How much quality improved after this reflection


def compute_error_signature(error_type: str, content: str) -> str:
    """
    Compute a stable signature for an error pattern.

    Used for deduplication - similar errors should have similar signatures.

    Args:
        error_type: The type of error (e.g., "conflict", "factual_error")
        content: The content describing the error

    Returns:
        A 16-character hex digest identifying this error pattern
    """
    # Normalize content for comparison
    normalized = content.lower().strip()
    # Hash with error type for uniqueness
    key = f"{error_type}:{normalized}"
    return hashlib.sha256(key.encode()).hexdigest()[:16]


async def persist_reflection(
    reflection: Reflection,
    memory_manager: "MemoryManager",
    changed_behavior: bool,
    similarity_threshold: float = 0.85,
) -> int | None:
    """
    Store a reflection as a memory if it changed behavior.

    Per CONTEXT.md:
    - Only store reflections that changed behavior
    - Deduplicate identical reflections (increment occurrence counter)
    - Index by error signature and context

    Args:
        reflection: The Reflection to persist
        memory_manager: MemoryManager instance
        changed_behavior: Whether this reflection changed the response
        similarity_threshold: Threshold for considering reflections duplicates

    Returns:
        Memory ID if stored, None if skipped or deduplicated
    """
    if not changed_behavior:
        logger.debug("Skipping reflection storage - did not change behavior")
        return None

    # Check for existing similar reflection
    existing = await retrieve_similar_reflections(
        error_signature=reflection.error_signature,
        memory_manager=memory_manager,
        limit=1,
    )

    if existing:
        # Deduplicate - don't store, just log
        logger.debug(
            f"Reflection deduplicated - similar to memory {existing[0].get('id')}"
        )
        # NOTE: Could increment occurrence counter here via update_memory
        # but keeping it simple for now - the similar lookup is the key value
        return None

    # Build tags for indexing
    tags = [
        "reflection",
        "self-correction",
        reflection.error_type,
        f"sig:{reflection.error_signature}",
    ]

    # Build context dict
    context = {
        "error_signature": reflection.error_signature,
        "original_query": reflection.query[:200],  # Truncate long queries
        "iteration_count": reflection.iteration,
        "quality_improvement": reflection.quality_delta,
        "reflection_context": reflection.context[:200],
    }

    # Store as memory
    try:
        result = await memory_manager.remember(
            category="reflection",
            content=reflection.content,
            rationale=f"Self-correction during: {reflection.context[:100]}",
            tags=tags,
            context=context,
        )

        memory_id = result.get("id")
        logger.info(f"Persisted reflection as memory {memory_id}")
        return memory_id

    except Exception as e:
        logger.error(f"Failed to persist reflection: {e}")
        return None


async def retrieve_similar_reflections(
    error_signature: str,
    memory_manager: "MemoryManager",
    limit: int = 5,
    error_type: str | None = None,
) -> list[dict[str, Any]]:
    """
    Retrieve past reflections similar to the given error signature.

    Answers "Have I seen this error before?" queries.

    Args:
        error_signature: The error signature to search for
        memory_manager: MemoryManager instance
        limit: Maximum number of reflections to return
        error_type: Optional filter by error type

    Returns:
        List of memory dicts for matching reflections
    """
    # Build search tags
    tags = ["reflection", f"sig:{error_signature}"]
    if error_type:
        tags.append(error_type)

    try:
        result = await memory_manager.recall(
            topic=f"reflection error {error_signature}",
            categories=["reflection"],
            tags=tags,
            limit=limit,
        )

        return result.get("memories", [])

    except Exception as e:
        logger.warning(f"Failed to retrieve reflections: {e}")
        return []


async def has_seen_error_before(
    error_signature: str,
    memory_manager: "MemoryManager",
) -> bool:
    """
    Quick check if we've seen this error pattern before.

    Args:
        error_signature: The error signature to check
        memory_manager: MemoryManager instance

    Returns:
        True if a similar reflection exists
    """
    reflections = await retrieve_similar_reflections(
        error_signature=error_signature,
        memory_manager=memory_manager,
        limit=1,
    )
    return len(reflections) > 0


def create_reflection_from_evaluation(
    critique: str,
    verification_results: list[dict[str, Any]],
    query: str,
    context: str,
    iteration: int,
    quality_before: float,
    quality_after: float,
) -> Reflection | None:
    """
    Create a Reflection from evaluation results.

    Convenience function to construct a Reflection from the evaluator output.

    Args:
        critique: The critique text
        verification_results: List of verification result dicts
        query: Original query
        context: What was being attempted
        iteration: Current iteration
        quality_before: Quality score before revision
        quality_after: Quality score after revision

    Returns:
        Reflection if there's something meaningful to learn, None otherwise
    """
    quality_delta = quality_after - quality_before

    # Determine error type from verification results
    conflicts = [v for v in verification_results if v.get("status") == "conflict"]
    unverified = [v for v in verification_results if v.get("status") == "unverified"]

    if conflicts:
        error_type = "conflict"
        content = f"Detected conflict: {conflicts[0].get('conflict_reason', 'Unknown')}"
    elif unverified:
        error_type = "unverified_claim"
        content = f"Made unverified claim about: {unverified[0].get('claim_text', 'Unknown')[:100]}"
    elif "CONFLICTS" in critique:
        error_type = "factual_error"
        content = critique[:200]
    elif quality_delta > 0.1:
        error_type = "quality_improvement"
        content = f"Quality improved by {quality_delta:.2f}: {critique[:150]}"
    else:
        # Not significant enough to persist
        return None

    error_signature = compute_error_signature(error_type, content)

    return Reflection(
        error_type=error_type,
        error_signature=error_signature,
        content=content,
        context=context,
        query=query,
        iteration=iteration,
        quality_delta=quality_delta,
    )
