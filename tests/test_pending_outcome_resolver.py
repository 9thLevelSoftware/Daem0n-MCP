"""Tests for PendingOutcomeResolver dream strategy.

Covers: query filtering, evidence classification, decision tree logic,
dry-run behavior, record_outcome calls, session tracking, yield behavior,
graceful error degradation, persistence calls, and ordering.
"""

import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

from daem0nmcp.dreaming.persistence import DreamSession
from daem0nmcp.dreaming.strategies import PendingOutcomeResolver

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_memory(
    id: int,
    content: str = "some pending decision",
    outcome=None,
    worked=None,
    archived: bool = False,
    created_at: datetime = None,
    category: str = "decision",
    tags=None,
):
    """Create a mock Memory object for pending decisions."""
    mem = MagicMock()
    mem.id = id
    mem.content = content
    mem.outcome = outcome
    mem.worked = worked
    mem.archived = archived
    mem.created_at = created_at or (datetime.now(timezone.utc) - timedelta(hours=48))
    mem.category = category
    mem.context = {}
    mem.tags = tags or []
    return mem


def _make_recall_result(memories=None):
    """Build a recall() return dict with categorised memories."""
    if memories is None:
        memories = []
    return {
        "topic": "query",
        "found": len(memories),
        "total_count": len(memories),
        "offset": 0,
        "limit": 10,
        "has_more": False,
        "summary": None,
        "decisions": [
            m for m in memories if m.get("category", "decision") == "decision"
        ],
        "patterns": [m for m in memories if m.get("category") == "pattern"],
        "learnings": [m for m in memories if m.get("category") == "learning"],
        "warnings": [m for m in memories if m.get("category") == "warning"],
    }


def _make_scheduler(user_active_set: bool = False):
    """Create a mock scheduler with a real asyncio.Event."""
    scheduler = MagicMock()
    event = asyncio.Event()
    if user_active_set:
        event.set()
    else:
        event.clear()
    scheduler.user_active = event
    return scheduler


def _make_ctx(memories_from_db=None, recall_result=None, review_memories=None):
    """Create a mock ProjectContext for PendingOutcomeResolver tests.

    Args:
        memories_from_db: Pending decisions returned by the first DB query.
        recall_result: Result returned by memory_manager.recall().
        review_memories: Learning memories returned by the second DB query
            (used by cooldown guard).
    """
    ctx = MagicMock()
    ctx.project_path = "/test/project"

    mock_session = AsyncMock()

    if review_memories is not None:
        mock_result_decisions = MagicMock()
        mock_result_decisions.scalars.return_value.all.return_value = (
            memories_from_db or []
        )
        mock_result_reviews = MagicMock()
        mock_result_reviews.scalars.return_value.all.return_value = review_memories
        mock_session.execute = AsyncMock(
            side_effect=[mock_result_decisions, mock_result_reviews]
        )
    else:
        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = memories_from_db or []
        mock_session.execute = AsyncMock(return_value=mock_result)

    async_cm = AsyncMock()
    async_cm.__aenter__ = AsyncMock(return_value=mock_session)
    async_cm.__aexit__ = AsyncMock(return_value=False)
    ctx.db_manager.get_session = MagicMock(return_value=async_cm)

    ctx.memory_manager.recall = AsyncMock(
        return_value=recall_result or _make_recall_result()
    )
    ctx.memory_manager.remember = AsyncMock(return_value={"id": 999})
    ctx.memory_manager.record_outcome = AsyncMock(return_value={"status": "updated"})

    return ctx, mock_session


def _make_session():
    """Create a fresh DreamSession for testing."""
    return DreamSession(project_path="/test/project")


# ---------------------------------------------------------------------------
# Test Cases
# ---------------------------------------------------------------------------


class TestNoPendingDecisions:
    async def test_no_pending_decisions(self):
        """Empty DB -> 0 reviewed."""
        ctx, _ = _make_ctx(memories_from_db=[])
        scheduler = _make_scheduler(user_active_set=False)
        session = _make_session()

        strategy = PendingOutcomeResolver(
            max_decisions=3, min_age_hours=24, dry_run=False
        )
        result = await strategy.execute(session, ctx, scheduler)

        assert result.decisions_reviewed == 0
        assert result.results == []
        assert not result.interrupted
        assert "PendingOutcomeResolver" in result.strategies_run


