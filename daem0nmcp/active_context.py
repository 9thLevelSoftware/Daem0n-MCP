"""
Active Context Manager - MemGPT-style working memory for Daem0n.

Manages a small set of "always-hot" memories that are auto-injected
into tool responses and briefings.
"""

import logging
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import delete, func, select

from .database import DatabaseManager
from .models import ActiveContextItem, Memory

logger = logging.getLogger(__name__)

# Maximum items in active context per project
MAX_ACTIVE_CONTEXT_ITEMS = 10


class ActiveContextManager:
    """
    Manages the active working context for a project.

    Active context items are:
    - Auto-included in get_briefing() responses
    - Available for injection into other tool responses
    - Limited to MAX_ACTIVE_CONTEXT_ITEMS to prevent bloat
    """

    def __init__(self, db_manager: DatabaseManager):
        self.db = db_manager

    async def add_to_context(
        self,
        project_path: str,
        memory_id: int,
        reason: str | None = None,
        priority: int = 0,
        expires_at: datetime | None = None,
    ) -> dict[str, Any]:
        """
        Add a memory to the active working context.

        Args:
            project_path: Project this context belongs to
            memory_id: Memory to add to active context
            reason: Why this memory should be in active context
            priority: Higher priority = shown first (default: 0)
            expires_at: Optional auto-expiry timestamp

        Returns:
            Status dict with added item info
        """
        async with self.db.get_session() as session:
            # Verify memory exists
            memory = await session.get(Memory, memory_id)
            if not memory:
                return {"error": f"Memory {memory_id} not found"}

            # Check if already in context
            existing = await session.execute(
                select(ActiveContextItem).where(
                    ActiveContextItem.project_path == project_path,
                    ActiveContextItem.memory_id == memory_id,
                )
            )
            if existing.scalar_one_or_none():
                return {
                    "status": "already_exists",
                    "memory_id": memory_id,
                    "message": "Memory is already in active context",
                }

            # Check count limit
            count_result = await session.execute(
                select(func.count(ActiveContextItem.id)).where(
                    ActiveContextItem.project_path == project_path
                )
            )
            current_count = count_result.scalar() or 0

            if current_count >= MAX_ACTIVE_CONTEXT_ITEMS:
                return {
                    "error": "CONTEXT_FULL",
                    "message": f"Active context is full ({MAX_ACTIVE_CONTEXT_ITEMS} items). Remove an item first.",
                    "current_count": current_count,
                }

            # Add to context
            item = ActiveContextItem(
                project_path=project_path,
                memory_id=memory_id,
                priority=priority,
                reason=reason,
                expires_at=expires_at,
            )
            session.add(item)
            await session.flush()

            logger.info(
                f"Added memory {memory_id} to active context for {project_path}"
            )

            return {
                "status": "added",
                "id": item.id,
                "memory_id": memory_id,
                "priority": priority,
                "reason": reason,
                "current_count": current_count + 1,
                "max_count": MAX_ACTIVE_CONTEXT_ITEMS,
            }

    async def remove_from_context(
        self, project_path: str, memory_id: int
    ) -> dict[str, Any]:
        """Remove a memory from active context."""
        async with self.db.get_session() as session:
            result = await session.execute(
                delete(ActiveContextItem).where(
                    ActiveContextItem.project_path == project_path,
                    ActiveContextItem.memory_id == memory_id,
                )
            )

            if result.rowcount == 0:
                return {"status": "not_found", "memory_id": memory_id}

            return {"status": "removed", "memory_id": memory_id}

    async def get_active_context(
        self, project_path: str, include_expired: bool = False, condensed: bool = False
    ) -> dict[str, Any]:
        """
        Get all items in the active working context.

        Args:
            project_path: Project to get context for
            include_expired: Include expired items (default: False)
            condensed: Return truncated content for token efficiency (default: False)

        Returns memories with full or condensed content, ordered by priority.
        """
        async with self.db.get_session() as session:
            query = (
                select(ActiveContextItem, Memory)
                .join(Memory, ActiveContextItem.memory_id == Memory.id)
                .where(ActiveContextItem.project_path == project_path)
                .order_by(ActiveContextItem.priority.desc())
            )

            if not include_expired:
                now = datetime.now(timezone.utc)
                query = query.where(
                    (ActiveContextItem.expires_at.is_(None))
                    | (ActiveContextItem.expires_at > now)
                )

            result = await session.execute(query)
            rows = result.all()

            items = []
            for ctx_item, memory in rows:
                # Build memory dict - truncate content in condensed mode
                if condensed:
                    # Truncate content to ~150 chars for token efficiency
                    content = (
                        memory.content[:150] + "..."
                        if len(memory.content) > 150
                        else memory.content
                    )
                    memory_dict = {
                        "category": memory.category,
                        "content": content,
                        "worked": memory.worked,
                    }
                else:
                    memory_dict = {
                        "category": memory.category,
                        "content": memory.content,
                        "rationale": memory.rationale,
                        "tags": memory.tags,
                        "outcome": memory.outcome,
                        "worked": memory.worked,
                    }

                items.append(
                    {
                        "context_id": ctx_item.id,
                        "memory_id": memory.id,
                        "priority": ctx_item.priority,
                        "reason": ctx_item.reason,
                        "added_at": ctx_item.added_at.isoformat()
                        if ctx_item.added_at
                        else None,
                        "expires_at": ctx_item.expires_at.isoformat()
                        if ctx_item.expires_at
                        else None,
                        "memory": memory_dict,
                    }
                )

            return {
                "project_path": project_path,
                "count": len(items),
                "max_count": MAX_ACTIVE_CONTEXT_ITEMS,
                "items": items,
            }

    async def clear_context(self, project_path: str) -> dict[str, Any]:
        """Clear all items from active context."""
        async with self.db.get_session() as session:
            result = await session.execute(
                delete(ActiveContextItem).where(
                    ActiveContextItem.project_path == project_path
                )
            )

            return {"status": "cleared", "removed_count": result.rowcount}

    async def cleanup_expired(self, project_path: str) -> dict[str, Any]:
        """Remove expired items from active context."""
        now = datetime.now(timezone.utc)

        async with self.db.get_session() as session:
            result = await session.execute(
                delete(ActiveContextItem).where(
                    ActiveContextItem.project_path == project_path,
                    ActiveContextItem.expires_at.isnot(None),
                    ActiveContextItem.expires_at <= now,
                )
            )

            return {"status": "cleaned", "expired_count": result.rowcount}
