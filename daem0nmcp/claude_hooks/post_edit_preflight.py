"""Stage only the actual structured MCP edit_preflight response."""

from __future__ import annotations

import json
import os
import re
import sys
from collections.abc import Mapping
from typing import Any

from ..edit_host import EditHostConfig, EditHostStateStore
from ._client import read_hook_event

_EDIT_PREFLIGHT_TOOL = re.compile(
    r"^(?:edit_preflight|mcp__[^\s]+__edit_preflight|[A-Za-z0-9][A-Za-z0-9_.-]{0,127}_edit_preflight)$"
)
_MAX_ACTUAL_RESPONSE_BYTES = 128 * 1024


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for name, item in pairs:
        if name in value:
            raise ValueError("duplicate JSON member")
        value[name] = item
    return value


def _reject_constant(_value: str) -> None:
    raise ValueError("non-finite JSON number")


def _actual_structured_response(value: object) -> Mapping[str, Any] | None:
    if isinstance(value, str):
        # Claude Code exposes the actual MCP tool result to PostToolUse as a
        # JSON string.  This field is emitted by the native host, rather than
        # copied from assistant-visible text, so decode it strictly here.
        if len(value.encode("utf-8")) > _MAX_ACTUAL_RESPONSE_BYTES:
            return None
        try:
            value = json.loads(
                value,
                object_pairs_hook=_unique_object,
                parse_constant=_reject_constant,
            )
        except (TypeError, ValueError, UnicodeError, json.JSONDecodeError):
            return None
    if not isinstance(value, Mapping):
        return None
    for name in ("structuredContent", "structured_content"):
        structured = value.get(name)
        if isinstance(structured, Mapping):
            return structured
    if {
        "api_version",
        "ok",
        "data",
        "error",
        "meta",
    } <= set(value):
        return value
    return None


def handle_edit_preflight_response(
    event: Mapping[str, Any],
    *,
    environ: Mapping[str, str] | None = None,
) -> bool:
    """Bind a trusted host-observed MCP result to its pending native edit."""

    try:
        native_session_id = event["session_id"]
        project_path = event["cwd"]
        tool_name = event["tool_name"]
        tool_input = event["tool_input"]
        actual_response = _actual_structured_response(event["tool_response"])
        if actual_response is None:
            # Claude Code 2.1.x keeps the model-visible rendering in
            # ``tool_response`` and supplies the original MCP result as
            # host metadata.  Only the structured metadata is eligible for
            # staging; never recover a receipt by parsing the rendered text.
            actual_response = _actual_structured_response(event.get("mcpMeta"))
        if (
            not isinstance(native_session_id, str)
            or not isinstance(project_path, str)
            or not project_path
            or not isinstance(tool_name, str)
            or _EDIT_PREFLIGHT_TOOL.fullmatch(tool_name) is None
            or not isinstance(tool_input, Mapping)
            or actual_response is None
        ):
            return False
        workspace_id = tool_input.get("workspace_id")
        edit_request_id = tool_input.get("edit_request_id")
        if not isinstance(workspace_id, str) or not isinstance(edit_request_id, str):
            return False
        environment = os.environ if environ is None else environ
        config = EditHostConfig.from_environment(environment)
        if config.workspace_id(project_path) != workspace_id:
            return False
        store = EditHostStateStore(config)
        pending = store.get_pending_by_request(
            workspace_id=workspace_id,
            native_session_id=native_session_id,
            credential_id=config.identity.credential_id,
            edit_request_id=edit_request_id,
        )
        if pending is None:
            return False
        response = config.build_client().call(
            "/v1/receipts/stage",
            {
                "workspace_id": workspace_id,
                "host_session_id": pending.host_session_id,
                "tool_name": "edit_preflight",
                "actual_mcp_response": dict(actual_response),
            },
        )
        return response.status == 200 and response.body.get("ok") is True
    except Exception:
        return False


def main() -> None:
    handle_edit_preflight_response(read_hook_event())
    sys.exit(0)


if __name__ == "__main__":
    main()
