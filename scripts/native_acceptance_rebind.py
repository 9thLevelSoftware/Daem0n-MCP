"""Rebind an owned acceptance fixture to a reviewed second backend interpreter."""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import re
from pathlib import Path

import tomlkit
from native_posix_acceptance import config_hashes, digest, lane_path

from daem0nmcp.client_install import _json_document, _set_json_value
from daem0nmcp.protected_files import reject_linked_ancestry


def require_resolved_cleanup(path: Path) -> None:
    report = json.loads(path.read_text())
    if report.get("owned_job_closed") is True:
        return
    if (
        report.get("process_group_closed") is False
        or report.get("owned_job_closed") is False
    ):
        raise RuntimeError("Previous native attempt reports an unclosed owned group")
    if (
        "process_group_closed" not in report
        and not report.get("cleanup_requires_exact_owned_process_inspection")
    ) or report.get("process_group_closed") is True:
        return
    resolution_path = path.with_name("cleanup-resolution.json")
    reject_linked_ancestry(resolution_path)
    if not resolution_path.is_file():
        raise RuntimeError(
            "Unknown cleanup requires an explicit owned-group inspection receipt"
        )
    resolution = json.loads(resolution_path.read_text())
    if (
        resolution.get("summary_sha256") != digest(path)
        or resolution.get("process_group_id") != report.get("process_group_id")
        or not isinstance(report.get("process_group_id"), int)
        or resolution.get("status") != "no_process_group_members_observed"
        or resolution.get("observed_group_members") != []
        or resolution.get("inspection_exit_code") != 0
    ):
        raise RuntimeError(
            "Cleanup inspection does not resolve this exact native attempt"
        )


