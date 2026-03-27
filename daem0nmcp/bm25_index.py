# daem0nmcp/bm25_index.py
"""
BM25 Index - Okapi BM25 for keyword-based retrieval.

Replaces TF-IDF for better term frequency saturation and document length normalization.
"""


from rank_bm25 import BM25Okapi

from .similarity import tokenize


class BM25Index:
    """
    BM25 index for document retrieval.

    Uses Okapi BM25 algorithm with:
    - k1=1.5 (term frequency saturation)
    - b=0.75 (document length normalization)
    """

    def __init__(self, k1: float = 1.5, b: float = 0.75):
        self.k1 = k1
        self.b = b
        self.documents: dict[int, list[str]] = {}  # doc_id -> tokens
        self._doc_id_list: list[int] = []  # Ordered list for BM25 index alignment
        self._bm25: BM25Okapi | None = None
        self._dirty = True  # Rebuild index when True

    def add_document(
        self, doc_id: int, text: str, tags: list[str] | None = None
    ) -> None:
        """Add a document to the index."""
        tokens = tokenize(text)

        # Add tags with boosted weight
        if tags:
            for tag in tags:
                tag_tokens = tokenize(tag)
                tokens.extend(tag_tokens * 3)

        self.documents[doc_id] = tokens
        self._dirty = True

    def remove_document(self, doc_id: int) -> None:
        """Remove a document from the index."""
        if doc_id in self.documents:
            del self.documents[doc_id]
            self._dirty = True

    def _rebuild_index(self) -> None:
        """Rebuild BM25 index from documents."""
        if not self.documents:
            self._bm25 = None
            self._doc_id_list = []
            self._dirty = False
            return

        self._doc_id_list = list(self.documents.keys())
        corpus = [self.documents[doc_id] for doc_id in self._doc_id_list]
        self._bm25 = BM25Okapi(corpus, k1=self.k1, b=self.b)
        self._dirty = False

    def get_scores(self, query: str) -> dict[int, float]:
        """Get BM25 scores for all documents."""
        if self._dirty:
            self._rebuild_index()

        if not self._bm25 or not self._doc_id_list:
            return {}

        query_tokens = tokenize(query)
        if not query_tokens:
            return {}

        scores = self._bm25.get_scores(query_tokens)
        return {
            self._doc_id_list[i]: float(scores[i])
            for i in range(len(self._doc_id_list))
        }

    def search(
        self, query: str, top_k: int = 10, threshold: float = 0.0
    ) -> list[tuple[int, float]]:
        """
        Search for documents similar to the query.

        Returns: List of (doc_id, score) tuples, sorted by score descending.
        """
        scores = self.get_scores(query)

        results = [
            (doc_id, score) for doc_id, score in scores.items() if score > threshold
        ]

        results.sort(key=lambda x: x[1], reverse=True)
        return results[:top_k]

    def __len__(self) -> int:
        return len(self.documents)
