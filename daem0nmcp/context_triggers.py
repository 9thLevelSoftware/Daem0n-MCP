"""
Context Trigger Manager - Auto-recall based on patterns.

Manages triggers that automatically recall memories when certain
patterns match the current context (file paths, tags, entities).
"""

import fnmatch
import logging
import re
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import delete, select

from .database import DatabaseManager
from .models import ContextTrigger

logger = logging.getLogger(__name__)

# Valid trigger types
VALID_TRIGGER_TYPES = frozenset({"file_pattern", "tag_match", "entity_match"})


class ContextTriggerManager:
    """
    Manages context triggers for auto-recall functionality.

    Triggers can be:
    - file_pattern: Glob pattern matching file paths (uses fnmatch)
    - tag_match: Regex pattern matching memory tags
    - entity_match: Regex pattern matching entity names

    When a trigger matches, it returns the recall topic and optional
    category filters for memory retrieval.
    """

    def __init__(self, db_manager: DatabaseManager):
        self.db = db_manager

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

        # Validate pattern
        if not pattern or not pattern.strip():
            return {"error": "Pattern cannot be empty"}

        # Validate regex patterns compile
        if trigger_type in ("tag_match", "entity_match"):
            try:
                re.compile(pattern)
            except re.error as e:
                return {"error": f"Invalid regex pattern: {e}"}

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
        async with self.db.get_session() as session:
            query = select(ContextTrigger).where(
                ContextTrigger.project_path == project_path
            )

            if active_only:
                query = query.where(ContextTrigger.is_active == True)  # noqa: E712

            query = query.order_by(ContextTrigger.priority.desc())

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
        # Normalize path separators
        file_path = file_path.replace("\\", "/")
        pattern = pattern.replace("\\", "/")

        # Use fnmatch for glob matching
        # For ** patterns, we need special handling
        if "**" in pattern:
            # Split pattern and path by /
            pattern_parts = pattern.split("/")
            path_parts = file_path.split("/")

            return self._match_glob_recursive(pattern_parts, path_parts)

        return fnmatch.fnmatch(file_path, pattern)

    def _match_glob_recursive(
        self, pattern_parts: list[str], path_parts: list[str]
    ) -> bool:
        """
        Recursively match glob pattern with ** support.
        """
        if not pattern_parts:
            return not path_parts

        if not path_parts:
            # Pattern has more parts but path is exhausted
            # Only match if remaining pattern is all **
            return all(p == "**" for p in pattern_parts)

        first_pattern = pattern_parts[0]

        if first_pattern == "**":
            # ** matches zero or more directories
            # Try matching rest of pattern at current position
            if self._match_glob_recursive(pattern_parts[1:], path_parts):
                return True
            # Try matching after consuming one path part
            return self._match_glob_recursive(pattern_parts, path_parts[1:])

        # Normal fnmatch for this part
        if fnmatch.fnmatch(path_parts[0], first_pattern):
            return self._match_glob_recursive(pattern_parts[1:], path_parts[1:])

        return False

    def _matches_regex(self, pattern: str, values: list[str]) -> bool:
        """
        Check if any value matches the regex pattern.
        """
        try:
            compiled = re.compile(pattern)
            return any(compiled.search(v) for v in values)
        except re.error:
            return False

    async def check_triggers(
        self,
        project_path: str,
        file_path: str | None = None,
        tags: list[str] | None = None,
        entities: list[str] | None = None,
    ) -> list[dict[str, Any]]:
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
        triggers = await self.list_triggers(project_path, active_only=True)

        matches = []
        matched_ids = []

        for trigger in triggers:
            matched = False

            if trigger["trigger_type"] == "file_pattern" and file_path:
                if self._matches_file_pattern(trigger["pattern"], file_path):
                    matched = True

            elif trigger["trigger_type"] == "tag_match" and tags:
                if self._matches_regex(trigger["pattern"], tags):
                    matched = True

            elif trigger["trigger_type"] == "entity_match" and entities:
                if self._matches_regex(trigger["pattern"], entities):
                    matched = True

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
        from .memory import MemoryManager

        # Check which triggers match
        matches = await self.check_triggers(
            project_path=project_path, file_path=file_path, tags=tags, entities=entities
        )

        if not matches:
            return {
                "triggers": [],
                "memories": {},
                "total_triggers": 0,
                "message": "No triggers matched the current context",
            }

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
