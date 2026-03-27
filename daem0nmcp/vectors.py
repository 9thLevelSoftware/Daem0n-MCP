"""
Vector Embeddings - Semantic understanding with sentence-transformers.

This module provides:
- Vector embeddings for memories (enhances TF-IDF semantic matching)
- Hybrid search combining TF-IDF + vector similarity
- Efficient storage of vectors in SQLite
"""

import logging
import struct

import numpy as np
from sentence_transformers import SentenceTransformer

from .config import settings

logger = logging.getLogger(__name__)

# Global model instance (lazy loaded, shared across all contexts)
_model: SentenceTransformer | None = None


def is_available() -> bool:
    """Check if vector embeddings are available. Always True since deps are core."""
    return True


def _get_model() -> SentenceTransformer:
    """Get or create the embedding model (lazy loading, shared across contexts)."""
    global _model

    if _model is None:
        backend = settings.embedding_backend
        logger.info(
            f"Loading embedding model ({settings.embedding_model}, "
            f"backend={backend}, dim={settings.embedding_dimension})..."
        )
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
                f"Failed to load model with backend={backend}, falling back to torch"
            )
            _model = SentenceTransformer(
                settings.embedding_model,
                truncate_dim=settings.embedding_dimension,
            )
        logger.info("Embedding model loaded.")

    return _model


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
    return struct.pack(f"{len(embedding)}f", *embedding)


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
    a = np.array(vec1)
    b = np.array(vec2)

    dot_product = np.dot(a, b)
    norm_a = np.linalg.norm(a)
    norm_b = np.linalg.norm(b)

    if norm_a == 0 or norm_b == 0:
        return 0.0

    return float(dot_product / (norm_a * norm_b))


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
        self.vectors[doc_id] = embedding.tolist()
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

        # Compute similarities
        results = []
        for doc_id, doc_vec in self.vectors.items():
            sim = cosine_similarity(query_vec.tolist(), doc_vec)
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
        tfidf_scores = {doc_id: score for doc_id, score in tfidf_results}

        # If vectors available, get vector results
        if len(self.vectors) > 0:
            vector_results = self.vectors.search(
                query, top_k=top_k * 2, threshold=vector_threshold
            )
            vector_scores = {doc_id: score for doc_id, score in vector_results}

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
