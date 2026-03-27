"""Tests for Reflexion LangGraph state machine."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from daem0nmcp.reflexion.graph import (
    build_reflexion_graph,
    create_reflexion_app,
    run_reflexion,
    should_continue,
)
from daem0nmcp.reflexion.nodes import (
    MAX_ITERATIONS,
    QUALITY_THRESHOLD_EXIT,
    create_actor_node,
    create_evaluator_node,
    create_reflector_node,
)


@pytest.fixture
def mock_memory_manager():
    """Create a mock MemoryManager."""
    manager = AsyncMock()
    manager.recall = AsyncMock(return_value={"memories": []})
    return manager


@pytest.fixture
def mock_knowledge_graph():
    """Create a mock KnowledgeGraph."""
    kg = AsyncMock()
    kg.ensure_loaded = AsyncMock()
    kg._graph = MagicMock()
    kg._graph.nodes = MagicMock(return_value=[])
    return kg


class TestShouldContinue:
    """Tests for loop control function."""

    def test_continue_when_should_continue_true(self):
        """Should return 'reflector' when should_continue is True."""
        state = {"should_continue": True}
        assert should_continue(state) == "reflector"

    def test_exit_when_should_continue_false(self):
        """Should return END when should_continue is False."""
        from langgraph.graph import END

        state = {"should_continue": False}
        assert should_continue(state) == END

    def test_exit_when_should_continue_missing(self):
        """Should return END when should_continue is not in state."""
        from langgraph.graph import END

        state = {}
        assert should_continue(state) == END


class TestActorNode:
    """Tests for Actor node."""

    def test_actor_first_iteration(self):
        """First iteration should generate from query."""
        actor = create_actor_node(llm_func=None)
        state = {"query": "Test query", "iteration": 0, "critique": ""}

        result = actor(state)

        assert result["iteration"] == 1
        assert "Test query" in result["draft"]

    def test_actor_subsequent_iteration(self):
        """Subsequent iterations should incorporate critique."""
        actor = create_actor_node(llm_func=None)
        state = {
            "query": "Test query",
            "iteration": 1,
            "critique": "Fix the error",
            "draft": "Previous draft",
        }

        result = actor(state)

        assert result["iteration"] == 2
        # Should reference addressing critique
        assert "Addressing" in result["draft"] or "Fix" in result["draft"]

    def test_actor_with_llm_func(self):
        """Actor should use provided LLM function."""
        llm_func = MagicMock(return_value="LLM generated response")
        actor = create_actor_node(llm_func=llm_func)
        state = {"query": "Test query", "iteration": 0, "critique": ""}

        result = actor(state)

        assert result["draft"] == "LLM generated response"
        llm_func.assert_called_once()

    def test_actor_increments_iteration(self):
        """Actor should increment iteration count."""
        actor = create_actor_node(llm_func=None)
        state = {"query": "q", "iteration": 0, "critique": ""}

        result = actor(state)
        assert result["iteration"] == 1

        # Simulate next call with updated state
        state["iteration"] = 1
        result = actor(state)
        assert result["iteration"] == 2


class TestEvaluatorNode:
    """Tests for Evaluator node."""

    @pytest.mark.asyncio
    async def test_evaluator_extracts_claims(self, mock_memory_manager):
        """Evaluator should extract claims from draft."""
        evaluator = create_evaluator_node(memory_manager=mock_memory_manager)
        state = {
            "draft": "We decided to use Python. The system requires PostgreSQL.",
            "iteration": 1,
        }

        with patch("daem0nmcp.reflexion.nodes.verify_claims") as mock_verify:
            mock_verify.return_value = []
            result = await evaluator(state)

        # Should have extracted claims
        assert "claims" in result
        # claims list populated based on extract_claims

    @pytest.mark.asyncio
    async def test_evaluator_quality_score(self, mock_memory_manager):
        """Evaluator should compute quality score."""
        evaluator = create_evaluator_node(memory_manager=mock_memory_manager)
        state = {"draft": "Simple response.", "iteration": 1}

        with patch("daem0nmcp.reflexion.nodes.verify_claims") as mock_verify:
            mock_verify.return_value = []
            with patch(
                "daem0nmcp.reflexion.nodes.summarize_verification"
            ) as mock_summary:
                mock_summary.return_value = {
                    "verified_count": 0,
                    "unverified_count": 0,
                    "conflict_count": 0,
                    "overall_confidence": 0.9,
                    "conflicts": [],
                }
                result = await evaluator(state)

        assert "quality_score" in result
        assert 0.0 <= result["quality_score"] <= 1.0

    @pytest.mark.asyncio
    async def test_evaluator_generates_critique(self, mock_memory_manager):
        """Evaluator should generate critique based on verification."""
        evaluator = create_evaluator_node(memory_manager=mock_memory_manager)
        state = {"draft": "Some draft.", "iteration": 1}

        with patch("daem0nmcp.reflexion.nodes.verify_claims") as mock_verify:
            mock_verify.return_value = []
            with patch(
                "daem0nmcp.reflexion.nodes.summarize_verification"
            ) as mock_summary:
                mock_summary.return_value = {
                    "verified_count": 1,
                    "unverified_count": 2,
                    "conflict_count": 0,
                    "overall_confidence": 0.6,
                    "conflicts": [],
                }
                result = await evaluator(state)

        assert "critique" in result
        # With unverified claims, should mention them in critique
        # (actual content depends on verification results)

    @pytest.mark.asyncio
    async def test_evaluator_logs_warning_at_iteration_2(
        self, mock_memory_manager, caplog
    ):
        """Evaluator should log warning at iteration 2."""
        import logging

        evaluator = create_evaluator_node(memory_manager=mock_memory_manager)
        state = {"draft": "Draft.", "iteration": 2}

        with patch("daem0nmcp.reflexion.nodes.verify_claims") as mock_verify:
            mock_verify.return_value = []
            with patch(
                "daem0nmcp.reflexion.nodes.summarize_verification"
            ) as mock_summary:
                mock_summary.return_value = {
                    "verified_count": 0,
                    "unverified_count": 0,
                    "conflict_count": 0,
                    "overall_confidence": 0.5,
                    "conflicts": [],
                }
                with caplog.at_level(logging.WARNING):
                    await evaluator(state)

        # Check for diminishing returns warning
        assert any("diminishing returns" in record.message for record in caplog.records)


class TestReflectorNode:
    """Tests for Reflector node."""

    def test_reflector_synthesizes_critique(self):
        """Reflector should synthesize critique into instructions."""
        reflector = create_reflector_node()
        state = {
            "critique": "CONFLICTS DETECTED: Something wrong",
            "iteration": 1,
        }

        result = reflector(state)

        assert "critique" in result
        assert "Revision Instructions" in result["critique"]
        assert "conflicting" in result["critique"].lower()

    def test_reflector_handles_unverified_claims(self):
        """Reflector should handle unverified claims critique."""
        reflector = create_reflector_node()
        state = {
            "critique": "UNVERIFIED CLAIMS (2): claim1; claim2",
            "iteration": 1,
        }

        result = reflector(state)

        assert (
            "hedging" in result["critique"].lower()
            or "remove" in result["critique"].lower()
        )

    def test_reflector_handles_no_issues(self):
        """Reflector should handle case with no issues."""
        reflector = create_reflector_node()
        state = {
            "critique": "No issues found. Response appears well-grounded.",
            "iteration": 1,
        }

        result = reflector(state)

        assert "Minor refinements" in result["critique"]


class TestBuildReflexionGraph:
    """Tests for graph construction."""

    def test_build_graph_structure(self, mock_memory_manager):
        """Graph should have correct node structure."""
        graph = build_reflexion_graph(memory_manager=mock_memory_manager)

        # Check nodes exist
        assert "actor" in graph.nodes
        assert "evaluator" in graph.nodes
        assert "reflector" in graph.nodes

    def test_build_graph_with_knowledge_graph(
        self, mock_memory_manager, mock_knowledge_graph
    ):
        """Graph should build with optional knowledge graph."""
        graph = build_reflexion_graph(
            memory_manager=mock_memory_manager,
            knowledge_graph=mock_knowledge_graph,
        )

        assert "actor" in graph.nodes
        assert "evaluator" in graph.nodes
        assert "reflector" in graph.nodes

    def test_build_graph_with_llm_func(self, mock_memory_manager):
        """Graph should build with custom LLM function."""
        llm_func = MagicMock(return_value="Response")
        graph = build_reflexion_graph(
            memory_manager=mock_memory_manager,
            llm_func=llm_func,
        )

        assert "actor" in graph.nodes


class TestEarlyExitQualityThreshold:
    """Tests for early exit behavior."""

    @pytest.mark.asyncio
    async def test_early_exit_quality_threshold(self, mock_memory_manager):
        """Loop should exit early when quality threshold met."""
        evaluator = create_evaluator_node(memory_manager=mock_memory_manager)
        state = {"draft": "Good response.", "iteration": 1}

        with patch("daem0nmcp.reflexion.nodes.verify_claims") as mock_verify:
            mock_verify.return_value = []
            with patch(
                "daem0nmcp.reflexion.nodes.summarize_verification"
            ) as mock_summary:
                # High confidence -> should exit
                mock_summary.return_value = {
                    "verified_count": 2,
                    "unverified_count": 0,
                    "conflict_count": 0,
                    "overall_confidence": 0.95,
                    "conflicts": [],
                }
                result = await evaluator(state)

        # High quality should trigger exit
        assert result["quality_score"] >= QUALITY_THRESHOLD_EXIT
        assert result["should_continue"] is False

    @pytest.mark.asyncio
    async def test_continue_when_quality_low(self, mock_memory_manager):
        """Loop should continue when quality below threshold."""
        evaluator = create_evaluator_node(memory_manager=mock_memory_manager)
        state = {"draft": "Response.", "iteration": 1}

        with patch("daem0nmcp.reflexion.nodes.verify_claims") as mock_verify:
            mock_verify.return_value = []
            with patch(
                "daem0nmcp.reflexion.nodes.summarize_verification"
            ) as mock_summary:
                # Low confidence -> should continue
                mock_summary.return_value = {
                    "verified_count": 0,
                    "unverified_count": 2,
                    "conflict_count": 0,
                    "overall_confidence": 0.5,
                    "conflicts": [],
                }
                result = await evaluator(state)

        # Low quality should continue
        assert result["quality_score"] < QUALITY_THRESHOLD_EXIT
        assert result["should_continue"] is True

    @pytest.mark.asyncio
    async def test_max_iterations_enforced(self, mock_memory_manager):
        """Loop should exit after max iterations."""
        evaluator = create_evaluator_node(memory_manager=mock_memory_manager)
        state = {"draft": "Response.", "iteration": MAX_ITERATIONS}

        with patch("daem0nmcp.reflexion.nodes.verify_claims") as mock_verify:
            mock_verify.return_value = []
            with patch(
                "daem0nmcp.reflexion.nodes.summarize_verification"
            ) as mock_summary:
                # Low quality but max iterations
                mock_summary.return_value = {
                    "verified_count": 0,
                    "unverified_count": 1,
                    "conflict_count": 0,
                    "overall_confidence": 0.3,
                    "conflicts": [],
                }
                result = await evaluator(state)

        # Max iterations should force exit
        assert result["should_continue"] is False


class TestConflictHandling:
    """Tests for conflict detection in evaluator."""

    @pytest.mark.asyncio
    async def test_conflict_penalty(self, mock_memory_manager):
        """Conflicts should reduce quality score."""
        evaluator = create_evaluator_node(memory_manager=mock_memory_manager)
        state = {"draft": "Response.", "iteration": 1}

        with patch("daem0nmcp.reflexion.nodes.verify_claims") as mock_verify:
            mock_verify.return_value = []
            with patch(
                "daem0nmcp.reflexion.nodes.summarize_verification"
            ) as mock_summary:
                mock_summary.return_value = {
                    "verified_count": 1,
                    "unverified_count": 0,
                    "conflict_count": 2,  # Two conflicts
                    "overall_confidence": 0.8,
                    "conflicts": [
                        {"claim": "x", "reason": "Conflict 1"},
                        {"claim": "y", "reason": "Conflict 2"},
                    ],
                }
                result = await evaluator(state)

        # Two conflicts at 0.2 penalty each = 0.4 penalty
        # 0.8 base - 0.4 = 0.4 max score
        assert result["quality_score"] <= 0.4

    @pytest.mark.asyncio
    async def test_conflict_in_critique(self, mock_memory_manager):
        """Conflicts should appear in critique."""
        evaluator = create_evaluator_node(memory_manager=mock_memory_manager)
        state = {"draft": "Response.", "iteration": 1}

        with patch("daem0nmcp.reflexion.nodes.verify_claims") as mock_verify:
            mock_verify.return_value = []
            with patch(
                "daem0nmcp.reflexion.nodes.summarize_verification"
            ) as mock_summary:
                mock_summary.return_value = {
                    "verified_count": 0,
                    "unverified_count": 0,
                    "conflict_count": 1,
                    "overall_confidence": 0.5,
                    "conflicts": [{"claim": "test", "reason": "Test conflict"}],
                }
                result = await evaluator(state)

        assert "CONFLICTS DETECTED" in result["critique"]
        assert "Test conflict" in result["critique"]


class TestIntegrationGraph:
    """Integration tests for the compiled graph."""

    @pytest.mark.asyncio
    async def test_create_app_without_checkpoint(self, mock_memory_manager):
        """Should create app without checkpointing."""
        app = await create_reflexion_app(memory_manager=mock_memory_manager)

        # App should be compiled and callable
        assert app is not None

    @pytest.mark.asyncio
    async def test_run_reflexion_basic(self, mock_memory_manager):
        """Basic run_reflexion should work."""
        with patch("daem0nmcp.reflexion.graph.create_reflexion_app") as mock_create:
            mock_app = AsyncMock()
            mock_app.ainvoke = AsyncMock(
                return_value={
                    "query": "test",
                    "draft": "Response",
                    "quality_score": 0.9,
                    "should_continue": False,
                }
            )
            mock_create.return_value = mock_app

            result = await run_reflexion(
                query="test query",
                memory_manager=mock_memory_manager,
            )

        assert "draft" in result
        assert "quality_score" in result
