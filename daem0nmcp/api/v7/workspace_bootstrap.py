"""Production bootstrap for an empty, registered v7 workspace."""

from __future__ import annotations

import asyncio
import logging
import os
import secrets
import sqlite3
import stat
import sys
import tempfile
from contextlib import suppress
from pathlib import Path

from ...database import DatabaseManager, FreshDatabaseRequiredError
from ...event_store import canonical_json_bytes
from ...protected_files import (
    DirectoryAncestryGuard,
    ProtectedPathError,
    guard_directory_ancestry,
    reject_linked_ancestry,
)
from ...storage_activation import (
    DEFAULT_DATABASE_NAME,
    LOCK_NAME,
    POINTER_NAME,
    ActiveDatabasePointer,
    DatabaseFileLock,
    DatabaseInUseError,
    validate_pointer,
)
from ...workspace import Workspace, resolve_derived_path

_BOOTSTRAP_LOCK_TIMEOUT_SECONDS = 30.0
_BOOTSTRAP_LOCK_RETRY_SECONDS = 0.05


class WorkspaceBootstrapError(RuntimeError):
    """A path-free, stable production bootstrap failure."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class _PreparedStorage:
    """Own the ancestry and open lock-file marker until bootstrap completes."""

    def __init__(
        self,
        storage: Path,
        guard: DirectoryAncestryGuard,
        marker_descriptor: int,
    ) -> None:
        self.storage = storage
        self.guard = guard
        self._marker_descriptor = marker_descriptor
        self._directory_descriptor = (
            None if os.name == "nt" else guard.duplicate_target_descriptor()
        )
        self._closed = False

    @property
    def directory_descriptor(self) -> int:
        if self._directory_descriptor is None:
            raise ProtectedPathError("directory descriptor is unavailable")
        return self._directory_descriptor

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            if self._directory_descriptor is not None:
                os.close(self._directory_descriptor)
        finally:
            try:
                os.close(self._marker_descriptor)
            finally:
                self.guard.close()


def _open_bootstrap_marker(storage: Path) -> int:
    marker = storage / LOCK_NAME
    flags = os.O_RDWR | getattr(os, "O_BINARY", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(marker, flags | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        descriptor = os.open(marker, flags)
    try:
        opened = os.fstat(descriptor)
        named = os.stat(marker, follow_symlinks=False)
        if not stat.S_ISREG(opened.st_mode) or not stat.S_ISREG(named.st_mode):
            raise ProtectedPathError("bootstrap marker is not a regular file")
        if not os.path.samestat(opened, named):
            raise ProtectedPathError("bootstrap marker identity changed")
        if sys.platform == "win32":
            import msvcrt

            try:
                if opened.st_size < 2:
                    os.lseek(descriptor, 1, os.SEEK_SET)
                    os.write(descriptor, b"\0")
                    os.fsync(descriptor)
                os.lseek(descriptor, 1, os.SEEK_SET)
                msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
            except OSError as exc:
                raise DatabaseInUseError() from exc
        else:
            try:
                import fcntl
            except ImportError as exc:
                raise ProtectedPathError("bootstrap locking is unavailable") from exc
            try:
                fcntl_symbols = vars(fcntl)
                flock = fcntl_symbols["flock"]
                operation = int(fcntl_symbols["LOCK_EX"]) | int(
                    fcntl_symbols["LOCK_NB"]
                )
                flock(descriptor, operation)
            except (BlockingIOError, OSError) as exc:
                raise DatabaseInUseError() from exc
        return descriptor
    except Exception:
        os.close(descriptor)
        raise


def _prepare_empty_storage(workspace: Workspace) -> _PreparedStorage | None:
    """Create the derived storage directory only when it can be trusted.

    A present database, activation pointer, or unrelated directory entry means
    the workspace is existing state.  Production startup must not interpret or
    repair that state as a fresh install.
    """

    unresolved = workspace.root / ".daem0nmcp" / "storage"
    reject_linked_ancestry(unresolved)
    storage = resolve_derived_path(workspace.root, ".daem0nmcp", "storage")
    storage.mkdir(parents=True, exist_ok=True)
    reject_linked_ancestry(unresolved)
    if not storage.is_dir():
        raise WorkspaceBootstrapError("WORKSPACE_STORAGE_UNSAFE")
    checked = resolve_derived_path(workspace.root, ".daem0nmcp", "storage")
    if checked != storage:
        raise WorkspaceBootstrapError("WORKSPACE_STORAGE_UNSAFE")

    database_exists = (storage / DEFAULT_DATABASE_NAME).exists()
    pointer_exists = (storage / POINTER_NAME).exists()
    lock_path = storage / LOCK_NAME
    if pointer_exists:
        if lock_path.exists() and lock_path.is_file() and not lock_path.is_symlink():
            with DatabaseFileLock(storage, "shared"):
                pass
        return None
    if database_exists:
        # A fresh initializer creates and holds this lock before it creates the
        # database.  Wait for that owner so a concurrent server cannot observe
        # the crash-safe interval before pointer publication.  An unlocked
        # pointerless database remains untouched and unavailable.
        if lock_path.exists() and lock_path.is_file() and not lock_path.is_symlink():
            with DatabaseFileLock(storage, "shared"):
                pass
        return None
    try:
        entries = iter(storage.iterdir())
        first = next(entries, None)
        if first is None:
            guard = guard_directory_ancestry(storage, boundary=workspace.root)
            try:
                marker = _open_bootstrap_marker(storage)
                try:
                    guard.verify()
                    return _PreparedStorage(storage, guard, marker)
                except Exception:
                    os.close(marker)
                    raise
            except Exception:
                guard.close()
                raise
        if (
            first.name != LOCK_NAME
            or next(entries, None) is not None
            or first.is_symlink()
            or not first.is_file()
        ):
            if (
                (
                    (storage / DEFAULT_DATABASE_NAME).exists()
                    or (storage / POINTER_NAME).exists()
                )
                and lock_path.exists()
                and lock_path.is_file()
                and not lock_path.is_symlink()
            ):
                with DatabaseFileLock(storage, "shared"):
                    pass
            return None
    except OSError as exc:
        raise WorkspaceBootstrapError("WORKSPACE_STORAGE_UNAVAILABLE") from exc
    guard = guard_directory_ancestry(storage, boundary=workspace.root)
    try:
        marker = _open_bootstrap_marker(storage)
        try:
            guard.verify()
            return _PreparedStorage(storage, guard, marker)
        except Exception:
            os.close(marker)
            raise
    except Exception:
        guard.close()
        raise


async def _open_exclusive_manager(storage: Path) -> DatabaseManager:
    deadline = asyncio.get_running_loop().time() + _BOOTSTRAP_LOCK_TIMEOUT_SECONDS
    while True:
        try:
            return await _construct_exclusive_manager(storage)
        except DatabaseInUseError:
            if asyncio.get_running_loop().time() >= deadline:
                raise WorkspaceBootstrapError("WORKSPACE_BOOTSTRAP_BUSY") from None
            await asyncio.sleep(_BOOTSTRAP_LOCK_RETRY_SECONDS)
            try:
                with DatabaseFileLock(storage, "shared"):
                    entries = list(storage.iterdir())
                    if len(entries) != 1:
                        raise FreshDatabaseRequiredError()
                    marker = entries[0]
                    if (
                        marker.name != LOCK_NAME
                        or marker.is_symlink()
                        or not stat.S_ISREG(marker.lstat().st_mode)
                    ):
                        raise FreshDatabaseRequiredError()
            except DatabaseInUseError:
                continue


async def _prepare_storage_once(workspace: Workspace) -> _PreparedStorage | None:
    preparation = asyncio.create_task(
        asyncio.to_thread(_prepare_empty_storage, workspace)
    )
    cancellation: asyncio.CancelledError | None = None
    while True:
        try:
            prepared = await asyncio.shield(preparation)
            break
        except asyncio.CancelledError as exc:
            cancellation = cancellation or exc
        except BaseException:
            if cancellation is not None:
                raise cancellation from None
            raise
    if cancellation is None:
        return prepared
    if prepared is not None:
        prepared.close()
    raise cancellation


async def _prepare_storage_with_retry(
    workspace: Workspace,
) -> _PreparedStorage | None:
    deadline = asyncio.get_running_loop().time() + _BOOTSTRAP_LOCK_TIMEOUT_SECONDS
    while True:
        try:
            return await _prepare_storage_once(workspace)
        except DatabaseInUseError:
            if asyncio.get_running_loop().time() >= deadline:
                raise WorkspaceBootstrapError("WORKSPACE_BOOTSTRAP_BUSY") from None
            await asyncio.sleep(_BOOTSTRAP_LOCK_RETRY_SECONDS)


async def _construct_exclusive_manager(storage: Path) -> DatabaseManager:
    """Transfer constructor ownership without leaking its lock on cancellation."""

    construction = asyncio.create_task(
        asyncio.to_thread(
            DatabaseManager,
            str(storage),
            lock_mode="exclusive",
            require_fresh=True,
        )
    )
    cancellation: asyncio.CancelledError | None = None
    while True:
        try:
            manager = await asyncio.shield(construction)
            break
        except asyncio.CancelledError as exc:
            cancellation = cancellation or exc
        except BaseException:
            if cancellation is not None:
                raise cancellation from None
            raise
    if cancellation is None:
        return manager

    try:
        await _close_manager_deferring_cancellation(manager)
    except asyncio.CancelledError:
        pass
    except Exception:
        logging.getLogger(__name__).warning(
            "Cancelled workspace bootstrap cleanup failed"
        )
    raise cancellation


async def _construct_staging_manager(
    storage: Path,
    workspace_root: Path,
) -> DatabaseManager:
    """Construct an isolated fresh manager with the production workspace ID."""

    construction = asyncio.create_task(
        asyncio.to_thread(
            DatabaseManager,
            str(storage),
            lock_mode="exclusive",
            require_fresh=True,
            fresh_workspace_root=workspace_root,
            defer_fresh_activation=True,
        )
    )
    cancellation: asyncio.CancelledError | None = None
    while True:
        try:
            manager = await asyncio.shield(construction)
            break
        except asyncio.CancelledError as exc:
            cancellation = cancellation or exc
        except BaseException:
            if cancellation is not None:
                raise cancellation from None
            raise
    if cancellation is None:
        return manager
    try:
        await _close_manager_deferring_cancellation(manager)
    except asyncio.CancelledError:
        pass
    except Exception:
        logging.getLogger(__name__).warning(
            "Cancelled workspace staging cleanup failed"
        )
    raise cancellation


def _write_all(descriptor: int, raw: bytes) -> None:
    remaining = memoryview(raw)
    while remaining:
        written = os.write(descriptor, remaining)
        if written <= 0:
            raise OSError("bootstrap publication made no progress")
        remaining = remaining[written:]


def _copy_staged_database_at(directory_descriptor: int, source: Path) -> None:
    """Publish a closed database without resolving the storage pathname."""

    entries = set(os.listdir(directory_descriptor))
    if entries != {LOCK_NAME}:
        raise FreshDatabaseRequiredError()
    source_metadata = source.stat()
    if not stat.S_ISREG(source_metadata.st_mode):
        raise ProtectedPathError("staged database is not regular")

    temporary_name = f".bootstrap-v7-{secrets.token_hex(16)}.tmp"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(
        temporary_name,
        flags,
        0o600,
        dir_fd=directory_descriptor,
    )
    linked = False
    try:
        with source.open("rb") as handle:
            while True:
                block = handle.read(1024 * 1024)
                if not block:
                    break
                _write_all(descriptor, block)
        os.fsync(descriptor)
        copied = os.fstat(descriptor)
        if copied.st_size != source_metadata.st_size:
            raise OSError("staged database publication was incomplete")
        os.close(descriptor)
        descriptor = -1
        os.link(
            temporary_name,
            DEFAULT_DATABASE_NAME,
            src_dir_fd=directory_descriptor,
            dst_dir_fd=directory_descriptor,
            follow_symlinks=False,
        )
        linked = True
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        with suppress(FileNotFoundError):
            os.unlink(temporary_name, dir_fd=directory_descriptor)
    if not linked:
        raise OSError("staged database publication failed")
    os.fsync(directory_descriptor)


def _write_active_pointer_at(directory_descriptor: int) -> None:
    """Publish generation one relative to the retained storage descriptor."""

    pointer = validate_pointer(
        ActiveDatabasePointer(7, 1, DEFAULT_DATABASE_NAME, None, None)
    )
    active = os.stat(
        pointer.active_db,
        dir_fd=directory_descriptor,
        follow_symlinks=False,
    )
    if not stat.S_ISREG(active.st_mode):
        raise ProtectedPathError("published database is not regular")
    raw = canonical_json_bytes(
        {
            "active_db": pointer.active_db,
            "format_version": pointer.format_version,
            "generation": pointer.generation,
            "migration_run_id": pointer.migration_run_id,
            "previous_db": pointer.previous_db,
        }
    )
    temporary_name = f".bootstrap-pointer-{secrets.token_hex(16)}.tmp"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(
        temporary_name,
        flags,
        0o600,
        dir_fd=directory_descriptor,
    )
    linked = False
    try:
        _write_all(descriptor, raw)
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = -1
        os.link(
            temporary_name,
            POINTER_NAME,
            src_dir_fd=directory_descriptor,
            dst_dir_fd=directory_descriptor,
            follow_symlinks=False,
        )
        linked = True
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        with suppress(FileNotFoundError):
            os.unlink(temporary_name, dir_fd=directory_descriptor)
    if not linked:
        raise OSError("activation pointer publication failed")
    os.fsync(directory_descriptor)


def _publish_staged_database(directory_descriptor: int, source: Path) -> None:
    _copy_staged_database_at(directory_descriptor, source)
    _write_active_pointer_at(directory_descriptor)


async def _publish_staged_database_deferring_cancellation(
    directory_descriptor: int,
    source: Path,
) -> None:
    publication = asyncio.create_task(
        asyncio.to_thread(_publish_staged_database, directory_descriptor, source)
    )
    cancellation: asyncio.CancelledError | None = None
    while True:
        try:
            await asyncio.shield(publication)
            break
        except asyncio.CancelledError as exc:
            cancellation = cancellation or exc
        except BaseException:
            if cancellation is not None:
                raise cancellation from None
            raise
    if cancellation is not None:
        raise cancellation


async def _bootstrap_posix_staged(
    workspace: Workspace,
    prepared: _PreparedStorage,
) -> None:
    """Build privately, then publish only through the retained directory FD."""

    with tempfile.TemporaryDirectory(prefix="daem0nmcp-v7-bootstrap-") as raw:
        staging = Path(raw) / "storage"
        manager = await _construct_staging_manager(staging, workspace.root)
        try:
            if not manager.fresh_at_construction:
                raise FreshDatabaseRequiredError()
            await manager.init_db()
        finally:
            await _close_manager_deferring_cancellation(manager)

        staged_database = staging / DEFAULT_DATABASE_NAME
        with sqlite3.connect(staged_database) as connection:
            connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            connection.execute("PRAGMA journal_mode=DELETE")
            integrity = [row[0] for row in connection.execute("PRAGMA integrity_check")]
        if integrity != ["ok"]:
            raise RuntimeError("DATABASE_INTEGRITY_FAILED")
        prepared.guard.verify()
        await _publish_staged_database_deferring_cancellation(
            prepared.directory_descriptor,
            staged_database,
        )
        prepared.guard.verify()


async def _close_manager_deferring_cancellation(manager: DatabaseManager) -> None:
    """Finish manager closure before propagating any caller cancellation."""

    close_task = asyncio.create_task(manager.close())
    cancellation: asyncio.CancelledError | None = None
    while True:
        try:
            await asyncio.shield(close_task)
            break
        except asyncio.CancelledError as exc:
            if close_task.done():
                raise
            cancellation = cancellation or exc
    if cancellation is not None:
        raise cancellation


async def bootstrap_registered_workspace(workspace: Workspace) -> bool:
    """Initialize one provably empty workspace and publish generation one.

    Returns true only when this process performed initialization.  Existing
    state is deliberately left untouched, including pointerless databases and
    malformed activation pointers.
    """

    try:
        prepared = await _prepare_storage_with_retry(workspace)
    except WorkspaceBootstrapError:
        raise
    except Exception as exc:
        raise WorkspaceBootstrapError("WORKSPACE_STORAGE_UNSAFE") from exc
    if prepared is None:
        return False
    storage = prepared.storage

    manager: DatabaseManager | None = None
    guard = prepared.guard
    try:
        guard.verify()
        if os.name != "nt":
            await _bootstrap_posix_staged(workspace, prepared)
            return True
        try:
            manager = await _open_exclusive_manager(storage)
        except FreshDatabaseRequiredError:
            return False
        guard.verify()
        # Another process may have completed bootstrap while this process was
        # waiting for the generation lock.  The locked predicate is decisive.
        if not manager.fresh_at_construction:
            return False
        guard.verify()
        await manager.init_db()
        guard.verify()
        return True
    except WorkspaceBootstrapError:
        raise
    except ProtectedPathError as exc:
        raise WorkspaceBootstrapError("WORKSPACE_STORAGE_UNSAFE") from exc
    except Exception as exc:
        raise WorkspaceBootstrapError("WORKSPACE_BOOTSTRAP_FAILED") from exc
    finally:
        try:
            if manager is not None:
                try:
                    await _close_manager_deferring_cancellation(manager)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logging.getLogger(__name__).warning(
                        "Workspace bootstrap cleanup failed"
                    )
        finally:
            prepared.close()


class WorkspaceBootstrapLifecycle:
    """Initialize fresh storage for the server's registered workspaces."""

    def __init__(self, workspaces: tuple[Workspace, ...]) -> None:
        self._workspaces = workspaces
        self.failures: dict[str, str] = {}

    async def start(self) -> None:
        for workspace in self._workspaces:
            try:
                await bootstrap_registered_workspace(workspace)
            except WorkspaceBootstrapError as exc:
                self.failures[workspace.workspace_id] = exc.code
                logging.getLogger(__name__).warning(
                    "Workspace bootstrap unavailable: %s", exc.code
                )


__all__ = [
    "WorkspaceBootstrapError",
    "WorkspaceBootstrapLifecycle",
    "bootstrap_registered_workspace",
]
