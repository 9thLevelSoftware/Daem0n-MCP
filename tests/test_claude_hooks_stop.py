"""Tests for the stop Claude Code hook."""

import pytest
import pytest_asyncio

from daem0nmcp.claude_hooks.stop import analyse_and_remember
from daem0nmcp.database import DatabaseManager
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


@pytest.mark.asyncio
async def test_completion_signal_triggers_reminder(tmp_project):
    messages = [
        {"role": "user", "content": "Implement the login feature"},
        {
            "role": "assistant",
            "content": "I've successfully implemented the login feature. All tasks complete.",
        },
    ]
    state = {"reminder_count": 0, "last_reminder_turn": -1}
    result = await analyse_and_remember(str(tmp_project), messages, state)
    assert "Daem0n" in result.message


@pytest.mark.asyncio
async def test_no_completion_no_output(tmp_project):
    messages = [
        {"role": "user", "content": "What does this function do?"},
        {
            "role": "assistant",
            "content": "Let me explain the function. It processes input data.",
        },
    ]
    state = {"reminder_count": 0, "last_reminder_turn": -1}
    result = await analyse_and_remember(str(tmp_project), messages, state)
    # Exploration-only or no completion -> empty
    assert result.message == ""


@pytest.mark.asyncio
async def test_auto_captures_decisions(tmp_project):
    messages = [
        {"role": "user", "content": "Add caching to the API"},
        {
            "role": "assistant",
            "content": (
                "I will use Redis for caching because it provides fast in-memory storage "
                "with persistence options. Implementation is complete and all tasks are done."
            ),
        },
    ]
    state = {"reminder_count": 0, "last_reminder_turn": -1}
    result = await analyse_and_remember(str(tmp_project), messages, state)
    assert "auto-captured" in result.message

    # Verify memory was actually created
    db = DatabaseManager(str(tmp_project / ".daem0nmcp" / "storage"))
    await db.init_db()
    mem = MemoryManager(db)
    stats = await mem.get_statistics()
    await db.close()
    assert stats["total_memories"] > 0


@pytest.mark.asyncio
async def test_anti_loop_prevents_spam(tmp_project):
    messages = [
        {"role": "user", "content": "Do something"},
        {"role": "assistant", "content": "All tasks are complete."},
    ]

    state = {"reminder_count": 0, "last_reminder_turn": -1}

    # First call produces output and mutates state
    r1 = await analyse_and_remember(str(tmp_project), messages, state)
    assert r1.message != ""

    # Second call still produces output (count is now 1)
    r2 = await analyse_and_remember(str(tmp_project), messages, state)
    assert r2.message != ""

    # Third call suppressed (count is now >= 2 and turn is recent)
    r3 = await analyse_and_remember(str(tmp_project), messages, state)
    assert r3.message == ""


@pytest.mark.asyncio
async def test_no_messages_returns_empty(tmp_project):
    """Empty transcript -> empty result (main() would sys.exit(0) before calling this)."""
    state = {"reminder_count": 0, "last_reminder_turn": -1}
    result = await analyse_and_remember(str(tmp_project), [], state)
    # 0-length messages -> anti-loop sees turn 0 vs -1, which passes,
    # but no content means no completion signal -> empty
    assert result.message == ""
