"""Native reminder outputs never grant permissions or manufacture tool calls."""

from daem0nmcp.native_reminders import REMINDER, reminder_result


def test_codex_session_and_edit_context_use_native_hook_shape():
    for name in ("SessionStart", "PreToolUse"):
        result = reminder_result("codex", name, {"hook_event_name": name})
        assert result == {
            "hookSpecificOutput": {"hookEventName": name, "additionalContext": REMINDER}
        }
        assert "permissionDecision" not in result["hookSpecificOutput"]


def test_agy_startup_injects_context_and_edit_defers_to_native_policy():
    start = reminder_result("agy", "SessionStart", {"conversationId": "probe"})
    assert start["injectSteps"][0]["systemMessage"]["systemMessage"] == REMINDER
    edit = reminder_result("agy", "PreToolUse", {"toolCall": {"name": "write_to_file"}})
    assert edit == {"decision": "allow", "reason": REMINDER}
    assert "permissionOverrides" not in edit
    assert reminder_result("agy", "Stop", {}) == {}
