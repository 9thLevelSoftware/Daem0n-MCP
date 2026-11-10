"""Protocol-clean stdio and Streamable HTTP launch contract for v7."""

from __future__ import annotations

import argparse
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any, Literal

Transport = Literal["stdio", "streamable-http"]


@dataclass(frozen=True, slots=True)
class ServerOptions:
    transport: Transport
    host: str
    port: int

    def __post_init__(self) -> None:
        if self.transport not in {"stdio", "streamable-http"}:
            raise ValueError("transport must be stdio or streamable-http")
        if not isinstance(self.host, str) or not self.host.strip():
            raise ValueError("host must be non-empty")
        if (
            isinstance(self.port, bool)
            or not isinstance(self.port, int)
            or not 1 <= self.port <= 65_535
        ):
            raise ValueError("port must be between 1 and 65535")


def parse_server_options(arguments: Sequence[str] | None = None) -> ServerOptions:
    parser = argparse.ArgumentParser(description="Daem0nMCP v7 server")
    parser.add_argument(
        "--transport",
        "-t",
        choices=("stdio", "streamable-http", "http"),
        default="stdio",
        help="stdio (default) or Streamable HTTP",
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", "-p", type=int, default=8765)
    parsed = parser.parse_args(arguments)
    transport: Transport = "stdio" if parsed.transport == "stdio" else "streamable-http"
    return ServerOptions(transport, parsed.host, parsed.port)


def _validate_transport(host: str, auth: object) -> None:
    from ...transport_security import validate_transport_security

    validate_transport_security(host, auth_provider=auth)


def _build_origin_middleware(host: str, port: int) -> list[object]:
    from ...transport_security import build_http_transport_middleware

    return list(build_http_transport_middleware(host, port))


def _stdio_message_parser() -> object:
    from mcp import types

    model = getattr(types, "JSONRPCMessage", None)
    if isinstance(model, type) and hasattr(model, "model_validate_json"):
        return model
    adapter = getattr(types, "jsonrpc_message_adapter", None)
    if adapter is not None and hasattr(adapter, "validate_json"):
        return adapter
    raise RuntimeError("The pinned MCP stdio parser is unavailable")


def run_strict_stdio(server: Any, *, message_model: object | None = None) -> None:
    """Run stdio with duplicate-key rejection installed before SDK parsing."""

    from ...transport_security import strict_stdio_json_boundary

    parser = _stdio_message_parser() if message_model is None else message_model
    with strict_stdio_json_boundary(parser):
        server.run(transport="stdio", show_banner=False)


def run_server(
    server: Any,
    options: ServerOptions,
    *,
    validate_security: Callable[[str, object], None] = _validate_transport,
    build_origin_middleware: Callable[[str, int], list[object]] = (
        _build_origin_middleware
    ),
) -> None:
    """Run exactly one reviewed transport without writing protocol stdout."""

    if not isinstance(options, ServerOptions):
        raise ValueError("server options are required")
    if options.transport == "stdio":
        run_strict_stdio(server)
        return
    validate_security(options.host, getattr(server, "auth", None))
    origin_middleware = build_origin_middleware(options.host, options.port)
    from ...transport_security import build_uvicorn_security_config

    server.run(
        transport="streamable-http",
        host=options.host,
        port=options.port,
        middleware=origin_middleware,
        stateless_http=False,
        uvicorn_config=build_uvicorn_security_config(),
    )


__all__ = [
    "ServerOptions",
    "Transport",
    "parse_server_options",
    "run_server",
    "run_strict_stdio",
]
