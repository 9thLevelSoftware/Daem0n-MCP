"""Run bounded native edit/hook acceptance against an installed frozen wheel."""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import hashlib
import json
import os
import site
import sqlite3
import stat
import subprocess
import threading
import time
from contextlib import suppress
from pathlib import Path

from native_acceptance_drivers import _close_owned_group, _spawn, redact, write_json
from native_windows_acceptance import (
    api_key,
    backend_command,
    binary,
    environment,
    existing_access_credentials,
    original_hashes,
)

REPO = Path(__file__).resolve().parents[1]
BASE = Path("D:/Daem0n-Native-Acceptance-20261112/automation")
PYTHON = REPO / ".test_tmp/native-followup-20261112/backend/.venv/Scripts/python.exe"
CLAUDE = (
    REPO
    / ".test_tmp/native-followup-20261112/claude-enforce/node_modules/@anthropic-ai/claude-code/bin/claude.exe"
)
OPERATIONS = (
    "session_brief",
    "edit_preflight",
    "daem0n_tools_search",
    "daem0n_tool_call",
)


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None


def prepare(host, stage, attempt):
    lane = BASE / host / attempt
    lane.mkdir(parents=True, exist_ok=False)
    project = lane / "project"
    project.mkdir()
    subprocess.run(
        ["git", "-c", "init.defaultBranch=main", "init", "--quiet", str(project)],
        check=True,
    )
    env = environment(lane, host)
    for key in (
        "APPDATA",
        "LOCALAPPDATA",
        "XDG_CONFIG_HOME",
        "XDG_DATA_HOME",
        "XDG_CACHE_HOME",
    ):
        Path(env[key]).mkdir(parents=True, exist_ok=True)
    parser_cache = lane / "private/parser-cache"
    parser_cache.mkdir(parents=True)
    env["TREE_SITTER_LANGUAGE_PACK_CACHE_DIR"] = str(parser_cache)
    before = original_hashes()
    target = project / ("target.ipynb" if stage == "notebook" else "target.py")
    if stage in {"core", "failed", "edit", "preimage"}:
        target.write_text('VALUE = "before"\n')
    if stage == "failed" and host == "claude":
        target.chmod(stat.S_IREAD)
    if stage == "notebook":
        target.write_text(
            json.dumps(
                {"cells": [], "metadata": {}, "nbformat": 4, "nbformat_minor": 5}
            )
        )
    payload = json.loads(
        backend_command(
            PYTHON,
            [
                "-c",
                "import json; from daem0nmcp.claude_hooks.install import module_launch_args; from daem0nmcp.client_install import _COVENANT; print(json.dumps({'args':module_launch_args('daem0nmcp.server'),'covenant':_COVENANT}))",
            ],
            lane,
            env,
        )
    )
    if host == "claude":
        write_json(
            project / ".mcp.json",
            {
                "mcpServers": {
                    "daem0nmcp": {
                        "command": str(PYTHON),
                        "args": ["-E", "-u", "-X", "utf8", *payload["args"]],
                        "cwd": str(project),
                        "env": {
                            "DAEM0NMCP_PROJECT_ROOT": str(project),
                            "DAEM0NMCP_WORKSPACE_ROOTS": json.dumps([str(project)]),
                        },
                    }
                }
            },
        )
        (project / "CLAUDE.md").write_text(payload["covenant"])
        backend_command(
            PYTHON,
            [
                "-m",
                "daem0nmcp.claude_hooks.install",
                "--project-path",
                str(project),
                "--automation",
                "enforce",
                "--client-version",
                "2.1.295",
            ],
            lane,
            env,
        )
        settings = project / ".claude/settings.local.json"
        value = json.loads(settings.read_text())
        value["enabledMcpjsonServers"] = ["daem0nmcp"]
        if stage not in {"core", "failed", "notebook", "edit", "preimage"}:
            for entry in value["hooks"]["PreToolUse"]:
                if entry["matcher"] != "Edit|Write|NotebookEdit":
                    continue
                handler = entry["hooks"][0]
                if stage == "missing":
                    handler["command"] = str(lane / "missing-python.exe")
                elif stage == "crash":
                    handler["args"] = [
                        "-I",
                        "-c",
                        "raise RuntimeError('synthetic native hook crash')",
                    ]
                elif stage == "timeout":
                    handler["args"] = ["-I", "-c", "import time; time.sleep(30)"]
                    handler["timeout"] = 1
        write_json(settings, value)
        native = str(CLAUDE)
    else:
        (project / "AGENTS.md").write_text(
            "# Existing acceptance policy\nUse only the planned disposable file edits.\n"
        )
        backend_command(
            PYTHON,
            [
                "-m",
                "daem0nmcp.cli",
                "--json",
                "--project-path",
                str(project),
                "install-client",
                "opencode",
                "--client-version",
                "1.18.21",
                "--automation",
                "enforce",
            ],
            lane,
            env,
        )
        native = binary("opencode")
        if stage in {"missing", "crash", "timeout"}:
            plugin = project / ".opencode/plugins/daem0n.ts"
            source = plugin.read_text()
            if stage == "missing":
                marker = "const PYTHON_EXECUTABLE = " + json.dumps(str(PYTHON)) + ";"
                if marker not in source:
                    raise RuntimeError("Installed plugin interpreter marker missing")
                source = source.replace(
                    marker,
                    "const PYTHON_EXECUTABLE = "
                    + json.dumps(str(lane / "missing-python.exe"))
                    + ";",
                )
            else:
                injection = (
                    "raise RuntimeError('synthetic native hook crash')"
                    if stage == "crash"
                    else "import time; time.sleep(30)"
                )
                marker = "function hookLaunchCode(module: string): string {"
                if marker not in source:
                    raise RuntimeError("Installed plugin hook launcher marker missing")
                source = source.replace(
                    marker,
                    marker
                    + "\n  if (module === 'pre_edit') return "
                    + json.dumps(injection)
                    + ";",
                )
            plugin.write_text(source)
    workspace = json.loads(
        backend_command(
            PYTHON,
            [
                "-m",
                "daem0nmcp.cli",
                "--json",
                "--project-path",
                str(project),
                "workspace-id",
            ],
            lane,
            env,
        )
    )["workspace_id"]
    provenance = json.loads(
        backend_command(
            PYTHON,
            [
                "-c",
                "import json,daem0nmcp,sys; print(json.dumps({'package':daem0nmcp.__file__,'python':sys.version}))",
            ],
            lane,
            env,
        )
    )
    write_json(
        lane / "fixture.json",
        {
            "host": host,
            "stage": stage,
            "project": str(project),
            "target": str(target),
            "workspace_id": workspace,
            "binary": native,
            "backend": provenance,
            "original_hashes": before,
            "fault_injection": None if stage == "core" else stage,
            "initial_target_sha256": digest(target),
        },
    )
    return lane, project, target, workspace, native, env, before


