"""Collect retained Windows native acceptance evidence without model calls."""

from __future__ import annotations

import argparse
import hashlib
import json
import platform
from pathlib import Path

HOSTS = ("claude", "codex", "agy", "opencode", "omp", "pi")


def read(path):
    return json.loads(path.read_text(encoding="utf-8-sig"))


def collect_lane(lane):
    fixture = read(lane / "fixture.json")
    attempts = []
    selected = {}
    resources = []
    for directory in sorted((lane / "evidence").iterdir()):
        if not directory.is_dir():
            continue
        original = directory / "summary.json"
        recollected = directory / "recollected-summary.json"
        path = recollected if recollected.exists() else original
        if not path.exists():
            continue
        report = read(path)
        validation = report["validation"]
        stage = validation["stage"]
        provenance = directory.with_name(directory.name + ".backend-provenance.json")
        attempt = {
            "attempt": directory.name,
            "stage": stage,
            "status": validation["status"],
            "summary": str(path),
            "summary_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "validation": validation,
            "original_summary": str(original) if original.exists() else None,
            "offline_revalidation": path == recollected,
            "backend_provenance": str(provenance) if provenance.is_file() else None,
            "backend_provenance_status": "retained_attempt_digest"
            if provenance.is_file()
            else "missing_attempt_digest",
            "owned_process_closed": report.get("owned_job_closed"),
        }
        if original.exists() and path != original:
            attempt["original_status"] = read(original)["validation"]["status"]
        attempts.append(attempt)
        if stage not in selected or path.stat().st_mtime_ns > selected[stage][0]:
            selected[stage] = (path.stat().st_mtime_ns, attempt)
        resource_path = directory / "native-resources.json"
        if resource_path.exists():
            resource = read(resource_path)
            resources.append(
                {
                    "evidence": str(resource_path),
                    "status": resource["status"],
                    "foreign_workspace_denied": resource["foreign_workspace_denied"],
                    "refresh_boundary": resource["refresh_boundary"],
                }
            )

    def proven(stage, keys):
        return any(
            attempt["stage"] == stage
            and all(attempt["validation"]["checks"].get(key) for key in keys)
            for attempt in attempts
        )

    quota = fixture["host"] == "claude"
    stages = {key: value[1] for key, value in selected.items()}
    status = (
        "blocked"
        if quota
        else "passed"
        if all(
            stages.get(stage, {}).get("status") == "passed"
            for stage in ("initial", "mutate", "remove", "restart")
        )
        else "failed"
    )
    return {
        "host": fixture["host"],
        "source": fixture["manifest"]["project"],
        "status": status,
        "blocker": "Native account weekly limit; observed remove-1 result, no further native quota retries"
        if quota
        else None,
        "fixture": str(lane / "fixture.json"),
        "fixture_backend_association": {
            "metadata": str(lane / "fixture.json"),
            "current_configured_python": fixture["backend_python"],
            "retarget_receipts": [
                str(path)
                for path in sorted((lane / "evidence").glob("retarget*/receipt.json"))
            ],
            "initial_installer_workspace_id_command": fixture.get("installation", {})
            .get("apply", {})
            .get("workspace_id_command"),
            "qualification": "Retained fixture configuration and frozen-runtime association; not retrospective per-attempt source-digest evidence",
        },
        "native_version": fixture["version_stdout"].strip(),
        "source_revision": fixture["manifest"]["source_revision"],
        "file_count": fixture["manifest"]["file_count"],
        "automation_installed_mode": fixture["automation"],
        "automation_execution_evidence": "Reported separately in native-automation-acceptance-2026-11-12.md; installation alone is not invocation proof",
        "original_files_unchanged_at_preparation": fixture["original_files_unchanged"],
        "capabilities": {
            "instruction_marker": proven(
                "initial", ("preloaded_instruction_marker_reported",)
            ),
            "core_memory": proven(
                "initial",
                (
                    "native_memory_store",
                    "native_memory_recall",
                    "stored_record_recalled",
                    "guided_auto_brief",
                    "native_outcome",
                ),
            ),
            "real_index_symbol_callers": any(
                proven(
                    stage,
                    (
                        "native_code_index",
                        "real_source_file_count",
                        "known_real_source_symbol",
                        "known_real_source_callers",
                    ),
                )
                for stage in ("initial", "index")
            ),
            "unchanged_index": any(
                proven(stage, ("unchanged_index_manifest",))
                for stage in ("initial", "index")
            ),
            "incremental_add_edit": proven(
                "mutate",
                (
                    "real_source_file_count",
                    "known_real_source_symbol",
                    "known_real_source_callers",
                    "mutation_search",
                    "added_search",
                ),
            ),
            "stale_removal": proven(
                "remove",
                (
                    "real_source_file_count",
                    "known_real_source_symbol",
                    "known_real_source_callers",
                    "mutation_search",
                    "added_search",
                ),
            ),
            "restart_memory": proven(
                "restart", ("same_key_original_record_event", "stored_record_recalled")
            ),
            "restart_code_without_rebuild": proven(
                "restart",
                (
                    "native_code_index",
                    "known_real_source_symbol",
                    "known_real_source_callers",
                    "mutation_search",
                    "added_search",
                ),
            ),
        },
        "selected_stages": {
            stage: {key: value for key, value in attempt.items() if key != "validation"}
            for stage, attempt in stages.items()
        },
        "attempts": attempts,
        "resources": resources
        or {
            "status": "blocked",
            "reason": "Not claimed by the real-indexing lane; native surface probes reported separately",
        },
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--base",
        type=Path,
        default=Path("D:/Daem0n-Native-Acceptance-20261112/windows"),
    )
    parser.add_argument(
        "--claude-base",
        type=Path,
        default=Path("D:/Daem0n-Native-Acceptance-20261112/windows-claude-retry"),
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path(".test_tmp/native-followup-20261112/windows/matrix.json"),
    )
    parser.add_argument(
        "--resource-lane",
        type=Path,
        default=Path(
            "D:/Daem0n-Native-Acceptance-20261112/windows-resources/codex/daem0n"
        ),
    )
    args = parser.parse_args()
    rows = [
        collect_lane(
            (args.claude_base if host == "claude" else args.base) / host / source
        )
        for host in HOSTS
        for source in ("daem0n", "legion")
    ]
    args.output.parent.mkdir(parents=True, exist_ok=True)
    followup = args.resource_lane / "evidence/resource-change-1/summary.json"
    args.output.write_bytes(
        (
            json.dumps(
                {
                    "platform": platform.platform(),
                    "scope": "Windows native acceptance; frozen source snapshots; no hosted CI inference",
                    "rows": rows,
                    "codex_changed_content_resource_followup": {
                        "evidence": str(followup),
                        "summary": read(followup),
                        "recollected_validation": read(
                            followup.with_name("recollected-summary.json")
                        )["validation"]
                        if followup.with_name("recollected-summary.json").exists()
                        else None,
                    }
                    if followup.exists()
                    else None,
                },
                indent=2,
            )
            + "\n"
        ).encode()
    )
    print(
        json.dumps(
            {
                "matrix": str(args.output),
                "rows": [
                    {
                        "host": row["host"],
                        "source": row["source"],
                        "status": row["status"],
                        "capabilities": row["capabilities"],
                    }
                    for row in rows
                ],
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
