"""Unit tests for ExemplarQueryClassifier.

All tests use a mocked SentenceTransformer model so they run fast and
don't require downloading the real 80 MB model in CI.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import numpy as np
import pytest

from daem0nmcp.query_classifier import ExemplarQueryClassifier, QueryComplexity

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_unit_vector(dim: int, index: int) -> np.ndarray:
    """Return a unit vector with 1.0 at *index* and 0.0 elsewhere."""
    v = np.zeros(dim, dtype=np.float32)
    v[index] = 1.0
    return v


def _build_mock_model(
    *,
    dim: int = 8,
    simple_idx: int = 0,
    medium_idx: int = 1,
    complex_idx: int = 2,
    query_idx: int | None = None,
):
    """Return a mock SentenceTransformer whose ``encode()`` returns
    deterministic unit vectors arranged so that cosine similarity is
    trivially predictable.

    * Exemplars for *simple* point in direction ``simple_idx``
    * Exemplars for *medium* point in direction ``medium_idx``
    * Exemplars for *complex* point in direction ``complex_idx``
    * The query points in direction ``query_idx`` (defaults to ``simple_idx``)
    """
    if query_idx is None:
        query_idx = simple_idx

    # Pre-build exemplar matrices (6 exemplars per level)
    simple_embs = np.tile(_make_unit_vector(dim, simple_idx), (6, 1))
    medium_embs = np.tile(_make_unit_vector(dim, medium_idx), (6, 1))
    complex_embs = np.tile(_make_unit_vector(dim, complex_idx), (6, 1))

    query_vec = _make_unit_vector(dim, query_idx)

    # Map of call-index -> return value.
    # _ensure_initialized() encodes 3 lists (simple, medium, complex)
    # then classify() encodes the query string.
    exemplar_returns = [simple_embs, medium_embs, complex_embs]
    call_counter = {"n": 0, "init_done": False}

    def _encode_side_effect(texts, convert_to_numpy=True):
        if isinstance(texts, list):
            idx = call_counter["n"]
            call_counter["n"] += 1
            return exemplar_returns[idx]
        # Single string -> query
        return query_vec

    model = MagicMock()
    model.encode.side_effect = _encode_side_effect
    return model


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestLazyInitialization:
    def test_lazy_initialization(self):
        """Classifier must not initialize until classify() is called."""
        clf = ExemplarQueryClassifier(model=None)
        assert clf._initialized is False

        mock_model = _build_mock_model()
        with patch(
            "daem0nmcp.query_classifier._get_model", return_value=mock_model
        ) as patched:
            clf.classify("test query")
            assert clf._initialized is True
            patched.assert_called_once()

    def test_model_reuse_from_vectors(self):
        """When no model is supplied, _get_model() must be called to get the
        shared singleton from vectors.py."""
        mock_model = _build_mock_model()
        clf = ExemplarQueryClassifier(model=None)

        with patch(
            "daem0nmcp.query_classifier._get_model", return_value=mock_model
        ) as patched:
            clf.classify("anything")
            patched.assert_called_once()


class TestClassification:
    def test_classify_simple_query(self):
        """Query aligned with simple exemplars should classify as SIMPLE."""
        model = _build_mock_model(query_idx=0)  # aligned with simple
        clf = ExemplarQueryClassifier(model=model)
        level, confidence, scores = clf.classify("what is this")

        assert level == QueryComplexity.SIMPLE
        assert confidence == pytest.approx(1.0, abs=0.01)

    def test_classify_medium_query(self):
        """Query aligned with medium exemplars should classify as MEDIUM."""
        model = _build_mock_model(query_idx=1)  # aligned with medium
        clf = ExemplarQueryClassifier(model=model)
        level, confidence, scores = clf.classify("how does X relate to Y")

        assert level == QueryComplexity.MEDIUM
        assert confidence == pytest.approx(1.0, abs=0.01)

    def test_classify_complex_query(self):
        """Query aligned with complex exemplars should classify as COMPLEX."""
        model = _build_mock_model(query_idx=2)  # aligned with complex
        clf = ExemplarQueryClassifier(model=model)
        level, confidence, scores = clf.classify("trace history of changes")

        assert level == QueryComplexity.COMPLEX
        assert confidence == pytest.approx(1.0, abs=0.01)


class TestFallback:
    def test_fallback_on_low_confidence(self):
        """When all similarities are below threshold, fallback to MEDIUM."""
        # Use a query direction orthogonal to all exemplars -> cosine = 0
        model = _build_mock_model(query_idx=5)  # idx 5 != 0,1,2
        clf = ExemplarQueryClassifier(model=model, confidence_threshold=0.25)
        level, confidence, scores = clf.classify("gibberish")

        assert level == QueryComplexity.MEDIUM  # fallback
        assert confidence < 0.25


class TestReturnShape:
    def test_scores_dict_returned(self):
        """classify() must return a scores dict with string keys and float values."""
        model = _build_mock_model(query_idx=0)
        clf = ExemplarQueryClassifier(model=model)
        level, confidence, scores = clf.classify("test")

        assert isinstance(scores, dict)
        assert set(scores.keys()) == {"simple", "medium", "complex"}
        for v in scores.values():
            assert isinstance(v, float)


class TestCaching:
    def test_exemplar_embeddings_computed_once(self):
        """Exemplar embeddings must be computed only on the first classify()
        call. Subsequent calls should only encode the query."""
        model = _build_mock_model(query_idx=0)
        clf = ExemplarQueryClassifier(model=model)

        clf.classify("first query")
        encode_calls_after_first = model.encode.call_count  # 3 exemplar + 1 query = 4

        clf.classify("second query")
        encode_calls_after_second = model.encode.call_count  # +1 query only = 5

        # Only 1 extra encode call (the second query), not 4 (re-init + query)
        assert encode_calls_after_second == encode_calls_after_first + 1
