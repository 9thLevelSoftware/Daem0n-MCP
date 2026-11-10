"""Scoped health reports current projections and actual service readiness."""

import asyncio
import json
import sqlite3
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from daem0nmcp.api.v7.health_diagnostics import RuntimeHealthDiagnostics
from daem0nmcp.api.v7.tools import RuntimeDiagnostic
from daem0nmcp.covenant import InvocationScope
from daem0nmcp.retrieval.job_queue import enqueue_projection_rebuild
from daem0nmcp.workspace import WorkspaceRegistry
from tests.api_v7.test_runtime_services import _apply_v7_schema


@pytest.fixture
def health_runtime(tmp_path):
    database = tmp_path / "state.db"
    workspace = WorkspaceRegistry([tmp_path], default_root=tmp_path).default
    with sqlite3.connect(database) as connection:
        _apply_v7_schema(connection)
        enqueue_projection_rebuild(
            connection,
            workspace_id=workspace.workspace_id,
            projection_name="lexical",
            source_event_id=None,
            recorded_at_us=1,
        )
        connection.execute(
            "UPDATE background_jobs SET status='dead_letter',last_error_json=?",
            (
                json.dumps(
                    {"code": "LEXICAL_UNAVAILABLE", "message": "private-source-value"}
                ),
            ),
        )

    @contextmanager
    def locked_active(_workspace):
        yield SimpleNamespace(path=database, generation=3)

    scope = InvocationScope("alice", "s1", str(tmp_path))
    allowed = [True]
    tasks = SimpleNamespace(
        is_ready=False,
        scoped_health_counts=lambda current: (
            {"queued": 2} if current.principal_id == "alice" else {}
        ),
    )
    service = RuntimeHealthDiagnostics(
        storage_resolver=SimpleNamespace(locked_active=locked_active),
        capability_statuses={},
        scope_provider=lambda: scope,
        workspace_authorizer=lambda _: allowed[0],
        task_provider=lambda: tasks,
        bridge_provider=lambda: None,
    )
    yield service, workspace, allowed, tasks
    service.close()


async def test_health_reports_dead_letters_without_private_error_content(
    health_runtime,
):
    service, workspace, _, tasks = health_runtime
    rows = await service.inspect(workspace)
    by_name = {row["component"]: RuntimeDiagnostic.model_validate(row) for row in rows}
    assert by_name["lexical"].counts["dead_letters"] == 1
    assert by_name["lexical"].stable_error_code == "LEXICAL_UNAVAILABLE"
    assert by_name["tasks"].status == "degraded"
    assert by_name["tasks"].counts == {"queued": 2}
    assert by_name["dense"].status == "disabled"
    assert by_name["edit_bridge"].status == "disabled"
    assert by_name["migration"].status == "not_verified"
    assert by_name["migration"].counts["active_generation"] == 3
    assert "private-source-value" not in json.dumps(rows)
    tasks.is_ready = True
    global_rows = await service.inspect(None)
    assert {row["component"] for row in global_rows} == {"tasks", "edit_bridge"}
    assert "counts" not in global_rows[0]
    assert global_rows[0]["status"] == "ready"


async def test_health_denies_revoked_and_wrong_workspace_scopes(
    health_runtime, tmp_path
):
    service, workspace, allowed, tasks = health_runtime
    wrong = WorkspaceRegistry([tmp_path / "other"]).default
    assert await service.inspect(wrong) == []
    allowed[0] = False
    assert await service.inspect(workspace) == []
    allowed[0] = True

    def revoke(_scope):
        allowed[0] = False
        return {"queued": 2}

    tasks.scoped_health_counts = revoke
    assert await service.inspect(workspace) == []


async def test_readiness_is_sampled_once_per_component(health_runtime):
    service, _, _, _ = health_runtime

    class Transitioning:
        def __init__(self):
            self.calls = 0

        @property
        def is_ready(self):
            self.calls += 1
            return self.calls == 1

    task, bridge = Transitioning(), Transitioning()
    service._tasks = lambda: task
    service._bridge = lambda: bridge
    rows = await service.inspect(None)
    assert task.calls == bridge.calls == 1
    assert all(row["status"] == "ready" for row in rows)
    assert all(row.get("stable_error_code") is None for row in rows)


def test_dispatcher_readiness_stops_before_worker_shutdown():
    from daem0nmcp.api.v7.task_dispatcher import DurableTaskDispatcher

    dispatcher = object.__new__(DurableTaskDispatcher)
    dispatcher._started = True
    dispatcher._queue_available = True
    dispatcher._stop = asyncio.Event()
    dispatcher._loops = [SimpleNamespace(done=lambda: False)]
    assert dispatcher.is_ready
    dispatcher._stop.set()
    assert not dispatcher.is_ready
