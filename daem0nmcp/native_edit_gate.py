"""Shared native-host edit approval, independent of host event wire formats."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from .claude_hooks.native_edit import configured_native_edit_tools, native_edit_request
from .edit_host import (
    CreatedHostSession,
    EditHostConfig,
    EditHostStateStore,
    PendingEditBinding,
)
from .event_store import canonical_json_bytes

AUTOMATION_MODE_ENV = "DAEM0NMCP_AUTOMATION_MODE"


def native_input_hash(tool_name: str, tool_input: Mapping[str, Any]) -> str:
    """Bind the original native call as well as its normalized bridge request."""

    return hashlib.sha256(
        canonical_json_bytes([tool_name, dict(tool_input)])
    ).hexdigest()


@dataclass(frozen=True, slots=True)
class NativeEditDecision:
    allowed: bool
    code: str
    remedy: Mapping[str, Any] | None = None

    def explanation(self) -> str:
        if self.remedy is not None:
            return (
                "Daem0n edit approval required. Call "
                + json.dumps(dict(self.remedy), sort_keys=True)
                + ", then retry the identical native edit in this session."
            )
        return f"Daem0n denied this edit: {self.code}."


def _data(response: Any) -> Mapping[str, Any]:
    body = response.body
    if (
        response.status not in {200, 201}
        or not isinstance(body, Mapping)
        or body.get("ok") is not True
    ):
        raise ValueError("edit bridge rejected the native request")
    data = body.get("data")
    if not isinstance(data, Mapping):
        raise ValueError("edit bridge response is invalid")
    return data


def _expires(value: Any) -> datetime:
    if not isinstance(value, str):
        raise ValueError("edit bridge expiry is invalid")
    return datetime.fromisoformat(value.removesuffix("Z") + "+00:00")


def approve_native_edit(
    event: Mapping[str, Any],
    project_path: str,
    *,
    environ: Mapping[str, str],
) -> NativeEditDecision:
    """Deny first use; consume a staged exact receipt before an unchanged retry.

    The caller supplies a trusted native event, never model-authored approval
    text. Any malformed input or unavailable infrastructure denies the edit.
    """

    try:
        session_id = event["session_id"]
        request_id = event["tool_use_id"]
        tool_name = event["tool_name"]
        tool_input = event["tool_input"]
        if not all(
            isinstance(value, str) and value and len(value) <= 4096
            for value in (session_id, request_id, tool_name)
        ) or not isinstance(tool_input, Mapping):
            return NativeEditDecision(False, "EDIT_HOST_EVENT_INVALID")
        original_hash = native_input_hash(tool_name, tool_input)
        config = EditHostConfig.from_environment(environ)
        workspace_id = config.workspace_id(project_path)
        store = EditHostStateStore(config)
        client = config.build_client()
        edit = native_edit_request(
            project_path=project_path,
            tool_name=tool_name,
            tool_input=tool_input,
            configured_tools=configured_native_edit_tools(environ),
        )

        def create() -> CreatedHostSession:
            data = _data(client.call("/v1/sessions", {"workspace_id": workspace_id}))
            return CreatedHostSession(
                str(data["host_session_id"]), _expires(data["expires_at"])
            )

        credential_id = config.identity.credential_id
        host = store.load_or_create_session(
            workspace_id=workspace_id,
            native_session_id=session_id,
            credential_id=credential_id,
            create=create,
        )
        pending = store.get_pending(
            workspace_id=workspace_id,
            native_session_id=session_id,
            credential_id=credential_id,
            edit_hash=edit.edit_hash,
        )
        wire_edit = {
            "tool_name": edit.tool_name,
            "arguments": dict(edit.arguments),
            "preimages": [item.canonical() for item in edit.preimages],
        }
        if pending is not None:
            if pending.native_input_hash != original_hash:
                return NativeEditDecision(False, "EDIT_NATIVE_ARGUMENT_MISMATCH")
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
                return NativeEditDecision(False, "EDIT_APPROVAL_REQUIRED")
            store.mark_consumed(
                workspace_id=workspace_id,
                native_session_id=session_id,
                native_request_id=request_id,
                credential_id=credential_id,
                edit_request_id=pending.edit_request_id,
            )
            return NativeEditDecision(True, "EDIT_APPROVED")
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
        edit_request_id = str(data["edit_request_id"])
        store.save_pending(
            native_session_id=session_id,
            native_request_id=request_id,
            credential_id=credential_id,
            pending=PendingEditBinding(
                workspace_id,
                host.host_session_id,
                edit_request_id,
                edit.edit_hash,
                _expires(data["expires_at"]),
                native_input_hash=original_hash,
            ),
        )
        return NativeEditDecision(
            False,
            "EDIT_APPROVAL_REQUIRED",
            {
                "tool": "edit_preflight",
                "arguments": {
                    "workspace_id": workspace_id,
                    "edit_request_id": edit_request_id,
                    "description": "Describe the exact planned native edit",
                },
            },
        )
    except Exception:
        return NativeEditDecision(False, "EDIT_BRIDGE_UNAVAILABLE")
