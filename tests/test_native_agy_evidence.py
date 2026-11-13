"""Binding checks for large native agy tool-output artifacts."""

from __future__ import annotations

import copy
import importlib
import json
from pathlib import Path

import pytest


@pytest.fixture
def evidence_module(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / "scripts"))
    return importlib.import_module("native_agy_evidence")


def native_event():
    return {
        "event": "step_update",
        "step_update": {
            "conversation_id": "12345678-1234-1234-1234-123456789abc",
            "step_index": 7,
            "state": "DONE",
            "step_type": "tool",
            "tool_name": "call_mcp_tool",
            "tool_info": {
                "parameters": {
                    "ServerName": "daem0nmcp",
                    "ToolName": "daem0n_tool_call",
                    "Arguments": {
                        "workspace_id": "ws_" + "a" * 24,
                        "tool": "code_impact_analyze",
                        "arguments": {},
                    },
                }
            },
        },
    }


def write_output(home: Path, event: dict, content: str) -> Path:
    step = event["step_update"]
    path = (
        home
        / ".gemini/antigravity-cli/brain"
        / step["conversation_id"]
        / ".system_generated/steps"
        / str(step["step_index"])
        / "output.txt"
    )
    path.parent.mkdir(parents=True)
    path.write_text(content)
    return path


def test_only_exact_completed_native_output_can_enrich_event(tmp_path, evidence_module):
    home = tmp_path / "private-home"
    home.mkdir()
    event = native_event()
    original = copy.deepcopy(event)
    content = json.dumps(
        {
            "api_version": "7",
            "ok": True,
            "data": {"secret": "selected-key"},
            "meta": {"request_id": "req_exact"},
        }
    )
    source = write_output(home, event, content)
    evidence = tmp_path / "evidence"
    result, provenance = evidence_module.retain_native_outputs(
        [event],
        home,
        evidence,
        clean=lambda text: text.replace("selected-key", "<redacted>"),
    )
    assert event == original
    assert source.read_text() == content
    assert "selected-key" not in result[0]["step_update"]["tool_info"]["output"]
    assert provenance[0]["step_index"] == 7
    assert (
        provenance[0]["native_parameters"]
        == event["step_update"]["tool_info"]["parameters"]
    )
    assert provenance[0]["request_ids"] == ["req_exact"]
    assert (
        "selected-key"
        not in (evidence / provenance[0]["retained_relative_path"]).read_text()
    )


def test_assistant_text_and_wrong_server_do_not_select_artifacts(
    tmp_path, evidence_module
):
    home = tmp_path / "home"
    home.mkdir()
    event = native_event()
    event["step_update"]["tool_info"]["parameters"]["ServerName"] = "other-server"
    source = write_output(
        home,
        event,
        json.dumps({"api_version": "7", "meta": {"request_id": "req_wrong"}}),
    )
    evidence = tmp_path / "evidence"
    events = [event, {"event": "result", "response": source.read_text()}]
    copied, provenance = evidence_module.retain_native_outputs(events, home, evidence)
    assert copied == events
    assert provenance == []
    assert not evidence.exists()


def test_native_step_cannot_follow_another_conversation_link(tmp_path, evidence_module):
    home = tmp_path / "home"
    home.mkdir()
    event = native_event()
    source = write_output(home, event, "placeholder")
    steps = source.parent.parent
    source.unlink()
    source.parent.rmdir()
    other = home / "other-conversation-output"
    other.mkdir()
    (other / "output.txt").write_text(
        json.dumps({"api_version": "7", "meta": {"request_id": "req_other"}})
    )
    try:
        (steps / "7").symlink_to(other, target_is_directory=True)
    except OSError:
        pytest.skip("platform does not permit creating symlinks")
    with pytest.raises(RuntimeError, match="owned conversation"):
        evidence_module.retain_native_outputs([event], home, tmp_path / "evidence")
