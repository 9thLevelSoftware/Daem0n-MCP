"""
Entity Manager - Database operations for extracted entities.

Handles:
- Storing extracted entities
- Creating memory-entity relationships
- Querying entities and related memories
"""

import logging
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import or_, select

from .database import DatabaseManager
from .entity_extractor import EntityExtractor
from .graph.entity_resolver import EntityResolver
from .models import ExtractedEntity, Memory, MemoryEntityRef

logger = logging.getLogger(__name__)


class EntityManager:
    """
    Manages extracted entities and their relationships to memories.
    """

    def __init__(self, db_manager: DatabaseManager):
        self.db = db_manager
        self.extractor = EntityExtractor()
        self.resolver = EntityResolver(db_manager)

    async def process_memory(
        self,
        memory_id: int,
        content: str,
        project_path: str,
        rationale: str | None = None,
    ) -> dict[str, Any]:
        """
        Extract entities from a memory and create relationships.

        Args:
            memory_id: Memory to process
            content: Memory content to extract from
            project_path: Project this belongs to
            rationale: Optional rationale to also extract from

        Returns:
            Summary of extraction results
        """
        # Combine content and rationale for extraction
        text = content
        if rationale:
            text += " " + rationale

        # Ensure resolver cache is loaded for this project
        await self.resolver.ensure_cache_loaded(project_path)

        # Extract entities
        extracted = self.extractor.extract_all(text)

        if not extracted:
            return {"memory_id": memory_id, "entities_found": 0, "refs_created": 0}

        refs_created = 0

        async with self.db.get_session() as session:
            for entity_data in extracted:
                # Get or create entity
                entity = await self._get_or_create_entity(
                    session,
                    project_path=project_path,
                    entity_type=entity_data["type"],
                    name=entity_data["name"],
                )

                # Create reference if not exists
                existing_ref = await session.execute(
                    select(MemoryEntityRef).where(
                        MemoryEntityRef.memory_id == memory_id,
                        MemoryEntityRef.entity_id == entity.id,
                    )
                )
                if not existing_ref.scalar_one_or_none():
                    ref = MemoryEntityRef(
                        memory_id=memory_id,
                        entity_id=entity.id,
                        relationship="mentions",
                        context_snippet=entity_data.get("context"),
                    )
                    session.add(ref)
                    refs_created += 1

        return {
            "memory_id": memory_id,
            "entities_found": len(extracted),
            "refs_created": refs_created,
        }

    async def _get_or_create_entity(
        self, session, project_path: str, entity_type: str, name: str
    ) -> ExtractedEntity:
        """Get existing entity or create new one using resolver."""
        entity_id, is_new = await self.resolver.resolve(
            name=name,
            entity_type=entity_type,
            project_path=project_path,
            session=session,
        )

        # Get the entity object
        entity = await session.get(ExtractedEntity, entity_id)

        if not is_new:
            # Increment mention count for existing entity
            entity.mention_count += 1
            entity.updated_at = datetime.now(timezone.utc)

        return entity

    async def get_entities_for_memory(self, memory_id: int) -> list[dict[str, Any]]:
        """Get all entities referenced by a memory."""
        async with self.db.get_session() as session:
            result = await session.execute(
                select(ExtractedEntity, MemoryEntityRef)
                .join(MemoryEntityRef, ExtractedEntity.id == MemoryEntityRef.entity_id)
                .where(MemoryEntityRef.memory_id == memory_id)
            )
            rows = result.all()

            return [
                {
                    "entity_id": entity.id,
                    "type": entity.entity_type,
                    "name": entity.name,
                    "qualified_name": entity.qualified_name,
                    "mention_count": entity.mention_count,
                    "relationship": ref.relationship,
                    "context_snippet": ref.context_snippet,
                }
                for entity, ref in rows
            ]

    async def get_memories_for_entity(
        self, entity_name: str, project_path: str, entity_type: str | None = None
    ) -> dict[str, Any]:
        """
        Get all memories that reference a specific entity.

        This enables queries like "show everything related to UserAuth".
        """
        async with self.db.get_session() as session:
            # Find the entity
            query = select(ExtractedEntity).where(
                ExtractedEntity.project_path == project_path,
                or_(
                    ExtractedEntity.name == entity_name,
                    ExtractedEntity.qualified_name == entity_name,
                ),
            )
            if entity_type:
                query = query.where(ExtractedEntity.entity_type == entity_type)

            result = await session.execute(query)
            entities = result.scalars().all()

            if not entities:
                return {"entity_name": entity_name, "found": False, "memories": []}

            # Get all memory IDs that reference these entities
            entity_ids = [e.id for e in entities]
            refs_result = await session.execute(
                select(MemoryEntityRef).where(MemoryEntityRef.entity_id.in_(entity_ids))
            )
            refs = refs_result.scalars().all()
            memory_ids = list(set(r.memory_id for r in refs))

            # Get full memory content
            if not memory_ids:
                return {
                    "entity_name": entity_name,
                    "found": True,
                    "entity_types": [e.entity_type for e in entities],
                    "mention_count": sum(e.mention_count for e in entities),
                    "memories": [],
                }

            memories_result = await session.execute(
                select(Memory).where(Memory.id.in_(memory_ids))
            )
            memories = memories_result.scalars().all()

            return {
                "entity_name": entity_name,
                "found": True,
                "entity_types": [e.entity_type for e in entities],
                "mention_count": sum(e.mention_count for e in entities),
                "memories": [
                    {
                        "id": m.id,
                        "category": m.category,
                        "content": m.content,
                        "rationale": m.rationale,
                        "tags": m.tags,
                        "outcome": m.outcome,
                        "worked": m.worked,
                        "created_at": m.created_at.isoformat()
                        if m.created_at
                        else None,
                    }
                    for m in memories
                ],
            }

    async def get_popular_entities(
        self, project_path: str, entity_type: str | None = None, limit: int = 20
    ) -> list[dict[str, Any]]:
        """Get most frequently mentioned entities."""
        async with self.db.get_session() as session:
            query = (
                select(ExtractedEntity)
                .where(ExtractedEntity.project_path == project_path)
                .order_by(ExtractedEntity.mention_count.desc())
                .limit(limit)
            )

            if entity_type:
                query = query.where(ExtractedEntity.entity_type == entity_type)

            result = await session.execute(query)
            entities = result.scalars().all()

            return [
                {
                    "id": e.id,
                    "type": e.entity_type,
                    "name": e.name,
                    "qualified_name": e.qualified_name,
                    "mention_count": e.mention_count,
                }
                for e in entities
            ]
