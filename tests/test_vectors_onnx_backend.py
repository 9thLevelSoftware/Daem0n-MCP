"""Real and bounded coverage for the legacy pooled-ONNX vector backend."""

from __future__ import annotations

import math
from importlib.util import find_spec
from unittest.mock import MagicMock, patch

import pytest

from daem0nmcp import vectors
from daem0nmcp.retrieval.onnx_encoder import OnnxSentenceEmbeddingModel

pytestmark = pytest.mark.skipif(
    find_spec("sentence_transformers") is None,
    reason="models-local profile is not installed; its release gate remains open",
)


@pytest.fixture(autouse=True)
def isolated_legacy_model():
    previous = vectors._model
    vectors._model = None
    try:
        yield
    finally:
        current = vectors._model
        if current is not None and current is not previous:
            close = getattr(current, "close", None)
            if callable(close):
                close()
        vectors._model = previous


def test_get_model_prefers_pooled_onnx_adapter(monkeypatch) -> None:
    pooled = MagicMock()
    monkeypatch.setattr(vectors.settings, "embedding_backend", "onnx")
    with (
        patch.object(vectors.CapabilityRegistry, "require"),
        patch(
            "daem0nmcp.retrieval.onnx_encoder.load_pooled_onnx_model",
            return_value=pooled,
        ) as load,
        patch("sentence_transformers.SentenceTransformer") as sentence_model,
    ):
        loaded = vectors._get_model()
        assert loaded is vectors._get_model()
        assert loaded._pooled_model is pooled

    load.assert_called_once_with(
        vectors.settings.embedding_model,
        vectors.settings.embedding_dimension,
    )
    sentence_model.assert_not_called()


def test_token_output_export_keeps_sentence_transformers_fallback(monkeypatch) -> None:
    fallback = MagicMock()
    monkeypatch.setattr(vectors.settings, "embedding_backend", "onnx")
    with (
        patch.object(vectors.CapabilityRegistry, "require"),
        patch(
            "daem0nmcp.retrieval.onnx_encoder.load_pooled_onnx_model",
            return_value=None,
        ),
        patch(
            "sentence_transformers.SentenceTransformer",
            return_value=fallback,
        ) as sentence_model,
    ):
        assert vectors._get_model() is fallback

    sentence_model.assert_called_once_with(
        vectors.settings.embedding_model,
        truncate_dim=vectors.settings.embedding_dimension,
        backend="onnx",
        model_kwargs={"file_name": "onnx/model_quantized.onnx"},
    )


def test_configured_cached_model_encodes_through_legacy_contract(monkeypatch) -> None:
    monkeypatch.setenv("DAEM0NMCP_MODELS_LOCAL_ENABLED", "true")
    monkeypatch.setenv("HF_HUB_OFFLINE", "1")
    monkeypatch.setattr(vectors.settings, "embedding_backend", "onnx")
    monkeypatch.setattr(
        vectors.settings,
        "embedding_model",
        "nomic-ai/modernbert-embed-base",
    )
    monkeypatch.setattr(vectors.settings, "embedding_dimension", 256)

    model = vectors._get_model()
    assert isinstance(model._pooled_model, OnnxSentenceEmbeddingModel)
    session = model._pooled_model._session
    assert session.get_providers() == ["CPUExecutionProvider"]
    options = session.get_session_options()
    assert options.intra_op_num_threads == 4
    assert options.inter_op_num_threads == 1

    document = vectors.decode(vectors.encode_document("bounded worker pools"))
    query = vectors.decode(vectors.encode_query("worker pool"))
    assert document is not None and query is not None
    assert len(document) == len(query) == 256
    assert all(math.isfinite(value) for value in (*document, *query))
    assert math.sqrt(sum(value * value for value in document)) == pytest.approx(
        1.0, abs=1e-5
    )
    assert math.sqrt(sum(value * value for value in query)) == pytest.approx(
        1.0, abs=1e-5
    )

    batch = model.encode(
        ["search_query: simple lookup", "search_query: causal history"],
        convert_to_numpy=True,
    )
    assert batch.shape == (2, 256)
    assert all(math.isfinite(float(value)) for value in batch.flat)

    from daem0nmcp.query_classifier import ExemplarQueryClassifier
    from daem0nmcp.recall_planner import QueryComplexity

    level, confidence, scores = ExemplarQueryClassifier(model=model).classify(
        "trace the causal history"
    )
    assert isinstance(level, QueryComplexity)
    assert math.isfinite(confidence)
    assert set(scores) == {"simple", "medium", "complex"}
    assert all(math.isfinite(score) for score in scores.values())

    index = vectors.VectorIndex()
    assert index.add(7, "bounded worker pools")
    results = index.search("worker pool", top_k=1, threshold=-1.0)
    assert len(results) == 1
    assert results[0][0] == 7
    assert math.isfinite(results[0][1])
