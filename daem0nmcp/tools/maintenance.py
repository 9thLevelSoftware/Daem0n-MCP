"""Data management tools: export_data, import_data, prune_memories, rebuild_index."""

import base64
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
    from ..covenant import legacy_entrypoint
    from ..event_store import (
        EventBundleError,
        delete_compatibility_memory,
        deterministic_id,
        export_event_bundle_async,
        import_event_bundle_async,
        resolve_compatibility_stream_async,
        sha256_json,
    )
    from ..logging_config import with_request_id
    from ..mcp_instance import mcp
    from ..models import Memory, MemoryRecord, MemoryVersion, Rule
except ImportError:
    from daem0nmcp import __version__
    from daem0nmcp.context_manager import (
        _default_project_path,
        _missing_project_path_error,
        get_project_context,
    )
    from daem0nmcp.covenant import legacy_entrypoint
    from daem0nmcp.event_store import (
        EventBundleError,
        delete_compatibility_memory,
        deterministic_id,
        export_event_bundle_async,
        import_event_bundle_async,
        resolve_compatibility_stream_async,
        sha256_json,
    )
    from daem0nmcp.logging_config import with_request_id
    from daem0nmcp.mcp_instance import mcp
    from daem0nmcp.models import Memory, MemoryRecord, MemoryVersion, Rule

from sqlalchemy import delete, or_, select

from ._deprecation import add_deprecation

logger = logging.getLogger(__name__)


# ============================================================================
# Tool 16: REBUILD_INDEX - Force rebuild of search indexes
# ============================================================================
@mcp.tool(version=__version__)
@with_request_id
@legacy_entrypoint("rebuild_index")
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
@legacy_entrypoint("export_data")
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
        memories = []
        for m in result.scalars().all():
            typed_id = None
            if ctx.db_manager.format_version == 7:
                typed_id = await resolve_compatibility_stream_async(
                    session,
                    ctx.db_manager.workspace_id,
                    "memory",
                    "memories",
                    m.id,
                )
                if typed_id is None:
                    raise RuntimeError("V7_MEMORY_STREAM_MISSING")
            memories.append(
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
                    "recall_count": m.recall_count,
                    "surprise_score": m.surprise_score,
                    "importance_score": m.importance_score,
                    "source_client": m.source_client,
                    "source_model": m.source_model,
                    "typed_id": typed_id,
                    "created_at": m.created_at.isoformat() if m.created_at else None,
                    "updated_at": m.updated_at.isoformat() if m.updated_at else None,
                    # Optionally include vectors (base64 encoded)
                    "vector_embedding": (
                        base64.b64encode(m.vector_embedding).decode()
                        if include_vectors
                        and ctx.db_manager.format_version != 7
                        and m.vector_embedding
                        else None
                    ),
                }
            )

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

        event_bundle = None
        if ctx.db_manager.format_version == 7:
            event_bundle = await export_event_bundle_async(
                session, ctx.db_manager.workspace_id
            )

    result = {
        "version": __version__,
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "project_path": ctx.project_path,
        "memories": memories,
        "rules": rules,
    }
    if event_bundle is not None:
        result["format_version"] = 7
        result["event_bundle"] = event_bundle
    return add_deprecation(result, "export_data", "maintain(action='export')")


