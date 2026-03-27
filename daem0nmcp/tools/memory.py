"""Memory tools: remember, recall, remember_batch, recall_visual, etc."""

import logging
from datetime import datetime, timezone
from typing import Any

try:
    from .. import __version__
    from ..context_manager import (
        _check_covenant_communion,
        _check_covenant_counsel,
        _default_project_path,
        _missing_project_path_error,
        get_project_context,
    )
    from ..logging_config import with_request_id
    from ..mcp_instance import mcp
    from ..models import Memory
except ImportError:
    from daem0nmcp import __version__
    from daem0nmcp.context_manager import (
        _check_covenant_communion,
        _check_covenant_counsel,
        _default_project_path,
        _missing_project_path_error,
        get_project_context,
    )
    from daem0nmcp.logging_config import with_request_id
    from daem0nmcp.mcp_instance import mcp
    from daem0nmcp.models import Memory

from sqlalchemy import select

from ._deprecation import add_deprecation

logger = logging.getLogger(__name__)


# ============================================================================
# Tool 1: REMEMBER - Store a memory with conflict detection
# ============================================================================
@mcp.tool(version=__version__)
@with_request_id
async def remember(
    category: str,
    content: str,
    rationale: str | None = None,
    context: dict[str, Any] | None = None,
    tags: list[str] | None = None,
    file_path: str | None = None,
    project_path: str | None = None,
    happened_at: str | None = None,
) -> dict[str, Any]:
    """
    [DEPRECATED] Use inscribe(action='remember') instead.

    Store a memory (decision/pattern/warning/learning).
    Auto-detects conflicts with past failures. Patterns and warnings are permanent.

    Args:
        category: One of 'decision', 'pattern', 'warning', 'learning'
        content: What to remember
        rationale: Why this matters
        context: Structured context dict
        tags: List of tags for retrieval
        file_path: Associate with a file
        project_path: Project root
        happened_at: When this fact was true in reality (ISO 8601 string).
                    Use for backfilling: "User told me last week they prefer Python"
    """
    # Require project_path for multi-project support
    if not project_path and not _default_project_path:
        return _missing_project_path_error()

    effective_path = project_path or _default_project_path

    # Covenant enforcement: remember requires counsel (which implies communion)
    violation = _check_covenant_counsel("remember", effective_path)
    if violation:
        return violation

    # Parse happened_at datetime if provided
    happened_at_dt = None
    if happened_at:
        try:
            happened_at_dt = datetime.fromisoformat(happened_at.replace("Z", "+00:00"))
        except ValueError:
            return {
                "error": f"Invalid 'happened_at' date format: {happened_at}. Use ISO format (e.g., '2025-01-01T00:00:00Z')"
            }

    ctx = await get_project_context(project_path)
    result = await ctx.memory_manager.remember(
        category=category,
        content=content,
        rationale=rationale,
        context=context,
        tags=tags,
        file_path=file_path,
        project_path=ctx.project_path,
        happened_at=happened_at_dt,
    )

    return add_deprecation(result, "remember", "inscribe(action='remember')")


# ============================================================================
# Tool 1b: REMEMBER_BATCH - Store multiple memories efficiently
# ============================================================================
@mcp.tool(version=__version__)
@with_request_id
async def remember_batch(
    memories: list[dict[str, Any]], project_path: str | None = None
) -> dict[str, Any]:
    """
    [DEPRECATED] Use inscribe(action='remember_batch') instead.

    Store multiple memories atomically. Efficient for bulk imports.

    Args:
        memories: List of dicts with category, content, rationale (opt), tags (opt), file_path (opt)
        project_path: Project root
    """
    # Require project_path for multi-project support
    if not project_path and not _default_project_path:
        return _missing_project_path_error()

    if not memories:
        return {
            "created_count": 0,
            "error_count": 0,
            "ids": [],
            "errors": [],
            "message": "No memories provided",
        }

    ctx = await get_project_context(project_path)
    result = await ctx.memory_manager.remember_batch(
        memories=memories, project_path=ctx.project_path
    )

    result["message"] = f"Stored {result['created_count']} memories" + (
        f" with {result['error_count']} error(s)" if result["error_count"] else ""
    )

    return add_deprecation(
        result, "remember_batch", "inscribe(action='remember_batch')"
    )


