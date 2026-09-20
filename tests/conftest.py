# tests/conftest.py
"""
Pytest configuration for Daem0nMCP tests.
"""

import getpass
import os
import shutil
import sys
import tempfile
import uuid
from pathlib import Path

import pytest

from tests.covenant_test_support import CovenantTestWorkspace

# Register pytest-asyncio plugin
pytest_plugins = ("pytest_asyncio",)


SAFE_TMP_ROOT = Path(__file__).resolve().parent.parent / ".test_tmp"


def _safe_mkdtemp(
    suffix: str | None = None, prefix: str | None = None, dir: str | None = None
) -> str:
    base = Path(dir) if dir else SAFE_TMP_ROOT
    base.mkdir(parents=True, exist_ok=True)
    name_prefix = "tmp" if prefix is None else prefix
    name_suffix = "" if suffix is None else suffix
    unique = uuid.uuid4().hex
    path = base / f"{name_prefix}{unique}{name_suffix}"
    path.mkdir(parents=True, exist_ok=False)
    return str(path)


# Override tempfile helpers to avoid restricted temp directories on Windows.
# Keep the standard TemporaryDirectory implementation: dependencies use its
# keyword options, and its cleanup failures must remain observable.
tempfile.tempdir = str(SAFE_TMP_ROOT)
tempfile.mkdtemp = _safe_mkdtemp  # type: ignore[assignment]
os.environ["GIT_CEILING_DIRECTORIES"] = str(SAFE_TMP_ROOT)


def pytest_configure(config):
    """Configure custom pytest markers and ensure tmp directories exist."""
    config.addinivalue_line("markers", "asyncio: mark test as an asyncio test.")
    config.addinivalue_line(
        "markers", "slow: mark test as slow (requires model loading)."
    )

    # Ensure pytest's tmp_path base directory exists on all platforms
    # This fixes issues on Windows CI where getpass.getuser() returns "unknown"
    try:
        username = getpass.getuser()
    except Exception:
        username = "unknown"

    pytest_tmp_base = SAFE_TMP_ROOT / f"pytest-of-{username}"
    pytest_tmp_base.mkdir(parents=True, exist_ok=True)


@pytest.fixture
def tmp_path(tmp_path_factory):
    """Override tmp_path to use our safe temp root."""
    # Create a unique temp directory under our safe root
    path = Path(_safe_mkdtemp(prefix="pytest_"))
    yield path
    # Cleanup after test
    shutil.rmtree(path, ignore_errors=True)


@pytest.fixture
def socket_tmp_path(tmp_path):
    """A directory short enough to hold AF_UNIX sockets (104-108 byte paths).

    The repository-local tmp_path is too deep on CI runners; Windows uses named
    pipes, so it keeps the ordinary tmp_path.
    """
    if sys.platform == "win32":
        yield tmp_path
        return
    path = Path("/tmp").resolve() / f"d7-{uuid.uuid4().hex[:12]}"
    path.mkdir(mode=0o700)
    yield path
    shutil.rmtree(path, ignore_errors=True)


@pytest.fixture
def covenant_workspace_factory():
    """Create explicit isolated Covenant scopes for test workspaces."""
    return CovenantTestWorkspace


@pytest.fixture
async def covenant_compliant_project(tmp_path):
    """
    Fixture that creates a project and ensures covenant compliance.

    Returns the project path that can be used with tools requiring
    communion and/or counsel.
    """
    from daem0nmcp import server
    from daem0nmcp.database import DatabaseManager

    project_path = str(tmp_path)
    storage_path = str(tmp_path / "storage")

    # Initialize database
    db_manager = DatabaseManager(storage_path)
    await db_manager.init_db()

    # Clear any cached contexts
    server._project_contexts.clear()

    workspace = CovenantTestWorkspace(project_path)
    await workspace.brief()

    yield workspace

    # Cleanup
    await db_manager.close()
