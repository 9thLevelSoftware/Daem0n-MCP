"""Consolidated workflow tools: commune, consult, inscribe, reflect, understand, govern, explore, maintain."""

import logging
from typing import Any

try:
    from .. import __version__
    from ..context_manager import (
        _default_project_path,
        _missing_project_path_error,
    )
    from ..logging_config import with_request_id
    from ..mcp_instance import mcp
except ImportError:
    from daem0nmcp import __version__
    from daem0nmcp.context_manager import (
        _default_project_path,
        _missing_project_path_error,
    )
    from daem0nmcp.logging_config import with_request_id
    from daem0nmcp.mcp_instance import mcp

from ._deprecation import workflow_call

# Import workflow error types
try:
    from ..workflows.errors import WorkflowError
except ImportError:
    from daem0nmcp.workflows.errors import WorkflowError

logger = logging.getLogger(__name__)


# ----------------------------------------------------------------------------
# Workflow 1: COMMUNE - Session start & status
# Actions: briefing, active_context, triggers, health, covenant, updates
# ----------------------------------------------------------------------------
@mcp.tool(version=__version__)
@with_request_id
async def commune(
    action: str,
    project_path: str | None = None,
    focus_areas: list[str] | None = None,
    visual: bool = False,
    file_path: str | None = None,
    tags: list[str] | None = None,
    entities: list[str] | None = None,
    limit: int = 5,
    since: str | None = None,
    interval_seconds: int = 10,
    parent_community_id: int | None = None,
) -> dict[str, Any]:
    """
    Session start & status operations.

    Actions: briefing, active_context, triggers, health, covenant, updates
    """
    pp = project_path or _default_project_path
    if not pp:
        return _missing_project_path_error()

    try:
        try:
            from ..workflows import commune as commune_mod
        except ImportError:
            from daem0nmcp.workflows import commune as commune_mod
        with workflow_call():
            return await commune_mod.dispatch(
                action=action,
                project_path=pp,
                focus_areas=focus_areas,
                visual=visual,
                file_path=file_path,
                tags=tags,
                entities=entities,
                limit=limit,
                since=since,
                interval_seconds=interval_seconds,
                parent_community_id=parent_community_id,
            )
    except WorkflowError as e:
        return {"error": str(e), "recovery_hint": e.recovery_hint}


# ----------------------------------------------------------------------------
# Workflow 2: CONSULT - Pre-action intelligence
# Actions: preflight, recall, recall_file, recall_entity,
#          recall_hierarchical, search, check_rules, compress
# ----------------------------------------------------------------------------
@mcp.tool(version=__version__)
@with_request_id
async def consult(
    action: str,
    project_path: str | None = None,
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
) -> dict[str, Any]:
    """
    Pre-action intelligence gathering.

    Actions: preflight, recall, recall_file, recall_entity,
             recall_hierarchical, search, check_rules, compress
    """
    pp = project_path or _default_project_path
    if not pp:
        return _missing_project_path_error()

    try:
        try:
            from ..workflows import consult as consult_mod
        except ImportError:
            from daem0nmcp.workflows import consult as consult_mod
        with workflow_call():
            return await consult_mod.dispatch(
                action=action,
                project_path=pp,
                description=description,
                topic=topic,
                categories=categories,
                tags=tags,
                file_path=file_path,
                offset=offset,
                limit=limit,
                since=since,
                until=until,
                include_linked=include_linked,
                visual=visual,
                condensed=condensed,
                entity_name=entity_name,
                entity_type=entity_type,
                include_members=include_members,
                query=query,
                include_meta=include_meta,
                highlight=highlight,
                highlight_start=highlight_start,
                highlight_end=highlight_end,
                action_desc=action_desc,
                context=context,
                compress_text=compress_text,
                rate=rate,
                content_type=content_type,
                preserve_code=preserve_code,
            )
    except WorkflowError as e:
        return {"error": str(e), "recovery_hint": e.recovery_hint}


