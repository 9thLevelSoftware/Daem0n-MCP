"""Native Linux/macOS fixture preparation and model acceptance orchestration.

Run this with the isolated backend interpreter on the native filesystem. Provider
API keys are accepted only from the launching process environment. Existing
Claude/agy account profiles stay in their explicitly selected test homes.
"""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

from native_acceptance_drivers import redact, run_codex, run_stream, write_json
from native_acceptance_scenarios import (
    OPERATIONS,
    STAGES,
    instruction_evidence,
    native_receipts,
    scenario_prompt,
    validate_receipts,
)

HOSTS = ("claude", "codex", "agy", "opencode", "omp", "pi")
VERSIONS = {
    "claude": "2.1.295",
    "codex": "0.167.0",
    "agy": "1.5.7",
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
    "codex": "remind",
    "agy": "remind",
    "opencode": "enforce",
    "omp": "remind",
    "pi": "remind",
}
CONFIGURATION_NAMES = (
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
)


def digest(path: Path) -> str | None:
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None


def config_hashes(project: Path) -> dict[str, str | None]:
    return {name: digest(project / name) for name in CONFIGURATION_NAMES}


def original_hashes() -> dict[str, str | None]:
    original = Path.home()
    names = (
        ".codex/config.toml",
        ".codex/auth.json",
        ".codex/hooks.json",
        ".claude.json",
        ".claude/settings.json",
        ".claude/.credentials.json",
        ".gemini/config/mcp_config.json",
        ".gemini/config/hooks.json",
        ".gemini/antigravity-cli/settings.json",
        ".gemini/antigravity/app_storage.json",
        ".config/opencode/opencode.json",
        ".local/share/opencode/auth.json",
        ".omp/agent/agent.db",
        ".pi/agent/auth.json",
    )
    return {str(original / name): digest(original / name) for name in names}


def bridge_root(lane: Path) -> Path:
    """Keep disposable Unix socket addresses within Darwin's 104-byte limit."""
    prefix = Path("/private/tmp" if platform.system() == "Darwin" else "/var/tmp")
    identity = hashlib.sha256(str(lane.resolve()).encode()).hexdigest()[:12]
    return prefix / f"d0n-a-{os.getuid()}" / identity


def environment(root: Path, host: str, source: str) -> dict[str, str]:
    if host in {"claude", "agy"}:
        home = root / "private" / f"{host}-account"
    else:
        home = root / "private" / host / source
    home.mkdir(parents=True, exist_ok=True, mode=0o700)
    original = Path.home()
    result = {
        "PATH": ":".join(
            [
                str(root / "tools/bin"),
                "/opt/homebrew/bin",
                "/usr/local/bin",
                str(original / ".local/bin"),
                "/usr/bin",
                "/bin",
                "/usr/sbin",
                "/sbin",
            ]
        ),
        "HOME": str(home),
        "XDG_CONFIG_HOME": str(home / ".config"),
        "XDG_DATA_HOME": str(home / ".local/share"),
        "XDG_CACHE_HOME": str(home / ".cache"),
        "XDG_STATE_HOME": str(home / ".local/state"),
        "CODEX_HOME": str(home / ".codex"),
        "CLAUDE_CONFIG_DIR": str(home / ".claude"),
        "LANG": "en_US.UTF-8",
        "LC_ALL": "en_US.UTF-8",
        "NO_COLOR": "1",
        "PYTHONUTF8": "1",
        "DAEM0NMCP_LOCAL_ENABLED": "false",
        "DAEM0NMCP_MODELS_LOCAL_ENABLED": "false",
        "DAEM0NMCP_LATE_INTERACTION_ENABLED": "false",
        "DAEM0NMCP_APPS_ENABLED": "true",
        "CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1",
        "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
    }
    if host == "claude" and platform.system() == "Darwin":
        # macOS resolves the user's default Keychain through the real HOME.
        # Claude configuration and state remain under its explicit private root.
        result["HOME"] = str(original)
    for name in ("SSH_CONNECTION", "SSH_CLIENT", "SSH_TTY"):
        if name in os.environ:
            result[name] = os.environ[name]
    for name in ("USER", "LOGNAME", "SHELL", "TMPDIR"):
        if name in os.environ:
            result[name] = os.environ[name]
    if host in {"omp", "pi"}:
        result["PI_CODING_AGENT_DIR"] = str(
            home / (".omp/agent" if host == "omp" else ".pi/agent")
        )
    return result


