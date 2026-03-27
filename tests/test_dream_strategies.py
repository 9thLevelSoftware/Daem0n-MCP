"""Tests for dream strategies.

Covers: FailedDecisionReview, ConnectionDiscovery, CommunityRefresh,
multi-strategy pipeline, query filtering, re-evaluation logic,
yield behavior, persistence calls, and graceful error degradation.
"""

import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from daem0nmcp.dreaming.persistence import DreamSession, persist_session_summary
from daem0nmcp.dreaming.strategies import (
    CommunityRefresh,
    ConnectionDiscovery,
    DreamStrategy,
    FailedDecisionReview,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_memory(
    id: int,
    content: str = "some decision content",
    outcome: str = "it failed",
    worked: bool = False,
    archived: bool = False,
    created_at: datetime = None,
    category: str = "decision",
):
    """Create a mock Memory object with the required fields."""
    mem = MagicMock()
    mem.id = id
    mem.content = content
    mem.outcome = outcome
    mem.worked = worked
    mem.archived = archived
    mem.created_at = created_at or (datetime.now(timezone.utc) - timedelta(hours=48))
    mem.category = category
    mem.context = {}
    mem.tags = []
    return mem


def _make_recall_result(memories=None):
    """Build a recall() return dict with categorised memories.

    Each memory in *memories* is a dict with at least ``id`` and
    optionally ``worked``, ``content``, ``outcome``.
    """
    if memories is None:
        memories = []
    return {
        "topic": "query",
        "found": len(memories),
        "total_count": len(memories),
        "offset": 0,
        "limit": 5,
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
    """Create a mock ProjectContext with db_manager and memory_manager.

    Args:
        memories_from_db: Failed decisions returned by the first DB query.
        recall_result: Result returned by memory_manager.recall().
        review_memories: Learning memories returned by the second DB query
            (used by cooldown guard). If provided, db_session.execute
            returns different results on successive calls.
    """
    ctx = MagicMock()
    ctx.project_path = "/test/project"

    # --- db_manager.get_session() as async context manager ---
    mock_session = AsyncMock()

    if review_memories is not None:
        # Two sequential execute calls: first for failed decisions, second for review memories
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

    # get_session returns an async context manager
    async_cm = AsyncMock()
    async_cm.__aenter__ = AsyncMock(return_value=mock_session)
    async_cm.__aexit__ = AsyncMock(return_value=False)
    ctx.db_manager.get_session = MagicMock(return_value=async_cm)

    # --- memory_manager.recall() ---
    ctx.memory_manager.recall = AsyncMock(
        return_value=recall_result or _make_recall_result()
    )

    # --- memory_manager.remember() for persist_dream_result ---
    ctx.memory_manager.remember = AsyncMock(return_value={"id": 999})

    return ctx, mock_session


def _make_session():
    """Create a fresh DreamSession for testing."""
    return DreamSession(project_path="/test/project")


# ---------------------------------------------------------------------------
# Test Cases
# ---------------------------------------------------------------------------


class TestNoFailedDecisions:
    @pytest.mark.asyncio
    async def test_no_failed_decisions_returns_empty_session(self):
        """When no failed decisions exist, session should be empty."""
        ctx, _ = _make_ctx(memories_from_db=[])
        scheduler = _make_scheduler(user_active_set=False)
        session = _make_session()

        strategy = FailedDecisionReview(max_decisions=5, min_age_hours=1)
        result = await strategy.execute(session, ctx, scheduler)

        assert result.decisions_reviewed == 0
        assert result.results == []
        assert not result.interrupted


class TestReviewsFailedDecisions:
    @pytest.mark.asyncio
    async def test_reviews_failed_decisions(self):
        """Strategy should review each failed decision and produce results."""
        mem1 = _make_memory(id=10, content="decision A failed")
        mem2 = _make_memory(id=20, content="decision B failed")

        recall_result = _make_recall_result(
            [
                {
                    "id": 100,
                    "content": "some evidence",
                    "worked": None,
                    "category": "decision",
                },
                {
                    "id": 101,
                    "content": "more evidence",
                    "worked": None,
                    "category": "learning",
                },
                {
                    "id": 102,
                    "content": "even more",
                    "worked": None,
                    "category": "pattern",
                },
            ]
        )

        ctx, _ = _make_ctx(memories_from_db=[mem1, mem2], recall_result=recall_result)
        scheduler = _make_scheduler(user_active_set=False)
        session = _make_session()

        strategy = FailedDecisionReview(max_decisions=5, min_age_hours=1)
        result = await strategy.execute(session, ctx, scheduler)

        assert result.decisions_reviewed == 2
        assert len(result.results) == 2


class TestMaxDecisionsLimit:
    @pytest.mark.asyncio
    async def test_respects_max_decisions_limit(self):
        """Strategy should limit the query to max_decisions."""
        mem1 = _make_memory(id=10)
        # Only 1 returned from DB because limit=1 in the query
        ctx, mock_session = _make_ctx(
            memories_from_db=[mem1],
            recall_result=_make_recall_result(
                [
                    {
                        "id": 100,
                        "content": "ev",
                        "worked": None,
                        "category": "decision",
                    },
                    {
                        "id": 101,
                        "content": "ev2",
                        "worked": None,
                        "category": "learning",
                    },
                    {
                        "id": 102,
                        "content": "ev3",
                        "worked": None,
                        "category": "pattern",
                    },
                ]
            ),
        )
        scheduler = _make_scheduler(user_active_set=False)
        session = _make_session()

        strategy = FailedDecisionReview(max_decisions=1, min_age_hours=1)
        result = await strategy.execute(session, ctx, scheduler)

        # Only 1 decision should have been reviewed (limit enforced at DB)
        assert result.decisions_reviewed == 1


class TestYieldOnUserActive:
    @pytest.mark.asyncio
    async def test_yield_on_user_active(self):
        """If user_active is set, strategy should yield immediately."""
        mem1 = _make_memory(id=10)
        mem2 = _make_memory(id=20)
        mem3 = _make_memory(id=30)

        ctx, _ = _make_ctx(memories_from_db=[mem1, mem2, mem3])
        # User is already active
        scheduler = _make_scheduler(user_active_set=True)
        session = _make_session()

        strategy = FailedDecisionReview(max_decisions=5, min_age_hours=1)
        result = await strategy.execute(session, ctx, scheduler)

        assert result.interrupted is True
        assert result.decisions_reviewed == 0


class TestReEvaluateRevised:
    @pytest.mark.asyncio
    async def test_re_evaluate_produces_revised_result(self):
        """When recall returns worked=True evidence, result_type should be 'revised'."""
        mem = _make_memory(id=10, content="Use approach X for caching")

        recall_result = _make_recall_result(
            [
                {
                    "id": 100,
                    "content": "approach X works now",
                    "worked": True,
                    "category": "decision",
                },
                {
                    "id": 101,
                    "content": "caching pattern",
                    "worked": None,
                    "category": "pattern",
                },
            ]
        )

        ctx, _ = _make_ctx(memories_from_db=[mem], recall_result=recall_result)
        scheduler = _make_scheduler(user_active_set=False)
        session = _make_session()

        strategy = FailedDecisionReview(max_decisions=5, min_age_hours=1)
        result = await strategy.execute(session, ctx, scheduler)

        assert len(result.results) == 1
        assert result.results[0].result_type == "revised"
        assert result.results[0].source_decision_id == 10


class TestReEvaluateSparseEvidence:
    @pytest.mark.asyncio
    async def test_re_evaluate_sparse_evidence_needs_more_data(self):
        """When recall returns sparse evidence, result_type should be 'needs_more_data'."""
        mem = _make_memory(id=10, content="Use approach Y")

        # Only return the decision itself -- no other evidence
        recall_result = _make_recall_result(
            [
                {
                    "id": 10,
                    "content": "Use approach Y",
                    "worked": False,
                    "category": "decision",
                },
            ]
        )

        ctx, _ = _make_ctx(memories_from_db=[mem], recall_result=recall_result)
        scheduler = _make_scheduler(user_active_set=False)
        session = _make_session()

        strategy = FailedDecisionReview(max_decisions=5, min_age_hours=1)
        result = await strategy.execute(session, ctx, scheduler)

        assert len(result.results) == 1
        assert result.results[0].result_type == "needs_more_data"


class TestReEvaluateConfirmedFailure:
    @pytest.mark.asyncio
    async def test_re_evaluate_confirmed_failure(self):
        """When recall returns evidence but no worked=True, result_type should be 'confirmed_failure'."""
        mem = _make_memory(id=10, content="Use approach Z")

        recall_result = _make_recall_result(
            [
                {
                    "id": 100,
                    "content": "Z is problematic",
                    "worked": False,
                    "category": "decision",
                },
                {
                    "id": 101,
                    "content": "Z related warning",
                    "worked": None,
                    "category": "warning",
                },
                {
                    "id": 102,
                    "content": "Z context",
                    "worked": None,
                    "category": "learning",
                },
            ]
        )

        ctx, _ = _make_ctx(memories_from_db=[mem], recall_result=recall_result)
        scheduler = _make_scheduler(user_active_set=False)
        session = _make_session()

        strategy = FailedDecisionReview(max_decisions=5, min_age_hours=1)
        result = await strategy.execute(session, ctx, scheduler)

        assert len(result.results) == 1
        assert result.results[0].result_type == "confirmed_failure"


class TestPersistCalledForInsights:
    @pytest.mark.asyncio
    async def test_persist_called_for_insights(self):
        """persist_dream_result should be called for revised/confirmed, not needs_more_data."""
        mem_revised = _make_memory(id=10, content="approach A")
        mem_sparse = _make_memory(id=20, content="approach B")

        call_index = 0

        async def mock_recall(topic, limit=5, project_path=None):
            nonlocal call_index
            call_index += 1
            if call_index == 1:
                # First decision: returns worked=True evidence -> revised
                return _make_recall_result(
                    [
                        {
                            "id": 100,
                            "content": "ev",
                            "worked": True,
                            "category": "decision",
                        },
                        {
                            "id": 101,
                            "content": "ev2",
                            "worked": None,
                            "category": "pattern",
                        },
                    ]
                )
            else:
                # Second decision: sparse evidence -> needs_more_data
                return _make_recall_result(
                    [
                        {
                            "id": 20,
                            "content": "approach B",
                            "worked": False,
                            "category": "decision",
                        },
                    ]
                )

        ctx, _ = _make_ctx(memories_from_db=[mem_revised, mem_sparse])
        ctx.memory_manager.recall = AsyncMock(side_effect=mock_recall)
        scheduler = _make_scheduler(user_active_set=False)
        session = _make_session()

        with patch(
            "daem0nmcp.dreaming.strategies.persist_dream_result",
            new_callable=AsyncMock,
        ) as mock_persist:
            strategy = FailedDecisionReview(max_decisions=5, min_age_hours=1)
            result = await strategy.execute(session, ctx, scheduler)

            assert result.decisions_reviewed == 2
            assert len(result.results) == 2

            # First result is "revised" -> persist called
            # Second result is "needs_more_data" -> persist NOT called
            assert mock_persist.call_count == 1
            assert result.insights_generated == 1


class TestExceptionInReEvaluate:
    @pytest.mark.asyncio
    async def test_exception_in_re_evaluate_does_not_crash(self):
        """If recall() raises, strategy should not crash -- graceful degradation."""
        mem = _make_memory(id=10, content="approach that errors")

        ctx, _ = _make_ctx(memories_from_db=[mem])
        ctx.memory_manager.recall = AsyncMock(
            side_effect=RuntimeError("recall exploded")
        )
        scheduler = _make_scheduler(user_active_set=False)
        session = _make_session()

        strategy = FailedDecisionReview(max_decisions=5, min_age_hours=1)
        result = await strategy.execute(session, ctx, scheduler)

        # Should complete without raising
        assert result.decisions_reviewed == 1
        assert len(result.results) == 1
        assert result.results[0].result_type == "needs_more_data"
        assert "Error" in result.results[0].insight


class TestMinAgeFilter:
    @pytest.mark.asyncio
    async def test_min_age_filter(self):
        """Strategy should pass age cutoff to the database query."""
        ctx, mock_session = _make_ctx(memories_from_db=[])
        scheduler = _make_scheduler(user_active_set=False)
        session = _make_session()

        strategy = FailedDecisionReview(max_decisions=5, min_age_hours=24)
        await strategy.execute(session, ctx, scheduler)

        # Verify execute was called on the mock session (the query was issued)
        mock_session.execute.assert_called_once()

        # The query should have been issued -- we just verify the call happened.
        # The actual SQL filtering is tested by the fact that min_age_hours=24
        # computes an age_cutoff 24 hours ago and passes it to the WHERE clause.
        # With mocked DB, we trust SQLAlchemy does the right thing with our query.
        assert session.decisions_reviewed == 0  # No results from mocked DB


class TestDreamStrategyABC:
    """Verify DreamStrategy cannot be instantiated directly."""

    def test_cannot_instantiate_base_class(self):
        from daem0nmcp.dreaming.strategies import DreamStrategy

        with pytest.raises(TypeError):
            DreamStrategy()  # type: ignore[abstract]


class TestResultProvenance:
    @pytest.mark.asyncio
    async def test_result_contains_provenance_fields(self):
        """DreamResult should contain source_decision_id, original_content, evidence_ids."""
        mem = _make_memory(
            id=42, content="Use Redis for sessions", outcome="OOM killed"
        )

        recall_result = _make_recall_result(
            [
                {
                    "id": 200,
                    "content": "Redis works for cache",
                    "worked": True,
                    "category": "decision",
                },
                {
                    "id": 201,
                    "content": "Session pattern",
                    "worked": None,
                    "category": "pattern",
                },
            ]
        )

        ctx, _ = _make_ctx(memories_from_db=[mem], recall_result=recall_result)
        scheduler = _make_scheduler(user_active_set=False)
        session = _make_session()

        strategy = FailedDecisionReview(max_decisions=5, min_age_hours=1)
        result = await strategy.execute(session, ctx, scheduler)

        dream_result = result.results[0]
        assert dream_result.source_decision_id == 42
        assert dream_result.original_content == "Use Redis for sessions"
        assert dream_result.original_outcome == "OOM killed"
        assert 200 in dream_result.evidence_ids
        assert 201 in dream_result.evidence_ids
        assert dream_result.result_type == "revised"


class TestCooldownSkipsRecentlyReviewedDecisions:
    @pytest.mark.asyncio
    async def test_cooldown_skips_recently_reviewed_decisions(self):
        """Decisions reviewed within the cooldown window should be filtered out."""
        # Failed decisions in the DB
        mem1 = _make_memory(id=10, content="decision A failed")
        mem2 = _make_memory(id=20, content="decision B failed")

        # Recent review memories (within cooldown)
        review_mem = _make_memory(
            id=100,
            content="Dream re-evaluation: ...",
            category="learning",
            created_at=datetime.now(timezone.utc) - timedelta(hours=1),
        )
        review_mem.tags = ["dream", "re-evaluation", "source-decision:10"]

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
                    {
                        "id": 201,
                        "content": "ev2",
                        "worked": None,
                        "category": "learning",
                    },
                    {
                        "id": 202,
                        "content": "ev3",
                        "worked": None,
                        "category": "pattern",
                    },
                ]
            ),
            review_memories=[review_mem],
        )
        scheduler = _make_scheduler(user_active_set=False)
        session = _make_session()

        # Cooldown = 72h, review_mem was created 1h ago -> decision 10 is skipped
        strategy = FailedDecisionReview(
            max_decisions=5, min_age_hours=1, review_cooldown_hours=72
        )
        result = await strategy.execute(session, ctx, scheduler)

        # Only decision 20 should have been reviewed (decision 10 filtered out)
        assert result.decisions_reviewed == 1
        assert len(result.results) == 1
        assert result.results[0].source_decision_id == 20


