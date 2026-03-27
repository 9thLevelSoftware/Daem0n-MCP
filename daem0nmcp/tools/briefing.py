"""Briefing and session tools: get_briefing, context_check, health, etc."""

import json
import logging
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

try:
    from .. import __version__, vectors
    from ..context_manager import (
        ProjectContext,
        _default_project_path,
        _missing_project_path_error,
        _project_contexts,
        get_project_context,
    )
    from ..logging_config import with_request_id
    from ..mcp_instance import mcp
    from ..models import CodeEntity, Memory, Rule
except ImportError:
    from daem0nmcp import __version__, vectors
    from daem0nmcp.context_manager import (
        ProjectContext,
        _default_project_path,
        _missing_project_path_error,
        _project_contexts,
        get_project_context,
    )
    from daem0nmcp.logging_config import with_request_id
    from daem0nmcp.mcp_instance import mcp
    from daem0nmcp.models import CodeEntity, Memory, Rule

from sqlalchemy import func, select

from ._deprecation import add_deprecation

logger = logging.getLogger(__name__)


# Directories to exclude when scanning project structure
BOOTSTRAP_EXCLUDED_DIRS = {
    "node_modules",
    ".git",
    "__pycache__",
    ".venv",
    "venv",
    "dist",
    "build",
    ".next",
    "target",
    ".idea",
    ".vscode",
    ".eggs",
    "eggs",
    ".tox",
    ".nox",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    "htmlcov",
    ".coverage",
    "site-packages",
}


def _extract_project_identity(project_path: str) -> str | None:
    """
    Extract project identity from manifest files.

    Tries manifests in priority order:
    1. package.json (Node.js)
    2. pyproject.toml (Python)
    3. Cargo.toml (Rust)
    4. go.mod (Go)

    Returns:
        Formatted string with project name, description, and key dependencies,
        or None if no manifest found.
    """
    root = Path(project_path)

    # Try package.json first
    package_json = root / "package.json"
    if package_json.exists():
        try:
            data = json.loads(package_json.read_text(encoding="utf-8", errors="ignore"))
            parts = []
            if data.get("name"):
                parts.append(f"Project: {data['name']}")
            if data.get("description"):
                parts.append(f"Description: {data['description']}")
            if data.get("scripts"):
                scripts = ", ".join(list(data["scripts"].keys())[:5])
                parts.append(f"Scripts: {scripts}")
            deps = list(data.get("dependencies", {}).keys())[:10]
            dev_deps = list(data.get("devDependencies", {}).keys())[:5]
            if deps:
                parts.append(f"Dependencies: {', '.join(deps)}")
            if dev_deps:
                parts.append(f"Dev dependencies: {', '.join(dev_deps)}")
            if parts:
                return "Tech stack (from package.json):\n" + "\n".join(parts)
        except Exception as e:
            logger.debug(f"Failed to parse package.json: {e}")

    # Try pyproject.toml
    pyproject = root / "pyproject.toml"
    if pyproject.exists():
        try:
            content = pyproject.read_text(encoding="utf-8", errors="ignore")
            parts = []
            for line in content.split("\n"):
                line = line.strip()
                if line.startswith("name = "):
                    project_name = line.split("=", 1)[1].strip().strip('"')
                    parts.append(f"Project: {project_name}")
                elif line.startswith("description = "):
                    description = line.split("=", 1)[1].strip().strip('"')
                    parts.append(f"Description: {description}")
            # Extract dependencies list
            if "dependencies = [" in content:
                start = content.find("dependencies = [")
                end = content.find("]", start)
                if end > start:
                    deps_str = content[start : end + 1]
                    deps = [
                        d.strip()
                        .strip('"')
                        .strip("'")
                        .split("[")[0]
                        .split(">")[0]
                        .split("<")[0]
                        .split("=")[0]
                        .strip()
                        for d in deps_str.split("[")[1].split("]")[0].split(",")
                        if d.strip()
                    ]
                    deps = [d for d in deps if d]  # Remove empty strings
                    if deps:
                        parts.append(f"Dependencies: {', '.join(deps[:10])}")
            if parts:
                return "Tech stack (from pyproject.toml):\n" + "\n".join(parts)
        except Exception as e:
            logger.debug(f"Failed to parse pyproject.toml: {e}")

    # Try Cargo.toml
    cargo = root / "Cargo.toml"
    if cargo.exists():
        try:
            content = cargo.read_text(encoding="utf-8", errors="ignore")
            parts = []
            for line in content.split("\n"):
                line = line.strip()
                if line.startswith("name = "):
                    project_name = line.split("=", 1)[1].strip().strip('"')
                    parts.append(f"Project: {project_name}")
                elif line.startswith("description = "):
                    description = line.split("=", 1)[1].strip().strip('"')
                    parts.append(f"Description: {description}")
            if parts:
                return "Tech stack (from Cargo.toml):\n" + "\n".join(parts)
        except Exception as e:
            logger.debug(f"Failed to parse Cargo.toml: {e}")

    # Try go.mod
    gomod = root / "go.mod"
    if gomod.exists():
        try:
            content = gomod.read_text(encoding="utf-8", errors="ignore")
            parts = []
            for line in content.split("\n"):
                line = line.strip()
                if line.startswith("module "):
                    parts.append(f"Module: {line.split(' ', 1)[1]}")
                elif line.startswith("go "):
                    parts.append(f"Go version: {line.split(' ', 1)[1]}")
            if parts:
                return "Tech stack (from go.mod):\n" + "\n".join(parts)
        except Exception as e:
            logger.debug(f"Failed to parse go.mod: {e}")

    return None


