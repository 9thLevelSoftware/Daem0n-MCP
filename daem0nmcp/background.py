"""Background task management for long-running operations."""

import asyncio
import contextlib
import uuid
from collections.abc import Awaitable
from datetime import datetime, timezone
from enum import Enum
from typing import Any


class TaskState(str, Enum):
    """States a background task can be in."""

    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class BackgroundTaskManager:
    """Manages background tasks with status tracking.

    This enables long-running operations like index_project or rebuild_communities
    to be started asynchronously with status checking.

    Usage:
        manager = BackgroundTaskManager()

        async def long_operation():
            # ... do work ...
            return result

        task_id = await manager.create_task(long_operation(), "indexing")

        # Check status later
        status = manager.get_status(task_id)

        # Or wait for completion
        result = await manager.wait_for(task_id)
    """

    def __init__(self):
        self._tasks: dict[str, dict[str, Any]] = {}
        self._results: dict[str, Any] = {}

    async def create_task(
        self, coro: Awaitable[Any], name: str, project_path: str | None = None
    ) -> str:
        """Create and start a background task.

        Args:
            coro: The coroutine to run
            name: Human-readable name for the task
            project_path: Optional project path to associate with task

        Returns:
            task_id: Unique identifier for tracking this task
        """
        task_id = str(uuid.uuid4())[:8]

        self._tasks[task_id] = {
            "id": task_id,
            "name": name,
            "project_path": project_path,
            "state": TaskState.PENDING,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "started_at": None,
            "completed_at": None,
            "error": None,
        }

        # Wrap coroutine to track state
        async def wrapper():
            self._tasks[task_id]["state"] = TaskState.RUNNING
            self._tasks[task_id]["started_at"] = datetime.now(timezone.utc).isoformat()
            try:
                result = await coro
                self._results[task_id] = result
                self._tasks[task_id]["state"] = TaskState.COMPLETED
            except asyncio.CancelledError:
                self._tasks[task_id]["state"] = TaskState.CANCELLED
                raise
            except Exception as e:
                self._tasks[task_id]["state"] = TaskState.FAILED
                self._tasks[task_id]["error"] = str(e)
                raise
            finally:
                self._tasks[task_id]["completed_at"] = datetime.now(
                    timezone.utc
                ).isoformat()

        self._tasks[task_id]["_task"] = asyncio.create_task(wrapper())
        return task_id

    def get_status(self, task_id: str) -> dict[str, Any] | None:
        """Get task status without internal fields.

        Args:
            task_id: The task identifier

        Returns:
            Status dict with id, name, state, timestamps, etc.
            Returns None if task_id is unknown.
        """
        task = self._tasks.get(task_id)
        if not task:
            return None
        return {k: v for k, v in task.items() if not k.startswith("_")}

    async def wait_for(self, task_id: str, timeout: float = 30.0) -> Any:
        """Wait for task completion and return result.

        Args:
            task_id: The task identifier
            timeout: Maximum seconds to wait (default 30)

        Returns:
            The result returned by the task

        Raises:
            ValueError: If task_id is unknown
            asyncio.TimeoutError: If task doesn't complete in time
            Exception: If the task raised an exception
        """
        task = self._tasks.get(task_id)
        if not task:
            raise ValueError(f"Unknown task: {task_id}")

        # Shield the task from cancellation so timeout doesn't cancel the task
        # This allows the caller to retry or check status later
        await asyncio.wait_for(asyncio.shield(task["_task"]), timeout=timeout)
        return self._results.get(task_id)

    async def cancel(self, task_id: str) -> bool:
        """Cancel a running task.

        Args:
            task_id: The task identifier

        Returns:
            True if task was cancelled, False if task_id unknown or no task
        """
        task = self._tasks.get(task_id)
        if not task or "_task" not in task:
            return False

        task["_task"].cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task["_task"]
        return True

    def list_tasks(self, project_path: str | None = None) -> list:
        """List all tasks, optionally filtered by project.

        Args:
            project_path: If provided, only return tasks for this project

        Returns:
            List of task status dicts
        """
        tasks = [self.get_status(tid) for tid in self._tasks]
        if project_path:
            tasks = [t for t in tasks if t and t.get("project_path") == project_path]
        return tasks