# ============================================================================
# Tool 2: RECALL - Semantic memory retrieval with decay
# ============================================================================
@mcp.tool(version=__version__)
@with_request_id
async def recall(
    topic: str,
    categories: list[str] | None = None,
    tags: list[str] | None = None,
    file_path: str | None = None,
    offset: int = 0,
    limit: int = 10,
    since: str | None = None,
    until: str | None = None,
    project_path: str | None = None,
    include_linked: bool = False,
    condensed: bool = False,
    as_of_time: str | None = None,
) -> dict[str, Any]:
    """
    [DEPRECATED] Use consult(action='recall') instead.

    Semantic search for memories using TF-IDF. Results weighted by relevance, recency, importance.

    Args:
        topic: What to search for
        categories: Filter by category
        tags: Filter by tags
        file_path: Filter by file
        offset/limit: Pagination
        since/until: Date range (ISO format)
        project_path: Project root
        include_linked: Search linked projects
        condensed: Compress output (~75% token reduction)
        as_of_time: Return knowledge state as of this time (ISO 8601 string).
                   Filters to memories valid at that time. Use for: "What did we know on 2025-12-01?"
    """
    # Require project_path for multi-project support
    if not project_path and not _default_project_path:
        return _missing_project_path_error()

    effective_path = project_path or _default_project_path

    # Covenant enforcement: recall requires communion (briefing)
    violation = _check_covenant_communion(effective_path)
    if violation:
        return violation

    # Parse date strings if provided
    since_dt = None
    until_dt = None
    as_of_time_dt = None

    if since:
        try:
            since_dt = datetime.fromisoformat(since.replace("Z", "+00:00"))
        except ValueError:
            return {
                "error": f"Invalid 'since' date format: {since}. Use ISO format (e.g., '2025-01-01T00:00:00Z')"
            }

    if until:
        try:
            until_dt = datetime.fromisoformat(until.replace("Z", "+00:00"))
        except ValueError:
            return {
                "error": f"Invalid 'until' date format: {until}. Use ISO format (e.g., '2025-12-31T23:59:59Z')"
            }

    if as_of_time:
        try:
            as_of_time_dt = datetime.fromisoformat(as_of_time.replace("Z", "+00:00"))
        except ValueError:
            return {
                "error": f"Invalid 'as_of_time' date format: {as_of_time}. Use ISO format (e.g., '2025-12-01T00:00:00Z')"
            }

    ctx = await get_project_context(project_path)
    result = await ctx.memory_manager.recall(
        topic=topic,
        categories=categories,
        tags=tags,
        file_path=file_path,
        offset=offset,
        limit=limit,
        since=since_dt,
        until=until_dt,
        project_path=ctx.project_path,
        include_linked=include_linked,
        condensed=condensed,
        as_of_time=as_of_time_dt,
    )
    return add_deprecation(result, "recall", "consult(action='recall')")