def _extract_architecture(project_path: str) -> str | None:
    """
    Extract architecture overview from README and directory structure.

    Combines:
    1. README.md content (first 2000 chars)
    2. Top-level directory structure (excluding noise)

    Returns:
        Formatted string with architecture overview, or None if empty project.
    """
    root = Path(project_path)
    parts = []

    # Extract README content
    for readme_name in ["README.md", "README.rst", "README.txt", "README"]:
        readme = root / readme_name
        if readme.exists():
            try:
                content = readme.read_text(encoding="utf-8", errors="ignore")[:2000]
                if content.strip():
                    parts.append(f"README:\n{content}")
                break
            except Exception as e:
                logger.debug(f"Failed to read {readme_name}: {e}")

    # Extract directory structure (top 2 levels)
    dirs = []
    files = []
    try:
        for item in sorted(root.iterdir()):
            name = item.name
            if name.startswith(".") and name not in [".github"]:
                continue
            if name in BOOTSTRAP_EXCLUDED_DIRS:
                continue
            if item.is_dir():
                # Get immediate children count
                try:
                    child_count = sum(1 for _ in item.iterdir())
                    dirs.append(f"  {name}/ ({child_count} items)")
                except PermissionError:
                    dirs.append(f"  {name}/")
            elif item.is_file() and name in [
                "main.py",
                "app.py",
                "index.ts",
                "index.js",
                "main.rs",
                "main.go",
                "Makefile",
                "Dockerfile",
                "docker-compose.yml",
            ]:
                files.append(f"  {name}")
    except Exception as e:
        logger.debug(f"Failed to scan directory: {e}")

    if dirs or files:
        structure = "Directory structure:\n"
        structure += "\n".join(dirs + files)
        parts.append(structure)

    if not parts:
        return None

    return "Architecture overview:\n\n" + "\n\n".join(parts)


def _extract_conventions(project_path: str) -> str | None:
    """
    Extract coding conventions from config files and docs.

    Checks for:
    1. CONTRIBUTING.md / CONTRIBUTING
    2. Linter configs (.eslintrc, ruff.toml, .pylintrc, etc.)
    3. Formatter configs (.prettierrc, pyproject.toml [tool.black])

    Returns:
        Formatted string with coding conventions, or None if nothing found.
    """
    root = Path(project_path)
    parts = []

    # Check CONTRIBUTING.md
    for contrib_name in ["CONTRIBUTING.md", "CONTRIBUTING.rst", "CONTRIBUTING"]:
        contrib = root / contrib_name
        if contrib.exists():
            try:
                content = contrib.read_text(encoding="utf-8", errors="ignore")[:1500]
                if content.strip():
                    parts.append(f"Contributing guidelines:\n{content}")
                break
            except Exception as e:
                logger.debug(f"Failed to read {contrib_name}: {e}")

    # Detect linter/formatter configs
    config_files = [
        (".eslintrc", "ESLint"),
        (".eslintrc.js", "ESLint"),
        (".eslintrc.json", "ESLint"),
        (".prettierrc", "Prettier"),
        (".prettierrc.json", "Prettier"),
        ("prettier.config.js", "Prettier"),
        ("ruff.toml", "Ruff"),
        (".pylintrc", "Pylint"),
        ("pylintrc", "Pylint"),
        ("mypy.ini", "Mypy"),
        (".flake8", "Flake8"),
        ("setup.cfg", "Setup.cfg"),
        ("tslint.json", "TSLint"),
        ("biome.json", "Biome"),
        (".editorconfig", "EditorConfig"),
    ]

    found_configs = []
    for filename, tool_name in config_files:
        if (root / filename).exists():
            found_configs.append(tool_name)

    # Check pyproject.toml for tool configs
    pyproject = root / "pyproject.toml"
    if pyproject.exists():
        try:
            content = pyproject.read_text(encoding="utf-8", errors="ignore")
            if "[tool.black]" in content:
                found_configs.append("Black")
            if "[tool.ruff]" in content:
                found_configs.append("Ruff")
            if "[tool.mypy]" in content:
                found_configs.append("Mypy")
            if "[tool.pytest]" in content or "[tool.pytest.ini_options]" in content:
                found_configs.append("Pytest")
        except Exception:
            pass

    if found_configs:
        # Deduplicate
        unique_configs = list(dict.fromkeys(found_configs))
        parts.append(f"Code tools configured: {', '.join(unique_configs)}")

    if not parts:
        return None

    return "Coding conventions:\n\n" + "\n\n".join(parts)


def _extract_entry_points(project_path: str) -> str | None:
    """
    Find common entry point files in the project.

    Looks for files like:
    - main.py, app.py, cli.py, __main__.py (Python)
    - index.js, index.ts, main.js, main.ts (Node.js)
    - main.rs (Rust)
    - main.go, cmd/ (Go)
    - server.py, server.js, api.py (Servers)

    Returns:
        Formatted list of entry points found, or None if none found.
    """
    root = Path(project_path)
    entry_point_patterns = [
        "main.py",
        "app.py",
        "cli.py",
        "__main__.py",
        "server.py",
        "api.py",
        "wsgi.py",
        "asgi.py",
        "manage.py",
        "index.js",
        "index.ts",
        "index.tsx",
        "main.js",
        "main.ts",
        "server.js",
        "server.ts",
        "app.js",
        "app.ts",
        "main.rs",
        "lib.rs",
        "main.go",
    ]

    found = []

    def scan_dir(dir_path: Path, depth: int = 0):
        if depth > 2:  # Only scan 2 levels deep
            return
        try:
            for item in dir_path.iterdir():
                if item.name in BOOTSTRAP_EXCLUDED_DIRS:
                    continue
                if item.is_file() and item.name in entry_point_patterns:
                    rel_path = item.relative_to(root)
                    found.append(str(rel_path))
                elif item.is_dir() and not item.name.startswith("."):
                    scan_dir(item, depth + 1)
        except PermissionError:
            pass

    scan_dir(root)

    # Also check for cmd/ directory (Go convention)
    cmd_dir = root / "cmd"
    if cmd_dir.exists() and cmd_dir.is_dir():
        try:
            for item in cmd_dir.iterdir():
                if item.is_dir():
                    found.append(f"cmd/{item.name}/")
        except PermissionError:
            pass

    if not found:
        return None

    return "Entry points identified:\n" + "\n".join(
        f"  - {f}" for f in sorted(found)[:15]
    )


