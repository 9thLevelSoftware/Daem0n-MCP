"""Fresh-workspace bootstrap acceptance through the production process."""

from __future__ import annotations

import asyncio
import os
import sqlite3
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from daem0nmcp.api.v7 import workspace_bootstrap
from daem0nmcp.api.v7.workspace_bootstrap import (
    WorkspaceBootstrapError,
    bootstrap_registered_workspace,
)
from daem0nmcp.storage_activation import DatabaseFileLock, resolve_active_database
from daem0nmcp.workspace import WorkspaceRegistry
from tests.api_v7.process_client import call, process_client, succeed


@pytest.mark.parametrize("transport", ["stdio", "streamable-http"])
async def test_fresh_process_bootstraps_store_and_recalls_after_restart(
    tmp_path, transport
):
    workspace = WorkspaceRegistry([tmp_path], default_root=tmp_path).default
    storage = tmp_path / ".daem0nmcp" / "storage"
    assert not storage.exists()
    scope = {"workspace_id": workspace.workspace_id}
    store_arguments = {
        "record_type": "decision",
        "content": "Fresh production bootstrap persists this decision",
        "idempotency_key": f"fresh-bootstrap-{transport}",
    }

    async with process_client(workspace.root, transport) as session:
        await succeed(session, "session_brief", scope)
        preflight = await succeed(
            session,
            "memory_preflight",
            {
                **scope,
                "target_tool": "memory_store",
                "target_arguments": store_arguments,
            },
        )
        stored = await succeed(
            session,
            "memory_store",
            {
                **scope,
                **store_arguments,
                "preflight_token": preflight["preflight_token"],
            },
        )
        record_id = stored["record"]["record_id"]

    active = resolve_active_database(storage)
    assert active.format_version == 7
    assert active.generation == 1

    async with process_client(workspace.root, transport) as restarted:
        await succeed(restarted, "session_brief", scope)
        visibility_deadline = asyncio.get_running_loop().time() + 5
        while True:
            recalled = await succeed(
                restarted,
                "memory_recall",
                {
                    **scope,
                    "query": "fresh production bootstrap decision",
                    "limit": 5,
                },
            )
            if record_id in {item["record"]["record_id"] for item in recalled["items"]}:
                break
            assert asyncio.get_running_loop().time() < visibility_deadline, recalled
            await asyncio.sleep(0.1)
    assert record_id in {item["record"]["record_id"] for item in recalled["items"]}


async def test_parallel_fresh_bootstrap_publishes_one_generation(tmp_path):
    workspace = WorkspaceRegistry([tmp_path], default_root=tmp_path).default

    results = await asyncio.gather(
        bootstrap_registered_workspace(workspace),
        bootstrap_registered_workspace(workspace),
    )

    assert sorted(results) == [False, True]
    active = resolve_active_database(tmp_path / ".daem0nmcp" / "storage")
    assert active.format_version == 7
    assert active.generation == 1
    assert active.previous_db is None
    assert active.migration_run_id is None


