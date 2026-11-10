"""A format-7 database that lost ``active-db.json`` is never run as v6."""

from __future__ import annotations

import asyncio
import json
import sqlite3
import subprocess
import sys
from contextlib import closing
from pathlib import Path

import pytest

from daem0nmcp.database import DatabaseManager
from daem0nmcp.event_store import EventCommand, EventStore
from daem0nmcp.memory import MemoryManager
from daem0nmcp.migrations.v7 import (
    MigrationInterrupted,
    MigrationV7Error,
    MigrationV7Service,
)
from daem0nmcp.storage_activation import resolve_active_database
from daem0nmcp.workspace import WorkspaceRegistry
from tests.test_v7_schema_upgrade import (
    _activate_a_projection_repair,
    _start_manager,
    _state,
)

RECOVERY_COMMAND = "migrate-v7 --recover-pointer"


def _count(database: Path, table: str) -> int:
    with closing(sqlite3.connect(database)) as connection:
        return int(connection.execute(f'SELECT count(*) FROM "{table}"').fetchone()[0])


def _append_event(database: Path, workspace_id: str, digit: str) -> None:
    """Record one memory the way the v7 server does: an event, no v6 row."""

    with closing(sqlite3.connect(database)) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        EventStore(connection).append_and_project(
            EventCommand(
                workspace_id=workspace_id,
                stream_id="mem_" + digit * 64,
                stream_kind="memory",
                event_type="memory.created",
                occurred_at_us=10,
                recorded_at_us=11,
                actor_type="user",
                payload={"record": _state(f"recorded by the v7 server {digit}")},
            )
        )
        connection.commit()


def _remember(storage: Path, content: str) -> int:
    """Store one memory through the legacy manager and return its format."""

    async def remember() -> int:
        manager = DatabaseManager(str(storage))
        try:
            await manager.init_db()
            memory = MemoryManager(manager)
            try:
                await memory.remember(category="decision", content=content)
            finally:
                memory.close()
            return manager.format_version
        finally:
            await manager.close()

    return asyncio.run(remember())


def _workspace(root: Path) -> tuple[Path, Path, WorkspaceRegistry]:
    """A workspace created the way a first server start creates one."""

    storage = root / ".daem0nmcp" / "storage"
    storage.mkdir(parents=True)
    assert _start_manager(storage) == (7, 1)
    return (
        storage,
        storage / "daem0nmcp.db",
        WorkspaceRegistry([root], default_root=root),
    )


def _lose_the_pointer(storage: Path) -> bytes:
    pointer = storage / "active-db.json"
    published = pointer.read_bytes()
    pointer.unlink()
    return published


def _assert_start_is_refused(storage: Path, root: Path) -> None:
    with pytest.raises(RuntimeError) as raised:
        _start_manager(storage)
    assert getattr(raised.value, "code", None) == "ACTIVE_POINTER_MISSING"
    assert RECOVERY_COMMAND in str(raised.value)
    assert str(root) not in str(raised.value)
    assert not (storage / "active-db.json").exists()


def test_a_v7_store_that_lost_its_pointer_is_not_written_as_v6(tmp_path: Path):
    storage, database, registry = _workspace(tmp_path)
    assert _remember(storage, "written while the pointer was present") == 7
    assert (_count(database, "memories"), _count(database, "memory_events")) == (1, 1)
    published = _lose_the_pointer(storage)

    with pytest.raises(RuntimeError) as raised:
        _remember(storage, "would bypass the event log")

    assert getattr(raised.value, "code", None) == "ACTIVE_POINTER_MISSING"
    assert RECOVERY_COMMAND in str(raised.value)
    assert str(tmp_path) not in str(raised.value)
    assert (_count(database, "memories"), _count(database, "memory_events")) == (1, 1)
    assert not (storage / "active-db.json").exists()

    recovered = MigrationV7Service(registry).recover_pointer(tmp_path)

    assert (recovered.status, recovered.action, recovered.active_generation) == (
        "activated",
        "recover_pointer",
        1,
    )
    assert (storage / "active-db.json").read_bytes() == published
    assert _remember(storage, "written after recovery") == 7
    assert (_count(database, "memories"), _count(database, "memory_events")) == (2, 2)


