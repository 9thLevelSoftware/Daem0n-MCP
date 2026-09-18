"""Claude Code SessionStart hook for the v7 session briefing ritual.

The hook is deliberately read-only. It can render a compatibility preview from
an existing active database, but only the MCP ``session_brief`` handler may
establish communion for an invocation scope.
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

from ..storage_activation import PointerValidationError, resolve_active_database
from ..workspace import WorkspaceRegistry
from ._client import get_project_path, succeed

_BUSY_TIMEOUT_MS = 2000


def _workspace_id(project_path: str) -> str:
    """Return the stable opaque ID for the hook's configured project root."""
    return WorkspaceRegistry(default_root=project_path).default.workspace_id


async def async_main(project_path: str) -> str:
    """Return a read-only compatibility preview for in-process callers."""
    return _fast_briefing(project_path)


def _fast_briefing(project_path: str) -> str:
    """Read bounded legacy statistics without creating or updating storage."""
    storage = Path(project_path) / ".daem0nmcp" / "storage"
    if not storage.is_dir():
        return ""
    try:
        db_path = resolve_active_database(storage).path
    except (OSError, PointerValidationError):
        return ""
    if not db_path.is_file():
        return ""

    try:
        database_uri = f"file:{db_path.as_posix()}?mode=ro"
        conn = sqlite3.connect(database_uri, uri=True, timeout=2)
    except sqlite3.Error:
        return ""

    try:
        conn.execute(f"PRAGMA busy_timeout={_BUSY_TIMEOUT_MS}")
        conn.execute("PRAGMA query_only=ON")
        cursor = conn.execute(
            "SELECT category, COUNT(id) FROM memories GROUP BY category"
        )
        by_category = {row[0]: row[1] for row in cursor.fetchall()}
        cursor = conn.execute("SELECT COUNT(id) FROM memories WHERE worked = 1")
        worked = cursor.fetchone()[0] or 0
        cursor = conn.execute("SELECT COUNT(id) FROM memories WHERE worked = 0")
        failed = cursor.fetchone()[0] or 0
        total = sum(by_category.values())
        return _format_briefing(
            {
                "total_memories": total,
                "by_category": by_category,
                "with_outcomes": {
                    "worked": worked,
                    "failed": failed,
                    "pending": total - worked - failed,
                },
            }
        )
    except sqlite3.Error:
        return ""
    finally:
        conn.close()


def _format_briefing(stats: dict) -> str:
    """Format the optional preview while making its non-authoritative status clear."""
    total = stats.get("total_memories", 0)
    by_category = stats.get("by_category", {})
    outcomes = stats.get("with_outcomes", {})
    categories = [
        f"{count} {category}s"
        for category in ("decision", "pattern", "warning", "learning")
        if (count := by_category.get(category, 0))
    ]
    category_text = ", ".join(categories) if categories else "empty"
    return "\n".join(
        (
            f"[Daem0n preview] {total} memories ({category_text})",
            (
                f"Pending outcomes: {outcomes.get('pending', 0)} | "
                f"Recent warnings: {by_category.get('warning', 0)}"
            ),
            "Call session_brief for the authoritative v7 briefing.",
        )
    )


def main() -> None:
    project_path = get_project_path()
    if project_path is None:
        sys.exit(0)
    workspace_id = _workspace_id(project_path)
    succeed(
        "[Daem0n] IMPORTANT: Before responding to the user, call "
        f'mcp__daem0nmcp__session_brief(workspace_id="{workspace_id}") '
        "to establish this invocation's v7 session scope."
    )


if __name__ == "__main__":
    import warnings

    warnings.filterwarnings("ignore")

    from daem0nmcp.claude_hooks._client import run_hook_safely

    run_hook_safely(main)
