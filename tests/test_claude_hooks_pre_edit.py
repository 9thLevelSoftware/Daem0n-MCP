"""Tests for the pre_edit Claude Code hook (remind only, never block)."""

import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _run(args: list[str], event: dict, env: dict | None = None):
    environment = {**os.environ, "PYTHONPATH": str(ROOT)}
    environment.pop("CLAUDE_PROJECT_DIR", None)
    environment.update(env or {})
    return subprocess.run(
        [sys.executable, *args],
        input=json.dumps(event),
        capture_output=True,
        text=True,
        env=environment,
        timeout=30,
        check=False,
    )


def _write_event(cwd: Path, file_path: str) -> dict:
    return {
        "session_id": "s-1",
        "transcript_path": str(cwd / "t.jsonl"),
        "cwd": str(cwd),
        "hook_event_name": "PreToolUse",
        "tool_name": "Write",
        "tool_input": {"file_path": file_path, "content": "x = 1\n"},
        "tool_use_id": "toolu_1",
    }


def test_non_daem0n_directory_exits_zero_silently(tmp_path):
    result = _run(
        ["-m", "daem0nmcp.claude_hooks.pre_edit"],
        _write_event(tmp_path, str(tmp_path / "app.py")),
    )
    assert result.returncode == 0
    assert result.stdout == ""


def test_daem0n_project_exits_zero_with_reminder(tmp_path):
    (tmp_path / ".daem0nmcp").mkdir()
    result = _run(
        ["-m", "daem0nmcp.claude_hooks.pre_edit"],
        _write_event(tmp_path, str(tmp_path / "src" / "app.py")),
    )
    assert result.returncode == 0
    payload = json.loads(result.stdout)
    output = payload["hookSpecificOutput"]
    assert output["hookEventName"] == "PreToolUse"
    assert "permissionDecision" not in output
    assert "memory_preflight" in output["additionalContext"]
    assert (
        'memory_recall_file(relative_file_path="src/app.py")'
        in output["additionalContext"]
    )
    assert "\n" not in output["additionalContext"]


def test_falls_back_to_claude_project_dir(tmp_path):
    (tmp_path / ".daem0nmcp").mkdir()
    event = _write_event(tmp_path, "notes.md")
    del event["cwd"]
    result = _run(
        ["-m", "daem0nmcp.claude_hooks.pre_edit"],
        event,
        env={"CLAUDE_PROJECT_DIR": str(tmp_path)},
    )
    assert result.returncode == 0
    context = json.loads(result.stdout)["hookSpecificOutput"]["additionalContext"]
    assert 'relative_file_path="notes.md"' in context


def test_path_outside_project_and_malformed_input_still_exit_zero(tmp_path):
    (tmp_path / "project" / ".daem0nmcp").mkdir(parents=True)
    outside = _run(
        ["-m", "daem0nmcp.claude_hooks.pre_edit"],
        _write_event(tmp_path / "project", str(tmp_path / "elsewhere.py")),
    )
    assert outside.returncode == 0
    context = json.loads(outside.stdout)["hookSpecificOutput"]["additionalContext"]
    assert "relative_file_path" not in context

    environment = {**os.environ, "PYTHONPATH": str(ROOT)}
    garbage = subprocess.run(
        [sys.executable, "-m", "daem0nmcp.claude_hooks.pre_edit"],
        input="not json",
        capture_output=True,
        text=True,
        env=environment,
        timeout=30,
        check=False,
    )
    assert garbage.returncode == 0
    assert garbage.stdout == ""


def test_imports_no_api_or_retrieval_modules(tmp_path):
    (tmp_path / ".daem0nmcp").mkdir()
    probe = (
        "import sys, runpy\n"
        "try:\n"
        "    runpy.run_module('daem0nmcp.claude_hooks.pre_edit', run_name='__main__')\n"
        "except SystemExit:\n"
        "    pass\n"
        "heavy = sorted(m for m in sys.modules if m.startswith(("
        "'daem0nmcp.api', 'daem0nmcp.retrieval', 'daem0nmcp.edit_host',"
        " 'daem0nmcp.database', 'sqlalchemy', 'pydantic')))\n"
        "print(heavy, file=sys.stderr)\n"
        "sys.exit(1 if heavy else 0)\n"
    )
    result = _run(["-c", probe], _write_event(tmp_path, str(tmp_path / "a.py")))
    assert result.returncode == 0, result.stderr
    assert "additionalContext" in result.stdout
