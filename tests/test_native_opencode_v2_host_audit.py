"""The companion audit retains host failures that MCP-only checks cannot see."""

from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path

import pytest

from scripts.native_opencode_v2_host_audit import audit_direct_restart

BACKEND = {"checks": {"exact_counsel_remedies": True}}


def pair(
    tool="opencode_list_mcp_resources",
    arguments=None,
    call_id="call_native",
    session="session_native",
    **after,
):
    base = {
        "tool": tool,
        "input": {"server": "daem0nmcp"} if arguments is None else arguments,
        "id": call_id,
        "sessionID": session,
    }
    return [
        {"event": "before", **deepcopy(base)},
        {
            "event": "after",
            **deepcopy(base),
            "status": "completed",
            "result": {"output": {"resources": [], "templates": []}},
            **after,
        },
    ]


def audit(events, backend=BACKEND):
    return audit_direct_restart(events, backend, workspace_id="ws_fixture")


def codes(result):
    return {failure["code"] for failure in result["failures"]}


def test_scoped_metadata_control_does_not_mutate_events_or_backend():
    events = pair()
    original = deepcopy(events)
    assert audit(events)["status"] == "passed"
    assert events == original
    assert BACKEND == {"checks": {"exact_counsel_remedies": True}}


def test_outer_completed_cannot_erase_inner_denial_with_same_call_id():
    outer = pair("execute", {"code": "await tools.opencode.list_mcp_resources({})"})
    inner = pair(
        arguments={},
        status="error",
        error={
            "name": "Tool.Error",
            "message": "Permission denied: opencode_list_mcp_resources",
        },
    )
    inner[-1].pop("result")
    result = audit([outer[0], *inner, outer[1]])
    assert result["status"] == "failed"
    assert codes(result) == {"RESOURCE_METADATA_SCOPE", "NATIVE_ERROR_OR_DENIAL"}


@pytest.mark.parametrize(
    "arguments",
    [{}, {"server": "acceptance_sentinel"}, {"server": "daem0nmcp", "extra": True}],
)
def test_metadata_scope_is_exact(arguments):
    assert "RESOURCE_METADATA_SCOPE" in codes(audit(pair(arguments=arguments)))


@pytest.mark.parametrize(
    "tool",
    ["opencode_read_mcp_resource", "edit", "write", "shell", "network", "unknown_tool"],
)
def test_content_reads_and_other_unplanned_native_tools_fail(tool):
    assert "UNPLANNED_NATIVE_TOOL" in codes(
        audit(pair(tool, {"server": "daem0nmcp", "uri": "memory://anything"}))
    )


@pytest.mark.parametrize(
    "changed",
    [
        "missing_after",
        "missing_before",
        "changed_input",
        "changed_session",
        "unknown_status",
        "unknown_event",
        "result_error",
    ],
)
def test_incomplete_unknown_or_error_variants_fail_closed(changed):
    events = pair()
    if changed == "missing_after":
        events.pop()
    elif changed == "missing_before":
        events.pop(0)
    elif changed == "changed_input":
        events[-1]["input"] = {}
    elif changed == "changed_session":
        events[-1]["sessionID"] = "other_session"
    elif changed == "unknown_status":
        events[-1]["status"] = "skipped"
    elif changed == "unknown_event":
        events.append({"event": "unknown_variant"})
    else:
        events[-1]["result"]["output"]["isError"] = True
    assert audit(events)["status"] == "failed"


def envelope(ok, error=None):
    return {
        "api_version": "7",
        "ok": ok,
        "error": error,
        "meta": {"workspace_id": "ws_fixture"},
    }


def counsel_events():
    arguments = {
        "workspace_id": "ws_fixture",
        "content": "exact decision",
        "preflight_token": "test_capability",
    }
    response = envelope(
        False,
        {
            "code": "COUNSEL_REQUIRED",
            "counsel": {"guidance": {}},
            "remedy": {"tool": "memory_store", "arguments": arguments},
        },
    )
    first = pair(
        "daem0nmcp_memory_store",
        {"workspace_id": "ws_fixture", "content": "exact decision"},
        status="error",
        error={"name": "Tool.Error", "message": json.dumps(response)},
    )
    first[-1].pop("result")
    later = pair(
        "daem0nmcp_memory_store",
        arguments,
        call_id="retry_native",
        result={"output": envelope(True)},
    )
    return [*first, *later]


def test_only_verified_exact_same_session_counsel_retry_is_exempt():
    result = audit(counsel_events())
    assert result["status"] == "passed"
    assert result["verified_expected_counsel_call_ids"] == ["call_native"]


@pytest.mark.parametrize(
    "changed",
    [
        "backend_false",
        "changed_retry",
        "other_session",
        "same_call_id",
        "missing_retry",
    ],
)
def test_counsel_exemption_cannot_hide_failed_provenance(changed):
    events = counsel_events()
    backend = deepcopy(BACKEND)
    if changed == "backend_false":
        backend["checks"]["exact_counsel_remedies"] = False
    elif changed == "missing_retry":
        events = events[:2]
    else:
        for event in events[2:]:
            if changed == "changed_retry":
                event["input"]["content"] = "different"
            elif changed == "other_session":
                event["sessionID"] = "other_session"
            else:
                event["id"] = "call_native"
    assert "NATIVE_ERROR_OR_DENIAL" in codes(audit(events, backend))


