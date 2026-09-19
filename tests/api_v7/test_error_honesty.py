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
from daem0nmcp.api.v7.production import create_v7_server
from daem0nmcp.api.v7.resource_repository import (
    ResourceRepositoryError,
    SQLiteResourceRepository,
)
from daem0nmcp.api.v7.resources import ResourceReadRequest
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


async def test_resource_reads_report_a_full_worker_pool_as_database_in_use(tmp_path):
    [workspace] = await initialize_workspaces((tmp_path,))
    pool = BoundedWorkerPool(max_workers=1, thread_name_prefix="test-busy")
    release = threading.Event()
    occupant = asyncio.create_task(pool.run(lambda: release.wait(timeout=30)))
    for _ in range(100):
        if pool.in_flight:
            break
        await asyncio.sleep(0)
    try:
        repository = SQLiteResourceRepository(
            lambda _workspace: pytest.fail("a full pool must not open storage"),
            worker_pool=pool,
        )
        with pytest.raises(ResourceRepositoryError) as caught:
            await repository.read_warnings(
                workspace,
                ResourceReadRequest("warnings", 10, "updated_at_desc"),
            )
        assert caught.value.code == "DATABASE_IN_USE"
    finally:
        release.set()
        await occupant
        pool.shutdown()
