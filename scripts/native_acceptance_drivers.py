"""Portable native Codex app-server and JSON/RPC client acceptance transports.

The caller selects real executable argv, native cwd and isolated environment.
No model SDK or custom MCP connection replaces the client's native MCP manager.
"""

from __future__ import annotations

import hashlib
import json
import os
import queue
import re
import secrets
import signal
import subprocess
import threading
import time
from contextlib import suppress
from pathlib import Path

from native_acceptance_scenarios import (
    envelopes,
    instruction_evidence,
    native_receipts,
    scenario_prompt,
    validate_receipts,
)


def terminal_capability_error(event, host):
    """Stop a native stage after a real unavailable-capability response."""
    result = None
    if host == "codex":
        item = event.get("params", {}).get("item", {})
        if (
            event.get("method") == "item/completed"
            and item.get("type") == "mcpToolCall"
            and item.get("server") == "daem0nmcp"
        ):
            result = [item.get("result"), item.get("error")]
    elif host in {"omp", "pi"}:
        name = event.get("toolName", "")
        if event.get("type") == "tool_execution_end" and name.startswith(
            "mcp__daem0nmcp_"
        ):
            result = event.get("result")
    elif host == "opencode":
        part = event.get("part", {})
        if event.get("type") == "tool_use" and str(part.get("tool", "")).startswith(
            "daem0nmcp_"
        ):
            state = part.get("state", {})
            result = [state.get("output"), state.get("error")]
    elif host == "agy":
        step = event.get("step_update", {})
        if step.get("step_type") == "tool" and step.get("state") in {"DONE", "ERROR"}:
            info = step.get("tool_info", {})
            result = [info.get("output"), info.get("error")]
    for response in envelopes(result):
        code = (response.get("error") or {}).get("code")
        if response.get("ok") is False and code in {
            "CAPABILITY_DISABLED",
            "CAPABILITY_DEGRADED",
            "AUTHENTICATION_REQUIRED",
            "AUTHENTICATION_FAILED",
        }:
            return code
    return None


def redact(value, sensitive_values=()):
    if isinstance(value, dict):
        return {
            key: "<REDACTED_AUTH>"
            if key.lower().replace("_", "")
            in {
                "accesstoken",
                "refreshtoken",
                "idtoken",
                "apikey",
                "authorization",
                "chatgptaccountid",
                "accountid",
            }
            else redact(item, sensitive_values)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact(item, sensitive_values) for item in value]
    if not isinstance(value, str):
        return value
    for secret in sensitive_values:
        if secret:
            value = value.replace(secret, "<REDACTED_AUTH>")
    value = re.sub(
        r"\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b",
        "<REDACTED_JWT>",
        value,
    )
    return re.sub(
        r"(?:sk-ant-|sk-proj-|sk-)[A-Za-z0-9_-]{12,}|Bearer\s+[^\s\"']+",
        "<REDACTED_AUTH>",
        value,
    )


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes((json.dumps(value, indent=2, default=str) + "\n").encode("utf-8"))


def _retain_stage_authority(evidence, prompt, stage):
    policy = {
        "version": "bounded-native-stage-v2",
        "stage": stage,
        "discovery": "query<=256; optional integer limit1..10; exact unscoped keys",
        "memory_writes": "exact stage seed/replay or outcome; same-workspace native provenance",
        "unknown_requests": "immediate denial; strict workflow failure",
        "eval": "only existing literal dir(tool) inventory; no global namespace/import/shell",
        "native_argument_rewriting": False,
    }
    encoded = json.dumps(policy, sort_keys=True).encode()
    (evidence / "scenario-prompt.txt").write_text(prompt, encoding="utf-8")
    write_json(
        evidence / "stage-authority.json",
        {
            "policy": policy,
            "policy_sha256": hashlib.sha256(encoded).hexdigest(),
            "prompt_sha256": hashlib.sha256(prompt.encode()).hexdigest(),
        },
    )


def _spawn(*arguments, **kwargs):
    if os.name != "nt":
        return subprocess.Popen(*arguments, start_new_session=True, **kwargs)
    from daem0nmcp.api.v7.resource_repository import _WindowsKillOnCloseJob

    process = subprocess.Popen(
        *arguments, creationflags=4, **kwargs
    )  # CREATE_SUSPENDED
    try:
        owner = _WindowsKillOnCloseJob(process)
        process._native_acceptance_job = owner
        owner.resume(process.pid)
    except BaseException:
        process.kill()
        process.wait(timeout=3)
        raise
    return process


def _close_owned_group(process):
    """Retain permission failures as unknown cleanup, without losing receipts."""
    try:
        return _close_owned_group_checked(process)
    except PermissionError as error:
        if os.name == "nt":
            raise
        return {
            "process_group_id": process.pid,
            "process_group_closed": None,
            "native_root_exited": process.poll() is not None,
            "cleanup_signal_error": {
                "type": type(error).__name__,
                "errno": error.errno,
                "message": str(error),
            },
            "cleanup_requires_exact_owned_process_inspection": True,
        }


def _close_owned_group_checked(process):
    """Close the POSIX session created specifically for this native attempt."""
    if os.name == "nt":
        process._native_acceptance_job.close()
        with suppress(subprocess.TimeoutExpired):
            process.wait(timeout=3)
        return {
            "owned_job_closed": True,
            "native_root_exited": process.poll() is not None,
        }
    # start_new_session=True made this exact child the session/group leader.
    pgid = process.pid
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return {"process_group_id": pgid, "process_group_closed": True}
    os.killpg(pgid, signal.SIGTERM)
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        try:
            os.killpg(pgid, 0)
        except ProcessLookupError:
            return {"process_group_id": pgid, "process_group_closed": True}
        time.sleep(0.1)
    with suppress(ProcessLookupError):
        os.killpg(pgid, signal.SIGKILL)
    if process.poll() is None:
        with suppress(subprocess.TimeoutExpired):
            process.wait(timeout=3)
    try:
        os.killpg(pgid, 0)
        closed = False
    except ProcessLookupError:
        closed = True
    return {"process_group_id": pgid, "process_group_closed": closed}


