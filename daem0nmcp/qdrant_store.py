"""
Qdrant Vector Store - Persistent vector storage backend for Daem0n-MCP.

This module provides:
- Persistent vector storage using Qdrant (local mode, no server required)
- Metadata filtering for efficient memory retrieval
- Replaces the in-memory VectorIndex from vectors.py for production use

The store uses configurable sentence-transformers embeddings with cosine similarity
for semantic matching. Dimension is controlled by settings.embedding_dimension.
"""

import logging
import os

from qdrant_client import QdrantClient
from qdrant_client.models import (
    Distance,
    FieldCondition,
    Filter,
    MatchAny,
    MatchValue,
    PointStruct,
    VectorParams,
)

from .config import settings

logger = logging.getLogger(__name__)


class QdrantVectorStore:
    """
    Vector storage backend using Qdrant.

    Provides persistent vector storage with metadata filtering capabilities.
    Uses local file-based mode (no server needed for single-user scenarios).
    """

    COLLECTION_MEMORIES = "daem0n_memories"
    COLLECTION_CODE = "daem0n_code_entities"  # Reserved for Phase 2
    EMBEDDING_DIMENSION = settings.embedding_dimension

    def __init__(self, path: str = "./storage/qdrant"):
        """
        Initialize the Qdrant vector store.

        Args:
            path: Directory path for local Qdrant storage.
                  Uses file-based mode (no server required).
        """
        logger.info(f"Initializing Qdrant vector store at: {path}")
        timeout = int(os.getenv("QDRANT_TIMEOUT", "60"))
        self.client = QdrantClient(path=path, timeout=timeout)
        self._ensure_collections()

    def _ensure_collections(self) -> None:
        """Ensure required collections exist with proper configuration."""
        collections = [c.name for c in self.client.get_collections().collections]

        for coll_name in [self.COLLECTION_MEMORIES, self.COLLECTION_CODE]:
            if coll_name in collections:
                # Check for dimension mismatch
                info = self.client.get_collection(coll_name)
                existing_dim = info.config.params.vectors.size
                if existing_dim != self.EMBEDDING_DIMENSION:
                    logger.warning(
                        f"Collection {coll_name} has dimension {existing_dim}, "
                        f"expected {self.EMBEDDING_DIMENSION}. Recreating collection."
                    )
                    self.client.delete_collection(coll_name)
                    collections.remove(coll_name)

            if coll_name not in collections:
                logger.info(f"Creating collection: {coll_name}")
                self.client.create_collection(
                    collection_name=coll_name,
                    vectors_config=VectorParams(
                        size=self.EMBEDDING_DIMENSION, distance=Distance.COSINE
                    ),
                )

    def upsert_memory(
        self, memory_id: int, embedding: list[float], metadata: dict
    ) -> None:
        """
        Store or update a memory's vector embedding.

        Args:
            memory_id: Unique identifier for the memory (from SQLite).
            embedding: Vector embedding (dimensions from sentence-transformers).
            metadata: Payload data including category, tags, file_path, worked, etc.
        """
        self.client.upsert(
            collection_name=self.COLLECTION_MEMORIES,
            points=[PointStruct(id=memory_id, vector=embedding, payload=metadata)],
        )

    def search(
        self,
        query_vector: list[float],
        limit: int = 20,
        category_filter: list[str] | None = None,
        tags_filter: list[str] | None = None,
        file_path: str | None = None,
    ) -> list[tuple[int, float]]:
        """
        Search for similar memories with optional metadata filtering.

        Uses the modern query_points API (qdrant-client >= 1.10).

        Args:
            query_vector: Query embedding vector (configured dimensions).
            limit: Maximum number of results to return.
            category_filter: Filter to memories in these categories.
            tags_filter: Filter to memories with any of these tags.
            file_path: Filter to memories associated with this file path.

        Returns:
            List of (memory_id, similarity_score) tuples, sorted by score descending.
        """
        filters = []
        if category_filter:
            filters.append(
                FieldCondition(key="category", match=MatchAny(any=category_filter))
            )
        if tags_filter:
            filters.append(FieldCondition(key="tags", match=MatchAny(any=tags_filter)))
        if file_path:
            filters.append(
                FieldCondition(key="file_path", match=MatchValue(value=file_path))
            )

        # Use query_points (modern API) instead of deprecated search
        response = self.client.query_points(
            collection_name=self.COLLECTION_MEMORIES,
            query=query_vector,
            query_filter=Filter(must=filters) if filters else None,
            limit=limit,
        )

        return [(point.id, point.score) for point in response.points]

    def delete_memory(self, memory_id: int) -> None:
        """
        Remove a memory's vector from the store.

        Args:
            memory_id: The memory ID to delete.
        """
        self.client.delete(
            collection_name=self.COLLECTION_MEMORIES, points_selector=[memory_id]
        )

    def get_count(self) -> int:
        """
        Get the number of vectors in the memories collection.

        Returns:
            Count of stored memory vectors.
        """
        info = self.client.get_collection(self.COLLECTION_MEMORIES)
        return info.points_count

    def close(self) -> None:
        """Close the Qdrant client connection."""
        self.client.close()
