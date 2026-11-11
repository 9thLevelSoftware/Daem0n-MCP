"""Validated v7 database activation pointers and cross-process storage locks.

This module deliberately depends only on the Python standard library.  Migration
dry-runs can therefore inspect storage without importing the ORM, vector, model,
or network stacks.
"""

from __future__ import annotations

import ctypes
import json
import os
import sqlite3
import stat
import sys
import threading
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath
from typing import BinaryIO, Literal

from .event_store import canonical_json_bytes
from .protected_files import reject_linked_ancestry
from .workspace import normalize_resolved_path, sqlite_file_uri

POINTER_NAME = "active-db.json"
POINTER_TEMP_NAME = "active-db.json.tmp"
LOCK_NAME = ".migrate-v7.lock"
DEFAULT_DATABASE_NAME = "daem0nmcp.db"
_POINTER_FIELDS = {
    "format_version",
    "generation",
    "active_db",
    "previous_db",
    "migration_run_id",
}
# Format recognition must remain stable across additive schema releases.  A
# pointerless copy at schema 18 is still format 7 after this process learns
# about schemas 19+, and legacy vector writers must continue to reject it.
_V7_FORMAT_MIN_SCHEMA_VERSION = 18
_V7_FORMAT_TABLES = frozenset(
    {
        "memory_events",
        "memory_records",
        "memory_fact_versions",
        "memory_provenance_edges",
        "memory_outcome_signals",
        "memory_relationship_versions",
        "projection_manifests",
        "enrichment_decisions",
        "background_jobs",
        "v7_migration_runs",
        "v7_migration_checkpoints",
        "legacy_id_map",
        "retrieval_documents",
        "record_procedures",
        "record_outcome_view",
        "dense_projection_refs",
    }
)


class PointerValidationError(ValueError):
    """Raised when an activation pointer cannot be trusted."""

    code = "UNSAFE_ACTIVE_POINTER"

    def __init__(self, detail: str) -> None:
        super().__init__(f"{self.code}: {detail}")


class DatabaseInUseError(RuntimeError):
    """Raised when a nonblocking storage lock cannot be acquired."""

    code = "DATABASE_IN_USE"

    def __init__(self) -> None:
        super().__init__(self.code)


@dataclass(frozen=True, slots=True)
class ActiveDatabasePointer:
    """The exact JSON value stored in ``active-db.json``."""

    format_version: int
    generation: int
    active_db: str
    previous_db: str | None
    migration_run_id: str | None


@dataclass(frozen=True, slots=True)
class ResolvedActiveDatabase:
    """A validated active database selection."""

    storage_path: Path
    path: Path
    relative_path: str
    format_version: int
    generation: int
    previous_db: str | None
    migration_run_id: str | None
    pointer: ActiveDatabasePointer | None
    pointer_bytes: bytes | None


def _invalid(detail: str) -> PointerValidationError:
    return PointerValidationError(detail)


def _is_plain_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _valid_prefixed_hash(value: object, prefix: str) -> bool:
    if not isinstance(value, str) or not value.startswith(prefix + "_"):
        return False
    digest = value[len(prefix) + 1 :]
    return len(digest) == 64 and all(char in "0123456789abcdef" for char in digest)