def prompt(host, stage, target, workspace):
    if host == "claude":
        write = {"file_path": str(target), "content": 'VALUE = "after"\n'}
        changed = {**write, "content": 'VALUE = "unapproved"\n'}
        edit = {
            "file_path": str(target),
            "old_string": "ABSENT_NATIVE_ACCEPTANCE_SENTINEL",
            "new_string": "never applied",
            "replace_all": False,
        }
        write_name, edit_name = "Write", "Edit"
    else:
        write = {"filePath": str(target), "content": 'VALUE = "after"\n'}
        changed = {**write, "content": 'VALUE = "unapproved"\n'}
        edit = {
            "filePath": str(target),
            "oldString": "ABSENT_NATIVE_ACCEPTANCE_SENTINEL",
            "newString": "never applied",
            "replaceAll": False,
        }
        write_name, edit_name = "write", "edit"
    if stage == "preimage":
        return (
            f"Bounded preimage-change acceptance, workspace_id={workspace}. Call session_brief, then Read/read only {target}. "
            f"Call native {write_name} EXACTLY {json.dumps(write)}. Expect Daem0n denial and edit_preflight remedy; call real MCP edit_preflight for that exact request. "
            "Immediately after the actual approval response, the external test coordinator will change this disposable target to VALUE = coordinator. "
            "Read/read the target AGAIN to update the native host's file-read tracking, then retry the IDENTICAL original Write exactly once. "
            "The old approval must not authorize this changed preimage: expect a new denied request. Do not approve that new request, repair the content or retry again. "
            "List memory_capture_list through the actual gateway; expect zero candidates. Stop. No shell, scripts, subagents, other files, instruction/config edits or capture promotion."
        )
    if stage == "edit":
        valid_edit = {
            "file_path" if host == "claude" else "filePath": str(target),
            "old_string" if host == "claude" else "oldString": 'VALUE = "before"',
            "new_string" if host == "claude" else "newString": 'VALUE = "after"',
        }
        return (
            f"Bounded native {edit_name} success acceptance, workspace_id={workspace}. First call session_brief, then Read/read only {target}. "
            f"Call native {edit_name} EXACTLY {json.dumps(valid_edit)}. Its first attempt must be denied by Daem0n and return edit_preflight remedy. "
            "Call the actual MCP edit_preflight for that exact request, then retry IDENTICAL Edit once. "
            "Call daem0n_tool_call(workspace_id,tool='memory_capture_list',arguments={}) to inspect exactly one native Edit candidate and stop. No shell, scripts, subagents, other files/config edits or capture promotion."
        )
    if stage == "notebook":
        arguments = {
            "notebook_path": str(target),
            "new_source": "print('native notebook acceptance')",
            "cell_type": "code",
            "edit_mode": "insert",
        }
        return (
            f"Bounded native NotebookEdit acceptance, workspace_id={workspace}. First call session_brief and Read only {target}. "
            f"Call native NotebookEdit with EXACTLY {json.dumps(arguments)}. Its first attempt must be denied by Daem0n and return edit_preflight remedy. "
            "Call the actual MCP edit_preflight for that exact request, then retry IDENTICAL NotebookEdit once. Expect one successful inserted code cell. "
            "Call daem0n_tool_call(workspace_id,tool='memory_capture_list',arguments={}) to inspect exactly one native NotebookEdit candidate and stop. No shell, scripts, subagents, other file/config edits or capture promotion."
        )
    if stage == "failed" and host == "claude":
        return (
            f"Bounded readonly native Write failure acceptance, workspace_id={workspace}. Call session_brief then Read only {target}. "
            f"Call native Write EXACTLY {json.dumps(write)}. Daem0n must deny first and return edit_preflight remedy; call the REAL MCP edit_preflight and retry the IDENTICAL Write. "
            "The coordinator deliberately made this target readonly, so the approved native execution should fail on filesystem write. Do not change permissions or route around the failure. "
            "List capture candidates through daem0n_tool_call(tool='memory_capture_list',arguments={}); expect zero. "
            "Then repeat the exact same Write: it must get a NEW edit_request_id because the previous actual execution failed. Approve that new request through real edit_preflight and retry once more, expecting readonly failure. List zero captures and stop. "
            "Expected native denials/failures are the scenario; continue these specified steps. Use no shell, scripts, subagents or any other edits."
        )
    if stage == "failed":
        return (
            f"Run only the bounded unsuccessful-native-edit acceptance, workspace_id={workspace}. "
            f"Call session_brief, then {'Read only the target file. ' if host == 'claude' else ''}Use only native {edit_name} and actual registered Daem0n tools; no shell, config edits, other files or Write. "
            f"Call {edit_name} EXACTLY {json.dumps(edit)}. Its first attempt should be denied by Daem0n with an edit_preflight remedy. Call the REAL MCP edit_preflight with that returned request ID and truthful description, then retry EXACTLY the same Edit. The approved native edit must fail because old text is absent; do not change oldString or make the edit succeed. "
            "Call daem0n_tool_call with tool='memory_capture_list' and arguments={} to verify zero candidates. "
            f"Then call the identical {edit_name} again. Because the last approved native execution failed, it must receive a NEW edit_request_id. Call real edit_preflight for this new request and retry the identical Edit once more. It should fail natively again. List candidates again; expect zero. Stop. "
            "Treat these expected denials/native failures as the acceptance scenario, not a reason to stop early. Never route around the guard."
        )
    if stage != "core":
        return f"Native hook failure acceptance. Make exactly ONE native {write_name} call with these exact arguments: {json.dumps(write)}. If blocked, stop immediately and report the actual error. Do not retry, use another tool, call MCP, or alter configuration. This disposable target must remain absent when a native hook fails."
    return (
        f"Run the exact bounded native edit acceptance. workspace_id={workspace}. Use only native {write_name}/{edit_name} and actual registered Daem0n MCP tools. No shell, scripts, subagents, credentials, instructions/config edits or other source edits. "
        f"First call session_brief. {'Then Read only the target file to satisfy the native read-before-write check. ' if host == 'claude' else ''}Then call native {write_name} with EXACTLY {json.dumps(write)}. The Daem0n hook MUST deny first and give edit_preflight arguments. Call the real MCP edit_preflight using that workspace and edit_request_id, with a truthful description. "
        f"After that approval, intentionally call {write_name} ONCE with changed arguments {json.dumps(changed)}. This changed payload must be denied. Do NOT approve the changed payload. Then retry the original EXACT {json.dumps(write)}; the original staged approval should allow only this unchanged retry. "
        "Now use daem0n_tool_call(workspace_id,tool='memory_capture_list',arguments={}) and inspect the actual pending candidates; expect one successful native edit candidate. "
        f"Next call {edit_name} EXACTLY {json.dumps(edit)}. Its first attempt must require a new edit_preflight. Call that real MCP edit_preflight and retry the EXACT same native edit. This approved native edit must fail because old text is absent. Do NOT change old/new text or try to make this edit succeed. "
        "List capture candidates again; there must still be only the one successful write candidate. "
        f"Finally call the identical failing {edit_name} again. The observed failure should have settled the spent host binding, so this must get a NEW edit_request_id and real edit_preflight remedy. Approve this NEW request and retry the identical failing edit once; it should fail natively again. List capture candidates once more and stop. "
        "For expected native denial/failure continue exactly as specified, never route around the hook. Record IDs/errors from actual results only. Do not promote or store candidates. If provider authentication is unavailable report the actual error and stop."
    )


