from __future__ import annotations

import asyncio
import os
import unittest
from pathlib import Path
from types import SimpleNamespace

from daem0nmcp.covenant import (
    InvocationScope,
    covenant_gate_var,
    invocation_scope_var,
    workspace_resolver_var,
)
from daem0nmcp.workspace import Workspace

WORKSPACE_ID = "ws_0123456789abcdef01234567"
OTHER_WORKSPACE_ID = "ws_fedcba9876543210fedcba98"
ROOT = Path("middleware-fixtures/workspace-one").resolve()
OTHER_ROOT = Path("middleware-fixtures/workspace-two").resolve()


class _Gate:
    def workspace_authorized(self, scope: InvocationScope) -> bool:
        return True

    def __init__(self) -> None:
        from daem0nmcp.covenant import CovenantStateStore

        self.state_store = CovenantStateStore(clock=lambda: 1_000)
        self.authorize_calls = 0

    def authorize(self, *args: object, **kwargs: object) -> None:
        del args, kwargs
        self.authorize_calls += 1
        raise AssertionError("middleware must not perform handler admission")


class _Resolver:
    def __init__(self, *, mismatch: bool = False, fail: bool = False) -> None:
        self.mismatch = mismatch
        self.fail = fail
        self.calls: list[str] = []

    def resolve(self, workspace_id: str) -> Workspace:
        self.calls.append(workspace_id)
        if self.fail:
            raise LookupError(f"unknown workspace under {ROOT}")
        if self.mismatch:
            return Workspace(OTHER_WORKSPACE_ID, OTHER_ROOT)
        if workspace_id == OTHER_WORKSPACE_ID:
            return Workspace(OTHER_WORKSPACE_ID, OTHER_ROOT)
        return Workspace(WORKSPACE_ID, ROOT)


class _PoisonContext:
    """Fails if identity is derived from a forbidden request surface."""

    def __init__(self, message: object, *, session_id: str = "session-one") -> None:
        self.message = message
        self.fastmcp_context = SimpleNamespace(
            request_context=object(),
            session_id=session_id,
        )

    @property
    def headers(self) -> object:
        raise AssertionError("headers are not an invocation identity")

    @property
    def client_ip(self) -> object:
        raise AssertionError("client IP is not an invocation identity")

    @property
    def client_info(self) -> object:
        raise AssertionError("clientInfo is not an invocation identity")


def _tool_context(
    *,
    workspace_id: str = WORKSPACE_ID,
    name: str = "memory_recall",
    session_id: str = "session-one",
    extra_arguments: dict[str, object] | None = None,
) -> _PoisonContext:
    arguments: dict[str, object] = {"workspace_id": workspace_id}
    arguments.update(extra_arguments or {})
    return _PoisonContext(
        SimpleNamespace(name=name, arguments=arguments),
        session_id=session_id,
    )


def _resource_context(
    *,
    workspace_id: str = WORKSPACE_ID,
    suffix: str = "warnings",
    session_id: str = "session-one",
) -> _PoisonContext:
    uri = f"memory://workspaces/{workspace_id}/{suffix}"
    return _PoisonContext(SimpleNamespace(uri=uri), session_id=session_id)


def _access_token(*, subject: str | None = "alice", client_id: str = "client-a"):
    claims = {} if subject is None else {"sub": subject}
    return SimpleNamespace(claims=claims, client_id=client_id)