def _scan_todos_for_bootstrap(project_path: str, limit: int = 20) -> str | None:
    """
    Scan for TODO/FIXME/HACK comments during bootstrap.

    Uses the existing _scan_for_todos helper but formats results
    for bootstrap memory storage.

    Args:
        project_path: Directory to scan
        limit: Maximum items to include (default: 20)

    Returns:
        Formatted string with TODO summary, or None if none found.
    """
    # Import from code_tools to avoid duplication
    from .code_tools import _scan_for_todos

    todos = _scan_for_todos(project_path, max_files=200)

    if not todos:
        return None

    # Limit and format
    limited = todos[:limit]

    # Group by type
    by_type: dict[str, int] = {}
    for todo in todos:
        todo_type = todo.get("type", "TODO")
        by_type[todo_type] = by_type.get(todo_type, 0) + 1

    summary_parts = []

    # Add counts summary
    counts = ", ".join(f"{count} {t}" for t, count in sorted(by_type.items()))
    summary_parts.append(f"Found: {counts}")

    # Add individual items
    for todo in limited:
        file_path = todo.get("file", "unknown")
        line = todo.get("line", 0)
        todo_type = todo.get("type", "TODO")
        content = todo.get("content", "")[:80]
        summary_parts.append(f"  [{todo_type}] {file_path}:{line} - {content}")

    if len(todos) > limit:
        summary_parts.append(f"  ... and {len(todos) - limit} more")

    return "Known issues from code comments:\n" + "\n".join(summary_parts)


def _extract_project_instructions(project_path: str) -> str | None:
    """
    Extract project instructions from CLAUDE.md and AGENTS.md.

    Returns:
        Combined instructions content, or None if no files found.
    """
    root = Path(project_path)
    parts = []

    # Check CLAUDE.md
    claude_md = root / "CLAUDE.md"
    if claude_md.exists():
        try:
            content = claude_md.read_text(encoding="utf-8", errors="ignore")[:3000]
            if content.strip():
                parts.append(f"From CLAUDE.md:\n{content}")
        except Exception as e:
            logger.debug(f"Failed to read CLAUDE.md: {e}")

    # Check AGENTS.md
    agents_md = root / "AGENTS.md"
    if agents_md.exists():
        try:
            content = agents_md.read_text(encoding="utf-8", errors="ignore")[:2000]
            if content.strip():
                parts.append(f"From AGENTS.md:\n{content}")
        except Exception as e:
            logger.debug(f"Failed to read AGENTS.md: {e}")

    if not parts:
        return None

    return "Project instructions:\n\n" + "\n\n".join(parts)


# ============================================================================
# Helper: Git awareness
# ============================================================================
def _get_git_history_summary(project_path: str, limit: int = 30) -> str | None:
    """Get a summary of git history for bootstrapping context.

    Args:
        project_path: Directory to run git commands in
        limit: Maximum number of commits to include

    Returns:
        Formatted string summary of git history, or None if not a git repo
    """
    try:
        # Check if we're in a git repo
        result = subprocess.run(
            ["git", "rev-parse", "--git-dir"],
            capture_output=True,
            text=True,
            timeout=5,
            cwd=project_path,
        )
        if result.returncode != 0:
            return None

        # Get commit history with more detail
        result = subprocess.run(
            ["git", "log", f"-{limit}", "--format=%h|%s|%an|%ar"],
            capture_output=True,
            text=True,
            timeout=10,
            cwd=project_path,
        )
        if result.returncode != 0 or not result.stdout.strip():
            return None

        lines = result.stdout.strip().split("\n")
        summary_parts = []
        for line in lines:
            parts = line.split("|", 3)
            if len(parts) >= 2:
                commit_hash, message = parts[0], parts[1]
                summary_parts.append(f"- {commit_hash}: {message}")

        if not summary_parts:
            return None

        return "\n".join(summary_parts)

    except (subprocess.TimeoutExpired, FileNotFoundError, Exception):
        return None


def _get_git_changes(
    since_date: datetime | None = None, project_path: str | None = None
) -> dict[str, Any] | None:
    """Get git changes since a given date.

    Args:
        since_date: Only show commits since this date
        project_path: Directory to run git commands in (defaults to CWD)
    """
    try:
        # Use project_path as working directory for git commands
        cwd = project_path if project_path else None

        # Check if we're in a git repo
        result = subprocess.run(
            ["git", "rev-parse", "--git-dir"],
            capture_output=True,
            text=True,
            timeout=5,
            cwd=cwd,
            stdin=subprocess.DEVNULL,
        )
        if result.returncode != 0:
            return None

        git_info = {}

        # Get recent commits
        if since_date:
            since_str = since_date.strftime("%Y-%m-%d")
            cmd = ["git", "log", f"--since={since_str}", "--oneline", "-10"]
        else:
            cmd = ["git", "log", "--oneline", "-5"]

        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=5,
            cwd=cwd,
            stdin=subprocess.DEVNULL,
        )
        if result.returncode == 0 and result.stdout.strip():
            git_info["recent_commits"] = result.stdout.strip().split("\n")

        # Get changed files (uncommitted) - limit to 10 for token efficiency
        result = subprocess.run(
            ["git", "status", "--porcelain"],
            capture_output=True,
            text=True,
            timeout=5,
            cwd=cwd,
            stdin=subprocess.DEVNULL,
        )
        if result.returncode == 0 and result.stdout.strip():
            changes = result.stdout.strip().split("\n")
            all_changes = [
                {"status": line[:2].strip(), "file": line[3:]}
                for line in changes
                if line.strip()
            ]
            git_info["uncommitted_changes"] = all_changes[:10]
            if len(all_changes) > 10:
                git_info["uncommitted_changes_truncated"] = len(all_changes) - 10

        # Get current branch
        result = subprocess.run(
            ["git", "branch", "--show-current"],
            capture_output=True,
            text=True,
            timeout=5,
            cwd=cwd,
            stdin=subprocess.DEVNULL,
        )
        if result.returncode == 0:
            git_info["branch"] = result.stdout.strip()

        return git_info if git_info else None

    except (subprocess.TimeoutExpired, FileNotFoundError, Exception):
        return None


