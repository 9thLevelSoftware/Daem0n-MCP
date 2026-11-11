"""Registered workspace and subordinate path security helpers."""

from __future__ import annotations

import hashlib
import json
import os
import posixpath
import re
import secrets
import sqlite3
import stat
from collections.abc import Iterable, Mapping, Sequence
from contextlib import closing, suppress
from dataclasses import dataclass, field
from pathlib import Path, PurePath, PureWindowsPath
from typing import Literal
from urllib.parse import quote

from .protected_files import ProtectedPathError, reject_linked_ancestry

_WINDOWS_DRIVE = re.compile(r"^[A-Za-z]:")


class WorkspaceAccessError(ValueError):
    """Raised when a caller selects a workspace that is not registered."""

    code = "UNAUTHORIZED_WORKSPACE"

    def __init__(self) -> None:
        super().__init__(f"{self.code}: workspace selector is not registered")


class WorkspacePathError(ValueError):
    """Raised when a path derived from a workspace root escapes that root."""

    code = "WORKSPACE_PATH_ESCAPE"

    def __init__(self) -> None:
        super().__init__(f"{self.code}: derived path leaves the registered workspace")


class IndexPathError(ValueError):
    """Raised when an indexing path or pattern crosses its workspace boundary."""

    code = "INVALID_INDEX_PATH"

    def __init__(self, message: str) -> None:
        super().__init__(f"{self.code}: {message}")


