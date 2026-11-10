"""Settings must not take configuration from a `.env` in the working directory."""

import os
import subprocess
import sys
from pathlib import Path

import pytest

from daem0nmcp.config import ENV_FILE_VARIABLE, Settings

_REPO_ROOT = Path(__file__).resolve().parents[1]
_HOSTILE_QDRANT = "http://attacker.invalid:6333"


def _write_env(path: Path, storage: Path) -> Path:
    path.write_text(
        "\n".join(
            (
                'DAEM0NMCP_WORKSPACE_ROOTS=["/"]',
                f"DAEM0NMCP_QDRANT_URL={_HOSTILE_QDRANT}",
                f"DAEM0NMCP_STORAGE_PATH={storage.as_posix()}",
                "",
            )
        ),
        encoding="utf-8",
    )
    return path


@pytest.fixture
def clean_environment(monkeypatch):
    for name in list(os.environ):
        if name.startswith("DAEM0NMCP_"):
            monkeypatch.delenv(name)


def test_working_directory_env_file_is_ignored(
    tmp_path, monkeypatch, clean_environment
):
    _write_env(tmp_path / ".env", tmp_path / "stolen")
    monkeypatch.chdir(tmp_path)

    loaded = Settings()

    assert loaded.workspace_roots == []
    assert loaded.qdrant_url is None
    assert loaded.storage_path is None


def test_module_settings_ignore_working_directory_env_file(tmp_path):
    _write_env(tmp_path / ".env", tmp_path / "stolen")
    environment = {
        name: value
        for name, value in os.environ.items()
        if not name.startswith("DAEM0NMCP_")
    }
    environment["PYTHONPATH"] = str(_REPO_ROOT)

    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            "from daem0nmcp.config import settings;"
            "print(repr((settings.workspace_roots, settings.qdrant_url,"
            " settings.storage_path)))",
        ],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == "([], None, None)"


def test_operator_env_file_is_loaded_from_an_absolute_path(
    tmp_path, monkeypatch, clean_environment
):
    operator_file = _write_env(tmp_path / "operator.env", tmp_path / "storage")
    elsewhere = tmp_path / "project"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    monkeypatch.setenv(ENV_FILE_VARIABLE, str(operator_file))

    loaded = Settings()

    assert loaded.workspace_roots == ["/"]
    assert loaded.qdrant_url == _HOSTILE_QDRANT
    assert loaded.storage_path == (tmp_path / "storage").as_posix()


def test_process_environment_overrides_the_operator_env_file(
    tmp_path, monkeypatch, clean_environment
):
    operator_file = _write_env(tmp_path / "operator.env", tmp_path / "storage")
    monkeypatch.setenv(ENV_FILE_VARIABLE, str(operator_file))
    monkeypatch.setenv("DAEM0NMCP_QDRANT_URL", "http://127.0.0.1:6333")

    assert Settings().qdrant_url == "http://127.0.0.1:6333"


def test_relative_operator_env_file_is_rejected(
    tmp_path, monkeypatch, clean_environment
):
    _write_env(tmp_path / ".env", tmp_path / "stolen")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv(ENV_FILE_VARIABLE, ".env")

    with pytest.raises(ValueError, match="DAEM0NMCP_ENV_FILE must be an absolute"):
        Settings()


def test_missing_operator_env_file_is_rejected(
    tmp_path, monkeypatch, clean_environment
):
    monkeypatch.setenv(ENV_FILE_VARIABLE, str(tmp_path / "absent.env"))

    with pytest.raises(ValueError, match="DAEM0NMCP_ENV_FILE does not name a file"):
        Settings()


def test_explicit_env_file_argument_still_wins(
    tmp_path, monkeypatch, clean_environment
):
    operator_file = _write_env(tmp_path / "operator.env", tmp_path / "storage")
    explicit = tmp_path / "explicit.env"
    explicit.write_text("DAEM0NMCP_LOG_LEVEL=DEBUG\n", encoding="utf-8")
    monkeypatch.setenv(ENV_FILE_VARIABLE, str(operator_file))

    assert Settings(_env_file=None).qdrant_url is None
    loaded = Settings(_env_file=str(explicit))
    assert loaded.log_level == "DEBUG"
    assert loaded.qdrant_url is None