# ============================================================================
# Tool 2.5: RECALL_VISUAL - Semantic recall with UI resource hint
# ============================================================================
@mcp.tool(version=__version__)
@with_request_id
async def recall_visual(
    topic: str,
    categories: list[str] | None = None,
    tags: list[str] | None = None,
    file_path: str | None = None,
    offset: int = 0,
    limit: int = 10,
    since: str | None = None,
    until: str | None = None,
    include_linked: bool = False,
    condensed: bool = False,
    as_of_time: str | None = None,
    project_path: str | None = None,
) -> dict[str, Any]:
    """
    [DEPRECATED] Use consult(action='recall', visual=True) instead.

    Search memories with visual UI support.

    Same as recall() but returns results with UI resource hint for
    MCP Apps hosts. Non-MCP-Apps hosts receive text fallback.

    Args:
        topic: What to search for
        categories: Filter by category (decision, warning, pattern, learning)
        tags: Filter by tags
        file_path: Filter by associated file
        offset: Pagination offset
        limit: Results per page
        since: Only memories created after this ISO datetime
        until: Only memories created before this ISO datetime
        include_linked: Include results from linked projects
        condensed: Return condensed output
        as_of_time: View memories as they existed at this ISO datetime
        project_path: Project root

    Returns:
        Dict with recall results + ui_resource hint + text fallback
    """
    from daem0nmcp.ui.fallback import format_search_results, format_with_ui_hint

    # Require project_path for multi-project support
    if not project_path and not _default_project_path:
        return _missing_project_path_error()

    effective_path = project_path or _default_project_path

    # Covenant enforcement: recall requires communion (briefing)
    violation = _check_covenant_communion(effective_path)
    if violation:
        return violation

    ctx = await get_project_context(project_path)

    # Get recall results using existing memory manager
    result = await ctx.memory_manager.recall(
        topic=topic,
        categories=categories,
        tags=tags,
        file_path=file_path,
        offset=offset,
        limit=limit,
        since=since,
        until=until,
        project_path=ctx.project_path,
        include_linked=include_linked,
        condensed=condensed,
        as_of_time=as_of_time,
    )

    # Add topic to result for UI rendering
    result["topic"] = topic

    # Flatten results for text formatting
    all_results = []
    for cat in ["decisions", "patterns", "warnings", "learnings"]:
        for r in result.get(cat, []):
            all_results.append(
                {
                    "id": r.get("id"),
                    "category": cat.rstrip("s"),  # decisions -> decision
                    "content": r.get("content", ""),
                    "score": r.get("relevance", 0),
                }
            )

    # Generate text fallback
    text = format_search_results(
        query=topic,
        results=all_results,
        total_count=result.get("total_count", len(all_results)),
    )

    # Return with UI hint
    ui_result = format_with_ui_hint(
        data=result, ui_resource="ui://daem0n/search", text=text
    )
    return add_deprecation(
        ui_result, "recall_visual", "consult(action='recall', visual=True)"
    )


# ============================================================================
# Tool 5: RECORD_OUTCOME - Track if a decision worked
# ============================================================================
@mcp.tool(version=__version__)
@with_request_id
async def record_outcome(
    memory_id: int, outcome: str, worked: bool, project_path: str | None = None
) -> dict[str, Any]:
    """
    [DEPRECATED] Use reflect(action='outcome') instead.

    Record whether a decision worked. Failed outcomes get boosted in future searches.

    Args:
        memory_id: ID from remember()
        outcome: What happened
        worked: True/False
        project_path: Project root
    """
    # Require project_path for multi-project support
    if not project_path and not _default_project_path:
        return _missing_project_path_error()

    ctx = await get_project_context(project_path)
    effective_project_path = project_path or _default_project_path
    result = await ctx.memory_manager.record_outcome(
        memory_id=memory_id,
        outcome=outcome,
        worked=worked,
        project_path=effective_project_path,
    )

    return add_deprecation(result, "record_outcome", "reflect(action='outcome')")


# ============================================================================
# Tool 12: RECALL_FOR_FILE - Get memories for a specific file
# ============================================================================
@mcp.tool(version=__version__)
@with_request_id
async def recall_for_file(
    file_path: str, limit: int = 10, project_path: str | None = None
) -> dict[str, Any]:
    """
    Get all memories associated with a specific file.

    Args:
        file_path: File to look up
        limit: Max results
        project_path: Project root
    """
    # Require project_path for multi-project support
    if not project_path and not _default_project_path:
        return _missing_project_path_error()

    ctx = await get_project_context(project_path)
    return await ctx.memory_manager.recall_for_file(
        file_path=file_path, limit=limit, project_path=ctx.project_path
    )


# ============================================================================
# Tool: RECALL_BY_ENTITY - Get memories mentioning a specific entity
# ============================================================================
@mcp.tool(version=__version__)
@with_request_id
async def recall_by_entity(
    entity_name: str,
    entity_type: str | None = None,
    project_path: str | None = None,
) -> dict[str, Any]:
    """
    Get all memories mentioning a specific entity (class/function/file).

    Args:
        entity_name: Entity to search for
        entity_type: Optional type filter
        project_path: Project root
    """
    if not project_path and not _default_project_path:
        return _missing_project_path_error()

    ctx = await get_project_context(project_path)

    # Import EntityManager locally
    try:
        from ..entity_manager import EntityManager
    except ImportError:
        from daem0nmcp.entity_manager import EntityManager

    entity_manager = EntityManager(ctx.db_manager)
    return await entity_manager.get_memories_for_entity(
        entity_name=entity_name, project_path=ctx.project_path, entity_type=entity_type
    )


