"""Repair runs must not rewrite historical evidence or persistence baselines."""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest


def test_repair_routes_evidence_and_replay_baseline_without_native_calls(
    tmp_path, monkeypatch
):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / "scripts"))
    import native_windows_acceptance as native
    from native_acceptance_fixtures import PROJECTS

    base = tmp_path / "original"
    lane = base / "pi/legion"
    lane.mkdir(parents=True)
    old_baseline = lane / "initial-validation.json"
    old_baseline.write_text('{"record_id":"historical","event_id":"historical"}')
    old_bytes = old_baseline.read_bytes()
    metadata = {
        "project": str(lane / "project"),
        "backend_python": "unused-python",
        "binary": "unused-pi",
        "binary_arguments": ["exact-existing-cli.js"],
        "config_hashes": {},
        "workspace_id": "ws_fixture",
        "manifest": {
            "project": "legion",
            "file_count": 1,
            "scenario": PROJECTS["legion"],
        },
    }
    (lane / "fixture.json").write_text(json.dumps(metadata))
    monkeypatch.setattr(native, "environment", lambda *_: {})
    monkeypatch.setattr(native, "config_paths", lambda *_: {})
    monkeypatch.setattr(native, "original_hashes", lambda: {})
    monkeypatch.setattr(native, "api_key", lambda *_: "fake-key")
    calls = []

    def provenance(_python, _lane, _env, _attempt, *, output=None):
        native.write_json(output, {"fixture_only": True})

    def transport(**kwargs):
        calls.append(kwargs)
        Path(kwargs["evidence"]).mkdir(parents=True, exist_ok=False)
        result = {
            "validation": {
                "status": "passed",
                "checks": {},
                "record_id": "retained-record",
                "event_id": "retained-event",
            }
        }
        native.write_json(Path(kwargs["evidence"]) / "summary.json", result)
        return result

    monkeypatch.setattr(native, "runtime_provenance", provenance)
    monkeypatch.setattr(native, "run_stream", transport)
    args = SimpleNamespace(
        base=base,
        host="pi",
        source="legion",
        attempt="repair-initial",
        stage="initial",
        model=None,
        evidence_dir=tmp_path / "repair/initial",
        baseline_path=tmp_path / "repair/initial-validation.json",
    )
    assert native.run(args) == 0
    assert calls[-1]["argv"][:2] == ["unused-pi", "exact-existing-cli.js"]
    assert old_baseline.read_bytes() == old_bytes
    assert not (lane / "evidence").exists()
    assert json.loads(args.baseline_path.read_text())["record_id"] == "retained-record"
    assert args.evidence_dir.with_name("initial.backend-provenance.json").is_file()
    saved_summary = (args.evidence_dir / "summary.json").read_bytes()
    with pytest.raises(RuntimeError, match="Preserve existing native attempt"):
        native.run(args)
    assert len(calls) == 1
    assert (args.evidence_dir / "summary.json").read_bytes() == saved_summary
    args.attempt, args.stage, args.evidence_dir = (
        "repair-restart",
        "restart",
        tmp_path / "repair/restart",
    )
    assert native.run(args) == 0
    assert calls[-1]["prior"]["record_id"] == "retained-record"
    assert old_baseline.read_bytes() == old_bytes


def test_legacy_attempt_paths_are_preserved_when_repair_options_are_absent(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / "scripts"))
    from native_windows_acceptance import attempt_paths

    lane = Path("original")
    assert attempt_paths(SimpleNamespace(attempt="first"), lane) == (
        lane / "evidence/first",
        lane / "initial-validation.json",
    )


def test_existing_initial_anchor_rejects_fresh_attempt_before_provenance_or_driver(
    tmp_path, monkeypatch
):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / "scripts"))
    import native_windows_acceptance as native

    lane = tmp_path / "pi/legion"
    lane.mkdir(parents=True)
    (lane / "fixture.json").write_text("{}")
    baseline = tmp_path / "retained-baseline.json"
    baseline.write_bytes(b"original immutable anchor")
    evidence = tmp_path / "new-evidence"
    args = SimpleNamespace(
        base=tmp_path,
        host="pi",
        source="legion",
        attempt="fresh",
        stage="initial",
        evidence_dir=evidence,
        baseline_path=baseline,
    )

    def forbidden(*_args, **_kwargs):
        pytest.fail(
            "Existing anchor must reject before environment/provenance/transport"
        )

    for name in ("environment", "runtime_provenance", "run_stream"):
        monkeypatch.setattr(native, name, forbidden)
    with pytest.raises(RuntimeError, match="Preserve existing initial baseline"):
        native.run(args)
    assert baseline.read_bytes() == b"original immutable anchor"
    assert not evidence.exists()
    assert not evidence.with_name("new-evidence.backend-provenance.json").exists()


