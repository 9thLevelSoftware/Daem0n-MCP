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

from daem0nmcp.api.v7 import production, rule_trigger_operations
from daem0nmcp.api.v7.fastmcp import _task_protocol_error
from daem0nmcp.api.v7.models import CapabilityState
from daem0nmcp.api.v7.task_dispatcher import TaskDispatcherError
from daem0nmcp.bounded_workers import BoundedWorkerBusyError
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


async def test_task_metadata_is_ignored_when_tasks_are_not_offered(tmp_path):
    async with _client(tmp_path, tasks=False) as (client, workspace_id):
        assert client.initialize_result.capabilities.tasks is None
        arguments = {"workspace_id": workspace_id, "query": "plain recall"}

        augmented = await _call(client, "memory_recall", arguments)
        plain = await _call(client, "memory_recall", arguments, task=False)

    # A receiver without the task capability processes the request normally.
    assert isinstance(augmented, mcp_types.CallToolResult)
    assert augmented.isError is False
    assert augmented.structuredContent["ok"] is True
    assert plain.structuredContent["ok"] is True
    assert (
        augmented.structuredContent["data"]["items"]
        == plain.structuredContent["data"]["items"]
    )


@pytest.mark.parametrize("tasks", [True, False])
async def test_tools_list_marks_task_support_only_when_tasks_are_offered(
    tmp_path, tasks
):
    async with _client(tmp_path, tasks=tasks, tool_surface="full") as (client, _):
        tools = await client.list_tools()

    declared = {
        tool.name
        for tool in tools
        if (tool.meta or {}).get("daem0nmcp/taskMode") == "optional"
    }
    advertised = {
        tool.name: tool.execution.taskSupport
        for tool in tools
        if tool.execution is not None and tool.execution.taskSupport is not None
    }
    # A task is admitted only when it is replay-safe: the tool is read-only
    # or its input carries an idempotency key.  The rest are not advertised.
    admissible = {
        tool.name
        for tool in tools
        if tool.name in declared
        and (
            (tool.annotations is not None and tool.annotations.readOnlyHint)
            or "idempotency_key" in tool.inputSchema["properties"]
        )
    }
    assert "memory_recall" in admissible and "memory_store" not in declared
    assert "memory_store_batch" in admissible
    assert "code_index" in declared - admissible
    assert advertised == (dict.fromkeys(admissible, "optional") if tasks else {})


async def test_gateway_names_the_task_path_for_an_oversized_hidden_tool(tmp_path):
    async with _client(tmp_path, tasks=True) as (client, workspace_id):
        listed = {tool.name for tool in await client.list_tools()}
        assert "code_todos_scan" not in listed
        oversized = {"limit": 201}

        relayed = await client.call_tool(
            "daem0n_tool_call",
            {
                "workspace_id": workspace_id,
                "tool": "code_todos_scan",
                "arguments": oversized,
            },
            raise_on_error=False,
        )
        error = relayed.structured_content["error"]
        assert error["code"] == "TASK_REQUIRED"
        assert "task-augmented tools/call" in error["message"]
        assert "by its own name" in error["message"]

        # The path it names exists even though the tool is not listed.
        accepted = await _call(
            client, "code_todos_scan", {"workspace_id": workspace_id} | oversized
        )
        assert isinstance(accepted, mcp_types.CreateTaskResult)


async def test_oversized_call_without_tasks_does_not_name_a_task_path(tmp_path):
    async with _client(tmp_path, tasks=False) as (client, workspace_id):
        refused = await client.call_tool(
            "daem0n_tool_call",
            {
                "workspace_id": workspace_id,
                "tool": "code_todos_scan",
                "arguments": {"limit": 201},
            },
            raise_on_error=False,
        )

    error = refused.structured_content["error"]
    assert error["code"] == "TASK_REQUIRED"
    assert "tools/call" not in error["message"]


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


