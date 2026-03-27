"""Dream strategies for autonomous re-evaluation of past decisions.

DreamStrategy defines the ABC for pluggable dream processing strategies.
FailedDecisionReview is the concrete strategy that queries worked=False
decisions, re-evaluates them against current evidence from memory, and
produces structured DreamResult insights with full provenance.

ConnectionDiscovery finds memories sharing extracted entities but lacking
explicit relationship edges and creates ``related_to`` links.

CommunityRefresh detects stale memory communities and rebuilds them via
the Leiden algorithm.

Design: Lightweight query-compare-insight approach (not full Reflexion).
Each decision is evaluated independently with its own database session
to avoid holding transactions across yield points.
"""

import asyncio
import itertools
import logging
from abc import ABC, abstractmethod
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any

from sqlalchemy import func, select

if TYPE_CHECKING:
    from ..context_manager import ProjectContext
    from .scheduler import IdleDreamScheduler

try:
    from ..config import settings
    from ..models import Memory, MemoryCommunity, MemoryEntityRef, MemoryRelationship
except ImportError:
    from daem0nmcp.config import settings
    from daem0nmcp.models import (
        Memory,
        MemoryCommunity,
        MemoryEntityRef,
        MemoryRelationship,
    )

from .persistence import DreamResult, DreamSession, persist_dream_result


class DreamStrategy(ABC):
    """Base class for dream processing strategies.

    Subclasses implement execute() which populates the session with
    DreamResult entries. Strategies must check scheduler.user_active
    at yield points and break immediately if the user returns.
    """

    @property
    def name(self) -> str:
        """Human-readable strategy name (defaults to class name)."""
        return self.__class__.__name__

    @abstractmethod
    async def execute(
        self,
        session: DreamSession,
        ctx: "ProjectContext",
        scheduler: "IdleDreamScheduler",
    ) -> DreamSession:
        """Execute the strategy, populating session.results.

        Args:
            session: The DreamSession to populate with results.
            ctx: ProjectContext with db_manager and memory_manager.
            scheduler: IdleDreamScheduler for cooperative yield checks.

        Returns:
            The updated DreamSession.
        """
        ...


