from __future__ import annotations

import asyncio
import importlib.util
import json
import sqlite3
import threading
import time
from pathlib import Path

import pytest

import daem0nmcp.dreaming.v7_runtime as dreaming_runtime
from daem0nmcp.capture_candidates import (
    _CAPTURE_WORKERS,
    CaptureCandidateError,
    CaptureCandidateRequest,
    CaptureCandidateStore,
)
from daem0nmcp.config import Settings
from daem0nmcp.database import DatabaseManager
from daem0nmcp.dreaming.v7_runtime import (
    V7DreamingCoordinator,
    _Analysis,
    _Proposal,
    _WorkspaceRuntime,
)
from daem0nmcp.event_store import EventCommand, EventStore
from daem0nmcp.workspace import WorkspaceRegistry


@pytest.fixture
async def dream_workspace(tmp_path: Path):
    storage = tmp_path / ".daem0nmcp" / "storage"
    storage.mkdir(parents=True)
    manager = DatabaseManager(str(storage))
    await manager.init_db()
    await manager.close()
    workspace = WorkspaceRegistry([tmp_path], default_root=tmp_path).default
    return workspace, storage / "daem0nmcp.db"


def _append(
    database: Path,
    workspace_id: str,
    suffix: str,
    content: str,
    *,
    worked: bool | None,
    record_type: str = "decision",
) -> str:
    record_id = "mem_" + (suffix if len(suffix) == 64 else suffix * 64)
    old = time.time_ns() // 1_000 - 48 * 3_600_000_000
    with sqlite3.connect(database) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        EventStore(connection).append_and_project(
            EventCommand(
                workspace_id=workspace_id,
                stream_id=record_id,
                stream_kind="memory",
                event_type="memory.created",
                occurred_at_us=old,
                recorded_at_us=old,
                actor_type="system",
                payload={
                    "record": {
                        "record_type": record_type,
                        "legacy_type": None,
                        "content": content,
                        "rationale": None,
                        "context": {},
                        "tags": [],
                        "file_path": None,
                        "file_path_relative": None,
                        "keywords": None,
                        "is_permanent": False,
                        "pinned": False,
                        "archived": False,
                        "outcome": "verified" if worked is not None else None,
                        "worked": worked,
                        "recall_count": 0,
                        "surprise_score": None,
                        "importance_score": None,
                        "source_client": "dream-test",
                        "source_model": None,
                        "deleted_at_us": None,
                    }
                },
            )
        )
        connection.commit()
    return record_id


def _append_many_decisions(
    database: Path,
    workspace_id: str,
    count: int,
) -> list[str]:
    old = time.time_ns() // 1_000 - 48 * 3_600_000_000
    record_ids: list[str] = []
    with sqlite3.connect(database) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        store = EventStore(connection)
        for index in range(count):
            record_id = "mem_" + f"{index + 1:064x}"
            store.append_and_project(
                EventCommand(
                    workspace_id=workspace_id,
                    stream_id=record_id,
                    stream_kind="memory",
                    event_type="memory.created",
                    occurred_at_us=old + index,
                    recorded_at_us=old + index,
                    actor_type="system",
                    payload={
                        "record": {
                            "record_type": "decision",
                            "legacy_type": None,
                            "content": f"bounded failed decision {index}",
                            "rationale": None,
                            "context": {},
                            "tags": [],
                            "file_path": None,
                            "file_path_relative": None,
                            "keywords": None,
                            "is_permanent": False,
                            "pinned": False,
                            "archived": False,
                            "outcome": "failed",
                            "worked": False,
                            "recall_count": 0,
                            "surprise_score": None,
                            "importance_score": None,
                            "source_client": "dream-test",
                            "source_model": None,
                            "deleted_at_us": None,
                        }
                    },
                )
            )
            record_ids.append(record_id)
        connection.commit()
    return record_ids


def _settings(root: Path, **changes: object) -> Settings:
    values: dict[str, object] = {
        "project_root": str(root),
        "dream_idle_timeout": 60,
        "dream_min_decision_age_hours": 0,
        "dream_review_cooldown_hours": 72,
        "dream_pending_min_age_hours": 0,
        "dream_pending_cooldown_hours": 72,
        "dream_pending_evidence_threshold": 2,
    }
    values.update(changes)
    return Settings(**values)


@pytest.mark.asyncio
async def test_failed_and_pending_strategies_are_bounded_and_restart_idempotent(
    dream_workspace,
) -> None:
    workspace, database = dream_workspace
    _append(
        database,
        workspace.workspace_id,
        "a",
        "redis session cache failed",
        worked=False,
    )
    _append(
        database,
        workspace.workspace_id,
        "b",
        "redis session cache passed tests",
        worked=True,
    )
    _append(
        database,
        workspace.workspace_id,
        "c",
        "redis session cache passed load",
        worked=True,
    )
    pending = _append(
        database,
        workspace.workspace_id,
        "d",
        "redis session cache pending",
        worked=None,
    )

    first = V7DreamingCoordinator(
        workspaces=(workspace,),
        settings=_settings(workspace.root),
        candidate_store=CaptureCandidateStore(),
        capability_statuses={"graph": "disabled"},
    )
    await first.start()
    await first.run_once(workspace)
    await first.aclose()
    with sqlite3.connect(database) as connection:
        count = connection.execute(
            "SELECT COUNT(*) FROM memory_capture_candidates WHERE source_kind='dreaming'"
        ).fetchone()[0]
        assert count == 2
        assert (
            connection.execute(
                "SELECT worked FROM memory_records WHERE record_id=?", (pending,)
            ).fetchone()[0]
            is None
        )

    restarted = V7DreamingCoordinator(
        workspaces=(workspace,),
        settings=_settings(workspace.root),
        candidate_store=CaptureCandidateStore(),
        capability_statuses={"graph": "disabled"},
    )
    await restarted.start()
    await restarted.run_once(workspace)
    health = await restarted.health(workspace)
    await restarted.aclose()
    with sqlite3.connect(database) as connection:
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM memory_capture_candidates WHERE source_kind='dreaming'"
            ).fetchone()[0]
            == count
        )
    graph = next(
        item for item in health["strategies"] if item["strategy"] == "community_refresh"
    )
    assert graph["status"] == "disabled"
    assert graph["stable_error_code"] == "GRAPH_CAPABILITY_UNAVAILABLE"