def lane_path(root: Path, host: str, source: str, matrix: str = "final") -> Path:
    return root / "matrices" / matrix / host / source


def _sentinel_server(root: Path) -> Path:
    target = root / "sentinel-server.py"
    if target.exists():
        return target
    target.write_text(
        "import json,sys\n"
        "for line in sys.stdin:\n"
        " try: request=json.loads(line)\n"
        " except ValueError: continue\n"
        " if 'id' not in request: continue\n"
        " method=request.get('method')\n"
        " result=({'protocolVersion':request.get('params',{}).get('protocolVersion','2025-11-25'),"
        "'capabilities':{},'serverInfo':{'name':'acceptance-sentinel','version':'1'}} "
        "if method=='initialize' else {'tools':[]} if method=='tools/list' else {})\n"
        " print(json.dumps({'jsonrpc':'2.0','id':request['id'],'result':result}),flush=True)\n",
        encoding="utf-8",
    )
    return target


def _pi_trust(lane: Path, project: Path) -> None:
    write_json(
        lane / "approved-project.json",
        {"project": str(project), "fileHashes": config_hashes(project)},
    )
    source = r"""
import {realpathSync,readFileSync,appendFileSync,existsSync} from 'node:fs';
import {createHash} from 'node:crypto';
import {resolve} from 'node:path';
const project=PROJECT;
const approval=APPROVAL;
const receipt=RECEIPT;
export default function(pi) {
  pi.on('project_trust', async(event) => {
    let trusted='no';
    try {
      const value=JSON.parse(readFileSync(approval,'utf8'));
      const exact=realpathSync(event.cwd)===realpathSync(project)
        && value.project===project
        && Object.entries(value.fileHashes).every(([name,hash]) =>
          hash===null ? !existsSync(resolve(project,name))
            : createHash('sha256').update(readFileSync(resolve(project,name))).digest('hex')===hash);
      trusted=exact?'yes':'no';
    } catch (_) {}
    appendFileSync(receipt,JSON.stringify({event:'project_trust',cwd:event.cwd,trusted,persistentTrustWritten:false})+'\n');
    return {trusted};
  });
}
"""
    for name, value in (
        ("PROJECT", str(project)),
        ("APPROVAL", str(lane / "approved-project.json")),
        ("RECEIPT", str(lane / "project-trust.jsonl")),
    ):
        source = source.replace(name, json.dumps(value))
    (lane / "pi-project-trust.mjs").write_text(source, encoding="utf-8")


