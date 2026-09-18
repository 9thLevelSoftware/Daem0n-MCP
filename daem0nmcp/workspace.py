"""Registered workspace and subordinate path security helpers."""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path


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


@dataclass(frozen=True, slots=True)
class Workspace:
    """A configured canonical workspace root and its stable opaque ID."""

    workspace_id: str
    root: Path


def _canonical_root(root: str | os.PathLike[str]) -> Path:
    return Path(root).expanduser().resolve()


def _root_key(root: Path) -> str:
    return os.path.normcase(str(root))


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
        ordered_roots = list(roots)
        if default_root is not None:
            ordered_roots.insert(0, default_root)

        self._by_id: dict[str, Workspace] = {}
        self._by_root: dict[str, Workspace] = {}
        self._default_id: str | None = None

        for index, configured_root in enumerate(ordered_roots):
            canonical = _canonical_root(configured_root)
            key = _root_key(canonical)
            workspace = self._by_root.get(key)
            if workspace is None:
                workspace = Workspace(_workspace_id(canonical), canonical)
                self._by_root[key] = workspace
                self._by_id[workspace.workspace_id] = workspace
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