def _validate_relative_name(value: object, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise _invalid(f"{field} must be a non-empty relative path")
    if "\\" in value:
        raise _invalid(f"{field} must use portable '/' separators")
    pure = PurePosixPath(value)
    if pure.is_absolute() or pure.anchor or ":" in pure.parts[0]:
        raise _invalid(f"{field} must be relative")
    if any(part in {"", ".", ".."} or ":" in part for part in pure.parts):
        raise _invalid(f"{field} contains an unsafe path component")
    return pure.as_posix()


def _resolve_storage_file(storage: Path, relative_name: str, field: str) -> Path:
    """Resolve a pointer target while rejecting every symlink component."""

    try:
        reject_linked_ancestry(storage)
        root = normalize_resolved_path(storage.resolve(strict=True))
    except (OSError, RuntimeError) as exc:
        raise _invalid("storage directory is unavailable") from exc
    candidate = storage.joinpath(*PurePosixPath(relative_name).parts)
    current = storage
    try:
        for part in PurePosixPath(relative_name).parts:
            current = current / part
            if current.is_symlink():
                raise _invalid(f"{field} may not traverse a symlink")
        resolved = normalize_resolved_path(candidate.resolve(strict=True))
        resolved.relative_to(root)
    except PointerValidationError:
        raise
    except (OSError, RuntimeError, ValueError) as exc:
        raise _invalid(f"{field} target is missing or outside storage") from exc
    if not resolved.is_file():
        raise _invalid(f"{field} target is not a file")
    return resolved


def readonly_database_uri(path: str | os.PathLike[str]) -> str:
    """Return an offline read URI that preserves database and sidecar data.

    With no ``-wal``, ``-shm`` or ``-journal`` the main file is the whole
    database and is read as immutable, preventing SQLite from creating WAL
    sidecars. Existing sidecars must remain visible so their committed data is
    included. Windows uses the no-lock VFS; POSIX uses the default VFS because
    ``unix-none`` cannot open WAL databases. Reads with sidecars can update the
    volatile ``-shm`` wal-index read marks.
    """

    resolved = Path(path).resolve()
    if not any(
        Path(f"{resolved}{suffix}").exists() for suffix in ("-wal", "-shm", "-journal")
    ):
        parameters = "&immutable=1"
    elif sys.platform == "win32":
        parameters = "&vfs=win32-none"
    else:
        parameters = ""
    return sqlite_file_uri(resolved, mode="ro") + parameters


def validate_pointer(pointer: ActiveDatabasePointer) -> ActiveDatabasePointer:
    """Validate pointer field relationships without touching the filesystem."""

    if pointer.format_version not in {6, 7} or isinstance(pointer.format_version, bool):
        raise _invalid("unsupported format_version")
    if not _is_plain_int(pointer.generation) or pointer.generation < 1:
        raise _invalid("generation must be a positive integer")
    active_db = _validate_relative_name(pointer.active_db, "active_db")
    previous_db = (
        None
        if pointer.previous_db is None
        else _validate_relative_name(pointer.previous_db, "previous_db")
    )
    run_id = pointer.migration_run_id
    if run_id is not None and not _valid_prefixed_hash(run_id, "mig"):
        raise _invalid("migration_run_id is invalid")

    is_fresh = (
        active_db == DEFAULT_DATABASE_NAME and previous_db is None and run_id is None
    )
    if is_fresh:
        if pointer.format_version != 7 or pointer.generation != 1:
            raise _invalid("fresh v7 pointer must be generation one")
    elif run_id is None or previous_db is None:
        raise _invalid("migration pointers require prior database and run ID")
    if active_db == previous_db:
        raise _invalid("active_db and previous_db must differ")
    return ActiveDatabasePointer(
        format_version=pointer.format_version,
        generation=pointer.generation,
        active_db=active_db,
        previous_db=previous_db,
        migration_run_id=run_id,
    )


def _decode_pointer(raw: bytes) -> ActiveDatabasePointer:
    duplicate = False

    def pairs_hook(pairs):
        nonlocal duplicate
        result = {}
        for key, value in pairs:
            if key in result:
                duplicate = True
            result[key] = value
        return result

    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=pairs_hook)
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as exc:
        raise _invalid("pointer is not valid UTF-8 JSON") from exc
    if duplicate or not isinstance(value, dict) or set(value) != _POINTER_FIELDS:
        raise _invalid("pointer must contain exactly the supported fields")
    try:
        pointer = ActiveDatabasePointer(**value)
    except TypeError as exc:
        raise _invalid("pointer fields are invalid") from exc
    pointer = validate_pointer(pointer)
    if raw != canonical_json_bytes(asdict(pointer)):
        raise _invalid("pointer JSON is not canonical")
    return pointer