@pytest.mark.asyncio
async def test_activity_yield_preserves_disabled_graph_strategy_state(
    dream_workspace,
) -> None:
    workspace, database = dream_workspace
    coordinator = V7DreamingCoordinator(
        workspaces=(workspace,),
        settings=_settings(workspace.root),
        candidate_store=CaptureCandidateStore(),
        capability_statuses={"graph": "disabled"},
    )
    await coordinator.start()

    async def interrupt_with_activity(_state, strategy):
        if strategy == "connection_discovery":
            coordinator.record_activity(workspace)

    coordinator._run_strategy = interrupt_with_activity  # type: ignore[method-assign]
    await coordinator.run_once(workspace)
    health = await coordinator.health(workspace)
    coordinator.record_activity(workspace, False)
    await coordinator.aclose()

    assert health is not None
    community = next(
        item for item in health["strategies"] if item["strategy"] == "community_refresh"
    )
    assert community["status"] == "disabled"
    assert community["stable_error_code"] == "GRAPH_CAPABILITY_UNAVAILABLE"
    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT status,stable_error_code FROM dreaming_strategy_state "
            "WHERE workspace_id=? AND strategy='community_refresh'",
            (workspace.workspace_id,),
        ).fetchone() == ("disabled", "GRAPH_CAPABILITY_UNAVAILABLE")


@pytest.mark.asyncio
async def test_failed_decision_cursor_skips_cooldown_before_session_limit(
    dream_workspace,
) -> None:
    workspace, database = dream_workspace
    expected = {
        _append(
            database,
            workspace.workspace_id,
            suffix,
            "cache decision failed",
            worked=False,
        )
        for suffix in ("a", "b", "c")
    }
    coordinator = V7DreamingCoordinator(
        workspaces=(workspace,),
        settings=_settings(workspace.root, dream_max_decisions_per_session=1),
        candidate_store=CaptureCandidateStore(),
        capability_statuses={"graph": "disabled"},
    )
    await coordinator.start()
    for _ in range(3):
        await coordinator.run_once(workspace)
    await coordinator.aclose()
    with sqlite3.connect(database) as connection:
        rows = connection.execute(
            "SELECT provenance_json FROM memory_capture_candidates WHERE "
            "json_extract(provenance_json,'$.strategy')='failed_decision'"
        ).fetchall()
    assert {json.loads(row[0])["sources"][0]["record_id"] for row in rows} == expected


@pytest.mark.asyncio
async def test_restart_cursor_reaches_records_older_than_previous_thousand_row_cap(
    dream_workspace,
) -> None:
    workspace, database = dream_workspace
    record_ids = _append_many_decisions(database, workspace.workspace_id, 1_002)
    settings = _settings(workspace.root, dream_max_decisions_per_session=1)
    first = V7DreamingCoordinator(
        workspaces=(workspace,),
        settings=settings,
        candidate_store=CaptureCandidateStore(),
        capability_statuses={"graph": "disabled"},
    )
    await first.start()
    await first.run_once(workspace)
    await first.aclose()

    restarted = V7DreamingCoordinator(
        workspaces=(workspace,),
        settings=settings,
        candidate_store=CaptureCandidateStore(),
        capability_statuses={"graph": "disabled"},
    )
    await restarted.start()
    await restarted.run_once(workspace)
    await restarted.aclose()
    with sqlite3.connect(database) as connection:
        rows = connection.execute(
            "SELECT provenance_json FROM memory_capture_candidates WHERE "
            "json_extract(provenance_json,'$.strategy')='failed_decision' "
            "ORDER BY created_at_us,candidate_id"
        ).fetchall()
    assert [
        json.loads(row[0])["sources"][0]["record_id"] for row in rows
    ] == record_ids[:2]


@pytest.mark.asyncio
async def test_foreground_arrival_while_capacity_queued_prevents_strategy_start(
    dream_workspace,
) -> None:
    workspace, _database = dream_workspace
    coordinator = V7DreamingCoordinator(
        workspaces=(workspace,),
        settings=_settings(workspace.root),
        candidate_store=CaptureCandidateStore(),
        capability_statuses={"graph": "disabled"},
    )
    await coordinator.start()
    calls: list[str] = []

    async def observed(_state, strategy):
        calls.append(strategy)

    coordinator._run_strategy = observed  # type: ignore[method-assign]
    permits = coordinator._settings.dream_max_concurrency
    for _ in range(permits):
        await coordinator._semaphore.acquire()
    pending = asyncio.create_task(coordinator.run_once(workspace))
    await asyncio.sleep(0)
    coordinator.record_activity(workspace, True)
    for _ in range(permits):
        coordinator._semaphore.release()
    await pending
    assert calls == []
    coordinator.record_activity(workspace, False)
    await coordinator.aclose()


@pytest.mark.asyncio
async def test_foreground_activity_never_waits_for_worker_publication_lock(
    dream_workspace,
) -> None:
    workspace, _database = dream_workspace
    coordinator = object.__new__(V7DreamingCoordinator)
    state = _WorkspaceRuntime(workspace, time.monotonic())
    coordinator._states = {workspace.workspace_id: state}
    held = threading.Event()

    def hold_publication() -> None:
        with state.publication_lock:
            held.set()
            time.sleep(0.35)

    holder = threading.Thread(target=hold_publication)
    holder.start()
    assert held.wait(1.0)
    heartbeat = asyncio.create_task(asyncio.sleep(0.05))
    started = time.monotonic()
    coordinator.record_activity(workspace, True)
    assert time.monotonic() - started < 0.05
    await heartbeat
    assert holder.is_alive()
    holder.join(timeout=1.0)
    assert not holder.is_alive()


@pytest.mark.asyncio
async def test_explicit_pending_action_is_audited_and_replay_safe(
    dream_workspace,
) -> None:
    workspace, database = dream_workspace
    pending = _append(
        database, workspace.workspace_id, "d", "postgres migration pending", worked=None
    )
    _append(
        database,
        workspace.workspace_id,
        "e",
        "postgres migration passed tests",
        worked=True,
    )
    _append(
        database,
        workspace.workspace_id,
        "f",
        "postgres migration passed staging",
        worked=True,
    )
    coordinator = V7DreamingCoordinator(
        workspaces=(workspace,),
        settings=_settings(workspace.root, dream_pending_dry_run=False),
        candidate_store=CaptureCandidateStore(),
        capability_statuses={"graph": "disabled"},
    )
    await coordinator.start()
    await coordinator.run_once(workspace)
    await coordinator.run_once(workspace)
    await coordinator.aclose()
    with sqlite3.connect(database) as connection:
        row = connection.execute(
            "SELECT worked,outcome FROM memory_records WHERE record_id=?", (pending,)
        ).fetchone()
        assert row == (1, dreaming_runtime._INFERRED_OUTCOME_TEXT)
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM memory_events WHERE stream_id=? "
                "AND event_type='memory.outcome_recorded'",
                (pending,),
            ).fetchone()[0]
            == 1
        )
        candidate = connection.execute(
            "SELECT proposed_record_json FROM memory_capture_candidates "
            "WHERE json_extract(provenance_json,'$.strategy')='pending_outcome'"
        ).fetchone()
        assert candidate is not None
        assert "committed" in candidate[0].casefold()