class FailedDecisionReview(DreamStrategy):
    """Re-evaluates worked=False decisions against current evidence.

    For each failed decision:
    1. Query memory for current evidence related to the decision
    2. Compare: does newer evidence suggest revision, or confirm failure?
    3. Produce a DreamResult with structured insight and provenance
    4. Persist insights (except needs_more_data) as learning memories

    Cooperative yielding: checks scheduler.user_active.is_set() before
    each decision and breaks immediately if the user returns.
    """

    def __init__(
        self,
        max_decisions: int | None = None,
        min_age_hours: int | None = None,
        review_cooldown_hours: int | None = None,
    ):
        self._max_decisions = max_decisions or settings.dream_max_decisions_per_session
        self._min_age_hours = min_age_hours or settings.dream_min_decision_age_hours
        self._review_cooldown_hours = (
            review_cooldown_hours
            if review_cooldown_hours is not None
            else settings.dream_review_cooldown_hours
        )
        self._logger = logging.getLogger(__name__)

    async def execute(
        self,
        session: DreamSession,
        ctx: "ProjectContext",
        scheduler: "IdleDreamScheduler",
    ) -> DreamSession:
        """Execute the FailedDecisionReview strategy.

        Fetches failed decisions, re-evaluates each against current
        evidence, and persists actionable insights.
        """
        session.strategies_run.append(self.name)
        self._logger.info(
            "Dream session %s: Starting FailedDecisionReview", session.session_id
        )

        decisions = await self._get_failed_decisions(ctx)

        if not decisions:
            self._logger.info("No failed decisions to review")
            return session

        for decision in decisions:
            # YIELD CHECKPOINT: break immediately if user returns
            if scheduler.user_active.is_set():
                session.interrupted = True
                break

            session.decisions_reviewed += 1

            result = await self._re_evaluate_decision(decision, ctx)
            session.results.append(result)

            if result.result_type != "needs_more_data":
                await persist_dream_result(ctx.memory_manager, result, session)
                session.insights_generated += 1

            # COOPERATIVE YIELD: give event loop a tick
            await asyncio.sleep(0)

        return session

    async def _get_failed_decisions(self, ctx: "ProjectContext") -> list[Any]:
        """Query worked=False decisions from the database.

        Uses its own database session (not held open across the
        decision processing loop) to avoid transaction timeouts.

        Filters:
        - worked == False
        - archived == False
        - created_at < age_cutoff (skip too-recent decisions)
        - Not reviewed within the cooldown window
        - Ordered by most recent first, limited to max_decisions
        """
        try:
            age_cutoff = datetime.now(timezone.utc) - timedelta(
                hours=self._min_age_hours
            )
            async with ctx.db_manager.get_session() as db_session:
                result = await db_session.execute(
                    select(Memory)
                    .where(Memory.worked == False)  # noqa: E712
                    .where(Memory.archived == False)  # noqa: E712
                    .where(Memory.created_at < age_cutoff)
                    .order_by(Memory.created_at.desc())
                    .limit(self._max_decisions)
                )
                decisions = result.scalars().all()

                if not decisions or self._review_cooldown_hours <= 0:
                    return decisions

                # Find recently-reviewed decision IDs within cooldown window
                recently_reviewed_ids = await self._get_recently_reviewed_ids(
                    db_session
                )

                original_count = len(decisions)
                decisions = [d for d in decisions if d.id not in recently_reviewed_ids]
                skipped = original_count - len(decisions)
                if skipped > 0:
                    self._logger.info(
                        "Skipped %d decisions reviewed within %dh cooldown",
                        skipped,
                        self._review_cooldown_hours,
                    )

                return decisions
        except Exception as e:
            self._logger.warning("Failed to query failed decisions: %s", e)
            return []

    async def _get_recently_reviewed_ids(self, db_session) -> set:
        """Get IDs of decisions that were reviewed within the cooldown window.

        Queries learning memories with dream+re-evaluation tags created
        within the cooldown period, extracts source-decision:{id} tags
        to build a set of recently-reviewed decision IDs.
        """
        cooldown_cutoff = datetime.now(timezone.utc) - timedelta(
            hours=self._review_cooldown_hours
        )
        result = await db_session.execute(
            select(Memory)
            .where(Memory.category == "learning")
            .where(Memory.created_at >= cooldown_cutoff)
        )
        review_memories = result.scalars().all()

        reviewed_ids: set = set()
        for mem in review_memories:
            tags = mem.tags or []
            has_dream = "dream" in tags
            has_reeval = "re-evaluation" in tags
            if not (has_dream and has_reeval):
                continue
            for tag in tags:
                if tag.startswith("source-decision:"):
                    try:
                        decision_id = int(tag.split(":", 1)[1])
                        reviewed_ids.add(decision_id)
                    except (ValueError, IndexError):
                        pass
        return reviewed_ids

    async def _re_evaluate_decision(
        self, decision: Any, ctx: "ProjectContext"
    ) -> DreamResult:
        """Re-evaluate a single failed decision against current evidence.

        Steps:
        1. Use the decision content as a search query
        2. Recall current evidence via memory_manager.recall()
        3. Analyze evidence to determine result_type
        4. Build structured DreamResult with provenance

        Returns:
            DreamResult with result_type in {revised, confirmed_failure,
            needs_more_data} and an insight string summarizing findings.
        """
        try:
            # Use first 200 chars of decision as search query
            query = decision.content[:200]

            # Gather current evidence from memory
            evidence = await ctx.memory_manager.recall(
                query, limit=5, project_path=ctx.project_path
            )

            # Extract evidence summaries across all categories
            evidence_items: list[dict[str, Any]] = []
            evidence_ids: list[int] = []

            for category_key in ("decisions", "patterns", "learnings", "warnings"):
                for mem in evidence.get(category_key, []):
                    # Skip the decision itself
                    if mem.get("id") == decision.id:
                        continue
                    evidence_items.append(mem)
                    evidence_ids.append(mem["id"])
                    if len(evidence_items) >= 5:
                        break
                if len(evidence_items) >= 5:
                    break

            # Determine result_type based on evidence
            has_worked_evidence = any(
                mem.get("worked") is True for mem in evidence_items
            )

            if has_worked_evidence:
                result_type = "revised"
                evidence_summary = (
                    f"Found {len(evidence_items)} related memories, including "
                    f"successful approaches that suggest the original failure "
                    f"may be addressable with current knowledge."
                )
            elif len(evidence_items) < 2:
                result_type = "needs_more_data"
                evidence_summary = (
                    f"Only {len(evidence_items)} related memories found "
                    f"(excluding the decision itself). Insufficient evidence "
                    f"for re-evaluation."
                )
            else:
                result_type = "confirmed_failure"
                evidence_summary = (
                    f"Found {len(evidence_items)} related memories. Available "
                    f"evidence still supports the original failure assessment."
                )

            # Build insight text
            content_preview = decision.content[:80]
            insight = (
                f"Re-evaluated decision #{decision.id}: "
                f"'{content_preview}...' -- {result_type}. {evidence_summary}"
            )

            return DreamResult(
                source_decision_id=decision.id,
                original_content=decision.content[:200],
                original_outcome=decision.outcome,
                insight=insight,
                result_type=result_type,
                evidence_ids=evidence_ids,
            )

        except Exception as e:
            self._logger.warning("Error re-evaluating decision #%d: %s", decision.id, e)
            return DreamResult(
                source_decision_id=decision.id,
                original_content=getattr(decision, "content", "")[:200],
                original_outcome=getattr(decision, "outcome", None),
                insight=f"Error during re-evaluation of decision #{decision.id}: {e}",
                result_type="needs_more_data",
                evidence_ids=[],
            )


