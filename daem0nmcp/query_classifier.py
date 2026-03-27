"""
Exemplar-embedding query classifier.

Classifies search query complexity (simple/medium/complex) using cosine
similarity to pre-defined exemplar description embeddings.  Replaces the
regex-based classifier in recall_planner.py with a semantic classifier
that correctly handles queries that are syntactically simple but
semantically complex (e.g. "authentication cascade" is complex despite
being 2 words).

Key design choices:
- Reuses the shared embedding model via vectors._get_model()
  instead of loading a second instance.
- Exemplar embeddings are computed lazily on first classify() call,
  not at import time (no startup penalty).
- Falls back to MEDIUM when confidence is below threshold.
"""

from __future__ import annotations

import logging

import numpy as np
from sentence_transformers.util import cos_sim

from .config import settings
from .recall_planner import QueryComplexity
from .vectors import _get_model

logger = logging.getLogger(__name__)


class ExemplarQueryClassifier:
    """Semantic query-complexity classifier using exemplar embeddings."""

    EXEMPLARS: dict[QueryComplexity, list[str]] = {
        QueryComplexity.SIMPLE: [
            "what is this",
            "define a concept",
            "look up a specific name or term",
            "find information about one topic",
            "explain a single concept briefly",
            "what does this function do",
        ],
        QueryComplexity.MEDIUM: [
            "how does X relate to Y",
            "find patterns about a topic across memories",
            "search for decisions about multiple concepts",
            "compare different approaches or tradeoffs",
            "what are the connections between these ideas",
            "find similar patterns and decisions",
        ],
        QueryComplexity.COMPLEX: [
            "trace the history of changes over time",
            "what caused this decision to lead to that outcome",
            "show the full chain from one decision to another result",
            "how has understanding of this topic evolved",
            "find all connected decisions and their cascading consequences",
            "explain the causal relationship across multiple steps",
        ],
    }

    FALLBACK_LEVEL = QueryComplexity.MEDIUM

    # ------------------------------------------------------------------
    # Construction & lazy init
    # ------------------------------------------------------------------

    def __init__(
        self,
        model=None,
        confidence_threshold: float = 0.25,
    ) -> None:
        self._model = model
        self._threshold = confidence_threshold
        self._initialized: bool = False
        self._exemplar_embeddings: dict[QueryComplexity, np.ndarray] | None = None

    def _ensure_initialized(self) -> None:
        """Compute exemplar embeddings on first use (lazy)."""
        if self._initialized:
            return

        if self._model is None:
            self._model = _get_model()

        embeddings: dict[QueryComplexity, np.ndarray] = {}
        for level, texts in self.EXEMPLARS.items():
            prefixed_texts = [f"{settings.embedding_query_prefix}{t}" for t in texts]
            embeddings[level] = self._model.encode(
                prefixed_texts, convert_to_numpy=True
            )

        self._exemplar_embeddings = embeddings
        self._initialized = True
        logger.debug("ExemplarQueryClassifier initialized (%d levels)", len(embeddings))

    # ------------------------------------------------------------------
    # Classification
    # ------------------------------------------------------------------

    def classify(self, query: str) -> tuple[QueryComplexity, float, dict[str, float]]:
        """Classify *query* complexity via cosine similarity to exemplars.

        Returns
        -------
        (level, confidence, scores_dict)
            *level*       -- best-matching QueryComplexity
            *confidence*  -- max cosine similarity score for that level
            *scores_dict* -- ``{"simple": float, "medium": float, "complex": float}``
        """
        self._ensure_initialized()

        query_embedding = self._model.encode(
            f"{settings.embedding_query_prefix}{query}", convert_to_numpy=True
        )

        scores: dict[str, float] = {}
        best_level = self.FALLBACK_LEVEL
        best_score = -1.0

        for level, exemplar_embs in self._exemplar_embeddings.items():
            similarities = cos_sim(query_embedding, exemplar_embs)
            max_sim = float(similarities.max())
            scores[level.value] = max_sim

            if max_sim > best_score:
                best_score = max_sim
                best_level = level

        if best_score < self._threshold:
            return (self.FALLBACK_LEVEL, best_score, scores)

        return (best_level, best_score, scores)


# ------------------------------------------------------------------
# Module-level convenience singleton
# ------------------------------------------------------------------

_default_classifier: ExemplarQueryClassifier | None = None


def get_classifier() -> ExemplarQueryClassifier:
    """Return (and lazily create) the module-level singleton classifier."""
    global _default_classifier
    if _default_classifier is None:
        _default_classifier = ExemplarQueryClassifier()
    return _default_classifier


def classify_query(
    query: str,
) -> tuple[QueryComplexity, float, dict[str, float]]:
    """Convenience wrapper around the singleton classifier."""
    return get_classifier().classify(query)
