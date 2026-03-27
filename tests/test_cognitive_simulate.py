"""Tests for simulate_decision (Temporal Scrying) core logic.

Covers: basic simulation, version fallback, not-found error, no-changes scenario,
and new-evidence detection.
"""

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from daem0nmcp.cognitive import SimulationResult
from daem0nmcp.cognitive.simulate import run_simulation

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_memory(
    id: int,
    content: str = "some decision content",
    outcome: str = "it worked",
    worked: bool = True,
    created_at: datetime = None,
):
    """Create a mock Memory ORM object."""
    mem = MagicMock()
    mem.id = id
    mem.content = content
    mem.outcome = outcome
    mem.worked = worked
    mem.created_at = created_at or (datetime.now(timezone.utc) - timedelta(hours=48))
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


def _make_ctx(
    decision_memory=None,
    first_version=None,
    historical_recall=None,
    current_recall=None,
):
    """Create a mock ProjectContext with db_manager and memory_manager."""
    ctx = MagicMock()
    ctx.project_path = "/test/project"

    # --- db_manager.get_session() as async context manager ---
    call_count = {"n": 0}

    def make_session_cm():
        mock_session = AsyncMock()

        async def mock_execute(query):
            call_count["n"] += 1
            mock_result = MagicMock()
            if call_count["n"] == 1:
                # First call: look up the decision memory
                mock_result.scalar_one_or_none.return_value = decision_memory
            else:
                # Second call: look up the version
                mock_result.scalar_one_or_none.return_value = first_version
            return mock_result

        mock_session.execute = mock_execute

        async_cm = AsyncMock()
        async_cm.__aenter__ = AsyncMock(return_value=mock_session)
        async_cm.__aexit__ = AsyncMock(return_value=False)
        return async_cm

    ctx.db_manager.get_session = make_session_cm

    # --- memory_manager.recall() ---
    recall_calls = {"n": 0}

    async def mock_recall(**kwargs):
        recall_calls["n"] += 1
        if recall_calls["n"] == 1:
            return historical_recall or _make_recall_result()
        return current_recall or _make_recall_result()

    ctx.memory_manager.recall = mock_recall

    return ctx


# ---------------------------------------------------------------------------
# Test Cases
# ---------------------------------------------------------------------------


class TestSimulateDecisionBasic:
    @pytest.mark.asyncio
    async def test_simulate_decision_basic(self):
        """Basic simulation: decision found, version exists, knowledge diff populated."""
        decision_time = datetime.now(timezone.utc) - timedelta(days=5)
        decision_mem = _make_memory(
            id=1, content="Use Redis for caching", created_at=decision_time
        )

        version = MagicMock()
        version.changed_at = decision_time

        historical = _make_recall_result(
            [
                {
                    "id": 10,
                    "content": "old evidence",
                    "worked": True,
                    "category": "decision",
                },
            ]
        )
        current = _make_recall_result(
            [
                {
                    "id": 10,
                    "content": "old evidence",
                    "worked": True,
                    "category": "decision",
                },
                {
                    "id": 20,
                    "content": "new evidence",
                    "worked": None,
                    "category": "learning",
                },
            ]
        )

        ctx = _make_ctx(
            decision_memory=decision_mem,
            first_version=version,
            historical_recall=historical,
            current_recall=current,
        )

        result = await run_simulation(1, ctx)

        assert isinstance(result, SimulationResult)
        assert result.decision_id == 1
        assert result.knowledge_diff["new_count"] >= 1
        assert result.confidence > 0


class TestSimulateDecisionNoVersionsFallback:
    @pytest.mark.asyncio
    async def test_simulate_decision_no_versions_fallback(self):
        """When no version records exist, falls back to Memory.created_at."""
        decision_time = datetime.now(timezone.utc) - timedelta(days=3)
        decision_mem = _make_memory(
            id=2, content="Use PostgreSQL", created_at=decision_time
        )

        ctx = _make_ctx(
            decision_memory=decision_mem,
            first_version=None,  # No version record
        )

        result = await run_simulation(2, ctx)

        assert isinstance(result, SimulationResult)
        assert result.decision_id == 2
        # Should use created_at as fallback -- verify it completes without error
        assert result.decision_time == decision_time.isoformat()


class TestSimulateDecisionNotFound:
    @pytest.mark.asyncio
    async def test_simulate_decision_not_found(self):
        """When decision memory doesn't exist, raises ValueError with daemon message."""
        ctx = _make_ctx(decision_memory=None)

        with pytest.raises(ValueError, match="never inscribed"):
            await run_simulation(999, ctx)


class TestSimulateDecisionNoChanges:
    @pytest.mark.asyncio
    async def test_simulate_decision_no_changes(self):
        """When historical and current recall return same memories, no changes detected."""
        decision_time = datetime.now(timezone.utc) - timedelta(days=1)
        decision_mem = _make_memory(
            id=3, content="Keep using REST", created_at=decision_time
        )

        same_memories = [
            {
                "id": 10,
                "content": "REST is stable",
                "worked": True,
                "category": "decision",
            },
            {
                "id": 11,
                "content": "REST pattern",
                "worked": None,
                "category": "pattern",
            },
        ]
        same_recall = _make_recall_result(same_memories)

        ctx = _make_ctx(
            decision_memory=decision_mem,
            first_version=None,
            historical_recall=same_recall,
            current_recall=same_recall,
        )

        result = await run_simulation(3, ctx)

        assert result.knowledge_diff["new_count"] == 0
        assert result.knowledge_diff["invalidated_count"] == 0
        assert result.confidence == pytest.approx(0.0)
        assert "unchanged" in result.counterfactual_assessment.lower()


class TestSimulateDecisionNewEvidence:
    @pytest.mark.asyncio
    async def test_simulate_decision_new_evidence(self):
        """When current recall has more memories than historical, new_evidence is populated."""
        decision_time = datetime.now(timezone.utc) - timedelta(days=7)
        decision_mem = _make_memory(
            id=4, content="Deploy to AWS", created_at=decision_time
        )

        historical = _make_recall_result(
            [
                {
                    "id": 10,
                    "content": "AWS setup",
                    "worked": True,
                    "category": "decision",
                },
            ]
        )
        current = _make_recall_result(
            [
                {
                    "id": 10,
                    "content": "AWS setup",
                    "worked": True,
                    "category": "decision",
                },
                {
                    "id": 20,
                    "content": "GCP comparison",
                    "worked": True,
                    "category": "learning",
                },
                {
                    "id": 30,
                    "content": "cost analysis",
                    "worked": None,
                    "category": "pattern",
                },
            ]
        )

        ctx = _make_ctx(
            decision_memory=decision_mem,
            first_version=None,
            historical_recall=historical,
            current_recall=current,
        )

        result = await run_simulation(4, ctx)

        assert result.knowledge_diff["new_count"] == 2
        new_ids = [e["id"] for e in result.knowledge_diff["new_evidence"]]
        assert 20 in new_ids
        assert 30 in new_ids
        assert result.confidence > 0