# ============================================================================
# Helper: Bootstrap project context on first run
# ============================================================================
async def _bootstrap_project_context(ctx: ProjectContext) -> dict[str, Any]:
    """
    Bootstrap initial context on first run.

    Called automatically when get_briefing() detects no memories exist.
    Ingests multiple sources to provide comprehensive project awareness:
    1. Project identity (tech stack from manifests)
    2. Architecture overview (README + directory structure)
    3. Coding conventions (from config files)
    4. Project instructions (CLAUDE.md, AGENTS.md)
    5. Git history baseline
    6. Known issues (TODO/FIXME scan)
    7. Entry points (main files)

    Args:
        ctx: The project context to bootstrap

    Returns:
        Dictionary with bootstrap results including sources status
    """
    results = {"bootstrapped": True, "memories_created": 0, "sources": {}}

    # Define all extractors with their memory configs
    extractors = [
        (
            "project_identity",
            lambda: _extract_project_identity(ctx.project_path),
            "pattern",
            "Tech stack and dependencies from project manifest",
            ["bootstrap", "tech-stack", "identity"],
        ),
        (
            "architecture",
            lambda: _extract_architecture(ctx.project_path),
            "pattern",
            "Project structure and README overview",
            ["bootstrap", "architecture", "structure"],
        ),
        (
            "conventions",
            lambda: _extract_conventions(ctx.project_path),
            "pattern",
            "Coding conventions and tool configurations",
            ["bootstrap", "conventions", "style"],
        ),
        (
            "project_instructions",
            lambda: _extract_project_instructions(ctx.project_path),
            "pattern",
            "Project-specific AI instructions from CLAUDE.md/AGENTS.md",
            ["bootstrap", "project-config", "instructions"],
        ),
        (
            "git_evolution",
            lambda: _get_git_history_summary(ctx.project_path, limit=30),
            "learning",
            "Recent git history showing project evolution",
            ["bootstrap", "git-history", "evolution"],
        ),
        (
            "known_issues",
            lambda: _scan_todos_for_bootstrap(ctx.project_path, limit=20),
            "warning",
            "Known issues from TODO/FIXME/HACK comments in code",
            ["bootstrap", "tech-debt", "issues"],
        ),
        (
            "entry_points",
            lambda: _extract_entry_points(ctx.project_path),
            "learning",
            "Main entry point files identified in the project",
            ["bootstrap", "entry-points", "structure"],
        ),
    ]

    # Run each extractor and create memories
    for name, extractor, category, rationale, tags in extractors:
        try:
            content = extractor()
            if content:
                await ctx.memory_manager.remember(
                    category=category,
                    content=content,
                    rationale=f"Auto-ingested on first run: {rationale}",
                    tags=tags,
                    project_path=ctx.project_path,
                )
                results["sources"][name] = "ingested"
                results["memories_created"] += 1
                logger.info(f"Bootstrapped {name} for {ctx.project_path}")
            else:
                results["sources"][name] = "skipped"
        except Exception as e:
            logger.warning(f"Failed to extract {name}: {e}")
            results["sources"][name] = f"error: {e}"

    return results


# ============================================================================
# Helper functions for get_briefing (extracted for maintainability)
# ============================================================================


async def _fetch_recent_context(ctx: ProjectContext) -> dict[str, Any]:
    """
    Fetch recent decisions, warnings, failed approaches, and top rules.

    Args:
        ctx: Project context with database access

    Returns:
        Dict with recent_decisions, active_warnings, failed_approaches,
        top_rules, and last_memory_date
    """
    last_memory_date = None

    async with ctx.db_manager.get_session() as session:
        # Get most recent memory timestamp
        result = await session.execute(
            select(Memory.created_at).order_by(Memory.created_at.desc()).limit(1)
        )
        row = result.first()
        if row:
            last_memory_date = row[0]

        # Get recent decisions - lean summary with first line only
        result = await session.execute(
            select(Memory)
            .where(Memory.category == "decision")
            .order_by(Memory.created_at.desc())
            .limit(5)
        )
        all_decisions = result.scalars().all()
        recent_decisions = [
            {
                "id": m.id,
                "summary": m.content.split("\n")[0][:120] + "..."
                if len(m.content.split("\n")[0]) > 120
                else m.content.split("\n")[0],
                "worked": m.worked,
            }
            for m in all_decisions
        ]

        # Count total decisions for context
        result = await session.execute(
            select(func.count(Memory.id)).where(Memory.category == "decision")
        )
        total_decisions = result.scalar() or 0

        # Get active warnings - first line summary only
        result = await session.execute(
            select(Memory)
            .where(Memory.category == "warning")
            .order_by(Memory.created_at.desc())
            .limit(5)
        )
        all_warnings = result.scalars().all()
        active_warnings = [
            {
                "id": m.id,
                "summary": m.content.split("\n")[0][:120] + "..."
                if len(m.content.split("\n")[0]) > 120
                else m.content.split("\n")[0],
            }
            for m in all_warnings
        ]

        # Count total warnings
        result = await session.execute(
            select(func.count(Memory.id)).where(Memory.category == "warning")
        )
        total_warnings = result.scalar() or 0

        # Get FAILED decisions - critical, show first line
        result = await session.execute(
            select(Memory)
            .where(Memory.worked == False)  # noqa: E712
            .order_by(Memory.created_at.desc())
            .limit(5)
        )
        all_failed = result.scalars().all()
        failed_approaches = [
            {
                "id": m.id,
                "summary": m.content.split("\n")[0][:100] + "..."
                if len(m.content.split("\n")[0]) > 100
                else m.content.split("\n")[0],
                "outcome": (m.outcome.split("\n")[0][:60] + "...")
                if m.outcome
                else None,
            }
            for m in all_failed
        ]

        # Count total failed
        result = await session.execute(
            select(func.count(Memory.id)).where(Memory.worked == False)  # noqa: E712
        )
        total_failed = result.scalar() or 0

        # Get high-priority rules - trigger first line only
        result = await session.execute(
            select(Rule)
            .where(Rule.enabled == True)  # noqa: E712
            .order_by(Rule.priority.desc())
            .limit(5)
        )
        all_rules = result.scalars().all()
        top_rules = [
            {
                "id": r.id,
                "trigger": r.trigger.split("\n")[0][:80] + "..."
                if len(r.trigger.split("\n")[0]) > 80
                else r.trigger.split("\n")[0],
                "priority": r.priority,
            }
            for r in all_rules
        ]

    return {
        "last_memory_date": last_memory_date,
        "recent_decisions": recent_decisions,
        "total_decisions": total_decisions,
        "active_warnings": active_warnings,
        "total_warnings": total_warnings,
        "failed_approaches": failed_approaches,
        "total_failed": total_failed,
        "top_rules": top_rules,
        "drill_down": "Use recall(topic) or recall_for_file(file) for full memory content",
    }


