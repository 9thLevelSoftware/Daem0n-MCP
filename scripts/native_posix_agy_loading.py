"""Probe fresh agy projects using the existing isolated native account."""

from __future__ import annotations

import argparse
import copy
import json
import platform
import secrets
import subprocess
import time
from contextlib import suppress
from pathlib import Path

from native_acceptance_drivers import _close_owned_group, _spawn, redact, write_json
from native_agy_loading_acceptance import validate_results
from native_posix_acceptance import environment, original_hashes

from daem0nmcp.client_install import install_client


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--attempt", required=True)
    args = parser.parse_args()
    evidence = args.root / "agy-loading" / args.attempt
    evidence.mkdir(parents=True, exist_ok=False)
    parent = Path("/private/tmp" if platform.system() == "Darwin" else "/var/tmp")
    projects = (
        parent / "daem0nmcp-native-acceptance-20261112/agy-loading" / args.attempt
    )
    account = args.root / "private/agy-account"
    settings_path = account / ".gemini/antigravity-cli/settings.json"
    original_settings = settings_path.read_bytes()
    original = original_hashes()
    native = args.root / "tools/bin/agy"
    env = environment(args.root, "agy", "loading")
    prompt = (
        "Without invoking any tools, return a single JSON object containing "
        "project_marker (the exact project acceptance marker already present in your "
        "loaded instructions, or null if none), auto_brief_triggers (the memory calls "
        "that automatically brief a fresh session according to loaded project policy), "
        "and counsel_action (what that policy says to do on COUNSEL_REQUIRED). "
        "Use only context already supplied to you. Do not read or search files, run "
        "commands, invoke subagents, or fetch information. Do not invent a marker."
    )
    denials = [
        f"{name}(*)"
        for name in (
            "read_file",
            "write_file",
            "edit_file",
            "list_directory",
            "list_dir",
            "search_files",
            "find_by_name",
            "grep_search",
            "view_file",
            "view_file_outline",
            "command",
            "read_url",
            "execute_url",
            "download_url",
            "mcp",
        )
    ]
    results = {}
    cleanups = []
    try:
        for case in ("positive", "negative"):
            project = projects / case
            project.mkdir(parents=True, exist_ok=False)
            subprocess.run(
                ["git", "init", "--quiet", str(project)], env=env, check=True
            )
            installed = install_client(
                "agy", project, client_version="1.5.7", automation="off"
            )
            write_json(evidence / f"{case}-install.json", installed)
            marker = (
                "agy-preload-" + secrets.token_hex(16) if case == "positive" else None
            )
            if marker:
                with (project / "AGENTS.md").open("a") as stream:
                    stream.write("\nProject acceptance marker: " + marker + "\n")
            settings = copy.deepcopy(json.loads(original_settings))
            settings["trustedWorkspaces"] = [str(project)]
            settings["permissions"] = {"deny": denials}
            settings_path.write_text(json.dumps(settings, indent=2) + "\n")
            argv = [
                str(native),
                "--print",
                prompt,
                "--output-format",
                "stream-json",
                "--model",
                "gemini-3.8-flash-low",
                "--effort",
                "low",
                "--print-timeout",
                "3m",
            ]
            write_json(
                evidence / f"{case}-request.json",
                {
                    "argv": argv,
                    "cwd": str(project),
                    "denied_native_tools": denials,
                    "marker_in_prompt": bool(marker and marker in prompt),
                    "instruction_file_not_named_in_prompt": True,
                    "native_account": "Existing isolated account; no credentials copied",
                },
            )
            started = time.monotonic()
            child = _spawn(
                argv,
                cwd=project,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
            timed_out = False
            try:
                stdout, stderr = child.communicate(timeout=210)
            except subprocess.TimeoutExpired:
                timed_out = True
                cleanups.append(_close_owned_group(child))
                stdout, stderr = child.communicate(timeout=10)
            else:
                cleanups.append(_close_owned_group(child))
            stdout, stderr = redact(stdout), redact(stderr)
            (evidence / f"{case}-events.jsonl").write_text(stdout)
            (evidence / f"{case}-stderr.log").write_text(stderr)
            events = []
            for line in stdout.splitlines():
                with suppress(ValueError):
                    events.append(json.loads(line))
            results[case] = {
                "exit_code": child.returncode,
                "timed_out": timed_out,
                "elapsed_seconds": time.monotonic() - started,
                "tool_event_count": sum(
                    event.get("step_update", {}).get("step_type") == "tool"
                    for event in events
                ),
                "results": [
                    event["result"]
                    for event in events
                    if event.get("event") == "result"
                ],
                "expected_marker": marker,
            }
            write_json(evidence / f"{case}-summary.json", results[case])
    finally:
        settings_path.write_bytes(original_settings)
    checks = validate_results(results)
    checks["owned_account_settings_restored"] = (
        settings_path.read_bytes() == original_settings
    )
    checks["original_profiles_unchanged"] = original == original_hashes()
    summary = {
        "status": "passed" if all(checks.values()) else "failed",
        "checks": checks,
        "cases": results,
        "native_cleanup": cleanups,
        "evidence": str(evidence),
        "native_client": "agy 1.5.7",
    }
    write_json(evidence / "summary.json", summary)
    print(json.dumps(summary, indent=2))
    return 0 if summary["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
