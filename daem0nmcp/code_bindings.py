"""Bounded repository fingerprints for server-managed memory code bindings."""

from __future__ import annotations

import hashlib
import re
from collections import OrderedDict
from collections.abc import Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from pathlib import Path, PureWindowsPath
from threading import Lock
from typing import Literal, Protocol, runtime_checkable

from .workspace import IndexPathError, Workspace, resolve_index_file

_MAX_BINDING_FILE_BYTES = 5 * 1024 * 1024
_MAX_BINDING_BATCH_BYTES = 32 * 1024 * 1024
_MAX_CACHED_SOURCE_BYTES = 32 * 1024 * 1024
_FINGERPRINT = re.compile(r"[0-9a-f]{64}\Z")
_HEAD = re.compile(r"[0-9a-f]{40,64}\Z")


class CodeBindingError(ValueError):
    """A binding cannot be captured or its stored representation is invalid."""


class _UnverifiableBindingError(CodeBindingError):
    pass


class CodeBindingUnavailableError(_UnverifiableBindingError):
    """The server cannot currently verify a symbol binding."""


class CodeBindingReferenceError(_UnverifiableBindingError):
    """The requested symbol reference is unsupported."""


@runtime_checkable
class _BindingCodeIndexer(Protocol):
    @property
    def available(self) -> bool: ...

    def get_supported_extensions(self) -> Iterable[str]: ...

    def index_source_strict(
        self, path: Path, root: Path, source: bytes
    ) -> Iterable[Mapping[str, object]]: ...


@dataclass(frozen=True, slots=True)
class CodeBinding:
    relative_file_path: str
    qualified_name: str | None
    fingerprint: str
    head_commit: str | None

    def to_json(self) -> dict[str, str | None]:
        return {
            "relative_file_path": self.relative_file_path,
            "qualified_name": self.qualified_name,
            "fingerprint": self.fingerprint,
            "head_commit": self.head_commit,
        }


@dataclass(frozen=True, slots=True)
class BindingEvaluation:
    applicability: Literal["current", "needs_revalidation", "unverifiable"]
    changed: tuple[str, ...]


def _validate_ref(relative: object, name: object) -> tuple[str, str | None]:
    if (
        not isinstance(relative, str)
        or not relative
        or "\x00" in relative
        or Path(relative).is_absolute()
        or PureWindowsPath(relative).drive
        or PureWindowsPath(relative).root
        or ".." in relative.replace("\\", "/").split("/")
        or (name is not None and (not isinstance(name, str) or not name.strip()))
    ):
        raise CodeBindingError("code binding reference is invalid")
    return relative, name


def _bindings_from_context(context: Mapping[str, object]) -> tuple[CodeBinding, ...]:
    if not isinstance(context, Mapping):
        raise CodeBindingError("code bindings context is invalid")
    if "code_bindings" not in context:
        return ()
    raw = context["code_bindings"]
    if not isinstance(raw, list) or not 1 <= len(raw) <= 16:
        raise CodeBindingError("code bindings context is invalid")
    bindings = []
    for value in raw:
        if not isinstance(value, Mapping) or set(value) != {
            "relative_file_path",
            "qualified_name",
            "fingerprint",
            "head_commit",
        }:
            raise CodeBindingError("code bindings context is invalid")
        relative, name = _validate_ref(
            value["relative_file_path"], value["qualified_name"]
        )
        fingerprint = value["fingerprint"]
        head = value["head_commit"]
        if (
            not isinstance(fingerprint, str)
            or _FINGERPRINT.fullmatch(fingerprint) is None
            or (
                head is not None
                and (not isinstance(head, str) or _HEAD.fullmatch(head) is None)
            )
        ):
            raise CodeBindingError("code bindings context is invalid")
        bindings.append(CodeBinding(relative, name, fingerprint, head))
    return tuple(bindings)


def binding_refs_from_context(
    context: Mapping[str, object],
) -> tuple[tuple[str, str | None], ...]:
    return tuple(
        (binding.relative_file_path, binding.qualified_name)
        for binding in _bindings_from_context(context)
    )


@dataclass(slots=True)
class _FileSnapshot:
    source: bytes
    fingerprints: dict[str | None, str] = field(default_factory=dict)


@dataclass(slots=True)
class _ReadBatch:
    remaining_bytes: int
    sources: dict[Path, bytes] = field(default_factory=dict)


