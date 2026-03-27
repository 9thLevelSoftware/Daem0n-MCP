"""Entity tools: list_entities, backfill_entities."""

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
    from ..models import Memory
except ImportError:
    from daem0nmcp import __version__
    from daem0nmcp.context_manager import (
        _default_project_path,
        _missing_project_path_error,
        get_project_context,
    )
    from daem0nmcp.logging_config import with_request_id
    from daem0nmcp.mcp_instance import mcp
    from daem0nmcp.models import Memory

from sqlalchemy import or_, select

logger = logging.getLogger(__name__)


@mcp.tool(version=__version__)
@with_request_id
async def list_entities(
    entity_type: str | None = None,
    limit: int = 20,
    project_path: str | None = None,
) -> dict[str, Any]:
    """
    List most frequently mentioned entities.

    Args:
        entity_type: Optional type filter
        limit: Max results
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
    entities = await entity_manager.get_popular_entities(
        project_path=ctx.project_path, entity_type=entity_type, limit=limit
    )

    return {"count": len(entities), "entities": entities}


@mcp.tool(version=__version__)
@with_request_id
async def backfill_entities(project_path: str | None = None) -> dict[str, Any]:
    """
    Extract entities from all existing memories. Safe to run multiple times.

    Args:
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

    # Query all non-archived memories
    async with ctx.db_manager.get_session() as session:
        result = await session.execute(
            select(Memory).where(
                or_(Memory.archived == False, Memory.archived.is_(None))  # noqa: E712
            )
        )
        memories = result.scalars().all()

    memories_processed = 0
    total_entities_extracted = 0

    for memory in memories:
        extraction_result = await entity_manager.process_memory(
            memory_id=memory.id,
            content=memory.content,
            project_path=ctx.project_path,
            rationale=memory.rationale,
        )
        memories_processed += 1
        total_entities_extracted += extraction_result.get("entities_found", 0)

    return {
        "status": "completed",
        "memories_processed": memories_processed,
        "entities_extracted": total_entities_extracted,
        "message": f"Processed {memories_processed} memories, extracted {total_entities_extracted} entities",
    }