@mcp.tool(version=__version__)
@with_request_id
@legacy_entrypoint("import_data")
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

    try:
        raw_memories = data.get("memories", [])
        raw_rules = data.get("rules", [])
        if not isinstance(raw_memories, list) or not isinstance(raw_rules, list):
            raise ValueError("memories and rules must be arrays")
        prepared_memories: list[tuple[dict[str, Any], bytes | None]] = []
        for item in raw_memories:
            if not isinstance(item, dict):
                raise ValueError("each memory must be an object")
            if not isinstance(item.get("content"), str):
                raise ValueError("memory content must be text")
            if "category" not in item:
                raise ValueError("memory category is required")
            if not isinstance(item.get("context", {}), dict):
                raise ValueError("memory context must be an object")
            if not isinstance(item.get("tags", []), list):
                raise ValueError("memory tags must be an array")
            for field in ("is_permanent", "pinned", "archived"):
                if (
                    field in item
                    and item[field] is not None
                    and not isinstance(item[field], bool)
                ):
                    raise ValueError(f"memory {field} must be boolean")
            recall_count = item.get("recall_count", 0)
            if isinstance(recall_count, bool) or not isinstance(recall_count, int):
                raise ValueError("memory recall_count must be an integer")
            if recall_count < 0:
                raise ValueError("memory recall_count cannot be negative")
            vector_bytes = None
            if item.get("vector_embedding") is not None:
                encoded = item["vector_embedding"]
                if not isinstance(encoded, str):
                    raise ValueError("memory vector must be base64 text")
                try:
                    vector_bytes = base64.b64decode(encoded, validate=True)
                except ValueError as exc:
                    raise ValueError("memory vector is invalid base64") from exc
            prepared_memories.append((item, vector_bytes))
        for item in raw_rules:
            if not isinstance(item, dict) or not isinstance(item.get("trigger"), str):
                raise ValueError("each rule requires a text trigger")
            for field in ("must_do", "must_not", "ask_first", "warnings"):
                if not isinstance(item.get(field, []), list):
                    raise ValueError(f"rule {field} must be an array")
    except ValueError:
        return {"error": "INVALID_IMPORT_DATA"}

    ctx = await get_project_context(project_path)
    v7_bundle = data.get("event_bundle") if data.get("format_version") == 7 else None
    if v7_bundle is not None and not isinstance(v7_bundle, dict):
        return {"error": "INVALID_EVENT_BUNDLE"}
    import_root = (
        v7_bundle.get("root_hash") if isinstance(v7_bundle, dict) else sha256_json(data)
    )
    if not isinstance(import_root, str):
        return {"error": "INVALID_IMPORT_DATA"}

    memories_imported = 0
    rules_imported = 0
    bundle_events_imported = 0
    bundle_events_existing = 0

    try:
        async with ctx.db_manager.get_session() as session:
            if not merge:
                existing_result = await session.execute(select(Memory))
                for existing in existing_result.scalars().all():
                    deleted_at = ctx.memory_manager._datetime_us()
                    await ctx.memory_manager._append_v7_memory_event(
                        session,
                        existing,
                        "memory.deleted",
                        deleted_at_us=deleted_at,
                        actor_type="import",
                        extra_payload={"import_replace": True},
                    )
                    await delete_compatibility_memory(session, existing)
                await session.execute(delete(Rule))
                await session.flush()

            if v7_bundle is not None:
                restored = await import_event_bundle_async(
                    session,
                    v7_bundle,
                    ctx.db_manager.workspace_id,
                )
                bundle_events_imported = restored.events_imported
                bundle_events_existing = restored.events_existing

            # Import memories. Direct v7 restore preserves compatibility IDs;
            # v6 imports receive a fresh compatibility row and deterministic stream.
            for index, (mem_data, vector_bytes) in enumerate(prepared_memories):
                if v7_bundle is not None and isinstance(mem_data.get("id"), int):
                    existing = await session.get(Memory, mem_data["id"])
                    if existing is not None:
                        continue
                import_stream_id = deterministic_id(
                    "mem",
                    "memory",
                    ctx.db_manager.workspace_id,
                    f"import:{import_root}:{index}",
                )
                if (
                    v7_bundle is None
                    and ctx.db_manager.format_version == 7
                    and await session.get(MemoryRecord, import_stream_id) is not None
                ):
                    bundle_events_existing += 1
                    continue

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

                created_at = _parse_datetime(mem_data.get("created_at"))
                updated_at = _parse_datetime(mem_data.get("updated_at"))
                if ctx.db_manager.format_version == 7:
                    created_at = created_at or datetime(1970, 1, 1)
                    updated_at = updated_at or created_at
                source_category = mem_data.get("category")
                compatibility_category = (
                    "<null>" if source_category is None else str(source_category)
                )
                memory = Memory(
                    id=(mem_data.get("id") if v7_bundle is not None else None),
                    category=compatibility_category,
                    content=mem_data["content"],
                    rationale=mem_data.get("rationale"),
                    context=mem_data.get("context", {}),
                    tags=mem_data.get("tags", []),
                    file_path=file_path_abs,
                    file_path_relative=file_path_rel,
                    keywords=mem_data.get("keywords"),
                    is_permanent=bool(mem_data.get("is_permanent", False)),
                    outcome=mem_data.get("outcome"),
                    worked=mem_data.get("worked"),
                    pinned=bool(mem_data.get("pinned", False)),
                    archived=bool(mem_data.get("archived", False)),
                    recall_count=mem_data.get("recall_count", 0),
                    surprise_score=mem_data.get("surprise_score"),
                    importance_score=mem_data.get("importance_score"),
                    source_client=mem_data.get("source_client"),
                    source_model=mem_data.get("source_model"),
                    created_at=created_at,
                    updated_at=updated_at,
                    vector_embedding=(
                        vector_bytes if ctx.db_manager.format_version != 7 else None
                    ),
                )
                session.add(memory)
                await session.flush()
                session.add(
                    MemoryVersion(
                        memory_id=memory.id,
                        version_number=1,
                        content=memory.content,
                        rationale=memory.rationale,
                        context=memory.context or {},
                        tags=memory.tags or [],
                        outcome=memory.outcome,
                        worked=memory.worked,
                        change_type="created",
                        change_description="Imported compatibility record",
                        changed_at=memory.created_at,
                        valid_from=memory.created_at,
                    )
                )
                if v7_bundle is None:
                    await ctx.memory_manager._append_v7_memory_event(
                        session,
                        memory,
                        "memory.imported",
                        occurred_at=memory.created_at,
                        recorded_at=memory.created_at,
                        stream_id=import_stream_id,
                        actor_type="import",
                        expected_stream_version=1,
                        compatibility_legacy_id=mem_data.get(
                            "id", f"import:{import_root}:{index}"
                        ),
                        extra_payload={
                            "import": {
                                "bundle_root_hash": import_root,
                                "item_index": index,
                                "source_category": source_category,
                            }
                        },
                    )
                memories_imported += 1

            # Rules remain a compatibility-only domain in Task 7.
            for rule_data in raw_rules:
                if v7_bundle is not None and isinstance(rule_data.get("id"), int):
                    existing_rule = await session.get(Rule, rule_data["id"])
                    if existing_rule is not None:
                        continue
                rule = Rule(
                    id=(rule_data.get("id") if v7_bundle is not None else None),
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
    except EventBundleError as exc:
        return {"error": exc.code}

    # Rebuild indexes
    await ctx.memory_manager.rebuild_index()
    await ctx.rules_engine.rebuild_index()

    result = {
        "status": "imported",
        "memories_imported": memories_imported,
        "rules_imported": rules_imported,
        "bundle_events_imported": bundle_events_imported,
        "bundle_events_existing": bundle_events_existing,
        "message": f"Imported {memories_imported} memories and {rules_imported} rules",
    }
    return add_deprecation(result, "import_data", "maintain(action='import_data')")


@mcp.tool(version=__version__)
@with_request_id
@legacy_entrypoint("prune_memories")
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
            deleted_at = ctx.memory_manager._datetime_us()
            await ctx.memory_manager._append_v7_memory_event(
                session,
                memory,
                "memory.deleted",
                deleted_at_us=deleted_at,
                extra_payload={"prune": {"older_than_days": older_than_days}},
            )
            await delete_compatibility_memory(session, memory)

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
