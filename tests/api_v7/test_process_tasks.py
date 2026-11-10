"""Real-process stdio and HTTP coverage for the MCP task lifecycle."""

from __future__ import annotations

import asyncio
import os
import sqlite3
from contextlib import closing

import mcp.types as mcp_types
import pytest
from mcp.shared.exceptions import McpError
from pydantic import RootModel

from daem0nmcp.api.v7.runtime_services import WorkspaceStorageResolver
from daem0nmcp.database import DatabaseManager
from daem0nmcp.workspace import WorkspaceRegistry
from tests.api_v7.process_client import process_client, succeed

_TaskCallResult = RootModel[mcp_types.CreateTaskResult | mcp_types.CallToolResult]


@pytest.fixture
async def task_workspace(tmp_path):
    storage = tmp_path / ".daem0nmcp" / "storage"
    storage.mkdir(parents=True)
    manager = DatabaseManager(str(storage))
    try:
        await manager.init_db()
    finally:
        await manager.close()
    return WorkspaceRegistry([tmp_path], default_root=tmp_path).default


async def _submit_task(session, workspace_id: str) -> mcp_types.Task:
    return await _submit_tool_task(
        session,
        "memory_recall",
        {
            "workspace_id": workspace_id,
            "query": "real process durable task",
            "limit": 5,
        },
    )


async def _submit_tool_task(
    session, tool_name: str, arguments: dict[str, object]
) -> mcp_types.Task:
    response = await session.send_request(
        mcp_types.CallToolRequest(
            params=mcp_types.CallToolRequestParams(
                name=tool_name,
                arguments=arguments,
                task=mcp_types.TaskMetadata(ttl=60_000),
            )
        ),
        _TaskCallResult,
    )
    assert isinstance(response.root, mcp_types.CreateTaskResult)
    return response.root.task


async def _completed_payload(session, task_id: str) -> dict:
    deadline = asyncio.get_running_loop().time() + 15
    while True:
        status = await _task_status(session, task_id)
        if status.status in {"completed", "failed", "cancelled"}:
            break
        assert asyncio.get_running_loop().time() < deadline
        await asyncio.sleep(0.05)
    assert status.status == "completed"
    payload = await session.send_request(
        mcp_types.GetTaskPayloadRequest(
            params=mcp_types.GetTaskPayloadRequestParams(taskId=task_id)
        ),
        mcp_types.GetTaskPayloadResult,
    )
    result = payload.model_dump(by_alias=True, exclude_none=True)
    structured = result["structuredContent"]
    assert structured["ok"] is True, structured.get("error")
    return structured["data"]


async def _task_status(session, task_id: str) -> mcp_types.GetTaskResult:
    return await session.send_request(
        mcp_types.GetTaskRequest(params=mcp_types.GetTaskRequestParams(taskId=task_id)),
        mcp_types.GetTaskResult,
    )


@pytest.mark.skipif(
    not os.environ.get("DAEM0NMCP_TASK_REDIS_URL"),
    reason="real authenticated Valkey certification endpoint is not configured",
)
@pytest.mark.parametrize("transport", ["stdio", "streamable-http"])
async def test_real_process_task_lifecycle_and_restart(task_workspace, transport):
    environment = {"DAEM0NMCP_TASK_REDIS_URL": os.environ["DAEM0NMCP_TASK_REDIS_URL"]}
    scope = {"workspace_id": task_workspace.workspace_id}
    async with process_client(
        task_workspace.root,
        transport,
        environment_overrides=environment,
    ) as session:
        await succeed(session, "session_brief", scope)
        accepted = await _submit_task(session, task_workspace.workspace_id)
        assert accepted.taskId.startswith("tsk_")
        deadline = asyncio.get_running_loop().time() + 10
        while True:
            status = await _task_status(session, accepted.taskId)
            if status.status in {"completed", "failed", "cancelled"}:
                break
            assert asyncio.get_running_loop().time() < deadline
            await asyncio.sleep(0.05)
        assert status.status == "completed"

        payload = await session.send_request(
            mcp_types.GetTaskPayloadRequest(
                params=mcp_types.GetTaskPayloadRequestParams(taskId=accepted.taskId)
            ),
            mcp_types.GetTaskPayloadResult,
        )
        result = payload.model_dump(by_alias=True, exclude_none=True)
        assert result["structuredContent"]["ok"] is True
        assert result["structuredContent"]["data"]["items"] == []

        listed = await session.send_request(
            mcp_types.ListTasksRequest(),
            mcp_types.ListTasksResult,
        )
        assert accepted.taskId in {task.taskId for task in listed.tasks}

        cancelled = await session.send_request(
            mcp_types.CancelTaskRequest(
                params=mcp_types.CancelTaskRequestParams(taskId=accepted.taskId)
            ),
            mcp_types.CancelTaskResult,
        )
        assert cancelled.status == "completed"

    async with process_client(
        task_workspace.root,
        transport,
        environment_overrides=environment,
    ) as restarted:
        with pytest.raises(McpError, match="briefing"):
            await _task_status(restarted, accepted.taskId)
        await succeed(restarted, "session_brief", scope)
        recovered = await _task_status(restarted, accepted.taskId)
        assert recovered.status == "completed"
        payload = await restarted.send_request(
            mcp_types.GetTaskPayloadRequest(
                params=mcp_types.GetTaskPayloadRequestParams(taskId=accepted.taskId)
            ),
            mcp_types.GetTaskPayloadResult,
        )
        recovered_result = payload.model_dump(by_alias=True)["structuredContent"]
        assert recovered_result["ok"] is True
        assert recovered_result["data"]["items"] == []


