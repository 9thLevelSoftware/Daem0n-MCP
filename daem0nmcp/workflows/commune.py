"""
Commune workflow - Session start & status.

Actions:
- briefing: Get session briefing with stats, decisions, warnings, git changes
- active_context: Get all always-hot memories ordered by priority
- triggers: Check which context triggers match and get auto-recalled memories
- health: Get server health, version, and statistics
- covenant: Get current Sacred Covenant status
- updates: Check if daemon knowledge has changed since a timestamp
"""

from typing import Any

from .errors import InvalidActionError

VALID_ACTIONS = frozenset(
    {
        "briefing",
        "active_context",
        "triggers",
        "health",
        "covenant",
        "updates",
    }
)


async def dispatch(
    action: str,
    project_path: str,
    *,
    focus_areas: list[str] | None = None,
    visual: bool = False,
    # triggers params
    file_path: str | None = None,
    tags: list[str] | None = None,
    entities: list[str] | None = None,
    limit: int = 5,
    # updates params
    since: str | None = None,
    interval_seconds: int = 10,
    # communities visual param
    parent_community_id: int | None = None,
    **kwargs,
) -> dict[str, Any]:
    """Dispatch action to appropriate handler."""
    if action not in VALID_ACTIONS:
        raise InvalidActionError(action, sorted(VALID_ACTIONS))

    if action == "briefing":
        return await _do_briefing(project_path, focus_areas, visual)
    elif action == "active_context":
        return await _do_active_context(project_path)
    elif action == "triggers":
        return await _do_triggers(project_path, file_path, tags, entities, limit)
    elif action == "health":
        return await _do_health(project_path)
    elif action == "covenant":
        return await _do_covenant(project_path, visual)
    elif action == "updates":
        return await _do_updates(project_path, since, interval_seconds)

    raise InvalidActionError(action, sorted(VALID_ACTIONS))


async def _do_briefing(
    project_path: str,
    focus_areas: list[str] | None,
    visual: bool,
) -> dict[str, Any]:
    """Get session briefing."""
    from ..server import get_briefing, get_briefing_visual

    if visual:
        return await get_briefing_visual(
            project_path=project_path, focus_areas=focus_areas
        )
    return await get_briefing(project_path=project_path, focus_areas=focus_areas)


async def _do_active_context(project_path: str) -> dict[str, Any]:
    """Get all always-hot memories."""
    from ..server import get_active_context

    return await get_active_context(project_path=project_path)


async def _do_triggers(
    project_path: str,
    file_path: str | None,
    tags: list[str] | None,
    entities: list[str] | None,
    limit: int,
) -> dict[str, Any]:
    """Check context triggers and get auto-recalled memories."""
    from ..server import check_context_triggers

    return await check_context_triggers(
        file_path=file_path,
        tags=tags,
        entities=entities,
        limit=limit,
        project_path=project_path,
    )


async def _do_health(project_path: str) -> dict[str, Any]:
    """Get server health and stats."""
    from ..server import health

    return await health(project_path=project_path)


async def _do_covenant(project_path: str, visual: bool) -> dict[str, Any]:
    """Get Sacred Covenant status."""
    from ..server import get_covenant_status, get_covenant_status_visual

    if visual:
        return await get_covenant_status_visual(project_path=project_path)
    return await get_covenant_status(project_path=project_path)


async def _do_updates(
    project_path: str,
    since: str | None,
    interval_seconds: int,
) -> dict[str, Any]:
    """Check if daemon knowledge has changed."""
    from ..server import check_for_updates

    return await check_for_updates(
        since=since,
        interval_seconds=interval_seconds,
        project_path=project_path,
    )
