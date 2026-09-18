"""Claude Code native-edit bridge adapter."""

from __future__ import annotations

import os
import sys
from collections.abc import Mapping
from datetime import datetime
from typing import Any

from ..edit_host import (
    CreatedHostSession,
    EditHostConfig,
    EditHostStateStore,
    PendingEditBinding,
)
from ._client import block, read_hook_event
from .native_edit import configured_native_edit_tools, native_edit_request


class PreEditResult:
    def __init__(self, allowed: bool, message: str) -> None:
        self.allowed, self.message = allowed, message


async def async_main(project_path: str, file_path: str) -> PreEditResult:
    """Compatibility entry point; standalone calls cannot forge host sessions."""
    del project_path, file_path
    return PreEditResult(False, "EDIT_BRIDGE_UNAVAILABLE")


def _expires(value: object) -> datetime:
    if not isinstance(value, str):
        raise ValueError
    return datetime.fromisoformat(value.removesuffix("Z") + "+00:00")


def _data(response: object) -> Mapping[str, Any]:
    body = getattr(response, "body", None)
    if (
        not isinstance(body, Mapping)
        or body.get("ok") is not True
        or not isinstance(body.get("data"), Mapping)
    ):
        raise RuntimeError
    return body["data"]


def handle_pre_edit(event: Mapping[str, Any], project_path: str) -> PreEditResult:
    try:
        native_session_id, native_request_id, tool_name, tool_input = (
            event["session_id"],
            event["tool_use_id"],
            event["tool_name"],
            event["tool_input"],
        )
        if (
            not isinstance(native_session_id, str)
            or not isinstance(native_request_id, str)
            or not isinstance(tool_name, str)
            or not isinstance(tool_input, Mapping)
        ):
            raise ValueError
        config = EditHostConfig.from_environment(os.environ)
        workspace_id = config.workspace_id(project_path)
        store = EditHostStateStore(config)
        client = config.build_client()
        edit = native_edit_request(
            project_path=project_path,
            tool_name=tool_name,
            tool_input=tool_input,
            configured_tools=configured_native_edit_tools(os.environ),
        )

        def create() -> CreatedHostSession:
            data = _data(client.call("/v1/sessions", {"workspace_id": workspace_id}))
            return CreatedHostSession(
                str(data["host_session_id"]), _expires(data["expires_at"])
            )

        host = store.load_or_create_session(
            workspace_id=workspace_id,
            native_session_id=native_session_id,
            credential_id=config.identity.credential_id,
            create=create,
        )
        pending = store.get_pending(
            workspace_id=workspace_id,
            native_session_id=native_session_id,
            credential_id=config.identity.credential_id,
            edit_hash=edit.edit_hash,
        )
        wire_edit = {
            "tool_name": edit.tool_name,
            "arguments": dict(edit.arguments),
            "preimages": [item.canonical() for item in edit.preimages],
        }
        if pending is not None:
            data = _data(
                client.call(
                    "/v1/receipts/consume",
                    {
                        "workspace_id": workspace_id,
                        "host_session_id": host.host_session_id,
                        "edit_request_id": pending.edit_request_id,
                        "edit": wire_edit,
                    },
                )
            )
            if data.get("allowed") is not True:
                raise RuntimeError
            store.mark_consumed(
                workspace_id=workspace_id,
                native_session_id=native_session_id,
                native_request_id=native_request_id,
                credential_id=config.identity.credential_id,
                edit_request_id=pending.edit_request_id,
            )
            return PreEditResult(True, "")
        data = _data(
            client.call(
                "/v1/edits",
                {
                    "workspace_id": workspace_id,
                    "host_session_id": host.host_session_id,
                    "edit": wire_edit,
                },
            )
        )
        store.save_pending(
            native_session_id=native_session_id,
            native_request_id=native_request_id,
            credential_id=config.identity.credential_id,
            pending=PendingEditBinding(
                workspace_id,
                host.host_session_id,
                str(data["edit_request_id"]),
                edit.edit_hash,
                _expires(data["expires_at"]),
            ),
        )
        remedy = data.get("remedy")
        return PreEditResult(
            False,
            str(remedy) if isinstance(remedy, Mapping) else "EDIT_BRIDGE_UNAVAILABLE",
        )
    except Exception:
        return PreEditResult(False, "EDIT_BRIDGE_UNAVAILABLE")


def main() -> None:
    event = read_hook_event()
    path = event.get("cwd")
    if not isinstance(path, str) or not path:
        block("EDIT_BRIDGE_UNAVAILABLE")
    result = handle_pre_edit(event, path)
    if not result.allowed:
        block(result.message)
    sys.exit(0)


if __name__ == "__main__":
    main()
