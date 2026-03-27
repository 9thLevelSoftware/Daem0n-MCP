"""Tests for enhanced bootstrap functionality."""

import json
import subprocess
from unittest.mock import AsyncMock, MagicMock

import pytest

from daem0nmcp.server import (
    _extract_architecture,
    _extract_conventions,
    _extract_entry_points,
    _extract_project_identity,
    _extract_project_instructions,
    _scan_todos_for_bootstrap,
)


class TestExtractProjectIdentity:
    """Tests for _extract_project_identity extractor."""

    def test_extracts_package_json(self, tmp_path):
        """Should extract name, description, and dependencies from package.json."""
        package = {
            "name": "my-app",
            "version": "1.0.0",
            "description": "A test application",
            "scripts": {"test": "jest", "build": "webpack"},
            "dependencies": {"react": "^18.0.0", "lodash": "^4.17.0"},
        }
        (tmp_path / "package.json").write_text(json.dumps(package))

        result = _extract_project_identity(str(tmp_path))

        assert result is not None
        assert "my-app" in result
        assert "A test application" in result
        assert "react" in result

    def test_extracts_pyproject_toml(self, tmp_path):
        """Should extract project info from pyproject.toml."""
        pyproject = """
[project]
name = "my-python-app"
version = "2.0.0"
description = "A Python application"
dependencies = ["fastapi", "sqlalchemy"]
"""
        (tmp_path / "pyproject.toml").write_text(pyproject)

        result = _extract_project_identity(str(tmp_path))

        assert result is not None
        assert "my-python-app" in result
        assert "A Python application" in result

    def test_returns_none_when_no_manifest(self, tmp_path):
        """Should return None when no manifest file exists."""
        result = _extract_project_identity(str(tmp_path))
        assert result is None

    def test_package_json_takes_priority(self, tmp_path):
        """When multiple manifests exist, package.json wins."""
        (tmp_path / "package.json").write_text('{"name": "node-app"}')
        (tmp_path / "pyproject.toml").write_text('[project]\nname = "python-app"')

        result = _extract_project_identity(str(tmp_path))

        assert "node-app" in result


class TestExtractArchitecture:
    """Tests for _extract_architecture extractor."""

    def test_extracts_readme_content(self, tmp_path):
        """Should extract first 2000 chars from README.md."""
        readme = "# My Project\n\nThis is a test project.\n\n## Features\n- Feature 1\n- Feature 2"
        (tmp_path / "README.md").write_text(readme)

        result = _extract_architecture(str(tmp_path))

        assert result is not None
        assert "My Project" in result
        assert "Feature 1" in result

    def test_includes_directory_structure(self, tmp_path):
        """Should include top-level directory structure."""
        (tmp_path / "src").mkdir()
        (tmp_path / "tests").mkdir()
        (tmp_path / "docs").mkdir()
        (tmp_path / "README.md").write_text("# Test")

        result = _extract_architecture(str(tmp_path))

        assert "src" in result
        assert "tests" in result

    def test_excludes_noise_directories(self, tmp_path):
        """Should exclude node_modules, .git, etc."""
        (tmp_path / "src").mkdir()
        (tmp_path / "node_modules").mkdir()
        (tmp_path / ".git").mkdir()

        result = _extract_architecture(str(tmp_path))

        assert result is not None
        assert "node_modules" not in result
        assert ".git" not in result

    def test_returns_structure_only_without_readme(self, tmp_path):
        """Should return directory structure even without README."""
        (tmp_path / "src").mkdir()
        (tmp_path / "lib").mkdir()

        result = _extract_architecture(str(tmp_path))

        assert result is not None
        assert "src" in result


