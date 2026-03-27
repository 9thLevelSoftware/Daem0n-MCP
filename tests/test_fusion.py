# tests/test_fusion.py
"""Tests for Reciprocal Rank Fusion."""

from daem0nmcp.fusion import RRFHybridSearch, reciprocal_rank_fusion


class TestReciprocalRankFusion:
    """Test RRF algorithm."""

    def test_basic_fusion(self):
        # Two result lists with overlapping docs
        results1 = [(1, 0.9), (2, 0.8), (3, 0.7)]  # BM25 results
        results2 = [(2, 0.95), (1, 0.85), (4, 0.75)]  # Vector results

        fused = reciprocal_rank_fusion([results1, results2], k=60)

        # Doc 2 appears in both at good positions, should be top
        assert fused[0][0] in [1, 2]  # Either 1 or 2 should be top
        # All docs from both lists should be present
        doc_ids = [doc_id for doc_id, _ in fused]
        assert set(doc_ids) == {1, 2, 3, 4}

    def test_single_list(self):
        results = [(1, 0.9), (2, 0.8)]
        fused = reciprocal_rank_fusion([results], k=60)

        assert len(fused) == 2
        assert fused[0][0] == 1  # Preserves order

    def test_empty_lists(self):
        fused = reciprocal_rank_fusion([[], []], k=60)
        assert fused == []

    def test_k_parameter_affects_scores(self):
        results1 = [(1, 0.9)]
        results2 = [(2, 0.8)]

        fused_low_k = reciprocal_rank_fusion([results1, results2], k=1)
        fused_high_k = reciprocal_rank_fusion([results1, results2], k=100)

        # Scores should be different with different k
        assert fused_low_k[0][1] != fused_high_k[0][1]


class TestRRFHybridSearch:
    """Test hybrid search combining BM25 and vectors."""

    def test_hybrid_search_combines_results(self):
        from daem0nmcp.bm25_index import BM25Index
        from daem0nmcp.vectors import VectorIndex

        bm25 = BM25Index()
        bm25.add_document(1, "JWT authentication security")
        bm25.add_document(2, "Database migration scripts")

        vectors = VectorIndex()
        vectors.add(1, "JWT authentication security")
        vectors.add(2, "Database migration scripts")

        hybrid = RRFHybridSearch(bm25, vectors)
        results = hybrid.search("authentication JWT", top_k=2)

        assert len(results) >= 1
        assert results[0][0] == 1  # Auth doc should be top

    def test_fallback_to_bm25_only(self):
        from daem0nmcp.bm25_index import BM25Index

        # BM25 needs multiple docs for meaningful IDF scores
        bm25 = BM25Index()
        bm25.add_document(1, "Authentication tokens and security")
        bm25.add_document(2, "Database migration scripts")
        bm25.add_document(3, "REST API endpoint design")

        hybrid = RRFHybridSearch(bm25, None)
        results = hybrid.search("authentication", top_k=2)

        assert len(results) >= 1
        assert results[0][0] == 1  # Auth doc should be top
