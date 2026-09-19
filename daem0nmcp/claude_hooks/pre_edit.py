"""Claude Code PreToolUse hook for edits: remind, never block.

Stdlib only (plus the ``_client`` helpers) so it stays fast on every edit.  Outside
a Daem0n project it is silent; inside one it adds a one-line reminder to the
model's context and always exits 0.
"""

from __future__ import annotations

import json
import sys

from ._client import find_project_root, read_hook_event, relative_project_path


def reminder(event: dict) -> str | None:
    """Return the reminder JSON for this event, or None to stay silent."""
    project = find_project_root(event)
    if project is None:
        return None
    tool_input = event.get("tool_input")
    raw = (
        tool_input.get("file_path") or tool_input.get("notebook_path")
        if isinstance(tool_input, dict)
        else None
    )
    path = relative_project_path(project, raw, event.get("cwd"))
    recall = (
        f"memory_recall_file(relative_file_path={json.dumps(path)})"
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
