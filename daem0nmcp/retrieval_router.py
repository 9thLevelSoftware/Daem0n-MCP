"""
Retrieval Router - Auto-Zoom query-aware search dispatch.

Routes search queries to the optimal retrieval strategy based on
ExemplarQueryClassifier output:

- SIMPLE  -> vector-only search via Qdrant (fast path, skips BM25+fusion)
- MEDIUM  -> hybrid BM25+vector with RRF fusion (current default baseline)
- COMPLEX -> GraphRAG multi-hop traversal + community summaries

After retrieval, optional JIT compression fires on assembled result text
when token counts exceed tiered thresholds (soft 4K / hard 8K / emergency 16K).
Compression metadata is returned alongside results for observability.

Safety guarantees:
- Shadow mode (default): logs classifications without changing behavior
- All strategy methods fall back to hybrid search on any failure
- Low-confidence classifications default to hybrid (never degrades below baseline)
- JIT compression failures never block the pipeline (try/except with fallback)
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from . import vectors
from .config import settings
from .query_classifier import ExemplarQueryClassifier
from .recall_planner import QueryComplexity

if TYPE_CHECKING:
    from .communities import CommunityManager
    from .graph.knowledge_graph import KnowledgeGraph
    from .memory import MemoryManager

logger = logging.getLogger(__name__)


class RetrievalRouter:
    """Dispatches search queries to the optimal retrieval strategy.

    Constructor accepts:
        memory_manager: MemoryManager instance (for _hybrid_search, _qdrant access)
        knowledge_graph: Optional KnowledgeGraph for complex queries
        community_manager: Optional CommunityManager for community summaries
        classifier: Optional ExemplarQueryClassifier (creates default if None)
    """

    def __init__(
        self,
        memory_manager: MemoryManager,
        knowledge_graph: KnowledgeGraph | None = None,
        community_manager: CommunityManager | None = None,
        classifier: ExemplarQueryClassifier | None = None,
    ) -> None:
        self._mm = memory_manager
        self._kg = knowledge_graph
        self._cm = community_manager
        self._classifier = classifier or ExemplarQueryClassifier(
            confidence_threshold=settings.auto_zoom_confidence_threshold,
        )

    # ------------------------------------------------------------------
    # Core dispatch
    # ------------------------------------------------------------------

    async def route_search(
        self,
        query: str,
        top_k: int = 10,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """Route *query* to the optimal retrieval strategy.

        Returns a dict with:
            results           -- list of (memory_id, score) tuples
            strategy_used     -- "vector_only" | "hybrid" | "graphrag"
            classification    -- {"level": str, "confidence": float, "scores": dict}
            community_context -- list | None (only for graphrag)
        """

        # ----- Disabled mode (both False) ----- #
        if not settings.auto_zoom_enabled and not settings.auto_zoom_shadow:
            results = self._hybrid_search(query, top_k, **kwargs)
            return {
                "results": results,
                "strategy_used": "hybrid",
                "classification": None,
                "community_context": None,
            }

        # ----- Classify the query ----- #
        try:
            level, confidence, scores = self._classifier.classify(query)
        except Exception:
            logger.warning(
                "[AUTO-ZOOM] Classifier failed, falling back to hybrid",
                exc_info=True,
            )
            results = self._hybrid_search(query, top_k, **kwargs)
            return {
                "results": results,
                "strategy_used": "hybrid",
                "classification": None,
                "community_context": None,
            }
        classification_info: dict[str, Any] = {
            "level": level.value,
            "confidence": confidence,
            "scores": scores,
        }

        # ----- Shadow mode: log only, always use hybrid ----- #
        if settings.auto_zoom_shadow and not settings.auto_zoom_enabled:
            logger.debug(
                "[AUTO-ZOOM SHADOW] query=%r classified=%s conf=%.3f scores=%s",
                query[:80],
                level.value,
                confidence,
                scores,
            )
            results = self._hybrid_search(query, top_k, **kwargs)
            return {
                "results": results,
                "strategy_used": "hybrid",
                "classification": classification_info,
                "shadow_classification": classification_info,
                "community_context": None,
            }

        # ----- Active mode: dispatch based on classification ----- #
        # Low confidence -> fall back to hybrid
        if confidence < settings.auto_zoom_confidence_threshold:
            logger.debug(
                "[AUTO-ZOOM] Low confidence %.3f < %.3f, falling back to hybrid",
                confidence,
                settings.auto_zoom_confidence_threshold,
            )
            results = self._hybrid_search(query, top_k, **kwargs)
            return {
                "results": results,
                "strategy_used": "hybrid",
                "classification": classification_info,
                "community_context": None,
            }

        strategy_used = "hybrid"
        results: list[tuple[int, float]] = []
        community_context: list[Any] | None = None

        if level == QueryComplexity.SIMPLE:
            results = self._vector_only_search(query, top_k, **kwargs)
            strategy_used = "vector_only"
        elif level == QueryComplexity.COMPLEX:
            results, community_context = await self._graphrag_search(
                query, top_k, **kwargs
            )
            strategy_used = "graphrag"
        else:
            # MEDIUM or fallback
            results = self._hybrid_search(query, top_k, **kwargs)
            strategy_used = "hybrid"

        return {
            "results": results,
            "strategy_used": strategy_used,
            "classification": classification_info,
            "community_context": community_context,
        }

    # ------------------------------------------------------------------
    # JIT compression on assembled results
    # ------------------------------------------------------------------

    async def route_and_compress(
        self,
        query: str,
        top_k: int = 10,
        result_text: str | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """Route query and optionally JIT-compress assembled result text.

        Calls :meth:`route_search` for classification and retrieval, then
        runs JIT compression on *result_text* if it exceeds the soft token
        threshold.  Compression metadata is added to the return dict so
        callers can observe what happened without destroying structured data.

        Args:
            query: Search query text.
            top_k: Maximum results to return.
            result_text: Assembled text of retrieval results (all memory
                content concatenated).  When ``None``, compression is skipped.
            **kwargs: Forwarded to :meth:`route_search`.

        Returns:
            Dict from ``route_search`` augmented with:
                compression_metadata -- dict or ``None``
                compressed_text      -- str (only when compression fired)
        """
        result = await self.route_search(query, top_k=top_k, **kwargs)

        # JIT compression on assembled text (best-effort, never blocks pipeline)
        if result_text:
            try:
                from .compression.jit import get_jit_compressor

                jit = get_jit_compressor()
                compression_result = jit.compress_if_needed(result_text)

                if compression_result.get("threshold_triggered") is not None:
                    result["compression_metadata"] = {
                        "original_tokens": compression_result["original_tokens"],
                        "compressed_tokens": compression_result["compressed_tokens"],
                        "compression_rate": compression_result["compression_rate"],
                        "threshold_triggered": compression_result[
                            "threshold_triggered"
                        ],
                        "content_type": compression_result.get("content_type"),
                    }
                    result["compressed_text"] = compression_result["text"]
                else:
                    result["compression_metadata"] = None
            except Exception:
                logger.warning(
                    "[AUTO-ZOOM] JIT compression failed, continuing without compression",
                    exc_info=True,
                )
                result["compression_metadata"] = None

        return result

    # ------------------------------------------------------------------
    # Strategy: vector-only (SIMPLE queries)
    # ------------------------------------------------------------------

    def _vector_only_search(
        self,
        query: str,
        top_k: int,
        **kwargs: Any,
    ) -> list[tuple[int, float]]:
        """Fast path: Qdrant vector search only, no BM25/fusion overhead."""
        try:
            qdrant = getattr(self._mm, "_qdrant", None)
            if qdrant is None:
                logger.debug(
                    "[AUTO-ZOOM] No Qdrant store available, falling back to hybrid"
                )
                return self._hybrid_search(query, top_k, **kwargs)

            query_embedding_bytes = vectors.encode_query(query)
            if not query_embedding_bytes:
                logger.debug(
                    "[AUTO-ZOOM] Vector encoding failed, falling back to hybrid"
                )
                return self._hybrid_search(query, top_k, **kwargs)

            query_vector = vectors.decode(query_embedding_bytes)
            if not query_vector:
                return self._hybrid_search(query, top_k, **kwargs)

            results = qdrant.search(query_vector=query_vector, limit=top_k)
            if not results:
                return self._hybrid_search(query, top_k, **kwargs)

            return results

        except Exception:
            logger.warning(
                "[AUTO-ZOOM] Vector-only search failed, falling back to hybrid",
                exc_info=True,
            )
            return self._hybrid_search(query, top_k, **kwargs)

    # ------------------------------------------------------------------
    # Strategy: hybrid (MEDIUM queries -- current baseline)
    # ------------------------------------------------------------------

    def _hybrid_search(
        self,
        query: str,
        top_k: int,
        **kwargs: Any,
    ) -> list[tuple[int, float]]:
        """Baseline: BM25 + vector hybrid search via MemoryManager."""
        return self._mm._hybrid_search(query, top_k=top_k)

    # ------------------------------------------------------------------
    # Strategy: GraphRAG multi-hop (COMPLEX queries)
    # ------------------------------------------------------------------

    async def _graphrag_search(
        self,
        query: str,
        top_k: int,
        **kwargs: Any,
    ) -> tuple[list[tuple[int, float]], list[Any] | None]:
        """Rich path: hybrid seeds + graph expansion + community summaries.

        Returns (results, community_context).  Any failure falls back to
        hybrid results with a logged warning.
        """
        # Step 1: seed results from hybrid search
        seeds = self._hybrid_search(query, top_k, **kwargs)

        # Step 2: graph expansion (if KnowledgeGraph available)
        expanded_results = list(seeds)  # copy
        try:
            kg = getattr(self._mm, "_knowledge_graph", None) or self._kg
            if kg is not None:
                await kg.ensure_loaded()

                # Check graph is non-empty
                graph = kg._graph
                if graph.number_of_nodes() > 0:
                    from .graph.traversal import find_related_memories

                    seen_ids = {mid for mid, _ in seeds}
                    max_depth = settings.auto_zoom_graph_expansion_depth

                    # Expand from top-5 seed memories
                    for memory_id, seed_score in seeds[:5]:
                        related = await find_related_memories(
                            graph, memory_id, max_depth=max_depth
                        )
                        if not related.get("found"):
                            continue

                        for _rel_type, entries in related.get(
                            "by_relationship", {}
                        ).items():
                            for entry in entries:
                                rid = entry["memory_id"]
                                if rid not in seen_ids:
                                    seen_ids.add(rid)
                                    # Score boost: closer = higher, depth
                                    # discount = seed_score * 0.8^depth
                                    depth = entry.get("depth", 1)
                                    boost = seed_score * (0.8**depth)
                                    expanded_results.append((rid, boost))

                    # Re-sort by score descending
                    expanded_results.sort(key=lambda x: x[1], reverse=True)
                    expanded_results = expanded_results[:top_k]
                else:
                    logger.warning(
                        "[AUTO-ZOOM] KnowledgeGraph is empty, using hybrid seeds"
                    )
        except Exception:
            logger.warning(
                "[AUTO-ZOOM] Graph expansion failed, using hybrid seeds",
                exc_info=True,
            )
            expanded_results = list(seeds)

        # Step 3: community summaries (if community_manager available)
        community_context: list[Any] | None = None
        try:
            if self._cm is not None:
                # Get communities for project (best-effort)
                project_path = getattr(
                    getattr(self._mm, "db", None), "storage_path", None
                )
                if project_path:
                    communities = await self._cm.get_communities(
                        project_path=project_path
                    )
                    if communities:
                        community_context = [
                            {
                                "name": c.get("name") or getattr(c, "name", ""),
                                "summary": c.get("summary")
                                or getattr(c, "summary", ""),
                            }
                            for c in communities[:5]
                        ]
        except Exception:
            logger.debug(
                "[AUTO-ZOOM] Community summary retrieval failed", exc_info=True
            )

        return expanded_results, community_context