def test_pointer_recovery_is_not_blocked_by_a_crashed_pointer_temp(tmp_path: Path):
    """A temp left by a write that died before its rename does not stop recovery."""
    storage, database, registry = _workspace(tmp_path)
    assert _remember(storage, "written while the pointer was present") == 7
    published = _lose_the_pointer(storage)
    (storage / "active-db.json.tmp").write_bytes(published[:10])

    recovered = MigrationV7Service(registry).recover_pointer(tmp_path)

    assert (recovered.status, recovered.action) == ("activated", "recover_pointer")
    assert (storage / "active-db.json").read_bytes() == published
    assert not (storage / "active-db.json.tmp").exists()
    assert _remember(storage, "written after recovery") == 7
    assert (_count(database, "memories"), _count(database, "memory_events")) == (2, 2)


def test_a_server_written_store_that_lost_its_pointer_is_not_adopted_silently(
    tmp_path: Path,
):
    storage, database, registry = _workspace(tmp_path)
    _append_event(database, registry.default.workspace_id, "1")
    assert _count(database, "memories") == 0
    published = _lose_the_pointer(storage)

    _assert_start_is_refused(storage, tmp_path)
    assert _count(database, "memory_events") == 1

    MigrationV7Service(registry).recover_pointer(tmp_path)

    assert (storage / "active-db.json").read_bytes() == published
    assert _start_manager(storage) == (7, 1)


def test_a_retained_predecessor_is_neither_opened_nor_adopted(tmp_path: Path):
    storage, database, registry = _workspace(tmp_path)
    workspace_id = registry.default.workspace_id
    _append_event(database, workspace_id, "1")
    _activate_a_projection_repair(storage, database, workspace_id)
    candidate = resolve_active_database(storage).path
    assert candidate != database
    _append_event(candidate, workspace_id, "2")
    _lose_the_pointer(storage)
    service = MigrationV7Service(registry)

    _assert_start_is_refused(storage, tmp_path)
    with pytest.raises(MigrationV7Error) as raised:
        service.recover_pointer(tmp_path)

    # The lost pointer named the candidate, which holds the newer record.
    assert raised.value.code == "POINTER_RECOVERY_AMBIGUOUS"
    assert not (storage / "active-db.json").exists()
    assert (_count(database, "memory_events"), _count(candidate, "memory_events")) == (
        1,
        2,
    )


def test_an_empty_retained_predecessor_is_not_finished_as_a_fresh_database(
    tmp_path: Path,
):
    storage, database, registry = _workspace(tmp_path)
    workspace_id = registry.default.workspace_id
    with closing(sqlite3.connect(database)) as connection:
        connection.execute(
            "UPDATE projection_manifests SET row_count=row_count+1 "
            "WHERE projection_name='lexical' AND status='active'"
        )
        connection.commit()
    from daem0nmcp.verification_v7 import verify_v7

    assert verify_v7(storage, workspace_id, repair_projections=True)["repair"][
        "activated"
    ]
    candidate = resolve_active_database(storage).path
    _append_event(candidate, workspace_id, "1")
    _lose_the_pointer(storage)

    _assert_start_is_refused(storage, tmp_path)
    assert _count(candidate, "memory_events") == 1


def test_recovery_adopts_the_default_database_once_every_run_was_rolled_back(
    tmp_path: Path,
):
    storage, database, registry = _workspace(tmp_path)
    workspace_id = registry.default.workspace_id
    _append_event(database, workspace_id, "1")
    _activate_a_projection_repair(storage, database, workspace_id)
    service = MigrationV7Service(registry)
    assert service.rollback(tmp_path).status == "rolled_back"
    published = _lose_the_pointer(storage)

    recovered = service.recover_pointer(tmp_path)

    assert recovered.status == "activated"
    assert (storage / "active-db.json").read_bytes() == published
    assert _start_manager(storage) == (7, 1)


def test_migrate_v7_does_not_migrate_a_pointerless_v7_database_as_v6(tmp_path: Path):
    storage, database, registry = _workspace(tmp_path)
    _append_event(database, registry.default.workspace_id, "1")
    _lose_the_pointer(storage)
    service = MigrationV7Service(registry)

    for operation in (service.dry_run, service.apply, service.rollback):
        with pytest.raises(MigrationV7Error) as raised:
            operation(tmp_path)
        assert raised.value.code == "ACTIVE_POINTER_MISSING"
        assert RECOVERY_COMMAND in str(raised.value)

    assert not (storage / "migrations").exists()
    assert not (storage / "active-db.json").exists()


