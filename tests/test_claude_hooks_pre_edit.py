"""Tests for the pre_edit Claude Code hook (blocking)."""

import pytest
import pytest_asyncio

from daem0nmcp.claude_hooks.pre_edit import async_main
from daem0nmcp.database import DatabaseManager
from daem0nmcp.enforcement import SessionManager
from daem0nmcp.memory import MemoryManager


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


async def _add_context_check(project_path):
    storage = str(project_path / ".daem0nmcp" / "storage")
    db = DatabaseManager(storage)
    await db.init_db()
    mgr = SessionManager(db)
    await mgr.add_context_check(str(project_path), "pre-edit test check")
    await db.close()


@pytest.mark.asyncio
async def test_blocks_without_preflight(tmp_project):
    file_path = str(tmp_project / "server.py")
    result = await async_main(str(tmp_project), file_path)
    assert not result.allowed
    assert "No preflight token" in result.message
    assert "consult" in result.message


@pytest.mark.asyncio
async def test_allows_with_preflight(tmp_project):
    await _add_context_check(tmp_project)
    file_path = str(tmp_project / "server.py")
    result = await async_main(str(tmp_project), file_path)
    assert result.allowed


@pytest.mark.asyncio
async def test_permissive_mode_allows_through(tmp_project, monkeypatch):
    """In permissive mode the block message is returned but allowed=False still."""
    monkeypatch.setenv("DAEM0N_HOOKS_PERMISSIVE", "1")

    file_path = str(tmp_project / "server.py")
    result = await async_main(str(tmp_project), file_path)

    # async_main still returns allowed=False (it doesn't know about permissive mode)
    # but the message is what block() would print. main() handles the permissive exit.
    assert not result.allowed
    assert "No preflight token" in result.message


@pytest.mark.asyncio
async def test_recalls_file_memories(tmp_project):
    # Seed a warning memory for a specific file
    storage = str(tmp_project / ".daem0nmcp" / "storage")
    db = DatabaseManager(storage)
    await db.init_db()
    mem = MemoryManager(db)
    await mem.remember(
        category="warning",
        content="This file has a known race condition",
        file_path=str(tmp_project / "server.py"),
        project_path=str(tmp_project),
    )
    await db.close()

    await _add_context_check(tmp_project)

    file_path = str(tmp_project / "server.py")
    result = await async_main(str(tmp_project), file_path)
    assert result.allowed
    assert "race condition" in result.message


def test_no_file_path_exits_clean(tmp_path, monkeypatch):
    (tmp_path / ".daem0nmcp").mkdir()
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(tmp_path))
    monkeypatch.setenv("TOOL_INPUT", '{"command": "ls -la"}')

    from daem0nmcp.claude_hooks.pre_edit import main

    with pytest.raises(SystemExit) as exc_info:
        main()
    assert exc_info.value.code == 0
