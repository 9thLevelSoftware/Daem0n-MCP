"""Leiden community detection with NetworkX-igraph bridge."""

import logging
from dataclasses import dataclass
from typing import Any

import networkx as nx

logger = logging.getLogger(__name__)


@dataclass
class LeidenConfig:
    """Configuration for Leiden algorithm."""

    resolution: float = 1.0  # >1 = smaller communities, <1 = larger
    seed: int = 42  # Deterministic seeding for stability
    n_iterations: int = -1  # -1 = run until convergence
    partition_type: str = "modularity"  # "modularity" or "cpm"


def run_leiden_on_networkx(
    nx_graph: nx.Graph,
    config: LeidenConfig | None = None,
) -> dict[str, int]:
    """
    Run Leiden community detection on a NetworkX graph.

    Args:
        nx_graph: NetworkX graph (will be converted to undirected for clustering)
        config: Leiden configuration options

    Returns:
        Dict mapping node_id -> community_id
    """
    import igraph as ig
    import leidenalg as la

    if config is None:
        config = LeidenConfig()

    if nx_graph.number_of_nodes() == 0:
        logger.debug("Empty graph, returning empty community mapping")
        return {}

    # Convert to undirected for community detection
    if nx_graph.is_directed():
        undirected = nx_graph.to_undirected()
    else:
        undirected = nx_graph

    # Convert NetworkX to igraph
    # Note: ig.Graph.from_networkx preserves node attributes
    ig_graph = ig.Graph.from_networkx(undirected)

    # Select partition type
    if config.partition_type == "cpm":
        partition_class = la.CPMVertexPartition
        kwargs: dict[str, Any] = {"resolution_parameter": config.resolution}
    else:
        partition_class = la.ModularityVertexPartition
        kwargs = {}

    # Run Leiden with deterministic seed
    partition = la.find_partition(
        ig_graph,
        partition_class,
        seed=config.seed,
        n_iterations=config.n_iterations,
        **kwargs,
    )

    # Map back to original node IDs
    # igraph preserves node order from networkx
    node_list = list(undirected.nodes())
    community_map = {
        node_list[i]: partition.membership[i] for i in range(len(node_list))
    }

    logger.info(
        f"Leiden found {len(set(partition.membership))} communities "
        f"in graph with {len(node_list)} nodes "
        f"(modularity: {partition.modularity:.4f})"
    )

    return community_map


def get_community_stats(community_map: dict[str, int]) -> dict[str, Any]:
    """Get statistics about detected communities."""
    from collections import Counter

    if not community_map:
        return {"num_communities": 0, "sizes": [], "avg_size": 0}

    community_sizes = Counter(community_map.values())

    return {
        "num_communities": len(community_sizes),
        "sizes": sorted(community_sizes.values(), reverse=True),
        "avg_size": len(community_map) / len(community_sizes) if community_sizes else 0,
        "largest_community": max(community_sizes.values()) if community_sizes else 0,
        "smallest_community": min(community_sizes.values()) if community_sizes else 0,
    }


def get_nodes_in_community(
    community_map: dict[str, int],
    community_id: int,
) -> list[str]:
    """Get all node IDs belonging to a specific community."""
    return [node for node, comm in community_map.items() if comm == community_id]
