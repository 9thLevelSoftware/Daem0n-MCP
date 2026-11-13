"""Expanded store defaults are authorized only by exact native counsel evidence."""

import copy

import pytest

from scripts.native_acceptance_scenarios import validate_receipts


@pytest.mark.parametrize("gateway", [False, True])
def test_exact_observed_seed_remedy_is_bound_to_original_session_call_and_scope(
    gateway,
):
    ws = "ws_owned"
    nonce = "native-followup-omp-daem0n-20261112"
    seed = {
        "workspace_id": ws,
        "record_type": "decision",
        "content": nonce + ": preserve real-source indexing and native client setup",
        "rationale": "Native MCP persistence and same-key replay acceptance",
        "idempotency_key": nonce + "-decision",
    }
    token_args = {
        **seed,
        "context": {},
        "tags": [],
        "relative_file_path": None,
        "preflight_token": "observed-token",
    }

    def arguments(value):
        return (
            {
                "workspace_id": value["workspace_id"],
                "tool": "memory_store",
                "arguments": {k: v for k, v in value.items() if k != "workspace_id"},
            }
            if gateway
            else value
        )

    canonical = "daem0n_tool_call" if gateway else "memory_store"
    retry_args = arguments(token_args)
    original = {
        "host": "omp",
        "canonical_tool": canonical,
        "operation": "memory_store",
        "arguments": arguments(seed),
        "call_id": "original",
        "native_session": "owned-session",
        "v7_responses": [
            {
                "api_version": "7",
                "ok": False,
                "data": None,
                "meta": {"workspace_id": ws, "request_id": "original-request"},
                "error": {
                    "code": "COUNSEL_REQUIRED",
                    "counsel": {"reasons": ["TEST"]},
                    "remedy": {"tool": canonical, "arguments": retry_args},
                },
            }
        ],
    }
    data = {"record": {"record_id": "mem_" + "a" * 64}}
    retry = {
        **original,
        "arguments": copy.deepcopy(retry_args),
        "call_id": "retry",
        "v7_responses": [
            {
                "api_version": "7",
                "ok": True,
                "meta": {"workspace_id": ws, "request_id": "retry-request"},
                "data": {"tool": "memory_store", "data": data} if gateway else data,
            }
        ],
    }
    outcome = {
        "host": "omp",
        "canonical_tool": "memory_record_outcome",
        "operation": "memory_record_outcome",
        "arguments": {
            "workspace_id": ws,
            "record_id": data["record"]["record_id"],
            "outcome_text": nonce + ": native persistence seed verified",
            "worked": True,
            "idempotency_key": nonce + "-outcome",
        },
        "v7_responses": [
            {"api_version": "7", "ok": True, "meta": {"workspace_id": ws}, "data": {}}
        ],
    }
    manifest = {
        "project": "daem0n",
        "file_count": 1,
        "scenario": {"subject_path": "a.py", "search": "known", "expected_callers": []},
    }

    def authorized(rows, stage="initial"):
        return validate_receipts(rows, stage, manifest, workspace_id=ws)["checks"][
            "only_stage_authorized_memory_writes"
        ]

    rows = [original, retry, outcome]
    preserved = copy.deepcopy(rows)
    assert authorized(rows)
    assert authorized(rows[:2], "restart")
    assert rows == preserved
    assert not authorized([retry, outcome])
    for field, value in (("tags", ["invented"]), ("preflight_token", "tampered")):
        changed = copy.deepcopy(rows)
        inner = (
            changed[1]["arguments"]["arguments"] if gateway else changed[1]["arguments"]
        )
        inner[field] = value
        assert not authorized(changed)
    for field, value in (
        ("native_session", "other-session"),
        ("call_id", "original"),
        ("native_session", None),
    ):
        changed = copy.deepcopy(rows)
        changed[1][field] = value
        assert not authorized(changed)
    changed = copy.deepcopy(rows)
    inner = changed[0]["arguments"]["arguments"] if gateway else changed[0]["arguments"]
    inner["content"] = "wrong original seed"
    assert not authorized(changed)
    changed = copy.deepcopy(rows)
    changed[1]["v7_responses"][0]["meta"]["workspace_id"] = "ws_foreign"
    assert not authorized(changed)
