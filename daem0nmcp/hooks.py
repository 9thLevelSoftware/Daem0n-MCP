"""
Git hook templates for Daem0nMCP enforcement.
"""

import os
import stat
from pathlib import Path

PRE_COMMIT_HOOK = """#!/bin/sh
# Daem0nMCP Pre-Commit Enforcement Hook
# Checks staged files against memories before allowing commit

# Get the project root
PROJECT_ROOT="$(git rev-parse --show-toplevel)"

# Run the pre-commit check
python -m daem0nmcp.cli --project-path "$PROJECT_ROOT" pre-commit

# Exit with the same code
exit $?
"""

POST_COMMIT_HOOK = """#!/bin/sh
# Daem0nMCP Post-Commit Hook
# Detects when enforcement was bypassed via --no-verify
# (placeholder for future bypass logging)
"""


def install_hooks(project_path: str, force: bool = False) -> tuple[bool, str]:
    """
    Install git hooks for enforcement.

    Args:
        project_path: Path to the project root
        force: Overwrite existing hooks

    Returns:
        (success, message)
    """
    project = Path(project_path)
    hooks_dir = project / ".git" / "hooks"

    if not hooks_dir.exists():
        return False, f"Not a git repository: {project_path}"

    pre_commit = hooks_dir / "pre-commit"
    post_commit = hooks_dir / "post-commit"

    messages = []

    # Install pre-commit hook
    if pre_commit.exists() and not force:
        content = pre_commit.read_text()
        if "daem0nmcp" not in content.lower():
            return False, "pre-commit hook already exists. Use --force to overwrite."
        messages.append("pre-commit hook updated")
    else:
        messages.append("pre-commit hook installed")

    pre_commit.write_text(PRE_COMMIT_HOOK)
    os.chmod(
        pre_commit,
        os.stat(pre_commit).st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH,
    )

    # Install post-commit hook (optional, only if it doesn't exist)
    if not post_commit.exists() or force:
        post_commit.write_text(POST_COMMIT_HOOK)
        os.chmod(
            post_commit,
            os.stat(post_commit).st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH,
        )
        messages.append("post-commit hook installed")

    return True, "; ".join(messages)


def uninstall_hooks(project_path: str) -> tuple[bool, str]:
    """
    Remove daem0nmcp git hooks.

    Args:
        project_path: Path to the project root

    Returns:
        (success, message)
    """
    project = Path(project_path)
    hooks_dir = project / ".git" / "hooks"

    if not hooks_dir.exists():
        return False, f"Not a git repository: {project_path}"

    removed = []

    for hook_name in ["pre-commit", "post-commit"]:
        hook_path = hooks_dir / hook_name
        if hook_path.exists():
            content = hook_path.read_text()
            if "daem0nmcp" in content.lower():
                hook_path.unlink()
                removed.append(hook_name)

    if removed:
        return True, f"Removed hooks: {', '.join(removed)}"
    return True, "No daem0nmcp hooks found"
