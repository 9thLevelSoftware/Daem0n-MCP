"""Retain agy's native large-tool outputs omitted from stream-json frames.

Only an already-completed Daem0n tool event can select its own conversation/step
artifact. Original stream events remain unchanged. No assistant text, transcript
instructions or model-supplied path can become a tool response.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
from collections.abc import Callable
from pathlib import Path

from native_acceptance_scenarios import envelopes, operation

_CONVERSATION = re.compile(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}")
_MAX_OUTPUT_BYTES = 1024 * 1024


def retain_native_outputs(
    events: list[dict],
    home: Path,
    evidence: Path,
    *,
    clean: Callable[[str], str] = lambda value: value,
) -> tuple[list[dict], list[dict]]:
    """Return event copies enriched only from exact native Daem0n step outputs."""
    result = copy.deepcopy(events)
    provenance = []
    home = home.resolve(strict=True)
    for original, enriched in zip(events, result, strict=True):
        step = original.get("step_update", {})
        info = step.get("tool_info", {})
        if (
            original.get("event") != "step_update"
            or step.get("step_type") != "tool"
            or step.get("state") not in {"DONE", "ERROR"}
            or info.get("output") is not None
            or info.get("error") is not None
        ):
            continue
        name = step.get("tool_name", info.get("name"))
        arguments = info.get("parameters")
        if isinstance(arguments, str):
            try:
                arguments = json.loads(arguments)
            except ValueError:
                continue
        if name == "call_mcp_tool":
            if (
                not isinstance(arguments, dict)
                or arguments.get("ServerName") != "daem0nmcp"
            ):
                continue
            name = arguments.get("ToolName")
        elif not isinstance(name, str) or not name.startswith(
            ("mcp__daem0nmcp_", "mcp_daem0nmcp_", "daem0nmcp_")
        ):
            continue
        if operation(name) is None:
            continue
        conversation = step.get("conversation_id")
        index = step.get("step_index")
        if (
            not isinstance(conversation, str)
            or _CONVERSATION.fullmatch(conversation) is None
            or isinstance(index, bool)
            or not isinstance(index, int)
            or not 0 <= index < 100_000
        ):
            continue
        conversation_root = home / ".gemini/antigravity-cli/brain" / conversation
        source = (
            conversation_root / ".system_generated/steps" / str(index) / "output.txt"
        )
        if not source.is_file() or source.is_symlink():
            continue
        canonical_conversation = conversation_root.resolve(strict=True)
        if (
            conversation_root.is_symlink()
            or not canonical_conversation.is_relative_to(home)
            or not source.resolve(strict=True).is_relative_to(canonical_conversation)
            or any(
                parent.is_symlink()
                for parent in source.parents
                if parent != home and home in parent.parents
            )
        ):
            raise RuntimeError(
                "Native agy output artifact escaped its owned conversation"
            )
        if source.stat().st_size > _MAX_OUTPUT_BYTES:
            continue
        raw = source.read_bytes()
        if len(raw) > _MAX_OUTPUT_BYTES:
            continue
        text = raw.decode("utf-8")
        responses = list(envelopes(text))
        if not responses or any(
            not isinstance(response.get("meta", {}).get("request_id"), str)
            for response in responses
        ):
            continue
        target = evidence / "native-host-outputs" / f"{conversation}-{index}.txt"
        target.parent.mkdir(parents=True, exist_ok=True)
        safe_text = clean(text)
        with target.open("x", encoding="utf-8") as stream:
            stream.write(safe_text)
        receipt = {
            "source": "native_agy_completed_step_output",
            "conversation_id": conversation,
            "step_index": index,
            "tool_name": name,
            "native_parameters": json.loads(clean(json.dumps(arguments))),
            "completion_event_sha256": hashlib.sha256(
                json.dumps(original, sort_keys=True).encode()
            ).hexdigest(),
            "native_relative_path": str(source.relative_to(home)),
            "native_output_sha256": hashlib.sha256(raw).hexdigest(),
            "retained_relative_path": str(target.relative_to(evidence)),
            "retained_sha256": hashlib.sha256(target.read_bytes()).hexdigest(),
            "native_output_bytes": len(raw),
            "request_ids": [response["meta"]["request_id"] for response in responses],
        }
        enriched["step_update"]["tool_info"]["output"] = safe_text
        enriched["native_output_provenance"] = receipt
        provenance.append(receipt)
    if provenance:
        target = evidence / "native-host-output-provenance.json"
        with target.open("x", encoding="utf-8") as stream:
            json.dump(provenance, stream, indent=2)
            stream.write("\n")
    return result, provenance
