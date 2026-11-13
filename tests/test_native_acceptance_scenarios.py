"""Prevent model narration or another MCP server from passing native acceptance."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.native_acceptance_fixtures import PROJECTS
from scripts.native_acceptance_scenarios import (
    instruction_evidence,
    native_receipts,
    scenario_prompt,
    validate_receipts,
)


def envelope():
    return {
        "api_version": "7",
        "ok": True,
        "data": {"items": []},
        "error": None,
        "meta": {"request_id": "req_fixture"},
    }


@pytest.mark.parametrize("host", ["claude", "codex", "agy", "opencode", "omp", "pi"])
def test_model_text_does_not_establish_native_tool_execution(host):
    text = json.dumps(envelope())
    events = [
        {"type": "text", "part": {"text": text}},
        {"sdk_message_type": "AssistantMessage", "content": [{"text": text}]},
        {"event": "result", "result": text},
        {
            "method": "item/completed",
            "params": {"item": {"type": "agentMessage", "text": text}},
        },
    ]
    assert native_receipts(events, host) == []


@pytest.mark.parametrize("host", ["omp", "pi"])
def test_another_server_cannot_impersonate_daem0n_by_tool_suffix(host):
    events = [
        {
            "type": "tool_execution_start",
            "toolCallId": "one",
            "toolName": "mcp__unrelated__code_search",
            "args": {"workspace_id": "ws_fixture"},
        },
        {"type": "tool_execution_end", "toolCallId": "one", "result": envelope()},
    ]
    assert native_receipts(events, host) == []


def test_native_gateway_receipt_preserves_executed_arguments_and_response():
    arguments = {
        "workspace_id": "ws_fixture",
        "tool": "code_search",
        "arguments": {"query": "known"},
    }
    events = [
        {
            "method": "item/completed",
            "params": {
                "threadId": "thread_one",
                "item": {
                    "id": "call_one",
                    "type": "mcpToolCall",
                    "server": "daem0nmcp",
                    "tool": "daem0n_tool_call",
                    "arguments": arguments,
                    "result": {"structuredContent": envelope()},
                },
            },
        }
    ]
    observed = native_receipts(events, "codex")
    assert len(observed) == 1
    assert observed[0]["operation"] == "code_search"
    assert observed[0]["arguments"] == arguments
    assert observed[0]["native_session"] == "thread_one"
    assert observed[0]["v7_responses"] == [envelope()]


def test_receipt_validator_requires_exact_counsel_retry_in_same_session():
    manifest = {
        "file_count": 1,
        "scenario": {"subject_path": "a.py", "search": "known", "expected_callers": []},
    }
    arguments = {"workspace_id": "ws_fixture", "preflight_token": "fixture-token"}
    challenge = {
        "host": "codex",
        "canonical_tool": "memory_store",
        "operation": "memory_store",
        "arguments": {"workspace_id": "ws_fixture"},
        "native_session": "one",
        "v7_responses": [
            {
                "api_version": "7",
                "ok": False,
                "data": None,
                "meta": {"request_id": "challenge"},
                "error": {
                    "code": "COUNSEL_REQUIRED",
                    "counsel": {"reasons": ["TEST"]},
                    "remedy": {"tool": "memory_store", "arguments": arguments},
                },
            }
        ],
    }
    retry = {
        **challenge,
        "arguments": arguments,
        "native_session": "another",
        "v7_responses": [envelope()],
    }
    report = validate_receipts(
        [challenge, retry], "initial", manifest, workspace_id="ws_fixture"
    )
    assert report["checks"]["exact_counsel_remedies"] is False
    assert report["counsel_remedies_verified"] == [False]
    retry["native_session"] = "one"
    report = validate_receipts(
        [challenge, retry], "initial", manifest, workspace_id="ws_other"
    )
    assert report["checks"]["exact_counsel_remedies"] is True
    assert report["checks"]["workspace_scope"] is False
    assert report["status"] == "failed"


def test_gateway_validation_unwraps_only_the_matching_executed_operation():
    manifest = {"file_count": 1, "scenario": PROJECTS["daem0n"]}
    response = {
        **envelope(),
        "data": {
            "tool": "code_index",
            "data": {
                "files_indexed": 1,
                "manifest": {"projection": "code", "generation": 1},
            },
        },
    }
    receipt = {
        "operation": "code_index",
        "canonical_tool": "daem0n_tool_call",
        "arguments": {
            "workspace_id": "ws_fixture",
            "tool": "code_index",
            "arguments": {},
        },
        "v7_responses": [response],
    }
    result = validate_receipts([receipt, receipt], "initial", manifest)
    assert result["checks"]["real_source_file_count"] is True
    assert result["checks"]["unchanged_index_manifest"] is True
    response["data"]["tool"] = "code_search"
    result = validate_receipts([receipt], "initial", manifest)
    assert result["checks"]["no_unexpected_mcp_errors"] is False
    assert result["unexpected_error_codes"] == ["GATEWAY_TOOL_MISMATCH"]


def test_instruction_nonce_is_not_supplied_by_prompt_or_accepted_from_tool_output():
    manifest = {
        "project": "daem0n",
        "scenario": PROJECTS["daem0n"],
        "instruction_sentinel": "unknown-unique-marker",
    }
    prompt = scenario_prompt("codex", "initial", "ws_fixture", manifest)
    assert manifest["instruction_sentinel"] not in prompt
    tool_event = {
        "method": "item/completed",
        "params": {
            "item": {
                "type": "mcpToolCall",
                "result": {"text": manifest["instruction_sentinel"]},
            }
        },
    }
    assert instruction_evidence([tool_event], "codex", manifest)["status"] == "failed"
    text_event = {
        "method": "item/completed",
        "params": {
            "item": {"type": "agentMessage", "text": manifest["instruction_sentinel"]}
        },
    }
    proof = instruction_evidence([text_event, tool_event], "codex", manifest)
    assert proof["status"] == "passed"
    assert proof["reported_before_first_tool"] is True


def test_bounded_native_approval_rejects_changed_scope_patterns_and_unobserved_ids(
    monkeypatch,
):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / "scripts"))
    from native_acceptance_drivers import _bounded_code_approval

    manifest = {"scenario": PROJECTS["daem0n"]}
    payload = {
        "workspace_id": "ws_fixture",
        "tool": "code_index",
        "arguments": {"relative_root": "daem0nmcp", "patterns": ["**/*.py"]},
    }
    request = {
        "method": "mcpServer/elicitation/request",
        "params": {
            "serverName": "daem0nmcp",
            "mode": "form",
            "_meta": {"tool_name": "daem0n_tool_call", "tool_params": payload},
        },
    }
    assert _bounded_code_approval(request, "ws_fixture", manifest, [])
    payload["workspace_id"] = "ws_other"
    assert _bounded_code_approval(request, "ws_fixture", manifest, []) is None
    payload["workspace_id"] = "ws_fixture"
    payload["arguments"]["force"] = True
    assert _bounded_code_approval(request, "ws_fixture", manifest, []) is None
    payload["arguments"] = {"relative_root": "..", "patterns": ["**/*.py"]}
    assert _bounded_code_approval(request, "ws_fixture", manifest, []) is None
    payload["tool"] = "code_impact_analyze"
    payload["arguments"] = {"code_entity_id": "unobserved-id", "max_depth": 2}
    assert _bounded_code_approval(request, "ws_fixture", manifest, []) is None


def test_omp_one_call_policy_parses_actual_native_frame_and_rejects_other_paths(
    monkeypatch,
):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / "scripts"))
    from native_acceptance_drivers import _bounded_omp_approval

    manifest = {"project": "daem0n", "scenario": PROJECTS["daem0n"]}
    payload = {
        "workspace_id": "ws_fixture",
        "query": "native-followup-omp-daem0n-20261112",
        "limit": 5,
    }
    title = "Allow tool: write\nPath: xd://mcp__daem0nmcp_memory_recall\nArguments:\n"
    event = {
        "method": "select",
        "options": ["Approve", "Approve for this session", "Deny"],
        "title": title + json.dumps(payload),
    }
    assert _bounded_omp_approval(event, "ws_fixture", manifest, [])
    assert _bounded_omp_approval(event, "ws_other", manifest, []) is None
    event["title"] = event["title"].replace(
        "xd://mcp__daem0nmcp_", "xd://mcp__foreign_"
    )
    assert _bounded_omp_approval(event, "ws_fixture", manifest, []) is None
    event["title"] = title + json.dumps({**payload, "limit": 500})
    assert _bounded_omp_approval(event, "ws_fixture", manifest, []) is None


def test_index_only_prompt_has_no_core_write_or_preload_request():
    manifest = {"project": "legion", "scenario": PROJECTS["legion"]}
    prompt = scenario_prompt("agy", "index", "ws_fixture", manifest)
    assert "Repeat the identical" in prompt
    assert "Before tools, report" not in prompt
    assert "Call memory_store" not in prompt
    assert "max_depth=2" in prompt


def test_omp_repair_prompt_supplies_exact_workspace_payloads_without_rebuilding_restart():
    manifest = {"project": "legion", "scenario": PROJECTS["legion"]}
    prompt = scenario_prompt("omp", "restart", "ws_fixture", manifest)
    assert (
        'For system_health use {"workspace_id": "ws_fixture", "include_components": true}'
        in prompt
    )
    assert (
        '"tool": "code_search", "arguments": {"query": "sha256utf8", "limit": 100}'
        in prompt
    )
    assert "On restart the code_index template is forbidden" in prompt


def test_native_numeric_templates_cover_only_requested_stage_operations():
    manifest = {"project": "legion", "scenario": PROJECTS["legion"]}
    for host in ("opencode", "pi", "omp"):
        prompt = scenario_prompt(host, "remove", "ws_fixture", manifest)
        assert '"limit": 100' in prompt
        assert '"max_depth": 2' in prompt
        assert '"limit": "100"' not in prompt
        assert "For the requested memory_store" not in prompt
        assert "For the requested memory_recall" not in prompt
        assert "For session_brief" not in prompt


def test_index_continuation_can_finish_only_the_missing_exact_outcome():
    pending = {
        "workspace_id": "ws_fixture",
        "record_id": "mem_prior",
        "outcome_text": "verified",
        "worked": True,
        "idempotency_key": "original-outcome",
    }
    manifest = {
        "project": "legion",
        "file_count": 1,
        "scenario": PROJECTS["legion"],
        "pending_outcome": pending,
    }
    assert json.dumps(pending) in scenario_prompt(
        "omp", "index", "ws_fixture", manifest
    )
    receipt = {
        "operation": "memory_record_outcome",
        "canonical_tool": "memory_record_outcome",
        "arguments": pending,
        "v7_responses": [envelope()],
    }
    assert (
        validate_receipts([receipt], "index", manifest)["checks"][
            "continued_native_outcome"
        ]
        is True
    )
    receipt["arguments"] = {**pending, "record_id": "mem_unobserved"}
    assert (
        validate_receipts([receipt], "index", manifest)["checks"][
            "continued_native_outcome"
        ]
        is False
    )


def test_nullable_covenant_is_valid_receipt_without_auto_brief():
    response = envelope()
    response["meta"]["covenant"] = None
    receipt = {
        "operation": "memory_recall",
        "canonical_tool": "memory_recall",
        "arguments": {"workspace_id": "ws_fixture"},
        "v7_responses": [response],
    }
    result = validate_receipts(
        [receipt], "restart", {"file_count": 1, "scenario": PROJECTS["legion"]}
    )
    assert result["checks"]["guided_auto_brief"] is False


def test_denied_native_request_stays_failed_despite_successful_functional_checks(
    tmp_path, monkeypatch
):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / "scripts"))
    from native_acceptance_drivers import retain_approval_denials

    directory = tmp_path / "approvals"
    directory.mkdir()
    (directory / "exact.receipt.json").write_text(
        json.dumps(
            {"choice": "Deny", "request_sha256": "exact", "reason": "Missing workspace"}
        )
    )
    validation = {"status": "passed", "checks": {"all_functional_checks": True}}
    retain_approval_denials(tmp_path, validation)
    assert validation["status"] == "failed"
    assert validation["checks"]["all_functional_checks"] is True
    assert validation["checks"]["no_native_denied_calls"] is False
    assert validation["native_approval_denials"][0]["request_sha256"] == "exact"


def test_omp_gateway_and_component_health_remain_exact_payload_scoped(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / "scripts"))
    from native_acceptance_drivers import _bounded_omp_approval

    manifest = {"project": "daem0n", "scenario": PROJECTS["daem0n"]}

    def frame(tool, payload):
        return {
            "method": "select",
            "options": ["Approve", "Deny"],
            "title": f"Allow tool: write\nPath: xd://mcp__daem0nmcp_{tool}\nArguments:\n"
            + json.dumps(payload),
        }

    gateway = {
        "workspace_id": "ws_fixture",
        "tool": "code_index",
        "arguments": {"relative_root": "daem0nmcp", "patterns": ["**/*.py"]},
    }
    assert _bounded_omp_approval(
        frame("daem0n_tool_call", gateway), "ws_fixture", manifest, []
    )
    assert _bounded_omp_approval(
        frame(
            "system_health", {"workspace_id": "ws_fixture", "include_components": True}
        ),
        "ws_fixture",
        manifest,
        [],
    )
    assert _bounded_omp_approval(
        frame(
            "daem0n_tools_search",
            {"query": "code index repository source files manifest"},
        ),
        "ws_fixture",
        manifest,
        [],
    )
    gateway["arguments"]["patterns"].append("../*")
    assert (
        _bounded_omp_approval(
            frame("daem0n_tool_call", gateway), "ws_fixture", manifest, []
        )
        is None
    )
    assert (
        _bounded_omp_approval(
            frame(
                "system_health",
                {
                    "workspace_id": "ws_fixture",
                    "include_components": True,
                    "extra": True,
                },
            ),
            "ws_fixture",
            manifest,
            [],
        )
        is None
    )
