"""Observe pinned OpenCode V2 through its native CLI and project plugin hooks.

The observation plugin records host events only. It never invents MCP responses,
approves an edit, copies OAuth credentials, or replaces OpenCode's MCP manager.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import sqlite3
import stat
import subprocess
import sys
import time
from contextlib import suppress
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from native_acceptance_drivers import _close_owned_group, _spawn, redact, write_json
from native_acceptance_fixtures import export_project
from native_acceptance_scenarios import (
    _receipt,
    envelopes,
    scenario_prompt,
    validate_receipts,
)
from native_agy_loading_acceptance import isolated_environment
from native_opencode_v2_readiness import validate_service_url

from daem0nmcp.client_install import install_client
from daem0nmcp.event_store import export_event_bundle

OBSERVATION = r"""import { appendFileSync } from "node:fs";
const file = process.env.DAEM0N_V2_OBSERVATION!;
function log(value: unknown) { appendFileSync(file, JSON.stringify(value) + "\n"); }
function canonical(value: any): string {
  const stable = (item: any): any => Array.isArray(item) ? item.map(stable) : item && typeof item === "object" ? Object.fromEntries(Object.entries(item).sort(([a], [b]) => a.localeCompare(b)).map(([key, child]) => [key, stable(child)])) : item;
  return JSON.stringify(stable(value));
}
export default {
  id: "daem0n-native-observation",
  async setup(ctx: any) {
    log({event: "setup", version: ctx.app.version, location: ctx.location});
    await ctx.session.hook("context", (event: any) => {
      log({event: "context", sessionID: event.sessionID, agent: event.agent,
        tools: Object.keys(event.tools), system: event.system});
    });
    await ctx.tool.hook("execute.before", (event: any) => {
      log({event: "before", ...event});
      if (process.env.DAEM0N_V2_EXPECTED_INDEX && event.tool.endsWith("_daem0n_tool_call") && event.input.tool === "code_index") {
        const expected = JSON.parse(process.env.DAEM0N_V2_EXPECTED_INDEX);
        if (canonical(event.input) !== canonical(expected)) {
          log({event: "acceptance_denial", code: "INDEX_SCOPE", actual: event.input, expected});
          throw new Error("NATIVE_ACCEPTANCE_INDEX_SCOPE_REQUIRED: use exactly " + JSON.stringify(expected));
        }
      }
      if (event.tool === "write" && /(?:^|[/\\])denied\.txt$/.test(event.input.filePath ?? event.input.path ?? "")) {
        throw new Error("DAEM0N_V2_NATIVE_DENIAL_SENTINEL");
      }
    });
    await ctx.tool.hook("execute.after", (event: any) => {
      log({event: "after", ...event, error: event.status === "error" ?
        {name: event.error?.name, message: event.error?.message} : undefined});
    });
  },
};
"""

CORE = (
    "system_health",
    "session_brief",
    "memory_preflight",
    "memory_store",
    "memory_recall",
    "memory_record_outcome",
    "daem0n_tools_search",
    "daem0n_tool_call",
    "edit_preflight",
)


def v2_receipts(events, mode):
    """Match the scenario's mode-qualified seed label; retain native identity."""
    if mode not in {"code", "direct"}:
        raise ValueError("Unknown native V2 exposure mode")
    receipts = []
    for event in events:
        if event.get("event") == "after" and str(event.get("tool", "")).startswith(
            "daem0nmcp_"
        ):
            receipts.extend(
                _receipt(
                    "opencode-v2-" + mode,
                    event["tool"],
                    event.get("input"),
                    [
                        event.get("result", {}).get("output"),
                        event.get("error", {}).get("message"),
                    ],
                    event.get("id"),
                    event.get("sessionID"),
                )
            )
    return receipts


def selected_key() -> str:
    inherited = os.environ.get("MINIMAX_API_KEY")
    if inherited:
        return inherited
    if os.name != "nt":
        raise RuntimeError(
            "Selected existing API key must be handed off in process environment"
        )
    path = Path(os.environ["USERPROFILE"]) / ".local/share/opencode/auth.json"
    record = json.loads(path.read_text(encoding="utf-8-sig")).get("minimax-coding-plan")
    if not isinstance(record, dict) or record.get("type") != "api":
        raise RuntimeError("Existing selected API-key account is unavailable")
    key = record.get("key")
    if not isinstance(key, str) or not key.strip():
        raise RuntimeError("Existing selected API-key account is empty")
    return key


