"""Transient contention is retryable and every internal error is logged (KD-6)."""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import threading
from contextlib import closing
from pathlib import Path
from typing import Any

import pytest
from fastmcp import Client

from daem0nmcp.api.v7 import runtime_services
from daem0nmcp.api.v7.production import (
    _briefing_reader,
    _guidance_reader,
    create_v7_server,
)
from daem0nmcp.api.v7.resource_repository import build_sqlite_resource_readers
from daem0nmcp.api.v7.runtime_services import (
    BasicBriefingService,
    BasicPreflightService,
    RuntimeServiceError,
)
from daem0nmcp.api.v7.tools import SessionBriefInput
from daem0nmcp.bounded_workers import BoundedWorkerPool
from daem0nmcp.config import Settings
from daem0nmcp.storage_activation import resolve_active_database
from tests.api_v7.process_client import initialize_workspaces

FLOWS = 8


def _server(root: Path):
    return create_v7_server(
        "stdio",
        settings=Settings(
            project_root=str(root),
            workspace_roots=[str(root)],
            dream_enabled=False,
        ),
        environ={},
    )


def _database(root: Path) -> Path:
    return resolve_active_database(root / ".daem0nmcp" / "storage").path


def _is_retryable_busy(response: dict[str, Any]) -> bool:
    error = response["error"]
    return (
        error["code"] == "DATABASE_IN_USE"
        and error["retryable"] is True
        and error["retry_after_ms"] == 250
    )