@pytest.mark.skipif(os.name != "nt", reason="Windows shared lock semantics")
def test_windows_generation_lock_is_shared_across_processes(tmp_path):
    storage = tmp_path / "storage"
    storage.mkdir()
    script = (
        "import sys\n"
        "from daem0nmcp.storage_activation import DatabaseFileLock\n"
        "lock = DatabaseFileLock(sys.argv[1], 'shared').acquire()\n"
        "print('ready', flush=True)\n"
        "sys.stdin.readline()\n"
        "lock.release()\n"
    )
    child = subprocess.Popen(
        [sys.executable, "-c", script, str(storage)],
        cwd=Path(__file__).resolve().parents[2],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert child.stdout is not None
        assert child.stdout.readline().strip() == "ready"
        with DatabaseFileLock(storage, "shared"):
            pass
        with pytest.raises(workspace_bootstrap.DatabaseInUseError):
            DatabaseFileLock(storage, "exclusive").acquire()
    finally:
        if child.poll() is None:
            assert child.stdin is not None
            child.stdin.write("\n")
            child.stdin.flush()
        _stdout, stderr = child.communicate(timeout=5)
    assert child.returncode == 0, stderr

    with DatabaseFileLock(storage, "exclusive"):
        pass


@pytest.mark.skipif(os.name != "nt", reason="Windows bootstrap marker semantics")
def test_windows_bootstrap_marker_serializes_preparers(tmp_path):
    storage = tmp_path / "storage"
    storage.mkdir()

    first = workspace_bootstrap._open_bootstrap_marker(storage)
    try:
        with pytest.raises(workspace_bootstrap.DatabaseInUseError):
            workspace_bootstrap._open_bootstrap_marker(storage)
    finally:
        os.close(first)

    third = workspace_bootstrap._open_bootstrap_marker(storage)
    os.close(third)


def test_empty_snapshot_waits_for_concurrent_generation_owner(tmp_path, monkeypatch):
    workspace = WorkspaceRegistry([tmp_path], default_root=tmp_path).default
    storage = tmp_path / ".daem0nmcp" / "storage"
    database = storage / workspace_bootstrap.DEFAULT_DATABASE_NAME
    original_exists = Path.exists
    owner = None
    injected = False

    def exists_with_race(path):
        nonlocal injected, owner
        result = original_exists(path)
        if path == database and not injected:
            injected = True
            database.write_bytes(b"concurrent bootstrap state")
            owner = DatabaseFileLock(storage, "exclusive").acquire()
        return result

    monkeypatch.setattr(Path, "exists", exists_with_race)
    try:
        with pytest.raises(workspace_bootstrap.DatabaseInUseError):
            workspace_bootstrap._prepare_empty_storage(workspace)
    finally:
        if owner is not None:
            owner.release()


async def test_cancelled_constructor_result_is_closed_before_propagation(
    tmp_path, monkeypatch
):
    started = threading.Event()
    release = threading.Event()
    closed = asyncio.Event()

    class Manager:
        async def close(self) -> None:
            closed.set()

    manager = Manager()

    def construct(_storage: str, *, lock_mode: str, require_fresh: bool):
        assert lock_mode == "exclusive"
        assert require_fresh is True
        started.set()
        assert release.wait(5)
        return manager

    monkeypatch.setattr(workspace_bootstrap, "DatabaseManager", construct)
    construction = asyncio.create_task(
        workspace_bootstrap._open_exclusive_manager(tmp_path)
    )
    assert await asyncio.to_thread(started.wait, 5)

    construction.cancel()
    await asyncio.sleep(0)
    release.set()

    with pytest.raises(asyncio.CancelledError):
        await construction
    assert closed.is_set()


async def test_exclusive_manager_retry_rejects_new_storage_state(tmp_path, monkeypatch):
    storage = tmp_path / "storage"
    storage.mkdir()
    (storage / workspace_bootstrap.LOCK_NAME).write_bytes(b"")
    calls = 0

    async def construct(_storage):
        nonlocal calls
        calls += 1
        if calls == 1:
            (storage / workspace_bootstrap.DEFAULT_DATABASE_NAME).write_bytes(
                b"concurrent bootstrap state"
            )
            raise workspace_bootstrap.DatabaseInUseError()
        raise AssertionError("exclusive manager retried after storage changed")

    monkeypatch.setattr(workspace_bootstrap, "_construct_exclusive_manager", construct)

    with pytest.raises(workspace_bootstrap.FreshDatabaseRequiredError):
        await workspace_bootstrap._open_exclusive_manager(storage)
    assert calls == 1


async def test_locked_fresh_check_rejects_foreign_entry_race(tmp_path):
    workspace = WorkspaceRegistry([tmp_path], default_root=tmp_path).default
    storage = tmp_path / ".daem0nmcp" / "storage"
    storage.mkdir(parents=True)
    blocker = DatabaseFileLock(storage, "exclusive").acquire()
    sentinel = storage / "preexisting-state.bin"
    try:
        bootstrap = asyncio.create_task(bootstrap_registered_workspace(workspace))
        await asyncio.sleep(0.15)
        sentinel.write_bytes(b"must-not-initialize")
    finally:
        blocker.release()

    assert await bootstrap is False
    assert sentinel.read_bytes() == b"must-not-initialize"
    assert not (storage / "daem0nmcp.db").exists()
    assert not (storage / "active-db.json").exists()


async def test_repeated_cancellation_drains_final_manager_close(tmp_path, monkeypatch):
    workspace = WorkspaceRegistry([tmp_path], default_root=tmp_path).default
    init_entered = asyncio.Event()
    close_entered = asyncio.Event()
    close_completed = asyncio.Event()
    close_cancelled = asyncio.Event()

    class Manager:
        fresh_at_construction = True

        async def init_db(self) -> None:
            init_entered.set()
            await asyncio.Event().wait()

        async def close(self) -> None:
            close_entered.set()
            try:
                await asyncio.sleep(0.1)
            except asyncio.CancelledError:
                close_cancelled.set()
                raise
            close_completed.set()

    async def open_manager(_storage, *_args):
        return Manager()

    manager_factory = (
        "_open_exclusive_manager" if os.name == "nt" else "_construct_staging_manager"
    )
    monkeypatch.setattr(workspace_bootstrap, manager_factory, open_manager)
    bootstrap = asyncio.create_task(bootstrap_registered_workspace(workspace))
    await init_entered.wait()
    bootstrap.cancel()
    await close_entered.wait()
    bootstrap.cancel()

    with pytest.raises(asyncio.CancelledError):
        await bootstrap
    assert close_completed.is_set()
    assert not close_cancelled.is_set()


async def test_parallel_process_startup_shares_fresh_generation(tmp_path):
    workspace = WorkspaceRegistry([tmp_path], default_root=tmp_path).default
    scope = {"workspace_id": workspace.workspace_id}

    async def brief_from_process() -> str:
        async with process_client(workspace.root, "stdio") as session:
            briefing = await succeed(session, "session_brief", scope)
            return briefing["workspace_id"]

    results = await asyncio.gather(brief_from_process(), brief_from_process())

    assert results == [workspace.workspace_id, workspace.workspace_id]
    active = resolve_active_database(tmp_path / ".daem0nmcp" / "storage")
    assert active.format_version == 7
    assert active.generation == 1


async def test_established_v7_bootstrap_path_is_byte_preserving(tmp_path):
    workspace = WorkspaceRegistry([tmp_path], default_root=tmp_path).default
    assert await bootstrap_registered_workspace(workspace) is True
    storage = tmp_path / ".daem0nmcp" / "storage"
    database = storage / "daem0nmcp.db"
    pointer = storage / "active-db.json"
    before = (database.read_bytes(), pointer.read_bytes())

    assert await bootstrap_registered_workspace(workspace) is False

    assert (database.read_bytes(), pointer.read_bytes()) == before


@pytest.mark.parametrize("existing_kind", ["v6", "corrupt", "interrupted"])
async def test_existing_pointerless_database_is_never_bootstrapped(
    tmp_path, existing_kind
):
    workspace = WorkspaceRegistry([tmp_path], default_root=tmp_path).default
    storage = tmp_path / ".daem0nmcp" / "storage"
    storage.mkdir(parents=True)
    database = storage / "daem0nmcp.db"
    if existing_kind == "corrupt":
        database.write_bytes(b"not-a-sqlite-database")
    else:
        with sqlite3.connect(database) as connection:
            if existing_kind == "v6":
                connection.execute("CREATE TABLE retained_v6 (value TEXT)")
                connection.execute("INSERT INTO retained_v6 VALUES ('preserve')")
            else:
                connection.execute("CREATE TABLE schema_version (version INTEGER)")
                connection.execute("INSERT INTO schema_version VALUES (29)")
    before = database.read_bytes()

    assert await bootstrap_registered_workspace(workspace) is False

    assert database.read_bytes() == before
    assert not (storage / "active-db.json").exists()
    assert not (storage / ".migrate-v7.lock").exists()


async def test_invalid_pointer_is_not_opened_or_rewritten(tmp_path):
    workspace = WorkspaceRegistry([tmp_path], default_root=tmp_path).default
    storage = tmp_path / ".daem0nmcp" / "storage"
    storage.mkdir(parents=True)
    pointer = storage / "active-db.json"
    pointer.write_bytes(b'{"invalid":"pointer"}')
    before = pointer.read_bytes()

    assert await bootstrap_registered_workspace(workspace) is False

    assert pointer.read_bytes() == before
    assert not (storage / "daem0nmcp.db").exists()
    assert not (storage / ".migrate-v7.lock").exists()

    async with process_client(workspace.root, "stdio") as session:
        result = await call(
            session,
            "session_brief",
            {"workspace_id": workspace.workspace_id},
        )
    assert result["error"]["code"] == "CAPABILITY_DEGRADED"
    assert pointer.read_bytes() == before


async def test_bootstrap_rejects_linked_storage_without_touching_target(tmp_path):
    workspace = WorkspaceRegistry([tmp_path], default_root=tmp_path).default
    target = tmp_path / "target"
    target.mkdir()
    managed = tmp_path / ".daem0nmcp"
    managed.mkdir()
    try:
        (managed / "storage").symlink_to(target, target_is_directory=True)
    except OSError:
        pytest.skip("directory symlinks are unavailable")

    with pytest.raises(WorkspaceBootstrapError, match="WORKSPACE_STORAGE_UNSAFE"):
        await bootstrap_registered_workspace(workspace)

    assert list(target.iterdir()) == []


@pytest.mark.skipif(os.name == "nt", reason="POSIX descriptor publication regression")
async def test_posix_storage_swap_before_staging_init_cannot_reach_outside(
    tmp_path, monkeypatch
):
    root = tmp_path / "workspace"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    workspace = WorkspaceRegistry([root], default_root=root).default
    storage = root / ".daem0nmcp" / "storage"
    moved = root / ".daem0nmcp" / "moved-storage"
    original = workspace_bootstrap._construct_staging_manager

    async def construct(staging, workspace_root):
        manager = await original(staging, workspace_root)
        initialize = manager.init_db

        async def swap_then_initialize():
            storage.rename(moved)
            storage.symlink_to(outside, target_is_directory=True)
            await initialize()

        manager.init_db = swap_then_initialize
        return manager

    monkeypatch.setattr(workspace_bootstrap, "_construct_staging_manager", construct)
    with pytest.raises(WorkspaceBootstrapError) as raised:
        await bootstrap_registered_workspace(workspace)

    assert raised.value.code == "WORKSPACE_STORAGE_UNSAFE", repr(raised.value.__cause__)
    assert list(outside.iterdir()) == []
    assert not (outside / "daem0nmcp.db").exists()
    assert not (outside / "active-db.json").exists()
    assert not (moved / "daem0nmcp.db").exists()


@pytest.mark.skipif(os.name == "nt", reason="POSIX descriptor publication regression")
async def test_posix_late_storage_swap_publishes_only_to_retained_directory(
    tmp_path, monkeypatch
):
    root = tmp_path / "workspace"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    workspace = WorkspaceRegistry([root], default_root=root).default
    storage = root / ".daem0nmcp" / "storage"
    moved = root / ".daem0nmcp" / "moved-storage"
    publish = workspace_bootstrap._publish_staged_database

    def swap_then_publish(directory_descriptor, source):
        storage.rename(moved)
        storage.symlink_to(outside, target_is_directory=True)
        publish(directory_descriptor, source)

    monkeypatch.setattr(
        workspace_bootstrap,
        "_publish_staged_database",
        swap_then_publish,
    )
    with pytest.raises(WorkspaceBootstrapError) as raised:
        await bootstrap_registered_workspace(workspace)

    assert raised.value.code == "WORKSPACE_STORAGE_UNSAFE", repr(raised.value.__cause__)
    assert list(outside.iterdir()) == []
    assert not (outside / "daem0nmcp.db").exists()
    assert not (outside / "active-db.json").exists()
    assert (moved / "daem0nmcp.db").is_file()
    assert (moved / "active-db.json").is_file()


@pytest.mark.skipif(os.name != "nt", reason="Windows junction regression")
async def test_prepare_to_lock_junction_swap_cannot_reach_outside(
    tmp_path, monkeypatch
):
    root = tmp_path / "workspace"
    target = tmp_path / "outside"
    root.mkdir()
    target.mkdir()
    workspace = WorkspaceRegistry([root], default_root=root).default
    original = workspace_bootstrap._open_exclusive_manager

    async def swap_to_junction(storage):
        storage.rmdir()
        created = subprocess.run(
            ["cmd.exe", "/d", "/c", "mklink", "/J", str(storage), str(target)],
            check=False,
            capture_output=True,
            text=True,
        )
        assert created.returncode == 0
        return await original(storage)

    monkeypatch.setattr(
        workspace_bootstrap,
        "_open_exclusive_manager",
        swap_to_junction,
    )
    with pytest.raises(WorkspaceBootstrapError) as raised:
        await bootstrap_registered_workspace(workspace)

    assert raised.value.code == "WORKSPACE_BOOTSTRAP_FAILED"
    assert list(target.iterdir()) == []
    assert not (target / "daem0nmcp.db").exists()
    assert not (target / "active-db.json").exists()


@pytest.mark.skipif(os.name != "nt", reason="Windows junction regression")
async def test_preexisting_storage_junction_is_rejected_before_marker(tmp_path):
    root = tmp_path / "workspace"
    target = tmp_path / "outside"
    managed = root / ".daem0nmcp"
    managed.mkdir(parents=True)
    target.mkdir()
    storage = managed / "storage"
    created = subprocess.run(
        ["cmd.exe", "/d", "/c", "mklink", "/J", str(storage), str(target)],
        check=False,
        capture_output=True,
        text=True,
    )
    if created.returncode != 0:
        pytest.skip("directory junctions are unavailable")
    workspace = WorkspaceRegistry([root], default_root=root).default
    try:
        with pytest.raises(WorkspaceBootstrapError) as raised:
            await bootstrap_registered_workspace(workspace)
        assert raised.value.code == "WORKSPACE_STORAGE_UNSAFE"
        assert list(target.iterdir()) == []
    finally:
        os.rmdir(storage)


@pytest.mark.skipif(os.name != "nt", reason="Windows junction regression")
async def test_late_storage_swap_is_blocked_until_bootstrap_closes(
    tmp_path, monkeypatch
):
    root = tmp_path / "workspace"
    target = tmp_path / "outside"
    root.mkdir()
    target.mkdir()
    workspace = WorkspaceRegistry([root], default_root=root).default
    storage = root / ".daem0nmcp" / "storage"
    moved = root / ".daem0nmcp" / "moved-storage"

    class Manager:
        fresh_at_construction = True

        async def init_db(self) -> None:
            storage.rename(moved)
            created = subprocess.run(
                [
                    "cmd.exe",
                    "/d",
                    "/c",
                    "mklink",
                    "/J",
                    str(storage),
                    str(target),
                ],
                check=False,
                capture_output=True,
                text=True,
            )
            assert created.returncode == 0
            (storage / "daem0nmcp.db").write_bytes(b"escaped")

        async def close(self) -> None:
            return None

    async def open_manager(_storage):
        return Manager()

    monkeypatch.setattr(workspace_bootstrap, "_open_exclusive_manager", open_manager)
    with pytest.raises(WorkspaceBootstrapError) as raised:
        await bootstrap_registered_workspace(workspace)

    assert raised.value.code == "WORKSPACE_BOOTSTRAP_FAILED"
    assert storage.is_dir()
    assert not moved.exists()
    assert list(target.iterdir()) == []
