"""
Vector Embeddings - Semantic understanding with sentence-transformers.

This module provides:
- Vector embeddings for memories (enhances TF-IDF semantic matching)
- Hybrid search combining TF-IDF + vector similarity
- Efficient storage of vectors in SQLite
"""

import logging
import math
import struct
from collections.abc import Sequence
from typing import Any

from .capabilities import CapabilityRegistry
from .config import settings

logger = logging.getLogger(__name__)

# Global model instance (lazy loaded, shared across all contexts)
_model: Any | None = None


class _PooledOnnxSentenceTransformerAdapter:
    """Keep the shared legacy model's single and batch ``encode`` surface."""

    def __init__(self, pooled_model: Any) -> None:
        self._pooled_model = pooled_model

    def encode(
        self,
        sentences: str | Sequence[str],
        *,
        convert_to_numpy: bool = True,
        **_options: Any,
    ) -> Any:
        import numpy as np

        if isinstance(sentences, str):
            values = self._pooled_model.encode(
                sentences,
                convert_to_numpy=False,
            )
        else:
            values = [
                self._pooled_model.encode(text, convert_to_numpy=False)
                for text in sentences
            ]
        if convert_to_numpy:
            return np.asarray(values, dtype=np.float32)
        return values

    def close(self) -> None:
        self._pooled_model.close()


def is_available() -> bool:
    """Check whether locally hosted embeddings are explicitly enabled and installed."""
    return CapabilityRegistry().get("models-local")["status"] == "ready"


def _get_model() -> Any:
    """Get or create the embedding model (lazy loading, shared across contexts)."""
    global _model

    CapabilityRegistry().require("models-local")

    if _model is None:
        backend = settings.embedding_backend
        logger.info(
            f"Loading embedding model ({settings.embedding_model}, "
            f"backend={backend}, dim={settings.embedding_dimension})..."
        )
        if backend == "onnx":
            try:
                from .retrieval.onnx_encoder import load_pooled_onnx_model

                pooled_model = load_pooled_onnx_model(
                    settings.embedding_model,
                    settings.embedding_dimension,
                )
                if pooled_model is not None:
                    _model = _PooledOnnxSentenceTransformerAdapter(pooled_model)
            except Exception:
                # Preserve the legacy fallback below for token-output exports
                # and installations without the pooled ONNX dependencies.
                logger.warning(
                    "Failed to load the pooled ONNX embedding adapter; "
                    "trying Sentence Transformers"
                )
        if _model is None:
            from sentence_transformers import SentenceTransformer

            try:
                _model = SentenceTransformer(
                    settings.embedding_model,
                    truncate_dim=settings.embedding_dimension,
                    backend=backend,
                    model_kwargs={"file_name": "onnx/model_quantized.onnx"}
                    if backend == "onnx"
                    else {},
                )
            except Exception:
                logger.warning(
                    "Failed to load model with backend=%s, falling back to torch",
                    backend,
                )
                _model = SentenceTransformer(
                    settings.embedding_model,
                    truncate_dim=settings.embedding_dimension,
                )
        logger.info("Embedding model loaded.")

    return _model


def _embedding_values(embedding: Any) -> list[float]:
    """Normalize NumPy and pooled-adapter outputs to the legacy list contract."""

    to_list = getattr(embedding, "tolist", None)
    values = to_list() if callable(to_list) else list(embedding)
    return [float(value) for value in values]


def get_dimension() -> int:
    """Return the configured embedding dimension."""
    return settings.embedding_dimension


def encode(text: str, *, prefix: str = "document") -> bytes | None:
    """
    Encode text to a vector embedding with the specified prefix.

    Args:
        text: Text to encode.
        prefix: "query" or "document" -- selects the appropriate prefix
                from settings.

    Returns bytes (packed floats) for storage in SQLite.
    """
    if prefix == "query":
        prefixed = f"{settings.embedding_query_prefix}{text}"
    else:
        prefixed = f"{settings.embedding_document_prefix}{text}"

    model = _get_model()
    embedding = model.encode(prefixed, convert_to_numpy=True)
    values = _embedding_values(embedding)
    return struct.pack(f"{len(values)}f", *values)


def encode_query(text: str) -> bytes | None:
    """Encode text as a search query (uses query prefix)."""
    return encode(text, prefix="query")


def encode_document(text: str) -> bytes | None:
    """Encode text as a document for storage (uses document prefix)."""
    return encode(text, prefix="document")


def decode(data: bytes) -> list[float] | None:
    """Decode vector bytes back to a list of floats."""
    if not data:
        return None

    num_floats = len(data) // 4  # 4 bytes per float
    return list(struct.unpack(f"{num_floats}f", data))


