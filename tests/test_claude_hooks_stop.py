"""Tests for the stop Claude Code hook."""

import json
import os
import subprocess
import sys
from pathlib import Path

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
async def test_suggests_decisions_without_writing_memory(tmp_project):
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
    assert "The hook wrote nothing" in result.message
    assert "ask Claude" in result.message
    assert "mcp__daem0nmcp__memory_preflight" in result.message
    assert "mcp__daem0nmcp__memory_store" in result.message

    # The standalone hook must never mutate storage behind the MCP boundary.
    db = DatabaseManager(str(tmp_project / ".daem0nmcp" / "storage"))
    await db.init_db()
    mem = MemoryManager(db)
    stats = await mem.get_statistics()
    await db.close()
    assert stats["total_memories"] == 0


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


def _run_stop(event: dict, home: Path) -> subprocess.CompletedProcess[str]:
    environment = {
        **os.environ,
        "PYTHONPATH": str(Path(__file__).resolve().parents[1]),
        "HOME": str(home),
        "USERPROFILE": str(home),
    }
    environment.pop("CLAUDE_PROJECT_DIR", None)
    return subprocess.run(
        [sys.executable, "-m", "daem0nmcp.claude_hooks.stop"],
        input=json.dumps(event),
        capture_output=True,
        text=True,
        env=environment,
        timeout=30,
        check=False,
    )


def test_main_reads_stdin_event_and_nested_transcript(tmp_path):
    project = tmp_path / "project"
    (project / ".daem0nmcp").mkdir(parents=True)
    (project / "src").mkdir()
    state_dir = tmp_path / ".daem0nmcp" / "hook_state"
    state_dir.mkdir(parents=True)
    stale = state_dir / "stop_old-session.json"
    stale.write_text("{}", encoding="utf-8")
    os.utime(stale, (0, 0))
    transcript = tmp_path / "transcript.jsonl"
    filler = [
        {"type": "user", "message": {"role": "user", "content": f"step {n}"}}
        for n in range(300)
    ]
    records = [
        *filler,
        {"type": "summary", "summary": "Caching work"},
        {
            "type": "user",
            "sessionId": "s-1",
            "message": {"role": "user", "content": "Add caching"},
        },
        {
            "type": "assistant",
            "sessionId": "s-1",
            "message": {
                "role": "assistant",
                "content": [
                    {
                        "type": "text",
                        "text": (
                            "I will use Redis for caching because it gives shared "
                            "state. Implementation is complete and all tasks are done."
                        ),
                    }
                ],
            },
        },
    ]
    transcript.write_text(
        "\n".join(json.dumps(record) for record in records) + "\n", encoding="utf-8"
    )
    event = {
        "session_id": "s-1",
        "transcript_path": str(transcript),
        "cwd": str(project / "src"),
        "hook_event_name": "Stop",
        "stop_hook_active": False,
    }

    result = _run_stop(event, tmp_path)

    assert result.returncode == 0, result.stderr
    assert '"decision"' not in result.stdout
    message = json.loads(result.stdout)["systemMessage"]
    assert "mcp__daem0nmcp__memory_store" in message
    assert "memory_record_outcome" in message
    assert not (project / ".daem0nmcp" / "storage").exists()
    state = json.loads((state_dir / "stop_s-1.json").read_text(encoding="utf-8"))
    assert state["last_reminder_turn"] == len(records)
    assert not stale.exists()


def test_main_is_silent_outside_a_daem0n_project(tmp_path):
    transcript = tmp_path / "transcript.jsonl"
    transcript.write_text(
        json.dumps(
            {"message": {"role": "assistant", "content": "All tasks are complete."}}
        )
        + "\n",
        encoding="utf-8",
    )
    result = _run_stop(
        {"session_id": "s-2", "transcript_path": str(transcript), "cwd": str(tmp_path)},
        tmp_path,
    )
    assert result.returncode == 0
    assert result.stdout == ""
