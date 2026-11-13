"""Isolation and source-preservation checks for native POSIX acceptance."""

from __future__ import annotations

import argparse
import hashlib
import importlib
import json
from pathlib import Path

import pytest


@pytest.fixture
def posix_runner(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / "scripts"))
    return importlib.import_module("native_posix_acceptance")


def test_model_environment_keeps_only_selected_test_profile(
    tmp_path, monkeypatch, posix_runner
):
    monkeypatch.setenv("OPENAI_API_KEY", "must-not-inherit")
    monkeypatch.setenv("MINIMAX_API_KEY", "must-not-inherit")
    monkeypatch.setenv("DAEM0NMCP_PROJECT_ROOT", "/original-project")
    monkeypatch.setenv("SSH_CONNECTION", "test-session")
    environment = posix_runner.environment(tmp_path, "claude", "daem0n")
    assert "OPENAI_API_KEY" not in environment
    assert "MINIMAX_API_KEY" not in environment
    assert "DAEM0NMCP_PROJECT_ROOT" not in environment
    assert environment["SSH_CONNECTION"] == "test-session"
    assert environment["HOME"] == str(tmp_path / "private/claude-account")
    assert environment["CLAUDE_CONFIG_DIR"] == str(
        tmp_path / "private/claude-account/.claude"
    )
    assert environment["DAEM0NMCP_MODELS_LOCAL_ENABLED"] == "false"


def test_mac_claude_keeps_native_keychain_home_and_private_configuration(
    tmp_path, monkeypatch, posix_runner
):
    monkeypatch.setattr(posix_runner.platform, "system", lambda: "Darwin")
    environment = posix_runner.environment(tmp_path, "claude", "daem0n")
    assert environment["HOME"] == str(Path.home())
    assert environment["CLAUDE_CONFIG_DIR"] == str(
        tmp_path / "private/claude-account/.claude"
    )
    assert environment["XDG_CONFIG_HOME"] == str(
        tmp_path / "private/claude-account/.config"
    )


def test_disposable_mac_bridge_address_fits_unix_socket_limit(
    tmp_path, monkeypatch, posix_runner
):
    monkeypatch.setattr(posix_runner.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(posix_runner.os, "getuid", lambda: 501, raising=False)
    lane = tmp_path / ("long-private-acceptance-path-" * 8)
    base = posix_runner.bridge_root(lane)
    socket = base / "run" / ("a" * 16) / ("b-" + "b" * 24 + ".sock")
    assert len(str(socket).encode()) < 104
    assert base != posix_runner.bridge_root(lane / "independent")


def test_mutation_refuses_changed_preimage(tmp_path, posix_runner):
    lane = posix_runner.lane_path(tmp_path, "agy", "daem0n", "final")
    project = lane / "project"
    project.mkdir(parents=True)
    target = project / "source.py"
    target.write_text("unrelated work\n")
    metadata = {
        "project": str(project),
        "manifest": {
            "files": {"source.py": hashlib.sha256(b"baseline\n").hexdigest()},
            "scenario": {"mutation_path": "source.py", "added_path": "added.py"},
        },
    }
    (lane / "fixture.json").write_text(json.dumps(metadata))
    arguments = argparse.Namespace(
        root=tmp_path, host="agy", source="daem0n", matrix="final", fixtures=tmp_path
    )
    with pytest.raises(RuntimeError, match="frozen baseline"):
        posix_runner.mutate(arguments)
    assert target.read_text() == "unrelated work\n"
    assert not (project / "added.py").exists()


def test_restore_refuses_modified_added_file(tmp_path, posix_runner):
    lane = posix_runner.lane_path(tmp_path, "agy", "daem0n", "final")
    project = lane / "project"
    project.mkdir(parents=True)
    fixtures = tmp_path / "sources"
    (fixtures / "daem0n").mkdir(parents=True)
    (fixtures / "daem0n/source.py").write_text("baseline\n")
    (project / "source.py").write_text("baseline\nowned append\n")
    added = project / "added.py"
    added.write_text("later unrelated change\n")
    metadata = {
        "project": str(project),
        "manifest": {
            "scenario": {
                "mutation_path": "source.py",
                "mutation_append": "owned append\n",
                "added_path": "added.py",
                "added_content": "owned content\n",
            }
        },
    }
    (lane / "fixture.json").write_text(json.dumps(metadata))
    arguments = argparse.Namespace(
        root=tmp_path, host="agy", source="daem0n", matrix="final", fixtures=fixtures
    )
    with pytest.raises(RuntimeError, match="exact owned mutation"):
        posix_runner.mutate(arguments, restore=True)
    assert added.read_text() == "later unrelated change\n"
    assert (project / "source.py").read_text() == "baseline\nowned append\n"