def test_recovery_refuses_what_it_cannot_prove(tmp_path: Path):
    from tests.test_migrate_v7 import _create_legacy_database

    storage, _database, registry = _workspace(tmp_path / "current")
    with pytest.raises(MigrationV7Error) as present:
        MigrationV7Service(registry).recover_pointer(tmp_path / "current")
    assert present.value.code == "ACTIVE_POINTER_PRESENT"

    legacy_root = tmp_path / "legacy"
    legacy_storage = legacy_root / ".daem0nmcp" / "storage"
    legacy_storage.mkdir(parents=True)
    _create_legacy_database(legacy_storage / "daem0nmcp.db").close()
    with pytest.raises(MigrationV7Error) as legacy:
        MigrationV7Service(
            WorkspaceRegistry([legacy_root], default_root=legacy_root)
        ).recover_pointer(legacy_root)
    assert legacy.value.code == "POINTER_RECOVERY_UNAVAILABLE"
    assert not (legacy_storage / "active-db.json").exists()


def test_a_store_migrated_from_v6_that_lost_its_pointer_is_not_run_as_v6(
    tmp_path: Path,
):
    """There the pointer named a candidate and the default file is the v6 source."""

    from tests.test_migrate_v7_end_to_end import _registry, _v6_database

    storage = _v6_database(tmp_path)
    source = storage / "daem0nmcp.db"
    registry = _registry(tmp_path)
    service = MigrationV7Service(registry)
    migrated = service.apply(tmp_path)
    candidate = resolve_active_database(storage)
    assert candidate.relative_path == (
        f"migrations/v7/{migrated.migration_run_id}/candidate.db"
    )
    source_bytes = source.read_bytes()
    candidate_bytes = candidate.path.read_bytes()
    runs = sorted(path.name for path in (storage / "migrations" / "v7").iterdir())
    _lose_the_pointer(storage)

    with pytest.raises(RuntimeError) as start:
        _remember(storage, "would land in the retained v6 source")
    assert getattr(start.value, "code", None) == "ACTIVE_POINTER_MISSING"
    assert candidate.relative_path in str(start.value)
    assert "from a backup" in str(start.value)
    assert str(tmp_path) not in str(start.value)

    for attempt in (
        lambda: service.dry_run(tmp_path),
        lambda: service.apply(tmp_path),
        lambda: service.rollback(tmp_path),
    ):
        with pytest.raises(MigrationV7Error) as refused:
            attempt()
        assert refused.value.code == "ACTIVE_POINTER_MISSING"
        assert candidate.relative_path in str(refused.value)
    with pytest.raises(MigrationV7Error) as recovery:
        service.recover_pointer(tmp_path)
    assert recovery.value.code == "POINTER_RECOVERY_AMBIGUOUS"
    assert candidate.relative_path in str(recovery.value)

    assert source.read_bytes() == source_bytes
    assert candidate.path.read_bytes() == candidate_bytes
    assert runs == sorted(
        path.name for path in (storage / "migrations" / "v7").iterdir()
    )
    assert not (storage / "active-db.json").exists()


def test_a_v6_store_whose_migration_was_rolled_back_or_never_activated_still_opens(
    tmp_path: Path,
):
    """Only a candidate still recorded as active stands for a lost pointer."""

    from tests.test_migrate_v7_end_to_end import _registry, _v6_database

    rolled_root = tmp_path / "rolled"
    rolled_storage = _v6_database(rolled_root)
    service = MigrationV7Service(_registry(rolled_root))
    service.apply(rolled_root)
    assert service.rollback(rolled_root).status == "rolled_back"
    _lose_the_pointer(rolled_storage)
    assert _start_manager(rolled_storage) == (6, 0)

    stopped_root = tmp_path / "stopped"
    stopped_storage = _v6_database(stopped_root)

    def interrupt(stage: str, _details: dict[str, object]) -> None:
        if stage == "before_pointer":
            raise MigrationInterrupted(stage)

    with pytest.raises(MigrationInterrupted):
        MigrationV7Service(_registry(stopped_root), fault_injector=interrupt).apply(
            stopped_root
        )
    assert not (stopped_storage / "active-db.json").exists()
    resumed = MigrationV7Service(_registry(stopped_root)).apply(stopped_root)
    assert resumed.status == "activated"
    assert _start_manager(stopped_storage) == (7, 1)


