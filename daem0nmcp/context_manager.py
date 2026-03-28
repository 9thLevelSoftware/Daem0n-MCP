"""
ProjectContext lifecycle management for Daem0nMCP.

This module manages the creation, caching, eviction, and cleanup of
per-project context objects. Each ProjectContext holds the DatabaseManager,
MemoryManager, and RulesEngine for a single project.

Import hierarchy position:
    mcp_instance  (shared FastMCP -- no business imports)
        <- context_manager  (THIS MODULE -- ProjectContext lifecycle)
            <- tools/*  (tool definitions)
                <- server.py  (composition root)

This module does NOT import from mcp_instance, server, or any tools module.
It sits between the MCP instance and the tool implementations in the DAG.
"""

import asyncio
import contextlib
import logging
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    from .config import settings
    from .covenant import set_context_callback
    from .database import DatabaseManager
    from .logging_config import request_id_var, set_release_callback
    from .memory import MemoryManager
    from .rules import RulesEngine
    from .rwlock import RWLock
except ImportError:
    from daem0nmcp.config import settings
    from daem0nmcp.covenant import set_context_callback
    from daem0nmcp.database import DatabaseManager
    from daem0nmcp.logging_config import request_id_var, set_release_callback
    from daem0nmcp.memory import MemoryManager
    from daem0nmcp.rules import RulesEngine
    from daem0nmcp.rwlock import RWLock

logger = logging.getLogger(__name__)


# ============================================================================
# PROJECT CONTEXT MANAGEMENT - Support multiple projects via HTTP transport
# ============================================================================
@dataclass
class ProjectContext:
    """Holds all managers for a specific project."""

    project_path: str
    storage_path: str
    db_manager: DatabaseManager
    memory_manager: MemoryManager
    rules_engine: RulesEngine
    initialized: bool = False
    last_accessed: float = 0.0  # For LRU tracking
    active_requests: int = 0  # Prevent eviction while in use
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    # Covenant state tracking
    briefed: bool = False  # True after get_briefing called
    context_checks: list[dict[str, Any]] = field(
        default_factory=list
    )  # Timestamped context checks


# Cache of project contexts by normalized path
_project_contexts: dict[str, ProjectContext] = {}
_context_locks: dict[str, asyncio.Lock] = {}
_contexts_lock = (
    RWLock()
)  # RWLock for context access: multiple readers, exclusive writers
_task_contexts: dict[asyncio.Task, dict[str, int]] = {}
_task_contexts_lock = asyncio.Lock()
_last_eviction: float = 0.0
_EVICTION_INTERVAL_SECONDS: float = 60.0

# Default project path (ONLY used if DAEM0NMCP_PROJECT_ROOT is explicitly set)
_default_project_path: str | None = os.environ.get("DAEM0NMCP_PROJECT_ROOT")

# Configuration constants (read from settings)
MAX_PROJECT_CONTEXTS = settings.max_project_contexts
CONTEXT_TTL_SECONDS = settings.context_ttl_seconds


def _get_context_for_covenant(project_path: str) -> ProjectContext | None:
    """
    Get a project context for covenant enforcement.

    This is called by the covenant decorators to check session state.
    """
    try:
        normalized = str(Path(project_path).resolve())
        return _project_contexts.get(normalized)
    except Exception:
        return None


# Register the callback for covenant enforcement
set_context_callback(_get_context_for_covenant)


def _get_context_state_for_middleware(
    project_path: str | None,
) -> dict[str, Any] | None:
    """
    Get covenant state for middleware enforcement.

    This callback is used by CovenantMiddleware to check session state.
    It returns the state dict expected by CovenantTransform.check_tool_access().

    Args:
        project_path: Project path to look up (may be None for some tools)

    Returns:
        Dict with 'briefed' and 'context_checks' keys, or None if no context
    """
    if project_path is None:
        return None

    ctx = _get_context_for_covenant(project_path)
    if ctx is None:
        return None

    return {
        "briefed": ctx.briefed,
        "context_checks": ctx.context_checks,
    }


