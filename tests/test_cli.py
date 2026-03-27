"""
Tests for the Daem0nMCP CLI.

These tests verify the command-line interface functionality using subprocess calls.
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest


@pytest.fixture
def temp_project():
    """Create a temporary project directory for testing."""
    temp_dir = tempfile.mkdtemp()
    yield temp_dir
    shutil.rmtree(temp_dir, ignore_errors=True)


@pytest.fixture
def cli_env(temp_project):
    """Environment for CLI commands pointing to temp project."""
    env = os.environ.copy()
    env["DAEM0NMCP_PROJECT_ROOT"] = temp_project
    return env


def run_cli(*args, env=None, project_path=None):
    """Run CLI command and return result.

    Uses Popen for better handle management on Windows.
    """
    cmd = [sys.executable, "-m", "daem0nmcp.cli"]
    if project_path:
        cmd.extend(["--project-path", project_path])
    cmd.extend(args)

    # Use Popen with explicit handle management to avoid Windows handle exhaustion
    process = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        stdin=subprocess.DEVNULL,
        text=True,
        env=env,
    )
    stdout, stderr = process.communicate()

    # Create a result object similar to subprocess.run's CompletedProcess
    result = subprocess.CompletedProcess(cmd, process.returncode, stdout, stderr)
    return result


class TestCLIHelp:
    """Tests for CLI help and basic functionality."""

    def test_help_displays(self):
        """Test that --help works."""
        result = run_cli("--help")
        assert result.returncode == 0
        assert "Daem0nMCP CLI" in result.stdout or "usage:" in result.stdout.lower()

    def test_no_command_shows_help(self):
        """Test that running without command shows help."""
        result = run_cli()
        assert result.returncode == 1
        # Should show usage or help text


class TestBriefingCommand:
    """Tests for the briefing command."""

    def test_briefing_json_output(self, temp_project):
        """Test briefing command with JSON output."""
        result = run_cli("--json", "briefing", project_path=temp_project)
        assert result.returncode == 0

        data = json.loads(result.stdout)
        assert "total_memories" in data
        assert "by_category" in data

    def test_briefing_text_output(self, temp_project):
        """Test briefing command with text output."""
        result = run_cli("briefing", project_path=temp_project)
        assert result.returncode == 0
        assert "Total memories:" in result.stdout or "memories" in result.stdout.lower()


class TestStatusCommand:
    """Tests for the status command."""

    def test_status_json_output(self, temp_project):
        """Test status command with JSON output."""
        result = run_cli("--json", "status", project_path=temp_project)
        assert result.returncode == 0

        data = json.loads(result.stdout)
        assert "pending_decisions" in data
        assert "total_memories" in data

    def test_status_text_output(self, temp_project):
        """Test status command with text output."""
        result = run_cli("status", project_path=temp_project)
        assert result.returncode == 0
        assert (
            "Pending decisions" in result.stdout or "pending" in result.stdout.lower()
        )


class TestMigrateCommand:
    """Tests for the migrate command."""

    def test_migrate_creates_database(self, temp_project):
        """Test that migrate command creates database."""
        result = run_cli("--json", "migrate", project_path=temp_project)
        assert result.returncode == 0

        data = json.loads(result.stdout)
        assert "schema_migrations" in data
        assert "database" in data

    def test_migrate_idempotent(self, temp_project):
        """Test that running migrate twice is safe."""
        # First run
        result1 = run_cli("--json", "migrate", project_path=temp_project)
        assert result1.returncode == 0

        # Second run should also succeed
        result2 = run_cli("--json", "migrate", project_path=temp_project)
        assert result2.returncode == 0

        data = json.loads(result2.stdout)
        assert data.get("up_to_date", False) or data.get("schema_migrations", 0) == 0


class TestScanTodosCommand:
    """Tests for the scan-todos command."""

    def test_scan_todos_empty_directory(self, temp_project):
        """Test scanning empty directory."""
        result = run_cli(
            "--json", "scan-todos", "--path", temp_project, project_path=temp_project
        )
        assert result.returncode == 0

        data = json.loads(result.stdout)
        assert "total" in data
        assert data["total"] == 0

    def test_scan_todos_finds_todo(self, temp_project):
        """Test scanning finds TODO comments."""
        # Create a file with TODO
        test_file = Path(temp_project) / "test.py"
        test_file.write_text("# TODO: Fix this bug\nprint('hello')\n")

        result = run_cli(
            "--json", "scan-todos", "--path", temp_project, project_path=temp_project
        )
        assert result.returncode == 0

        data = json.loads(result.stdout)
        assert data["total"] >= 1
        assert any("TODO" in t.get("type", "") for t in data.get("todos", []))

    def test_scan_todos_finds_fixme(self, temp_project):
        """Test scanning finds FIXME comments."""
        test_file = Path(temp_project) / "test.py"
        test_file.write_text("# FIXME: Critical issue here\nprint('hello')\n")

        result = run_cli(
            "--json", "scan-todos", "--path", temp_project, project_path=temp_project
        )
        assert result.returncode == 0

        data = json.loads(result.stdout)
        assert data["total"] >= 1


class TestCheckCommand:
    """Tests for the check command."""

    def test_check_nonexistent_file(self, temp_project):
        """Test checking a file that doesn't exist in memory."""
        result = run_cli("--json", "check", "nonexistent.py", project_path=temp_project)
        assert result.returncode == 0

        data = json.loads(result.stdout)
        assert "file" in data
        assert data["file"] == "nonexistent.py"
        assert "warnings" in data
        assert "must_do" in data
        assert "must_not" in data

    def test_check_existing_file(self, temp_project):
        """Test checking an actual file."""
        test_file = Path(temp_project) / "mycode.py"
        test_file.write_text("print('hello')\n")

        result = run_cli("--json", "check", str(test_file), project_path=temp_project)
        assert result.returncode == 0

        data = json.loads(result.stdout)
        assert "file" in data


