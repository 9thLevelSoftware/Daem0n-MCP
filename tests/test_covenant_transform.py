"""Middleware tests for scoped Covenant admission and token consumption."""

from __future__ import annotations

import importlib.util
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from daem0nmcp.covenant import (
    CapabilityAuthority,
    CovenantGate,
    CovenantStateStore,
    InvocationScope,
    issue_current_preflight,
)
from daem0nmcp.transforms.covenant import CovenantMiddleware


class FakeContext:
    def __init__(self, name: str, arguments: dict | None = None) -> None:
        self.message = SimpleNamespace(name=name, arguments=arguments or {})


class ScopedCovenantMiddlewareTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.workspace = str(Path(self.temp.name).resolve())
        self.scope = InvocationScope("principal", "session", self.workspace)
        self.gate = CovenantGate(
            state_store=CovenantStateStore(clock=lambda: 1_000),
            authority=CapabilityAuthority(
                secret=b"transform-test-module-migrated-key-32-bytes",
                kid="test",
                clock=lambda: 1_000,
            ),
        )
        self.middleware = CovenantMiddleware(
            gate=self.gate,
            scope_provider=lambda _context, _workspace: self.scope,
            workspace_resolver=lambda _selector: self.workspace,
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    @staticmethod
    def _violation(result: object) -> str | None:
        structured = getattr(result, "structured_content", None)
        if isinstance(structured, dict):
            return structured.get("violation")
        return None

    async def test_briefing_records_only_the_current_scope(self) -> None:
        calls = 0

        async def success(_context: FakeContext) -> dict:
            nonlocal calls
            calls += 1
            return {"status": "ready"}

        blocked = await self.middleware.on_call_tool(
            FakeContext("consult", {"action": "recall", "topic": "auth"}),
            success,
        )
        self.assertEqual("COMMUNION_REQUIRED", self._violation(blocked))
        self.assertEqual(0, calls)

        await self.middleware.on_call_tool(
            FakeContext("commune", {"action": "briefing"}), success
        )
        allowed = await self.middleware.on_call_tool(
            FakeContext("consult", {"action": "recall", "topic": "auth"}),
            success,
        )
        self.assertEqual({"status": "ready"}, allowed)
        self.assertEqual(2, calls)

    async def test_middleware_consumes_exact_token_before_dispatch(self) -> None:
        self.gate.record_briefing(self.scope)
        target_args = {"category": "decision", "content": "bound"}

        async def preflight(_context: FakeContext) -> dict:
            return {
                "preflight_token": issue_current_preflight(
                    "inscribe.remember", target_args
                )
            }

        response = await self.middleware.on_call_tool(
            FakeContext(
                "consult",
                {
                    "action": "preflight",
                    "target_operation": "inscribe.remember",
                    "target_args": target_args,
                },
            ),
            preflight,
        )
        token = response["preflight_token"]

        async def protected(context: FakeContext) -> dict:
            self.assertNotIn("preflight_token", context.message.arguments)
            return {"stored": True}

        allowed = await self.middleware.on_call_tool(
            FakeContext(
                "inscribe",
                {
                    "action": "remember",
                    **target_args,
                    "preflight_token": token,
                },
            ),
            protected,
        )
        self.assertEqual({"stored": True}, allowed)
        replay = await self.middleware.on_call_tool(
            FakeContext(
                "inscribe",
                {
                    "action": "remember",
                    **target_args,
                    "preflight_token": token,
                },
            ),
            protected,
        )
        self.assertEqual("TOKEN_REPLAYED", self._violation(replay))

    @unittest.skipUnless(
        importlib.util.find_spec("fastmcp") is not None,
        "FastMCP dependency unavailable",
    )
    def test_fastmcp_middleware_contract_is_available(self) -> None:
        from fastmcp.server.middleware import Middleware, MiddlewareContext

        self.assertTrue(issubclass(CovenantMiddleware, Middleware))
        self.assertIsNotNone(MiddlewareContext)


if __name__ == "__main__":
    unittest.main()
