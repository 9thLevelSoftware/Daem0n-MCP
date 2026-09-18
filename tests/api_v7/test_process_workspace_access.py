"""Real JWT verification and live workspace grants through packaged HTTP MCP."""

from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
import pytest
from joserfc import jwt
from joserfc.jwk import RSAKey
from mcp.shared.exceptions import McpError

from daem0nmcp.database import DatabaseManager
from daem0nmcp.protected_files import write_new_owner_only_file
from daem0nmcp.workspace import WorkspaceRegistry
from tests.api_v7.process_client import call, process_client, succeed


@pytest.fixture
def issuer():
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

    def token(**overrides):
        claims = {
            "iss": issuer_url,
            "aud": "daem0n-access-test",
            "sub": "alice",
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


async def test_real_jwt_workspace_access_and_revocation(tmp_path, issuer):
    roots = (tmp_path / "alpha", tmp_path / "beta")
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
    alpha, beta = workspaces
    policy_path = tmp_path / "protected" / "access.json"

    def grants(identifiers):
        return json.dumps(
            {"schema_version": 1, "grants": {"oauth-sub:alice": identifiers}}
        ).encode()

    write_new_owner_only_file(policy_path, grants([alpha.workspace_id]))
    issuer_url, token = issuer
    overrides = {
        "FASTMCP_SERVER_AUTH": "fastmcp.server.auth.providers.jwt.JWTVerifier",
        "FASTMCP_SERVER_AUTH_JWT_JWKS_URI": issuer_url + "/jwks.json",
        "FASTMCP_SERVER_AUTH_JWT_ISSUER": issuer_url,
        "FASTMCP_SERVER_AUTH_JWT_AUDIENCE": "daem0n-access-test",
        "DAEM0NMCP_WORKSPACE_ACCESS_FILE": str(policy_path),
    }

    async def probe(url):
        async with httpx.AsyncClient(timeout=5) as client:
            for bearer in (
                None,
                token(exp=int(time.time()) - 120),
                token(aud="wrong-audience"),
            ):
                headers = (
                    {} if bearer is None else {"authorization": "Bearer " + bearer}
                )
                response = await client.post(
                    url,
                    json={"jsonrpc": "2.0", "method": "initialize", "id": 1},
                    headers=headers,
                )
                assert response.status_code == 401

    async with process_client(
        roots[0],
        "streamable-http",
        workspace_roots=roots,
        environment_overrides=overrides,
        http_headers={"Authorization": "Bearer " + token()},
        http_probe=probe,
    ) as session:
        await succeed(session, "session_brief", {"workspace_id": alpha.workspace_id})
        denied = await call(
            session, "session_brief", {"workspace_id": beta.workspace_id}
        )
        assert denied["error"]["code"] == "UNAUTHORIZED_WORKSPACE"
        denied = await call(
            session, "system_health", {"workspace_id": beta.workspace_id}
        )
        assert denied["error"]["code"] == "UNAUTHORIZED_WORKSPACE"
        denied = await call(
            session,
            "memory_search_text",
            {"workspace_id": beta.workspace_id, "query": "blocked"},
        )
        assert denied["error"]["code"] == "UNAUTHORIZED_WORKSPACE"
        await session.read_resource(
            f"memory://workspaces/{alpha.workspace_id}/warnings"
        )

        async def link():
            args = {"linked_workspace_id": beta.workspace_id}
            grant = await succeed(
                session,
                "memory_preflight",
                {
                    "workspace_id": alpha.workspace_id,
                    "target_tool": "workspace_link",
                    "target_arguments": args,
                },
            )
            return await call(
                session,
                "workspace_link",
                {
                    "workspace_id": alpha.workspace_id,
                    **args,
                    "preflight_token": grant["preflight_token"],
                },
            )

        denied = await link()
        assert denied["error"]["code"] == "UNAUTHORIZED_WORKSPACE"
        policy_path.write_bytes(grants([alpha.workspace_id, beta.workspace_id]))
        await succeed(session, "session_brief", {"workspace_id": beta.workspace_id})
        assert (await link())["ok"]
        policy_path.write_bytes(grants([alpha.workspace_id]))
        denied = await call(
            session,
            "memory_recall",
            {
                "workspace_id": alpha.workspace_id,
                "query": "linked",
                "linked_workspace_ids": [beta.workspace_id],
            },
        )
        assert denied["error"]["code"] == "UNAUTHORIZED_WORKSPACE"
        policy_path.write_bytes(grants([]))
        denied = await call(
            session,
            "memory_recall",
            {"workspace_id": alpha.workspace_id, "query": "revoked"},
        )
        assert denied["error"]["code"] == "UNAUTHORIZED_WORKSPACE"
        with pytest.raises(McpError, match="Error reading resource"):
            await session.read_resource(
                f"memory://workspaces/{alpha.workspace_id}/warnings"
            )
        policy_path.write_bytes(grants([beta.workspace_id]))
        await succeed(session, "session_brief", {"workspace_id": beta.workspace_id})
        await succeed(
            session,
            "memory_recall",
            {"workspace_id": beta.workspace_id, "query": "allowed"},
        )
