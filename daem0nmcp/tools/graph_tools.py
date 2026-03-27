"""Graph and community tools: link_memories, trace_chain, get_graph, communities, etc."""

import logging
from typing import Any

try:
    from .. import __version__
    from ..context_manager import (
        _default_project_path,
        _missing_project_path_error,
        get_project_context,
    )
    from ..logging_config import with_request_id
    from ..mcp_instance import mcp
except ImportError:
    from daem0nmcp import __version__
    from daem0nmcp.context_manager import (
        _default_project_path,
        _missing_project_path_error,
        get_project_context,
    )
    from daem0nmcp.logging_config import with_request_id
    from daem0nmcp.mcp_instance import mcp

logger = logging.getLogger(__name__)


@mcp.tool(version=__version__)
@with_request_id
async def link_memories(
    source_id: int,
    target_id: int,
    relationship: str,
    description: str | None = None,
    project_path: str | None = None,
) -> dict[str, Any]:
    """
    Create relationship between memories. Types: led_to, supersedes, depends_on, conflicts_with, related_to.

    Args:
        source_id: From memory ID
        target_id: To memory ID
        relationship: Relationship type
        description: Optional context
        project_path: Project root
    """
    if not project_path and not _default_project_path:
        return _missing_project_path_error()

    ctx = await get_project_context(project_path)
    return await ctx.memory_manager.link_memories(
        source_id=source_id,
        target_id=target_id,
        relationship=relationship,
        description=description,
    )


@mcp.tool(version=__version__)
@with_request_id
async def unlink_memories(
    source_id: int,
    target_id: int,
    relationship: str | None = None,
    project_path: str | None = None,
) -> dict[str, Any]:
    """
    Remove relationship between memories.

    Args:
        source_id: From memory ID
        target_id: To memory ID
        relationship: Specific type to remove (None = all)
        project_path: Project root
    """
    if not project_path and not _default_project_path:
        return _missing_project_path_error()

    ctx = await get_project_context(project_path)
    return await ctx.memory_manager.unlink_memories(
        source_id=source_id, target_id=target_id, relationship=relationship
    )


@mcp.tool(version=__version__)
@with_request_id
async def trace_chain(
    memory_id: int,
    direction: str = "both",
    relationship_types: list[str] | None = None,
    max_depth: int = 10,
    project_path: str | None = None,
) -> dict[str, Any]:
    """
    Traverse memory graph to understand causal chains and dependencies.

    Args:
        memory_id: Starting point
        direction: forward/backward/both
        relationship_types: Filter by type
        max_depth: How far to traverse
        project_path: Project root
    """
    if not project_path and not _default_project_path:
        return _missing_project_path_error()

    ctx = await get_project_context(project_path)
    return await ctx.memory_manager.trace_chain(
        memory_id=memory_id,
        direction=direction,
        relationship_types=relationship_types,
        max_depth=max_depth,
    )


@mcp.tool(version=__version__)
@with_request_id
async def get_graph(
    memory_ids: list[int] | None = None,
    topic: str | None = None,
    format: str = "json",
    project_path: str | None = None,
) -> dict[str, Any]:
    """
    Get subgraph of memories and relationships as JSON or Mermaid diagram.

    Args:
        memory_ids: Specific IDs to include
        topic: Alternative to memory_ids
        format: json or mermaid
        project_path: Project root
    """
    if not project_path and not _default_project_path:
        return _missing_project_path_error()

    ctx = await get_project_context(project_path)
    return await ctx.memory_manager.get_graph(
        memory_ids=memory_ids, topic=topic, format=format
    )


@mcp.tool(version=__version__)
@with_request_id
async def get_graph_visual(
    memory_ids: list[int] | None = None,
    topic: str | None = None,
    include_orphans: bool = False,
    project_path: str | None = None,
) -> dict[str, Any]:
    """
    Get visual memory graph with UI resource hint for MCP Apps rendering.

    Returns interactive force-directed graph visualization showing memory
    relationships with node coloring by category and edge styling by
    relationship type.

    Args:
        memory_ids: Specific memory IDs to include (if None, uses topic search)
        topic: Topic to search for memories (alternative to memory_ids)
        include_orphans: Include memories with no relationships
        project_path: Project context path

    Returns:
        Graph data with ui_resource hint for visual rendering and text fallback.
    """
    import json
    import urllib.parse

    from daem0nmcp.ui.fallback import format_graph_text, format_with_ui_hint

    if not project_path and not _default_project_path:
        return _missing_project_path_error()

    ctx = await get_project_context(project_path)

    # Get graph data using existing function
    result = await ctx.memory_manager.get_graph(
        memory_ids=memory_ids, topic=topic, format="json"
    )

    # Check for errors
    if "error" in result:
        return result

    # Add topic to result for UI title
    if topic:
        result["topic"] = topic

    # Generate text fallback
    text = format_graph_text(result)

    # Build UI resource URI with encoded data
    data_json = json.dumps(result)
    encoded_data = urllib.parse.quote(data_json)
    ui_resource = f"ui://daem0n/graph/{encoded_data}"

    return format_with_ui_hint(result, ui_resource, text)


