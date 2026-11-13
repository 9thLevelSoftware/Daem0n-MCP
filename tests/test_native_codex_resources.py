"""Resource deltas require actual matching native execution and JSON documents."""

import json
from pathlib import Path


def test_resource_change_binds_warning_id_exact_write_scope_and_read_uri(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / "scripts"))
    from native_acceptance_drivers import validate_resource_change

    store = {
        "workspace_id": "ws_fixture",
        "record_type": "warning",
        "content": "sentinel",
        "idempotency_key": "one",
    }
    uri = "memory://workspaces/ws_fixture/warnings"

    def resource(items):
        return {
            "contents": [
                {
                    "uri": uri,
                    "text": json.dumps({"workspace_id": "ws_fixture", "items": items}),
                }
            ]
        }

    receipts = [
        {
            "operation": "session_brief",
            "native_session": "native_session",
            "arguments": {"workspace_id": "ws_fixture"},
            "v7_responses": [{"ok": True}],
        },
        {
            "operation": "memory_store",
            "native_session": "native_session",
            "arguments": store,
            "v7_responses": [
                {"ok": True, "data": {"record": {"record_id": "mem_exact"}}}
            ],
        },
    ]
    evidence = {
        "thread_id": "native_session",
        "uri": uri,
        "before": resource([]),
        "after": resource([{"record_id": "mem_exact"}]),
    }
    assert validate_resource_change(receipts, evidence, store)["status"] == "passed"
    evidence["after"] = resource([{"record_id": "mem_other"}])
    assert validate_resource_change(receipts, evidence, store)["status"] == "failed"
    evidence["after"] = resource([{"record_id": "mem_exact"}])
    evidence["after"]["contents"][0]["uri"] = "memory://workspaces/ws_other/warnings"
    assert validate_resource_change(receipts, evidence, store)["status"] == "failed"
    evidence["after"] = resource([{"record_id": "mem_exact"}])
    receipts[1]["arguments"] = {**store, "workspace_id": "ws_other"}
    assert validate_resource_change(receipts, evidence, store)["status"] == "failed"
    receipts[1]["arguments"] = store
    receipts[1]["native_session"] = "another_session"
    assert validate_resource_change(receipts, evidence, store)["status"] == "failed"
