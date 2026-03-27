"""Tests for evolve_rule (Rule Entropy Analysis) core logic.

Covers: single rule analysis, batch cap, code index unavailability,
rule-not-found error, and high staleness detection.
"""

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from daem0nmcp.cognitive import StalenessReport
from daem0nmcp.cognitive.evolve import run_evolution

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_rule(
    id: int,
    trigger: str = "use caching for database queries",
    created_at: datetime = None,
):
    """Create a mock Rule ORM object."""
    rule = MagicMock()
    rule.id = id
    rule.trigger = trigger
    rule.created_at = created_at or (datetime.now(timezone.utc) - timedelta(days=30))
    return rule


def _make_recall_result(memories=None):
    """Build a recall() return dict with categorised memories."""
    if memories is None:
        memories = []
    return {
        "topic": "query",
        "found": len(memories),
        "total_count": len(memories),
        "offset": 0,
        "limit": 20,
        "has_more": False,
        "summary": None,
        "decisions": [
            m for m in memories if m.get("category", "decision") == "decision"
        ],
        "patterns": [m for m in memories if m.get("category") == "pattern"],
        "learnings": [m for m in memories if m.get("category") == "learning"],
        "warnings": [m for m in memories if m.get("category") == "warning"],
    }


def _make_ctx(rules_from_db=None, recall_result=None):
    """Create a mock ProjectContext for evolve tests."""
    ctx = MagicMock()
    ctx.project_path = "/test/project"

    # --- db_manager.get_session() ---
    mock_session = AsyncMock()
    mock_result = MagicMock()

    if rules_from_db is not None and len(rules_from_db) == 1:
        # Single rule mode: scalar_one_or_none returns the rule
        mock_result.scalar_one_or_none.return_value = rules_from_db[0]
    elif rules_from_db is not None:
        # Batch mode: scalars().all() returns the list
        mock_result.scalar_one_or_none.return_value = None
        mock_result.scalars.return_value.all.return_value = rules_from_db
    else:
        mock_result.scalar_one_or_none.return_value = None
        mock_result.scalars.return_value.all.return_value = []

    mock_session.execute = AsyncMock(return_value=mock_result)

    async_cm = AsyncMock()
    async_cm.__aenter__ = AsyncMock(return_value=mock_session)
    async_cm.__aexit__ = AsyncMock(return_value=False)
    ctx.db_manager.get_session = MagicMock(return_value=async_cm)

    # --- memory_manager.recall() ---
    ctx.memory_manager.recall = AsyncMock(
        return_value=recall_result or _make_recall_result()
    )

    return ctx


def _mock_code_drift_unavailable():
    """Return a coroutine function that simulates unavailable code index."""

    async def _code_drift_analysis(terms, ctx):
        suggestion_notes = ["Code index unavailable -- drift analysis skipped"]
        return 0.0, [], [], suggestion_notes

    return _code_drift_analysis


# ---------------------------------------------------------------------------
# Test Cases
# ---------------------------------------------------------------------------


class TestEvolveSingleRule:
    @pytest.mark.asyncio
    async def test_evolve_single_rule(self):
        """Single rule with mixed outcomes produces a staleness report."""
        rule = _make_rule(id=1, trigger="use caching for database queries")
        recall = _make_recall_result(
            [
                {
                    "id": 10,
                    "content": "caching worked",
                    "worked": True,
                    "category": "decision",
                },
                {
                    "id": 11,
                    "content": "caching failed once",
                    "worked": False,
                    "category": "decision",
                },
                {
                    "id": 12,
                    "content": "db pattern",
                    "worked": None,
                    "category": "pattern",
                },
            ]
        )

        ctx = _make_ctx(rules_from_db=[rule], recall_result=recall)

        with patch(
            "daem0nmcp.cognitive.evolve._code_drift_analysis",
            side_effect=_mock_code_drift_unavailable(),
        ):
            results = await run_evolution(rule_id=1, ctx=ctx)

        assert len(results) == 1
        report = results[0]
        assert isinstance(report, StalenessReport)
        assert 0.0 <= report.staleness_score <= 1.0
        assert report.outcome_summary["worked"] >= 0
        assert report.outcome_summary["failed"] >= 0


