"""Portable native Claude fault probe using project hooks and normal host auth."""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import json
import os
import re
import site
import sqlite3
from pathlib import Path


def safe(value):
    if isinstance(value, dict):
        return {
            key: "<REDACTED_AUTH>"
            if key.lower().replace("_", "")
            in {"accesstoken", "refreshtoken", "apikey", "authorization"}
            else safe(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [safe(item) for item in value]
    if isinstance(value, str):
        for key, secret in os.environ.items():
            if key.endswith(("_API_KEY", "_AUTH_TOKEN", "_OAUTH_TOKEN")) and secret:
                value = value.replace(secret, "<REDACTED_AUTH>")
        return re.sub(
            r"(?:sk-ant-|sk-proj-|sk-)[A-Za-z0-9_-]{12,}|Bearer\s+[^\s\"']+",
            "<REDACTED_AUTH>",
            value,
        )
    return value


async def run(args):
    if args.sdk_path:
        site.addsitedir(str(args.sdk_path))
    from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient
    from claude_agent_sdk.types import (
        HookMatcher,
        PermissionResultAllow,
        PermissionResultDeny,
    )

    project = args.project.resolve(strict=True)
    target = project / "native-fault-target.txt"
    if target.exists():
        raise RuntimeError("Use a fresh fixture: fault target already exists")
    args.evidence.mkdir(parents=True, exist_ok=False)
    settings = project / ".claude/settings.local.json"
    original = settings.read_bytes()
    (args.evidence / "settings-before.json").write_bytes(original)
    value = json.loads(original)
    found = False
    for entry in value.get("hooks", {}).get("PreToolUse", []):
        if entry.get("matcher") != "Edit|Write|NotebookEdit":
            continue
        for handler in entry["hooks"]:
            if handler.get("onFailure") != "block":
                raise RuntimeError(
                    "Installed native handler does not declare failure blocking"
                )
            found = True
            if args.fault == "missing":
                handler["command"] = str(project / "missing-hook-interpreter")
            elif args.fault == "crash":
                handler["args"] = [
                    "-I",
                    "-c",
                    "raise RuntimeError('synthetic hook import crash')",
                ]
            elif args.fault == "timeout":
                handler["args"] = ["-I", "-c", "import time; time.sleep(30)"]
                handler["timeout"] = 1
    if not found:
        raise RuntimeError("Prepared fixture has no enforcing native edit hook")
    if args.fault != "positive":
        settings.write_text(json.dumps(value, indent=2) + "\n")
    events = []

    async def approve(tool, arguments, context):
        allowed = tool == "Write" and arguments == {
            "file_path": str(target),
            "content": "native fault probe\n",
        }
        if args.fault == "positive":
            allowed = tool in {"Read", "Write", "Edit"} and arguments.get(
                "file_path"
            ) == str(target)
            if tool in {
                "mcp__daem0nmcp__session_brief",
                "mcp__daem0nmcp__edit_preflight",
                "mcp__daem0nmcp__daem0n_tool_call",
            }:
                allowed = arguments.get("workspace_id") == args.workspace_id
                if tool.endswith("daem0n_tool_call"):
                    allowed = (
                        allowed
                        and arguments.get("tool") == "memory_capture_list"
                        and arguments.get("arguments") == {}
                    )
        return (
            PermissionResultAllow(updated_input=arguments)
            if allowed
            else PermissionResultDeny(message="Outside exact disposable fault probe")
        )

    async def noop(_input, _id, _context):
        return {"continue_": True}

    env = dict(os.environ)
    if args.account_profile:
        env["CLAUDE_CONFIG_DIR"] = str(args.account_profile)
        env.pop("CLAUDE_CODE_OAUTH_TOKEN", None)
    prompt = (
        "Run exactly one native hook failure probe. Call native Write ONCE with EXACT "
        + json.dumps({"file_path": str(target), "content": "native fault probe\n"})
        + ". If blocked, stop immediately and report the actual native error. Do not retry, use MCP, shell, subagents or any other file/config edit."
    )
    if args.fault == "positive":
        if not re.fullmatch(r"ws_[0-9a-f]{24}", args.workspace_id or ""):
            raise RuntimeError(
                "Positive probe requires the prepared opaque --workspace-id"
            )
        write = {"file_path": str(target), "content": "native before\n"}
        edit = {
            "file_path": str(target),
            "old_string": "native before",
            "new_string": "native after",
        }
        invalid = {**edit, "old_string": "ABSENT_NATIVE_PROBE_SENTINEL"}
        prompt = (
            f"Run bounded native edit acceptance, workspace_id={args.workspace_id}. Call session_brief first. "
            f"Call native Write with EXACT {json.dumps(write)}. On Daem0n denial call real MCP edit_preflight using the returned request and truthful description, then retry the IDENTICAL Write. "
            "List memory_capture_list through daem0n_tool_call(workspace_id,tool='memory_capture_list',arguments={}); expect one candidate. "
            f"Read only {target}, then call native Edit EXACT {json.dumps(edit)}. Again obtain real edit_preflight after denial and retry unchanged, expecting success. "
            f"Then call native Edit EXACT {json.dumps(invalid)} ONCE; it must fail because old_string is absent. Do not fix arguments or try to make it succeed. "
            "List memory_capture_list again; expect exactly two successful native edit candidates and no new candidate for the failed edit. Stop. "
            "These expected denials and final native failure are the planned scenario, so complete all specified steps. No shell, scripts, subagents, other files/config edits or capture promotion."
        )
    options = ClaudeAgentOptions(
        cli_path=str(args.cli),
        cwd=str(project),
        env=env,
        model="haiku",
        effort="low",
        max_budget_usd=1,
        permission_mode="default",
        can_use_tool=approve,
        tools=["Read", "Write", "Edit"] if args.fault == "positive" else ["Write"],
        setting_sources=["user", "project", "local"],
        include_hook_events=True,
        mcp_servers=str(project / ".mcp.json"),
        strict_mcp_config=True,
        hooks={"PreToolUse": [HookMatcher(matcher=None, hooks=[noop])]},
        extra_args={
            "no-session-persistence": None,
            "disable-slash-commands": None,
            "permission-prompts": "host",
        },
    )
    failure = None
    try:
        async with asyncio.timeout(180):
            async with ClaudeSDKClient(options=options) as client:
                readiness = []
                for _ in range(120):
                    status = await client.get_mcp_status()
                    readiness.append(status)
                    (args.evidence / "mcp-readiness.json").write_text(
                        json.dumps(safe(readiness), indent=2)
                    )
                    if any(
                        server.get("name") == "daem0nmcp"
                        and server.get("status") == "connected"
                        for server in status.get("mcpServers", [])
                    ):
                        break
                    await asyncio.sleep(0.5)
                else:
                    raise RuntimeError("Native project MCP did not become connected")
                await client.query(prompt)
                with (args.evidence / "events.jsonl").open("w") as log:
                    async for item in client.receive_response():
                        row = (
                            dataclasses.asdict(item)
                            if dataclasses.is_dataclass(item)
                            else {"message": str(item)}
                        )
                        row["sdk_message_type"] = type(item).__name__
                        row = safe(row)
                        events.append(row)
                        log.write(json.dumps(row, default=str) + "\n")
                        log.flush()
    except Exception as exc:
        failure = str(safe(str(exc)))
    results = [
        block
        for row in events
        for block in row.get("content", [])
        if isinstance(block, dict) and "tool_use_id" in block
    ]
    raw = "\n".join(str(block.get("content")) for block in results)
    checks = {
        "native_write_result_observed": bool(results),
        "all_native_results_denied": bool(results)
        and all(block.get("is_error") is True for block in results),
        "target_absent": not target.exists(),
        "native_onfailure_block": "onFailure" in raw and "block" in raw,
    }
    if args.fault == "positive":
        calls = {}
        observed = []
        for row in events:
            for block in row.get("content", []):
                if not isinstance(block, dict):
                    continue
                if "id" in block and "name" in block and "input" in block:
                    calls[block["id"]] = block
                elif "tool_use_id" in block:
                    observed.append(
                        {
                            "tool": calls.get(block["tool_use_id"], {}).get("name"),
                            "error": block.get("is_error") is True,
                            "result": block.get("content"),
                        }
                    )
        (args.evidence / "native-receipts.json").write_text(
            json.dumps(observed, indent=2)
        )
        writes = [row for row in observed if row["tool"] == "Write"]
        edits = [row for row in observed if row["tool"] == "Edit"]
        preflights = [
            row
            for row in observed
            if str(row["tool"]).endswith("edit_preflight") and not row["error"]
        ]
        database = project / ".daem0nmcp/storage/daem0nmcp.db"
        count = None
        if database.is_file():
            with sqlite3.connect(
                database.as_uri() + "?mode=ro", uri=True
            ) as connection:
                count = connection.execute(
                    "SELECT COUNT(*) FROM memory_capture_candidates"
                ).fetchone()[0]
        checks = {
            "first_write_denied": bool(writes) and writes[0]["error"],
            "write_succeeded": any(not row["error"] for row in writes),
            "first_edit_denied": bool(edits) and edits[0]["error"],
            "edit_succeeded": any(not row["error"] for row in edits),
            "two_real_preflights": len(preflights) >= 2,
            "failed_native_edit": bool(edits) and edits[-1]["error"],
            "two_successful_captures_only": count == 2,
            "target_content": target.is_file()
            and target.read_text() == "native after\n",
        }
    summary = {
        "fault": args.fault,
        "status": "passed" if all(checks.values()) and not failure else "failed",
        "checks": checks,
        "driver_error": failure,
        "project": str(project),
        "cli": str(args.cli),
    }
    (args.evidence / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--project", type=Path, required=True)
    parser.add_argument("--cli", type=Path, required=True)
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument("--sdk-path", type=Path)
    parser.add_argument("--account-profile", type=Path)
    parser.add_argument("--workspace-id")
    parser.add_argument(
        "--fault", choices=("positive", "missing", "crash", "timeout"), required=True
    )
    asyncio.run(run(parser.parse_args()))
