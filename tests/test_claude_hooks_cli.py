"""Tests for the CLI integration of install/uninstall-claude-hooks commands."""

import json

import pytest

from daem0nmcp.claude_hooks.install import install_claude_hooks, uninstall_claude_hooks


@pytest.fixture
def fake_settings(tmp_path, monkeypatch):
    """Redirect settings to a temp dir and return the settings file path."""
    settings_dir = tmp_path / ".claude"
    settings_dir.mkdir()
    settings_file = settings_dir / "settings.json"

    monkeypatch.setattr(
        "daem0nmcp.claude_hooks.install._settings_path",
        lambda: settings_file,
    )
    return settings_file


def test_cli_install_claude_hooks_dry_run(fake_settings):
    ok, msg = install_claude_hooks(dry_run=True)
    assert ok
    assert "[dry-run]" in msg
    assert "PreToolUse" in msg


def test_cli_install_claude_hooks_json(fake_settings):
    ok, msg = install_claude_hooks(dry_run=True)
    assert ok
    output = json.dumps({"success": ok, "message": msg})
    data = json.loads(output)
    assert data["success"] is True


def test_cli_uninstall_claude_hooks_dry_run(fake_settings):
    # Nothing installed -> "No Daem0n hooks found"
    fake_settings.write_text(json.dumps({"hooks": {}}))
    ok, msg = uninstall_claude_hooks(dry_run=True)
    assert ok
    assert "No Daem0n hooks found" in msg


def _cli(monkeypatch, capsys, *argv: str) -> tuple[int, str]:
    from daem0nmcp import cli

    monkeypatch.setattr("sys.argv", ["daem0nmcp.cli", *argv])
    with pytest.raises(SystemExit) as exc_info:
        cli.main()
    return exc_info.value.code, capsys.readouterr().out


def test_cli_uninstall_accepts_project_path_after_the_subcommand(
    fake_settings, tmp_path, monkeypatch, capsys
):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("DAEM0NMCP_PROJECT_ROOT", "unset-by-test")
    project = tmp_path / "project"
    project.mkdir()

    code, _out = _cli(
        monkeypatch, capsys, "--project-path", str(project), "install-claude-hooks"
    )
    assert code == 0
    bridges = home / ".daem0nmcp" / "edit-bridges"
    credential_dirs = [p for p in bridges.iterdir() if p.name != "run"]
    assert len(credential_dirs) == 1

    code, out = _cli(
        monkeypatch,
        capsys,
        "uninstall-claude-hooks",
        "--project-path",
        str(project),
        "--dry-run",
        "--remove-credentials",
    )
    assert code == 0
    assert f"[dry-run] Would remove {credential_dirs[0]}" in out
    assert credential_dirs[0].exists()

    code, out = _cli(
        monkeypatch,
        capsys,
        "uninstall-claude-hooks",
        "--project-path",
        str(project),
        "--remove-credentials",
    )
    assert code == 0, out
    assert not credential_dirs[0].exists()
    local = json.loads(
        (project / ".claude" / "settings.local.json").read_text(encoding="utf-8")
    )
    assert local == {}


def test_cli_remove_credentials_defaults_to_the_current_directory(
    fake_settings, tmp_path, monkeypatch, capsys
):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.chdir(project)

    assert _cli(monkeypatch, capsys, "install-claude-hooks")[0] == 0
    code, out = _cli(
        monkeypatch, capsys, "uninstall-claude-hooks", "--remove-credentials"
    )

    assert code == 0, out
    assert "edit-bridges" in out
    bridges = home / ".daem0nmcp" / "edit-bridges"
    assert [p for p in bridges.iterdir() if p.name != "run"] == []
