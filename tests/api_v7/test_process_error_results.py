"""Real subprocess coverage for how failed tool calls reach an MCP client.

A v7 failure is an ordinary envelope with ``ok: false``.  The protocol flag
``isError`` must say the same thing, and it must not cost the client the
envelope: guided counsel is delivered in ``error.counsel`` and ``error.remedy``.
"""

from __future__ import annotations

import json

import pytest
from mcp import McpError
from mcp.types import INVALID_PARAMS

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


MARKER = "zq9-caller-supplied-marker"


def _rejection_text(result) -> str:
    assert result.isError is True
    assert result.structuredContent is None
    [content] = result.content
    return content.text


async def test_rejected_arguments_are_described_without_echoing_them(
    initialized_workspace,
):
    """stdio has no request size limit, so the reply must bound itself."""

    workspace = initialized_workspace
    scope = {"workspace_id": workspace.workspace_id}
    oversized = MARKER + "y" * 1_100_000
    store = {
        **scope,
        "record_type": "decision",
        "content": "Use signed session cookies",
        "idempotency_key": "rejected-arguments-0001",
    }
    rejected = {
        # Refused by the advertised JSON schema.
        "oversized value": ("memory_recall", {**scope, "query": oversized}),
        "wrong type": ("memory_recall", {**scope, "query": [MARKER]}),
        "unexpected field": ("memory_recall", {**scope, "query": "x", MARKER: 1}),
        "oversized field name": (
            "memory_recall",
            {**scope, "query": "x", oversized: 1},
        ),
        "oversized workspace": ("memory_recall", {"workspace_id": oversized}),
        # A registered tool the default listing omits: the MCP layer checks
        # only listed tools, so this one is refused by the framework instead.
        "unlisted tool": ("memory_search_text", {**scope, "query": oversized}),
        "unexpected field, unlisted tool": (
            "memory_search_text",
            {**scope, "query": "x", MARKER: 1},
        ),
        # A key the caller chose inside a free-form map is input too.
        "caller-chosen map key": (
            "memory_store",
            {**store, "context": {MARKER: oversized}},
        ),
        # Accepted by the JSON schema, refused by the model's own validators.
        "duplicate tags": ("memory_store", {**store, "tags": [MARKER, MARKER]}),
        "escaping path": (
            "memory_store",
            {**store, "relative_file_path": f"../{MARKER}.py"},
        ),
    }
    async with process_client(workspace.root, "stdio") as session:
        for label, (tool, arguments) in rejected.items():
            text = _rejection_text(await session.call_tool(tool, arguments))
            assert MARKER not in text, (label, text[:300])
            assert len(text) < 4_000, (label, len(text))
            assert text.startswith("Input validation error: "), (label, text)

        text = _rejection_text(
            await session.call_tool("memory_recall", {**scope, "query": oversized})
        )
        assert "arguments.query (maxLength)" in text
        text = _rejection_text(await session.call_tool("memory_recall", scope))
        assert "arguments.query (required)" in text

        text = _rejection_text(await session.call_tool("tool_" + oversized, {}))
        assert MARKER not in text
        assert len(text) < 4_000

        # The gateway's own arguments are held to the same rule ...
        text = _rejection_text(
            await session.call_tool(
                "daem0n_tool_call",
                {**scope, "tool": "memory_recall", "arguments": {"query": oversized}},
            )
        )
        assert MARKER not in text
        assert len(text) < 4_000
        # ... and it reports its target's rejection as a bounded envelope.
        relayed = await session.call_tool(
            "daem0n_tool_call",
            {**scope, "tool": "memory_recall", "arguments": {"query": [MARKER]}},
        )
        envelope = _envelope(relayed)
        assert relayed.isError is True
        assert envelope["error"]["code"] == "INVALID_ARGUMENT"
        assert envelope["error"]["field_errors"] == [
            {
                "field": "arguments.query",
                "code": "INVALID_VALUE",
                "message": "Expected a JSON string matching the tool's declared constraints.",
            }
        ]
        assert MARKER not in relayed.content[0].text
        relayed = await session.call_tool(
            "daem0n_tool_call",
            {
                **scope,
                "tool": "memory_search_text",
                "arguments": {"query": "x", MARKER: 1},
            },
        )
        assert relayed.isError is True
        assert _envelope(relayed)["error"]["field_errors"] == [
            {
                "field": "arguments",
                "code": "INVALID_VALUE",
                "message": "Remove fields not declared by the tool's input schema.",
            }
        ]
        assert MARKER not in relayed.content[0].text

        # A well-formed call is unaffected by the checks above.
        stored = await session.call_tool("memory_store", {**store, "tags": [MARKER]})
        assert _envelope(stored)["ok"] is True


async def test_unknown_prompt_is_refused_without_echoing_its_name(
    initialized_workspace,
):
    """No prompts are registered; the refusal must not repeat the request."""

    workspace = initialized_workspace
    async with process_client(workspace.root, "stdio") as session:
        assert (await session.list_prompts()).prompts == []
        for name in ("no_such_prompt", MARKER + "y" * 1_100_000):
            with pytest.raises(McpError) as refused:
                await session.get_prompt(name, {MARKER: MARKER})
            assert refused.value.error.code == INVALID_PARAMS
            assert refused.value.error.message == "Unknown prompt"
            assert refused.value.error.data is None