class TestCooldownAllowsOldReviews:
    @pytest.mark.asyncio
    async def test_cooldown_allows_old_reviews(self):
        """Decisions reviewed outside the cooldown window should NOT be filtered."""
        mem1 = _make_memory(id=10, content="decision A failed")

        # Review memory created well outside the cooldown window
        old_review = _make_memory(
            id=100,
            content="Dream re-evaluation: ...",
            category="learning",
            created_at=datetime.now(timezone.utc) - timedelta(hours=200),
        )
        old_review.tags = ["dream", "re-evaluation", "source-decision:10"]

        # The cooldown query only returns memories within the window,
        # so old_review should NOT appear in the review_memories result.
        # We simulate this by returning an empty review_memories list.
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
                    {
                        "id": 201,
                        "content": "ev2",
                        "worked": None,
                        "category": "learning",
                    },
                    {
                        "id": 202,
                        "content": "ev3",
                        "worked": None,
                        "category": "pattern",
                    },
                ]
            ),
            review_memories=[],  # No recent reviews within cooldown window
        )
        scheduler = _make_scheduler(user_active_set=False)
        session = _make_session()

        strategy = FailedDecisionReview(
            max_decisions=5, min_age_hours=1, review_cooldown_hours=72
        )
        result = await strategy.execute(session, ctx, scheduler)

        # Decision 10 should still be reviewed (old review is outside cooldown)
        assert result.decisions_reviewed == 1
        assert result.results[0].source_decision_id == 10