@pytest.mark.asyncio
async def test_foreground_activity_yields_and_no_workspace_overlap(
    dream_workspace,
) -> None:
    workspace, _database = dream_workspace
    coordinator = V7DreamingCoordinator(
        workspaces=(workspace,),
        settings=_settings(workspace.root),
        candidate_store=CaptureCandidateStore(),
        capability_statuses={"graph": "disabled"},
    )
    await coordinator.start()
    original = coordinator._analyze
    started = threading.Event()
    guard = threading.Lock()
    active = 0
    maximum = 0

    def slow_analysis(*args, **kwargs):
        nonlocal active, maximum
        with guard:
            active += 1
            maximum = max(maximum, active)
        started.set()
        time.sleep(0.1)
        try:
            return original(*args, **kwargs)
        finally:
            with guard:
                active -= 1

    coordinator._analyze = slow_analysis  # type: ignore[method-assign]
    first = asyncio.create_task(coordinator.run_once(workspace))
    while not started.is_set():
        await asyncio.sleep(0.01)
    second = asyncio.create_task(coordinator.run_once(workspace))
    coordinator.record_activity(workspace, True)
    coordinator.record_activity(workspace, False)
    await asyncio.gather(first, second)
    health = await coordinator.health(workspace)
    assert health is not None
    assert maximum == 1
    assert health["yielded"] is True
    assert any(item["yielded"] for item in health["strategies"])
    assert await coordinator.health(None) is None
    await coordinator.aclose()


def test_dreaming_settings_are_bounded() -> None:
    with pytest.raises(ValueError):
        Settings(dream_max_concurrency=0)
    with pytest.raises(ValueError):
        Settings(dream_connection_min_shared_entities=33)


def test_migration_28_bounds_durable_strategy_state() -> None:
    from daem0nmcp.migrations.schema import MIGRATIONS

    statements = next(sql for version, _description, sql in MIGRATIONS if version == 28)
    connection = sqlite3.connect(":memory:")
    try:
        connection.execute("PRAGMA foreign_keys=ON")
        for statement in statements:
            connection.execute(statement)
        workspace_id = "ws_" + "a" * 24
        connection.execute(
            "INSERT INTO dreaming_strategy_state(workspace_id,strategy,status,"
            "pending_count,yielded,updated_at_us) VALUES (?,?,?,?,?,?)",
            (workspace_id, "failed_decision", "idle", 0, 0, 1),
        )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO dreaming_strategy_state(workspace_id,strategy,status,"
                "pending_count,yielded,updated_at_us) VALUES (?,?,?,?,?,?)",
                (workspace_id, "invented", "idle", 0, 0, 1),
            )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "UPDATE dreaming_strategy_state SET strategy='pending_outcome'"
            )
    finally:
        connection.close()


@pytest.mark.asyncio
async def test_candidate_write_rejects_a_stale_authoritative_source(
    dream_workspace,
) -> None:
    workspace, _database = dream_workspace
    store = CaptureCandidateStore()
    request = CaptureCandidateRequest(
        source_kind="dreaming",
        record={"record_type": "learning", "content": "A bounded proposal."},
        provenance={"strategy": "failed_decision", "sources": []},
        idempotency_key="dream-stale-source-0001",
    )
    with pytest.raises(CaptureCandidateError) as raised:
        await store.stage_validated(
            workspace,
            request,
            source_versions=(("mem_" + "f" * 64, "evt_" + "e" * 64),),
        )
    assert raised.value.code == "CAPTURE_SOURCE_STALE"


@pytest.mark.asyncio
async def test_cancelled_source_scan_and_candidate_transaction_publish_nothing(
    dream_workspace,
) -> None:
    workspace, database = dream_workspace
    _append(
        database,
        workspace.workspace_id,
        "a",
        "cancelled decision scan",
        worked=False,
    )
    _append(
        database,
        workspace.workspace_id,
        "b",
        "second cancelled decision scan",
        worked=False,
    )
    coordinator = V7DreamingCoordinator(
        workspaces=(workspace,),
        settings=_settings(workspace.root),
        candidate_store=CaptureCandidateStore(),
        capability_statuses={"graph": "disabled"},
    )
    await coordinator.start()

    class CancelDuringSourceScan:
        def __init__(self) -> None:
            self.checks = 0

        def is_set(self) -> bool:
            self.checks += 1
            return self.checks > 1

    scan_cancelled = CancelDuringSourceScan()
    analysis = coordinator._analyze(
        workspace,
        "failed_decision",
        scan_cancelled,  # type: ignore[arg-type]
    )
    assert analysis.proposals == ()
    assert analysis.pending_count == 1
    assert scan_cancelled.checks > 1

    request = CaptureCandidateRequest(
        source_kind="dreaming",
        record={"record_type": "learning", "content": "Must not publish."},
        provenance={"strategy": "failed_decision", "sources": []},
        idempotency_key="dream-cancelled-publish-0001",
    )
    cancelled = threading.Event()
    publication_lock = threading.Lock()
    publication_lock.acquire()
    publishing = asyncio.create_task(
        coordinator._candidates.stage_validated(
            workspace,
            request,
            source_versions=(),
            cancelled=cancelled,
            publication_lock=publication_lock,
        )
    )
    await asyncio.sleep(0.05)
    cancelled.set()
    publication_lock.release()
    with pytest.raises(CaptureCandidateError) as raised:
        await publishing
    assert raised.value.code == "CAPTURE_CANCELLED"
    await coordinator.aclose()
    with sqlite3.connect(database) as connection:
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM memory_capture_candidates WHERE "
                "idempotency_key='dream-cancelled-publish-0001'"
            ).fetchone()[0]
            == 0
        )