async def test_tool_that_can_never_be_a_task_is_not_sent_to_the_task_path(tmp_path):
    # code_index writes and takes no idempotency key, so no task of it is
    # ever replay-safe.
    async with _client(tmp_path, tasks=True, tool_surface="full") as (
        client,
        workspace_id,
    ):
        oversized = {
            "workspace_id": workspace_id,
            "patterns": [f"pkg{index}/**/*.py" for index in range(9)],
        }
        plain = await client.call_tool("code_index", oversized, raise_on_error=False)
        refused = await _refusal(client, "code_index", oversized)

    error = plain.structured_content["error"]
    assert error["code"] == "TASK_REQUIRED"
    assert "tools/call" not in error["message"]
    # Not a missing key the caller could supply: the schema has no such field.
    assert refused.code == mcp_types.METHOD_NOT_FOUND
    assert refused.data == {"code": "TASK_NOT_SUPPORTED"}
    assert "idempotency_key" not in refused.message


async def test_busy_task_forbidden_tool_is_not_sent_to_the_task_path(tmp_path):
    async def busy(*_args: Any, **_kwargs: Any) -> Any:
        raise BoundedWorkerBusyError("busy")

    async with _client(tmp_path, tasks=True, tool_surface="full") as (
        client,
        workspace_id,
    ):
        scope = {"workspace_id": workspace_id}
        with patch.object(rule_trigger_operations._RULE_TRIGGER_WORKERS, "run", busy):
            direct = await client.call_tool("rule_list", scope, raise_on_error=False)
            relayed = await client.call_tool(
                "daem0n_tool_call",
                scope | {"tool": "rule_list", "arguments": {}},
                raise_on_error=False,
            )
        as_task = await _refusal(client, "rule_list", scope)

    # rule_list cannot run as a task, so neither answer may name that path.
    assert as_task.data == {"code": "TASK_NOT_SUPPORTED"}
    for answer in (direct, relayed):
        error = answer.structured_content["error"]
        assert error["code"] == "TASK_REQUIRED"
        assert "tools/call" not in error["message"]


def test_server_conditions_and_idempotency_refusals_are_reported_apart():
    def reported(code: str) -> Any:
        return _task_protocol_error(TaskDispatcherError(code)).error

    full = reported("TASK_QUEUE_FULL")
    unavailable = reported("TASKS_UNAVAILABLE")
    degraded = reported("CAPABILITY_DEGRADED")
    assert full.code == unavailable.code == degraded.code == mcp_types.INTERNAL_ERROR
    assert full.data == {"code": "TASK_QUEUE_FULL", "retryable": True}
    assert unavailable.data == {"code": "TASKS_UNAVAILABLE", "retryable": False}
    assert degraded.data == {"code": "CAPABILITY_DEGRADED", "retryable": True}

    # A covenant refusal whose counsel could not be fetched still names itself.
    request_faults = [
        "IDEMPOTENCY_REQUIRED",
        "IDEMPOTENCY_CONFLICT",
        "TASK_RESULT_UNAVAILABLE",
        "IDENTITY_UNAVAILABLE",
        "COUNSEL_REQUIRED",
        "TOKEN_TAMPERED",
        "TOKEN_ARGUMENT_MISMATCH",
    ]
    messages = set()
    for code in request_faults:
        error = reported(code)
        assert error.code == mcp_types.INVALID_PARAMS, code
        assert error.data == {"code": code}
        messages.add(error.message)
    assert len(messages) == len(request_faults) - 1  # the two TOKEN_* share one

    unknown = reported("SOMETHING_ELSE")
    assert unknown.code == mcp_types.INVALID_PARAMS and unknown.data is None


async def test_token_refusal_without_a_remedy_does_not_point_at_one(tmp_path):
    async with _client(tmp_path, tasks=True) as (client, workspace_id):
        forged = _batch(workspace_id) | {"preflight_token": "cap_" + "A" * 43}
        error = await _refusal(client, "memory_store_batch", forged)

    assert error.data["code"].startswith("TOKEN_")
    assert error.data["error"]["remedy"] is None
    assert "follow its remedy" not in error.message
    assert error.data["error"]["message"] in error.message
