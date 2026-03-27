"""Graph module for GraphRAG knowledge graph operations."""

from .contradiction import (
    Contradiction,
    check_and_invalidate_contradictions,
    detect_contradictions,
    has_negation_mismatch,
    invalidate_contradicted_facts,
)
from .entity_resolver import EntityResolver
from .knowledge_graph import KnowledgeGraph
from .leiden import LeidenConfig, get_community_stats, run_leiden_on_networkx
from .summarizer import CommunitySummarizer, SummaryConfig
from .temporal import (
    create_temporal_version,
    get_versions_at_time,
    invalidate_version,
    trace_knowledge_evolution,
)
from .traversal import (
    find_related_memories,
    get_graph_metrics,
    trace_causal_chain,
)
from .traversal import (
    trace_knowledge_evolution as trace_knowledge_evolution_graph,
)

__all__ = [
    "KnowledgeGraph",
    "EntityResolver",
    "run_leiden_on_networkx",
    "LeidenConfig",
    "get_community_stats",
    "trace_causal_chain",
    "find_related_memories",
    "trace_knowledge_evolution",
    "get_graph_metrics",
    "CommunitySummarizer",
    "SummaryConfig",
    "create_temporal_version",
    "get_versions_at_time",
    "invalidate_version",
    "Contradiction",
    "detect_contradictions",
    "invalidate_contradicted_facts",
    "check_and_invalidate_contradictions",
    "has_negation_mismatch",
    "trace_knowledge_evolution_graph",
]