def resolve_active_database(
    storage_path: str | os.PathLike[str],
) -> ResolvedActiveDatabase:
    """Resolve the active database without creating or modifying any file."""

    storage = Path(storage_path)
    pointer_path = storage / POINTER_NAME
    if pointer_path.is_symlink():
        raise _invalid("pointer itself must be a regular file")
    if not pointer_path.exists():
        default = _resolve_storage_file(storage, DEFAULT_DATABASE_NAME, "active_db")
        return ResolvedActiveDatabase(
            storage_path=storage,
            path=default,
            relative_path=DEFAULT_DATABASE_NAME,
            format_version=6,
            generation=0,
            previous_db=None,
            migration_run_id=None,
            pointer=None,
            pointer_bytes=None,
        )
    if not pointer_path.is_file():
        raise _invalid("pointer itself must be a regular file")
    try:
        raw = pointer_path.read_bytes()
    except OSError as exc:
        raise _invalid("pointer cannot be read") from exc
    pointer = _decode_pointer(raw)
    active = _resolve_storage_file(storage, pointer.active_db, "active_db")
    if pointer.previous_db is not None:
        _resolve_storage_file(storage, pointer.previous_db, "previous_db")
    return ResolvedActiveDatabase(
        storage_path=storage,
        path=active,
        relative_path=pointer.active_db,
        format_version=pointer.format_version,
        generation=pointer.generation,
        previous_db=pointer.previous_db,
        migration_run_id=pointer.migration_run_id,
        pointer=pointer,
        pointer_bytes=raw,
    )


def canonical_v7_state_in(connection: sqlite3.Connection) -> bool:
    """Whether an open database holds format-7 canonical state.

    Only reads; a failure to read is raised, not answered as "no".
    """

    tables = {
        str(row[0])
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }
    if "schema_version" not in tables:
        return False
    try:
        version = int(
            connection.execute(
                "SELECT COALESCE(MAX(version),0) FROM schema_version"
            ).fetchone()[0]
        )
    except (TypeError, ValueError):
        return False
    if version < _V7_FORMAT_MIN_SCHEMA_VERSION:
        return False
    required_tables = _V7_FORMAT_TABLES
    if version < 33:
        required_tables = required_tables - {
            "memory_provenance_edges",
            "memory_outcome_signals",
        }
    if not tables >= required_tables:
        return False
    return any(
        connection.execute(f'SELECT 1 FROM "{table}" LIMIT 1').fetchone() is not None
        for table in (
            "memory_events",
            "memory_records",
            "projection_manifests",
            "v7_migration_runs",
        )
    )


def has_canonical_v7_state(database_path: str | os.PathLike[str]) -> bool:
    """Recognize copied v7 canonical state when its activation pointer is absent."""

    try:
        connection = sqlite3.connect(Path(database_path))
        try:
            return canonical_v7_state_in(connection)
        finally:
            connection.close()
    except (OSError, sqlite3.DatabaseError, TypeError, ValueError):
        return False


# A pointerless store resolves as format 6.  When it holds format-7 state the
# pointer was lost, and every opener that would run format-6 code refuses with
# this detail instead.
ACTIVE_POINTER_MISSING_CODE = "ACTIVE_POINTER_MISSING"
ACTIVE_POINTER_MISSING_DETAIL = (
    f"{DEFAULT_DATABASE_NAME} holds format-7 state but {POINTER_NAME} is "
    "missing, so it is not opened as a format-6 store. Restore "
    f"{POINTER_NAME} from a backup, or stop the server and run: "
    "python -m daem0nmcp.cli --project-path <project root> "
    "migrate-v7 --recover-pointer"
)


