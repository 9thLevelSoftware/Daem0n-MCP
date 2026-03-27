"""
Explore workflow - Graph & discovery.

Actions:
- related: Find memories related via graph traversal
- chain: Find causal paths between two memories
- graph: Get subgraph of memories and relationships
- stats: Get knowledge graph metrics
- communities: List memory communities
- community_detail: Get full community details
- rebuild_communities: Detect memory communities using Leiden algorithm
- entities: List most frequently mentioned entities
- backfill_entities: Extract entities from all existing memories
- evolution: Trace how knowledge about an entity evolved
- versions: Get version history of a memory
- at_time: Get memory state at a specific point in time
"""

from typing import Any

from .errors import InvalidActionError, MissingParamError

VALID_ACTIONS = frozenset(
    {
        "related",
        "chain",
        "graph",
        "stats",
        "communities",
        "community_detail",
        "rebuild_communities",
        "entities",
        "backfill_entities",
        "evolution",
        "versions",
        "at_time",
    }
)


async def dispatch(
    action: str,
    project_path: str,
    *,
    # related params
    memory_id: int | None = None,
    relationship_types: list[str] | None = None,
    direction: str = "both",
    max_depth: int = 2,
    # chain params
    start_memory_id: int | None = None,
    end_memory_id: int | None = None,
    # graph params
    memory_ids: list[int] | None = None,
    topic: str | None = None,
    format: str = "json",
    visual: bool = False,
    include_orphans: bool = False,
    # communities params
    level: int | None = None,
    parent_community_id: int | None = None,
    # community_detail params
    community_id: int | None = None,
    # rebuild_communities params
    min_community_size: int = 2,
    resolution: float = 1.0,
    # entities params
    entity_type: str | None = None,
    limit: int = 20,
    # evolution params
    entity_name: str | None = None,
    include_invalidated: bool = True,
    entity_id: int | None = None,
    # versions params (uses memory_id)
    # at_time params
    timestamp: str | None = None,
    **kwargs,
) -> Any:
    """Dispatch action to appropriate handler."""
    if action not in VALID_ACTIONS:
        raise InvalidActionError(action, sorted(VALID_ACTIONS))

    if action == "related":
        if memory_id is None:
            raise MissingParamError("memory_id", action)
        return await _do_related(
            project_path, memory_id, relationship_types, direction, max_depth
        )

    elif action == "chain":
        if start_memory_id is None:
            raise MissingParamError("start_memory_id", action)
        if end_memory_id is None:
            raise MissingParamError("end_memory_id", action)
        return await _do_chain(project_path, start_memory_id, end_memory_id, max_depth)

    elif action == "graph":
        return await _do_graph(
            project_path, memory_ids, topic, format, visual, include_orphans
        )

    elif action == "stats":
        return await _do_stats(project_path)

    elif action == "communities":
        return await _do_communities(project_path, level, parent_community_id, visual)

    elif action == "community_detail":
        if community_id is None:
            raise MissingParamError("community_id", action)
        return await _do_community_detail(project_path, community_id)

    elif action == "rebuild_communities":
        return await _do_rebuild_communities(
            project_path, min_community_size, resolution
        )

    elif action == "entities":
        return await _do_entities(project_path, entity_type, limit)

    elif action == "backfill_entities":
        return await _do_backfill_entities(project_path)

    elif action == "evolution":
        return await _do_evolution(
            project_path,
            entity_name,
            entity_type,
            include_invalidated,
            entity_id,
        )

    elif action == "versions":
        if memory_id is None:
            raise MissingParamError("memory_id", action)
        return await _do_versions(project_path, memory_id, limit)

    elif action == "at_time":
        if memory_id is None:
            raise MissingParamError("memory_id", action)
        if not timestamp:
            raise MissingParamError("timestamp", action)
        return await _do_at_time(project_path, memory_id, timestamp)

    raise InvalidActionError(action, sorted(VALID_ACTIONS))