# ============================================================================
# Tool: RECALL_HIERARCHICAL - GraphRAG-style layered recall
# ============================================================================
@mcp.tool(version=__version__)
@with_request_id
async def recall_hierarchical(
    topic: str,
    include_members: bool = False,
    limit: int = 10,
    project_path: str | None = None,
) -> dict[str, Any]:
    """
    GraphRAG-style layered recall: community summaries first, then individual memories.

    Args:
        topic: What to search for
        include_members: Include full member content
        limit: Max results per layer
        project_path: Project root
    """
    if project_path is None and not _default_project_path:
        return _missing_project_path_error()

    ctx = await get_project_context(project_path)

    return await ctx.memory_manager.recall_hierarchical(
        topic=topic,
        project_path=project_path or _default_project_path,
        include_members=include_members,
        limit=limit,
    )


# ============================================================================
# Tool 7: SEARCH - Full text search across memories
# ============================================================================
@mcp.tool(version=__version__)
@with_request_id
async def search_memories(
    query: str,
    limit: int = 20,
    offset: int = 0,
    include_meta: bool = False,
    highlight: bool = False,
    highlight_start: str = "<b>",
    highlight_end: str = "</b>",
    project_path: str | None = None,
) -> list[dict[str, Any]] | dict[str, Any]:
    """
    [DEPRECATED] Use consult(action='search') instead.

    Full-text search across all memories with TF-IDF ranking.

    Args:
        query: Search text
        limit/offset: Pagination
        include_meta: Return pagination metadata
        highlight: Include matched term excerpts
        highlight_start/end: Tags for highlighting
        project_path: Project root
    """
    # Require project_path for multi-project support
    if not project_path and not _default_project_path:
        return _missing_project_path_error()

    if offset < 0:
        return {"error": "offset must be non-negative"}

    ctx = await get_project_context(project_path)
    raw_limit = offset + limit + 1

    if highlight:
        # Use FTS search with highlighting
        results = await ctx.memory_manager.fts_search(
            query=query,
            limit=raw_limit,
            highlight=True,
            highlight_start=highlight_start,
            highlight_end=highlight_end,
        )
    else:
        results = await ctx.memory_manager.search(query=query, limit=raw_limit)

    has_more = len(results) > offset + limit
    paginated = results[offset : offset + limit]

    if include_meta:
        result = {
            "query": query,
            "offset": offset,
            "limit": limit,
            "has_more": has_more,
            "highlight": highlight,
            "results": paginated,
        }
        return add_deprecation(result, "search_memories", "consult(action='search')")

    # List return path: wrap in dict to include deprecation field
    return add_deprecation(
        {"results": paginated, "query": query},
        "search_memories",
        "consult(action='search')",
    )


# ============================================================================
# Tool 10: FIND_RELATED - Discover connected memories
# ============================================================================
@mcp.tool(version=__version__)
@with_request_id
async def find_related(
    memory_id: int, limit: int = 5, project_path: str | None = None
) -> list[dict[str, Any]]:
    """
    Find memories semantically related to a specific memory.

    Args:
        memory_id: Memory to find relations for
        limit: Max results
        project_path: Project root
    """
    # Require project_path for multi-project support
    if not project_path and not _default_project_path:
        return _missing_project_path_error()

    ctx = await get_project_context(project_path)
    return await ctx.memory_manager.find_related(memory_id=memory_id, limit=limit)


# ============================================================================
# Tool: GET_RELATED_MEMORIES - Graph-based related memories
# ============================================================================
@mcp.tool(version=__version__)
@with_request_id
async def get_related_memories(
    memory_id: int,
    relationship_types: list[str] | None = None,
    direction: str = "both",
    max_depth: int = 2,
    project_path: str | None = None,
) -> dict[str, Any]:
    """
    Find memories related to a given memory via graph traversal. Answers: "What depends on this decision?"

    Args:
        memory_id: Starting memory ID
        relationship_types: Filter by types (led_to, supersedes, depends_on, conflicts_with, related_to)
        direction: "outgoing", "incoming", or "both"
        max_depth: Maximum traversal depth (default: 2)
        project_path: Project root
    """
    if not project_path and not _default_project_path:
        return _missing_project_path_error()

    ctx = await get_project_context(project_path)
    knowledge_graph = await ctx.memory_manager.get_knowledge_graph()

    return await knowledge_graph.get_related(
        memory_id=memory_id,
        relationship_types=relationship_types,
        direction=direction,
        max_depth=max_depth,
    )


