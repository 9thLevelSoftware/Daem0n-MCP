"""Test-only native host driver for the edit bridge.

The Claude ``pre_edit`` hook no longer drives the bridge (it only reminds), but
the bridge itself stays until its planned removal.  These steps are what a
native host does before an edit: open a host session, then either consume an
approved receipt or create a pending edit request.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from datetime import datetime
from typing import Any

from daem0nmcp.claude_hooks.native_edit import (
    configured_native_edit_tools,
    native_edit_request,
)
from daem0nmcp.edit_host import (
    CreatedHostSession,
    EditHostConfig,
    EditHostStateStore,
    PendingEditBinding,
)


def _expires(value: object) -> datetime:
    assert isinstance(value, str)
    return datetime.fromisoformat(value.removesuffix("Z") + "+00:00")


def _data(response: object) -> Mapping[str, Any]:
    body = getattr(response, "body", None)
    assert isinstance(body, Mapping) and body.get("ok") is True, body
    return body["data"]


def drive_native_edit(event: Mapping[str, Any], project_path: str) -> bool:
    """Return True when an approved receipt was consumed for this exact edit."""
    native_session_id = event["session_id"]
    native_request_id = event["tool_use_id"]
    config = EditHostConfig.from_environment(os.environ)
    workspace_id = config.workspace_id(project_path)
    store = EditHostStateStore(config)
    client = config.build_client()
    edit = native_edit_request(
        project_path=project_path,
        tool_name=event["tool_name"],
        tool_input=event["tool_input"],
        configured_tools=configured_native_edit_tools(os.environ),
    )

    def create() -> CreatedHostSession:
        data = _data(client.call("/v1/sessions", {"workspace_id": workspace_id}))
        return CreatedHostSession(
            str(data["host_session_id"]), _expires(data["expires_at"])
        )

    credential_id = config.identity.credential_id
    host = store.load_or_create_session(
        workspace_id=workspace_id,
        native_session_id=native_session_id,
        credential_id=credential_id,
        create=create,
    )
    pending = store.get_pending(
        workspace_id=workspace_id,
        native_session_id=native_session_id,
        credential_id=credential_id,
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
        assert data.get("allowed") is True, data
        store.mark_consumed(
            workspace_id=workspace_id,
            native_session_id=native_session_id,
            native_request_id=native_request_id,
            credential_id=credential_id,
            edit_request_id=pending.edit_request_id,
        )
        return True
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
        credential_id=credential_id,
        pending=PendingEditBinding(
            workspace_id,
            host.host_session_id,
            str(data["edit_request_id"]),
            edit.edit_hash,
            _expires(data["expires_at"]),
        ),
    )
    return False