async def _fetch_dream_sessions(
    ctx: ProjectContext, limit: int = 5
) -> list[dict[str, Any]]:
    """
    Fetch recent dream session summaries for the briefing dashboard.

    Queries dream-tagged memories and groups them by session ID to build
    a summary of recent autonomous dream activity.

    Args:
        ctx: Project context with database access
        limit: Maximum number of sessions to return

    Returns:
        List of dream session dicts with session_id, decisions_reviewed,
        insights_generated, and individual insight summaries.
    """
    try:
        async with ctx.db_manager.get_session() as session:
            # Query dream-tagged memories (most recent first)
            result = await session.execute(
                select(Memory)
                .where(Memory.tags.contains('"dream"'))
                .where(Memory.archived == False)  # noqa: E712
                .order_by(Memory.created_at.desc())
                .limit(limit * 3)  # Fetch extra to group by session
            )
            dreams = result.scalars().all()

        if not dreams:
            return []

        # Group by session ID from context dict
        sessions: dict[str, dict[str, Any]] = {}
        for m in dreams:
            ctx_data = m.context or {}
            sid = ctx_data.get("dream_session_id", "unknown")

            if sid not in sessions:
                sessions[sid] = {
                    "session_id": sid,
                    "decisions_reviewed": ctx_data.get("decisions_reviewed", 0),
                    "insights_generated": ctx_data.get("insights_generated", 0),
                    "outcomes_resolved": ctx_data.get("outcomes_resolved", 0),
                    "interrupted": ctx_data.get("interrupted", False),
                    "timestamp": m.created_at.isoformat() if m.created_at else None,
                    "insights": [],
                }

            # Individual insight (not session summary)
            if "dream-summary" not in (m.tags or []):
                sessions[sid]["insights"].append(
                    {
                        "source_decision_id": ctx_data.get("source_decision_id"),
                        "result": ctx_data.get("re_evaluation_result"),
                        "summary": m.content[:100] + "..."
                        if len(m.content) > 100
                        else m.content,
                    }
                )

                # Update counters from individual results (more accurate than summary)
                if sessions[sid]["decisions_reviewed"] == 0:
                    sessions[sid]["decisions_reviewed"] = len(sessions[sid]["insights"])
                if sessions[sid]["insights_generated"] == 0:
                    sessions[sid]["insights_generated"] = len(
                        [
                            i
                            for i in sessions[sid]["insights"]
                            if i["result"] != "needs_more_data"
                        ]
                    )

        return list(sessions.values())[:limit]
    except Exception as e:
        logger.warning(f"Failed to fetch dream sessions: {e}")
        return []


async def _prefetch_focus_areas(
    ctx: ProjectContext, focus_areas: list[str]
) -> dict[str, dict[str, Any]]:
    """
    Pre-fetch memories for specified focus areas.

    Returns lean summaries - use recall(topic) for full content.

    Args:
        ctx: Project context with memory manager
        focus_areas: List of topics to fetch (max 4 processed)

    Returns:
        Dict mapping area name to summary info with top relevant items
    """
    focus_memories = {}

    for area in focus_areas[:4]:  # Limit to 4 areas
        memories = await ctx.memory_manager.recall(
            area,
            limit=3,
            project_path=ctx.project_path,
            condensed=True,  # Use condensed mode for token efficiency
        )

        # Extract top 2 most relevant items with first-line summaries
        top_items = []
        for cat in ["decisions", "warnings", "patterns", "learnings"]:
            for m in memories.get(cat, [])[:2]:
                content = m.get("content", "")
                first_line = content.split("\n")[0][:80]
                top_items.append(
                    {
                        "id": m.get("id"),
                        "type": cat[:-1],  # Remove 's' (decisions -> decision)
                        "summary": first_line + "..."
                        if len(content.split("\n")[0]) > 80
                        else first_line,
                        "relevance": m.get("relevance", 0),
                    }
                )

        # Sort by relevance and take top 3
        top_items.sort(key=lambda x: x.get("relevance", 0), reverse=True)

        focus_memories[area] = {
            "found": memories.get("found", 0),
            "top_matches": top_items[:3],
            "has_warnings": len(memories.get("warnings", [])) > 0,
            "has_failed": any(
                m.get("worked") is False
                for cat in ["decisions", "patterns", "learnings"]
                for m in memories.get(cat, [])
            ),
            "hint": f"recall('{area}') for details"
            if memories.get("found", 0) > 3
            else None,
        }

    return focus_memories


async def _get_linked_projects_summary(ctx: ProjectContext) -> list[dict[str, Any]]:
    """
    Get summary of linked projects with warning/memory counts.

    Args:
        ctx: Project context with db_manager and project_path

    Returns:
        List of dicts with path, relationship, label, available, warning_count, memory_count
    """
    try:
        from ..links import LinkManager
    except ImportError:
        from daem0nmcp.links import LinkManager

    try:
        from ..database import DatabaseManager
        from ..memory import MemoryManager
    except ImportError:
        from daem0nmcp.database import DatabaseManager
        from daem0nmcp.memory import MemoryManager

    link_mgr = LinkManager(ctx.db_manager)
    links = await link_mgr.list_linked_projects(ctx.project_path)

    summaries = []
    for link in links:
        linked_path = link["linked_path"]
        linked_storage = Path(linked_path) / ".daem0nmcp" / "storage"

        summary = {
            "path": linked_path,
            "relationship": link["relationship"],
            "label": link.get("label"),
            "available": False,
            "warning_count": 0,
            "memory_count": 0,
        }

        if linked_storage.exists():
            try:
                linked_db = DatabaseManager(str(linked_storage))
                await linked_db.init_db()

                linked_memory = MemoryManager(linked_db)
                stats = await linked_memory.get_statistics()

                summary["available"] = True
                summary["warning_count"] = stats.get("by_category", {}).get(
                    "warning", 0
                )
                summary["memory_count"] = stats.get("total_memories", 0)
            except Exception as e:
                logger.warning(
                    f"Could not get summary for linked project {linked_path}: {e}"
                )

        summaries.append(summary)

    return summaries


