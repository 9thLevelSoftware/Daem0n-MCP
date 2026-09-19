"""Tests for the post_edit Claude Code hook."""

import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

PROBE = (
    "import runpy, sys\n"
    "try:\n"
    "    runpy.run_module('daem0nmcp.claude_hooks.post_edit', run_name='__main__')\n"
    "except SystemExit as exc:\n"
    "    code = exc.code\n"
    "print(code, 'daem0nmcp.edit_host' in sys.modules)\n"
)


def _run(event: dict, extra_env: dict | None = None) -> subprocess.CompletedProcess:
    environment = {**os.environ, "PYTHONPATH": str(ROOT)}
    environment.pop("DAEM0NMCP_EDIT_BRIDGE_CREDENTIAL_FILE", None)
    environment.update(extra_env or {})
    return subprocess.run(
        [sys.executable, "-c", PROBE],
        input=json.dumps(event),
        capture_output=True,
        text=True,
        env=environment,
        timeout=30,
        check=False,
    )


def _event(project: Path) -> dict:
    return {
        "session_id": "s-1",
        "cwd": str(project),
        "hook_event_name": "PostToolUse",
        "tool_name": "Write",
        "tool_use_id": "toolu_1",
        "tool_input": {"file_path": str(project / "a.py"), "content": "x = 1\n"},
        "tool_response": {"success": True},
    }


def test_unpaired_project_exits_zero_without_importing_the_bridge(tmp_path):
    (tmp_path / ".daem0nmcp").mkdir()
    result = _run(_event(tmp_path))
    assert result.returncode == 0, result.stderr
    assert result.stdout.split() == ["0", "False"]


def test_misconfigured_credential_still_exits_zero(tmp_path):
    (tmp_path / ".daem0nmcp").mkdir()
    result = _run(
        _event(tmp_path),
        {"DAEM0NMCP_EDIT_BRIDGE_CREDENTIAL_FILE": str(tmp_path / "missing.json")},
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.split() == ["0", "True"]


def test_paired_project_without_consumed_edit_captures_nothing(tmp_path, monkeypatch):
    from daem0nmcp.claude_hooks.post_edit import handle_post_edit
    from daem0nmcp.edit_host import (
        EditHostStateStore,
        provision_local_bridge_installation,
    )

    project = tmp_path / "project"
    (project / ".daem0nmcp").mkdir(parents=True)
    installation = provision_local_bridge_installation(
        project, config_root=tmp_path / "host"
    )
    for name, value in installation.environment().items():
        monkeypatch.setenv(name, value)
    lookups = []
    original = EditHostStateStore.get_consumed

    def spy(self, **kwargs):
        result = original(self, **kwargs)
        lookups.append(result)
        return result

    monkeypatch.setattr(EditHostStateStore, "get_consumed", spy)

    assert handle_post_edit(_event(project), str(project)) is False
    assert lookups == [None]
