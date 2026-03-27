"""Tests for debate_internal (Adversarial Council) core logic.

Covers: evidence scoring, insufficient evidence, basic debate flow,
convergence detection, max rounds cap, consensus persistence, and the
NO-LLM constraint.
"""

import inspect
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from daem0nmcp.cognitive import DebateResult
from daem0nmcp.cognitive.debate import run_debate, score_evidence

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


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


def _make_ctx(recall_result=None, recall_side_effect=None):
    """Create a mock ProjectContext for debate tests."""
    ctx = MagicMock()
    ctx.project_path = "/test/project"

    if recall_side_effect:
        ctx.memory_manager.recall = AsyncMock(side_effect=recall_side_effect)
    else:
        ctx.memory_manager.recall = AsyncMock(
            return_value=recall_result or _make_recall_result()
        )

    # remember() for consensus persistence
    ctx.memory_manager.remember = AsyncMock(return_value={"id": 777})

    return ctx


# ---------------------------------------------------------------------------
# Evidence Scoring
# ---------------------------------------------------------------------------


class TestScoreEvidenceEmpty:
    def test_score_evidence_empty(self):
        """score_evidence([]) returns 0.0."""
        assert score_evidence([]) == 0.0


class TestScoreEvidenceWithOutcomes:
    def test_score_evidence_with_outcomes(self):
        """Memories with worked=True produce higher score than worked=False."""
        positive_memories = [
            {"id": 1, "content": "worked", "worked": True, "relevance": 0.8},
            {"id": 2, "content": "also worked", "worked": True, "relevance": 0.7},
        ]
        negative_memories = [
            {"id": 3, "content": "failed", "worked": False, "relevance": 0.8},
            {"id": 4, "content": "also failed", "worked": False, "relevance": 0.7},
        ]

        positive_score = score_evidence(positive_memories)
        negative_score = score_evidence(negative_memories)

        assert positive_score > negative_score
        assert positive_score > 0.0
        assert negative_score > 0.0  # Still some score from relevance


# ---------------------------------------------------------------------------
# Debate Flow
# ---------------------------------------------------------------------------


class TestDebateInsufficientEvidence:
    @pytest.mark.asyncio
    async def test_debate_insufficient_evidence(self):
        """When both sides have <min_evidence memories, returns early."""
        # Return only 1 memory per recall (below default min_evidence=2)
        recall = _make_recall_result(
            [
                {
                    "id": 1,
                    "content": "lonely evidence",
                    "worked": True,
                    "category": "decision",
                },
            ]
        )
        ctx = _make_ctx(recall_result=recall)

        result = await run_debate(
            topic="test topic",
            advocate_position="option A",
            challenger_position="option B",
            ctx=ctx,
        )

        assert isinstance(result, DebateResult)
        assert result.confidence == 0.0
        assert result.winning_perspective == "insufficient_evidence"
        assert result.consensus_memory_id is None
        assert result.total_rounds == 0


class TestDebateBasicRuns:
    @pytest.mark.asyncio
    async def test_debate_basic_runs(self):
        """With enough evidence, debate runs at least 2 rounds and produces synthesis."""
        recall = _make_recall_result(
            [
                {
                    "id": 10,
                    "content": "evidence A1",
                    "worked": True,
                    "category": "decision",
                },
                {
                    "id": 11,
                    "content": "evidence A2",
                    "worked": True,
                    "category": "learning",
                },
                {
                    "id": 12,
                    "content": "evidence A3",
                    "worked": None,
                    "category": "pattern",
                },
            ]
        )
        ctx = _make_ctx(recall_result=recall)

        result = await run_debate(
            topic="framework choice",
            advocate_position="use React",
            challenger_position="use Vue",
            ctx=ctx,
        )

        assert isinstance(result, DebateResult)
        assert result.total_rounds >= 2
        assert result.synthesis != ""
        assert result.debate_id != ""
        assert result.topic == "framework choice"
        assert result.advocate_position == "use React"
        assert result.challenger_position == "use Vue"


class TestDebateConvergence:
    @pytest.mark.asyncio
    async def test_debate_convergence(self):
        """When recall returns consistent results, debate converges."""
        # Same evidence every call -> scores stabilize -> convergence
        recall = _make_recall_result(
            [
                {
                    "id": 10,
                    "content": "consistent evidence 1",
                    "worked": True,
                    "category": "decision",
                    "relevance": 0.8,
                },
                {
                    "id": 11,
                    "content": "consistent evidence 2",
                    "worked": True,
                    "category": "learning",
                    "relevance": 0.7,
                },
                {
                    "id": 12,
                    "content": "consistent evidence 3",
                    "worked": None,
                    "category": "pattern",
                    "relevance": 0.6,
                },
            ]
        )
        ctx = _make_ctx(recall_result=recall)

        result = await run_debate(
            topic="consistent topic",
            advocate_position="stable A",
            challenger_position="stable B",
            ctx=ctx,
        )

        assert result.converged is True
        assert result.convergence_round is not None
        assert result.convergence_round >= 2


