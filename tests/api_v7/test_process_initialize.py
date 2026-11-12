"""What the server says about itself in the MCP initialize result."""

from __future__ import annotations

import os
from unittest.mock import patch

import pytest
from fastmcp import Client

from daem0nmcp.api.v7.fastmcp import server_instructions
from daem0nmcp.api.v7.gateway_operations import CORE_LISTED_TOOLS
from daem0nmcp.api.v7.production import create_v7_server
from daem0nmcp.config import Settings
from daem0nmcp.database import DatabaseManager
from daem0nmcp.workspace import WorkspaceRegistry
from tests.api_v7.process_client import initialize_workspaces, process_client

ESSENTIALS = (
    "workspace_id",
    "session_brief",
    "error.remedy",
    "not instructions",
    "idempotency_key",
    "daem0n_tools_search",
)


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


@pytest.mark.parametrize("transport", ["stdio", "streamable-http"])
async def test_no_list_changed_notifications_are_advertised(
    initialized_workspace, transport
):
    # The registry is fixed for the life of the process, so the server never
    # sends a list-changed notification and must not promise one.
    async with process_client(initialized_workspace.root, transport) as session:
        capabilities = session.get_server_capabilities()
        assert capabilities.tools is not None
        assert capabilities.resources is not None
        for name in ("tools", "resources", "prompts"):
            capability = getattr(capabilities, name)
            if capability is not None:
                assert capability.listChanged is False, name
        assert capabilities.resources.subscribe is False


@pytest.mark.parametrize("covenant_mode", ["guided", "strict"])
@pytest.mark.parametrize("tool_surface", ["core", "full"])
async def test_initialize_carries_instructions_for_the_active_settings(
    tmp_path, covenant_mode, tool_surface
):
    root = tmp_path.resolve()
    with patch.dict(os.environ, {"DAEM0NMCP_PROJECT_ROOT": str(root)}):
        await initialize_workspaces((root,))
        settings = Settings(
            project_root=str(root),
            workspace_roots=[str(root)],
            tool_surface=tool_surface,
            covenant_mode=covenant_mode,
            dream_enabled=False,
        )
        server = create_v7_server("stdio", settings=settings, environ={})
        async with Client(server) as client:
            instructions = client.initialize_result.instructions

    assert instructions == server_instructions(
        covenant_mode=covenant_mode,
        listed_tools=CORE_LISTED_TOOLS if tool_surface == "core" else None,
    )
    assert len(instructions.split()) <= 150
    for essential in ESSENTIALS:
        assert essential in instructions, essential
    if covenant_mode == "guided":
        assert "automatically briefs" in instructions
        assert "COUNSEL_REQUIRED" in instructions
        assert "error.counsel" in instructions
    else:
        assert "automatically briefs" not in instructions
        assert "Call session_brief first" in instructions
        assert "memory_preflight" in instructions
        assert "preflight_token" in instructions
    if tool_surface == "core":
        assert "daem0n_tool_call(workspace_id, tool, arguments)" in instructions
    else:
        assert "daem0n_tool_call" not in instructions


def test_instructions_cover_every_setting_and_reject_an_unknown_mode():
    texts = {
        server_instructions(covenant_mode=mode, listed_tools=listed)
        for mode in ("guided", "strict", None)
        for listed in (CORE_LISTED_TOOLS, None)
    }
    assert len(texts) == 6
    for text in texts:
        assert len(text.split()) <= 150
        assert "\n" not in text
    with pytest.raises(ValueError):
        server_instructions(covenant_mode="lenient", listed_tools=None)
