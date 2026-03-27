"""
Integration tests for Phase 15: Auto-Zoom + JIT Compression pipeline.

Validates the complete end-to-end pipeline:
    query -> classify -> route -> retrieve -> JIT compress -> output with metadata

Tests mock at the boundary level (embedding model, database, Qdrant) but let
the real classifier -> router -> JIT logic flow through.

Requirements validated:
- ZOOM-01: Every search query classified
- ZOOM-02: Routed to optimal strategy (vector-only / hybrid / graphrag)
- ZOOM-03: Low-confidence classifications default to hybrid
- ZOOM-04: Shadow mode captures classification without altering behavior
- ZOOM-05: GraphRAG expansion for complex queries
- COMP-01: JIT compression fires at tiered thresholds (4K/8K/16K)
- COMP-02: Dynamic compression rates with sqrt dampening
- COMP-03: Compression metadata in tool output
- COMP-04: Code blocks and entity names survive compression
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from daem0nmcp.compression.jit import JITCompressor
from daem0nmcp.recall_planner import QueryComplexity
from daem0nmcp.retrieval_router import RetrievalRouter

# ---------------------------------------------------------------------------
# Shared helpers
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
    """Return a mock KnowledgeGraph with optional related memories."""
    kg = AsyncMock()
    kg.ensure_loaded = AsyncMock()
    graph = MagicMock()
    graph.number_of_nodes.return_value = node_count
    kg._graph = graph
    return kg


def _make_mock_adaptive(token_count_fn=None):
    """Create a mock AdaptiveCompressor for JIT testing.

    Args:
        token_count_fn: Custom token counting function. Defaults to len(text) // 4.
    """
    mock_adaptive = MagicMock()

    if token_count_fn is None:

        def token_count_fn(text):
            return len(text) // 4

    mock_adaptive.compressor.count_tokens.side_effect = token_count_fn

    def mock_compress(text, rate_override=None, additional_force_tokens=None, **kwargs):
        original = token_count_fn(text)
        rate = rate_override or 0.33
        compressed = max(1, int(original * rate))
        return {
            "compressed_prompt": text[: int(len(text) * rate)],
            "original_tokens": original,
            "compressed_tokens": compressed,
            "ratio": original / max(compressed, 1),
            "content_type": "mixed",
        }

    mock_adaptive.compress.side_effect = mock_compress
    return mock_adaptive


def _settings_context(enabled=False, shadow=False, confidence=0.25, depth=2):
    """Return a patch context for daem0nmcp.retrieval_router.settings."""
    mock_settings = MagicMock()
    mock_settings.auto_zoom_enabled = enabled
    mock_settings.auto_zoom_shadow = shadow
    mock_settings.auto_zoom_confidence_threshold = confidence
    mock_settings.auto_zoom_graph_expansion_depth = depth
    return mock_settings


# ---------------------------------------------------------------------------
# Test 1: Full pipeline with simple query (no compression)
# Validates ZOOM-01, ZOOM-02
# ---------------------------------------------------------------------------


class TestFullPipelineSimpleQuery:
    """Simple query: classify -> vector_only -> retrieve -> no compression."""

    @pytest.mark.asyncio
    async def test_full_pipeline_simple_query(self):
        """Simple query is classified, routed to vector_only, no compression metadata."""
        qdrant = MagicMock()
        qdrant.search.return_value = [
            (5, 0.88),
            (6, 0.75),
            (7, 0.60),
            (8, 0.50),
            (9, 0.45),
        ]
        mm = _make_mock_mm(qdrant=qdrant)
        clf = _make_mock_classifier(QueryComplexity.SIMPLE, 0.9)

        router = RetrievalRouter(memory_manager=mm, classifier=clf)

        with (
            patch(
                "daem0nmcp.retrieval_router.settings", _settings_context(enabled=True)
            ),
            patch("daem0nmcp.retrieval_router.vectors") as mock_vectors,
        ):
            mock_vectors.encode.return_value = b"\x00" * 16
            mock_vectors.decode.return_value = [0.1, 0.2, 0.3]

            # Short text (below 4K tokens) - no compression expected
            result = await router.route_and_compress(
                "what is X", top_k=10, result_text="Short result text"
            )

        assert result["strategy_used"] == "vector_only"
        assert result["classification"]["level"] == "simple"
        # No compression metadata since text is below threshold
        assert result.get("compression_metadata") is None


# ---------------------------------------------------------------------------
# Test 2: Full pipeline with complex query + compression
# Validates ZOOM-01, ZOOM-02, ZOOM-05, COMP-01, COMP-03
# ---------------------------------------------------------------------------


class TestFullPipelineComplexQueryWithCompression:
    """Complex query: classify -> graphrag -> retrieve -> JIT compress."""

    @pytest.mark.asyncio
    async def test_full_pipeline_complex_query_with_compression(self):
        """Complex query triggers graphrag strategy and JIT compression on large results."""
        mm = _make_mock_mm(hybrid_results=[(1, 0.9), (2, 0.7)])
        clf = _make_mock_classifier(QueryComplexity.COMPLEX, 0.85)
        kg = _make_mock_kg(node_count=50)

        router = RetrievalRouter(memory_manager=mm, knowledge_graph=kg, classifier=clf)

        mock_adaptive = _make_mock_adaptive()
        jit = JITCompressor(adaptive_compressor=mock_adaptive)

        # Large text that exceeds hard threshold (>8K tokens = >32K chars at 4:1)
        large_text = (
            "Memory content about complex architecture decisions. " * 700
        )  # ~35K chars = ~8750 tokens

        with (
            patch(
                "daem0nmcp.retrieval_router.settings", _settings_context(enabled=True)
            ),
            patch(
                "daem0nmcp.graph.traversal.find_related_memories",
                new_callable=AsyncMock,
                return_value={
                    "found": True,
                    "source_memory_id": 1,
                    "total_related": 2,
                    "by_relationship": {
                        "led_to": [
                            {"memory_id": 99, "depth": 1, "confidence": 0.9},
                            {"memory_id": 100, "depth": 2, "confidence": 0.7},
                        ]
                    },
                },
            ),
            patch("daem0nmcp.compression.jit.get_jit_compressor", return_value=jit),
        ):
            result = await router.route_and_compress(
                "trace the complete causal chain of auth decisions",
                top_k=10,
                result_text=large_text,
            )

        assert result["strategy_used"] == "graphrag"
        # Graph expansion should have added results
        result_ids = [r[0] for r in result["results"]]
        assert 1 in result_ids
        assert 99 in result_ids
        # Compression metadata present
        assert result["compression_metadata"] is not None
        assert result["compression_metadata"]["threshold_triggered"] == "hard"
        assert (
            result["compression_metadata"]["original_tokens"]
            > result["compression_metadata"]["compressed_tokens"]
        )
        assert "compressed_text" in result


# ---------------------------------------------------------------------------
# Test 3: Shadow mode
# Validates ZOOM-04, COMP-01
# ---------------------------------------------------------------------------


class TestFullPipelineShadowMode:
    """Shadow mode: classify and log, but always use hybrid; JIT still fires."""

    @pytest.mark.asyncio
    async def test_full_pipeline_shadow_mode(self):
        """Shadow mode logs classification without changing routing; JIT fires independently."""
        mm = _make_mock_mm(hybrid_results=[(10, 0.95), (11, 0.80)])
        clf = _make_mock_classifier(QueryComplexity.COMPLEX, 0.9)

        router = RetrievalRouter(memory_manager=mm, classifier=clf)

        mock_adaptive = _make_mock_adaptive()
        jit = JITCompressor(adaptive_compressor=mock_adaptive)

        # Text above soft threshold
        medium_text = "Some long result content. " * 500  # ~12.5K chars = ~3125 tokens
        # Actually need >4K tokens = >16K chars
        medium_text = (
            "Some long result content for JIT. " * 1300
        )  # ~44K chars = ~11K tokens

        with (
            patch(
                "daem0nmcp.retrieval_router.settings", _settings_context(shadow=True)
            ),
            patch("daem0nmcp.compression.jit.get_jit_compressor", return_value=jit),
        ):
            result = await router.route_and_compress(
                "complex query about architecture",
                top_k=10,
                result_text=medium_text,
            )

        # Shadow mode always uses hybrid
        assert result["strategy_used"] == "hybrid"
        # Shadow classification captured
        assert result.get("shadow_classification") is not None
        assert result["shadow_classification"]["level"] == "complex"
        # JIT compression fires on the result text regardless of routing mode
        assert result["compression_metadata"] is not None
        assert result["compression_metadata"]["threshold_triggered"] in (
            "soft",
            "hard",
            "emergency",
        )


# ---------------------------------------------------------------------------
# Test 4: Compression metadata in recall output
# Validates COMP-01, COMP-03
# ---------------------------------------------------------------------------


class TestCompressionMetadataInRecallOutput:
    """Compression metadata surfaces in recall() output when JIT fires."""

    @pytest.mark.asyncio
    async def test_compression_metadata_in_recall_output(self):
        """JIT compression adds metadata to recall result dict when results are large."""
        mock_adaptive = _make_mock_adaptive()
        jit = JITCompressor(adaptive_compressor=mock_adaptive)

        # Simulate what recall() does: assemble content from categories
        # Each memory has ~800 chars = ~200 tokens; 30 memories = ~6000 tokens (soft tier)
        memory_content = (
            "This is a substantial memory about authentication patterns and decisions. "
            * 11
        )
        memories = [{"content": memory_content, "id": i} for i in range(30)]

        all_content = "\n\n".join(m["content"] for m in memories)
        jit_result = jit.compress_if_needed(all_content)

        assert jit_result["threshold_triggered"] == "soft"
        assert jit_result["original_tokens"] > jit_result["compressed_tokens"]

        # Build metadata as recall() would
        metadata = {
            "original_tokens": jit_result["original_tokens"],
            "compressed_tokens": jit_result["compressed_tokens"],
            "compression_rate": jit_result["compression_rate"],
            "threshold_triggered": jit_result["threshold_triggered"],
        }
        assert metadata["threshold_triggered"] == "soft"
        assert metadata["original_tokens"] > metadata["compressed_tokens"]


# ---------------------------------------------------------------------------
# Test 5: No compression for small results
# Validates COMP-01 (threshold boundary)
# ---------------------------------------------------------------------------


class TestNoCompressionForSmallResults:
    """Small result sets do not trigger compression or add metadata."""

    @pytest.mark.asyncio
    async def test_no_compression_metadata_for_small_results(self):
        """Text below soft threshold produces no compression metadata."""
        mock_adaptive = _make_mock_adaptive()
        jit = JITCompressor(adaptive_compressor=mock_adaptive)

        # Small text: 2000 chars = ~500 tokens (well below 4K soft threshold)
        small_text = "Short memory content. " * 100

        jit_result = jit.compress_if_needed(small_text)

        assert jit_result["threshold_triggered"] is None
        assert jit_result["compression_rate"] == 1.0
        assert jit_result["original_tokens"] == jit_result["compressed_tokens"]

    @pytest.mark.asyncio
    async def test_route_and_compress_small_results(self):
        """route_and_compress with small text returns compression_metadata=None."""
        mm = _make_mock_mm(hybrid_results=[(1, 0.9)])
        clf = _make_mock_classifier(QueryComplexity.MEDIUM, 0.7)

        router = RetrievalRouter(memory_manager=mm, classifier=clf)

        mock_adaptive = _make_mock_adaptive()
        jit = JITCompressor(adaptive_compressor=mock_adaptive)

        with (
            patch(
                "daem0nmcp.retrieval_router.settings", _settings_context(enabled=True)
            ),
            patch("daem0nmcp.compression.jit.get_jit_compressor", return_value=jit),
        ):
            result = await router.route_and_compress(
                "find patterns", top_k=5, result_text="Short text"
            )

        assert result["compression_metadata"] is None


# ---------------------------------------------------------------------------
# Test 6: GraphRAG expansion increases results
# Validates ZOOM-05
# ---------------------------------------------------------------------------


class TestGraphRAGExpansionIncreasesResults:
    """GraphRAG expansion adds related memory IDs from graph traversal."""

    @pytest.mark.asyncio
    async def test_graphrag_expansion_increases_results(self):
        """Graph traversal adds related memories beyond seed results, deduplicated."""
        mm = _make_mock_mm(hybrid_results=[(1, 0.9), (2, 0.7), (3, 0.5)])
        clf = _make_mock_classifier(QueryComplexity.COMPLEX, 0.85)
        kg = _make_mock_kg(node_count=100)

        router = RetrievalRouter(memory_manager=mm, knowledge_graph=kg, classifier=clf)

        with (
            patch(
                "daem0nmcp.retrieval_router.settings", _settings_context(enabled=True)
            ),
            patch(
                "daem0nmcp.graph.traversal.find_related_memories",
                new_callable=AsyncMock,
                return_value={
                    "found": True,
                    "source_memory_id": 1,
                    "total_related": 5,
                    "by_relationship": {
                        "led_to": [
                            {"memory_id": 50, "depth": 1, "confidence": 0.9},
                            {"memory_id": 51, "depth": 1, "confidence": 0.8},
                        ],
                        "depends_on": [
                            {"memory_id": 60, "depth": 1, "confidence": 0.7},
                            {"memory_id": 61, "depth": 2, "confidence": 0.5},
                            {"memory_id": 62, "depth": 2, "confidence": 0.4},
                        ],
                    },
                },
            ),
        ):
            result = await router.route_search("trace causal chain of auth")

        assert result["strategy_used"] == "graphrag"
        result_ids = [r[0] for r in result["results"]]
        # Seed results present
        assert 1 in result_ids
        assert 2 in result_ids
        assert 3 in result_ids
        # Expanded results present
        assert 50 in result_ids
        assert 51 in result_ids
        assert 60 in result_ids
        # More results than seeds alone
        assert len(result["results"]) > 3
        # No duplicates
        assert len(result_ids) == len(set(result_ids))


# ---------------------------------------------------------------------------
# Test 7: Pipeline resilience - classifier failure
# Validates ZOOM-03 (low-confidence fallback extends to error fallback)
# ---------------------------------------------------------------------------


class TestPipelineResilienceClassifierFailure:
    """Classifier exceptions fall back to hybrid search, no crash."""

    @pytest.mark.asyncio
    async def test_pipeline_resilience_classifier_failure(self):
        """Classifier failure causes fallback to hybrid, not a crash."""
        mm = _make_mock_mm(hybrid_results=[(20, 0.6)])
        clf = MagicMock()
        clf.classify.side_effect = RuntimeError("Classifier model not loaded")

        router = RetrievalRouter(memory_manager=mm, classifier=clf)

        with patch(
            "daem0nmcp.retrieval_router.settings", _settings_context(enabled=True)
        ):
            # The router catches classifier errors and falls back to hybrid
            result = await router.route_search("query that breaks classifier")

        # Router catches classifier exception and falls back to hybrid
        assert result["strategy_used"] == "hybrid"
        assert result["results"] == [(20, 0.6)]
        assert result["classification"] is None
        # Verify classifier was called
        clf.classify.assert_called_once_with("query that breaks classifier")
        # Router fell back to hybrid search
        assert result["strategy_used"] == "hybrid"
        assert result["results"] == [(20, 0.6)]
        assert result["classification"] is None

    @pytest.mark.asyncio
    async def test_recall_level_classifier_resilience(self):
        """At the router level, classifier failures fall back to hybrid.

        The router catches classifier exceptions internally and falls back to
        hybrid search, so recall() never needs its own fallback for this case.
        """
        mm = _make_mock_mm(hybrid_results=[(20, 0.6)])
        clf = MagicMock()
        clf.classify.side_effect = RuntimeError("Broken")

        router = RetrievalRouter(memory_manager=mm, classifier=clf)
        with patch(
            "daem0nmcp.retrieval_router.settings", _settings_context(enabled=True)
        ):
            result = await router.route_search("broken query")

        # Router caught the classifier error and fell back to hybrid
        assert result["strategy_used"] == "hybrid"
        assert result["results"] == [(20, 0.6)]
        assert result["classification"] is None


# ---------------------------------------------------------------------------
# Test 8: Pipeline resilience - JIT compression failure
# Validates COMP-01 (compression is enhancement, not gate)
# ---------------------------------------------------------------------------


class TestPipelineResilienceJITFailure:
    """JIT compression failures return results without metadata, no crash."""

    @pytest.mark.asyncio
    async def test_pipeline_resilience_jit_failure(self):
        """JIT failure during route_and_compress returns results without metadata."""
        mm = _make_mock_mm(hybrid_results=[(1, 0.9)])
        clf = _make_mock_classifier(QueryComplexity.MEDIUM, 0.7)

        router = RetrievalRouter(memory_manager=mm, classifier=clf)

        # Create a JIT that always raises
        broken_jit = MagicMock()
        broken_jit.compress_if_needed.side_effect = RuntimeError("LLMLingua not loaded")

        with (
            patch(
                "daem0nmcp.retrieval_router.settings", _settings_context(enabled=True)
            ),
            patch(
                "daem0nmcp.compression.jit.get_jit_compressor", return_value=broken_jit
            ),
        ):
            result = await router.route_and_compress(
                "some query", top_k=10, result_text="x" * 50000
            )

        # Results still returned
        assert result["results"] == [(1, 0.9)]
        assert result["strategy_used"] == "hybrid"
        # Compression metadata is None (failure was caught)
        assert result.get("compression_metadata") is None


# ---------------------------------------------------------------------------
# Test 9: Existing recall behavior preserved when fully disabled
# Validates backward compatibility
# ---------------------------------------------------------------------------


class TestExistingRecallBehaviorPreserved:
    """With auto_zoom fully disabled, no extra keys appear in results."""

    @pytest.mark.asyncio
    async def test_existing_recall_behavior_preserved(self):
        """Disabled auto_zoom produces same output format as pre-Phase-15."""
        mm = _make_mock_mm(hybrid_results=[(4, 0.5)])
        clf = _make_mock_classifier()

        router = RetrievalRouter(memory_manager=mm, classifier=clf)

        with patch(
            "daem0nmcp.retrieval_router.settings",
            _settings_context(enabled=False, shadow=False),
        ):
            result = await router.route_search("anything")

        # No classification performed
        clf.classify.assert_not_called()
        assert result["strategy_used"] == "hybrid"
        assert result["classification"] is None
        # No compression keys should appear from route_search
        assert "compression_metadata" not in result
        assert "compressed_text" not in result

    @pytest.mark.asyncio
    async def test_route_and_compress_disabled_no_compression(self):
        """Disabled auto_zoom with route_and_compress still skips classification."""
        mm = _make_mock_mm(hybrid_results=[(4, 0.5)])
        clf = _make_mock_classifier()

        router = RetrievalRouter(memory_manager=mm, classifier=clf)

        mock_adaptive = _make_mock_adaptive()
        jit = JITCompressor(adaptive_compressor=mock_adaptive)

        with (
            patch(
                "daem0nmcp.retrieval_router.settings",
                _settings_context(enabled=False, shadow=False),
            ),
            patch("daem0nmcp.compression.jit.get_jit_compressor", return_value=jit),
        ):
            result = await router.route_and_compress(
                "query", top_k=5, result_text="Short text"
            )

        # Still no classification
        clf.classify.assert_not_called()
        # Compression metadata may be None (text too small) but structure is stable
        assert result["strategy_used"] == "hybrid"


# ---------------------------------------------------------------------------
# Test 10: Concurrent classify and compress (no shared mutable state)
# Validates pipeline safety under sequential calls
# ---------------------------------------------------------------------------


class TestConcurrentClassifyAndCompress:
    """Multiple sequential pipeline calls produce correct, independent results."""

    @pytest.mark.asyncio
    async def test_concurrent_classify_and_compress(self):
        """Three sequential pipeline calls with different query types all return correct results."""
        mm = _make_mock_mm(hybrid_results=[(1, 0.9)])
        qdrant = MagicMock()
        qdrant.search.return_value = [(10, 0.95)]
        mm._qdrant = qdrant

        mock_adaptive = _make_mock_adaptive()
        jit = JITCompressor(adaptive_compressor=mock_adaptive)

        # Call 1: Simple query
        clf1 = _make_mock_classifier(QueryComplexity.SIMPLE, 0.9)
        router1 = RetrievalRouter(memory_manager=mm, classifier=clf1)

        with (
            patch(
                "daem0nmcp.retrieval_router.settings", _settings_context(enabled=True)
            ),
            patch("daem0nmcp.retrieval_router.vectors") as mock_vectors,
            patch("daem0nmcp.compression.jit.get_jit_compressor", return_value=jit),
        ):
            mock_vectors.encode.return_value = b"\x00" * 16
            mock_vectors.decode.return_value = [0.1, 0.2, 0.3]

            result1 = await router1.route_and_compress("what is X", result_text="short")

        assert result1["strategy_used"] == "vector_only"
        assert result1.get("compression_metadata") is None

        # Call 2: Medium query with large text
        clf2 = _make_mock_classifier(QueryComplexity.MEDIUM, 0.7)
        router2 = RetrievalRouter(memory_manager=mm, classifier=clf2)

        with (
            patch(
                "daem0nmcp.retrieval_router.settings", _settings_context(enabled=True)
            ),
            patch("daem0nmcp.compression.jit.get_jit_compressor", return_value=jit),
        ):
            result2 = await router2.route_and_compress(
                "how does X relate to Y",
                result_text="x" * 40000,  # ~10K tokens = hard threshold
            )

        assert result2["strategy_used"] == "hybrid"
        assert result2["compression_metadata"] is not None
        assert result2["compression_metadata"]["threshold_triggered"] == "hard"

        # Call 3: Complex query with small text
        clf3 = _make_mock_classifier(QueryComplexity.COMPLEX, 0.85)
        kg = _make_mock_kg(node_count=50)
        router3 = RetrievalRouter(
            memory_manager=mm, knowledge_graph=kg, classifier=clf3
        )

        with (
            patch(
                "daem0nmcp.retrieval_router.settings", _settings_context(enabled=True)
            ),
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
            ),
            patch("daem0nmcp.compression.jit.get_jit_compressor", return_value=jit),
        ):
            result3 = await router3.route_and_compress(
                "trace causal chain", result_text="short"
            )

        assert result3["strategy_used"] == "graphrag"
        assert result3.get("compression_metadata") is None  # Text too small

        # All three calls returned independent, correct results
        assert result1["classification"]["level"] == "simple"
        assert result2["classification"]["level"] == "medium"
        assert result3["classification"]["level"] == "complex"


# ---------------------------------------------------------------------------
# Test 11: All requirements smoke test
# Validates ZOOM-01 through ZOOM-05, COMP-01 through COMP-04
# ---------------------------------------------------------------------------


class TestAllRequirementsSmoke:
    """Smoke test exercising all 9 Phase 15 requirements in sequence."""

    @pytest.mark.asyncio
    async def test_all_requirements_smoke(self):
        """Quick smoke test that validates every requirement is met."""
        mm = _make_mock_mm(hybrid_results=[(1, 0.9), (2, 0.7), (3, 0.5)])
        qdrant = MagicMock()
        qdrant.search.return_value = [(10, 0.95)]
        mm._qdrant = qdrant
        kg = _make_mock_kg(node_count=50)

        mock_adaptive = _make_mock_adaptive()
        jit = JITCompressor(adaptive_compressor=mock_adaptive)

        # --- ZOOM-01: Every search query classified ---
        clf = _make_mock_classifier(QueryComplexity.MEDIUM, 0.7)
        router = RetrievalRouter(memory_manager=mm, classifier=clf)

        with patch(
            "daem0nmcp.retrieval_router.settings", _settings_context(enabled=True)
        ):
            result = await router.route_search("test query")
        assert result["classification"] is not None  # ZOOM-01
        assert result["classification"]["level"] in ("simple", "medium", "complex")

        # --- ZOOM-02: Routed to optimal strategy ---
        clf_simple = _make_mock_classifier(QueryComplexity.SIMPLE, 0.9)
        router_simple = RetrievalRouter(memory_manager=mm, classifier=clf_simple)

        with (
            patch(
                "daem0nmcp.retrieval_router.settings", _settings_context(enabled=True)
            ),
            patch("daem0nmcp.retrieval_router.vectors") as mv,
        ):
            mv.encode.return_value = b"\x00" * 16
            mv.decode.return_value = [0.1]
            result = await router_simple.route_search("what is X")
        assert result["strategy_used"] == "vector_only"  # ZOOM-02

        # --- ZOOM-03: Low confidence defaults to hybrid ---
        clf_low = _make_mock_classifier(QueryComplexity.SIMPLE, 0.1)
        router_low = RetrievalRouter(memory_manager=mm, classifier=clf_low)

        with patch(
            "daem0nmcp.retrieval_router.settings", _settings_context(enabled=True)
        ):
            result = await router_low.route_search("ambiguous query")
        assert result["strategy_used"] == "hybrid"  # ZOOM-03

        # --- ZOOM-04: Shadow mode captures classification ---
        clf_shadow = _make_mock_classifier(QueryComplexity.COMPLEX, 0.9)
        router_shadow = RetrievalRouter(memory_manager=mm, classifier=clf_shadow)

        with patch(
            "daem0nmcp.retrieval_router.settings", _settings_context(shadow=True)
        ):
            result = await router_shadow.route_search("complex query")
        assert result["strategy_used"] == "hybrid"
        assert result["shadow_classification"] is not None  # ZOOM-04

        # --- ZOOM-05: GraphRAG expansion for complex queries ---
        clf_complex = _make_mock_classifier(QueryComplexity.COMPLEX, 0.85)
        router_complex = RetrievalRouter(
            memory_manager=mm, knowledge_graph=kg, classifier=clf_complex
        )

        with (
            patch(
                "daem0nmcp.retrieval_router.settings", _settings_context(enabled=True)
            ),
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
            ),
        ):
            result = await router_complex.route_search("trace causal chain")
        assert result["strategy_used"] == "graphrag"  # ZOOM-05
        result_ids = [r[0] for r in result["results"]]
        assert 99 in result_ids  # Graph expansion added results

        # --- COMP-01: JIT fires at tiered thresholds ---
        soft_text = "x" * 20000  # ~5K tokens -> soft
        hard_text = "x" * 40000  # ~10K tokens -> hard
        emerg_text = "x" * 80000  # ~20K tokens -> emergency

        assert (
            jit.compress_if_needed(soft_text)["threshold_triggered"] == "soft"
        )  # COMP-01
        assert (
            jit.compress_if_needed(hard_text)["threshold_triggered"] == "hard"
        )  # COMP-01
        assert (
            jit.compress_if_needed(emerg_text)["threshold_triggered"] == "emergency"
        )  # COMP-01

        # --- COMP-02: Dynamic rates with sqrt dampening ---
        soft_result = jit.compress_if_needed(soft_text)
        hard_result = jit.compress_if_needed(hard_text)
        # Hard tier achieves higher compression ratio (more aggressive = higher ratio)
        # compression_rate = original_tokens / compressed_tokens (rounded)
        assert (
            hard_result["compression_rate"] > soft_result["compression_rate"]
        )  # COMP-02

        # --- COMP-03: Compression metadata in output ---
        router_comp = RetrievalRouter(memory_manager=mm, classifier=clf)

        with (
            patch(
                "daem0nmcp.retrieval_router.settings", _settings_context(enabled=True)
            ),
            patch("daem0nmcp.compression.jit.get_jit_compressor", return_value=jit),
        ):
            result = await router_comp.route_and_compress(
                "query", result_text=hard_text
            )
        assert result["compression_metadata"] is not None  # COMP-03
        assert "original_tokens" in result["compression_metadata"]
        assert "compressed_tokens" in result["compression_metadata"]
        assert "compression_rate" in result["compression_metadata"]
        assert "threshold_triggered" in result["compression_metadata"]

        # --- COMP-04: Code blocks and entity names survive compression ---
        # This is validated by the existence of AdaptiveCompressor +
        # CodeEntityPreserver integration, tested in test_compression_integration.py.
        # Here we verify the JIT compressor passes force_tokens through.
        code_text = "def my_function():\n    pass\n" * 2000  # ~15K tokens
        jit_code_result = jit.compress_if_needed(
            code_text, additional_force_tokens=["my_function"]
        )
        assert jit_code_result["threshold_triggered"] is not None  # COMP-04
        mock_adaptive.compress.assert_called()
        # Verify force tokens were passed to the compressor
        last_call = mock_adaptive.compress.call_args
        passed_tokens = last_call.kwargs.get("additional_force_tokens") or last_call[
            1
        ].get("additional_force_tokens")
        assert "my_function" in passed_tokens  # COMP-04


# ---------------------------------------------------------------------------
# Test: route_and_compress without result_text
# Validates that route_and_compress works when no text is provided
# ---------------------------------------------------------------------------


class TestRouteAndCompressNoText:
    """route_and_compress with no result_text skips compression entirely."""

    @pytest.mark.asyncio
    async def test_route_and_compress_no_text(self):
        """Without result_text, route_and_compress skips JIT and returns route_search result."""
        mm = _make_mock_mm(hybrid_results=[(1, 0.9)])
        clf = _make_mock_classifier(QueryComplexity.MEDIUM, 0.7)

        router = RetrievalRouter(memory_manager=mm, classifier=clf)

        with patch(
            "daem0nmcp.retrieval_router.settings", _settings_context(enabled=True)
        ):
            result = await router.route_and_compress("query", top_k=5)

        assert result["results"] == [(1, 0.9)]
        assert result["strategy_used"] == "hybrid"
        # No compression_metadata key at all when no text provided
        assert "compression_metadata" not in result


# ---------------------------------------------------------------------------
# Test: JIT compression metadata structure validation
# Validates COMP-03 (metadata has all required keys)
# ---------------------------------------------------------------------------


class TestCompressionMetadataStructure:
    """Validate exact structure of compression_metadata dict."""

    @pytest.mark.asyncio
    async def test_compression_metadata_structure(self):
        """compression_metadata has all required keys with correct types."""
        mm = _make_mock_mm(hybrid_results=[(1, 0.9)])
        clf = _make_mock_classifier(QueryComplexity.MEDIUM, 0.7)

        router = RetrievalRouter(memory_manager=mm, classifier=clf)

        mock_adaptive = _make_mock_adaptive()
        jit = JITCompressor(adaptive_compressor=mock_adaptive)

        large_text = "x" * 20000  # ~5K tokens = soft threshold

        with (
            patch(
                "daem0nmcp.retrieval_router.settings", _settings_context(enabled=True)
            ),
            patch("daem0nmcp.compression.jit.get_jit_compressor", return_value=jit),
        ):
            result = await router.route_and_compress(
                "query", top_k=5, result_text=large_text
            )

        metadata = result["compression_metadata"]
        assert metadata is not None

        # All required keys
        assert "original_tokens" in metadata
        assert "compressed_tokens" in metadata
        assert "compression_rate" in metadata
        assert "threshold_triggered" in metadata
        assert "content_type" in metadata

        # Correct types
        assert isinstance(metadata["original_tokens"], int)
        assert isinstance(metadata["compressed_tokens"], int)
        assert isinstance(metadata["compression_rate"], (int, float))
        assert isinstance(metadata["threshold_triggered"], str)

        # Values make sense
        assert metadata["original_tokens"] > metadata["compressed_tokens"]
        assert metadata["threshold_triggered"] in ("soft", "hard", "emergency")
        # compression_rate is ratio (original/compressed), so > 1.0 when compressed
        assert metadata["compression_rate"] > 1.0
