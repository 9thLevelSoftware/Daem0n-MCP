"""
Memory Consolidation for Metacognitive Architecture.

Consolidates similar episodic reflections into semantic pattern memories.
Per CONTEXT.md:
- Consolidation after 5+ similar occurrences
- Creates "pattern" category memory (semantic, permanent)
- Links to source reflections via supersedes edges

Based on AriGraph and memory survey (arxiv 2512.13564) patterns.
"""

import logging
from collections import Counter
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ..memory import MemoryManager

logger = logging.getLogger(__name__)

# Default consolidation threshold per CONTEXT.md
DEFAULT_CONSOLIDATION_THRESHOLD = 5


def extract_common_elements(contents: list[str]) -> str:
    """
    Extract common elements from a list of similar content strings.

    Uses word frequency analysis to identify the most common patterns.

    Args:
        contents: List of content strings to analyze

    Returns:
        Summary string of common elements
    """
    if not contents:
        return ""

    # Tokenize all contents
    all_words: list[str] = []
    for content in contents:
        # Simple tokenization - lowercase, split on whitespace/punctuation
        words = content.lower().replace(",", " ").replace(".", " ").split()
        all_words.extend(words)

    # Count word frequencies
    word_freq = Counter(all_words)

    # Filter to words appearing in majority of contents
    threshold = len(contents) // 2
    common_words = [
        word for word, count in word_freq.most_common(50) if count >= threshold
    ]

    # Filter out very common stop words
    stop_words = {
        "the",
        "a",
        "an",
        "is",
        "are",
        "was",
        "were",
        "to",
        "of",
        "in",
        "for",
        "on",
        "with",
    }
    common_words = [w for w in common_words if w not in stop_words and len(w) > 2]

    if not common_words:
        # Fallback: use first content as representative
        return contents[0][:200] if contents else ""

    return " ".join(common_words[:15])  # Limit to 15 most common terms


def identify_pattern_type(reflections: list[dict[str, Any]]) -> str:
    """
    Identify the pattern type from reflection error types.

    Args:
        reflections: List of reflection memory dicts

    Returns:
        The most common error type among reflections
    """
    error_types = []
    for reflection in reflections:
        tags = reflection.get("tags", [])
        for tag in tags:
            if tag in (
                "conflict",
                "factual_error",
                "unverified_claim",
                "quality_improvement",
            ):
                error_types.append(tag)

    if error_types:
        return Counter(error_types).most_common(1)[0][0]
    return "general"


