"""V1 resource success must follow native tool output, not model narration."""

import json
from pathlib import Path


def test_native_resource_delta_checks_exact_store_identity_and_foreign_denial(
    monkeypatch,
):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / "scripts"))
    from native_opencode_resources import assess

    warning = {
        "workspace_id": "ws_test",
        "record_type": "warning",
        "content": "synthetic",
        "idempotency_key": "exact",
    }
    own = "memory://workspaces/ws_test/warnings"
    foreign = "memory://workspaces/ws_foreign/warnings"

    def event(tool, arguments, data=None, error=None):
        return {
            "type": "tool_use",
            "part": {
                "tool": tool,
                "sessionID": "native_session",
                "callID": tool + str(arguments),
                "state": {
                    "status": "error" if error else "completed",
                    "input": arguments,
                    "output": json.dumps(data),
                    "error": error,
                },
            },
        }

    def envelope(data):
        return {"api_version": "7", "ok": True, "data": data, "error": None, "meta": {}}

    def document(items):
        return {"workspace_id": "ws_test", "items": items}

    events = [
        event("daem0nmcp_session_brief", {"workspace_id": "ws_test"}, envelope({})),
        event("list_mcp_resources", {"server": "daem0nmcp"}, {"resources": []}),
        event(
            "list_mcp_resource_templates",
            {"server": "daem0nmcp"},
            {
                "resourceTemplates": [
                    {"uriTemplate": f"memory://workspaces/{{workspace_id}}/{kind}"}
                    for kind in ("warnings", "failures", "rules", "active-context")
                ]
            },
        ),
        event("read_mcp_resource", {"server": "daem0nmcp", "uri": own}, document([])),
        event(
            "daem0nmcp_memory_store",
            warning,
            envelope({"record": {"record_id": "mem_actual"}}),
        ),
        event(
            "read_mcp_resource",
            {"server": "daem0nmcp", "uri": own},
            document([{"record_id": "mem_actual"}]),
        ),
        event(
            "read_mcp_resource",
            {"server": "daem0nmcp", "uri": foreign},
            error="Resource unavailable",
        ),
        *[
            event(
                "read_mcp_resource",
                {"server": "daem0nmcp", "uri": f"memory://workspaces/ws_test/{kind}"},
                document([]),
            )
            for kind in ("failures", "rules", "active-context")
        ],
    ]
    assert assess(events, warning, foreign)["status"] == "passed"
    events[5]["part"]["state"]["output"] = json.dumps(
        document([{"record_id": "mem_other"}])
    )
    assert (
        assess(events, warning, foreign)["checks"][
            "same_uri_refreshed_with_actual_record"
        ]
        is False
    )
    events[5]["part"]["state"]["output"] = json.dumps(
        document([{"record_id": "mem_actual"}])
    )
    events[6]["part"]["state"]["error"] = "An unrelated failure"
    assert assess(events, warning, foreign)["status"] == "failed"


def test_native_error_log_supplements_generic_foreign_error_only_for_matching_uri(
    monkeypatch,
):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / "scripts"))
    from native_opencode_resources import assess

    foreign = "memory://workspaces/ws_foreign/warnings"
    warning = {"workspace_id": "ws_test"}
    event = {
        "type": "tool_use",
        "part": {
            "tool": "read_mcp_resource",
            "state": {
                "status": "error",
                "input": {"server": "daem0nmcp", "uri": foreign},
                "error": "Failed to read MCP resource",
            },
        },
    }
    line = f'clientName=daem0nmcp resourceUri={foreign} error="MCP error -32002: Resource unavailable"'
    assert (
        assess([event], warning, foreign, line)["checks"]["foreign_resource_denied"]
        is True
    )
    assert (
        assess([event], warning, foreign, line.replace("ws_foreign", "ws_other"))[
            "checks"
        ]["foreign_resource_denied"]
        is False
    )


def test_resource_receipts_require_one_matching_nonempty_native_session(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / "scripts"))
    from native_opencode_resources import assess

    warning = {"workspace_id": "ws_test"}

    def event(tool, arguments, data):
        return {
            "type": "tool_use",
            "part": {
                "sessionID": "same",
                "tool": tool,
                "callID": tool,
                "state": {
                    "status": "completed",
                    "input": arguments,
                    "output": json.dumps(
                        {"api_version": "7", "ok": True, "data": data, "meta": {}}
                    ),
                },
            },
        }

    events = [
        event("daem0nmcp_session_brief", warning, {}),
        event(
            "daem0nmcp_memory_store", warning, {"record": {"record_id": "mem_actual"}}
        ),
    ]
    assert assess(events, warning, "foreign")["checks"]["same_native_session"] is True
    events[1]["part"]["sessionID"] = "another"
    assert assess(events, warning, "foreign")["checks"]["same_native_session"] is False
