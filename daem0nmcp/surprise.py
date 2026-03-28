# daem0nmcp/surprise.py
"""
Surprise Calculator - Titans-inspired novelty detection.

Measures how "surprising" new information is compared to existing memories.
High surprise = novel, important information to prioritize.
Low surprise = routine, can be deprioritized.
"""

import numpy as np


def calculate_surprise(
    new_embedding: list[float],
    existing_embeddings: list[list[float]],
    k_nearest: int = 5,
) -> float:
    """
    Calculate surprise score for a new embedding.

    Uses average distance to k-nearest neighbors as surprise metric.
    Higher distance = more surprising (novel information).

    Args:
        new_embedding: Vector for new content
        existing_embeddings: Vectors for existing memories
        k_nearest: Number of nearest neighbors to consider

    Returns:
        Surprise score between 0.0 (routine) and 1.0 (very surprising)
    """
    if not existing_embeddings:
        return 1.0  # First memory is maximally surprising

    new_vec = np.array(new_embedding)

    # Calculate distances to all existing embeddings
    distances = []
    for existing in existing_embeddings:
        existing_vec = np.array(existing)
        # Cosine distance = 1 - cosine_similarity
        dot = np.dot(new_vec, existing_vec)
        norm_new = np.linalg.norm(new_vec)
        norm_existing = np.linalg.norm(existing_vec)

        if norm_new == 0 or norm_existing == 0:
            distances.append(1.0)
        else:
            similarity = dot / (norm_new * norm_existing)
            distances.append(1.0 - similarity)

    # Get k nearest (smallest distances)
    k = min(k_nearest, len(distances))
    nearest_distances = sorted(distances)[:k]

    # Average distance to nearest neighbors
    avg_distance = sum(nearest_distances) / len(nearest_distances)

    # Normalize to 0-1 range (cosine distance is already 0-2, typically 0-1)
    surprise = min(1.0, max(0.0, avg_distance))

    return surprise


class SurpriseCalculator:
    """
    Calculator for memory surprise scores.

    Configurable k_nearest for comparison.
    """

    def __init__(self, k_nearest: int = 5):
        self.k_nearest = k_nearest

    def calculate(
        self, new_embedding: list[float], existing_embeddings: list[list[float]]
    ) -> float:
        """Calculate surprise score."""
        return calculate_surprise(
            new_embedding, existing_embeddings, k_nearest=self.k_nearest
        )
