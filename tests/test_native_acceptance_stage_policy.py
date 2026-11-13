"""Historical native frames must follow bounded, stage-specific test authority."""

import importlib
import json
from pathlib import Path

import pytest


@pytest.fixture
def policy(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / "scripts"))
    return importlib.import_module("native_acceptance_drivers")


def frame(tool, arguments):
    return {
        "method": "select",
        "options": ["Approve", "Approve for this session", "Deny"],
        "title": f"Allow tool: write\nPath: xd://mcp__daem0nmcp_{tool}\nArguments:\n"
        + json.dumps(arguments),
    }


def test_discovery_accepts_bounded_readonly_queries_without_keyword_allowlist(policy):
    manifest = {"project": "daem0n", "scenario": {}}
    for payload in (
        {"query": "memory warnings"},
        {"query": "index repository", "limit": 1},
    ):
        assert policy._bounded_omp_approval(
            frame("daem0n_tools_search", payload), "ws_owned", manifest, []
        )
    for changes in (
        {"limit": 0},
        {"limit": 11},
        {"limit": True},
        {"limit": "10"},
        {"limit": 1.0},
        {"workspace_id": "ws_foreign"},
        {"query": " "},
        {"query": "x" * 257},
    ):
        assert (
            policy._bounded_omp_approval(
                frame("daem0n_tools_search", {"query": "code", **changes}),
                "ws_owned",
                manifest,
                [],
            )
            is None
        )


def test_memory_writes_are_only_exact_stage_payloads(policy):
    manifest = {"project": "daem0n", "scenario": {}}
    seed = {
        "workspace_id": "ws_owned",
        "record_type": "decision",
        "content": "native-followup-omp-daem0n-20261112: preserve real-source indexing and native client setup",
        "rationale": "Native MCP persistence and same-key replay acceptance",
        "idempotency_key": "native-followup-omp-daem0n-20261112-decision",
    }
    for stage in ("initial", "restart"):
        assert policy._bounded_omp_approval(
            frame("memory_store", seed), "ws_owned", manifest, [], stage=stage
        )
    for stage in (None, "mutate", "remove", "index"):
        assert (
            policy._bounded_omp_approval(
                frame("memory_store", seed), "ws_owned", manifest, [], stage=stage
            )
            is None
        )
    assert (
        policy._bounded_omp_approval(
            frame("memory_store", {**seed, "workspace_id": "ws_foreign"}),
            "ws_owned",
            manifest,
            [],
            stage="initial",
        )
        is None
    )


def test_unrecognized_native_request_denies_immediately_and_keeps_strict_failure(
    policy, tmp_path, monkeypatch
):
    monkeypatch.setattr(
        policy.time, "sleep", lambda _delay: pytest.fail("Unexpected approval wait")
    )
    request = {
        "method": "select",
        "title": "Allow tool: bash\nCommand: ls",
        "options": ["Approve", "Deny"],
    }
    assert (
        policy._decision(
            tmp_path, request, ["Approve", "Deny"], 600, unknown_policy="deny"
        )
        == "Deny"
    )
    result = {"status": "passed", "checks": {"functional": True}}
    policy.retain_approval_denials(tmp_path, result)
    assert result["status"] == "failed"
    assert result["checks"]["functional"] is True
    assert result["checks"]["no_native_denied_calls"] is False


def test_global_namespace_eval_stays_denied(policy):
    request = {
        "method": "select",
        "options": ["Approve", "Deny"],
        "title": "Allow tool: eval\nLanguage: python\nCode:\nprint([t for t in dir() if not t.startswith('_')])",
    }
    assert (
        policy._bounded_omp_approval(request, "ws_owned", {"project": "daem0n"}, [])
        is None
    )


def test_successful_extra_write_cannot_pass_index_only_validation(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / "scripts"))
    scenarios = importlib.import_module("native_acceptance_scenarios")
    fixtures = importlib.import_module("native_acceptance_fixtures")
    manifest = {
        "project": "daem0n",
        "file_count": 1,
        "scenario": fixtures.PROJECTS["daem0n"],
    }
    receipt = {
        "host": "omp",
        "operation": "memory_store",
        "canonical_tool": "memory_store",
        "arguments": {"workspace_id": "ws_owned"},
        "v7_responses": [
            {
                "api_version": "7",
                "ok": True,
                "meta": {"request_id": "actual"},
                "data": {"record": {"record_id": "mem_one"}},
            }
        ],
    }
    result = scenarios.validate_receipts(
        [receipt], "index", manifest, workspace_id="ws_owned"
    )
    assert result["checks"]["only_stage_authorized_memory_writes"] is False
    assert result["status"] == "failed"


