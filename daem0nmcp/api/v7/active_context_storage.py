"""Canonical identity seam for durable v7 active-context state."""

from __future__ import annotations

import re

from ...event_store import sha256_json

_WORKSPACE_ID_RE = re.compile(r"^ws_[0-9a-f]{24}$")
_RECORD_ID_RE = re.compile(r"^mem_[0-9a-f]{64}$")


def active_context_id_for_record(workspace_id: str, record_id: str) -> str:
    """Return the stable opaque identity for one workspace/record binding."""

    if (
        not isinstance(workspace_id, str)
        or _WORKSPACE_ID_RE.fullmatch(workspace_id) is None
    ):
        raise ValueError("workspace_id must be an opaque v7 workspace identifier")
    if not isinstance(record_id, str) or _RECORD_ID_RE.fullmatch(record_id) is None:
        raise ValueError("record_id must be an opaque v7 memory identifier")
    return "act_" + sha256_json(
        [
            "daem0nmcp",
            "v7",
            "active-context-entry",
            workspace_id,
            record_id,
        ]
    )


__all__ = ["active_context_id_for_record"]
