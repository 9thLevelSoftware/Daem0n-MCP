# daem0nmcp/fusion.py
"""
Reciprocal Rank Fusion - Combines multiple ranked lists into one.

RRF formula: score(d) = Σ 1 / (k + rank(d))
where k is a constant (typically 60) and rank starts at 1.
"""


from .bm25_index import BM25Index
from .vectors import VectorIndex


def reciprocal_rank_fusion(
    ranked_lists: list[list[tuple[int, float]]], k: int = 60
) -> list[tuple[int, float]]:
    """
    Combine multiple ranked lists using Reciprocal Rank Fusion.

    Args:
        ranked_lists: List of result lists, each containing (doc_id, score) tuples
        k: Constant to dampen high-ranking effects (default: 60)

    Returns:
        Fused list of (doc_id, rrf_score) tuples, sorted by score descending.
    """
    rrf_scores: dict[int, float] = {}

    for results in ranked_lists:
        for rank, (doc_id, _) in enumerate(results, start=1):
            if doc_id not in rrf_scores:
                rrf_scores[doc_id] = 0.0
            rrf_scores[doc_id] += 1.0 / (k + rank)

    fused = [(doc_id, score) for doc_id, score in rrf_scores.items()]
    fused.sort(key=lambda x: x[1], reverse=True)
    return fused


class RRFHybridSearch:
    """
    Hybrid search combining BM25 and vector similarity using RRF.

    Runs both retrieval methods in parallel and fuses results.
    """

    def __init__(
        self,
        bm25_index: BM25Index,
        vector_index: VectorIndex | None = None,
        k: int = 60,
    ):
        self.bm25 = bm25_index
        self.vectors = vector_index
        self.k = k

    def search(
        self,
        query: str,
        top_k: int = 10,
        bm25_candidates: int = 50,
        vector_candidates: int = 50,
        bm25_threshold: float = 0.0,
        vector_threshold: float = 0.3,
    ) -> list[tuple[int, float]]:
        """
        Hybrid search with RRF fusion.

        Args:
            query: Search query
            top_k: Final results to return
            bm25_candidates: BM25 candidates before fusion
            vector_candidates: Vector candidates before fusion
            bm25_threshold: Minimum BM25 score
            vector_threshold: Minimum vector similarity

        Returns:
            List of (doc_id, rrf_score) tuples.
        """
        ranked_lists = []

        # Get BM25 results
        bm25_results = self.bm25.search(
            query, top_k=bm25_candidates, threshold=bm25_threshold
        )
        if bm25_results:
            ranked_lists.append(bm25_results)

        # Get vector results if available
        if self.vectors and len(self.vectors) > 0:
            vector_results = self.vectors.search(
                query, top_k=vector_candidates, threshold=vector_threshold
            )
            if vector_results:
                ranked_lists.append(vector_results)

        if not ranked_lists:
            return []

        # Fuse and return top_k
        fused = reciprocal_rank_fusion(ranked_lists, k=self.k)
        return fused[:top_k]
