"""
Context Trigger Manager - Auto-recall based on patterns.

Manages triggers that automatically recall memories when certain
patterns match the current context (file paths, tags, entities).
"""

import logging
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import delete, select

from .database import DatabaseManager
from .models import ContextTrigger
from .trigger_security import (
    MAX_ACTIVE_TRIGGERS,
    SafeUserPattern,
    TriggerPatternError,
    bounded_glob_match,
    validate_active_trigger_count,
    validate_file_path,
    validate_glob_pattern,
)

logger = logging.getLogger(__name__)

# Valid trigger types
VALID_TRIGGER_TYPES = frozenset({"file_pattern", "tag_match", "entity_match"})
_SAFE_USER_PATTERNS = SafeUserPattern()


class ContextTriggerManager:
    """
    Manages context triggers for auto-recall functionality.

    Triggers can be:
    - file_pattern: Bounded glob matching for file paths
    - tag_match: Regex pattern matching memory tags
    - entity_match: Regex pattern matching entity names

    When a trigger matches, it returns the recall topic and optional
    category filters for memory retrieval.
    """

    def __init__(
        self,
        db_manager: DatabaseManager,
        pattern_matcher: SafeUserPattern | None = None,
    ):
        self.db = db_manager
        self._pattern_matcher = pattern_matcher or _SAFE_USER_PATTERNS

    async def add_trigger(
        self,
        project_path: str,
        trigger_type: str,
        pattern: str,
        recall_topic: str,
        recall_categories: list[str] | None = None,
        priority: int = 0,
    ) -> dict[str, Any]:
        """
        Create a new context trigger.

        Args:
            project_path: Project this trigger belongs to
            trigger_type: One of: file_pattern, tag_match, entity_match
            pattern: The pattern to match (glob for files, regex for tags/entities)
            recall_topic: Topic to recall when this trigger matches
            recall_categories: Optional list of categories to filter recall
            priority: Higher priority triggers are evaluated first (default: 0)

        Returns:
            Status dict with trigger_id
        """
        # Validate trigger type
        if trigger_type not in VALID_TRIGGER_TYPES:
            return {
                "error": f"Invalid trigger_type '{trigger_type}'. "
                f"Valid types: {', '.join(sorted(VALID_TRIGGER_TYPES))}"
            }

        try:
            if trigger_type in ("tag_match", "entity_match"):
                self._pattern_matcher.validate(pattern)
            else:
                validate_glob_pattern(pattern)
        except TriggerPatternError as error:
            return {"error": error.as_dict()}

        async with self.db.get_session() as session:
            trigger = ContextTrigger(
                project_path=project_path,
                trigger_type=trigger_type,
                pattern=pattern,
                recall_topic=recall_topic,
                recall_categories=recall_categories or [],
                priority=priority,
                is_active=True,
                trigger_count=0,
            )
            session.add(trigger)
            await session.flush()

            logger.info(
                f"Created {trigger_type} trigger: '{pattern}' -> '{recall_topic}' "
                f"(id={trigger.id})"
            )

            return {
                "status": "created",
                "trigger_id": trigger.id,
                "trigger_type": trigger_type,
                "pattern": pattern,
                "recall_topic": recall_topic,
            }

    async def remove_trigger(
        self, trigger_id: int, project_path: str
    ) -> dict[str, Any]:
        """
        Remove a trigger.

        Args:
            trigger_id: ID of the trigger to remove
            project_path: Project path (for authorization)

        Returns:
            Status dict
        """
        async with self.db.get_session() as session:
            result = await session.execute(
                delete(ContextTrigger).where(
                    ContextTrigger.id == trigger_id,
                    ContextTrigger.project_path == project_path,
                )
            )

            if result.rowcount == 0:
                return {"status": "not_found", "trigger_id": trigger_id}

            logger.info(f"Removed trigger {trigger_id}")

            return {"status": "removed", "trigger_id": trigger_id}

    async def list_triggers(
        self, project_path: str, active_only: bool = True
    ) -> list[dict[str, Any]]:
        """
        List all triggers for a project.

        Args:
            project_path: Project to list triggers for
            active_only: If True, only return active triggers (default: True)

        Returns:
            List of trigger dicts
        """
        return await self._query_triggers(
            project_path=project_path,
            active_only=active_only,
            limit=None,
        )

    async def _list_active_triggers_for_evaluation(
        self, project_path: str, *, limit: int
    ) -> list[dict[str, Any]]:
        """Fetch only enough active rows to detect evaluation overflow."""
        return await self._query_triggers(
            project_path=project_path,
            active_only=True,
            limit=limit,
        )

    async def _query_triggers(
        self,
        *,
        project_path: str,
        active_only: bool,
        limit: int | None,
    ) -> list[dict[str, Any]]:
        async with self.db.get_session() as session:
            query = select(ContextTrigger).where(
                ContextTrigger.project_path == project_path
            )

            if active_only:
                query = query.where(ContextTrigger.is_active == True)  # noqa: E712

            query = query.order_by(ContextTrigger.priority.desc())
            if limit is not None:
                query = query.limit(limit)

            result = await session.execute(query)
            triggers = result.scalars().all()

            return [
                {
                    "id": t.id,
                    "trigger_type": t.trigger_type,
                    "pattern": t.pattern,
                    "recall_topic": t.recall_topic,
                    "recall_categories": t.recall_categories or [],
                    "priority": t.priority,
                    "is_active": t.is_active,
                    "trigger_count": t.trigger_count,
                    "last_triggered": t.last_triggered.isoformat()
                    if t.last_triggered
                    else None,
                    "created_at": t.created_at.isoformat() if t.created_at else None,
                }
                for t in triggers
            ]

    def _matches_file_pattern(self, pattern: str, file_path: str) -> bool:
        """
        Check if a file path matches a glob pattern.

        Supports ** for recursive directory matching.
        """
        return bounded_glob_match(pattern, file_path).matched

    async def check_triggers(
        self,
        project_path: str,
        file_path: str | None = None,
        tags: list[str] | None = None,
        entities: list[str] | None = None,
    ) -> list[dict[str, Any]] | dict[str, Any]:
        """
        Check which triggers match the given context.

        Args:
            project_path: Project to check triggers for
            file_path: Optional file path to match against file_pattern triggers
            tags: Optional tags to match against tag_match triggers
            entities: Optional entity names to match against entity_match triggers

        Returns:
            List of matching triggers with recall info, sorted by priority
        """
        try:
            if file_path is not None:
                validate_file_path(file_path)
            if tags is not None:
                self._pattern_matcher.validate_candidates(tags, field="tags")
            if entities is not None:
                self._pattern_matcher.validate_candidates(entities, field="entities")
            self._pattern_matcher.validate_candidate_total(
                (len(tags) if tags is not None else 0)
                + (len(entities) if entities is not None else 0)
            )
        except TriggerPatternError as error:
            return {"triggers": [], "error": error.as_dict()}

        triggers = await self._list_active_triggers_for_evaluation(
            project_path,
            limit=MAX_ACTIVE_TRIGGERS + 1,
        )
        try:
            validate_active_trigger_count(len(triggers))
        except TriggerPatternError as error:
            return {"triggers": [], "error": error.as_dict()}

        matches: list[dict[str, Any]] = []
        matched_ids: list[int] = []

        try:
            for trigger in triggers:
                matched = False

                if (
                    trigger["trigger_type"] == "file_pattern"
                    and file_path is not None
                    and self._matches_file_pattern(trigger["pattern"], file_path)
                ):
                    matched = True

                elif trigger["trigger_type"] == "tag_match" and tags is not None:
                    matched = (
                        await self._pattern_matcher.matches_async(
                            trigger["id"], trigger["pattern"], tags, field="tags"
                        )
                    ).matched

                elif trigger["trigger_type"] == "entity_match" and entities is not None:
                    matched = (
                        await self._pattern_matcher.matches_async(
                            trigger["id"],
                            trigger["pattern"],
                            entities,
                            field="entities",
                        )
                    ).matched

                if matched:
                    matches.append(
                        {
                            "trigger_id": trigger["id"],
                            "trigger_type": trigger["trigger_type"],
                            "pattern": trigger["pattern"],
                            "recall_topic": trigger["recall_topic"],
                            "recall_categories": trigger["recall_categories"],
                            "priority": trigger["priority"],
                        }
                    )
                    matched_ids.append(trigger["id"])
        except TriggerPatternError as error:
            return {"triggers": [], "error": error.as_dict()}

        # Update trigger stats
        if matched_ids:
            await self._update_trigger_stats(matched_ids)

        return matches

    async def _update_trigger_stats(self, trigger_ids: list[int]) -> None:
        """Update trigger_count and last_triggered for matched triggers."""
        now = datetime.now(timezone.utc)

        async with self.db.get_session() as session:
            await session.execute(
                ContextTrigger.__table__.update()
                .where(ContextTrigger.id.in_(trigger_ids))
                .values(
                    trigger_count=ContextTrigger.trigger_count + 1, last_triggered=now
                )
            )

    async def get_triggered_context(
        self,
        project_path: str,
        file_path: str | None = None,
        tags: list[str] | None = None,
        entities: list[str] | None = None,
        limit: int = 5,
    ) -> dict[str, Any]:
        """
        Check triggers and recall memories for matching triggers.

        This is the full auto-recall flow:
        1. Check which triggers match the context
        2. For each matching trigger, recall relevant memories
        3. Return combined results

        Args:
            project_path: Project to check triggers for
            file_path: Optional file path for context
            tags: Optional tags for context
            entities: Optional entity names for context
            limit: Max memories per trigger topic

        Returns:
            Dict with triggers and their associated memories
        """
        # Check which triggers match
        matches = await self.check_triggers(
            project_path=project_path, file_path=file_path, tags=tags, entities=entities
        )

        if isinstance(matches, dict):
            return {
                "triggers": [],
                "memories": {},
                "total_triggers": 0,
                "error": matches["error"],
                "message": "Trigger evaluation was rejected.",
            }

        if not matches:
            return {
                "triggers": [],
                "memories": {},
                "total_triggers": 0,
                "message": "No triggers matched the current context",
            }

        from .memory import MemoryManager

        # Get memory manager for recall
        memory_mgr = MemoryManager(self.db)

        # Recall memories for each trigger
        memories_by_topic: dict[str, dict[str, Any]] = {}

        for match in matches:
            topic = match["recall_topic"]

            # Skip if already recalled this topic
            if topic in memories_by_topic:
                continue

            # Recall memories for this topic
            recall_result = await memory_mgr.recall(
                topic=topic,
                categories=match["recall_categories"]
                if match["recall_categories"]
                else None,
                limit=limit,
                project_path=project_path,
            )

            memories_by_topic[topic] = recall_result

        return {
            "triggers": matches,
            "memories": memories_by_topic,
            "total_triggers": len(matches),
            "topics_recalled": list(memories_by_topic.keys()),
            "message": f"Matched {len(matches)} trigger(s), recalled {len(memories_by_topic)} topic(s)",
        }