@pytest.mark.skipif(
    not os.environ.get("DAEM0NMCP_TASK_REDIS_URL"),
    reason="real authenticated Valkey certification endpoint is not configured",
)
@pytest.mark.parametrize("transport", ["stdio", "streamable-http"])
async def test_five_source_consolidation_runs_as_durable_task(tmp_path, transport):
    roots = (tmp_path / "target",) + tuple(
        tmp_path / f"source-{index}" for index in range(5)
    )
    workspaces = []
    for root in roots:
        storage = root / ".daem0nmcp" / "storage"
        storage.mkdir(parents=True)
        manager = DatabaseManager(str(storage))
        try:
            await manager.init_db()
        finally:
            await manager.close()
        workspaces.append(WorkspaceRegistry([root], default_root=root).default)
    target, *sources = workspaces
    environment = {"DAEM0NMCP_TASK_REDIS_URL": os.environ["DAEM0NMCP_TASK_REDIS_URL"]}
    apply_task: mcp_types.Task | None = None
    release_lock: asyncio.Task[None] | None = None
    async with process_client(
        target.root,
        transport,
        workspace_roots=tuple(source.root for source in sources),
        environment_overrides=environment,
    ) as session:
        for workspace in workspaces:
            await succeed(
                session, "session_brief", {"workspace_id": workspace.workspace_id}
            )
        for index, source in enumerate(sources):
            store_arguments = {
                "record_type": "learning",
                "content": f"durable consolidation source {index}",
                "idempotency_key": f"durable-source-{index:04d}",
            }
            store_preflight = await succeed(
                session,
                "memory_preflight",
                {
                    "workspace_id": source.workspace_id,
                    "target_tool": "memory_store",
                    "target_arguments": store_arguments,
                },
            )
            await succeed(
                session,
                "memory_store",
                {
                    "workspace_id": source.workspace_id,
                    **store_arguments,
                    "preflight_token": store_preflight["preflight_token"],
                },
            )
            link_arguments = {
                "linked_workspace_id": source.workspace_id,
                "relationship": "related",
            }
            link_preflight = await succeed(
                session,
                "memory_preflight",
                {
                    "workspace_id": target.workspace_id,
                    "target_tool": "workspace_link",
                    "target_arguments": link_arguments,
                },
            )
            await succeed(
                session,
                "workspace_link",
                {
                    "workspace_id": target.workspace_id,
                    **link_arguments,
                    "preflight_token": link_preflight["preflight_token"],
                },
            )

        source_ids = [source.workspace_id for source in sources]
        preview_task = await _submit_tool_task(
            session,
            "workspace_consolidation_preview",
            {
                "workspace_id": target.workspace_id,
                "source_workspace_ids": source_ids,
            },
        )
        preview = await _completed_payload(session, preview_task.taskId)
        assert preview["selected"] == 5
        consolidate_arguments = {
            "source_workspace_ids": source_ids,
            "idempotency_key": "durable-consolidate-0001",
            "selection_token": preview["selection_token"],
        }
        preflight = await succeed(
            session,
            "memory_preflight",
            {
                "workspace_id": target.workspace_id,
                "target_tool": "workspace_consolidate",
                "target_arguments": consolidate_arguments,
            },
        )
        # Hold the target activation lock across admission so the first worker
        # cannot enter the operation's mutation region before process shutdown.
        # Releasing shortly afterward lets graceful stdio shutdown drain its
        # cancelled child; HTTP termination exercises startup recovery.
        activation_lock = WorkspaceStorageResolver().locked_active(target)
        activation_lock.__enter__()

        async def delayed_release() -> None:
            await asyncio.sleep(1)
            activation_lock.__exit__(None, None, None)

        release_lock = asyncio.create_task(delayed_release())
        apply_task = await _submit_tool_task(
            session,
            "workspace_consolidate",
            {
                "workspace_id": target.workspace_id,
                **consolidate_arguments,
                "preflight_token": preflight["preflight_token"],
            },
        )

    assert apply_task is not None
    assert release_lock is not None
    await release_lock
    task_database = (
        target.root / ".daem0nmcp" / "storage" / "v7-task-dispatcher.sqlite3"
    )
    with closing(sqlite3.connect(task_database)) as connection:
        state = connection.execute(
            "SELECT state FROM durable_tasks WHERE task_id=?",
            (apply_task.taskId,),
        ).fetchone()[0]
    assert state in {"queued", "running"}

    async with process_client(
        target.root,
        transport,
        workspace_roots=tuple(source.root for source in sources),
        environment_overrides=environment,
    ) as restarted:
        for workspace in workspaces:
            await succeed(
                restarted,
                "session_brief",
                {"workspace_id": workspace.workspace_id},
            )
        result = await _completed_payload(restarted, apply_task.taskId)
        assert result["imported"] == 5