class WorkspaceIdentityError(ValueError):
    """Raised when a registered root cannot claim a workspace identity."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(f"{code}: workspace identity is not usable")


@dataclass(frozen=True, slots=True)
class Workspace:
    """A configured canonical workspace root and its stable opaque ID.

    ``identity_block`` names the non-retryable error code every call must
    return for this root; it is set when two registered roots claim one
    identity, or when the stored events do not match the recorded one.
    """

    workspace_id: str
    root: Path
    identity_block: str | None = None
    storage_unavailable: bool = False
    storage_busy: bool = False


def _canonical_root(root: str | os.PathLike[str]) -> Path:
    return Path(root).expanduser().resolve()


def _root_key(root: Path) -> str:
    return os.path.normcase(str(root))


def configured_roots(
    roots: Iterable[str | os.PathLike[str]] = (),
    *,
    default_root: str | os.PathLike[str] | None = None,
) -> list[Path]:
    """Canonicalize configured roots, keeping order and dropping repeats."""

    ordered = list(roots)
    if default_root is not None:
        ordered.insert(0, default_root)
    canonical: list[Path] = []
    seen: set[str] = set()
    for value in ordered:
        root = _canonical_root(value)
        key = _root_key(root)
        if key not in seen:
            seen.add(key)
            canonical.append(root)
    return canonical


def normalize_resolved_path(path: Path) -> Path:
    """Normalize equivalent resolved Windows spellings for identity checks.

    Callers must resolve and perform their normal link/reparse validation first;
    this helper changes only Windows' extended-length spelling returned
    intermittently by the runtime, not the selected filesystem object.
    """

    if os.name != "nt":
        return path
    value = str(path)
    if value.startswith("\\\\?\\UNC\\"):
        return Path("\\\\" + value[8:])
    if value.startswith("\\\\?\\"):
        return Path(value[4:])
    return path


def sqlite_file_uri(
    path: str | os.PathLike[str],
    *,
    mode: Literal["ro", "rw", "rwc"] | None = None,
) -> str:
    """Return the SQLite URI filename that opens exactly ``path``.

    SQLite reads ``?`` as the start of the query, ``#`` as a fragment and
    ``%HH`` as an escaped byte, so a path pasted into ``file:...`` unescaped
    can name a different file, or none.  Every byte outside the unreserved
    set is percent-encoded here.  The path is made absolute first, and the
    URI always carries an empty authority: ``file:///dir/x.db`` on POSIX,
    ``file:///C:/dir/x.db`` for a drive path and ``file:////server/share/x.db``
    for a UNC path, whose server SQLite would otherwise reject as an authority.
    """

    pure = path if isinstance(path, PurePath) else Path(path)
    if not pure.is_absolute():
        if not isinstance(pure, Path):
            raise ValueError("a path of another platform must be absolute")
        # POSIX resolves ``link/..`` through the link, so the path is joined
        # to the working directory as written; collapsing ``..`` first could
        # name another file.  Windows collapses it the same way itself.
        pure = Path(
            os.path.abspath(pure)
            if os.name == "nt"
            else os.path.join(os.getcwd(), pure)
        )
    text = pure.as_posix()
    if isinstance(pure, PureWindowsPath):
        # SQLite's Windows VFS accepts forward slashes only outside the
        # extended-length spelling, which names the same file without it.
        if text[:8].upper() == "//?/UNC/":
            text = "//" + text[8:]
        elif text.startswith("//?/"):
            text = text[4:]
    if not text.startswith("/"):
        text = "/" + text
    uri = "file://" + quote(text, safe="/:", errors="surrogateescape")
    return uri if mode is None else f"{uri}?mode={mode}"


def is_sqlite_busy(error: BaseException | None) -> bool:
    """Recognize SQLite contention, including Python 3.10 diagnostics."""

    if not isinstance(error, sqlite3.OperationalError):
        return False
    code = getattr(error, "sqlite_errorcode", None)
    if isinstance(code, int):
        return code & 0xFF in {5, 6}  # SQLITE_BUSY, SQLITE_LOCKED
    return str(error).casefold() in {
        "database is locked",
        "database table is locked",
        "database schema is locked",
    }


def _workspace_id(root: Path) -> str:
    digest = hashlib.sha256(_root_key(root).encode("utf-8")).hexdigest()[:24]
    return f"ws_{digest}"


# --- Persisted workspace identity (KD-3) -----------------------------------
#
# ``<root>/.daem0nmcp/workspace-id`` records the ID the workspace's rows were
# written under, so memories follow a project that is moved or renamed, and a
# re-clone works by copying ``.daem0nmcp/`` into the new clone.  Only ``id``,
# ``root_key`` and ``forked_from`` are read; no value read from the file is
# ever handed to the filesystem (R-40).  Copy and move are told apart only
# among the roots registered in this process, by device and inode, never by
# comparing path strings (R-28).

IDENTITY_DIRECTORY_NAME = ".daem0nmcp"
IDENTITY_FILE_NAME = "workspace-id"
WORKSPACE_IDENTITY_AMBIGUOUS = "WORKSPACE_IDENTITY_AMBIGUOUS"
WORKSPACE_IDENTITY_DUPLICATE = "WORKSPACE_IDENTITY_DUPLICATE"

_WORKSPACE_ID_VALUE = re.compile(r"^ws_[0-9a-f]{24}\Z")
_ROOT_KEY_VALUE = re.compile(r"^[0-9a-f]{64}\Z")
_MAX_IDENTITY_BYTES = 4096
_IDENTITY_CACHE_LIMIT = 256


@dataclass(frozen=True, slots=True)
class _RecordedIdentity:
    """The three values read out of an identity file; nothing else is kept."""

    workspace_id: str
    root_key: str | None
    forked_from: str | None


@dataclass(frozen=True, slots=True)
class WorkspaceIdentity:
    """What identity resolution decided for one registered root."""

    workspace_id: str
    blocked: str | None = None
    recorded_id: str | None = None
    recorded_root_key: str | None = None
    current_root_key: str = ""
    forked_from: str | None = None
    malformed: bool = False
    has_file: bool = False
    sole_holder: bool = True
    stored_ids: frozenset[str] = field(default_factory=frozenset)
    storage_unavailable: bool = False
    storage_busy: bool = False

    @property
    def root_key_matches(self) -> bool:
        """Whether the folder still is the one the identity was written for."""

        return (
            self.recorded_root_key is None
            or self.recorded_root_key == self.current_root_key
        )


def _identity_directory(root: Path) -> Path:
    return root / IDENTITY_DIRECTORY_NAME


def _identity_path(root: Path) -> Path:
    return _identity_directory(root) / IDENTITY_FILE_NAME


def _identity_root_key(root: Path) -> str:
    """Hash the registered root's own device and inode, never its spelling.

    Some network shares report ``st_ino == 0``; those fall back to the
    normcased resolved root so the value stays stable for that folder.
    """

    try:
        status = os.stat(root)
    except OSError:
        status = None
    if status is None or status.st_ino == 0:
        return hashlib.sha256(("path:" + _root_key(root)).encode("utf-8")).hexdigest()
    raw = f"{status.st_dev}:{status.st_ino}".encode()
    return hashlib.sha256(raw).hexdigest()


def _is_link_or_reparse(status: os.stat_result) -> bool:
    attributes = getattr(status, "st_file_attributes", 0)
    reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return stat.S_ISLNK(status.st_mode) or bool(attributes & reparse)


def _lstat(path: Path) -> os.stat_result | None:
    try:
        return os.lstat(path)
    except FileNotFoundError:
        return None


def _read_identity_bytes(path: Path) -> tuple[bytes | None, bool]:
    """Read the identity file without following a link. Cap at 4 KiB."""

    named = _lstat(path)
    if named is None:
        return None, False
    if _is_link_or_reparse(named) or not stat.S_ISREG(named.st_mode):
        return None, True
    if named.st_size > _MAX_IDENTITY_BYTES:
        return None, True
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except FileNotFoundError:
        return None, False
    except OSError:
        return None, True
    try:
        opened = os.fstat(descriptor)
        named_again = os.lstat(path)
        if (
            not stat.S_ISREG(opened.st_mode)
            or _is_link_or_reparse(named_again)
            or not os.path.samestat(opened, named_again)
        ):
            return None, True
        raw = os.read(descriptor, _MAX_IDENTITY_BYTES + 1)
    except OSError:
        return None, True
    finally:
        os.close(descriptor)
    if len(raw) > _MAX_IDENTITY_BYTES:
        return None, True
    return raw, False


def _read_identity_file(root: Path) -> tuple[_RecordedIdentity | None, bool]:
    """Return the recorded identity and whether the file is malformed."""

    raw, malformed = _read_identity_bytes(_identity_path(root))
    if malformed:
        return None, True
    if raw is None:
        return None, False
    try:
        parsed = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None, True
    if not isinstance(parsed, dict):
        return None, True
    identifier = parsed.get("id")
    if not isinstance(identifier, str) or _WORKSPACE_ID_VALUE.match(identifier) is None:
        return None, True
    recorded = parsed.get("root_key")
    if recorded is not None and (
        not isinstance(recorded, str) or _ROOT_KEY_VALUE.match(recorded) is None
    ):
        return None, True
    forked = parsed.get("forked_from")
    if forked is not None and (
        not isinstance(forked, str) or _WORKSPACE_ID_VALUE.match(forked) is None
    ):
        return None, True
    return _RecordedIdentity(identifier, recorded, forked), False


def _open_identity_reader(
    root: Path, *, live_storage: bool = False
) -> sqlite3.Connection | None:
    """Read the active database using live locking or offline no-sidecar mode."""

    storage = _identity_directory(root) / "storage"
    reject_linked_ancestry(storage)
    if not storage.is_dir():
        return None
    from .storage_activation import (
        DEFAULT_DATABASE_NAME,
        LOCK_NAME,
        POINTER_NAME,
        readonly_database_uri,
        resolve_active_database,
    )

    if (
        _lstat(storage / POINTER_NAME) is None
        and _lstat(storage / DEFAULT_DATABASE_NAME) is None
    ):
        # Bootstrap creates the directory and lock before publishing a DB.
        # Neither an empty directory nor its plain lock marker is a store.
        entries = list(storage.iterdir())
        if not entries or (
            len(entries) == 1
            and entries[0].name == LOCK_NAME
            and (status := _lstat(entries[0])) is not None
            and stat.S_ISREG(status.st_mode)
            and not _is_link_or_reparse(status)
        ):
            return None

    database = resolve_active_database(storage).path
    if not database.is_file():
        raise sqlite3.DatabaseError("active database is missing")
    # Offline inspection must leave no sidecars. Live readers must instead
    # participate in SQLite locking and keep every identity query in one
    # snapshot: immutable/no-lock readers are unsafe across checkpoints.
    uri = (
        sqlite_file_uri(database, mode="ro")
        if live_storage
        else readonly_database_uri(database)
    )
    connection = sqlite3.connect(uri, uri=True)
    try:
        connection.execute("PRAGMA query_only=ON")
        if live_storage:
            connection.execute("BEGIN")
    except Exception:
        connection.close()
        raise
    return connection


def _load_stored_workspace_ids(
    root: Path, *, live_storage: bool = False
) -> frozenset[str] | None:
    """IDs in the store, or ``None`` if a store exists but could not be read."""

    try:
        connection = _open_identity_reader(root, live_storage=live_storage)
        if connection is None:
            return frozenset()
        found: set[str] = set()
        with closing(connection):
            tables = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            for table in ("memory_events", "governance_events"):
                if table not in tables:
                    continue
                rows = connection.execute(
                    f"SELECT DISTINCT workspace_id FROM {table}"  # noqa: S608
                ).fetchall()
                found.update(value for (value,) in rows if isinstance(value, str))
        return frozenset(found)
    except ProtectedPathError:
        raise
    except Exception as error:
        if live_storage and is_sqlite_busy(error):
            raise
        return None


def stored_event_counts(root: str | os.PathLike[str]) -> dict[str, int]:
    """Count the events stored under each workspace ID, for operator output."""

    try:
        connection = _open_identity_reader(_canonical_root(root))
    except Exception:
        return {}
    if connection is None:
        return {}
    counts: dict[str, int] = {}
    with closing(connection):
        for table in ("memory_events", "governance_events"):
            try:
                rows = connection.execute(
                    f"SELECT workspace_id, COUNT(*) FROM {table} "  # noqa: S608
                    "GROUP BY workspace_id"
                ).fetchall()
            except sqlite3.Error:
                continue
            for identifier, total in rows:
                if isinstance(identifier, str):
                    counts[identifier] = counts.get(identifier, 0) + int(total)
    return counts


def _same_folder(left: Path, right: Path) -> bool:
    try:
        return os.path.samefile(left, right)
    except OSError:
        return False


def _claim_identity(root: Path, *, live_storage: bool = False) -> WorkspaceIdentity:
    """Decide the ID one root claims, ignoring every other registered root."""

    current = _identity_root_key(root)
    path_hash = _workspace_id(root)
    recorded, malformed = _read_identity_file(root)
    storage_busy = False
    try:
        stored = _load_stored_workspace_ids(root, live_storage=live_storage)
    except sqlite3.OperationalError as error:
        stored = None
        storage_busy = is_sqlite_busy(error)
    except ProtectedPathError:
        return WorkspaceIdentity(
            workspace_id=path_hash,
            blocked=WORKSPACE_IDENTITY_AMBIGUOUS,
            recorded_id=None if recorded is None else recorded.workspace_id,
            recorded_root_key=None if recorded is None else recorded.root_key,
            current_root_key=current,
            forked_from=None if recorded is None else recorded.forked_from,
            malformed=malformed,
            has_file=recorded is not None or malformed,
        )
    # Storage owns unreadable databases and invalid pointers, including hot
    # journal recovery. Unknown stored IDs must never seed or rebind an ID.
    storage_unavailable = stored is None
    if stored is None:
        stored = frozenset()
    if malformed:
        # ``workspace-id`` still reports the stored counts, and ``--new`` can
        # still fork from them, so the operator is not left with nothing.
        return WorkspaceIdentity(
            workspace_id=path_hash,
            blocked=WORKSPACE_IDENTITY_AMBIGUOUS,
            current_root_key=current,
            malformed=True,
            has_file=True,
            stored_ids=stored,
            storage_unavailable=storage_unavailable,
            storage_busy=storage_busy,
        )
    if recorded is None:
        if not stored:
            claimed: str | None = path_hash
        elif len(stored) == 1:
            claimed = next(iter(stored))
        else:
            claimed = None
        return WorkspaceIdentity(
            workspace_id=claimed or path_hash,
            blocked=None if claimed else WORKSPACE_IDENTITY_AMBIGUOUS,
            current_root_key=current,
            stored_ids=stored,
            storage_unavailable=storage_unavailable,
            storage_busy=storage_busy,
        )
    # A fork only hides ``forked_from``; adopt (no fork) hides every other ID.
    allowed = {recorded.workspace_id}
    if recorded.forked_from is not None:
        allowed.add(recorded.forked_from)
        settled = not stored or stored <= allowed
    else:
        settled = not stored or recorded.workspace_id in stored
    blocked = None if settled else WORKSPACE_IDENTITY_AMBIGUOUS
    return WorkspaceIdentity(
        workspace_id=path_hash if blocked else recorded.workspace_id,
        blocked=blocked,
        recorded_id=recorded.workspace_id,
        recorded_root_key=recorded.root_key,
        current_root_key=current,
        forked_from=recorded.forked_from,
        has_file=True,
        stored_ids=stored,
        storage_unavailable=storage_unavailable,
        storage_busy=storage_busy,
    )


def _block(identity: WorkspaceIdentity, root: Path, code: str) -> WorkspaceIdentity:
    return WorkspaceIdentity(
        workspace_id=_workspace_id(root),
        blocked=code,
        recorded_id=identity.recorded_id,
        recorded_root_key=identity.recorded_root_key,
        current_root_key=identity.current_root_key,
        forked_from=identity.forked_from,
        malformed=identity.malformed,
        has_file=identity.has_file,
        sole_holder=False,
        stored_ids=identity.stored_ids,
        storage_unavailable=identity.storage_unavailable,
        storage_busy=identity.storage_busy,
    )


def _resolve_identities(
    canonical: Sequence[Path], *, live_storage: bool = False
) -> dict[str, WorkspaceIdentity]:
    """Resolve every registered root together; the order never matters."""

    identities = {
        _root_key(root): _claim_identity(root, live_storage=live_storage)
        for root in canonical
    }
    by_root = {_root_key(root): root for root in canonical}
    holders: dict[str, list[str]] = {}
    for key, identity in identities.items():
        if identity.blocked is None:
            holders.setdefault(identity.workspace_id, []).append(key)
    for keys in holders.values():
        # Case variants, subst/mapped drives, junctions and bind mounts are one
        # folder, never a copy (R-28), so collapse them before deciding.
        clusters: list[list[str]] = []
        for key in keys:
            for cluster in clusters:
                if _same_folder(by_root[key], by_root[cluster[0]]):
                    cluster.append(key)
                    break
            else:
                clusters.append([key])
        if len(clusters) == 1:
            continue
        matching = [
            cluster
            for cluster in clusters
            if any(
                identities[key].recorded_root_key is not None
                and identities[key].root_key_matches
                for key in cluster
            )
        ]
        # Never first-wins: with no match, or more than one, every root blocks.
        keep = matching[0] if len(matching) == 1 else []
        for cluster in clusters:
            if cluster is keep:
                continue
            for key in cluster:
                identities[key] = _block(
                    identities[key], by_root[key], WORKSPACE_IDENTITY_DUPLICATE
                )
    for key, identity in identities.items():
        if identity.blocked is None and len(holders[identity.workspace_id]) > 1:
            identities[key] = WorkspaceIdentity(
                workspace_id=identity.workspace_id,
                recorded_id=identity.recorded_id,
                recorded_root_key=identity.recorded_root_key,
                current_root_key=identity.current_root_key,
                forked_from=identity.forked_from,
                has_file=identity.has_file,
                sole_holder=all(
                    _same_folder(by_root[key], by_root[other])
                    for other in holders[identity.workspace_id]
                ),
                stored_ids=identity.stored_ids,
                storage_unavailable=identity.storage_unavailable,
                storage_busy=identity.storage_busy,
            )
    return identities


def _file_stamp(path: Path) -> tuple[int, int, int] | None:
    status = _lstat(path)
    if status is None:
        return None
    return (status.st_mtime_ns, status.st_size, status.st_ino)


def _identity_stamp(
    root: Path,
) -> tuple[tuple[int, int, int] | None, ...]:
    storage = _identity_directory(root) / "storage"
    return (
        _file_stamp(_identity_path(root)),
        _file_stamp(storage / "active-db.json"),
        _file_stamp(storage / "daem0nmcp.db"),
    )


_IDENTITY_CACHE: dict[
    tuple[str, ...],
    tuple[
        tuple[tuple[tuple[int, int, int] | None, ...], ...],
        dict[str, WorkspaceIdentity],
    ],
] = {}


def reset_workspace_identity_cache() -> None:
    """Drop the per-process identity cache after any identity is written."""

    _IDENTITY_CACHE.clear()


def resolve_workspace_identities(
    canonical: Sequence[Path],
    *,
    live_storage: bool = False,
) -> dict[str, WorkspaceIdentity]:
    """Resolve, and cache per process, the identities of registered roots.

    The cache is keyed by the registered group and revalidated against each
    identity file's stat, so a fork or adoption written anywhere is seen.
    """

    ordered = sorted(canonical, key=_root_key)
    key = (
        "live" if live_storage else "offline",
        *(_root_key(root) for root in ordered),
    )
    stamps = tuple(_identity_stamp(root) for root in ordered)
    cached = _IDENTITY_CACHE.get(key)
    if cached is not None and cached[0] == stamps:
        return cached[1]
    identities = _resolve_identities(canonical, live_storage=live_storage)
    # A transient database lock need not change any file stamp when it clears.
    # Recheck unknown identities instead of caching a provisional path hash.
    if any(identity.storage_unavailable for identity in identities.values()):
        _IDENTITY_CACHE.pop(key, None)
        return identities
    if len(_IDENTITY_CACHE) >= _IDENTITY_CACHE_LIMIT:
        _IDENTITY_CACHE.clear()
    _IDENTITY_CACHE[key] = (stamps, identities)
    return identities


def path_hash_workspace_id(root: str | os.PathLike[str]) -> str:
    """Return the deterministic ID a brand-new root at this path is given."""

    return _workspace_id(_canonical_root(root))


def workspace_identity(
    root: str | os.PathLike[str],
    *,
    other_roots: Iterable[str | os.PathLike[str]] = (),
    live_storage: bool = False,
) -> WorkspaceIdentity:
    """Resolve one root's identity; offline reads leave no SQLite sidecars."""

    canonical = configured_roots(other_roots, default_root=root)
    return resolve_workspace_identities(canonical, live_storage=live_storage)[
        _root_key(canonical[0])
    ]