def test_outcome_requires_same_workspace_observed_seed_and_initial_stage(policy):
    nonce = "native-followup-omp-daem0n-20261112"
    seed = {
        "workspace_id": "ws_owned",
        "record_type": "decision",
        "content": nonce + ": preserve real-source indexing and native client setup",
        "rationale": "Native MCP persistence and same-key replay acceptance",
        "idempotency_key": nonce + "-decision",
    }
    outcome = {
        "workspace_id": "ws_owned",
        "record_id": "mem_" + "a" * 64,
        "outcome_text": nonce + ": native persistence seed verified",
        "worked": True,
        "idempotency_key": nonce + "-outcome",
    }
    manifest = {"project": "daem0n", "scenario": {}}
    events = [
        {
            "type": "tool_execution_start",
            "toolCallId": "native-store",
            "toolName": "mcp__daem0nmcp_memory_store",
            "args": seed,
        },
        {
            "type": "tool_execution_end",
            "toolCallId": "native-store",
            "toolName": "mcp__daem0nmcp_memory_store",
            "result": {
                "api_version": "7",
                "ok": True,
                "meta": {"workspace_id": "ws_owned", "request_id": "actual"},
                "data": {"record": {"record_id": outcome["record_id"]}},
            },
        },
    ]
    assert policy._bounded_omp_approval(
        frame("memory_record_outcome", outcome),
        "ws_owned",
        manifest,
        events,
        stage="initial",
    )
    for stage in ("mutate", "remove", "restart", "index"):
        assert (
            policy._bounded_omp_approval(
                frame("memory_record_outcome", outcome),
                "ws_owned",
                manifest,
                events,
                stage=stage,
            )
            is None
        )
    events[0]["args"] = {**seed, "workspace_id": "ws_foreign"}
    assert (
        policy._bounded_omp_approval(
            frame("memory_record_outcome", outcome),
            "ws_owned",
            manifest,
            events,
            stage="initial",
        )
        is None
    )


def test_minimal_index_setup_requires_one_mutated_index_and_no_core_calls(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / "scripts"))
    scenarios = importlib.import_module("native_acceptance_scenarios")
    fixtures = importlib.import_module("native_acceptance_fixtures")
    manifest = {
        "project": "daem0n",
        "file_count": 1,
        "scenario": fixtures.PROJECTS["daem0n"],
    }
    index = {
        "host": "omp",
        "operation": "code_index",
        "canonical_tool": "code_index",
        "arguments": {"workspace_id": "ws_owned"},
        "v7_responses": [
            {
                "api_version": "7",
                "ok": True,
                "meta": {"request_id": "index"},
                "data": {"files_indexed": 2, "manifest": {"generation": 1}},
            }
        ],
    }
    result = scenarios.validate_receipts(
        [index], "index_setup", manifest, workspace_id="ws_owned"
    )
    assert result["status"] == "passed"
    assert (
        scenarios.validate_receipts(
            [index, index], "index_setup", manifest, workspace_id="ws_owned"
        )["status"]
        == "failed"
    )
    extra = {**index, "operation": "system_health", "canonical_tool": "system_health"}
    assert (
        scenarios.validate_receipts(
            [index, extra], "index_setup", manifest, workspace_id="ws_owned"
        )["status"]
        == "failed"
    )
    prompt = scenarios.scenario_prompt("omp", "index_setup", "ws_owned", manifest)
    assert "exactly ONE actual code_index" in prompt
    assert "Finally call system_health" not in prompt


