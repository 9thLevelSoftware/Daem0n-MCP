"""Real subprocess MCP harness, also usable with a wheel-installed Python."""

from __future__ import annotations

import asyncio
import json
import os
import socket
import subprocess
import sys
import threading
import time
from collections.abc import Awaitable, Callable, Iterator
from contextlib import asynccontextmanager, contextmanager
from datetime import timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx
from joserfc import jwt
from joserfc.jwk import RSAKey
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamable_http_client

from daem0nmcp.database import DatabaseManager
from daem0nmcp.protected_files import write_new_owner_only_file
from daem0nmcp.workspace import Workspace, WorkspaceRegistry

JWT_AUDIENCE = "daem0n-access-test"
JWT_SUBJECT = "alice"


async def initialize_workspaces(roots: tuple[Path, ...]) -> list[Workspace]:
    """Create an initialized store below each root and return its workspace."""
    workspaces = []
    for root in roots:
        storage = root / ".daem0nmcp" / "storage"
        storage.mkdir(parents=True)
        manager = DatabaseManager(str(storage))
        try:
            await manager.init_db()
        finally:
            await manager.close()
        workspaces.append(WorkspaceRegistry([root], default_root=root).default)
    return workspaces


@contextmanager
def jwt_issuer() -> Iterator[tuple[str, Callable[..., str]]]:
    """Serve a JWKS on loopback and yield its issuer URL and a token factory."""
    key = RSAKey.generate_key(2048, parameters={"kid": "workspace-access-test"})
    body = json.dumps({"keys": [key.as_dict(private=False)]}).encode()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    issuer_url = f"http://127.0.0.1:{server.server_port}"

    def token(**overrides) -> str:
        claims = {
            "iss": issuer_url,
            "aud": JWT_AUDIENCE,
            "sub": JWT_SUBJECT,
            "exp": int(time.time()) + 300,
            **overrides,
        }
        return jwt.encode({"alg": "RS256", "kid": "workspace-access-test"}, claims, key)

    try:
        yield issuer_url, token
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        assert not thread.is_alive()


def write_workspace_grants(policy_path: Path, workspace_ids: list[str]) -> None:
    """Grant the test JWT subject exactly these workspaces."""
    body = json.dumps(
        {"schema_version": 1, "grants": {f"oauth-sub:{JWT_SUBJECT}": workspace_ids}}
    ).encode()
    if policy_path.exists():
        policy_path.write_bytes(body)
    else:
        write_new_owner_only_file(policy_path, body)


def jwt_environment(issuer_url: str, policy_path: Path) -> dict[str, str]:
    """Server environment that verifies the test issuer's JWTs."""
    return {
        "FASTMCP_SERVER_AUTH": "fastmcp.server.auth.providers.jwt.JWTVerifier",
        "FASTMCP_SERVER_AUTH_JWT_JWKS_URI": issuer_url + "/jwks.json",
        "FASTMCP_SERVER_AUTH_JWT_ISSUER": issuer_url,
        "FASTMCP_SERVER_AUTH_JWT_AUDIENCE": JWT_AUDIENCE,
        "DAEM0NMCP_WORKSPACE_ACCESS_FILE": str(policy_path),
    }


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
    log_name: str = "stdio-server.log",
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
            with (workspace / log_name).open("w", encoding="utf-8") as log:
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