class _Session:
    def __init__(self, client: Client, workspace_id: str) -> None:
        self._client = client
        self.workspace_id = workspace_id

    async def call(self, tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
        result = await self._client.call_tool(
            tool,
            {"workspace_id": self.workspace_id, **arguments},
            raise_on_error=False,
        )
        assert isinstance(result.structured_content, dict), tool
        return result.structured_content

    async def preflight_and_store(self, index: int) -> dict[str, Any]:
        arguments = {
            "record_type": "decision",
            "content": f"Concurrent decision number {index}",
            "idempotency_key": f"concurrent-store-{index:04d}",
        }
        preflight = await self.call(
            "memory_preflight",
            {"target_tool": "memory_store", "target_arguments": arguments},
        )
        if not preflight["ok"]:
            return preflight
        return await self.call(
            "memory_store",
            {**arguments, "preflight_token": preflight["data"]["preflight_token"]},
        )


async def test_parallel_stores_are_ok_or_retryable_and_land_exactly_once(tmp_path):
    [workspace] = await initialize_workspaces((tmp_path,))
    async with Client(_server(tmp_path)) as client:
        session = _Session(client, workspace.workspace_id)
        assert (await session.call("session_brief", {}))["ok"]

        async def flow(index: int) -> list[dict[str, Any]]:
            observed = []
            deadline = asyncio.get_running_loop().time() + 60
            while True:
                response = await session.preflight_and_store(index)
                observed.append(response)
                if response["ok"]:
                    return observed
                assert _is_retryable_busy(response), response
                assert asyncio.get_running_loop().time() < deadline, observed
                await asyncio.sleep(response["error"]["retry_after_ms"] / 1000)

        results = await asyncio.gather(*(flow(index) for index in range(FLOWS)))

    for observed in results:
        assert observed[-1]["ok"], observed
    # Logged, not asserted: a fast host may never contend.
    print("DATABASE_IN_USE retries:", sum(len(observed) - 1 for observed in results))
    with closing(sqlite3.connect(_database(tmp_path))) as connection:
        records = connection.execute("SELECT count(*) FROM memory_records").fetchone()
        events = connection.execute(
            "SELECT count(*) FROM memory_events WHERE event_type='memory.created'"
        ).fetchone()
    assert (records[0], events[0]) == (FLOWS, FLOWS)


async def test_store_behind_a_held_write_lock_is_retryable_database_in_use(tmp_path):
    [workspace] = await initialize_workspaces((tmp_path,))
    async with Client(_server(tmp_path)) as client:
        session = _Session(client, workspace.workspace_id)
        assert (await session.call("session_brief", {}))["ok"]
        with closing(
            sqlite3.connect(_database(tmp_path), isolation_level=None)
        ) as holder:
            holder.execute("BEGIN IMMEDIATE")
            try:
                # Waits out the writer's 5 s SQLite busy timeout.
                blocked = await session.preflight_and_store(1)
            finally:
                holder.execute("ROLLBACK")
        assert not blocked["ok"]
        assert _is_retryable_busy(blocked), blocked
        retried = await session.preflight_and_store(1)
        assert retried["ok"], retried


async def test_internal_error_is_logged_under_the_returned_correlation_id(
    tmp_path, monkeypatch, caplog
):
    def explode(*_arguments, **_keywords):
        raise RuntimeError("induced-failure-canary")

    # Raised inside the writer transaction, so it reaches the log only through
    # the MEMORY_STORE_FAILED exception chain.
    monkeypatch.setattr(runtime_services, "_existing_idempotent_event", explode)
    [workspace] = await initialize_workspaces((tmp_path,))
    async with Client(_server(tmp_path)) as client:
        session = _Session(client, workspace.workspace_id)
        assert (await session.call("session_brief", {}))["ok"]
        with caplog.at_level(logging.ERROR, logger="daem0nmcp.api.v7.responses"):
            response = await session.preflight_and_store(1)

    assert not response["ok"]
    error = response["error"]
    assert (error["code"], error["retryable"]) == ("INTERNAL_ERROR", False)
    assert "induced-failure-canary" not in json.dumps(response)
    [record] = [
        record
        for record in caplog.records
        if record.name == "daem0nmcp.api.v7.responses"
    ]
    assert error["correlation_id"] in record.getMessage()
    assert "induced-failure-canary" in caplog.text
    assert "MEMORY_STORE_FAILED" in caplog.text


@pytest.mark.parametrize("contention", ["full_pool", "overrun_read"])
@pytest.mark.parametrize("service", ["memory_preflight", "session_brief"])
async def test_contended_briefing_snapshot_is_retryable_database_in_use(
    tmp_path, service, contention
):
    """The CI failure path: guidance/briefing -> read_briefing_snapshot -> _run.

    MCP resource URIs still report an opaque access failure; the repository
    code only matters on these two service paths.
    """
    [workspace] = await initialize_workspaces((tmp_path,))
    storage = tmp_path / ".daem0nmcp" / "storage"
    pool = BoundedWorkerPool(max_workers=1, thread_name_prefix="test-contention")
    release = threading.Event()

    def resolve(_workspace):
        if contention == "overrun_read":
            release.wait(timeout=30)  # overrun the 0.05 s deadline, then succeed
        return resolve_active_database(storage)

    readers = build_sqlite_resource_readers(
        resolve, timeout_seconds=0.05, worker_pool=pool
    )

    async def pool_occupied() -> None:
        deadline = asyncio.get_running_loop().time() + 10
        while not pool.in_flight:
            assert asyncio.get_running_loop().time() < deadline
            await asyncio.sleep(0.01)

    occupant = None
    if contention == "full_pool":
        occupant = asyncio.create_task(pool.run(lambda: release.wait(timeout=30)))
        await pool_occupied()
    if service == "memory_preflight":
        call = BasicPreflightService(reader=_guidance_reader(readers)).guidance(
            workspace, "memory_store", {}, "Plan a decision."
        )
    else:
        call = BasicBriefingService(reader=_briefing_reader(readers)).assemble(
            workspace, SessionBriefInput(workspace_id=workspace.workspace_id)
        )
    task = asyncio.ensure_future(call)
    try:
        if contention == "overrun_read":
            await pool_occupied()
            await asyncio.sleep(0.2)
            release.set()
        with pytest.raises(RuntimeServiceError) as caught:
            await task
    finally:
        release.set()
        if occupant is not None:
            await occupant
        pool.shutdown()
    assert caught.value.code == "DATABASE_IN_USE"


async def test_exclusive_storage_lock_is_database_in_use_not_unavailable(tmp_path):
    """A migration/bootstrap holding the storage lock is transient contention."""
    from daem0nmcp.api.v7.runtime_services import WorkspaceStorageResolver
    from daem0nmcp.storage_activation import DatabaseFileLock

    [workspace] = await initialize_workspaces((tmp_path,))
    holder = DatabaseFileLock(tmp_path / ".daem0nmcp" / "storage", "exclusive")
    holder.acquire()
    try:
        with (
            pytest.raises(RuntimeServiceError) as caught,
            WorkspaceStorageResolver().locked_active(workspace),
        ):
            pytest.fail("a shared read was admitted under an exclusive lock")
    finally:
        holder.release()
    assert caught.value.code == "DATABASE_IN_USE"
    with WorkspaceStorageResolver().locked_active(workspace) as active:
        assert active.format_version == 7


def test_internal_error_log_omits_pydantic_input_values(caplog):
    from pydantic import BaseModel, ValidationError

    from daem0nmcp.api.v7.responses import ResponseFactory

    class Guidance(BaseModel):
        priority: int

    # Built at runtime so the traceback's source lines cannot contain it.
    stored_content = "-".join(("secret", "memory", "fragment", "canary"))
    try:
        try:
            Guidance.model_validate({"priority": stored_content})
        except ValidationError as invalid:
            raise RuntimeError("PREFLIGHT_FAILED") from invalid
    except RuntimeError as error:
        with caplog.at_level(logging.ERROR, logger="daem0nmcp.api.v7.responses"):
            response = ResponseFactory().begin(None).internal_error(error)

    assert response.error.correlation_id in caplog.text
    assert "priority (int_parsing)" in caplog.text
    assert "PREFLIGHT_FAILED" in caplog.text
    assert "test_internal_error_log_omits_pydantic_input_values" in caplog.text
    assert "secret-memory-fragment-canary" not in caplog.text
