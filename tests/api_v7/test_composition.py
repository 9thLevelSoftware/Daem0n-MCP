from __future__ import annotations

import unittest
from datetime import datetime, timezone
from pathlib import Path

from daem0nmcp.api.v7.responses import ResponseFactory
from daem0nmcp.covenant import (
    CapabilityAuthority,
    CovenantGate,
    CovenantStateStore,
)
from daem0nmcp.workspace import Workspace

WORKSPACE = Workspace(
    workspace_id="ws_0123456789abcdef01234567",
    root=Path("tests/composition-workspace").resolve(),
)


class _Resolver:
    def resolve(self, workspace_id: str) -> Workspace:
        if workspace_id != WORKSPACE.workspace_id:
            raise LookupError("unavailable")
        return WORKSPACE


class _Service:
    def __getattr__(self, name: str):
        def operation(*args: object, **kwargs: object) -> object:
            del args, kwargs
            raise AssertionError(f"unexpected service call: {name}")

        return operation


class _FakeFastMCP:
    def __init__(self, name: str, **options: object) -> None:
        self.name = name
        self.options = options
        self.middlewares: list[object] = []
        self.tools: list[tuple[dict[str, object], object]] = []
        self.resources: list[tuple[tuple[object, ...], dict[str, object], object]] = []

    def add_middleware(self, middleware: object) -> None:
        self.middlewares.append(middleware)

    def tool(self, **metadata: object):
        def register(handler: object) -> object:
            self.tools.append((metadata, handler))
            return handler

        return register

    def resource(self, *arguments: object, **metadata: object):
        def register(handler: object) -> object:
            self.resources.append((arguments, metadata, handler))
            return handler

        return register


class V7CompositionTests(unittest.TestCase):
    def _dependencies(self):
        from daem0nmcp.api.v7.pinned import PinnedDependencies
        from daem0nmcp.api.v7.policy import V7_COVENANT_POLICY
        from daem0nmcp.api.v7.tools import build_argument_normalizer

        resolver = _Resolver()
        gate = CovenantGate(
            state_store=CovenantStateStore(clock=lambda: 1_000),
            authority=CapabilityAuthority(
                secret=b"c" * 32,
                kid="composition",
                clock=lambda: 1_000,
            ),
            policy=V7_COVENANT_POLICY,
            argument_normalizer=build_argument_normalizer(),
        )
        dependencies = PinnedDependencies(
            workspace_resolver=resolver,
            covenant_gate=gate,
            argument_normalizer=build_argument_normalizer(),
            briefing_service=_Service(),
            preflight_service=_Service(),
            recall_service=_Service(),
            memory_event_writer=_Service(),
            health_service=_Service(),
            response_factory=ResponseFactory(request_id=lambda: "req_composition_test"),
            scope_provider=lambda: None,
            clock=lambda: datetime(2026, 8, 8, tzinfo=timezone.utc),
        )
        return dependencies, resolver, gate

    def test_surface_has_one_gate_resolver_and_exact_manifest(self) -> None:
        from daem0nmcp.api.v7.composition import build_v7_surface
        from daem0nmcp.api.v7.policy import V7_TOOL_LEVELS
        from daem0nmcp.api.v7.registry import (
            FULL_V7_RESOURCE_URI_TEMPLATES,
            PINNED_TOOL_NAMES,
        )

        pinned, resolver, gate = self._dependencies()
        surface = build_v7_surface(
            pinned_dependencies=pinned,
            operations={},
            warning_reader=lambda workspace, request: (),
            failure_reader=lambda workspace, request: (),
            rule_reader=lambda workspace, request: (),
            active_context_reader=lambda workspace, request: (),
            transport_mode="stdio",
            process_principal="process:test",
            session_id_factory=lambda: "session-test",
        )

        self.assertIs(surface.gate, gate)
        self.assertIs(surface.workspace_resolver, resolver)
        self.assertEqual(set(surface.handlers), set(V7_TOOL_LEVELS))
        self.assertEqual(
            {name for name in surface.handlers if name in PINNED_TOOL_NAMES},
            set(PINNED_TOOL_NAMES),
        )
        self.assertEqual(
            {resource.uri_template for resource in surface.manifest.resources},
            set(FULL_V7_RESOURCE_URI_TEMPLATES),
        )
        self.assertEqual(len(surface.middleware), 1)

    def test_surface_rejects_pinned_operation_bypass_and_builds_fresh_server(
        self,
    ) -> None:
        from daem0nmcp.api.v7.composition import build_v7_surface

        pinned, _, _ = self._dependencies()
        readers = {
            "warning_reader": lambda workspace, request: (),
            "failure_reader": lambda workspace, request: (),
            "rule_reader": lambda workspace, request: (),
            "active_context_reader": lambda workspace, request: (),
        }
        with self.assertRaisesRegex(ValueError, "pinned"):
            build_v7_surface(
                pinned_dependencies=pinned,
                operations={"memory_store": lambda **kwargs: None},
                transport_mode="stdio",
                **readers,
            )

        surface = build_v7_surface(
            pinned_dependencies=pinned,
            operations={},
            transport_mode="streamable-http",
            access_token_provider=lambda: object(),
            **readers,
        )
        first = surface.build_server(
            fastmcp_cls=_FakeFastMCP,
            distribution_version="3.4.7",
        )
        second = surface.build_server(
            fastmcp_cls=_FakeFastMCP,
            distribution_version="3.4.7",
        )
        self.assertIsNot(first, second)
        self.assertEqual(len(first.middlewares), 1)
        self.assertEqual(len(first.tools), 75)
        self.assertEqual(len(first.resources), 10)


if __name__ == "__main__":
    unittest.main()