def cosine_similarity(vec1: list[float], vec2: list[float]) -> float:
    """Compute cosine similarity between two vectors."""
    if len(vec1) != len(vec2):
        raise ValueError("Vector dimensions must match.")

    dot_product = sum(left * right for left, right in zip(vec1, vec2, strict=True))
    norm_a = math.sqrt(sum(value * value for value in vec1))
    norm_b = math.sqrt(sum(value * value for value in vec2))

    if norm_a == 0 or norm_b == 0:
        return 0.0

    return dot_product / (norm_a * norm_b)


class VectorIndex:
    """
    In-memory vector index for fast similarity search.

    Stores vectors keyed by document ID and supports batch similarity queries.
    """

    def __init__(self):
        self.vectors: dict[int, list[float]] = {}

    def add(self, doc_id: int, text: str) -> bool:
        """Add a document to the index (encodes as document)."""
        model = _get_model()
        prefixed = f"{settings.embedding_document_prefix}{text}"
        embedding = model.encode(prefixed, convert_to_numpy=True)
        self.vectors[doc_id] = _embedding_values(embedding)
        return True

    def add_from_bytes(self, doc_id: int, data: bytes) -> bool:
        """Add a pre-computed vector from bytes."""
        vec = decode(data)
        if vec:
            self.vectors[doc_id] = vec
            return True
        return False

    def remove(self, doc_id: int) -> None:
        """Remove a document from the index."""
        self.vectors.pop(doc_id, None)

    def search(
        self, query: str, top_k: int = 10, threshold: float = 0.3
    ) -> list[tuple[int, float]]:
        """
        Search for similar documents.

        Args:
            query: Query text
            top_k: Maximum results
            threshold: Minimum similarity score

        Returns:
            List of (doc_id, similarity) tuples, sorted by similarity descending.
        """
        if not self.vectors:
            return []

        model = _get_model()

        # Encode query with query prefix
        prefixed = f"{settings.embedding_query_prefix}{query}"
        query_vec = model.encode(prefixed, convert_to_numpy=True)
        query_values = _embedding_values(query_vec)

        # Compute similarities
        results = []
        for doc_id, doc_vec in self.vectors.items():
            sim = cosine_similarity(query_values, doc_vec)
            if sim >= threshold:
                results.append((doc_id, sim))

        # Sort by similarity descending
        results.sort(key=lambda x: x[1], reverse=True)

        return results[:top_k]

    def __len__(self) -> int:
        return len(self.vectors)


class HybridSearch:
    """
    Hybrid search combining TF-IDF and vector similarity.

    Uses TF-IDF as primary with vector boosting for enhanced semantic matching.
    """

    def __init__(self, tfidf_index, vector_index: VectorIndex | None = None):
        self.tfidf = tfidf_index
        self.vectors = vector_index or VectorIndex()
        self.vector_weight = settings.hybrid_vector_weight

    def search(
        self,
        query: str,
        top_k: int = 10,
        tfidf_threshold: float = 0.1,
        vector_threshold: float = 0.3,
    ) -> list[tuple[int, float]]:
        """
        Hybrid search combining TF-IDF and vector similarity.

        Combines scores: final_score = (1 - weight) * tfidf_score + weight * vector_score
        Falls back to TF-IDF only if vector index is empty.
        """
        # Get TF-IDF results
        tfidf_results = self.tfidf.search(
            query, top_k=top_k * 2, threshold=tfidf_threshold
        )
        tfidf_scores = dict(tfidf_results)

        # If vectors available, get vector results
        if len(self.vectors) > 0:
            vector_results = self.vectors.search(
                query, top_k=top_k * 2, threshold=vector_threshold
            )
            vector_scores = dict(vector_results)

            # Combine scores
            all_docs = set(tfidf_scores.keys()) | set(vector_scores.keys())
            combined = []

            for doc_id in all_docs:
                tfidf_score = tfidf_scores.get(doc_id, 0.0)
                vector_score = vector_scores.get(doc_id, 0.0)

                # Weighted combination
                final_score = (
                    1 - self.vector_weight
                ) * tfidf_score + self.vector_weight * vector_score

                combined.append((doc_id, final_score))

            combined.sort(key=lambda x: x[1], reverse=True)
            return combined[:top_k]

        # Fall back to TF-IDF only if no vectors indexed
        return tfidf_results[:top_k]


# Global vector index instance
_global_vector_index: VectorIndex | None = None


def get_vector_index() -> VectorIndex:
    """Get or create the global vector index."""
    global _global_vector_index
    if _global_vector_index is None:
        _global_vector_index = VectorIndex()
    return _global_vector_index


def reset_vector_index() -> None:
    """Reset the global vector index (useful for testing)."""
    global _global_vector_index
    _global_vector_index = None
