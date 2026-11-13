"""Native harness cleanup must preserve uncertainty and avoid broader signals."""

import errno
import importlib
import json
from pathlib import Path
from types import SimpleNamespace


def test_permission_denied_group_probe_stops_signals_and_preserves_unknown(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / "scripts"))
    driver = importlib.import_module("native_acceptance_drivers")
    signals = []

    def denied(group, signal):
        signals.append((group, signal))
        raise PermissionError(errno.EPERM, "Operation not permitted")

    monkeypatch.setattr(driver, "os", SimpleNamespace(name="posix", killpg=denied))
    result = driver._close_owned_group(SimpleNamespace(pid=12345, poll=lambda: 0))
    assert signals == [(12345, 0)]
    assert result["process_group_closed"] is None
    assert result["native_root_exited"] is True
    assert result["cleanup_signal_error"]["errno"] == errno.EPERM
    assert result["cleanup_requires_exact_owned_process_inspection"] is True


def test_omp_bounded_recall_rejects_foreign_scope_extra_fields_and_unbounded_limits(
    monkeypatch,
):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / "scripts"))
    driver = importlib.import_module("native_acceptance_drivers")
    title = "Allow tool: write\nPath: xd://mcp__daem0nmcp_memory_recall\nArguments:\n"
    payload = {
        "workspace_id": "ws_owned",
        "query": "source indexing warnings",
        "limit": 10,
    }
    manifest = {"project": "daem0n", "scenario": {}}

    def approve(arguments):
        return driver._bounded_omp_approval(
            {
                "method": "select",
                "options": ["Approve", "Deny"],
                "title": title + json.dumps(arguments),
            },
            "ws_owned",
            manifest,
            [],
        )

    assert approve(payload)
    for changes in (
        {"workspace_id": "ws_foreign"},
        {"limit": 0},
        {"limit": 11},
        {"limit": "5"},
        {"limit": True},
        {"query": ""},
        {"query": " "},
        {"query": "x" * 257},
        {"extra": "not authorized"},
    ):
        assert approve({**payload, **changes}) is None