class CandidateUnreadableError(RuntimeError):
    """A published candidate of a pointerless store could not be inspected.

    The message is the operator text; ``candidate`` is storage-relative and
    names the candidate, or the migration root when that cannot be listed.
    """

    def __init__(self, candidate: str, *, unlistable: bool = False) -> None:
        self.candidate = candidate
        if unlistable:
            message = (
                f"{POINTER_NAME} is missing and {candidate} cannot be listed, so "
                "it may hold the active format-7 database and "
                f"{DEFAULT_DATABASE_NAME} is not opened in its place. Make that "
                f"directory readable and retry, or restore {POINTER_NAME} from "
                "a backup"
            )
        else:
            message = (
                f"{POINTER_NAME} is missing and {candidate} cannot be read, so it "
                f"may be the active format-7 database and {DEFAULT_DATABASE_NAME} "
                "is not opened in its place. If another process is using that "
                f"file, stop it and retry. Otherwise restore {POINTER_NAME} from a "
                "backup, or, if that migration run was never activated, move its "
                "directory out of the storage directory"
            )
        super().__init__(message)


def active_candidate_without_pointer(storage: Path) -> str | None:
    """The migration candidate a pointerless store still records as active.

    A store migrated from format 6 keeps that file as ``daem0nmcp.db`` and runs
    on a candidate only the pointer names.  With the pointer lost the retained
    format-6 file would be opened and written instead, so a candidate whose own
    run is still ``active`` is reported by its storage-relative name.  Links
    are skipped: no pointer can have named one.

    A published candidate that cannot be read (locked, damaged, I/O error) may
    be that active database, so when no readable one is active it raises
    ``CandidateUnreadableError`` instead of answering "none".  Only a published
    ``candidate.db`` counts: a run directory without one, a partial candidate,
    and a readable file that holds no run table were never named by a pointer.
    A migration root that exists but cannot be listed, or a run directory that
    cannot be looked into, may hide that database and is refused the same way.
    """

    root = storage / "migrations" / "v7"
    try:
        if (storage / "migrations").is_symlink() or root.is_symlink():
            return None
        run_dirs = sorted(root.iterdir(), key=lambda path: path.name)
    except (FileNotFoundError, NotADirectoryError):
        return None
    except OSError as exc:
        raise CandidateUnreadableError("migrations/v7", unlistable=True) from exc
    unreadable: str | None = None
    for run_dir in run_dirs:
        name = run_dir.name
        if not _valid_prefixed_hash(name, "mig"):
            continue
        candidate = run_dir / "candidate.db"
        relative = f"migrations/v7/{name}/candidate.db"
        try:
            if (
                run_dir.is_symlink()
                or candidate.is_symlink()
                or not candidate.is_file()
            ):
                continue
        except OSError:
            if unreadable is None:
                unreadable = relative
            continue
        try:
            # Read-only: inspecting a candidate must not checkpoint or
            # otherwise change a database this process does not own.
            connection = sqlite3.connect(
                sqlite_file_uri(candidate, mode="ro"), uri=True
            )
            try:
                row = None
                if connection.execute(
                    "SELECT 1 FROM sqlite_master "
                    "WHERE type='table' AND name='v7_migration_runs'"
                ).fetchone():
                    row = connection.execute(
                        "SELECT status FROM v7_migration_runs WHERE migration_run_id=?",
                        (name,),
                    ).fetchone()
            finally:
                connection.close()
        except (OSError, sqlite3.Error):
            if unreadable is None:
                unreadable = relative
            continue
        if row is not None and row[0] == "active":
            return relative
    if unreadable is not None:
        raise CandidateUnreadableError(unreadable)
    return None


def active_candidate_pointer_missing_detail(candidate: str) -> str:
    """Operator text for a store whose lost pointer named ``candidate``."""

    return (
        f"{POINTER_NAME} is missing and {candidate} is still recorded as the "
        f"active format-7 database, so {DEFAULT_DATABASE_NAME}, its retained "
        f"predecessor, is not opened. Restore {POINTER_NAME} from a backup"
    )