def envelopes(value):
    if isinstance(value, dict):
        if (
            value.get("api_version") == "7"
            and isinstance(value.get("ok"), bool)
            and isinstance(value.get("meta"), dict)
        ):
            yield value
        for item in value.values():
            yield from envelopes(item)
    elif isinstance(value, list):
        for item in value:
            yield from envelopes(item)
    elif isinstance(value, str) and value.lstrip().startswith(("{", "[")):
        with suppress(ValueError):
            yield from envelopes(json.loads(value))


def receipts(events, host):
    calls, output = {}, []
    for event in events:
        if host == "claude":
            for item in event.get("content", []):
                if not isinstance(item, dict):
                    continue
                if "name" in item and "input" in item and "id" in item:
                    calls[item["id"]] = item
                elif "tool_use_id" in item:
                    prior = calls.get(item["tool_use_id"], {})
                    output.append(
                        {
                            "tool": prior.get("name"),
                            "input": prior.get("input"),
                            "call_id": item["tool_use_id"],
                            "native_error": item.get("is_error", False),
                            "native_result": item.get("content"),
                            "envelopes": list(envelopes(item.get("content"))),
                        }
                    )
        elif event.get("type") == "tool_use":
            part = event.get("part", {})
            state = part.get("state", {})
            if state.get("status") not in {"completed", "error"}:
                continue
            result = state.get("output", state.get("error"))
            output.append(
                {
                    "tool": part.get("tool"),
                    "input": state.get("input"),
                    "call_id": part.get("callID", part.get("id")),
                    "native_error": state.get("status") == "error",
                    "native_result": result,
                    "envelopes": list(envelopes(result)),
                }
            )
    return output