def test_stage_rejects_unplanned_gateway_mutator_and_normalizes_outcome():
    from scripts.native_acceptance_scenarios import validate_receipts

    manifest = {
        "project": "daem0n",
        "file_count": 1,
        "scenario": {"subject_path": "a.py", "search": "known", "expected_callers": []},
    }

    def receipt(operation, arguments, data, gateway=False):
        return {
            "host": "omp",
            "operation": operation,
            "canonical_tool": "daem0n_tool_call" if gateway else operation,
            "arguments": {
                "workspace_id": "ws_owned",
                "tool": operation,
                "arguments": {
                    k: v for k, v in arguments.items() if k != "workspace_id"
                },
            }
            if gateway
            else arguments,
            "v7_responses": [
                {
                    "api_version": "7",
                    "ok": True,
                    "meta": {"request_id": operation},
                    "data": {"tool": operation, "data": data} if gateway else data,
                }
            ],
        }

    args = {"workspace_id": "ws_owned"}
    index = receipt(
        "code_index", args, {"files_indexed": 1, "manifest": {"generation": 1}}
    )
    calls = [
        index,
        index,
        receipt(
            "code_search",
            args,
            {"items": [{"relative_file_path": "a.py", "qualified_name": "known"}]},
        ),
        receipt(
            "code_impact_analyze",
            args,
            {"subject": {"relative_file_path": "a.py"}, "affected": []},
        ),
        receipt("system_health", args, {}),
    ]
    assert (
        validate_receipts(calls, "index", manifest, workspace_id="ws_owned")["status"]
        == "passed"
    )
    for operation in ("memory_prune", "rule_create", "active_context_clear"):
        result = validate_receipts(
            calls + [receipt(operation, args, {}, True)],
            "index",
            manifest,
            workspace_id="ws_owned",
        )
        assert result["checks"]["only_stage_authorized_operations"] is False
        assert result["status"] == "failed"
    nonce = "native-followup-omp-daem0n-20261112"
    seed = {
        **args,
        "record_type": "decision",
        "content": nonce + ": preserve real-source indexing and native client setup",
        "rationale": "Native MCP persistence and same-key replay acceptance",
        "idempotency_key": nonce + "-decision",
    }
    outcome = {
        **args,
        "record_id": "mem_" + "a" * 64,
        "outcome_text": nonce + ": native persistence seed verified",
        "worked": True,
        "idempotency_key": nonce + "-outcome",
    }
    for gateway in (False, True):
        writes = [
            receipt(
                "memory_store",
                seed,
                {"record": {"record_id": outcome["record_id"]}},
                gateway,
            ),
            receipt("memory_record_outcome", outcome, {}, gateway),
        ]
        assert validate_receipts(writes, "initial", manifest, workspace_id="ws_owned")[
            "checks"
        ]["only_stage_authorized_memory_writes"]
        assert not validate_receipts(
            list(reversed(writes)), "initial", manifest, workspace_id="ws_owned"
        )["checks"]["only_stage_authorized_memory_writes"]
        retry = json.loads(json.dumps(writes[0]))
        retry_arguments = (
            retry["arguments"]["arguments"] if gateway else retry["arguments"]
        )
        retry_arguments["challenge_token"] = "actual-counsel-token"
        retry.update(native_session="native-session", call_id="retry-call")
        retry["v7_responses"][0]["meta"]["workspace_id"] = "ws_owned"
        denied = json.loads(json.dumps(writes[0]))
        denied.update(native_session="native-session", call_id="original-call")
        denied["v7_responses"] = [
            {
                "api_version": "7",
                "ok": False,
                "data": None,
                "meta": {"request_id": "counsel", "workspace_id": "ws_owned"},
                "error": {
                    "code": "COUNSEL_REQUIRED",
                    "counsel": {"reasons": ["TEST"]},
                    "remedy": {
                        "tool": "daem0n_tool_call" if gateway else "memory_store",
                        "arguments": retry["arguments"],
                    },
                },
            }
        ]
        result = validate_receipts(
            [denied, retry, writes[1]], "initial", manifest, workspace_id="ws_owned"
        )
        assert result["checks"]["only_stage_authorized_memory_writes"]
        assert result["checks"]["exact_counsel_remedies"]
        writes[0]["arguments"]["workspace_id"] = "ws_foreign"
        result = validate_receipts(writes, "initial", manifest, workspace_id="ws_owned")
        assert not result["checks"]["workspace_scope"]
        assert not result["checks"]["only_stage_authorized_memory_writes"]


