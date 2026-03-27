"""Tests for background task support."""

import asyncio

import pytest


class TestBackgroundTasks:
    """Verify background task management."""

    @pytest.mark.asyncio
    async def test_create_background_task(self):
        """Should create and track background task."""
        from daem0nmcp.background import BackgroundTaskManager

        manager = BackgroundTaskManager()

        async def slow_task():
            await asyncio.sleep(0.1)
            return {"result": "done"}

        task_id = await manager.create_task(slow_task(), "test_task")
        assert task_id is not None

        status = manager.get_status(task_id)
        assert status["state"] in ("pending", "running")

    @pytest.mark.asyncio
    async def test_task_completion(self):
        """Completed task should have result."""
        from daem0nmcp.background import BackgroundTaskManager

        manager = BackgroundTaskManager()

        async def fast_task():
            return {"value": 42}

        task_id = await manager.create_task(fast_task(), "fast_task")

        # Wait for completion
        result = await manager.wait_for(task_id, timeout=1.0)

        assert result["value"] == 42

        status = manager.get_status(task_id)
        assert status["state"] == "completed"

    @pytest.mark.asyncio
    async def test_task_cancellation(self):
        """Should be able to cancel running task."""
        from daem0nmcp.background import BackgroundTaskManager

        manager = BackgroundTaskManager()

        async def infinite_task():
            while True:
                await asyncio.sleep(0.1)

        task_id = await manager.create_task(infinite_task(), "infinite")

        await asyncio.sleep(0.05)  # Let it start
        cancelled = await manager.cancel(task_id)

        assert cancelled
        status = manager.get_status(task_id)
        assert status["state"] == "cancelled"

    @pytest.mark.asyncio
    async def test_task_failure(self):
        """Failed task should capture error."""
        from daem0nmcp.background import BackgroundTaskManager

        manager = BackgroundTaskManager()

        async def failing_task():
            raise ValueError("Something went wrong")

        task_id = await manager.create_task(failing_task(), "failing_task")

        # Wait for task to fail
        with pytest.raises(Exception):
            await manager.wait_for(task_id, timeout=1.0)

        status = manager.get_status(task_id)
        assert status["state"] == "failed"
        assert "Something went wrong" in status["error"]

    @pytest.mark.asyncio
    async def test_list_tasks(self):
        """Should list all tasks with optional project filter."""
        from daem0nmcp.background import BackgroundTaskManager

        manager = BackgroundTaskManager()

        async def task1():
            return 1

        async def task2():
            return 2

        await manager.create_task(task1(), "task1", project_path="/project/a")
        await manager.create_task(task2(), "task2", project_path="/project/b")

        # List all
        all_tasks = manager.list_tasks()
        assert len(all_tasks) == 2

        # Filter by project
        project_a_tasks = manager.list_tasks(project_path="/project/a")
        assert len(project_a_tasks) == 1
        assert project_a_tasks[0]["name"] == "task1"

    @pytest.mark.asyncio
    async def test_unknown_task_id(self):
        """Should handle unknown task IDs gracefully."""
        from daem0nmcp.background import BackgroundTaskManager

        manager = BackgroundTaskManager()

        # get_status returns None for unknown task
        status = manager.get_status("nonexistent")
        assert status is None

        # wait_for raises ValueError for unknown task
        with pytest.raises(ValueError):
            await manager.wait_for("nonexistent")

        # cancel returns False for unknown task
        result = await manager.cancel("nonexistent")
        assert result is False

    @pytest.mark.asyncio
    async def test_task_timestamps(self):
        """Task should track creation, start, and completion times."""
        from daem0nmcp.background import BackgroundTaskManager

        manager = BackgroundTaskManager()

        async def quick_task():
            return "done"

        task_id = await manager.create_task(quick_task(), "timed_task")
        await manager.wait_for(task_id, timeout=1.0)

        status = manager.get_status(task_id)
        assert status["created_at"] is not None
        assert status["started_at"] is not None
        assert status["completed_at"] is not None

    @pytest.mark.asyncio
    async def test_wait_for_timeout(self):
        """Should raise TimeoutError if task takes too long."""
        from daem0nmcp.background import BackgroundTaskManager

        manager = BackgroundTaskManager()

        async def slow_task():
            await asyncio.sleep(10)
            return "done"

        task_id = await manager.create_task(slow_task(), "slow_task")

        with pytest.raises(asyncio.TimeoutError):
            await manager.wait_for(task_id, timeout=0.1)

        # Task should still be running
        status = manager.get_status(task_id)
        assert status["state"] == "running"

        # Cleanup
        await manager.cancel(task_id)