class StdioInvocationMiddlewareTests(unittest.IsolatedAsyncioTestCase):
    async def test_activity_callback_wraps_only_authorized_workspace_calls(
        self,
    ) -> None:
        from daem0nmcp.api.v7.middleware import V7InvocationMiddleware

        events: list[tuple[str, bool]] = []
        gate = _Gate()
        middleware = V7InvocationMiddleware(
            gate=gate,
            workspace_resolver=_Resolver(),
            transport_mode="stdio",
            process_principal="process:test-server",
            session_id_factory=lambda: "activity-session",
            activity_callback=lambda workspace, active: events.append(
                (workspace.workspace_id, active)
            ),
        )
        await middleware.on_initialize(
            _PoisonContext(SimpleNamespace()), lambda _context: asyncio.sleep(0)
        )

        async def fail(_context: object) -> None:
            raise RuntimeError("expected")

        with self.assertRaisesRegex(RuntimeError, "expected"):
            await middleware.on_call_tool(_tool_context(), fail)
        self.assertEqual(events, [(WORKSPACE_ID, True), (WORKSPACE_ID, False)])

        gate.workspace_authorized = lambda _scope: False  # type: ignore[method-assign]
        await middleware.on_call_tool(
            _tool_context(), lambda _context: asyncio.sleep(0)
        )
        self.assertEqual(events, [(WORKSPACE_ID, True), (WORKSPACE_ID, False)])

    async def test_initialize_issues_server_session_and_installs_process_scope(
        self,
    ) -> None:
        from daem0nmcp.api.v7.middleware import V7InvocationMiddleware

        gate = _Gate()
        resolver = _Resolver()
        middleware = V7InvocationMiddleware(
            gate=gate,
            workspace_resolver=resolver,
            transport_mode="stdio",
            process_principal="process:test-server",
            session_id_factory=lambda: "init-session-123",
        )
        initialized = await middleware.on_initialize(
            _PoisonContext(SimpleNamespace(clientInfo={"name": "untrusted"})),
            lambda _context: asyncio.sleep(0, result="initialized"),
        )
        self.assertEqual(initialized, "initialized")
        self.assertEqual(middleware.stdio_session_id, "init-session-123")

        observed: dict[str, object] = {}

        async def downstream(context: object) -> str:
            observed["scope"] = invocation_scope_var.get()
            observed["gate"] = covenant_gate_var.get()
            observed["resolver"] = workspace_resolver_var.get()
            observed["arguments"] = context.message.arguments
            return "tool-result"

        result = await middleware.on_call_tool(_tool_context(), downstream)

        self.assertEqual(result, "tool-result")
        scope = observed["scope"]
        self.assertIsInstance(scope, InvocationScope)
        assert isinstance(scope, InvocationScope)
        self.assertEqual(scope.principal_id, "process:test-server")
        self.assertEqual(scope.transport_session_id, "mcp-session:init-session-123")
        self.assertEqual(scope.canonical_workspace, os.path.normcase(str(ROOT)))
        self.assertIs(observed["gate"], gate)
        self.assertEqual(observed["resolver"], resolver.resolve)
        self.assertEqual(observed["arguments"], {"workspace_id": WORKSPACE_ID})
        self.assertEqual(gate.authorize_calls, 0)
        self.assertIsNone(invocation_scope_var.get())
        self.assertIsNone(covenant_gate_var.get())
        self.assertIsNone(workspace_resolver_var.get())

    async def test_stdio_call_before_initialize_dispatches_identity_failure(
        self,
    ) -> None:
        from daem0nmcp.api.v7.middleware import V7InvocationMiddleware

        called = False

        async def downstream(_context: object) -> object:
            nonlocal called
            called = True
            return invocation_scope_var.get()

        middleware = V7InvocationMiddleware(
            gate=_Gate(),
            workspace_resolver=_Resolver(),
            transport_mode="stdio",
            process_principal="process:test-server",
            session_id_factory=lambda: "unused",
        )
        result = await middleware.on_call_tool(_tool_context(), downstream)

        self.assertTrue(called)
        self.assertIsNone(result)

    async def test_downstream_failure_restores_preexisting_context(self) -> None:
        from daem0nmcp.api.v7.middleware import V7InvocationMiddleware

        gate = _Gate()
        resolver = _Resolver()
        middleware = V7InvocationMiddleware(
            gate=gate,
            workspace_resolver=resolver,
            transport_mode="stdio",
            process_principal="process:test-server",
            session_id_factory=lambda: "init-session-123",
        )
        await middleware.on_initialize(
            _PoisonContext(SimpleNamespace()),
            lambda _context: asyncio.sleep(0),
        )

        previous_scope = InvocationScope("outer", "outer-session", OTHER_ROOT)
        previous_gate = _Gate()

        def previous_resolver(_selector: str | None) -> Workspace:
            return Workspace(OTHER_WORKSPACE_ID, OTHER_ROOT)

        scope_token = invocation_scope_var.set(previous_scope)
        gate_token = covenant_gate_var.set(previous_gate)
        resolver_token = workspace_resolver_var.set(previous_resolver)
        try:

            async def downstream(_context: object) -> None:
                raise RuntimeError("downstream failure")

            with self.assertRaisesRegex(RuntimeError, "downstream failure"):
                await middleware.on_call_tool(_tool_context(), downstream)
            self.assertIs(invocation_scope_var.get(), previous_scope)
            self.assertIs(covenant_gate_var.get(), previous_gate)
            self.assertIs(workspace_resolver_var.get(), previous_resolver)
        finally:
            workspace_resolver_var.reset(resolver_token)
            covenant_gate_var.reset(gate_token)
            invocation_scope_var.reset(scope_token)


