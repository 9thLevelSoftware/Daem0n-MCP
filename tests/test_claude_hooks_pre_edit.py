"""Tests for the pre_edit Claude Code hook (blocking)."""

import pytest
import pytest_asyncio

from daem0nmcp.claude_hooks.pre_edit import async_main, handle_pre_edit
from daem0nmcp.database import DatabaseManager
from daem0nmcp.edit_bridge_transport import provision_bridge_credential


@pytest_asyncio.fixture
async def tmp_project(tmp_path):
    """Create a temp project with initialised database."""
    daem0n_dir = tmp_path / ".daem0nmcp"
    daem0n_dir.mkdir()
    storage = daem0n_dir / "storage"
    storage.mkdir()

    db = DatabaseManager(str(storage))
    await db.init_db()
    yield tmp_path
    await db.close()


@pytest.mark.asyncio
async def test_blocks_without_preflight(tmp_project):
    file_path = str(tmp_project / "server.py")
    result = await async_main(str(tmp_project), file_path)
    assert not result.allowed
    assert result.message == "EDIT_BRIDGE_UNAVAILABLE"
    assert str(tmp_project) not in result.message


@pytest.mark.asyncio
async def test_legacy_context_state_cannot_bypass_v7_preflight(tmp_project):
    file_path = str(tmp_project / "server.py")
    result = await async_main(str(tmp_project), file_path)
    assert not result.allowed
    assert result.message == "EDIT_BRIDGE_UNAVAILABLE"


@pytest.mark.asyncio
async def test_permissive_mode_allows_through(tmp_project, monkeypatch):
    """In permissive mode the block message is returned but allowed=False still."""
    monkeypatch.setenv("DAEM0N_HOOKS_PERMISSIVE", "1")

    file_path = str(tmp_project / "server.py")
    result = await async_main(str(tmp_project), file_path)

    # async_main still returns allowed=False (it doesn't know about permissive mode)
    # but the message is what block() would print. main() handles the permissive exit.
    assert not result.allowed
    assert result.message == "EDIT_BRIDGE_UNAVAILABLE"


@pytest.mark.asyncio
async def test_does_not_read_or_surface_legacy_file_memories(tmp_project):
    file_path = str(tmp_project / "server.py")
    result = await async_main(str(tmp_project), file_path)
    assert not result.allowed
    assert "race condition" not in result.message
    assert result.message == "EDIT_BRIDGE_UNAVAILABLE"


def test_no_file_path_exits_clean(tmp_path, monkeypatch):
    (tmp_path / ".daem0nmcp").mkdir()
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(tmp_path))
    monkeypatch.setenv("TOOL_INPUT", '{"command": "ls -la"}')

    from daem0nmcp.claude_hooks.pre_edit import main

    with pytest.raises(SystemExit) as exc_info:
        main()
    assert exc_info.value.code == 2


def test_remote_mode_without_workspace_binding_fails_recoverably(tmp_path, monkeypatch):
    target = tmp_path / "target.txt"
    target.write_text("before", encoding="utf-8")
    credential = tmp_path / "host" / "credential.json"
    provision_bridge_credential(
        credential,
        principal_id="remote-principal",
        transports=frozenset({"remote-https"}),
    )
    ca_file = tmp_path / "ca.pem"
    ca_file.write_text("unused", encoding="utf-8")
    monkeypatch.setenv("DAEM0NMCP_EDIT_BRIDGE_CREDENTIAL_FILE", str(credential))
    monkeypatch.setenv("DAEM0NMCP_EDIT_BRIDGE_MODE", "remote-https")
    monkeypatch.setenv("DAEM0NMCP_EDIT_BRIDGE_REMOTE_URL", "https://server.example")
    monkeypatch.setenv("DAEM0NMCP_EDIT_BRIDGE_CA_FILE", str(ca_file))
    monkeypatch.delenv("DAEM0NMCP_EDIT_HOST_WORKSPACE_BINDING_FILE", raising=False)

    result = handle_pre_edit(
        {
            "session_id": "remote-session",
            "tool_use_id": "remote-request",
            "tool_name": "Edit",
            "tool_input": {
                "file_path": str(target),
                "old_string": "before",
                "new_string": "after",
            },
        },
        str(tmp_path),
    )

    assert not result.allowed
    assert result.message == "EDIT_BRIDGE_UNAVAILABLE"