def _publish_identity(temporary: Path, destination: Path, *, replace: bool) -> None:
    """Move a regular temp file into place. Seed never clobbers; rewrite may."""

    temp_status = _lstat(temporary)
    dest_status = _lstat(destination)
    if temp_status is None or _is_link_or_reparse(temp_status):
        raise OSError("identity temp is missing or linked")
    if dest_status is not None and _is_link_or_reparse(dest_status):
        raise OSError("identity file is a link")
    if replace:
        os.replace(temporary, destination)
        return
    try:
        os.link(temporary, destination, follow_symlinks=False)
    except FileExistsError:
        raise
    except (NotImplementedError, OSError):
        # Windows rename refuses an existing dest; POSIX link is the no-clobber
        # path, so any other POSIX failure must not fall through to overwrite.
        if os.name != "nt":
            raise
        os.rename(temporary, destination)


def _write_identity_file(
    root: Path,
    workspace_id: str,
    forked_from: str | None,
    *,
    replace: bool,
) -> bool:
    """Publish one identity by temp file. Seed is create-if-absent."""

    payload: dict[str, str] = {
        "id": workspace_id,
        "root_key": _identity_root_key(root),
    }
    if forked_from is not None:
        payload["forked_from"] = forked_from
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    directory = _identity_directory(root)
    destination = _identity_path(root)
    temporary = directory / f".{IDENTITY_FILE_NAME}-{secrets.token_hex(8)}.tmp"
    written = False
    try:
        directory.mkdir(parents=True, exist_ok=True)
        dest_status = _lstat(destination)
        if dest_status is not None and _is_link_or_reparse(dest_status):
            return False
        if not replace and dest_status is not None:
            recorded, _malformed = _read_identity_file(root)
            return recorded is not None and recorded.workspace_id == workspace_id
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        flags |= getattr(os, "O_BINARY", 0) | getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(temporary, flags, 0o600)
        try:
            opened = os.fstat(descriptor)
            named = os.lstat(temporary)
            if not stat.S_ISREG(opened.st_mode) or not os.path.samestat(opened, named):
                raise OSError("identity temp is not a regular file")
            os.write(descriptor, raw + b"\n")
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        _publish_identity(temporary, destination, replace=replace)
        written = True
    except OSError:
        recorded, _malformed = _read_identity_file(root)
        written = recorded is not None and recorded.workspace_id == workspace_id
    finally:
        with suppress(OSError):
            leftover = _lstat(temporary)
            if leftover is not None and not _is_link_or_reparse(leftover):
                temporary.unlink()
        reset_workspace_identity_cache()
    return written


