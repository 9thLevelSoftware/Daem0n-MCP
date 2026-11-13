"""Guarded, separately retained Windows repair qualification against freeze 08."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace

import native_windows_acceptance as native
from native_acceptance_drivers import write_json

REPOSITORY = Path(__file__).resolve().parents[1]
BASE = Path("D:/Daem0n-Native-Acceptance-20261112/windows")
REPAIR = REPOSITORY / ".test_tmp/native-followup-20261112/repair-06/windows"
PYTHON = (
    REPOSITORY
    / ".test_tmp/native-followup-20261112/backend/.venv-08/Scripts/python.exe"
)
WHEEL = (
    REPOSITORY
    / ".test_tmp/native-followup-20261112/backend/freeze-08/wheels/daem0nmcp-7.0.0.dev0-py3-none-any.whl"
)
WHEEL_SHA = "1a7082b687ae6759934d6ee480a732722f6b9a8798b36d834f9f5d116a627609"
FAILED = {
    ("opencode", "daem0n"): ("remove",),
    ("opencode", "legion"): ("remove",),
    ("omp", "daem0n"): ("mutate", "remove"),
    ("omp", "legion"): ("initial", "mutate", "remove", "restart"),
    ("pi", "daem0n"): ("remove", "restart"),
    ("pi", "legion"): ("initial", "remove"),
}


def read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def hashes(root):
    return {
        str(path.relative_to(root)): native.digest(path)
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def source_state(metadata):
    project = Path(metadata["project"])
    mismatches = [
        name
        for name, expected in metadata["manifest"]["files"].items()
        if not (project / name).is_file() or native.digest(project / name) != expected
    ]
    added = project / metadata["manifest"]["scenario"]["added_path"]
    return {
        "original_source_matches": not mismatches,
        "mismatches": mismatches,
        "added_probe_absent": not added.exists(),
    }


def inventory():
    matrix = REPOSITORY / ".test_tmp/native-followup-20261112/windows/matrix.json"
    rows = read(matrix)["rows"]
    plan = []
    for (host, source), stages in FAILED.items():
        row = next(
            item for item in rows if item["host"] == host and item["source"] == source
        )
        assert all(
            row["selected_stages"][stage]["status"] == "failed" for stage in stages
        )
        lane = BASE / host / source
        metadata = read(lane / "fixture.json")
        plan.append(
            {
                "host": host,
                "source": source,
                "failed_stages": stages,
                "minimal_index_setup_before_remove": "mutate" not in stages,
                "historical_selected": {
                    stage: row["selected_stages"][stage] for stage in stages
                },
                "retained_passes": {
                    stage: value
                    for stage, value in row["selected_stages"].items()
                    if value["status"] == "passed"
                },
                "workspace_id": metadata["workspace_id"],
                "source_state": source_state(metadata),
            }
        )
    return {
        "historical_matrix": str(matrix),
        "historical_matrix_sha256": native.digest(matrix),
        "runtime_python": str(PYTHON),
        "wheel_sha256": WHEEL_SHA,
        "whole_failed_initials_replay_existing_keys": True,
        "provider_process_limit": 1,
        "rows": plan,
    }


def validate_partial07(root, metadata, actual):
    """Recognize only the exact retained07 command and supported Covenant update."""
    from daem0nmcp.client_install import _json_document, _set_json_value
    from daem0nmcp.verification_v7 import verify_v7

    project = Path(metadata["project"])
    checkpoint = read(root / "checkpoint.json")
    if checkpoint["config_hashes"] != metadata["config_hashes"]:
        raise RuntimeError("Partial07 checkpoint differs from retained metadata")
    old_python = (
        REPOSITORY
        / ".test_tmp/native-followup-20261112/backend/.venv-07/Scripts/python.exe"
    )
    original = (root / "config-before/opencode.jsonc").read_text(encoding="utf-8")
    command = _json_document(original, comments=True).value["mcp"]["daem0nmcp"][
        "command"
    ]
    replacement = (
        [str(old_python), *command[1:]]
        if isinstance(command, list)
        else str(old_python)
    )
    expected = dict(checkpoint["config_hashes"])
    expected["opencode.jsonc"] = hashlib.sha256(
        _set_json_value(
            original, ("mcp", "daem0nmcp", "command"), replacement, comments=True
        ).encode()
    ).hexdigest()
    lane = BASE / "opencode/daem0n"
    covenant = read_json_command(
        old_python,
        [
            "-c",
            "import json; from daem0nmcp.client_install import _COVENANT; print(json.dumps(_COVENANT))",
        ],
        lane,
        native.environment(lane, "opencode"),
    )
    instructions = (root / "config-before/AGENTS.md").read_bytes().decode()
    newline = "\r\n" if "\r\n" in instructions else "\n"
    instructions = re.sub(
        r"<!-- daem0nmcp:v7:start -->.*?<!-- daem0nmcp:v7:end -->",
        lambda _: covenant.replace("\n", newline),
        instructions,
        flags=re.S,
    )
    expected["AGENTS.md"] = hashlib.sha256(instructions.encode()).hexdigest()
    if actual != expected:
        raise RuntimeError(
            "Partial07 files differ from exact retained command/Covenant update"
        )
    if (
        hashes(lane / "private") != checkpoint["private_profile_hashes"]
        or native.original_hashes() != checkpoint["original_user_hashes"]
        or json.loads(
            json.dumps(
                verify_v7(project / ".daem0nmcp/storage", metadata["workspace_id"])[
                    "authority_roots"
                ]
            )
        )
        != checkpoint["authority_roots"]
    ):
        raise RuntimeError("Partial07 authority/private/original hashes drifted")


def prepare(host, source, *, resume_partial07=False):
    from daem0nmcp.client_install import _json_document, _set_json_value
    from daem0nmcp.verification_v7 import verify_v7

    if native.digest(WHEEL) != WHEEL_SHA or not PYTHON.is_file():
        raise RuntimeError("Frozen 08 wheel/interpreter mismatch")
    lane = BASE / host / source
    destination = REPAIR / host / source
    root = destination
    if resume_partial07:
        if (host, source) != ("opencode", "daem0n") or (root / "binding.json").exists():
            raise RuntimeError("Only the retained incomplete07 preparation may resume")
        destination = root / "freeze08-preparation"
    if destination.exists():
        if not resume_partial07 or any(destination.iterdir()):
            raise RuntimeError("Preserve existing nonempty preparation evidence")
    else:
        destination.mkdir(parents=True, exist_ok=False)
    metadata_path = lane / "fixture.json"
    metadata = read(metadata_path)
    project = Path(metadata["project"])
    before = native.config_paths(project)
    if resume_partial07:
        validate_partial07(root, metadata, before)
    elif before != metadata["config_hashes"]:
        raise RuntimeError("Owned fixture configuration drifted before repair")
    state = source_state(metadata)
    if not state["original_source_matches"] or not state["added_probe_absent"]:
        raise RuntimeError(
            "Repair source preimages are not the retained original snapshot"
        )
    profiles = hashes(lane / "private")
    original = native.original_hashes()
    storage = project / ".daem0nmcp/storage"
    authority = verify_v7(storage, metadata["workspace_id"])["authority_roots"]
    shutil.copyfile(metadata_path, destination / "fixture-before.json")
    if not resume_partial07:
        shutil.copyfile(
            lane / "initial-validation.json",
            root / "historical-initial-validation.json",
        )
    for relative in before:
        target = destination / "config-before" / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(project / relative, target)
    pointer = read(storage / "active-db.json")
    database = (storage / pointer["active_db"]).resolve(strict=True)
    if not database.is_relative_to(storage.resolve()):
        raise RuntimeError("Active fixture database escapes its storage")
    with (
        closing(
            sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)
        ) as connection,
        closing(sqlite3.connect(destination / "database-before.db")) as backup,
    ):
        connection.backup(backup)
    write_json(
        destination / "checkpoint.json",
        {
            "source": state,
            "original_user_hashes": original,
            "private_profile_hashes": profiles,
            "config_hashes": before,
            "authority_roots": authority,
            "database_backup": str(destination / "database-before.db"),
            "database_backup_sha256": native.digest(destination / "database-before.db"),
            "active_pointer": pointer,
        },
    )
    if host == "pi":
        trust = read(lane / "approved-project.json")
        if (
            Path(trust["project"]).resolve() != project.resolve()
            or trust["fileHashes"] != before
        ):
            raise RuntimeError(
                "Pi exact reviewed trust receipt no longer matches fixture preimages"
            )
        shutil.copyfile(
            lane / "approved-project.json", destination / "pi-trust-before.json"
        )
    relative = {
        "opencode": "opencode.jsonc",
        "omp": ".omp/mcp.json",
        "pi": ".pi/mcp.json",
    }[host]
    config = project / relative
    namespace = "mcp" if host == "opencode" else "mcpServers"
    text = config.read_text(encoding="utf-8")
    command = _json_document(text, comments=host == "opencode").value[namespace][
        "daem0nmcp"
    ]["command"]
    executable = command[0] if isinstance(command, list) else command
    expected_python = (
        REPOSITORY
        / ".test_tmp/native-followup-20261112/backend/.venv-07/Scripts/python.exe"
        if resume_partial07
        else Path(metadata["backend_python"])
    )
    if Path(executable).resolve() != expected_python.resolve():
        raise RuntimeError("Registered Daem0n command differs from retained binding")
    replacement = (
        [str(PYTHON), *command[1:]] if isinstance(command, list) else str(PYTHON)
    )
    config.write_bytes(
        _set_json_value(
            text,
            (namespace, "daem0nmcp", "command"),
            replacement,
            comments=host == "opencode",
        ).encode()
    )
    rebound = native.config_paths(project)
    env = native.environment(lane, host)
    match = re.search(r"\d+\.\d+\.\d+", metadata["version_stdout"])
    if match is None:
        raise RuntimeError("Retained native client version missing")
    arguments = [
        "-m",
        "daem0nmcp.cli",
        "--json",
        "--project-path",
        str(project),
        "install-client",
        host,
        "--client-version",
        match.group(0),
    ]
    preview = read_json_command(PYTHON, [*arguments, "--dry-run"], lane, env)
    write_json(destination / "installer-preview.json", preview)
    if native.config_paths(project) != rebound:
        raise RuntimeError("Installer preview changed fixture bytes")
    installed = read_json_command(PYTHON, arguments, lane, env)
    write_json(destination / "installer-apply.json", installed)
    after = native.config_paths(project)
    if set(after) != set(before) or any(
        before[name] != after[name]
        for name in before
        if name not in {relative, "AGENTS.md"}
    ):
        raise RuntimeError(
            "Covenant-only installer update changed unrelated files or hooks"
        )
    old_agents = (destination / "config-before/AGENTS.md").read_bytes()
    new_agents = (project / "AGENTS.md").read_bytes()
    pattern = rb"<!-- daem0nmcp:v7:start -->.*?<!-- daem0nmcp:v7:end -->"
    if re.sub(pattern, b"<owned-covenant>", old_agents, flags=re.S) != re.sub(
        pattern, b"<owned-covenant>", new_agents, flags=re.S
    ):
        raise RuntimeError("Instruction refresh changed text outside its owned block")
    after_authority = verify_v7(storage, metadata["workspace_id"])["authority_roots"]
    if (
        after_authority != authority
        or profiles != hashes(lane / "private")
        or original != native.original_hashes()
    ):
        raise RuntimeError(
            "Instruction refresh changed authority, private profiles or original user settings"
        )
    native.runtime_provenance(
        PYTHON,
        lane,
        env,
        "repair08-binding",
        output=destination / "backend-provenance.json",
    )
    attestation = attest_installed(lane, env)
    write_json(destination / "installed-runtime-attestation.json", attestation)
    metadata["backend_python"], metadata["config_hashes"] = str(PYTHON), after
    write_json(metadata_path, metadata)
    if host == "pi":
        write_json(
            lane / "approved-project.json",
            {
                "project": str(project),
                "fileHashes": after,
                "authority": "Exact reviewed repair-06 MCP command and owned Covenant refresh; prior receipt and preimages retained",
                "prior_receipt_sha256": native.digest(
                    destination / "pi-trust-before.json"
                ),
            },
        )
    write_json(
        root / "binding.json",
        {
            "runtime_python": str(PYTHON),
            "wheel_sha256": WHEEL_SHA,
            "configuration_before": before,
            "configuration_after": after,
            "authority_roots_before": authority,
            "authority_roots_after": after_authority,
            "private_profiles_unchanged": True,
            "original_user_settings_unchanged": True,
            "only_owned_mcp_and_covenant_changed": True,
            "automation_omitted": True,
            "old_hooks_retained": True,
            "preparation_evidence": str(destination),
            "resumed_partial07": resume_partial07,
            "installed_runtime_attestation": str(
                destination / "installed-runtime-attestation.json"
            ),
        },
    )
    return {
        "host": host,
        "source": source,
        "evidence": str(destination),
        "status": "prepared",
    }


def attest_installed(lane, env):
    from native_backend_artifact import required_package_files, verify_wheel

    freeze = WHEEL.parent.parent
    expected = read(freeze / "manifest.json")["source_hashes"]
    wheel = verify_wheel(
        WHEEL, expected, required_package_files(freeze / "source", expected)
    )
    if wheel["wheel_sha256"] != WHEEL_SHA:
        raise RuntimeError("Wheel attestation changed the frozen digest")
    code = (
        "import hashlib,json; from pathlib import Path; import daem0nmcp; "
        "root=Path(daem0nmcp.__file__).parent; "
        "print(json.dumps({'daem0nmcp/'+p.relative_to(root).as_posix():"
        "hashlib.sha256(p.read_bytes()).hexdigest() for p in root.rglob('*') "
        "if p.is_file() and '__pycache__' not in p.parts and p.suffix not in {'.pyc','.pyo'}}))"
    )
    installed = read_json_command(PYTHON, ["-c", code], lane, env)
    if installed != wheel["package_files"]:
        raise RuntimeError(
            "Installed runtime inventory/hash differs from the fully attested wheel"
        )
    return {
        **wheel,
        "installed_package_files": installed,
        "installed_matches_wheel": True,
    }


def read_json_command(python, arguments, lane, env):
    return json.loads(native.backend_command(python, arguments, lane, env))


def final_matrix():
    from daem0nmcp.verification_v7 import verify_v7

    historical = REPOSITORY / ".test_tmp/native-followup-20261112/windows/matrix.json"
    rows = []
    for (host, source), stages in FAILED.items():
        destination = REPAIR / host / source
        metadata = read(BASE / host / source / "fixture.json")
        checkpoint = read(destination / "checkpoint.json")
        attempts = []
        for summary_path in sorted((destination / "attempts").glob("*/summary.json")):
            summary = read(summary_path)
            evidence = summary_path.parent
            assessment = evidence / "repair-assessment.json"
            attempts.append(
                {
                    "stage": summary["validation"]["stage"],
                    "attempt": evidence.name,
                    "status": read(assessment)["status"]
                    if assessment.exists()
                    else summary["validation"]["status"],
                    "native_strict_status": summary["validation"]["status"],
                    "summary": str(summary_path),
                    "summary_sha256": native.digest(summary_path),
                    "assessment": str(assessment) if assessment.exists() else None,
                    "assessment_sha256": native.digest(assessment),
                    "backend_provenance": str(
                        evidence.with_name(evidence.name + ".backend-provenance.json")
                    ),
                    "backend_provenance_sha256": native.digest(
                        evidence.with_name(evidence.name + ".backend-provenance.json")
                    ),
                    "request": str(evidence / "request.json"),
                    "request_sha256": native.digest(evidence / "request.json"),
                    "scenario_prompt_sha256": native.digest(
                        evidence / "scenario-prompt.txt"
                    ),
                    "necessary_setup_only": summary["validation"]["stage"]
                    == "index_setup",
                    "failed_whole_stage_replay": summary["validation"]["stage"]
                    == "initial",
                }
            )
        selected = {
            stage: next(
                (
                    attempt
                    for attempt in reversed(attempts)
                    if attempt["stage"] == stage
                ),
                {"stage": stage, "status": "not_run"},
            )
            for stage in stages
        }
        authority = json.loads(
            json.dumps(
                verify_v7(
                    Path(metadata["project"]) / ".daem0nmcp/storage",
                    metadata["workspace_id"],
                )["authority_roots"]
            )
        )
        rows.append(
            {
                "host": host,
                "source": source,
                "workspace_id": metadata["workspace_id"],
                "status": "passed"
                if all(item["status"] == "passed" for item in selected.values())
                else "failed"
                if any(item["status"] == "failed" for item in selected.values())
                else "pending",
                "client_version_stdout": metadata["version_stdout"],
                "backend_python": metadata["backend_python"],
                "backend_version": "7.0.0.dev0",
                "client_executable": metadata["binary"],
                "client_executable_sha256": native.digest(Path(metadata["binary"])),
                "client_binary_arguments": metadata.get("binary_arguments", []),
                "planned_model_unchanged": native.MODELS[host],
                "source_revision": metadata["manifest"]["source_revision"],
                "source_file_count": metadata["manifest"]["file_count"],
                "wheel_sha256": WHEEL_SHA,
                "binding": str(destination / "binding.json"),
                "binding_sha256": native.digest(destination / "binding.json"),
                "preparation_history": [
                    {
                        "freeze": "07"
                        if path == destination
                        and (destination / "freeze08-preparation").exists()
                        else "08",
                        "status": "partial_preparation_only_no_native_calls"
                        if path == destination
                        and (destination / "freeze08-preparation").exists()
                        else "completed_preparation",
                        "installer_preview": str(path / "installer-preview.json"),
                        "installer_preview_sha256": native.digest(
                            path / "installer-preview.json"
                        ),
                        "installer_apply": str(path / "installer-apply.json"),
                        "installer_apply_sha256": native.digest(
                            path / "installer-apply.json"
                        ),
                    }
                    for path in [
                        destination,
                        *sorted(destination.glob("freeze08-preparation")),
                    ]
                ],
                "source_before": checkpoint["source"],
                "source_after": source_state(metadata),
                "authority_roots_before": checkpoint["authority_roots"],
                "authority_roots_after": authority,
                "authority_unchanged": authority == checkpoint["authority_roots"],
                "original_user_settings_unchanged": native.original_hashes()
                == checkpoint["original_user_hashes"],
                "attempts": attempts,
                "selected_affected_stages": selected,
                "historical_successful_stages_retained_without_replay": True,
            }
        )
    return {
        "platform": "Windows native",
        "scope": "Affected failed workflows only; historical strict results retained",
        "historical_matrix": str(historical),
        "historical_matrix_sha256": native.digest(historical),
        "historical_matrix_unchanged": native.digest(historical)
        == read(REPAIR.parent / "windows-plan.json")["historical_matrix_sha256"],
        "rows": rows,
    }


def source_targets(metadata):
    project = Path(metadata["project"]).resolve()
    specification = metadata["manifest"]["scenario"]
    target, added = (
        project / specification["mutation_path"],
        project / specification["added_path"],
    )
    for path in (target, added):
        if path.is_symlink() or not path.resolve().is_relative_to(project):
            raise RuntimeError("Declared source preparation path escapes the fixture")
    return target, added, specification


def change_source(host, source, attempt, restore=False):
    destination = REPAIR / host / source
    if not (destination / "binding.json").is_file():
        raise RuntimeError("Complete guarded runtime binding before source preparation")
    metadata = read(BASE / host / source / "fixture.json")
    target, added, specification = source_targets(metadata)
    preparation = destination / "preparations" / attempt
    preparation.mkdir(parents=True, exist_ok=False)
    original_path = destination / "original-source.bin"
    expected_hash = metadata["manifest"]["files"][specification["mutation_path"]]
    if not restore:
        state = source_state(metadata)
        if not state["original_source_matches"] or not state["added_probe_absent"]:
            raise RuntimeError("Source mutation requires the exact original preimages")
        original = target.read_bytes()
        if original_path.exists():
            if original_path.read_bytes() != original:
                raise RuntimeError(
                    "Retained original source differs from current preimage"
                )
        else:
            with original_path.open("xb") as output:
                output.write(original)
        before = {str(target): native.digest(target), str(added): None}
        target.write_bytes(original + specification["mutation_append"].encode())
        added.write_bytes(specification["added_content"].encode())
    else:
        original = original_path.read_bytes()
        if (
            hashlib.sha256(original).hexdigest() != expected_hash
            or target.read_bytes()
            != original + specification["mutation_append"].encode()
            or added.read_bytes() != specification["added_content"].encode()
        ):
            raise RuntimeError(
                "Restore refuses changed source/preimages or an unrelated added file"
            )
        pending = pending_probes(metadata)
        write_json(
            preparation
            / (
                "pending-removal.json"
                if pending["both_probes_present"]
                else "cleanup-only-no-index-precondition.json"
            ),
            pending,
        )
        before = {str(target): native.digest(target), str(added): native.digest(added)}
        target.write_bytes(original)
        added.unlink()
    write_json(
        preparation / "source-transition.json",
        {
            "restore": restore,
            "before": before,
            "after": {
                str(target): native.digest(target),
                str(added): native.digest(added) if added.exists() else None,
            },
            "coordinator_preparation_only": True,
        },
    )
    return {
        "host": host,
        "source": source,
        "restore": restore,
        "evidence": str(preparation),
    }


def pending_probes(metadata):
    project = Path(metadata["project"])
    storage = project / ".daem0nmcp/storage"
    database = storage / read(storage / "active-db.json")["active_db"]
    with closing(
        sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)
    ) as connection:
        connection.row_factory = sqlite3.Row
        rows = [
            dict(row)
            for row in connection.execute(
                "SELECT * FROM discovery_code_entities WHERE workspace_id=?",
                (metadata["workspace_id"],),
            )
        ]
    generation = max((row["code_generation"] for row in rows), default=None)
    current = [row for row in rows if row["code_generation"] == generation]
    probes = {
        key: [
            row
            for row in current
            if row["qualified_name"].endswith(metadata["manifest"]["scenario"][key])
        ]
        for key in ("mutation_search", "added_search")
    }
    return {
        "workspace_id": metadata["workspace_id"],
        "generation": generation,
        "both_probes_present": all(probes.values()),
        "probe_rows": probes,
    }


def admit_stage(host, source, stage, destination):
    if stage in FAILED[(host, source)]:
        return None
    if stage == "index_setup":
        if "mutate" not in FAILED[(host, source)]:
            return None
        for path in sorted(
            (destination / "attempts").glob("*/summary.json"), reverse=True
        ):
            summary = read(path)
            if summary["validation"].get("stage") != "mutate":
                continue
            receipts = [
                json.loads(line)
                for line in (path.parent / "native-tool-receipts.jsonl")
                .read_text(encoding="utf-8")
                .splitlines()
            ]
            if summary["validation"]["status"] == "failed" and not any(
                row["operation"] == "code_index" for row in receipts
            ):
                return {
                    "failed_mutation_summary": str(path),
                    "sha256": native.digest(path),
                    "no_backend_index_observed": True,
                }
            break
    raise RuntimeError(
        "Only affected stages or their evidenced minimal state setup are authorized"
    )


def run_stage(host, source, stage, attempt):
    from daem0nmcp.verification_v7 import verify_v7

    destination = REPAIR / host / source
    binding = read(destination / "binding.json")
    if binding["wheel_sha256"] != WHEEL_SHA:
        raise RuntimeError("Native repair binding differs from the frozen wheel")
    setup_authority = admit_stage(host, source, stage, destination)
    metadata = read(BASE / host / source / "fixture.json")
    if host == "opencode" and Path(metadata["binary"]).suffix.lower() == ".cmd":
        wrapper = Path(metadata["binary"])
        target = wrapper.parent / "node_modules/opencode-ai/bin/opencode.exe"
        text = wrapper.read_text(encoding="utf-8")
        if (
            not text.endswith(
                '"%dp0%\\node_modules\\opencode-ai\\bin\\opencode.exe"   %*\n'
            )
            or not target.is_file()
        ):
            raise RuntimeError(
                "Unrecognized native npm wrapper; refuse arbitrary command resolution"
            )
        before_digest = native.digest(target)
        observed = subprocess.run(
            [str(target), "--version"],
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        )
        if (
            observed.stdout.strip() not in metadata["version_stdout"]
            or native.digest(target) != before_digest
        ):
            raise RuntimeError(
                "Direct native executable differs from retained client version"
            )
        write_json(
            destination / "native-executable-resolution.json",
            {
                "wrapper": str(wrapper),
                "wrapper_sha256": native.digest(wrapper),
                "executable": str(target),
                "executable_sha256": before_digest,
                "version_stdout": observed.stdout,
                "reason": "CMD %* forwarding interprets angle brackets in JSON prompt placeholders as redirection; use the same existing native executable directly",
            },
        )
        metadata["binary"] = str(target)
        write_json(BASE / host / source / "fixture.json", metadata)
    if host == "pi" and Path(metadata["binary"]).suffix.lower() == ".cmd":
        wrapper = Path(metadata["binary"])
        script = (
            wrapper.parent
            / "node_modules/@earendil-works/pi-coding-agent/dist/bundle/cli.js"
        )
        node = wrapper.parent / "node.exe"
        if not node.is_file():
            node = Path(shutil.which("node") or "")
        text = wrapper.read_text(encoding="utf-8")
        if (
            '"%dp0%\\node_modules\\@earendil-works\\pi-coding-agent\\dist\\bundle\\cli.js" %*'
            not in text
            or not script.is_file()
            or not node.is_file()
        ):
            raise RuntimeError("Unrecognized Pi npm wrapper; refuse command resolution")
        before = {str(path): native.digest(path) for path in (wrapper, node, script)}
        observed = subprocess.run(
            [str(node), str(script), "--version"],
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        )
        if observed.stdout.strip() not in metadata["version_stdout"] or before != {
            str(path): native.digest(path) for path in (wrapper, node, script)
        }:
            raise RuntimeError(
                "Resolved Pi runtime differs from retained client version"
            )
        write_json(
            destination / "native-executable-resolution.json",
            {
                "wrapper": str(wrapper),
                "node": str(node),
                "script": str(script),
                "hashes": before,
                "version_stdout": observed.stdout,
                "reason": "Invoke the exact existing npm target directly without CMD interpreting native prompt JSON",
            },
        )
        metadata["binary"], metadata["binary_arguments"] = str(node), [str(script)]
        write_json(BASE / host / source / "fixture.json", metadata)
    state = source_state(metadata)
    if stage in {"index_setup", "mutate"}:
        target, added, specification = source_targets(metadata)
        original = (destination / "original-source.bin").read_bytes()
        if (
            target.read_bytes() != original + specification["mutation_append"].encode()
            or added.read_bytes() != specification["added_content"].encode()
        ):
            raise RuntimeError(
                "Native mutation/index setup requires exact prepared source"
            )
    elif not state["original_source_matches"] or not state["added_probe_absent"]:
        raise RuntimeError("Native initial/removal/restart requires restored source")
    if stage == "remove":
        preconditions = list(
            (destination / "preparations").glob("*/pending-removal.json")
        )
        if not preconditions or not pending_probes(metadata)["both_probes_present"]:
            raise RuntimeError(
                "Removal must begin with freshly indexed stale probe entities"
            )
    evidence = destination / "attempts" / attempt
    context_path = evidence.with_name(evidence.name + ".stage-context.json")
    if evidence.exists() or context_path.exists():
        raise RuntimeError("Preserve existing native stage evidence")
    baseline = destination / (
        "initial-repair-validation.json"
        if stage == "initial"
        or (destination / "initial-repair-validation.json").exists()
        else "historical-initial-validation.json"
    )
    lock = REPAIR / "windows-provider.lease"
    lock.parent.mkdir(parents=True, exist_ok=True)
    with lock.open("x", encoding="utf-8") as output:
        json.dump(
            {
                "pid": os.getpid(),
                "host": host,
                "source": source,
                "stage": stage,
                "attempt": attempt,
            },
            output,
        )
    try:
        write_json(
            context_path,
            {
                "host": host,
                "source": source,
                "stage": stage,
                "workspace_id": metadata["workspace_id"],
                "binding": str(destination / "binding.json"),
                "binding_sha256": native.digest(destination / "binding.json"),
                "backend_python": metadata["backend_python"],
                "wheel_sha256": WHEEL_SHA,
                "config_hashes": native.config_paths(Path(metadata["project"])),
                "original_user_hashes": native.original_hashes(),
                "private_profile_hashes": hashes(BASE / host / source / "private"),
                "source_state": state,
                "current_probe_entities": pending_probes(metadata),
                "authority_roots": verify_v7(
                    Path(metadata["project"]) / ".daem0nmcp/storage",
                    metadata["workspace_id"],
                )["authority_roots"],
                "baseline_path": str(baseline),
                "baseline_sha256": native.digest(baseline),
                "historical_matrix_unchanged": True,
                "minimal_setup_after_failed_mutation": setup_authority,
            },
        )
        status = native.run(
            SimpleNamespace(
                base=BASE,
                host=host,
                source=source,
                stage=stage,
                attempt=attempt,
                model=None,
                evidence_dir=evidence,
                baseline_path=baseline,
            )
        )
        result = read(evidence / "summary.json")
        checks = {
            "native_strict_stage_passed": result["validation"]["status"] == "passed"
        }
        if stage == "initial":
            anchor = read(destination / "historical-initial-validation.json")
            checks["retained_seed_record_event_replayed"] = all(
                result["validation"].get(key) == anchor.get(key)
                for key in ("record_id", "event_id")
            )
            records = [
                json.loads(line)
                for line in (evidence / "native-tool-receipts.jsonl")
                .read_text(encoding="utf-8")
                .splitlines()
            ]
            stores = []
            for record in records:
                if record["operation"] != "memory_store":
                    continue
                for response in record["v7_responses"]:
                    if response.get("ok") is not True:
                        continue
                    data = response.get("data") or {}
                    if record["canonical_tool"] == "daem0n_tool_call":
                        data = data.get("data") or {}
                    stores.append(data)
            checks["native_store_idempotent_replay"] = bool(stores) and all(
                row.get("idempotent_replay") is True for row in stores
            )
        write_json(
            evidence / "repair-assessment.json",
            {
                "status": "passed" if all(checks.values()) else "failed",
                "stage": stage,
                "checks": checks,
                "native_summary_preserved": str(evidence / "summary.json"),
                "historical_fresh_write_evidence_retained": stage == "initial",
                "wheel_sha256": WHEEL_SHA,
            },
        )
        return status if all(checks.values()) else 1
    finally:
        lock.unlink()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "action", choices=["plan", "prepare", "modify", "restore", "run", "matrix"]
    )
    parser.add_argument("--host", choices=["opencode", "omp", "pi"])
    parser.add_argument("--source", choices=["daem0n", "legion"])
    parser.add_argument("--stage", choices=native.STAGES)
    parser.add_argument("--attempt", default="repair-06-01")
    parser.add_argument("--resume-partial07", action="store_true")
    args = parser.parse_args()
    if not re.fullmatch(r"repair-06[A-Za-z0-9_-]*", args.attempt):
        parser.error("Attempt must use the simple repair-06 prefix")
    if args.action == "plan":
        print(json.dumps(inventory(), indent=2))
    elif args.action == "matrix":
        print(json.dumps(final_matrix(), indent=2))
    else:
        if (args.host, args.source) not in FAILED:
            parser.error("An affected host/source pair is required")
        if args.action == "prepare":
            print(
                json.dumps(
                    prepare(
                        args.host, args.source, resume_partial07=args.resume_partial07
                    ),
                    indent=2,
                )
            )
        elif args.action in {"modify", "restore"}:
            print(
                json.dumps(
                    change_source(
                        args.host,
                        args.source,
                        args.attempt,
                        restore=args.action == "restore",
                    ),
                    indent=2,
                )
            )
        else:
            if args.stage is None:
                parser.error("--stage is required for a native run")
            return run_stage(args.host, args.source, args.stage, args.attempt)


if __name__ == "__main__":
    raise SystemExit(main())