async def consolidate_reflections(
    error_signature: str,
    memory_manager: "MemoryManager",
    consolidation_threshold: int = DEFAULT_CONSOLIDATION_THRESHOLD,
) -> int | None:
    """
    Consolidate similar episodic reflections into a semantic pattern.

    When 5+ similar reflections exist, creates a semantic "pattern" memory
    that abstracts the lesson learned, and links it to source reflections.

    Args:
        error_signature: The error signature to consolidate
        memory_manager: MemoryManager instance
        consolidation_threshold: Number of reflections needed (default: 5)

    Returns:
        Pattern memory ID if created, None if not enough reflections
    """
    # Find reflections with this error signature
    try:
        result = await memory_manager.recall(
            topic=f"reflection error {error_signature}",
            categories=["reflection"],
            tags=["reflection", f"sig:{error_signature}"],
            limit=consolidation_threshold + 5,  # Buffer for safety
        )
    except Exception as e:
        logger.error(f"Failed to retrieve reflections for consolidation: {e}")
        return None

    reflections = result.get("memories", [])

    if len(reflections) < consolidation_threshold:
        logger.debug(
            f"Not enough reflections for consolidation: "
            f"{len(reflections)} < {consolidation_threshold}"
        )
        return None

    # Extract common elements from reflection contents
    contents = [r.get("content", "") for r in reflections]
    common_elements = extract_common_elements(contents)

    # Identify pattern type
    pattern_type = identify_pattern_type(reflections)

    # Create semantic pattern memory
    pattern_content = f"Learned pattern from {len(reflections)} similar corrections: {common_elements}"

    pattern_tags = [
        "pattern",
        "learned-pattern",
        "consolidated",
        pattern_type,
        f"sig:{error_signature}",
    ]

    # Extract source reflection IDs
    source_ids = [r.get("id") for r in reflections if r.get("id")]

    pattern_context = {
        "source_reflection_ids": source_ids,
        "consolidation_date": datetime.now(timezone.utc).isoformat(),
        "reflection_count": len(reflections),
        "error_signature": error_signature,
        "pattern_type": pattern_type,
    }

    try:
        # Store pattern memory
        pattern_result = await memory_manager.remember(
            category="pattern",  # Semantic, permanent
            content=pattern_content,
            rationale=f"Consolidated from {len(reflections)} episodic reflections",
            tags=pattern_tags,
            context=pattern_context,
        )

        pattern_id = pattern_result.get("id")
        if not pattern_id:
            logger.error("Failed to get pattern memory ID")
            return None

        logger.info(
            f"Created pattern memory {pattern_id} from {len(reflections)} reflections"
        )

        # Link pattern to source reflections via supersedes
        # Note: link_memories may not exist; only attempt if available
        if hasattr(memory_manager, "link_memories"):
            linked_count = 0
            for source_id in source_ids:
                if source_id:
                    try:
                        await memory_manager.link_memories(
                            source_id=pattern_id,
                            target_id=source_id,
                            relationship="supersedes",
                        )
                        linked_count += 1
                    except Exception as link_error:
                        logger.warning(
                            f"Failed to link to reflection {source_id}: {link_error}"
                        )

            logger.info(f"Linked pattern to {linked_count} source reflections")
        else:
            logger.debug("link_memories not available, skipping supersedes edges")

        return pattern_id

    except Exception as e:
        logger.error(f"Failed to create pattern memory: {e}")
        return None


async def check_and_consolidate(
    memory_manager: "MemoryManager",
    consolidation_threshold: int = DEFAULT_CONSOLIDATION_THRESHOLD,
) -> list[int]:
    """
    Check all reflection signatures and consolidate where threshold is met.

    Scans all reflection memories and groups by error signature,
    then consolidates any that meet the threshold.

    Args:
        memory_manager: MemoryManager instance
        consolidation_threshold: Number needed for consolidation

    Returns:
        List of created pattern memory IDs
    """
    # Get all reflection memories
    try:
        result = await memory_manager.recall(
            topic="reflection",
            categories=["reflection"],
            limit=100,  # Reasonable limit
        )
    except Exception as e:
        logger.error(f"Failed to retrieve reflections: {e}")
        return []

    reflections = result.get("memories", [])

    # Group by error signature
    signature_groups: dict[str, list[dict]] = {}
    for reflection in reflections:
        tags = reflection.get("tags", [])
        sig_tag = next((t for t in tags if t.startswith("sig:")), None)
        if sig_tag:
            signature = sig_tag[4:]  # Remove "sig:" prefix
            if signature not in signature_groups:
                signature_groups[signature] = []
            signature_groups[signature].append(reflection)

    # Consolidate groups that meet threshold
    pattern_ids = []
    for signature, group in signature_groups.items():
        if len(group) >= consolidation_threshold:
            # Check if already consolidated (has pattern memory)
            pattern_result = await memory_manager.recall(
                topic=f"pattern {signature}",
                categories=["pattern"],
                tags=[f"sig:{signature}"],
                limit=1,
            )
            if pattern_result.get("memories"):
                logger.debug(f"Signature {signature} already consolidated")
                continue

            pattern_id = await consolidate_reflections(
                error_signature=signature,
                memory_manager=memory_manager,
                consolidation_threshold=consolidation_threshold,
            )
            if pattern_id:
                pattern_ids.append(pattern_id)

    return pattern_ids