class ConnectionDiscovery(DreamStrategy):
    """Discover and create ``related_to`` edges between memories that share entities.

    Scans ``MemoryEntityRef`` rows within a lookback window, groups by entity,
    finds memory pairs sharing at least ``min_shared_entities`` entities, then
    creates ``related_to`` relationship edges for pairs that are not yet linked.
    """

    def __init__(
        self,
        min_shared_entities: int = 2,
        lookback_hours: int = 168,
        max_connections: int = 20,
        confidence: float = 0.7,
    ):
        self._min_shared_entities = min_shared_entities
        self._lookback_hours = lookback_hours
        self._max_connections = max_connections
        self._confidence = confidence
        self._logger = logging.getLogger(__name__)

    async def execute(
        self,
        session: DreamSession,
        ctx: "ProjectContext",
        scheduler: "IdleDreamScheduler",
    ) -> DreamSession:
        session.strategies_run.append(self.name)
        self._logger.info(
            "Dream session %s: Starting ConnectionDiscovery", session.session_id
        )

        try:
            unlinked_pairs = await self._find_unlinked_pairs(ctx)
        except Exception as e:
            self._logger.warning("ConnectionDiscovery query failed: %s", e)
            return session

        if not unlinked_pairs:
            self._logger.info("No unlinked memory pairs found")
            return session

        created = 0
        for source_id, target_id, shared_names in unlinked_pairs:
            if scheduler.user_active.is_set():
                session.interrupted = True
                break
            if created >= self._max_connections:
                break

            result = await ctx.memory_manager.link_memories(
                source_id=source_id,
                target_id=target_id,
                relationship="related_to",
                description=f"Shared entities: {', '.join(sorted(shared_names))}",
                confidence=self._confidence,
            )

            if result.get("status") != "already_exists" and "error" not in result:
                created += 1
                session.insights_generated += 1

            await asyncio.sleep(0)

        if created > 0:
            ctx.memory_manager.invalidate_graph_cache()
            self._logger.info("ConnectionDiscovery created %d edges", created)

        return session

    async def _find_unlinked_pairs(
        self, ctx: "ProjectContext"
    ) -> list[tuple[int, int, set[str]]]:
        """Find memory pairs sharing entities but lacking relationship edges.

        Returns:
            List of (source_id, target_id, shared_entity_names) tuples.
        """
        cutoff = datetime.now(timezone.utc) - timedelta(hours=self._lookback_hours)

        async with ctx.db_manager.get_session() as db_session:
            # Fetch entity refs within the lookback window joined to Memory
            rows = await db_session.execute(
                select(
                    MemoryEntityRef.entity_id,
                    MemoryEntityRef.memory_id,
                )
                .join(Memory, Memory.id == MemoryEntityRef.memory_id)
                .where(Memory.created_at >= cutoff)
            )
            refs = rows.all()

            if not refs:
                return []

            # Also load entity names for descriptions
            entity_ids = {r[0] for r in refs}
            try:
                from daem0nmcp.models import ExtractedEntity
            except ImportError:
                from ..models import ExtractedEntity
            name_rows = await db_session.execute(
                select(ExtractedEntity.id, ExtractedEntity.name).where(
                    ExtractedEntity.id.in_(entity_ids)
                )
            )
            entity_names: dict[int, str] = {r[0]: r[1] for r in name_rows.all()}

            # Group memory_ids by entity_id
            entity_to_memories: dict[int, set[int]] = defaultdict(set)
            for entity_id, memory_id in refs:
                entity_to_memories[entity_id].add(memory_id)

            # Find pairs sharing >= min_shared_entities
            pair_shared: dict[tuple[int, int], set[str]] = defaultdict(set)
            for entity_id, memory_ids in entity_to_memories.items():
                if len(memory_ids) < 2:
                    continue
                ename = entity_names.get(entity_id, f"entity-{entity_id}")
                for a, b in itertools.combinations(sorted(memory_ids), 2):
                    pair_shared[(a, b)].add(ename)

            candidates = [
                (a, b, names)
                for (a, b), names in pair_shared.items()
                if len(names) >= self._min_shared_entities
            ]

            if not candidates:
                return []

            # Batch-check existing relationships
            all_ids = set()
            for a, b, _ in candidates:
                all_ids.add(a)
                all_ids.add(b)

            existing_rows = await db_session.execute(
                select(
                    MemoryRelationship.source_id,
                    MemoryRelationship.target_id,
                ).where(
                    MemoryRelationship.source_id.in_(all_ids),
                    MemoryRelationship.target_id.in_(all_ids),
                )
            )
            existing_edges: set[tuple[int, int]] = set()
            for src, tgt in existing_rows.all():
                existing_edges.add((src, tgt))
                existing_edges.add((tgt, src))

            return [
                (a, b, names)
                for a, b, names in candidates
                if (a, b) not in existing_edges
            ]