def prepare(args: argparse.Namespace) -> None:
    from daem0nmcp.claude_hooks.install import install_claude_hooks, module_launch_args
    from daem0nmcp.client_install import _COVENANT, install_client
    from daem0nmcp.workspace import WorkspaceRegistry

    lane = lane_path(args.root, args.host, args.source, args.matrix)
    project = args.projects_root / args.matrix / args.host / args.source / "project"
    before = original_hashes()
    shutil.copytree(args.fixtures / args.source, project)
    manifest = json.loads((project / "native-acceptance-source.json").read_text())
    for name, expected in manifest["files"].items():
        if digest(project / name) != expected:
            raise RuntimeError("Transferred source fixture digest mismatch")
    env = environment(args.root, args.host, args.source)
    subprocess.run(
        [
            "/usr/bin/git",
            "-c",
            "init.defaultBranch=main",
            "init",
            "--quiet",
            str(project),
        ],
        env=env,
        check=True,
        capture_output=True,
    )
    sentinel = f"native-followup-preserve-{platform.system().lower()}-{args.host}-{args.source}-20261112"
    manifest["instruction_sentinel"] = sentinel
    (project / "AGENTS.md").write_text(
        "# Existing project policy\n\nPreserve this policy: "
        + sentinel
        + ".\n"
        + "Project acceptance marker: "
        + sentinel
        + ".\n",
        encoding="utf-8",
    )
    (project / "EXISTING_POLICY.md").write_text(sentinel + "\n", encoding="utf-8")
    definition = {
        "command": sys.executable,
        "args": ["-I", str(_sentinel_server(args.root))],
        "cwd": str(project),
    }
    if args.host == "codex":
        target = project / ".codex/config.toml"
        target.parent.mkdir()
        target.write_text(
            f"# {sentinel}\n[mcp_servers.acceptance_sentinel]\n"
            f"command = {json.dumps(sys.executable)}\n"
            f"args = {json.dumps(definition['args'])}\n"
            f"cwd = {json.dumps(str(project))}\n",
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
                            "command": [sys.executable, *definition["args"]],
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
    automation = args.automation or MODES[args.host]
    if args.host == "claude":
        write_json(
            project / ".mcp.json",
            {
                "mcpServers": {
                    "acceptance_sentinel": definition,
                    "daem0nmcp": {
                        "command": sys.executable,
                        "args": [
                            "-E",
                            "-u",
                            "-X",
                            "utf8",
                            *module_launch_args("daem0nmcp.server"),
                        ],
                        "cwd": str(project),
                        "env": {
                            "DAEM0NMCP_PROJECT_ROOT": str(project),
                            "DAEM0NMCP_WORKSPACE_ROOTS": json.dumps([str(project)]),
                            "DAEM0NMCP_COVENANT_MODE": "guided",
                        },
                    },
                }
            },
        )
        (project / "CLAUDE.md").write_text(
            "Project acceptance marker: " + sentinel + ".\n\n" + _COVENANT + "\n",
            encoding="utf-8",
        )
        installed = []
        for _ in range(2):
            previous = config_hashes(project)
            result = install_claude_hooks(
                project_path=project,
                automation=automation,
                client_version=VERSIONS["claude"],
                bridge_config_root=bridge_root(lane),
            )
            if not result[0]:
                raise RuntimeError(result[1])
            if installed and previous != config_hashes(project):
                raise RuntimeError(
                    "Claude repeated installation changed project configuration"
                )
            installed.append(result[1])
    else:
        parameters = {
            "python_executable": sys.executable,
            "client_version": VERSIONS[args.host],
            "automation": automation,
            "bridge_config_root": bridge_root(lane),
        }
        original = config_hashes(project)
        preview = install_client(args.host, project, dry_run=True, **parameters)
        if original != config_hashes(project):
            raise RuntimeError("Client preview changed project configuration")
        applied = install_client(args.host, project, **parameters)
        previous = config_hashes(project)
        repeated = install_client(args.host, project, **parameters)
        if previous != config_hashes(project):
            raise RuntimeError("Client repeat changed project configuration")
        installed = {"preview": preview, "applied": applied, "repeated": repeated}
    workspace = WorkspaceRegistry([project], default_root=project).default.workspace_id
    if args.host == "codex":
        home = Path(env["CODEX_HOME"])
        home.mkdir(parents=True, exist_ok=True)
        (home / "config.toml").write_text(
            'cli_auth_credentials_store = "ephemeral"\n'
            + f'[projects.{json.dumps(str(project))}]\ntrust_level = "trusted"\n',
            encoding="utf-8",
        )
    if args.host == "pi":
        _pi_trust(lane, project)
    if args.host == "claude":
        settings = project / ".claude/settings.local.json"
        value = json.loads(settings.read_text()) if settings.exists() else {}
        value["enabledMcpjsonServers"] = ["daem0nmcp", "acceptance_sentinel"]
        write_json(settings, value)
    native = args.root / (
        "tools-claude-enforce/bin/claude"
        if args.host == "claude"
        else "tools/bin/" + args.host
    )
    after = original_hashes()
    write_json(
        lane / "fixture.json",
        {
            "host": args.host,
            "source": args.source,
            "project": str(project),
            "workspace_id": workspace,
            "manifest": manifest,
            "binary": str(native),
            "binary_sha256": digest(native),
            "client_version": VERSIONS[args.host],
            "automation": automation,
            "config_hashes": config_hashes(project),
            "installation": installed,
            "original_hashes_before": before,
            "original_hashes_after": after,
            "original_files_unchanged": before == after,
            "source_state": "initial",
        },
    )
    if before != after:
        raise RuntimeError(
            "Original user files changed during isolated fixture preparation"
        )
    print(
        json.dumps(
            {
                "host": args.host,
                "source": args.source,
                "workspace_id": workspace,
                "prepared": True,
            }
        )
    )


async def _run_claude(metadata, lane, stage, evidence, env, prior, model):
    from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient
    from claude_agent_sdk.types import PermissionResultDeny

    async def deny(_tool, _arguments, _context):
        return PermissionResultDeny(
            message="Only declared native Daem0n MCP calls are authorized in this indexing stage"
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
        mcp_servers=str(Path(metadata["project"]) / ".mcp.json"),
        strict_mcp_config=True,
        extra_args={
            "no-session-persistence": None,
            "disable-slash-commands": None,
            "permission-prompts": "host",
        },
    )
    prompt = scenario_prompt(
        "claude", stage, metadata["workspace_id"], metadata["manifest"]
    )
    evidence.mkdir(parents=True, exist_ok=False)
    write_json(
        evidence / "request.json",
        {
            "host": "claude",
            "stage": stage,
            "model": model,
            "native_cli": metadata["binary"],
            "authentication": "Existing isolated native account; no OAuth export",
        },
    )
    events = []

    started = time.monotonic()
    failure = None
    original_environment = dict(os.environ)
    os.environ.clear()
    os.environ.update(env)
    with (evidence / "events.jsonl").open("w", encoding="utf-8") as log:
        try:
            async with asyncio.timeout(900):
                async with ClaudeSDKClient(options=options) as client:
                    readiness = []
                    for _ in range(120):
                        status = await client.get_mcp_status()
                        readiness.append(status)
                        write_json(evidence / "mcp-readiness.json", redact(readiness))
                        if any(
                            server.get("name") == "daem0nmcp"
                            and server.get("status") == "connected"
                            for server in status.get("mcpServers", [])
                        ):
                            break
                        await asyncio.sleep(0.5)
                    else:
                        raise RuntimeError(
                            "Native project Daem0n MCP did not become connected"
                        )
                    write_json(evidence / "mcp-readiness.json", redact(readiness))
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
        except Exception as error:
            failure = {"type": type(error).__name__, "reason": redact(str(error))}
        finally:
            os.environ.clear()
            os.environ.update(original_environment)
    observed = native_receipts(events, "claude")
    (evidence / "native-tool-receipts.jsonl").write_text(
        "".join(json.dumps(item) + "\n" for item in observed), encoding="utf-8"
    )
    result = {
        "host": "claude",
        "stage": stage,
        "elapsed_seconds": time.monotonic() - started,
        "validation": validate_receipts(
            observed,
            stage,
            metadata["manifest"],
            prior,
            workspace_id=metadata["workspace_id"],
        ),
    }
    if failure or any(
        item.get("sdk_message_type") == "ResultMessage" and item.get("is_error")
        for item in events
    ):
        result["validation"]["status"] = "failed"
        result["native_driver_error"] = failure
    if stage == "initial":
        loading = instruction_evidence(events, "claude", metadata["manifest"])
        result["instruction_loading"] = loading
        result["validation"]["checks"]["preloaded_instruction_marker_reported"] = (
            loading["status"] == "passed"
        )
        if loading["status"] != "passed":
            result["validation"]["status"] = "failed"
    write_json(evidence / "summary.json", result)
    return result


def prepare_baseline_parent(path: Path) -> None:
    """Prepare a new output parent while refusing linked path components."""
    absolute = path.absolute()
    if any(part.is_symlink() for part in (absolute, *absolute.parents)):
        raise RuntimeError("Linked native baseline paths are not supported")
    if absolute.exists():
        raise RuntimeError(
            "Preserve existing native initial baseline; select a new baseline path"
        )
    absolute.parent.mkdir(parents=True, exist_ok=True)


def run(args: argparse.Namespace) -> int:
    lane = lane_path(args.root, args.host, args.source, args.matrix)
    metadata = json.loads((lane / "fixture.json").read_text())
    project = Path(metadata["project"])
    if config_hashes(project) != metadata["config_hashes"]:
        raise RuntimeError("Reviewed native project configuration changed")
    if digest(Path(metadata["binary"])) != metadata["binary_sha256"]:
        raise RuntimeError("Pinned native client executable changed")
    env = environment(args.root, args.host, args.source)
    before = original_hashes()
    evidence = getattr(args, "evidence_dir", None) or lane / "evidence" / args.attempt
    baseline_path = (
        getattr(args, "baseline_path", None) or lane / "initial-validation.json"
    )
    if evidence.exists():
        raise RuntimeError("Preserve existing native attempt evidence")
    if args.stage == "initial":
        prepare_baseline_parent(baseline_path)
    prior = json.loads(baseline_path.read_text()) if args.stage == "restart" else None
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
    secrets = ()
    if args.host == "codex":
        if platform.system() == "Linux":
            import tomlkit

            account_home = args.root / "private/codex-account/.codex"
            if not (account_home / "auth.json").is_file():
                raise RuntimeError("Isolated native Codex account login is required")
            account_config = account_home / "config.toml"
            document = tomlkit.parse(account_config.read_text())
            if "projects" not in document:
                document["projects"] = tomlkit.table()
            document["projects"][str(project)] = {"trust_level": "trusted"}
            account_config.write_text(tomlkit.dumps(document), encoding="utf-8")
            account_config.chmod(0o600)
            env["CODEX_HOME"] = str(account_home)
            result = run_codex(argv=[native, "app-server"], model=model, **common)
        else:
            original = json.loads((Path.home() / ".codex/auth.json").read_text())
            access = original.get("tokens", {}).get("access_token")
            account = original.get("tokens", {}).get("account_id")
            del original
            if not isinstance(access, str) or not isinstance(account, str):
                raise RuntimeError(
                    "Existing native Codex access-only credentials unavailable"
                )
            result = run_codex(
                argv=[native, "app-server"],
                model=model,
                access_token=access,
                account_id=account,
                **common,
            )
    elif args.host == "claude":
        result = asyncio.run(
            _run_claude(metadata, lane, args.stage, evidence, env, prior, model)
        )
    else:
        prompt = scenario_prompt(
            args.host, args.stage, metadata["workspace_id"], metadata["manifest"]
        )
        if args.host == "agy":
            settings = Path(env["HOME"]) / ".gemini/antigravity-cli/settings.json"
            value = json.loads(settings.read_text()) if settings.exists() else {}
            value["trustedWorkspaces"] = [str(project)]
            value["permissions"] = {
                "allow": [f"mcp(daem0nmcp/{name})" for name in OPERATIONS],
                "deny": [
                    "run_command(*)",
                    "write_to_file(*)",
                    "replace_file_content(*)",
                    "multi_replace_file_content(*)",
                    "read_url_content(*)",
                ],
            }
            write_json(settings, value)
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
        else:
            key = os.environ.get("MINIMAX_API_KEY")
            if not key:
                raise RuntimeError(
                    "Existing selected MiniMax API key must be supplied in process environment"
                )
            env["MINIMAX_API_KEY"] = key
            secrets = (key,)
            if args.host == "opencode":
                env["OPENCODE_PERMISSION"] = json.dumps(
                    {
                        "*": "deny",
                        **{"daem0nmcp_" + name: "allow" for name in OPERATIONS},
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
                    model,
                    prompt,
                ]
            elif args.host == "pi":
                argv = [
                    native,
                    "--mode",
                    "json",
                    "--print",
                    "--no-session",
                    "--exclude-tools",
                    "bash,read,edit,write",
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
    preserved = before == after
    write_json(
        evidence / "preservation.json",
        {"before": before, "after": after, "original_files_unchanged": preserved},
    )
    if not preserved or digest(Path(native)) != metadata["binary_sha256"]:
        result["validation"]["status"] = "failed"
        result["validation"]["checks"]["original_files_and_pinned_binary_unchanged"] = (
            False
        )
        write_json(evidence / "summary.json", result)
    if args.stage == "initial" and result["validation"]["status"] == "passed":
        with baseline_path.open("x", encoding="utf-8") as handle:
            json.dump(result["validation"], handle, indent=2)
            handle.write("\n")
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


def mutate(args: argparse.Namespace, *, restore: bool = False) -> None:
    lane = lane_path(args.root, args.host, args.source, args.matrix)
    metadata = json.loads((lane / "fixture.json").read_text())
    project = Path(metadata["project"])
    specification = metadata["manifest"]["scenario"]
    target = project / specification["mutation_path"]
    added = project / specification["added_path"]
    original = args.fixtures / args.source / specification["mutation_path"]
    if restore:
        expected = original.read_bytes() + specification["mutation_append"].encode()
        if (
            target.read_bytes() != expected
            or added.read_text() != specification["added_content"]
        ):
            raise RuntimeError(
                "Disposable source differs from the exact owned mutation"
            )
        target.write_bytes(original.read_bytes())
        added.unlink()
        state = "restored"
    else:
        if (
            digest(target)
            != metadata["manifest"]["files"][specification["mutation_path"]]
            or added.exists()
        ):
            raise RuntimeError(
                "Disposable source no longer matches the frozen baseline"
            )
        target.write_bytes(
            target.read_bytes() + specification["mutation_append"].encode()
        )
        added.parent.mkdir(parents=True, exist_ok=True)
        added.write_text(specification["added_content"], encoding="utf-8")
        state = "mutated"
    metadata["source_state"] = state
    write_json(lane / "fixture.json", metadata)
    print(json.dumps({"host": args.host, "source": args.source, "source_state": state}))


def baseline(args) -> None:
    """Keep verified core identifiers without replacing a failed source verdict."""
    if getattr(args, "evidence_dir", None) or getattr(args, "baseline_path", None):
        raise ValueError(
            "baseline action does not support repair output flags; use run"
        )
    lane = lane_path(args.root, args.host, args.source, args.matrix)
    source = lane / "evidence" / args.attempt / "summary.json"
    result = json.loads(source.read_text())["validation"]
    required = (
        "native_memory_store",
        "native_memory_recall",
        "stored_record_recalled",
        "guided_auto_brief",
        "native_outcome",
        "preloaded_instruction_marker_reported",
    )
    if not all(result["checks"].get(name) for name in required):
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
            "checks": {name: result["checks"][name] for name in required},
        },
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "action", choices=("prepare", "run", "mutate", "restore", "baseline")
    )
    parser.add_argument(
        "--root",
        type=Path,
        default=Path.home() / ".daem0nmcp-native-acceptance/20261112",
    )
    parser.add_argument("--fixtures", type=Path, required=True)
    parser.add_argument(
        "--projects-root",
        type=Path,
        default=Path("/private/tmp" if platform.system() == "Darwin" else "/var/tmp")
        / "daem0nmcp-native-acceptance-20261112",
    )
    parser.add_argument("--matrix", default="final")
    parser.add_argument("--host", choices=HOSTS, required=True)
    parser.add_argument("--source", choices=("daem0n", "legion"), required=True)
    parser.add_argument("--automation", choices=("off", "remind", "enforce"))
    parser.add_argument("--stage", choices=STAGES, default="initial")
    parser.add_argument("--attempt")
    parser.add_argument("--model")
    parser.add_argument("--evidence-dir", type=Path)
    parser.add_argument("--baseline-path", type=Path)
    args = parser.parse_args()
    if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_-]{0,80}", args.matrix):
        parser.error("matrix requires a simple bounded identifier")
    if args.action == "run" and (
        not args.attempt or not args.attempt.replace("-", "").replace("_", "").isalnum()
    ):
        parser.error("run requires a simple unique --attempt identifier")
    if args.action == "prepare":
        prepare(args)
    elif args.action == "baseline":
        if not args.attempt:
            parser.error("baseline requires --attempt for the original source evidence")
        baseline(args)
    elif args.action == "run":
        evidence = args.evidence_dir or (
            lane_path(args.root, args.host, args.source, args.matrix)
            / "evidence"
            / args.attempt
        )
        if evidence.exists():
            parser.error(
                "native evidence attempt already exists; choose a new identifier"
            )
        original_before = original_hashes()
        try:
            return run(args)
        except Exception as error:
            evidence.mkdir(parents=True, exist_ok=True)
            original_after = original_hashes()
            failure = {
                "host": args.host,
                "source": args.source,
                "stage": args.stage,
                "status": "failed",
                "error_type": type(error).__name__,
                "reason": redact(str(error), (os.environ.get("MINIMAX_API_KEY", ""),)),
                "original_files_unchanged": original_before == original_after,
            }
            write_json(evidence / "driver-failure.json", failure)
            write_json(
                evidence / "preservation.json",
                {"before": original_before, "after": original_after},
            )
            print(json.dumps(failure), flush=True)
            return 2
    else:
        mutate(args, restore=args.action == "restore")
    return 0


if __name__ == "__main__":
    sys.exit(main())
