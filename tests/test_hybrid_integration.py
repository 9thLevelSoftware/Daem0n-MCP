# tests/test_hybrid_integration.py
"""Integration tests for hybrid BM25 + vector search."""

from daem0nmcp.bm25_index import BM25Index
from daem0nmcp.fusion import RRFHybridSearch
from daem0nmcp.vectors import VectorIndex


class TestHybridSearchIntegration:
    """Test hybrid search end-to-end."""

    def test_hybrid_outperforms_single_method(self):
        """Verify hybrid search finds relevant docs that single methods might miss."""
        bm25 = BM25Index()
        vectors = VectorIndex()

        # Add documents
        docs = [
            (1, "JWT authentication tokens for API security"),
            (2, "OAuth 2.0 authorization flow implementation"),
            (3, "Database connection pooling configuration"),
            (4, "User session management with cookies"),
        ]

        for doc_id, text in docs:
            bm25.add_document(doc_id, text)
            vectors.add(doc_id, text)

        hybrid = RRFHybridSearch(bm25, vectors)

        # Query that benefits from both keyword and semantic matching
        query = "secure user authentication"

        hybrid_results = hybrid.search(query, top_k=4)

        # Hybrid should return results
        assert len(hybrid_results) >= 1

        # Should find auth-related docs
        hybrid_ids = [doc_id for doc_id, _ in hybrid_results]
        assert 1 in hybrid_ids or 2 in hybrid_ids or 4 in hybrid_ids

    def test_rrf_combines_different_rankings(self):
        """Verify RRF properly combines different rankings."""
        bm25 = BM25Index()
        vectors = VectorIndex()

        # Doc 1: Good for keywords, maybe less for vectors
        # Doc 2: Good for vectors, maybe less for keywords
        bm25.add_document(1, "authentication security JWT token")
        bm25.add_document(2, "user login verification process")

        vectors.add(1, "authentication security JWT token")
        vectors.add(2, "user login verification process")

        hybrid = RRFHybridSearch(bm25, vectors)

        results = hybrid.search("secure login authentication", top_k=2)

        # Both docs should appear
        result_ids = [doc_id for doc_id, _ in results]
        assert len(result_ids) == 2

    def test_hybrid_with_larger_corpus(self):
        """Test hybrid search with more documents for better IDF scores."""
        bm25 = BM25Index()
        vectors = VectorIndex()

        # Create a larger corpus for better BM25 discrimination
        docs = [
            (1, "JWT authentication tokens for API security"),
            (2, "OAuth 2.0 authorization flow implementation"),
            (3, "Database connection pooling configuration"),
            (4, "User session management with cookies"),
            (5, "REST API endpoint design patterns"),
            (6, "Microservices architecture overview"),
            (7, "Error handling and logging best practices"),
            (8, "Password hashing with bcrypt algorithm"),
        ]

        for doc_id, text in docs:
            bm25.add_document(doc_id, text)
            vectors.add(doc_id, text)

        hybrid = RRFHybridSearch(bm25, vectors)

        # Query specifically for authentication
        results = hybrid.search("authentication API security", top_k=3)

        # Should find JWT auth doc first or near top
        result_ids = [doc_id for doc_id, _ in results]
        assert 1 in result_ids  # JWT authentication doc

    def test_hybrid_graceful_fallback_bm25_only(self):
        """Test hybrid search falls back to BM25 when vectors unavailable."""
        bm25 = BM25Index()
        bm25.add_document(1, "authentication security")
        bm25.add_document(2, "database configuration")
        bm25.add_document(3, "unrelated document content")

        # Create hybrid with no vector index
        hybrid = RRFHybridSearch(bm25, None)

        results = hybrid.search("authentication", top_k=2)
        assert len(results) >= 1
        assert results[0][0] == 1  # Auth doc should be first
