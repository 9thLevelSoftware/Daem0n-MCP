"""
Maintain workflow - Housekeeping & federation.

Actions:
- prune: Prune old low-value memories
- archive: Archive/unarchive a memory
- cleanup: Merge duplicate memories
- compact: Consolidate recent episodic memories into a summary
- rebuild_index: Force rebuild of TF-IDF/vector indexes
- export: Export all memories and rules as JSON
- import_data: Import memories/rules from exported JSON
- link_project: Link to another project for cross-project reading
- unlink_project: Remove project link
- list_projects: List all linked projects
- consolidate: Merge memories from all linked projects
- purge_dream_spam: Deduplicate dream re-evaluation and summary memories
"""

import logging
from collections import defaultdict
from typing import Any

from .errors import InvalidActionError, MissingParamError

logger = logging.getLogger(__name__)

VALID_ACTIONS = frozenset(
    {
        "prune",
        "archive",
        "cleanup",
        "compact",
        "rebuild_index",
        "export",
        "import_data",
        "link_project",
        "unlink_project",
        "list_projects",
        "consolidate",
        "purge_dream_spam",
    }
)


async def dispatch(
    action: str,
    project_path: str,
    *,
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
    **kwargs,
) -> dict[str, Any]:
    """Dispatch action to appropriate handler."""
    if action not in VALID_ACTIONS:
        raise InvalidActionError(action, sorted(VALID_ACTIONS))

    if action == "prune":
        return await _do_prune(
            project_path,
            older_than_days,
            categories,
            min_recall_count,
            protect_successful,
            dry_run,
        )

    elif action == "archive":
        if memory_id is None:
            raise MissingParamError("memory_id", action)
        return await _do_archive(project_path, memory_id, archived)

    elif action == "cleanup":
        return await _do_cleanup(project_path, dry_run, merge_duplicates)

    elif action == "compact":
        if not summary:
            raise MissingParamError("summary", action)
        return await _do_compact(project_path, summary, limit, topic, dry_run)

    elif action == "rebuild_index":
        return await _do_rebuild_index(project_path)

    elif action == "export":
        return await _do_export(project_path, include_vectors)

    elif action == "import_data":
        if data is None:
            raise MissingParamError("data", action)
        return await _do_import(project_path, data, merge)

    elif action == "link_project":
        if not linked_path:
            raise MissingParamError("linked_path", action)
        return await _do_link_project(project_path, linked_path, relationship, label)

    elif action == "unlink_project":
        if not linked_path:
            raise MissingParamError("linked_path", action)
        return await _do_unlink_project(project_path, linked_path)

    elif action == "list_projects":
        return await _do_list_projects(project_path)

    elif action == "consolidate":
        return await _do_consolidate(project_path, archive_sources)

    elif action == "purge_dream_spam":
        return await _do_purge_dream_spam(project_path, dry_run)

    raise InvalidActionError(action, sorted(VALID_ACTIONS))


async def _do_prune(
    project_path: str,
    older_than_days: int,
    categories: list[str] | None,
    min_recall_count: int,
    protect_successful: bool,
    dry_run: bool,
) -> dict[str, Any]:
    """Prune old low-value memories."""
    from ..server import prune_memories

    return await prune_memories(
        older_than_days=older_than_days,
        categories=categories,
        min_recall_count=min_recall_count,
        protect_successful=protect_successful,
        dry_run=dry_run,
        project_path=project_path,
    )


async def _do_archive(
    project_path: str, memory_id: int, archived: bool
) -> dict[str, Any]:
    """Archive/unarchive a memory."""
    from ..server import archive_memory

    return await archive_memory(
        memory_id=memory_id, archived=archived, project_path=project_path
    )


async def _do_cleanup(
    project_path: str, dry_run: bool, merge_duplicates: bool
) -> dict[str, Any]:
    """Merge duplicate memories."""
    from ..server import cleanup_memories

    return await cleanup_memories(
        dry_run=dry_run,
        merge_duplicates=merge_duplicates,
        project_path=project_path,
    )


async def _do_compact(
    project_path: str,
    summary: str,
    limit: int,
    topic: str | None,
    dry_run: bool,
) -> dict[str, Any]:
    """Consolidate recent episodic memories into a summary."""
    from ..server import compact_memories

    return await compact_memories(
        summary=summary,
        limit=limit,
        topic=topic,
        dry_run=dry_run,
        project_path=project_path,
    )


async def _do_rebuild_index(project_path: str) -> dict[str, Any]:
    """Force rebuild of TF-IDF/vector indexes."""
    from ..server import rebuild_index

    return await rebuild_index(project_path=project_path)


async def _do_export(project_path: str, include_vectors: bool) -> dict[str, Any]:
    """Export all memories and rules as JSON."""
    from ..server import export_data

    return await export_data(project_path=project_path, include_vectors=include_vectors)