_OMP_CORE_TOOLS = frozenset(
    {
        "session_brief",
        "memory_preflight",
        "memory_recall",
        "memory_store",
        "memory_record_outcome",
        "system_health",
        "edit_preflight",
        "daem0n_tools_search",
        "daem0n_tool_call",
    }
)


class _OMPReadinessError(RuntimeError):
    pass


def _omp_core_inventory(state):
    """Read pinned OMP's native catalog, excluding auto-loaded project text."""
    if not isinstance(state, dict):
        raise _OMPReadinessError("NATIVE_MCP_READINESS_MALFORMED")
    tools, prompt = state.get("dumpTools"), state.get("systemPrompt")
    if (
        not isinstance(tools, list)
        or any(
            not isinstance(tool, dict) or not isinstance(tool.get("name"), str)
            for tool in tools
        )
        or not (
            isinstance(prompt, str)
            or isinstance(prompt, list)
            and all(isinstance(block, str) for block in prompt)
        )
    ):
        raise _OMPReadinessError("NATIVE_MCP_READINESS_MALFORMED")
    direct = {
        tool["name"].removeprefix("mcp__daem0nmcp_")
        for tool in tools
        if tool["name"].startswith("mcp__daem0nmcp_")
        and isinstance(tool.get("parameters"), dict)
        and tool["parameters"].get("type") == "object"
        and isinstance(tool["parameters"].get("properties"), dict)
    }
    if _OMP_CORE_TOOLS.issubset(direct):
        return {"source": "dumpTools", "tools": sorted(_OMP_CORE_TOOLS)}
    for block in [prompt] if isinstance(prompt, str) else prompt:
        if "</project-context>" not in block:
            continue
        # OMP appends its mounted catalog after the final context closure.
        # Names or forged route headings inside AGENTS/source text are not
        # evidence that its asynchronous MCP discovery has completed.
        native = block.rsplit("</project-context>", 1)[1]
        heading = re.search(r"(?m)^## MCP Tool Routes\s*$", native)
        if heading is None:
            continue
        section = native[heading.end() :]
        section = re.split(r"(?m)^## ", section, maxsplit=1)[0]
        pairs = re.findall(
            r'(?m)^- "([a-z0-9_]+)" → `xd://mcp__daem0nmcp_([a-z0-9_]+)`(?: —.*)?$',
            section,
        )
        mounted = {name for name, route in pairs if name == route}
        if _OMP_CORE_TOOLS.issubset(mounted):
            return {
                "source": "native_mcp_routes",
                "tools": sorted(_OMP_CORE_TOOLS),
                "catalog_sha256": hashlib.sha256(section.encode()).hexdigest(),
            }
    return None


def _wait_omp_mcp_ready(process, incoming, record, evidence, *, timeout):
    """Poll supported get_state before sending any model prompt or tool call."""
    started = time.monotonic()
    deadline = started + min(timeout, 120)
    nonce = secrets.token_hex(12)
    requests = 0
    response_id = None
    next_poll = started
    ignored_responses = 0
    status = {"status": "failed", "provider_prompts_submitted": 0, "memory_calls": 0}
    try:
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise _OMPReadinessError("NATIVE_MCP_READINESS_PROCESS_EXIT")
            now = time.monotonic()
            if response_id is None and now >= next_poll:
                response_id = f"daem0n-readiness-{nonce}-{requests}"
                request = {"id": response_id, "type": "get_state"}
                record({"host_sent": request})
                process.stdin.write(json.dumps(request) + "\n")
                process.stdin.flush()
                requests += 1
                next_poll = now + 0.25
            wait = min(0.25, max(0.001, deadline - time.monotonic()))
            if response_id is None:
                wait = min(wait, max(0.001, next_poll - time.monotonic()))
            try:
                event = incoming.get(timeout=wait)
            except queue.Empty:
                continue
            if not isinstance(event, dict):
                raise _OMPReadinessError("NATIVE_MCP_READINESS_MALFORMED")
            if event.get("process_exit"):
                raise _OMPReadinessError("NATIVE_MCP_READINESS_PROCESS_EXIT")
            if event.get("type") == "extension_ui_request" and event.get(
                "method"
            ) not in {
                "notify",
                "setStatus",
                "setWidget",
                "setTitle",
                "set_editor_text",
            }:
                raise _OMPReadinessError("NATIVE_MCP_READINESS_UNEXPECTED_REQUEST")
            if event.get("type") == "error":
                raise _OMPReadinessError("NATIVE_MCP_READINESS_ERROR")
            if response_id is None or event.get("id") != response_id:
                if event.get("type") == "response":
                    ignored_responses += 1
                continue
            if event.get("type") != "response" or event.get("command") != "get_state":
                raise _OMPReadinessError("NATIVE_MCP_READINESS_MALFORMED")
            if event.get("success") is not True:
                raise _OMPReadinessError("NATIVE_MCP_READINESS_ERROR")
            inventory = _omp_core_inventory(event.get("data"))
            response_id = None
            if inventory is not None:
                status = {**status, "status": "passed", "inventory": inventory}
                return status
        raise _OMPReadinessError("NATIVE_MCP_READINESS_TIMEOUT")
    except (BrokenPipeError, OSError) as error:
        status["reason_code"] = "NATIVE_MCP_READINESS_IO_ERROR"
        raise _OMPReadinessError(status["reason_code"]) from error
    except _OMPReadinessError as error:
        status["reason_code"] = str(error)
        raise
    finally:
        status.update(
            requests=requests,
            ignored_responses=ignored_responses,
            elapsed_seconds=time.monotonic() - started,
        )
        write_json(Path(evidence) / "omp-readiness.json", status)


