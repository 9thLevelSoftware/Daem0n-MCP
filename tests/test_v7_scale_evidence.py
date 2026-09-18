"""Benchmark evidence must retain failed attempts without overstating targets."""

import asyncio
import json
from types import SimpleNamespace

import pytest

from benchmarks import v7_scale


@pytest.mark.parametrize(
    "failure", [RuntimeError, TimeoutError, asyncio.CancelledError]
)
async def test_failed_measurement_retains_partial_evidence(
    tmp_path, monkeypatch, failure
):
    (tmp_path / "scale-fixture.json").write_text(
        json.dumps({"workspace_id": "ws_fixture"}), encoding="utf-8"
    )
    monkeypatch.setattr(v7_scale, "source_fingerprint", lambda: "source-digest")
    monkeypatch.setattr(v7_scale, "host_snapshot", lambda: {"available": False})
    monkeypatch.setattr(
        v7_scale, "canonical_counts", lambda *_: {"records": 1, "events": 1}
    )
    monkeypatch.setattr(
        v7_scale, "subprocess", SimpleNamespace(check_output=lambda *_, **__: "base")
    )

    async def fail_late(root, transport, rounds, result, samples):
        samples.extend([0.1, 0.2])
        result["phase"] = "warm_recall"
        raise failure("credential-like text must not be stored")

    monkeypatch.setattr(v7_scale, "_measure_phases", fail_late)
    for _ in range(2):
        with pytest.raises(failure):
            await v7_scale.measure(tmp_path, "stdio", 1)
    attempts = list(tmp_path.glob("scale-measure-stdio-*.json"))
    assert len(attempts) == 2
    latest = json.loads((tmp_path / "scale-measure-stdio.json").read_text())
    assert latest["run_id"] in {
        json.loads(path.read_text())["run_id"] for path in attempts
    }
    for path in attempts:
        content = path.read_text(encoding="utf-8")
        result = json.loads(content)
        assert result["status"] == "incomplete"
        assert result["phase"] == "warm_recall"
        assert result["failure_type"] == failure.__name__
        assert result["requests"] == 2
        assert result["p95_seconds"] == 0.2
        assert result["warm_recall_complete"] is False
        assert result["meets_lexical_target"] is False
        assert result["new_write_visibility"] is None
        assert "credential-like" not in content


async def test_visibility_target_includes_time_before_write_response(monkeypatch):
    counter = iter([0.0, 4.0, 6.0, 6.1])
    monkeypatch.setattr(
        v7_scale,
        "time",
        SimpleNamespace(perf_counter=lambda: next(counter), monotonic=lambda: 0.0),
    )

    async def respond(session, tool, arguments):
        if tool == "memory_preflight":
            return {"preflight_token": "test"}
        if tool == "memory_store":
            return {"record": {"record_id": "mem_test"}}
        if tool == "memory_recall":
            return {"items": [{"record": {"record_id": "mem_test"}}]}
        assert tool == "system_health"
        return {
            "runtime_diagnostics": [
                {"component": name, "status": "ready", "counts": {}}
                for name in ("temporal", "procedure", "outcome")
            ]
        }

    monkeypatch.setattr(v7_scale, "succeed", respond)
    result = await v7_scale.write_visibility(None, {"workspace_id": "ws_test"})
    assert result["lexical_seconds_after_write_response"] == 2
    assert result["lexical_seconds_from_write_request"] == 6
    assert result["meets_lexical_visibility_target"] is False
    assert result["specialized_seconds_from_write_request"] == pytest.approx(6.1)
    assert result["meets_specialized_visibility_target"] is True