class CommunityRefresh(DreamStrategy):
    """Rebuild memory communities when the graph has grown stale.

    Checks how many new memories have been created since the last community
    detection run. If the count exceeds ``staleness_threshold``, triggers a
    full Leiden-based community rebuild.
    """

    def __init__(self, staleness_threshold: int = 10):
        self._staleness_threshold = staleness_threshold
        self._logger = logging.getLogger(__name__)

    async def execute(
        self,
        session: DreamSession,
        ctx: "ProjectContext",
        scheduler: "IdleDreamScheduler",
    ) -> DreamSession:
        session.strategies_run.append(self.name)
        self._logger.info(
            "Dream session %s: Starting CommunityRefresh", session.session_id
        )

        # Early return if leidenalg is not installed
        try:
            import leidenalg  # noqa: F401
        except ImportError:
            self._logger.warning("leidenalg not installed -- skipping CommunityRefresh")
            return session

        try:
            is_stale = await self._check_staleness(ctx)
        except Exception as e:
            self._logger.warning("CommunityRefresh staleness check failed: %s", e)
            return session

        if not is_stale:
            self._logger.info("Communities are fresh -- skipping rebuild")
            return session

        # YIELD CHECKPOINT before expensive rebuild
        if scheduler.user_active.is_set():
            session.interrupted = True
            return session

        try:
            from daem0nmcp.communities import CommunityManager
        except ImportError:
            from ..communities import CommunityManager

        try:
            cm = CommunityManager(ctx.db_manager)
            kg = await ctx.memory_manager.get_knowledge_graph()
            communities = await cm.detect_communities_from_graph(
                project_path=ctx.project_path,
                knowledge_graph=kg,
            )
            await cm.save_communities(
                project_path=ctx.project_path,
                communities=communities,
            )
            session.insights_generated += 1
            ctx.memory_manager.invalidate_graph_cache()
            self._logger.info(
                "CommunityRefresh rebuilt %d communities", len(communities)
            )
        except Exception as e:
            self._logger.warning("CommunityRefresh rebuild failed: %s", e)

        return session

    async def _check_staleness(self, ctx: "ProjectContext") -> bool:
        """Return True if communities are stale (enough new memories since last build)."""
        async with ctx.db_manager.get_session() as db_session:
            # Get the most recent community creation timestamp
            result = await db_session.execute(
                select(func.max(MemoryCommunity.created_at))
            )
            last_community_at = result.scalar()

            if last_community_at is None:
                # No communities exist yet -- check if there are enough memories
                count_result = await db_session.execute(select(func.count(Memory.id)))
                total = count_result.scalar() or 0
                return total >= self._staleness_threshold

            # Count memories created since the last community build
            count_result = await db_session.execute(
                select(func.count(Memory.id)).where(
                    Memory.created_at > last_community_at
                )
            )
            new_count = count_result.scalar() or 0
            return new_count >= self._staleness_threshold