# ----------------------------------------------------------------------------
# Workflow 3: INSCRIBE - Memory writing & linking
# Actions: remember, remember_batch, link, unlink, pin,
#          activate, deactivate, clear_active, ingest
# ----------------------------------------------------------------------------
@mcp.tool(version=__version__)
@with_request_id
async def inscribe(
    action: str,
    project_path: str | None = None,
    # remember params
    category: str | None = None,
    content: str | None = None,
    rationale: str | None = None,
    context: dict[str, Any] | None = None,
    tags: list[str] | None = None,
    file_path: str | None = None,
    happened_at: str | None = None,
    # remember_batch params
    memories: list[dict[str, Any]] | None = None,
    # link/unlink params
    source_id: int | None = None,
    target_id: int | None = None,
    relationship: str | None = None,
    description: str | None = None,
    # pin/activate/deactivate params
    memory_id: int | None = None,
    pinned: bool = True,
    # activate params
    reason: str | None = None,
    priority: int = 0,
    expires_in_hours: int | None = None,
    # ingest params
    url: str | None = None,
    topic: str | None = None,
    chunk_size: int = 2000,
) -> dict[str, Any]:
    """
    Memory writing & linking operations.

    Actions: remember, remember_batch, link, unlink, pin,
             activate, deactivate, clear_active, ingest
    """
    pp = project_path or _default_project_path
    if not pp:
        return _missing_project_path_error()

    try:
        try:
            from ..workflows import inscribe as inscribe_mod
        except ImportError:
            from daem0nmcp.workflows import inscribe as inscribe_mod
        with workflow_call():
            return await inscribe_mod.dispatch(
                action=action,
                project_path=pp,
                category=category,
                content=content,
                rationale=rationale,
                context=context,
                tags=tags,
                file_path=file_path,
                happened_at=happened_at,
                memories=memories,
                source_id=source_id,
                target_id=target_id,
                relationship=relationship,
                description=description,
                memory_id=memory_id,
                pinned=pinned,
                reason=reason,
                priority=priority,
                expires_in_hours=expires_in_hours,
                url=url,
                topic=topic,
                chunk_size=chunk_size,
            )
    except WorkflowError as e:
        return {"error": str(e), "recovery_hint": e.recovery_hint}


# ----------------------------------------------------------------------------
# Workflow 4: REFLECT - Outcomes & verification
# Actions: outcome, verify, execute
# ----------------------------------------------------------------------------
@mcp.tool(version=__version__)
@with_request_id
async def reflect(
    action: str,
    project_path: str | None = None,
    # outcome params
    memory_id: int | None = None,
    outcome_text: str | None = None,
    worked: bool | None = None,
    # verify params
    text: str | None = None,
    categories: list[str] | None = None,
    as_of_time: str | None = None,
    # execute params
    code: str | None = None,
    timeout_seconds: int | None = None,
) -> dict[str, Any]:
    """
    Outcomes & verification operations.

    Actions: outcome, verify, execute
    """
    pp = project_path or _default_project_path
    if not pp:
        return _missing_project_path_error()

    try:
        try:
            from ..workflows import reflect as reflect_mod
        except ImportError:
            from daem0nmcp.workflows import reflect as reflect_mod
        with workflow_call():
            return await reflect_mod.dispatch(
                action=action,
                project_path=pp,
                memory_id=memory_id,
                outcome_text=outcome_text,
                worked=worked,
                text=text,
                categories=categories,
                as_of_time=as_of_time,
                code=code,
                timeout_seconds=timeout_seconds,
            )
    except WorkflowError as e:
        return {"error": str(e), "recovery_hint": e.recovery_hint}


# ----------------------------------------------------------------------------
# Workflow 5: UNDERSTAND - Code comprehension
# Actions: index, find, impact, todos, refactor
# ----------------------------------------------------------------------------
@mcp.tool(version=__version__)
@with_request_id
async def understand(
    action: str,
    project_path: str | None = None,
    # index params
    path: str | None = None,
    patterns: list[str] | None = None,
    # find params
    query: str | None = None,
    limit: int = 20,
    # impact params
    entity_name: str | None = None,
    # todos params
    auto_remember: bool = False,
    types: list[str] | None = None,
    # refactor params
    file_path: str | None = None,
) -> dict[str, Any]:
    """
    Code comprehension operations.

    Actions: index, find, impact, todos, refactor
    """
    pp = project_path or _default_project_path
    if not pp:
        return _missing_project_path_error()

    try:
        try:
            from ..workflows import understand as understand_mod
        except ImportError:
            from daem0nmcp.workflows import understand as understand_mod
        with workflow_call():
            return await understand_mod.dispatch(
                action=action,
                project_path=pp,
                path=path,
                patterns=patterns,
                query=query,
                limit=limit,
                entity_name=entity_name,
                auto_remember=auto_remember,
                types=types,
                file_path=file_path,
            )
    except WorkflowError as e:
        return {"error": str(e), "recovery_hint": e.recovery_hint}