def test_posix_repair_refuses_existing_baseline_before_native_transport(
    tmp_path, monkeypatch
):
    import json
    from types import SimpleNamespace

    import pytest

    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / "scripts"))
    import native_posix_acceptance as native

    lane = tmp_path / "lane"
    lane.mkdir()
    binary = tmp_path / "native"
    binary.write_text("fixture")
    (lane / "fixture.json").write_text(
        json.dumps(
            {
                "project": str(lane),
                "config_hashes": {},
                "binary": str(binary),
                "binary_sha256": native.digest(binary),
            }
        )
    )
    monkeypatch.setattr(native, "lane_path", lambda *_: lane)
    monkeypatch.setattr(native, "config_hashes", lambda *_: {})
    monkeypatch.setattr(native, "environment", lambda *_: {})
    monkeypatch.setattr(native, "original_hashes", lambda: {})
    baseline = tmp_path / "historical.json"
    baseline.write_text("preserve")
    args = SimpleNamespace(
        root=tmp_path,
        host="omp",
        source="daem0n",
        matrix="final",
        attempt="repair",
        stage="initial",
        evidence_dir=tmp_path / "repair",
        baseline_path=baseline,
    )
    with pytest.raises(RuntimeError, match="Preserve existing native initial baseline"):
        native.run(args)
    assert baseline.read_text() == "preserve"
    assert not args.evidence_dir.exists()


def test_posix_baseline_parent_prepared_and_baseline_action_rejects_repair_flags(
    tmp_path, monkeypatch
):
    from types import SimpleNamespace

    import pytest

    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / "scripts"))
    import native_posix_acceptance as native

    target = tmp_path / "new" / "nested" / "initial.json"
    native.prepare_baseline_parent(target)
    assert target.parent.is_dir()
    assert not target.exists()
    with pytest.raises(ValueError, match="does not support repair output flags"):
        native.baseline(SimpleNamespace(baseline_path=target, evidence_dir=None))


def test_posix_main_routes_custom_failure_without_touching_existing_legacy(
    tmp_path, monkeypatch
):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / "scripts"))
    import native_posix_acceptance as native

    legacy = tmp_path / "lane" / "evidence" / "same-attempt"
    legacy.mkdir(parents=True)
    marker = legacy / "preserve.txt"
    marker.write_text("historical")
    target = tmp_path / "repair" / "new-attempt"
    monkeypatch.setattr(native, "lane_path", lambda *_: legacy.parent.parent)
    monkeypatch.setattr(native, "original_hashes", lambda: {})

    def fail(args):
        assert args.evidence_dir == target
        raise RuntimeError("bounded fixture failure")

    monkeypatch.setattr(native, "run", fail)
    monkeypatch.setattr(
        native.sys,
        "argv",
        [
            "native_posix_acceptance.py",
            "run",
            "--host",
            "omp",
            "--source",
            "daem0n",
            "--fixtures",
            str(tmp_path),
            "--attempt",
            "same-attempt",
            "--evidence-dir",
            str(target),
        ],
    )
    assert native.main() == 2
    assert (target / "driver-failure.json").is_file()
    assert marker.read_text() == "historical"
    assert list(legacy.iterdir()) == [marker]


def test_clean_index_setup_requires_original_count_not_mutation_count(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / "scripts"))
    import native_acceptance_scenarios as scenarios
    from native_acceptance_fixtures import PROJECTS

    manifest = {"project": "legion", "file_count": 269, "scenario": PROJECTS["legion"]}
    row = {
        "host": "opencode",
        "operation": "code_index",
        "canonical_tool": "code_index",
        "arguments": {"workspace_id": "ws_owned"},
        "v7_responses": [
            {
                "api_version": "7",
                "ok": True,
                "meta": {"request_id": "clean"},
                "data": {"files_indexed": 269, "manifest": {"generation": 4}},
            }
        ],
    }
    result = scenarios.validate_receipts(
        [row], "index_setup_clean", manifest, workspace_id="ws_owned"
    )
    assert result["status"] == "passed"
    row["v7_responses"][0]["data"]["files_indexed"] = 270
    assert (
        scenarios.validate_receipts(
            [row], "index_setup_clean", manifest, workspace_id="ws_owned"
        )["status"]
        == "failed"
    )
    prompt = scenarios.scenario_prompt(
        "opencode", "index_setup_clean", "ws_owned", manifest
    )
    assert "verified clean frozen baseline" in prompt
    assert "already applied the source edit/addition" not in prompt
