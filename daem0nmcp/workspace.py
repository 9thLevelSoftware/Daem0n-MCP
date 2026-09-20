"""Registered workspace and subordinate path security helpers."""

from __future__ import annotations

import hashlib
import json
import os
import posixpath
import re
import secrets
import sqlite3
from collections.abc import Iterable, Mapping, Sequence
from contextlib import closing, suppress
from dataclasses import dataclass, field
from pathlib import Path

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


def _read_identity_file(root: Path) -> tuple[_RecordedIdentity | None, bool]:
    """Return the recorded identity and whether the file is malformed."""

    try:
        raw = _identity_path(root).read_bytes()
    except (FileNotFoundError, NotADirectoryError, IsADirectoryError):
        return None, False
    except OSError:
        return None, True
    if len(raw) > _MAX_IDENTITY_BYTES:
        return None, True
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


def _open_identity_reader(root: Path) -> sqlite3.Connection | None:
    """Open the root's own active database read-only, creating nothing."""

    storage = _identity_directory(root) / "storage"
    if not storage.is_dir():
        return None
    from .storage_activation import resolve_active_database

    try:
        database = resolve_active_database(storage).path
    except Exception:
        return None
    if not database.is_file():
        return None
    try:
        return sqlite3.connect(
            f"file:{database.as_posix()}?mode=ro",
            uri=True,
            timeout=2,
        )
    except sqlite3.Error:
        return None


def _stored_workspace_ids(root: Path) -> frozenset[str]:
    """Distinct workspace IDs the root's own event tables already carry."""

    connection = _open_identity_reader(root)
    if connection is None:
        return frozenset()
    found: set[str] = set()
    with closing(connection):
        for table in ("memory_events", "governance_events"):
            try:
                rows = connection.execute(
                    f"SELECT DISTINCT workspace_id FROM {table}"  # noqa: S608
                ).fetchall()
            except sqlite3.Error:
                continue
            found.update(value for (value,) in rows if isinstance(value, str))
    return frozenset(found)


def stored_event_counts(root: str | os.PathLike[str]) -> dict[str, int]:
    """Count the events stored under each workspace ID, for operator output."""

    connection = _open_identity_reader(_canonical_root(root))
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


def _claim_identity(root: Path) -> WorkspaceIdentity:
    """Decide the ID one root claims, ignoring every other registered root."""

    current = _identity_root_key(root)
    path_hash = _workspace_id(root)
    recorded, malformed = _read_identity_file(root)
    stored = _stored_workspace_ids(root)
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
        )
    # The recorded ID must be one the stored events already carry, unless the
    # only events present are the ones an explicit ``workspace-id --new`` fork
    # hid under ``forked_from`` (R-41).  Any other ID blocks.
    allowed = {recorded.workspace_id}
    if recorded.forked_from is not None:
        allowed.add(recorded.forked_from)
    settled = not stored or recorded.workspace_id in stored or stored <= allowed
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
    )


def _resolve_identities(canonical: Sequence[Path]) -> dict[str, WorkspaceIdentity]:
    """Resolve every registered root together; the order never matters."""

    identities = {_root_key(root): _claim_identity(root) for root in canonical}
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
            )
    return identities


def _identity_stamp(root: Path) -> tuple[int, int, int] | None:
    try:
        status = os.stat(_identity_path(root))
    except OSError:
        return None
    return (status.st_mtime_ns, status.st_size, status.st_ino)


_IDENTITY_CACHE: dict[
    tuple[str, ...],
    tuple[tuple[tuple[int, int, int] | None, ...], dict[str, WorkspaceIdentity]],
] = {}


def reset_workspace_identity_cache() -> None:
    """Drop the per-process identity cache after any identity is written."""

    _IDENTITY_CACHE.clear()


