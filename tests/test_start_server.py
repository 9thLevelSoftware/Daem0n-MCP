"""The HTTP launchers serve the user's project, never the Daem0n-MCP checkout."""

from __future__ import annotations

import os
import subprocess
import sys
import types
from pathlib import Path
from unittest.mock import patch

import pytest

# Import before patch.dict(sys.modules) snapshots it, so the patched launcher is
# the one start_server imports in every test (not a fresh re-import).
import daem0nmcp.api.v7.launcher  # noqa: F401

REPO_ROOT = Path(__file__).resolve().parents[1]


def _launched_project_root(argv: list[str], environ: dict[str, str]) -> str:
    import start_server

    seen: list[str] = []

    def create_server(transport: str, *, host: str | None = None) -> object:
        seen.append(os.environ["DAEM0NMCP_PROJECT_ROOT"])
        return object()

    fake_server = types.ModuleType("daem0nmcp.server")
    fake_server.create_server = create_server
    with (
        patch.dict(sys.modules, {"daem0nmcp.server": fake_server}),
        patch.object(sys, "argv", ["start_server.py", *argv]),
        patch.dict(os.environ, environ, clear=True),
        patch("daem0nmcp.api.v7.launcher.run_server"),
    ):
        start_server.main()
    return seen[0]


def test_exported_project_root_survives(tmp_path):
    exported = tmp_path / "exported"
    exported.mkdir()
    root = _launched_project_root([], {"DAEM0NMCP_PROJECT_ROOT": str(exported)})
    assert root == str(exported.resolve())


def test_project_argument_wins_over_exported_root(tmp_path):
    chosen = tmp_path / "chosen"
    chosen.mkdir()
    (tmp_path / "exported").mkdir()
    root = _launched_project_root(
        ["--project", str(chosen)],
        {"DAEM0NMCP_PROJECT_ROOT": str(tmp_path / "exported")},
    )
    assert root == str(chosen.resolve())


def test_current_directory_is_the_default(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert _launched_project_root([], {}) == str(tmp_path.resolve())


def test_a_missing_project_directory_is_refused(tmp_path):
    """A typo must not create and serve an empty workspace."""
    with pytest.raises(SystemExit) as raised:
        _launched_project_root(["--project", str(tmp_path / "typo")], {})
    assert raised.value.code == 2


@pytest.mark.skipif(sys.platform != "win32", reason="Windows batch launcher")
@pytest.mark.parametrize("use_argument", [False, True])
def test_batch_launcher_passes_the_callers_project(tmp_path, use_argument):
    """The .bat changes to the repo dir, so it must capture the project first."""
    recorded, environment = _shim(tmp_path)
    project = tmp_path / "my project (x86) ^ !name!"
    project.mkdir()

    _run_batch(
        ["my project (x86) ^ !name!"] if use_argument else [],
        cwd=tmp_path if use_argument else project,
        environment=environment,
    )

    arguments = recorded.read_text(encoding="ascii").strip()
    assert arguments == f'start_server.py --port 9876 --project "{project}\\."'
    assert Path(f"{project}\\.").resolve() == project.resolve()


@pytest.mark.skipif(sys.platform != "win32", reason="Windows batch launcher")
def test_batch_launcher_leaves_an_exported_project_root_alone(tmp_path):
    recorded, environment = _shim(tmp_path)
    exported = tmp_path / "exported"
    exported.mkdir()
    environment["DAEM0NMCP_PROJECT_ROOT"] = str(exported)

    _run_batch([], cwd=tmp_path, environment=environment)

    arguments = recorded.read_text(encoding="ascii").strip()
    assert arguments == "start_server.py --port 9876"


@pytest.mark.skipif(sys.platform != "win32", reason="Windows batch launcher")
def test_batch_launcher_refuses_the_windows_system_directory(tmp_path):
    """ "Run as administrator" starts in System32; that is never the project."""
    recorded, environment = _shim(tmp_path)
    system32 = Path(os.environ["SYSTEMROOT"]) / "System32"

    completed = _run_batch([], cwd=system32, environment=environment, check=False)

    assert completed.returncode == 2
    assert not recorded.exists()


def _shim(tmp_path: Path) -> tuple[Path, dict[str, str]]:
    """Put a python.cmd on PATH that records the arguments it is given."""
    shim = tmp_path / "shim"
    shim.mkdir()
    recorded = tmp_path / "argv.txt"
    (shim / "python.cmd").write_text(f'@echo %*> "{recorded}"\r\n', encoding="ascii")
    environment = {**os.environ, "PATH": f"{shim}{os.pathsep}{os.environ['PATH']}"}
    environment.pop("DAEM0NMCP_PROJECT_ROOT", None)
    return recorded, environment


def _run_batch(
    arguments: list[str],
    *,
    cwd: Path,
    environment: dict[str, str],
    check: bool = True,
) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        ["cmd", "/c", str(REPO_ROOT / "start_daem0nmcp_server.bat"), *arguments],
        cwd=cwd,
        env=environment,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        check=check,
        timeout=30,
    )