def inject_preimage(stage, lane, target, events, host):
    if stage != "preimage" or (lane / "coordinator-preimage.json").exists():
        return
    for receipt in receipts(events, host):
        if not str(receipt["tool"]).endswith("edit_preflight"):
            continue
        approved = next(
            (
                response
                for response in receipt["envelopes"]
                if response.get("ok") is True
                and isinstance(response.get("data"), dict)
                and response["data"].get("edit_receipt")
            ),
            None,
        )
        if approved is None:
            continue
        before = digest(target)
        target.write_text('VALUE = "coordinator"\n')
        write_json(
            lane / "coordinator-preimage.json",
            {
                "action": "authorized disposable preimage mutation after actual native MCP approval response",
                "approved_request_id": approved["data"]["edit_request_id"],
                "before_sha256": before,
                "after_sha256": digest(target),
            },
        )
        return


async def claude_run(lane, project, target, native, env, message, stage):
    site.addsitedir(str(REPO / ".test_tmp/acceptance-20261112-summon/host-sdk"))
    from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient
    from claude_agent_sdk.types import (
        HookMatcher,
        PermissionResultAllow,
        PermissionResultDeny,
    )

    account_profile = BASE.parent / "accounts/claude/.claude"
    if account_profile.is_dir():
        env["CLAUDE_CONFIG_DIR"] = str(account_profile)
        env.pop("CLAUDE_CODE_OAUTH_TOKEN", None)
        token = ""
    else:
        token = existing_access_credentials("claude")["access_token"]
        env["CLAUDE_CODE_OAUTH_TOKEN"] = token
    events, approvals = [], []

    async def approve(name, arguments, context):
        native_edit = name in {
            "Read",
            "Write",
            "Edit",
            "NotebookEdit",
        } and arguments.get("file_path", arguments.get("notebook_path")) == str(target)
        mcp = name in {"mcp__daem0nmcp__" + operation for operation in OPERATIONS}
        if name.endswith("daem0n_tool_call"):
            mcp = (
                arguments.get("tool") == "memory_capture_list"
                and arguments.get("arguments") == {}
            )
        allowed = native_edit or mcp
        approvals.append(
            {
                "tool": name,
                "input_sha256": hashlib.sha256(
                    json.dumps(arguments, sort_keys=True).encode()
                ).hexdigest(),
                "allowed": allowed,
                "reason": "bounded disposable native edit/MCP acceptance",
            }
        )
        return (
            PermissionResultAllow(updated_input=arguments)
            if allowed
            else PermissionResultDeny(message="Outside exact native acceptance scope")
        )

    async def keep_stream_open(_input, _tool_use_id, _context):
        return {"continue_": True}

    def stderr(line):
        with (lane / "stderr.log").open("a", encoding="utf-8") as handle:
            handle.write(str(redact(line, (token,))) + "\n")

    options = ClaudeAgentOptions(
        cli_path=native,
        cwd=str(project),
        env=env,
        model="haiku",
        effort="low",
        max_budget_usd=3,
        permission_mode="default",
        can_use_tool=approve,
        allowed_tools=[],
        tools=["Read", "Write", "Edit", "NotebookEdit"],
        setting_sources=["user", "project", "local"],
        include_hook_events=True,
        stderr=stderr,
        mcp_servers=str(project / ".mcp.json"),
        strict_mcp_config=True,
        hooks={"PreToolUse": [HookMatcher(matcher=None, hooks=[keep_stream_open])]},
        extra_args={
            "no-session-persistence": None,
            "disable-slash-commands": None,
            "permission-prompts": "host",
        },
    )
    original_environment = dict(os.environ)
    os.environ.clear()
    os.environ.update(env)
    try:
        with (lane / "events.jsonl").open("w", encoding="utf-8") as log:
            async with asyncio.timeout(600):
                async with ClaudeSDKClient(options=options) as client:
                    readiness = []
                    for _ in range(240):
                        status = await client.get_mcp_status()
                        readiness.append(status)
                        servers = status.get(
                            "mcpServers", status.get("mcp_servers", [])
                        )
                        local = next(
                            (
                                server
                                for server in servers
                                if server.get("name") == "daem0nmcp"
                            ),
                            None,
                        )
                        if local is not None and local.get("status") == "connected":
                            break
                        await asyncio.sleep(0.5)
                    else:
                        write_json(lane / "mcp-readiness.json", readiness)
                        raise RuntimeError(
                            "Native MCP server did not become connected before the model turn"
                        )
                    write_json(lane / "mcp-readiness.json", readiness)
                    await client.query(message)
                    async for item in client.receive_response():
                        value = (
                            dataclasses.asdict(item)
                            if dataclasses.is_dataclass(item)
                            else {"message": str(item)}
                        )
                        value["sdk_message_type"] = type(item).__name__
                        value = redact(value, (token,))
                        events.append(value)
                        inject_preimage(stage, lane, target, events, "claude")
                        log.write(json.dumps(value, default=str) + "\n")
                        log.flush()
    finally:
        os.environ.clear()
        os.environ.update(original_environment)
        write_json(lane / "approvals.json", approvals)
    return events