class RemoteInvocationMiddlewareTests(unittest.IsolatedAsyncioTestCase):
    async def test_explicit_loopback_profile_uses_process_and_mcp_session(self) -> None:
        from daem0nmcp.api.v7.middleware import V7InvocationMiddleware

        middleware = V7InvocationMiddleware(
            gate=_Gate(),
            workspace_resolver=_Resolver(),
            transport_mode="streamable-http",
            access_token_provider=lambda: None,
            process_principal="process:loopback-server",
            allow_unauthenticated_loopback=True,
        )
        scope = await middleware.on_call_tool(
            _tool_context(session_id="loopback-session"),
            lambda _context: asyncio.sleep(0, result=invocation_scope_var.get()),
        )

        self.assertIsInstance(scope, InvocationScope)
        assert isinstance(scope, InvocationScope)
        self.assertEqual(scope.principal_id, "process:loopback-server")
        self.assertEqual(
            scope.transport_session_id,
            "mcp-session:loopback-session",
        )

    async def test_remote_identity_uses_oauth_subject_and_mcp_session_only(
        self,
    ) -> None:
        from daem0nmcp.api.v7.middleware import V7InvocationMiddleware

        gate = _Gate()
        resolver = _Resolver()
        middleware = V7InvocationMiddleware(
            gate=gate,
            workspace_resolver=resolver,
            transport_mode="streamable-http",
            access_token_provider=lambda: _access_token(subject="alice"),
        )
        context = _tool_context(
            session_id="mcp-session-from-protocol",
            extra_arguments={
                "_client_meta": {
                    "principal": "forged",
                    "session": "forged-session",
                }
            },
        )

        async def downstream(received_context: object) -> InvocationScope | None:
            self.assertIn("_client_meta", received_context.message.arguments)
            return invocation_scope_var.get()

        scope = await middleware.on_call_tool(context, downstream)

        self.assertIsInstance(scope, InvocationScope)
        assert isinstance(scope, InvocationScope)
        self.assertEqual(scope.principal_id, "oauth-sub:alice")
        self.assertEqual(
            scope.transport_session_id,
            "mcp-session:mcp-session-from-protocol",
        )
        self.assertEqual(scope.canonical_workspace, os.path.normcase(str(ROOT)))
        self.assertEqual(gate.authorize_calls, 0)

    async def test_remote_identity_preserves_exact_subject(self) -> None:
        from daem0nmcp.api.v7.middleware import V7InvocationMiddleware

        principals = []
        for subject in ("alice", " alice", "alice ", "Alice"):
            middleware = V7InvocationMiddleware(
                gate=_Gate(),
                workspace_resolver=_Resolver(),
                transport_mode="streamable-http",
                access_token_provider=lambda subject=subject: _access_token(
                    subject=subject
                ),
            )
            principal, _ = await middleware._remote_identity(_tool_context())
            self.assertEqual(principal, "oauth-sub:" + subject)
            principals.append(principal)
        self.assertEqual(len(set(principals)), 4)

    async def test_remote_identity_requires_an_authenticated_subject(self) -> None:
        from daem0nmcp.api.v7.middleware import V7InvocationMiddleware

        middleware = V7InvocationMiddleware(
            gate=_Gate(),
            workspace_resolver=_Resolver(),
            transport_mode="streamable-http",
            access_token_provider=lambda: _access_token(
                subject=None,
                client_id="service-client",
            ),
        )
        scope = await middleware.on_call_tool(
            _tool_context(),
            lambda _context: asyncio.sleep(0, result=invocation_scope_var.get()),
        )

        self.assertIsNone(scope)

    async def test_missing_identity_unknown_workspace_and_mismatch_are_indistinguishable(
        self,
    ) -> None:
        from daem0nmcp.api.v7.middleware import V7InvocationMiddleware

        cases = (
            V7InvocationMiddleware(
                gate=_Gate(),
                workspace_resolver=_Resolver(),
                transport_mode="streamable-http",
                access_token_provider=lambda: _access_token(
                    subject=None,
                    client_id="",
                ),
            ),
            V7InvocationMiddleware(
                gate=_Gate(),
                workspace_resolver=_Resolver(fail=True),
                transport_mode="streamable-http",
                access_token_provider=lambda: _access_token(),
            ),
            V7InvocationMiddleware(
                gate=_Gate(),
                workspace_resolver=_Resolver(mismatch=True),
                transport_mode="streamable-http",
                access_token_provider=lambda: _access_token(),
            ),
        )
        results: list[object] = []
        for middleware in cases:
            with self.subTest(middleware=middleware):
                result = await middleware.on_call_tool(
                    _tool_context(),
                    lambda _context: asyncio.sleep(
                        0,
                        result=invocation_scope_var.get(),
                    ),
                )
                results.append(result)

        self.assertEqual([None, None, None], results)

    async def test_system_health_without_workspace_runs_without_inventing_scope(
        self,
    ) -> None:
        from daem0nmcp.api.v7.middleware import V7InvocationMiddleware

        gate = _Gate()
        resolver = _Resolver()
        middleware = V7InvocationMiddleware(
            gate=gate,
            workspace_resolver=resolver,
            transport_mode="streamable-http",
            access_token_provider=lambda: _access_token(),
        )
        context = _PoisonContext(SimpleNamespace(name="system_health", arguments={}))

        async def downstream(_context: object) -> tuple[object, object, object]:
            return (
                invocation_scope_var.get(),
                covenant_gate_var.get(),
                workspace_resolver_var.get(),
            )

        scope, installed_gate, installed_resolver = await middleware.on_call_tool(
            context,
            downstream,
        )

        self.assertIsNone(scope)
        self.assertIs(installed_gate, gate)
        self.assertEqual(installed_resolver, resolver.resolve)
        self.assertEqual(resolver.calls, [])
        self.assertIsNone(covenant_gate_var.get())
        self.assertIsNone(workspace_resolver_var.get())

    async def test_concurrent_workspaces_do_not_cross_contaminate_context(self) -> None:
        from daem0nmcp.api.v7.middleware import V7InvocationMiddleware

        middleware = V7InvocationMiddleware(
            gate=_Gate(),
            workspace_resolver=_Resolver(),
            transport_mode="streamable-http",
            access_token_provider=lambda: _access_token(),
        )
        both_entered = asyncio.Event()
        entered = 0
        lock = asyncio.Lock()

        async def downstream(_context: object) -> str:
            nonlocal entered
            before = invocation_scope_var.get()
            assert isinstance(before, InvocationScope)
            async with lock:
                entered += 1
                if entered == 2:
                    both_entered.set()
            await both_entered.wait()
            await asyncio.sleep(0)
            after = invocation_scope_var.get()
            self.assertIs(after, before)
            return before.canonical_workspace

        first, second = await asyncio.gather(
            middleware.on_call_tool(
                _tool_context(workspace_id=WORKSPACE_ID, session_id="s-one"),
                downstream,
            ),
            middleware.on_call_tool(
                _tool_context(workspace_id=OTHER_WORKSPACE_ID, session_id="s-two"),
                downstream,
            ),
        )

        self.assertEqual(
            {first, second},
            {os.path.normcase(str(ROOT)), os.path.normcase(str(OTHER_ROOT))},
        )
        self.assertIsNone(invocation_scope_var.get())
        self.assertIsNone(covenant_gate_var.get())
        self.assertIsNone(workspace_resolver_var.get())


class ResourceInvocationMiddlewareTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        from daem0nmcp.api.v7.middleware import V7InvocationMiddleware

        self.gate = _Gate()
        self.resolver = _Resolver()
        self.middleware = V7InvocationMiddleware(
            gate=self.gate,
            workspace_resolver=self.resolver,
            transport_mode="streamable-http",
            access_token_provider=lambda: _access_token(),
        )

    async def test_resource_hook_installs_exact_scope_for_communion_authorizer(
        self,
    ) -> None:
        from daem0nmcp.api.v7.middleware import ResourceCommunionAuthorizer

        expected_scope = InvocationScope(
            "oauth-sub:alice",
            "mcp-session:session-one",
            ROOT,
        )
        self.gate.state_store.mark_briefed(expected_scope)
        authorizer = ResourceCommunionAuthorizer(expected_gate=self.gate)

        async def downstream(context: object) -> str:
            workspace = self.resolver.resolve(WORKSPACE_ID)
            authorizer.authorize(
                workspace=workspace,
                resource_uri=context.message.uri,
            )
            self.assertEqual(invocation_scope_var.get(), expected_scope)
            self.assertIs(covenant_gate_var.get(), self.gate)
            self.assertEqual(workspace_resolver_var.get(), self.resolver.resolve)
            return "resource-result"

        result = await self.middleware.on_read_resource(
            _resource_context(),
            downstream,
        )

        self.assertEqual(result, "resource-result")
        self.assertIsNone(invocation_scope_var.get())
        self.assertIsNone(covenant_gate_var.get())
        self.assertIsNone(workspace_resolver_var.get())

    async def test_resource_authorizer_masks_unbriefed_and_scope_mismatch(self) -> None:
        from daem0nmcp.api.v7.middleware import (
            ResourceAuthorizationError,
            ResourceCommunionAuthorizer,
        )

        authorizer = ResourceCommunionAuthorizer(expected_gate=self.gate)
        errors: list[ResourceAuthorizationError] = []

        async def unbriefed(context: object) -> None:
            with self.assertRaises(ResourceAuthorizationError) as caught:
                authorizer.authorize(
                    workspace=self.resolver.resolve(WORKSPACE_ID),
                    resource_uri=context.message.uri,
                )
            errors.append(caught.exception)

        await self.middleware.on_read_resource(_resource_context(), unbriefed)

        expected_scope = InvocationScope(
            "oauth-sub:alice",
            "mcp-session:session-one",
            ROOT,
        )
        self.gate.state_store.mark_briefed(expected_scope)

        async def mismatch(context: object) -> None:
            with self.assertRaises(ResourceAuthorizationError) as caught:
                authorizer.authorize(
                    workspace=Workspace(OTHER_WORKSPACE_ID, OTHER_ROOT),
                    resource_uri=context.message.uri,
                )
            errors.append(caught.exception)

        await self.middleware.on_read_resource(_resource_context(), mismatch)

        self.assertEqual(
            {(type(error), error.args, str(error)) for error in errors},
            {
                (
                    ResourceAuthorizationError,
                    ("Resource unavailable",),
                    "Resource unavailable",
                )
            },
        )
        for error in errors:
            self.assertIsNone(error.__cause__)
            self.assertIsNone(error.__context__)
        self.assertNotIn(str(ROOT), " ".join(repr(error) for error in errors))

    async def test_malformed_unknown_and_mismatched_resource_ids_fail_before_dispatch(
        self,
    ) -> None:
        from daem0nmcp.api.v7.middleware import (
            ResourceInvocationContextError,
        )

        malformed = _PoisonContext(
            SimpleNamespace(uri="memory://workspaces/not-an-id/warnings")
        )
        mismatch_middleware = type(self.middleware)(
            gate=self.gate,
            workspace_resolver=_Resolver(mismatch=True),
            transport_mode="streamable-http",
            access_token_provider=lambda: _access_token(),
        )
        failures = (
            (self.middleware, malformed),
            (mismatch_middleware, _resource_context()),
        )
        called = False

        async def downstream(_context: object) -> None:
            nonlocal called
            called = True

        errors: list[ResourceInvocationContextError] = []
        for middleware, context in failures:
            with self.assertRaises(ResourceInvocationContextError) as caught:
                await middleware.on_read_resource(context, downstream)
            errors.append(caught.exception)

        self.assertFalse(called)
        self.assertEqual(
            {(type(error), error.args, str(error)) for error in errors},
            {
                (
                    ResourceInvocationContextError,
                    ("Resource unavailable",),
                    "Resource unavailable",
                )
            },
        )
        for error in errors:
            self.assertIsNone(error.__cause__)
            self.assertIsNone(error.__context__)


if __name__ == "__main__":
    unittest.main()