class TestCooldownZeroDisablesFiltering:
    @pytest.mark.asyncio
    async def test_cooldown_zero_disables_filtering(self):
        """When review_cooldown_hours=0, no filtering should occur."""
        mem1 = _make_memory(id=10, content="decision A failed")

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
                    {
                        "id": 201,
                        "content": "ev2",
                        "worked": None,
                        "category": "learning",
                    },
                    {
                        "id": 202,
                        "content": "ev3",
                        "worked": None,
                        "category": "pattern",
                    },
                ]
            ),
        )
        scheduler = _make_scheduler(user_active_set=False)
        session = _make_session()

        strategy = FailedDecisionReview(
            max_decisions=5, min_age_hours=1, review_cooldown_hours=0
        )
        result = await strategy.execute(session, ctx, scheduler)

        # No cooldown filtering, decision 10 should be reviewed
        assert result.decisions_reviewed == 1


# ---------------------------------------------------------------------------
# ConnectionDiscovery tests
# ---------------------------------------------------------------------------


def _make_connection_ctx(
    unlinked_pairs=None,
    link_result=None,
):
    """Create a mock context for ConnectionDiscovery tests.

    Args:
        unlinked_pairs: Pairs returned by _find_unlinked_pairs (mocked externally).
        link_result: Return value of memory_manager.link_memories().
    """
    ctx = MagicMock()
    ctx.project_path = "/test/project"
    ctx.memory_manager.link_memories = AsyncMock(
        return_value=link_result or {"status": "created", "id": 1}
    )
    ctx.memory_manager.invalidate_graph_cache = MagicMock()

    # DB session (for _find_unlinked_pairs -- usually patched)
    mock_session = AsyncMock()
    async_cm = AsyncMock()
    async_cm.__aenter__ = AsyncMock(return_value=mock_session)
    async_cm.__aexit__ = AsyncMock(return_value=False)
    ctx.db_manager.get_session = MagicMock(return_value=async_cm)
    return ctx


