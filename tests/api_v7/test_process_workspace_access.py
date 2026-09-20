"""Real JWT verification and live workspace grants through packaged HTTP MCP."""

from __future__ import annotations

import time

import httpx
import pytest
from mcp.shared.exceptions import McpError

from tests.api_v7.process_client import (
    call,
    initialize_workspaces,
    jwt_environment,
    jwt_issuer,
    process_client,
    succeed,
    write_workspace_grants,
)


@pytest.fixture
def issuer():
    with jwt_issuer() as value:
        yield value


async def test_real_jwt_workspace_access_and_revocation(tmp_path, issuer):
    roots = (tmp_path / "alpha", tmp_path / "beta")
    workspaces = await initialize_workspaces(roots)
    alpha, beta = workspaces
    policy_path = tmp_path / "protected" / "access.json"
    write_workspace_grants(policy_path, [alpha.workspace_id])
    issuer_url, token = issuer
    overrides = jwt_environment(issuer_url, policy_path)

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
        write_workspace_grants(policy_path, [alpha.workspace_id, beta.workspace_id])
        await succeed(session, "session_brief", {"workspace_id": beta.workspace_id})
        assert (await link())["ok"]
        write_workspace_grants(policy_path, [alpha.workspace_id])
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
        write_workspace_grants(policy_path, [])
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
        write_workspace_grants(policy_path, [beta.workspace_id])
        await succeed(session, "session_brief", {"workspace_id": beta.workspace_id})
        await succeed(
            session,
            "memory_recall",
            {"workspace_id": beta.workspace_id, "query": "allowed"},
        )
