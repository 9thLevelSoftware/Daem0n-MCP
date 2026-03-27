"""MCP Resources: warnings, failed, rules, context, triggered context."""

import json
import logging
import os

try:
    from ..context_manager import (
        _default_project_path,
        get_project_context,
    )
    from ..database import DatabaseManager
    from ..mcp_instance import mcp
    from ..models import Memory, Rule
except ImportError:
    from daem0nmcp.context_manager import (
        _default_project_path,
        get_project_context,
    )
    from daem0nmcp.database import DatabaseManager
    from daem0nmcp.mcp_instance import mcp
    from daem0nmcp.models import Memory, Rule

from sqlalchemy import or_, select

logger = logging.getLogger(__name__)


# ============================================================================
# Resource implementations (testable functions)
# ============================================================================


async def _warnings_resource_impl(
    project_path: str, db_manager: DatabaseManager
) -> str:
    """
    Implementation: Get active warnings for a project.

    Args:
        project_path: Path to the project root
        db_manager: Database manager to query

    Returns:
        Formatted markdown string of active warnings
    """
    try:
        async with db_manager.get_session() as session:
            result = await session.execute(
                select(Memory)
                .where(
                    Memory.category == "warning",
                    or_(Memory.archived == False, Memory.archived.is_(None)),  # noqa: E712,
                )
                .order_by(Memory.created_at.desc())
                .limit(10)
            )
            warnings = result.scalars().all()

        if not warnings:
            return "No active warnings for this project."

        lines = ["# Active Warnings", ""]
        for w in warnings:
            lines.append(f"- {w.content}")
            if w.rationale:
                lines.append(f"  Reason: {w.rationale}")

        return "\n".join(lines)

    except Exception as e:
        logger.error(f"Error fetching warnings resource: {e}")
        return f"Error: {e}"


async def _failed_resource_impl(project_path: str, db_manager: DatabaseManager) -> str:
    """
    Implementation: Get failed approaches to avoid repeating.

    These are decisions where worked=False.

    Args:
        project_path: Path to the project root
        db_manager: Database manager to query

    Returns:
        Formatted markdown string of failed approaches
    """
    try:
        async with db_manager.get_session() as session:
            result = await session.execute(
                select(Memory)
                .where(
                    Memory.worked == False,  # noqa: E712
                    or_(Memory.archived == False, Memory.archived.is_(None)),  # noqa: E712,
                )
                .order_by(Memory.created_at.desc())
                .limit(10)
            )
            failed = result.scalars().all()

        if not failed:
            return "No failed approaches recorded."

        lines = ["# Failed Approaches (Do Not Repeat)", ""]
        for f in failed:
            lines.append(f"- {f.content}")
            if f.outcome:
                lines.append(f"  Outcome: {f.outcome}")

        return "\n".join(lines)

    except Exception as e:
        logger.error(f"Error fetching failed resource: {e}")
        return f"Error: {e}"


async def _rules_resource_impl(project_path: str, db_manager: DatabaseManager) -> str:
    """
    Implementation: Get high-priority rules for a project.

    Returns top 5 rules by priority.

    Args:
        project_path: Path to the project root
        db_manager: Database manager to query

    Returns:
        Formatted markdown string of rules
    """
    try:
        async with db_manager.get_session() as session:
            result = await session.execute(
                select(Rule)
                .where(Rule.enabled == True)  # noqa: E712
                .order_by(Rule.priority.desc())
                .limit(5)
            )
            rules = result.scalars().all()

        if not rules:
            return "No rules defined for this project."

        lines = ["# Project Rules", ""]
        for r in rules:
            lines.append(f"## {r.trigger}")
            if r.must_do:
                lines.append("Must do:")
                for item in r.must_do:
                    lines.append(f"  - {item}")
            if r.must_not:
                lines.append("Must NOT:")
                for item in r.must_not:
                    lines.append(f"  - {item}")
            lines.append("")

        return "\n".join(lines)

    except Exception as e:
        logger.error(f"Error fetching rules resource: {e}")
        return f"Error: {e}"


async def _context_resource_impl(project_path: str, db_manager: DatabaseManager) -> str:
    """
    Implementation: Get combined project context.

    Combines warnings, failed approaches, and rules into one context document.

    Args:
        project_path: Path to the project root
        db_manager: Database manager to query

    Returns:
        Formatted markdown string with all context sections
    """
    try:
        warnings = await _warnings_resource_impl(project_path, db_manager)
        failed = await _failed_resource_impl(project_path, db_manager)
        rules = await _rules_resource_impl(project_path, db_manager)

        return f"""# Daem0n Project Context

{warnings}

---

{failed}

---

{rules}
"""

    except Exception as e:
        logger.error(f"Error fetching context resource: {e}")
        return f"Error: {e}"


