"""Successful discovery and gateway calls through the production transports."""

from __future__ import annotations

import asyncio

import pytest

from tests.api_v7.process_client import (
    call,
    initialize_workspaces,
    process_client,
    succeed,
)


async def _gateway(session, scope, tool, arguments):
    result = await succeed(
        session,
        "daem0n_tool_call",
        {**scope, "tool": tool, "arguments": arguments},
    )
    assert result["tool"] == tool
    return result["data"]


@pytest.mark.parametrize("transport", ["stdio", "streamable-http"])
async def test_production_gateway_mutation_counsel_replay_and_restart(
    tmp_path, transport
):
    [workspace] = await initialize_workspaces((tmp_path,))
    scope = {"workspace_id": workspace.workspace_id}
    content = "Gateway persistence acceptance keeps this decision across restart."
    store_arguments = {
        "record_type": "decision",
        "content": content,
        "idempotency_key": "process-gateway-store-0001",
    }

    async with process_client(workspace.root, transport) as session:
        listed = {tool.name for tool in (await session.list_tools()).tools}
        assert "daem0n_tools_search" in listed
        assert "daem0n_tool_call" in listed
        for tool in ("memory_archive_set", "memory_search_text"):
            assert tool not in listed
            found = await succeed(session, "daem0n_tools_search", {"query": tool})
            descriptor = next(item for item in found["tools"] if item["name"] == tool)
            assert descriptor["listed"] is False
            assert "workspace_id" in descriptor["input_schema"]["required"]

        stored = await _gateway(session, scope, "memory_store", store_arguments)
        record_id = stored["record"]["record_id"]
        assert stored["idempotent_replay"] is False
        replayed = await _gateway(session, scope, "memory_store", store_arguments)
        assert replayed["idempotent_replay"] is True
        assert replayed["record"]["record_id"] == record_id
        assert replayed["event_id"] == stored["event_id"]

        deadline = asyncio.get_running_loop().time() + 5
        while True:
            searched = await _gateway(
                session,
                scope,
                "memory_search_text",
                {"query": "gateway persistence acceptance"},
            )
            if searched["items"]:
                break
            assert asyncio.get_running_loop().time() < deadline, searched
            await asyncio.sleep(0.1)
        assert len(searched["items"]) == 1
        assert searched["items"][0]["record"]["record_id"] == record_id
        assert searched["items"][0]["bounded_excerpt"] == content

        archive_arguments = {"record_id": record_id, "archived": True}
        challenged = await call(
            session,
            "daem0n_tool_call",
            {**scope, "tool": "memory_archive_set", "arguments": archive_arguments},
        )
        assert challenged["ok"] is False
        error = challenged["error"]
        assert error["code"] == "COUNSEL_REQUIRED"
        assert "DESTRUCTIVE_OPERATION" in error["counsel"]["reasons"]
        remedy = error["remedy"]
        assert remedy == {
            "tool": "daem0n_tool_call",
            "arguments": {
                **scope,
                "tool": "memory_archive_set",
                "arguments": {
                    **archive_arguments,
                    "preflight_token": error["counsel"]["preflight_token"],
                },
            },
        }
        archived = await succeed(session, remedy["tool"], remedy["arguments"])
        assert archived["tool"] == "memory_archive_set"
        assert archived["data"]["affected_ids"] == [record_id]
        reused = await call(session, remedy["tool"], remedy["arguments"])
        assert reused["ok"] is False
        assert reused["error"]["code"] == "COUNSEL_REQUIRED"
        assert (
            reused["error"]["counsel"]["preflight_token"]
            != error["counsel"]["preflight_token"]
        )

    async with process_client(workspace.root, transport) as restarted:
        history = await _gateway(
            restarted,
            scope,
            "memory_versions_list",
            {"record_id": record_id},
        )
        assert len(history["items"]) == 2
        latest = history["items"][-1]["record"]
        assert latest["record_id"] == record_id
        assert latest["excerpt"] == content
        assert latest["current_status"] == "archived"