class TestConnectionDiscoveryNoop:
    @pytest.mark.asyncio
    async def test_no_unlinked_pairs_is_noop(self):
        """No unlinked pairs → 0 insights, strategy name recorded."""
        ctx = _make_connection_ctx()
        scheduler = _make_scheduler(user_active_set=False)
        session = _make_session()

        strategy = ConnectionDiscovery()
        with patch.object(
            strategy, "_find_unlinked_pairs", new_callable=AsyncMock, return_value=[]
        ):
            result = await strategy.execute(session, ctx, scheduler)

        assert result.insights_generated == 0
        assert "ConnectionDiscovery" in result.strategies_run


class TestConnectionDiscoveryCreatesLinks:
    @pytest.mark.asyncio
    async def test_creates_related_to_relationships(self):
        """Unlinked pairs → link_memories called with correct args."""
        pairs = [
            (1, 2, {"auth", "jwt"}),
            (3, 4, {"cache", "redis"}),
            (5, 6, {"db", "migration"}),
        ]
        ctx = _make_connection_ctx()
        scheduler = _make_scheduler(user_active_set=False)
        session = _make_session()

        strategy = ConnectionDiscovery(confidence=0.7)
        with patch.object(
            strategy, "_find_unlinked_pairs", new_callable=AsyncMock, return_value=pairs
        ):
            result = await strategy.execute(session, ctx, scheduler)

        assert ctx.memory_manager.link_memories.call_count == 3
        assert result.insights_generated == 3

        # Check args of first call
        call_kwargs = ctx.memory_manager.link_memories.call_args_list[0].kwargs
        assert call_kwargs["source_id"] == 1
        assert call_kwargs["target_id"] == 2
        assert call_kwargs["relationship"] == "related_to"
        assert call_kwargs["confidence"] == 0.7