def _missing_project_path_error() -> dict[str, Any]:
    """Return an error dict when project_path is not provided."""
    return {
        "error": "MISSING_PROJECT_PATH",
        "message": (
            "The project_path parameter is REQUIRED. "
            "The Daem0n serves multiple realms - you must specify which project's memories to access. "
            "Pass your current working directory as project_path. "
            "Example: project_path='C:/Users/you/projects/myapp' or project_path='/home/you/projects/myapp'"
        ),
        "hint": "Run 'pwd' in bash to get your current directory, or check your Claude Code session header.",
    }


def _check_covenant_communion(project_path: str) -> dict[str, Any] | None:
    """
    Check if communion (get_briefing) was performed for this project.

    This function provides direct covenant enforcement for tool calls that
    bypass the FastMCP middleware (e.g., direct function calls in tests).

    Args:
        project_path: Project to check

    Returns:
        Violation dict if communion required, None if communion complete
    """
    from .covenant import CovenantViolation

    ctx = _get_context_for_covenant(project_path)
    if ctx is None or not ctx.briefed:
        return CovenantViolation.communion_required(project_path)
    return None


def _check_covenant_counsel(tool_name: str, project_path: str) -> dict[str, Any] | None:
    """
    Check if counsel (context_check) was sought for this project.

    This function provides direct covenant enforcement for tool calls that
    bypass the FastMCP middleware (e.g., direct function calls in tests).

    Args:
        tool_name: Name of the tool being called
        project_path: Project to check

    Returns:
        Violation dict if counsel required, None if counsel is fresh
    """
    from .covenant import COUNSEL_TTL_SECONDS, CovenantViolation

    # First check communion
    communion_violation = _check_covenant_communion(project_path)
    if communion_violation:
        return communion_violation

    ctx = _get_context_for_covenant(project_path)
    if ctx is None:
        return CovenantViolation.counsel_required(tool_name, project_path)

    context_checks = ctx.context_checks
    if not context_checks:
        return CovenantViolation.counsel_required(tool_name, project_path)

    # Find the most recent context check and verify it's still fresh
    now = datetime.now(timezone.utc)
    most_recent_age = None

    for check in context_checks:
        if isinstance(check, dict) and "timestamp" in check:
            try:
                check_time = datetime.fromisoformat(check["timestamp"])
                if check_time.tzinfo is None:
                    check_time = check_time.replace(tzinfo=timezone.utc)
                age = (now - check_time).total_seconds()
                if most_recent_age is None or age < most_recent_age:
                    most_recent_age = age
            except (ValueError, TypeError):
                continue

    if most_recent_age is None:
        return CovenantViolation.counsel_required(tool_name, project_path)

    if most_recent_age > COUNSEL_TTL_SECONDS:
        return CovenantViolation.counsel_expired(
            tool_name, project_path, int(most_recent_age)
        )

    return None  # Counsel is fresh


def _normalize_path(path: str) -> str:
    """Normalize a path for consistent cache keys."""
    if path is None:
        raise ValueError("Cannot normalize None path")
    return str(Path(path).resolve())


def _get_storage_for_project(project_path: str) -> str:
    """Get the storage path for a project."""
    return str(Path(project_path) / ".daem0nmcp" / "storage")


def _resolve_within_project(
    project_root: str, target_path: str | None
) -> tuple[Path | None, str | None]:
    """
    Resolve a path and ensure it stays within the project root.

    Args:
        project_root: The project root directory
        target_path: Optional path relative to project root

    Returns:
        Tuple of (resolved_path, error_message). On success, error_message is None.
        On failure, resolved_path is None and error_message describes the issue.
    """
    try:
        root = Path(project_root).resolve()
        candidate = root if not target_path else (root / target_path)
        resolved = candidate.resolve()
    except OSError as e:
        # Handle invalid paths (too long, invalid characters, permission issues, etc.)
        logger.warning(
            f"Path resolution failed for '{project_root}' / '{target_path}': {e}"
        )
        return None, f"Invalid path: {e}"

    try:
        resolved.relative_to(root)
    except ValueError:
        return None, "Path must be within the project root"

    return resolved, None


async def _release_task_contexts(task: asyncio.Task) -> None:
    """Release context usage counts for a completed task."""
    async with _task_contexts_lock:
        counts = _task_contexts.pop(task, None)

    if not counts:
        return

    for path, count in counts.items():
        ctx = _project_contexts.get(path)
        if ctx:
            async with ctx.lock:
                ctx.active_requests = max(0, ctx.active_requests - count)


