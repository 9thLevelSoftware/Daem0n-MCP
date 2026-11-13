"""Omitted agy stream fields require matched native completion artifacts."""

import json
from pathlib import Path

import pytest


def test_retained_resource_result_requires_native_completion_not_model_prose(
    tmp_path, monkeypatch
):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / "scripts"))
    from native_agy_resources import retained_resource_receipts

    conversation = "11111111-2222-3333-4444-555555555555"
    root = (
        tmp_path
        / "private/home/.gemini/antigravity-cli/brain"
        / conversation
        / ".system_generated"
    )
    logs, steps = root / "logs", root / "steps/2"
    logs.mkdir(parents=True)
    steps.mkdir(parents=True)
    output = '[{"uri":"memory://workspaces/ws_test/warnings"}]'
    (steps / "output.txt").write_text(output, encoding="utf-8")
    proposed = {
        "step_index": 1,
        "type": "PLANNER_RESPONSE",
        "tool_calls": [
            {
                "name": "read_resource",
                "args": {
                    "ServerName": "daem0nmcp",
                    "Uri": "memory://workspaces/ws_test/warnings",
                },
            }
        ],
    }
    result = {"step_index": 2, "type": "GENERIC", "status": "DONE", "content": output}
    transcript = logs / "transcript_full.jsonl"
    transcript.write_text(
        json.dumps(proposed) + "\n" + json.dumps(result), encoding="utf-8"
    )
    events = [
        {"conversation_id": conversation},
        {
            "step_update": {
                "conversation_id": conversation,
                "step_index": 2,
                "state": "DONE",
                "step_type": "unknown",
            }
        },
    ]
    receipts = retained_resource_receipts(tmp_path, events)
    assert receipts[0]["output"] == output
    assert receipts[0]["step_index"] == 2
    result["type"] = "PLANNER_RESPONSE"
    transcript.write_text(
        json.dumps(proposed) + "\n" + json.dumps(result), encoding="utf-8"
    )
    with pytest.raises(ValueError, match="actual native completion"):
        retained_resource_receipts(tmp_path, events)
    result["type"] = "GENERIC"
    transcript.write_text(
        json.dumps(proposed) + "\n" + json.dumps(result), encoding="utf-8"
    )
    with pytest.raises(ValueError, match="actual native completion"):
        retained_resource_receipts(tmp_path, events[:1])
