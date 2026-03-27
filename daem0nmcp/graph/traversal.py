"""Multi-hop graph traversal for relationship queries."""

import logging
from collections import defaultdict
from typing import TYPE_CHECKING, Any, Optional

import networkx as nx

if TYPE_CHECKING:
    from ..database import DatabaseManager

logger = logging.getLogger(__name__)


async def trace_causal_chain(
    graph: nx.DiGraph,
    start_memory_id: int,
    end_memory_id: int,
    max_depth: int = 5,
) -> dict[str, Any]:
    """
    Find paths between two memories showing causal relationships.

    Answers: "How did decision X lead to outcome Y?"

    Args:
        graph: NetworkX graph from KnowledgeGraph
        start_memory_id: Starting memory ID
        end_memory_id: Target memory ID
        max_depth: Maximum path length

    Returns:
        Dict with paths found and relationship details
    """
    start_node = f"memory:{start_memory_id}"
    end_node = f"memory:{end_memory_id}"

    if start_node not in graph or end_node not in graph:
        return {
            "found": False,
            "error": "One or both memories not in graph",
            "paths": [],
        }

    try:
        # Find all simple paths up to max_depth
        paths = list(nx.all_simple_paths(graph, start_node, end_node, cutoff=max_depth))
    except nx.NetworkXNoPath:
        paths = []

    if not paths:
        return {
            "found": False,
            "paths": [],
            "message": (
                f"No path found between memories {start_memory_id} and "
                f"{end_memory_id} within {max_depth} hops"
            ),
        }

    # Format paths with relationship details
    formatted_paths = []
    for path in paths:
        path_details = []
        for i in range(len(path) - 1):
            source = path[i]
            target = path[i + 1]
            edge_data = graph.get_edge_data(source, target, default={})

            path_details.append(
                {
                    "from": source,
                    "to": target,
                    "relationship": edge_data.get("relationship", "related_to"),
                    "description": edge_data.get("description"),
                    "confidence": edge_data.get("confidence", 1.0),
                }
            )

        formatted_paths.append(
            {
                "length": len(path) - 1,
                "nodes": path,
                "edges": path_details,
            }
        )

    # Sort by path length (shortest first)
    formatted_paths.sort(key=lambda p: p["length"])

    return {
        "found": True,
        "path_count": len(formatted_paths),
        "shortest_path_length": formatted_paths[0]["length"] if formatted_paths else 0,
        "paths": formatted_paths,
    }


async def find_related_memories(
    graph: nx.DiGraph,
    memory_id: int,
    relationship_types: list[str] | None = None,
    direction: str = "both",
    max_depth: int = 2,
) -> dict[str, Any]:
    """
    Find memories related to a given memory by traversing edges.

    Args:
        graph: NetworkX graph from KnowledgeGraph
        memory_id: Starting memory ID
        relationship_types: Filter by these types (None = all)
        direction: "outgoing", "incoming", or "both"
        max_depth: Maximum traversal depth

    Returns:
        Dict with related memories grouped by relationship type
    """
    start_node = f"memory:{memory_id}"

    if start_node not in graph:
        return {
            "found": False,
            "error": f"Memory {memory_id} not in graph",
            "related": {},
        }

    related: dict[str, list[dict[str, Any]]] = defaultdict(list)
    visited: set[str] = {start_node}

    def get_neighbors_with_edges(node: str, depth: int) -> None:
        """Recursively get neighbors with edge info."""
        if depth > max_depth:
            return

        # Get edges based on direction
        edges: list[tuple] = []
        if direction in ("outgoing", "both"):
            for target in graph.successors(node):
                if target.startswith("memory:"):
                    edge_data = graph.get_edge_data(node, target, default={})
                    edges.append((target, edge_data, "outgoing"))

        if direction in ("incoming", "both"):
            for source in graph.predecessors(node):
                if source.startswith("memory:"):
                    edge_data = graph.get_edge_data(source, node, default={})
                    edges.append((source, edge_data, "incoming"))

        for neighbor, edge_data, dir_type in edges:
            if neighbor in visited:
                continue

            rel_type = edge_data.get("relationship", "related_to")

            # Filter by relationship types if specified
            if relationship_types and rel_type not in relationship_types:
                continue

            visited.add(neighbor)
            mem_id = int(neighbor.split(":")[1])

            related[rel_type].append(
                {
                    "memory_id": mem_id,
                    "direction": dir_type,
                    "confidence": edge_data.get("confidence", 1.0),
                    "description": edge_data.get("description"),
                    "depth": depth,
                }
            )

            # Continue traversal
            get_neighbors_with_edges(neighbor, depth + 1)

    get_neighbors_with_edges(start_node, 1)

    return {
        "found": True,
        "source_memory_id": memory_id,
        "total_related": sum(len(v) for v in related.values()),
        "by_relationship": dict(related),
    }