def _bounded_code_approval(request, workspace_id, manifest, events, host="codex"):
    """Authorize one exact planned local indexing call, never a tool family."""
    if request.get("method") != "mcpServer/elicitation/request":
        return None
    params = request.get("params", {})
    metadata = params.get("_meta", {})
    payload = metadata.get("tool_params", {})
    if (
        params.get("serverName") != "daem0nmcp"
        or params.get("mode") != "form"
        or metadata.get("tool_name") != "daem0n_tool_call"
        or set(payload) != {"workspace_id", "tool", "arguments"}
        or payload["workspace_id"] != workspace_id
    ):
        return None
    specification = manifest["scenario"]
    arguments = payload["arguments"]
    if not isinstance(arguments, dict):
        return None
    if payload["tool"] == "code_index" and arguments in [
        {"relative_root": ".", "patterns": specification["patterns"]},
        {
            "relative_root": specification["source_prefix"].rstrip("/"),
            "patterns": specification["patterns"],
        },
    ]:
        return "Exact source root and pattern set from the frozen source manifest"
    queries = {
        specification["search"],
        specification["mutation_search"],
        specification["added_search"],
    }
    if (
        payload["tool"] == "code_search"
        and arguments.get("query") in queries
        and set(arguments) <= {"query", "limit"}
        and type(arguments.get("limit", 100)) is int
        and arguments.get("limit", 100) == 100
    ):
        return "Exact known symbol/probe query from the frozen source manifest"
    if (
        payload["tool"] == "code_impact_analyze"
        and set(arguments) == {"code_entity_id", "max_depth"}
        and type(arguments["max_depth"]) is int
        and arguments["max_depth"] == 2
    ):
        for receipt in native_receipts(events, host):
            if (
                receipt["operation"] != "code_search"
                or receipt["arguments"].get("workspace_id") != workspace_id
            ):
                continue
            for response in receipt["v7_responses"]:
                if (
                    response.get("ok") is not True
                    or response.get("meta", {}).get("workspace_id") != workspace_id
                ):
                    continue
                data = response.get("data") or {}
                if receipt["canonical_tool"] == "daem0n_tool_call":
                    if data.get("tool") != "code_search":
                        continue
                    data = data.get("data") or {}
                if any(
                    item.get("code_entity_id") == arguments["code_entity_id"]
                    and item.get("relative_file_path") == specification["subject_path"]
                    and item.get("qualified_name", "").endswith(specification["search"])
                    for item in data.get("items", [])
                ):
                    return (
                        "Exact code entity from a preceding actual native search result"
                    )
    return None


