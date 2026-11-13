"""Probe native agy instruction preloading without model filesystem access.

Uses the account's normal OS keyring, never extracts authentication material.
All generated configuration, projects and evidence belong to --output.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import secrets
import subprocess
import sys
import time
from pathlib import Path


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")


def digest(path: Path) -> str | None:
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None


def sanitize(value: str) -> str:
    value = re.sub(
        r"\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b", "<JWT>", value
    )
    value = re.sub(
        r"(?:sk-ant-|sk-proj-|sk-)[A-Za-z0-9_-]{12,}|Bearer\s+[^\s\"']+",
        "<CREDENTIAL>",
        value,
    )
    return re.sub(
        r'("(?:access_?token|refresh_?token|id_?token|api_?key|authorization)"\s*:\s*)"(?:[^"\\]|\\.)*"',
        r'\1"<AUTH_FIELD>"',
        value,
        flags=re.IGNORECASE,
    )


def isolated_environment(home: Path) -> dict[str, str]:
    excluded = (
        "DAEM0NMCP_",
        "CODEX_",
        "AGY_",
        "OPENCODE_",
        "OMP_",
        "PI_",
        "ANTHROPIC_",
        "OPENAI_",
        "GEMINI_",
        "GOOGLE_",
        "AZURE_",
        "HF_",
        "OTEL_",
        "CLAUDE_",
    )
    env = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith(excluded)
        and not k.endswith(("_API_KEY", "_AUTH_TOKEN", "_ACCESS_TOKEN"))
    }
    for name in ("PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV", "CLAUDECODE"):
        env.pop(name, None)
    env.update(
        HOME=str(home),
        USERPROFILE=str(home),
        APPDATA=str(home / "AppData/Roaming"),
        LOCALAPPDATA=str(home / "AppData/Local"),
        XDG_CONFIG_HOME=str(home / ".config"),
        XDG_DATA_HOME=str(home / ".local/share"),
        XDG_STATE_HOME=str(home / ".local/state"),
        XDG_CACHE_HOME=str(home / ".cache"),
        PYTHONUTF8="1",
        NO_COLOR="1",
    )
    return env


def validate_results(results: dict[str, object]) -> dict[str, bool]:
    checks: dict[str, bool] = {}
    triggers = {
        "memory_recall",
        "memory_preflight",
        "memory_store",
        "memory_record_outcome",
    }
    for case, summary in results.items():
        assert isinstance(summary, dict)
        checks[f"{case}_no_tool_calls"] = summary["tool_event_count"] == 0
        final = summary["results"]
        checks[f"{case}_native_success"] = (
            summary["exit_code"] == 0
            and len(final) == 1
            and final[0].get("status") == "SUCCESS"
        )
        response = final[0].get("response", "") if final else ""
        try:
            payload = json.loads(
                re.sub(r"^```(?:json)?\s*|\s*```$", "", response.strip())
            )
        except (ValueError, TypeError):
            payload = {}
        checks[f"{case}_marker"] = (
            "project_marker" in payload
            and payload["project_marker"] == summary["expected_marker"]
        )
        checks[f"{case}_triggers"] = (
            set(payload.get("auto_brief_triggers", [])) == triggers
        )
        action = str(payload.get("counsel_action", ""))
        checks[f"{case}_counsel"] = (
            "error.counsel" in action and "error.remedy" in action
        )
    return checks


def run(args: argparse.Namespace) -> int:
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    original_paths = json.loads(args.monitored_paths.read_text(encoding="utf-8"))
    before = {name: digest(Path(name)) for name in original_paths}
    write_json(output / "original-before.json", before)
    results: dict[str, object] = {}
    prompt = (
        "Without invoking any tools, return a single JSON object containing "
        "project_marker (the exact project acceptance marker already present in your "
        "loaded instructions, or null if none), auto_brief_triggers (the memory calls "
        "that automatically brief a fresh session according to loaded project policy), "
        "and counsel_action (what that policy says to do on COUNSEL_REQUIRED). "
        "Use only context already supplied to you. Do not read or search files, run "
        "commands, invoke subagents, or fetch information. Do not invent a marker."
    )
    for case in ("positive", "negative"):
        root = output / "projects" / case
        root.mkdir(parents=True)
        home = output / "profiles" / case
        home.mkdir(parents=True)
        env = isolated_environment(home)
        subprocess.run(
            ["git", "-c", "init.defaultBranch=main", "init", "--quiet", str(root)],
            check=True,
            env=env,
            capture_output=True,
        )
        install = subprocess.run(
            [
                str(args.python),
                "-m",
                "daem0nmcp.cli",
                "--json",
                "--project-path",
                str(root),
                "install-client",
                "agy",
                "--client-version",
                "1.4.7",
            ],
            cwd=args.backend,
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=120,
        )
        (output / f"{case}-install.json").write_text(
            sanitize(install.stdout), encoding="utf-8"
        )
        (output / f"{case}-install.stderr.log").write_text(
            sanitize(install.stderr), encoding="utf-8"
        )
        if install.returncode:
            raise RuntimeError(f"{case} installation failed; inspect retained evidence")
        marker = "agy-preload-" + secrets.token_hex(16) if case == "positive" else None
        if marker:
            with (root / "AGENTS.md").open("a", encoding="utf-8") as stream:
                stream.write(f"\nProject acceptance marker: {marker}\n")
        # The initialization-level instruction loader remains active, while all
        # model-driven filesystem/command/network/MCP operations are denied.
        settings = {
            "trustedWorkspaces": [str(root)],
            "permissions": {
                "deny": [
                    "read_file(*)",
                    "write_file(*)",
                    "command(*)",
                    "read_url(*)",
                    "execute_url(*)",
                    "download_url(*)",
                    "mcp(*)",
                ]
            },
        }
        write_json(home / ".gemini/antigravity-cli/settings.json", settings)
        command = [
            str(args.agy),
            "--print",
            prompt,
            "--output-format",
            "stream-json",
            "--model",
            args.model,
            "--effort",
            "low",
            "--print-timeout",
            "3m",
        ]
        write_json(
            output / f"{case}-request.json",
            {
                "argv": command,
                "cwd": str(root),
                "settings": settings,
                "instruction_sha256": digest(root / "AGENTS.md"),
                "auth_route": "normal shared OS keyring; no credentials extracted",
                "marker_in_prompt": bool(marker and marker in prompt),
            },
        )
        started = time.monotonic()
        proc = subprocess.Popen(
            command,
            cwd=root,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        try:
            stdout, stderr = proc.communicate(timeout=210)
        except subprocess.TimeoutExpired:
            if os.name == "nt":
                subprocess.run(
                    ["taskkill", "/PID", str(proc.pid), "/T", "/F"], capture_output=True
                )
            else:
                proc.kill()
            stdout, stderr = proc.communicate(timeout=15)
            stderr += "\nACCEPTANCE_DEADLINE_EXCEEDED\n"
        stdout, stderr = sanitize(stdout), sanitize(stderr)
        (output / f"{case}-events.jsonl").write_text(stdout, encoding="utf-8")
        (output / f"{case}-stderr.log").write_text(stderr, encoding="utf-8")
        events = []
        for line in stdout.splitlines():
            try:
                events.append(json.loads(line))
            except ValueError:
                continue
        tool_events = [
            e for e in events if e.get("step_update", {}).get("step_type") == "tool"
        ]
        final = [e.get("result") for e in events if e.get("event") == "result"]
        summary = {
            "exit_code": proc.returncode,
            "elapsed_seconds": round(time.monotonic() - started, 3),
            "tool_event_count": len(tool_events),
            "results": final,
            "expected_marker": marker,
            "marker_in_final": bool(marker and marker in json.dumps(final)),
            "native_process_pid": proc.pid,
        }
        write_json(output / f"{case}-summary.json", summary)
        results[case] = summary
        print(json.dumps({"case": case, **summary}), flush=True)
    after = {name: digest(Path(name)) for name in original_paths}
    write_json(output / "original-after.json", after)
    checks = validate_results(results)
    write_json(
        output / "summary.json",
        {
            "cases": results,
            "checks": checks,
            "status": "passed" if all(checks.values()) else "failed",
            "changed_original_paths": [
                name for name in before if before[name] != after[name]
            ],
            "prompt_identical": True,
            "instructions_not_named_in_prompt": True,
        },
    )
    return 0 if all(checks.values()) else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--backend", type=Path, required=True)
    parser.add_argument("--python", type=Path, required=True)
    parser.add_argument("--agy", type=Path, required=True)
    parser.add_argument("--monitored-paths", type=Path, required=True)
    parser.add_argument("--model", default="gemini-3.8-flash-low")
    sys.exit(run(parser.parse_args()))