async def trace_knowledge_evolution(
    graph: nx.DiGraph,
    entity_id: int,
    db_manager: Optional["DatabaseManager"] = None,
) -> dict[str, Any]:
    """
    Trace how knowledge about an entity has evolved over time.

    Answers: "How has understanding of X changed?"

    Args:
        graph: NetworkX graph from KnowledgeGraph
        entity_id: Entity to trace
        db_manager: DatabaseManager for fetching memory details

    Returns:
        Dict with timeline of memories mentioning this entity
    """
    entity_node = f"entity:{entity_id}"

    if entity_node not in graph:
        return {
            "found": False,
            "error": f"Entity {entity_id} not in graph",
            "evolution": [],
        }

    # Find all memories that reference this entity
    memory_ids = []
    for predecessor in graph.predecessors(entity_node):
        if predecessor.startswith("memory:"):
            memory_ids.append(int(predecessor.split(":")[1]))

    if not memory_ids:
        return {
            "found": True,
            "entity_id": entity_id,
            "memory_count": 0,
            "evolution": [],
        }

    # If db_manager provided, fetch memory details and sort by time
    if db_manager:
        from sqlalchemy import select

        from ..models import Memory

        async with db_manager.get_session() as session:
            result = await session.execute(
                select(Memory)
                .where(Memory.id.in_(memory_ids))
                .order_by(Memory.created_at)
            )
            memories = result.scalars().all()

            evolution = []
            for mem in memories:
                content_preview = mem.content
                if len(content_preview) > 200:
                    content_preview = content_preview[:200] + "..."
                evolution.append(
                    {
                        "memory_id": mem.id,
                        "category": mem.category,
                        "content_preview": content_preview,
                        "created_at": (
                            mem.created_at.isoformat() if mem.created_at else None
                        ),
                        "outcome": mem.outcome,
                        "worked": mem.worked,
                    }
                )

            # Track supersession chains
            superseded = []
            for mem_node in [f"memory:{mid}" for mid in memory_ids]:
                for target in graph.successors(mem_node):
                    if target.startswith("memory:"):
                        edge_data = graph.get_edge_data(mem_node, target, default={})
                        if edge_data.get("relationship") == "supersedes":
                            superseded.append(
                                {
                                    "old_memory_id": int(mem_node.split(":")[1]),
                                    "new_memory_id": int(target.split(":")[1]),
                                }
                            )

            return {
                "found": True,
                "entity_id": entity_id,
                "memory_count": len(evolution),
                "evolution": evolution,
                "supersession_chain": superseded,
            }
    else:
        # No db_manager, return just IDs
        return {
            "found": True,
            "entity_id": entity_id,
            "memory_count": len(memory_ids),
            "memory_ids": memory_ids,
        }


def get_graph_metrics(graph: nx.DiGraph) -> dict[str, Any]:
    """Get metrics about the knowledge graph structure."""
    if graph.number_of_nodes() == 0:
        return {
            "nodes": 0,
            "edges": 0,
            "density": 0,
            "components": 0,
        }

    # Count node types
    memory_nodes = sum(1 for n in graph.nodes() if n.startswith("memory:"))
    entity_nodes = sum(1 for n in graph.nodes() if n.startswith("entity:"))

    # Count relationship types
    rel_counts: dict[str, int] = defaultdict(int)
    for _, _, data in graph.edges(data=True):
        rel_counts[data.get("relationship", "unknown")] += 1

    # Get connected components (on undirected version)
    undirected = graph.to_undirected()
    components = nx.number_connected_components(undirected)

    return {
        "nodes": graph.number_of_nodes(),
        "edges": graph.number_of_edges(),
        "memory_nodes": memory_nodes,
        "entity_nodes": entity_nodes,
        "density": nx.density(graph),
        "connected_components": components,
        "relationship_distribution": dict(rel_counts),
    }