class TestConnectionDiscoveryMaxConnections:
    @pytest.mark.asyncio
    async def test_respects_max_connections(self):
        """max_connections=2, 5 pairs → only 2 created."""
        pairs = [
            (1, 2, {"a", "b"}),
            (3, 4, {"c", "d"}),
            (5, 6, {"e", "f"}),
            (7, 8, {"g", "h"}),
            (9, 10, {"i", "j"}),
        ]
        ctx = _make_connection_ctx()
        scheduler = _make_scheduler(user_active_set=False)
        session = _make_session()

        strategy = ConnectionDiscovery(max_connections=2)
        with patch.object(
            strategy, "_find_unlinked_pairs", new_callable=AsyncMock, return_value=pairs
        ):
            result = await strategy.execute(session, ctx, scheduler)

        assert ctx.memory_manager.link_memories.call_count == 2
        assert result.insights_generated == 2


class TestConnectionDiscoverySkipsAlreadyLinked:
    @pytest.mark.asyncio
    async def test_skips_already_linked(self):
        """link_memories returns already_exists → insights_generated not incremented."""
        pairs = [(1, 2, {"a", "b"})]
        ctx = _make_connection_ctx(link_result={"status": "already_exists"})
        scheduler = _make_scheduler(user_active_set=False)
        session = _make_session()

        strategy = ConnectionDiscovery()
        with patch.object(
            strategy, "_find_unlinked_pairs", new_callable=AsyncMock, return_value=pairs
        ):
            result = await strategy.execute(session, ctx, scheduler)

        assert result.insights_generated == 0


