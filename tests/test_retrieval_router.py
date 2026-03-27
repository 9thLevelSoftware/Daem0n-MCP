"""
Tests for Auto-Zoom RetrievalRouter.

All tests mock the MemoryManager, KnowledgeGraph, and classifier to avoid
needing real databases or embedding models.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from daem0nmcp.recall_planner import QueryComplexity
from daem0nmcp.retrieval_router import RetrievalRouter

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_mock_mm(hybrid_results=None, qdrant=None):
    """Return a mock MemoryManager with _hybrid_search and optional _qdrant."""
    mm = MagicMock()
    mm._hybrid_search.return_value = hybrid_results or [(1, 0.9), (2, 0.7)]
    mm._qdrant = qdrant
    mm._knowledge_graph = (
        None  # Prevent MagicMock auto-attribute from shadowing router's self._kg
    )
    mm.db = MagicMock()
    mm.db.storage_path = "/tmp/test"
    return mm


def _make_mock_classifier(level=QueryComplexity.MEDIUM, confidence=0.8):
    """Return a mock ExemplarQueryClassifier."""
    clf = MagicMock()
    clf.classify.return_value = (
        level,
        confidence,
        {"simple": 0.3, "medium": 0.5, "complex": 0.2},
    )
    return clf


def _make_mock_kg(node_count=10, related=None):
    """Return a mock KnowledgeGraph."""
    kg = AsyncMock()
    kg.ensure_loaded = AsyncMock()
    graph = MagicMock()
    graph.number_of_nodes.return_value = node_count
    kg._graph = graph
    return kg


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestShadowMode:
    """Shadow mode logs classification without changing retrieval behavior."""

    @pytest.mark.asyncio
    async def test_shadow_mode_logs_classification(self):
        """Shadow mode returns hybrid results with shadow_classification."""
        mm = _make_mock_mm(hybrid_results=[(10, 0.95)])
        clf = _make_mock_classifier(QueryComplexity.SIMPLE, 0.85)

        router = RetrievalRouter(memory_manager=mm, classifier=clf)

        with patch("daem0nmcp.retrieval_router.settings") as mock_settings:
            mock_settings.auto_zoom_shadow = True
            mock_settings.auto_zoom_enabled = False
            mock_settings.auto_zoom_confidence_threshold = 0.25

            result = await router.route_search("test query")

        assert result["strategy_used"] == "hybrid"
        assert result["shadow_classification"] is not None
        assert result["shadow_classification"]["level"] == "simple"
        assert result["shadow_classification"]["confidence"] == 0.85
        clf.classify.assert_called_once_with("test query")


class TestActiveMode:
    """Active mode dispatches to the correct strategy."""

    @pytest.mark.asyncio
    async def test_simple_routes_vector_only(self):
        """SIMPLE classification routes to vector-only search."""
        qdrant = MagicMock()
        qdrant.search.return_value = [(5, 0.88)]
        mm = _make_mock_mm(qdrant=qdrant)
        clf = _make_mock_classifier(QueryComplexity.SIMPLE, 0.9)

        router = RetrievalRouter(memory_manager=mm, classifier=clf)

        with (
            patch("daem0nmcp.retrieval_router.settings") as mock_settings,
            patch("daem0nmcp.retrieval_router.vectors") as mock_vectors,
        ):
            mock_settings.auto_zoom_enabled = True
            mock_settings.auto_zoom_shadow = False
            mock_settings.auto_zoom_confidence_threshold = 0.25
            mock_vectors.encode.return_value = b"\x00" * 16
            mock_vectors.decode.return_value = [0.1, 0.2, 0.3]

            result = await router.route_search("what is X")

        assert result["strategy_used"] == "vector_only"
        assert result["results"] == [(5, 0.88)]
        qdrant.search.assert_called_once()

    @pytest.mark.asyncio
    async def test_medium_routes_hybrid(self):
        """MEDIUM classification routes to hybrid search."""
        mm = _make_mock_mm(hybrid_results=[(3, 0.75)])
        clf = _make_mock_classifier(QueryComplexity.MEDIUM, 0.7)

        router = RetrievalRouter(memory_manager=mm, classifier=clf)

        with patch("daem0nmcp.retrieval_router.settings") as mock_settings:
            mock_settings.auto_zoom_enabled = True
            mock_settings.auto_zoom_shadow = False
            mock_settings.auto_zoom_confidence_threshold = 0.25

            result = await router.route_search("how does X relate to Y")

        assert result["strategy_used"] == "hybrid"
        assert result["results"] == [(3, 0.75)]

    @pytest.mark.asyncio
    async def test_complex_routes_graphrag(self):
        """COMPLEX classification routes to GraphRAG search."""
        mm = _make_mock_mm(hybrid_results=[(1, 0.9), (2, 0.7)])
        clf = _make_mock_classifier(QueryComplexity.COMPLEX, 0.85)
        kg = _make_mock_kg(node_count=50)

        router = RetrievalRouter(memory_manager=mm, knowledge_graph=kg, classifier=clf)

        with (
            patch("daem0nmcp.retrieval_router.settings") as mock_settings,
            patch(
                "daem0nmcp.graph.traversal.find_related_memories",
                new_callable=AsyncMock,
                return_value={
                    "found": True,
                    "source_memory_id": 1,
                    "total_related": 1,
                    "by_relationship": {
                        "led_to": [{"memory_id": 99, "depth": 1, "confidence": 0.9}]
                    },
                },
            ) as _mock_find,
        ):  # noqa: F841
            mock_settings.auto_zoom_enabled = True
            mock_settings.auto_zoom_shadow = False
            mock_settings.auto_zoom_confidence_threshold = 0.25
            mock_settings.auto_zoom_graph_expansion_depth = 2

            result = await router.route_search("trace the causal chain")

        assert result["strategy_used"] == "graphrag"
        # Should contain both seed results and expanded results
        result_ids = [r[0] for r in result["results"]]
        assert 1 in result_ids
        assert 99 in result_ids


class TestFallbackPaths:
    """All failure paths fall back to hybrid search."""

    @pytest.mark.asyncio
    async def test_vector_only_fallback_no_qdrant(self):
        """SIMPLE with no Qdrant falls back to hybrid."""
        mm = _make_mock_mm(hybrid_results=[(7, 0.6)], qdrant=None)
        clf = _make_mock_classifier(QueryComplexity.SIMPLE, 0.9)

        router = RetrievalRouter(memory_manager=mm, classifier=clf)

        with patch("daem0nmcp.retrieval_router.settings") as mock_settings:
            mock_settings.auto_zoom_enabled = True
            mock_settings.auto_zoom_shadow = False
            mock_settings.auto_zoom_confidence_threshold = 0.25

            result = await router.route_search("simple query")

        # Falls back to hybrid since no Qdrant
        assert result["strategy_used"] == "vector_only"
        assert result["results"] == [(7, 0.6)]
        mm._hybrid_search.assert_called()

    @pytest.mark.asyncio
    async def test_graphrag_fallback_empty_graph(self):
        """COMPLEX with empty KnowledgeGraph uses hybrid seeds only."""
        mm = _make_mock_mm(hybrid_results=[(1, 0.8)])
        clf = _make_mock_classifier(QueryComplexity.COMPLEX, 0.85)
        kg = _make_mock_kg(node_count=0)

        router = RetrievalRouter(memory_manager=mm, knowledge_graph=kg, classifier=clf)

        with patch("daem0nmcp.retrieval_router.settings") as mock_settings:
            mock_settings.auto_zoom_enabled = True
            mock_settings.auto_zoom_shadow = False
            mock_settings.auto_zoom_confidence_threshold = 0.25

            result = await router.route_search("trace history of changes")

        assert result["strategy_used"] == "graphrag"
        # Should still have seed results from hybrid
        assert result["results"] == [(1, 0.8)]

    @pytest.mark.asyncio
    async def test_graphrag_fallback_on_exception(self):
        """COMPLEX with KnowledgeGraph exception falls back to hybrid seeds."""
        mm = _make_mock_mm(hybrid_results=[(1, 0.8)])
        clf = _make_mock_classifier(QueryComplexity.COMPLEX, 0.85)
        kg = AsyncMock()
        kg.ensure_loaded = AsyncMock(side_effect=RuntimeError("DB connection lost"))

        router = RetrievalRouter(memory_manager=mm, knowledge_graph=kg, classifier=clf)

        with patch("daem0nmcp.retrieval_router.settings") as mock_settings:
            mock_settings.auto_zoom_enabled = True
            mock_settings.auto_zoom_shadow = False
            mock_settings.auto_zoom_confidence_threshold = 0.25

            result = await router.route_search("complex query")

        assert result["strategy_used"] == "graphrag"
        assert result["results"] == [(1, 0.8)]


class TestDisabledMode:
    """Both auto_zoom_enabled=False and auto_zoom_shadow=False."""

    @pytest.mark.asyncio
    async def test_disabled_mode_skips_classification(self):
        """When both flags are False, classifier is never called."""
        mm = _make_mock_mm(hybrid_results=[(4, 0.5)])
        clf = _make_mock_classifier()

        router = RetrievalRouter(memory_manager=mm, classifier=clf)

        with patch("daem0nmcp.retrieval_router.settings") as mock_settings:
            mock_settings.auto_zoom_enabled = False
            mock_settings.auto_zoom_shadow = False

            result = await router.route_search("anything")

        clf.classify.assert_not_called()
        assert result["strategy_used"] == "hybrid"
        assert result["classification"] is None


class TestReturnStructure:
    """Verify the return dict shape."""

    @pytest.mark.asyncio
    async def test_route_search_returns_correct_structure(self):
        """Returned dict has required keys."""
        mm = _make_mock_mm()
        clf = _make_mock_classifier()

        router = RetrievalRouter(memory_manager=mm, classifier=clf)

        with patch("daem0nmcp.retrieval_router.settings") as mock_settings:
            mock_settings.auto_zoom_enabled = True
            mock_settings.auto_zoom_shadow = False
            mock_settings.auto_zoom_confidence_threshold = 0.25

            result = await router.route_search("query")

        assert "results" in result
        assert "strategy_used" in result
        assert "classification" in result
        assert result["strategy_used"] in ("vector_only", "hybrid", "graphrag")
        assert isinstance(result["results"], list)

    @pytest.mark.asyncio
    async def test_low_confidence_falls_back_to_hybrid(self):
        """Low confidence classification defaults to hybrid."""
        mm = _make_mock_mm(hybrid_results=[(8, 0.4)])
        clf = _make_mock_classifier(QueryComplexity.SIMPLE, confidence=0.1)

        router = RetrievalRouter(memory_manager=mm, classifier=clf)

        with patch("daem0nmcp.retrieval_router.settings") as mock_settings:
            mock_settings.auto_zoom_enabled = True
            mock_settings.auto_zoom_shadow = False
            mock_settings.auto_zoom_confidence_threshold = 0.25

            result = await router.route_search("ambiguous")

        assert result["strategy_used"] == "hybrid"
        assert result["classification"]["level"] == "simple"
        assert result["classification"]["confidence"] == 0.1
