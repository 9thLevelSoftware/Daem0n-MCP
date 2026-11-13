"""Capture a successful approved native edit without transcript content."""

from __future__ import annotations

import os
import sys
from collections.abc import Mapping
from typing import Any

from ._client import find_project_root, read_hook_event

# Pairing writes this into the project's Claude settings; without it there is
# no edit bridge, so the heavy bridge modules are never imported.
_CREDENTIAL_ENV = "DAEM0NMCP_EDIT_BRIDGE_CREDENTIAL_FILE"


def _ok(response: object) -> bool:
    body = getattr(response, "body", None)
    return isinstance(body, Mapping) and body.get("ok") is True


def _completion_outcome(event: Mapping[str, Any]) -> str | None:
    """Recognize terminal host events without treating missing results as success."""
    event_name = event.get("hook_event_name")
    if event_name not in ("PostToolUse", "PostToolUseFailure"):
        return None
    status = event.get("execution_status")
    if status in ("failed", "skipped", "cancelled", "denied"):
        return "failed"
    if event_name == "PostToolUseFailure":
        return "failed"
    if status is not None and status != "succeeded":
        return None
    response = event.get("tool_response")
    if response is None:
        return None
    if isinstance(response, Mapping):
        if response.get("isError") is True or response.get("success") is False:
            return "failed"
        if response.get("status") in (
            "error",
            "failed",
            "skipped",
            "cancelled",
            "denied",
        ):
            return "failed"
    return "succeeded"


def handle_post_edit(event: Mapping[str, Any], project_path: str) -> bool:
    """Stage only a bounded host-generated candidate for a consumed request."""
    outcome = _completion_outcome(event)
    if outcome is None:
        return False
    try:
        from ..edit_host import (
            EditHostConfig,
            EditHostStateStore,
            native_edit_capture_body,
        )
        from .native_edit import (
            configured_native_edit_tools,
            native_edit_relative_paths,
        )

        native_session_id = event["session_id"]
        native_request_id = event["tool_use_id"]
        tool_name = event["tool_name"]
        tool_input = event["tool_input"]
        if not all(
            isinstance(value, str) and value
            for value in (native_session_id, native_request_id, tool_name)
        ) or not isinstance(tool_input, Mapping):
            return False
        config = EditHostConfig.from_environment(os.environ)
        project = find_project_root(dict(event)) if config.mode == "local" else None
        if project is not None:
            project_path = str(project)
        workspace_id = config.workspace_id(project_path)
        store = EditHostStateStore(config)
        consumed = store.get_consumed(
            workspace_id=workspace_id,
            native_session_id=native_session_id,
            credential_id=config.identity.credential_id,
            native_request_id=native_request_id,
        )
        if consumed is None:
            return False
        from ..native_edit_gate import native_input_hash

        if consumed.native_input_hash is not None:
            if consumed.native_input_hash != native_input_hash(tool_name, tool_input):
                return False
        elif os.environ.get("DAEM0NMCP_AUTOMATION_MODE") == "enforce":
            return False
        if outcome == "failed":
            if consumed.native_input_hash is not None:
                store.settle_failed(
                    workspace_id=workspace_id,
                    native_session_id=native_session_id,
                    credential_id=config.identity.credential_id,
                    native_request_id=native_request_id,
                    edit_request_id=consumed.edit_request_id,
                    native_input_hash=consumed.native_input_hash,
                )
            return False
        relative_paths = native_edit_relative_paths(
            project_path=project_path,
            tool_name=tool_name,
            tool_input=tool_input,
            configured_tools=configured_native_edit_tools(os.environ),
        )
        body = native_edit_capture_body(
            workspace_id=workspace_id,
            host_session_id=consumed.host_session_id,
            edit_request_id=consumed.edit_request_id,
            tool_name=tool_name,
            relative_paths=relative_paths,
            result="succeeded",
        )
        if not _ok(config.build_client().call("/v1/captures", body)):
            return False
        return store.mark_captured(
            workspace_id=workspace_id,
            native_session_id=native_session_id,
            credential_id=config.identity.credential_id,
            edit_request_id=consumed.edit_request_id,
        )
    except Exception:
        return False


def main() -> None:
    event = read_hook_event()
    path = event.get("cwd")
    if isinstance(path, str) and path and os.environ.get(_CREDENTIAL_ENV):
        handle_post_edit(event, path)
    sys.exit(0)


if __name__ == "__main__":
    main()
