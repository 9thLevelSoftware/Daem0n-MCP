from __future__ import annotations

import unittest
from pathlib import Path

from daem0nmcp.api.v7.responses import ResponseFactory
from daem0nmcp.covenant import (
    CapabilityAuthority,
    CovenantGate,
    CovenantStateStore,
    InvocationScope,
)
from daem0nmcp.workspace import Workspace

WORKSPACE_ID = "ws_0123456789abcdef01234567"
RECORD_ID = "mem_" + "a" * 64


class _Resolver:
    def __init__(self, root: Path) -> None:
        self.root = root.resolve()
        self.calls: list[str] = []

    def resolve(self, workspace_id: str) -> Workspace:
        self.calls.append(workspace_id)
        if workspace_id != WORKSPACE_ID:
            raise LookupError("D:/secret workspace")
        return Workspace(workspace_id=WORKSPACE_ID, root=self.root)


class V7ApplicationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        from daem0nmcp.api.v7.policy import V7_COVENANT_POLICY
        from daem0nmcp.api.v7.tools import build_argument_normalizer

        self.root = Path("tests") / "fixture-workspace"

        def clock() -> int:
            return 1_000

        self.gate = CovenantGate(
            state_store=CovenantStateStore(clock=clock),
            authority=CapabilityAuthority(
                secret=b"v" * 32,
                kid="test",
                clock=clock,
            ),
            policy=V7_COVENANT_POLICY,
            argument_normalizer=build_argument_normalizer(),
        )
        self.scope = InvocationScope("principal", "session", str(self.root))
        self.resolver = _Resolver(self.root)

    def _router(self, operations: dict[str, object], scope=None):
        from daem0nmcp.api.v7.application import V7ApplicationDependencies, V7ToolRouter

        selected_scope = self.scope if scope is None else scope
        return V7ToolRouter(
            V7ApplicationDependencies(
                workspace_resolver=self.resolver,
                covenant_gate=self.gate,
                scope_provider=lambda: selected_scope,
                operations=operations,
                response_factory=ResponseFactory(
                    request_id=lambda: "req_application_test"
                ),
            )
        )

    async def test_communion_blocks_before_operation_then_allows_after_brief(
        self,
    ) -> None:
        called: list[str] = []

        async def operation(*, workspace: Workspace, request: object) -> object:
            from daem0nmcp.api.v7.models import Page
            from daem0nmcp.api.v7.resources import RuleView

            called.append(workspace.workspace_id)
            del request
            return Page[RuleView](items=[], next_cursor=None, truncated=False)

        handler = self._router({"rule_list": operation}).handler("rule_list")
        blocked = await handler(workspace_id=WORKSPACE_ID, cursor=None, limit=50)
        self.assertFalse(blocked.ok)
        self.assertEqual(blocked.error.code, "COMMUNION_REQUIRED")
        self.assertEqual(blocked.error.remedy.tool, "session_brief")
        self.assertEqual(called, [])

        self.gate.record_briefing(self.scope)
        allowed = await handler(workspace_id=WORKSPACE_ID, cursor=None, limit=50)
        self.assertTrue(allowed.ok)
        self.assertEqual(called, [WORKSPACE_ID])

    async def test_protected_token_is_consumed_before_operation_and_never_forwarded(
        self,
    ) -> None:
        calls: list[object] = []

        async def operation(*, workspace: Workspace, request: object) -> object:
            from daem0nmcp.api.v7.models import MutationReceipt

            del workspace
            calls.append(request)
            self.assertNotIn("preflight_token", request.model_dump())
            return MutationReceipt(
                operation_id="op_archive_test",
                affected_ids=[request.record_id],
                event_ids=[],
                counts={"changed": 1},
                idempotent_replay=False,
            )

        self.gate.record_briefing(self.scope)
        target = {
            "workspace_id": WORKSPACE_ID,
            "record_id": RECORD_ID,
            "archived": True,
        }
        token = self.gate.issue_preflight(self.scope, "memory_archive_set", target)
        handler = self._router({"memory_archive_set": operation}).handler(
            "memory_archive_set"
        )

        first = await handler(**target, preflight_token=token)
        second = await handler(**target, preflight_token=token)
        self.assertTrue(first.ok)
        self.assertFalse(second.ok)
        self.assertEqual(second.error.code, "TOKEN_REPLAYED")
        self.assertEqual(len(calls), 1)

    async def test_database_in_use_is_retryable_and_points_at_a_fresh_preflight(
        self,
    ) -> None:
        from daem0nmcp.api.v7.models import MutationReceipt

        class BusyError(RuntimeError):
            code = "DATABASE_IN_USE"

        attempts: list[object] = []

        async def operation(*, workspace: Workspace, request: object) -> object:
            del workspace
            attempts.append(request)
            if len(attempts) == 1:
                raise BusyError("database is locked")
            return MutationReceipt(
                operation_id="op_archive_retry",
                affected_ids=[request.record_id],
                event_ids=[],
                counts={"changed": 1},
                idempotent_replay=False,
            )

        self.gate.record_briefing(self.scope)
        target = {
            "workspace_id": WORKSPACE_ID,
            "record_id": RECORD_ID,
            "archived": True,
        }
        handler = self._router({"memory_archive_set": operation}).handler(
            "memory_archive_set"
        )
        token = self.gate.issue_preflight(self.scope, "memory_archive_set", target)

        busy = await handler(**target, preflight_token=token)
        self.assertFalse(busy.ok)
        self.assertEqual(busy.error.code, "DATABASE_IN_USE")
        self.assertTrue(busy.error.retryable)
        self.assertEqual(busy.error.retry_after_ms, 250)
        self.assertEqual(busy.error.remedy.tool, "memory_preflight")
        self.assertEqual(
            busy.error.remedy.arguments["target_arguments"],
            {"record_id": RECORD_ID, "archived": True},
        )
        # The token was spent before the operation ran: a literal retry is
        # refused, and the remedy (a fresh preflight) succeeds.
        replayed = await handler(**target, preflight_token=token)
        self.assertEqual(replayed.error.code, "TOKEN_REPLAYED")
        fresh = self.gate.issue_preflight(self.scope, "memory_archive_set", target)
        retried = await handler(**target, preflight_token=fresh)
        self.assertTrue(retried.ok)
        self.assertEqual(len(attempts), 2)

    async def test_protected_durable_execution_requires_exact_worker_context(
        self,
    ) -> None:
        from daem0nmcp.api.v7.models import MutationReceipt
        from daem0nmcp.api.v7.tasks import (
            DurableTaskExecution,
            durable_task_arguments_sha256,
            durable_task_execution_var,
        )

        calls: list[object] = []

        async def operation(*, workspace: Workspace, request: object) -> object:
            del workspace
            calls.append(request)
            self.assertNotIn("preflight_token", request.model_dump())
            return MutationReceipt(
                operation_id="op_durable_archive_test",
                affected_ids=[request.record_id],
                event_ids=[],
                counts={"changed": 1},
                idempotent_replay=False,
            )

        target = {
            "workspace_id": WORKSPACE_ID,
            "record_id": RECORD_ID,
            "archived": True,
        }
        handler = self._router({"memory_archive_set": operation}).handler(
            "memory_archive_set"
        )

        execution = DurableTaskExecution(
            task_id="tsk_" + "1" * 64,
            tool_name="memory_archive_set",
            workspace_id=WORKSPACE_ID,
            arguments_sha256=durable_task_arguments_sha256(target),
            principal_id="principal",
            transport_session_id="session",
        )
        context = durable_task_execution_var.set(execution)
        try:
            succeeded = await handler(**target)
        finally:
            durable_task_execution_var.reset(context)
        self.assertTrue(succeeded.ok)
        self.assertEqual(len(calls), 1)

        for changed in (
            {"arguments_sha256": "0" * 64},
            {"tool_name": "memory_store"},
            {"workspace_id": "ws_" + "f" * 24},
        ):
            invalid = {
                "task_id": execution.task_id,
                "tool_name": execution.tool_name,
                "workspace_id": execution.workspace_id,
                "arguments_sha256": execution.arguments_sha256,
                "principal_id": execution.principal_id,
                "transport_session_id": execution.transport_session_id,
                **changed,
            }
            context = durable_task_execution_var.set(DurableTaskExecution(**invalid))
            try:
                rejected = await handler(**target)
            finally:
                durable_task_execution_var.reset(context)
            self.assertFalse(rejected.ok)
            self.assertEqual(rejected.error.code, "INVALID_ARGUMENT")
        self.assertEqual(len(calls), 1)

        client_placeholder = await handler(
            **target, preflight_token="durable.task.validation"
        )
        self.assertFalse(client_placeholder.ok)
        self.assertEqual(client_placeholder.error.code, "TOKEN_TAMPERED")
        self.assertEqual(len(calls), 1)

        self.gate._workspace_authorizer = lambda scope: False
        context = durable_task_execution_var.set(execution)
        try:
            revoked = await handler(**target)
        finally:
            durable_task_execution_var.reset(context)
        self.assertFalse(revoked.ok)
        self.assertEqual(revoked.error.code, "UNAUTHORIZED_WORKSPACE")
        self.assertEqual(len(calls), 1)

    async def test_scope_mismatch_and_unavailable_capability_fail_closed(self) -> None:
        called = False

        async def operation(*, workspace: Workspace, request: object) -> object:
            nonlocal called
            called = True
            return request

        wrong_scope = InvocationScope("principal", "session", "tests/other")
        handler = self._router({"rule_list": operation}, wrong_scope).handler(
            "rule_list"
        )
        result = await handler(workspace_id=WORKSPACE_ID, cursor=None, limit=50)
        self.assertFalse(result.ok)
        self.assertEqual(result.error.code, "TOKEN_SCOPE_MISMATCH")
        self.assertFalse(called)

        self.gate.record_briefing(self.scope)
        unavailable = self._router({}).handler("code_search")
        result = await unavailable(
            workspace_id=WORKSPACE_ID,
            query="symbol",
            cursor=None,
            limit=20,
        )
        self.assertFalse(result.ok)
        self.assertEqual(result.error.code, "CAPABILITY_DISABLED")
        self.assertNotIn("fixture-workspace", result.model_dump_json())

    async def test_disabled_capability_never_bypasses_communion(self) -> None:
        handler = self._router({}).handler("code_search")

        unbriefed = await handler(
            workspace_id=WORKSPACE_ID,
            query="symbol",
            cursor=None,
            limit=20,
        )
        self.assertFalse(unbriefed.ok)
        self.assertEqual(unbriefed.error.code, "COMMUNION_REQUIRED")

        self.gate.record_briefing(self.scope)
        briefed = await handler(
            workspace_id=WORKSPACE_ID,
            query="symbol",
            cursor=None,
            limit=20,
        )
        self.assertFalse(briefed.ok)
        self.assertEqual(briefed.error.code, "CAPABILITY_DISABLED")

    async def test_business_output_is_validated_before_success_envelope(self) -> None:
        async def unsafe(*, workspace: Workspace, request: object) -> object:
            del workspace, request
            return {"project_path": r"D:\private\workspace", "items": []}

        self.gate.record_briefing(self.scope)
        handler = self._router({"code_search": unsafe}).handler("code_search")
        result = await handler(
            workspace_id=WORKSPACE_ID,
            query="symbol",
            cursor=None,
            limit=20,
        )
        self.assertFalse(result.ok)
        self.assertEqual(result.error.code, "INTERNAL_ERROR")
        self.assertNotIn("private", result.model_dump_json())

    async def test_disabled_protected_operation_does_not_consume_capability(
        self,
    ) -> None:
        from daem0nmcp.api.v7.models import MutationReceipt

        self.gate.record_briefing(self.scope)
        target = {
            "workspace_id": WORKSPACE_ID,
            "record_id": RECORD_ID,
            "archived": True,
        }
        token = self.gate.issue_preflight(
            self.scope,
            "memory_archive_set",
            target,
        )
        disabled = await self._router({}).handler("memory_archive_set")(
            **target,
            preflight_token=token,
        )
        self.assertFalse(disabled.ok)
        self.assertEqual(disabled.error.code, "CAPABILITY_DISABLED")

        async def operation(*, workspace: Workspace, request: object) -> object:
            del workspace
            return MutationReceipt(
                operation_id="op_disabled_gate_test",
                affected_ids=[request.record_id],
                event_ids=[],
                counts={"changed": 1},
                idempotent_replay=False,
            )

        admitted = await self._router({"memory_archive_set": operation}).handler(
            "memory_archive_set"
        )(
            **target,
            preflight_token=token,
        )
        self.assertTrue(admitted.ok)

    async def test_task_unavailable_is_reported_only_after_policy_admission(
        self,
    ) -> None:
        from daem0nmcp.api.v7.tasks import task_admission_only_var

        called = False

        async def operation(*, workspace: Workspace, request: object) -> object:
            nonlocal called
            del workspace, request
            called = True
            raise AssertionError("task-unavailable fallback executed the mutation")

        target = {
            "workspace_id": WORKSPACE_ID,
            "records": [
                {
                    "record_type": "learning",
                    "content": "bounded batch item",
                    "rationale": None,
                    "context": {},
                    "tags": [],
                    "relative_file_path": None,
                    "happened_at": None,
                    "procedure_steps": [],
                }
            ],
            "idempotency_key": "batch-task-0001",
        }
        handler = self._router({"memory_store_batch": operation}).handler(
            "memory_store_batch"
        )
        admission = task_admission_only_var.set(True)
        try:
            unbriefed = await handler(
                **target,
                preflight_token="cap_not_admitted_0001",
            )
        finally:
            task_admission_only_var.reset(admission)
        self.assertEqual(unbriefed.error.code, "TOKEN_TAMPERED")

        self.gate.record_briefing(self.scope)
        token = self.gate.issue_preflight(self.scope, "memory_store_batch", target)
        admission = task_admission_only_var.set(True)
        try:
            unavailable = await handler(**target, preflight_token=token)
        finally:
            task_admission_only_var.reset(admission)

        self.assertFalse(unavailable.ok)
        self.assertEqual(unavailable.error.code, "TASKS_UNAVAILABLE")
        self.assertEqual(
            unavailable.meta.capability_states[0].remediation,
            "Upgrade to a reviewed task-acceptance framework seam.",
        )
        self.assertFalse(called)
        self.assertIsNone(
            self.gate.authorize(
                "memory_store_batch",
                target,
                self.scope,
                preflight_token=token,
                consume_capability=False,
            )
        )


if __name__ == "__main__":
    unittest.main()