def _fsync_directory(path: Path) -> None:
    """Persist a directory entry where the platform exposes directory handles."""

    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _open_owned_file(
    path: Path,
    *,
    exclusive_create: bool,
) -> BinaryIO:
    """Open a regular file without following a link or accepting a swap.

    ``O_NOFOLLOW`` supplies the atomic guarantee where available.  The identity
    comparison is also required because Windows' CRT does not expose that flag.
    Exclusive creation is used for the pointer temp so a name this writer does
    not own is never truncated.
    """

    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise _invalid(f"unsafe activation file: {path.name}")
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_BINARY", 0)
    if exclusive_create:
        flags |= os.O_EXCL
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags, 0o600)
    except (FileExistsError, IsADirectoryError, PermissionError, OSError) as exc:
        raise _invalid(f"activation file cannot be opened safely: {path.name}") from exc
    try:
        opened = os.fstat(descriptor)
        named = os.stat(path, follow_symlinks=False)
        if not stat.S_ISREG(opened.st_mode) or not stat.S_ISREG(named.st_mode):
            raise _invalid(f"activation file must be regular: {path.name}")
        if not os.path.samestat(opened, named):
            raise _invalid(f"activation file changed while opening: {path.name}")
        return os.fdopen(descriptor, "r+b", buffering=0)
    except Exception:
        os.close(descriptor)
        raise


def _discard_crashed_pointer_temp(storage: Path, temporary: Path) -> None:
    """Remove the pointer temp a process left when it died before the rename.

    Only while this process holds the storage lock exclusively: no other
    writer can then be alive, so a regular file under the temp name belongs to
    nobody.  Under a shared lock, or none, it may be a live writer's and stays.
    A link or any other non-regular entry is never touched and is refused by
    the exclusive create that follows.

    The lock is looked up for the process, not for the caller: pointer writes
    are made by the one holder of the exclusive lock, never by a second thread
    of the same process beside it.
    """

    key = os.path.normcase(str((storage / LOCK_NAME).resolve(strict=False)))
    with _PROCESS_LOCK_GUARD:
        state = _PROCESS_LOCKS.get(key)
        if state is None or state.mode != "exclusive":
            return
    try:
        if stat.S_ISREG(os.lstat(temporary).st_mode):
            os.unlink(temporary)
    except OSError:
        return


def write_active_pointer(
    storage_path: str | os.PathLike[str],
    pointer: ActiveDatabasePointer,
    *,
    replace: Callable[
        [str | os.PathLike[str], str | os.PathLike[str]], None
    ] = os.replace,
) -> bytes:
    """Validate and atomically publish an activation pointer."""

    storage = Path(storage_path)
    try:
        reject_linked_ancestry(storage)
    except (OSError, PermissionError) as exc:
        raise _invalid("storage contains a link or reparse point") from exc
    if not storage.is_dir() or storage.is_symlink():
        raise _invalid("storage must be an existing non-symlink directory")
    pointer = validate_pointer(pointer)
    _resolve_storage_file(storage, pointer.active_db, "active_db")
    if pointer.previous_db is not None:
        _resolve_storage_file(storage, pointer.previous_db, "previous_db")
    raw = canonical_json_bytes(asdict(pointer))
    temporary = storage / POINTER_TEMP_NAME
    destination = storage / POINTER_NAME
    owned_temporary = False
    _discard_crashed_pointer_temp(storage, temporary)
    try:
        with _open_owned_file(temporary, exclusive_create=True) as handle:
            owned_temporary = True
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        replace(temporary, destination)
        _fsync_directory(storage)
    except Exception:
        try:
            if owned_temporary and temporary.exists() and not temporary.is_symlink():
                temporary.unlink()
        except OSError:
            pass
        raise
    return raw


@dataclass(slots=True)
class _ProcessLockState:
    mode: Literal["shared", "exclusive"]
    count: int
    handle: BinaryIO


_PROCESS_LOCK_GUARD = threading.RLock()
_PROCESS_LOCKS: dict[str, _ProcessLockState] = {}


class _WindowsOverlapped(ctypes.Structure):
    _fields_ = (
        ("Internal", ctypes.c_size_t),
        ("InternalHigh", ctypes.c_size_t),
        ("Offset", ctypes.c_ulong),
        ("OffsetHigh", ctypes.c_ulong),
        ("hEvent", ctypes.c_void_p),
    )


