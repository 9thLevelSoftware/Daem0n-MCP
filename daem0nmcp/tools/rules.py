"""Rules engine tools: add_rule, check_rules, list_rules, update_rule."""

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

from ._deprecation import add_deprecation

logger = logging.getLogger(__name__)


# ============================================================================
# Tool 3: ADD_RULE - Create a decision tree node
# ============================================================================
@mcp.tool(version=__version__)
@with_request_id
async def add_rule(
    trigger: str,
    must_do: list[str] | None = None,
    must_not: list[str] | None = None,
    ask_first: list[str] | None = None,
    warnings: list[str] | None = None,
    priority: int = 0,
    project_path: str | None = None,
) -> dict[str, Any]:
    """
    [DEPRECATED] Use govern(action='add_rule') instead.

    Add a decision tree rule. Rules are matched semantically.

    Args:
        trigger: What activates this rule (natural language)
        must_do: Required actions
        must_not: Forbidden actions
        ask_first: Questions to consider
        warnings: Past experience warnings
        priority: Higher = shown first
        project_path: Project root
    """
    # Require project_path for multi-project support
    if not project_path and not _default_project_path:
        return _missing_project_path_error()

    ctx = await get_project_context(project_path)
    result = await ctx.rules_engine.add_rule(
        trigger=trigger,
        must_do=must_do,
        must_not=must_not,
        ask_first=ask_first,
        warnings=warnings,
        priority=priority,
    )

    return add_deprecation(result, "add_rule", "govern(action='add_rule')")


# ============================================================================
# Tool 4: CHECK_RULES - Validate an action against rules
# ============================================================================
@mcp.tool(version=__version__)
@with_request_id
async def check_rules(
    action: str,
    context: dict[str, Any] | None = None,
    project_path: str | None = None,
) -> dict[str, Any]:
    """
    [DEPRECATED] Use consult(action='check_rules') instead.

    Check if an action matches any rules. Call before significant changes.

    Args:
        action: What you're about to do
        context: Optional context dict
        project_path: Project root
    """
    # Require project_path for multi-project support
    if not project_path and not _default_project_path:
        return _missing_project_path_error()

    ctx = await get_project_context(project_path)
    result = await ctx.rules_engine.check_rules(action=action, context=context)
    return add_deprecation(result, "check_rules", "consult(action='check_rules')")


# ============================================================================
# Tool 8: LIST_RULES - See all configured rules
# ============================================================================
@mcp.tool(version=__version__)
@with_request_id
async def list_rules(
    enabled_only: bool = True, limit: int = 50, project_path: str | None = None
) -> list[dict[str, Any]]:
    """
    List all configured rules.

    Args:
        enabled_only: Only show enabled rules
        limit: Max results
        project_path: Project root
    """
    # Require project_path for multi-project support
    if not project_path and not _default_project_path:
        return _missing_project_path_error()

    ctx = await get_project_context(project_path)
    return await ctx.rules_engine.list_rules(enabled_only=enabled_only, limit=limit)


# ============================================================================
# Tool 9: UPDATE_RULE - Modify existing rules
# ============================================================================
@mcp.tool(version=__version__)
@with_request_id
async def update_rule(
    rule_id: int,
    must_do: list[str] | None = None,
    must_not: list[str] | None = None,
    ask_first: list[str] | None = None,
    warnings: list[str] | None = None,
    priority: int | None = None,
    enabled: bool | None = None,
    project_path: str | None = None,
) -> dict[str, Any]:
    """
    Update an existing rule.

    Args:
        rule_id: ID of rule to update
        must_do/must_not/ask_first/warnings: New lists (replace existing)
        priority: New priority
        enabled: Enable/disable
        project_path: Project root
    """
    # Require project_path for multi-project support
    if not project_path and not _default_project_path:
        return _missing_project_path_error()

    ctx = await get_project_context(project_path)
    return await ctx.rules_engine.update_rule(
        rule_id=rule_id,
        must_do=must_do,
        must_not=must_not,
        ask_first=ask_first,
        warnings=warnings,
        priority=priority,
        enabled=enabled,
    )
