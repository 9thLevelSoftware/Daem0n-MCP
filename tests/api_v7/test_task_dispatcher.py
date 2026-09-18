"""Durable task admission, recovery, isolation, and real Valkey delivery."""

from __future__ import annotations

import asyncio
import os
import sqlite3
import tempfile
import unittest
from contextlib import asynccontextmanager, closing
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlsplit, urlunsplit

from pydantic import BaseModel, ConfigDict

from daem0nmcp.covenant import (
    CapabilityAuthority,
    CovenantGate,
    CovenantStateStore,
    InvocationScope,
)
from daem0nmcp.workspace import Workspace

WORKSPACE_ID = "ws_0123456789abcdef01234567"


class _Output(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    value: str


class _Resolver:
    def __init__(self, workspace: Workspace) -> None:
        self.workspace = workspace

    def resolve(self, workspace_id: str) -> Workspace:
        if workspace_id != self.workspace.workspace_id:
            raise KeyError(workspace_id)
        return self.workspace


def _unavailable_url(redis_url: str) -> str:
    parsed = urlsplit(redis_url)
    hostname = parsed.hostname or "127.0.0.1"
    username = "" if parsed.username is None else parsed.username
    password = "" if parsed.password is None else parsed.password
    credentials = f"{username}:{password}@" if username else f":{password}@"
    return urlunsplit(
        (parsed.scheme, f"{credentials}{hostname}:1", parsed.path, "", "")
    )


class TaskDispatcherConfigurationTests(unittest.TestCase):
    def test_queue_endpoint_must_be_authenticated_and_loopback(self) -> None:
        from daem0nmcp.api.v7.task_dispatcher import validate_task_redis_url

        self.assertEqual(
            validate_task_redis_url("redis://:secret@127.0.0.1:16379/0"),
            "redis://:secret@127.0.0.1:16379/0",
        )
        for value in (
            "redis://127.0.0.1:16379/0",
            "redis://:secret@example.com:6379/0",
            "http://:secret@127.0.0.1:16379/0",
        ):
            with self.subTest(value=value), self.assertRaises(ValueError):
                validate_task_redis_url(value)

    def test_production_enables_only_the_owned_configured_profile(self) -> None:
        from daem0nmcp.api.v7.production import _task_configuration

        with patch("importlib.util.find_spec", return_value=object()):
            enabled, state, redis_url = _task_configuration(
                {"DAEM0NMCP_TASK_REDIS_URL": ("redis://:secret@127.0.0.1:16379/0")}
            )
        self.assertTrue(enabled)
        self.assertEqual(state.status, "ready")
        self.assertEqual(redis_url, "redis://:secret@127.0.0.1:16379/0")

        disabled, state, redis_url = _task_configuration({})
        self.assertFalse(disabled)
        self.assertEqual(state.status, "disabled")
        self.assertIsNone(redis_url)


@unittest.skipUnless(
    os.environ.get("DAEM0NMCP_TASK_REDIS_URL"),
    "real authenticated Valkey certification endpoint is not configured",
)
class DurableTaskDispatcherValkeyTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        from daem0nmcp.api.v7.policy import V7_COVENANT_POLICY
        from daem0nmcp.api.v7.registry import ToolSpec, V7Manifest
        from daem0nmcp.api.v7.tools import (
            MemoryRecallInput,
            MemoryStoreInput,
            build_argument_normalizer,
        )
        from daem0nmcp.covenant import CovenantLevel

        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name) / "workspace"
        self.root.mkdir()
        self.workspace = Workspace(WORKSPACE_ID, self.root)
        self.resolver = _Resolver(self.workspace)
        self.gate = CovenantGate(
            state_store=CovenantStateStore(),
            authority=CapabilityAuthority(secret=b"t" * 32, kid="test"),
            policy=V7_COVENANT_POLICY,
            argument_normalizer=build_argument_normalizer(),
        )
        self.scope = InvocationScope("principal-a", "session-a", str(self.root))
        self.gate.record_briefing(self.scope)
        self.calls: list[str] = []
        self.cancel_started = asyncio.Event()
        self.cancel_cleaned = asyncio.Event()
        self.shutdown_read_started = asyncio.Event()
        self.shutdown_store_started = asyncio.Event()
        self.shutdown_read_attempts = 0
        self.shutdown_store_attempts = 0

        async def recall(**arguments):
            from daem0nmcp.api.v7.tasks import (
                durable_task_execution_var,
                is_durable_task_execution,
            )

            self.assertTrue(is_durable_task_execution("memory_recall", arguments))
            execution = durable_task_execution_var.get()
            self.assertIsNotNone(execution)
            self.assertEqual(self.scope.principal_id, execution.principal_id)
            self.assertIn(
                execution.transport_session_id,
                {"session-a", "mcp-session:session-a"},
            )
            self.calls.append("recall")
            if arguments["query"] == "cancel this task":
                self.cancel_started.set()
                try:
                    await asyncio.Future()
                finally:
                    self.cancel_cleaned.set()
            if arguments["query"] == "shutdown replay read":
                self.shutdown_read_attempts += 1
                if self.shutdown_read_attempts == 1:
                    self.shutdown_read_started.set()
                    await asyncio.Future()
            return _Output(value=str(arguments["query"]))

        async def store(**arguments):
            from daem0nmcp.api.v7.tasks import is_durable_task_execution

            self.assertTrue(is_durable_task_execution("memory_store", arguments))
            self.calls.append("store")
            if arguments["idempotency_key"] == "shutdown-store-0001":
                self.shutdown_store_attempts += 1
                if self.shutdown_store_attempts == 1:
                    self.shutdown_store_started.set()
                    await asyncio.Future()
            return _Output(value=str(arguments["idempotency_key"]))

        recall.__daem0nmcp_admission_aware__ = True  # type: ignore[attr-defined]
        store.__daem0nmcp_admission_aware__ = True  # type: ignore[attr-defined]

        annotations = {
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": False,
        }
        recall_spec = ToolSpec(
            name="memory_recall",
            description="Recall through a durable task.",
            handler=recall,
            input_model=MemoryRecallInput,
            output_model=_Output,
            category="memory",
            tags=("memory",),
            covenant=CovenantLevel.COMMUNION,
            task_mode="optional",
            annotations=annotations,
        )
        store_spec = ToolSpec(
            name="memory_store",
            description="Store through a durable task.",
            handler=store,
            input_model=MemoryStoreInput,
            output_model=_Output,
            category="memory",
            tags=("memory",),
            covenant=CovenantLevel.COUNSEL,
            task_mode="optional",
            annotations=annotations | {"readOnlyHint": False},
        )
        self.manifest = V7Manifest(
            tools=(recall_spec, store_spec),
            resources=(),
            policy={
                "memory_recall": CovenantLevel.COMMUNION,
                "memory_store": CovenantLevel.COUNSEL,
            },
            require_full_surface=False,
        )
        self.redis_url = os.environ["DAEM0NMCP_TASK_REDIS_URL"]
        self.queue_name = "daem0nmcp:test:" + os.urandom(12).hex()
        self.database = Path(self.temporary.name) / "storage" / "tasks.sqlite3"
        self.dispatchers = []

    async def asyncTearDown(self) -> None:
        for dispatcher in reversed(self.dispatchers):
            await dispatcher.aclose()
        from redis.asyncio import Redis

        client = Redis.from_url(self.redis_url, decode_responses=True)
        try:
            await client.delete(self.queue_name)
        finally:
            await client.aclose()
        self.temporary.cleanup()

    def _dispatcher(self, redis_url: str | None = None, **options):
        from daem0nmcp.api.v7.task_dispatcher import DurableTaskDispatcher

        dispatcher = DurableTaskDispatcher(
            database_path=self.database,
            redis_url=redis_url or self.redis_url,
            manifest=self.manifest,
            covenant_gate=self.gate,
            workspace_resolver=self.resolver,
            max_workers=2,
            queue_name=self.queue_name,
            **options,
        )
        self.dispatchers.append(dispatcher)
        return dispatcher

    async def test_scoped_task_quotas_reserve_atomically_before_side_effects(self):
        from daem0nmcp.api.v7.task_dispatcher import TaskDispatcherError

        dispatcher = self._dispatcher(
            max_pending_per_principal=1, max_pending_per_workspace=2
        )
        args = {"workspace_id": WORKSPACE_ID, "query": "first admitted"}
        first = await dispatcher.submit(
            "memory_recall", args, scope=self.scope, task_metadata={}
        )
        # A retry returns its existing admission even when capacity is full.
        retry = await dispatcher.submit(
            "memory_recall", args, scope=self.scope, task_metadata={}
        )
        self.assertEqual(first.task_id, retry.task_id)
        with self.assertRaisesRegex(TaskDispatcherError, "TASK_QUEUE_FULL"):
            await dispatcher.submit(
                "memory_recall",
                args | {"query": "principal excess"},
                scope=self.scope,
                task_metadata={},
            )
        other = InvocationScope("principal-b", "session-b", str(self.root))
        self.gate.record_briefing(other)
        await dispatcher.submit("memory_recall", args, scope=other, task_metadata={})
        third = InvocationScope("principal-c", "session-c", str(self.root))
        self.gate.record_briefing(third)
        with self.assertRaisesRegex(TaskDispatcherError, "TASK_QUEUE_FULL"):
            await dispatcher.submit(
                "memory_recall", args, scope=third, task_metadata={}
            )
        with closing(sqlite3.connect(self.database)) as connection:
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM durable_tasks").fetchone()[0],
                2,
            )
        self.assertEqual(self.calls, [])

    async def test_revoked_workspace_blocks_retry_results_and_queued_execution(self):
        from daem0nmcp.api.v7.task_dispatcher import TaskDispatcherError

        allowed = True
        self.gate._workspace_authorizer = lambda scope: allowed
        dispatcher = self._dispatcher()
        args = {"workspace_id": WORKSPACE_ID, "query": "revoked admission"}
        admitted = await dispatcher.submit(
            "memory_recall", args, scope=self.scope, task_metadata={}
        )
        allowed = False
        identity = {
            "principal_id": self.scope.principal_id,
            "transport_session_id": self.scope.transport_session_id,
        }
        for operation in (
            dispatcher.get_task,
            dispatcher.get_result,
            dispatcher.cancel,
        ):
            with self.assertRaisesRegex(TaskDispatcherError, "TASK_NOT_FOUND"):
                await operation(admitted.task_id, **identity)
        self.assertEqual((await dispatcher.list_tasks(**identity)).tasks, ())
        with self.assertRaisesRegex(TaskDispatcherError, "UNAUTHORIZED_WORKSPACE"):
            await dispatcher.submit(
                "memory_recall", args, scope=self.scope, task_metadata={}
            )
        await dispatcher.start()
        for _ in range(100):
            if dispatcher._get_unscoped(admitted.task_id).state == "failed":
                break
            await asyncio.sleep(0.05)
        self.assertEqual(dispatcher._get_unscoped(admitted.task_id).state, "failed")
        self.assertEqual(self.calls, [])

    async def _wait_terminal(self, dispatcher, task_id: str):
        for _ in range(100):
            view = await dispatcher.get_task(
                task_id,
                principal_id=self.scope.principal_id,
                transport_session_id=self.scope.transport_session_id,
            )
            if view.status in {"completed", "failed", "cancelled"}:
                return view
            await asyncio.sleep(0.05)
        self.fail("task did not reach a terminal state")

    async def test_restart_duplicate_delivery_and_queue_outage_recover_once(
        self,
    ) -> None:
        unavailable = self._dispatcher(_unavailable_url(self.redis_url))
        await unavailable.start()
        accepted = await unavailable.submit(
            "memory_recall",
            {"workspace_id": WORKSPACE_ID, "query": "recover exactly once"},
            scope=self.scope,
            task_metadata={"ttl": 60_000, "executionTimeout": 30},
        )
        await unavailable.aclose()

        from redis.asyncio import Redis

        client = Redis.from_url(self.redis_url, decode_responses=True)
        try:
            await client.rpush(self.queue_name, accepted.task_id, accepted.task_id)
            queued_values = await client.lrange(self.queue_name, 0, -1)
        finally:
            await client.aclose()
        self.assertTrue(queued_values)
        self.assertEqual(set(queued_values), {accepted.task_id})

        recovered = self._dispatcher()
        await recovered.start()
        terminal = await self._wait_terminal(recovered, accepted.task_id)
        self.assertEqual(terminal.status, "completed")
        result = await recovered.get_result(
            accepted.task_id,
            principal_id=self.scope.principal_id,
            transport_session_id=self.scope.transport_session_id,
        )
        self.assertEqual(result, {"value": "recover exactly once"})
        self.assertEqual(self.calls, ["recall"])

    async def test_credentials_never_enter_database_or_queue_and_access_is_scoped(
        self,
    ) -> None:
        dispatcher = self._dispatcher()
        store_arguments = {
            "workspace_id": WORKSPACE_ID,
            "record_type": "decision",
            "content": "Use the durable dispatcher",
            "idempotency_key": "task-store-0001",
        }
        token = self.gate.issue_preflight(
            self.scope,
            "memory_store",
            store_arguments,
        )
        accepted = await dispatcher.submit(
            "memory_store",
            store_arguments | {"preflight_token": token},
            scope=self.scope,
            task_metadata={"ttl": 60_000, "executionTimeout": 30},
        )

        database_bytes = self.database.read_bytes()
        self.assertNotIn(token.encode("utf-8"), database_bytes)
        password = urlsplit(self.redis_url).password
        self.assertIsNotNone(password)
        self.assertNotIn(str(password).encode("utf-8"), database_bytes)

        with self.assertRaisesRegex(Exception, "TASK_NOT_FOUND"):
            await dispatcher.get_task(
                accepted.task_id,
                principal_id="principal-b",
                transport_session_id="session-b",
            )

        renewed = InvocationScope("principal-a", "session-renewed", str(self.root))
        with self.assertRaisesRegex(Exception, "COMMUNION_REQUIRED"):
            await dispatcher.get_task(
                accepted.task_id,
                principal_id=renewed.principal_id,
                transport_session_id=renewed.transport_session_id,
            )
        self.gate.record_briefing(renewed)
        reconnected = await dispatcher.get_task(
            accepted.task_id,
            principal_id=renewed.principal_id,
            transport_session_id=renewed.transport_session_id,
        )
        self.assertEqual(reconnected.task_id, accepted.task_id)

        await dispatcher.start()
        terminal = await self._wait_terminal(dispatcher, accepted.task_id)
        self.assertEqual(terminal.status, "completed")
        self.assertEqual(self.calls, ["store"])

    async def test_standard_mcp_submit_status_result_list_and_cancel(self) -> None:
        from fastmcp import Client

        from daem0nmcp.api.v7.fastmcp import build_fastmcp_server
        from daem0nmcp.api.v7.middleware import V7InvocationMiddleware
        from daem0nmcp.api.v7.tasks import ForegroundExecutionPolicy

        dispatcher = self._dispatcher()
        middleware = V7InvocationMiddleware(
            gate=self.gate,
            workspace_resolver=self.resolver,
            transport_mode="stdio",
            process_principal=self.scope.principal_id,
            session_id_factory=lambda: "session-a",
        )

        @asynccontextmanager
        async def lifespan(_server):
            await dispatcher.start()
            try:
                yield {}
            finally:
                await dispatcher.aclose()

        server = build_fastmcp_server(
            self.manifest,
            tasks_enabled=True,
            task_dispatcher=dispatcher,
            middleware=(middleware,),
            lifespan=lifespan,
            foreground_policies={
                "memory_recall": ForegroundExecutionPolicy(64 * 1024),
                "memory_store": ForegroundExecutionPolicy(64 * 1024),
            },
        )
        async with Client(server) as client:
            self.gate.record_briefing(
                InvocationScope(
                    self.scope.principal_id,
                    "mcp-session:session-a",
                    str(self.root),
                )
            )
            successful = await client.call_tool(
                "memory_recall",
                {"workspace_id": WORKSPACE_ID, "query": "adapter result"},
                task=True,
                ttl=60_000,
            )
            status = await successful.status()
            self.assertEqual(status.taskId, successful.task_id)
            result = await successful.result()
            self.assertEqual(
                result.structured_content,
                {"value": "adapter result"},
            )
            listed = await client.list_tasks()
            self.assertIn(
                successful.task_id,
                {task["taskId"] for task in listed["tasks"]},
            )

            cancelled = await client.call_tool(
                "memory_recall",
                {"workspace_id": WORKSPACE_ID, "query": "cancel this task"},
                task=True,
                ttl=60_000,
            )
            await asyncio.wait_for(self.cancel_started.wait(), timeout=5)
            await cancelled.cancel()
            cancelled_status = await cancelled.status()
            self.assertEqual(cancelled_status.status, "cancelled")
            self.assertTrue(self.cancel_cleaned.is_set())

    async def test_ambiguous_admission_requires_fresh_authorization(self) -> None:
        from daem0nmcp.api.v7.task_dispatcher import TaskDispatcherError

        dispatcher = self._dispatcher()
        arguments = {
            "workspace_id": WORKSPACE_ID,
            "record_type": "decision",
            "content": "Crash-safe admission",
            "idempotency_key": "ambiguous-admission-0001",
        }
        first_token = self.gate.issue_preflight(
            self.scope,
            "memory_store",
            arguments,
        )
        authorize = self.gate.authorize

        def crash_before_consume(*args, **kwargs):
            if kwargs.get("consume_capability"):
                raise RuntimeError("simulated admission interruption")
            return authorize(*args, **kwargs)

        with (
            patch.object(
                self.gate,
                "authorize",
                side_effect=crash_before_consume,
            ),
            self.assertRaisesRegex(RuntimeError, "admission interruption"),
        ):
            await dispatcher.submit(
                "memory_store",
                arguments | {"preflight_token": first_token},
                scope=self.scope,
                task_metadata={"ttl": 60_000, "executionTimeout": 30},
            )

        with self.assertRaises(TaskDispatcherError):
            await dispatcher.submit(
                "memory_store",
                arguments | {"preflight_token": "invalid-replacement"},
                scope=self.scope,
                task_metadata={"ttl": 60_000, "executionTimeout": 30},
            )

        recovered = self._dispatcher()
        recovered._recover()
        with closing(sqlite3.connect(self.database)) as connection:
            state, message = connection.execute(
                "SELECT state,status_message FROM durable_tasks"
            ).fetchone()
        self.assertEqual(state, "failed")
        self.assertIn("new preflight", message)

        replacement = self.gate.issue_preflight(
            self.scope,
            "memory_store",
            arguments,
        )
        accepted = await recovered.submit(
            "memory_store",
            arguments | {"preflight_token": replacement},
            scope=self.scope,
            task_metadata={"ttl": 60_000, "executionTimeout": 30},
        )
        await recovered.start()
        terminal = await self._wait_terminal(recovered, accepted.task_id)
        self.assertEqual(terminal.status, "completed")
        replay = self.gate.authorize(
            "memory_store",
            arguments | {"preflight_token": replacement},
            self.scope,
            preflight_token=replacement,
            consume_capability=False,
        )
        self.assertIsNotNone(replay)

    async def test_consumed_but_unpublished_admission_is_not_promoted(self) -> None:
        dispatcher = self._dispatcher()
        arguments = {
            "workspace_id": WORKSPACE_ID,
            "record_type": "decision",
            "content": "Fail closed after consumption",
            "idempotency_key": "post-consume-crash-0001",
        }
        token = self.gate.issue_preflight(self.scope, "memory_store", arguments)
        with (
            patch.object(
                dispatcher,
                "_promote_authorizing",
                side_effect=sqlite3.OperationalError("simulated commit outage"),
            ),
            self.assertRaises(sqlite3.OperationalError),
        ):
            await dispatcher.submit(
                "memory_store",
                arguments | {"preflight_token": token},
                scope=self.scope,
                task_metadata={"ttl": 60_000, "executionTimeout": 30},
            )

        recovered = self._dispatcher()
        recovered._recover()
        with closing(sqlite3.connect(self.database)) as connection:
            state = connection.execute("SELECT state FROM durable_tasks").fetchone()[0]
        self.assertEqual(state, "failed")
        self.assertEqual(self.calls, [])

    async def test_restart_preserves_cancellation_and_claim_window(self) -> None:
        dispatcher = self._dispatcher()
        accepted = await dispatcher.submit(
            "memory_recall",
            {"workspace_id": WORKSPACE_ID, "query": "must never start"},
            scope=self.scope,
            task_metadata={"ttl": 60_000, "executionTimeout": 30},
        )
        stored = dispatcher._claim(accepted.task_id)
        self.assertIsNotNone(stored)
        cancelling = asyncio.create_task(
            dispatcher.cancel(
                accepted.task_id,
                principal_id=self.scope.principal_id,
                transport_session_id=self.scope.transport_session_id,
            )
        )
        for _ in range(100):
            if dispatcher._task_state(accepted.task_id) == "cancel_requested":
                break
            await asyncio.sleep(0.001)
        else:
            self.fail("cancellation intent was not persisted")
        await dispatcher._execute(stored)
        cancelled = await asyncio.wait_for(cancelling, timeout=5)
        self.assertEqual(cancelled.status, "cancelled")
        self.assertEqual(self.calls, [])

        with closing(sqlite3.connect(self.database)) as connection:
            connection.execute(
                "UPDATE durable_tasks SET state='cancel_requested' WHERE task_id=?",
                (accepted.task_id,),
            )
            connection.commit()
        recovered = self._dispatcher()
        recovered._recover()
        self.assertEqual(recovered._task_state(accepted.task_id), "cancelled")

    async def test_graceful_close_requeues_active_accepted_work(self) -> None:
        dispatcher = self._dispatcher()
        await dispatcher.start()
        accepted_read = await dispatcher.submit(
            "memory_recall",
            {"workspace_id": WORKSPACE_ID, "query": "shutdown replay read"},
            scope=self.scope,
            task_metadata={"ttl": 60_000, "executionTimeout": 30},
        )
        await asyncio.wait_for(self.shutdown_read_started.wait(), timeout=5)
        await dispatcher.aclose()
        self.assertEqual(dispatcher._task_state(accepted_read.task_id), "queued")

        reopened = self._dispatcher()
        await reopened.start()
        read_terminal = await self._wait_terminal(reopened, accepted_read.task_id)
        self.assertEqual(read_terminal.status, "completed")
        self.assertEqual(self.shutdown_read_attempts, 2)

        store_arguments = {
            "workspace_id": WORKSPACE_ID,
            "record_type": "decision",
            "content": "Replay idempotent work after graceful shutdown",
            "idempotency_key": "shutdown-store-0001",
        }
        token = self.gate.issue_preflight(
            self.scope,
            "memory_store",
            store_arguments,
        )
        accepted_store = await reopened.submit(
            "memory_store",
            store_arguments | {"preflight_token": token},
            scope=self.scope,
            task_metadata={"ttl": 60_000, "executionTimeout": 30},
        )
        await asyncio.wait_for(self.shutdown_store_started.wait(), timeout=5)
        await reopened.aclose()
        self.assertEqual(reopened._task_state(accepted_store.task_id), "queued")

        final = self._dispatcher()
        await final.start()
        store_terminal = await self._wait_terminal(final, accepted_store.task_id)
        self.assertEqual(store_terminal.status, "completed")
        self.assertEqual(self.shutdown_store_attempts, 2)
        await final.aclose()

        dormant = self._dispatcher()
        unsafe = await dormant.submit(
            "memory_recall",
            {"workspace_id": WORKSPACE_ID, "query": "unsafe interruption"},
            scope=self.scope,
            task_metadata={"ttl": 60_000, "executionTimeout": 30},
        )
        with closing(sqlite3.connect(self.database)) as connection:
            connection.execute(
                "UPDATE durable_tasks SET state='running',replay_safe=0 "
                "WHERE task_id=?",
                (unsafe.task_id,),
            )
            connection.execute(
                "DELETE FROM task_outbox WHERE task_id=?",
                (unsafe.task_id,),
            )
            connection.commit()
        dormant._settle_shutdown(unsafe.task_id)
        failed = await dormant.get_task(
            unsafe.task_id,
            principal_id=self.scope.principal_id,
            transport_session_id=self.scope.transport_session_id,
        )
        self.assertEqual(failed.status, "failed")
        self.assertIn("non-replay-safe", failed.status_message)

    async def test_close_between_claim_and_active_registration_never_starts(
        self,
    ) -> None:
        dispatcher = self._dispatcher()
        claimed = asyncio.Event()
        release_execute = asyncio.Event()
        execute = dispatcher._execute

        async def paused_execute(stored) -> None:
            claimed.set()
            await release_execute.wait()
            await execute(stored)

        with patch.object(dispatcher, "_execute", new=paused_execute):
            try:
                await dispatcher.start()
                accepted = await dispatcher.submit(
                    "memory_recall",
                    {
                        "workspace_id": WORKSPACE_ID,
                        "query": "claim shutdown replay",
                    },
                    scope=self.scope,
                    task_metadata={"ttl": 60_000, "executionTimeout": 30},
                )
                await asyncio.wait_for(claimed.wait(), timeout=5)
                close_task = asyncio.create_task(dispatcher.aclose())
                while not dispatcher._stop.is_set():
                    await asyncio.sleep(0)
                await asyncio.sleep(0)
                release_execute.set()
                await asyncio.wait_for(close_task, timeout=5)
            finally:
                release_execute.set()
                await dispatcher.aclose()

        self.assertEqual(self.calls, [])
        self.assertEqual(dispatcher._task_state(accepted.task_id), "queued")
        reopened = self._dispatcher()
        await reopened.start()
        terminal = await self._wait_terminal(reopened, accepted.task_id)
        self.assertEqual(terminal.status, "completed")
        self.assertEqual(self.calls, ["recall"])
        await reopened.aclose()

        unsafe_dispatcher = self._dispatcher()
        unsafe_claimed = asyncio.Event()
        unsafe_release = asyncio.Event()
        unsafe_execute = unsafe_dispatcher._execute

        async def paused_unsafe_execute(stored) -> None:
            unsafe_claimed.set()
            await unsafe_release.wait()
            await unsafe_execute(stored)

        with patch.object(
            unsafe_dispatcher,
            "_execute",
            new=paused_unsafe_execute,
        ):
            try:
                await unsafe_dispatcher.start()
                unsafe = await unsafe_dispatcher.submit(
                    "memory_recall",
                    {
                        "workspace_id": WORKSPACE_ID,
                        "query": "claim shutdown unsafe",
                    },
                    scope=self.scope,
                    task_metadata={"ttl": 60_000, "executionTimeout": 30},
                )
                await asyncio.wait_for(unsafe_claimed.wait(), timeout=5)
                with closing(sqlite3.connect(self.database)) as connection:
                    connection.execute(
                        "UPDATE durable_tasks SET replay_safe=0 WHERE task_id=?",
                        (unsafe.task_id,),
                    )
                    connection.commit()
                unsafe_closing = asyncio.create_task(unsafe_dispatcher.aclose())
                while not unsafe_dispatcher._stop.is_set():
                    await asyncio.sleep(0)
                await asyncio.sleep(0)
                unsafe_release.set()
                await asyncio.wait_for(unsafe_closing, timeout=5)
            finally:
                unsafe_release.set()
                await unsafe_dispatcher.aclose()

        self.assertEqual(self.calls, ["recall"])
        failed = await unsafe_dispatcher.get_task(
            unsafe.task_id,
            principal_id=self.scope.principal_id,
            transport_session_id=self.scope.transport_session_id,
        )
        self.assertEqual(failed.status, "failed")
        self.assertIn("non-replay-safe", failed.status_message)

    async def test_live_reconciliation_recovers_lost_published_wakeup(self) -> None:
        dispatcher = self._dispatcher()
        release_workers = asyncio.Event()
        worker_loop = dispatcher._worker_loop

        async def delayed_worker() -> None:
            await release_workers.wait()
            await worker_loop()

        from redis.asyncio import Redis

        client = Redis.from_url(self.redis_url, decode_responses=True)
        try:
            with patch.object(dispatcher, "_worker_loop", new=delayed_worker):
                await dispatcher.start()
                accepted = await dispatcher.submit(
                    "memory_recall",
                    {
                        "workspace_id": WORKSPACE_ID,
                        "query": "recover lost published wakeup",
                    },
                    scope=self.scope,
                    task_metadata={"ttl": 60_000, "executionTimeout": 30},
                )
                for _ in range(100):
                    with closing(sqlite3.connect(self.database)) as connection:
                        pending = connection.execute(
                            "SELECT COUNT(*) FROM task_outbox WHERE task_id=?",
                            (accepted.task_id,),
                        ).fetchone()[0]
                    if pending == 0 and await client.llen(self.queue_name) == 1:
                        break
                    await asyncio.sleep(0.05)
                else:
                    self.fail("initial wake-up was not acknowledged")

                await client.delete(self.queue_name)
                self.assertEqual(await client.llen(self.queue_name), 0)
                await asyncio.sleep(2.2)
                self.assertEqual(await client.llen(self.queue_name), 1)
                release_workers.set()
                terminal = await self._wait_terminal(
                    dispatcher,
                    accepted.task_id,
                )
                self.assertEqual(terminal.status, "completed")
        finally:
            release_workers.set()
            await client.aclose()

    async def test_transient_database_faults_are_supervised(self) -> None:
        boundaries = (
            "_prune_expired",
            "_next_outbox",
            "_record_publication",
            "_claim",
            "_finish",
        )
        for index, boundary in enumerate(boundaries):
            with self.subTest(boundary=boundary):
                dispatcher = self._dispatcher()
                original = getattr(dispatcher, boundary)
                failed = False

                def fail_once(*args, _original=original, **kwargs):
                    nonlocal failed
                    if not failed:
                        failed = True
                        raise sqlite3.OperationalError("one-shot database fault")
                    return _original(*args, **kwargs)

                with patch.object(dispatcher, boundary, side_effect=fail_once):
                    await dispatcher.start()
                    accepted = await dispatcher.submit(
                        "memory_recall",
                        {
                            "workspace_id": WORKSPACE_ID,
                            "query": f"fault boundary {index}",
                        },
                        scope=self.scope,
                        task_metadata={"ttl": 60_000, "executionTimeout": 30},
                    )
                    terminal = await self._wait_terminal(
                        dispatcher,
                        accepted.task_id,
                    )
                    self.assertEqual(terminal.status, "completed")
                await dispatcher.aclose()
                self.assertTrue(failed)
                self.assertTrue(
                    any(
                        int(status["restarts"]) > 0
                        for status in dispatcher.loop_health.values()
                    )
                    or boundary in {"_claim", "_finish"}
                )

    async def test_listing_scans_hidden_workspaces_beyond_one_thousand(self) -> None:
        dispatcher = self._dispatcher()
        hidden_root = Path(self.temporary.name) / "hidden-workspace"
        hidden_root.mkdir()
        now = 2_000_000
        rows = []
        for index in range(1_105):
            visible = index < 2
            task_id = "tsk_" + f"{index + 1:064x}"
            rows.append(
                (
                    task_id,
                    self.scope.principal_id,
                    WORKSPACE_ID if visible else "ws_hidden00000000000000000000",
                    (
                        self.scope.canonical_workspace
                        if visible
                        else os.path.normcase(str(hidden_root.resolve()))
                    ),
                    "memory_recall",
                    "{}",
                    "0" * 64,
                    f"seed-{index}",
                    "completed",
                    now + index,
                )
            )
        with closing(sqlite3.connect(self.database)) as connection:
            connection.executemany(
                "INSERT INTO durable_tasks("
                "task_id,principal_id,workspace_id,canonical_workspace,tool_name,"
                "arguments_json,arguments_sha256,idempotency_key,state,"
                "created_at_us,updated_at_us,deadline_at_us,ttl_ms,replay_safe) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,? + 1000000,60000,1)",
                [row + (row[-1], row[-1]) for row in rows],
            )
            connection.commit()
        first = await dispatcher.list_tasks(
            principal_id=self.scope.principal_id,
            transport_session_id=self.scope.transport_session_id,
            limit=1,
        )
        self.assertEqual(len(first.tasks), 1)
        self.assertIsNotNone(first.next_cursor)
        second = await dispatcher.list_tasks(
            principal_id=self.scope.principal_id,
            transport_session_id=self.scope.transport_session_id,
            cursor=first.next_cursor,
            limit=2,
        )
        self.assertEqual(len(second.tasks), 1)
        self.assertIsNone(second.next_cursor)

    async def test_default_queue_namespace_is_per_durable_authority(self) -> None:
        from daem0nmcp.api.v7.task_dispatcher import DurableTaskDispatcher

        second_database = Path(self.temporary.name) / "other" / "tasks.sqlite3"
        first = DurableTaskDispatcher(
            database_path=self.database,
            redis_url=self.redis_url,
            manifest=self.manifest,
            covenant_gate=self.gate,
            workspace_resolver=self.resolver,
        )
        second = DurableTaskDispatcher(
            database_path=second_database,
            redis_url=self.redis_url,
            manifest=self.manifest,
            covenant_gate=self.gate,
            workspace_resolver=self.resolver,
        )
        self.dispatchers.extend((first, second))
        self.assertNotEqual(first.queue_name, second.queue_name)
        reopened = DurableTaskDispatcher(
            database_path=self.database,
            redis_url=self.redis_url,
            manifest=self.manifest,
            covenant_gate=self.gate,
            workspace_resolver=self.resolver,
        )
        self.dispatchers.append(reopened)
        self.assertEqual(first.queue_name, reopened.queue_name)
        await first.start()
        await second.start()
        one = await first.submit(
            "memory_recall",
            {"workspace_id": WORKSPACE_ID, "query": "authority one"},
            scope=self.scope,
            task_metadata={"ttl": 60_000, "executionTimeout": 30},
        )
        two = await second.submit(
            "memory_recall",
            {"workspace_id": WORKSPACE_ID, "query": "authority two"},
            scope=self.scope,
            task_metadata={"ttl": 60_000, "executionTimeout": 30},
        )
        self.assertEqual(
            (await self._wait_terminal(first, one.task_id)).status, "completed"
        )
        self.assertEqual(
            (await self._wait_terminal(second, two.task_id)).status, "completed"
        )


if __name__ == "__main__":
    unittest.main()
