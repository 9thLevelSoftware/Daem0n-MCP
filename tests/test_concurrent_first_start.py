"""Several processes starting on one store, or migrating one database, at once."""

from __future__ import annotations

import asyncio
import json
import sqlite3
import subprocess
import sys
import time
from contextlib import closing
from pathlib import Path

import pytest
from sqlalchemy import create_engine

from daem0nmcp import database
from daem0nmcp.database import DatabaseManager
from daem0nmcp.migrations import MIGRATIONS, run_migrations
from daem0nmcp.models import Base
from daem0nmcp.schema_version import CURRENT_SCHEMA_VERSION
from daem0nmcp.storage_activation import (
    LOCK_NAME,
    POINTER_NAME,
    POINTER_TEMP_NAME,
    DatabaseFileLock,
    DatabaseInUseError,
    resolve_active_database,
)
from daem0nmcp.workspace import WorkspaceRegistry

PROCESSES = 4
TIMEOUT_SECONDS = 300

# Every worker imports first, reports ready and then spins on the gate, so the
# contended calls start together instead of being spread over interpreter
# start-up.
_GATE = """
import json, sys, time, traceback
from pathlib import Path

target, gate, index = Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3]


def wait_for_gate():
    (gate / ("ready-" + index)).write_text("", encoding="utf-8")
    while not (gate / "go").exists():
        time.sleep(0.001)
"""

_FIRST_START_WORKER = (
    _GATE
    + """
import asyncio

from daem0nmcp.database import DatabaseManager
from daem0nmcp.storage_activation import DatabaseInUseError


async def start_once():
    manager = DatabaseManager(str(target))
    try:
        await manager.init_db()
        return {
            "format_version": manager.format_version,
            "generation": manager.active_generation,
            "schema_version": manager._schema_version(),
        }
    finally:
        await manager.close()


async def main():
    busy = 0
    deadline = time.monotonic() + 240
    while True:
        try:
            result = await start_once()
        except DatabaseInUseError:
            # The documented retryable answer to storage-lock contention.
            busy += 1
            if time.monotonic() >= deadline:
                raise
            await asyncio.sleep(0.05)
            continue
        result["busy"] = busy
        return result


wait_for_gate()
try:
    print(json.dumps(asyncio.run(main())))
except BaseException:
    traceback.print_exc()
    sys.exit(1)
"""
)

_LEDGER_WORKER = (
    _GATE
    + """
from daem0nmcp.migrations import run_migrations

workspace_id = sys.argv[4]
wait_for_gate()
try:
    count, _ = run_migrations(str(target), workspace_id=workspace_id)
    print(json.dumps({"applied": count}))
except BaseException:
    traceback.print_exc()
    sys.exit(1)
"""
)


