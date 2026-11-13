"""Observe installed reminder adapters during actual, bounded native turns."""

from __future__ import annotations

import argparse
import hashlib
import json
import queue
import subprocess
import threading
import time
from pathlib import Path

from native_acceptance_drivers import _close_owned_group, _spawn, redact, write_json
from native_windows_acceptance import (
    LEGACY,
    MODELS,
    api_key,
    backend_command,
    binary,
    environment,
    existing_access_credentials,
    original_hashes,
)

REPO = Path(__file__).resolve().parents[1]
BASE = Path("D:/Daem0n-Native-Acceptance-20261112/automation/reminders")
PYTHON = REPO / ".test_tmp/native-followup-20261112/backend/.venv/Scripts/python.exe"
PROMPT = "Reply exactly REMINDER_NATIVE_OK. Do not call any tools or read any files."


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


COMMAND_OBSERVER = """import json, subprocess, sys
from pathlib import Path
definition = json.loads(Path(sys.argv[1]).read_text())
event = sys.stdin.buffer.read()
result = subprocess.run(definition["command"], input=event, capture_output=True, shell=True)
with Path(definition["log"]).open("a", encoding="utf-8") as log:
    log.write(json.dumps({"event":definition["event"],"input":event.decode("utf-8", "replace"),"stdout":result.stdout.decode("utf-8", "replace"),"stderr":result.stderr.decode("utf-8", "replace"),"returncode":result.returncode})+"\\n")
sys.stdout.buffer.write(result.stdout)
sys.stderr.buffer.write(result.stderr)
raise SystemExit(result.returncode)
"""


