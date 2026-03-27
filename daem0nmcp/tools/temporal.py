"""Temporal tools: trace_causal_path, trace_evolution."""

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
async def trace_causal_path(
    start_memory_id: int,
    end_memory_id: int,
    max_depth: int = 5,
    project_path: str | None = None,
) -> dict[str, Any]:
    """
    Find causal paths between two memories. Answers: "How did decision X lead to outcome Y?"

    Args:
        start_memory_id: Starting memory ID
        end_memory_id: Target memory ID
        max_depth: Maximum path length (default: 5)
        project_path: Project root
    """
    if not project_path and not _default_project_path:
        return _missing_project_path_error()

    ctx = await get_project_context(project_path)
    knowledge_graph = await ctx.memory_manager.get_knowledge_graph()

    return await knowledge_graph.trace_chain(
        start_memory_id=start_memory_id,
        end_memory_id=end_memory_id,
        max_depth=max_depth,
    )


@mcp.tool(version=__version__)
@with_request_id
async def trace_evolution(
    entity_name: str | None = None,
    entity_type: str | None = None,
    include_invalidated: bool = True,
    entity_id: int | None = None,
    project_path: str | None = None,
) -> dict[str, Any]:
    """
    Trace how knowledge about an entity evolved over time. Answers: "How has our understanding of X changed?"

    Returns a bi-temporal timeline with valid_from, valid_to, transaction_time, and invalidation info.
    Critical for answering: "T1 believed X, at T2 learned X wrong, query at T3 shows invalidation"

    Args:
        entity_name: Name of the entity to trace (e.g., "UserService", "auth")
        entity_type: Filter by entity type (e.g., "class", "concept", "function")
        include_invalidated: Include invalidated versions in timeline (default: True)
        entity_id: Entity database ID (alternative to entity_name)
        project_path: Project root

    Either entity_name or entity_id must be provided.
    """
    if not project_path and not _default_project_path:
        return _missing_project_path_error()

    if not entity_name and not entity_id:
        return {"error": "Either entity_name or entity_id must be provided"}

    ctx = await get_project_context(project_path)

    # If entity_name provided, use MemoryManager.get_memory_evolution (bi-temporal)
    if entity_name:
        return await ctx.memory_manager.get_memory_evolution(
            entity_name=entity_name,
            entity_type=entity_type,
            include_invalidated=include_invalidated,
        )

    # If only entity_id provided, use KnowledgeGraph.trace_evolution (graph-based)
    knowledge_graph = await ctx.memory_manager.get_knowledge_graph()
    return await knowledge_graph.trace_evolution(entity_id=entity_id)
