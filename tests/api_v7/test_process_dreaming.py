"""Real-process certification for production v7 dreaming lifecycle."""

from __future__ import annotations

import asyncio
import sqlite3
import time

import pytest

from daem0nmcp.database import DatabaseManager
from daem0nmcp.event_store import EventCommand, EventStore
from daem0nmcp.workspace import WorkspaceRegistry
from tests.api_v7.process_client import process_client, succeed


@pytest.fixture
async def dreaming_workspace(tmp_path):
    storage = tmp_path / ".daem0nmcp" / "storage"
    storage.mkdir(parents=True)
    manager = DatabaseManager(str(storage))
    try:
        await manager.init_db()
    finally:
        await manager.close()
    return WorkspaceRegistry([tmp_path], default_root=tmp_path).default


async def _store(session, workspace_id: str, suffix: str, content: str) -> str:
    arguments = {
        "record_type": "decision",
        "content": content,
        "idempotency_key": f"dream-process-store-{suffix}",
    }
    preflight = await succeed(
        session,
        "memory_preflight",
        {
            "workspace_id": workspace_id,
            "target_tool": "memory_store",
            "target_arguments": arguments,
            "description": "Create deterministic dreaming process evidence",
        },
    )
    result = await succeed(
        session,
        "memory_store",
        {
            "workspace_id": workspace_id,
            **arguments,
            "preflight_token": preflight["preflight_token"],
        },
    )
    return result["record"]["record_id"]


async def _outcome(
    session, workspace_id: str, record_id: str, suffix: str, worked: bool
) -> None:
    await succeed(
        session,
        "memory_record_outcome",
        {
            "workspace_id": workspace_id,
            "record_id": record_id,
            "outcome_text": "Verified process evidence",
            "worked": worked,
            "idempotency_key": f"dream-process-outcome-{suffix}",
        },
    )


@pytest.mark.parametrize("transport", ["stdio", "streamable-http"])
async def test_production_dreaming_runs_idempotently_and_reports_missing_graph(
    dreaming_workspace,
    transport: str,
) -> None:
    workspace = dreaming_workspace
    environment = {
        "DAEM0NMCP_DREAM_IDLE_TIMEOUT": "0.20",
        "DAEM0NMCP_DREAM_MIN_DECISION_AGE_HOURS": "0",
        "DAEM0NMCP_DREAM_PENDING_MIN_AGE_HOURS": "0",
        "DAEM0NMCP_DREAM_PENDING_EVIDENCE_THRESHOLD": "1",
        "DAEM0NMCP_DREAM_REVIEW_COOLDOWN_HOURS": "72",
    }
    scope = {"workspace_id": workspace.workspace_id}
    async with process_client(
        workspace.root,
        transport,
        environment_overrides=environment,
    ) as session:
        await succeed(session, "session_brief", scope)
        failed = await _store(
            session,
            workspace.workspace_id,
            "failed-0001",
            "CacheManager rollout failed",
        )
        evidence = await _store(
            session,
            workspace.workspace_id,
            "evidence-0001",
            "CacheManager rollout passed",
        )
        await _outcome(session, workspace.workspace_id, failed, "failed-0001", False)
        await _outcome(session, workspace.workspace_id, evidence, "evidence-0001", True)
        await asyncio.sleep(1.0)
        candidates = await succeed(
            session, "memory_capture_list", {**scope, "limit": 25}
        )
        dreaming = [
            item for item in candidates["items"] if item["source_kind"] == "dreaming"
        ]
        health = await succeed(session, "system_health", scope)
        assert dreaming, health.get("dreaming")
        assert health["dreaming"]["enabled"] is True
        graph = {item["strategy"]: item for item in health["dreaming"]["strategies"]}
        assert graph["community_refresh"]["status"] == "disabled"
        assert (
            graph["community_refresh"]["stable_error_code"]
            == "GRAPH_CAPABILITY_UNAVAILABLE"
        )
        count = len(dreaming)

    async with process_client(
        workspace.root,
        transport,
        environment_overrides=environment,
    ) as session:
        await succeed(session, "session_brief", scope)
        await asyncio.sleep(1.0)
        replay = await succeed(session, "memory_capture_list", {**scope, "limit": 25})
        assert (
            len([item for item in replay["items"] if item["source_kind"] == "dreaming"])
            == count
        )


@pytest.mark.parametrize("transport", ["stdio", "streamable-http"])
async def test_production_dreaming_builds_real_graph_generation(
    dreaming_workspace,
    transport: str,
) -> None:
    workspace = dreaming_workspace
    database = workspace.root / ".daem0nmcp" / "storage" / "daem0nmcp.db"
    old = time.time_ns() // 1_000 - 3_600_000_000
    with sqlite3.connect(database) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        EventStore(connection).append_and_project(
            EventCommand(
                workspace_id=workspace.workspace_id,
                stream_id="mem_" + "7" * 64,
                stream_kind="memory",
                event_type="memory.created",
                occurred_at_us=old,
                recorded_at_us=old,
                actor_type="system",
                payload={
                    "record": {
                        "record_type": "observation",
                        "legacy_type": None,
                        "content": "CacheManager calls refresh_cache() in cache.py",
                        "rationale": None,
                        "context": {},
                        "tags": [],
                        "file_path": None,
                        "file_path_relative": None,
                        "keywords": None,
                        "is_permanent": False,
                        "pinned": False,
                        "archived": False,
                        "outcome": None,
                        "worked": None,
                        "recall_count": 0,
                        "surprise_score": None,
                        "importance_score": None,
                        "source_client": "dream-process",
                        "source_model": None,
                        "deleted_at_us": None,
                    }
                },
            )
        )
        connection.commit()
    environment = {
        "DAEM0NMCP_GRAPH_ENABLED": "true",
        "DAEM0NMCP_DREAM_IDLE_TIMEOUT": "0.20",
        "DAEM0NMCP_DREAM_COMMUNITY_STALENESS_THRESHOLD": "1",
    }
    scope = {"workspace_id": workspace.workspace_id}
    async with process_client(
        workspace.root,
        transport,
        environment_overrides=environment,
    ) as session:
        await succeed(session, "session_brief", scope)
        await asyncio.sleep(2.0)
        health = await succeed(session, "system_health", scope)
        strategies = {
            item["strategy"]: item for item in health["dreaming"]["strategies"]
        }
        assert strategies["community_refresh"]["status"] == "idle"
        assert strategies["community_refresh"]["last_success_at"] is not None
    with sqlite3.connect(database) as connection:
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM projection_manifests WHERE workspace_id=? "
                "AND projection_name='graph' AND status='active'",
                (workspace.workspace_id,),
            ).fetchone()[0]
            == 1
        )
