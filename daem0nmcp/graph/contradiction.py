"""
Contradiction Detection for Bi-Temporal Knowledge.

Implements contradiction detection via embedding similarity + negation patterns.
When new facts conflict with existing beliefs:
1. Identify high similarity + semantic negation = contradiction
2. Invalidate contradicted versions (set valid_to), not delete them
3. Preserve audit trail while ensuring current knowledge is accurate

Links:
- Uses daem0nmcp.vectors for cosine_similarity
- Uses MemoryVersion model for version invalidation
"""

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import MemoryVersion
from ..vectors import cosine_similarity, decode, encode_document

# Similarity threshold: Above this = potentially discussing same topic
SIMILARITY_THRESHOLD = 0.75

# Negation patterns that indicate semantic opposition
NEGATION_PATTERNS = [
    # Direct negation prefixes
    (r"\bnot\s+", r"\b"),  # "not X" vs "X"
    (r"\bno\s+", r"\b"),  # "no X" vs "X"
    (r"\bnever\s+", r"\b"),  # "never X" vs "X"
    (r"\bdon\'t\s+", r"\bdo\s+"),  # "don't X" vs "do X"
    (r"\bdoesn\'t\s+", r"\bdoes\s+"),  # "doesn't X" vs "does X"
    (r"\bwon\'t\s+", r"\bwill\s+"),  # "won't X" vs "will X"
    (r"\bcan\'t\s+", r"\bcan\s+"),  # "can't X" vs "can X"
    (r"\bcannot\s+", r"\bcan\s+"),  # "cannot X" vs "can X"
    (r"\bshouldn\'t\s+", r"\bshould\s+"),  # "shouldn't X" vs "should X"
    (r"\bisn\'t\s+", r"\bis\s+"),  # "isn't X" vs "is X"
    (r"\baren\'t\s+", r"\bare\s+"),  # "aren't X" vs "are X"
    (r"\bwasn\'t\s+", r"\bwas\s+"),  # "wasn't X" vs "was X"
    (r"\bweren\'t\s+", r"\bwere\s+"),  # "weren't X" vs "were X"
    # Antonym pairs (common in technical contexts)
    (r"\benable\b", r"\bdisable\b"),
    (r"\ballow\b", r"\bdeny\b"),
    (r"\ballow\b", r"\bblock\b"),
    (r"\baccept\b", r"\breject\b"),
    (r"\binclude\b", r"\bexclude\b"),
    (r"\brequire\b", r"\boptional\b"),
    (r"\brequired\b", r"\boptional\b"),
    (r"\btrue\b", r"\bfalse\b"),
    (r"\byes\b", r"\bno\b"),
    (r"\bvalid\b", r"\binvalid\b"),
    (r"\bcorrect\b", r"\bincorrect\b"),
    (r"\bsupported\b", r"\bunsupported\b"),
    (r"\bsafe\b", r"\bunsafe\b"),
    (r"\bsecure\b", r"\binsecure\b"),
    (r"\bworking\b", r"\bbroken\b"),
    (r"\bworks\b", r"\bfails\b"),
    (r"\buse\b", r"\bavoid\b"),
    (r"\brecommended\b", r"\bdeprecated\b"),
    (r"\bpreferred\b", r"\bdiscouraged\b"),
]


@dataclass
class Contradiction:
    """Represents a detected contradiction between memory versions."""

    new_content: str
    existing_version_id: int
    existing_content: str
    existing_memory_id: int
    similarity_score: float
    negation_pattern: tuple[str, str] | None = None
    reason: str = ""


def has_negation_mismatch(text1: str, text2: str) -> tuple[str, str] | None:
    """
    Check if two texts have negation patterns that indicate contradiction.

    Returns the matching pattern tuple if found, None otherwise.
    """
    text1_lower = text1.lower()
    text2_lower = text2.lower()

    for pattern1, pattern2 in NEGATION_PATTERNS:
        # Check if text1 has pattern1 and text2 has pattern2
        if re.search(pattern1, text1_lower) and re.search(pattern2, text2_lower):
            return (pattern1, pattern2)
        # Check reverse: text1 has pattern2 and text2 has pattern1
        if re.search(pattern2, text1_lower) and re.search(pattern1, text2_lower):
            return (pattern2, pattern1)

    return None