def _bounded_omp_approval(event, workspace_id, manifest, events, *, stage=None):
    """Match OMP's actual one-shot frame to this fixture's bounded operations."""
    if event.get("method") != "select" or "Approve" not in event.get("options", []):
        return None
    if (
        event.get("title")
        == "Allow tool: eval\nLanguage: python\nCode:\nprint([n for n in dir(tool)])"
    ):
        return "Exact read-only native tool namespace inventory expression"
    match = re.fullmatch(
        r"Allow tool: write\nPath: xd://(mcp__daem0nmcp_[a-z0-9_]+)\nArguments:\n(\{.*\})",
        event.get("title", ""),
        re.DOTALL,
    )
    if not match:
        return None
    name, encoded = match.groups()
    try:
        payload = json.loads(encoded)
    except ValueError:
        return None
    if not isinstance(payload, dict):
        return None
    tool = name.removeprefix("mcp__daem0nmcp_")
    if tool == "daem0n_tool_call":
        return _bounded_code_approval(
            {
                "method": "mcpServer/elicitation/request",
                "params": {
                    "serverName": "daem0nmcp",
                    "mode": "form",
                    "_meta": {"tool_name": tool, "tool_params": payload},
                },
            },
            workspace_id,
            manifest,
            events,
            host="omp",
        )
    nonce = f"native-followup-omp-{manifest['project']}-20261112"
    scope = {"workspace_id": workspace_id}
    if (
        tool == "memory_recall"
        and set(payload) == {"workspace_id", "query", "limit"}
        and payload["workspace_id"] == workspace_id
        and isinstance(payload["query"], str)
        and 0 < len(payload["query"]) <= 256
        and payload["query"].strip()
        and type(payload["limit"]) is int
        and 1 <= payload["limit"] <= 10
    ):
        return (
            "Bounded read-only recall in the exact disposable workspace; one call only"
        )
    expected = {
        "session_brief": scope,
        "system_health": scope,
        "memory_recall": {**scope, "query": nonce, "limit": 5},
    }
    seed = {
        **scope,
        "record_type": "decision",
        "content": f"{nonce}: preserve real-source indexing and native client setup",
        "rationale": "Native MCP persistence and same-key replay acceptance",
        "idempotency_key": nonce + "-decision",
    }
    planned_writes = {}
    if stage in {"initial", "restart"}:
        planned_writes["memory_store"] = seed
    if stage == "index" and manifest.get("pending_outcome"):
        pending = manifest["pending_outcome"]
        if (
            set(pending)
            == {
                "workspace_id",
                "record_id",
                "outcome_text",
                "worked",
                "idempotency_key",
            }
            and pending["workspace_id"] == workspace_id
            and re.fullmatch(r"mem_[0-9a-f]{64}", pending.get("record_id", ""))
            and pending["outcome_text"] == nonce + ": native persistence seed verified"
            and pending["worked"] is True
            and pending["idempotency_key"] == nonce + "-outcome"
        ):
            planned_writes["memory_record_outcome"] = pending
    for observed in native_receipts(events, "omp"):
        if (
            stage != "initial"
            or observed["operation"] != "memory_store"
            or {
                k: v for k, v in observed["arguments"].items() if k != "challenge_token"
            }
            != seed
        ):
            continue
        for response in observed["v7_responses"]:
            if (
                response.get("ok") is True
                and response.get("meta", {}).get("workspace_id") == workspace_id
            ):
                record_id = (
                    (response.get("data") or {}).get("record", {}).get("record_id")
                )
                if isinstance(record_id, str) and re.fullmatch(
                    r"mem_[0-9a-f]{64}", record_id
                ):
                    planned_writes["memory_record_outcome"] = {
                        **scope,
                        "record_id": record_id,
                        "outcome_text": nonce + ": native persistence seed verified",
                        "worked": True,
                        "idempotency_key": nonce + "-outcome",
                    }
    if tool in planned_writes and payload == planned_writes[tool]:
        return "Exact memory write authorized for this stage and observed record"
    if tool in expected and payload == expected[tool]:
        return "Exact fixture-scoped core operation and planned payload"
    if tool == "system_health" and payload == {**scope, "include_components": True}:
        return "Exact fixture-scoped read-only component health diagnostic"
    for receipt in native_receipts(events, "omp"):
        if receipt["arguments"].get("workspace_id") != workspace_id:
            continue
        base = {k: v for k, v in receipt["arguments"].items() if k != "challenge_token"}
        if (
            receipt["canonical_tool"] not in planned_writes
            or base != planned_writes[receipt["canonical_tool"]]
        ):
            continue
        for response in receipt["v7_responses"]:
            error = response.get("error") or {}
            remedy = error.get("remedy") or {}
            if (
                error.get("code") == "COUNSEL_REQUIRED"
                and response.get("meta", {}).get("workspace_id") == workspace_id
                and remedy.get("tool") in {tool, name}
                and receipt["canonical_tool"] == tool
                and remedy.get("arguments") == payload
                and payload.get("workspace_id") == workspace_id
                and {
                    key: value
                    for key, value in payload.items()
                    if key != "challenge_token"
                }
                == planned_writes[tool]
            ):
                return (
                    "Exact same-workspace counsel remedy for a stage-authorized write"
                )
    if (
        tool == "daem0n_tools_search"
        and set(payload) <= {"query", "limit"}
        and isinstance(payload.get("query"), str)
        and payload["query"].strip()
        and len(payload["query"]) <= 256
        and type(payload.get("limit", 10)) is int
        and 1 <= payload.get("limit", 10) <= 10
    ):
        return "Bounded unscoped read-only native tool discovery; one call only"

    return None


def _decision(
    evidence,
    request,
    choices,
    timeout,
    validated_reason=None,
    *,
    unknown_policy="manual",
):
    if unknown_policy not in {"manual", "deny"}:
        raise ValueError("Unknown native approval policy")
    digest = hashlib.sha256(json.dumps(request, sort_keys=True).encode()).hexdigest()
    slug = digest[:20]
    path = evidence / "approvals" / "requests" / f"{slug}.json"
    response_path = evidence / "approvals" / "responses" / f"{slug}.json"
    write_json(
        path,
        {
            "native_request": redact(request),
            "request_sha256": digest,
            "choices": choices,
            "response_path": str(response_path),
        },
    )
    affirmative = "accept" if "accept" in choices else "Approve"
    if validated_reason is not None and affirmative in choices:
        write_json(
            response_path,
            {
                "request_sha256": digest,
                "choice": affirmative,
                "reviewer": "Bounded test coordinator policy for this exact native request",
                "validated_reason": validated_reason,
                "grant_scope": "one call; no persistence",
            },
        )
    elif unknown_policy == "deny":
        negative = next(
            (choice for choice in ("Deny", "decline", "cancel") if choice in choices),
            None,
        )
        if negative is None:
            raise RuntimeError("Native decision surface has no safe denial choice")
        write_json(
            response_path,
            {
                "request_sha256": digest,
                "choice": negative,
                "reason": "Outside the bounded stage authority; no permission granted",
                "reviewer": "Explicit fail-closed native acceptance policy",
                "grant_scope": "none",
            },
        )
    else:
        print(json.dumps({"pending_native_approval": str(path)}), flush=True)
    deadline = time.monotonic() + timeout
    while not response_path.exists() and time.monotonic() < deadline:
        time.sleep(0.2)
    if not response_path.exists():
        raise TimeoutError("Exact native approval decision not supplied")
    answer = json.loads(response_path.read_text(encoding="utf-8-sig"))
    if answer.get("request_sha256") != digest or answer.get("choice") not in choices:
        raise RuntimeError("Native approval decision does not match this exact request")
    write_json(evidence / "approvals" / f"{slug}.receipt.json", answer)
    return answer["choice"]


