from __future__ import annotations

import asyncio
import inspect
import unittest


class SyncFallbackTests(unittest.IsolatedAsyncioTestCase):
    def test_default_deadline_is_fifteen_seconds(self) -> None:
        from daem0nmcp.api.v7.fastmcp import build_fastmcp_server
        from daem0nmcp.api.v7.tasks import run_sync_fallback

        self.assertEqual(
            inspect.signature(run_sync_fallback).parameters["timeout_seconds"].default,
            15,
        )
        self.assertEqual(
            inspect.signature(build_fastmcp_server)
            .parameters["sync_timeout_seconds"]
            .default,
            15,
        )

    async def test_short_optional_work_completes_within_bound(self) -> None:
        from daem0nmcp.api.v7.tasks import run_sync_fallback

        async def operation() -> str:
            await asyncio.sleep(0)
            return "done"

        self.assertEqual(
            await run_sync_fallback(
                operation,
                estimated_to_fit=True,
                timeout_seconds=1,
            ),
            "done",
        )

    async def test_estimate_rejects_before_mutation(self) -> None:
        from daem0nmcp.api.v7.tasks import TaskExecutionError, run_sync_fallback

        mutated = False

        async def operation() -> None:
            nonlocal mutated
            mutated = True

        with self.assertRaisesRegex(TaskExecutionError, "TASK_REQUIRED") as caught:
            await run_sync_fallback(
                operation,
                estimated_to_fit=False,
                timeout_seconds=1,
            )
        self.assertEqual(caught.exception.code, "TASK_REQUIRED")
        self.assertFalse(mutated)

    async def test_timeout_cancels_and_joins_child_work(self) -> None:
        from daem0nmcp.api.v7.tasks import TaskExecutionError, run_sync_fallback

        started = asyncio.Event()
        cancelled = asyncio.Event()
        completed = False

        async def operation() -> None:
            nonlocal completed
            started.set()
            try:
                await asyncio.Future()
            finally:
                cancelled.set()
            completed = True

        with self.assertRaises(TaskExecutionError) as caught:
            await run_sync_fallback(
                operation,
                estimated_to_fit=True,
                timeout_seconds=0.01,
                _test_allow_subsecond=True,
            )
        self.assertEqual(caught.exception.code, "DEADLINE_EXCEEDED")
        self.assertTrue(started.is_set())
        self.assertTrue(cancelled.is_set())
        await asyncio.sleep(0)
        self.assertFalse(completed)

    async def test_committed_receipt_wins_over_deadline_cancellation(self) -> None:
        from daem0nmcp.api.v7.tasks import run_sync_fallback

        async def operation() -> str:
            try:
                await asyncio.Future()
            except asyncio.CancelledError:
                return "committed-receipt"

        result = await run_sync_fallback(
            operation,
            estimated_to_fit=True,
            timeout_seconds=0.01,
            _test_allow_subsecond=True,
        )

        self.assertEqual(result, "committed-receipt")

    async def test_caller_cancellation_is_never_translated_or_swallowed(self) -> None:
        from daem0nmcp.api.v7.tasks import run_sync_fallback

        child_cancelled = asyncio.Event()

        async def operation() -> None:
            try:
                await asyncio.Future()
            finally:
                child_cancelled.set()

        caller = asyncio.create_task(
            run_sync_fallback(
                operation,
                estimated_to_fit=True,
                timeout_seconds=1,
            )
        )
        await asyncio.sleep(0)
        caller.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await caller
        self.assertTrue(child_cancelled.is_set())

    async def test_repeated_cancellation_cannot_interrupt_child_drain(self) -> None:
        from daem0nmcp.api.v7.tasks import run_sync_fallback

        draining = asyncio.Event()
        release = asyncio.Event()
        finished = asyncio.Event()

        async def operation() -> None:
            try:
                await asyncio.Future()
            except asyncio.CancelledError:
                draining.set()
                while not release.is_set():
                    try:
                        await release.wait()
                    except asyncio.CancelledError:
                        continue
                raise
            finally:
                finished.set()

        caller = asyncio.create_task(
            run_sync_fallback(
                operation,
                estimated_to_fit=True,
                timeout_seconds=1,
            )
        )
        await asyncio.sleep(0)
        caller.cancel()
        await draining.wait()
        caller.cancel()
        await asyncio.sleep(0)
        self.assertFalse(caller.done())
        self.assertFalse(finished.is_set())

        release.set()
        with self.assertRaises(asyncio.CancelledError):
            await caller
        self.assertTrue(finished.is_set())

    async def test_public_timeout_bounds_are_strict(self) -> None:
        from daem0nmcp.api.v7.tasks import run_sync_fallback

        async def operation() -> None:
            return None

        for value in (0, 61, True, 10**400):
            with self.subTest(value=value), self.assertRaises(ValueError):
                await run_sync_fallback(
                    operation,
                    estimated_to_fit=True,
                    timeout_seconds=value,
                )


class ForegroundExecutionPolicyTests(unittest.TestCase):
    def test_every_optional_manifest_tool_has_an_explicit_policy(self) -> None:
        from daem0nmcp.api.v7.policy import V7_TOOL_LEVELS
        from daem0nmcp.api.v7.tasks import FOREGROUND_EXECUTION_POLICIES
        from daem0nmcp.api.v7.tools import build_tool_specs

        async def handler(**arguments):
            return arguments

        specs = build_tool_specs(dict.fromkeys(V7_TOOL_LEVELS, handler))
        optional = {spec.name for spec in specs if spec.task_mode == "optional"}

        self.assertEqual(set(FOREGROUND_EXECUTION_POLICIES), optional)

    def test_policy_bounds_payload_nested_collections_and_deadline(self) -> None:
        from daem0nmcp.api.v7.tasks import ForegroundExecutionPolicy

        policy = ForegroundExecutionPolicy(
            max_request_bytes=100,
            max_collection_lengths={"bundle.events": 2},
            max_numeric_values={"limit": 5},
            deadline_field="timeout_seconds",
        )
        admitted = {
            "bundle": {"events": [1, 2]},
            "limit": 5,
            "timeout_seconds": 15,
        }

        self.assertTrue(policy.admits(admitted, timeout_seconds=15))
        self.assertFalse(
            policy.admits(
                admitted | {"bundle": {"events": [1, 2, 3]}},
                timeout_seconds=15,
            )
        )
        self.assertFalse(policy.admits(admitted | {"limit": 6}, timeout_seconds=15))
        self.assertFalse(
            policy.admits(
                admitted | {"timeout_seconds": 16},
                timeout_seconds=15,
            )
        )
        self.assertFalse(policy.admits({"payload": "x" * 101}, timeout_seconds=15))


if __name__ == "__main__":
    unittest.main()