class TestExtractConventions:
    """Tests for _extract_conventions extractor."""

    def test_extracts_contributing_guidelines(self, tmp_path):
        """Should extract content from CONTRIBUTING.md."""
        contributing = "# Contributing\n\n## Code Style\nUse 4 spaces for indentation."
        (tmp_path / "CONTRIBUTING.md").write_text(contributing)

        result = _extract_conventions(str(tmp_path))

        assert result is not None
        assert "Code Style" in result

    def test_detects_eslint_config(self, tmp_path):
        """Should detect ESLint configuration."""
        (tmp_path / ".eslintrc.json").write_text('{"extends": "airbnb"}')

        result = _extract_conventions(str(tmp_path))

        assert result is not None
        assert "eslint" in result.lower()

    def test_detects_ruff_config(self, tmp_path):
        """Should detect Ruff configuration."""
        (tmp_path / "ruff.toml").write_text("[tool.ruff]\nline-length = 88")

        result = _extract_conventions(str(tmp_path))

        assert result is not None
        assert "ruff" in result.lower()

    def test_returns_none_when_no_configs(self, tmp_path):
        """Should return None when no convention configs found."""
        result = _extract_conventions(str(tmp_path))
        assert result is None


class TestExtractEntryPoints:
    """Tests for _extract_entry_points extractor."""

    def test_finds_python_entry_points(self, tmp_path):
        """Should find main.py, app.py, etc."""
        (tmp_path / "main.py").write_text("# main")
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "app.py").write_text("# app")

        result = _extract_entry_points(str(tmp_path))

        assert result is not None
        assert "main.py" in result

    def test_finds_node_entry_points(self, tmp_path):
        """Should find index.js, index.ts."""
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "index.ts").write_text("// index")

        result = _extract_entry_points(str(tmp_path))

        assert result is not None
        assert "index.ts" in result

    def test_finds_cli_entry_points(self, tmp_path):
        """Should find cli.py, __main__.py."""
        (tmp_path / "myapp").mkdir()
        (tmp_path / "myapp" / "__main__.py").write_text("# main")
        (tmp_path / "myapp" / "cli.py").write_text("# cli")

        result = _extract_entry_points(str(tmp_path))

        assert result is not None
        assert "__main__.py" in result

    def test_returns_none_when_no_entry_points(self, tmp_path):
        """Should return None when no entry points found."""
        (tmp_path / "utils.py").write_text("# utils")

        result = _extract_entry_points(str(tmp_path))

        assert result is None


class TestScanTodosForBootstrap:
    """Tests for _scan_todos_for_bootstrap extractor."""

    def test_finds_todo_comments(self, tmp_path):
        """Should find TODO comments in code files."""
        (tmp_path / "code.py").write_text("# TODO: Fix this later\nx = 1")

        result = _scan_todos_for_bootstrap(str(tmp_path))

        assert result is not None
        assert "TODO" in result
        assert "Fix this later" in result

    def test_finds_fixme_comments(self, tmp_path):
        """Should find FIXME comments."""
        (tmp_path / "code.py").write_text("# FIXME: This is broken\nx = 1")

        result = _scan_todos_for_bootstrap(str(tmp_path))

        assert result is not None
        assert "FIXME" in result

    def test_limits_results(self, tmp_path):
        """Should limit to 20 items."""
        code = "\n".join(f"# TODO: Item {i}" for i in range(30))
        (tmp_path / "code.py").write_text(code)

        result = _scan_todos_for_bootstrap(str(tmp_path), limit=20)

        # Count TODOs in result
        assert result.count("TODO:") <= 20

    def test_returns_none_when_no_todos(self, tmp_path):
        """Should return None when no TODOs found."""
        (tmp_path / "code.py").write_text("x = 1\ny = 2")

        result = _scan_todos_for_bootstrap(str(tmp_path))

        assert result is None


