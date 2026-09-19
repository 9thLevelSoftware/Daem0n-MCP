"""Two stdio server processes writing one workspace store each key exactly once."""

from __future__ import annotations

import asyncio
import sqlite3
from collections.abc import Callable
from contextlib import closing
from pathlib import Path
from typing import Any

from mcp import ClientSession

from daem0nmcp.storage_activation import resolve_active_database
from tests.api_v7.process_client import call, initialize_workspaces, process_client

SAME_KEY_STORES = 5
DISTINCT_KEY_STORES = 10
LOG_NAMES = ("stdio-server-a.log", "stdio-server-b.log")


def _server_log_tails(root: Path, lines: int = 40) -> str:
    """Server stderr (tracebacks by correlation ID) for assertion messages."""
    tails = []
    for name in LOG_NAMES:
        try:
            text = (root / name).read_text(encoding="utf-8", errors="replace")
        except OSError as error:
            text = f"<unreadable: {error}>"
        tails.append(f"--- {name} ---\n" + "\n".join(text.splitlines()[-lines:]))
    return "\n".join(tails)


async def _store_until_ok(
    session: ClientSession,
    workspace_id: str,
    key: str,
    content: str,
    diagnostics: Callable[[], str],
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
        retryable = (
            response["error"]["code"] == "DATABASE_IN_USE"
            and response["error"]["retryable"] is True
        )
        assert retryable, (observed, diagnostics())
        assert asyncio.get_running_loop().time() < deadline, (observed, diagnostics())
        await asyncio.sleep(response["error"]["retry_after_ms"] / 1000)


async def test_two_stdio_servers_store_each_key_exactly_once(tmp_path):
    [workspace] = await initialize_workspaces((tmp_path,))
    scope = {"workspace_id": workspace.workspace_id}
    async with (
        process_client(tmp_path, "stdio", log_name=LOG_NAMES[0]) as first,
        process_client(tmp_path, "stdio", log_name=LOG_NAMES[1]) as second,
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
                    sessions[index % 2],
                    workspace.workspace_id,
                    key,
                    content,
                    lambda: _server_log_tails(tmp_path),
                )
                for index, (key, content) in enumerate(stores)
            )
        )

    # Logged, not asserted: a fast host may never contend.
    print("DATABASE_IN_USE retries:", sum(len(observed) - 1 for observed in results))
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