def _windows_file_lock(
    handle: BinaryIO,
    *,
    exclusive: bool,
    nonblocking: bool,
) -> None:
    import msvcrt
    from ctypes import wintypes

    flags = 0
    if nonblocking:
        flags |= 0x00000001  # LOCKFILE_FAIL_IMMEDIATELY
    if exclusive:
        flags |= 0x00000002  # LOCKFILE_EXCLUSIVE_LOCK
    overlapped = _WindowsOverlapped()
    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        lock_file = kernel32.LockFileEx
        lock_file.argtypes = (
            wintypes.HANDLE,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.DWORD,
            ctypes.POINTER(_WindowsOverlapped),
        )
        lock_file.restype = wintypes.BOOL
        locked = lock_file(
            wintypes.HANDLE(msvcrt.get_osfhandle(handle.fileno())),
            flags,
            0,
            1,
            0,
            ctypes.byref(overlapped),
        )
    except OSError as exc:
        raise DatabaseInUseError() from exc
    if not locked:
        raise DatabaseInUseError()


def _windows_file_unlock(handle: BinaryIO) -> None:
    import msvcrt
    from ctypes import wintypes

    overlapped = _WindowsOverlapped()
    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        unlock_file = kernel32.UnlockFileEx
        unlock_file.argtypes = (
            wintypes.HANDLE,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.DWORD,
            ctypes.POINTER(_WindowsOverlapped),
        )
        unlock_file.restype = wintypes.BOOL
        unlocked = unlock_file(
            wintypes.HANDLE(msvcrt.get_osfhandle(handle.fileno())),
            0,
            1,
            0,
            ctypes.byref(overlapped),
        )
    except OSError as exc:
        raise DatabaseInUseError() from exc
    if not unlocked:
        raise DatabaseInUseError()


def _lock_file_handle(handle: BinaryIO, *, exclusive: bool, nonblocking: bool) -> None:
    if os.name == "nt":
        _windows_file_lock(handle, exclusive=exclusive, nonblocking=nonblocking)
        return
    try:
        import fcntl
    except ImportError as exc:
        raise RuntimeError("UNSUPPORTED_FILE_LOCK") from exc
    operation = fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH
    if nonblocking:
        operation |= fcntl.LOCK_NB
    try:
        fcntl.flock(handle.fileno(), operation)
    except (BlockingIOError, OSError) as exc:
        raise DatabaseInUseError() from exc


def _convert_file_lock(handle: BinaryIO, *, exclusive: bool) -> None:
    """Change the mode of the lock this process holds through ``handle``.

    No platform converts a held lock both atomically and without giving it up
    when the conversion is refused, so a refused or interrupted conversion is
    followed by taking the shared lock again, waiting for it if a locker in
    another process got in between: the holders registered on the handle never
    go on believing in a lock the process no longer has.  Raises
    ``DatabaseInUseError``, holding the shared lock, when the exclusive lock
    is refused.
    """

    if os.name == "nt":
        # LockFileEx stacks a second lock on the handle rather than converting.
        _windows_file_unlock(handle)
    refused: DatabaseInUseError | None = None
    if exclusive:
        try:
            _lock_file_handle(handle, exclusive=True, nonblocking=True)
            return
        except DatabaseInUseError as exc:
            refused = exc
    try:
        _lock_file_handle(handle, exclusive=False, nonblocking=True)
    except DatabaseInUseError:
        _lock_file_handle(handle, exclusive=False, nonblocking=False)
    if refused is not None:
        raise refused


