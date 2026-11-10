"""Real subprocess coverage for how failed tool calls reach an MCP client.

A v7 failure is an ordinary envelope with ``ok: false``.  The protocol flag
``isError`` must say the same thing, and it must not cost the client the
envelope: guided counsel is delivered in ``error.counsel`` and ``error.remedy``.
"""

from __future__ import annotations

import json

import pytest

from daem0nmcp.database import DatabaseManager
from daem0nmcp.workspace import WorkspaceRegistry
from tests.api_v7.process_client import process_client


@pytest.fixture
async def initialized_workspace(tmp_path):
    storage = tmp_path / ".daem0nmcp" / "storage"
    storage.mkdir(parents=True)
    manager = DatabaseManager(str(storage))
    try:
        await manager.init_db()
    finally:
        await manager.close()
    return WorkspaceRegistry([tmp_path], default_root=tmp_path).default


def _envelope(result) -> dict:
    """Return the envelope, proving both renderings of it are intact."""

    assert isinstance(result.structuredContent, dict)
    assert len(result.content) == 1
    assert json.loads(result.content[0].text) == result.structuredContent
    return result.structuredContent


@pytest.mark.parametrize("transport", ["stdio", "streamable-http"])
async def test_failed_envelopes_set_is_error_and_keep_the_envelope(
    initialized_workspace, transport
):
    workspace = initialized_workspace
    scope = {"workspace_id": workspace.workspace_id}
    async with process_client(workspace.root, transport) as session:
        stored = await session.call_tool(
            "memory_store",
            {
                **scope,
                "record_type": "decision",
                "content": "Use signed session cookies",
                "idempotency_key": "error-result-store-0001",
            },
        )
        envelope = _envelope(stored)
        assert envelope["ok"] is True, envelope
        assert stored.isError is False
        record_id = envelope["data"]["record"]["record_id"]

        # Gateway path: a failure the gateway produces itself.
        unknown = await session.call_tool(
            "daem0n_tool_call",
            {**scope, "tool": "no_such_tool", "arguments": {}},
        )
        envelope = _envelope(unknown)
        assert envelope["ok"] is False
        assert envelope["error"]["field_errors"][0]["code"] == "UNKNOWN_TOOL"
        assert unknown.isError is True

        # Direct path: a replayed key with different content.
        conflict = await session.call_tool(
            "memory_store",
            {
                **scope,
                "record_type": "decision",
                "content": "Use opaque bearer tokens",
                "idempotency_key": "error-result-store-0001",
            },
        )
        envelope = _envelope(conflict)
        assert envelope["ok"] is False
        assert envelope["error"]["code"] == "IDEMPOTENCY_CONFLICT"
        assert conflict.isError is True

        # Guided counsel: the client must still be able to read the counsel
        # and replay the exact remedy, directly and through the gateway.
        arguments = {"record_id": record_id, "archived": True}
        for tool, call_arguments in (
            ("memory_archive_set", {**scope, **arguments}),
            (
                "daem0n_tool_call",
                {**scope, "tool": "memory_archive_set", "arguments": arguments},
            ),
        ):
            challenged = await session.call_tool(tool, call_arguments)
            envelope = _envelope(challenged)
            assert envelope["ok"] is False
            assert envelope["error"]["code"] == "COUNSEL_REQUIRED"
            assert challenged.isError is True
            counsel = envelope["error"]["counsel"]
            assert counsel["reasons"] == ["DESTRUCTIVE_OPERATION"]
            assert counsel["preflight_token"]
            remedy = envelope["error"]["remedy"]
            assert remedy["tool"] == tool

        retried = await session.call_tool(remedy["tool"], remedy["arguments"])
        envelope = _envelope(retried)
        assert envelope["ok"] is True, envelope
        assert retried.isError is False