def _schedule_task_release(task: asyncio.Task) -> None:
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    loop.create_task(_release_task_contexts(task))


async def _track_task_context(ctx: ProjectContext) -> None:
    """Track which task is using a context to avoid eviction while in-flight."""
    if not request_id_var.get():
        return

    task = asyncio.current_task()
    if task is None:
        return

    async with _task_contexts_lock:
        counts = _task_contexts.setdefault(task, {})
        counts[ctx.project_path] = counts.get(ctx.project_path, 0) + 1

        if not getattr(task, "_daem0n_ctx_tracked", False):
            task._daem0n_ctx_tracked = True
            task.add_done_callback(_schedule_task_release)

    async with ctx.lock:
        ctx.active_requests += 1


async def _release_current_task_contexts() -> None:
    """Release context usage for the current task (per tool call)."""
    task = asyncio.current_task()
    if task:
        await _release_task_contexts(task)


def _maybe_schedule_eviction(now: float) -> None:
    """Avoid running eviction too frequently."""
    global _last_eviction
    if now - _last_eviction < _EVICTION_INTERVAL_SECONDS:
        return
    _last_eviction = now
    asyncio.create_task(evict_stale_contexts())


# Register the release callback for per-request resource cleanup
set_release_callback(_release_current_task_contexts)


async def get_project_context(project_path: str | None = None) -> ProjectContext:
    """
    Get or create a ProjectContext for the given project path.
    Thread-safe with RWLock for concurrent access to prevent race conditions.

    Uses double-checked locking pattern with RWLock:
    - Fast path uses read lock (allows concurrent readers)
    - Slow path uses write lock (exclusive access for context creation)

    This enables the HTTP server to handle multiple projects simultaneously,
    each with its own isolated database.

    Args:
        project_path: Path to the project root. If None, uses default.

    Returns:
        ProjectContext with initialized managers for that project.
    """

    # Use default if not specified
    if not project_path:
        project_path = _default_project_path

    # Normalize for consistent caching - validate project_path is not None
    if not project_path:
        raise ValueError(
            "project_path is required when DAEM0NMCP_PROJECT_ROOT is not set"
        )
    normalized = _normalize_path(project_path)

    # Fast path with read lock: context exists and is initialized
    # Multiple concurrent readers can check simultaneously
    async with _contexts_lock.read():
        if normalized in _project_contexts:
            ctx = _project_contexts[normalized]
            if ctx.initialized:
                now = time.time()
                ctx.last_accessed = now
                # Opportunistic eviction: trigger background cleanup if over limit
                if len(_project_contexts) > MAX_PROJECT_CONTEXTS:
                    asyncio.create_task(evict_stale_contexts())
                else:
                    _maybe_schedule_eviction(now)
                await _track_task_context(ctx)
                return ctx

    # Slow path: need to create context, requires write lock
    async with _contexts_lock.write():
        # Double-check after acquiring write lock (another writer may have created it)
        if normalized in _project_contexts:
            ctx = _project_contexts[normalized]
            if ctx.initialized:
                now = time.time()
                ctx.last_accessed = now
                _maybe_schedule_eviction(now)
                await _track_task_context(ctx)
                return ctx

        # Get or create per-project lock for initialization
        if normalized not in _context_locks:
            _context_locks[normalized] = asyncio.Lock()
        lock = _context_locks[normalized]

    # Initialize under project-specific lock (outside RWLock to avoid holding it during I/O)
    async with lock:
        # Triple-check: another task may have initialized while we waited for per-project lock
        if normalized in _project_contexts:
            ctx = _project_contexts[normalized]
            if ctx.initialized:
                now = time.time()
                ctx.last_accessed = now
                _maybe_schedule_eviction(now)
                await _track_task_context(ctx)
                return ctx

        # Create new context
        storage_path = _get_storage_for_project(normalized)
        db_mgr = DatabaseManager(storage_path)
        mem_mgr = MemoryManager(db_mgr)
        rules_eng = RulesEngine(db_mgr)

        ctx = ProjectContext(
            project_path=normalized,
            storage_path=storage_path,
            db_manager=db_mgr,
            memory_manager=mem_mgr,
            rules_engine=rules_eng,
            initialized=False,
            last_accessed=time.time(),
        )

        # Initialize database
        await db_mgr.init_db()
        ctx.initialized = True

        # Store in cache under write lock
        async with _contexts_lock.write():
            _project_contexts[normalized] = ctx
        logger.info(
            f"Created project context for: {normalized} (storage: {storage_path})"
        )

        _maybe_schedule_eviction(time.time())
        await _track_task_context(ctx)
        return ctx