def opencode_run(lane, project, target, native, env, message, stage):
    key = api_key("opencode", "minimax-coding-plan")
    env["MINIMAX_API_KEY"] = key
    env["OPENCODE_PERMISSION"] = json.dumps(
        {
            "*": "deny",
            "write": "allow",
            "read": {"*": "deny", str(project / "target.py"): "allow"},
            "edit": "allow",
            **{"daem0nmcp_" + operation: "allow" for operation in OPERATIONS},
        }
    )
    env["OPENCODE_DISABLE_AUTOUPDATE"] = "true"
    argv = [
        native,
        "run",
        "--format",
        "json",
        "--dir",
        str(project),
        "--model",
        "minimax-coding-plan/MiniMax-M3",
        message,
    ]
    process = _spawn(
        argv,
        cwd=project,
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
    )
    errors = []

    def read_errors():
        for line in process.stderr:
            errors.append(str(redact(line, (key,))))

    thread = threading.Thread(target=read_errors, daemon=True)
    thread.start()
    timer = threading.Timer(600, process.kill)
    timer.start()
    events = []
    try:
        with (lane / "events.jsonl").open("w", encoding="utf-8") as log:
            for line in process.stdout:
                safe = str(redact(line, (key,)))
                log.write(safe)
                log.flush()
                try:
                    events.append(json.loads(safe))
                    inject_preimage(stage, lane, target, events, "opencode")
                except ValueError:
                    pass
        process.wait(timeout=10)
    finally:
        timer.cancel()
        _close_owned_group(process)
        thread.join(timeout=3)
        (lane / "stderr.log").write_text("".join(errors))
    return events