class TestQueryFiltersByAge:
    async def test_query_filters_by_age(self):
        """Age cutoff applied -- DB query issued with age filter."""
        ctx, mock_session = _make_ctx(memories_from_db=[])
        scheduler = _make_scheduler(user_active_set=False)
        session = _make_session()

        strategy = PendingOutcomeResolver(
            max_decisions=3, min_age_hours=24, dry_run=False
        )
        await strategy.execute(session, ctx, scheduler)

        # Verify execute was called on the mock session (query was issued)
        mock_session.execute.assert_called_once()
        assert session.decisions_reviewed == 0


class TestCooldownSkipsRecent:
    async def test_cooldown_skips_recent(self):
        """pending-resolution tagged memories cause skip."""
        mem1 = _make_memory(id=10, content="decision A pending")
        mem2 = _make_memory(id=20, content="decision B pending")

        # Recent review memory for decision 10 (within cooldown)
        review_mem = _make_memory(
            id=100,
            content="Dream re-evaluation: ...",
            category="learning",
            created_at=datetime.now(timezone.utc) - timedelta(hours=1),
        )
        review_mem.tags = ["dream", "pending-resolution", "source-decision:10"]

        ctx, _ = _make_ctx(
            memories_from_db=[mem1, mem2],
            recall_result=_make_recall_result(
                [
                    {
                        "id": 200,
                        "content": "ev",
                        "worked": None,
                        "category": "decision",
                    },
                ]
            ),
            review_memories=[review_mem],
        )
        scheduler = _make_scheduler(user_active_set=False)
        session = _make_session()

        strategy = PendingOutcomeResolver(
            max_decisions=5, min_age_hours=1, cooldown_hours=168, dry_run=False
        )
        result = await strategy.execute(session, ctx, scheduler)

        # Decision 10 should be skipped, only decision 20 reviewed
        assert result.decisions_reviewed == 1
        assert result.results[0].source_decision_id == 20


class TestCooldownZeroDisables:
    async def test_cooldown_zero_disables(self):
        """cooldown=0 skips filtering."""
        mem1 = _make_memory(id=10, content="decision A pending")

        ctx, _ = _make_ctx(
            memories_from_db=[mem1],
            recall_result=_make_recall_result(
                [
                    {
                        "id": 200,
                        "content": "ev",
                        "worked": None,
                        "category": "decision",
                    },
                ]
            ),
        )
        scheduler = _make_scheduler(user_active_set=False)
        session = _make_session()

        strategy = PendingOutcomeResolver(
            max_decisions=5, min_age_hours=1, cooldown_hours=0, dry_run=False
        )
        result = await strategy.execute(session, ctx, scheduler)

        assert result.decisions_reviewed == 1


class TestInsufficientEvidenceSparse:
    async def test_insufficient_evidence_sparse(self):
        """< 2 items (after excluding self) -> insufficient_evidence, not persisted."""
        mem = _make_memory(id=10, content="pending decision X")

        # Only the decision itself returned -- after excluding self, 0 items
        recall_result = _make_recall_result(
            [
                {
                    "id": 10,
                    "content": "pending decision X",
                    "worked": None,
                    "category": "decision",
                },
            ]
        )

        ctx, _ = _make_ctx(memories_from_db=[mem], recall_result=recall_result)
        scheduler = _make_scheduler(user_active_set=False)
        session = _make_session()

        strategy = PendingOutcomeResolver(
            max_decisions=5, min_age_hours=1, dry_run=False
        )

        with patch(
            "daem0nmcp.dreaming.strategies.persist_dream_result",
            new_callable=AsyncMock,
        ) as mock_persist:
            result = await strategy.execute(session, ctx, scheduler)

            assert result.results[0].result_type == "insufficient_evidence"
            mock_persist.assert_not_called()


class TestInsufficientEvidenceBelowThreshold:
    async def test_insufficient_evidence_below_threshold(self):
        """Directional < threshold -> insufficient_evidence."""
        mem = _make_memory(id=10, content="pending decision Y")

        # 3 items but only 2 directional (below threshold of 3)
        recall_result = _make_recall_result(
            [
                {"id": 100, "content": "ev1", "worked": True, "category": "decision"},
                {"id": 101, "content": "ev2", "worked": True, "category": "learning"},
                {"id": 102, "content": "ev3", "worked": None, "category": "pattern"},
            ]
        )

        ctx, _ = _make_ctx(memories_from_db=[mem], recall_result=recall_result)
        scheduler = _make_scheduler(user_active_set=False)
        session = _make_session()

        strategy = PendingOutcomeResolver(
            max_decisions=5, min_age_hours=1, evidence_threshold=3, dry_run=False
        )
        result = await strategy.execute(session, ctx, scheduler)

        assert result.results[0].result_type == "insufficient_evidence"


