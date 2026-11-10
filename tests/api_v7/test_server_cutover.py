from __future__ import annotations

import ast
import importlib
import io
import sys
import types
import unittest
from collections.abc import Iterator
from contextlib import contextmanager, redirect_stdout
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]


class _RecordingServer:
    def __init__(self) -> None:
        self.auth = None
        self.run_calls: list[dict[str, object]] = []

    def run(self, **options: object) -> None:
        self.run_calls.append(options)


@contextmanager
def _import_server_with_factory(
    factory: Any,
) -> Iterator[types.ModuleType]:
    """Import the real composition root against an isolated production seam."""

    server_name = "daem0nmcp.server"
    production_name = "daem0nmcp.api.v7.production"
    previous_server = sys.modules.pop(server_name, None)
    previous_production = sys.modules.get(production_name)
    production = types.ModuleType(production_name)
    production.create_v7_server = factory
    sys.modules[production_name] = production
    try:
        yield importlib.import_module(server_name)
    finally:
        sys.modules.pop(server_name, None)
        if previous_server is not None:
            sys.modules[server_name] = previous_server
        if previous_production is None:
            sys.modules.pop(production_name, None)
        else:
            sys.modules[production_name] = previous_production


class V7ServerCutoverTests(unittest.TestCase):
    def test_server_builds_the_default_stdio_root_lazily(self) -> None:
        calls: list[tuple[str, str | None, _RecordingServer]] = []

        def create_v7_server(
            transport_mode: str, *, host: str | None = None
        ) -> _RecordingServer:
            server = _RecordingServer()
            calls.append((transport_mode, host, server))
            return server

        with _import_server_with_factory(create_v7_server) as server_module:
            self.assertEqual(calls, [])
            self.assertIs(server_module.mcp, calls[0][2])
            self.assertEqual(calls[0][:2], ("stdio", None))

            self.assertIs(server_module.mcp, calls[0][2])
            self.assertEqual(len(calls), 1)

            remote = server_module.create_server("streamable-http", host="127.0.0.1")

        self.assertIs(remote, calls[1][2])
        self.assertEqual(calls[1][:2], ("streamable-http", "127.0.0.1"))

    def test_server_rejects_retired_sse_before_composition(self) -> None:
        calls: list[str] = []

        def create_v7_server(
            transport_mode: str, *, host: str | None = None
        ) -> _RecordingServer:
            calls.append(transport_mode)
            return _RecordingServer()

        with (
            _import_server_with_factory(create_v7_server) as server_module,
            self.assertRaisesRegex(ValueError, "stdio or streamable-http"),
        ):
            server_module.create_server("sse")

        self.assertEqual(calls, [])

    def test_main_uses_shared_launcher_without_protocol_stdout(self) -> None:
        calls: list[tuple[str, str | None, _RecordingServer]] = []

        def create_v7_server(
            transport_mode: str, *, host: str | None = None
        ) -> _RecordingServer:
            server = _RecordingServer()
            calls.append((transport_mode, host, server))
            return server

        output = io.StringIO()
        with (
            _import_server_with_factory(create_v7_server) as server_module,
            redirect_stdout(output),
        ):
            server_module.main(
                [
                    "--transport",
                    "streamable-http",
                    "--host",
                    "127.0.0.1",
                    "--port",
                    "9988",
                ]
            )

        self.assertEqual(output.getvalue(), "")
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][:2], ("streamable-http", "127.0.0.1"))
        self.assertEqual(len(calls[0][2].run_calls), 1)
        run_call = dict(calls[0][2].run_calls[0])
        middleware = run_call.pop("middleware")
        self.assertEqual(
            run_call,
            {
                "transport": "streamable-http",
                "host": "127.0.0.1",
                "port": 9988,
                "stateless_http": False,
                "uvicorn_config": {"proxy_headers": False, "forwarded_allow_ips": ""},
            },
        )
        self.assertEqual(len(middleware), 3)
        self.assertEqual(
            middleware[0].cls.__name__,
            "HostPolicyMiddleware",
        )
        self.assertEqual(
            middleware[1].cls.__name__,
            "StrictJsonBodyMiddleware",
        )
        self.assertEqual(
            middleware[2].kwargs["allowed_origins"],
            ("http://127.0.0.1:9988",),
        )

    def test_server_owns_a_fresh_v7_root_without_registry_surgery(self) -> None:
        source = (ROOT / "daem0nmcp" / "server.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        functions = {
            node.name
            for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        }

        self.assertIn("create_server", functions)
        self.assertIn("main", functions)
        self.assertNotIn("from .mcp_instance import mcp", source)
        self.assertNotIn("from daem0nmcp.mcp_instance import mcp", source)
        self.assertNotIn("mcp.remove_tool", source)
        self.assertNotIn("register_ui_resources(mcp)", source)
        self.assertNotIn("_DEPRECATED_TOOLS", source)
        main_source = source[source.index("def main(") :]
        self.assertNotIn('"sse"', main_source)
        self.assertIn("parse_server_options", main_source)
        self.assertIn("run_server", main_source)

    def test_legacy_python_adapters_are_resolved_only_when_accessed(self) -> None:
        def create_v7_server(
            transport_mode: str, *, host: str | None = None
        ) -> _RecordingServer:
            return _RecordingServer()

        legacy_name = "daem0nmcp.tools.memory"
        previous_legacy = sys.modules.pop(legacy_name, None)
        sentinel = object()
        try:
            with _import_server_with_factory(create_v7_server) as server_module:
                self.assertNotIn(legacy_name, sys.modules)
                fake_legacy = types.ModuleType(legacy_name)
                fake_legacy.remember = sentinel
                sys.modules[legacy_name] = fake_legacy

                self.assertIs(server_module.remember, sentinel)
                self.assertIs(server_module.remember, sentinel)
        finally:
            sys.modules.pop(legacy_name, None)
            if previous_legacy is not None:
                sys.modules[legacy_name] = previous_legacy


if __name__ == "__main__":
    unittest.main()