def _build_briefing_message(
    stats: dict[str, Any],
    bootstrap_result: dict[str, Any] | None,
    failed_approaches: list[dict[str, Any]],
    active_warnings: list[dict[str, Any]],
    git_changes: dict[str, Any] | None,
    dream_sessions: list[dict[str, Any]] | None = None,
) -> str:
    """
    Build the actionable message for the briefing.

    Args:
        stats: Memory statistics
        bootstrap_result: Bootstrap result if first run
        failed_approaches: List of failed approaches
        active_warnings: List of active warnings
        git_changes: Git changes info
        dream_sessions: Recent dream session summaries

    Returns:
        Human-readable briefing message
    """
    message_parts = [f"Daem0nMCP ready. {stats['total_memories']} memories stored."]

    # Add bootstrap notification if this was first run
    if bootstrap_result:
        sources = bootstrap_result.get("sources", {})
        ingested = [k for k, v in sources.items() if v == "ingested"]

        if ingested:
            source_summary = ", ".join(ingested)
            message_parts.append(f"[BOOTSTRAP] First run - ingested: {source_summary}.")
        else:
            message_parts.append("[BOOTSTRAP] First run - no sources found.")

    if failed_approaches:
        message_parts.append(
            f"[WARNING] {len(failed_approaches)} failed approaches to avoid!"
        )

    if active_warnings:
        message_parts.append(f"{len(active_warnings)} active warnings.")

    if git_changes and git_changes.get("uncommitted_changes"):
        message_parts.append(
            f"{len(git_changes['uncommitted_changes'])} uncommitted file(s)."
        )

    # Dream activity summary
    if dream_sessions:
        total_insights = sum(s.get("insights_generated", 0) for s in dream_sessions)
        total_reviewed = sum(s.get("decisions_reviewed", 0) for s in dream_sessions)
        total_resolved = sum(s.get("outcomes_resolved", 0) for s in dream_sessions)
        if total_insights > 0:
            dream_msg = (
                f"[DREAM] Daemon reviewed {total_reviewed} decisions while idle, "
                f"generated {total_insights} insight(s)."
            )
            if total_resolved > 0:
                dream_msg = dream_msg.rstrip(".")
                dream_msg += f", auto-resolved {total_resolved} pending outcome(s)."
            message_parts.append(dream_msg)

    if stats.get("learning_insights", {}).get("suggestion"):
        message_parts.append(stats["learning_insights"]["suggestion"])

    return " ".join(message_parts)


# ============================================================================
# Tool 6: GET_BRIEFING - Smart session start summary
# ============================================================================
@mcp.tool(version=__version__)
@with_request_id
async def get_briefing(
    project_path: str | None = None, focus_areas: list[str] | None = None
) -> dict[str, Any]:
    """
    [DEPRECATED] Use commune(action='briefing') instead.

    Session start - call FIRST. Returns stats, recent decisions, warnings, failed approaches, git changes.

    Args:
        project_path: Project root (REQUIRED)
        focus_areas: Topics to pre-fetch
    """
    # Require project_path for multi-project support
    if not project_path and not _default_project_path:
        return _missing_project_path_error()

    ctx = await get_project_context(project_path)

    # Get statistics with learning insights
    stats = await ctx.memory_manager.get_statistics()

    # AUTO-BOOTSTRAP: First run detection
    bootstrap_result = None
    if stats.get("total_memories", 0) == 0:
        bootstrap_result = await _bootstrap_project_context(ctx)
        stats = await ctx.memory_manager.get_statistics()

    # Fetch recent context (decisions, warnings, failed approaches, rules)
    recent_context = await _fetch_recent_context(ctx)

    # Get git changes since last memory
    git_changes = _get_git_changes(
        recent_context["last_memory_date"], project_path=ctx.project_path
    )

    # Pre-fetch memories for focus areas if specified
    focus_memories = None
    if focus_areas:
        focus_memories = await _prefetch_focus_areas(ctx, focus_areas)

    # Get linked projects summary
    linked_summary = await _get_linked_projects_summary(ctx)

    # Fetch recent dream sessions
    dream_sessions = await _fetch_dream_sessions(ctx)

    # Build actionable message
    message = _build_briefing_message(
        stats=stats,
        bootstrap_result=bootstrap_result,
        failed_approaches=recent_context["failed_approaches"],
        active_warnings=recent_context["active_warnings"],
        git_changes=git_changes,
        dream_sessions=dream_sessions,
    )

    # Mark this project as briefed (Sacred Covenant: communion complete)
    ctx.briefed = True

    # Get active working context (limited to 5 items for token efficiency)
    active_context = {"count": 0, "items": [], "max_count": 5}
    try:
        try:
            from ..active_context import ActiveContextManager
        except ImportError:
            from daem0nmcp.active_context import ActiveContextManager

        acm = ActiveContextManager(ctx.db_manager)
        full_context = await acm.get_active_context(ctx.project_path, condensed=True)

        # Limit items for briefing (full context available via get_active_context tool)
        active_context["count"] = full_context.get("count", 0)
        active_context["items"] = full_context.get("items", [])[:5]
        if full_context.get("count", 0) > 5:
            active_context["truncated"] = full_context["count"] - 5

        # Clean up expired items
        await acm.cleanup_expired(ctx.project_path)
    except Exception as e:
        logger.warning(f"Failed to fetch active context: {e}")
        active_context["error"] = str(e)

    result = {
        "status": "ready",
        "statistics": stats,
        "recent_decisions": recent_context["recent_decisions"],
        "active_warnings": recent_context["active_warnings"],
        "failed_approaches": recent_context["failed_approaches"],
        "top_rules": recent_context["top_rules"],
        "git_changes": git_changes,
        "focus_areas": focus_memories,
        "bootstrap": bootstrap_result,
        "linked_projects": linked_summary,
        "dream_sessions": dream_sessions,
        "active_context": active_context,
        "message": message,
    }
    return add_deprecation(result, "get_briefing", "commune(action='briefing')")


