"""Claude Code PreToolUse hook: reminders by default, explicit edit enforcement.

Stdlib only (plus the ``_client`` helpers) so it stays fast on every edit.  Outside
a Daem0n project it is silent; inside one it adds a one-line reminder to the
model's context. Explicit enforcement uses the protected native edit bridge.
"""

from __future__ import annotations

import json
import os
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
    recall = 'daem0n_tool_call(tool="memory_recall_file"'
    if path:
        recall += f', arguments={{"relative_file_path": {json.dumps(path)}}}'
    recall += ")"
    text = f"Daem0n: before this edit, call {recall} for past decisions and warnings."
    return json.dumps(
        {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "additionalContext": text,
            }
        }
    )


def main() -> None:
    event = read_hook_event()
    mode = os.environ.get("DAEM0NMCP_AUTOMATION_MODE")
    if mode == "off":
        sys.exit(0)
    if mode == "enforce":
        from ..native_edit_gate import approve_native_edit

        if os.environ.get("DAEM0NMCP_EDIT_BRIDGE_MODE") == "remote-https":
            # The bridge validates this exact checkout against its protected
            # remote binding. A remote checkout need not have local storage.
            project_path = event.get("cwd")
        else:
            project = find_project_root(event)
            project_path = str(project) if project is not None else None
        if not isinstance(project_path, str) or not project_path:
            print("EDIT_HOST_WORKSPACE_UNAVAILABLE", file=sys.stderr)
            sys.exit(2)
        decision = approve_native_edit(event, project_path, environ=os.environ)
        if not decision.allowed:
            print(decision.explanation(), file=sys.stderr)
            sys.exit(2)
        sys.exit(0)
    if mode not in (None, "remind"):
        print("EDIT_AUTOMATION_MODE_INVALID", file=sys.stderr)
        sys.exit(2)
    try:
        output = reminder(event)
    except Exception:
        output = None
    if output:
        print(output)
    sys.exit(0)


if __name__ == "__main__":
    main()