@pytest.mark.asyncio
async def test_large_truncated_evidence_never_commits_automatic_outcome(
    dream_workspace,
) -> None:
    workspace, database = dream_workspace
    pending = _append(
        database,
        workspace.workspace_id,
        "d",
        "postgres migration pending " + "payload " * 2_000,
        worked=None,
    )
    _append(
        database,
        workspace.workspace_id,
        "e",
        "postgres migration passed " + "payload " * 2_000,
        worked=True,
    )
    _append(
        database,
        workspace.workspace_id,
        "f",
        "postgres migration passed " + "payload " * 2_000,
        worked=True,
    )
    coordinator = V7DreamingCoordinator(
        workspaces=(workspace,),
        settings=_settings(workspace.root, dream_pending_dry_run=False),
        candidate_store=CaptureCandidateStore(),
        capability_statuses={"graph": "disabled"},
    )
    await coordinator.start()
    await coordinator.run_once(workspace)
    await coordinator.aclose()
    with sqlite3.connect(database) as connection:
        assert (
            connection.execute(
                "SELECT worked FROM memory_records WHERE record_id=?", (pending,)
            ).fetchone()[0]
            is None
        )
        proposal = connection.execute(
            "SELECT proposed_record_json FROM memory_capture_candidates WHERE "
            "json_extract(provenance_json,'$.strategy')='pending_outcome' AND "
            "json_extract(provenance_json,'$.sources[0].record_id')=?",
            (pending,),
        ).fetchone()
    assert proposal is not None
    assert "requires more directional evidence" in proposal[0]


@pytest.mark.asyncio
async def test_shutdown_cancels_inflight_scan_and_waits_for_worker(
    dream_workspace,
) -> None:
    workspace, _database = dream_workspace
    coordinator = V7DreamingCoordinator(
        workspaces=(workspace,),
        settings=_settings(workspace.root),
        candidate_store=CaptureCandidateStore(),
        capability_statuses={"graph": "disabled"},
    )
    await coordinator.start()
    entered = threading.Event()
    original = coordinator._analyze

    def cancellable_analysis(workspace_arg, strategy, cancelled):
        entered.set()
        assert cancelled.wait(1.0)
        return original(workspace_arg, strategy, cancelled)

    coordinator._analyze = cancellable_analysis  # type: ignore[method-assign]
    running = asyncio.create_task(coordinator.run_once(workspace))
    while not entered.is_set():
        await asyncio.sleep(0.01)
    await asyncio.wait_for(coordinator.aclose(), timeout=2.0)
    await asyncio.gather(running, return_exceptions=True)
    assert coordinator._pool.in_flight == 0


@pytest.mark.asyncio
async def test_shutdown_drains_owned_candidate_publication_worker(
    dream_workspace,
) -> None:
    workspace, database = dream_workspace
    coordinator = V7DreamingCoordinator(
        workspaces=(workspace,),
        settings=_settings(workspace.root),
        candidate_store=CaptureCandidateStore(),
        capability_statuses={"graph": "disabled"},
    )
    await coordinator.start()
    proposal = _Proposal(
        request=CaptureCandidateRequest(
            source_kind="dreaming",
            record={"record_type": "learning", "content": "shutdown candidate"},
            provenance={"strategy": "failed_decision", "sources": []},
            idempotency_key="dream-shutdown-owned-worker-0001",
        ),
        source_versions=(),
    )
    coordinator._analyze = lambda *_args: _Analysis(  # type: ignore[method-assign]
        proposals=(proposal,),
        pending_count=1,
    )
    state = coordinator._states[workspace.workspace_id]
    state.publication_lock.acquire()
    running = asyncio.create_task(coordinator.run_once(workspace))
    deadline = time.monotonic() + 1.0
    while not coordinator._publication_tasks and time.monotonic() < deadline:
        await asyncio.sleep(0.005)
    assert coordinator._publication_tasks
    while _CAPTURE_WORKERS.in_flight == 0 and time.monotonic() < deadline:
        await asyncio.sleep(0.005)
    assert _CAPTURE_WORKERS.in_flight >= 1
    await asyncio.sleep(0.1)
    closing = asyncio.create_task(coordinator.aclose())
    await asyncio.sleep(0.05)
    assert not closing.done()
    state.publication_lock.release()
    await asyncio.wait_for(closing, timeout=2.0)
    await asyncio.gather(running, return_exceptions=True)
    assert not coordinator._publication_tasks
    assert _CAPTURE_WORKERS.in_flight == 0
    with sqlite3.connect(database) as connection:
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM memory_capture_candidates WHERE idempotency_key=?",
                ("dream-shutdown-owned-worker-0001",),
            ).fetchone()[0]
            == 0
        )


@pytest.mark.asyncio
async def test_graph_pair_cursor_finds_pair_across_production_membership_page(
    dream_workspace,
) -> None:
    workspace, _database = dream_workspace

    class Row(dict):
        names = (
            "record_id",
            "entity_id",
            "normalized_name",
            "record_type",
            "content",
            "content_bytes",
            "content_hash",
            "state_hash",
            "source_event_id",
            "stream_version",
            "created_at_us",
            "updated_at_us",
            "outcome",
            "worked",
        )

        def __getitem__(self, key):
            if isinstance(key, int):
                key = self.names[key]
            return super().__getitem__(key)

    class Result:
        def __init__(self, rows):
            self.rows = rows

        def fetchone(self):
            return self.rows[0] if self.rows else None

        def fetchall(self):
            return self.rows

    class Connection:
        def __init__(self, rows):
            self.rows = rows

        def execute(self, sql, args=()):
            if "FROM projection_manifests" in sql:
                return Result([(1,)])
            if "FROM memory_relationship_versions" in sql:
                return Result([])
            if "FROM memory_capture_candidates" in sql:
                return Result([])
            if "FROM discovery_entity_records" not in sql:
                raise AssertionError(sql)
            rows = self.rows
            if "der.record_id=?" in sql:
                rows = [row for row in rows if row["record_id"] == args[-2]]
            elif "der.record_id>?" in sql:
                rows = [row for row in rows if row["record_id"] > args[-2]]
            return Result(rows[: args[-1]])

    now = time.time_ns() // 1_000
    rows = []
    for index in range(1, dreaming_runtime._MAX_GRAPH_MEMBERSHIPS + 2):
        shared = index in {1, dreaming_runtime._MAX_GRAPH_MEMBERSHIPS + 1}
        rows.append(
            Row(
                record_id="mem_" + f"{index:064x}",
                entity_id="ent_" + f"{index:064x}",
                normalized_name="shared" if shared else f"other-{index}",
                record_type="observation",
                content=f"record {index}",
                content_bytes=12,
                content_hash=f"{index:064x}",
                state_hash=f"{index + 10_000:064x}",
                source_event_id="evt_" + f"{index:064x}",
                stream_version=1,
                created_at_us=1,
                updated_at_us=now,
                outcome=None,
                worked=None,
            )
        )
    coordinator = object.__new__(V7DreamingCoordinator)
    coordinator._settings = _settings(
        workspace.root,
        dream_connection_min_shared_entities=1,
        dream_connection_lookback_hours=10_000,
    )
    coordinator._clock_us = lambda: now
    analysis = coordinator._connection_analysis(
        Connection(rows),  # type: ignore[arg-type]
        workspace,
        None,
        threading.Event(),
    )
    assert len(analysis.proposals) == 1
    sources = analysis.proposals[0].request.provenance["sources"]
    assert [item["record_id"] for item in sources] == [
        "mem_" + f"{1:064x}",
        "mem_" + f"{dreaming_runtime._MAX_GRAPH_MEMBERSHIPS + 1:064x}",
    ]


