"""Tests for git integration."""

import subprocess
import tempfile
from pathlib import Path

import pytest


class TestGitContext:
    """Test git-related functionality."""

    @pytest.fixture
    def git_repo(self):
        """Create a temporary git repository."""
        temp_dir = tempfile.mkdtemp()
        subprocess.run(
            ["git", "init"], cwd=temp_dir, capture_output=True, stdin=subprocess.DEVNULL
        )
        subprocess.run(
            ["git", "config", "user.email", "test@test.com"],
            cwd=temp_dir,
            capture_output=True,
            stdin=subprocess.DEVNULL,
        )
        subprocess.run(
            ["git", "config", "user.name", "Test"],
            cwd=temp_dir,
            capture_output=True,
            stdin=subprocess.DEVNULL,
        )

        # Create initial commit
        Path(temp_dir, "README.md").write_text("# Test")
        subprocess.run(
            ["git", "add", "."],
            cwd=temp_dir,
            capture_output=True,
            stdin=subprocess.DEVNULL,
        )
        subprocess.run(
            ["git", "commit", "-m", "Initial commit"],
            cwd=temp_dir,
            capture_output=True,
            stdin=subprocess.DEVNULL,
        )

        yield temp_dir

        import shutil

        shutil.rmtree(temp_dir, ignore_errors=True)

    def test_get_git_changes_with_project_path(self, git_repo):
        """Verify git changes are read from correct directory."""
        from daem0nmcp.server import _get_git_changes

        result = _get_git_changes(project_path=git_repo)

        assert result is not None
        assert "branch" in result

    def test_get_git_changes_detects_uncommitted(self, git_repo):
        """Verify uncommitted changes are detected."""
        from daem0nmcp.server import _get_git_changes

        # Create uncommitted change
        Path(git_repo, "new_file.txt").write_text("new content")

        result = _get_git_changes(project_path=git_repo)

        assert result is not None
        assert "uncommitted_changes" in result
        assert len(result["uncommitted_changes"]) >= 1

    def test_get_git_changes_returns_none_for_non_repo(self):
        """Verify None is returned for non-git directories."""
        from daem0nmcp.server import _get_git_changes

        with tempfile.TemporaryDirectory() as temp_dir:
            result = _get_git_changes(project_path=temp_dir)
            assert result is None
