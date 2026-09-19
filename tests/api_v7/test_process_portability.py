"""Production transport proof for retained-storage restore and aged pruning."""

from __future__ import annotations

import asyncio
import sqlite3
from contextlib import closing
from datetime import datetime, timedelta, timezone

import pytest

from daem0nmcp.api.v7.runtime_services import WorkspaceStorageResolver
from daem0nmcp.database import DatabaseManager
from daem0nmcp.event_store import EventCommand, EventStore
from daem0nmcp.workspace import WorkspaceRegistry
from tests.api_v7.process_client import process_client, succeed
from tests.api_v7.test_process_surface import _preflight, _store


async def _export_pages(session, scope, legacy):
    page = await succeed(
        session,
        "workspace_export",
        {**scope, "include_legacy_projection": legacy, "page_byte_limit": 65536},
    )
    pages = [page]
    while not page["complete"]:
        page = await succeed(
            session,
            "workspace_export",
            {
                **scope,
                "include_legacy_projection": legacy,
                "page_byte_limit": 65536,
                "export_session_id": page["export_session_id"],
                "page_index": page["page_index"] + 1,
                "cursor": page["next_cursor"],
            },
        )
        pages.append(page)
    return pages


@pytest.mark.parametrize("transport", ["stdio", "streamable-http"])
@pytest.mark.parametrize("legacy", [False, True])
async def test_production_export_restores_retained_workspace(
    tmp_path, transport, legacy
):
    workspace = WorkspaceRegistry([tmp_path], default_root=tmp_path).default
    scope = {"workspace_id": workspace.workspace_id}
    async with process_client(tmp_path, transport) as session:
        await succeed(session, "session_brief", scope)
        record = await _store(
            session,
            scope,
            content="Retained portable heron decision.",
            key="portable-process-store-0001",
        )
        outcome_args = {
            "record_id": record["record_id"],
            "outcome_text": "Heron restore succeeded.",
            "worked": True,
            "idempotency_key": "portable-process-outcome-0001",
        }
        await succeed(session, "memory_record_outcome", {**scope, **outcome_args})
        pages = await _export_pages(session, scope, legacy)
        event_ids = [event["event_id"] for page in pages for event in page["events"]]
        assert len(event_ids) == 2
        assert all(page["legacy_projection_included"] is legacy for page in pages)
        if legacy:
            assert any(page["legacy_rows"] for page in pages)
        else:
            assert all(not page["legacy_rows"] for page in pages)
        expected_root = pages[0]["root_hash"]

    # Both directories are inside this test's temporary workspace. Preserve the
    # source generation; a subsequent real server initializes empty storage.
    storage = tmp_path / ".daem0nmcp" / "storage"
    retained = tmp_path / ".daem0nmcp" / "retained-before-restore"
    assert storage.resolve().is_relative_to(tmp_path.resolve())
    assert retained.resolve().is_relative_to(tmp_path.resolve())
    # Windows may briefly keep a handle open inside storage after the server
    # exits (seen with the graph profile on), so retry within a bounded deadline.
    rename_deadline = asyncio.get_running_loop().time() + 15
    while True:
        try:
            storage.rename(retained)
            break
        except PermissionError:
            if asyncio.get_running_loop().time() >= rename_deadline:
                raise
            await asyncio.sleep(0.2)
    async with process_client(tmp_path, transport) as session:
        await succeed(session, "session_brief", scope)
        import_id = None
        for index, page in enumerate(pages):
            arguments = {
                "bundle": page,
                "finalize": False,
                "idempotency_key": "portable-process-import-0001",
            }
            if import_id is not None:
                arguments["import_session_id"] = import_id
            staged = await succeed(
                session,
                "workspace_import",
                {
                    **scope,
                    **arguments,
                    "preflight_token": await _preflight(
                        session, scope, "workspace_import", arguments
                    ),
                },
            )
            import_id = staged["import_session_id"]
            assert staged["staged_pages"] == index + 1
            assert staged["imported"] == 0
        finish = {
            "import_session_id": import_id,
            "finalize": True,
            "idempotency_key": "portable-process-import-0001",
        }
        restored = await succeed(
            session,
            "workspace_import",
            {
                **scope,
                **finish,
                "preflight_token": await _preflight(
                    session, scope, "workspace_import", finish
                ),
            },
        )
        assert restored["status"] == "succeeded"
        assert restored["imported"] == 2
        assert restored["root_hash"] == expected_root
        again = await _export_pages(session, scope, legacy)
        assert again[0]["root_hash"] == expected_root
        assert [
            event["event_id"] for page in again for event in page["events"]
        ] == event_ids
    assert retained.is_dir()
    async with process_client(tmp_path, transport) as session:
        await succeed(session, "session_brief", scope)
        recalled = await succeed(
            session, "memory_recall", {**scope, "query": "portable heron"}
        )
        assert record["record_id"] in {
            item["record"]["record_id"] for item in recalled["items"]
        }


@pytest.mark.parametrize("transport", ["stdio", "streamable-http"])
async def test_production_prune_aged_canonical_record(tmp_path, transport):
    workspace = WorkspaceRegistry([tmp_path], default_root=tmp_path).default
    storage = tmp_path / ".daem0nmcp" / "storage"
    storage.mkdir(parents=True)
    manager = DatabaseManager(str(storage))
    await manager.init_db()
    await manager.close()
    record_id = "mem_" + "a" * 64
    old_us = int(
        (datetime.now(timezone.utc) - timedelta(days=120)).timestamp() * 1_000_000
    )
    with (
        WorkspaceStorageResolver().locked_active(workspace) as active,
        closing(sqlite3.connect(active.path)) as connection,
        connection,
    ):
        EventStore(connection).append_and_project(
            EventCommand(
                workspace_id=workspace.workspace_id,
                stream_id=record_id,
                stream_kind="memory",
                event_type="memory.created",
                occurred_at_us=old_us,
                recorded_at_us=old_us,
                actor_type="system",
                payload={
                    "record": {
                        "record_type": "decision",
                        "content": "Old prune heron.",
                        "context": {},
                        "tags": [],
                        "pinned": False,
                        "archived": False,
                        "is_permanent": False,
                        "recall_count": 0,
                        "legacy_type": None,
                        "rationale": None,
                        "file_path": None,
                        "file_path_relative": None,
                        "keywords": None,
                        "outcome": None,
                        "worked": None,
                        "surprise_score": None,
                        "importance_score": None,
                        "source_client": "process-fixture",
                        "source_model": None,
                        "deleted_at_us": None,
                    }
                },
            )
        )
    scope = {"workspace_id": workspace.workspace_id}
    async with process_client(tmp_path, transport) as session:
        await succeed(session, "session_brief", scope)
        criteria = {"older_than_days": 90, "protect_successful": True}
        preview = await succeed(session, "memory_prune_preview", {**scope, **criteria})
        assert preview["counts"]["selected"] == 1
        assert preview["sample_ids"] == [record_id]
        arguments = {**criteria, "selection_token": preview["selection_token"]}
        receipt = await succeed(
            session,
            "memory_prune",
            {
                **scope,
                **arguments,
                "preflight_token": await _preflight(
                    session, scope, "memory_prune", arguments
                ),
            },
        )
        assert receipt["affected_ids"] == [record_id]
        after = await succeed(session, "memory_prune_preview", {**scope, **criteria})
        assert after["counts"]["selected"] == 0
