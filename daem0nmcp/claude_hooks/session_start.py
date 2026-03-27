"""
Claude Code SessionStart hook - auto-communion with Daem0n.

Runs at session start to output a briefing summary and mark the
session as briefed. Never blocks.

Uses raw sqlite3 for the fast path (main) to avoid the overhead and
lock-contention risk of spinning up a full SQLAlchemy async engine
alongside the already-running MCP server.
"""

import hashlib
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

from ._client import get_project_path, succeed

# Keep async_main for test compatibility -------------------------------------------------


async def async_main(project_path: str) -> str:
    """
    Core async logic.  Returns the briefing text (empty string if nothing).

    Separated from ``main()`` so tests can call it inside an existing
    event loop without needing a subprocess.
    """
    from ._client import get_managers  # noqa: F811 – lazy import

    db, memory, _rules = get_managers(project_path)
    await db.init_db()

    stats = await memory.get_statistics()

    from ..enforcement import SessionManager

    session_mgr = SessionManager(db)
    await session_mgr.mark_briefed(project_path)

    return _format_briefing(stats)


# Fast synchronous path for the actual hook ----------------------------------------------

_BUSY_TIMEOUT_MS = 2000  # 2 seconds – fail fast if the MCP server holds a lock


def _get_session_id(project_path: str) -> str:
    """Mirror enforcement.get_session_id without importing the module."""
    repo_hash = hashlib.md5(project_path.encode()).hexdigest()[:8]
    hour_bucket = datetime.now().strftime("%Y%m%d%H")
    return f"{repo_hash}-{hour_bucket}"


def _fast_briefing(project_path: str) -> str:
    """
    Gather briefing stats using a direct sqlite3 connection.

    This avoids importing SQLAlchemy / aiosqlite / the full model stack,
    skips migrations (the MCP server handles those), and uses a short
    busy_timeout so we never block the Claude Code startup sequence.
    """
    db_path = Path(project_path) / ".daem0nmcp" / "storage" / "daem0nmcp.db"
    if not db_path.exists():
        return ""

    conn = sqlite3.connect(str(db_path), timeout=2)
    try:
        conn.execute(f"PRAGMA busy_timeout={_BUSY_TIMEOUT_MS}")
        conn.execute("PRAGMA journal_mode=WAL")

        # --- stats ---
        cursor = conn.execute(
            "SELECT category, COUNT(id) FROM memories GROUP BY category"
        )
        by_cat = {row[0]: row[1] for row in cursor.fetchall()}

        cursor = conn.execute("SELECT COUNT(id) FROM memories WHERE worked = 1")
        worked = cursor.fetchone()[0] or 0

        cursor = conn.execute("SELECT COUNT(id) FROM memories WHERE worked = 0")
        failed = cursor.fetchone()[0] or 0

        total = sum(by_cat.values())

        stats = {
            "total_memories": total,
            "by_category": by_cat,
            "with_outcomes": {
                "worked": worked,
                "failed": failed,
                "pending": total - worked - failed,
            },
        }

        # --- mark briefed (best-effort) ---
        session_id = _get_session_id(project_path)
        now = datetime.now(timezone.utc).isoformat()
        try:
            conn.execute(
                """
                INSERT INTO session_state
                    (session_id, project_path, briefed, context_checks,
                     pending_decisions, last_activity, created_at)
                VALUES (?, ?, 1, '[]', '[]', ?, ?)
                ON CONFLICT(session_id) DO UPDATE
                    SET briefed = 1, last_activity = excluded.last_activity
                """,
                (session_id, project_path, now, now),
            )
            conn.commit()
        except Exception:
            pass  # non-critical – don't fail the briefing over this

        return _format_briefing(stats)

    except sqlite3.OperationalError:
        # Database locked or table missing – exit silently
        return ""
    finally:
        conn.close()


# Shared formatting ----------------------------------------------------------------------


def _format_briefing(stats: dict) -> str:
    total = stats.get("total_memories", 0)
    by_cat = stats.get("by_category", {})
    outcomes = stats.get("with_outcomes", {})

    cat_parts = []
    for cat in ("decision", "pattern", "warning", "learning"):
        count = by_cat.get(cat, 0)
        if count:
            cat_parts.append(f"{count} {cat}s")

    cat_str = ", ".join(cat_parts) if cat_parts else "empty"
    parts = [f"[Daem0n Briefing] {total} memories ({cat_str})"]

    pending = outcomes.get("pending", 0)
    warnings_count = by_cat.get("warning", 0)
    parts.append(f"Pending outcomes: {pending} | Recent warnings: {warnings_count}")
    parts.append("Commune complete.")

    return "\n".join(parts)


# Entry points --------------------------------------------------------------------------


def main() -> None:
    project_path = get_project_path()
    if project_path is None:
        sys.exit(0)
    succeed(
        "[Daem0n] IMPORTANT: Before responding to the user, "
        'call mcp__daem0nmcp__commune(action="briefing") '
        "to receive your session briefing and project context."
    )


if __name__ == "__main__":
    import warnings

    warnings.filterwarnings("ignore")  # stderr output → Claude Code "hook error"

    from daem0nmcp.claude_hooks._client import run_hook_safely

    run_hook_safely(main)