class TestDebateMaxRoundsCap:
    @pytest.mark.asyncio
    async def test_debate_max_rounds_cap(self):
        """Debate never exceeds cognitive_debate_max_rounds."""
        call_idx = {"n": 0}

        async def varying_recall(**kwargs):
            """Return different evidence each time to prevent convergence."""
            call_idx["n"] += 1
            return _make_recall_result(
                [
                    {
                        "id": call_idx["n"] * 100 + 1,
                        "content": f"evidence {call_idx['n']}",
                        "worked": True,
                        "category": "decision",
                        "relevance": 0.3 + (call_idx["n"] % 5) * 0.1,
                    },
                    {
                        "id": call_idx["n"] * 100 + 2,
                        "content": f"more {call_idx['n']}",
                        "worked": False,
                        "category": "learning",
                        "relevance": 0.5 + (call_idx["n"] % 3) * 0.15,
                    },
                    {
                        "id": call_idx["n"] * 100 + 3,
                        "content": f"extra {call_idx['n']}",
                        "worked": None,
                        "category": "pattern",
                        "relevance": 0.2 + (call_idx["n"] % 4) * 0.2,
                    },
                ]
            )

        ctx = _make_ctx(recall_side_effect=varying_recall)

        with patch("daem0nmcp.cognitive.debate.settings") as mock_settings:
            mock_settings.cognitive_debate_max_rounds = 3
            mock_settings.cognitive_debate_convergence_threshold = (
                0.001  # Very tight threshold to prevent convergence
            )
            mock_settings.cognitive_debate_min_evidence = 2
            result = await run_debate(
                topic="varied topic",
                advocate_position="option X",
                challenger_position="option Y",
                ctx=ctx,
            )

        assert result.total_rounds <= 3


class TestDebateConsensusStored:
    @pytest.mark.asyncio
    async def test_debate_consensus_stored(self):
        """Consensus memory is persisted with debate and consensus tags."""
        recall = _make_recall_result(
            [
                {
                    "id": 10,
                    "content": "evidence 1",
                    "worked": True,
                    "category": "decision",
                    "relevance": 0.8,
                },
                {
                    "id": 11,
                    "content": "evidence 2",
                    "worked": True,
                    "category": "learning",
                    "relevance": 0.7,
                },
                {
                    "id": 12,
                    "content": "evidence 3",
                    "worked": None,
                    "category": "pattern",
                    "relevance": 0.6,
                },
            ]
        )
        ctx = _make_ctx(recall_result=recall)

        await run_debate(
            topic="persistence test",
            advocate_position="store it",
            challenger_position="skip it",
            ctx=ctx,
        )

        # remember() should have been called for consensus persistence
        ctx.memory_manager.remember.assert_called_once()

        call_kwargs = ctx.memory_manager.remember.call_args
        # Check tags contain debate and consensus
        tags = (
            call_kwargs.kwargs.get("tags", [])
            if call_kwargs.kwargs
            else call_kwargs[1].get("tags", [])
        )
        assert "debate" in tags
        assert "consensus" in tags


class TestDebateNoLLMCalls:
    def test_debate_no_llm_calls(self):
        """The debate module must not import any LLM client libraries."""
        import daem0nmcp.cognitive.debate as debate_module

        source = inspect.getsource(debate_module)

        # Check that no LLM client libraries are imported
        llm_libs = ["openai", "anthropic", "langchain", "litellm", "cohere"]
        for lib in llm_libs:
            # Match actual import statements, not docstring mentions
            import_patterns = [
                f"import {lib}",
                f"from {lib}",
            ]
            for pattern in import_patterns:
                # Skip lines that are comments or docstrings
                for line in source.split("\n"):
                    stripped = line.strip()
                    if (
                        stripped.startswith("#")
                        or stripped.startswith('"')
                        or stripped.startswith("'")
                    ):
                        continue
                    assert pattern not in stripped, (
                        f"debate.py imports LLM library '{lib}' -- "
                        f"the Adversarial Council must use only memory evidence"
                    )
