"""Real subprocess MCP harness, also usable with a wheel-installed Python."""

from __future__ import annotations

import asyncio
import json
import os
import socket
import subprocess
import sys
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager
from datetime import timedelta
from pathlib import Path

import httpx
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamable_http_client


def server_environment(
    workspace: Path,
    *,
    workspace_roots: tuple[Path, ...] = (),
    environment_overrides: dict[str, str] | None = None,
) -> dict[str, str]:
    """Keep machine plumbing, excluding user service credentials/configuration."""
    allowed = {
        "PATH",
        "SYSTEMROOT",
        "WINDIR",
        "COMSPEC",
        "PATHEXT",
        "TEMP",
        "TMP",
        "HOME",
        "USERPROFILE",
        "LOCALAPPDATA",
        "APPDATA",
        "LANG",
        "GIT_CEILING_DIRECTORIES",
    }
    environment = {
        key: value for key, value in os.environ.items() if key.upper() in allowed
    }
    environment.update(
        {
            "DAEM0NMCP_PROJECT_ROOT": str(workspace),
            "DAEM0NMCP_WORKSPACE_ROOTS": json.dumps(
                [str(root) for root in workspace_roots]
            ),
            "DAEM0NMCP_PROFILE": "core",
            "PYTHONUNBUFFERED": "1",
            "PYTHONUTF8": "1",
        }
    )
    if environment_overrides:
        environment.update(environment_overrides)
    return environment


@asynccontextmanager
async def process_client(
    workspace: Path,
    transport: str,
    *,
    python: str = sys.executable,
    workspace_roots: tuple[Path, ...] = (),
    environment_overrides: dict[str, str] | None = None,
    http_probe: Callable[[str], Awaitable[None]] | None = None,
    http_headers: dict[str, str] | None = None,
):
    """Launch the installed module in a workspace and clean up on every exit."""
    environment = server_environment(
        workspace,
        workspace_roots=workspace_roots,
        environment_overrides=environment_overrides,
    )
    if transport == "stdio":
        parameters = StdioServerParameters(
            command=python,
            args=["-m", "daem0nmcp.server"],
            env=environment,
            cwd=str(workspace),
        )
        body_error: BaseException | None = None
        try:
            with (workspace / "stdio-server.log").open("w", encoding="utf-8") as log:
                async with (
                    stdio_client(parameters, errlog=log) as (read, write),
                    ClientSession(
                        read, write, read_timeout_seconds=timedelta(seconds=30)
                    ) as session,
                ):
                    await session.initialize()
                    try:
                        yield session
                    except BaseException as error:
                        # An assertion can otherwise be replaced by a concurrent
                        # SDK stdout-reader BrokenResourceError during shutdown.
                        body_error = error
        except BaseException as cleanup_error:
            if body_error is not None:
                raise body_error from cleanup_error
            raise
        if body_error is not None:
            raise body_error
        return
    if transport != "streamable-http":
        raise ValueError("unsupported test transport")
    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        port = reservation.getsockname()[1]
    with (workspace / "http-server.log").open("w", encoding="utf-8") as log:
        process = subprocess.Popen(
            [
                python,
                "-m",
                "daem0nmcp.server",
                "--transport",
                transport,
                "--host",
                "127.0.0.1",
                "--port",
                str(port),
            ],
            cwd=workspace,
            env=environment,
            stdout=log,
            stderr=log,
            creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
        )
        try:
            deadline = asyncio.get_running_loop().time() + 30
            while True:
                if process.poll() is not None:
                    raise RuntimeError(
                        f"MCP server exited with code {process.returncode}; inspect http-server.log"
                    )
                try:
                    _, writer = await asyncio.open_connection("127.0.0.1", port)
                    writer.close()
                    await writer.wait_closed()
                    break
                except OSError:
                    if asyncio.get_running_loop().time() >= deadline:
                        raise TimeoutError(
                            "MCP HTTP server did not become ready"
                        ) from None
                    await asyncio.sleep(0.05)
            if http_probe is not None:
                await http_probe(f"http://127.0.0.1:{port}/mcp")
            async with (
                httpx.AsyncClient(headers=http_headers, timeout=30) as http_client,
                streamable_http_client(
                    f"http://127.0.0.1:{port}/mcp", http_client=http_client
                ) as (
                    read,
                    write,
                    _,
                ),
                ClientSession(
                    read, write, read_timeout_seconds=timedelta(seconds=30)
                ) as session,
            ):
                await session.initialize()
                yield session
        finally:
            if process.poll() is None:
                process.terminate()
            try:
                await asyncio.to_thread(process.wait, timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                await asyncio.to_thread(process.wait, timeout=10)


async def call(session: ClientSession, tool: str, arguments: dict) -> dict:
    result = await session.call_tool(tool, arguments)
    assert not result.isError, f"{tool}: MCP protocol error"
    assert isinstance(result.structuredContent, dict), (
        f"{tool}: missing structured response"
    )
    return result.structuredContent


async def succeed(session: ClientSession, tool: str, arguments: dict) -> dict:
    result = await call(session, tool, arguments)
    assert result["ok"], f"{tool}: {result.get('error')}"
    return result["data"]