def prepare(args):
    if not args.python.is_file():
        raise FileNotFoundError("Selected isolated Python executable is unavailable")
    root = args.output.resolve()
    root.mkdir(parents=True, exist_ok=False)
    project = root / "project"
    if args.fixture:
        source = args.fixture.resolve(strict=True)
        manifest = json.loads(
            (source / "native-acceptance-source.json").read_text(encoding="utf-8")
        )
        project.mkdir()
        for relative, expected in manifest["files"].items():
            origin = (source / relative).resolve(strict=True)
            target = (project / relative).resolve()
            if not origin.is_relative_to(source) or not target.is_relative_to(project):
                raise RuntimeError("Pre-exported fixture path escapes its root")
            data = origin.read_bytes()
            if hashlib.sha256(data).hexdigest() != expected:
                raise RuntimeError("Pre-exported source digest changed")
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
        write_json(project / "native-acceptance-source.json", manifest)
    else:
        manifest = export_project(args.source, args.repository, project)
    mutation = project / manifest["scenario"]["mutation_path"]
    (root / "original-mutation.bin").write_bytes(mutation.read_bytes())
    subprocess.run(
        ["git", "init", "--quiet", str(project)],
        check=True,
        capture_output=True,
    )
    initial_policy = (
        "# Existing project policy\n\nPreserve this native acceptance policy.\n"
    )
    (project / "AGENTS.md").write_text(initial_policy, encoding="utf-8")
    unrelated = {
        "type": "local",
        "command": [str(args.python.absolute()), "-c", "pass"],
        "disabled": True,
    }
    (project / "opencode.json").write_text(
        json.dumps({"mcp": {"servers": {"acceptance_sentinel": unrelated}}}),
        encoding="utf-8",
    )
    parameters = {
        # Resolving a POSIX venv's symlink would select its base interpreter
        # and discard the isolated installed package environment.
        "python_executable": str(args.python.absolute()),
        "client_version": "2.2.7",
        "opencode_interface": "v2",
        "opencode_code_mode": args.mode,
        "automation": args.automation,
        "bridge_config_root": root / "private-bridge",
    }
    before_preview = {
        path.name: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in (project / "AGENTS.md", project / "opencode.json")
    }
    preview = install_client("opencode", project, dry_run=True, **parameters)
    if before_preview != {
        path.name: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in (project / "AGENTS.md", project / "opencode.json")
    }:
        raise RuntimeError("Installer preview modified project settings")
    result = install_client(
        "opencode",
        project,
        **parameters,
    )
    write_json(root / "install.json", result)
    installed_hashes = {
        str(path.relative_to(project)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in [
            project / "AGENTS.md",
            project / "opencode.json",
            *sorted((project / ".opencode").rglob("*")),
        ]
        if path.is_file()
    }
    repeat = install_client("opencode", project, **parameters)
    if installed_hashes != {
        str(path.relative_to(project)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in [
            project / "AGENTS.md",
            project / "opencode.json",
            *sorted((project / ".opencode").rglob("*")),
        ]
        if path.is_file()
    }:
        raise RuntimeError("Installer repeat changed managed project bytes")
    write_json(
        root / "installer-preservation.json",
        {
            "preview": preview,
            "repeat": repeat,
            "preview_unchanged": True,
            "repeat_unchanged": True,
            "existing_policy_preserved": (project / "AGENTS.md")
            .read_text(encoding="utf-8")
            .startswith(initial_policy),
            "unrelated_server_preserved": json.loads(
                (project / "opencode.json").read_text(encoding="utf-8")
            )["mcp"]["servers"]["acceptance_sentinel"]
            == unrelated,
        },
    )
    workspace = json.loads(
        subprocess.check_output(
            result["workspace_id_command"], cwd=project, text=True, encoding="utf-8"
        )
    )["workspace_id"]
    configuration = project / "opencode.json"
    value = json.loads(configuration.read_text(encoding="utf-8"))
    value["permissions"] = [
        {"action": "*", "resource": "*", "effect": "deny"},
        *[
            {"action": "daem0nmcp_" + tool, "resource": "*", "effect": "allow"}
            for tool in CORE
        ],
        {"action": "execute", "resource": "*", "effect": "allow"},
        {
            "action": "opencode_list_mcp_resources",
            "resource": "daem0nmcp",
            "effect": "allow",
        },
        {
            "action": "opencode_read_mcp_resource",
            "resource": "daem0nmcp:memory://*",
            "effect": "allow",
        },
        {"action": "edit", "resource": "denied.txt", "effect": "allow"},
        {"action": "edit", "resource": "completed.txt", "effect": "allow"},
        {"action": "edit", "resource": "gate-approved.txt", "effect": "allow"},
    ]
    server = value["mcp"]["servers"]["daem0nmcp"]
    server["environment"].update(
        {
            "DAEM0NMCP_LOCAL_ENABLED": "false",
            "DAEM0NMCP_MODELS_LOCAL_ENABLED": "false",
            "DAEM0NMCP_LATE_INTERACTION_ENABLED": "false",
            "DAEM0NMCP_DREAM_ENABLED": "false",
            "DAEM0NMCP_GRAPH_ENABLED": "true",
        }
    )
    if args.bridge_runtime:
        runtime = args.bridge_runtime
        if runtime.is_symlink() or runtime != runtime.resolve():
            raise RuntimeError("Bridge runtime override must use its canonical path")
        runtime.mkdir(mode=0o700, parents=True, exist_ok=True)
        information = runtime.stat()
        if os.name != "nt" and (
            information.st_uid != os.getuid()
            or stat.S_IMODE(information.st_mode) != 0o700
        ):
            raise RuntimeError("Bridge runtime override must be owner-only")
        sidecar_path = project / ".opencode/daem0n-v2-host.json"
        sidecar = json.loads(sidecar_path.read_text(encoding="utf-8"))
        previous = server["environment"].get("DAEM0NMCP_EDIT_BRIDGE_RUNTIME_DIR")
        server["environment"]["DAEM0NMCP_EDIT_BRIDGE_RUNTIME_DIR"] = str(runtime)
        sidecar["environment"]["DAEM0NMCP_EDIT_BRIDGE_RUNTIME_DIR"] = str(runtime)
        write_json(sidecar_path, sidecar)
        write_json(
            root / "bridge-runtime-override.json",
            {
                "previous": previous,
                "runtime_dir": str(runtime),
                "owner_only": True,
                "scope": "Supported matching MCP server and native-plugin environment override",
            },
        )
    configuration.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")
    plugin = project / ".opencode/plugins/zz-observation.ts"
    plugin.parent.mkdir(parents=True, exist_ok=True)
    plugin.write_text(OBSERVATION, encoding="utf-8")
    write_json(
        root / "scenario.json",
        {
            "mode": args.mode,
            "source": manifest,
            "workspace_id": workspace,
            "backend": str(args.python.absolute()),
            "version": "2.2.7",
            "automation": args.automation,
        },
    )
    print(json.dumps({"prepared": str(root), "workspace_id": workspace}))


def execute(args):
    if args.ordinary_workflow and (
        args.index_argument_policy != "off"
        or args.indexing_only
        or args.restart_code_only
    ):
        raise ValueError(
            "Ordinary workflow requires unguarded shared scenario guidance"
        )
    if getattr(args, "observation_file", None) and not getattr(args, "server", None):
        raise ValueError("External observation requires explicit owned --server mode")
    server_password = (
        os.environ.get("OPENCODE_PASSWORD") if getattr(args, "server", None) else None
    )
    if getattr(args, "server", None):
        validate_service_url(args.server)
        if not server_password:
            raise ValueError("Owned native --server mode requires OPENCODE_PASSWORD")
    root = args.output.resolve(strict=True)
    observation_source = None
    if getattr(args, "observation_file", None):
        candidate = args.observation_file.absolute()
        observation_source = candidate.resolve(strict=True)
        if (
            candidate != observation_source
            or not observation_source.is_file()
            or observation_source.name != "host-events.jsonl"
            or observation_source.parent.parent != root
        ):
            raise ValueError("External observation must be an owned real attempt log")
        if observation_source.parent.name != args.attempt + "-service":
            raise ValueError("External observation must belong to this exact attempt")
    project = root / "project"
    attempt = root / args.attempt
    attempt.mkdir(exist_ok=False)
    observation_source = observation_source or attempt / "host-events.jsonl"
    home = root / "private-home"
    home.mkdir(exist_ok=True)
    env = isolated_environment(home)
    parser_cache = home / "parser-cache"
    parser_cache.mkdir(exist_ok=True)
    key = selected_key()
    sensitive_values = (key,)
    if server_password:
        env["OPENCODE_PASSWORD"] = server_password
        sensitive_values += (
            server_password,
            base64.b64encode(("opencode:" + server_password).encode()).decode(),
        )
    env.update(
        MINIMAX_API_KEY=key,
        DAEM0N_V2_OBSERVATION=str(observation_source),
        TREE_SITTER_LANGUAGE_PACK_CACHE_DIR=str(parser_cache),
    )
    metadata = json.loads((root / "scenario.json").read_text(encoding="utf-8"))
    prompt_manifest = json.loads(json.dumps(metadata["source"]))
    # The shared driver uses the source prefix as its index root. This V2
    # driver already prefixes its patterns to exclude installed TS adapters;
    # use workspace root consistently rather than prefixing the path twice.
    if all(
        pattern.startswith(prompt_manifest["scenario"]["source_prefix"])
        for pattern in prompt_manifest["scenario"]["patterns"]
    ):
        prompt_manifest["scenario"]["source_prefix"] = "."
    expected_index = {
        "workspace_id": metadata["workspace_id"],
        "tool": "code_index",
        "arguments": {
            "relative_root": ".",
            "patterns": metadata["source"]["scenario"]["patterns"],
        },
    }
    if (
        args.scenario
        and args.scenario != "restart"
        and args.index_argument_policy == "enforce"
    ):
        env["DAEM0N_V2_EXPECTED_INDEX"] = json.dumps(expected_index)
    prompt = (
        scenario_prompt(
            "opencode-v2-" + metadata["mode"],
            args.scenario,
            metadata["workspace_id"],
            prompt_manifest,
        )
        if args.scenario
        else args.prompt.read_text(encoding="utf-8")
    )
    if args.indexing_only:
        specification = metadata["source"]["scenario"]
        prompt = (
            "Run only the previously failed native indexing checks after the coordinator fixed "
            "the isolated parser-cache environment. Use actual native Daem0n MCP tools, discovery "
            "and gateway only; no shell, filesystem reads/edits, network, subagents or maintenance. "
            "Stop on any nonretryable capability error; do not repair or repeat failed calls. "
            f"workspace_id={metadata['workspace_id']}. Call system_health. Discover code_index, "
            "code_search and code_impact_analyze using daem0n_tools_search. Invoke code_index with "
            f"relative_root='.', patterns={json.dumps(specification['patterns'])}; repeat that "
            "IDENTICAL index once without force and compare manifests. Invoke code_search with "
            f"query={json.dumps(specification['search'])}, limit=100; select the current entity "
            f"from {specification['subject_path']} then code_impact_analyze with that returned "
            "code_entity_id, max_depth=2. In gateway put workspace_id at top level only, tool as "
            "discovered name, arguments without workspace_id. On COUNSEL_REQUIRED read actual "
            "counsel and retry exactly its remedy in this same session. Do not repeat the already "
            "successful memory checks."
        )
    if args.restart_code_only:
        specification = metadata["source"]["scenario"]
        calls = [
            {
                "workspace_id": metadata["workspace_id"],
                "tool": "code_search",
                "arguments": {"query": specification[name], "limit": 100},
            }
            for name in ("search", "mutation_search", "added_search")
        ]
        prompt = (
            "Run ONLY the previously failed persisted-code checks in this fresh native process. "
            "The memory recall/replay checks already passed; do not repeat them. Use actual "
            "registered Daem0n MCP gateway tools only. No code_index/rebuild, shell, filesystem, "
            "network or subagents. Copy EACH of these whole daem0n_tool_call inputs exactly: "
            + json.dumps(calls)
            + ". Select the current code_entity_id for "
            + specification["subject_path"]
            + " ending in "
            + specification["search"]
            + ", then call daem0n_tool_call with workspace_id at TOP LEVEL, tool='code_impact_analyze', "
            "arguments={code_entity_id:<that actual returned ID>,max_depth:2}. Do not duplicate "
            "workspace_id inside arguments. The two mutation searches must be empty. Finally "
            f"call system_health(workspace_id='{metadata['workspace_id']}')."
        )
    prompt += (
        "\nEvery system_health call, including the final diagnostic, must explicitly "
        f"include workspace_id={metadata['workspace_id']}. Do not omit this argument."
    )
    if args.scenario and args.scenario != "restart" and not args.ordinary_workflow:
        prompt += (
            "\nFor EACH index invocation copy this WHOLE daem0n_tool_call input exactly, "
            "without moving or adding fields: " + json.dumps(expected_index)
        )
    command = [
        str(args.client.resolve()),
        "run",
        *(
            ["--server", args.server]
            if getattr(args, "server", None)
            else ["--standalone"]
        ),
        "--format",
        "json",
        "--model",
        "minimax-coding-plan/MiniMax-M3",
        "--title",
        "Daem0n V2 " + args.attempt,
        prompt,
    ]
    write_json(
        attempt / "request.json",
        {
            "command": command,
            "cwd": str(project),
            "prompt_sha256": hashlib.sha256(prompt.encode()).hexdigest(),
            "credential": "Existing selected API key in child process environment only",
            "ordinary_workflow": args.ordinary_workflow,
            "index_argument_policy": args.index_argument_policy,
            **(
                {"observation_source": str(observation_source)}
                if getattr(args, "observation_file", None)
                else {}
            ),
        },
    )
    start = time.monotonic()
    process = _spawn(
        command,
        cwd=project,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    owned = {"pid": process.pid, "native_root_owned": True}
    if os.name != "nt":
        with suppress(ProcessLookupError):
            owned.update(
                pgid=os.getpgid(process.pid),
                session_id=os.getsid(process.pid),
                uid=os.getuid(),
            )
    write_json(attempt / "owned-process.json", owned)

    def close_group():
        try:
            return _close_owned_group(process)
        except PermissionError:
            # Preserve actual tool evidence and leave group closure explicitly
            # unverified; never signal another computed group to hide failure.
            return {
                "process_group_id": process.pid,
                "process_group_closed": None,
                "native_root_exited": process.poll() is not None,
                "cleanup_error": "PermissionError",
            }

    timed_out = False
    cleanup = None
    abort_code = None
    try:
        while True:
            try:
                stdout, stderr = process.communicate(timeout=1)
                break
            except subprocess.TimeoutExpired:
                timed_out = time.monotonic() - start >= args.timeout
                observed = observation_source
                if observed.is_file():
                    policy_denials = 0
                    for line in observed.read_text(encoding="utf-8").splitlines():
                        try:
                            item = json.loads(line)
                        except ValueError:
                            continue
                        if item.get("event") == "acceptance_denial":
                            policy_denials += 1
                            if policy_denials >= 3:
                                abort_code = "NATIVE_ARGUMENT_POLICY"
                                break
                        if item.get("event") != "after" or not str(
                            item.get("tool", "")
                        ).startswith("daem0nmcp_"):
                            continue
                        for response in envelopes(
                            [item.get("result"), item.get("error")]
                        ):
                            code = (response.get("error") or {}).get("code")
                            if (
                                response.get("ok") is False
                                and code != "COUNSEL_REQUIRED"
                            ):
                                abort_code = code or "NATIVE_MCP_ERROR"
                                break
                        if abort_code:
                            break
                if timed_out or abort_code:
                    cleanup = close_group()
                    stdout, stderr = process.communicate(timeout=30)
                    break
    finally:
        if cleanup is None:
            cleanup = close_group()
    (attempt / "stdout.jsonl").write_text(
        redact(stdout, sensitive_values), encoding="utf-8"
    )
    (attempt / "stderr.log").write_text(
        redact(stderr, sensitive_values), encoding="utf-8"
    )
    event_path = attempt / "host-events.jsonl"
    events = []
    if observation_source.is_file():
        raw = observation_source.read_text(encoding="utf-8")
        event_path.write_text(redact(raw, sensitive_values), encoding="utf-8")
        events = [
            json.loads(line)
            for line in event_path.read_text(encoding="utf-8").splitlines()
            if line
        ]
    receipts = v2_receipts(events, metadata["mode"])
    write_json(attempt / "native-mcp-receipts.json", receipts)
    validation = None
    if args.scenario:
        prior = (
            json.loads(args.prior.read_text(encoding="utf-8")) if args.prior else None
        )
        validation = validate_receipts(
            receipts,
            args.scenario,
            metadata["source"],
            prior,
            workspace_id=metadata["workspace_id"],
        )
        if args.indexing_only or args.restart_code_only:
            validation["checks"] = {
                key: value
                for key, value in validation["checks"].items()
                if key
                not in {
                    "native_memory_store",
                    "native_memory_recall",
                    "stored_record_recalled",
                    "guided_auto_brief",
                    "native_outcome",
                    "same_key_original_record_event",
                }
            }
            validation["status"] = (
                "passed" if all(validation["checks"].values()) else "failed"
            )
            validation["scope"] = (
                "Affected indexing checks only; original core receipts retained"
            )
        write_json(attempt / "validation.json", validation)
    write_json(
        attempt / "summary.json",
        {
            "exit_code": process.returncode,
            "timed_out": timed_out,
            "abort_error_code": abort_code,
            "elapsed_seconds": time.monotonic() - start,
            "host_event_count": len(events),
            "event_kinds": {
                name: sum(event.get("event") == name for event in events)
                for name in {event.get("event") for event in events}
            },
            "native_before_tools": [
                event.get("tool") for event in events if event.get("event") == "before"
            ],
            "denied_file_exists": (project / "denied.txt").exists(),
            "completed_file_content": (project / "completed.txt").read_text()
            if (project / "completed.txt").exists()
            else None,
            "validation": validation,
            "cleanup": cleanup,
        },
    )
    print(
        json.dumps(
            {
                "attempt": str(attempt),
                "exit_code": process.returncode,
                "timed_out": timed_out,
                "host_event_count": len(events),
            }
        )
    )


def mutate(args):
    root = args.output.resolve(strict=True)
    project = (root / "project").resolve(strict=True)
    metadata = json.loads((root / "scenario.json").read_text(encoding="utf-8"))
    specification = metadata["source"]["scenario"]
    mutation = (project / specification["mutation_path"]).resolve(strict=True)
    added = (project / specification["added_path"]).resolve()
    if not mutation.is_relative_to(project) or not added.is_relative_to(project):
        raise RuntimeError("Mutation target escapes disposable project")
    original = (root / "original-mutation.bin").read_bytes()
    if args.action == "mutate":
        if mutation.read_bytes() != original or added.exists():
            raise RuntimeError("Disposable fixture preimage does not match")
        mutation.write_bytes(original + specification["mutation_append"].encode())
        added.write_text(specification["added_content"], encoding="utf-8")
    else:
        if (
            mutation.read_bytes()
            != original + specification["mutation_append"].encode()
        ):
            raise RuntimeError("Disposable mutation preimage does not match")
        if added.read_text(encoding="utf-8") != specification["added_content"]:
            raise RuntimeError("Disposable added-file preimage does not match")
        mutation.write_bytes(original)
        added.unlink()
    write_json(
        root / (args.action + "-files.json"),
        {
            "mutation_sha256": hashlib.sha256(mutation.read_bytes()).hexdigest(),
            "added_sha256": hashlib.sha256(added.read_bytes()).hexdigest()
            if added.exists()
            else None,
        },
    )


def rebind(args):
    """Repoint only inactive, test-owned configuration and retain authority roots."""
    root = args.output.resolve(strict=True)
    metadata_path = root / "scenario.json"
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    project = root / "project"
    database = project / ".daem0nmcp/storage/daem0nmcp.db"
    with sqlite3.connect(database.as_uri() + "?mode=ro", uri=True) as connection:
        before_authority = export_event_bundle(connection, metadata["workspace_id"])[
            "root_hash"
        ]
    configuration = project / "opencode.json"
    plugin = project / ".opencode/plugins/daem0n-v2.ts"
    before = {
        str(path.relative_to(project)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in (configuration, plugin)
    }
    old = metadata["backend"]
    if not args.python.is_file():
        raise FileNotFoundError("Selected isolated Python executable is unavailable")
    new = str(args.python.absolute())
    document = json.loads(configuration.read_text(encoding="utf-8"))
    server = document["mcp"]["servers"]["daem0nmcp"]
    if server["command"][0] != old:
        raise RuntimeError("Test-owned MCP executable preimage changed")
    original = plugin.read_text(encoding="utf-8")
    old_line = "const PYTHON_EXECUTABLE = " + json.dumps(old) + ";"
    if original.count(old_line) != 1:
        raise RuntimeError("Test-owned plugin executable preimage changed")
    server["command"][0] = new
    write_json(configuration, document)
    plugin.write_text(
        original.replace(
            old_line, "const PYTHON_EXECUTABLE = " + json.dumps(new) + ";"
        ),
        encoding="utf-8",
    )
    metadata["backend_history"] = [
        *metadata.get("backend_history", []),
        {"from": old, "to": new, "wheel_sha256": args.wheel_sha256},
    ]
    metadata["backend"] = new
    write_json(metadata_path, metadata)
    with sqlite3.connect(database.as_uri() + "?mode=ro", uri=True) as connection:
        after_authority = export_event_bundle(connection, metadata["workspace_id"])[
            "root_hash"
        ]
    if before_authority != after_authority:
        raise RuntimeError("Authority changed during configuration-only rebind")
    write_json(
        root / ((args.attempt or "freeze-02") + "-rebind.json"),
        {
            "before_hashes": before,
            "after_hashes": {
                str(path.relative_to(project)): hashlib.sha256(
                    path.read_bytes()
                ).hexdigest()
                for path in (configuration, plugin)
            },
            "authority_root_before": before_authority,
            "authority_root_after": after_authority,
            "backend": new,
            "wheel_sha256": args.wheel_sha256,
        },
    )


def scope_source(args):
    """Keep installed TypeScript adapters outside real-source indexing patterns."""
    root = args.output.resolve(strict=True)
    metadata_path = root / "scenario.json"
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    specification = metadata["source"]["scenario"]
    before = specification["patterns"]
    prefix = specification["source_prefix"]
    expected = [prefix + pattern for pattern in before]
    if any(pattern.startswith(prefix) for pattern in before):
        raise RuntimeError("Source patterns were already scoped")
    project = root / "project"
    if not all(
        hashlib.sha256((project / relative).read_bytes()).hexdigest() == digest
        for relative, digest in metadata["source"]["files"].items()
    ):
        raise RuntimeError("Frozen source bytes changed before pattern correction")
    specification["patterns"] = expected
    write_json(metadata_path, metadata)
    write_json(
        root / "index-scope-correction.json",
        {
            "before_patterns": before,
            "after_patterns": expected,
            "source_files_unchanged": True,
            "reason": "Exclude installed client adapter TypeScript from source fixture indexing",
        },
    )


def refresh_observer(args):
    root = args.output.resolve(strict=True)
    plugin = root / "project/.opencode/plugins/zz-observation.ts"
    before = hashlib.sha256(plugin.read_bytes()).hexdigest()
    plugin.write_text(OBSERVATION, encoding="utf-8")
    write_json(
        root / "observer-policy-update.json",
        {
            "before_sha256": before,
            "after_sha256": hashlib.sha256(plugin.read_bytes()).hexdigest(),
            "scope": "Test-owned exact source-index argument approval; production adapter unchanged",
        },
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "action",
        choices=(
            "prepare",
            "run",
            "mutate",
            "remove",
            "rebind",
            "scope-source",
            "refresh-observer",
        ),
    )
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--python", type=Path, default=Path(sys.executable))
    parser.add_argument("--source", choices=("daem0n", "legion"), default="daem0n")
    parser.add_argument("--repository", type=Path, default=Path.cwd())
    parser.add_argument("--fixture", type=Path)
    parser.add_argument("--bridge-runtime", type=Path)
    parser.add_argument("--wheel-sha256")
    parser.add_argument("--mode", choices=("code", "direct"), default="code")
    parser.add_argument("--automation", choices=("off", "remind", "enforce"))
    parser.add_argument("--client", type=Path)
    parser.add_argument("--server", type=validate_service_url)
    parser.add_argument("--observation-file", type=Path)
    parser.add_argument("--attempt")
    parser.add_argument("--prompt", type=Path)
    parser.add_argument(
        "--scenario", choices=("initial", "index", "mutate", "remove", "restart")
    )
    parser.add_argument(
        "--ordinary-workflow",
        action="store_true",
        help="Use shared scenario guidance without test index guards or extra gateway templates",
    )
    parser.add_argument("--prior", type=Path)
    parser.add_argument("--indexing-only", action="store_true")
    parser.add_argument(
        "--index-argument-policy", choices=("off", "enforce"), default="off"
    )
    parser.add_argument("--restart-code-only", action="store_true")
    parser.add_argument("--timeout", type=int, default=600)
    args = parser.parse_args()
    if args.action == "prepare":
        prepare(args)
    elif args.action == "run":
        execute(args)
    elif args.action == "rebind":
        rebind(args)
    elif args.action == "scope-source":
        scope_source(args)
    elif args.action == "refresh-observer":
        refresh_observer(args)
    else:
        mutate(args)


if __name__ == "__main__":
    main()
