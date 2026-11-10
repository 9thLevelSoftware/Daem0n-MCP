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

# Holds the storage lock in a process of its own until told to let go.
_HOLDER = """
import sys, time
from pathlib import Path

from daem0nmcp.storage_activation import DatabaseFileLock

storage, ready, release = (Path(argument) for argument in sys.argv[1:4])
with DatabaseFileLock(storage, "shared"):
    ready.write_text("", encoding="utf-8")
    while not release.exists():
        time.sleep(0.01)
"""

# Reports whether another process could take the storage for itself.
_EXCLUSIVE_PROBE = """
import sys

from daem0nmcp.storage_activation import DatabaseFileLock, DatabaseInUseError

try:
    DatabaseFileLock(sys.argv[1], "exclusive").acquire().release()
except DatabaseInUseError:
    print("in use")
else:
    print("free")
"""


def _storage_is_free_for_another_process(storage: Path) -> bool:
    probe = subprocess.run(
        [sys.executable, "-c", _EXCLUSIVE_PROBE, str(storage)],
        capture_output=True,
        text=True,
        timeout=TIMEOUT_SECONDS,
    )
    assert probe.returncode == 0, probe.stderr
    return {"free": True, "in use": False}[probe.stdout.strip()]


class _OtherProcessHoldingStorage:
    """A second process that holds the shared storage lock and never starts."""

    def __init__(self, storage: Path, scratch: Path) -> None:
        self._ready = scratch / "holder-ready"
        self._release = scratch / "holder-release"
        self._arguments = [str(storage), str(self._ready), str(self._release)]

    def __enter__(self) -> None:
        self._process = subprocess.Popen(
            [sys.executable, "-c", _HOLDER, *self._arguments],
            stderr=subprocess.PIPE,
            text=True,
        )
        deadline = time.monotonic() + TIMEOUT_SECONDS
        while not self._ready.exists():
            assert self._process.poll() is None, self._process.communicate()[1]
            assert time.monotonic() < deadline, "holder never took the lock"
            time.sleep(0.01)

    def __exit__(self, *exc_info) -> None:
        self._release.write_text("", encoding="utf-8")
        try:
            self._process.communicate(timeout=TIMEOUT_SECONDS)
        finally:
            if self._process.poll() is None:
                self._process.kill()
                self._process.communicate()


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
    """A store is not created under a lock another process also holds."""
    monkeypatch.setattr(database, "_CREATION_LOCK_TIMEOUT_SECONDS", 0.2)
    storage = tmp_path / ".daem0nmcp" / "storage"
    manager = DatabaseManager(str(storage))
    try:
        with _OtherProcessHoldingStorage(storage, tmp_path):
            with pytest.raises(DatabaseInUseError):
                await manager.init_db()
            assert {path.name for path in storage.iterdir()} == {LOCK_NAME}
        await manager.init_db()
        assert (manager.format_version, manager.active_generation) == (7, 1)
        assert resolve_active_database(storage).pointer is not None
    finally:
        await manager.close()


async def test_idle_manager_in_the_same_process_does_not_block_a_first_start(
    tmp_path, monkeypatch
):
    """A manager that was constructed and never started is not waited for.

    This is the command line's ``remember``: one manager is built for every
    database command and a second one, on the same storage, does the work.
    """
    monkeypatch.setattr(database, "_CREATION_LOCK_TIMEOUT_SECONDS", 5.0)
    storage = tmp_path / ".daem0nmcp" / "storage"
    idle = DatabaseManager(str(storage))
    manager = DatabaseManager(str(storage))
    try:
        await manager.init_db()
        assert (manager.format_version, manager.active_generation) == (7, 1)
        assert resolve_active_database(storage).pointer is not None
        # Both still hold the storage, shared: a reader is admitted and a
        # migration is kept out, in this process and in any other.
        with DatabaseFileLock(storage, "shared"):
            pass
        with pytest.raises(DatabaseInUseError):
            DatabaseFileLock(storage, "exclusive").acquire()
        assert not _storage_is_free_for_another_process(storage)
        await manager.close()
        assert not _storage_is_free_for_another_process(storage)
        # The idle manager read the storage while it was empty; its own start
        # opens the store instead of creating it again.
        await idle.init_db()
        assert (idle.format_version, idle.active_generation) == (7, 1)
        assert idle.fresh_at_construction is False
    finally:
        await manager.close()
        await idle.close()
    assert _storage_is_free_for_another_process(storage)


async def test_refused_creation_lock_leaves_idle_holders_their_shared_lock(
    tmp_path, monkeypatch
):
    """Failing to take the storage over does not cost the process its lock."""
    monkeypatch.setattr(database, "_CREATION_LOCK_TIMEOUT_SECONDS", 0.2)
    storage = tmp_path / ".daem0nmcp" / "storage"
    idle = DatabaseManager(str(storage))
    manager = DatabaseManager(str(storage))
    try:
        with (
            _OtherProcessHoldingStorage(storage, tmp_path),
            pytest.raises(DatabaseInUseError),
        ):
            await manager.init_db()
        assert {path.name for path in storage.iterdir()} == {LOCK_NAME}
        assert not _storage_is_free_for_another_process(storage)
        await manager.init_db()
        assert (manager.format_version, manager.active_generation) == (7, 1)
    finally:
        await manager.close()
        await idle.close()


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
