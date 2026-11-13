"""Audit observed V2 direct-restart calls beside the frozen backend checks.

This checks the declared native event surface, not arbitrary JavaScript or a
shell sandbox. It does not rewrite requests or infer calls absent from events.
"""

from __future__ import annotations

import argparse
import json
from collections import defaultdict
from contextlib import suppress
from pathlib import Path

RESTART_TOOLS = frozenset(
    {
        "memory_recall",
        "memory_store",
        "daem0n_tools_search",
        "daem0n_tool_call",
        "code_search",
        "code_impact_analyze",
        "system_health",
    }
)
# Acceptance bounds, not purported SDK protocol maxima.
MAX_CATALOG_ENTRIES = 256
MAX_METADATA_STRING_CHARS = 16_384


def _json_identity(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _metadata_catalog(result):
    if not isinstance(result, dict):
        return False
    output = result.get("output")
    if not isinstance(output, dict) or set(output) != {"resources", "templates"}:
        return False
    if not all(isinstance(output[key], list) for key in ("resources", "templates")):
        return False
    if len(output["resources"]) + len(output["templates"]) > MAX_CATALOG_ENTRIES:
        return False
    for key, uri_key in (("resources", "uri"), ("templates", "uriTemplate")):
        required = {"server", "name", uri_key}
        for entry in output[key]:
            if (
                not isinstance(entry, dict)
                or not required <= set(entry)
                or set(entry) - required - {"description", "mimeType"}
                or entry["server"] != "daem0nmcp"
                or not all(
                    isinstance(value, str) and len(value) <= MAX_METADATA_STRING_CHARS
                    for value in entry.values()
                )
            ):
                return False
    return True


def _envelopes(value):
    if isinstance(value, dict):
        if value.get("api_version") == "7" and isinstance(value.get("meta"), dict):
            yield value
        else:
            for child in value.values():
                yield from _envelopes(child)
    elif isinstance(value, list):
        for child in value:
            yield from _envelopes(child)
    elif isinstance(value, str):
        try:
            parsed, _ = json.JSONDecoder().raw_decode(value[value.index("{") :])
        except (ValueError, TypeError):
            return
        yield from _envelopes(parsed)


def _key(event):
    if not all(
        isinstance(event.get(k), str) and event[k] for k in ("sessionID", "id", "tool")
    ):
        raise ValueError("Missing native session, call ID or tool")
    if not isinstance(event.get("input"), dict):
        raise ValueError("Native input is not an object")
    return (
        event["sessionID"],
        event["id"],
        event["tool"],
        _json_identity(event["input"]),
    )


def _canonical(event):
    name = event.get("tool", "")
    return name.removeprefix("daem0nmcp_") if name.startswith("daem0nmcp_") else None


def audit_direct_restart(events, backend_validation, *, workspace_id):
    """Fail closed on unknown, denied, unplanned or incomplete observed calls.

    Resource metadata discovery is optional and Daem0n-scoped. Resource content
    reads are outside this restart stage. Expected COUNSEL errors require both
    the unchanged backend predicate and an exact successful native retry.
    """
    failures = []
    calls = []
    pending = defaultdict(list)
    completed = []
    expected_counsel = []
    afters = [(i, e) for i, e in enumerate(events) if e.get("event") == "after"]

    def fail(code, event=None):
        failures.append(
            {
                "code": code,
                "native_call_id": (event or {}).get("id"),
                "tool": (event or {}).get("tool"),
            }
        )

    def counsel_verified(position, event, response):
        error = response.get("error") or {}
        remedy = error.get("remedy") or {}
        if (
            backend_validation.get("checks", {}).get("exact_counsel_remedies")
            is not True
            or response.get("ok") is not False
            or error.get("code") != "COUNSEL_REQUIRED"
            or not error.get("counsel")
            or response.get("meta", {}).get("workspace_id") != workspace_id
            or remedy.get("tool") != _canonical(event)
        ):
            return False
        return any(
            later.get("status") == "completed"
            and later.get("sessionID") == event.get("sessionID")
            and later.get("id") != event.get("id")
            and _canonical(later) == remedy.get("tool")
            and _json_identity(later.get("input"))
            == _json_identity(remedy.get("arguments"))
            and not later.get("error")
            and isinstance(later.get("result"), dict)
            and isinstance(later["result"].get("output"), dict)
            and later["result"]["output"].get("api_version") == "7"
            and later["result"]["output"].get("ok") is True
            and later["result"]["output"].get("meta", {}).get("workspace_id")
            == workspace_id
            for offset, later in afters
            if offset > position
        )

    for position, event in enumerate(events):
        kind = event.get("event")
        if kind in {"setup", "context"}:
            continue
        if kind not in {"before", "after"}:
            fail("UNKNOWN_EVENT_VARIANT", event)
            continue
        try:
            key = _key(event)
        except (ValueError, TypeError):
            fail("INVALID_NATIVE_IDENTITY_OR_INPUT", event)
            continue
        if kind == "before":
            pending[key].append(position)
            continue
        if not pending[key]:
            begin = None
            fail("UNMATCHED_NATIVE_RESULT", event)
        else:
            begin = pending[key].pop(0)
        tool = event["tool"]
        inputs = event["input"]
        canonical = _canonical(event)
        calls.append(
            {"native_call_id": event["id"], "tool": tool, "status": event.get("status")}
        )
        if tool == "execute":
            if not any(
                begin is not None
                and begin < child_begin < child_end < position
                and child_key[2] != "execute"
                and child_key[:2] == key[:2]
                for child_begin, child_end, child_key in completed
            ):
                fail("UNOBSERVED_EXECUTE_INNER_CALL", event)
        elif tool == "opencode_list_mcp_resources":
            if inputs != {"server": "daem0nmcp"}:
                fail("RESOURCE_METADATA_SCOPE", event)
            if event.get("status") == "completed" and not _metadata_catalog(
                event.get("result")
            ):
                fail("RESOURCE_METADATA_RESULT", event)
        elif canonical not in RESTART_TOOLS:
            fail("UNPLANNED_NATIVE_TOOL", event)
        if canonical == "daem0n_tool_call" and inputs.get(
            "tool"
        ) not in RESTART_TOOLS - {"daem0n_tool_call"}:
            fail("UNPLANNED_GATEWAY_TARGET", event)
        if (
            canonical
            and canonical != "daem0n_tools_search"
            and inputs.get("workspace_id") != workspace_id
        ):
            fail("NATIVE_WORKSPACE_SCOPE", event)
        responses = list(_envelopes([event.get("result"), event.get("error")]))
        errors = [r for r in responses if r.get("ok") is False]
        authoritative_error = None
        native_error = event.get("error")
        if (
            event.get("status") == "error"
            and "result" not in event
            and isinstance(native_error, dict)
            and set(native_error) == {"name", "message"}
            and native_error["name"] == "Tool.Error"
            and isinstance(native_error["message"], str)
        ):
            with suppress(ValueError):
                authoritative_error = json.loads(native_error["message"])
        expected = (
            canonical == "memory_store"
            and len(errors) == 1
            and authoritative_error == errors[0]
            and counsel_verified(position, event, errors[0])
        )
        result_error = isinstance(event.get("result"), dict) and (
            event["result"].get("isError") is True
            or isinstance(event["result"].get("output"), dict)
            and event["result"]["output"].get("isError") is True
        )
        if event.get("status") == "error" or errors or result_error or native_error:
            if expected:
                expected_counsel.append(event["id"])
            else:
                fail("NATIVE_ERROR_OR_DENIAL", event)
        elif event.get("status") != "completed" or "result" not in event:
            fail("UNKNOWN_OR_INCOMPLETE_NATIVE_RESULT", event)
        elif canonical and not any(r.get("ok") is True for r in responses):
            fail("MISSING_V7_SUCCESS_ENVELOPE", event)
        if begin is not None:
            completed.append((begin, position, key))
    for key, offsets in pending.items():
        for _ in offsets:
            fail("MISSING_NATIVE_RESULT", {"id": key[1], "tool": key[2]})
    if not calls:
        fail("NO_NATIVE_CALLS")
    return {
        "audit": "OpenCode V2 2.2.7 direct restart observed host surface",
        "status": "failed" if failures else "passed",
        "failures": failures,
        "calls": calls,
        "verified_expected_counsel_call_ids": expected_counsel,
        "backend_checks_unchanged": True,
        "resource_content_reads_authorized": False,
        "limitation": "Observed event audit; not a shell sandbox or proof of unobserved JavaScript behavior",
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--events", type=Path, required=True)
    parser.add_argument("--backend-validation", type=Path, required=True)
    parser.add_argument("--workspace-id", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    events = [json.loads(line) for line in args.events.read_text().splitlines() if line]
    result = audit_direct_restart(
        events,
        json.loads(args.backend_validation.read_text()),
        workspace_id=args.workspace_id,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2) + "\n")
    return 0 if result["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