class TestMixedEvidenceFlagged:
    async def test_mixed_evidence_flagged(self):
        """positive + negative -> flagged_for_review, persisted."""
        mem = _make_memory(id=10, content="pending decision Z")

        recall_result = _make_recall_result(
            [
                {"id": 100, "content": "ev1", "worked": True, "category": "decision"},
                {"id": 101, "content": "ev2", "worked": True, "category": "learning"},
                {"id": 102, "content": "ev3", "worked": True, "category": "pattern"},
                {"id": 103, "content": "warn", "worked": None, "category": "warning"},
            ]
        )

        ctx, _ = _make_ctx(memories_from_db=[mem], recall_result=recall_result)
        scheduler = _make_scheduler(user_active_set=False)
        session = _make_session()

        strategy = PendingOutcomeResolver(
            max_decisions=5, min_age_hours=1, evidence_threshold=3, dry_run=False
        )

        with patch(
            "daem0nmcp.dreaming.strategies.persist_dream_result",
            new_callable=AsyncMock,
        ) as mock_persist:
            result = await strategy.execute(session, ctx, scheduler)

            assert result.results[0].result_type == "flagged_for_review"
            mock_persist.assert_called_once()


class TestUnanimousPositive:
    async def test_unanimous_positive(self):
        """3+ worked=True, 0 negative -> auto_resolved_success."""
        mem = _make_memory(id=10, content="pending decision success")

        recall_result = _make_recall_result(
            [
                {"id": 100, "content": "ev1", "worked": True, "category": "decision"},
                {"id": 101, "content": "ev2", "worked": True, "category": "learning"},
                {"id": 102, "content": "ev3", "worked": True, "category": "pattern"},
            ]
        )

        ctx, _ = _make_ctx(memories_from_db=[mem], recall_result=recall_result)
        scheduler = _make_scheduler(user_active_set=False)
        session = _make_session()

        strategy = PendingOutcomeResolver(
            max_decisions=5, min_age_hours=1, evidence_threshold=3, dry_run=False
        )
        result = await strategy.execute(session, ctx, scheduler)

        assert result.results[0].result_type == "auto_resolved_success"


class TestUnanimousNegative:
    async def test_unanimous_negative(self):
        """3+ failed/warnings, 0 positive -> auto_resolved_failure."""
        mem = _make_memory(id=10, content="pending decision fail")

        recall_result = _make_recall_result(
            [
                {"id": 100, "content": "ev1", "worked": False, "category": "decision"},
                {"id": 101, "content": "ev2", "worked": False, "category": "learning"},
                {"id": 102, "content": "warn", "worked": None, "category": "warning"},
            ]
        )

        ctx, _ = _make_ctx(memories_from_db=[mem], recall_result=recall_result)
        scheduler = _make_scheduler(user_active_set=False)
        session = _make_session()

        strategy = PendingOutcomeResolver(
            max_decisions=5, min_age_hours=1, evidence_threshold=3, dry_run=False
        )
        result = await strategy.execute(session, ctx, scheduler)

        assert result.results[0].result_type == "auto_resolved_failure"


class TestAutoResolveCallsRecordOutcome:
    async def test_auto_resolve_calls_record_outcome(self):
        """record_outcome called with correct args on auto_resolved_success."""
        mem = _make_memory(id=42, content="pending decision to resolve")

        recall_result = _make_recall_result(
            [
                {"id": 100, "content": "ev1", "worked": True, "category": "decision"},
                {"id": 101, "content": "ev2", "worked": True, "category": "learning"},
                {"id": 102, "content": "ev3", "worked": True, "category": "pattern"},
            ]
        )

        ctx, _ = _make_ctx(memories_from_db=[mem], recall_result=recall_result)
        scheduler = _make_scheduler(user_active_set=False)
        session = _make_session()

        strategy = PendingOutcomeResolver(
            max_decisions=5, min_age_hours=1, evidence_threshold=3, dry_run=False
        )

        with patch(
            "daem0nmcp.dreaming.strategies.persist_dream_result",
            new_callable=AsyncMock,
        ):
            await strategy.execute(session, ctx, scheduler)

        ctx.memory_manager.record_outcome.assert_called_once()
        call_kwargs = ctx.memory_manager.record_outcome.call_args.kwargs
        assert call_kwargs["memory_id"] == 42
        assert call_kwargs["worked"] is True
        assert "[DREAM AUTO-RESOLVED]" in call_kwargs["outcome"]