class TestExtractProjectInstructions:
    """Tests for _extract_project_instructions extractor."""

    def test_extracts_claude_md(self, tmp_path):
        """Should extract content from CLAUDE.md."""
        (tmp_path / "CLAUDE.md").write_text("# Instructions\n\nUse TypeScript.")

        result = _extract_project_instructions(str(tmp_path))

        assert result is not None
        assert "CLAUDE.md" in result
        assert "TypeScript" in result

    def test_extracts_agents_md(self, tmp_path):
        """Should extract content from AGENTS.md."""
        (tmp_path / "AGENTS.md").write_text("# Agent Config\n\nBe concise.")

        result = _extract_project_instructions(str(tmp_path))

        assert result is not None
        assert "AGENTS.md" in result

    def test_returns_none_when_no_files(self, tmp_path):
        """Should return None when no instruction files exist."""
        result = _extract_project_instructions(str(tmp_path))
        assert result is None


class TestBootstrapProjectContext:
    """Integration tests for _bootstrap_project_context."""

    @pytest.mark.asyncio
    async def test_creates_multiple_memories(self, tmp_path):
        """Should create memories for each available source."""
        # Set up project files
        (tmp_path / "package.json").write_text(
            '{"name": "test-app", "description": "Test"}'
        )
        (tmp_path / "README.md").write_text("# Test App\n\nA test application.")
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "index.ts").write_text("// main")
        (tmp_path / "code.py").write_text("# TODO: Fix this")

        # Mock the context
        mock_memory_manager = AsyncMock()
        mock_memory_manager.remember = AsyncMock(return_value={"id": 1})

        mock_ctx = MagicMock()
        mock_ctx.project_path = str(tmp_path)
        mock_ctx.memory_manager = mock_memory_manager

        from daem0nmcp.server import _bootstrap_project_context

        result = await _bootstrap_project_context(mock_ctx)

        assert result["bootstrapped"] is True
        assert (
            result["memories_created"] >= 3
        )  # At least identity, architecture, entry_points
        assert "sources" in result
        assert result["sources"].get("project_identity") == "ingested"

    @pytest.mark.asyncio
    async def test_graceful_fallback_empty_project(self, tmp_path):
        """Should handle empty project gracefully."""
        mock_memory_manager = AsyncMock()
        mock_memory_manager.remember = AsyncMock(return_value={"id": 1})

        mock_ctx = MagicMock()
        mock_ctx.project_path = str(tmp_path)
        mock_ctx.memory_manager = mock_memory_manager

        from daem0nmcp.server import _bootstrap_project_context

        result = await _bootstrap_project_context(mock_ctx)

        assert result["bootstrapped"] is True
        assert "sources" in result