@pytest.mark.asyncio
@pytest.mark.skipif(
    importlib.util.find_spec("igraph") is None,
    reason="real graph profile is not installed",
)
async def test_connection_discovery_and_community_refresh_use_active_graph(
    dream_workspace,
) -> None:
    from daem0nmcp.graph_projection import GraphProjectionBuilder

    workspace, database = dream_workspace
    _append(
        database,
        workspace.workspace_id,
        "7",
        "CacheManager calls refresh_cache() in cache.py",
        worked=True,
        record_type="observation",
    )
    _append(
        database,
        workspace.workspace_id,
        "8",
        "CacheManager calls refresh_cache() after cache.py changes",
        worked=True,
        record_type="observation",
    )
    with sqlite3.connect(database) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        first = GraphProjectionBuilder(connection).rebuild(
            workspace.workspace_id, force=True
        )
    coordinator = V7DreamingCoordinator(
        workspaces=(workspace,),
        settings=_settings(
            workspace.root,
            dream_connection_min_shared_entities=2,
            dream_community_staleness_threshold=1,
        ),
        candidate_store=CaptureCandidateStore(),
        capability_statuses={"graph": "ready"},
    )
    await coordinator.start()

    class CancelDuringGraphScan:
        def __init__(self) -> None:
            self.checks = 0

        def is_set(self) -> bool:
            self.checks += 1
            return self.checks > 2

    cancelled = CancelDuringGraphScan()
    cancelled_analysis = coordinator._analyze(
        workspace,
        "connection_discovery",
        cancelled,  # type: ignore[arg-type]
    )
    assert cancelled_analysis.proposals == ()
    assert cancelled_analysis.pending_count == 1
    assert cancelled.checks > 2
    await coordinator.run_once(workspace)
    _append(
        database,
        workspace.workspace_id,
        "9",
        "CacheManager observation in cache.py",
        worked=True,
        record_type="observation",
    )
    await coordinator.run_once(workspace)
    await coordinator.aclose()
    with sqlite3.connect(database) as connection:
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM memory_capture_candidates WHERE "
                "json_extract(provenance_json,'$.strategy')='connection_discovery'"
            ).fetchone()[0]
            >= 1
        )
        generation = connection.execute(
            "SELECT generation FROM projection_manifests WHERE workspace_id=? "
            "AND projection_name='graph' AND status='active'",
            (workspace.workspace_id,),
        ).fetchone()[0]
        assert generation > first.generation


# Read with defaults so the module still collects against older source.
_MST = getattr(dreaming_runtime, "_MIN_SHARED_TERMS", 2)
_EPR = getattr(dreaming_runtime, "_EVIDENCE_PAGE_ROWS", 64)
_MCC = dreaming_runtime._MAX_CONTENT_CHARS


def _pending_candidate_context(database: Path, record_id: str) -> dict[str, object]:
    with sqlite3.connect(database) as connection:
        row = connection.execute(
            "SELECT proposed_record_json FROM memory_capture_candidates WHERE "
            "json_extract(provenance_json,'$.strategy')='pending_outcome' AND "
            "json_extract(provenance_json,'$.sources[0].record_id')=?",
            (record_id,),
        ).fetchone()
    assert row is not None
    return json.loads(row[0])["context"]


async def _run_pending_pass(workspace, **changes: object) -> None:
    coordinator = V7DreamingCoordinator(
        workspaces=(workspace,),
        settings=_settings(workspace.root, **changes),
        candidate_store=CaptureCandidateStore(),
        capability_statuses={"graph": "disabled"},
    )
    await coordinator.start()
    try:
        await coordinator.run_once(workspace)
    finally:
        await coordinator.aclose()


def _append_unrelated_outcomes(database: Path, workspace_id: str) -> str:
    pending = _append(
        database,
        workspace_id,
        "d",
        "Adopt the new postgres connection pooler",
        worked=None,
    )
    _append(
        database,
        workspace_id,
        "e",
        "Ship the new onboarding carousel",
        worked=True,
    )
    _append(
        database,
        workspace_id,
        "f",
        "Rename the new billing webhook",
        worked=True,
    )
    return pending


def _append_migration_evidence(database: Path, workspace_id: str) -> list[str]:
    return [
        _append(
            database,
            workspace_id,
            suffix,
            f"postgres migration passed {stage}",
            worked=True,
        )
        for suffix, stage in (("e", "tests"), ("f", "staging"))
    ]


@pytest.mark.asyncio
async def test_records_sharing_only_ordinary_words_are_not_evidence(
    dream_workspace,
) -> None:
    workspace, database = dream_workspace
    pending = _append_unrelated_outcomes(database, workspace.workspace_id)

    await _run_pending_pass(workspace)

    context = _pending_candidate_context(database, pending)
    assert context["evidence_count"] == 0
    assert context["classification"] == "insufficient"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("shared_terms", "padding", "expected"),
    [
        (_MST - 1, 0, 0),
        (_MST, 0, 1),
        # Enough shared terms, but diluted below the similarity floor.
        (_MST, 12, 0),
    ],
)
async def test_evidence_threshold_is_enforced_on_both_sides(
    dream_workspace, shared_terms: int, padding: int, expected: int
) -> None:
    workspace, database = dream_workspace
    subject = ["postgres", "pooler", "failover", "replica"]
    assert len(subject) >= _MST
    pending = _append(
        database,
        workspace.workspace_id,
        "d",
        " ".join(subject),
        worked=None,
    )
    other = ["carousel", "webhook", "invoice", "tooltip"]
    filler = [f"unrelated{index}" for index in range(padding)]
    _append(
        database,
        workspace.workspace_id,
        "e",
        " ".join(subject[:shared_terms] + other[shared_terms:] + filler),
        worked=True,
    )

    await _run_pending_pass(workspace)

    assert _pending_candidate_context(database, pending)["evidence_count"] == expected


@pytest.mark.asyncio
async def test_unrelated_outcomes_never_commit_an_automatic_outcome(
    dream_workspace,
) -> None:
    workspace, database = dream_workspace
    pending = _append_unrelated_outcomes(database, workspace.workspace_id)

    await _run_pending_pass(workspace, dream_pending_dry_run=False)

    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT worked,outcome FROM memory_records WHERE record_id=?", (pending,)
        ).fetchone() == (None, None)
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM memory_events WHERE stream_id=? "
                "AND event_type='memory.outcome_recorded'",
                (pending,),
            ).fetchone()[0]
            == 0
        )


