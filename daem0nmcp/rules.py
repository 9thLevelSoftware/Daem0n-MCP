"""
Rules Engine - Decision trees and enforcement for AI agents.

This module handles:
- Storing rules (decision tree nodes)
- Semantic matching of actions against rules using TF-IDF
- Providing guidance based on matching rules
- Learning from rule effectiveness
"""

import logging
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import desc, select

from .cache import get_rules_cache, make_cache_key
from .database import DatabaseManager
from .models import Rule
from .similarity import TFIDFIndex, extract_keywords

logger = logging.getLogger(__name__)


class RulesEngine:
    """
    Manages rules - the decision tree nodes that guide AI behavior.

    Uses TF-IDF similarity for better matching than naive keyword overlap.

    A rule has:
    - trigger: What activates it (e.g., "adding new API endpoint")
    - must_do: Things that must be done
    - must_not: Things to avoid
    - ask_first: Questions to consider
    - warnings: Warnings from past experience
    """

    def __init__(self, db_manager: DatabaseManager):
        self.db = db_manager
        self._index: TFIDFIndex | None = None
        self._index_loaded = False
        self._index_built_at: datetime | None = None

    async def _ensure_index(self) -> TFIDFIndex:
        """Ensure the TF-IDF index is loaded with all rules."""
        if self._index is None:
            self._index = TFIDFIndex()

        if not self._index_loaded:
            async with self.db.get_session() as session:
                result = await session.execute(
                    select(Rule).where(Rule.enabled == True)  # noqa: E712
                )
                rules = result.scalars().all()

                for rule in rules:
                    # Index the trigger text
                    self._index.add_document(rule.id, rule.trigger)

                self._index_loaded = True
                self._index_built_at = datetime.now(timezone.utc)
                logger.info(f"Loaded {len(rules)} rules into TF-IDF index")

        return self._index

    async def _check_index_freshness(self) -> bool:
        """
        Check if index needs rebuilding due to external DB changes.
        Returns True if index was rebuilt.
        """
        if not self._index_loaded:
            return False

        if await self.db.has_changes_since(self._index_built_at):
            logger.info("Database changed since index was built, rebuilding...")
            self._index_loaded = False
            self._index = None
            await self._ensure_index()
            return True

        return False

    def _invalidate_index(self) -> None:
        """Invalidate the index and cache when rules change."""
        self._index_loaded = False
        if self._index:
            self._index = None
        # Also clear the rules cache since rules changed
        get_rules_cache().clear()

    async def add_rule(
        self,
        trigger: str,
        must_do: list[str] | None = None,
        must_not: list[str] | None = None,
        ask_first: list[str] | None = None,
        warnings: list[str] | None = None,
        priority: int = 0,
    ) -> dict[str, Any]:
        """
        Add a new rule to the decision tree.

        Args:
            trigger: What activates this rule (natural language description)
            must_do: List of things that must be done
            must_not: List of things to avoid
            ask_first: Questions to consider before proceeding
            warnings: Warnings from past experience
            priority: Higher priority rules are shown first

        Returns:
            The created rule as a dict
        """
        # Extract keywords for backward compat
        trigger_keywords = extract_keywords(trigger)

        rule = Rule(
            trigger=trigger,
            trigger_keywords=trigger_keywords,
            must_do=must_do or [],
            must_not=must_not or [],
            ask_first=ask_first or [],
            warnings=warnings or [],
            priority=priority,
            enabled=True,
        )

        async with self.db.get_session() as session:
            session.add(rule)
            await session.flush()
            rule_id = rule.id

            # Update index
            index = await self._ensure_index()
            index.add_document(rule_id, trigger)

            # Clear cache since rules changed
            get_rules_cache().clear()

            logger.info(f"Added rule: {trigger[:50]}...")

            return {
                "id": rule_id,
                "trigger": trigger,
                "must_do": must_do or [],
                "must_not": must_not or [],
                "ask_first": ask_first or [],
                "warnings": warnings or [],
                "priority": priority,
                "created_at": rule.created_at.isoformat(),
            }

    async def check_rules(
        self,
        action: str,
        context: dict[str, Any] | None = None,
        threshold: float = 0.15,
    ) -> dict[str, Any]:
        """
        Check if an action triggers any rules and return guidance.

        Uses TF-IDF semantic matching for better rule activation.
        Results are cached for 5 seconds to avoid repeated searches.

        Args:
            action: Description of what the AI is about to do
            context: Optional context (files involved, etc.)
            threshold: Minimum similarity score to match (default: 0.15)

        Returns:
            Matching rules with combined guidance
        """
        # Check cache first (context is excluded from key as it's just metadata)
        cache = get_rules_cache()
        cache_key = make_cache_key(action, threshold)
        found, cached_result = cache.get(cache_key)
        if found:
            logger.debug(f"check_rules cache hit for action: {action[:50]}...")
            return cached_result

        await self._check_index_freshness()
        index = await self._ensure_index()

        # Search for matching rules using TF-IDF
        matches = index.search(action, top_k=10, threshold=threshold)

        if not matches:
            return {
                "action": action,
                "matched_rules": 0,
                "guidance": None,
                "message": "No rules match this action - proceed with caution",
            }

        # Get full rule objects
        rule_ids = [rule_id for rule_id, _ in matches]
        score_map = {rule_id: score for rule_id, score in matches}

        async with self.db.get_session() as session:
            result = await session.execute(
                select(Rule).where(Rule.id.in_(rule_ids)).where(Rule.enabled == True)  # noqa: E712
            )
            rules = {r.id: r for r in result.scalars().all()}

        # Sort by priority then score
        sorted_matches = sorted(
            [
                (rule_id, score_map[rule_id], rules[rule_id])
                for rule_id, _ in matches
                if rule_id in rules
            ],
            key=lambda x: (x[2].priority, x[1]),
            reverse=True,
        )

        if not sorted_matches:
            return {
                "action": action,
                "matched_rules": 0,
                "guidance": None,
                "message": "No active rules match this action",
            }

        # Combine guidance from matching rules
        combined = {"must_do": [], "must_not": [], "ask_first": [], "warnings": []}

        matched_details = []
        for rule_id, score, rule in sorted_matches:
            combined["must_do"].extend(rule.must_do)
            combined["must_not"].extend(rule.must_not)
            combined["ask_first"].extend(rule.ask_first)
            combined["warnings"].extend(rule.warnings)

            matched_details.append(
                {
                    "id": rule.id,
                    "trigger": rule.trigger,
                    "match_score": round(score, 3),
                    "priority": rule.priority,
                }
            )

        # Deduplicate while preserving order
        combined["must_do"] = list(dict.fromkeys(combined["must_do"]))
        combined["must_not"] = list(dict.fromkeys(combined["must_not"]))
        combined["ask_first"] = list(dict.fromkeys(combined["ask_first"]))
        combined["warnings"] = list(dict.fromkeys(combined["warnings"]))

        # Determine severity
        has_blockers = len(combined["must_not"]) > 0 or len(combined["warnings"]) > 0

        # Build actionable message
        if has_blockers:
            message = "⚠️ STOP: Review warnings and must_not items before proceeding"
        elif combined["ask_first"]:
            message = "❓ Consider these questions before proceeding"
        elif combined["must_do"]:
            message = "✓ Rules matched - follow the must_do checklist"
        else:
            message = "Rules matched but no specific guidance"

        result = {
            "action": action,
            "matched_rules": len(sorted_matches),
            "rules": matched_details,
            "guidance": combined,
            "has_blockers": has_blockers,
            "message": message,
        }

        # Cache the result
        cache.set(cache_key, result)

        return result

    async def list_rules(
        self, enabled_only: bool = True, limit: int = 50
    ) -> list[dict[str, Any]]:
        """List all rules."""
        async with self.db.get_session() as session:
            query = select(Rule).order_by(desc(Rule.priority), desc(Rule.created_at))

            if enabled_only:
                query = query.where(Rule.enabled == True)  # noqa: E712

            query = query.limit(limit)

            result = await session.execute(query)
            rules = result.scalars().all()

            return [
                {
                    "id": r.id,
                    "trigger": r.trigger,
                    "must_do": r.must_do,
                    "must_not": r.must_not,
                    "ask_first": r.ask_first,
                    "warnings": r.warnings,
                    "priority": r.priority,
                    "enabled": r.enabled,
                    "created_at": r.created_at.isoformat(),
                }
                for r in rules
            ]

    async def update_rule(
        self,
        rule_id: int,
        must_do: list[str] | None = None,
        must_not: list[str] | None = None,
        ask_first: list[str] | None = None,
        warnings: list[str] | None = None,
        priority: int | None = None,
        enabled: bool | None = None,
    ) -> dict[str, Any]:
        """Update an existing rule."""
        async with self.db.get_session() as session:
            result = await session.execute(select(Rule).where(Rule.id == rule_id))
            rule = result.scalar_one_or_none()

            if not rule:
                return {"error": f"Rule {rule_id} not found"}

            if must_do is not None:
                rule.must_do = must_do
            if must_not is not None:
                rule.must_not = must_not
            if ask_first is not None:
                rule.ask_first = ask_first
            if warnings is not None:
                rule.warnings = warnings
            if priority is not None:
                rule.priority = priority
            if enabled is not None:
                rule.enabled = enabled
                # Invalidate index if enabled status changed
                self._invalidate_index()

            return {"id": rule.id, "trigger": rule.trigger, "updated": True}

    async def delete_rule(self, rule_id: int) -> dict[str, Any]:
        """Delete a rule."""
        async with self.db.get_session() as session:
            result = await session.execute(select(Rule).where(Rule.id == rule_id))
            rule = result.scalar_one_or_none()

            if not rule:
                return {"error": f"Rule {rule_id} not found"}

            trigger = rule.trigger
            await session.delete(rule)

            # Invalidate index
            self._invalidate_index()

            return {"deleted": True, "trigger": trigger}

    async def add_warning_to_rule(self, rule_id: int, warning: str) -> dict[str, Any]:
        """Add a warning to an existing rule (from learned experience)."""
        async with self.db.get_session() as session:
            result = await session.execute(select(Rule).where(Rule.id == rule_id))
            rule = result.scalar_one_or_none()

            if not rule:
                return {"error": f"Rule {rule_id} not found"}

            if warning not in rule.warnings:
                rule.warnings = rule.warnings + [warning]

            return {"id": rule.id, "trigger": rule.trigger, "warnings": rule.warnings}

    async def find_similar_rules(
        self, trigger: str, limit: int = 5
    ) -> list[dict[str, Any]]:
        """
        Find rules similar to a given trigger.

        Useful for avoiding duplicate rules.
        """
        index = await self._ensure_index()
        matches = index.search(trigger, top_k=limit, threshold=0.2)

        if not matches:
            return []

        rule_ids = [rule_id for rule_id, _ in matches]
        score_map = {rule_id: score for rule_id, score in matches}

        async with self.db.get_session() as session:
            result = await session.execute(select(Rule).where(Rule.id.in_(rule_ids)))
            rules = result.scalars().all()

            return [
                {
                    "id": r.id,
                    "trigger": r.trigger,
                    "similarity": round(score_map[r.id], 3),
                    "must_do_count": len(r.must_do),
                    "warnings_count": len(r.warnings),
                }
                for r in rules
            ]

    async def rebuild_index(self) -> dict[str, Any]:
        """Force rebuild of TF-IDF index for rules."""
        self._index = TFIDFIndex()
        self._index_loaded = False

        async with self.db.get_session() as session:
            result = await session.execute(
                select(Rule).where(Rule.enabled == True)  # noqa: E712
            )
            rules = result.scalars().all()

            for rule in rules:
                self._index.add_document(rule.id, rule.trigger)

        self._index_loaded = True
        self._index_built_at = datetime.now(timezone.utc)

        return {
            "rules_indexed": len(rules),
            "built_at": self._index_built_at.isoformat(),
        }
