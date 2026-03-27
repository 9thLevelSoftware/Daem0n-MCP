"""Tests for the claude_hooks shared client utilities."""

import json
import sys

import pytest

from daem0nmcp.claude_hooks._client import (
    block,
    get_command_from_input,
    get_file_path_from_input,
    get_project_path,
    get_tool_input,
    run_hook_safely,
    succeed,
)


class TestGetProjectPath:
    def test_from_claude_env(self, monkeypatch, tmp_path):
        daem0n_dir = tmp_path / ".daem0nmcp"
        daem0n_dir.mkdir()
        monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(tmp_path))
        monkeypatch.delenv("DAEM0NMCP_PROJECT_ROOT", raising=False)
        assert get_project_path() == str(tmp_path)

    def test_no_daem0n_dir_returns_none(self, monkeypatch, tmp_path):
        monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(tmp_path))
        monkeypatch.delenv("DAEM0NMCP_PROJECT_ROOT", raising=False)
        assert get_project_path() is None

    def test_fallback_to_daem0nmcp_root(self, monkeypatch, tmp_path):
        daem0n_dir = tmp_path / ".daem0nmcp"
        daem0n_dir.mkdir()
        monkeypatch.delenv("CLAUDE_PROJECT_DIR", raising=False)
        monkeypatch.setenv("DAEM0NMCP_PROJECT_ROOT", str(tmp_path))
        assert get_project_path() == str(tmp_path)

    def test_fallback_to_cwd(self, monkeypatch, tmp_path):
        daem0n_dir = tmp_path / ".daem0nmcp"
        daem0n_dir.mkdir()
        monkeypatch.delenv("CLAUDE_PROJECT_DIR", raising=False)
        monkeypatch.delenv("DAEM0NMCP_PROJECT_ROOT", raising=False)
        monkeypatch.chdir(tmp_path)
        assert get_project_path() == str(tmp_path)


class TestBlockSucceed:
    def test_block_exits_2(self):
        with pytest.raises(SystemExit) as exc_info:
            block("halt")
        assert exc_info.value.code == 2

    def test_block_permissive_exits_0(self, monkeypatch):
        monkeypatch.setenv("DAEM0N_HOOKS_PERMISSIVE", "1")
        with pytest.raises(SystemExit) as exc_info:
            block("soft warning")
        assert exc_info.value.code == 0

    def test_block_outputs_stderr(self, monkeypatch, capsys):
        monkeypatch.delenv("DAEM0N_HOOKS_PERMISSIVE", raising=False)
        with pytest.raises(SystemExit):
            block("error msg")
        assert "error msg" in capsys.readouterr().err

    def test_block_permissive_outputs_stdout(self, monkeypatch, capsys):
        monkeypatch.setenv("DAEM0N_HOOKS_PERMISSIVE", "1")
        with pytest.raises(SystemExit):
            block("warning msg")
        assert "warning msg" in capsys.readouterr().out

    def test_succeed_exits_0(self):
        with pytest.raises(SystemExit) as exc_info:
            succeed()
        assert exc_info.value.code == 0

    def test_succeed_with_message(self, capsys):
        with pytest.raises(SystemExit):
            succeed("all good")
        assert "all good" in capsys.readouterr().out


class TestToolInputParsing:
    def test_file_path_from_edit(self, monkeypatch):
        monkeypatch.setenv("TOOL_INPUT", json.dumps({"file_path": "/foo/bar.py"}))
        assert get_file_path_from_input() == "/foo/bar.py"

    def test_notebook_path(self, monkeypatch):
        monkeypatch.setenv("TOOL_INPUT", json.dumps({"notebook_path": "/foo.ipynb"}))
        assert get_file_path_from_input() == "/foo.ipynb"

    def test_file_path_takes_precedence(self, monkeypatch):
        monkeypatch.setenv(
            "TOOL_INPUT",
            json.dumps({"file_path": "/a.py", "notebook_path": "/b.ipynb"}),
        )
        assert get_file_path_from_input() == "/a.py"

    def test_command_from_bash(self, monkeypatch):
        monkeypatch.setenv("TOOL_INPUT", json.dumps({"command": "ls -la"}))
        assert get_command_from_input() == "ls -la"

    def test_invalid_json(self, monkeypatch):
        monkeypatch.setenv("TOOL_INPUT", "not json")
        assert get_tool_input() == {}
        assert get_file_path_from_input() is None

    def test_missing_env(self, monkeypatch):
        monkeypatch.delenv("TOOL_INPUT", raising=False)
        assert get_tool_input() == {}


class TestRunHookSafely:
    def test_swallows_exceptions(self):
        def bad():
            raise ValueError("boom")

        # Should exit 0, not propagate the error
        with pytest.raises(SystemExit) as exc_info:
            run_hook_safely(bad)
        assert exc_info.value.code == 0

    def test_converts_nonzero_exit_to_zero(self):
        """Non-zero SystemExit is converted to exit 0 so Claude Code doesn't report hook errors."""

        def exits():
            sys.exit(2)

        with pytest.raises(SystemExit) as exc_info:
            run_hook_safely(exits)
        assert exc_info.value.code == 0

    def test_normal_completion(self):
        results = []

        def ok():
            results.append("done")

        # Should not raise (no sys.exit call inside ok())
        run_hook_safely(ok)
        assert results == ["done"]
