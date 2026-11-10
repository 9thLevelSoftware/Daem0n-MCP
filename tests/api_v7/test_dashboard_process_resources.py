"""Real MCP discovery and reads for the static v7 dashboard shells."""

from __future__ import annotations

import subprocess

import pytest

from daem0nmcp.database import DatabaseManager
from daem0nmcp.workspace import WorkspaceRegistry
from tests.api_v7.process_client import process_client, succeed


@pytest.fixture
async def initialized_dashboard_workspace(tmp_path):
    storage = tmp_path / ".daem0nmcp" / "storage"
    storage.mkdir(parents=True)
    manager = DatabaseManager(str(storage))
    try:
        await manager.init_db()
    finally:
        await manager.close()
    return WorkspaceRegistry([tmp_path], default_root=tmp_path).default


@pytest.mark.parametrize("transport", ["stdio", "streamable-http"])
async def test_production_briefing_git_paths_stay_in_nested_workspace(
    tmp_path, transport
):
    subprocess.run(
        ["git", "init", "-q", str(tmp_path)], check=True, capture_output=True
    )
    nested = tmp_path / "nested"
    nested.mkdir()
    (nested / "inside.txt").write_text("inside", encoding="utf-8")
    (tmp_path / "outside.txt").write_text("outside", encoding="utf-8")
    subprocess.run(
        ["git", "-C", str(tmp_path), "add", "."], check=True, capture_output=True
    )
    (nested / ".gitignore").write_text(
        ".daem0nmcp/\nstdio-server.log\nhttp-server.log\n", encoding="utf-8"
    )
    manager = DatabaseManager(str(nested / ".daem0nmcp" / "storage"))
    try:
        await manager.init_db()
    finally:
        await manager.close()
    workspace = WorkspaceRegistry([nested], default_root=nested).default
    async with process_client(nested, transport) as session:
        briefing = await succeed(
            session, "session_brief", {"workspace_id": workspace.workspace_id}
        )
        assert {item["relative_file_path"] for item in briefing["git_changes"]} == {
            "inside.txt",
            ".gitignore",
        }
        assert briefing["workspace_statistics"]["patterns"] == 0
        assert briefing["workspace_statistics"]["successful_outcomes"] == 0


@pytest.mark.parametrize("transport", ["stdio", "streamable-http"])
async def test_production_dashboard_shells_are_discoverable_and_readable(
    initialized_dashboard_workspace,
    transport,
):
    workspace = initialized_dashboard_workspace
    expected = {
        "ui://daem0n/test",
        "ui://daem0n/search",
        "ui://daem0n/briefing",
        "ui://daem0n/covenant",
        "ui://daem0n/community",
        "ui://daem0n/graph",
    }
    async with process_client(workspace.root, transport) as session:
        resources = await session.list_resources()
        listed = {str(resource.uri) for resource in resources.resources}
        assert expected <= listed
        templates = await session.list_resource_templates()
        assert {
            str(template.uriTemplate) for template in templates.resourceTemplates
        } == {
            "memory://workspaces/{workspace_id}/warnings",
            "memory://workspaces/{workspace_id}/failures",
            "memory://workspaces/{workspace_id}/rules",
            "memory://workspaces/{workspace_id}/active-context",
        }
        # A static shell contains no workspace data and does not require the
        # scoped session_brief ritual needed by workspace JSON resources.
        for uri in expected:
            result = await session.read_resource(uri)
            assert len(result.contents) == 1
            document = result.contents[0].text
            assert "Content-Security-Policy" in document
            assert "default-src 'none'" in document
            assert '<script id="app-data" type="application/json">' in document
            assert f'"workspace_id":"{workspace.workspace_id}"' not in document
