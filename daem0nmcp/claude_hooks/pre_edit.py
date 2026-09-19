"""Claude Code PreToolUse hook for edits: remind, never block.

Stdlib only (plus ``read_hook_event``) so it stays fast on every edit.  Outside
a Daem0n project it is silent; inside one it adds a one-line reminder to the
model's context and always exits 0.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from ._client import read_hook_event


def _relative_path(project: Path, tool_input: object) -> str | None:
    if not isinstance(tool_input, dict):
        return None
    raw = tool_input.get("file_path") or tool_input.get("notebook_path")
    if not isinstance(raw, str) or not raw:
        return None
    try:
        return (project / raw).resolve().relative_to(project.resolve()).as_posix()
    except (OSError, ValueError):
        return None


def reminder(event: dict) -> str | None:
    """Return the reminder JSON for this event, or None to stay silent."""
    root = event.get("cwd") or os.environ.get("CLAUDE_PROJECT_DIR")
    if not isinstance(root, str) or not root:
        return None
    project = Path(root)
    if not (project / ".daem0nmcp").is_dir():
        return None
    path = _relative_path(project, event.get("tool_input"))
    recall = (
        f'memory_recall_file(relative_file_path="{path}")'
        if path
        else "memory_recall_file"
    )
    text = (
        f"Daem0n: before this edit, call {recall} for past decisions and "
        "warnings, and memory_preflight for the exact change."
    )
    return json.dumps(
        {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "additionalContext": text,
            }
        }
    )


def main() -> None:
    try:
        output = reminder(read_hook_event())
    except Exception:
        output = None
    if output:
        print(output)
    sys.exit(0)


if __name__ == "__main__":
    main()
