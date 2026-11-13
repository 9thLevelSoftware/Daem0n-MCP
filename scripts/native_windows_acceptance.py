"""Prepare and exercise independent real-source Windows native-client fixtures.

Every run uses a new evidence attempt. No original client configuration or whole
credential store is copied. API keys and access-only OAuth values stay in memory.
"""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import hashlib
import json
import os
import re
import secrets
import shutil
import site
import subprocess
import time
from pathlib import Path

from native_acceptance_drivers import (
    redact,
    retain_approval_denials,
    run_codex,
    run_stream,
    write_json,
)
from native_acceptance_fixtures import export_project
from native_acceptance_scenarios import (
    OPERATIONS,
    STAGES,
    instruction_evidence,
    native_receipts,
    scenario_prompt,
    validate_receipts,
)

REPOSITORY = Path(__file__).resolve().parents[1]
DEFAULT_BASE = Path("D:/Daem0n-Native-Acceptance-20261112/windows")
LEGACY = REPOSITORY / ".test_tmp/multi-cli-20261112/acceptance"
HOSTS = ("claude", "codex", "agy", "opencode", "omp", "pi")
VERSIONS = {
    "codex": "0.167.0",
    "agy": "1.4.7",
    "opencode": "1.18.21",
    "omp": "18.16.0",
    "pi": "1.0.0",
}
MODELS = {
    "claude": "haiku",
    "codex": "gpt-6.1-sol",
    "agy": "gemini-3.8-flash-low",
    "opencode": "minimax-coding-plan/MiniMax-M3",
    "omp": "minimax/MiniMax-M2.7",
    "pi": "MiniMax-M2.7",
}
MODES = {
    "claude": "enforce",
    "opencode": "enforce",
    "codex": "remind",
    "agy": "remind",
    "omp": "remind",
    "pi": "remind",
}


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None


def original_hashes():
    home = Path.home()
    names = (
        ".codex/config.toml",
        ".codex/auth.json",
        ".codex/hooks.json",
        ".claude/settings.json",
        ".claude/.credentials.json",
        ".claude.json",
        ".gemini/config/mcp_config.json",
        ".gemini/config/hooks.json",
        ".gemini/antigravity-cli/settings.json",
        ".gemini/antigravity/app_storage.json",
        ".local/share/opencode/auth.json",
        ".omp/agent/agent.db",
        ".pi/agent/auth.json",
    )
    return {str(home / name): digest(home / name) for name in names}


def environment(lane, host):
    home = lane / "private/home"
    home.mkdir(parents=True, exist_ok=True)
    allowed = {
        "PATH",
        "SYSTEMROOT",
        "WINDIR",
        "COMSPEC",
        "PATHEXT",
        "TEMP",
        "TMP",
        "PROCESSOR_ARCHITECTURE",
        "NUMBER_OF_PROCESSORS",
        "PROGRAMFILES",
        "PROGRAMFILES(X86)",
        "PROGRAMDATA",
        "APPDATA",
        "LOCALAPPDATA",
    }
    env = {k: v for k, v in os.environ.items() if k.upper() in allowed}
    env.update(
        HOME=str(home),
        USERPROFILE=str(home),
        APPDATA=str(home / "AppData/Roaming"),
        LOCALAPPDATA=str(home / "AppData/Local"),
        XDG_CONFIG_HOME=str(home / ".config"),
        XDG_DATA_HOME=str(home / ".local/share"),
        XDG_STATE_HOME=str(home / ".local/state"),
        XDG_CACHE_HOME=str(home / ".cache"),
        CODEX_HOME=str(home / ".codex"),
        CLAUDE_CONFIG_DIR=str(home / ".claude"),
        PYTHONUTF8="1",
        NO_COLOR="1",
        DAEM0NMCP_LOCAL_ENABLED="false",
        DAEM0NMCP_MODELS_LOCAL_ENABLED="false",
        DAEM0NMCP_APPS_ENABLED="true",
        DAEM0NMCP_LATE_INTERACTION_ENABLED="false",
        CLAUDE_CODE_DISABLE_AUTO_MEMORY="1",
        CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC="1",
    )
    for directory in (
        home / "AppData/Local",
        home / "AppData/Roaming",
        home / "parser-cache",
    ):
        directory.mkdir(parents=True, exist_ok=True)
    env["TREE_SITTER_LANGUAGE_PACK_CACHE_DIR"] = str(home / "parser-cache")
    if host in {"omp", "pi"}:
        env["PI_CODING_AGENT_DIR"] = str(
            home / (".omp/agent" if host == "omp" else ".pi/agent")
        )
    return env


