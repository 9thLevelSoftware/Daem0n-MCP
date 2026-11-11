"""Tests for the pre_bash Claude Code hook (blocking)."""

import pytest
import pytest_asyncio

from daem0nmcp.claude_hooks.pre_bash import async_main
from daem0nmcp.database import DatabaseManager
from daem0nmcp.rules import RulesEngine


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


async def _add_rule(project_path, trigger, must_not=None, warnings=None):
    storage = str(project_path / ".daem0nmcp" / "storage")
    db = DatabaseManager(storage)
    await db.init_db()
    engine = RulesEngine(db)
    await engine.add_rule(
        trigger=trigger,
        must_not=must_not or [],
        warnings=warnings or [],
    )
    await db.close()


@pytest.mark.asyncio
async def test_no_rules_allows_through(tmp_project):
    result = await async_main(str(tmp_project), "git status")
    assert not result.blocked
    assert result.message == ""


@pytest.mark.asyncio
async def test_blocks_on_must_not(tmp_project):
    await _add_rule(
        tmp_project,
        trigger="executing bash dangerous database command",
        must_not=["Never run DROP TABLE in production"],
    )

    result = await async_main(str(tmp_project), "psql -c 'DROP TABLE users' database")
    assert result.blocked
    assert "Daem0n blocks" in result.message
    assert "MUST NOT" in result.message


@pytest.mark.asyncio
async def test_warns_on_soft_rule(tmp_project):
    await _add_rule(
        tmp_project,
        trigger="executing bash git push command",
        warnings=["Always verify branch before pushing"],
    )

    result = await async_main(str(tmp_project), "git push origin main")
    assert not result.blocked
    assert "Daem0n warns" in result.message


@pytest.mark.asyncio
async def test_permissive_mode(tmp_project):
    """async_main still flags the block; main() handles permissive exit."""
    await _add_rule(
        tmp_project,
        trigger="executing bash dangerous database command",
        must_not=["Never run DROP TABLE"],
    )

    # async_main always returns blocked=True regardless of DAEM0N_HOOKS_PERMISSIVE.
    # The permissive logic is in main() -> block() which we already test via _client.
    result = await async_main(str(tmp_project), "psql -c 'DROP TABLE users' database")
    assert result.blocked
    assert "MUST NOT" in result.message


def test_no_project_exits_clean(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(tmp_path))
    monkeypatch.setenv("TOOL_INPUT", '{"command": "ls"}')

    from daem0nmcp.claude_hooks.pre_bash import main

    with pytest.raises(SystemExit) as exc_info:
        main()
    assert exc_info.value.code == 0


def test_v7_governance_rules_stdin_reminder(tmp_path):
    import json
    import sqlite3

    from daem0nmcp.claude_hooks.pre_bash import reminder

    storage = tmp_path / ".daem0nmcp" / "storage"
    storage.mkdir(parents=True)
    db_file = storage / "daem0nmcp.db"
    conn = sqlite3.connect(db_file)
    try:
        conn.execute(
            "CREATE TABLE governance_rules ("
            "rule_id TEXT PRIMARY KEY, "
            "trigger TEXT NOT NULL, "
            "must_not_json TEXT NOT NULL, "
            "warnings_json TEXT NOT NULL, "
            "priority INTEGER NOT NULL DEFAULT 0, "
            "enabled INTEGER NOT NULL DEFAULT 1"
            ")"
        )
        conn.execute(
            "INSERT INTO governance_rules VALUES (?, ?, ?, ?, ?, ?)",
            (
                "rul_1",
                "executing bash dangerous git force push",
                json.dumps(["Never force push to main"]),
                json.dumps([]),
                10,
                1,
            ),
        )
        conn.commit()
    finally:
        conn.close()

    output_raw = reminder(
        {
            "cwd": str(tmp_path),
            "tool_name": "Bash",
            "tool_input": {"command": "git push --force origin main"},
        }
    )
    assert output_raw is not None
    output = json.loads(output_raw)["hookSpecificOutput"]
    assert output["hookEventName"] == "PreToolUse"
    assert "Never force push to main" in output["additionalContext"]
    assert "permissionDecision" not in output
