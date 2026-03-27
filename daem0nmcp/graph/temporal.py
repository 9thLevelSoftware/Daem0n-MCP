"""
Bi-Temporal Operations - Track what was true and when we learned it.

This module provides helpers for:
- Creating memory versions with valid_time
- Point-in-time queries (what did we know at time T?)
- Temporal filtering for recall operations

Time dimensions:
- transaction_time (changed_at): When we recorded this in the system
- valid_time (valid_from/valid_to): When this was true in reality
"""

import logging
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from sqlalchemy import or_, select

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from ..models import MemoryVersion

logger = logging.getLogger(__name__)


async def create_temporal_version(
    session: "AsyncSession",
    memory_id: int,
    version_number: int,
    content: str,
    rationale: str | None,
    context: dict,
    tags: list,
    outcome: str | None,
    worked: bool | None,
    change_type: str,
    change_description: str | None = None,
    valid_from: datetime | None = None,
) -> "MemoryVersion":
    """
    Create a MemoryVersion with proper bi-temporal timestamps.

    Args:
        session: Database session
        memory_id: Parent memory ID
        version_number: Sequential version number
        content: Version content snapshot
        rationale: Version rationale snapshot
        context: Version context snapshot
        tags: Version tags snapshot
        outcome: Outcome state at this version
        worked: Worked state at this version
        change_type: What triggered this version
        change_description: Optional description
        valid_from: When this fact became true (default: now)

    Returns:
        Created MemoryVersion with both time dimensions set
    """
    from ..models import MemoryVersion

    now = datetime.now(timezone.utc)

    # If valid_from not specified, fact is true as of now
    if valid_from is None:
        valid_from = now

    # Ensure timezone awareness
    if valid_from.tzinfo is None:
        valid_from = valid_from.replace(tzinfo=timezone.utc)

    version = MemoryVersion(
        memory_id=memory_id,
        version_number=version_number,
        content=content,
        rationale=rationale,
        context=context,
        tags=tags,
        outcome=outcome,
        worked=worked,
        change_type=change_type,
        change_description=change_description,
        changed_at=now,  # transaction_time: when we learned this
        valid_from=valid_from,  # valid_time: when this was true
        valid_to=None,  # NULL = currently valid
    )

    session.add(version)
    return version


async def get_versions_at_time(
    session: "AsyncSession",
    memory_id: int,
    as_of_valid_time: datetime,
    as_of_transaction_time: datetime | None = None,
) -> list["MemoryVersion"]:
    """
    Get memory versions valid at a specific point in time.

    This implements the core bi-temporal query pattern:
    - valid_time filter: fact was true at query time
    - transaction_time filter: we knew about it at query time

    Args:
        session: Database session
        memory_id: Memory to query
        as_of_valid_time: Return facts true at this time
        as_of_transaction_time: Return knowledge as of this time (default: now)

    Returns:
        List of MemoryVersion objects valid at the specified time
    """
    from ..models import MemoryVersion

    if as_of_transaction_time is None:
        as_of_transaction_time = datetime.now(timezone.utc)

    # Ensure timezone awareness
    if as_of_valid_time.tzinfo is None:
        as_of_valid_time = as_of_valid_time.replace(tzinfo=timezone.utc)
    if as_of_transaction_time.tzinfo is None:
        as_of_transaction_time = as_of_transaction_time.replace(tzinfo=timezone.utc)

    result = await session.execute(
        select(MemoryVersion)
        .where(MemoryVersion.memory_id == memory_id)
        # Valid time filter: fact was true at query time
        # Handle NULL valid_from as "same as changed_at" (backwards compatibility)
        .where(
            or_(
                MemoryVersion.valid_from <= as_of_valid_time,
                MemoryVersion.valid_from.is_(None),  # NULL means valid since changed_at
            )
        )
        .where(
            or_(
                MemoryVersion.valid_to.is_(None),
                MemoryVersion.valid_to > as_of_valid_time,
            )
        )
        # Transaction time filter: we knew about it at query time
        .where(MemoryVersion.changed_at <= as_of_transaction_time)
        .order_by(MemoryVersion.version_number.desc())
    )

    return list(result.scalars().all())


async def invalidate_version(
    session: "AsyncSession",
    version_id: int,
    invalidated_by_version_id: int,
    invalidation_time: datetime | None = None,
) -> bool:
    """
    Mark a version as invalidated (set valid_to).

    Used when new information contradicts existing knowledge.
    Does NOT delete - preserves audit trail.

    Args:
        session: Database session
        version_id: Version to invalidate
        invalidated_by_version_id: Version that caused invalidation
        invalidation_time: When invalidation occurred (default: now)

    Returns:
        True if version was invalidated, False if already invalid
    """
    from sqlalchemy import update

    from ..models import MemoryVersion

    if invalidation_time is None:
        invalidation_time = datetime.now(timezone.utc)

    if invalidation_time.tzinfo is None:
        invalidation_time = invalidation_time.replace(tzinfo=timezone.utc)

    result = await session.execute(
        update(MemoryVersion)
        .where(MemoryVersion.id == version_id)
        .where(MemoryVersion.valid_to.is_(None))  # Only invalidate current versions
        .values(
            valid_to=invalidation_time,
            invalidated_by_version_id=invalidated_by_version_id,
        )
    )

    return result.rowcount > 0


