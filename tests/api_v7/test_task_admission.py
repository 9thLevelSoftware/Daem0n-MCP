"""Task-augmented tool calls through the composed server, without a queue.

The durable dispatcher admits and persists a task without its queue, so these
tests need no Redis: an accepted task simply stays queued.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from unittest.mock import patch

import mcp.types as mcp_types
import pytest
from fastmcp import Client
from mcp.shared.exceptions import McpError
from pydantic import RootModel

from daem0nmcp.api.v7 import production
from daem0nmcp.api.v7.models import CapabilityState
from daem0nmcp.config import Settings
from tests.api_v7.process_client import initialize_workspaces

_TaskCallResult = RootModel[mcp_types.CreateTaskResult | mcp_types.CallToolResult]
_UNREACHABLE_QUEUE = "redis://:secret@127.0.0.1:1/0"


@asynccontextmanager
async def _client(root: Path, *, tasks: bool, **settings: Any):
    (workspace,) = await initialize_workspaces((root,))
    loaded = Settings(
        project_root=str(root),
        workspace_roots=[str(root)],
        dream_enabled=False,
        **settings,
    )
    if tasks:
        ready = CapabilityState.model_validate({"name": "tasks", "status": "ready"})
        with patch.object(
            production,
            "_task_configuration",
            return_value=(True, ready, _UNREACHABLE_QUEUE),
        ):
            server = production.create_v7_server("stdio", settings=loaded, environ={})
    else:
        server = production.create_v7_server("stdio", settings=loaded, environ={})
    async with Client(server) as client:
        yield client, workspace.workspace_id


async def _call(
    client: Client, name: str, arguments: dict[str, Any], *, task: bool = True
) -> mcp_types.CreateTaskResult | mcp_types.CallToolResult:
    response = await client.session.send_request(
        mcp_types.ClientRequest(
            mcp_types.CallToolRequest(
                params=mcp_types.CallToolRequestParams(
                    name=name,
                    arguments=arguments,
                    task=mcp_types.TaskMetadata(ttl=60_000) if task else None,
                )
            )
        ),
        _TaskCallResult,
    )
    return response.root


async def _refusal(client: Client, name: str, arguments: dict[str, Any]) -> Any:
    with pytest.raises(McpError) as refused:
        await _call(client, name, arguments)
    return refused.value.error


def _batch(workspace_id: str) -> dict[str, Any]:
    return {
        "workspace_id": workspace_id,
        "idempotency_key": "task-admission-batch-0001",
        "records": [
            {"record_type": "learning", "content": "Queued batches keep their order"}
        ],
    }


async def test_guided_task_refusal_carries_the_counsel_and_its_remedy(tmp_path):
    async with _client(tmp_path, tasks=True) as (client, workspace_id):
        error = await _refusal(client, "memory_store_batch", _batch(workspace_id))

        assert error.code == mcp_types.INVALID_PARAMS
        assert error.data["code"] == "COUNSEL_REQUIRED"
        answer = error.data["error"]
        assert answer["code"] == "COUNSEL_REQUIRED"
        assert "TASK_ADMISSION" in answer["counsel"]["reasons"]
        assert answer["remedy"]["tool"] == "memory_store_batch"
        token = answer["remedy"]["arguments"]["preflight_token"]
        assert token == answer["counsel"]["preflight_token"]
        # The session was briefed on the way, exactly as a plain call's is.
        assert error.data["covenant"]["auto_brief"] is not None

        # Nothing was accepted without the capability ...
        listed = await client.session.send_request(
            mcp_types.ClientRequest(mcp_types.ListTasksRequest()),
            mcp_types.ListTasksResult,
        )
        assert listed.tasks == []

        # ... and following the remedy as a task is accepted.
        accepted = await _call(
            client, "memory_store_batch", answer["remedy"]["arguments"]
        )
        assert isinstance(accepted, mcp_types.CreateTaskResult)
        assert accepted.task.taskId.startswith("tsk_")


async def test_guided_task_on_an_unbriefed_session_is_briefed_and_accepted(tmp_path):
    async with _client(tmp_path, tasks=True) as (client, workspace_id):
        accepted = await _call(
            client,
            "memory_recall",
            {"workspace_id": workspace_id, "query": "queued recall"},
        )

        assert isinstance(accepted, mcp_types.CreateTaskResult)
        assert accepted.task.taskId.startswith("tsk_")
        assert accepted.meta is not None
        assert accepted.meta["daem0nmcp/covenant"]["auto_brief"] is not None


async def test_strict_task_refusals_name_the_explicit_ceremony(tmp_path):
    async with _client(tmp_path, tasks=True, covenant_mode="strict") as (
        client,
        workspace_id,
    ):
        scope = {"workspace_id": workspace_id}
        unbriefed = await _refusal(
            client, "memory_recall", scope | {"query": "queued recall"}
        )
        assert unbriefed.code == mcp_types.INVALID_PARAMS
        assert unbriefed.data["code"] == "COMMUNION_REQUIRED"
        assert unbriefed.data["error"]["remedy"] == {
            "tool": "session_brief",
            "arguments": scope,
        }

        briefed = await client.call_tool("session_brief", scope)
        assert briefed.structured_content["ok"] is True

        unadvised = await _refusal(client, "memory_store_batch", _batch(workspace_id))
        assert unadvised.data["code"] == "COUNSEL_REQUIRED"
        assert unadvised.data["error"]["remedy"]["tool"] == "memory_preflight"
        assert unadvised.data["error"]["counsel"] is None


async def test_task_refusals_are_told_apart_by_code_and_data(tmp_path):
    async with _client(tmp_path, tasks=True) as (client, workspace_id):
        scope = {"workspace_id": workspace_id}
        forbidden = await _refusal(
            client,
            "memory_store",
            scope
            | {
                "record_type": "decision",
                "content": "Plain calls only",
                "idempotency_key": "task-admission-store-0001",
            },
        )
        malformed = await _refusal(client, "memory_recall", scope)
        with pytest.raises(McpError) as bad_metadata:
            await client.session.send_request(
                mcp_types.ClientRequest(
                    mcp_types.CallToolRequest(
                        params=mcp_types.CallToolRequestParams(
                            name="memory_recall",
                            arguments=scope | {"query": "queued recall"},
                            task=mcp_types.TaskMetadata(ttl=-1),
                        )
                    )
                ),
                _TaskCallResult,
            )
        with pytest.raises(McpError) as missing:
            await client.session.send_request(
                mcp_types.ClientRequest(
                    mcp_types.GetTaskRequest(
                        params=mcp_types.GetTaskRequestParams(taskId="tsk_" + "0" * 64)
                    )
                ),
                mcp_types.GetTaskResult,
            )
        with pytest.raises(McpError) as bad_cursor:
            await client.session.send_request(
                mcp_types.ClientRequest(
                    mcp_types.ListTasksRequest(
                        params=mcp_types.PaginatedRequestParams(cursor="not-a-cursor")
                    )
                ),
                mcp_types.ListTasksResult,
            )

    # The protocol's own code for a tool that cannot run as a task.
    assert forbidden.code == mcp_types.METHOD_NOT_FOUND
    assert forbidden.data == {"code": "TASK_NOT_SUPPORTED"}
    others = {
        "INVALID_ARGUMENT": malformed,
        "INVALID_TASK_METADATA": bad_metadata.value.error,
        "TASK_NOT_FOUND": missing.value.error,
        "INVALID_CURSOR": bad_cursor.value.error,
    }
    for code, error in others.items():
        assert error.code == mcp_types.INVALID_PARAMS, code
        assert error.data == {"code": code}
    messages = [forbidden.message, *(error.message for error in others.values())]
    assert len(set(messages)) == len(messages)
