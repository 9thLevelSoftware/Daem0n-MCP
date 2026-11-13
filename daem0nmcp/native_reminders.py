"""Small host-specific command hooks for covenant reminders only."""

from __future__ import annotations

import json
import sys
from typing import Any

from .claude_hooks._client import read_hook_event

REMINDER = (
    "Daem0n: follow the project covenant and the current task's call order. Every "
    "workspace-scoped call, including session_brief, must carry the supplied opaque "
    "workspace_id; never {} for a scoped call. Recall relevant decisions and file "
    "warnings before source edits. "
    "Guided memory_recall, memory_preflight, memory_store and memory_record_outcome "
    "automatically brief an unbriefed session; diagnostics do not. The full "
    "session_brief is optional in guided mode, not a prerequisite; do not prepend "
    "a briefing ritual. Strict mode retains its explicit briefing requirements. On "
    "COUNSEL_REQUIRED read error.counsel and retry exactly error.remedy in the "
    "same MCP session. Record verified outcomes using stable idempotency keys; "
    "indexing or searching alone does not require inventing memory calls or "
    "repeating an already recorded outcome."
)


def reminder_result(
    host: str, event_name: str, event: dict[str, Any]
) -> dict[str, Any]:
    """Return the declared native output shape without approving any tool."""
    if host == "agy":
        if event_name == "SessionStart":
            return {"injectSteps": [{"systemMessage": {"systemMessage": REMINDER}}]}
        if event_name == "PreToolUse":
            return {"decision": "allow", "reason": REMINDER}
        return {}
    if host == "codex" and event_name in {"SessionStart", "PreToolUse"}:
        return {
            "hookSpecificOutput": {
                "hookEventName": event_name,
                "additionalContext": REMINDER,
            }
        }
    if host == "codex" and event_name == "Stop":
        return {
            "systemMessage": "Daem0n: record the verified outcome of completed work."
        }
    return {}


def main() -> None:
    if len(sys.argv) != 3 or sys.argv[1] not in {"codex", "agy"}:
        return
    event = read_hook_event()
    if event:
        print(json.dumps(reminder_result(sys.argv[1], sys.argv[2], event)))


if __name__ == "__main__":
    main()
