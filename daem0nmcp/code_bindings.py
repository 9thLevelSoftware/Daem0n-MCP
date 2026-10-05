"""Bounded repository fingerprints for server-managed memory code bindings."""

from __future__ import annotations

import hashlib
import re
from collections import OrderedDict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path, PureWindowsPath
from threading import Lock
from typing import Literal, Protocol, runtime_checkable

from .workspace import IndexPathError, Workspace, resolve_index_file

_MAX_BINDING_FILE_BYTES = 5 * 1024 * 1024
_FINGERPRINT = re.compile(r"[0-9a-f]{64}\Z")
_HEAD = re.compile(r"[0-9a-f]{40,64}\Z")


class CodeBindingError(ValueError):
    """A binding cannot be captured or its stored representation is invalid."""


class _UnverifiableBindingError(CodeBindingError):
    pass


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


def _read_source(path: Path) -> bytes:
    try:
        with path.open("rb") as source_file:
            source = source_file.read(_MAX_BINDING_FILE_BYTES + 1)
    except OSError as exc:
        raise CodeBindingError("bound file cannot be read") from exc
    if len(source) > _MAX_BINDING_FILE_BYTES:
        raise CodeBindingError("bound file exceeds size limit")
    return source.replace(b"\r\n", b"\n")


def _fingerprint(path: Path, root: Path, source: bytes, name: str | None) -> str:
    if name is None:
        return hashlib.sha256(source).hexdigest()
    from .api.v7.discovery_operations import default_code_indexer_factory

    try:
        producer = default_code_indexer_factory()
        if not isinstance(producer, _BindingCodeIndexer) or not producer.available:
            raise _UnverifiableBindingError("code parser is unavailable")
        supported = producer.get_supported_extensions()
        if path.suffix.lower() not in supported:
            raise _UnverifiableBindingError("bound symbol extension is unsupported")
        entities = producer.index_source_strict(path, root, source)
        lines = source.split(b"\n")
        chunks = []
        for entity in entities:
            if not isinstance(entity, Mapping):
                raise _UnverifiableBindingError("code parser returned invalid entities")
            if name not in (entity.get("qualified_name"), entity.get("name")):
                continue
            start, end = entity.get("line_start"), entity.get("line_end")
            if (
                not isinstance(start, int)
                or not isinstance(end, int)
                or not 1 <= start <= end <= len(lines)
            ):
                raise _UnverifiableBindingError(
                    "code parser returned invalid line bounds"
                )
            chunks.append(b"\n".join(lines[start - 1 : end]))
    except _UnverifiableBindingError:
        raise
    except Exception as exc:
        raise _UnverifiableBindingError("code parser failed") from exc
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
    """Reuse bounded parsed snapshots only while their source content agrees."""

    def __init__(self) -> None:
        self._lock = Lock()
        self._cache: OrderedDict[Path, _FileSnapshot] = OrderedDict()

    def _current_fingerprint(self, root: Path, binding: CodeBinding) -> str:
        try:
            path = resolve_index_file(root, root / binding.relative_file_path)
            stat = path.stat()
        except (IndexPathError, OSError) as exc:
            raise CodeBindingError("bound file is missing or inaccessible") from exc
        if stat.st_size > _MAX_BINDING_FILE_BYTES:
            raise CodeBindingError("bound file exceeds size limit")
        source = _read_source(path)
        with self._lock:
            snapshot = self._cache.get(path)
            if snapshot is None or snapshot.source != source:
                snapshot = _FileSnapshot(source)
                self._cache[path] = snapshot
                if len(self._cache) > 256:
                    self._cache.popitem(last=False)
            self._cache.move_to_end(path)
            if (
                binding.qualified_name is not None
                and binding.qualified_name in snapshot.fingerprints
            ):
                from .api.v7.discovery_operations import default_code_indexer_factory

                if not getattr(default_code_indexer_factory(), "available", False):
                    raise _UnverifiableBindingError("code parser is unavailable")
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
