# daem0nmcp/recall_planner.py
"""
Recall Planner - TiMem-style complexity-aware retrieval.

Classifies query complexity and plans which memory levels to access:
- Simple queries → Community summaries only (Level 3)
- Medium queries → Summaries + key raw memories (Level 2-3)
- Complex queries → Full raw memory access (Level 1-3)

Reduces context by ~50% for simple queries.
"""

import re
from dataclasses import dataclass
from enum import Enum


class QueryComplexity(Enum):
    """Query complexity levels."""

    SIMPLE = "simple"  # Single concept, short queries
    MEDIUM = "medium"  # Multi-concept, moderate queries
    COMPLEX = "complex"  # Temporal, causal, or detailed queries


@dataclass
class RecallPlan:
    """Plan for memory retrieval based on query complexity."""

    complexity: QueryComplexity
    use_communities: bool  # Use community summaries
    use_raw_memories: bool  # Access raw memory level
    max_communities: int  # Max community summaries to return
    max_raw_memories: int  # Max raw memories to return
    filter_threshold: float  # Similarity threshold for filtering
    # Phase 4: Compression parameters
    compress: bool = False  # Whether to compress retrieved context
    compression_rate: float = 0.33  # Target compression rate (0.33 = 3x)


# Patterns indicating complex queries
COMPLEX_PATTERNS = [
    r"\b(trace|flow|chain|sequence|history|timeline)\b",
    r"\b(when|what time|last week|yesterday|before|after)\b",
    r"\b(why did|how did|what led to|what caused)\b",
    r"\b(all|every|complete|full)\b.*\b(decision|memory|pattern)\b",
]

# Patterns indicating simple queries
SIMPLE_PATTERNS = [
    r"^(what is|define|explain)\s+\w+\??$",
    r"^\w+\??$",  # Single word
]


def classify_query_complexity(query: str) -> QueryComplexity:
    """
    Classify query complexity based on patterns.

    Args:
        query: The search query

    Returns:
        QueryComplexity level
    """
    query_lower = query.lower().strip()

    # Check for complex patterns first
    for pattern in COMPLEX_PATTERNS:
        if re.search(pattern, query_lower):
            return QueryComplexity.COMPLEX

    # Check for simple patterns
    for pattern in SIMPLE_PATTERNS:
        if re.match(pattern, query_lower):
            return QueryComplexity.SIMPLE

    # Use heuristics for remaining queries
    word_count = len(query_lower.split())

    if word_count <= 3:
        return QueryComplexity.SIMPLE
    elif word_count <= 8:
        return QueryComplexity.MEDIUM
    else:
        return QueryComplexity.COMPLEX


class RecallPlanner:
    """
    Plans memory retrieval strategy based on query complexity.

    TiMem-inspired hierarchical access:
    - Level 3 (personas/summaries): Always checked first
    - Level 2 (session summaries): For medium+ queries
    - Level 1 (raw memories): For complex queries
    """

    def __init__(
        self,
        simple_max_communities: int = 3,
        simple_max_raw: int = 5,
        medium_max_communities: int = 5,
        medium_max_raw: int = 10,
        complex_max_communities: int = 10,
        complex_max_raw: int = 20,
    ):
        self.simple_max_communities = simple_max_communities
        self.simple_max_raw = simple_max_raw
        self.medium_max_communities = medium_max_communities
        self.medium_max_raw = medium_max_raw
        self.complex_max_communities = complex_max_communities
        self.complex_max_raw = complex_max_raw

    def plan_recall(
        self, query: str, complexity: QueryComplexity | None = None
    ) -> RecallPlan:
        """
        Create a recall plan for the given query.

        Args:
            query: Search query
            complexity: Override auto-classification

        Returns:
            RecallPlan with retrieval parameters
        """
        if complexity is None:
            complexity = classify_query_complexity(query)

        if complexity == QueryComplexity.SIMPLE:
            return RecallPlan(
                complexity=complexity,
                use_communities=True,
                use_raw_memories=True,
                max_communities=self.simple_max_communities,
                max_raw_memories=self.simple_max_raw,
                filter_threshold=0.5,  # Higher threshold for simple queries
                # Simple queries use community summaries (already compressed)
                compress=False,
                compression_rate=0.33,
            )
        elif complexity == QueryComplexity.MEDIUM:
            return RecallPlan(
                complexity=complexity,
                use_communities=True,
                use_raw_memories=True,
                max_communities=self.medium_max_communities,
                max_raw_memories=self.medium_max_raw,
                filter_threshold=0.3,
                # Medium queries may benefit from moderate compression
                compress=True,
                compression_rate=0.4,  # ~2.5x
            )
        else:  # COMPLEX
            return RecallPlan(
                complexity=complexity,
                use_communities=True,
                use_raw_memories=True,
                max_communities=self.complex_max_communities,
                max_raw_memories=self.complex_max_raw,
                filter_threshold=0.2,  # Lower threshold to get more context
                # Complex queries need context, use conservative compression
                compress=True,
                compression_rate=0.25,  # 4x, but will be tempered by adaptive
            )
