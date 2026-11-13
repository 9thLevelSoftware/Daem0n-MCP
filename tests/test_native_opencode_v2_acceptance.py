"""Keep ordinary V2 acceptance distinct from guarded historical probes."""

from __future__ import annotations

import argparse
import importlib
import json
from copy import deepcopy
from pathlib import Path

import pytest

from scripts.native_acceptance_fixtures import PROJECTS


@pytest.fixture
def driver(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / "scripts"))
    return importlib.import_module("scripts.native_opencode_v2_acceptance")


@pytest.mark.parametrize(
    "overrides",
    [
        {"index_argument_policy": "enforce"},
        {"indexing_only": True},
        {"restart_code_only": True},
    ],
)
def test_ordinary_mode_rejects_instrumentation_before_credentials_or_files(
    driver, monkeypatch, overrides
):
    def unexpected_credential_lookup():
        pytest.fail("Invalid acceptance modes must fail before credential lookup")

    monkeypatch.setattr(driver, "selected_key", unexpected_credential_lookup)
    arguments = argparse.Namespace(
        ordinary_workflow=True,
        index_argument_policy="off",
        indexing_only=False,
        restart_code_only=False,
    )
    for key, value in overrides.items():
        setattr(arguments, key, value)
    with pytest.raises(ValueError, match="unguarded shared scenario guidance"):
        # No output path is supplied: admission must precede filesystem access.
        driver.execute(arguments)


def test_ordinary_index_cli_uses_consistent_scope_without_test_rewrites(
    driver, monkeypatch, tmp_path
):
    lane = tmp_path / "lane"
    (lane / "project").mkdir(parents=True)
    specification = deepcopy(PROJECTS["daem0n"])
    specification["patterns"] = ["daem0nmcp/**/*.py"]
    metadata = {
        "mode": "code",
        "workspace_id": "ws_fixture",
        "source": {"project": "daem0n", "scenario": specification},
    }
    (lane / "scenario.json").write_text(json.dumps(metadata), encoding="utf-8")
    monkeypatch.setattr(driver, "selected_key", lambda: "fixture-key")
    observed = {}

    class NativeSpawnInterceptedError(Exception):
        pass

    def intercept(command, **kwargs):
        observed.update(command=command, environment=kwargs["env"])
        raise NativeSpawnInterceptedError

    monkeypatch.setattr(driver, "_spawn", intercept)
    monkeypatch.setattr(
        driver.sys,
        "argv",
        [
            "native_opencode_v2_acceptance.py",
            "run",
            "--output",
            str(lane),
            "--client",
            str(tmp_path / "opencode"),
            "--attempt",
            "ordinary-index",
            "--scenario",
            "index",
            "--ordinary-workflow",
        ],
    )
    with pytest.raises(NativeSpawnInterceptedError):
        driver.main()
    prompt = observed["command"][-1]
    index, _ = json.JSONDecoder().raw_decode(prompt.split("Call code_index with ")[1])
    assert index == {
        "workspace_id": "ws_fixture",
        "relative_root": ".",
        "patterns": ["daem0nmcp/**/*.py"],
    }
    assert "copy this WHOLE daem0n_tool_call" not in prompt
    assert "DAEM0N_V2_EXPECTED_INDEX" not in observed["environment"]
    request_text = (lane / "ordinary-index/request.json").read_text(encoding="utf-8")
    request = json.loads(request_text)
    assert request["ordinary_workflow"] is True
    assert request["index_argument_policy"] == "off"
    assert "fixture-key" not in request_text
    assert metadata == json.loads((lane / "scenario.json").read_text(encoding="utf-8"))


def test_historical_guard_mode_remains_explicit(driver, monkeypatch, tmp_path):
    observed = []
    monkeypatch.setattr(driver, "execute", observed.append)
    monkeypatch.setattr(
        driver.sys,
        "argv",
        [
            "native_opencode_v2_acceptance.py",
            "run",
            "--output",
            str(tmp_path),
            "--scenario",
            "mutate",
            "--index-argument-policy",
            "enforce",
        ],
    )
    driver.main()
    assert observed[0].index_argument_policy == "enforce"


@pytest.mark.parametrize("mode", ["code", "direct"])
def test_mode_specific_receipt_seed_matches_prompt_without_changing_native_scope(
    driver, mode
):
    nonce = f"native-followup-opencode-v2-{mode}-legion-20261112"
    arguments = {
        "workspace_id": "ws_fixture",
        "record_type": "decision",
        "content": nonce + ": preserve real-source indexing and native client setup",
        "rationale": "Native MCP persistence and same-key replay acceptance",
        "idempotency_key": nonce + "-decision",
    }
    event = {
        "event": "after",
        "tool": "daem0nmcp_memory_store",
        "input": arguments,
        "id": "actual_native_call",
        "sessionID": "actual_native_session",
        "result": {
            "output": {
                "api_version": "7",
                "ok": True,
                "data": {
                    "record": {"record_id": "mem_fixture"},
                    "event_id": "evt_fixture",
                    "idempotent_replay": True,
                },
                "error": None,
                "meta": {"request_id": "req_fixture"},
            }
        },
    }
    original_event = deepcopy(event)
    receipts = driver.v2_receipts([event], mode)
    manifest = {"project": "legion", "file_count": 1, "scenario": PROJECTS["legion"]}
    report = driver.validate_receipts(
        receipts, "restart", manifest, workspace_id="ws_fixture"
    )
    assert report["checks"]["only_stage_authorized_memory_writes"] is True
    assert receipts[0]["host"] == "opencode-v2-" + mode
    assert receipts[0]["arguments"] == arguments
    assert receipts[0]["call_id"] == "actual_native_call"
    assert receipts[0]["native_session"] == "actual_native_session"
    assert event == original_event
    unplanned = deepcopy(event)
    unplanned["input"]["content"] += " unplanned change"
    rejected = driver.validate_receipts(
        driver.v2_receipts([unplanned], mode),
        "restart",
        manifest,
        workspace_id="ws_fixture",
    )
    assert rejected["checks"]["only_stage_authorized_memory_writes"] is False