@pytest.mark.asyncio
async def test_automatic_outcome_is_attributed_to_dreaming_and_unverified(
    dream_workspace,
) -> None:
    from daem0nmcp.retrieval.utility import verification_weight

    workspace, database = dream_workspace
    pending = _append(
        database, workspace.workspace_id, "d", "postgres migration pending", worked=None
    )
    evidence = _append_migration_evidence(database, workspace.workspace_id)

    await _run_pending_pass(workspace, dream_pending_dry_run=False)

    with sqlite3.connect(database) as connection:
        actor, payload_json, event_id = connection.execute(
            "SELECT actor_type,payload_json,event_id FROM memory_events WHERE "
            "stream_id=? AND event_type='memory.outcome_recorded'",
            (pending,),
        ).fetchone()
        payload = json.loads(payload_json)
        weight = connection.execute(
            "SELECT weight FROM memory_outcome_signals WHERE event_id=?", (event_id,)
        ).fetchone()[0]
        evidence_events = {
            row[0]
            for row in connection.execute(
                "SELECT source_event_id FROM memory_records WHERE record_id IN (?,?)",
                evidence,
            )
        }
    assert actor == "system"
    assert payload["dreaming"]["strategy"] == "pending_outcome"
    assert set(payload["dreaming"]["evidence_event_ids"]) == evidence_events
    # No verification object: the outcome model's unverified form and weight.
    assert "verification" not in payload
    assert weight == verification_weight(True, None) == 0.5
    outcome_text = payload["record"]["outcome"].casefold()
    assert "dreaming" in outcome_text
    assert "not verified" in outcome_text


@pytest.mark.asyncio
async def test_outcome_inferred_by_dreaming_is_not_evidence_for_another_decision(
    dream_workspace,
) -> None:
    workspace, database = dream_workspace
    inferred = _append(
        database, workspace.workspace_id, "a", "postgres migration pending", worked=None
    )
    _append_migration_evidence(database, workspace.workspace_id)
    await _run_pending_pass(workspace, dream_pending_dry_run=False)
    with sqlite3.connect(database) as connection:
        assert (
            connection.execute(
                "SELECT worked FROM memory_records WHERE record_id=?", (inferred,)
            ).fetchone()[0]
            == 1
        )

    # Two verified outcomes plus the inferred one must not reach a threshold of 3.
    second = _append(
        database, workspace.workspace_id, "b", "postgres migration rollout", worked=None
    )
    await _run_pending_pass(
        workspace, dream_pending_dry_run=False, dream_pending_evidence_threshold=3
    )

    with sqlite3.connect(database) as connection:
        assert (
            connection.execute(
                "SELECT worked FROM memory_records WHERE record_id=?", (second,)
            ).fetchone()[0]
            is None
        )
    assert _pending_candidate_context(database, second)["classification"] == (
        "insufficient"
    )


@pytest.mark.asyncio
async def test_evidence_completeness_scales_past_one_page_of_records(
    dream_workspace,
) -> None:
    workspace, database = dream_workspace
    pending = _append(
        database, workspace.workspace_id, "d", "postgres migration pending", worked=None
    )
    _append_migration_evidence(database, workspace.workspace_id)
    for index in range(_EPR + 6):
        _append(
            database,
            workspace.workspace_id,
            f"{index + 1:064x}",
            f"unrelated observation number{index}",
            worked=None,
            record_type="observation",
        )

    await _run_pending_pass(workspace)

    context = _pending_candidate_context(database, pending)
    assert context["evidence_count"] == 2
    assert context["classification"] == "unanimous_positive"