def _read_source(path: Path, batch: _ReadBatch | None = None) -> bytes:
    remaining = batch.remaining_bytes if batch is not None else None
    if remaining == 0:
        raise CodeBindingUnavailableError("binding read budget is exhausted")
    limit = (
        min(_MAX_BINDING_FILE_BYTES, remaining)
        if remaining is not None
        else _MAX_BINDING_FILE_BYTES
    )
    try:
        with path.open("rb") as source_file:
            source = source_file.read(limit)
            overflow = source_file.read(1) if len(source) == limit else b""
    except OSError as exc:
        raise CodeBindingError("bound file cannot be read") from exc
    raw_bytes = len(source) + len(overflow)
    if batch is not None:
        batch.remaining_bytes = max(0, batch.remaining_bytes - raw_bytes)
    if raw_bytes > _MAX_BINDING_FILE_BYTES:
        raise CodeBindingError("bound file exceeds size limit")
    if remaining is not None and raw_bytes > remaining:
        raise CodeBindingUnavailableError("binding read budget is exhausted")
    return source.replace(b"\r\n", b"\n")


def _fingerprint(path: Path, root: Path, source: bytes, name: str | None) -> str:
    if name is None:
        return hashlib.sha256(source).hexdigest()
    from .api.v7.discovery_operations import default_code_indexer_factory

    try:
        producer = default_code_indexer_factory()
        if not isinstance(producer, _BindingCodeIndexer) or not producer.available:
            raise CodeBindingUnavailableError("code parser is unavailable")
        supported = producer.get_supported_extensions()
        if path.suffix.lower() not in supported:
            raise CodeBindingReferenceError("bound symbol extension is unsupported")
        entities = producer.index_source_strict(path, root, source)
        lines = source.split(b"\n")
        exact = []
        aliases = []
        for entity in entities:
            if not isinstance(entity, Mapping):
                raise CodeBindingUnavailableError(
                    "code parser returned invalid entities"
                )
            if entity.get("qualified_name") == name:
                exact.append(entity)
            elif entity.get("name") == name:
                aliases.append(entity)
        selected = exact or aliases
        if not exact and aliases:
            identities = set()
            for entity in aliases:
                identity = entity.get("qualified_name")
                if not isinstance(identity, str) or not identity.strip():
                    raise CodeBindingUnavailableError(
                        "code parser returned no usable symbol identity"
                    )
                identities.add(identity)
            if len(identities) != 1:
                raise CodeBindingReferenceError("bound symbol reference is ambiguous")
        chunks = []
        for entity in selected:
            navigation_start, end = entity.get("line_start"), entity.get("line_end")
            start = entity.get("binding_line_start", navigation_start)
            if (
                not isinstance(start, int)
                or isinstance(start, bool)
                or not isinstance(navigation_start, int)
                or isinstance(navigation_start, bool)
                or not isinstance(end, int)
                or isinstance(end, bool)
                or not 1 <= start <= navigation_start <= end <= len(lines)
            ):
                raise CodeBindingUnavailableError(
                    "code parser returned invalid line bounds"
                )
            chunks.append(b"\n".join(lines[start - 1 : end]))
    except _UnverifiableBindingError:
        raise
    except Exception as exc:
        raise CodeBindingUnavailableError("code parser failed") from exc
    if not chunks:
        raise CodeBindingError("bound symbol is missing")
    return hashlib.sha256(b"\n".join(chunks)).hexdigest()


def capture_bindings(
    workspace: Workspace, refs: Sequence[tuple[str, str | None]]
) -> tuple[CodeBinding, ...]:
    from .api.v7.resource_repository import read_git_output_sync

    if len(refs) > 16:
        raise CodeBindingError("too many code bindings")
    fingerprints = []
    for ref in refs:
        if not isinstance(ref, (tuple, list)) or len(ref) != 2:
            raise CodeBindingError("code binding reference is invalid")
        relative, name = _validate_ref(*ref)
        try:
            path = resolve_index_file(workspace.root, workspace.root / relative)
        except IndexPathError as exc:
            raise CodeBindingError(
                "bound file is missing or outside the workspace"
            ) from exc
        fingerprint = _fingerprint(path, workspace.root, _read_source(path), name)
        fingerprints.append((relative, name, fingerprint))
    head_raw = read_git_output_sync(workspace, ["rev-parse", "HEAD"])
    head = head_raw.decode("ascii", errors="replace").strip() if head_raw else None
    if head is not None and _HEAD.fullmatch(head) is None:
        head = None
    return tuple(
        CodeBinding(relative, name, fingerprint, head)
        for relative, name, fingerprint in fingerprints
    )