def prepare(host, attempt, diagnostic_match_all=False, replacements=False):
    lane = BASE / host / attempt
    lane.mkdir(parents=True, exist_ok=False)
    project = lane / "project"
    project.mkdir()
    if replacements:
        (project / "reminder-replace.txt").write_text(
            "FIRST = old\nKEEP_1 = stable\nMIDDLE = old\nKEEP_2 = stable\nLAST = old\n"
        )
    subprocess.run(["git", "init", "--quiet", str(project)], check=True)
    env = environment(lane, host)
    original = original_hashes()
    native = binary(host)
    version = subprocess.run(
        [native, "--version"], capture_output=True, text=True, timeout=30, env=env
    ).stdout.strip()
    installed = json.loads(
        backend_command(
            PYTHON,
            [
                "-m",
                "daem0nmcp.cli",
                "--json",
                "--project-path",
                str(project),
                "install-client",
                host,
                "--automation",
                "remind",
            ],
            lane,
            env,
        )
    )
    observer_log = lane / "observations.jsonl"
    if host in {"codex", "agy"}:
        config = project / (
            ".codex/hooks.json" if host == "codex" else ".agents/hooks.json"
        )
        value = json.loads(config.read_text())
        before = digest(config)
        (lane / "installed-hooks.json").write_bytes(config.read_bytes())
        if diagnostic_match_all:
            assert host == "agy"
            value["daem0nmcp"]["PreToolUse"][0]["matcher"] = "*"
        observer = lane / "command-observer.py"
        observer.write_text(COMMAND_OBSERVER)
        count = 0

        def wrap(node, event=None):
            nonlocal count
            if isinstance(node, dict):
                if node.get(
                    "type"
                ) == "command" and "daem0nmcp.native_reminders" in node.get(
                    "command", ""
                ):
                    count += 1
                    definition = lane / f"command-{count}.json"
                    write_json(
                        definition,
                        {
                            "command": node["command"],
                            "event": event,
                            "log": str(observer_log),
                        },
                    )
                    node["command"] = subprocess.list2cmdline(
                        [str(PYTHON), "-I", str(observer), str(definition)]
                    )
                else:
                    for key, child in node.items():
                        wrap(
                            child,
                            key
                            if key in {"SessionStart", "PreToolUse", "Stop"}
                            else event,
                        )
            elif isinstance(node, list):
                for child in node:
                    wrap(child, event)

        wrap(value)
        assert count == 3
        write_json(config, value)
        observation = {
            "route": "observational command wrapper; forwards exact stdin/stdout/stderr and exit status",
            "installed_sha256": before,
            "observed_config_sha256": digest(config),
            "wrapped_commands": count,
            "diagnostic_match_all": diagnostic_match_all,
        }
    else:
        config = project / f".{host}/extensions/daem0n.ts"
        source = lane / "installed-daem0n.ts"
        source.write_bytes(config.read_bytes())
        before = digest(source)
        config.write_text(
            "import original from " + json.dumps(str(source).replace("\\", "/")) + ";\n"
            'import { appendFileSync } from "node:fs";\n'
            "const output = " + json.dumps(str(observer_log)) + ";\n"
            "export default function (api) {\n"
            "  original(new Proxy(api, { get(target, key) {\n"
            '    if (key === "on") return (name, callback) => target.on(name, async (...args) => {\n'
            "      const result = await callback(...args);\n"
            '      appendFileSync(output, JSON.stringify({event:name,result})+"\\n");\n'
            "      return result;\n"
            "    });\n"
            '    const value = Reflect.get(target,key); return typeof value === "function" ? value.bind(target) : value;\n'
            "  }}));\n}\n"
        )
        observation = {
            "route": "observational API proxy around byte-preserved shipped extension; only native callbacks invoke it",
            "installed_sha256": before,
            "observed_config_sha256": digest(config),
        }
    if host == "codex":
        home = Path(env["CODEX_HOME"])
        home.mkdir(parents=True, exist_ok=True)
        (home / "config.toml").write_text(
            'cli_auth_credentials_store = "ephemeral"\n'
            + f'[projects.{json.dumps(str(project))}]\ntrust_level = "trusted"\n'
        )
    if host == "agy":
        write_json(
            Path(env["HOME"]) / ".gemini/antigravity-cli/settings.json",
            {
                "trustedWorkspaces": [str(project)],
                "permissions": {
                    "deny": [
                        "run_command(*)",
                        "write_to_file(*)",
                        "replace_file_content(*)",
                        "multi_replace_file_content(*)",
                        "read_url_content(*)",
                    ]
                },
            },
        )
    if host == "pi":
        trust = (LEGACY / "pi-project-trust.mjs").read_text()
        for old, new in [
            (LEGACY / "projects/pi", project),
            (
                LEGACY / "evidence/pi/approved-project.json",
                lane / "approved-project.json",
            ),
            (LEGACY / "evidence/pi/project-trust.jsonl", lane / "project-trust.jsonl"),
        ]:
            trust = trust.replace(json.dumps(str(old)), json.dumps(str(new)))
        (lane / "project-trust.mjs").write_text(trust)
        reviewed = {
            str(path.relative_to(project)): digest(path)
            for path in project.rglob("*")
            if path.is_file() and ".git" not in path.parts
        }
        write_json(
            lane / "approved-project.json",
            {"project": str(project), "fileHashes": reviewed},
        )
    write_json(
        lane / "fixture.json",
        {
            "host": host,
            "backend_python": str(PYTHON),
            "version": version,
            "binary": native,
            "project": str(project),
            "installer": installed,
            "observation": observation,
            "original_hashes": original,
            "prompt": PROMPT,
        },
    )
    return lane, project, env, native, original