def test_retained_round10_actual_traces_remain_red():
    root = Path(__file__).resolve().parents[1]
    evidence = (
        root
        / ".test_tmp/native-followup-20261112/repair-10/v2/native-helper-denial-details.json"
    )
    if not evidence.is_file():
        pytest.skip(
            "Disposable native evidence is retained locally, not in source distributions"
        )
    rows = json.loads(evidence.read_text())["details"]
    for row in rows:
        path = root / row["evidence"]
        events = [json.loads(line) for line in path.read_text().splitlines()]
        validation = json.loads(path.with_name("validation.json").read_text())
        workspace = next(
            e["input"]["workspace_id"]
            for e in events
            if e.get("event") == "before" and "workspace_id" in e.get("input", {})
        )
        result = audit_direct_restart(events, validation, workspace_id=workspace)
        assert result["status"] == "failed"
        assert {"RESOURCE_METADATA_SCOPE", "NATIVE_ERROR_OR_DENIAL"} <= codes(result)


@pytest.mark.parametrize(
    "output",
    [
        None,
        {},
        {"resources": None, "templates": []},
        {"resources": [], "templates": "not an array"},
        {
            "resources": [{"server": "other", "name": "foreign", "uri": "x:"}],
            "templates": [],
        },
        {
            "resources": [{"server": "daem0nmcp", "name": "bad", "uri": 1}],
            "templates": [],
        },
        {
            "resources": [],
            "templates": [
                {"server": "other", "name": "foreign", "uriTemplate": "x:{id}"}
            ],
        },
        {"resources": [], "templates": [], "contents": [{"text": "read payload"}]},
        {
            "resources": [
                {"server": "daem0nmcp", "name": "read", "uri": "x:", "text": "payload"}
            ],
            "templates": [],
        },
        {
            "resources": [
                {"server": "daem0nmcp", "name": "bad", "uri": "x:", "mimeType": 5}
            ],
            "templates": [],
        },
        {
            "resources": [{"server": "daem0nmcp", "name": "x" * 16385, "uri": "x:"}],
            "templates": [],
        },
        {
            "resources": [{"server": "daem0nmcp", "name": "x", "uri": "x:"}] * 257,
            "templates": [],
        },
    ],
)
def test_metadata_output_must_match_pinned_bounded_scope(output):
    assert "RESOURCE_METADATA_RESULT" in codes(audit(pair(result={"output": output})))


@pytest.mark.parametrize("relationship", ["prior", "future", "reply_after_outer"])
def test_execute_requires_completed_child_inside_its_exact_interval(relationship):
    outer = pair("execute", {"code": "await scopedMetadata()"})
    child = pair()
    if relationship == "prior":
        events = [*child, *outer]
    elif relationship == "future":
        events = [*outer, *child]
    else:
        events = [outer[0], child[0], outer[1], child[1]]
    assert "UNOBSERVED_EXECUTE_INNER_CALL" in codes(audit(events))


def test_counsel_envelope_cannot_hide_unrelated_native_error():
    events = counsel_events()
    events[1]["error"]["name"] = "Tool.Error"
    events[1]["error"]["message"] = (
        "Permission denied: memory_store " + events[1]["error"]["message"]
    )
    assert "NATIVE_ERROR_OR_DENIAL" in codes(audit(events))


def test_counsel_in_result_cannot_hide_separate_permission_denial():
    events = counsel_events()
    original = json.loads(events[1]["error"]["message"])
    events[1]["error"] = {
        "name": "Tool.Error",
        "message": "Permission denied: memory_store",
    }
    events[1]["result"] = {"output": original}
    assert "NATIVE_ERROR_OR_DENIAL" in codes(audit(events))


def test_completed_success_with_nonempty_native_error_fails():
    events = pair(error={"name": "Tool.Error", "message": "Permission denied"})
    assert "NATIVE_ERROR_OR_DENIAL" in codes(audit(events))


def test_counsel_remedy_keeps_boolean_and_integer_types_distinct():
    events = counsel_events()
    challenge = json.loads(events[1]["error"]["message"])
    challenge["error"]["remedy"]["arguments"]["flag"] = 1
    events[1]["error"]["message"] = json.dumps(challenge)
    for event in events[2:]:
        event["input"]["flag"] = True
    assert "NATIVE_ERROR_OR_DENIAL" in codes(audit(events))


def test_pinned_resource_and_template_metadata_pass_inside_current_execute():
    catalog = {
        "resources": [
            {
                "server": "daem0nmcp",
                "name": "warnings",
                "uri": "memory://workspaces/ws_fixture/warnings",
                "description": "Metadata only",
                "mimeType": "application/json",
            }
        ],
        "templates": [
            {
                "server": "daem0nmcp",
                "name": "rules",
                "uriTemplate": "memory://workspaces/{workspace_id}/rules",
            }
        ],
    }
    outer = pair("execute", {"code": "await scopedMetadata()"})
    inner = pair(result={"output": catalog})
    assert audit([outer[0], *inner, outer[1]])["status"] == "passed"


def test_nested_success_envelope_is_not_a_verified_counsel_retry():
    events = counsel_events()
    events[-1]["result"]["output"] = {"nested": envelope(True)}
    assert "NATIVE_ERROR_OR_DENIAL" in codes(audit(events))
