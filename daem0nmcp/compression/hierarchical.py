"""
Hierarchical Context Manager - Leverage Leiden communities for pre-compressed context.

Implements CONTEXT-04: Hierarchical compression leverages Phase 1 community structure.

For simple queries, community summaries ARE the compressed context (no LLMLingua needed).
For complex queries, retrieves raw memories and applies adaptive compression.
"""

import logging
from typing import Any

from ..recall_planner import QueryComplexity, RecallPlan, RecallPlanner
from .adaptive import AdaptiveCompressor

logger = logging.getLogger(__name__)


class HierarchicalContextManager:
    """
    Manages context retrieval with hierarchical compression.

    Strategy by query complexity:
    - SIMPLE: Return community summaries only (already compressed by design)
    - MEDIUM: Community summaries + selective raw memories, moderate compression
    - COMPLEX: Full raw memories, adaptive compression

    This leverages Phase 1's Leiden community summaries as the "first tier"
    of compression - they're human-readable summaries that don't need
    LLMLingua processing.

    NOTE: JIT compression (compression/jit.py) is now the preferred compression
    point in the retrieval pipeline. When JIT is active, HierarchicalContextManager
    should be used for context assembly only (choosing between summaries and raw
    memories) without applying its own compression. The JIT layer handles all
    token-threshold-based compression with tiered rates and metadata output.
    Pass skip_compression=True to get_context() when JIT is the compression point.

    Usage:
        manager = HierarchicalContextManager(
            compressor=AdaptiveCompressor(),
            recall_planner=RecallPlanner(),
        )
        context = manager.get_context(
            query="what is the auth flow?",
            memories=retrieved_memories,
            community_summaries=summaries,
        )

        # With JIT compression (skip internal compression):
        context = manager.get_context(
            query="what is the auth flow?",
            memories=retrieved_memories,
            community_summaries=summaries,
            skip_compression=True,
        )
    """

    def __init__(
        self,
        compressor: AdaptiveCompressor | None = None,
        recall_planner: RecallPlanner | None = None,
    ):
        """
        Initialize with compressor and planner.

        Args:
            compressor: AdaptiveCompressor for content-aware compression.
            recall_planner: RecallPlanner for query classification.
        """
        self.compressor = compressor or AdaptiveCompressor()
        self.recall_planner = recall_planner or RecallPlanner()

    def get_context(
        self,
        query: str,
        memories: list[dict[str, Any]],
        community_summaries: list[str] | None = None,
        plan: RecallPlan | None = None,
        skip_compression: bool = False,
    ) -> dict[str, Any]:
        """
        Get optimized context for the query.

        Chooses between community summaries and compressed raw memories
        based on query complexity.

        Args:
            query: The search query
            memories: Retrieved memory dicts (with 'content' field)
            community_summaries: Pre-computed Leiden community summaries
            plan: Optional RecallPlan. Computes from query if None.
            skip_compression: If True, skip internal compression (for JIT callers).
                              When JIT compression is the compression point, pass True
                              to prevent double-compression.

        Returns:
            Dict with:
                - context: The optimized context string
                - strategy: "summaries", "compressed", or "hybrid"
                - compression_applied: Whether LLMLingua was used
                - token_count: Approximate token count
        """
        # Get or compute plan
        if plan is None:
            plan = self.recall_planner.plan_recall(query)

        # Format raw memories
        raw_context = self._format_memories(memories)

        # Strategy based on complexity
        if plan.complexity == QueryComplexity.SIMPLE:
            return self._simple_strategy(community_summaries, raw_context)

        elif plan.complexity == QueryComplexity.MEDIUM:
            return self._medium_strategy(
                community_summaries, raw_context, plan, skip_compression
            )

        else:  # COMPLEX
            return self._complex_strategy(raw_context, plan, skip_compression)

    def _format_memories(self, memories: list[dict[str, Any]]) -> str:
        """Format memory list into context string."""
        if not memories:
            return ""

        lines = []
        for mem in memories:
            content = mem.get("content", "")
            category = mem.get("category", "memory")
            lines.append(f"[{category}] {content}")

        return "\n\n".join(lines)

    def _format_summaries(self, summaries: list[str] | None) -> str:
        """Format community summaries into context string."""
        if not summaries:
            return ""
        return "\n\n".join(summaries)

    def _simple_strategy(
        self,
        community_summaries: list[str] | None,
        raw_context: str,
    ) -> dict[str, Any]:
        """
        Simple query strategy: Use community summaries (pre-compressed).

        Falls back to raw context if no summaries available.
        """
        if community_summaries:
            context = self._format_summaries(community_summaries)
            return {
                "context": context,
                "strategy": "summaries",
                "compression_applied": False,
                "token_count": self.compressor.compressor.count_tokens(context),
            }
        else:
            # No summaries, return raw (simple queries are short anyway)
            return {
                "context": raw_context,
                "strategy": "raw_fallback",
                "compression_applied": False,
                "token_count": self.compressor.compressor.count_tokens(raw_context),
            }

    def _medium_strategy(
        self,
        community_summaries: list[str] | None,
        raw_context: str,
        plan: RecallPlan,
        skip_compression: bool = False,
    ) -> dict[str, Any]:
        """
        Medium query strategy: Hybrid summaries + compressed raw.

        Combines community context with selective raw memories,
        applies moderate compression if over threshold.

        Args:
            community_summaries: Pre-computed Leiden community summaries.
            raw_context: Formatted raw memory text.
            plan: RecallPlan with compression settings.
            skip_compression: If True, skip internal compression (JIT handles it).
        """
        # Combine summaries with raw
        summary_context = (
            self._format_summaries(community_summaries) if community_summaries else ""
        )
        combined = (
            f"{summary_context}\n\n---\n\n{raw_context}"
            if summary_context
            else raw_context
        )

        # Check if compression needed (skip when JIT is the compression point)
        if (
            not skip_compression
            and plan.compress
            and self.compressor.compressor.should_compress(combined)
        ):
            result = self.compressor.compress(
                combined, rate_override=plan.compression_rate
            )
            return {
                "context": result["compressed_prompt"],
                "strategy": "hybrid_compressed",
                "compression_applied": True,
                "token_count": result["compressed_tokens"],
                "original_tokens": result["original_tokens"],
                "compression_ratio": result["ratio"],
            }
        else:
            return {
                "context": combined,
                "strategy": "hybrid",
                "compression_applied": False,
                "token_count": self.compressor.compressor.count_tokens(combined),
            }

    def _complex_strategy(
        self,
        raw_context: str,
        plan: RecallPlan,
        skip_compression: bool = False,
    ) -> dict[str, Any]:
        """
        Complex query strategy: Full raw with adaptive compression.

        Uses content-aware compression for maximum detail retention.

        Args:
            raw_context: Formatted raw memory text.
            plan: RecallPlan with compression settings.
            skip_compression: If True, skip internal compression (JIT handles it).
        """
        if (
            not skip_compression
            and plan.compress
            and self.compressor.compressor.should_compress(raw_context)
        ):
            # Let AdaptiveCompressor detect content type
            result = self.compressor.compress(raw_context)
            return {
                "context": result["compressed_prompt"],
                "strategy": "compressed",
                "compression_applied": True,
                "content_type": result.get("content_type"),
                "token_count": result["compressed_tokens"],
                "original_tokens": result["original_tokens"],
                "compression_ratio": result["ratio"],
            }
        else:
            return {
                "context": raw_context,
                "strategy": "raw",
                "compression_applied": False,
                "token_count": self.compressor.compressor.count_tokens(raw_context),
            }
