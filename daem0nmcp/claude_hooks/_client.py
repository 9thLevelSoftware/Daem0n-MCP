"""
Shared client utilities for Claude Code hooks.

Provides direct Python imports (no HTTP/subprocess) for accessing
Daem0n-MCP's database, memory, and rules from hook scripts.
"""

import asyncio
import json
import os
import sys
import threading
from pathlib import Path
from typing import Any, NoReturn


def get_project_path() -> str | None:
    """
    Detect the project path from environment variables.

    Priority:
    1. CLAUDE_PROJECT_DIR (always set by Claude Code)
    2. DAEM0NMCP_PROJECT_ROOT
    3. os.getcwd()

    Returns None if .daem0nmcp directory doesn't exist at the detected path.
    """
    path = (
        os.environ.get("CLAUDE_PROJECT_DIR")
        or os.environ.get("DAEM0NMCP_PROJECT_ROOT")
        or os.getcwd()
    )
    if path and (Path(path) / ".daem0nmcp").exists():
        return path
    return None


def get_managers(project_path: str):
    """
    Construct DatabaseManager, MemoryManager, and RulesEngine for a project.

    Returns (db, memory, rules) tuple. Caller must call `await db.init_db()`
    before using.
    """
    from ..database import DatabaseManager
    from ..memory import MemoryManager
    from ..rules import RulesEngine

    storage_path = str(Path(project_path) / ".daem0nmcp" / "storage")
    db = DatabaseManager(storage_path)
    memory = MemoryManager(db)
    rules = RulesEngine(db)
    return db, memory, rules


def run_async(coro) -> Any:
    """Run an async coroutine synchronously."""
    return asyncio.run(coro)


def get_tool_input() -> dict:
    """Parse TOOL_INPUT env var as JSON, returning {} on failure."""
    raw = os.environ.get("TOOL_INPUT", "{}")
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return {}


def get_file_path_from_input() -> str | None:
    """Extract file_path or notebook_path from tool input."""
    data = get_tool_input()
    return data.get("file_path") or data.get("notebook_path")


def get_command_from_input() -> str | None:
    """Extract command from tool input (for Bash hooks)."""
    data = get_tool_input()
    return data.get("command")


def block(message: str) -> NoReturn:
    """
    Block the tool call with a message.

    If DAEM0N_HOOKS_PERMISSIVE=1, downgrades to stdout warning + exit 0.
    Otherwise, prints to stderr and exits with code 2.
    """
    if os.environ.get("DAEM0N_HOOKS_PERMISSIVE") == "1":
        print(message, file=sys.stdout)
        sys.exit(0)
    print(message, file=sys.stderr)
    sys.exit(2)


def succeed(message: str = "") -> NoReturn:
    """Exit successfully, optionally printing a message to stdout."""
    if message:
        print(message, file=sys.stdout)
    sys.exit(0)


def run_hook_safely(main_func, timeout_seconds: int = 5) -> None:
    """
    Run a hook's main function with a timeout and exception swallowing.

    Uses threading.Timer with os._exit as a last resort.  The real
    defence is that hook functions themselves use short timeouts
    (e.g. sqlite3 busy_timeout=2s) so they return before the
    watchdog fires.

    On timeout or exception, exits cleanly with code 0 so hooks never
    break the user's workflow.
    """

    def _timeout_handler():
        # Last-resort kill.  os._exit bypasses Python cleanup but
        # guarantees termination even if the main thread is stuck in
        # a native C call (e.g. SQLite busy-wait).
        os._exit(0)

    timer = threading.Timer(timeout_seconds, _timeout_handler)
    timer.daemon = True
    timer.start()
    try:
        main_func()
    except SystemExit as exc:
        # Re-raise clean exits; convert non-zero to 0 so hooks
        # never report errors to Claude Code.
        if exc.code and exc.code != 0:
            sys.exit(0)
        raise
    except Exception:
        sys.exit(0)
    finally:
        timer.cancel()