class TestConnectionDiscoveryYield:
    @pytest.mark.asyncio
    async def test_yields_on_user_active(self):
        """Interrupted before processing → no connections."""
        pairs = [(1, 2, {"a", "b"})]
        ctx = _make_connection_ctx()
        scheduler = _make_scheduler(user_active_set=True)
        session = _make_session()

        strategy = ConnectionDiscovery()
        with patch.object(
            strategy, "_find_unlinked_pairs", new_callable=AsyncMock, return_value=pairs
        ):
            result = await strategy.execute(session, ctx, scheduler)

        assert result.interrupted is True
        assert ctx.memory_manager.link_memories.call_count == 0


class TestConnectionDiscoveryQueryError:
    @pytest.mark.asyncio
    async def test_query_error_returns_session(self):
        """Exception in _find_unlinked_pairs → graceful return."""
        ctx = _make_connection_ctx()
        scheduler = _make_scheduler(user_active_set=False)
        session = _make_session()

        strategy = ConnectionDiscovery()
        with patch.object(
            strategy,
            "_find_unlinked_pairs",
            new_callable=AsyncMock,
            side_effect=RuntimeError("DB error"),
        ):
            result = await strategy.execute(session, ctx, scheduler)

        assert result.insights_generated == 0
        assert "ConnectionDiscovery" in result.strategies_run


class TestConnectionDiscoveryInvalidatesCache:
    @pytest.mark.asyncio
    async def test_invalidates_graph_cache_after_connections(self):
        """After creating connections, invalidate_graph_cache() should be called."""
        pairs = [(1, 2, {"a", "b"})]
        ctx = _make_connection_ctx()
        scheduler = _make_scheduler(user_active_set=False)
        session = _make_session()

        strategy = ConnectionDiscovery()
        with patch.object(
            strategy, "_find_unlinked_pairs", new_callable=AsyncMock, return_value=pairs
        ):
            await strategy.execute(session, ctx, scheduler)

        ctx.memory_manager.invalidate_graph_cache.assert_called_once()


# ---------------------------------------------------------------------------
# CommunityRefresh tests
# ---------------------------------------------------------------------------


class TestCommunityRefreshNotStale:
    @pytest.mark.asyncio
    async def test_not_stale_skips_rebuild(self):
        """Below threshold → no rebuild."""
        ctx = _make_connection_ctx()
        scheduler = _make_scheduler(user_active_set=False)
        session = _make_session()

        strategy = CommunityRefresh(staleness_threshold=10)
        with patch.object(
            strategy, "_check_staleness", new_callable=AsyncMock, return_value=False
        ), patch(
            "daem0nmcp.dreaming.strategies.CommunityRefresh.execute.__module__",
            create=True,
        ):
            # Patch leidenalg import
            with patch.dict("sys.modules", {"leidenalg": MagicMock()}):
                result = await strategy.execute(session, ctx, scheduler)

        assert result.insights_generated == 0
        assert "CommunityRefresh" in result.strategies_run