def native_turn(
    host,
    lane,
    project,
    env,
    native,
    approval=None,
    timeout=150,
    argv_prefix=None,
    native_account=False,
):
    secrets = []
    if host == "codex":
        credentials = {} if native_account else existing_access_credentials(host)
        secrets.extend(credentials.values())
        argv = [native, "app-server"]
    elif host == "agy":
        argv = [
            native,
            "--print",
            PROMPT,
            "--output-format",
            "stream-json",
            "--model",
            MODELS[host],
            "--effort",
            "low",
            "--print-timeout",
            "2m",
        ]
    else:
        key = api_key("pi", "minimax")
        secrets.append(key)
        env["MINIMAX_API_KEY"] = key
        if host == "pi":
            argv = [
                native,
                "--mode",
                "json",
                "--print",
                "--no-session",
                "--exclude-tools",
                "bash,edit,write",
                "--extension",
                str(lane / "project-trust.mjs"),
                "--no-skills",
                "--no-prompt-templates",
                "--no-themes",
                "--provider",
                "minimax",
                "--model",
                MODELS[host],
                "--thinking",
                "low",
                PROMPT,
            ]
        else:
            argv = [
                native,
                "--cwd",
                str(project),
                "--mode",
                "rpc-ui",
                "--approval-mode",
                "always-ask",
                "--no-session",
                "--no-title",
                "--no-lsp",
                "--no-skills",
                "--no-prewalk",
                "--thinking",
                "low",
                "--max-cost",
                "1",
                "--max-time",
                "2m",
                "--model",
                MODELS[host],
            ]
    if argv_prefix:
        argv = [*argv_prefix, *argv[1:]]
    process = _spawn(
        argv,
        cwd=project,
        env=env,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
    )
    messages = queue.Queue()
    events = []

    def read_stdout():
        with (lane / "events.jsonl").open("w", encoding="utf-8") as log:
            for line in process.stdout:
                try:
                    event = json.loads(line)
                except ValueError:
                    event = {"non_protocol_stdout": line}
                safe = redact(event, secrets)
                events.append(safe)
                log.write(json.dumps(safe) + "\n")
                log.flush()
                messages.put(event)
        messages.put({"exit": True})

    def read_stderr():
        with (lane / "stderr.log").open("w", encoding="utf-8") as log:
            for line in process.stderr:
                log.write(redact(line, secrets))

    threads = [
        threading.Thread(target=read_stdout, daemon=True),
        threading.Thread(target=read_stderr, daemon=True),
    ]
    for thread in threads:
        thread.start()
    started = time.monotonic()
    identifier = 0

    def send(value):
        process.stdin.write(json.dumps(value) + "\n")
        process.stdin.flush()

    def receive():
        while time.monotonic() - started < timeout:
            try:
                event = messages.get(timeout=5)
            except queue.Empty:
                continue
            if event.get("exit"):
                raise RuntimeError("Native client exited")
            if "id" in event and "method" in event:
                send(
                    {
                        "id": event["id"],
                        "error": {
                            "code": -32601,
                            "message": "No tool or refresh authorization in reminder-only probe",
                        },
                    }
                )
            return event
        raise TimeoutError("Bounded reminder native turn timed out")

    def call(method, params):
        nonlocal identifier
        identifier += 1
        expected = identifier
        send({"id": expected, "method": method, "params": params})
        while True:
            event = receive()
            if event.get("id") == expected and "method" not in event:
                if event.get("error"):
                    raise RuntimeError(f"Native RPC {method}: {event['error']}")
                return event.get("result")

    error = None
    complete = False
    try:
        if host == "codex":
            call(
                "initialize",
                {
                    "clientInfo": {"name": "native-reminder-probe", "version": "1"},
                    "capabilities": {"experimentalApi": True},
                },
            )
            send({"method": "initialized"})
            if not native_account:
                call(
                    "account/login/start",
                    {
                        "type": "chatgptAuthTokens",
                        "accessToken": credentials["access_token"],
                        "chatgptAccountId": credentials["account_id"],
                    },
                )
            inventory = call("hooks/list", {"cwds": [str(project)]})
            write_json(lane / "native-hooks-inventory.json", redact(inventory, secrets))
            hooks = [hook for entry in inventory["data"] for hook in entry["hooks"]]
            assert len(hooks) == 3
            assert all(
                Path(hook["sourcePath"]).resolve()
                == (project / ".codex/hooks.json").resolve()
                for hook in hooks
            )
            edits = [
                {
                    "keyPath": "hooks.state."
                    + json.dumps(hook["key"])
                    + ".trusted_hash",
                    "value": hook["currentHash"],
                    "mergeStrategy": "upsert",
                }
                for hook in hooks
            ]
            reviewed = call(
                "config/batchWrite",
                {
                    "edits": edits,
                    "filePath": str(Path(env["CODEX_HOME"]) / "config.toml"),
                    "reloadUserConfig": True,
                },
            )
            write_json(
                lane / "native-exact-hook-trust.json",
                {"reviewed_hooks": hooks, "edits": edits, "result": reviewed},
            )
            trusted = call("hooks/list", {"cwds": [str(project)]})
            write_json(lane / "native-trusted-hooks-inventory.json", trusted)
            assert all(
                hook["trustStatus"] == "trusted"
                for entry in trusted["data"]
                for hook in entry["hooks"]
            )
            thread = call(
                "thread/start",
                {
                    "cwd": str(project),
                    "model": MODELS[host],
                    "approvalPolicy": "untrusted",
                    "sandbox": "read-only",
                    "ephemeral": True,
                    "config": {"model_reasoning_effort": "low"},
                },
            )
            call(
                "turn/start",
                {
                    "threadId": thread["thread"]["id"],
                    "input": [{"type": "text", "text": PROMPT}],
                    "effort": "low",
                },
            )
            while True:
                event = receive()
                if event.get("method") == "turn/completed":
                    complete = True
                    break
        elif host == "omp":
            send({"id": "reminder-native", "type": "prompt", "message": PROMPT})
            while True:
                event = receive()
                if (
                    event.get("type") == "extension_ui_request"
                    and event.get("method") == "select"
                ):
                    send(
                        {
                            "type": "extension_ui_response",
                            "id": event["id"],
                            "value": approval(event, events) if approval else "Deny",
                        }
                    )
                if event.get("type") in {"agent_end", "agent_settled"}:
                    complete = True
                    break
        else:
            process.stdin.close()
            process.wait(timeout=timeout)
            complete = process.returncode == 0
        time.sleep(0.5)
    except Exception as exc:
        error = str(exc)
    finally:
        cleanup = _close_owned_group(process)
        for thread in threads:
            thread.join(timeout=5)
    return {
        "complete": complete,
        "error": redact(error, secrets),
        "cleanup": cleanup,
        "elapsed_seconds": round(time.monotonic() - started, 2),
    }