# ============================================================================
# Tool 6.5: GET_BRIEFING_VISUAL - Briefing with UI resource hint
# ============================================================================
@mcp.tool(version=__version__)
@with_request_id
async def get_briefing_visual(
    project_path: str | None = None, focus_areas: list[str] | None = None
) -> dict[str, Any]:
    """
    [DEPRECATED] Use commune(action='briefing', visual=True) instead.

    Session start with visual UI support.

    Same as get_briefing() but returns results with UI resource hint for
    MCP Apps hosts. Non-MCP-Apps hosts receive text fallback.

    Args:
        project_path: Project root (REQUIRED)
        focus_areas: Topics to pre-fetch

    Returns:
        Dict with briefing data + ui_resource hint + text fallback
    """
    from daem0nmcp.ui.fallback import format_briefing_text, format_with_ui_hint

    # Get briefing data using existing get_briefing function
    result = await get_briefing(project_path=project_path, focus_areas=focus_areas)

    # Check for error
    if "error" in result:
        return result

    # Generate text fallback
    text = format_briefing_text(result)

    # Return with UI hint
    ui_result = format_with_ui_hint(
        data=result, ui_resource="ui://daem0n/briefing", text=text
    )
    return add_deprecation(
        ui_result, "get_briefing_visual", "commune(action='briefing', visual=True)"
    )


# ============================================================================
# Tool 6.6: GET_COVENANT_STATUS - Current covenant state for dashboard
# ============================================================================
@mcp.tool(version=__version__)
@with_request_id
async def get_covenant_status(project_path: str | None = None) -> dict[str, Any]:
    """
    [DEPRECATED] Use commune(action='covenant') instead.

    Get current Sacred Covenant status for dashboard visualization.

    Returns the current ritual phase, preflight token status, and data
    needed to render the Covenant Status Dashboard.

    Args:
        project_path: Project root (REQUIRED)

    Returns:
        Dict with phase info, token status, and message
    """
    try:
        from ..covenant import COUNSEL_TTL_SECONDS
    except ImportError:
        from daem0nmcp.covenant import COUNSEL_TTL_SECONDS

    if not project_path and not _default_project_path:
        return _missing_project_path_error()

    ctx = await get_project_context(project_path)

    # Determine covenant phase from session state
    if not ctx.briefed:
        covenant_phase = "commune"
    elif not ctx.context_checks:
        covenant_phase = "counsel"
    else:
        covenant_phase = "inscribe"

    PHASE_DISPLAY = {
        "commune": {
            "label": "COMMUNE",
            "description": "Receive briefing from the Daem0n",
        },
        "counsel": {
            "label": "SEEK COUNSEL",
            "description": "Check context before acting",
        },
        "inscribe": {
            "label": "INSCRIBE",
            "description": "Record memories and decisions",
        },
        "seal": {"label": "SEAL", "description": "Evaluate and record outcomes"},
    }
    phase_info = PHASE_DISPLAY.get(covenant_phase, PHASE_DISPLAY["commune"])

    # Check preflight token status
    preflight_status = "none"
    preflight_expires = None
    preflight_remaining = None

    if ctx.context_checks:
        latest = ctx.context_checks[-1]
        check_time = datetime.fromisoformat(latest["timestamp"].replace("Z", "+00:00"))
        expires_at = check_time + timedelta(seconds=COUNSEL_TTL_SECONDS)

        if datetime.now(timezone.utc) < expires_at:
            preflight_status = "valid"
            preflight_expires = expires_at.isoformat()
            preflight_remaining = int(
                (expires_at - datetime.now(timezone.utc)).total_seconds()
            )
        else:
            preflight_status = "expired"

    # Build status message
    messages = {
        "commune": "Begin by receiving your briefing (get_briefing).",
        "counsel": "Counsel sought. You may now inscribe memories or take action.",
        "inscribe": "Actions taken. Consider recording outcomes when complete.",
        "seal": "Outcomes recorded. The covenant cycle may begin anew.",
    }

    result = {
        "phase": covenant_phase,
        "phase_label": phase_info["label"],
        "phase_description": phase_info["description"],
        "is_briefed": ctx.briefed,
        "context_check_count": len(ctx.context_checks),
        "preflight": {
            "status": preflight_status,
            "expires_at": preflight_expires,
            "remaining_seconds": preflight_remaining,
        },
        "can_mutate": preflight_status == "valid",
        "message": messages.get(covenant_phase, messages["commune"]),
    }
    return add_deprecation(result, "get_covenant_status", "commune(action='covenant')")


# ============================================================================
# Tool 6.7: GET_COVENANT_STATUS_VISUAL - Covenant status with UI support
# ============================================================================
@mcp.tool(version=__version__)
@with_request_id
async def get_covenant_status_visual(
    project_path: str | None = None,
) -> dict[str, Any]:
    """
    Get covenant status with visual UI support.

    Same as get_covenant_status() but returns results with UI resource hint for
    MCP Apps hosts. Non-MCP-Apps hosts receive text fallback.

    Args:
        project_path: Project root (REQUIRED)

    Returns:
        Dict with covenant data + ui_resource hint + text fallback
    """
    from daem0nmcp.ui.fallback import format_covenant_status_text, format_with_ui_hint

    # Get covenant status using existing function
    result = await get_covenant_status(project_path=project_path)

    # Check for error
    if "error" in result:
        return result

    # Generate text fallback
    text = format_covenant_status_text(result)

    # Create UI resource URI with encoded data
    import json
    import urllib.parse

    data_json = json.dumps(result)
    encoded_data = urllib.parse.quote(data_json)
    ui_resource = f"ui://daem0n/covenant/{encoded_data}"

    return format_with_ui_hint(data=result, ui_resource=ui_resource, text=text)


