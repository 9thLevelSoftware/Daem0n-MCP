"""Prepare a real MCP workspace and launch the pinned MCPJam Apps host.

UI observations must be collected from MCPJam itself; this script does not
replace its renderer or infer rendering success from protocol responses.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import secrets
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from native_agy_loading_acceptance import isolated_environment, sanitize, write_json

from daem0nmcp.claude_hooks.install import module_launch_args
from daem0nmcp.protected_files import write_new_owner_only_file
from tests.api_v7.process_client import initialize_workspaces, process_client

OVERRIDES = {
    "DAEM0NMCP_APPS_ENABLED": "true",
    "DAEM0NMCP_GRAPH_ENABLED": "true",
    "DAEM0NMCP_TOOL_SURFACE": "full",
    "DAEM0NMCP_LOCAL_ENABLED": "false",
    "DAEM0NMCP_MODELS_LOCAL_ENABLED": "false",
    "DAEM0NMCP_LATE_INTERACTION_ENABLED": "false",
    "DAEM0NMCP_DREAM_ENABLED": "false",
}


async def prepare(args):
    root = args.output.resolve()
    root.mkdir(parents=True, exist_ok=False)
    project = root / "workspace"
    project.mkdir()
    [workspace] = await initialize_workspaces((project,))
    scope = {"workspace_id": workspace.workspace_id}
    receipts = root / "seed-tool-responses.jsonl"
    with (root / "seed-server.stderr.log").open("w", encoding="utf-8") as log:
        async with process_client(
            project,
            "stdio",
            python=str(args.python.resolve()),
            environment_overrides=OVERRIDES,
        ) as session:

            async def call(name, arguments):
                result = await session.call_tool(name, arguments)
                response = result.model_dump(
                    mode="json", by_alias=True, exclude_none=True
                )
                with receipts.open("a", encoding="utf-8") as out:
                    out.write(
                        json.dumps(
                            {"name": name, "arguments": arguments, "result": response}
                        )
                        + "\n"
                    )
                envelope = result.structuredContent
                if not envelope:
                    raise RuntimeError("Native response has no structured envelope")
                if (
                    not envelope["ok"]
                    and envelope["error"]["code"] == "COUNSEL_REQUIRED"
                ):
                    remedy = envelope["error"]["remedy"]
                    return await call(remedy["tool"], remedy["arguments"])
                if not envelope["ok"]:
                    raise RuntimeError(json.dumps(envelope["error"]))
                return envelope["data"]

            await call("session_brief", scope)
            records = []
            for index in range(2):
                result = await call(
                    "memory_store",
                    {
                        **scope,
                        "record_type": "decision",
                        "content": f"Native MCPJam acceptance decision {index}: preserve the test project and its linked memory.",
                        "idempotency_key": f"native-apps-record-{index}",
                    },
                )
                records.append(result["record"]["record_id"])
            await call(
                "memory_link",
                {
                    **scope,
                    "source_record_id": records[0],
                    "target_record_id": records[1],
                    "relationship_type": "related_to",
                    "idempotency_key": "native-apps-link",
                },
            )
            await call(
                "community_rebuild",
                {
                    **scope,
                    "min_community_size": 2,
                    "resolution": 1.0,
                    "idempotency_key": "native-apps-community",
                },
            )
            for _ in range(60):
                found = await call(
                    "memory_recall",
                    {**scope, "query": "Native MCPJam acceptance", "limit": 5},
                )
                if len(found["items"]) >= 2:
                    break
                await asyncio.sleep(0.25)
            else:
                raise RuntimeError("Seed memories did not become searchable")
            resources = await session.list_resources()
            templates = await session.list_resource_templates()
            write_json(
                root / "seed-resource-inventory.json",
                {
                    "resources": resources.model_dump(mode="json"),
                    "templates": templates.model_dump(mode="json"),
                },
            )
            # Live execution is blocked without an existing service credential;
            # this records actual disabled capability behavior, not a live pass.
            disabled = await session.call_tool(
                "sandbox_execute_python",
                {**scope, "code": "print(1 + 1)", "timeout_seconds": 5},
            )
            write_json(
                root / "e2b-initial-response.json",
                disabled.model_dump(mode="json", by_alias=True),
            )
            if disabled.structuredContent["error"]["code"] == "COUNSEL_REQUIRED":
                remedy = disabled.structuredContent["error"]["remedy"]
                disabled = await session.call_tool(remedy["tool"], remedy["arguments"])
            assert disabled.structuredContent["error"]["code"] == "CAPABILITY_DISABLED"
            write_json(
                root / "e2b-disabled-response.json",
                disabled.model_dump(mode="json", by_alias=True),
            )
            log.write("Seed calls and returned results retained separately.\n")
    definition = {
        "command": str(args.python.resolve()),
        "args": ["-E", "-u", "-X", "utf8", *module_launch_args("daem0nmcp.server")],
        "cwd": str(project),
        "env": {
            **OVERRIDES,
            "DAEM0NMCP_PROJECT_ROOT": str(project),
            "DAEM0NMCP_PROFILE": "core",
        },
    }
    write_json(root / "mcp.json", {"mcpServers": {"daem0nmcp": definition}})
    write_json(
        root / "scenario.json",
        {
            "workspace_id": workspace.workspace_id,
            "records": records,
            "tools": {
                "search": {
                    "name": "memory_recall",
                    "arguments": {
                        **scope,
                        "query": "Native MCPJam acceptance",
                        "limit": 5,
                    },
                },
                "briefing": {"name": "session_brief", "arguments": scope},
                "covenant": {"name": "covenant_status", "arguments": scope},
                "community": {"name": "community_list", "arguments": scope},
                "graph": {
                    "name": "knowledge_graph_get",
                    "arguments": {
                        **scope,
                        "record_ids": records,
                        "include_orphans": True,
                        "max_nodes": 10,
                    },
                },
            },
        },
    )
    print(
        json.dumps(
            {
                "prepared": str(root),
                "workspace_id": workspace.workspace_id,
                "records": records,
            }
        )
    )


async def optional(args):
    import importlib.metadata

    import tiktoken

    root = args.output.resolve(strict=True)
    scope = {
        "workspace_id": json.loads((root / "scenario.json").read_text())["workspace_id"]
    }
    async with process_client(
        root / "workspace",
        "stdio",
        python=str(args.python.resolve()),
        environment_overrides=OVERRIDES,
    ) as session:
        await session.call_tool("session_brief", scope)
        result = await session.call_tool(
            "sandbox_execute_python",
            {**scope, "code": "print(1 + 1)", "timeout_seconds": 5},
        )
        write_json(
            root / "e2b-optional-initial.json",
            result.model_dump(mode="json", by_alias=True),
        )
        if result.structuredContent["error"]["code"] == "COUNSEL_REQUIRED":
            remedy = result.structuredContent["error"]["remedy"]
            result = await session.call_tool(remedy["tool"], remedy["arguments"])
        write_json(
            root / "e2b-optional-disabled.json",
            result.model_dump(mode="json", by_alias=True),
        )
        assert result.structuredContent["error"]["code"] == "CAPABILITY_DISABLED"
        capability = result.structuredContent["meta"]["capability_states"]
        assert any(c.get("reason_code") == "E2B_API_KEY_MISSING" for c in capability)
    encoding = tiktoken.get_encoding("cl100k_base")
    sample = "Daem0n native acceptance token counting."
    tokens = encoding.encode(sample)
    assert tokens and encoding.decode(tokens) == sample
    write_json(
        root / "optional-capabilities.json",
        {
            "e2b": {
                "live_status": "blocked",
                "reason": "E2B_API_KEY missing from Windows Process/User/Machine scopes",
                "disabled_response_verified": True,
                "live_sandbox_created": False,
            },
            "hosted_embeddings": {
                "status": "unsupported",
                "reason": "No hosted embedding encoder adapter; models-hosted advertises token support",
            },
            "hosted_model_token_counting": {
                "status": "passed",
                "package": "tiktoken",
                "version": importlib.metadata.version("tiktoken"),
                "encoding": "cl100k_base",
                "token_count": len(tokens),
                "roundtrip": True,
            },
        },
    )
    print(
        json.dumps(
            {
                "e2b": "blocked; disabled response verified",
                "hosted_embeddings": "unsupported",
                "token_counting": "passed",
            }
        )
    )


def serve(args):
    root = args.output.resolve(strict=True)
    home = root / "inspector-home"
    home.mkdir(exist_ok=True)
    env = isolated_environment(home)
    token = secrets.token_urlsafe(32)
    env.update(
        MCPJAM_SESSION_TOKEN=token,
        MCPJAM_LOCAL_COMPUTER_ENABLED="false",
        MCPJAM_LOCAL_BROWSER_ENABLED="false",
        DO_NOT_TRACK="1",
    )
    # The launcher opens its own authenticated local tab. Keep the control link
    # out of public evidence; redact it from captured server output.
    write_new_owner_only_file(root / "inspector-session-token", token.encode())
    command = [
        "node",
        str(args.inspector.resolve()),
        "--config",
        str(root / "mcp.json"),
        "--server",
        "daem0nmcp",
        "--port",
        str(args.port),
    ]
    write_json(
        root / "inspector-launch.json",
        {
            "command": command,
            "port": args.port,
            "private_profile": str(home),
            "inspector_version": "3.21.0",
        },
    )
    proc = subprocess.Popen(
        command,
        cwd=root,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    write_json(root / "inspector-process.json", {"pid": proc.pid})
    print(json.dumps({"pid": proc.pid, "port": args.port}), flush=True)
    with (root / "inspector.log").open("w", encoding="utf-8") as log:
        for line in proc.stdout:
            log.write(sanitize(line.replace(token, "<LOCAL_INSPECTOR_TOKEN>")))
            log.flush()
    return proc.wait()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("prepare", "serve", "optional"))
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--python", type=Path, default=Path(sys.executable))
    parser.add_argument("--inspector", type=Path)
    parser.add_argument("--port", type=int, default=6275)
    options = parser.parse_args()
    if options.action == "prepare":
        asyncio.run(prepare(options))
    elif options.action == "optional":
        asyncio.run(optional(options))
    else:
        sys.exit(serve(options))
