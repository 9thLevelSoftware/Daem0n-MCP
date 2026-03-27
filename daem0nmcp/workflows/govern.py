"""
Govern workflow - Rules & triggers.

Actions:
- add_rule: Add a decision tree rule (matched semantically)
- update_rule: Update an existing rule
- list_rules: List all configured rules
- add_trigger: Create auto-recall trigger
- list_triggers: List all configured context triggers
- remove_trigger: Remove a context trigger
"""

from typing import Any

from .errors import InvalidActionError, MissingParamError

VALID_ACTIONS = frozenset(
    {
        "add_rule",
        "update_rule",
        "list_rules",
        "add_trigger",
        "list_triggers",
        "remove_trigger",
    }
)


async def dispatch(
    action: str,
    project_path: str,
    *,
    # add_rule params
    trigger: str | None = None,
    must_do: list[str] | None = None,
    must_not: list[str] | None = None,
    ask_first: list[str] | None = None,
    warnings: list[str] | None = None,
    priority: int = 0,
    # update_rule params
    rule_id: int | None = None,
    enabled: bool | None = None,
    # list_rules params
    enabled_only: bool = True,
    limit: int = 50,
    # add_trigger params
    trigger_type: str | None = None,
    pattern: str | None = None,
    recall_topic: str | None = None,
    recall_categories: list[str] | None = None,
    # list_triggers params
    active_only: bool = True,
    # remove_trigger params
    trigger_id: int | None = None,
    **kwargs,
) -> Any:
    """Dispatch action to appropriate handler."""
    if action not in VALID_ACTIONS:
        raise InvalidActionError(action, sorted(VALID_ACTIONS))

    if action == "add_rule":
        if not trigger:
            raise MissingParamError("trigger", action)
        return await _do_add_rule(
            project_path,
            trigger,
            must_do,
            must_not,
            ask_first,
            warnings,
            priority,
        )

    elif action == "update_rule":
        if rule_id is None:
            raise MissingParamError("rule_id", action)
        return await _do_update_rule(
            project_path,
            rule_id,
            must_do,
            must_not,
            ask_first,
            warnings,
            priority,
            enabled,
        )

    elif action == "list_rules":
        return await _do_list_rules(project_path, enabled_only, limit)

    elif action == "add_trigger":
        if not trigger_type:
            raise MissingParamError("trigger_type", action)
        if not pattern:
            raise MissingParamError("pattern", action)
        if not recall_topic:
            raise MissingParamError("recall_topic", action)
        return await _do_add_trigger(
            project_path,
            trigger_type,
            pattern,
            recall_topic,
            recall_categories,
            priority,
        )

    elif action == "list_triggers":
        return await _do_list_triggers(project_path, active_only)

    elif action == "remove_trigger":
        if trigger_id is None:
            raise MissingParamError("trigger_id", action)
        return await _do_remove_trigger(project_path, trigger_id)

    raise InvalidActionError(action, sorted(VALID_ACTIONS))


async def _do_add_rule(
    project_path: str,
    trigger: str,
    must_do: list[str] | None,
    must_not: list[str] | None,
    ask_first: list[str] | None,
    warnings: list[str] | None,
    priority: int,
) -> dict[str, Any]:
    """Add a decision tree rule."""
    from ..server import add_rule

    return await add_rule(
        trigger=trigger,
        must_do=must_do,
        must_not=must_not,
        ask_first=ask_first,
        warnings=warnings,
        priority=priority,
        project_path=project_path,
    )


async def _do_update_rule(
    project_path: str,
    rule_id: int,
    must_do: list[str] | None,
    must_not: list[str] | None,
    ask_first: list[str] | None,
    warnings: list[str] | None,
    priority: int,
    enabled: bool | None,
) -> dict[str, Any]:
    """Update an existing rule."""
    from ..server import update_rule

    return await update_rule(
        rule_id=rule_id,
        must_do=must_do,
        must_not=must_not,
        ask_first=ask_first,
        warnings=warnings,
        priority=priority,
        enabled=enabled,
        project_path=project_path,
    )


async def _do_list_rules(project_path: str, enabled_only: bool, limit: int) -> Any:
    """List all configured rules."""
    from ..server import list_rules

    return await list_rules(
        enabled_only=enabled_only, limit=limit, project_path=project_path
    )


async def _do_add_trigger(
    project_path: str,
    trigger_type: str,
    pattern: str,
    recall_topic: str,
    recall_categories: list[str] | None,
    priority: int,
) -> dict[str, Any]:
    """Create auto-recall trigger."""
    from ..server import add_context_trigger

    return await add_context_trigger(
        trigger_type=trigger_type,
        pattern=pattern,
        recall_topic=recall_topic,
        recall_categories=recall_categories,
        priority=priority,
        project_path=project_path,
    )


async def _do_list_triggers(project_path: str, active_only: bool) -> dict[str, Any]:
    """List all configured context triggers."""
    from ..server import list_context_triggers

    return await list_context_triggers(
        active_only=active_only, project_path=project_path
    )


async def _do_remove_trigger(project_path: str, trigger_id: int) -> dict[str, Any]:
    """Remove a context trigger."""
    from ..server import remove_context_trigger

    return await remove_context_trigger(
        trigger_id=trigger_id, project_path=project_path
    )