def run(args):
    lane, project, target, workspace, native, env, before = prepare(
        args.host, args.stage, args.attempt
    )
    message = prompt(args.host, args.stage, target, workspace)
    write_json(
        lane / "request.json",
        {
            "prompt": message,
            "host": args.host,
            "stage": args.stage,
            "backend_python": str(PYTHON),
            "native_binary": native,
        },
    )
    started = time.monotonic()
    driver_error = None
    try:
        events = (
            asyncio.run(
                claude_run(lane, project, target, native, env, message, args.stage)
            )
            if args.host == "claude"
            else opencode_run(lane, project, target, native, env, message, args.stage)
        )
    except Exception as exc:
        driver_error = str(redact(str(exc)))
        events = (
            [
                json.loads(line)
                for line in (lane / "events.jsonl").read_text().splitlines()
                if line.strip().startswith("{")
            ]
            if (lane / "events.jsonl").is_file()
            else []
        )
    observed = receipts(events, args.host)
    (lane / "receipts.jsonl").write_text(
        "".join(json.dumps(row) + "\n" for row in observed)
    )
    db = project / ".daem0nmcp/storage/daem0nmcp.db"
    storage = {}
    if db.is_file():
        with sqlite3.connect(db.as_uri() + "?mode=ro", uri=True) as connection:
            for table in (
                "native_edit_pending",
                "native_edit_receipts",
                "memory_capture_candidates",
            ):
                storage[table] = [
                    dict(
                        zip(
                            [column[0] for column in cursor.description],
                            row,
                            strict=True,
                        )
                    )
                    for cursor in [connection.execute("SELECT * FROM " + table)]
                    for row in cursor
                ]
    write_json(lane / "storage.json", storage)
    writes = [row for row in observed if row["tool"] in {"Write", "write"}]
    edits = [row for row in observed if row["tool"] in {"Edit", "edit"}]
    preflights = [
        row
        for row in observed
        if str(row["tool"]).endswith("edit_preflight") and not row["native_error"]
    ]
    checks = {
        "original_files_unchanged": before == original_hashes(),
        "native_write_observed": bool(writes),
    }
    if args.stage == "preimage":
        checks = {
            "original_files_unchanged": before == original_hashes(),
            "actual_preflight_then_injection": (
                lane / "coordinator-preimage.json"
            ).exists(),
            "two_writes_denied": len(writes) >= 2
            and all(row["native_error"] for row in writes),
            "target_preserves_external_preimage": target.read_text()
            == 'VALUE = "coordinator"\n',
            "one_real_preflight": len(preflights) == 1,
            "staged_receipt_unconsumed": any(
                row.get("staged_at_us") is not None
                and row.get("consumed_at_us") is None
                for row in storage.get("native_edit_receipts", [])
            ),
            "zero_capture": len(storage.get("memory_capture_candidates", [])) == 0,
        }
    elif args.stage == "edit":
        checks = {
            "original_files_unchanged": before == original_hashes(),
            "first_native_edit_denied": bool(edits)
            and edits[0]["native_error"] is True,
            "real_edit_preflight": len(preflights) == 1,
            "native_edit_succeeded": any(not row["native_error"] for row in edits),
            "exact_file_written": target.read_text() == 'VALUE = "after"\n',
            "one_successful_capture": len(storage.get("memory_capture_candidates", []))
            == 1,
        }
    elif args.stage == "notebook":
        notebooks = [row for row in observed if row["tool"] == "NotebookEdit"]
        checks = {
            "original_files_unchanged": before == original_hashes(),
            "first_notebook_edit_denied": bool(notebooks)
            and notebooks[0]["native_error"] is True,
            "real_edit_preflight": len(preflights) == 1,
            "native_notebook_edit_succeeded": any(
                not row["native_error"] for row in notebooks
            ),
            "notebook_cell_written": "native notebook acceptance" in target.read_text(),
            "one_successful_capture": len(storage.get("memory_capture_candidates", []))
            == 1,
        }
    elif args.stage == "failed":
        unsuccessful = writes if args.host == "claude" else edits
        checks = {
            "original_files_unchanged": before == original_hashes(),
            "target_unchanged": target.read_text() == 'VALUE = "before"\n',
            "native_edit_observed": bool(unsuccessful),
            "all_edits_unsuccessful": bool(unsuccessful)
            and all(row["native_error"] for row in unsuccessful),
            "two_real_preflights": len(preflights) >= 2,
            "fresh_approval_after_failure": len(
                {
                    row["input"].get("edit_request_id")
                    for row in preflights
                    if isinstance(row["input"], dict)
                }
            )
            >= 2,
            "no_successful_capture": len(storage.get("memory_capture_candidates", []))
            == 0,
        }
    elif args.stage == "core":
        checks.update(
            {
                "exact_successful_file": target.read_text() == 'VALUE = "after"\n',
                "first_write_denied": bool(writes)
                and writes[0]["native_error"] is True,
                "changed_write_denied": any(
                    row["native_error"] and "unapproved" in json.dumps(row["input"])
                    for row in writes
                ),
                "successful_write": any(not row["native_error"] for row in writes),
                "real_edit_preflights": len(preflights) >= 3,
                "native_failed_edit_observed": any(
                    row["native_error"] and "old" in str(row["native_result"]).lower()
                    for row in edits
                ),
                "one_successful_capture": len(
                    storage.get("memory_capture_candidates", [])
                )
                == 1,
                "fresh_failed_call_approval": len(
                    {
                        row["input"].get("edit_request_id")
                        for row in preflights
                        if isinstance(row["input"], dict)
                    }
                )
                >= 3,
            }
        )
    else:
        results = "\n".join(str(row["native_result"]) for row in writes)
        checks.update(
            {
                "target_absent": not target.exists(),
                "write_denied": bool(writes)
                and all(row["native_error"] for row in writes),
                "native_failure_block_observed": (
                    "onFailure" in results
                    and ("blocking" in results or "block" in results)
                )
                if args.host == "claude"
                else "EDIT_BRIDGE_UNAVAILABLE" in results,
            }
        )
    summary = {
        "host": args.host,
        "stage": args.stage,
        "attempt": args.attempt,
        "status": "blocked"
        if driver_error
        and any(
            code in driver_error.lower()
            for code in ("401", "authenticate", "expired", "quota")
        )
        else "passed"
        if all(checks.values())
        else "failed",
        "checks": checks,
        "elapsed_seconds": round(time.monotonic() - started, 2),
        "native_receipt_count": len(observed),
        "native_write_count": len(writes),
        "native_edit_count": len(edits),
        "preflight_count": len(preflights),
        "evidence": str(lane),
        "driver_error": driver_error,
    }
    write_json(lane / "summary.json", summary)
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", choices=("claude", "opencode"), required=True)
    parser.add_argument(
        "--stage",
        choices=(
            "core",
            "failed",
            "edit",
            "preimage",
            "notebook",
            "missing",
            "crash",
            "timeout",
        ),
        required=True,
    )
    parser.add_argument("--attempt", required=True)
    args = parser.parse_args()
    run(args)
