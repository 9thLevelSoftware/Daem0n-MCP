"""
Shared client utilities for Claude Code hooks.

Provides direct Python imports (no HTTP/subprocess) for accessing
Daem0n-MCP's database, memory, and rules from hook scripts.
"""

import json
import os
import sys
import threading
from pathlib import Path
from typing import Any, NoReturn

MAX_HOOK_INPUT_BYTES = 256 * 1024


def read_hook_event() -> dict[str, Any]:
    """Read Claude Code's bounded JSON event from standard input.

    Claude passes hook data on stdin; environment variables are retained only
    for the legacy standalone hooks and test compatibility.  Malformed or
    oversized data deliberately becomes an empty event so callers can deny a
    protected edit without echoing client data.
    """
    try:
        raw = sys.stdin.buffer.read(MAX_HOOK_INPUT_BYTES + 1)
        if len(raw) > MAX_HOOK_INPUT_BYTES:
            return {}
        value = json.loads(raw)
    except (AttributeError, OSError, TypeError, UnicodeError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def find_project_root(event: dict[str, Any]) -> Path | None:
    """Return the nearest Daem0n project containing the event cwd.

    Tries ``cwd`` (which follows ``cd`` in Bash), then ``CLAUDE_PROJECT_DIR``,
    walking up to the first ancestor with ``.daem0nmcp/``.  The home directory
    never counts: Daem0n keeps its own global state in ``~/.daem0nmcp/``.
    """
    home = Path.home()
    for raw in (event.get("cwd"), os.environ.get("CLAUDE_PROJECT_DIR")):
        if not isinstance(raw, str) or not raw:
            continue
        start = Path(os.path.abspath(raw))
        for candidate in (start, *start.parents):
            if candidate == home:
                break
            if (candidate / ".daem0nmcp").is_dir():
                return candidate
    return None


def _is_unc(path: str) -> bool:
    return path.startswith(("\\\\", "//"))


def relative_project_path(
    project: Path, raw: object, base: object = None
) -> str | None:
    """Return *raw* (relative to *base*, default *project*) relative to *project*.

    The result is in POSIX form, or None when *raw* is not inside *project*.

    Purely lexical: the path comes from the model, and resolving it on
    Windows could open a ``\\\\host\\share`` path (SMB authentication) before
    the user has approved anything.
    """
    if not isinstance(raw, str) or not raw or "\0" in raw or _is_unc(raw):
        return None
    root = os.path.normpath(str(project))
    start = base if isinstance(base, str) and base else root
    full = os.path.normpath(os.path.join(root, start, raw))
    if _is_unc(full):
        return None
    try:
        inside = os.path.commonpath(
            [os.path.normcase(root), os.path.normcase(full)]
        ) == os.path.normcase(root)
    except ValueError:  # different drives, or mixed absolute/relative
        return None
    if not inside or os.path.normcase(full) == os.path.normcase(root):
        return None
    return Path(os.path.relpath(full, root)).as_posix()


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
    import asyncio  # lazy: keeps the stdlib-only pre_edit hook fast

    return asyncio.run(coro)


def get_command_from_input() -> str | None:
    """Extract command from the legacy TOOL_INPUT env var (for Bash hooks)."""
    try:
        data = json.loads(os.environ.get("TOOL_INPUT", "{}"))
    except (json.JSONDecodeError, TypeError):
        return None
    return data.get("command") if isinstance(data, dict) else None


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