def main():
    global PROMPT, PYTHON
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", required=True, choices=["codex", "agy", "omp", "pi"])
    parser.add_argument("--attempt", required=True)
    parser.add_argument("--edit-matcher", action="store_true")
    parser.add_argument("--diagnostic-agy-match-all", action="store_true")
    parser.add_argument("--agy-replacements", action="store_true")
    parser.add_argument("--agy-write-edit", action="store_true")
    parser.add_argument("--backend-python", type=Path, default=PYTHON)
    args = parser.parse_args()
    PYTHON = args.backend_python
    if args.edit_matcher:
        if args.host not in {"codex", "agy"}:
            parser.error("Only command-hook hosts have native edit matchers")
        tool = "apply_patch" if args.host == "codex" else "write_to_file"
        PROMPT = f"Attempt exactly one native {tool} call to create the workspace-relative file reminder-native.txt containing exactly REMINDER_EDIT_PROBE followed by a newline. Do not use a shell or other tools, and do not call MCP tools. If native approval or policy denies the edit, stop immediately and report it; do not retry or work around the denial."
    if args.agy_replacements:
        assert args.host == "agy" and args.diagnostic_agy_match_all
        PROMPT = "This disposable project has reminder-replace.txt containing five lines: FIRST = old; KEEP_1 = stable; MIDDLE = old; KEEP_2 = stable; LAST = old (each on its own line). First use exactly one native replace_file_content to change FIRST = old to FIRST = single. Then use exactly one native multi_replace_file_content to change the two noncontiguous lines MIDDLE = old to MIDDLE = multi and LAST = old to LAST = multi. Do not use shell, write_to_file, MCP tools or other tools. Perform only these two requested native edits and stop."
    if args.agy_write_edit:
        assert args.host == "agy" and not args.diagnostic_agy_match_all
        PROMPT = "In this disposable project, use native write_to_file exactly once to create reminder-native.txt containing REMINDER_EDIT_PROBE followed by a newline. Then use native replace_file_content exactly once to change FIRST = old to FIRST = single in existing reminder-replace.txt. Its five lines are FIRST = old; KEEP_1 = stable; MIDDLE = old; KEEP_2 = stable; LAST = old (each on its own line). Do not use shell or MCP tools, and do not edit other files. Native Read of that exact replacement file is permitted if required. Stop after these two requested edits."
    lane, project, env, native, original = prepare(
        args.host,
        args.attempt,
        args.diagnostic_agy_match_all,
        args.agy_replacements or args.agy_write_edit,
    )
    result = native_turn(args.host, lane, project, env, native)
    log = lane / "observations.jsonl"
    observations = (
        [json.loads(line) for line in log.read_text().splitlines()]
        if log.exists()
        else []
    )
    checks = {
        "native_turn_completed": result["complete"],
        "adapter_invoked": bool(observations),
        "original_settings_unchanged": original == original_hashes(),
    }
    if args.host in {"omp", "pi"}:
        checks["native_callback_result"] = any(
            item.get("event") == "before_agent_start"
            and item.get("result", {}).get("message", {}).get("customType")
            == "daem0n-covenant-reminder"
            for item in observations
        )
    else:
        checks["session_start_result"] = any(
            item.get("event") == "SessionStart"
            and item.get("returncode") == 0
            and "Daem0n:" in item.get("stdout", "")
            for item in observations
        )
        checks["stop_observed"] = any(
            item.get("event") == "Stop" for item in observations
        )
        if args.edit_matcher:
            checks["native_edit_matcher_invoked"] = any(
                item.get("event") == "PreToolUse"
                and item.get("returncode") == 0
                and "Daem0n:" in item.get("stdout", "")
                for item in observations
            )
            checks["unapproved_target_absent"] = not (
                project / "reminder-native.txt"
            ).exists()
        if args.agy_write_edit:
            calls = [
                json.loads(item["input"])["toolCall"]
                for item in observations
                if item.get("event") == "PreToolUse"
                and item.get("returncode") == 0
                and "Daem0n:" in item.get("stdout", "")
            ]
            checks["installed_write_alias_invoked"] = any(
                call["name"] == "write_file" for call in calls
            )
            checks["installed_edit_alias_invoked"] = any(
                call["name"] == "edit_file" for call in calls
            )
            checks["exact_new_file"] = (
                project / "reminder-native.txt"
            ).read_text() == "REMINDER_EDIT_PROBE\n"
            checks["exact_replacement_file"] = (
                (project / "reminder-replace.txt").read_text()
                == "FIRST = single\nKEEP_1 = stable\nMIDDLE = old\nKEEP_2 = stable\nLAST = old\n"
            )
    summary = {
        "host": args.host,
        "status": "passed" if all(checks.values()) else "failed",
        "checks": checks,
        "native": result,
        "evidence": str(lane),
        "events_observed": [item.get("event") for item in observations],
        "edit_matcher": "installed matcher observed on actual native write_file and edit_file"
        if args.agy_write_edit
        else "native edit requested once, native edit approval withheld"
        if args.edit_matcher
        else "not exercised; no native filesystem tools requested",
    }
    write_json(lane / "summary.json", summary)
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
