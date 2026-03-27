"""
Claude Code PostToolUse hook for Edit/Write - suggest remembering significant changes.

Runs after Edit/Write tools complete. Analyzes the change and suggests
calling inscribe() for significant modifications. Never blocks.
"""

import sys
from pathlib import Path

from ._client import get_file_path_from_input, get_project_path, get_tool_input, succeed

# Patterns indicating architecturally or operationally significant changes
SIGNIFICANT_PATTERNS = [
    "class ",
    "def __init__",
    "async def ",
    "@dataclass",
    "@mcp.tool",
    "config",
    "settings",
    "environment",
    "auth",
    "password",
    "token",
    "secret",
    "credential",
    "migration",
    "schema",
    "model",
    "table",
    "column",
    "endpoint",
    "route",
    "api",
    "request",
    "response",
]

# File types that are usually significant
SIGNIFICANT_EXTENSIONS = {
    ".py",
    ".ts",
    ".js",
    ".go",
    ".rs",
    ".java",
    ".yaml",
    ".yml",
    ".json",
    ".toml",
    ".sql",
    ".prisma",
}


def _is_significant(file_path: str, change_content: str) -> bool:
    """Determine if a change is significant enough to suggest remembering."""
    ext = Path(file_path).suffix.lower()
    if ext not in SIGNIFICANT_EXTENSIONS:
        return False

    change_lower = change_content.lower()
    for pattern in SIGNIFICANT_PATTERNS:
        if pattern.lower() in change_lower:
            return True

    if len(change_content) > 500:
        return True

    return False


def main() -> None:
    project_path = get_project_path()
    if project_path is None:
        sys.exit(0)

    file_path = get_file_path_from_input()
    if not file_path:
        sys.exit(0)

    # Extract change content from tool input
    data = get_tool_input()
    old_string = data.get("old_string", "")
    new_string = data.get("new_string", "")
    content = data.get("content", "")  # Write tool uses 'content'
    change_content = f"{old_string} {new_string} {content}"

    if not _is_significant(file_path, change_content):
        sys.exit(0)

    filename = Path(file_path).name
    succeed(
        f"[Daem0n suggests] Significant change to {filename}. "
        f"Consider: inscribe(action='remember', content='...', file_path='{file_path}')"
    )


if __name__ == "__main__":
    import warnings

    warnings.filterwarnings("ignore")

    from daem0nmcp.claude_hooks._client import run_hook_safely

    run_hook_safely(main)