def test_collect_preserves_existing_initial_anchor_before_writing_recollection(
    tmp_path, monkeypatch
):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / "scripts"))
    import native_windows_acceptance as native

    lane = tmp_path / "pi/legion"
    lane.mkdir(parents=True)
    (lane / "fixture.json").write_text("{}")
    baseline = tmp_path / "retained-baseline.json"
    baseline.write_bytes(b"original immutable anchor")
    evidence = tmp_path / "new-evidence"
    evidence.mkdir()
    (evidence / "events.jsonl").write_bytes(b"retained original events")
    args = SimpleNamespace(
        base=tmp_path,
        host="pi",
        source="legion",
        attempt="fresh",
        stage="initial",
        evidence_dir=evidence,
        baseline_path=baseline,
    )
    with pytest.raises(RuntimeError, match="Preserve existing initial baseline"):
        native.collect(args)
    assert baseline.read_bytes() == b"original immutable anchor"
    assert (evidence / "events.jsonl").read_bytes() == b"retained original events"
    assert not (evidence / "recollected-summary.json").exists()


def test_initial_anchor_publication_is_create_only(tmp_path, monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / "scripts"))
    from native_windows_acceptance import publish_initial_baseline

    baseline = tmp_path / "anchor.json"
    publish_initial_baseline(baseline, {"record_id": "first"})
    original = baseline.read_bytes()
    with pytest.raises(RuntimeError, match="at publication"):
        publish_initial_baseline(baseline, {"record_id": "second"})
    assert baseline.read_bytes() == original


def test_repair_source_preparation_restores_only_exact_owned_mutation(
    tmp_path, monkeypatch
):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / "scripts"))
    import native_windows_repair as repair

    base, evidence = tmp_path / "native", tmp_path / "evidence"
    project = base / "opencode/daem0n/project"
    project.mkdir(parents=True)
    original = b"def original():\n    pass\n"
    target = project / "source.py"
    target.write_bytes(original)
    manifest = {
        "files": {"source.py": repair.native.digest(target)},
        "scenario": {
            "mutation_path": "source.py",
            "added_path": "added.py",
            "mutation_append": "\ndef changed(): pass\n",
            "added_content": "def added(): pass\n",
        },
    }
    metadata = {"project": str(project), "manifest": manifest}
    (base / "opencode/daem0n/fixture.json").write_text(json.dumps(metadata))
    destination = evidence / "opencode/daem0n"
    destination.mkdir(parents=True)
    (destination / "binding.json").write_text("{}")
    monkeypatch.setattr(repair, "BASE", base)
    monkeypatch.setattr(repair, "REPAIR", evidence)
    monkeypatch.setattr(
        repair, "pending_probes", lambda _: {"both_probes_present": True}
    )
    repair.change_source("opencode", "daem0n", "prepare")
    target.write_bytes(target.read_bytes() + b"# unrelated\n")
    with pytest.raises(RuntimeError, match="Restore refuses changed"):
        repair.change_source("opencode", "daem0n", "changed", restore=True)
    assert target.read_bytes().endswith(b"# unrelated\n")
    target.write_bytes(original + manifest["scenario"]["mutation_append"].encode())
    repair.change_source("opencode", "daem0n", "restore", restore=True)
    assert target.read_bytes() == original
    assert not (project / "added.py").exists()
    assert (destination / "preparations/restore/pending-removal.json").is_file()


def test_repair_source_target_rejects_traversal(tmp_path, monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / "scripts"))
    from native_windows_repair import source_targets

    with pytest.raises(RuntimeError, match="escapes the fixture"):
        source_targets(
            {
                "project": str(tmp_path),
                "manifest": {
                    "scenario": {
                        "mutation_path": "../foreign.py",
                        "added_path": "added.py",
                    }
                },
            }
        )


@pytest.mark.parametrize(
    "status,operations,allowed",
    [("failed", [], True), ("passed", [], False), ("failed", ["code_index"], False)],
)
def test_setup_after_mutation_requires_retained_failed_turn_without_index(
    tmp_path, monkeypatch, status, operations, allowed
):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / "scripts"))
    from native_windows_repair import admit_stage

    attempt = tmp_path / "attempts/failed-mutate"
    attempt.mkdir(parents=True)
    (attempt / "summary.json").write_text(
        json.dumps({"validation": {"stage": "mutate", "status": status}})
    )
    (attempt / "native-tool-receipts.jsonl").write_text(
        "".join(json.dumps({"operation": op}) + "\n" for op in operations)
    )
    if allowed:
        assert admit_stage("omp", "legion", "index_setup", tmp_path)[
            "no_backend_index_observed"
        ]
    else:
        with pytest.raises(RuntimeError, match="evidenced minimal state setup"):
            admit_stage("omp", "legion", "index_setup", tmp_path)