async def detect_contradictions(
    new_content: str,
    session: AsyncSession,
    memory_id: int | None = None,
    similarity_threshold: float = SIMILARITY_THRESHOLD,
) -> list[Contradiction]:
    """
    Detect if new content contradicts existing memory versions.

    Logic:
    1. Get all currently valid memory versions (valid_to IS NULL)
    2. For each version with embedding, compute similarity to new content
    3. If similarity > threshold, check for negation patterns
    4. High similarity + negation = contradiction

    Args:
        new_content: The new content to check for contradictions
        session: Database session
        memory_id: If provided, exclude versions from this memory (avoid self-contradiction)
        similarity_threshold: Minimum similarity to consider (default 0.75)

    Returns:
        List of Contradiction objects for detected conflicts
    """
    contradictions = []

    # Encode the new content
    new_embedding_bytes = encode_document(new_content)
    if new_embedding_bytes is None:
        # Can't compute similarity without embeddings
        return contradictions

    new_embedding = decode(new_embedding_bytes)
    if new_embedding is None:
        return contradictions

    # Query for valid versions (valid_to IS NULL means still valid)
    query = select(MemoryVersion).where(MemoryVersion.valid_to.is_(None))

    # Optionally exclude versions from the same memory
    if memory_id is not None:
        query = query.where(MemoryVersion.memory_id != memory_id)

    result = await session.execute(query)
    versions = result.scalars().all()

    # Cache embeddings to avoid recomputing for the same content across calls
    embedding_cache: dict[int, Any] = {}

    for version in versions:
        # Use cached embedding if available, otherwise compute and cache
        if version.id in embedding_cache:
            version_embedding = embedding_cache[version.id]
        else:
            version_embedding_bytes = encode_document(version.content)
            if version_embedding_bytes is None:
                continue
            version_embedding = decode(version_embedding_bytes)
            if version_embedding is None:
                continue
            embedding_cache[version.id] = version_embedding

        # Compute similarity
        similarity = cosine_similarity(new_embedding, version_embedding)

        if similarity >= similarity_threshold:
            # High similarity - check for negation mismatch
            negation = has_negation_mismatch(new_content, version.content)

            if negation:
                reason = f"High similarity ({similarity:.2f}) with negation pattern: {negation[0]} vs {negation[1]}"
                contradictions.append(
                    Contradiction(
                        new_content=new_content,
                        existing_version_id=version.id,
                        existing_content=version.content,
                        existing_memory_id=version.memory_id,
                        similarity_score=similarity,
                        negation_pattern=negation,
                        reason=reason,
                    )
                )

    return contradictions


async def invalidate_contradicted_facts(
    contradictions: list[Contradiction],
    new_version_id: int,
    session: AsyncSession,
    invalidation_time: datetime | None = None,
) -> int:
    """
    Invalidate memory versions that have been contradicted.

    Sets valid_to to mark the version as no longer valid, and
    links to the new version that invalidated it.

    Args:
        contradictions: List of detected contradictions
        new_version_id: The ID of the new version that caused the contradictions
        session: Database session
        invalidation_time: When to set valid_to (defaults to now)

    Returns:
        Number of versions invalidated
    """
    if not contradictions:
        return 0

    if invalidation_time is None:
        invalidation_time = datetime.now(timezone.utc)

    invalidated_count = 0

    for contradiction in contradictions:
        # Get the version to invalidate
        result = await session.execute(
            select(MemoryVersion).where(
                MemoryVersion.id == contradiction.existing_version_id
            )
        )
        version = result.scalar_one_or_none()

        if version is None:
            continue

        # Skip if already invalidated
        if version.valid_to is not None:
            continue

        # Invalidate the version
        version.valid_to = invalidation_time
        version.invalidated_by_version_id = new_version_id

        invalidated_count += 1

    return invalidated_count


async def check_and_invalidate_contradictions(
    new_content: str,
    new_version_id: int,
    session: AsyncSession,
    memory_id: int | None = None,
    similarity_threshold: float = SIMILARITY_THRESHOLD,
    invalidation_time: datetime | None = None,
) -> tuple[list[Contradiction], int]:
    """
    Combined detection and invalidation of contradictions.

    Convenience function that:
    1. Detects contradictions for new content
    2. Invalidates any contradicted versions
    3. Returns both the contradictions found and count invalidated

    Args:
        new_content: The new content to check for contradictions
        new_version_id: The ID of the new version
        session: Database session
        memory_id: If provided, exclude versions from this memory
        similarity_threshold: Minimum similarity to consider
        invalidation_time: When to set valid_to (defaults to now)

    Returns:
        Tuple of (contradictions list, number invalidated)
    """
    contradictions = await detect_contradictions(
        new_content=new_content,
        session=session,
        memory_id=memory_id,
        similarity_threshold=similarity_threshold,
    )

    invalidated = await invalidate_contradicted_facts(
        contradictions=contradictions,
        new_version_id=new_version_id,
        session=session,
        invalidation_time=invalidation_time,
    )

    return contradictions, invalidated