async def evict_stale_contexts() -> int:
    """
    Evict stale project contexts based on LRU and TTL policies.

    Returns the number of contexts evicted.

    Note: Uses a two-phase approach to avoid nested lock acquisition:
    1. Collect candidates under contexts_lock (no nested locks)
    2. Process each candidate individually with proper lock ordering
    """

    evicted = 0
    now = time.time()

    # Phase 1: Collect TTL candidates (no nested locks)
    ttl_candidates = []
    async with _contexts_lock.read():
        for path, ctx in _project_contexts.items():
            if (now - ctx.last_accessed) <= CONTEXT_TTL_SECONDS:
                continue
            # Skip if path-level lock is held
            if _context_locks.get(path) and _context_locks[path].locked():
                continue
            ttl_candidates.append(path)

    # Phase 2: Process TTL candidates individually
    for path in ttl_candidates:
        async with _contexts_lock.write():
            ctx = _project_contexts.get(path)
            if ctx is None:
                continue  # Already evicted by another task

            # Now safely check active_requests under context's own lock
            async with ctx.lock:
                if ctx.active_requests > 0:
                    continue  # Became active, skip

                # Safe to evict
                _project_contexts.pop(path, None)

            try:
                await ctx.db_manager.close()
            except Exception as e:
                logger.warning(f"Error closing context for {path}: {e}")
            evicted += 1
            logger.info(f"Evicted TTL-expired context: {path}")

    # Phase 3: LRU eviction if still over limit
    while True:
        async with _contexts_lock.write():
            if len(_project_contexts) <= MAX_PROJECT_CONTEXTS:
                break

            # Find candidates (paths with unlocked context locks)
            candidates = []
            for path, ctx in _project_contexts.items():
                if _context_locks.get(path) and _context_locks[path].locked():
                    continue
                candidates.append((path, ctx.last_accessed))

            if not candidates:
                break

            # Find oldest
            oldest_path = min(candidates, key=lambda x: x[1])[0]
            ctx = _project_contexts.get(oldest_path)

            if ctx is None:
                continue

            # Check if still idle under context lock
            async with ctx.lock:
                if ctx.active_requests > 0:
                    continue

                _project_contexts.pop(oldest_path, None)

            try:
                await ctx.db_manager.close()
            except Exception as e:
                logger.warning(f"Error closing context for {oldest_path}: {e}")
            evicted += 1
            logger.info(f"Evicted LRU context: {oldest_path}")

    # Phase 4: Clean up orphaned locks
    async with _contexts_lock.write():
        orphaned_locks = set(_context_locks.keys()) - set(_project_contexts.keys())
        for path in orphaned_locks:
            del _context_locks[path]

    return evicted


async def cleanup_all_contexts():
    """Clean up all project contexts on shutdown."""
    for path, ctx in _project_contexts.items():
        try:
            await ctx.db_manager.close()
            logger.info(f"Closed database for: {path}")
        except Exception as e:
            logger.warning(f"Error closing database for {path}: {e}")
    _project_contexts.clear()


@contextlib.asynccontextmanager
async def hold_context(ctx: "ProjectContext"):
    """
    Hold a project context's active_requests for the duration of a block.

    Use this for long-running operations (like Reflexion loops) that span
    multiple iterations. This prevents context eviction mid-operation.

    Usage:
        ctx = await get_project_context(project_path)
        async with hold_context(ctx):
            # ... long-running operation, context cannot be evicted ...
    """
    async with ctx.lock:
        ctx.active_requests += 1
    try:
        yield ctx
    finally:
        async with ctx.lock:
            ctx.active_requests = max(0, ctx.active_requests - 1)