class TestRecordOutcomeCommand:
    """Tests for the record-outcome command."""

    def test_record_outcome_with_worked_flag(self, temp_project):
        """Test recording outcome with --worked flag."""
        result = run_cli(
            "record-outcome",
            "99999",
            "This is the outcome",
            "--worked",
            project_path=temp_project,
        )
        # Command should complete (note: doesn't validate memory existence)
        assert result.returncode == 0
        assert "outcome" in result.stdout.lower()

    def test_record_outcome_with_failed_flag(self, temp_project):
        """Test recording outcome with --failed flag."""
        result = run_cli(
            "record-outcome",
            "99999",
            "This did not work",
            "--failed",
            project_path=temp_project,
        )
        # Command should complete
        assert result.returncode == 0

    def test_record_outcome_requires_worked_or_failed(self, temp_project):
        """Test that record-outcome requires --worked or --failed."""
        result = run_cli(
            "record-outcome", "1", "Outcome text", project_path=temp_project
        )
        # Should fail without --worked or --failed
        assert (
            result.returncode != 0
            or "worked" in result.stdout.lower()
            or "failed" in result.stdout.lower()
        )


class TestHooksCommands:
    """Tests for install-hooks and uninstall-hooks commands."""

    def test_install_hooks_no_git_repo(self, temp_project):
        """Test install-hooks fails gracefully without git repo."""
        result = run_cli("install-hooks", project_path=temp_project)
        # Should fail because no .git directory
        assert (
            result.returncode != 0
            or "git" in result.stdout.lower()
            or "git" in result.stderr.lower()
        )

    def test_install_hooks_in_git_repo(self, temp_project):
        """Test install-hooks in a git repository."""
        # Initialize git repo (use Popen for Windows handle safety)
        proc = subprocess.Popen(
            ["git", "init"],
            cwd=temp_project,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            stdin=subprocess.DEVNULL,
        )
        proc.communicate()

        result = run_cli("install-hooks", project_path=temp_project)
        # Should succeed or indicate hooks were installed
        assert (
            result.returncode == 0
            or "installed" in result.stdout.lower()
            or "created" in result.stdout.lower()
        )

    def test_uninstall_hooks_no_git_repo(self, temp_project):
        """Test uninstall-hooks fails gracefully without git repo."""
        result = run_cli("uninstall-hooks", project_path=temp_project)
        # Should fail or indicate no hooks to remove
        assert (
            result.returncode != 0
            or "no" in result.stdout.lower()
            or "git" in result.stderr.lower()
        )


class TestProjectPathOption:
    """Tests for the --project-path global option."""

    def test_project_path_overrides_default(self, temp_project):
        """Test that --project-path correctly sets project root."""
        result = run_cli("--json", "briefing", project_path=temp_project)
        assert result.returncode == 0

        # Verify output is valid JSON
        data = json.loads(result.stdout)
        assert isinstance(data, dict)

    def test_different_projects_isolated(self):
        """Test that different project paths have isolated data."""
        temp1 = tempfile.mkdtemp()
        temp2 = tempfile.mkdtemp()

        try:
            # Run briefing on both projects
            result1 = run_cli("--json", "briefing", project_path=temp1)
            result2 = run_cli("--json", "briefing", project_path=temp2)

            assert result1.returncode == 0
            assert result2.returncode == 0

            # Both should return valid data
            data1 = json.loads(result1.stdout)
            data2 = json.loads(result2.stdout)

            assert isinstance(data1, dict)
            assert isinstance(data2, dict)
        finally:
            shutil.rmtree(temp1, ignore_errors=True)
            shutil.rmtree(temp2, ignore_errors=True)


class TestJSONOutputOption:
    """Tests for the --json global option."""

    def test_json_output_is_valid_json(self, temp_project):
        """Test that --json produces valid JSON output."""
        commands = [
            ["briefing"],
            ["status"],
            ["scan-todos", "--path", temp_project],
        ]

        for cmd in commands:
            result = run_cli("--json", *cmd, project_path=temp_project)
            assert result.returncode == 0, f"Command {cmd} failed: {result.stderr}"

            try:
                data = json.loads(result.stdout)
                assert isinstance(data, dict), (
                    f"Expected dict for {cmd}, got {type(data)}"
                )
            except json.JSONDecodeError as e:
                pytest.fail(f"Invalid JSON from {cmd}: {e}\nOutput: {result.stdout}")


class TestErrorHandling:
    """Tests for error handling in CLI."""

    def test_invalid_command(self):
        """Test that invalid command shows error."""
        result = run_cli("invalid-command")
        assert result.returncode != 0

    def test_missing_required_args(self, temp_project):
        """Test that missing required args shows error."""
        # check requires filepath
        result = run_cli("check", project_path=temp_project)
        assert result.returncode != 0

        # record-outcome requires memory_id and outcome
        result = run_cli("record-outcome", project_path=temp_project)
        assert result.returncode != 0