# ============================================================================
# Tool: CONTEXT_CHECK - Quick pre-flight check for current work
# ============================================================================
@mcp.tool(version=__version__)
@with_request_id
async def context_check(
    description: str, project_path: str | None = None
) -> dict[str, Any]:
    """
    [DEPRECATED] Use consult(action='preflight') instead.

    Pre-flight check combining recall + check_rules. Issues preflight token valid for 5 min.

    Args:
        description: What you're about to do
        project_path: Project root
    """
    # Require project_path for multi-project support
    if not project_path and not _default_project_path:
        return _missing_project_path_error()

    ctx = await get_project_context(project_path)

    # Get relevant memories (with defensive None check)
    memories = await ctx.memory_manager.recall(
        description, limit=5, project_path=ctx.project_path
    )
    if memories is None:
        memories = {}

    # Check rules (with defensive None check)
    rules = await ctx.rules_engine.check_rules(description)
    if not isinstance(rules, dict):
        rules = {}

    # Collect all warnings
    warnings = []

    # From memories
    for cat in ["warnings", "decisions", "patterns", "learnings"]:
        for mem in memories.get(cat, []):
            if mem.get("worked") is False:
                warnings.append(
                    {
                        "source": "failed_decision",
                        "content": mem["content"],
                        "outcome": mem.get("outcome"),
                    }
                )
            elif cat == "warnings":
                warnings.append({"source": "warning", "content": mem["content"]})

    # From rules (defensive check for None)
    guidance = rules.get("guidance") if rules else None
    if guidance and guidance.get("warnings"):
        for w in guidance["warnings"]:
            warnings.append({"source": "rule", "content": w})

    has_concerns = len(warnings) > 0 or (rules and rules.get("has_blockers", False))

    # Record this context check (Sacred Covenant: counsel sought)
    ctx.context_checks.append(
        {
            "description": description,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
    )

    # Issue preflight token as proof of consultation
    try:
        from ..covenant import PreflightToken
        from ..enforcement import get_session_id
    except ImportError:
        from daem0nmcp.covenant import PreflightToken
        from daem0nmcp.enforcement import get_session_id

    token = PreflightToken.issue(
        action=description,
        session_id=get_session_id(ctx.project_path),
        project_path=ctx.project_path,
    )

    result = {
        "description": description,
        "has_concerns": has_concerns,
        "memories_found": memories.get("found", 0),
        "rules_matched": rules.get("matched_rules", 0) if rules else 0,
        "warnings": warnings,
        "must_do": guidance.get("must_do", []) if guidance else [],
        "must_not": guidance.get("must_not", []) if guidance else [],
        "ask_first": guidance.get("ask_first", []) if guidance else [],
        "preflight_token": token.serialize(),
        "message": (
            "\u26a0\ufe0f Review warnings before proceeding"
            if has_concerns
            else "\u2713 No concerns found, but always use good judgment"
        ),
    }
    return add_deprecation(result, "context_check", "consult(action='preflight')")


# ============================================================================
# Tool: CHECK_FOR_UPDATES - Real-time polling
# ============================================================================
@mcp.tool(version=__version__)
@with_request_id
async def check_for_updates(
    since: str | None = None,
    interval_seconds: int = 10,
    project_path: str | None = None,
) -> dict[str, Any]:
    """
    Check if daemon knowledge has changed since the given timestamp.

    Used for real-time update polling. MCP hosts call this tool periodically
    and send 'data_updated' postMessage to UIs when has_changes is True.

    Args:
        since: ISO 8601 timestamp to check from (e.g., '2026-01-28T12:00:00Z').
               If None, returns current state (always has_changes=True).
        interval_seconds: Recommended polling interval (5-60, default 10).
                         Returned to help hosts configure polling.
        project_path: Project context path (uses current directory if not specified)

    Returns:
        Dict with:
            - has_changes: bool - True if data changed since timestamp
            - last_update: str - ISO timestamp of most recent change
            - recommended_interval: int - Suggested polling interval
            - checked_at: str - ISO timestamp of this check
    """
    if not project_path and not _default_project_path:
        return _missing_project_path_error()

    project_path = project_path or _default_project_path

    try:
        from ..ui.ui_tools import check_for_updates as _check
    except ImportError:
        from daem0nmcp.ui.ui_tools import check_for_updates as _check

    ctx = await get_project_context(project_path)
    return await _check(ctx.db_manager, since, interval_seconds)


# ============================================================================
# Tool: HEALTH - Server health, version, and statistics
# ============================================================================
@mcp.tool(version=__version__)
@with_request_id
async def health(project_path: str | None = None) -> dict[str, Any]:
    """
    [DEPRECATED] Use commune(action='health') instead.

    Get server health, version, and statistics.

    Args:
        project_path: Project root
    """
    import time

    if not project_path and not _default_project_path:
        return _missing_project_path_error()

    ctx = await get_project_context(project_path)
    stats = await ctx.memory_manager.get_statistics()

    # Get rule count
    rules = await ctx.rules_engine.list_rules(enabled_only=False, limit=1000)

    # Code entity stats
    async with ctx.db_manager.get_session() as session:
        result = await session.execute(select(func.count(CodeEntity.id)))
        entity_count = result.scalar() or 0

        result = await session.execute(select(func.max(CodeEntity.indexed_at)))
        last_indexed = result.scalar()

        result = await session.execute(
            select(CodeEntity.entity_type, func.count(CodeEntity.id)).group_by(
                CodeEntity.entity_type
            )
        )
        entities_by_type = {row[0]: row[1] for row in result.all()}

    # Index freshness
    index_age_seconds = None
    index_stale = False
    if last_indexed:
        now = datetime.now(timezone.utc)
        if last_indexed.tzinfo is None:
            last_indexed = last_indexed.replace(tzinfo=timezone.utc)
        index_age_seconds = (now - last_indexed).total_seconds()
        index_stale = index_age_seconds > 86400  # 24 hours

    result = {
        "status": "healthy",
        "version": __version__,
        "project_path": ctx.project_path,
        "storage_path": ctx.storage_path,
        "memories_count": stats.get("total_memories", 0),
        "rules_count": len(rules),
        "by_category": stats.get("by_category", {}),
        "contexts_cached": len(_project_contexts),
        "vectors_enabled": vectors.is_available(),
        "timestamp": time.time(),
        # Code index stats
        "code_entities_count": entity_count,
        "entities_by_type": entities_by_type,
        "last_indexed_at": last_indexed.isoformat() if last_indexed else None,
        "index_age_seconds": index_age_seconds,
        "index_stale": index_stale,
    }
    return add_deprecation(result, "health", "commune(action='health')")