def _cli(root: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "daem0nmcp.cli",
            "--json",
            "--project-path",
            str(root),
            *arguments,
        ],
        cwd=Path(__file__).resolve().parents[1],
        text=True,
        capture_output=True,
        timeout=300,
        check=False,
    )


def test_actual_cli_names_and_performs_the_pointer_recovery(tmp_path: Path):
    storage, database, registry = _workspace(tmp_path)
    _append_event(database, registry.default.workspace_id, "1")
    published = _lose_the_pointer(storage)

    for command in ("status", "briefing", "migrate"):
        refusal = _cli(tmp_path, command)
        assert refusal.returncode == 1, refusal.stdout + refusal.stderr
        message = json.loads(refusal.stdout)["error"]
        assert message.startswith("ACTIVE_POINTER_MISSING")
        assert RECOVERY_COMMAND in message
        assert "Traceback" not in refusal.stderr
        assert str(tmp_path) not in refusal.stdout

    refused = _cli(tmp_path, "migrate-v7", "--apply")
    assert refused.returncode == 1, refused.stdout + refused.stderr
    error = json.loads(refused.stdout)["error"]
    assert error["code"] == "ACTIVE_POINTER_MISSING"
    assert RECOVERY_COMMAND in error["message"]
    assert str(tmp_path) not in refused.stdout
    assert _cli(tmp_path, "migrate-v7", "--apply", "--recover-pointer").returncode == 2

    recovered = _cli(tmp_path, "migrate-v7", "--recover-pointer")

    assert recovered.returncode == 0, recovered.stdout + recovered.stderr
    payload = json.loads(recovered.stdout)
    assert (payload["status"], payload["action"], payload["active_generation"]) == (
        "activated",
        "recover_pointer",
        1,
    )
    assert (storage / "active-db.json").read_bytes() == published
    assert _cli(tmp_path, "status").returncode == 0


def _migrated_v6_store_without_pointer(root: Path) -> tuple[Path, Path, str]:
    """A v6 store migrated to a candidate, with ``active-db.json`` then lost."""

    from tests.test_migrate_v7_end_to_end import _registry, _v6_database

    storage = _v6_database(root)
    MigrationV7Service(_registry(root)).apply(root)
    active = resolve_active_database(storage)
    assert active.relative_path.endswith("/candidate.db")
    _lose_the_pointer(storage)
    return storage, active.path, active.relative_path


def _assert_every_opener_refuses_the_unreadable_candidate(
    root: Path, storage: Path, relative: str
) -> None:
    from tests.test_migrate_v7_end_to_end import _registry

    source = storage / "daem0nmcp.db"
    source_bytes = source.read_bytes()
    service = MigrationV7Service(_registry(root))

    with pytest.raises(RuntimeError) as start:
        _remember(storage, "would land in the retained v6 source")
    assert getattr(start.value, "code", None) == "ACTIVE_POINTER_MISSING"
    assert relative in str(start.value)
    assert "cannot be read" in str(start.value)
    assert "from a backup" in str(start.value)
    assert str(root) not in str(start.value)
    for attempt in (service.dry_run, service.apply, service.rollback):
        with pytest.raises(MigrationV7Error) as refused:
            attempt(root)
        assert refused.value.code == "ACTIVE_POINTER_MISSING"
        assert relative in str(refused.value)
    with pytest.raises(MigrationV7Error) as recovery:
        service.recover_pointer(root)
    assert recovery.value.code == "POINTER_RECOVERY_AMBIGUOUS"
    assert relative in str(recovery.value)

    assert source.read_bytes() == source_bytes
    assert not (storage / "active-db.json").exists()


def test_a_damaged_candidate_of_a_pointerless_store_is_not_taken_for_inactive(
    tmp_path: Path,
):
    """A candidate that cannot be read may be the one the lost pointer named."""

    storage, candidate, relative = _migrated_v6_store_without_pointer(tmp_path)
    damaged = bytearray(candidate.read_bytes())
    damaged[:100] = b"\xff" * 100
    candidate.write_bytes(bytes(damaged))

    _assert_every_opener_refuses_the_unreadable_candidate(tmp_path, storage, relative)

    # The refusal names its way out: without the unreadable run the retained
    # format-6 file is the whole store again.
    candidate.parent.rename(tmp_path / "set-aside")
    assert _start_manager(storage) == (6, 0)


