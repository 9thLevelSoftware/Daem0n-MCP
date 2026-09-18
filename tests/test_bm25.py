# tests/test_bm25.py
"""Tests for BM25 index."""

import pytest

pytest.importorskip("rank_bm25")

from daem0nmcp.bm25_index import BM25Index


class TestBM25Index:
    """Test BM25 indexing and search."""

    def test_add_and_search(self):
        index = BM25Index()
        index.add_document(1, "JWT authentication for API security")
        index.add_document(2, "Database migration and schema changes")
        index.add_document(3, "REST API endpoint design patterns")

        results = index.search("API authentication", top_k=3)
        assert len(results) >= 1
        # Document 1 should be most relevant (has both API and authentication)
        assert results[0][0] == 1

    def test_search_with_tags(self):
        # BM25 needs a larger corpus for meaningful IDF scores
        index = BM25Index()
        index.add_document(1, "Use tokens for auth", tags=["security", "jwt"])
        index.add_document(2, "Database configuration and setup")
        index.add_document(3, "REST API endpoint design")
        index.add_document(4, "User management system")
        index.add_document(5, "Cache optimization techniques")

        results = index.search("JWT security", top_k=5)
        assert len(results) >= 1
        assert results[0][0] == 1  # Doc with security and jwt tags should rank first

    def test_remove_document(self):
        index = BM25Index()
        index.add_document(1, "Authentication API")
        index.add_document(2, "Database changes")

        index.remove_document(1)
        results = index.search("Authentication", top_k=2)
        assert not any(doc_id == 1 for doc_id, _ in results)

    def test_empty_index(self):
        index = BM25Index()
        results = index.search("anything", top_k=5)
        assert results == []

    def test_get_scores_returns_all_docs(self):
        index = BM25Index()
        index.add_document(1, "hello world")
        index.add_document(2, "goodbye world")

        scores = index.get_scores("hello")
        assert len(scores) == 2