def _ensure_storage_gitignore(root: Path) -> None:
    """Keep the identity file and the database out of the project's commits."""

    directory = _identity_directory(root)
    path = directory / ".gitignore"
    if path.exists():
        return
    try:
        directory.mkdir(parents=True, exist_ok=True)
        with open(path, "x", encoding="utf-8") as handle:
            handle.write("*\n")
    except OSError:
        return


def write_workspace_identity(
    root: str | os.PathLike[str],
    *,
    workspace_id: str,
    forked_from: str | None = None,
) -> None:
    """Record an explicit identity for one root (``--adopt`` and ``--new``)."""

    if _WORKSPACE_ID_VALUE.match(workspace_id) is None:
        raise WorkspaceIdentityError("INVALID_WORKSPACE_ID")
    canonical = _canonical_root(root)
    if not _write_identity_file(canonical, workspace_id, forked_from, replace=True):
        raise WorkspaceIdentityError("WORKSPACE_IDENTITY_NOT_WRITABLE")
    _ensure_storage_gitignore(canonical)


def ensure_workspace_identity(
    roots: Iterable[str | os.PathLike[str]],
    *,
    live_storage: bool = False,
) -> dict[str, WorkspaceIdentity]:
    """Persist the identity of every configured root before it is served.

    A root with no file is seeded with the ID its events already carry, or
    with today's path hash when it has none: a deterministic value two
    concurrent starts agree on, and one that rewrites no row (R-27).
    """

    canonical = configured_roots(roots)
    identities = _resolve_identities(canonical, live_storage=live_storage)
    persist_failed = False
    for root in canonical:
        key = _root_key(root)
        identity = identities[key]
        if identity.blocked is not None or identity.storage_unavailable:
            continue
        if not identity.has_file:
            landed = _write_identity_file(
                root, identity.workspace_id, identity.forked_from, replace=False
            )
            if not landed and _lstat(_identity_path(root)) is None:
                identities[key] = _block(identity, root, WORKSPACE_IDENTITY_AMBIGUOUS)
                persist_failed = True
                continue
        elif identity.sole_holder and not identity.root_key_matches:
            # Same folder, different spelling keeps device+inode, so it never
            # reaches this branch.  Only the ID's single holder rewrites.
            _write_identity_file(
                root, identity.workspace_id, identity.forked_from, replace=True
            )
        _ensure_storage_gitignore(root)
    if persist_failed:
        ordered = sorted(canonical, key=_root_key)
        cache_key = (
            "live" if live_storage else "offline",
            *(_root_key(item) for item in ordered),
        )
        stamps = tuple(_identity_stamp(item) for item in ordered)
        if len(_IDENTITY_CACHE) >= _IDENTITY_CACHE_LIMIT:
            _IDENTITY_CACHE.clear()
        _IDENTITY_CACHE[cache_key] = (stamps, identities)
        return identities
    return resolve_workspace_identities(canonical, live_storage=live_storage)


