"""Tests for the post_edit Claude Code hook."""

import json

import pytest

from daem0nmcp.claude_hooks.post_edit import main


def _set_env(monkeypatch, project_dir, tool_input):
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", project_dir)
    monkeypatch.setenv("TOOL_INPUT", json.dumps(tool_input))


def test_significant_change_outputs_suggestion(tmp_path, monkeypatch, capsys):
    (tmp_path / ".daem0nmcp").mkdir()
    _set_env(
        monkeypatch,
        str(tmp_path),
        {
            "file_path": str(tmp_path / "server.py"),
            "old_string": "",
            "new_string": "class UserAuthService:\n    def __init__(self):\n        pass\n",
        },
    )

    with pytest.raises(SystemExit) as exc_info:
        main()

    assert exc_info.value.code == 0
    out = capsys.readouterr().out
    assert "[Daem0n suggests]" in out
    assert "server.py" in out


def test_trivial_change_no_output(tmp_path, monkeypatch, capsys):
    (tmp_path / ".daem0nmcp").mkdir()
    _set_env(
        monkeypatch,
        str(tmp_path),
        {
            "file_path": str(tmp_path / "notes.txt"),
            "old_string": "hello",
            "new_string": "world",
        },
    )

    with pytest.raises(SystemExit) as exc_info:
        main()

    assert exc_info.value.code == 0
    assert capsys.readouterr().out == ""


def test_large_change_is_significant(tmp_path, monkeypatch, capsys):
    (tmp_path / ".daem0nmcp").mkdir()
    _set_env(
        monkeypatch,
        str(tmp_path),
        {
            "file_path": str(tmp_path / "big.py"),
            "old_string": "",
            "new_string": "x = 1\n" * 200,
        },
    )

    with pytest.raises(SystemExit) as exc_info:
        main()

    assert exc_info.value.code == 0
    assert "[Daem0n suggests]" in capsys.readouterr().out


def test_no_file_path_exits_clean(tmp_path, monkeypatch, capsys):
    (tmp_path / ".daem0nmcp").mkdir()
    _set_env(monkeypatch, str(tmp_path), {"command": "ls"})

    with pytest.raises(SystemExit) as exc_info:
        main()

    assert exc_info.value.code == 0
    assert capsys.readouterr().out == ""


def test_no_project_exits_clean(tmp_path, monkeypatch, capsys):
    _set_env(
        monkeypatch,
        str(tmp_path),
        {
            "file_path": str(tmp_path / "server.py"),
            "old_string": "",
            "new_string": "class Foo: pass",
        },
    )

    with pytest.raises(SystemExit) as exc_info:
        main()

    assert exc_info.value.code == 0
    assert capsys.readouterr().out == ""
