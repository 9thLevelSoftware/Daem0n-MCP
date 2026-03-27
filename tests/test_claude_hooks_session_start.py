"""Tests for the session_start Claude Code hook."""

import pytest
import pytest_asyncio

from daem0nmcp.claude_hooks.session_start import async_main
from daem0nmcp.database import DatabaseManager
from daem0nmcp.enforcement import SessionManager
from daem0nmcp.memory import MemoryManager


@pytest_asyncio.fixture
async def tmp_project(tmp_path):
    """Create a temporary project with .daem0nmcp storage and seeded data."""
    daem0n_dir = tmp_path / ".daem0nmcp"
    daem0n_dir.mkdir()
    storage = daem0n_dir / "storage"
    storage.mkdir()

    db = DatabaseManager(str(storage))
    await db.init_db()
    memory = MemoryManager(db)

    await memory.remember(
        category="decision", content="Use Redis for caching", project_path=str(tmp_path)
    )
    await memory.remember(
        category="pattern",
        content="Always use dependency injection",
        project_path=str(tmp_path),
    )
    await memory.remember(
        category="warning",
        content="SQLite locks under heavy load",
        project_path=str(tmp_path),
    )

    yield tmp_path

    await db.close()


@pytest.mark.asyncio
async def test_session_start_outputs_briefing(tmp_project):
    text = await async_main(str(tmp_project))
    assert "[Daem0n Briefing]" in text
    assert "3 memories" in text
    assert "Commune complete." in text


@pytest.mark.asyncio
async def test_session_start_marks_briefed(tmp_project):
    await async_main(str(tmp_project))

    db = DatabaseManager(str(tmp_project / ".daem0nmcp" / "storage"))
    await db.init_db()
    session_mgr = SessionManager(db)
    state = await session_mgr.get_session_state(str(tmp_project))
    await db.close()

    assert state is not None
    assert state["briefed"] is True


def test_main_outputs_prompt_message(tmp_path, monkeypatch, capsys):
    """main() prints the prompt message telling the LLM to call commune."""
    # Create .daem0nmcp so get_project_path() returns a path
    (tmp_path / ".daem0nmcp").mkdir()
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(tmp_path))

    from daem0nmcp.claude_hooks.session_start import main

    with pytest.raises(SystemExit) as exc_info:
        main()

    assert exc_info.value.code == 0
    captured = capsys.readouterr()
    assert "[Daem0n] IMPORTANT" in captured.out
    assert 'commune(action="briefing")' in captured.out


@pytest.mark.asyncio
async def test_session_start_no_daem0n_exits_clean(tmp_path, monkeypatch):
    """main() calls sys.exit(0) when no .daem0nmcp dir exists."""
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(tmp_path))

    from daem0nmcp.claude_hooks.session_start import main

    with pytest.raises(SystemExit) as exc_info:
        main()

    assert exc_info.value.code == 0