def binary(host):
    home = Path.home()
    candidates = {
        "agy": home / "AppData/Local/agy/bin/agy.exe",
        "omp": home / "AppData/Local/omp/omp.exe",
        "codex": home / "AppData/Roaming/npm/codex.cmd",
        "opencode": home / "AppData/Roaming/npm/opencode.cmd",
        "pi": home / "AppData/Roaming/npm/pi.cmd",
    }
    path = candidates.get(host)
    if path is not None and path.is_file():
        return str(path)
    return shutil.which(host)


def backend_command(python, arguments, cwd, env):
    result = subprocess.run(
        [str(python), "-I", *arguments],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
    )
    if result.returncode:
        raise RuntimeError(
            "Backend preparation failed: " + redact(result.stderr[-3000:])
        )
    return result.stdout


def runtime_provenance(python, lane, env, attempt, *, output=None):
    code = (
        "import hashlib,json,sys,importlib.metadata as m; from pathlib import Path; "
        "import daem0nmcp; root=Path(daem0nmcp.__file__).parent; "
        "print(json.dumps({'executable':sys.executable,'package':str(root),"
        "'version':m.version('daem0nmcp'),'files':{p.relative_to(root).as_posix():"
        "hashlib.sha256(p.read_bytes()).hexdigest() for p in root.rglob('*.py')}}))"
    )
    result = json.loads(backend_command(python, ["-c", code], lane, env))
    write_json(
        output or lane / "evidence" / (attempt + ".backend-provenance.json"), result
    )


def config_paths(project):
    names = (
        "AGENTS.md",
        "CLAUDE.md",
        "EXISTING_POLICY.md",
        ".mcp.json",
        ".claude/settings.local.json",
        ".codex/config.toml",
        ".codex/hooks.json",
        ".agents/mcp_config.json",
        ".agents/hooks.json",
        "opencode.jsonc",
        ".opencode/daem0n-host.json",
        ".opencode/plugins/daem0n.ts",
        ".omp/mcp.json",
        ".pi/mcp.json",
        ".omp/extensions/daem0n.ts",
        ".pi/extensions/daem0n.ts",
    )
    return {
        name: digest(project / name) for name in names if (project / name).is_file()
    }