def test_a_locked_candidate_of_a_pointerless_store_is_not_taken_for_inactive(
    tmp_path: Path,
):
    storage, candidate, relative = _migrated_v6_store_without_pointer(tmp_path)
    with closing(sqlite3.connect(candidate)) as holder:
        holder.execute("PRAGMA locking_mode=EXCLUSIVE")
        holder.execute("BEGIN IMMEDIATE")
        with pytest.raises(RuntimeError) as start:
            _start_manager(storage)
        holder.rollback()
    assert getattr(start.value, "code", None) == "ACTIVE_POINTER_MISSING"
    assert relative in str(start.value)
    assert "cannot be read" in str(start.value)

    # Released, it is readable again and still recorded as active.
    with pytest.raises(RuntimeError) as later:
        _start_manager(storage)
    assert "is still recorded as the active" in str(later.value)


def test_run_directories_no_pointer_could_have_named_do_not_block_a_v6_store(
    tmp_path: Path,
):
    """Only a published candidate that cannot be inspected stands in the way."""

    from tests.test_migrate_v7_end_to_end import _v6_database

    storage = _v6_database(tmp_path)
    runs = storage / "migrations" / "v7"
    partial = runs / ("mig_" + "1" * 64)
    partial.mkdir(parents=True)
    (partial / "candidate.db.partial").write_bytes(b"not a database" * 100)
    empty = runs / ("mig_" + "2" * 64)
    empty.mkdir()
    (empty / "candidate.db").write_bytes(b"")
    unrelated = runs / ("mig_" + "3" * 64)
    unrelated.mkdir()
    with closing(sqlite3.connect(unrelated / "candidate.db")) as connection:
        connection.execute("CREATE TABLE notes(body TEXT)")
        connection.commit()
    malformed = runs / "mig_not-a-run"
    malformed.mkdir()
    (malformed / "candidate.db").write_bytes(b"not a database" * 100)

    assert _start_manager(storage) == (6, 0)


def test_the_pointer_guard_leaves_an_unfinished_wal_of_the_source_alone(
    tmp_path: Path,
):
    """Inspecting a pointerless source must not checkpoint it."""

    import shutil

    from tests.test_migrate_v7_end_to_end import _registry, _v6_database

    storage = _v6_database(tmp_path)
    source = storage / "daem0nmcp.db"
    wal = storage / "daem0nmcp.db-wal"
    aside = tmp_path / "aside"
    aside.mkdir()
    with closing(sqlite3.connect(source)) as writer:
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute("PRAGMA wal_autocheckpoint=0")
        writer.execute("CREATE TABLE left_in_the_wal(value TEXT)")
        writer.commit()
        assert wal.stat().st_size > 0
        # What a writer that died here leaves behind.
        for name in ("daem0nmcp.db", "daem0nmcp.db-wal", "daem0nmcp.db-shm"):
            shutil.copyfile(storage / name, aside / name)
    for name in ("daem0nmcp.db", "daem0nmcp.db-wal", "daem0nmcp.db-shm"):
        shutil.copyfile(aside / name, storage / name)
    source_bytes, wal_bytes = source.read_bytes(), wal.read_bytes()

    result = MigrationV7Service(_registry(tmp_path)).dry_run(tmp_path)

    assert (result.status, result.action) == ("dry_run", "migrate")
    assert result.inventory["wal_bytes"] == len(wal_bytes)
    assert source.read_bytes() == source_bytes
    assert wal.read_bytes() == wal_bytes


def test_a_source_the_pointer_guard_cannot_read_is_refused_not_passed(
    tmp_path: Path,
):
    """A pointerless format-7 database that is locked is not taken for format 6."""

    storage, database, registry = _workspace(tmp_path)
    _append_event(database, registry.default.workspace_id, "1")
    _lose_the_pointer(storage)
    service = MigrationV7Service(registry)

    with closing(sqlite3.connect(database)) as holder:
        holder.execute("PRAGMA locking_mode=EXCLUSIVE")
        holder.execute("BEGIN IMMEDIATE")
        for operation in (service.dry_run, service.apply, service.rollback):
            with pytest.raises(MigrationV7Error) as refused:
                operation(tmp_path)
            assert refused.value.code == "SOURCE_UNREADABLE"
        holder.rollback()

    assert not (storage / "migrations").exists()
    with pytest.raises(MigrationV7Error) as readable:
        service.dry_run(tmp_path)
    assert readable.value.code == "ACTIVE_POINTER_MISSING"