def resolve_workspace_identities(
    canonical: Sequence[Path],
) -> dict[str, WorkspaceIdentity]:
    """Resolve, and cache per process, the identities of registered roots.

    The cache is keyed by the registered group and revalidated against each
    identity file's stat, so a fork or adoption written anywhere is seen.
    """

    ordered = sorted(canonical, key=_root_key)
    key = tuple(_root_key(root) for root in ordered)
    stamps = tuple(_identity_stamp(root) for root in ordered)
    cached = _IDENTITY_CACHE.get(key)
    if cached is not None and cached[0] == stamps:
        return cached[1]
    identities = _resolve_identities(canonical)
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
) -> WorkspaceIdentity:
    """Resolve one root's identity, read-only, writing nothing."""

    canonical = configured_roots(other_roots, default_root=root)
    return resolve_workspace_identities(canonical)[_root_key(canonical[0])]


def _write_identity_file(
    root: Path,
    workspace_id: str,
    forked_from: str | None,
) -> None:
    directory = _identity_directory(root)
    directory.mkdir(parents=True, exist_ok=True)
    payload: dict[str, str] = {
        "id": workspace_id,
        "root_key": _identity_root_key(root),
    }
    if forked_from is not None:
        payload["forked_from"] = forked_from
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    temporary = directory / f".{IDENTITY_FILE_NAME}-{secrets.token_hex(8)}.tmp"
    try:
        temporary.write_bytes(raw + b"\n")
        os.replace(temporary, _identity_path(root))
    finally:
        with suppress(OSError):
            temporary.unlink()
    reset_workspace_identity_cache()


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
    _write_identity_file(canonical, workspace_id, forked_from)
    _ensure_storage_gitignore(canonical)


def ensure_workspace_identity(
    roots: Iterable[str | os.PathLike[str]],
) -> dict[str, WorkspaceIdentity]:
    """Persist the identity of every configured root before it is served.

    A root with no file is seeded with the ID its events already carry, or
    with today's path hash when it has none: a deterministic value two
    concurrent starts agree on, and one that rewrites no row (R-27).
    """

    canonical = configured_roots(roots)
    identities = _resolve_identities(canonical)
    for root in canonical:
        identity = identities[_root_key(root)]
        if identity.blocked is not None:
            continue
        if not identity.has_file:
            _write_identity_file(root, identity.workspace_id, identity.forked_from)
        elif identity.sole_holder and not identity.root_key_matches:
            # The folder was moved, renamed, or ``.daem0nmcp/`` was copied into
            # a new clone.  A different spelling of the same folder keeps the
            # same device and inode, so it never reaches this branch (R-28).
            _write_identity_file(root, identity.workspace_id, identity.forked_from)
        _ensure_storage_gitignore(root)
    reset_workspace_identity_cache()
    return _resolve_identities(canonical)


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
    ) -> None:
        canonical_roots = configured_roots(roots, default_root=default_root)
        identities = resolve_workspace_identities(canonical_roots)

        self._by_id: dict[str, Workspace] = {}
        self._by_root: dict[str, Workspace] = {}
        self._default_id: str | None = None

        for index, canonical in enumerate(canonical_roots):
            key = _root_key(canonical)
            identity = identities[key]
            workspace = Workspace(identity.workspace_id, canonical, identity.blocked)
            self._by_root[key] = workspace
            self._by_id.setdefault(workspace.workspace_id, workspace)
            if index == 0:
                self._default_id = workspace.workspace_id

    @classmethod
    def from_environment(
        cls, environ: Mapping[str, str] | None = None
    ) -> WorkspaceRegistry:
        env = os.environ if environ is None else environ
        project_root = env.get("DAEM0NMCP_PROJECT_ROOT")
        extra_roots = _parse_workspace_roots(env.get("DAEM0NMCP_WORKSPACE_ROOTS"))
        return cls(extra_roots, default_root=project_root)

    @classmethod
    def from_config(
        cls,
        *,
        project_root: str | None,
        workspace_roots: Sequence[str] = (),
    ) -> WorkspaceRegistry:
        return cls(workspace_roots, default_root=project_root)

    @classmethod
    def from_settings(cls, loaded_settings: object) -> WorkspaceRegistry:
        """Build a registry from Pydantic-loaded settings, including `.env`."""
        project_root = getattr(loaded_settings, "project_root", ".")
        workspace_roots = getattr(loaded_settings, "workspace_roots", ())
        return cls.from_config(
            project_root=project_root,
            workspace_roots=workspace_roots,
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