async def _do_import(
    project_path: str, data: dict[str, Any], merge: bool
) -> dict[str, Any]:
    """Import memories/rules from exported JSON."""
    from ..server import import_data

    return await import_data(data=data, project_path=project_path, merge=merge)


async def _do_link_project(
    project_path: str,
    linked_path: str,
    relationship: str,
    label: str | None,
) -> dict[str, Any]:
    """Link to another project."""
    from ..server import link_projects

    return await link_projects(
        linked_path=linked_path,
        relationship=relationship,
        label=label,
        project_path=project_path,
    )


async def _do_unlink_project(project_path: str, linked_path: str) -> dict[str, Any]:
    """Remove project link."""
    from ..server import unlink_projects

    return await unlink_projects(linked_path=linked_path, project_path=project_path)


async def _do_list_projects(project_path: str) -> dict[str, Any]:
    """List all linked projects."""
    from ..server import list_linked_projects

    return await list_linked_projects(project_path=project_path)


async def _do_consolidate(project_path: str, archive_sources: bool) -> dict[str, Any]:
    """Merge memories from all linked projects."""
    from ..server import consolidate_linked_databases

    return await consolidate_linked_databases(
        archive_sources=archive_sources, project_path=project_path
    )


async def _do_purge_dream_spam(project_path: str, dry_run: bool) -> dict[str, Any]:
    """Deduplicate dream re-evaluation and summary memories.

    For re-evaluations: groups by source decision ID, keeps only the
    most recent per decision, deletes the rest.

    For summaries: keeps only the most recent per calendar day, deletes
    the rest.

    Args:
        project_path: Project root path.
        dry_run: If True (default), preview what would be deleted without
            actually deleting.

    Returns:
        Dict with counts of deleted/would-delete re-evaluations and summaries.
    """
    from sqlalchemy import delete, select

    try:
        from ..context_manager import get_project_context
    except ImportError:
        from daem0nmcp.context_manager import get_project_context

    try:
        from ..models import Memory
    except ImportError:
        from daem0nmcp.models import Memory

    ctx = await get_project_context(project_path)

    reeval_to_delete: list[int] = []
    summary_to_delete: list[int] = []

    async with ctx.db_manager.get_session() as db_session:
        # Query all learning memories with dream-related tags
        result = await db_session.execute(
            select(Memory)
            .where(Memory.category == "learning")
            .order_by(Memory.created_at.desc())
        )
        all_learning = result.scalars().all()

        # Separate re-evaluations and summaries
        reeval_by_decision: dict[int, list[Any]] = defaultdict(list)
        summary_by_day: dict[str, list[Any]] = defaultdict(list)

        for mem in all_learning:
            tags = mem.tags or []
            if "dream" not in tags:
                continue

            if "re-evaluation" in tags:
                # Extract source decision ID
                for tag in tags:
                    if tag.startswith("source-decision:"):
                        try:
                            decision_id = int(tag.split(":", 1)[1])
                            reeval_by_decision[decision_id].append(mem)
                        except (ValueError, IndexError):
                            pass
                        break
            elif "dream-summary" in tags:
                # Group by calendar day
                day_key = (
                    mem.created_at.strftime("%Y-%m-%d") if mem.created_at else "unknown"
                )
                summary_by_day[day_key].append(mem)

        # For each decision, keep only the most recent re-evaluation
        for decision_id, mems in reeval_by_decision.items():
            # Already sorted desc by created_at from query
            if len(mems) > 1:
                reeval_to_delete.extend(m.id for m in mems[1:])

        # For each day, keep only the most recent summary
        for day, mems in summary_by_day.items():
            if len(mems) > 1:
                summary_to_delete.extend(m.id for m in mems[1:])

        total_to_delete = reeval_to_delete + summary_to_delete

        if not dry_run and total_to_delete:
            await db_session.execute(
                delete(Memory).where(Memory.id.in_(total_to_delete))
            )

    # Rebuild index after deletion
    if not dry_run and total_to_delete:
        try:
            await ctx.memory_manager.rebuild_index()
        except Exception as e:
            logger.warning("Failed to rebuild index after purge: %s", e)

    return {
        "action": "purge_dream_spam",
        "dry_run": dry_run,
        "reeval_duplicates": len(reeval_to_delete),
        "summary_duplicates": len(summary_to_delete),
        "total_deleted" if not dry_run else "total_would_delete": len(total_to_delete),
        "unique_decisions_with_duplicates": sum(
            1 for mems in reeval_by_decision.values() if len(mems) > 1
        ),
        "unique_days_with_duplicate_summaries": sum(
            1 for mems in summary_by_day.values() if len(mems) > 1
        ),
        "message": (
            f"{'Would delete' if dry_run else 'Deleted'} "
            f"{len(reeval_to_delete)} duplicate re-evaluations and "
            f"{len(summary_to_delete)} duplicate summaries"
        ),
    }