def prepare(args):
    lane = args.base / args.host / args.source
    if lane.resolve().is_relative_to(REPOSITORY):
        raise ValueError(
            "Final native fixture roots must be outside repository ancestry; preserve earlier provisional fixtures"
        )
    project = lane / "projects" / args.host
    repository = REPOSITORY if args.source == "daem0n" else args.legion
    manifest = export_project(args.source, repository, project)
    (lane / "evidence").mkdir(parents=True)
    before = original_hashes()
    env = environment(lane, args.host)
    native = args.binary or binary(args.host)
    if not native:
        raise RuntimeError("Native client executable not installed")
    version = subprocess.run(
        [native, "--version"],
        cwd=lane,
        env=env,
        capture_output=True,
        text=True,
        timeout=45,
    )
    match = re.search(r"\d+\.\d+\.\d+", version.stdout)
    if version.returncode or match is None:
        raise RuntimeError("Native version probe did not establish a supported version")
    observed_version = match.group(0)
    subprocess.run(
        ["git", "-c", "init.defaultBranch=main", "init", "--quiet", str(project)],
        check=True,
        capture_output=True,
    )
    sentinel = f"native-followup-preserve-{args.host}-{args.source}-20261112"
    marker = "native-context-" + secrets.token_hex(16)
    manifest["instruction_sentinel"] = marker
    (project / "AGENTS.md").write_text(
        "# Existing project policy\n\nPreserve this policy: "
        + sentinel
        + ".\n"
        + (
            "\nProject acceptance marker: " + marker + "\n"
            if args.host != "claude"
            else ""
        ),
        encoding="utf-8",
    )
    (project / "EXISTING_POLICY.md").write_text(sentinel + "\n", encoding="utf-8")
    sentinel_server = args.base / "sentinel_server.py"
    if not sentinel_server.exists():
        sentinel_server.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(LEGACY / "sentinel_server.py", sentinel_server)
    definition = {
        "command": str(args.backend_python),
        "args": ["-I", str(sentinel_server)],
        "cwd": str(project),
    }
    if args.host == "codex":
        path = project / ".codex/config.toml"
        path.parent.mkdir()
        path.write_text(
            f"# {sentinel}\n[mcp_servers.acceptance_sentinel]\ncommand = {json.dumps(str(args.backend_python))}\nargs = {json.dumps(definition['args'])}\ncwd = {json.dumps(str(project))}\n",
            encoding="utf-8",
        )
    elif args.host == "opencode":
        (project / "opencode.jsonc").write_text(
            "// "
            + sentinel
            + "\n"
            + json.dumps(
                {
                    "instructions": ["EXISTING_POLICY.md"],
                    "mcp": {
                        "acceptance_sentinel": {
                            "type": "local",
                            "command": [str(args.backend_python), *definition["args"]],
                            "enabled": True,
                        }
                    },
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
    elif args.host != "claude":
        directory = {"agy": ".agents", "omp": ".omp", "pi": ".pi"}[args.host]
        write_json(
            project
            / directory
            / ("mcp_config.json" if args.host == "agy" else "mcp.json"),
            {"mcpServers": {"acceptance_sentinel": definition}},
        )
    installed = {}
    automation = args.automation or MODES[args.host]
    if args.host == "claude":
        payload = json.loads(
            backend_command(
                args.backend_python,
                [
                    "-c",
                    "import json; from daem0nmcp.claude_hooks.install import module_launch_args; from daem0nmcp.client_install import _COVENANT; print(json.dumps({'args':module_launch_args('daem0nmcp.server'),'covenant':_COVENANT}))",
                ],
                lane,
                env,
            )
        )
        managed = {
            "command": str(args.backend_python),
            "args": ["-E", "-u", "-X", "utf8", *payload["args"]],
            "cwd": str(project),
            "env": {
                "DAEM0NMCP_PROJECT_ROOT": str(project),
                "DAEM0NMCP_WORKSPACE_ROOTS": json.dumps([str(project)]),
                "DAEM0NMCP_COVENANT_MODE": "guided",
                "DAEM0NMCP_TOOL_SURFACE": "core",
            },
        }
        write_json(
            project / ".mcp.json",
            {"mcpServers": {"acceptance_sentinel": definition, "daem0nmcp": managed}},
        )
        (project / "CLAUDE.md").write_text(
            "# Existing policy\n\n"
            + sentinel
            + "\n\n"
            + payload["covenant"]
            + "\n"
            + "\nProject acceptance marker: "
            + marker
            + "\n",
            encoding="utf-8",
        )
        output = backend_command(
            args.backend_python,
            [
                "-m",
                "daem0nmcp.claude_hooks.install",
                "--project-path",
                str(project),
                "--automation",
                automation,
                "--client-version",
                observed_version,
            ],
            lane,
            env,
        )
        installed = {
            "route": "native project MCP config + maintained Claude hooks installer",
            "output": output,
        }
        settings = project / ".claude/settings.local.json"
        value = json.loads(settings.read_text())
        value["enabledMcpjsonServers"] = ["daem0nmcp", "acceptance_sentinel"]
        write_json(settings, value)
        initial = config_paths(project)
        backend_command(
            args.backend_python,
            [
                "-m",
                "daem0nmcp.claude_hooks.install",
                "--project-path",
                str(project),
                "--automation",
                automation,
                "--client-version",
                observed_version,
            ],
            lane,
            env,
        )
        if config_paths(project) != initial:
            raise RuntimeError("Claude hook reinstall changed reviewed config bytes")
    else:
        common = [
            "-m",
            "daem0nmcp.cli",
            "--json",
            "--project-path",
            str(project),
            "install-client",
            args.host,
            "--client-version",
            observed_version,
            "--automation",
            automation,
        ]
        initial = config_paths(project)
        installed["preview"] = json.loads(
            backend_command(args.backend_python, [*common, "--dry-run"], lane, env)
        )
        if config_paths(project) != initial:
            raise RuntimeError("Installer preview changed project config")
        installed["apply"] = json.loads(
            backend_command(args.backend_python, common, lane, env)
        )
        initial = config_paths(project)
        installed["repeat"] = json.loads(
            backend_command(args.backend_python, common, lane, env)
        )
        if config_paths(project) != initial:
            raise RuntimeError("Installer repeat changed project config bytes")
    workspace = json.loads(
        backend_command(
            args.backend_python,
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
    if args.host == "codex":
        home = Path(env["CODEX_HOME"])
        home.mkdir(parents=True, exist_ok=True)
        (home / "config.toml").write_text(
            'cli_auth_credentials_store = "ephemeral"\n'
            + f'[projects.{json.dumps(str(project))}]\ntrust_level = "trusted"\n',
            encoding="utf-8",
        )
    if args.host == "pi":
        trust = LEGACY / "pi-project-trust.mjs"
        source = trust.read_text(encoding="utf-8")
        source = source.replace(
            json.dumps(str(LEGACY / "projects/pi")), json.dumps(str(project))
        )
        source = source.replace(
            json.dumps(str(LEGACY / "evidence/pi/approved-project.json")),
            json.dumps(str(lane / "approved-project.json")),
        )
        source = source.replace(
            json.dumps(str(LEGACY / "evidence/pi/project-trust.jsonl")),
            json.dumps(str(lane / "project-trust.jsonl")),
        )
        (lane / "pi-project-trust.mjs").write_text(source, encoding="utf-8")
        write_json(
            lane / "approved-project.json",
            {"project": str(project), "fileHashes": config_paths(project)},
        )
    after = original_hashes()
    write_json(
        lane / "fixture.json",
        {
            "host": args.host,
            "project": str(project),
            "workspace_id": workspace,
            "manifest": manifest,
            "backend_python": str(args.backend_python),
            "binary": native,
            "automation": automation,
            "version_stdout": redact(version.stdout),
            "version_stderr": redact(version.stderr),
            "config_hashes": config_paths(project),
            "original_hashes": before,
            "original_hashes_after": after,
            "original_files_unchanged": before == after,
            "installation": installed,
            "state": "initial",
        },
    )
    if before != after:
        raise RuntimeError("Original user files changed during fixture preparation")
    print(
        json.dumps(
            {
                "host": args.host,
                "source": args.source,
                "workspace_id": workspace,
                "project": str(project),
                "files": manifest["file_count"],
            }
        ),
        flush=True,
    )


def api_key(host, provider):
    relative = (
        ".pi/agent/auth.json" if host == "pi" else ".local/share/opencode/auth.json"
    )
    record = json.loads((Path.home() / relative).read_text(encoding="utf-8-sig")).get(
        provider, {}
    )
    expected = "api_key" if host == "pi" else "api"
    if record.get("type") != expected or not isinstance(record.get("key"), str):
        raise RuntimeError("Selected existing API-key provider credential unavailable")
    return record["key"]


def existing_access_credentials(host):
    """Select access material in memory; never return refresh credentials."""
    if host == "claude":
        value = json.loads(
            (Path.home() / ".claude/.credentials.json").read_text(encoding="utf-8-sig")
        )
        token = value.get("claudeAiOauth", {}).get("accessToken")
        if not isinstance(token, str) or not token:
            raise RuntimeError("Existing Claude access token unavailable")
        return {"access_token": token}
    if host == "codex":
        value = json.loads(
            (Path.home() / ".codex/auth.json").read_text(encoding="utf-8-sig")
        )
        tokens = value.get("tokens", {})
        token, account = tokens.get("access_token"), tokens.get("account_id")
        if (
            not isinstance(token, str)
            or not token
            or not isinstance(account, str)
            or not account
        ):
            raise RuntimeError("Existing Codex access-only credentials unavailable")
        return {"access_token": token, "account_id": account}
    raise ValueError("No access-only credential route for this client")


async def claude_run(metadata, lane, stage, evidence, env, prior, model):
    site.addsitedir(str(REPOSITORY / ".test_tmp/acceptance-20261112-summon/host-sdk"))
    from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient
    from claude_agent_sdk.types import PermissionResultDeny

    account = Path("D:/Daem0n-Native-Acceptance-20261112/accounts/claude/.claude")
    if not account.is_dir():
        raise RuntimeError("Authorized native Claude account profile unavailable")
    env["CLAUDE_CONFIG_DIR"] = str(account)
    env.pop("CLAUDE_CODE_OAUTH_TOKEN", None)
    evidence.mkdir(parents=True, exist_ok=False)
    events = []

    async def deny(tool_name, input_data, context):
        return PermissionResultDeny(
            message="Only bounded native Daem0n MCP calls are authorized in this indexing stage"
        )

    options = ClaudeAgentOptions(
        cli_path=metadata["binary"],
        cwd=metadata["project"],
        env=env,
        model=model,
        effort="low",
        max_budget_usd=3,
        permission_mode="default",
        can_use_tool=deny,
        allowed_tools=["mcp__daem0nmcp__" + name for name in OPERATIONS],
        tools=[],
        setting_sources=["user", "project", "local"],
        include_hook_events=True,
        extra_args={
            "no-session-persistence": None,
            "disable-slash-commands": None,
            "permission-prompts": "host",
        },
    )
    prompt = scenario_prompt(
        "claude", stage, metadata["workspace_id"], metadata["manifest"]
    )
    write_json(
        evidence / "request.json",
        {
            "stage": stage,
            "model": model,
            "prompt": prompt,
            "auth": "User-authenticated owned native profile; normal native refresh, no auth file copy",
        },
    )

    started = time.monotonic()
    original_environment = dict(os.environ)
    os.environ.clear()
    os.environ.update(env)
    try:
        with (evidence / "events.jsonl").open("w", encoding="utf-8") as log:
            async with asyncio.timeout(900):
                async with ClaudeSDKClient(options=options) as client:
                    deadline = time.monotonic() + 120
                    while True:
                        status = await client.get_mcp_status()
                        daemon = next(
                            (
                                server
                                for server in status.get("mcpServers", [])
                                if server.get("name") == "daem0nmcp"
                            ),
                            {},
                        )
                        if daemon.get("status") == "connected":
                            break
                        if (
                            daemon.get("status") in {"failed", "needs-auth", "disabled"}
                            or time.monotonic() > deadline
                        ):
                            write_json(
                                evidence / "pre-turn-native-daemon-status.json",
                                redact(daemon),
                            )
                            raise RuntimeError(
                                "Native Claude Daem0n MCP connection unavailable before model turn"
                            )
                        await asyncio.sleep(0.2)
                    write_json(
                        evidence / "pre-turn-native-daemon-status.json", redact(daemon)
                    )
                    await client.query(prompt)
                    async for message in client.receive_response():
                        item = (
                            dataclasses.asdict(message)
                            if dataclasses.is_dataclass(message)
                            else {"message": str(message)}
                        )
                        item["sdk_message_type"] = type(message).__name__
                        item = redact(item)
                        events.append(item)
                        log.write(json.dumps(item, default=str) + "\n")
                        log.flush()
    finally:
        os.environ.clear()
        os.environ.update(original_environment)
    receipts = native_receipts(events, "claude")
    (evidence / "native-tool-receipts.jsonl").write_text(
        "".join(json.dumps(item) + "\n" for item in receipts), encoding="utf-8"
    )
    summary = {
        "host": "claude",
        "stage": stage,
        "elapsed_seconds": time.monotonic() - started,
        "validation": validate_receipts(
            receipts,
            stage,
            metadata["manifest"],
            prior,
            workspace_id=metadata["workspace_id"],
        ),
    }
    if stage == "initial":
        loading = instruction_evidence(events, "claude", metadata["manifest"])
        summary["instruction_loading"] = loading
        if loading["status"] != "not_requested":
            summary["validation"]["checks"]["preloaded_instruction_marker_reported"] = (
                loading["status"] == "passed"
            )
            if loading["status"] != "passed":
                summary["validation"]["status"] = "failed"
    write_json(evidence / "summary.json", summary)
    return summary


def attempt_paths(args, lane):
    """Allow repair evidence/baselines without modifying historical artifacts."""
    evidence = Path(
        getattr(args, "evidence_dir", None) or lane / "evidence" / args.attempt
    )
    baseline = Path(
        getattr(args, "baseline_path", None) or lane / "initial-validation.json"
    )
    return evidence, baseline


def admit_initial_baseline(stage, baseline):
    if stage == "initial" and baseline.exists():
        raise RuntimeError(
            "Preserve existing initial baseline; choose a new baseline path"
        )


def publish_initial_baseline(baseline, value):
    """An admitted initial turn cannot overwrite a concurrently published anchor."""
    serialized = json.dumps(value, indent=2) + "\n"
    baseline.parent.mkdir(parents=True, exist_ok=True)
    try:
        with baseline.open("x", encoding="utf-8", newline="\n") as output:
            output.write(serialized)
    except FileExistsError:
        raise RuntimeError(
            "Preserve existing initial baseline at publication"
        ) from None


def run(args):
    lane = args.base / args.host / args.source
    metadata = json.loads((lane / "fixture.json").read_text())
    evidence, baseline = attempt_paths(args, lane)
    provenance = evidence.with_name(evidence.name + ".backend-provenance.json")
    if evidence.exists() or provenance.exists():
        raise RuntimeError("Preserve existing native attempt and backend provenance")
    admit_initial_baseline(args.stage, baseline)
    env = environment(lane, args.host)
    project = Path(metadata["project"])
    if config_paths(project) != metadata["config_hashes"]:
        raise RuntimeError("Reviewed native project configuration changed")
    before = original_hashes()
    prior = json.loads(baseline.read_text()) if args.stage == "restart" else None
    model = args.model or MODELS[args.host]
    common = {
        "cwd": project,
        "env": env,
        "workspace_id": metadata["workspace_id"],
        "manifest": metadata["manifest"],
        "evidence": evidence,
        "stage": args.stage,
        "prior": prior,
    }
    native = metadata["binary"]
    runtime_provenance(
        metadata["backend_python"], lane, env, args.attempt, output=provenance
    )
    prompt = scenario_prompt(
        args.host, args.stage, metadata["workspace_id"], metadata["manifest"]
    )
    secrets = []
    if args.host == "codex":
        credentials = existing_access_credentials("codex")
        token, account = credentials["access_token"], credentials["account_id"]
        result = run_codex(
            argv=[native, "app-server"],
            model=model,
            access_token=token,
            account_id=account,
            **common,
        )
    elif args.host == "claude":
        result = asyncio.run(
            claude_run(metadata, lane, args.stage, evidence, env, prior, model)
        )
    else:
        if args.host == "agy":
            settings = Path(env["HOME"]) / ".gemini/antigravity-cli/settings.json"
            write_json(
                settings,
                {
                    "trustedWorkspaces": [str(project)],
                    "permissions": {
                        "allow": [f"mcp(daem0nmcp/{name})" for name in OPERATIONS],
                        "deny": [
                            "run_command(*)",
                            "write_to_file(*)",
                            "replace_file_content(*)",
                            "multi_replace_file_content(*)",
                            "read_url_content(*)",
                        ],
                    },
                },
            )
            argv = [
                native,
                "--print",
                prompt,
                "--output-format",
                "stream-json",
                "--model",
                model,
                "--effort",
                "low",
                "--print-timeout",
                "15m",
            ]
        elif args.host == "opencode":
            key = api_key("opencode", "minimax-coding-plan")
            secrets.append(key)
            env["MINIMAX_API_KEY"] = key
            env["OPENCODE_PERMISSION"] = json.dumps(
                {"*": "deny", **{"daem0nmcp_" + name: "allow" for name in OPERATIONS}}
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
                model,
                prompt,
            ]
        else:
            key = api_key("pi", "minimax")
            secrets.append(key)
            env["MINIMAX_API_KEY"] = key
            if args.host == "pi":
                argv = [
                    native,
                    *metadata.get("binary_arguments", []),
                    "--mode",
                    "json",
                    "--print",
                    "--no-session",
                    "--exclude-tools",
                    "bash,edit,write",
                    "--extension",
                    str(lane / "pi-project-trust.mjs"),
                    "--no-skills",
                    "--no-prompt-templates",
                    "--no-themes",
                    "--provider",
                    "minimax",
                    "--model",
                    model,
                    "--thinking",
                    "low",
                    prompt,
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
                    "3",
                    "--max-time",
                    "15m",
                    "--model",
                    model,
                ]
        result = run_stream(
            host=args.host,
            argv=argv,
            sensitive_values=secrets,
            omp_rpc=args.host == "omp",
            prompt_text=prompt,
            **common,
        )
    after = original_hashes()
    write_json(
        evidence / "preservation.json",
        {"before": before, "after": after, "original_files_unchanged": before == after},
    )
    if before != after:
        result["validation"]["status"] = "failed"
        result["validation"]["checks"]["original_files_unchanged"] = False
        write_json(evidence / "summary.json", result)
    if args.stage == "initial" and result["validation"]["status"] == "passed":
        publish_initial_baseline(baseline, result["validation"])
    print(
        json.dumps(
            {
                "host": args.host,
                "source": args.source,
                "attempt": args.attempt,
                **result["validation"],
            }
        ),
        flush=True,
    )
    return 0 if result["validation"]["status"] == "passed" else 1


def mutate(args, restore=False):
    lane = args.base / args.host / args.source
    metadata = json.loads((lane / "fixture.json").read_text())
    project = Path(metadata["project"])
    specification = metadata["manifest"]["scenario"]
    target = project / specification["mutation_path"]
    added = project / specification["added_path"]
    baseline = lane / "source-baseline.bin"
    if not restore:
        if baseline.exists() or added.exists():
            raise RuntimeError("Preserve prior mutation or unexpected source file")
        if (
            digest(target)
            != metadata["manifest"]["files"][specification["mutation_path"]]
        ):
            raise RuntimeError("Original source bytes changed")
        baseline.write_bytes(target.read_bytes())
        target.write_bytes(
            baseline.read_bytes() + specification["mutation_append"].encode()
        )
        added.write_text(specification["added_content"], encoding="utf-8")
    else:
        if (
            target.read_bytes()
            != baseline.read_bytes() + specification["mutation_append"].encode()
        ):
            raise RuntimeError("Changed mutation bytes; preserve source")
        if added.read_text(encoding="utf-8") != specification["added_content"]:
            raise RuntimeError("Changed added source; preserve it")
        target.write_bytes(baseline.read_bytes())
        added.unlink()
    write_json(
        lane / ("restore.json" if restore else "mutation.json"),
        {
            "relative_paths": [
                specification["mutation_path"],
                specification["added_path"],
            ],
            "subject_sha256": digest(target),
            "added_sha256": digest(added),
        },
    )


def collect(args):
    """Re-evaluate retained native events without repeating any model request."""
    lane = args.base / args.host / args.source
    metadata = json.loads((lane / "fixture.json").read_text())
    evidence, baseline = attempt_paths(args, lane)
    admit_initial_baseline(args.stage, baseline)
    output = evidence / "recollected-summary.json"
    if output.exists():
        raise RuntimeError("Preserve earlier recollection")
    events = [
        json.loads(line)
        for line in (evidence / "events.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    normalized = evidence / "native-tool-receipts.jsonl"
    observed = (
        [
            json.loads(line)
            for line in normalized.read_text(encoding="utf-8").splitlines()
        ]
        if normalized.is_file()
        else native_receipts(events, args.host)
    )
    prior = json.loads(baseline.read_text()) if args.stage == "restart" else None
    result = validate_receipts(
        observed,
        args.stage,
        metadata["manifest"],
        prior,
        workspace_id=metadata["workspace_id"],
    )
    loading = (
        instruction_evidence(events, args.host, metadata["manifest"])
        if args.stage == "initial"
        else {"status": "not_requested"}
    )
    if args.stage == "initial" and loading["status"] != "not_requested":
        result["checks"]["preloaded_instruction_marker_reported"] = (
            loading["status"] == "passed"
        )
    result["status"] = "passed" if all(result["checks"].values()) else "failed"
    retain_approval_denials(evidence, result)
    summary = {
        "validation": result,
        "instruction_loading": loading,
        "model_calls": 0,
        "reason": "Offline revalidation of preserved native receipts; no native operations repeated",
        "original_hashes_unchanged_since_preparation": original_hashes()
        == metadata["original_hashes"],
    }
    write_json(output, summary)
    if args.stage == "initial" and result["status"] == "passed":
        publish_initial_baseline(baseline, result)
    print(json.dumps(summary), flush=True)
    return 0 if result["status"] == "passed" else 1


def baseline(args):
    """Retain independently verified core IDs without changing a failed verdict."""
    lane = args.base / args.host / args.source
    source = lane / "evidence" / args.attempt / "summary.json"
    if not source.exists():
        source = source.with_name("recollected-summary.json")
    result = json.loads(source.read_text())["validation"]
    required = (
        "native_memory_store",
        "native_memory_recall",
        "stored_record_recalled",
        "guided_auto_brief",
        "native_outcome",
        "preloaded_instruction_marker_reported",
    )
    if not all(result["checks"].get(key) for key in required):
        raise RuntimeError("Core component evidence is incomplete")
    if not re.fullmatch(
        r"mem_[0-9a-f]{64}", result.get("record_id") or ""
    ) or not re.fullmatch(r"evt_[0-9a-f]{64}", result.get("event_id") or ""):
        raise RuntimeError("Core component identifiers are invalid")
    target = lane / "initial-validation.json"
    if target.exists():
        raise RuntimeError("Preserve existing initial baseline")
    write_json(
        target,
        {
            "status": "component_verified",
            "source_overall_status": result["status"],
            "record_id": result["record_id"],
            "event_id": result["event_id"],
            "source_attempt": args.attempt,
            "source_evidence": str(source),
            "checks": {key: result["checks"][key] for key in required},
        },
    )


def retarget(args):
    """Rebind only reviewed owned fixture config to a new immutable backend."""
    import tomlkit

    from daem0nmcp.client_install import _json_document, _set_json_value
    from daem0nmcp.verification_v7 import verify_v7

    lane = args.base / args.host / args.source
    metadata = json.loads((lane / "fixture.json").read_text())
    project = Path(metadata["project"])
    before = config_paths(project)
    if before != metadata["config_hashes"]:
        raise RuntimeError("Reviewed fixture configuration changed before retarget")
    evidence = lane / "evidence" / ("retarget-" + args.attempt)
    evidence.mkdir(parents=True, exist_ok=False)
    for relative in before:
        target = evidence / "before" / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(project / relative, target)
    storage = project / ".daem0nmcp/storage"
    roots_before = (
        verify_v7(storage, metadata["workspace_id"])["authority_roots"]
        if (storage / "active-db.json").is_file()
        else None
    )
    replacement = str(args.backend_python)
    if args.host == "codex":
        path = project / ".codex/config.toml"
        config = tomlkit.parse(path.read_text(encoding="utf-8"))
        config["mcp_servers"]["daem0nmcp"]["command"] = replacement
        path.write_bytes(tomlkit.dumps(config).encode())
    else:
        relative = {
            "claude": ".mcp.json",
            "agy": ".agents/mcp_config.json",
            "opencode": "opencode.jsonc",
            "omp": ".omp/mcp.json",
            "pi": ".pi/mcp.json",
        }[args.host]
        path = project / relative
        text = path.read_text(encoding="utf-8")
        namespace = "mcp" if args.host == "opencode" else "mcpServers"
        current = _json_document(text, comments=args.host == "opencode").value[
            namespace
        ]["daem0nmcp"]["command"]
        command = (
            [replacement, *current[1:]] if isinstance(current, list) else replacement
        )
        text = _set_json_value(
            text,
            (namespace, "daem0nmcp", "command"),
            command,
            comments=args.host == "opencode",
        )
        path.write_bytes(text.encode())
    env = environment(lane, args.host)
    match = re.search(r"\d+\.\d+\.\d+", metadata["version_stdout"])
    if match is None:
        raise RuntimeError("Recorded native version missing")
    arguments = (
        [
            "-m",
            "daem0nmcp.claude_hooks.install",
            "--project-path",
            str(project),
            "--automation",
            metadata["automation"],
            "--client-version",
            match.group(0),
        ]
        if args.host == "claude"
        else [
            "-m",
            "daem0nmcp.cli",
            "--json",
            "--project-path",
            str(project),
            "install-client",
            args.host,
            "--client-version",
            match.group(0),
            "--automation",
            metadata["automation"],
        ]
    )
    output = backend_command(args.backend_python, arguments, lane, env)
    after = config_paths(project)
    roots_after = (
        verify_v7(storage, metadata["workspace_id"])["authority_roots"]
        if (storage / "active-db.json").is_file()
        else None
    )
    if roots_before != roots_after:
        raise RuntimeError("Fixture authority changed during runtime retarget")
    write_json(
        evidence / "receipt.json",
        {
            "before": before,
            "after": after,
            "old_backend": metadata["backend_python"],
            "new_backend": replacement,
            "authority_roots_before": roots_before,
            "authority_roots_after": roots_after,
            "authority_unchanged": True,
            "installer_output": output,
        },
    )
    metadata["backend_python"] = replacement
    metadata["config_hashes"] = after
    write_json(lane / "fixture.json", metadata)
    if args.host == "pi":
        write_json(
            lane / "approved-project.json",
            {
                "project": str(project),
                "fileHashes": after,
                "authority": "Coordinator reviewed exact owned runtime retarget receipt",
            },
        )
    print(
        json.dumps(
            {
                "host": args.host,
                "source": args.source,
                "new_backend": replacement,
                "authority_unchanged": True,
            }
        ),
        flush=True,
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "action",
        choices=[
            "prepare",
            "run",
            "mutate",
            "restore",
            "collect",
            "baseline",
            "retarget",
        ],
    )
    parser.add_argument("--host", choices=HOSTS, required=True)
    parser.add_argument("--source", choices=["daem0n", "legion"], required=True)
    parser.add_argument("--base", type=Path, default=DEFAULT_BASE)
    parser.add_argument("--legion", type=Path, default=Path("D:/legion-cli"))
    parser.add_argument("--backend-python", type=Path)
    parser.add_argument("--binary")
    parser.add_argument("--automation", choices=["off", "remind", "enforce"])
    parser.add_argument("--stage", choices=STAGES, default="initial")
    parser.add_argument("--attempt", default="initial-1")
    parser.add_argument("--evidence-dir", type=Path)
    parser.add_argument("--baseline-path", type=Path)
    parser.add_argument("--model")
    args = parser.parse_args()
    args.base = args.base.absolute()
    if not all(c.isalnum() or c in "-_" for c in args.attempt):
        parser.error("Attempt must be a simple identifier")
    if args.action == "prepare":
        if args.backend_python is None:
            parser.error("--backend-python is required to prepare")
        args.backend_python = args.backend_python.resolve(strict=True)
        prepare(args)
    elif args.action == "run":
        return run(args)
    elif args.action == "collect":
        return collect(args)
    elif args.action == "baseline":
        baseline(args)
    elif args.action == "retarget":
        if args.backend_python is None:
            parser.error("--backend-python is required to retarget")
        args.backend_python = args.backend_python.resolve(strict=True)
        retarget(args)
    else:
        mutate(args, restore=args.action == "restore")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
