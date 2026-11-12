"""Exercise the public installer command without initializing a workspace store."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _run(arguments: list[str], home: Path) -> subprocess.CompletedProcess[str]:
    environment = {
        name: value
        for name, value in os.environ.items()
        if not name.startswith("DAEM0NMCP_")
    }
    environment.update(HOME=str(home), USERPROFILE=str(home), PYTHONIOENCODING="utf-8")
    return subprocess.run(
        [sys.executable, "-m", "daem0nmcp.cli", "--json", *arguments],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
        check=False,
    )


@pytest.mark.parametrize("project_argument_first", [True, False])
def test_dry_run_respects_both_project_argument_positions(
    tmp_path: Path, project_argument_first: bool
) -> None:
    project = tmp_path / "project with spaces"
    project.mkdir()
    sentinel = project / "keep.txt"
    sentinel.write_bytes(b"existing project")
    project_arguments = ["--project-path", str(project)]
    command = ["install-client", "pi", "--dry-run"]
    arguments = (
        project_arguments + command
        if project_argument_first
        else command + project_arguments
    )

    result = _run(arguments, tmp_path)

    assert result.returncode == 0, result.stderr
    response = json.loads(result.stdout)
    assert response["ok"] is True
    assert response["client"] == "pi"
    assert Path(response["project_path"]) == project.resolve()
    assert list(project.iterdir()) == [sentinel]
    assert sentinel.read_bytes() == b"existing project"


def test_observed_opencode_version_reaches_installer(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()

    result = _run(
        [
            "install-client",
            "opencode",
            "--client-version",
            "1.18.21",
            "--project-path",
            str(project),
            "--dry-run",
        ],
        tmp_path,
    )

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["ok"] is True
    assert list(project.iterdir()) == []


def test_config_failure_is_json_and_preserves_input(tmp_path: Path) -> None:
    project = tmp_path / "project"
    configuration = project / ".pi" / "mcp.json"
    configuration.parent.mkdir(parents=True)
    original = b'{"mcpServers": [], "privateValue": "not-for-the-console"}'
    configuration.write_bytes(original)

    result = _run(["--project-path", str(project), "install-client", "pi"], tmp_path)

    assert result.returncode == 1
    response = json.loads(result.stdout)
    assert response["ok"] is False
    assert response["error"]["code"]
    assert "not-for-the-console" not in result.stdout + result.stderr
    assert configuration.read_bytes() == original
    assert not (project / "AGENTS.md").exists()
    assert not (project / ".daem0nmcp").exists()


def test_human_failure_reports_changed_files_and_recovery_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from daem0nmcp import cli, client_install

    configuration = tmp_path / ".pi" / "mcp.json"
    backup = tmp_path / ".daem0nmcp" / "client-install" / "mcp.backup"
    pending = tmp_path / ".pi" / "staged.tmp"

    def fail_install(*args: object, **kwargs: object) -> None:
        raise client_install.ClientInstallError(
            "PATH_CHANGED",
            "The configuration directory changed during publication.",
            files=[
                {
                    "path": str(configuration),
                    "action": "updated",
                    "backup": str(backup),
                },
                {"path": str(pending), "action": "cleanup_pending"},
            ],
        )

    monkeypatch.setattr(client_install, "install_client", fail_install)
    # main() assigns this directly; register it for fixture cleanup too.
    monkeypatch.setenv("DAEM0NMCP_PROJECT_ROOT", str(tmp_path))
    monkeypatch.setattr(
        sys,
        "argv",
        ["daem0nmcp", "--project-path", str(tmp_path), "install-client", "pi"],
    )

    with pytest.raises(SystemExit) as stopped:
        cli.main()

    assert stopped.value.code == 1
    output = capsys.readouterr().out
    assert "PATH_CHANGED" in output
    assert f"updated: {configuration}" in output
    assert f"Backup: {backup}" in output
    assert f"cleanup_pending: {pending}" in output
