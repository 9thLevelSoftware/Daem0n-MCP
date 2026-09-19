"""Two stdio server processes writing one workspace store each key exactly once."""

from __future__ import annotations

import asyncio
import sqlite3
from contextlib import closing
from typing import Any

from mcp import ClientSession

from daem0nmcp.storage_activation import resolve_active_database
from tests.api_v7.process_client import call, initialize_workspaces, process_client

SAME_KEY_STORES = 5
DISTINCT_KEY_STORES = 10


async def _store_until_ok(
    session: ClientSession,
    workspace_id: str,
    key: str,
    content: str,
) -> list[dict[str, Any]]:
    """Preflight and store, retrying only what the server marks retryable."""
    scope = {"workspace_id": workspace_id}
    arguments = {"record_type": "decision", "content": content, "idempotency_key": key}
    observed: list[dict[str, Any]] = []
    deadline = asyncio.get_running_loop().time() + 120
    while True:
        response = await call(
            session,
            "memory_preflight",
            {**scope, "target_tool": "memory_store", "target_arguments": arguments},
        )
        if response["ok"]:
            token = response["data"]["preflight_token"]
            response = await call(
                session,
                "memory_store",
                {**scope, **arguments, "preflight_token": token},
            )
        observed.append(response)
        if response["ok"]:
            return observed
        assert response["error"]["code"] == "DATABASE_IN_USE", observed
        assert response["error"]["retryable"] is True, observed
        assert asyncio.get_running_loop().time() < deadline, observed
        await asyncio.sleep(response["error"]["retry_after_ms"] / 1000)


async def test_two_stdio_servers_store_each_key_exactly_once(tmp_path):
    [workspace] = await initialize_workspaces((tmp_path,))
    scope = {"workspace_id": workspace.workspace_id}
    async with (
        process_client(tmp_path, "stdio", log_name="stdio-server-a.log") as first,
        process_client(tmp_path, "stdio", log_name="stdio-server-b.log") as second,
    ):
        sessions = (first, second)
        for session in sessions:
            assert (await call(session, "session_brief", scope))["ok"]
        stores = [
            ("shared-store-key-0001", "One decision stored from two processes")
        ] * SAME_KEY_STORES + [
            (f"distinct-store-key-{index:04d}", f"Distinct decision {index}")
            for index in range(DISTINCT_KEY_STORES)
        ]
        results = await asyncio.gather(
            *(
                _store_until_ok(
                    sessions[index % 2], workspace.workspace_id, key, content
                )
                for index, (key, content) in enumerate(stores)
            )
        )

    responses = [response for observed in results for response in observed]
    assert all(
        response["ok"] or response["error"]["code"] == "DATABASE_IN_USE"
        for response in responses
    ), responses
    record_ids = {observed[-1]["data"]["record"]["record_id"] for observed in results}
    assert len(record_ids) == 1 + DISTINCT_KEY_STORES
    replays = [observed[-1]["data"]["idempotent_replay"] for observed in results]
    assert replays[:SAME_KEY_STORES].count(False) == 1, replays

    database = resolve_active_database(tmp_path / ".daem0nmcp" / "storage").path
    with closing(sqlite3.connect(database)) as connection:
        per_stream = dict(
            connection.execute(
                "SELECT stream_id, count(*) FROM memory_events "
                "WHERE event_type='memory.created' GROUP BY stream_id"
            ).fetchall()
        )
        records = connection.execute("SELECT count(*) FROM memory_records").fetchone()
    assert per_stream == dict.fromkeys(record_ids, 1)
    assert records[0] == 1 + DISTINCT_KEY_STORES