class TestEvolveAllRulesCapped:
    @pytest.mark.asyncio
    async def test_evolve_all_rules_capped(self):
        """Batch mode with 15 rules caps results to cognitive_evolve_max_rules (10)."""
        rules = [
            _make_rule(id=i, trigger=f"rule {i} trigger text") for i in range(1, 16)
        ]
        recall = _make_recall_result(
            [
                {
                    "id": 100,
                    "content": "some evidence",
                    "worked": True,
                    "category": "decision",
                },
            ]
        )

        ctx = _make_ctx(rules_from_db=rules, recall_result=recall)

        # Override the db session to return all 15 in batch mode
        mock_session = AsyncMock()
        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = rules
        mock_session.execute = AsyncMock(return_value=mock_result)
        async_cm = AsyncMock()
        async_cm.__aenter__ = AsyncMock(return_value=mock_session)
        async_cm.__aexit__ = AsyncMock(return_value=False)
        ctx.db_manager.get_session = MagicMock(return_value=async_cm)

        with (
            patch(
                "daem0nmcp.cognitive.evolve._code_drift_analysis",
                side_effect=_mock_code_drift_unavailable(),
            ),
            patch("daem0nmcp.cognitive.evolve.settings") as mock_settings,
        ):
            mock_settings.cognitive_evolve_max_rules = 10
            mock_settings.cognitive_staleness_age_weight = 0.3
            results = await run_evolution(rule_id=None, ctx=ctx)

        assert len(results) <= 10


class TestEvolveNoCodeIndex:
    @pytest.mark.asyncio
    async def test_evolve_no_code_index(self):
        """When code index unavailable, code_drift_score=0.0 with note in suggestions."""
        rule = _make_rule(id=5, trigger="implement authentication middleware")
        recall = _make_recall_result(
            [
                {
                    "id": 50,
                    "content": "auth pattern",
                    "worked": True,
                    "category": "decision",
                },
            ]
        )

        ctx = _make_ctx(rules_from_db=[rule], recall_result=recall)

        with patch(
            "daem0nmcp.cognitive.evolve._code_drift_analysis",
            side_effect=_mock_code_drift_unavailable(),
        ):
            results = await run_evolution(rule_id=5, ctx=ctx)

        assert len(results) == 1
        report = results[0]
        assert report.code_drift_score == 0.0
        # Should have a suggestion noting code index unavailability
        info_suggestions = [
            s for s in report.evolution_suggestions if s.get("type") == "info"
        ]
        assert any(
            "unavailable" in s.get("reason", "").lower() for s in info_suggestions
        )


class TestEvolveRuleNotFound:
    @pytest.mark.asyncio
    async def test_evolve_rule_not_found(self):
        """When specific rule_id does not exist, raises ValueError."""
        ctx = _make_ctx(rules_from_db=None)

        # Override session to return None for scalar_one_or_none
        mock_session = AsyncMock()
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None
        mock_session.execute = AsyncMock(return_value=mock_result)
        async_cm = AsyncMock()
        async_cm.__aenter__ = AsyncMock(return_value=mock_session)
        async_cm.__aexit__ = AsyncMock(return_value=False)
        ctx.db_manager.get_session = MagicMock(return_value=async_cm)

        with pytest.raises(ValueError, match="no rule inscribed"):
            await run_evolution(rule_id=999, ctx=ctx)


class TestEvolveHighStaleness:
    @pytest.mark.asyncio
    async def test_evolve_high_staleness(self):
        """Rule with many failures and old age produces high staleness score."""
        old_date = datetime.now(timezone.utc) - timedelta(days=400)
        rule = _make_rule(
            id=7, trigger="use legacy system approach", created_at=old_date
        )

        # Many failures, no successes
        recall = _make_recall_result(
            [
                {
                    "id": 60,
                    "content": "legacy failed",
                    "worked": False,
                    "category": "decision",
                },
                {
                    "id": 61,
                    "content": "legacy broke",
                    "worked": False,
                    "category": "decision",
                },
                {
                    "id": 62,
                    "content": "legacy issues",
                    "worked": False,
                    "category": "decision",
                },
                {
                    "id": 63,
                    "content": "still failing",
                    "worked": False,
                    "category": "decision",
                },
            ]
        )

        ctx = _make_ctx(rules_from_db=[rule], recall_result=recall)

        with patch(
            "daem0nmcp.cognitive.evolve._code_drift_analysis",
            side_effect=_mock_code_drift_unavailable(),
        ):
            results = await run_evolution(rule_id=7, ctx=ctx)

        assert len(results) == 1
        report = results[0]
        # With code_drift=0.0, outcome_correlation=1.0, age_factor=0.3:
        # staleness = (0.0 * 0.4) + (1.0 * 0.4) + (0.3 * 0.2) = 0.46
        assert report.staleness_score > 0.4
        assert len(report.evolution_suggestions) > 0