# ----------------------------------------------------------------------------
# Workflow 6: GOVERN - Rules & triggers
# Actions: add_rule, update_rule, list_rules,
#          add_trigger, list_triggers, remove_trigger
# ----------------------------------------------------------------------------
@mcp.tool(version=__version__)
@with_request_id
async def govern(
    action: str,
    project_path: str | None = None,
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
) -> dict[str, Any]:
    """
    Rules & triggers management.

    Actions: add_rule, update_rule, list_rules,
             add_trigger, list_triggers, remove_trigger
    """
    pp = project_path or _default_project_path
    if not pp:
        return _missing_project_path_error()

    try:
        try:
            from ..workflows import govern as govern_mod
        except ImportError:
            from daem0nmcp.workflows import govern as govern_mod
        with workflow_call():
            return await govern_mod.dispatch(
                action=action,
                project_path=pp,
                trigger=trigger,
                must_do=must_do,
                must_not=must_not,
                ask_first=ask_first,
                warnings=warnings,
                priority=priority,
                rule_id=rule_id,
                enabled=enabled,
                enabled_only=enabled_only,
                limit=limit,
                trigger_type=trigger_type,
                pattern=pattern,
                recall_topic=recall_topic,
                recall_categories=recall_categories,
                active_only=active_only,
                trigger_id=trigger_id,
            )
    except WorkflowError as e:
        return {"error": str(e), "recovery_hint": e.recovery_hint}


# ----------------------------------------------------------------------------
# Workflow 7: EXPLORE - Graph & discovery
# Actions: related, chain, graph, stats, communities, community_detail,
#          rebuild_communities, entities, backfill_entities, evolution,
#          versions, at_time
# ----------------------------------------------------------------------------
@mcp.tool(version=__version__)
@with_request_id
async def explore(
    action: str,
    project_path: str | None = None,
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
    # at_time params
    timestamp: str | None = None,
) -> dict[str, Any]:
    """
    Graph & discovery operations.

    Actions: related, chain, graph, stats, communities, community_detail,
             rebuild_communities, entities, backfill_entities, evolution,
             versions, at_time
    """
    pp = project_path or _default_project_path
    if not pp:
        return _missing_project_path_error()

    try:
        try:
            from ..workflows import explore as explore_mod
        except ImportError:
            from daem0nmcp.workflows import explore as explore_mod
        with workflow_call():
            return await explore_mod.dispatch(
                action=action,
                project_path=pp,
                memory_id=memory_id,
                relationship_types=relationship_types,
                direction=direction,
                max_depth=max_depth,
                start_memory_id=start_memory_id,
                end_memory_id=end_memory_id,
                memory_ids=memory_ids,
                topic=topic,
                format=format,
                visual=visual,
                include_orphans=include_orphans,
                level=level,
                parent_community_id=parent_community_id,
                community_id=community_id,
                min_community_size=min_community_size,
                resolution=resolution,
                entity_type=entity_type,
                limit=limit,
                entity_name=entity_name,
                include_invalidated=include_invalidated,
                entity_id=entity_id,
                timestamp=timestamp,
            )
    except WorkflowError as e:
        return {"error": str(e), "recovery_hint": e.recovery_hint}


# ----------------------------------------------------------------------------
# Workflow 8: MAINTAIN - Housekeeping & federation
# Actions: prune, archive, cleanup, compact, rebuild_index,
#          export, import_data, link_project, unlink_project,
#          list_projects, consolidate, purge_dream_spam
# ----------------------------------------------------------------------------
@mcp.tool(version=__version__)
@with_request_id
async def maintain(
    action: str,
    project_path: str | None = None,
    # prune params
    older_than_days: int = 90,
    categories: list[str] | None = None,
    min_recall_count: int = 5,
    protect_successful: bool = True,
    dry_run: bool = True,
    # archive params
    memory_id: int | None = None,
    archived: bool = True,
    # cleanup params
    merge_duplicates: bool = True,
    # compact params
    summary: str | None = None,
    limit: int = 10,
    topic: str | None = None,
    # export params
    include_vectors: bool = False,
    # import_data params
    data: dict[str, Any] | None = None,
    merge: bool = True,
    # link_project params
    linked_path: str | None = None,
    relationship: str = "related",
    label: str | None = None,
    # consolidate params
    archive_sources: bool = False,
) -> dict[str, Any]:
    """
    Housekeeping & federation operations.

    Actions: prune, archive, cleanup, compact, rebuild_index,
             export, import_data, link_project, unlink_project,
             list_projects, consolidate, purge_dream_spam
    """
    pp = project_path or _default_project_path
    if not pp:
        return _missing_project_path_error()

    try:
        try:
            from ..workflows import maintain as maintain_mod
        except ImportError:
            from daem0nmcp.workflows import maintain as maintain_mod
        with workflow_call():
            return await maintain_mod.dispatch(
                action=action,
                project_path=pp,
                older_than_days=older_than_days,
                categories=categories,
                min_recall_count=min_recall_count,
                protect_successful=protect_successful,
                dry_run=dry_run,
                memory_id=memory_id,
                archived=archived,
                merge_duplicates=merge_duplicates,
                summary=summary,
                limit=limit,
                topic=topic,
                include_vectors=include_vectors,
                data=data,
                merge=merge,
                linked_path=linked_path,
                relationship=relationship,
                label=label,
                archive_sources=archive_sources,
            )
    except WorkflowError as e:
        return {"error": str(e), "recovery_hint": e.recovery_hint}
