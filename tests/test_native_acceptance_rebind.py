"""Only the reviewed MCP rebind may advance a disposable Pi trust receipt."""

import hashlib
import importlib
import json
from pathlib import Path

import pytest


@pytest.mark.parametrize("foreign_prior", [False, True])
@pytest.mark.parametrize(
    ("prior_version", "target_version", "sha"),
    [
        (
            "09",
            "10",
            "5de43b4a7f3eb3ceff40fe254b10a0d5200fed63d78fd855aa0436731e5e08e7",
        ),
        (
            "10",
            "11",
            "0d8d9087888b09860297619d25c74cb2a47f0d8c7fa8f5cc92d872393f42c687",
        ),
    ],
    ids=["freeze10", "freeze11"],
)
def test_reviewed_rebind_requires_exact_owned_interpreter(
    tmp_path, monkeypatch, foreign_prior, prior_version, target_version, sha
):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / "scripts"))
    helper = importlib.import_module("native_acceptance_rebind")
    root = tmp_path / "owned"
    lane = helper.lane_path(root, "omp", "legion", "final")
    project = lane / "project"
    configuration = project / ".omp/mcp.json"
    configuration.parent.mkdir(parents=True)
    prior_root = tmp_path / "foreign" if foreign_prior else root
    prior = prior_root / f"venv-{prior_version}/bin/python"
    configuration.write_text(
        json.dumps({"mcpServers": {"daem0nmcp": {"command": str(prior)}}})
    )
    metadata = {
        "project": str(project),
        "config_hashes": helper.config_hashes(project),
        "manifest": {"files": {}},
    }
    (lane / "fixture.json").write_text(json.dumps(metadata))
    target = root / f"venv-{target_version}/bin/python"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"unused fixture interpreter; never executed")
    before = configuration.read_bytes()
    if foreign_prior:
        with pytest.raises(RuntimeError, match="distinct reviewed prior interpreter"):
            helper.rebind(root, "final", "omp", "legion", target, sha)
        assert configuration.read_bytes() == before
        assert json.loads((lane / "fixture.json").read_text()) == metadata
    else:
        result = helper.rebind(root, "final", "omp", "legion", target, sha)
        assert result["old_python"] == str(prior)
        assert result["new_python"] == str(target)
        assert result["only_mcp_interpreter_changed"]
        assert json.loads(configuration.read_text())["mcpServers"]["daem0nmcp"][
            "command"
        ] == str(target)


def test_unknown_cleanup_requires_resolution_bound_to_exact_summary(
    tmp_path, monkeypatch
):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / "scripts"))
    helper = importlib.import_module("native_acceptance_rebind")
    summary = tmp_path / "summary.json"
    summary.write_text(
        json.dumps(
            {
                "process_group_closed": None,
                "process_group_id": 12345,
                "cleanup_requires_exact_owned_process_inspection": True,
            }
        )
    )
    with pytest.raises(RuntimeError, match="explicit owned-group inspection"):
        helper.require_resolved_cleanup(summary)
    resolution = {
        "summary_sha256": helper.digest(summary),
        "process_group_id": 12345,
        "status": "no_process_group_members_observed",
        "observed_group_members": [],
        "inspection_exit_code": 0,
    }
    (tmp_path / "cleanup-resolution.json").write_text(json.dumps(resolution))
    helper.require_resolved_cleanup(summary)
    summary.write_text(summary.read_text() + "\n")
    with pytest.raises(RuntimeError, match="this exact native attempt"):
        helper.require_resolved_cleanup(summary)


@pytest.mark.parametrize("unrelated_trust_changed", [False, True])
def test_pi_rebind_trust_correction_preserves_other_entries(
    tmp_path, monkeypatch, unrelated_trust_changed
):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / "scripts"))
    helper = importlib.import_module("native_acceptance_rebind")
    lane = helper.lane_path(tmp_path, "pi", "legion", "final")
    project = lane / "project"
    configuration = project / ".pi/mcp.json"
    configuration.parent.mkdir(parents=True)
    configuration.write_text('{"command":"reviewed-second-interpreter"}')
    hashes = helper.config_hashes(project)
    sha = "a" * 64
    receipt = lane / ("backend-rebind-" + sha[:16])
    receipt.mkdir()
    before = b'{"command":"first-interpreter"}'
    (receipt / "configuration-before.txt").write_bytes(before)
    (receipt / "configuration-after.txt").write_bytes(configuration.read_bytes())
    metadata = {
        "project": str(project),
        "backend_wheel_sha256": sha,
        "config_hashes": hashes,
        "manifest": {"files": {}},
    }
    (lane / "fixture.json").write_text(json.dumps(metadata))
    expected_old = dict(hashes)
    expected_old[".pi/mcp.json"] = hashlib.sha256(before).hexdigest()
    if unrelated_trust_changed:
        expected_old["AGENTS.md"] = "unreviewed-change"
    approval = {"project": str(project), "fileHashes": expected_old}
    target = lane / "approved-project.json"
    target.write_text(json.dumps(approval))
    prior = target.read_bytes()
    if unrelated_trust_changed:
        with pytest.raises(RuntimeError, match="Trust entries differ"):
            helper.repair_pi_approval(tmp_path, "final", "legion", sha)
        assert target.read_bytes() == prior
    else:
        result = helper.repair_pi_approval(tmp_path, "final", "legion", sha)
        after = json.loads(target.read_text())
        assert after["fileHashes"] == hashes
        assert after["project"] == approval["project"]
        assert result["only_approved_entry_changed"] == ".pi/mcp.json"
        for name, value in expected_old.items():
            if name != ".pi/mcp.json":
                assert after["fileHashes"][name] == value
    assert helper.config_hashes(project) == hashes