# ============================================================================
# TEMPORAL VERSIONING - Memory History Tools
# ============================================================================
@mcp.tool(version=__version__)
@with_request_id
async def get_memory_versions(
    memory_id: int, limit: int = 50, project_path: str | None = None
) -> dict[str, Any]:
    """
    Get version history showing how a memory evolved over time.

    Args:
        memory_id: Memory to query
        limit: Max versions
        project_path: Project root
    """
    if project_path is None and not _default_project_path:
        return _missing_project_path_error()

    ctx = await get_project_context(project_path)
    versions = await ctx.memory_manager.get_memory_versions(memory_id, limit)

    return {
        "memory_id": memory_id,
        "version_count": len(versions),
        "versions": versions,
    }


@mcp.tool(version=__version__)
@with_request_id
async def get_memory_at_time(
    memory_id: int, timestamp: str, project_path: str | None = None
) -> dict[str, Any]:
    """
    Get memory state at a specific point in time.

    Args:
        memory_id: Memory to query
        timestamp: ISO format timestamp
        project_path: Project root
    """
    if project_path is None and not _default_project_path:
        return _missing_project_path_error()

    try:
        point_in_time = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    except ValueError as e:
        return {"error": f"Invalid timestamp format: {e}"}

    ctx = await get_project_context(project_path)
    historical = await ctx.memory_manager.get_memory_at_time(memory_id, point_in_time)

    if historical is None:
        return {
            "error": "NOT_FOUND",
            "message": f"Memory {memory_id} did not exist at {timestamp}",
        }

    return historical


# ============================================================================
# Tool: COMPACT_MEMORIES - Consolidate episodic memories into summaries
# ============================================================================
@mcp.tool(version=__version__)
@with_request_id
async def compact_memories(
    summary: str,
    limit: int = 10,
    topic: str | None = None,
    dry_run: bool = True,
    project_path: str | None = None,
) -> dict[str, Any]:
    """
    Consolidate recent episodic memories into a summary. Originals archived with graph links.

    Args:
        summary: Summary text (min 50 chars)
        limit: Max memories to compact
        topic: Filter by topic
        dry_run: Preview only
        project_path: Project root
    """
    if not project_path and not _default_project_path:
        return _missing_project_path_error()

    ctx = await get_project_context(project_path)

    return await ctx.memory_manager.compact_memories(
        summary=summary, limit=limit, topic=topic, dry_run=dry_run
    )


