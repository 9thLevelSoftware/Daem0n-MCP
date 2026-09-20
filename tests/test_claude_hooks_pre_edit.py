"""Tests for the pre_edit Claude Code hook (remind only, never block)."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from daem0nmcp.claude_hooks._client import relative_project_path

ROOT = Path(__file__).resolve().parents[1]


def _run(
    args: list[str], event: dict | str, home: Path, env: dict | None = None
) -> subprocess.CompletedProcess[str]:
    # HOME bounds the project search, so an enclosing checkout's .daem0nmcp/
    # (or the developer's CLAUDE_PROJECT_DIR) can never leak into a test.
    environment = {
        **os.environ,
        "PYTHONPATH": str(ROOT),
        "HOME": str(home),
        "USERPROFILE": str(home),
    }
    environment.pop("CLAUDE_PROJECT_DIR", None)
    environment.update(env or {})
    return subprocess.run(
        [sys.executable, *args],
        input=event if isinstance(event, str) else json.dumps(event),
        capture_output=True,
        text=True,
        env=environment,
        timeout=30,
        check=False,
    )


def _pre_edit(event: dict | str, home: Path, env: dict | None = None):
    return _run(["-m", "daem0nmcp.claude_hooks.pre_edit"], event, home, env)


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


def _context(result: subprocess.CompletedProcess[str]) -> str:
    return json.loads(result.stdout)["hookSpecificOutput"]["additionalContext"]


@pytest.fixture
def project(tmp_path: Path) -> Path:
    root = tmp_path / "project"
    (root / ".daem0nmcp").mkdir(parents=True)
    return root


def test_non_daem0n_directory_exits_zero_silently(tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()
    result = _pre_edit(_write_event(plain, str(plain / "app.py")), tmp_path)
    assert result.returncode == 0
    assert result.stdout == ""


def test_daem0n_project_exits_zero_with_reminder(tmp_path, project):
    result = _pre_edit(_write_event(project, str(project / "src" / "app.py")), tmp_path)
    assert result.returncode == 0
    output = json.loads(result.stdout)["hookSpecificOutput"]
    assert output["hookEventName"] == "PreToolUse"
    assert "permissionDecision" not in output
    assert "memory_preflight" in output["additionalContext"]
    assert (
        'memory_recall_file(relative_file_path="src/app.py")'
        in output["additionalContext"]
    )
    assert "\n" not in output["additionalContext"]


def test_subdirectory_cwd_uses_the_enclosing_project(tmp_path, project):
    sub = project / "src"
    sub.mkdir()
    result = _pre_edit(_write_event(sub, "app.py"), tmp_path)
    assert result.returncode == 0
    assert 'relative_file_path="src/app.py"' in _context(result)


def test_falls_back_to_claude_project_dir(tmp_path, project):
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    event = _write_event(elsewhere, str(project / "notes.md"))
    result = _pre_edit(event, tmp_path, env={"CLAUDE_PROJECT_DIR": str(project)})
    assert result.returncode == 0
    assert 'relative_file_path="notes.md"' in _context(result)


def test_home_directory_global_state_is_not_a_project(tmp_path):
    (tmp_path / ".daem0nmcp" / "edit-bridges").mkdir(parents=True)
    result = _pre_edit(_write_event(tmp_path, str(tmp_path / "a.py")), tmp_path)
    assert result.returncode == 0
    assert result.stdout == ""


def test_path_outside_project_and_malformed_input_still_exit_zero(tmp_path, project):
    outside = _pre_edit(_write_event(project, str(tmp_path / "x.py")), tmp_path)
    assert outside.returncode == 0
    assert "relative_file_path" not in _context(outside)

    garbage = _pre_edit("not json", tmp_path)
    assert garbage.returncode == 0
    assert garbage.stdout == ""


def test_file_name_is_json_escaped_in_the_reminder(tmp_path, project):
    result = _pre_edit(_write_event(project, 'a"b).py'), tmp_path)
    assert result.returncode == 0
    assert 'relative_file_path="a\\"b).py"' in _context(result)


@pytest.mark.parametrize(
    "raw",
    [
        r"\\attacker-host\share\x.py",
        "//attacker-host/share/x.py",
        "../outside.py",
        "C:other.py" if os.name == "nt" else "/etc/passwd",
    ],
)
def test_relative_path_is_lexical_and_drops_unc_or_outside(tmp_path, monkeypatch, raw):
    def no_filesystem(*_args, **_kwargs):
        raise AssertionError("model-supplied paths must not touch the filesystem")

    monkeypatch.setattr(Path, "resolve", no_filesystem)
    monkeypatch.setattr(os, "stat", no_filesystem)
    monkeypatch.setattr(os.path, "realpath", no_filesystem)
    assert relative_project_path(tmp_path / "project", raw) is None
    assert relative_project_path(tmp_path / "project", "src/../a.py") == "a.py"


def test_imports_no_api_or_retrieval_modules(tmp_path, project):
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
    result = _run(["-c", probe], _write_event(project, str(project / "a.py")), tmp_path)
    assert result.returncode == 0, result.stderr
    assert "additionalContext" in result.stdout
