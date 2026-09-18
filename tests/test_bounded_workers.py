"""Dependency-free tests for bounded background worker admission."""

from __future__ import annotations

import asyncio
import threading
import unittest


async def _wait_until(predicate, *, attempts: int = 100) -> None:
    for _ in range(attempts):
        if predicate():
            return
        await asyncio.sleep(0.001)
    raise AssertionError("condition was not reached")


class TestBoundedWorkerPool(unittest.IsolatedAsyncioTestCase):
    async def test_cancellation_wins_over_worker_failure_during_cleanup(self):
        from daem0nmcp.bounded_workers import BoundedWorkerPool

        started = threading.Event()
        cancellation = threading.Event()
        cleanup_finished = threading.Event()
        pool = BoundedWorkerPool(max_workers=1, thread_name_prefix="test-worker")

        def failing_cleanup() -> None:
            started.set()
            cancellation.wait(timeout=1.0)
            cleanup_finished.set()
            raise RuntimeError("worker failed during cancellation cleanup")

        try:
            task = asyncio.create_task(
                pool.run_with_cancellation_cleanup(
                    failing_cleanup,
                    request_cancel=cancellation.set,
                )
            )
            await _wait_until(started.is_set)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            self.assertTrue(cleanup_finished.is_set())
            self.assertEqual(pool.in_flight, 0)
        finally:
            pool.shutdown()

    async def test_cancelled_caller_retains_capacity_until_worker_finishes(self):
        """Cancellation must not admit replacement work over a live worker."""
        from daem0nmcp.bounded_workers import (
            BoundedWorkerBusyError,
            BoundedWorkerPool,
        )

        started = threading.Event()
        release = threading.Event()
        pool = BoundedWorkerPool(max_workers=1, thread_name_prefix="test-worker")

        def blocking_operation() -> str:
            started.set()
            release.wait(timeout=1.0)
            return "finished"

        task = asyncio.create_task(pool.run(blocking_operation))
        await _wait_until(started.is_set)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task

        self.assertEqual(pool.in_flight, 1)
        with self.assertRaises(BoundedWorkerBusyError):
            await pool.run(lambda: "must-not-run")

        release.set()
        await _wait_until(lambda: pool.in_flight == 0)
        self.assertEqual(await pool.run(lambda: "recovered"), "recovered")
        pool.shutdown()

    async def test_full_pool_rejects_without_queueing_an_operation(self):
        """Overflow must fail immediately and never execute the rejected callable."""
        from daem0nmcp.bounded_workers import (
            BoundedWorkerBusyError,
            BoundedWorkerPool,
        )

        started = threading.Event()
        release = threading.Event()
        rejected_ran = threading.Event()
        pool = BoundedWorkerPool(max_workers=1, thread_name_prefix="test-worker")

        def blocking_operation() -> None:
            started.set()
            release.wait(timeout=1.0)

        first = asyncio.create_task(pool.run(blocking_operation))
        await _wait_until(started.is_set)
        loop_time = asyncio.get_running_loop().time
        before = loop_time()
        with self.assertRaises(BoundedWorkerBusyError):
            await pool.run(rejected_ran.set)
        elapsed = loop_time() - before

        self.assertLess(elapsed, 0.05)
        self.assertFalse(rejected_ran.is_set())
        release.set()
        await first
        pool.shutdown()


if __name__ == "__main__":
    unittest.main()
