"""What the server says about itself in the MCP initialize result."""

from __future__ import annotations

import pytest

from daem0nmcp.database import DatabaseManager
from daem0nmcp.workspace import WorkspaceRegistry
from tests.api_v7.process_client import process_client


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