def retain_approval_denials(evidence, validation):
    denied = []
    for path in (Path(evidence) / "approvals").glob("*.receipt.json"):
        decision = json.loads(path.read_text(encoding="utf-8-sig"))
        if decision.get("choice") in {"Deny", "decline", "cancel"}:
            denied.append(
                {
                    "receipt": str(path),
                    "request_sha256": decision.get("request_sha256"),
                    "reason": decision.get("reason"),
                }
            )
    if denied:
        validation["native_approval_denials"] = denied
        validation["checks"]["no_native_denied_calls"] = False
        validation["status"] = "failed"


def _finish(host, events, evidence, stage, manifest, prior, summary, workspace_id):
    observed = native_receipts(events, host)
    (evidence / "native-tool-receipts.jsonl").write_text(
        "".join(json.dumps(item) + "\n" for item in observed), encoding="utf-8"
    )
    validation = validate_receipts(
        observed, stage, manifest, prior, workspace_id=workspace_id
    )
    retain_approval_denials(evidence, validation)
    if stage == "initial":
        loading = instruction_evidence(events, host, manifest)
        summary["instruction_loading"] = loading
        if loading["status"] != "not_requested":
            validation["checks"]["preloaded_instruction_marker_reported"] = (
                loading["status"] == "passed"
            )
            if loading["status"] != "passed":
                validation["status"] = "failed"
    if summary.get("exit_code") not in (None, 0) or summary.get("timed_out"):
        validation["status"] = "failed"
    summary["validation"] = validation
    write_json(evidence / "summary.json", summary)
    return summary


def run_stream(
    *,
    host,
    argv,
    cwd,
    env,
    workspace_id,
    manifest,
    evidence,
    stage,
    prior=None,
    sensitive_values=(),
    timeout=900,
    omp_rpc=False,
    prompt_text=None,
):
    """Run a native JSON client; OMP RPC approvals remain individual decisions."""
    evidence = Path(evidence)
    evidence.mkdir(parents=True, exist_ok=False)
    prompt_text = prompt_text or scenario_prompt(host, stage, workspace_id, manifest)
    _retain_stage_authority(evidence, prompt_text, stage)
    write_json(
        evidence / "request.json",
        {
            "host": host,
            "argv": argv,
            "cwd": str(cwd),
            "stage": stage,
            "workspace_id": workspace_id,
            "native_mcp": True,
        },
    )
    process = _spawn(
        argv,
        cwd=cwd,
        env=env,
        stdin=subprocess.PIPE if omp_rpc else subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
    )
    write_json(
        evidence / "owned-process.json",
        {"root_pid": process.pid, "started_at_unix": time.time(), "argv": argv},
    )
    incoming = queue.Queue()
    events = []
    stderr_lines = []
    terminal_error = []
    stop_stage = threading.Event()
    lock = threading.Lock()

    def read_stdout():
        with (evidence / "events.jsonl").open("w", encoding="utf-8") as stream:
            for line in process.stdout:
                try:
                    event = redact(json.loads(line), sensitive_values)
                except ValueError:
                    stream.write(
                        json.dumps(
                            {"non_protocol_stdout": redact(line, sensitive_values)}
                        )
                        + "\n"
                    )
                    stream.flush()
                    continue
                with lock:
                    events.append(event)
                    stream.write(json.dumps(event) + "\n")
                    stream.flush()
                incoming.put(event)
                code = terminal_capability_error(event, host)
                if code and not terminal_error:
                    terminal_error.append(code)
                    stop_stage.set()
        incoming.put({"process_exit": True})

    def read_stderr():
        with (evidence / "stderr.log").open("w", encoding="utf-8") as stream:
            for line in process.stderr:
                safe = redact(line, sensitive_values)
                stderr_lines.append(safe)
                stream.write(safe)
                stream.flush()

    out = threading.Thread(target=read_stdout, daemon=True)
    err = threading.Thread(target=read_stderr, daemon=True)
    out.start()
    err.start()
    started = time.monotonic()
    timed_out = False
    complete = False

    def record_startup(item):
        with (evidence / "omp-readiness-commands.jsonl").open(
            "a", encoding="utf-8"
        ) as log:
            log.write(json.dumps(redact(item, sensitive_values)) + "\n")

    try:
        if omp_rpc:
            _wait_omp_mcp_ready(
                process, incoming, record_startup, evidence, timeout=timeout
            )
            process.stdin.write(
                json.dumps(
                    {
                        "id": "native-acceptance-prompt",
                        "type": "prompt",
                        "message": prompt_text,
                    }
                )
                + "\n"
            )
            process.stdin.flush()
            while time.monotonic() - started < timeout and not stop_stage.is_set():
                try:
                    event = incoming.get(timeout=10)
                except queue.Empty:
                    continue
                if event.get("process_exit"):
                    break
                if event.get("type") == "extension_ui_request":
                    if event.get("method") in {
                        "notify",
                        "setStatus",
                        "setWidget",
                        "setTitle",
                        "set_editor_text",
                    }:
                        continue
                    if event.get("method") != "select" or not isinstance(
                        event.get("options"), list
                    ):
                        raise RuntimeError("Unexpected native OMP decision surface")
                    choice = _decision(
                        evidence,
                        event,
                        [c for c in ("Approve", "Deny") if c in event["options"]],
                        min(timeout, 600),
                        _bounded_omp_approval(
                            event, workspace_id, manifest, events, stage=stage
                        ),
                        unknown_policy="deny",
                    )
                    process.stdin.write(
                        json.dumps(
                            {
                                "type": "extension_ui_response",
                                "id": event["id"],
                                "value": choice,
                            }
                        )
                        + "\n"
                    )
                    process.stdin.flush()
                    if choice == "Deny":
                        terminal_error.append("NATIVE_APPROVAL_DENIED")
                        stop_stage.set()
                        break
                if event.get("type") in {"agent_end", "agent_settled"}:
                    complete = True
                    break
            timed_out = not complete and process.poll() is None
            if process.poll() is None and not timed_out:
                process.stdin.close()
        while (
            process.poll() is None
            and not stop_stage.is_set()
            and time.monotonic() - started < timeout
        ):
            time.sleep(0.1)
        if stop_stage.is_set():
            _close_owned_group(process)
        else:
            process.wait(timeout=max(1, timeout - (time.monotonic() - started)))
    except _OMPReadinessError as error:
        terminal_error.append(str(error))
        stop_stage.set()
    except subprocess.TimeoutExpired:
        timed_out = True
    finally:
        cleanup = _close_owned_group(process)
        if process.poll() is not None:
            out.join(timeout=10)
            err.join(timeout=10)
    supplements = []
    if host == "agy":
        from native_agy_evidence import retain_native_outputs

        events, supplements = retain_native_outputs(
            events,
            Path(env["HOME"]),
            evidence,
            clean=lambda text: redact(text, sensitive_values),
        )
    return _finish(
        host,
        events,
        evidence,
        stage,
        manifest,
        prior,
        {
            "host": host,
            "stage": stage,
            "native_cli_pid": process.pid,
            "exit_code": process.poll(),
            "timed_out": timed_out,
            "terminal_capability_error": terminal_error[0] if terminal_error else None,
            "elapsed_seconds": time.monotonic() - started,
            "cleanup_required_owned_pid": process.pid
            if process.poll() is None
            else None,
            "native_event_count": len(events),
            "native_output_supplements": len(supplements),
            "native_completion": complete if omp_rpc else process.poll() == 0,
            **cleanup,
        },
        workspace_id,
    )


