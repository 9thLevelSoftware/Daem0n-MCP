"""A record's version history must survive occurrence times out of append order."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from daem0nmcp.database import DatabaseManager
from daem0nmcp.workspace import WorkspaceRegistry
from tests.api_v7.process_client import process_client, succeed
from tests.api_v7.test_process_surface import _preflight


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


def _instant(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


async def _store(session, scope, *, key, happened_at=None):
    arguments = {
        "record_type": "decision",
        "content": f"Version history record {key}.",
        "idempotency_key": key,
    }
    if happened_at is not None:
        arguments["happened_at"] = happened_at
    stored = await succeed(
        session,
        "memory_store",
        {
            **scope,
            **arguments,
            "preflight_token": await _preflight(
                session, scope, "memory_store", arguments
            ),
        },
    )
    return stored["record"]["record_id"]


async def _record_outcome(session, scope, record_id, *, key, happened_at=None):
    arguments = {
        "record_id": record_id,
        "outcome_text": "The decision held up in practice.",
        "worked": True,
        "idempotency_key": key,
    }
    if happened_at is not None:
        arguments["happened_at"] = happened_at
    await succeed(session, "memory_record_outcome", {**scope, **arguments})


async def _history(session, scope, record_id):
    versions = await succeed(
        session, "memory_versions_list", {**scope, "record_id": record_id}
    )
    assert not versions["truncated"]
    return versions["items"]


async def _version_at(session, scope, record_id, valid_time):
    at_time = await succeed(
        session,
        "memory_at_time_get",
        {**scope, "record_id": record_id, "valid_time": valid_time},
    )
    return at_time["version_id"]


async def test_backdated_outcome_and_future_store_keep_their_history(
    initialized_workspace,
):
    """Either write used to turn the record's history into IMPORT_INVALID."""

    workspace = initialized_workspace
    scope = {"workspace_id": workspace.workspace_id}
    async with process_client(workspace.root, "stdio") as session:
        await succeed(session, "session_brief", scope)

        backdated = await _store(session, scope, key="history-backdated-0001")
        await _record_outcome(
            session,
            scope,
            backdated,
            key="history-backdated-outcome-0001",
            happened_at="2020-01-01T00:00:00Z",
        )
        created, outcome = await _history(session, scope, backdated)
        assert created["record"]["current_status"] == "superseded"
        assert outcome["record"]["current_status"] == "current"
        assert _instant(outcome["valid_from"]) == datetime(
            2020, 1, 1, tzinfo=timezone.utc
        )
        assert outcome["valid_to"] is None
        # The outcome was recorded later and reaches back before the store,
        # so the stored version is never the valid one any more.
        assert _instant(created["valid_to"]) == _instant(created["valid_from"])
        assert _instant(created["transaction_time"]) <= _instant(
            outcome["transaction_time"]
        )
        assert (
            await _version_at(session, scope, backdated, created["valid_from"])
            == outcome["version_id"]
        )

        future = await _store(
            session,
            scope,
            key="history-future-0001",
            happened_at="2099-01-01T00:00:00Z",
        )
        await _record_outcome(session, scope, future, key="history-future-outcome-0001")
        created, outcome = await _history(session, scope, future)
        assert _instant(created["valid_from"]) == datetime(
            2099, 1, 1, tzinfo=timezone.utc
        )
        assert _instant(created["valid_to"]) == _instant(created["valid_from"])
        assert outcome["valid_to"] is None
        assert _instant(outcome["valid_from"]) < _instant(created["valid_from"])
        assert (
            await _version_at(session, scope, future, "2100-01-01T00:00:00Z")
            == outcome["version_id"]
        )
