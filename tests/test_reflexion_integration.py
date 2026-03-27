"""Integration tests for Reflexion Metacognitive Architecture.

These tests exercise the full reflexion loop, verifying that all components
work together: claim extraction -> verification -> persistence -> consolidation.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from daem0nmcp.reflexion import (
    MAX_ITERATIONS,
    # Nodes
    QUALITY_THRESHOLD_EXIT,
    Reflection,
    # Graph
    build_reflexion_graph,
    # Consolidation
    consolidate_reflections,
    # State
    extract_claims,
    has_seen_error_before,
    # Persistence
    persist_reflection,
    retrieve_similar_reflections,
    run_reflexion,
    summarize_verification,
    verify_claims,
)


@pytest.fixture
def mock_memory_manager():
    """Create a realistic mock MemoryManager."""
    manager = AsyncMock()

    # Mock remember to return incrementing IDs
    remember_id = [0]

    async def mock_remember(**kwargs):
        remember_id[0] += 1
        return {"id": remember_id[0]}

    manager.remember = AsyncMock(side_effect=mock_remember)

    # Default recall behavior - overridden in specific tests
    async def mock_recall(topic=None, categories=None, tags=None, limit=10, **kwargs):
        # Simulate finding memories based on topic
        if topic and "postgresql" in topic.lower():
            return {
                "memories": [
                    {
                        "id": 1,
                        "content": "We decided to use PostgreSQL for the database.",
                        "category": "decision",
                        "tags": ["database", "postgresql"],
                    }
                ]
            }
        return {"memories": []}

    manager.recall = AsyncMock(side_effect=mock_recall)
    manager.link_memories = AsyncMock()

    return manager


@pytest.fixture
def mock_knowledge_graph():
    """Create a mock KnowledgeGraph."""
    kg = AsyncMock()
    kg.ensure_loaded = AsyncMock()
    kg._graph = MagicMock()
    kg._graph.nodes.return_value = []
    kg._graph.predecessors = MagicMock(return_value=[])
    return kg


class TestClaimExtractionToVerification:
    """Tests for claim extraction -> verification pipeline."""

    @pytest.mark.asyncio
    async def test_memory_claim_verification_pipeline(self, mock_memory_manager):
        """Test full pipeline: extract claim -> verify against memory."""
        text = "We decided to use PostgreSQL for our database."

        # Extract claims
        claims = extract_claims(text)
        assert len(claims) >= 1

        # Verify claims
        with (
            patch("daem0nmcp.reflexion.verification.encode_query") as mock_encode,
            patch(
                "daem0nmcp.reflexion.verification.encode_document"
            ) as mock_encode_doc,
        ):
            with patch("daem0nmcp.reflexion.verification.decode") as mock_decode:
                with patch(
                    "daem0nmcp.reflexion.verification.cosine_similarity"
                ) as mock_sim:
                    mock_encode.return_value = b"embedding"
                    mock_encode_doc.return_value = b"embedding"
                    mock_decode.return_value = [0.1] * 256
                    mock_sim.return_value = 0.85

                    results = await verify_claims(
                        claims=claims,
                        memory_manager=mock_memory_manager,
                    )

        # Should have verification results
        assert len(results) == len(claims)

        # PostgreSQL claim should be verified (we have matching memory)
        pg_result = next(
            (r for r in results if "postgresql" in r.claim_text.lower()), None
        )
        assert pg_result is not None
        assert pg_result.status == "verified"

    @pytest.mark.asyncio
    async def test_unverified_claim_no_evidence(self, mock_memory_manager):
        """Claims with no supporting memories should be unverified."""
        text = "We decided to use MongoDB for caching."

        claims = extract_claims(text)
        assert len(claims) >= 1

        # Mock recall to return nothing for MongoDB topic
        mock_memory_manager.recall = AsyncMock(return_value={"memories": []})

        results = await verify_claims(
            claims=claims,
            memory_manager=mock_memory_manager,
        )

        # Should be unverified (no matching memories)
        assert results[0].status == "unverified"
        assert results[0].confidence < 0.5

    @pytest.mark.asyncio
    async def test_summary_aggregates_results(self, mock_memory_manager):
        """Summarize verification should aggregate verified/unverified/conflict counts."""
        text = "We decided to use PostgreSQL. We also mentioned testing."

        claims = extract_claims(text)

        with (
            patch("daem0nmcp.reflexion.verification.encode_query") as mock_encode,
            patch(
                "daem0nmcp.reflexion.verification.encode_document"
            ) as mock_encode_doc,
        ):
            with patch("daem0nmcp.reflexion.verification.decode") as mock_decode:
                with patch(
                    "daem0nmcp.reflexion.verification.cosine_similarity"
                ) as mock_sim:
                    mock_encode.return_value = b"embedding"
                    mock_encode_doc.return_value = b"embedding"
                    mock_decode.return_value = [0.1] * 256
                    mock_sim.return_value = 0.85

                    results = await verify_claims(
                        claims=claims,
                        memory_manager=mock_memory_manager,
                    )

        summary = summarize_verification(results)

        assert "verified_count" in summary
        assert "unverified_count" in summary
        assert "conflict_count" in summary
        assert "overall_confidence" in summary
        assert 0.0 <= summary["overall_confidence"] <= 1.0


class TestFullReflexionLoop:
    """Tests for complete Actor-Evaluator-Reflector loop."""

    @pytest.mark.asyncio
    async def test_full_reflexion_loop(self, mock_memory_manager, mock_knowledge_graph):
        """Test complete reflexion loop execution."""
        # Build the graph
        graph = build_reflexion_graph(
            memory_manager=mock_memory_manager,
            knowledge_graph=mock_knowledge_graph,
            llm_func=None,  # Use placeholder
        )

        # Compile and run
        app = graph.compile()

        initial_state = {
            "query": "What database should we use?",
            "draft": "",
            "critique": "",
            "quality_score": 0.0,
            "claims": [],
            "verification_results": [],
            "iteration": 0,
            "should_continue": True,
            "context_filter": None,
        }

        with patch("daem0nmcp.reflexion.nodes.verify_claims") as mock_verify:
            with patch(
                "daem0nmcp.reflexion.nodes.summarize_verification"
            ) as mock_summary:
                # High quality on first try -> should exit early
                mock_verify.return_value = []
                mock_summary.return_value = {
                    "verified_count": 0,
                    "unverified_count": 0,
                    "conflict_count": 0,
                    "overall_confidence": 0.9,
                    "conflicts": [],
                }

                result = await app.ainvoke(initial_state)

        # Should have a draft
        assert result["draft"]

        # Should have iterated at least once
        assert result["iteration"] >= 1

        # High quality should have caused early exit
        assert (
            result["quality_score"] >= QUALITY_THRESHOLD_EXIT
            or result["iteration"] >= MAX_ITERATIONS
        )

    @pytest.mark.asyncio
    async def test_loop_respects_max_iterations(
        self, mock_memory_manager, mock_knowledge_graph
    ):
        """Test that loop exits after MAX_ITERATIONS."""
        graph = build_reflexion_graph(
            memory_manager=mock_memory_manager,
            knowledge_graph=mock_knowledge_graph,
            llm_func=None,
        )
        app = graph.compile()

        initial_state = {
            "query": "Complex question requiring iteration",
            "draft": "",
            "critique": "",
            "quality_score": 0.0,
            "claims": [],
            "verification_results": [],
            "iteration": 0,
            "should_continue": True,
            "context_filter": None,
        }

        with patch("daem0nmcp.reflexion.nodes.verify_claims") as mock_verify:
            with patch(
                "daem0nmcp.reflexion.nodes.summarize_verification"
            ) as mock_summary:
                # Always return low quality -> should hit max iterations
                mock_verify.return_value = []
                mock_summary.return_value = {
                    "verified_count": 0,
                    "unverified_count": 2,
                    "conflict_count": 0,
                    "overall_confidence": 0.3,
                    "conflicts": [],
                }

                result = await app.ainvoke(initial_state)

        # Should have hit max iterations
        assert result["iteration"] <= MAX_ITERATIONS
        # Loop should have stopped
        assert result["should_continue"] is False

    @pytest.mark.asyncio
    async def test_run_reflexion_helper(
        self, mock_memory_manager, mock_knowledge_graph
    ):
        """Test run_reflexion convenience function."""
        with patch("daem0nmcp.reflexion.nodes.verify_claims") as mock_verify:
            with patch(
                "daem0nmcp.reflexion.nodes.summarize_verification"
            ) as mock_summary:
                mock_verify.return_value = []
                mock_summary.return_value = {
                    "verified_count": 0,
                    "unverified_count": 0,
                    "conflict_count": 0,
                    "overall_confidence": 0.95,
                    "conflicts": [],
                }

                result = await run_reflexion(
                    query="What is the best approach?",
                    memory_manager=mock_memory_manager,
                    knowledge_graph=mock_knowledge_graph,
                )

        # Should return a valid state
        assert "draft" in result
        assert "iteration" in result
        assert "quality_score" in result


class TestReflectionPersistenceIntegration:
    """Tests for reflection persistence integration."""

    @pytest.mark.asyncio
    async def test_persist_and_retrieve_reflection(self, mock_memory_manager):
        """Test persisting a reflection and retrieving it later."""
        # Create a reflection
        reflection = Reflection(
            error_type="conflict",
            error_signature="test123",
            content="Detected conflict about database choice",
            context="Database discussion",
            query="What database?",
            iteration=2,
            quality_delta=0.2,
        )

        # Persist it
        memory_id = await persist_reflection(
            reflection=reflection,
            memory_manager=mock_memory_manager,
            changed_behavior=True,
        )

        assert memory_id is not None

        # Check it was stored with correct parameters
        call_args = mock_memory_manager.remember.call_args
        assert call_args.kwargs["category"] == "reflection"
        assert "conflict" in call_args.kwargs["tags"]
        assert "sig:test123" in call_args.kwargs["tags"]

    @pytest.mark.asyncio
    async def test_retrieve_similar_reflections(self, mock_memory_manager):
        """Test retrieving similar reflections by signature."""

        # Override recall with new side_effect that returns reflections
        async def mock_recall_reflections(**kwargs):
            return {
                "memories": [
                    {
                        "id": 1,
                        "content": "Reflection about conflict",
                        "tags": ["reflection", "sig:db123"],
                    },
                    {
                        "id": 2,
                        "content": "Another reflection",
                        "tags": ["reflection", "sig:db123"],
                    },
                ]
            }

        mock_memory_manager.recall = AsyncMock(side_effect=mock_recall_reflections)

        reflections = await retrieve_similar_reflections(
            error_signature="db123",
            memory_manager=mock_memory_manager,
            limit=5,
        )

        assert len(reflections) == 2
        mock_memory_manager.recall.assert_called()

    @pytest.mark.asyncio
    async def test_has_seen_error_before(self, mock_memory_manager):
        """Test checking if error has been seen before."""

        # Override recall with new side_effect that returns a matching reflection
        async def mock_recall_match(**kwargs):
            return {
                "memories": [
                    {
                        "id": 1,
                        "content": "Previous error reflection",
                        "tags": ["reflection", "sig:known_error"],
                    }
                ]
            }

        mock_memory_manager.recall = AsyncMock(side_effect=mock_recall_match)

        seen = await has_seen_error_before(
            error_signature="known_error",
            memory_manager=mock_memory_manager,
        )

        assert seen is True

    @pytest.mark.asyncio
    async def test_has_not_seen_error_before(self, mock_memory_manager):
        """Test checking if error has NOT been seen before."""
        # Override recall with new side_effect that returns empty
        mock_memory_manager.recall = AsyncMock(return_value={"memories": []})

        seen = await has_seen_error_before(
            error_signature="new_error",
            memory_manager=mock_memory_manager,
        )

        assert seen is False


class TestConsolidationIntegration:
    """Tests for consolidation integration."""

    @pytest.mark.asyncio
    async def test_consolidation_creates_pattern(self, mock_memory_manager):
        """Test that consolidation creates pattern memory."""

        # Override recall to return 6 reflections (above threshold)
        async def mock_recall_reflections(**kwargs):
            return {
                "memories": [
                    {
                        "id": i,
                        "content": f"Reflection {i} about database conflict",
                        "tags": ["reflection", "conflict", "sig:db123"],
                    }
                    for i in range(1, 7)
                ]
            }

        mock_memory_manager.recall = AsyncMock(side_effect=mock_recall_reflections)

        pattern_id = await consolidate_reflections(
            error_signature="db123",
            memory_manager=mock_memory_manager,
            consolidation_threshold=5,
        )

        assert pattern_id is not None

        # Verify pattern was created
        mock_memory_manager.remember.assert_called_once()
        call_args = mock_memory_manager.remember.call_args
        assert call_args.kwargs["category"] == "pattern"
        assert "consolidated" in call_args.kwargs["tags"]

    @pytest.mark.asyncio
    async def test_consolidation_below_threshold_returns_none(
        self, mock_memory_manager
    ):
        """Consolidation should not create pattern below threshold."""

        # Override recall to return only 3 reflections (below threshold of 5)
        async def mock_recall_few(**kwargs):
            return {
                "memories": [
                    {
                        "id": i,
                        "content": f"Reflection {i}",
                        "tags": ["reflection", "sig:db123"],
                    }
                    for i in range(1, 4)
                ]
            }

        mock_memory_manager.recall = AsyncMock(side_effect=mock_recall_few)

        pattern_id = await consolidate_reflections(
            error_signature="db123",
            memory_manager=mock_memory_manager,
            consolidation_threshold=5,
        )

        assert pattern_id is None
        mock_memory_manager.remember.assert_not_called()

    @pytest.mark.asyncio
    async def test_consolidation_links_episodic_to_semantic(self, mock_memory_manager):
        """Consolidation should link episodic reflections to semantic pattern."""

        # Override recall to return 5 reflections (at threshold)
        async def mock_recall_reflections(**kwargs):
            return {
                "memories": [
                    {
                        "id": i,
                        "content": f"Reflection {i} about timeout error",
                        "tags": ["reflection", "sig:timeout123"],
                    }
                    for i in range(1, 6)
                ]
            }

        mock_memory_manager.recall = AsyncMock(side_effect=mock_recall_reflections)

        pattern_id = await consolidate_reflections(
            error_signature="timeout123",
            memory_manager=mock_memory_manager,
            consolidation_threshold=5,
        )

        assert pattern_id is not None

        # Should have called link_memories if method exists
        if hasattr(mock_memory_manager, "link_memories"):
            # Pattern should be linked to episodic reflections
            call_count = mock_memory_manager.link_memories.call_count
            # One link call per episodic reflection
            assert call_count >= 5


class TestVerifyFactsToolIntegration:
    """Tests for verify_facts MCP tool (via direct import)."""

    @pytest.mark.asyncio
    async def test_verify_facts_returns_structure(self, mock_memory_manager):
        """Test verify_facts returns expected structure."""
        text = "We decided to use PostgreSQL. The system is fast."

        claims = extract_claims(text)
        results = await verify_claims(
            claims=claims,
            memory_manager=mock_memory_manager,
        )
        summary = summarize_verification(results)

        # Should have proper structure
        assert "verified_count" in summary
        assert "unverified_count" in summary
        assert "conflict_count" in summary
        assert "overall_confidence" in summary

    @pytest.mark.asyncio
    async def test_end_to_end_verification_flow(self, mock_memory_manager):
        """Test complete end-to-end flow: text -> claims -> verify -> summarize."""
        # Input text with multiple claim types
        text = "We decided to use PostgreSQL for the database. Previously, we mentioned MySQL."

        # Step 1: Extract claims
        claims = extract_claims(text)
        assert len(claims) >= 1, "Should extract at least one claim"

        # Step 2: Verify claims
        with (
            patch("daem0nmcp.reflexion.verification.encode_query") as mock_encode,
            patch(
                "daem0nmcp.reflexion.verification.encode_document"
            ) as mock_encode_doc,
        ):
            with patch("daem0nmcp.reflexion.verification.decode") as mock_decode:
                with patch(
                    "daem0nmcp.reflexion.verification.cosine_similarity"
                ) as mock_sim:
                    mock_encode.return_value = b"embedding"
                    mock_encode_doc.return_value = b"embedding"
                    mock_decode.return_value = [0.1] * 256
                    mock_sim.return_value = 0.75

                    results = await verify_claims(
                        claims=claims,
                        memory_manager=mock_memory_manager,
                    )

        assert len(results) == len(claims)

        # Step 3: Summarize
        summary = summarize_verification(results)

        # Step 4: Verify structure
        total = (
            summary["verified_count"]
            + summary["unverified_count"]
            + summary["conflict_count"]
        )
        assert total == len(claims)

    @pytest.mark.asyncio
    async def test_no_claims_returns_empty_structure(self, mock_memory_manager):
        """Text with no claims should return empty structure."""
        text = "Just some random text without any claims."

        claims = extract_claims(text)

        # If no claims, verification should handle gracefully
        if len(claims) == 0:
            summary = summarize_verification([])
            assert summary["verified_count"] == 0
            assert summary["unverified_count"] == 0
            assert summary["conflict_count"] == 0


class TestGraphRAGIntegration:
    """Tests for GraphRAG integration in verification."""

    @pytest.mark.asyncio
    async def test_verification_with_knowledge_graph(
        self, mock_memory_manager, mock_knowledge_graph
    ):
        """Test verification uses knowledge graph when available."""
        text = "PostgreSQL is a database."

        claims = extract_claims(text)

        # Setup mock graph with entity
        mock_knowledge_graph._graph.nodes.return_value = ["entity:postgresql"]
        mock_knowledge_graph._graph.nodes.__getitem__ = lambda self, key: {
            "name": "PostgreSQL"
        }

        await verify_claims(
            claims=claims,
            memory_manager=mock_memory_manager,
            knowledge_graph=mock_knowledge_graph,
        )

        # Should have called ensure_loaded on knowledge graph
        mock_knowledge_graph.ensure_loaded.assert_called()

    @pytest.mark.asyncio
    async def test_verification_without_knowledge_graph(self, mock_memory_manager):
        """Test verification works without knowledge graph."""
        text = "We decided to use Redis for caching."

        claims = extract_claims(text)

        # Verify without knowledge graph
        results = await verify_claims(
            claims=claims,
            memory_manager=mock_memory_manager,
            knowledge_graph=None,
        )

        # Should still return results
        assert len(results) == len(claims)
