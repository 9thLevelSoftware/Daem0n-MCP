"""
Consult workflow - Pre-action intelligence.

Actions:
- preflight: Pre-flight check combining recall + check_rules
- recall: Semantic search for memories using TF-IDF
- recall_file: Get all memories associated with a specific file
- recall_entity: Get all memories mentioning a specific entity
- recall_hierarchical: GraphRAG-style layered recall
- search: Full-text search across all memories
- check_rules: Check if an action matches any rules
- compress: Compress context for token reduction
"""

from typing import Any

from .errors import InvalidActionError, MissingParamError

VALID_ACTIONS = frozenset(
    {
        "preflight",
        "recall",
        "recall_file",
        "recall_entity",
        "recall_hierarchical",
        "search",
        "check_rules",
        "compress",
    }
)


async def dispatch(
    action: str,
    project_path: str,
    *,
    # preflight params
    description: str | None = None,
    # recall params
    topic: str | None = None,
    categories: list[str] | None = None,
    tags: list[str] | None = None,
    file_path: str | None = None,
    offset: int = 0,
    limit: int = 10,
    since: str | None = None,
    until: str | None = None,
    include_linked: bool = False,
    visual: bool = False,
    condensed: bool = False,
    # recall_entity params
    entity_name: str | None = None,
    entity_type: str | None = None,
    # recall_hierarchical params
    include_members: bool = False,
    # search params
    query: str | None = None,
    include_meta: bool = False,
    highlight: bool = False,
    highlight_start: str = "<b>",
    highlight_end: str = "</b>",
    # check_rules params
    action_desc: str | None = None,
    context: dict[str, Any] | None = None,
    # compress params
    compress_text: str | None = None,
    rate: float | None = None,
    content_type: str | None = None,
    preserve_code: bool = True,
    **kwargs,
) -> Any:
    """Dispatch action to appropriate handler."""
    if action not in VALID_ACTIONS:
        raise InvalidActionError(action, sorted(VALID_ACTIONS))

    if action == "preflight":
        if not description:
            raise MissingParamError("description", action)
        return await _do_preflight(project_path, description)

    elif action == "recall":
        if not topic:
            raise MissingParamError("topic", action)
        return await _do_recall(
            project_path,
            topic,
            categories,
            tags,
            file_path,
            offset,
            limit,
            since,
            until,
            include_linked,
            visual,
            condensed,
        )

    elif action == "recall_file":
        if not file_path:
            raise MissingParamError("file_path", action)
        return await _do_recall_file(project_path, file_path, limit)

    elif action == "recall_entity":
        if not entity_name:
            raise MissingParamError("entity_name", action)
        return await _do_recall_entity(project_path, entity_name, entity_type)

    elif action == "recall_hierarchical":
        if not topic:
            raise MissingParamError("topic", action)
        return await _do_recall_hierarchical(
            project_path, topic, include_members, limit
        )

    elif action == "search":
        if not query:
            raise MissingParamError("query", action)
        return await _do_search(
            project_path,
            query,
            limit,
            offset,
            include_meta,
            highlight,
            highlight_start,
            highlight_end,
        )

    elif action == "check_rules":
        if not action_desc:
            raise MissingParamError("action_desc", action)
        return await _do_check_rules(project_path, action_desc, context)

    elif action == "compress":
        if compress_text is None:
            raise MissingParamError("compress_text", action)
        return await _do_compress(compress_text, rate, content_type, preserve_code)

    raise InvalidActionError(action, sorted(VALID_ACTIONS))


async def _do_preflight(project_path: str, description: str) -> dict[str, Any]:
    """Pre-flight check combining recall + check_rules."""
    from ..server import context_check

    return await context_check(description=description, project_path=project_path)


async def _do_recall(
    project_path: str,
    topic: str,
    categories: list[str] | None,
    tags: list[str] | None,
    file_path: str | None,
    offset: int,
    limit: int,
    since: str | None,
    until: str | None,
    include_linked: bool,
    visual: bool,
    condensed: bool,
) -> Any:
    """Semantic search for memories."""
    from ..server import recall, recall_visual

    if visual:
        return await recall_visual(
            topic=topic,
            categories=categories,
            tags=tags,
            file_path=file_path,
            offset=offset,
            limit=limit,
            include_linked=include_linked,
            project_path=project_path,
        )
    return await recall(
        topic=topic,
        categories=categories,
        tags=tags,
        file_path=file_path,
        offset=offset,
        limit=limit,
        since=since,
        until=until,
        project_path=project_path,
        include_linked=include_linked,
        condensed=condensed,
    )


async def _do_recall_file(
    project_path: str, file_path: str, limit: int
) -> dict[str, Any]:
    """Get all memories for a specific file."""
    from ..server import recall_for_file

    return await recall_for_file(
        file_path=file_path, limit=limit, project_path=project_path
    )


async def _do_recall_entity(
    project_path: str,
    entity_name: str,
    entity_type: str | None,
) -> dict[str, Any]:
    """Get all memories mentioning a specific entity."""
    from ..server import recall_by_entity

    return await recall_by_entity(
        entity_name=entity_name,
        entity_type=entity_type,
        project_path=project_path,
    )


async def _do_recall_hierarchical(
    project_path: str,
    topic: str,
    include_members: bool,
    limit: int,
) -> dict[str, Any]:
    """GraphRAG-style layered recall."""
    from ..server import recall_hierarchical

    return await recall_hierarchical(
        topic=topic,
        include_members=include_members,
        limit=limit,
        project_path=project_path,
    )


async def _do_search(
    project_path: str,
    query: str,
    limit: int,
    offset: int,
    include_meta: bool,
    highlight: bool,
    highlight_start: str,
    highlight_end: str,
) -> Any:
    """Full-text search across all memories."""
    from ..server import search_memories

    return await search_memories(
        query=query,
        limit=limit,
        offset=offset,
        include_meta=include_meta,
        highlight=highlight,
        highlight_start=highlight_start,
        highlight_end=highlight_end,
        project_path=project_path,
    )


async def _do_check_rules(
    project_path: str,
    action_desc: str,
    context: dict[str, Any] | None,
) -> dict[str, Any]:
    """Check if an action matches any rules."""
    from ..server import check_rules

    return await check_rules(
        action=action_desc, context=context, project_path=project_path
    )


async def _do_compress(
    context: str,
    rate: float | None,
    content_type: str | None,
    preserve_code: bool,
) -> str:
    """Compress context for token reduction."""
    from ..server import compress_context

    return await compress_context(
        context=context,
        rate=rate,
        content_type=content_type,
        preserve_code=preserve_code,
    )