class TestOutcomeTextPrefix:
    async def test_outcome_text_prefix(self):
        """Outcome starts with [DREAM AUTO-RESOLVED]."""
        mem = _make_memory(id=10, content="pending decision prefix test")

        recall_result = _make_recall_result(
            [
                {"id": 100, "content": "ev1", "worked": True, "category": "decision"},
                {"id": 101, "content": "ev2", "worked": True, "category": "learning"},
                {"id": 102, "content": "ev3", "worked": True, "category": "pattern"},
            ]
        )

        ctx, _ = _make_ctx(memories_from_db=[mem], recall_result=recall_result)
        scheduler = _make_scheduler(user_active_set=False)
        session = _make_session()

        strategy = PendingOutcomeResolver(
            max_decisions=5, min_age_hours=1, evidence_threshold=3, dry_run=False
        )

        with patch(
            "daem0nmcp.dreaming.strategies.persist_dream_result",
            new_callable=AsyncMock,
        ):
            await strategy.execute(session, ctx, scheduler)

        call_kwargs = ctx.memory_manager.record_outcome.call_args.kwargs
        assert call_kwargs["outcome"].startswith("[DREAM AUTO-RESOLVED]")


class TestDryRunDowngrades:
    async def test_dry_run_downgrades(self):
        """dry_run=True -> flagged_for_review with [DRY RUN] prefix."""
        mem = _make_memory(id=10, content="pending decision dry run")

        recall_result = _make_recall_result(
            [
                {"id": 100, "content": "ev1", "worked": True, "category": "decision"},
                {"id": 101, "content": "ev2", "worked": True, "category": "learning"},
                {"id": 102, "content": "ev3", "worked": True, "category": "pattern"},
            ]
        )

        ctx, _ = _make_ctx(memories_from_db=[mem], recall_result=recall_result)
        scheduler = _make_scheduler(user_active_set=False)
        session = _make_session()

        strategy = PendingOutcomeResolver(
            max_decisions=5, min_age_hours=1, evidence_threshold=3, dry_run=True
        )

        with patch(
            "daem0nmcp.dreaming.strategies.persist_dream_result",
            new_callable=AsyncMock,
        ):
            result = await strategy.execute(session, ctx, scheduler)

        assert result.results[0].result_type == "flagged_for_review"
        assert result.results[0].insight.startswith("[DRY RUN]")


class TestDryRunNoRecordOutcome:
    async def test_dry_run_no_record_outcome(self):
        """record_outcome never called in dry_run mode."""
        mem = _make_memory(id=10, content="pending decision no record")

        recall_result = _make_recall_result(
            [
                {"id": 100, "content": "ev1", "worked": True, "category": "decision"},
                {"id": 101, "content": "ev2", "worked": True, "category": "learning"},
                {"id": 102, "content": "ev3", "worked": True, "category": "pattern"},
            ]
        )

        ctx, _ = _make_ctx(memories_from_db=[mem], recall_result=recall_result)
        scheduler = _make_scheduler(user_active_set=False)
        session = _make_session()

        strategy = PendingOutcomeResolver(
            max_decisions=5, min_age_hours=1, evidence_threshold=3, dry_run=True
        )

        with patch(
            "daem0nmcp.dreaming.strategies.persist_dream_result",
            new_callable=AsyncMock,
        ):
            await strategy.execute(session, ctx, scheduler)

        ctx.memory_manager.record_outcome.assert_not_called()


class TestSessionTracksOutcomesResolved:
    async def test_session_tracks_outcomes_resolved(self):
        """outcomes_resolved increments on auto-resolve (not dry_run)."""
        mem = _make_memory(id=10, content="pending decision track")

        recall_result = _make_recall_result(
            [
                {"id": 100, "content": "ev1", "worked": True, "category": "decision"},
                {"id": 101, "content": "ev2", "worked": True, "category": "learning"},
                {"id": 102, "content": "ev3", "worked": True, "category": "pattern"},
            ]
        )

        ctx, _ = _make_ctx(memories_from_db=[mem], recall_result=recall_result)
        scheduler = _make_scheduler(user_active_set=False)
        session = _make_session()

        strategy = PendingOutcomeResolver(
            max_decisions=5, min_age_hours=1, evidence_threshold=3, dry_run=False
        )

        with patch(
            "daem0nmcp.dreaming.strategies.persist_dream_result",
            new_callable=AsyncMock,
        ):
            result = await strategy.execute(session, ctx, scheduler)

        assert result.outcomes_resolved == 1


class TestYieldOnUserActive:
    async def test_yield_on_user_active(self):
        """user_active.set() -> interrupted, early exit."""
        mem1 = _make_memory(id=10, content="pending A")
        mem2 = _make_memory(id=20, content="pending B")

        ctx, _ = _make_ctx(memories_from_db=[mem1, mem2])
        scheduler = _make_scheduler(user_active_set=True)
        session = _make_session()

        strategy = PendingOutcomeResolver(
            max_decisions=5, min_age_hours=1, dry_run=False
        )
        result = await strategy.execute(session, ctx, scheduler)

        assert result.interrupted is True
        assert result.decisions_reviewed == 0