# ============================================================================
# Tool: CLEANUP_MEMORIES - Merge duplicate memories
# ============================================================================
@mcp.tool(version=__version__)
@with_request_id
async def cleanup_memories(
    dry_run: bool = True,
    merge_duplicates: bool = True,
    project_path: str | None = None,
) -> dict[str, Any]:
    """
    Merge duplicate memories (same category + content + file_path). Keeps newest.

    Args:
        dry_run: Preview only
        merge_duplicates: Actually merge
        project_path: Project root
    """
    if not project_path and not _default_project_path:
        return _missing_project_path_error()

    ctx = await get_project_context(project_path)

    async with ctx.db_manager.get_session() as session:
        result = await session.execute(select(Memory))
        all_memories = result.scalars().all()

        # Group by (category, normalized_content, file_path)
        groups = {}
        for mem in all_memories:
            # Normalize content for comparison (lowercase, collapse whitespace)
            normalized = " ".join(mem.content.lower().split())
            key = (mem.category, normalized, mem.file_path or "")

            if key not in groups:
                groups[key] = []
            groups[key].append(mem)

        # Find duplicates (groups with more than 1 memory)
        duplicates = {k: v for k, v in groups.items() if len(v) > 1}

        if dry_run:
            return {
                "dry_run": True,
                "duplicate_groups": len(duplicates),
                "total_duplicates": sum(len(v) - 1 for v in duplicates.values()),
                "samples": [
                    {
                        "content": mems[0].content[:50],
                        "count": len(mems),
                        "ids": [m.id for m in mems],
                    }
                    for mems in list(duplicates.values())[:5]
                ],
            }

        # Merge duplicates: keep newest, preserve outcomes
        merged = 0
        if merge_duplicates:
            for key, mems in duplicates.items():

                def _to_naive(dt_value: datetime | None) -> datetime:
                    if not dt_value:
                        return datetime.min
                    return (
                        dt_value.replace(tzinfo=None) if dt_value.tzinfo else dt_value
                    )

                def _outcome_timestamp(mem: Memory) -> datetime:
                    return _to_naive(mem.updated_at or mem.created_at)

                # Sort by created_at descending (newest first)
                mems.sort(key=lambda m: _to_naive(m.created_at), reverse=True)
                keeper = mems[0]

                # Pick the most recent outcome across duplicates (if any)
                outcome_source = None
                for candidate in mems:
                    if candidate.outcome:
                        if outcome_source is None or _outcome_timestamp(
                            candidate
                        ) > _outcome_timestamp(outcome_source):
                            outcome_source = candidate

                if outcome_source:
                    keeper.outcome = outcome_source.outcome
                    keeper.worked = outcome_source.worked

                # Merge outcomes, tags, and metadata from others
                for dupe in mems[1:]:
                    # Preserve pinned status (if any duplicate is pinned, keep pinned)
                    if dupe.pinned and not keeper.pinned:
                        keeper.pinned = True

                    # If keeper is archived but duplicate isn't, unarchive
                    if not dupe.archived and keeper.archived:
                        keeper.archived = False

                    # Merge tags (union of all tags)
                    if dupe.tags:
                        keeper_tags = set(keeper.tags or [])
                        keeper_tags.update(dupe.tags or [])
                        keeper.tags = list(keeper_tags)

                # Update keeper's updated_at timestamp
                keeper.updated_at = datetime.now(timezone.utc)

                # Flush changes to keeper before deleting duplicates
                await session.flush()

                # Delete duplicates
                for dupe in mems[1:]:
                    await session.delete(dupe)
                    merged += 1

    # Rebuild index to reflect merged/deleted documents
    await ctx.memory_manager.rebuild_index()

    return {
        "merged": merged,
        "duplicate_groups": len(duplicates),
        "message": f"Merged {merged} duplicate memories",
    }


# ============================================================================
# Tool: ARCHIVE_MEMORY - Archive/unarchive a memory
# ============================================================================
@mcp.tool(version=__version__)
@with_request_id
async def archive_memory(
    memory_id: int, archived: bool = True, project_path: str | None = None
) -> dict[str, Any]:
    """
    Archive/unarchive a memory. Archived = hidden from recall but preserved.

    Args:
        memory_id: Memory to archive
        archived: True to archive, False to restore
        project_path: Project root
    """
    if not project_path and not _default_project_path:
        return _missing_project_path_error()

    ctx = await get_project_context(project_path)

    async with ctx.db_manager.get_session() as session:
        result = await session.execute(select(Memory).where(Memory.id == memory_id))
        memory = result.scalar_one_or_none()

        if not memory:
            return {"error": f"Memory {memory_id} not found"}

        memory.archived = archived

        return {
            "id": memory_id,
            "archived": archived,
            "content": memory.content[:100],
            "message": f"Memory {'archived' if archived else 'restored'}",
        }


# ============================================================================
# Tool: PIN_MEMORY - Pin/unpin a memory
# ============================================================================
@mcp.tool(version=__version__)
@with_request_id
async def pin_memory(
    memory_id: int, pinned: bool = True, project_path: str | None = None
) -> dict[str, Any]:
    """
    Pin/unpin a memory. Pinned: never pruned, boosted in recall, permanent.

    Args:
        memory_id: Memory to pin
        pinned: True to pin, False to unpin
        project_path: Project root
    """
    if not project_path and not _default_project_path:
        return _missing_project_path_error()

    ctx = await get_project_context(project_path)

    async with ctx.db_manager.get_session() as session:
        result = await session.execute(select(Memory).where(Memory.id == memory_id))
        memory = result.scalar_one_or_none()

        if not memory:
            return {"error": f"Memory {memory_id} not found"}

        memory.pinned = pinned
        memory.is_permanent = pinned  # Pinned = permanent

        return {
            "id": memory_id,
            "pinned": pinned,
            "content": memory.content[:100],
            "message": f"Memory {'pinned' if pinned else 'unpinned'}",
        }
