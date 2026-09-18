"""Security regression tests for HTTP listener configuration."""

import asyncio
import json
import unittest

from pydantic import BaseModel

from daem0nmcp.transport_security import (
    OriginPolicyMiddleware,
    StrictJsonBodyMiddleware,
    TransportSecurityError,
    build_http_origin_middleware,
    strict_stdio_json_boundary,
    validate_transport_security,
)


class TransportSecurityTests(unittest.TestCase):
    def test_loopback_hosts_do_not_require_authentication(self):
        for host in ("127.0.0.1", "::1", "[::1]", "localhost", "LOCALHOST"):
            with self.subTest(host=host):
                validate_transport_security(host, environ={})

    def test_remote_hosts_require_production_authentication(self):
        for host in ("0.0.0.0", "::", "[::]", "203.0.113.10", "mcp.example.com"):
            with (
                self.subTest(host=host),
                self.assertRaisesRegex(
                    TransportSecurityError, "REMOTE_BIND_REQUIRES_AUTH"
                ),
            ):
                validate_transport_security(host, environ={})

    def test_unrelated_token_does_not_authorize_remote_bind(self):
        with self.assertRaisesRegex(
            TransportSecurityError, "REMOTE_BIND_REQUIRES_AUTH"
        ):
            validate_transport_security(
                "0.0.0.0", environ={"DAEM0NMCP_TOKEN": "development-secret"}
            )

    def test_invalid_and_development_verifiers_do_not_authorize_remote_bind(self):
        invalid_environments = (
            {"FASTMCP_SERVER_AUTH": "not.a.real.Provider"},
            {
                "FASTMCP_SERVER_AUTH": (
                    "fastmcp.server.auth.providers.debug.DebugTokenVerifier"
                )
            },
            {
                "FASTMCP_SERVER_AUTH": (
                    "fastmcp.server.auth.providers.jwt.StaticTokenVerifier"
                ),
                "FASTMCP_SERVER_AUTH_STATIC_TOKENS": "dev-token",
            },
            {
                "FASTMCP_SERVER_AUTH": (
                    "fastmcp.server.auth.providers.jwt.JWTVerifier"
                ),
                "FASTMCP_SERVER_AUTH_JWT_JWKS_URI": "https://issuer.example/jwks",
            },
        )

        for environ in invalid_environments:
            with (
                self.subTest(environ=environ),
                self.assertRaises(TransportSecurityError),
            ):
                validate_transport_security("mcp.example.com", environ=environ)

    def test_complete_jwt_verifier_configuration_authorizes_remote_bind(self):
        validate_transport_security(
            "0.0.0.0",
            environ={
                "FASTMCP_SERVER_AUTH": (
                    "fastmcp.server.auth.providers.jwt.JWTVerifier"
                ),
                "FASTMCP_SERVER_AUTH_JWT_JWKS_URI": "https://issuer.example/jwks",
                "FASTMCP_SERVER_AUTH_JWT_ISSUER": "https://issuer.example",
                "FASTMCP_SERVER_AUTH_JWT_AUDIENCE": "daem0nmcp",
            },
        )

    def test_remote_browser_origins_are_default_deny_and_explicitly_allowlisted(self):
        middleware = build_http_origin_middleware(
            "0.0.0.0",
            9876,
            environ={},
        )
        self.assertEqual(len(middleware), 1)
        self.assertEqual(middleware[0].kwargs["allowed_origins"], ())

        configured = build_http_origin_middleware(
            "0.0.0.0",
            9876,
            environ={
                "DAEM0NMCP_ALLOWED_ORIGINS": (
                    "https://mcp.example.com,https://console.example.com"
                )
            },
        )
        self.assertEqual(
            configured[0].kwargs["allowed_origins"],
            ("https://console.example.com", "https://mcp.example.com"),
        )

    def test_origin_middleware_rejects_unknown_and_serves_exact_preflight(self):
        downstream_calls: list[object] = []

        async def downstream(scope, receive, send):
            downstream_calls.append(scope)
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b"ok"})

        middleware = OriginPolicyMiddleware(
            downstream,
            allowed_origins=("https://console.example.com",),
        )

        async def request(origin: str, method: str = "POST"):
            sent: list[dict[str, object]] = []
            headers = [(b"origin", origin.encode("ascii"))]
            if method == "OPTIONS":
                headers.append((b"access-control-request-method", b"POST"))

            async def send(message):
                sent.append(message)

            await middleware(
                {"type": "http", "method": method, "headers": headers},
                lambda: None,
                send,
            )
            return sent

        rejected = asyncio.run(request("https://evil.example"))
        self.assertEqual(rejected[0]["status"], 403)
        self.assertEqual(downstream_calls, [])

        preflight = asyncio.run(request("https://console.example.com", "OPTIONS"))
        self.assertEqual(preflight[0]["status"], 204)
        headers = dict(preflight[0]["headers"])
        self.assertEqual(
            headers[b"access-control-allow-origin"],
            b"https://console.example.com",
        )
        self.assertEqual(downstream_calls, [])

    def test_origin_configuration_rejects_wildcards_and_non_origins(self):
        for value in (
            "*",
            "https://example.com/path",
            "https://user@example.com",
            "file:///tmp/source",
            "https://example.com,",
            "https://exa\nmple.com",
            "https://exa\rmple.com",
            "https://exa\tmple.com",
        ):
            with self.subTest(value=value), self.assertRaises(TransportSecurityError):
                build_http_origin_middleware(
                    "0.0.0.0",
                    9876,
                    environ={"DAEM0NMCP_ALLOWED_ORIGINS": value},
                )

    def test_http_json_boundary_rejects_duplicate_keys_before_dispatch(self):
        downstream_bodies: list[bytes] = []

        async def downstream(_scope, receive, send):
            message = await receive()
            downstream_bodies.append(message["body"])
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b"ok"})

        middleware = StrictJsonBodyMiddleware(downstream)

        async def request(body: bytes):
            sent: list[dict[str, object]] = []
            messages = iter(
                [
                    {
                        "type": "http.request",
                        "body": body,
                        "more_body": False,
                    }
                ]
            )

            async def receive():
                return next(messages)

            async def send(message):
                sent.append(message)

            await middleware(
                {
                    "type": "http",
                    "method": "POST",
                    "headers": [(b"content-type", b"application/json")],
                },
                receive,
                send,
            )
            return sent

        duplicate = (
            b'{"jsonrpc":"2.0","method":"tools/call","params":'
            b'{"name":"memory_store","arguments":{"content":"first",'
            b'"content":"last"}}}'
        )
        rejected = asyncio.run(request(duplicate))
        self.assertEqual(rejected[0]["status"], 400)
        self.assertEqual(downstream_bodies, [])

        unique = b'{"jsonrpc":"2.0","method":"tools/list","id":1}'
        accepted = asyncio.run(request(unique))
        self.assertEqual(accepted[0]["status"], 200)
        self.assertEqual(downstream_bodies, [unique])

    def test_http_json_boundary_rejects_excessive_nesting(self):
        async def downstream(_scope, _receive, _send):
            raise AssertionError("malformed JSON reached MCP dispatch")

        middleware = StrictJsonBodyMiddleware(downstream)

        async def request():
            sent: list[dict[str, object]] = []
            delivered = False

            async def receive():
                nonlocal delivered
                if delivered:
                    return {"type": "http.disconnect"}
                delivered = True
                return {
                    "type": "http.request",
                    "body": (b"[" * 2_000) + (b"]" * 2_000),
                    "more_body": False,
                }

            async def send(message):
                sent.append(message)

            await middleware(
                {"type": "http", "method": "POST", "headers": []},
                receive,
                send,
            )
            return sent

        rejected = asyncio.run(request())
        self.assertEqual(rejected[0]["status"], 400)

    def test_stdio_json_boundary_rejects_duplicate_keys_and_restores_model(self):
        class Message(BaseModel):
            content: str

        duplicate = '{"content":"first","content":"last"}'
        self.assertEqual(Message.model_validate_json(duplicate).content, "last")
        with strict_stdio_json_boundary(Message):
            with self.assertRaises(ValueError):
                Message.model_validate_json(duplicate)
            self.assertEqual(
                Message.model_validate_json('{"content":"only"}').content,
                "only",
            )
            with self.assertRaises(ValueError):
                Message.model_validate_json(json.dumps([[[float("nan")]]]))
        self.assertEqual(Message.model_validate_json(duplicate).content, "last")


if __name__ == "__main__":
    unittest.main()