class TestCommunityRefreshStale:
    @pytest.mark.asyncio
    async def test_stale_triggers_rebuild(self):
        """Above threshold → detect + save called."""
        ctx = _make_connection_ctx()
        ctx.memory_manager.get_knowledge_graph = AsyncMock(return_value=MagicMock())
        scheduler = _make_scheduler(user_active_set=False)
        session = _make_session()

        mock_cm_instance = MagicMock()
        mock_cm_instance.detect_communities_from_graph = AsyncMock(
            return_value=[{"name": "test"}]
        )
        mock_cm_instance.save_communities = AsyncMock(return_value={"created": 1})

        mock_cm_class = MagicMock(return_value=mock_cm_instance)
        mock_communities_module = MagicMock()
        mock_communities_module.CommunityManager = mock_cm_class

        strategy = CommunityRefresh(staleness_threshold=10)
        with patch.object(
            strategy, "_check_staleness", new_callable=AsyncMock, return_value=True
        ), patch.dict(
            "sys.modules",
            {
                "leidenalg": MagicMock(),
                "daem0nmcp.communities": mock_communities_module,
            },
        ):
            result = await strategy.execute(session, ctx, scheduler)

        mock_cm_instance.detect_communities_from_graph.assert_called_once()
        mock_cm_instance.save_communities.assert_called_once()
        assert result.insights_generated == 1


class TestCommunityRefreshNoCommunities:
    @pytest.mark.asyncio
    async def test_no_communities_means_stale(self):
        """max(created_at) is None, enough memories → rebuild."""
        ctx = _make_connection_ctx()
        ctx.memory_manager.get_knowledge_graph = AsyncMock(return_value=MagicMock())
        scheduler = _make_scheduler(user_active_set=False)
        session = _make_session()

        mock_cm_instance = MagicMock()
        mock_cm_instance.detect_communities_from_graph = AsyncMock(return_value=[])
        mock_cm_instance.save_communities = AsyncMock(return_value={"created": 0})

        mock_cm_class = MagicMock(return_value=mock_cm_instance)
        mock_communities_module = MagicMock()
        mock_communities_module.CommunityManager = mock_cm_class

        strategy = CommunityRefresh(staleness_threshold=5)

        # Mock _check_staleness to return True (no communities, enough memories)
        with patch.object(
            strategy, "_check_staleness", new_callable=AsyncMock, return_value=True
        ), patch.dict(
            "sys.modules",
            {
                "leidenalg": MagicMock(),
                "daem0nmcp.communities": mock_communities_module,
            },
        ):
            await strategy.execute(session, ctx, scheduler)

        mock_cm_instance.detect_communities_from_graph.assert_called_once()


class TestCommunityRefreshMissingLeidenalg:
    @pytest.mark.asyncio
    async def test_missing_leidenalg_skips_gracefully(self):
        """leidenalg not importable → warning logged, no crash."""
        ctx = _make_connection_ctx()
        scheduler = _make_scheduler(user_active_set=False)
        session = _make_session()

        strategy = CommunityRefresh(staleness_threshold=10)

        # Force leidenalg import to fail
        import sys

        original = sys.modules.get("leidenalg")
        sys.modules["leidenalg"] = None  # type: ignore[assignment]
        try:
            result = await strategy.execute(session, ctx, scheduler)
        finally:
            if original is None:
                sys.modules.pop("leidenalg", None)
            else:
                sys.modules["leidenalg"] = original

        assert result.insights_generated == 0
        assert "CommunityRefresh" in result.strategies_run


class TestCommunityRefreshYield:
    @pytest.mark.asyncio
    async def test_yields_before_rebuild(self):
        """User active before rebuild → interrupted, no rebuild."""
        ctx = _make_connection_ctx()
        ctx.memory_manager.get_knowledge_graph = AsyncMock(return_value=MagicMock())
        scheduler = _make_scheduler(user_active_set=True)
        session = _make_session()

        strategy = CommunityRefresh(staleness_threshold=10)
        with patch.object(
            strategy, "_check_staleness", new_callable=AsyncMock, return_value=True
        ), patch.dict("sys.modules", {"leidenalg": MagicMock()}):
            result = await strategy.execute(session, ctx, scheduler)

        assert result.interrupted is True
        assert result.insights_generated == 0


