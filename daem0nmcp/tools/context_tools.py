"""Active context and trigger tools."""

import logging
from datetime import datetime, timedelta, timezone
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
async def set_active_context(
    memory_id: int,
    reason: str | None = None,
    priority: int = 0,
    expires_in_hours: int | None = None,
    project_path: str | None = None,
) -> dict[str, Any]:
    """
    Add memory to always-hot working context. Auto-included in briefings.

    Args:
        memory_id: Memory to add
        reason: Why it should stay hot
        priority: Higher = shown first
        expires_in_hours: Auto-remove after N hours
        project_path: Project root
    """
    if not project_path and not _default_project_path:
        return _missing_project_path_error()

    try:
        from ..active_context import ActiveContextManager
    except ImportError:
        from daem0nmcp.active_context import ActiveContextManager

    ctx = await get_project_context(project_path)
    acm = ActiveContextManager(ctx.db_manager)

    expires_at = None
    if expires_in_hours:
        expires_at = datetime.now(timezone.utc) + timedelta(hours=expires_in_hours)

    return await acm.add_to_context(
        project_path=ctx.project_path,
        memory_id=memory_id,
        reason=reason,
        priority=priority,
        expires_at=expires_at,
    )


@mcp.tool(version=__version__)
@with_request_id
async def get_active_context(project_path: str | None = None) -> dict[str, Any]:
    """
    Get all always-hot memories ordered by priority.

    Args:
        project_path: Project root
    """
    if not project_path and not _default_project_path:
        return _missing_project_path_error()

    try:
        from ..active_context import ActiveContextManager
    except ImportError:
        from daem0nmcp.active_context import ActiveContextManager

    ctx = await get_project_context(project_path)
    acm = ActiveContextManager(ctx.db_manager)

    return await acm.get_active_context(ctx.project_path)


@mcp.tool(version=__version__)
@with_request_id
async def remove_from_active_context(
    memory_id: int, project_path: str | None = None
) -> dict[str, Any]:
    """
    Remove memory from active context.

    Args:
        memory_id: Memory to remove
        project_path: Project root
    """
    if not project_path and not _default_project_path:
        return _missing_project_path_error()

    try:
        from ..active_context import ActiveContextManager
    except ImportError:
        from daem0nmcp.active_context import ActiveContextManager

    ctx = await get_project_context(project_path)
    acm = ActiveContextManager(ctx.db_manager)

    return await acm.remove_from_context(ctx.project_path, memory_id)


@mcp.tool(version=__version__)
@with_request_id
async def clear_active_context(project_path: str | None = None) -> dict[str, Any]:
    """
    Clear all active context memories. Use when switching focus.

    Args:
        project_path: Project root
    """
    if not project_path and not _default_project_path:
        return _missing_project_path_error()

    try:
        from ..active_context import ActiveContextManager
    except ImportError:
        from daem0nmcp.active_context import ActiveContextManager

    ctx = await get_project_context(project_path)
    acm = ActiveContextManager(ctx.db_manager)

    return await acm.clear_context(ctx.project_path)


# ============================================================================
# CONTEXT TRIGGER TOOLS
# ============================================================================
@mcp.tool(version=__version__)
@with_request_id
async def add_context_trigger(
    trigger_type: str,
    pattern: str,
    recall_topic: str,
    recall_categories: list[str] | None = None,
    priority: int = 0,
    project_path: str | None = None,
) -> dict[str, Any]:
    """
    Create auto-recall trigger. Types: file_pattern (glob), tag_match (regex), entity_match (regex).

    Args:
        trigger_type: file_pattern/tag_match/entity_match
        pattern: Glob or regex pattern
        recall_topic: Topic to recall when triggered
        recall_categories: Optional category filter
        priority: Higher = evaluated first
        project_path: Project root
    """
    if not project_path and not _default_project_path:
        return _missing_project_path_error()

    project_path = project_path or _default_project_path

    try:
        from ..context_triggers import ContextTriggerManager
    except ImportError:
        from daem0nmcp.context_triggers import ContextTriggerManager

    ctx = await get_project_context(project_path)
    tm = ContextTriggerManager(ctx.db_manager)

    return await tm.add_trigger(
        project_path=project_path,
        trigger_type=trigger_type,
        pattern=pattern,
        recall_topic=recall_topic,
        recall_categories=recall_categories,
        priority=priority,
    )


@mcp.tool(version=__version__)
@with_request_id
async def list_context_triggers(
    active_only: bool = True, project_path: str | None = None
) -> dict[str, Any]:
    """
    List all configured context triggers.

    Args:
        active_only: Only return active triggers
        project_path: Project root
    """
    if not project_path and not _default_project_path:
        return _missing_project_path_error()

    project_path = project_path or _default_project_path

    try:
        from ..context_triggers import ContextTriggerManager
    except ImportError:
        from daem0nmcp.context_triggers import ContextTriggerManager

    ctx = await get_project_context(project_path)
    tm = ContextTriggerManager(ctx.db_manager)

    triggers = await tm.list_triggers(
        project_path=project_path, active_only=active_only
    )

    return {"triggers": triggers, "count": len(triggers), "active_only": active_only}


@mcp.tool(version=__version__)
@with_request_id
async def remove_context_trigger(
    trigger_id: int, project_path: str | None = None
) -> dict[str, Any]:
    """
    Remove a context trigger.

    Args:
        trigger_id: ID of trigger to remove
        project_path: Project root
    """
    if not project_path and not _default_project_path:
        return _missing_project_path_error()

    project_path = project_path or _default_project_path

    try:
        from ..context_triggers import ContextTriggerManager
    except ImportError:
        from daem0nmcp.context_triggers import ContextTriggerManager

    ctx = await get_project_context(project_path)
    tm = ContextTriggerManager(ctx.db_manager)

    return await tm.remove_trigger(trigger_id=trigger_id, project_path=project_path)


@mcp.tool(version=__version__)
@with_request_id
async def check_context_triggers(
    file_path: str | None = None,
    tags: list[str] | None = None,
    entities: list[str] | None = None,
    limit: int = 5,
    project_path: str | None = None,
) -> dict[str, Any]:
    """
    Check which triggers match context and get auto-recalled memories.

    Args:
        file_path: Match against file_pattern triggers
        tags: Match against tag_match triggers
        entities: Match against entity_match triggers
        limit: Max memories per trigger
        project_path: Project root
    """
    if not project_path and not _default_project_path:
        return _missing_project_path_error()

    project_path = project_path or _default_project_path

    try:
        from ..context_triggers import ContextTriggerManager
    except ImportError:
        from daem0nmcp.context_triggers import ContextTriggerManager

    ctx = await get_project_context(project_path)
    tm = ContextTriggerManager(ctx.db_manager)

    return await tm.get_triggered_context(
        project_path=project_path,
        file_path=file_path,
        tags=tags,
        entities=entities,
        limit=limit,
    )