# ============================================================================
# MCP Resource registrations
# ============================================================================


@mcp.resource("daem0n://warnings/{project_path}")
async def warnings_resource(project_path: str) -> str:
    """
    Active warnings for this project.

    Automatically injected - no tool call needed.
    MCP clients subscribing to this resource get automatic updates.
    """
    try:
        ctx = await get_project_context(project_path)
        return await _warnings_resource_impl(project_path, ctx.db_manager)
    except Exception as e:
        logger.error(f"Error in warnings_resource: {e}")
        return f"Error: {e}"


@mcp.resource("daem0n://failed/{project_path}")
async def failed_resource(project_path: str) -> str:
    """
    Failed approaches to avoid repeating.

    These are decisions where worked=False.
    """
    try:
        ctx = await get_project_context(project_path)
        return await _failed_resource_impl(project_path, ctx.db_manager)
    except Exception as e:
        logger.error(f"Error in failed_resource: {e}")
        return f"Error: {e}"


@mcp.resource("daem0n://rules/{project_path}")
async def rules_resource(project_path: str) -> str:
    """
    High-priority rules for this project.

    Top 5 rules by priority.
    """
    try:
        ctx = await get_project_context(project_path)
        return await _rules_resource_impl(project_path, ctx.db_manager)
    except Exception as e:
        logger.error(f"Error in rules_resource: {e}")
        return f"Error: {e}"


@mcp.resource("daem0n://context/{project_path}")
async def context_resource(project_path: str) -> str:
    """
    Combined project context - warnings, failed approaches, and rules.

    This is the main resource for automatic context injection.
    Subscribe to this for complete project awareness.
    """
    try:
        ctx = await get_project_context(project_path)
        return await _context_resource_impl(project_path, ctx.db_manager)
    except Exception as e:
        logger.error(f"Error in context_resource: {e}")
        return f"Error: {e}"


async def get_triggered_context_resource(
    file_path: str, project_path: str | None = None
) -> str:
    """
    MCP Resource implementation for dynamic context injection based on file path.

    When an AI tool accesses this resource with a file path,
    it returns auto-recalled memories based on matching triggers.

    Args:
        file_path: The file path to check triggers against
        project_path: Project root path (defaults to _default_project_path or cwd)

    Returns:
        JSON string with triggered context or error message
    """
    project_path = project_path or _default_project_path or os.getcwd()

    try:
        from ..context_triggers import ContextTriggerManager
    except ImportError:
        from daem0nmcp.context_triggers import ContextTriggerManager

    try:
        ctx = await get_project_context(project_path)
        tm = ContextTriggerManager(ctx.db_manager)

        result = await tm.get_triggered_context(
            project_path=project_path, file_path=file_path
        )

        if not result["triggers"]:
            return json.dumps(
                {
                    "file": file_path,
                    "triggers_matched": 0,
                    "context": [],
                    "message": "No triggers matched for this file",
                }
            )

        # Format for easy reading
        output = {
            "file": file_path,
            "triggers_matched": len(result["triggers"]),
            "topics_recalled": result.get("topics_recalled", []),
            "context": [],
        }

        # Include memory context for each topic
        for topic, recall_result in result.get("memories", {}).items():
            topic_context = {"topic": topic, "memories": []}

            # Extract memories from the recall result
            for category in ["decision", "pattern", "warning", "learning"]:
                category_memories = recall_result.get(category, [])
                for m in category_memories:
                    topic_context["memories"].append(
                        {
                            "category": category,
                            "content": m.get("content", ""),
                            "worked": m.get("worked"),
                        }
                    )

            output["context"].append(topic_context)

        return json.dumps(output, indent=2)

    except Exception as e:
        logger.error(f"Error in get_triggered_context_resource: {e}")
        return json.dumps({"file": file_path, "error": str(e)})


@mcp.resource("daem0n://triggered/{file_path}")
async def triggered_context_resource(file_path: str) -> str:
    """
    MCP Resource for dynamic context injection based on file path.

    When an AI tool accesses this resource with a file path,
    it returns auto-recalled memories based on matching triggers.

    Usage: Access daem0n://triggered/src/auth/service.py
    Returns: Contextually relevant memories for that file
    """
    return await get_triggered_context_resource(file_path)