async def _do_related(
    project_path: str,
    memory_id: int,
    relationship_types: list[str] | None,
    direction: str,
    max_depth: int,
) -> dict[str, Any]:
    """Find memories related via graph traversal."""
    from ..server import get_related_memories

    return await get_related_memories(
        memory_id=memory_id,
        relationship_types=relationship_types,
        direction=direction,
        max_depth=max_depth,
        project_path=project_path,
    )


async def _do_chain(
    project_path: str,
    start_memory_id: int,
    end_memory_id: int,
    max_depth: int,
) -> dict[str, Any]:
    """Find causal paths between two memories."""
    from ..server import trace_chain

    return await trace_chain(
        start_memory_id=start_memory_id,
        end_memory_id=end_memory_id,
        max_depth=max_depth,
        project_path=project_path,
    )


async def _do_graph(
    project_path: str,
    memory_ids: list[int] | None,
    topic: str | None,
    format: str,
    visual: bool,
    include_orphans: bool,
) -> dict[str, Any]:
    """Get subgraph of memories and relationships."""
    from ..server import get_graph, get_graph_visual

    if visual:
        return await get_graph_visual(
            memory_ids=memory_ids,
            topic=topic,
            include_orphans=include_orphans,
            project_path=project_path,
        )
    return await get_graph(
        memory_ids=memory_ids,
        topic=topic,
        format=format,
        project_path=project_path,
    )


async def _do_stats(project_path: str) -> dict[str, Any]:
    """Get knowledge graph metrics."""
    from ..server import get_graph_stats

    return await get_graph_stats(project_path=project_path)


async def _do_communities(
    project_path: str,
    level: int | None,
    parent_community_id: int | None,
    visual: bool,
) -> dict[str, Any]:
    """List memory communities."""
    from ..server import list_communities, list_communities_visual

    if visual:
        return await list_communities_visual(
            level=level,
            parent_community_id=parent_community_id,
            project_path=project_path,
        )
    return await list_communities(level=level, project_path=project_path)


async def _do_community_detail(project_path: str, community_id: int) -> dict[str, Any]:
    """Get full community details."""
    from ..server import get_community_details

    return await get_community_details(
        community_id=community_id, project_path=project_path
    )


async def _do_rebuild_communities(
    project_path: str, min_community_size: int, resolution: float
) -> dict[str, Any]:
    """Detect memory communities using Leiden algorithm."""
    from ..server import rebuild_communities

    return await rebuild_communities(
        min_community_size=min_community_size,
        resolution=resolution,
        project_path=project_path,
    )


async def _do_entities(
    project_path: str, entity_type: str | None, limit: int
) -> dict[str, Any]:
    """List most frequently mentioned entities."""
    from ..server import list_entities

    return await list_entities(
        entity_type=entity_type, limit=limit, project_path=project_path
    )


async def _do_backfill_entities(project_path: str) -> dict[str, Any]:
    """Extract entities from all existing memories."""
    from ..server import backfill_entities

    return await backfill_entities(project_path=project_path)


async def _do_evolution(
    project_path: str,
    entity_name: str | None,
    entity_type: str | None,
    include_invalidated: bool,
    entity_id: int | None,
) -> dict[str, Any]:
    """Trace how knowledge about an entity evolved."""
    from ..server import trace_evolution

    return await trace_evolution(
        entity_name=entity_name,
        entity_type=entity_type,
        include_invalidated=include_invalidated,
        entity_id=entity_id,
        project_path=project_path,
    )


async def _do_versions(project_path: str, memory_id: int, limit: int) -> dict[str, Any]:
    """Get version history of a memory."""
    from ..server import get_memory_versions

    return await get_memory_versions(
        memory_id=memory_id, limit=limit, project_path=project_path
    )


async def _do_at_time(
    project_path: str, memory_id: int, timestamp: str
) -> dict[str, Any]:
    """Get memory state at a specific point in time."""
    from ..server import get_memory_at_time

    return await get_memory_at_time(
        memory_id=memory_id, timestamp=timestamp, project_path=project_path
    )
