"""Real subprocess coverage for what a resource read puts on the wire."""

from __future__ import annotations

import json

import pytest
from mcp import McpError

from daem0nmcp.database import DatabaseManager
from daem0nmcp.workspace import WorkspaceRegistry
from tests.api_v7.process_client import process_client, succeed

RESOURCE_NOT_FOUND = -32002
RESOURCE_KINDS = ("warnings", "failures", "rules", "active-context")


@pytest.fixture
async def initialized_workspace(tmp_path):
    storage = tmp_path / ".daem0nmcp" / "storage"
    storage.mkdir(parents=True)
    manager = DatabaseManager(str(storage))
    try:
        await manager.init_db()
    finally:
        await manager.close()
    return WorkspaceRegistry([tmp_path], default_root=tmp_path).default


async def _refusal(session, uri: str) -> tuple[int, str, object]:
    with pytest.raises(McpError) as caught:
        await session.read_resource(uri)
    error = caught.value.error
    return error.code, error.message, error.data


@pytest.mark.parametrize("transport", ["stdio", "streamable-http"])
async def test_resource_reads_return_the_declared_json_mime_type(
    initialized_workspace, transport
):
    workspace = initialized_workspace
    scope = {"workspace_id": workspace.workspace_id}
    async with process_client(workspace.root, transport) as session:
        templates = (await session.list_resource_templates()).resourceTemplates
        declared = {
            template.uriTemplate: template.mimeType
            for template in templates
            if template.uriTemplate.startswith("memory://")
        }
        assert set(declared.values()) == {"application/json"}
        assert len(declared) == len(RESOURCE_KINDS)

        await succeed(session, "session_brief", scope)
        for kind in RESOURCE_KINDS:
            uri = f"memory://workspaces/{workspace.workspace_id}/{kind}"
            result = await session.read_resource(uri)
            [content] = result.contents
            assert content.mimeType == "application/json", kind
            assert str(content.uri) == uri
            document = json.loads(content.text)
            assert document["workspace_id"] == workspace.workspace_id


@pytest.mark.parametrize("transport", ["stdio", "streamable-http"])
async def test_resource_refusals_share_one_code_and_message(
    initialized_workspace, transport
):
    workspace = initialized_workspace
    known = workspace.workspace_id
    # Well-formed, but no workspace of this server has it.
    unknown = "ws_" + "".join("1" if digit == "0" else "0" for digit in known[3:])
    assert unknown != known
    async with process_client(workspace.root, transport) as session:
        # Nothing has briefed yet, so the known workspace is refused too.
        refusals = {
            "known but unbriefed": await _refusal(
                session, f"memory://workspaces/{known}/warnings"
            ),
            "unknown workspace": await _refusal(
                session, f"memory://workspaces/{unknown}/warnings"
            ),
            "malformed workspace": await _refusal(
                session, "memory://workspaces/not-an-id/warnings"
            ),
            "unknown kind": await _refusal(
                session, f"memory://workspaces/{known}/secrets"
            ),
        }
        assert set(refusals.values()) == {
            (RESOURCE_NOT_FOUND, "Resource unavailable", None)
        }, refusals