def validate_resource_change(receipts, evidence, expected_store):
    """Require an actual exact write and a record delta on the identical URI."""
    expected_uri = f"memory://workspaces/{expected_store['workspace_id']}/warnings"

    def documents(result):
        result = result or {}
        return [
            json.loads(content["text"])
            for content in result.get("contents", [])
            if content.get("uri") == expected_uri
            and isinstance(content.get("text"), str)
        ]

    before = documents((evidence or {}).get("before"))
    after = documents((evidence or {}).get("after"))
    stores = [
        (receipt, response)
        for receipt in receipts
        if receipt["operation"] == "memory_store"
        for response in receipt["v7_responses"]
        if response.get("ok") is True
    ]
    actual_ids = [
        (response.get("data", {}).get("record") or {}).get("record_id")
        for _, response in stores
    ]
    record_id = actual_ids[0] if len(actual_ids) == 1 else None

    def ids(docs):
        return {row.get("record_id") for doc in docs for row in doc.get("items", [])}

    checks = {
        "same_native_session": bool((evidence or {}).get("thread_id"))
        and all(row.get("native_session") == evidence["thread_id"] for row in receipts),
        "same_native_uri_before_after": (evidence or {}).get("uri") == expected_uri
        and bool(before)
        and bool(after),
        "workspace_scope": bool(before)
        and bool(after)
        and all(
            doc.get("workspace_id") == expected_store["workspace_id"]
            for doc in before + after
        ),
        "native_brief_before_write": bool(stores)
        and any(
            row["operation"] == "session_brief"
            and any(value.get("ok") is True for value in row["v7_responses"])
            for row in receipts[: receipts.index(stores[0][0])]
        ),
        "exact_single_native_warning_write": len(stores) == 1
        and stores[0][0]["arguments"] == expected_store,
        "record_absent_before_present_after": bool(record_id)
        and record_id not in ids(before)
        and record_id in ids(after),
        "only_planned_native_operations": all(
            row["operation"] in {"session_brief", "memory_store", "memory_preflight"}
            for row in receipts
        ),
        "no_native_errors": not any(
            response.get("ok") is False
            and (response.get("error") or {}).get("code") != "COUNSEL_REQUIRED"
            for row in receipts
            for response in row["v7_responses"]
        ),
    }
    return {
        "status": "passed" if all(checks.values()) else "failed",
        "stage": "resource-change",
        "checks": checks,
        "record_id": record_id,
        "uri": expected_uri,
        "native_receipt_count": len(receipts),
    }


