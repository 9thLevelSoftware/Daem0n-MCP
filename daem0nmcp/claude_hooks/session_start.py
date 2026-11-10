"""Claude Code SessionStart hook for the v7 session briefing ritual.

The hook is deliberately read-only. It can render a compatibility preview from
an existing active database, but only the MCP ``session_brief`` handler may
establish communion for an invocation scope.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

from ..storage_activation import PointerValidationError, resolve_active_database
from ..workspace import WorkspaceIdentity, sqlite_file_uri, workspace_identity
from ._client import (
    REINSTALL_NOTICE,
    get_project_path,
    started_by_module_switch,
    succeed,
)

_BUSY_TIMEOUT_MS = 2000


# The hook is read-only: it resolves the recorded identity and never writes
# one.  Only the server, ``migrate-v7 --apply`` and ``workspace-id`` persist.
_MOVED_NOTE = (
    "[Daem0n] This folder looks moved or copied; if a shared server also "
    "serves the original, run `workspace-id --new` here."
)


def _hook_message(identity: WorkspaceIdentity) -> str:
    """Name the workspace to brief, or say why the folder is blocked."""
    if identity.blocked is not None:
        return (
            f"[Daem0n] This workspace is unavailable ({identity.blocked}). Run "
            "`python -m daem0nmcp.cli workspace-id` in the project directory "
            "to see why and settle it."
        )
    lines = [
        "[Daem0n] IMPORTANT: The first Daem0n call establishes this invocation's "
        "v7 session and brief automatically (meta.covenant.auto_brief). "
        f'Use workspace_id="{identity.workspace_id}". Call '
        f'mcp__daem0nmcp__session_brief(workspace_id="{identity.workspace_id}") '
        "for the full brief."
    ]
    if not identity.root_key_matches:
        lines.append(_MOVED_NOTE)
    return "\n".join(lines)


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
        database_uri = sqlite_file_uri(db_path, mode="ro")
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
        if started_by_module_switch():
            # The command is in the user's settings, so it runs in every
            # project they open, not only in Daem0n ones.
            succeed(json.dumps({"systemMessage": REINSTALL_NOTICE}))
        sys.exit(0)
    message = _hook_message(workspace_identity(project_path, live_storage=True))
    if started_by_module_switch():
        # JSON output lets the notice reach the user, not only the model.
        succeed(
            json.dumps(
                {
                    "systemMessage": REINSTALL_NOTICE,
                    "hookSpecificOutput": {
                        "hookEventName": "SessionStart",
                        "additionalContext": message,
                    },
                }
            )
        )
    succeed(message)


if __name__ == "__main__":
    import warnings

    warnings.filterwarnings("ignore")

    from daem0nmcp.claude_hooks._client import run_hook_safely

    run_hook_safely(main)