class TestExceptionGraceful:
    async def test_exception_graceful(self):
        """recall() raises -> insufficient_evidence, no crash."""
        mem = _make_memory(id=10, content="pending decision error")

        ctx, _ = _make_ctx(memories_from_db=[mem])
        ctx.memory_manager.recall = AsyncMock(
            side_effect=RuntimeError("recall exploded")
        )
        scheduler = _make_scheduler(user_active_set=False)
        session = _make_session()

        strategy = PendingOutcomeResolver(
            max_decisions=5, min_age_hours=1, dry_run=False
        )
        result = await strategy.execute(session, ctx, scheduler)

        assert result.decisions_reviewed == 1
        assert result.results[0].result_type == "insufficient_evidence"
        assert "Error" in result.results[0].insight


class TestSelfReferenceExcluded:
    async def test_self_reference_excluded(self):
        """Evidence matching decision.id excluded from classification."""
        mem = _make_memory(id=10, content="pending self-ref test")

        # Only the decision itself in evidence -- should be excluded, leaving 0 items
        recall_result = _make_recall_result(
            [
                {
                    "id": 10,
                    "content": "pending self-ref test",
                    "worked": True,
                    "category": "decision",
                },
                {
                    "id": 100,
                    "content": "unrelated",
                    "worked": None,
                    "category": "pattern",
                },
            ]
        )

        ctx, _ = _make_ctx(memories_from_db=[mem], recall_result=recall_result)
        scheduler = _make_scheduler(user_active_set=False)
        session = _make_session()

        strategy = PendingOutcomeResolver(
            max_decisions=5, min_age_hours=1, evidence_threshold=3, dry_run=False
        )
        result = await strategy.execute(session, ctx, scheduler)

        # After excluding id=10, only 1 item remains -- insufficient
        assert result.results[0].result_type == "insufficient_evidence"
        # Verify id=10 is NOT in evidence_ids
        assert 10 not in result.results[0].evidence_ids
        assert 100 in result.results[0].evidence_ids


class TestPersistNotCalledForInsufficient:
    async def test_persist_not_called_for_insufficient(self):
        """Only actionable results persisted -- insufficient_evidence skipped."""
        mem = _make_memory(id=10, content="sparse pending")

        # Sparse evidence -> insufficient
        recall_result = _make_recall_result(
            [
                {
                    "id": 10,
                    "content": "sparse pending",
                    "worked": None,
                    "category": "decision",
                },
            ]
        )

        ctx, _ = _make_ctx(memories_from_db=[mem], recall_result=recall_result)
        scheduler = _make_scheduler(user_active_set=False)
        session = _make_session()

        strategy = PendingOutcomeResolver(
            max_decisions=5, min_age_hours=1, dry_run=False
        )

        with patch(
            "daem0nmcp.dreaming.strategies.persist_dream_result",
            new_callable=AsyncMock,
        ) as mock_persist:
            result = await strategy.execute(session, ctx, scheduler)

            assert result.results[0].result_type == "insufficient_evidence"
            mock_persist.assert_not_called()
            assert result.insights_generated == 0


class TestOldestFirstOrdering:
    async def test_oldest_first_ordering(self):
        """Query uses created_at.asc() -- oldest decisions evaluated first."""
        # Youngest decision
        mem_new = _make_memory(
            id=20,
            content="newer pending",
            created_at=datetime.now(timezone.utc) - timedelta(hours=25),
        )
        # Oldest decision
        mem_old = _make_memory(
            id=10,
            content="older pending",
            created_at=datetime.now(timezone.utc) - timedelta(hours=100),
        )

        # DB returns them in ASC order (oldest first)
        ctx, _ = _make_ctx(
            memories_from_db=[mem_old, mem_new],
            recall_result=_make_recall_result(
                [
                    {
                        "id": 200,
                        "content": "ev",
                        "worked": None,
                        "category": "decision",
                    },
                ]
            ),
        )
        scheduler = _make_scheduler(user_active_set=False)
        session = _make_session()

        strategy = PendingOutcomeResolver(
            max_decisions=5, min_age_hours=1, dry_run=False
        )
        result = await strategy.execute(session, ctx, scheduler)

        assert result.decisions_reviewed == 2
        # Oldest decision processed first
        assert result.results[0].source_decision_id == 10
        assert result.results[1].source_decision_id == 20
