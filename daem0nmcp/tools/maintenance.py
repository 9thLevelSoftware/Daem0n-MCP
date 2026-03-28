"""Data management tools: export_data, import_data, prune_memories, rebuild_index."""

import base64
import contextlib
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
    from ..models import Memory, Rule
except ImportError:
    from daem0nmcp import __version__
    from daem0nmcp.context_manager import (
        _default_project_path,
        _missing_project_path_error,
        get_project_context,
    )
    from daem0nmcp.logging_config import with_request_id
    from daem0nmcp.mcp_instance import mcp
    from daem0nmcp.models import Memory, Rule

from sqlalchemy import delete, or_, select

from ._deprecation import add_deprecation

logger = logging.getLogger(__name__)


# ============================================================================
# Tool 16: REBUILD_INDEX - Force rebuild of search indexes
# ============================================================================
@mcp.tool(version=__version__)
@with_request_id
async def rebuild_index(project_path: str | None = None) -> dict[str, Any]:
    """
    Force rebuild of TF-IDF/vector indexes. Use if search seems stale.

    Args:
        project_path: Project root
    """
    if not project_path and not _default_project_path:
        return _missing_project_path_error()

    ctx = await get_project_context(project_path)

    memory_stats = await ctx.memory_manager.rebuild_index()
    rules_stats = await ctx.rules_engine.rebuild_index()

    return {
        "status": "rebuilt",
        "memories": memory_stats,
        "rules": rules_stats,
        "message": f"Rebuilt indexes: {memory_stats['memories_indexed']} memories, {rules_stats['rules_indexed']} rules",
    }


@mcp.tool(version=__version__)
@with_request_id
async def export_data(
    project_path: str | None = None, include_vectors: bool = False
) -> dict[str, Any]:
    """
    [DEPRECATED] Use maintain(action='export') instead.

    Export all memories and rules as JSON for backup/migration.

    Args:
        project_path: Project root
        include_vectors: Include embeddings (large)
    """
    if not project_path and not _default_project_path:
        return _missing_project_path_error()

    ctx = await get_project_context(project_path)

    async with ctx.db_manager.get_session() as session:
        # Export memories
        result = await session.execute(select(Memory))
        memories = [
            {
                "id": m.id,
                "category": m.category,
                "content": m.content,
                "rationale": m.rationale,
                "context": m.context,
                "tags": m.tags,
                "file_path": m.file_path,
                "file_path_relative": m.file_path_relative,
                "keywords": m.keywords,
                "is_permanent": m.is_permanent,
                "outcome": m.outcome,
                "worked": m.worked,
                "pinned": m.pinned,
                "archived": m.archived,
                "created_at": m.created_at.isoformat() if m.created_at else None,
                "updated_at": m.updated_at.isoformat() if m.updated_at else None,
                # Optionally include vectors (base64 encoded)
                "vector_embedding": (
                    base64.b64encode(m.vector_embedding).decode()
                    if include_vectors and m.vector_embedding
                    else None
                ),
            }
            for m in result.scalars().all()
        ]

        # Export rules
        result = await session.execute(select(Rule))
        rules = [
            {
                "id": r.id,
                "trigger": r.trigger,
                "trigger_keywords": r.trigger_keywords,
                "must_do": r.must_do,
                "must_not": r.must_not,
                "ask_first": r.ask_first,
                "warnings": r.warnings,
                "priority": r.priority,
                "enabled": r.enabled,
                "created_at": r.created_at.isoformat() if r.created_at else None,
            }
            for r in result.scalars().all()
        ]

    result = {
        "version": __version__,
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "project_path": ctx.project_path,
        "memories": memories,
        "rules": rules,
    }
    return add_deprecation(result, "export_data", "maintain(action='export')")