def rebind(
    root: Path, matrix: str, host: str, source: str, python: Path, wheel_sha256: str
):
    if re.fullmatch(r"[0-9a-f]{64}", wheel_sha256) is None:
        raise ValueError("Exact reviewed wheel SHA256 is required")
    reviewed_targets = {
        root
        / "venv-11/bin/python": "0d8d9087888b09860297619d25c74cb2a47f0d8c7fa8f5cc92d872393f42c687",
        root
        / "venv-10/bin/python": "5de43b4a7f3eb3ceff40fe254b10a0d5200fed63d78fd855aa0436731e5e08e7",
        root
        / "venv-09/bin/python": "a6c371a0c1a5b2f69f6e68b04d16caa5c77a2a5b1fc4689915b2e6fe6b571010",
        root
        / "venv-08/bin/python": "1a7082b687ae6759934d6ee480a732722f6b9a8798b36d834f9f5d116a627609",
        root
        / "venv-02/bin/python": "6d9939417043b705631c03342095c5af7c2534c7cf40592b85a0a604b35b64da",
        root
        / "venv-05/bin/python": "9b6ff0e18753e2075cc14eab2eeaa4dd2435ca8be06daace00906ce183618963",
    }
    if reviewed_targets.get(python) != wheel_sha256 or not python.is_file():
        raise ValueError(
            "Interpreter and wheel must match an existing reviewed frozen environment"
        )
    lane = lane_path(root, host, source, matrix)
    metadata_path = lane / "fixture.json"
    metadata = json.loads(metadata_path.read_text())
    project = Path(metadata["project"])
    if config_hashes(project) != metadata["config_hashes"]:
        raise RuntimeError("Reviewed fixture configuration changed before rebind")
    approval_path = lane / "approved-project.json"
    approval_before = None
    if host == "pi":
        reject_linked_ancestry(approval_path)
        approval_before = approval_path.read_bytes()
        approval = json.loads(approval_before)
        if approval != {
            "project": str(project),
            "fileHashes": metadata["config_hashes"],
        }:
            raise RuntimeError(
                "Pi trust receipt differs from the reviewed configuration"
            )
    if any(
        digest(project / name) != expected
        for name, expected in metadata["manifest"]["files"].items()
    ):
        raise RuntimeError("Frozen source bytes changed before rebind")
    for path in (lane / "evidence").glob("*/summary.json"):
        require_resolved_cleanup(path)
    new_python = str(python)
    if host == "codex":
        configuration = project / ".codex/config.toml"
        reject_linked_ancestry(configuration)
        before = configuration.read_bytes()
        document = tomlkit.parse(before.decode())
        old_python = str(document["mcp_servers"]["daem0nmcp"]["command"])
        expected = copy.deepcopy(document.unwrap())
        expected["mcp_servers"]["daem0nmcp"]["command"] = new_python
        document["mcp_servers"]["daem0nmcp"]["command"] = new_python
        text = tomlkit.dumps(document)
        if tomlkit.parse(text).unwrap() != expected:
            raise RuntimeError("Unrelated TOML changed during interpreter rebind")
    else:
        name = {
            "claude": ".mcp.json",
            "agy": ".agents/mcp_config.json",
            "opencode": "opencode.jsonc",
            "omp": ".omp/mcp.json",
            "pi": ".pi/mcp.json",
        }[host]
        configuration = project / name
        reject_linked_ancestry(configuration)
        before = configuration.read_bytes()
        document = _json_document(before.decode(), comments=host == "opencode").value
        expected = copy.deepcopy(document)
        if host == "opencode":
            command = document["mcp"]["daem0nmcp"]["command"]
            old_python = command[0]
            replacement = [new_python, *command[1:]]
            expected["mcp"]["daem0nmcp"]["command"] = replacement
            path = ("mcp", "daem0nmcp", "command")
        else:
            old_python = document["mcpServers"]["daem0nmcp"]["command"]
            replacement = new_python
            expected["mcpServers"]["daem0nmcp"]["command"] = replacement
            path = ("mcpServers", "daem0nmcp", "command")
        text = _set_json_value(
            before.decode(), path, replacement, comments=host == "opencode"
        )
        if _json_document(text, comments=host == "opencode").value != expected:
            raise RuntimeError("Unrelated JSON changed during interpreter rebind")
    if (
        old_python
        not in {
            str(root / "venv/bin/python"),
            str(root / "venv-02/bin/python"),
            str(root / "venv-05/bin/python"),
            str(root / "venv-08/bin/python"),
            str(root / "venv-09/bin/python"),
            str(root / "venv-10/bin/python"),
        }
        or old_python == new_python
    ):
        raise RuntimeError(
            "Fixture is not bound to a distinct reviewed prior interpreter"
        )
    storage = project / ".daem0nmcp/storage"
    storage_before = {
        str(path.relative_to(project)): digest(path)
        for path in storage.glob("*.db*")
        if path.is_file()
    }
    receipt = lane / ("backend-rebind-" + wheel_sha256[:16])
    receipt.mkdir(exist_ok=False)
    (receipt / "configuration-before.txt").write_bytes(before)
    if configuration.read_bytes() != before:
        raise RuntimeError("Fixture changed during interpreter rebind")
    configuration.write_text(text, encoding="utf-8")
    (receipt / "configuration-after.txt").write_text(text, encoding="utf-8")
    storage_after = {
        str(path.relative_to(project)): digest(path)
        for path in storage.glob("*.db*")
        if path.is_file()
    }
    if storage_before != storage_after:
        raise RuntimeError("Fixture storage changed during interpreter rebind")
    metadata.update(
        backend_python=new_python,
        backend_wheel_sha256=wheel_sha256,
        hook_backend_python=metadata.get("hook_backend_python", old_python),
        config_hashes=config_hashes(project),
    )
    if approval_before is not None:
        if approval_path.read_bytes() != approval_before:
            raise RuntimeError("Pi trust receipt changed during rebind")
        (receipt / "approved-project-before.json").write_bytes(approval_before)
        approval["fileHashes"][".pi/mcp.json"] = metadata["config_hashes"][
            ".pi/mcp.json"
        ]
        approval_path.write_text(json.dumps(approval, indent=2) + "\n")
        (receipt / "approved-project-after.json").write_bytes(
            approval_path.read_bytes()
        )
    metadata_path.write_text(json.dumps(metadata, indent=2) + "\n")
    result = {
        "host": host,
        "source": source,
        "old_python": old_python,
        "new_python": new_python,
        "backend_wheel_sha256": wheel_sha256,
        "only_mcp_interpreter_changed": True,
        "source_hashes_unchanged": True,
        "hook_runtime_preserved": True,
        "hook_backend_python": metadata["hook_backend_python"],
        "storage_before": storage_before,
        "storage_after": storage_after,
    }
    (receipt / "receipt.json").write_text(json.dumps(result, indent=2) + "\n")
    return result