async def trace_knowledge_evolution(
    session: "AsyncSession",
    entity_id: int,
    include_invalidated: bool = True,
) -> dict:
    """
    Trace how understanding of an entity changed over time.

    Returns a timeline of memory versions that mention this entity,
    including bi-temporal information (when true, when learned, invalidations).

    Args:
        session: Database session
        entity_id: Entity ID to trace evolution for
        include_invalidated: Whether to include invalidated versions (default True)

    Returns:
        Dict with:
        - found: Whether entity was found
        - entity: Entity details (name, type)
        - timeline: List of version entries with temporal info
        - current_beliefs: Versions still valid (not invalidated)
        - invalidation_chain: Which versions invalidated which

    Each timeline entry includes:
        - memory_id: ID of the memory
        - version_id: ID of this version
        - version_number: Sequential version within memory
        - content_preview: First 200 chars of content
        - valid_from: When this became true in reality
        - valid_to: When this was superseded (NULL if current)
        - transaction_time: When we learned this (changed_at)
        - is_current: Whether this is the current belief
        - invalidated_by_version_id: What version invalidated this
    """
    from sqlalchemy import select

    from ..models import ExtractedEntity, MemoryEntityRef, MemoryVersion

    # First, get the entity
    entity_result = await session.execute(
        select(ExtractedEntity).where(ExtractedEntity.id == entity_id)
    )
    entity = entity_result.scalar_one_or_none()

    if not entity:
        return {
            "found": False,
            "error": f"Entity {entity_id} not found",
            "entity": None,
            "timeline": [],
            "current_beliefs": [],
            "invalidation_chain": [],
        }

    # Get all memory IDs that reference this entity
    refs_result = await session.execute(
        select(MemoryEntityRef.memory_id)
        .where(MemoryEntityRef.entity_id == entity_id)
        .distinct()
    )
    memory_ids = [row[0] for row in refs_result.fetchall()]

    if not memory_ids:
        return {
            "found": True,
            "entity": {
                "id": entity.id,
                "name": entity.name,
                "type": entity.entity_type,
            },
            "timeline": [],
            "current_beliefs": [],
            "invalidation_chain": [],
            "message": "No memories reference this entity",
        }

    # Get all versions for these memories
    query = select(MemoryVersion).where(MemoryVersion.memory_id.in_(memory_ids))

    if not include_invalidated:
        # Only include versions that are still valid (valid_to is NULL)
        query = query.where(MemoryVersion.valid_to.is_(None))

    # Order by valid_from (when true), then transaction_time (when learned)
    query = query.order_by(
        MemoryVersion.valid_from.asc().nullsfirst(), MemoryVersion.changed_at.asc()
    )

    versions_result = await session.execute(query)
    versions = versions_result.scalars().all()

    # Build timeline and track invalidations
    timeline = []
    current_beliefs = []
    invalidation_chain = []

    for v in versions:
        content_preview = v.content
        if len(content_preview) > 200:
            content_preview = content_preview[:200] + "..."

        is_current = v.valid_to is None

        entry = {
            "memory_id": v.memory_id,
            "version_id": v.id,
            "version_number": v.version_number,
            "content_preview": content_preview,
            "valid_from": v.valid_from.isoformat() if v.valid_from else None,
            "valid_to": v.valid_to.isoformat() if v.valid_to else None,
            "transaction_time": v.changed_at.isoformat() if v.changed_at else None,
            "is_current": is_current,
            "invalidated_by_version_id": v.invalidated_by_version_id,
            "change_type": v.change_type,
            "outcome": v.outcome,
            "worked": v.worked,
        }

        timeline.append(entry)

        if is_current:
            current_beliefs.append(entry)

        if v.invalidated_by_version_id is not None:
            invalidation_chain.append(
                {
                    "invalidated_version_id": v.id,
                    "invalidated_by_version_id": v.invalidated_by_version_id,
                    "invalidation_time": v.valid_to.isoformat() if v.valid_to else None,
                }
            )

    return {
        "found": True,
        "entity": {
            "id": entity.id,
            "name": entity.name,
            "type": entity.entity_type,
        },
        "timeline": timeline,
        "current_beliefs": current_beliefs,
        "invalidation_chain": invalidation_chain,
        "total_versions": len(timeline),
        "invalidated_count": len(invalidation_chain),
    }