class PendingOutcomeResolver(DreamStrategy):
    """Resolve pending decisions (outcome IS NULL, worked IS NULL) using stored evidence.

    For each pending decision:
    1. Recall related memories as evidence
    2. Classify evidence as positive (worked=True) or negative (worked=False/warning)
    3. Apply decision tree:
       - Total evidence < 2 → insufficient_evidence (skip)
       - Directional evidence < threshold → insufficient_evidence (skip)
       - Mixed (positive > 0 AND negative > 0) → flagged_for_review
       - Unanimous positive >= threshold → auto_resolved_success
       - Unanimous negative >= threshold → auto_resolved_failure
    4. If not dry_run: call record_outcome() on the original decision
    5. Persist actionable results (not insufficient_evidence)

    Safety: Ships with dry_run=True. Must opt-in via DAEM0NMCP_DREAM_PENDING_DRY_RUN=false.
    """

    def __init__(
        self,
        max_decisions: int | None = None,
        min_age_hours: int | None = None,
        cooldown_hours: int | None = None,
        evidence_threshold: int | None = None,
        dry_run: bool | None = None,
    ):
        self._max_decisions = (
            max_decisions
            if max_decisions is not None
            else settings.dream_pending_max_per_session
        )
        self._min_age_hours = (
            min_age_hours
            if min_age_hours is not None
            else settings.dream_pending_min_age_hours
        )
        self._cooldown_hours = (
            cooldown_hours
            if cooldown_hours is not None
            else settings.dream_pending_cooldown_hours
        )
        self._evidence_threshold = (
            evidence_threshold
            if evidence_threshold is not None
            else settings.dream_pending_evidence_threshold
        )
        self._dry_run = (
            dry_run if dry_run is not None else settings.dream_pending_dry_run
        )
        self._logger = logging.getLogger(__name__)

    async def execute(
        self,
        session: DreamSession,
        ctx: "ProjectContext",
        scheduler: "IdleDreamScheduler",
    ) -> DreamSession:
        """Execute the PendingOutcomeResolver strategy."""
        session.strategies_run.append(self.name)
        self._logger.info(
            "Dream session %s: Starting PendingOutcomeResolver (dry_run=%s)",
            session.session_id,
            self._dry_run,
        )

        decisions = await self._get_pending_decisions(ctx)

        if not decisions:
            self._logger.info("No pending decisions to resolve")
            return session

        for decision in decisions:
            # YIELD CHECKPOINT: break immediately if user returns
            if scheduler.user_active.is_set():
                session.interrupted = True
                break

            session.decisions_reviewed += 1

            result = await self._evaluate_decision(decision, ctx)
            session.results.append(result)

            if result.result_type in ("auto_resolved_success", "auto_resolved_failure"):
                if self._dry_run:
                    # Downgrade to flagged_for_review in dry-run mode
                    result = DreamResult(
                        source_decision_id=result.source_decision_id,
                        original_content=result.original_content,
                        original_outcome=result.original_outcome,
                        insight=f"[DRY RUN] {result.insight}",
                        result_type="flagged_for_review",
                        evidence_ids=result.evidence_ids,
                    )
                    # Replace the last result
                    session.results[-1] = result
                else:
                    # Actually resolve the outcome
                    worked = result.result_type == "auto_resolved_success"
                    try:
                        await ctx.memory_manager.record_outcome(
                            memory_id=decision.id,
                            outcome=f"[DREAM AUTO-RESOLVED] {result.insight}",
                            worked=worked,
                            project_path=ctx.project_path,
                        )
                        session.outcomes_resolved += 1
                    except Exception as e:
                        self._logger.warning(
                            "Failed to record outcome for decision #%d: %s",
                            decision.id,
                            e,
                        )

            # Persist actionable results (not insufficient_evidence)
            if result.result_type != "insufficient_evidence":
                await persist_dream_result(ctx.memory_manager, result, session)
                session.insights_generated += 1

            # COOPERATIVE YIELD: give event loop a tick
            await asyncio.sleep(0)

        return session

    async def _get_pending_decisions(self, ctx: "ProjectContext") -> list[Any]:
        """Query pending decisions (outcome IS NULL, worked IS NULL).

        Filters:
        - category == 'decision'
        - outcome IS NULL AND worked IS NULL
        - archived == False
        - created_at < age_cutoff
        - Not reviewed within the cooldown window (via pending-resolution tag)
        - Ordered by created_at ASC (oldest first)
        - Limited to max_decisions
        """
        try:
            age_cutoff = datetime.now(timezone.utc) - timedelta(
                hours=self._min_age_hours
            )
            async with ctx.db_manager.get_session() as db_session:
                result = await db_session.execute(
                    select(Memory)
                    .where(Memory.category == "decision")
                    .where(Memory.outcome.is_(None))
                    .where(Memory.worked.is_(None))
                    .where(Memory.archived == False)  # noqa: E712
                    .where(Memory.created_at < age_cutoff)
                    .order_by(Memory.created_at.asc())
                    .limit(self._max_decisions)
                )
                decisions = result.scalars().all()

                if not decisions or self._cooldown_hours <= 0:
                    return decisions

                # Filter out recently reviewed decisions
                recently_reviewed_ids = await self._get_recently_reviewed_ids(
                    db_session
                )

                original_count = len(decisions)
                decisions = [d for d in decisions if d.id not in recently_reviewed_ids]
                skipped = original_count - len(decisions)
                if skipped > 0:
                    self._logger.info(
                        "Skipped %d pending decisions reviewed within %dh cooldown",
                        skipped,
                        self._cooldown_hours,
                    )

                return decisions
        except Exception as e:
            self._logger.warning("Failed to query pending decisions: %s", e)
            return []

    async def _get_recently_reviewed_ids(self, db_session) -> set:
        """Get IDs of pending decisions reviewed within the cooldown window.

        Queries learning memories with dream+pending-resolution tags created
        within the cooldown period, extracts source-decision:{id} tags.
        """
        cooldown_cutoff = datetime.now(timezone.utc) - timedelta(
            hours=self._cooldown_hours
        )
        result = await db_session.execute(
            select(Memory)
            .where(Memory.category == "learning")
            .where(Memory.created_at >= cooldown_cutoff)
        )
        review_memories = result.scalars().all()

        reviewed_ids: set = set()
        for mem in review_memories:
            tags = mem.tags or []
            has_dream = "dream" in tags
            has_pending = "pending-resolution" in tags
            if not (has_dream and has_pending):
                continue
            for tag in tags:
                if tag.startswith("source-decision:"):
                    try:
                        decision_id = int(tag.split(":", 1)[1])
                        reviewed_ids.add(decision_id)
                    except (ValueError, IndexError):
                        pass
        return reviewed_ids

    async def _evaluate_decision(
        self, decision: Any, ctx: "ProjectContext"
    ) -> DreamResult:
        """Evaluate a pending decision using evidence from memory.

        Evidence algorithm:
        1. recall(decision.content[:200], limit=10)
        2. Exclude self-references (matching decision.id)
        3. Classify: worked=True → positive, worked=False OR category=warning → negative
        4. Decision tree based on counts and threshold
        """
        try:
            query = decision.content[:200]

            evidence = await ctx.memory_manager.recall(
                query, limit=10, project_path=ctx.project_path
            )

            # Gather evidence items across all categories
            evidence_items: list[dict[str, Any]] = []
            evidence_ids: list[int] = []

            for category_key in ("decisions", "patterns", "learnings", "warnings"):
                for mem in evidence.get(category_key, []):
                    # Exclude self-references
                    if mem.get("id") == decision.id:
                        continue
                    evidence_items.append(mem)
                    evidence_ids.append(mem["id"])

            # Classify evidence
            positive_count = sum(
                1 for mem in evidence_items if mem.get("worked") is True
            )
            negative_count = sum(
                1
                for mem in evidence_items
                if mem.get("worked") is False or mem.get("category") == "warning"
            )

            total_evidence = len(evidence_items)
            directional_evidence = positive_count + negative_count

            # Decision tree
            content_preview = decision.content[:80]
            if total_evidence < 2:
                result_type = "insufficient_evidence"
                insight = (
                    f"Pending decision #{decision.id}: "
                    f"'{content_preview}...' -- insufficient evidence "
                    f"({total_evidence} total, need at least 2)"
                )
            elif directional_evidence < self._evidence_threshold:
                result_type = "insufficient_evidence"
                insight = (
                    f"Pending decision #{decision.id}: "
                    f"'{content_preview}...' -- insufficient directional evidence "
                    f"({directional_evidence} directional, need {self._evidence_threshold})"
                )
            elif positive_count > 0 and negative_count > 0:
                result_type = "flagged_for_review"
                insight = (
                    f"Pending decision #{decision.id}: "
                    f"'{content_preview}...' -- mixed evidence "
                    f"({positive_count} positive, {negative_count} negative). "
                    f"Requires human review."
                )
            elif positive_count >= self._evidence_threshold and negative_count == 0:
                result_type = "auto_resolved_success"
                insight = (
                    f"Pending decision #{decision.id}: "
                    f"'{content_preview}...' -- unanimous positive evidence "
                    f"({positive_count} positive, 0 negative). Auto-resolved as success."
                )
            elif negative_count >= self._evidence_threshold and positive_count == 0:
                result_type = "auto_resolved_failure"
                insight = (
                    f"Pending decision #{decision.id}: "
                    f"'{content_preview}...' -- unanimous negative evidence "
                    f"({negative_count} negative, 0 positive). Auto-resolved as failure."
                )
            else:
                result_type = "insufficient_evidence"
                insight = (
                    f"Pending decision #{decision.id}: "
                    f"'{content_preview}...' -- below threshold "
                    f"({positive_count} positive, {negative_count} negative)"
                )

            return DreamResult(
                source_decision_id=decision.id,
                original_content=decision.content[:200],
                original_outcome=decision.outcome,
                insight=insight,
                result_type=result_type,
                evidence_ids=evidence_ids,
            )

        except Exception as e:
            self._logger.warning(
                "Error evaluating pending decision #%d: %s", decision.id, e
            )
            return DreamResult(
                source_decision_id=decision.id,
                original_content=getattr(decision, "content", "")[:200],
                original_outcome=getattr(decision, "outcome", None),
                insight=f"Error during evaluation of pending decision #{decision.id}: {e}",
                result_type="insufficient_evidence",
                evidence_ids=[],
            )