class TestBootstrapIntegration:
    """End-to-end integration tests for bootstrap."""

    @pytest.mark.asyncio
    async def test_full_bootstrap_flow(self, tmp_path):
        """Test that all extractors work together on a realistic project."""
        # Create realistic project structure

        # 1. package.json with name, description, scripts, dependencies
        package_json = {
            "name": "test-web-app",
            "version": "1.0.0",
            "description": "A realistic test web application",
            "scripts": {"dev": "vite", "build": "tsc && vite build", "test": "jest"},
            "dependencies": {
                "react": "^18.2.0",
                "react-dom": "^18.2.0",
                "axios": "^1.4.0",
            },
            "devDependencies": {
                "typescript": "^5.0.0",
                "vite": "^4.3.0",
                "jest": "^29.5.0",
            },
        }
        (tmp_path / "package.json").write_text(json.dumps(package_json, indent=2))

        # 2. README.md with overview
        readme_content = """# Test Web App

A realistic web application for testing bootstrap functionality.

## Features
- React-based UI
- TypeScript for type safety
- Vite for fast development
- Jest for testing

## Architecture
This project follows a standard React application structure with:
- Component-based architecture
- Centralized state management
- API integration layer
"""
        (tmp_path / "README.md").write_text(readme_content)

        # 3. src/index.ts as entry point
        src_dir = tmp_path / "src"
        src_dir.mkdir()
        (src_dir / "index.ts").write_text("""// Application entry point
import React from 'react';
import ReactDOM from 'react-dom/client';
import App from './App';

const root = ReactDOM.createRoot(document.getElementById('root')!);
root.render(<App />);
""")

        # 4. .eslintrc.json for conventions
        eslint_config = {
            "extends": ["airbnb", "airbnb-typescript"],
            "rules": {
                "indent": ["error", 2],
                "quotes": ["error", "single"],
                "semi": ["error", "always"],
            },
        }
        (tmp_path / ".eslintrc.json").write_text(json.dumps(eslint_config, indent=2))

        # 5. CLAUDE.md for project instructions
        claude_md = """# Project Instructions

## Development Guidelines
- Use TypeScript for all new code
- Follow Airbnb style guide
- Write tests for all new features
- Keep components small and focused

## Architecture Decisions
- Use React 18 with hooks
- Prefer functional components over class components
- Use axios for API calls
"""
        (tmp_path / "CLAUDE.md").write_text(claude_md)

        # 6. A file with TODO comment for known issues
        (src_dir / "api.ts").write_text("""// API integration layer
// TODO: Add retry logic for failed requests
// FIXME: Handle network timeouts properly

export async function fetchData(url: string) {
    // Implementation here
}
""")

        # 7. Additional directory structure
        (tmp_path / "tests").mkdir()
        (tmp_path / "docs").mkdir()

        # Initialize git repo with at least one commit
        subprocess.run(
            ["git", "init"],
            cwd=str(tmp_path),
            capture_output=True,
            check=True,
            stdin=subprocess.DEVNULL,
        )
        subprocess.run(
            ["git", "config", "user.email", "test@test.com"],
            cwd=str(tmp_path),
            capture_output=True,
            check=True,
            stdin=subprocess.DEVNULL,
        )
        subprocess.run(
            ["git", "config", "user.name", "Test User"],
            cwd=str(tmp_path),
            capture_output=True,
            check=True,
            stdin=subprocess.DEVNULL,
        )
        subprocess.run(
            ["git", "add", "."],
            cwd=str(tmp_path),
            capture_output=True,
            check=True,
            stdin=subprocess.DEVNULL,
        )
        subprocess.run(
            ["git", "commit", "-m", "Initial commit"],
            cwd=str(tmp_path),
            capture_output=True,
            check=True,
            stdin=subprocess.DEVNULL,
        )

        # Run all extractors directly and verify each returns expected content

        # Test project_identity extractor
        identity = _extract_project_identity(str(tmp_path))
        assert identity is not None
        assert "test-web-app" in identity
        assert "A realistic test web application" in identity
        assert "react" in identity.lower()
        assert "typescript" in identity.lower()

        # Test architecture extractor
        architecture = _extract_architecture(str(tmp_path))
        assert architecture is not None
        assert "Test Web App" in architecture
        assert "React-based UI" in architecture
        assert "src" in architecture
        assert "tests" in architecture
        assert "docs" in architecture
        # Should exclude node_modules and .git
        assert "node_modules" not in architecture
        assert ".git" not in architecture

        # Test conventions extractor
        conventions = _extract_conventions(str(tmp_path))
        assert conventions is not None
        assert "eslint" in conventions.lower()
        # The extractor reports tool names, not config content
        assert "code tools configured" in conventions.lower()

        # Test project_instructions extractor
        instructions = _extract_project_instructions(str(tmp_path))
        assert instructions is not None
        assert "CLAUDE.md" in instructions
        assert "TypeScript" in instructions
        assert "Airbnb style guide" in instructions

        # Test entry_points extractor
        entry_points = _extract_entry_points(str(tmp_path))
        assert entry_points is not None
        assert "index.ts" in entry_points
        assert "entry point" in entry_points.lower() or "src" in entry_points

        # Test known_issues extractor
        known_issues = _scan_todos_for_bootstrap(str(tmp_path))
        assert known_issues is not None
        assert "TODO" in known_issues
        assert "retry logic" in known_issues
        assert "FIXME" in known_issues
        assert "network timeouts" in known_issues