class DatabaseFileLock:
    """Lifetime shared or exclusive advisory lock for a storage generation.

    The operating-system lock is held once per process and counted here, so
    locks taken in one process exclude each other by this registry alone.
    ``over_process_holders`` is for the short exclusive lock under which a
    store is created: it is granted over shared holders in this same process,
    which keep their registration and get the shared lock back when it is
    released or demoted.  It still excludes every other process, and every
    further lock in this one, for as long as it is held.
    """

    def __init__(
        self,
        storage_path: str | os.PathLike[str],
        mode: Literal["shared", "exclusive"],
        *,
        nonblocking: bool = True,
        over_process_holders: bool = False,
    ) -> None:
        if mode not in {"shared", "exclusive"}:
            raise ValueError("lock mode must be shared or exclusive")
        if over_process_holders and mode != "exclusive":
            raise ValueError("only an exclusive lock is taken over process holders")
        self.path = Path(storage_path) / LOCK_NAME
        self.mode = mode
        self.nonblocking = nonblocking
        self.over_process_holders = over_process_holders
        self._key: str | None = None
        self._registered = False

    @property
    def acquired(self) -> bool:
        return self._registered

    def acquire(self) -> DatabaseFileLock:
        if self._registered:
            return self
        try:
            reject_linked_ancestry(self.path.parent)
        except (OSError, PermissionError) as exc:
            raise _invalid("lock storage directory is unsafe") from exc
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            reject_linked_ancestry(self.path.parent)
        except (OSError, PermissionError) as exc:
            raise _invalid("lock storage directory is unsafe") from exc
        if self.path.parent.is_symlink():
            raise _invalid("lock storage directory may not be a symlink")
        key = os.path.normcase(str(self.path.resolve(strict=False)))
        with _PROCESS_LOCK_GUARD:
            state = _PROCESS_LOCKS.get(key)
            if state is not None:
                if state.mode != "shared":
                    raise DatabaseInUseError()
                if self.mode != "shared":
                    if not self.over_process_holders:
                        raise DatabaseInUseError()
                    _convert_file_lock(state.handle, exclusive=True)
                    state.mode = "exclusive"
                state.count += 1
                self._key = key
                self._registered = True
                return self
            handle = _open_owned_file(self.path, exclusive_create=False)
            try:
                handle.seek(0, os.SEEK_END)
                if handle.tell() == 0:
                    handle.write(b"\0")
                    handle.flush()
                    os.fsync(handle.fileno())
                handle.seek(0)
                self._lock_handle(handle)
            except Exception:
                handle.close()
                raise
            _PROCESS_LOCKS[key] = _ProcessLockState(self.mode, 1, handle)
            self._key = key
            self._registered = True
            return self

    def _lock_handle(self, handle: BinaryIO) -> None:
        _lock_file_handle(
            handle,
            exclusive=self.mode == "exclusive",
            nonblocking=self.nonblocking,
        )

    def demote(self) -> None:
        """Turn the exclusive lock held through this object into a shared one.

        The process never stops holding the storage, so nothing registered on
        it is left without a lock; where the platform cannot convert in place
        this waits out a locker that got in between.
        """

        if not self._registered or self._key is None or self.mode != "exclusive":
            return
        with _PROCESS_LOCK_GUARD:
            state = _PROCESS_LOCKS.get(self._key)
            if state is not None and state.mode == "exclusive":
                _convert_file_lock(state.handle, exclusive=False)
                state.mode = "shared"
            self.mode = "shared"

    def release(self) -> None:
        if not self._registered or self._key is None:
            return
        with _PROCESS_LOCK_GUARD:
            state = _PROCESS_LOCKS.get(self._key)
            if state is None:
                self._registered = False
                self._key = None
                return
            if state.count > 1 and self.mode == "exclusive":
                # Taken over shared holders in this process: they stay.
                self.demote()
                self.mode = "exclusive"
            state.count -= 1
            if state.count == 0:
                handle = state.handle
                try:
                    if os.name == "nt":
                        _windows_file_unlock(handle)
                    else:
                        import fcntl

                        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                finally:
                    handle.close()
                    _PROCESS_LOCKS.pop(self._key, None)
            self._registered = False
            self._key = None

    def __enter__(self) -> DatabaseFileLock:
        return self.acquire()

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.release()


__all__ = [
    "ActiveDatabasePointer",
    "DatabaseFileLock",
    "DatabaseInUseError",
    "PointerValidationError",
    "ResolvedActiveDatabase",
    "has_canonical_v7_state",
    "resolve_active_database",
    "validate_pointer",
    "write_active_pointer",
]