def _parse_workspace_roots(raw: str | None) -> list[str]:
    if raw is None or not raw.strip():
        return []

    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(
            "DAEM0NMCP_WORKSPACE_ROOTS must be a JSON array of paths"
        ) from exc

    if not isinstance(parsed, list) or not all(
        isinstance(item, str) and item.strip() for item in parsed
    ):
        raise ValueError("DAEM0NMCP_WORKSPACE_ROOTS must be a JSON array of paths")
    return parsed


class WorkspaceRegistry:
    """Resolve caller selectors only against explicitly configured roots."""

    def __init__(
        self,
        roots: Iterable[str | os.PathLike[str]] = (),
        *,
        default_root: str | os.PathLike[str] | None = None,
        live_storage: bool = False,
    ) -> None:
        canonical_roots = configured_roots(roots, default_root=default_root)
        identities = resolve_workspace_identities(
            canonical_roots, live_storage=live_storage
        )

        self._by_id: dict[str, Workspace] = {}
        self._by_root: dict[str, Workspace] = {}
        self._default_id: str | None = None

        for index, canonical in enumerate(canonical_roots):
            key = _root_key(canonical)
            identity = identities[key]
            workspace = Workspace(
                identity.workspace_id,
                canonical,
                identity.blocked,
                identity.storage_unavailable,
                identity.storage_busy,
            )
            self._by_root[key] = workspace
            self._by_id.setdefault(workspace.workspace_id, workspace)
            if index == 0:
                self._default_id = workspace.workspace_id

    @classmethod
    def from_environment(
        cls, environ: Mapping[str, str] | None = None, *, live_storage: bool = False
    ) -> WorkspaceRegistry:
        env = os.environ if environ is None else environ
        project_root = env.get("DAEM0NMCP_PROJECT_ROOT")
        extra_roots = _parse_workspace_roots(env.get("DAEM0NMCP_WORKSPACE_ROOTS"))
        return cls(extra_roots, default_root=project_root, live_storage=live_storage)

    @classmethod
    def from_config(
        cls,
        *,
        project_root: str | None,
        workspace_roots: Sequence[str] = (),
        live_storage: bool = False,
    ) -> WorkspaceRegistry:
        return cls(
            workspace_roots, default_root=project_root, live_storage=live_storage
        )

    @classmethod
    def from_settings(
        cls, loaded_settings: object, *, live_storage: bool = False
    ) -> WorkspaceRegistry:
        """Build a registry from Pydantic-loaded settings, including `.env`."""
        project_root = getattr(loaded_settings, "project_root", ".")
        workspace_roots = getattr(loaded_settings, "workspace_roots", ())
        return cls.from_config(
            project_root=project_root,
            workspace_roots=workspace_roots,
            live_storage=live_storage,
        )

    @property
    def default(self) -> Workspace:
        if self._default_id is None:
            raise WorkspaceAccessError()
        return self._by_id[self._default_id]

    @property
    def default_selector(self) -> str | None:
        return self._default_id

    def resolve(self, selector: str | os.PathLike[str] | None) -> Workspace:
        if selector is None or not str(selector):
            return self.default

        selector_text = str(selector)
        if selector_text.startswith("ws_"):
            workspace = self._by_id.get(selector_text)
            if workspace is None:
                raise WorkspaceAccessError()
            return workspace

        try:
            canonical = _canonical_root(selector_text)
        except (OSError, RuntimeError, ValueError) as exc:
            raise WorkspaceAccessError() from exc

        workspace = self._by_root.get(_root_key(canonical))
        if workspace is None:
            raise WorkspaceAccessError()
        return workspace