def run_codex(
    *,
    argv,
    cwd,
    env,
    workspace_id,
    manifest,
    evidence,
    stage,
    model="gpt-6.1-sol",
    access_token=None,
    account_id=None,
    prior=None,
    timeout=900,
    resource_delta=None,
):
    """Use native app-server; external ChatGPT auth supplies access material only."""
    evidence = Path(evidence)
    evidence.mkdir(parents=True, exist_ok=False)
    write_json(
        evidence / "request.json",
        {
            "host": "codex",
            "argv": argv,
            "cwd": str(cwd),
            "stage": stage,
            "model": model,
            "workspace_id": workspace_id,
            "auth": "external access-only" if access_token else "native existing login",
            "resource_delta": resource_delta,
        },
    )
    process = _spawn(
        argv,
        cwd=cwd,
        env=env,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
    )
    write_json(
        evidence / "owned-process.json",
        {"root_pid": process.pid, "started_at_unix": time.time(), "argv": argv},
    )
    messages = queue.Queue()
    events = []
    counter = 0
    started = time.monotonic()
    log_lock = threading.Lock()
    secrets = tuple(value for value in (access_token, account_id) if value)

    def record(item):
        safe = redact(item, secrets)
        with log_lock, (evidence / "events.jsonl").open("a", encoding="utf-8") as log:
            log.write(json.dumps(safe) + "\n")
        events.append(safe)

    def read_stdout():
        for line in process.stdout:
            try:
                item = json.loads(line)
            except ValueError:
                record({"non_protocol_stdout": line})
                continue
            record(item)
            messages.put(item)
        messages.put({"process_exit": True})

    def read_stderr():
        with (evidence / "stderr.log").open("w", encoding="utf-8") as log:
            for line in process.stderr:
                log.write(redact(line, secrets))
                log.flush()

    out = threading.Thread(target=read_stdout, daemon=True)
    err = threading.Thread(target=read_stderr, daemon=True)
    out.start()
    err.start()

    def send(item):
        record({"host_sent": item})
        process.stdin.write(json.dumps(item) + "\n")
        process.stdin.flush()

    def receive():
        while time.monotonic() - started < timeout:
            try:
                item = messages.get(timeout=10)
            except queue.Empty:
                continue
            if item.get("process_exit"):
                raise RuntimeError("Native Codex exited before completion")
            if "id" in item and "method" in item:
                method = item["method"]
                if method == "account/chatgptAuthTokens/refresh":
                    send(
                        {
                            "id": item["id"],
                            "error": {
                                "code": -32000,
                                "message": "No refresh material in acceptance host",
                            },
                        }
                    )
                    raise RuntimeError("Native Codex requested authentication refresh")
                if method == "mcpServer/elicitation/request":
                    reason = None
                    if resource_delta is None:
                        reason = _bounded_code_approval(
                            item, workspace_id, manifest, events
                        )
                    else:
                        metadata = item.get("params", {}).get("_meta", {})
                        if (
                            item.get("params", {}).get("serverName") == "daem0nmcp"
                            and metadata.get("tool_name") == "memory_store"
                            and metadata.get("tool_params") == resource_delta["store"]
                        ):
                            reason = "Exact single synthetic warning payload for this resource-change probe"
                    choice = _decision(
                        evidence,
                        item,
                        ["accept", "decline", "cancel"],
                        min(timeout, 600),
                        reason,
                        unknown_policy="deny",
                    )
                    send(
                        {"id": item["id"], "result": {"action": choice, "content": {}}}
                    )
                elif method in {
                    "item/commandExecution/requestApproval",
                    "item/fileChange/requestApproval",
                }:
                    send({"id": item["id"], "result": {"decision": "decline"}})
                else:
                    send(
                        {
                            "id": item["id"],
                            "error": {
                                "code": -32601,
                                "message": "Native request unsupported by bounded test host",
                            },
                        }
                    )
            code = terminal_capability_error(item, "codex")
            if code:
                raise RuntimeError("Native capability unavailable: " + code)
            return item
        raise TimeoutError("Native Codex turn exceeded acceptance deadline")

    def call(method, params, *, allow_error=False):
        nonlocal counter
        counter += 1
        identifier = counter
        send({"id": identifier, "method": method, "params": params})
        while True:
            item = receive()
            if item.get("id") == identifier and "method" not in item:
                if item.get("error") and allow_error:
                    return {"native_rpc_error": item["error"]}
                if item.get("error"):
                    raise RuntimeError("Native Codex request failed: " + method)
                return item.get("result")

    completion = None
    error_type = None
    resources = {"status": "not_exercised"}
    delta_evidence = None
    try:
        call(
            "initialize",
            {
                "clientInfo": {"name": "daem0n-native-followup", "version": "1"},
                "capabilities": {"experimentalApi": True},
            },
        )
        send({"method": "initialized"})
        if access_token:
            call(
                "account/login/start",
                {
                    "type": "chatgptAuthTokens",
                    "accessToken": access_token,
                    "chatgptAccountId": account_id,
                },
            )
        thread = call(
            "thread/start",
            {
                "cwd": str(cwd),
                "model": model,
                "modelProvider": "openai",
                "approvalPolicy": "untrusted",
                "sandbox": "read-only",
                "ephemeral": True,
                "config": {"model_reasoning_effort": "low"},
            },
        )
        ready_deadline = time.monotonic() + min(120, timeout)
        while True:
            before_turn = call(
                "mcpServerStatus/list",
                {
                    "threadId": thread["thread"]["id"],
                    "serverName": "daem0nmcp",
                    "detail": "full",
                    "limit": 100,
                },
                allow_error=True,
            )
            daemon = next(
                (
                    server
                    for server in before_turn.get("data", [])
                    if server.get("name") == "daem0nmcp"
                ),
                {},
            )
            if daemon.get("runtimeStatus") == "connected" and daemon.get("tools"):
                break
            if (
                daemon.get("runtimeStatus")
                in {"failed", "authenticationRequired", "disabled", "cancelled"}
                or time.monotonic() >= ready_deadline
            ):
                raise RuntimeError(
                    "Native Daem0n MCP inventory unavailable before model turn"
                )
            time.sleep(0.2)
        write_json(
            evidence / "pre-turn-native-inventory.json", redact(before_turn, secrets)
        )
        call(
            "turn/start",
            {
                "threadId": thread["thread"]["id"],
                "input": [
                    {
                        "type": "text",
                        "text": (
                            "Use only the actual native Daem0n MCP tool session_brief. "
                            "Do not use filesystem, shell, networking or other MCP tools. "
                            "Call session_brief with these exact arguments, then stop: "
                            + json.dumps({"workspace_id": workspace_id})
                            if resource_delta
                            else scenario_prompt("codex", stage, workspace_id, manifest)
                        ),
                    }
                ],
                "effort": "low",
            },
        )
        while completion is None:
            item = receive()
            if item.get("method") == "turn/completed":
                completion = item.get("params")
        thread_id = thread["thread"]["id"]
        if resource_delta:
            uri = f"memory://workspaces/{workspace_id}/warnings"
            read_arguments = {"threadId": thread_id, "server": "daem0nmcp", "uri": uri}
            before = call("mcpServer/resource/read", read_arguments)
            delta_evidence = {"thread_id": thread_id, "uri": uri, "before": before}
            write_json(
                evidence / "resource-change-before.json",
                redact(delta_evidence, secrets),
            )
            completion = None
            call(
                "turn/start",
                {
                    "threadId": thread_id,
                    "input": [
                        {
                            "type": "text",
                            "text": (
                                "This is a bounded resource-refresh check. Use only the actual native Daem0n "
                                "memory_store MCP tool to create this one synthetic warning. Do not repeat "
                                "session_brief, recall, indexing or other operations. Do not use filesystem "
                                "or shell tools. Call memory_store with these exact JSON arguments then stop: "
                                + json.dumps(resource_delta["store"])
                                + ". If COUNSEL_REQUIRED occurs, read actual counsel and retry exactly its "
                                "returned remedy. Report the actual returned record_id only."
                            ),
                        }
                    ],
                    "effort": "low",
                },
            )
            while completion is None:
                item = receive()
                if item.get("method") == "turn/completed":
                    completion = item.get("params")
            delta_evidence["after"] = call("mcpServer/resource/read", read_arguments)
            write_json(
                evidence / "resource-change.json", redact(delta_evidence, secrets)
            )
        inventory = call(
            "mcpServerStatus/list",
            {
                "threadId": thread_id,
                "serverName": "daem0nmcp",
                "detail": "full",
                "limit": 100,
            },
            allow_error=True,
        )
        resources = {
            "native_inventory": inventory,
            "reads": [],
            "status": "unsupported",
        }
        if "native_rpc_error" not in inventory:
            for kind in ("warnings", "failures", "rules", "active-context"):
                uri = f"memory://workspaces/{workspace_id}/{kind}"
                result = call(
                    "mcpServer/resource/read",
                    {"threadId": thread_id, "server": "daem0nmcp", "uri": uri},
                    allow_error=True,
                )
                documents = []
                for content in result.get("contents", []):
                    if isinstance(content.get("text"), str):
                        with suppress(ValueError):
                            documents.append(json.loads(content["text"]))
                resources["reads"].append(
                    {
                        "uri": uri,
                        "native_response": result,
                        "workspace_matches": bool(documents)
                        and all(
                            doc.get("workspace_id") == workspace_id for doc in documents
                        ),
                    }
                )
            foreign = "ws_" + "0" * 24
            foreign_response = call(
                "mcpServer/resource/read",
                {
                    "threadId": thread_id,
                    "server": "daem0nmcp",
                    "uri": f"memory://workspaces/{foreign}/warnings",
                },
                allow_error=True,
            )
            resources["foreign_workspace_denied"] = bool(
                foreign_response.get("native_rpc_error")
            )
            resources["foreign_native_response"] = foreign_response
            resources["reread_native_response"] = call(
                "mcpServer/resource/read",
                {
                    "threadId": thread_id,
                    "server": "daem0nmcp",
                    "uri": f"memory://workspaces/{workspace_id}/warnings",
                },
                allow_error=True,
            )
            resources["status"] = (
                "passed"
                if all(row["workspace_matches"] for row in resources["reads"])
                and resources["foreign_workspace_denied"]
                else "failed"
            )
            resources["refresh_boundary"] = (
                "Changed-content read retained in resource-change.json; subscription support is reported by actual native server capabilities"
                if resource_delta
                else "Repeated native read retained; changed-content subscription refresh is a separate gate"
            )
        write_json(evidence / "native-resources.json", redact(resources, secrets))
    except (RuntimeError, TimeoutError) as exc:
        error_type = type(exc).__name__
        record({"driver_error_type": error_type, "reason": str(exc)})
    finally:
        if process.poll() is None:
            process.stdin.close()
            with suppress(subprocess.TimeoutExpired):
                process.wait(timeout=30)
        if process.poll() is not None:
            out.join(timeout=10)
            err.join(timeout=10)
        cleanup = _close_owned_group(process)
    if resource_delta:
        observed = native_receipts(events, "codex")
        (evidence / "native-tool-receipts.jsonl").write_text(
            "".join(json.dumps(row) + "\n" for row in observed), encoding="utf-8"
        )
        validation = validate_resource_change(
            observed, delta_evidence, resource_delta["store"]
        )
        if error_type or completion is None or process.poll() != 0:
            validation["status"] = "failed"
        summary = {
            "host": "codex",
            "stage": "resource-change",
            "validation": validation,
            "driver_error_type": error_type,
            "native_cli_pid": process.pid,
            "exit_code": process.poll(),
            "completion": redact(completion, secrets),
            "elapsed_seconds": time.monotonic() - started,
            **cleanup,
        }
        write_json(evidence / "summary.json", summary)
        return summary
    summary = _finish(
        "codex",
        events,
        evidence,
        stage,
        manifest,
        prior,
        {
            "host": "codex",
            "stage": stage,
            "native_cli_pid": process.pid,
            "exit_code": process.poll(),
            "elapsed_seconds": time.monotonic() - started,
            "completion": redact(completion, secrets),
            "driver_error_type": error_type,
            "native_resources": {"status": resources["status"]},
            "cleanup_required_owned_pid": process.pid
            if process.poll() is None
            else None,
            **cleanup,
        },
        workspace_id,
    )
    if error_type or completion is None:
        summary["validation"]["status"] = "failed"
        write_json(evidence / "summary.json", summary)
    return summary