def repair_pi_approval(root: Path, matrix: str, source: str, wheel_sha256: str):
    """Approve only the exact MCP hash already reviewed in a previous rebind."""
    if re.fullmatch(r"[0-9a-f]{64}", wheel_sha256) is None:
        raise ValueError("Exact reviewed wheel SHA256 is required")
    lane = lane_path(root, "pi", source, matrix)
    metadata = json.loads((lane / "fixture.json").read_text())
    if metadata.get("backend_wheel_sha256") != wheel_sha256:
        raise RuntimeError("Fixture does not match the reviewed backend wheel")
    project = Path(metadata["project"])
    current = config_hashes(project)
    if current != metadata["config_hashes"]:
        raise RuntimeError("Current configuration differs from the reviewed fixture")
    old_receipt = lane / ("backend-rebind-" + wheel_sha256[:16])
    before_configuration = old_receipt / "configuration-before.txt"
    reject_linked_ancestry(before_configuration)
    prior_hash = hashlib.sha256(before_configuration.read_bytes()).hexdigest()
    if digest(old_receipt / "configuration-after.txt") != current[".pi/mcp.json"]:
        raise RuntimeError("MCP configuration differs from the retained rebind result")
    approval_path = lane / "approved-project.json"
    reject_linked_ancestry(approval_path)
    before = approval_path.read_bytes()
    approval = json.loads(before)
    expected = copy.deepcopy(current)
    expected[".pi/mcp.json"] = prior_hash
    if approval != {"project": str(project), "fileHashes": expected}:
        raise RuntimeError(
            "Trust entries differ from the exact reviewed rebind preimage"
        )
    if any(
        digest(project / name) != expected
        for name, expected in metadata["manifest"]["files"].items()
    ):
        raise RuntimeError("Source differs from the frozen fixture")
    evidence = lane / "evidence/pi-trust-rebind-correction-01"
    evidence.mkdir(parents=True, exist_ok=False)
    (evidence / "before-approved-project.json").write_bytes(before)
    approval["fileHashes"][".pi/mcp.json"] = current[".pi/mcp.json"]
    if approval_path.read_bytes() != before:
        raise RuntimeError("Trust receipt changed during correction")
    approval_path.write_text(json.dumps(approval, indent=2) + "\n")
    (evidence / "after-approved-project.json").write_bytes(approval_path.read_bytes())
    result = {
        "status": "corrected",
        "only_approved_entry_changed": ".pi/mcp.json",
        "prior_approved_hash": prior_hash,
        "reviewed_current_hash": current[".pi/mcp.json"],
        "reviewed_backend_wheel_sha256": wheel_sha256,
        "persistent_native_trust_written": False,
    }
    (evidence / "summary.json").write_text(json.dumps(result, indent=2) + "\n")
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--matrix", required=True)
    parser.add_argument(
        "--host",
        choices=("claude", "codex", "agy", "opencode", "omp", "pi"),
        required=True,
    )
    parser.add_argument("--source", choices=("daem0n", "legion"), required=True)
    parser.add_argument("--python", type=Path, required=True)
    parser.add_argument("--wheel-sha256", required=True)
    parser.add_argument("--repair-pi-trust", action="store_true")
    arguments = parser.parse_args()
    if re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_-]{0,80}", arguments.matrix) is None:
        parser.error("matrix requires a simple identifier")
    if arguments.repair_pi_trust:
        if arguments.host != "pi":
            parser.error("Trust correction is restricted to the Pi fixture")
        result = repair_pi_approval(
            arguments.root, arguments.matrix, arguments.source, arguments.wheel_sha256
        )
    else:
        result = rebind(
            arguments.root,
            arguments.matrix,
            arguments.host,
            arguments.source,
            arguments.python,
            arguments.wheel_sha256,
        )
    print(json.dumps(result))


if __name__ == "__main__":
    main()
