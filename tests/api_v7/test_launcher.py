from __future__ import annotations

import io
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

from pydantic import BaseModel


class _Server:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def run(self, **arguments: object) -> None:
        self.calls.append(arguments)


class LauncherTests(unittest.TestCase):
    def test_parser_defaults_to_stdio_and_removes_sse(self) -> None:
        from daem0nmcp.api.v7.launcher import parse_server_options

        options = parse_server_options([])
        self.assertEqual(options.transport, "stdio")
        self.assertEqual(options.host, "127.0.0.1")
        self.assertEqual(options.port, 8765)
        self.assertEqual(
            parse_server_options(["--transport", "http"]).transport, "streamable-http"
        )
        with self.assertRaises(SystemExit):
            parse_server_options(["--transport", "sse"])

    def test_stdio_is_protocol_clean_and_http_runs_only_canonical_transport(
        self,
    ) -> None:
        from daem0nmcp.api.v7.launcher import ServerOptions, run_server

        stdio = _Server()
        output = io.StringIO()
        with redirect_stdout(output):
            run_server(stdio, ServerOptions("stdio", "127.0.0.1", 8765))
        self.assertEqual(output.getvalue(), "")
        self.assertEqual(
            stdio.calls,
            [{"transport": "stdio", "show_banner": False}],
        )

        security: list[tuple[str, object]] = []
        http = _Server()
        with patch.dict("os.environ", {"FASTMCP_STATELESS_HTTP": "true"}):
            run_server(
                http,
                ServerOptions("streamable-http", "0.0.0.0", 9999),
                validate_security=lambda host, auth: security.append((host, auth)),
                build_origin_middleware=lambda host, port: [
                    ("origin-policy", host, port)
                ],
            )
        self.assertEqual(security, [("0.0.0.0", None)])
        self.assertEqual(
            http.calls,
            [
                {
                    "transport": "streamable-http",
                    "host": "0.0.0.0",
                    "port": 9999,
                    "middleware": [("origin-policy", "0.0.0.0", 9999)],
                    "stateless_http": False,
                    "uvicorn_config": {
                        "proxy_headers": False,
                        "forwarded_allow_ips": "",
                    },
                }
            ],
        )

    def test_stdio_runner_rejects_duplicate_json_before_sdk_dispatch(self) -> None:
        from daem0nmcp.api.v7.launcher import run_strict_stdio

        class Message(BaseModel):
            content: str

        duplicate_rejected = False

        class ParsingServer:
            def run(self, **arguments: object) -> None:
                nonlocal duplicate_rejected
                self.arguments = arguments
                try:
                    Message.model_validate_json('{"content":"first","content":"last"}')
                except ValueError:
                    duplicate_rejected = True

        server = ParsingServer()
        run_strict_stdio(server, message_model=Message)
        self.assertTrue(duplicate_rejected)
        self.assertEqual(
            server.arguments,
            {"transport": "stdio", "show_banner": False},
        )
        self.assertEqual(
            Message.model_validate_json('{"content":"first","content":"last"}').content,
            "last",
        )

    def test_invalid_port_host_and_transport_are_rejected_before_run(self) -> None:
        from daem0nmcp.api.v7.launcher import ServerOptions

        for arguments in (
            ("sse", "127.0.0.1", 8765),
            ("streamable-http", "", 8765),
            ("streamable-http", "127.0.0.1", 0),
            ("streamable-http", "127.0.0.1", 65536),
            ("stdio", "127.0.0.1", True),
        ):
            with self.subTest(arguments=arguments), self.assertRaises(ValueError):
                ServerOptions(*arguments)


if __name__ == "__main__":
    unittest.main()
