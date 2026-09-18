"""Capture a successful approved native edit without transcript content."""

from __future__ import annotations

import os
import sys
from collections.abc import Mapping
from typing import Any

from ..edit_host import EditHostConfig, EditHostStateStore, native_edit_capture_body
from ._client import read_hook_event
from .native_edit import configured_native_edit_tools, native_edit_relative_paths


def _ok(response: object) -> bool:
    body = getattr(response, "body", None)
    return isinstance(body, Mapping) and body.get("ok") is True


def handle_post_edit(event: Mapping[str, Any], project_path: str) -> bool:
    """Stage only a bounded host-generated candidate for a consumed request."""
    try:
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
    if isinstance(path, str) and path:
        handle_post_edit(event, path)
    sys.exit(0)


if __name__ == "__main__":
    main()