@mcp.tool(version=__version__)
@with_request_id
async def get_graph_stats(project_path: str | None = None) -> dict[str, Any]:
    """
    Get metrics about the knowledge graph structure: node/edge counts, density, components.

    Args:
        project_path: Project root
    """
    if not project_path and not _default_project_path:
        return _missing_project_path_error()

    ctx = await get_project_context(project_path)
    knowledge_graph = await ctx.memory_manager.get_knowledge_graph()

    return knowledge_graph.get_metrics()


# ============================================================================
# COMMUNITY MANAGEMENT TOOLS
# ============================================================================
@mcp.tool(version=__version__)
@with_request_id
async def rebuild_communities(
    min_community_size: int = 2,
    resolution: float = 1.0,
    project_path: str | None = None,
) -> dict[str, Any]:
    """
    Detect memory communities using Leiden algorithm on the knowledge graph.

    Args:
        min_community_size: Min members per community
        resolution: Leiden resolution (>1 = smaller communities)
        project_path: Project root
    """
    if project_path is None and not _default_project_path:
        return _missing_project_path_error()

    from ..communities import CommunityManager

    ctx = await get_project_context(project_path)
    cm = CommunityManager(ctx.db_manager)

    # Get knowledge graph for Leiden algorithm
    knowledge_graph = await ctx.memory_manager.get_knowledge_graph()

    # Detect communities using Leiden algorithm
    communities = await cm.detect_communities_from_graph(
        project_path=project_path or _default_project_path,
        knowledge_graph=knowledge_graph,
        resolution=resolution,
        min_community_size=min_community_size,
    )

    # Save to database
    result = await cm.save_communities(
        project_path or _default_project_path, communities
    )

    return {
        **result,
        "status": "rebuilt",
        "communities_found": len(communities),
    }


@mcp.tool(version=__version__)
@with_request_id
async def list_communities(
    level: int | None = None, project_path: str | None = None
) -> dict[str, Any]:
    """
    List all memory communities with summaries.

    Args:
        level: Filter by hierarchy level
        project_path: Project root
    """
    if project_path is None and not _default_project_path:
        return _missing_project_path_error()

    from ..communities import CommunityManager

    ctx = await get_project_context(project_path)
    cm = CommunityManager(ctx.db_manager)

    communities = await cm.get_communities(project_path or _default_project_path, level)

    return {"count": len(communities), "communities": communities}


@mcp.tool(version=__version__)
@with_request_id
async def list_communities_visual(
    level: int | None = None,
    parent_community_id: int | None = None,
    project_path: str | None = None,
) -> dict[str, Any]:
    """
    List communities with visual UI support.

    Same as list_communities() but returns results with UI resource hint for
    MCP Apps hosts. Non-MCP-Apps hosts receive text fallback.

    Args:
        level: Filter by hierarchy level
        parent_community_id: Filter to children of this community (for drill-down)
        project_path: Project root

    Returns:
        Dict with community data + ui_resource hint + text fallback
    """
    from daem0nmcp.ui.fallback import format_communities_text, format_with_ui_hint

    # Get communities using existing function
    result = await list_communities(level=level, project_path=project_path)

    # Check for error
    if "error" in result:
        return result

    # If parent_community_id specified, filter to children only
    if parent_community_id is not None:
        communities = result.get("communities", [])
        filtered = [
            c
            for c in communities
            if c.get("parent_community_id") == parent_community_id
        ]

        parent = next(
            (c for c in communities if c.get("id") == parent_community_id), None
        )
        path = []
        if parent:
            path.append(
                {"id": parent.get("id"), "name": parent.get("name", "Community")}
            )

        result = {"count": len(filtered), "communities": filtered, "path": path}

    # Generate text fallback
    text = format_communities_text(result)

    # Create UI resource URI with encoded data
    import json
    import urllib.parse

    data_json = json.dumps(result)
    encoded_data = urllib.parse.quote(data_json)
    ui_resource = f"ui://daem0n/community/{encoded_data}"

    return format_with_ui_hint(data=result, ui_resource=ui_resource, text=text)


@mcp.tool(version=__version__)
@with_request_id
async def get_community_details(
    community_id: int, project_path: str | None = None
) -> dict[str, Any]:
    """
    Get full community details including all member memories.

    Args:
        community_id: Community to expand
        project_path: Project root
    """
    if project_path is None and not _default_project_path:
        return _missing_project_path_error()

    from ..communities import CommunityManager

    ctx = await get_project_context(project_path)
    cm = CommunityManager(ctx.db_manager)

    return await cm.get_community_members(community_id)