def is_workspace_relative_path(value: object) -> bool:
    """Return whether a string is a normalized workspace-relative POSIX path."""

    if not isinstance(value, str) or not 1 <= len(value) <= 1024:
        return False
    if value == ".":
        return True
    if (
        "\\" in value
        or "\x00" in value
        or value.startswith(("/", "~"))
        or _WINDOWS_DRIVE.match(value) is not None
    ):
        return False
    if any(component in {"", ".", ".."} for component in value.split("/")):
        return False
    return posixpath.normpath(value) == value


def relative_to_root(
    path: str | os.PathLike[str],
    root: str | os.PathLike[str],
) -> str | None:
    """Return ``path`` inside ``root`` as a POSIX relative path, else ``None``.

    Containment is decided with ``Path.relative_to`` on resolved, normalized
    paths, never with a string prefix, so ``C:\\proj2\\a.py`` is not treated as
    living under ``C:\\proj``.  The returned string always satisfies the wire's
    relative-path rule.
    """

    try:
        resolved_root = normalize_resolved_path(Path(root).expanduser().resolve())
        resolved = normalize_resolved_path(Path(path).expanduser().resolve())
        depth = len(
            Path(os.path.normcase(str(resolved)))
            .relative_to(os.path.normcase(str(resolved_root)))
            .parts
        )
    except (OSError, RuntimeError, ValueError):
        return None
    if depth < 1:
        return None
    # Join the resolved path's own tail so the result keeps the filesystem's
    # spelling rather than the case-folded form containment was decided on.
    value = "/".join(resolved.parts[-depth:])
    return value if is_workspace_relative_path(value) else None


