"""Transport policy tests through server construction and the HTTP launcher."""

import importlib.util
import logging
import os
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from daem0nmcp.transport_security import TransportSecurityError


class _FakeFastMCP:
    def __init__(self, name, **kwargs):
        self.name = name
        self.auth = kwargs.get("auth")


class _FakeJWTVerifier:
    __module__ = "fastmcp.server.auth.providers.jwt"

    def __init__(self, **kwargs):
        self.kwargs = kwargs


def _fake_module(name: str, **attributes):
    module = types.ModuleType(name)
    for key, value in attributes.items():
        setattr(module, key, value)
    return module


def _load_mcp_instance():
    source = Path(__file__).resolve().parents[1] / "daem0nmcp" / "mcp_instance.py"
    module_name = "daem0nmcp._transport_mcp_instance_test"
    spec = importlib.util.spec_from_file_location(module_name, source)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def _load_server_for_main_test(fake_mcp, *, creation_calls=None):
    def create_v7_server(transport_mode, *, host=None):
        if creation_calls is not None:
            creation_calls.append((transport_mode, host))
        return fake_mcp

    fake_modules = {
        "daem0nmcp.api.v7.production": _fake_module(
            "daem0nmcp.api.v7.production",
            create_v7_server=create_v7_server,
        ),
    }
    originals = {name: sys.modules.get(name) for name in fake_modules}
    sys.modules.update(fake_modules)
    module_name = "daem0nmcp._server_transport_boundary_test"
    try:
        source = Path(__file__).resolve().parents[1] / "daem0nmcp" / "server.py"
        spec = importlib.util.spec_from_file_location(module_name, source)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        spec.loader.exec_module(module)
        return module
    finally:
        for name, original in originals.items():
            if original is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = original


class TransportLaunchBoundaryTests(unittest.TestCase):
    def test_server_import_does_not_eagerly_compose_default_runtime(self):
        creations = []
        fake_mcp = types.SimpleNamespace(
            auth=None,
            run=lambda **kwargs: None,
        )

        _load_server_for_main_test(fake_mcp, creation_calls=creations)

        self.assertEqual(creations, [])

    def test_mcp_instance_attaches_configured_fastmcp_jwt_verifier(self):
        environment = {
            "FASTMCP_SERVER_AUTH": ("fastmcp.server.auth.providers.jwt.JWTVerifier"),
            "FASTMCP_SERVER_AUTH_JWT_JWKS_URI": "https://issuer.example/jwks",
            "FASTMCP_SERVER_AUTH_JWT_ISSUER": "https://issuer.example",
            "FASTMCP_SERVER_AUTH_JWT_AUDIENCE": "daem0nmcp",
        }
        fake_modules = {
            "fastmcp": _fake_module("fastmcp", FastMCP=_FakeFastMCP),
            "fastmcp.server": _fake_module("fastmcp.server"),
            "fastmcp.server.auth": _fake_module("fastmcp.server.auth"),
            "fastmcp.server.auth.providers": _fake_module(
                "fastmcp.server.auth.providers"
            ),
            "fastmcp.server.auth.providers.jwt": _fake_module(
                "fastmcp.server.auth.providers.jwt", JWTVerifier=_FakeJWTVerifier
            ),
            "daem0nmcp.config": _fake_module(
                "daem0nmcp.config", settings=types.SimpleNamespace(log_level="INFO")
            ),
            "daem0nmcp.logging_config": _fake_module(
                "daem0nmcp.logging_config", StructuredFormatter=logging.Formatter
            ),
        }
        originals = {name: sys.modules.get(name) for name in fake_modules}
        sys.modules.update(fake_modules)
        try:
            with patch.dict(os.environ, environment, clear=True):
                module = _load_mcp_instance()
        finally:
            for name, original in originals.items():
                if original is None:
                    sys.modules.pop(name, None)
                else:
                    sys.modules[name] = original

        self.assertIsInstance(module.mcp.auth, _FakeJWTVerifier)
        self.assertEqual(module.mcp.auth.kwargs["audience"], "daem0nmcp")

    def test_http_launcher_rejects_remote_bind_before_run(self):
        import start_server

        calls = []
        fake_server = _fake_module(
            "daem0nmcp.server",
            create_server=lambda mode, host=None: types.SimpleNamespace(
                auth=None,
                run=lambda **kwargs: calls.append(kwargs),
            ),
        )
        argv = ["start_server.py", "--host", "0.0.0.0"]
        with (
            patch.dict(sys.modules, {"daem0nmcp.server": fake_server}),
            patch.object(sys, "argv", argv),
            patch.dict(os.environ, {}, clear=True),
            self.assertRaisesRegex(TransportSecurityError, "REMOTE_BIND_REQUIRES_AUTH"),
        ):
            start_server.main()

        self.assertEqual(calls, [])

    def test_http_launcher_preserves_unauthenticated_loopback(self):
        import start_server

        calls = []
        fake_server = _fake_module(
            "daem0nmcp.server",
            create_server=lambda mode, host=None: types.SimpleNamespace(
                auth=None,
                run=lambda **kwargs: calls.append(kwargs),
            ),
        )
        argv = ["start_server.py", "--host", "127.0.0.1", "--port", "9988"]
        with (
            patch.dict(sys.modules, {"daem0nmcp.server": fake_server}),
            patch.object(sys, "argv", argv),
            patch.dict(os.environ, {}, clear=True),
            patch("builtins.print"),
        ):
            start_server.main()

        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["transport"], "streamable-http")
        self.assertEqual(calls[0]["host"], "127.0.0.1")
        self.assertEqual(calls[0]["port"], 9988)
        self.assertEqual(len(calls[0]["middleware"]), 3)

    def test_server_main_rejects_remote_sse_bind_before_run(self):
        calls = []
        fake_mcp = types.SimpleNamespace(
            auth=None,
            run=lambda **kwargs: calls.append(kwargs),
            add_middleware=lambda middleware: None,
            remove_tool=lambda name: None,
        )
        server = _load_server_for_main_test(fake_mcp)
        argv = [
            "daem0nmcp.server",
            "--transport",
            "streamable-http",
            "--host",
            "0.0.0.0",
        ]

        with (
            patch.object(sys, "argv", argv),
            self.assertRaisesRegex(TransportSecurityError, "REMOTE_BIND_REQUIRES_AUTH"),
        ):
            server.main()

        self.assertEqual(calls, [])


if __name__ == "__main__":
    unittest.main()