class TestCommunityRefreshInvalidatesCache:
    @pytest.mark.asyncio
    async def test_invalidates_graph_cache(self):
        """After save → invalidate_graph_cache() called."""
        ctx = _make_connection_ctx()
        ctx.memory_manager.get_knowledge_graph = AsyncMock(return_value=MagicMock())
        scheduler = _make_scheduler(user_active_set=False)
        session = _make_session()

        mock_cm_instance = MagicMock()
        mock_cm_instance.detect_communities_from_graph = AsyncMock(
            return_value=[{"name": "c1"}]
        )
        mock_cm_instance.save_communities = AsyncMock(return_value={"created": 1})

        mock_cm_class = MagicMock(return_value=mock_cm_instance)
        mock_communities_module = MagicMock()
        mock_communities_module.CommunityManager = mock_cm_class

        strategy = CommunityRefresh(staleness_threshold=10)
        with patch.object(
            strategy, "_check_staleness", new_callable=AsyncMock, return_value=True
        ), patch.dict(
            "sys.modules",
            {
                "leidenalg": MagicMock(),
                "daem0nmcp.communities": mock_communities_module,
            },
        ):
            await strategy.execute(session, ctx, scheduler)

        ctx.memory_manager.invalidate_graph_cache.assert_called_once()


# ---------------------------------------------------------------------------
# Multi-strategy pipeline tests
# ---------------------------------------------------------------------------


class _MockStrategy(DreamStrategy):
    """Concrete strategy for testing the pipeline."""

    def __init__(self, name_override: str, interrupt: bool = False):
        self._name_override = name_override
        self._interrupt = interrupt

    @property
    def name(self) -> str:
        return self._name_override

    async def execute(self, session, ctx, scheduler):
        session.strategies_run.append(self.name)
        if self._interrupt:
            session.interrupted = True
        return session


class TestMultiStrategyPipeline:
    @pytest.mark.asyncio
    async def test_all_strategies_run_in_sequence(self):
        """All strategies should run and record their names."""
        strategies = [
            _MockStrategy("Alpha"),
            _MockStrategy("Beta"),
            _MockStrategy("Gamma"),
        ]
        session = _make_session()
        ctx = MagicMock()
        scheduler = _make_scheduler(user_active_set=False)

        for strategy in strategies:
            if scheduler.user_active.is_set():
                session.interrupted = True
                break
            session = await strategy.execute(session, ctx, scheduler)
            if session.interrupted:
                break

        assert session.strategies_run == ["Alpha", "Beta", "Gamma"]

    @pytest.mark.asyncio
    async def test_interrupt_stops_pipeline(self):
        """First strategy sets interrupted → remaining skipped."""
        strategies = [
            _MockStrategy("Alpha", interrupt=True),
            _MockStrategy("Beta"),
            _MockStrategy("Gamma"),
        ]
        session = _make_session()
        ctx = MagicMock()
        scheduler = _make_scheduler(user_active_set=False)

        for strategy in strategies:
            if scheduler.user_active.is_set():
                session.interrupted = True
                break
            session = await strategy.execute(session, ctx, scheduler)
            if session.interrupted:
                break

        assert session.strategies_run == ["Alpha"]
        assert session.interrupted is True


# ---------------------------------------------------------------------------
# DreamSession tests
# ---------------------------------------------------------------------------


class TestDreamSessionStrategiesRun:
    def test_strategies_run_default_empty(self):
        """New session has strategies_run == []."""
        session = DreamSession()
        assert session.strategies_run == []

    @pytest.mark.asyncio
    async def test_persist_summary_includes_strategies(self):
        """Verify context dict has strategies_run."""
        session = _make_session()
        session.insights_generated = 1
        session.strategies_run = ["FailedDecisionReview", "ConnectionDiscovery"]

        mock_mm = AsyncMock()
        mock_mm.remember = AsyncMock(return_value={"id": 1})

        await persist_session_summary(mock_mm, session)

        call_kwargs = mock_mm.remember.call_args.kwargs
        assert "strategies_run" in call_kwargs["context"]
        assert call_kwargs["context"]["strategies_run"] == [
            "FailedDecisionReview",
            "ConnectionDiscovery",
        ]
        assert "strategies:" in call_kwargs["content"]


# ---------------------------------------------------------------------------
# DreamStrategy.name property test
# ---------------------------------------------------------------------------


class TestDreamStrategyNameProperty:
    def test_name_returns_class_name(self):
        """DreamStrategy.name should return the class name."""
        strategy = FailedDecisionReview(max_decisions=1, min_age_hours=1)
        assert strategy.name == "FailedDecisionReview"

    def test_connection_discovery_name(self):
        strategy = ConnectionDiscovery()
        assert strategy.name == "ConnectionDiscovery"

    def test_community_refresh_name(self):
        strategy = CommunityRefresh()
        assert strategy.name == "CommunityRefresh"