def _has_parent_component(value: str) -> bool:
    normalized = value.replace("\\", "/")
    return ".." in normalized.split("/")


def _is_absolute(value: str) -> bool:
    path = Path(value)
    return path.is_absolute() or bool(path.drive) or value.startswith(("/", "\\"))


def resolve_derived_path(
    workspace_root: str | os.PathLike[str],
    *relative_parts: str | os.PathLike[str],
) -> Path:
    """Resolve a workspace-derived path and fail closed on links or traversal."""
    try:
        registered_root = Path(workspace_root)
        current_root = normalize_resolved_path(registered_root.resolve(strict=True))
        comparable_registered = normalize_resolved_path(registered_root)
        if _root_key(current_root) != _root_key(comparable_registered):
            raise WorkspacePathError()

        candidate = registered_root.joinpath(*relative_parts)
        resolved = normalize_resolved_path(candidate.resolve(strict=False))
        resolved.relative_to(current_root)
    except WorkspacePathError:
        raise
    except (OSError, RuntimeError, ValueError) as exc:
        raise WorkspacePathError() from exc
    return resolved


def resolve_index_target(workspace: Workspace, target_path: str | None) -> Path:
    """Resolve a relative indexing root within a selected workspace."""
    if not target_path:
        return workspace.root
    if _is_absolute(target_path) or _has_parent_component(target_path):
        raise IndexPathError("index roots must be relative workspace paths")

    try:
        root = normalize_resolved_path(workspace.root.resolve(strict=True))
        resolved = normalize_resolved_path((root / target_path).resolve())
        resolved.relative_to(root)
    except (OSError, RuntimeError, ValueError) as exc:
        raise IndexPathError("index root must remain inside the workspace") from exc
    return resolved


def validate_index_patterns(patterns: Sequence[str]) -> list[str]:
    """Reject absolute glob patterns and all parent-directory components."""
    validated: list[str] = []
    for pattern in patterns:
        if not pattern or _is_absolute(pattern) or _has_parent_component(pattern):
            raise IndexPathError(
                "glob patterns must be relative and cannot contain parent components"
            )
        validated.append(pattern)
    return validated


def resolve_index_file(project_root: Path, file_path: Path) -> Path:
    """Resolve a matched file immediately before reading it."""
    try:
        root = normalize_resolved_path(project_root.resolve(strict=True))
        resolved = normalize_resolved_path(file_path.resolve(strict=True))
        resolved.relative_to(root)
    except (OSError, RuntimeError, ValueError) as exc:
        raise IndexPathError("matched file resolves outside the workspace") from exc
    return resolved
