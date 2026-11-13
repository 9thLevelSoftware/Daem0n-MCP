"""Privacy and preservation guarantees of the native acceptance harness."""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess

import pytest

from scripts import native_acceptance_fixtures as fixtures
from scripts import native_acceptance_platforms as platforms


def test_source_export_excludes_tracked_configuration_and_untracked_source(
    tmp_path, monkeypatch
):
    repository = tmp_path / "repository"
    source = repository / "daem0nmcp/retrieval/lexical_config.py"
    source.parent.mkdir(parents=True)
    source.write_text(
        "def lexical_fts_table_name(): return 'uncommitted-private-change'\n"
    )
    revision_bytes = b"def lexical_fts_table_name(): return 'known'\n"
    untracked = source.parent / "private.py"
    untracked.write_text("private = 'must not export'\n")
    config = repository / ".codex/config.toml"
    config.parent.mkdir()
    config.write_text("credential = 'must not export'\n")

    def git(_repository, *arguments):
        if arguments == ("ls-tree", "-r", "-z", "a" * 40):
            return (
                "100644 blob "
                + "b" * 40
                + "\tdaem0nmcp/retrieval/lexical_config.py\0"
                + "100644 blob "
                + "c" * 40
                + "\t.codex/config.toml\0"
            )
        if arguments == ("rev-parse", "HEAD"):
            return "a" * 40
        return " M daem0nmcp/retrieval/lexical_config.py"

    monkeypatch.setattr(fixtures, "git", git)
    monkeypatch.setattr(
        fixtures, "git_blob", lambda _repository, _object: revision_bytes
    )
    destination = tmp_path / "export"
    manifest = fixtures.export_project("daem0n", repository, destination)
    assert manifest["source_dirty"] is False
    assert manifest["source_worktree_dirty"] is True
    assert manifest["files"] == {
        "daem0nmcp/retrieval/lexical_config.py": hashlib.sha256(
            revision_bytes
        ).hexdigest()
    }
    assert not (destination / ".codex").exists()
    assert not (destination / "daem0nmcp/retrieval/private.py").exists()


def test_stage_refuses_runtime_state_and_traversing_destination(tmp_path):
    (tmp_path / ".daem0nmcp").mkdir()
    (tmp_path / ".daem0nmcp/storage.db").write_bytes(b"runtime state")
    with pytest.raises(ValueError, match="runtime state"):
        platforms.stage_script(tmp_path, "fixture")
    with pytest.raises(ValueError, match="simple relative names"):
        platforms.stage_script(tmp_path, "../outside")


def test_remote_log_redacts_selected_credentials_without_saving_script(
    tmp_path, monkeypatch
):
    credential = "opaque-provider-key-with-no-standard-prefix"
    script = f"selected_credential={credential!r}"
    monkeypatch.setattr(platforms, "transport", lambda _: ["fake-transport"])
    monkeypatch.setattr(
        platforms.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args[0], 0, f"provider echoed {credential}", f"failed value {credential}"
        ),
    )
    arguments = argparse.Namespace(
        evidence=tmp_path,
        platform="macos",
        attempt="one",
        action="script",
        timeout=10,
    )
    result = platforms.execute(arguments, script, sensitive_values=(credential,))
    assert result["exit_code"] == 0
    attempt = tmp_path / "macos/one"
    for path in attempt.iterdir():
        assert credential not in path.read_text()
        assert "selected_credential=" not in path.read_text()
    request = json.loads((attempt / "request.json").read_text())
    assert request["script_sha256"] == hashlib.sha256(script.encode()).hexdigest()


def test_remote_attempt_never_overwrites_previous_evidence(tmp_path, monkeypatch):
    attempt = tmp_path / "linux/kept"
    attempt.mkdir(parents=True)
    receipt = attempt / "summary.json"
    receipt.write_text("original")
    monkeypatch.setattr(
        platforms.subprocess,
        "run",
        lambda *args, **kwargs: pytest.fail("existing attempt must not execute"),
    )
    arguments = argparse.Namespace(
        evidence=tmp_path,
        platform="linux",
        attempt="kept",
        action="inspect",
        timeout=10,
    )
    with pytest.raises(FileExistsError):
        platforms.execute(arguments, "print('must not run')")
    assert receipt.read_text() == "original"


@pytest.mark.parametrize(
    "builder",
    [platforms.inspect_script, platforms.prepare_script, platforms.clients_script],
)
def test_remote_preparation_scripts_are_valid_python(builder):
    compile(builder(), "<remote-preparation>", "exec")