@mcp.tool(version=__version__)
@with_request_id
async def import_data(
    data: dict[str, Any], project_path: str | None = None, merge: bool = True
) -> dict[str, Any]:
    """
    [DEPRECATED] Use maintain(action='import_data') instead.

    Import memories/rules from exported JSON.

    Args:
        data: Exported data structure
        merge: Add to existing (True) or replace all (False)
        project_path: Project root
    """
    if not project_path and not _default_project_path:
        return _missing_project_path_error()

    if "memories" not in data or "rules" not in data:
        return {"error": "Invalid data format. Expected 'memories' and 'rules' keys."}

    ctx = await get_project_context(project_path)

    def _parse_datetime(value: str | None) -> datetime | None:
        if not value:
            return None
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        if parsed.tzinfo:
            return parsed.astimezone(timezone.utc).replace(tzinfo=None)
        return parsed

    memories_imported = 0
    rules_imported = 0

    async with ctx.db_manager.get_session() as session:
        if not merge:
            await session.execute(delete(Memory))
            await session.execute(delete(Rule))

        # Import memories
        for mem_data in data.get("memories", []):
            # Decode vector if present
            vector_bytes = None
            if mem_data.get("vector_embedding"):
                with contextlib.suppress(Exception):
                    vector_bytes = base64.b64decode(mem_data["vector_embedding"])

            # Normalize file_path if present and project_path is available
            try:
                from ..memory import _normalize_file_path
            except ImportError:
                from daem0nmcp.memory import _normalize_file_path

            file_path_abs = mem_data.get("file_path")
            file_path_rel = mem_data.get("file_path_relative")
            if file_path_abs and ctx.project_path:
                file_path_abs, file_path_rel = _normalize_file_path(
                    file_path_abs, ctx.project_path
                )

            memory = Memory(
                category=mem_data["category"],
                content=mem_data["content"],
                rationale=mem_data.get("rationale"),
                context=mem_data.get("context", {}),
                tags=mem_data.get("tags", []),
                file_path=file_path_abs,
                file_path_relative=file_path_rel,
                keywords=mem_data.get("keywords"),
                is_permanent=mem_data.get("is_permanent", False),
                outcome=mem_data.get("outcome"),
                worked=mem_data.get("worked"),
                pinned=mem_data.get("pinned", False),
                archived=mem_data.get("archived", False),
                created_at=_parse_datetime(mem_data.get("created_at")),
                updated_at=_parse_datetime(mem_data.get("updated_at")),
                vector_embedding=vector_bytes,
            )
            session.add(memory)
            memories_imported += 1

        # Import rules
        for rule_data in data.get("rules", []):
            rule = Rule(
                trigger=rule_data["trigger"],
                trigger_keywords=rule_data.get("trigger_keywords"),
                must_do=rule_data.get("must_do", []),
                must_not=rule_data.get("must_not", []),
                ask_first=rule_data.get("ask_first", []),
                warnings=rule_data.get("warnings", []),
                priority=rule_data.get("priority", 0),
                enabled=rule_data.get("enabled", True),
            )
            session.add(rule)
            rules_imported += 1

    # Rebuild indexes
    await ctx.memory_manager.rebuild_index()
    await ctx.rules_engine.rebuild_index()

    result = {
        "status": "imported",
        "memories_imported": memories_imported,
        "rules_imported": rules_imported,
        "message": f"Imported {memories_imported} memories and {rules_imported} rules",
    }
    return add_deprecation(result, "import_data", "maintain(action='import_data')")


@mcp.tool(version=__version__)
@with_request_id
async def prune_memories(
    older_than_days: int = 90,
    categories: list[str] | None = None,
    min_recall_count: int = 5,
    protect_successful: bool = True,
    dry_run: bool = True,
    project_path: str | None = None,
) -> dict[str, Any]:
    """
    [DEPRECATED] Use maintain(action='prune') instead.

    Prune old low-value memories. Protected: permanent, pinned, with outcomes, frequently accessed.

    Args:
        older_than_days: Age threshold
        categories: Limit to these categories
        min_recall_count: Protect if accessed >= N times
        protect_successful: Protect worked=True
        dry_run: Preview only
        project_path: Project root
    """
    if not project_path and not _default_project_path:
        return _missing_project_path_error()

    ctx = await get_project_context(project_path)

    if categories is None:
        categories = ["decision", "learning"]

    cutoff = datetime.now(timezone.utc) - timedelta(days=older_than_days)

    async with ctx.db_manager.get_session() as session:
        # Find prunable memories with saliency-based protection
        query = select(Memory).where(
            Memory.category.in_(categories),
            Memory.created_at < cutoff,
            Memory.is_permanent == False,  # noqa: E712
            Memory.pinned == False,  # noqa: E712
            Memory.outcome.is_(None),  # Don't prune memories with outcomes
            or_(Memory.archived == False, Memory.archived.is_(None)),  # noqa: E712
            or_(
                Memory.recall_count < min_recall_count, Memory.recall_count.is_(None)
            ),  # Saliency protection
        )

        # Optionally protect successful decisions
        if protect_successful:
            query = query.where(or_(Memory.worked != True, Memory.worked.is_(None)))  # noqa: E712

        result = await session.execute(query)
        to_prune = result.scalars().all()

        if dry_run:
            return add_deprecation(
                {
                    "dry_run": True,
                    "would_prune": len(to_prune),
                    "categories": categories,
                    "older_than_days": older_than_days,
                    "min_recall_count": min_recall_count,
                    "protect_successful": protect_successful,
                    "samples": [
                        {
                            "id": m.id,
                            "content": m.content[:50],
                            "recall_count": getattr(m, "recall_count", 0) or 0,
                            "created_at": m.created_at.isoformat(),
                        }
                        for m in to_prune[:5]
                    ],
                },
                "prune_memories",
                "maintain(action='prune')",
            )

        # Actually delete
        for memory in to_prune:
            await session.delete(memory)

    # Rebuild index to remove pruned documents
    await ctx.memory_manager.rebuild_index()

    return add_deprecation(
        {
            "pruned": len(to_prune),
            "categories": categories,
            "older_than_days": older_than_days,
            "min_recall_count": min_recall_count,
            "message": f"Pruned {len(to_prune)} old memories (protected: pinned, outcomes, recall_count>={min_recall_count}, successful)",
        },
        "prune_memories",
        "maintain(action='prune')",
    )