@pytest.mark.asyncio
async def test_exhausted_evidence_scan_budget_is_reported_as_incomplete(
    dream_workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace, database = dream_workspace
    pending = _append(
        database, workspace.workspace_id, "d", "postgres migration pending", worked=None
    )
    _append_migration_evidence(database, workspace.workspace_id)
    monkeypatch.setattr(dreaming_runtime, "_EVIDENCE_SCAN_SECONDS", 0.0)

    await _run_pending_pass(workspace, dream_pending_dry_run=False)

    assert _pending_candidate_context(database, pending)["classification"] == (
        "insufficient"
    )
    with sqlite3.connect(database) as connection:
        assert (
            connection.execute(
                "SELECT worked FROM memory_records WHERE record_id=?", (pending,)
            ).fetchone()[0]
            is None
        )


def _connection_candidates(database: Path) -> list[dict[str, object]]:
    with sqlite3.connect(database) as connection:
        rows = connection.execute(
            "SELECT proposed_record_json FROM memory_capture_candidates WHERE "
            "json_extract(provenance_json,'$.strategy')='connection_discovery' "
            "ORDER BY created_at_us,candidate_id"
        ).fetchall()
    return [json.loads(row[0])["context"] for row in rows]


def _graph_pair(database: Path, workspace_id: str) -> None:
    for suffix, tail in (("7", "in cache.py"), ("8", "after cache.py changes")):
        _append(
            database,
            workspace_id,
            suffix,
            f"CacheManager calls refresh_cache() {tail}",
            worked=True,
            record_type="observation",
        )


def _rebuild_graph(database: Path, workspace_id: str) -> int:
    from daem0nmcp.graph_projection import GraphProjectionBuilder

    with sqlite3.connect(database) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        return (
            GraphProjectionBuilder(connection)
            .rebuild(workspace_id, force=True)
            .generation
        )


def _connection_coordinator(workspace) -> V7DreamingCoordinator:
    return V7DreamingCoordinator(
        workspaces=(workspace,),
        settings=_settings(
            workspace.root,
            dream_connection_min_shared_entities=2,
            dream_community_staleness_threshold=10_000,
        ),
        candidate_store=CaptureCandidateStore(),
        capability_statuses={"graph": "ready"},
    )


_NEEDS_GRAPH = pytest.mark.skipif(
    importlib.util.find_spec("igraph") is None,
    reason="real graph profile is not installed",
)


@pytest.mark.asyncio
@_NEEDS_GRAPH
async def test_graph_rebuild_does_not_propose_the_same_connection_again(
    dream_workspace,
) -> None:
    workspace, database = dream_workspace
    _graph_pair(database, workspace.workspace_id)
    first_generation = _rebuild_graph(database, workspace.workspace_id)
    coordinator = _connection_coordinator(workspace)
    await coordinator.start()
    try:
        await coordinator.run_once(workspace)
        assert len(_connection_candidates(database)) == 1
        assert _rebuild_graph(database, workspace.workspace_id) > first_generation
        await coordinator.run_once(workspace)
        health = await coordinator.health(workspace)
    finally:
        await coordinator.aclose()

    assert len(_connection_candidates(database)) == 1
    assert health is not None
    state = next(
        item
        for item in health["strategies"]
        if item["strategy"] == "connection_discovery"
    )
    assert (state["status"], state["stable_error_code"]) == ("idle", None)


@pytest.mark.asyncio
@_NEEDS_GRAPH
@pytest.mark.parametrize(
    ("same_reason", "expected"), [(True, 1), (False, 2)], ids=["same", "new-reason"]
)
async def test_connection_stored_before_the_fingerprint_change_is_recognised(
    dream_workspace, same_reason: bool, expected: int
) -> None:
    workspace, database = dream_workspace
    _graph_pair(database, workspace.workspace_id)
    generation = _rebuild_graph(database, workspace.workspace_id)
    coordinator = _connection_coordinator(workspace)
    await coordinator.start()
    try:
        analysis = coordinator._analyze(
            workspace, "connection_discovery", threading.Event()
        )
        assert len(analysis.proposals) == 1
        request = analysis.proposals[0].request
        context = dict(request.record["context"])
        if not same_reason:
            context["shared_entities"] = ["some-other-entity", "yet-another-entity"]
        # The shape stored by earlier releases: the fingerprint covered the
        # graph generation and the exact source versions.
        provenance = {
            "strategy": "connection_discovery",
            "strategy_version": 1,
            "sources": request.provenance["sources"],
            "graph_generation": generation - 1,
        }
        await CaptureCandidateStore().stage(
            workspace,
            CaptureCandidateRequest(
                source_kind="dreaming",
                record={**request.record, "context": context},
                provenance=provenance,
                idempotency_key="dream-connection_discovery-" + "0" * 64,
            ),
        )
        await coordinator.run_once(workspace)
    finally:
        await coordinator.aclose()

    assert len(_connection_candidates(database)) == expected


def _community_state(health) -> tuple[object, object, object]:
    assert health is not None
    item = next(
        item for item in health["strategies"] if item["strategy"] == "community_refresh"
    )
    return item["status"], item["stable_error_code"], item["yielded"]


def _community_coordinator(workspace) -> V7DreamingCoordinator:
    return V7DreamingCoordinator(
        workspaces=(workspace,),
        settings=_settings(workspace.root, dream_community_staleness_threshold=1),
        candidate_store=CaptureCandidateStore(),
        capability_statuses={"graph": "ready"},
    )


@pytest.mark.asyncio
async def test_community_refresh_backs_off_after_a_deadline_until_inputs_change(
    dream_workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    from daem0nmcp.graph_projection import (
        GraphProjectionBuilder,
        GraphProjectionBuildError,
    )

    workspace, database = dream_workspace
    _append(
        database,
        workspace.workspace_id,
        "7",
        "CacheManager observation",
        worked=None,
        record_type="observation",
    )
    passes: list[float] = []

    def over_deadline(self, workspace_id, **_kwargs):
        passes.append(time.monotonic())
        raise GraphProjectionBuildError("DEADLINE_EXCEEDED")

    monkeypatch.setattr(GraphProjectionBuilder, "rebuild", over_deadline)
    coordinator = _community_coordinator(workspace)
    await coordinator.start()
    try:
        state = coordinator._states[workspace.workspace_id]
        for _ in range(3):
            await coordinator.run_once(workspace)
        # One full pass, then idle passes wait instead of repeating it.
        assert len(passes) == 1
        assert _community_state(await coordinator.health(workspace)) == (
            "degraded",
            "DEADLINE_EXCEEDED",
            False,
        )
        first_wait = state.community_retry_at - passes[-1]
        assert first_wait >= dreaming_runtime._COMMUNITY_BACKOFF_SECONDS

        # The same inputs are retried once the wait has elapsed, and a second
        # consecutive deadline waits longer.
        state.community_retry_at = 0.0
        await coordinator.run_once(workspace)
        await coordinator.run_once(workspace)
        assert len(passes) == 2
        assert state.community_retry_at - passes[-1] >= 2 * first_wait - 1.0

        # New events are new inputs: retry at once, from the first wait again.
        _append(
            database,
            workspace.workspace_id,
            "8",
            "CacheManager follow-up observation",
            worked=None,
            record_type="observation",
        )
        await coordinator.run_once(workspace)
        await coordinator.run_once(workspace)
        assert len(passes) == 3
        assert (
            state.community_retry_at - passes[-1]
            < 2 * dreaming_runtime._COMMUNITY_BACKOFF_SECONDS
        )
    finally:
        await coordinator.aclose()


def test_community_refresh_backoff_is_exponential_and_capped() -> None:
    base = dreaming_runtime._COMMUNITY_BACKOFF_SECONDS
    cap = dreaming_runtime._COMMUNITY_BACKOFF_MAX_SECONDS
    waits = [dreaming_runtime._community_backoff_seconds(n) for n in range(1, 40)]
    assert waits[:3] == [base, 2 * base, 4 * base]
    assert waits == sorted(waits)
    assert waits[-1] == cap > base


@pytest.mark.asyncio
async def test_community_refresh_that_yields_to_foreground_is_not_degraded(
    dream_workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    from daem0nmcp.graph_projection import (
        GraphProjectionBuilder,
        GraphProjectionBuildError,
    )

    workspace, database = dream_workspace
    _append(
        database,
        workspace.workspace_id,
        "7",
        "CacheManager observation",
        worked=None,
        record_type="observation",
    )
    scheduled: list[Path] = []

    def interrupted(self, workspace_id, *, cancelled, **_kwargs):
        # Foreground activity arrives while the pass is running.
        cancelled.set()
        raise GraphProjectionBuildError("CANCELLED")

    monkeypatch.setattr(GraphProjectionBuilder, "rebuild", interrupted)
    coordinator = V7DreamingCoordinator(
        workspaces=(workspace,),
        settings=_settings(workspace.root, dream_community_staleness_threshold=1),
        candidate_store=CaptureCandidateStore(),
        capability_statuses={"graph": "ready"},
        projection_scheduler=scheduled.append,
    )
    await coordinator.start()
    try:
        await coordinator.run_once(workspace)
        health = await coordinator.health(workspace)
        state = coordinator._states[workspace.workspace_id]
    finally:
        await coordinator.aclose()

    assert _community_state(health) == ("idle", None, True)
    assert health["yielded"] is True
    assert scheduled == []
    assert state.community_failures == 0


def _append_numbered(
    database: Path, workspace_id: str, contents: list[str], *, worked: bool | None
) -> list[str]:
    return [
        _append(database, workspace_id, f"{index + 1:064x}", content, worked=worked)
        for index, content in enumerate(contents)
    ]


def _pending_state(database: Path, record_id: str) -> tuple[object, object]:
    with sqlite3.connect(database) as connection:
        return connection.execute(
            "SELECT worked,outcome FROM memory_records WHERE record_id=?", (record_id,)
        ).fetchone()


@pytest.mark.asyncio
async def test_opposing_outcome_ranked_below_the_retained_evidence_blocks_unanimity(
    dream_workspace,
) -> None:
    workspace, database = dream_workspace
    pending = _append(
        database,
        workspace.workspace_id,
        "d",
        "postgres migration cutover replica pending",
        worked=None,
    )
    # More agreeing records than are retained, all ranked above the opposing one.
    _append_numbered(
        database,
        workspace.workspace_id,
        [f"postgres migration cutover replica stage{index}" for index in range(12)],
        worked=True,
    )
    _append(
        database,
        workspace.workspace_id,
        "e",
        "postgres migration rollback",
        worked=False,
    )

    await _run_pending_pass(workspace, dream_pending_dry_run=False)

    assert _pending_state(database, pending) == (None, None)
    assert _pending_candidate_context(database, pending)["classification"] == "mixed"


@pytest.mark.asyncio
async def test_successful_alternative_ranked_below_the_retained_evidence_is_found(
    dream_workspace,
) -> None:
    workspace, database = dream_workspace
    failed = _append(
        database,
        workspace.workspace_id,
        "d",
        "postgres migration cutover replica",
        worked=False,
    )
    _append_numbered(
        database,
        workspace.workspace_id,
        [f"postgres migration cutover replica stage{index}" for index in range(12)],
        worked=None,
    )
    alternative = _append(
        database,
        workspace.workspace_id,
        "e",
        "postgres migration rollback",
        worked=True,
    )

    await _run_pending_pass(workspace)

    with sqlite3.connect(database) as connection:
        proposed, provenance = connection.execute(
            "SELECT proposed_record_json,provenance_json FROM "
            "memory_capture_candidates WHERE "
            "json_extract(provenance_json,'$.strategy')='failed_decision' AND "
            "json_extract(provenance_json,'$.sources[0].record_id')=?",
            (failed,),
        ).fetchone()
    assert json.loads(proposed)["context"]["classification"] == (
        "successful_alternative"
    )
    assert alternative in {
        source["record_id"] for source in json.loads(provenance)["sources"]
    }


@pytest.mark.asyncio
async def test_long_unrelated_record_does_not_make_evidence_incomplete(
    dream_workspace,
) -> None:
    workspace, database = dream_workspace
    pending = _append(
        database, workspace.workspace_id, "d", "postgres migration pending", worked=None
    )
    _append_migration_evidence(database, workspace.workspace_id)
    for index in range(_EPR + 6):
        _append(
            database,
            workspace.workspace_id,
            f"{index + 1:064x}",
            f"unrelated observation number{index}",
            worked=None,
            record_type="observation",
        )
    _append(
        database,
        workspace.workspace_id,
        "a",
        "carousel " + "x" * (_MCC + 100),
        worked=None,
        record_type="observation",
    )

    await _run_pending_pass(workspace)

    context = _pending_candidate_context(database, pending)
    assert context["evidence_count"] == 2
    assert context["classification"] == "unanimous_positive"


@pytest.mark.asyncio
async def test_long_record_is_judged_on_its_whole_text(dream_workspace) -> None:
    workspace, database = dream_workspace
    pending = _append(
        database, workspace.workspace_id, "d", "postgres migration pending", worked=None
    )
    _append_migration_evidence(database, workspace.workspace_id)
    # The subject of this failed record only appears after the first page of text.
    _append(
        database,
        workspace.workspace_id,
        "a",
        "x" * (_MCC + 100) + " postgres migration rollback",
        worked=False,
    )

    await _run_pending_pass(workspace, dream_pending_dry_run=False)

    assert _pending_state(database, pending) == (None, None)
    assert _pending_candidate_context(database, pending)["classification"] == "mixed"


@pytest.mark.asyncio
async def test_cut_short_evidence_scan_does_not_name_partial_evidence(
    dream_workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace, database = dream_workspace
    pending = _append(
        database, workspace.workspace_id, "d", "postgres migration pending", worked=None
    )
    for index in range(_EPR + 6):
        _append(
            database,
            workspace.workspace_id,
            f"{index + 1:064x}",
            f"postgres migration note{index}",
            worked=None,
            record_type="observation",
        )
    # One page is read, then the row budget ends the scan.
    monkeypatch.setattr(dreaming_runtime, "_MAX_EVIDENCE_SCAN_ROWS", _EPR)

    await _run_pending_pass(workspace)

    context = _pending_candidate_context(database, pending)
    assert (context["classification"], context["evidence_count"]) == (
        "insufficient",
        0,
    )
    with sqlite3.connect(database) as connection:
        provenance = connection.execute(
            "SELECT provenance_json FROM memory_capture_candidates WHERE "
            "json_extract(provenance_json,'$.strategy')='pending_outcome'"
        ).fetchone()[0]
    assert [item["record_id"] for item in json.loads(provenance)["sources"]] == [
        pending
    ]


@pytest.mark.asyncio
async def test_interrupted_analysis_is_pending_work_without_sqlite_error_codes(
    dream_workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace, database = dream_workspace
    _append(
        database, workspace.workspace_id, "d", "postgres migration pending", worked=None
    )

    def interrupted(*_args: object, **_kwargs: object) -> _Analysis:
        # What sqlite3 raises before Python 3.11: no sqlite_errorcode attribute.
        raise sqlite3.OperationalError("interrupted")

    monkeypatch.setattr(V7DreamingCoordinator, "_pending_analysis", interrupted)
    monkeypatch.delattr(sqlite3, "SQLITE_INTERRUPT", raising=False)
    coordinator = V7DreamingCoordinator(
        workspaces=(workspace,),
        settings=_settings(workspace.root),
        candidate_store=CaptureCandidateStore(),
        capability_statuses={"graph": "disabled"},
    )
    await coordinator.start()
    try:
        await coordinator.run_once(workspace)
        health = await coordinator.health(workspace)
    finally:
        await coordinator.aclose()

    assert health is not None
    state = next(
        item for item in health["strategies"] if item["strategy"] == "pending_outcome"
    )
    assert (state["status"], state["stable_error_code"]) == ("idle", None)
