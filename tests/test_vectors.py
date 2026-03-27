"""Tests for the vector embeddings module."""

from unittest.mock import MagicMock, patch

from daem0nmcp import vectors
from daem0nmcp.config import settings


class TestVectorAvailability:
    """Test vector availability detection."""

    def test_is_available_returns_bool(self):
        """is_available should return a boolean."""
        result = vectors.is_available()
        assert isinstance(result, bool)


class TestVectorIndex:
    """Test the VectorIndex class."""

    def test_create_empty_index(self):
        """Can create an empty vector index."""
        index = vectors.VectorIndex()
        assert len(index) == 0

    def test_search_empty_returns_empty(self):
        """Search on empty index returns empty list."""
        index = vectors.VectorIndex()
        results = index.search("test query")
        assert results == []


class TestHybridSearch:
    """Test the hybrid search combining TF-IDF and vectors."""

    def test_hybrid_fallback_to_tfidf(self):
        """Hybrid search falls back to TF-IDF when vectors unavailable."""
        from daem0nmcp.similarity import TFIDFIndex

        tfidf = TFIDFIndex()
        tfidf.add_document(1, "JWT authentication for API security")
        tfidf.add_document(2, "Database migration and schema changes")

        hybrid = vectors.HybridSearch(tfidf)
        results = hybrid.search("API authentication")

        # Should get TF-IDF results
        assert len(results) >= 1
        # Doc 1 should match
        doc_ids = [r[0] for r in results]
        assert 1 in doc_ids


class TestEncodeDecode:
    """Test vector encoding and decoding."""

    def test_encode_returns_none_when_unavailable(self):
        """encode returns None when vectors not available."""
        if not vectors.is_available():
            result = vectors.encode("test text")
            assert result is None

    def test_decode_empty_returns_none(self):
        """decode returns None for empty bytes."""
        result = vectors.decode(b"")
        assert result is None

    def test_decode_none_returns_none(self):
        """decode returns None for None input."""
        result = vectors.decode(None)
        assert result is None


class TestCosineSimWithoutVectors:
    """Test cosine similarity when numpy not available."""

    def test_cosine_returns_zero_when_unavailable(self):
        """cosine_similarity returns 0.0 when numpy not available."""
        if not vectors.is_available():
            result = vectors.cosine_similarity([1, 2, 3], [1, 2, 3])
            # Without numpy, should return 0
            assert result == 0.0


class TestGlobalVectorIndex:
    """Test the global vector index singleton."""

    def test_get_vector_index_returns_index(self):
        """get_vector_index returns a VectorIndex."""
        vectors.reset_vector_index()
        index = vectors.get_vector_index()
        assert isinstance(index, vectors.VectorIndex)

    def test_get_vector_index_returns_same_instance(self):
        """get_vector_index returns the same instance."""
        vectors.reset_vector_index()
        index1 = vectors.get_vector_index()
        index2 = vectors.get_vector_index()
        assert index1 is index2

    def test_reset_clears_index(self):
        """reset_vector_index clears the global index."""
        vectors.reset_vector_index()
        index1 = vectors.get_vector_index()
        vectors.reset_vector_index()
        index2 = vectors.get_vector_index()
        assert index1 is not index2


class TestEncodeQueryDocument:
    """Test encode_query and encode_document use correct prefixes."""

    @patch("daem0nmcp.vectors._get_model")
    def test_encode_document_prepends_document_prefix(self, mock_get_model):
        import numpy as np

        from daem0nmcp import vectors

        fake_model = MagicMock()
        fake_model.encode.return_value = np.array([0.1] * 256, dtype=np.float32)
        mock_get_model.return_value = fake_model

        result = vectors.encode_document("hello world")
        assert result is not None

        # Verify document prefix was prepended
        fake_model.encode.assert_called_once()
        call_text = fake_model.encode.call_args[0][0]
        assert call_text == f"{settings.embedding_document_prefix}hello world"

    @patch("daem0nmcp.vectors._get_model")
    def test_encode_query_prepends_query_prefix(self, mock_get_model):
        import numpy as np

        from daem0nmcp import vectors

        fake_model = MagicMock()
        fake_model.encode.return_value = np.array([0.1] * 256, dtype=np.float32)
        mock_get_model.return_value = fake_model

        result = vectors.encode_query("hello world")
        assert result is not None

        call_text = fake_model.encode.call_args[0][0]
        assert call_text == f"{settings.embedding_query_prefix}hello world"

    @patch("daem0nmcp.vectors._get_model")
    def test_encode_returns_correct_byte_length(self, mock_get_model):
        import numpy as np

        from daem0nmcp import vectors

        fake_model = MagicMock()
        fake_model.encode.return_value = np.array([0.1] * 256, dtype=np.float32)
        mock_get_model.return_value = fake_model

        result = vectors.encode_document("test")
        assert result is not None
        # 256 floats * 4 bytes each = 1024 bytes
        assert len(result) == 256 * 4


class TestGetDimension:
    def test_returns_configured_dimension(self):
        from daem0nmcp.vectors import get_dimension

        assert get_dimension() == settings.embedding_dimension