def _run_together(source: str, target: Path, gate: Path, *extra: str) -> list[dict]:
    gate.mkdir()
    workers = [
        subprocess.Popen(
            [sys.executable, "-c", source, str(target), str(gate), str(index), *extra],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for index in range(PROCESSES)
    ]
    try:
        deadline = time.monotonic() + TIMEOUT_SECONDS
        while len(list(gate.glob("ready-*"))) < PROCESSES:
            crashed = [worker for worker in workers if worker.poll() is not None]
            assert not crashed, [worker.communicate()[1] for worker in crashed]
            assert time.monotonic() < deadline, "workers never became ready"
            time.sleep(0.01)
        (gate / "go").write_text("", encoding="utf-8")
        outcomes = [worker.communicate(timeout=TIMEOUT_SECONDS) for worker in workers]
    finally:
        for worker in workers:
            if worker.poll() is None:
                worker.kill()
                worker.communicate()
    failures = [
        stderr
        for worker, (_, stderr) in zip(workers, outcomes, strict=True)
        if worker.returncode != 0
    ]
    assert not failures, "\n=====\n".join(failures)
    return [json.loads(stdout.strip().splitlines()[-1]) for stdout, _ in outcomes]


def _schema(database: Path) -> list[tuple]:
    with closing(sqlite3.connect(database)) as connection:
        return connection.execute(
            "SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY type,name"
        ).fetchall()


def _ledger(database: Path) -> list[int]:
    with closing(sqlite3.connect(database)) as connection:
        return [
            row[0]
            for row in connection.execute(
                "SELECT version FROM schema_version ORDER BY version"
            )
        ]


def _model_tables_only(database: Path) -> None:
    """The state a first start is in between table creation and the ledger."""

    engine = create_engine(f"sqlite:///{database}")
    try:
        Base.metadata.create_all(engine)
    finally:
        engine.dispose()


def test_concurrent_first_start_gives_every_process_the_same_v7_store(tmp_path):
    """Fresh-store bootstrap is serialised between processes."""
    storage = tmp_path / ".daem0nmcp" / "storage"

    results = _run_together(_FIRST_START_WORKER, storage, tmp_path / "gate")

    print("DATABASE_IN_USE retries:", [result.pop("busy") for result in results])
    expected = {
        "format_version": 7,
        "generation": 1,
        "schema_version": CURRENT_SCHEMA_VERSION,
    }
    assert results == [expected] * PROCESSES
    active = resolve_active_database(storage)
    assert active.pointer is not None
    assert (active.format_version, active.generation) == (7, 1)
    assert active.path == (storage / "daem0nmcp.db").resolve()
    names = {path.name for path in storage.iterdir()}
    assert {LOCK_NAME, POINTER_NAME, "daem0nmcp.db"} <= names
    assert POINTER_TEMP_NAME not in names
    assert _ledger(active.path) == [version for version, _, _ in MIGRATIONS]
    with closing(sqlite3.connect(active.path)) as connection:
        assert connection.execute("PRAGMA integrity_check").fetchall() == [("ok",)]
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


async def test_two_managers_in_one_process_start_on_the_same_fresh_storage(tmp_path):
    """The start that loses the creation lock opens what the winner created."""
    storage = tmp_path / ".daem0nmcp" / "storage"
    managers = [DatabaseManager(str(storage)) for _ in range(2)]
    try:
        assert [manager.fresh_at_construction for manager in managers] == [True, True]
        await asyncio.gather(*(manager.init_db() for manager in managers))
        assert [manager.format_version for manager in managers] == [7, 7]
        assert [manager.active_generation for manager in managers] == [1, 1]
        assert sorted(manager.fresh_at_construction for manager in managers) == [
            False,
            True,
        ]
        # Creation is over, so the storage is shared again: a further reader
        # is admitted and a migration is still kept out.
        with DatabaseFileLock(storage, "shared"):
            pass
        with pytest.raises(DatabaseInUseError):
            DatabaseFileLock(storage, "exclusive").acquire()
    finally:
        for manager in managers:
            await manager.close()
    assert resolve_active_database(storage).generation == 1


async def test_first_start_behind_another_storage_user_is_retryable(
    tmp_path, monkeypatch
):
    """A store is not created under a lock that someone else also holds."""
    monkeypatch.setattr(database, "_CREATION_LOCK_TIMEOUT_SECONDS", 0.2)
    storage = tmp_path / ".daem0nmcp" / "storage"
    manager = DatabaseManager(str(storage))
    try:
        with DatabaseFileLock(storage, "shared"):
            with pytest.raises(DatabaseInUseError):
                await manager.init_db()
            assert {path.name for path in storage.iterdir()} == {LOCK_NAME}
        await manager.init_db()
        assert (manager.format_version, manager.active_generation) == (7, 1)
        assert resolve_active_database(storage).pointer is not None
    finally:
        await manager.close()


def test_concurrent_ledger_runs_apply_each_migration_exactly_once(tmp_path):
    """The migration runner decides what is pending under the write lock."""
    workspace_id = WorkspaceRegistry(
        [tmp_path], default_root=tmp_path
    ).default.workspace_id
    reference = tmp_path / "reference.db"
    _model_tables_only(reference)
    run_migrations(str(reference), workspace_id=workspace_id)
    database = tmp_path / "contended.db"
    _model_tables_only(database)

    results = _run_together(_LEDGER_WORKER, database, tmp_path / "gate", workspace_id)

    assert sum(result["applied"] for result in results) == len(MIGRATIONS)
    assert _ledger(database) == [version for version, _, _ in MIGRATIONS]
    assert _schema(database) == _schema(reference)
    with closing(sqlite3.connect(database)) as connection:
        assert connection.execute("PRAGMA integrity_check").fetchall() == [("ok",)]