class BindingEvaluator:
    """Reuse parsed snapshots capped by entries and normalized source bytes.

    The source-byte cap is not a total heap cap for parsers or fingerprints.
    """

    def __init__(self) -> None:
        self._lock = Lock()
        self._cache: OrderedDict[Path, _FileSnapshot] = OrderedDict()
        self._cached_source_bytes = 0
        self._batch_context: ContextVar[_ReadBatch | None] = ContextVar(
            "binding_read_batch", default=None
        )

    @contextmanager
    def read_batch(
        self, *, budget_bytes: int = _MAX_BINDING_BATCH_BYTES
    ) -> Iterator[None]:
        """Memoize each resolved source within this context, not atomically.

        Every evaluation still resolves and stats the file before using its
        memoized bytes. Later content edits become visible in the next batch or
        an unbatched evaluation; this is not an atomic filesystem snapshot.
        The internal budget counts raw bytes before newline normalization,
        including failed size/budget probes, but not failed OSError reads.
        Reads deliver at most the budget plus one overflow-probe byte in total;
        memo hits are free and stat-based refusals preserve the remaining budget.
        """
        if (
            not isinstance(budget_bytes, int)
            or isinstance(budget_bytes, bool)
            or budget_bytes < 0
        ):
            raise ValueError("binding read budget must be a nonnegative integer")
        if self._batch_context.get() is not None:
            raise RuntimeError("binding read batch is already active")
        token = self._batch_context.set(_ReadBatch(budget_bytes))
        try:
            yield
        finally:
            self._batch_context.reset(token)

    def _current_fingerprint(self, root: Path, binding: CodeBinding) -> str:
        try:
            path = resolve_index_file(root, root / binding.relative_file_path)
            stat = path.stat()
        except (IndexPathError, OSError) as exc:
            raise CodeBindingError("bound file is missing or inaccessible") from exc
        if stat.st_size > _MAX_BINDING_FILE_BYTES:
            raise CodeBindingError("bound file exceeds size limit")
        batch = self._batch_context.get()
        source = batch.sources.get(path) if batch is not None else None
        if source is None:
            if batch is not None and stat.st_size > batch.remaining_bytes:
                raise CodeBindingUnavailableError("binding read budget is exhausted")
            source = _read_source(path, batch)
            if batch is not None:
                batch.sources[path] = source
        with self._lock:
            snapshot = self._cache.get(path)
            if snapshot is None or snapshot.source != source:
                if snapshot is not None:
                    self._cached_source_bytes -= len(snapshot.source)
                snapshot = _FileSnapshot(source)
                self._cache[path] = snapshot
                self._cached_source_bytes += len(source)
            self._cache.move_to_end(path)
            while (
                len(self._cache) > 256
                or self._cached_source_bytes > _MAX_CACHED_SOURCE_BYTES
            ):
                _, evicted = self._cache.popitem(last=False)
                self._cached_source_bytes -= len(evicted.source)
            if (
                binding.qualified_name is not None
                and binding.qualified_name in snapshot.fingerprints
            ):
                from .api.v7.discovery_operations import default_code_indexer_factory

                try:
                    if not getattr(default_code_indexer_factory(), "available", False):
                        raise CodeBindingUnavailableError("code parser is unavailable")
                except _UnverifiableBindingError:
                    raise
                except Exception as exc:
                    raise CodeBindingUnavailableError("code parser failed") from exc
            if binding.qualified_name not in snapshot.fingerprints:
                snapshot.fingerprints[binding.qualified_name] = _fingerprint(
                    path, root, snapshot.source, binding.qualified_name
                )
            return snapshot.fingerprints[binding.qualified_name]

    def evaluate(
        self, root: Path, context: Mapping[str, object]
    ) -> BindingEvaluation | None:
        bindings = _bindings_from_context(context)
        if not bindings:
            return None
        changed = []
        unverifiable = False
        for binding in bindings:
            label = binding.relative_file_path
            if binding.qualified_name is not None:
                label += "::" + binding.qualified_name
            try:
                fingerprint = self._current_fingerprint(root, binding)
            except _UnverifiableBindingError:
                unverifiable = True
                continue
            except CodeBindingError:
                changed.append(label)
                continue
            if fingerprint != binding.fingerprint:
                changed.append(label)
        applicability: Literal["current", "needs_revalidation", "unverifiable"]
        if changed:
            applicability = "needs_revalidation"
        elif unverifiable:
            applicability = "unverifiable"
        else:
            applicability = "current"
        return BindingEvaluation(applicability, tuple(dict.fromkeys(changed)))
