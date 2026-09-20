"""Host-side normalization for the documented native file-edit tools.

This module deliberately supports only declared file-edit shapes.  It is not a
shell sandbox and does not attempt to infer paths from arbitrary arguments.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..edit_bridge import FilePreimage, NativeEditRequest

_PATH_FIELDS = ("file_path", "notebook_path", "filePath", "notebookPath")
_DEFAULT_EDIT_TOOLS = frozenset(
    {"Edit", "Write", "NotebookEdit", "edit", "write", "apply_patch"}
)
_MAX_PREIMAGE_BYTES = 16 * 1024 * 1024
_MAX_PATCH_BYTES = 128 * 1024


class NativeEditNormalizationError(ValueError):
    """A native call cannot be safely represented by the edit bridge."""


@dataclass(frozen=True, slots=True)
class _PathBinding:
    relative_path: str
    required_state: str | None = None


def configured_native_edit_tools(environ: Mapping[str, str]) -> frozenset[str]:
    """Return explicitly configured native edit names or Claude's known set."""
    raw = environ.get("DAEM0NMCP_NATIVE_EDIT_TOOLS")
    if raw is None:
        return _DEFAULT_EDIT_TOOLS
    tools = frozenset(part.strip() for part in raw.split(",") if part.strip())
    if not tools or any(len(tool) > 80 for tool in tools):
        raise NativeEditNormalizationError("native edit tools are invalid")
    return tools


def native_edit_request(
    *,
    project_path: str | Path,
    tool_name: str,
    tool_input: Mapping[str, Any],
    configured_tools: frozenset[str],
) -> NativeEditRequest:
    """Normalize a real native edit and calculate its current preimages.

    The host copies the arguments as canonical JSON, rewrites only documented
    path fields to workspace-relative POSIX paths, and independently hashes the
    referenced files.  Callers retain their original native arguments for the
    actual retry; this object exists solely for exact bridge comparison.
    """
    root = Path(project_path).resolve(strict=True)
    normalized, bindings = _normalized_edit(
        root=root,
        tool_name=tool_name,
        tool_input=tool_input,
        configured_tools=configured_tools,
    )
    preimages = tuple(_preimage(root, item.relative_path) for item in bindings)
    for binding, preimage in zip(bindings, preimages, strict=True):
        if (
            binding.required_state is not None
            and preimage.state != binding.required_state
        ):
            raise NativeEditNormalizationError(
                "native patch path does not match the required preimage state"
            )
    try:
        return NativeEditRequest(
            tool_name=tool_name,
            arguments=normalized,
            preimages=preimages,
        )
    except ValueError as exc:
        raise NativeEditNormalizationError("native edit is invalid") from exc


def native_edit_relative_paths(
    *,
    project_path: str | Path,
    tool_name: str,
    tool_input: Mapping[str, Any],
    configured_tools: frozenset[str],
) -> tuple[str, ...]:
    """Return host-validated paths after execution without rechecking preimages."""

    root = Path(project_path).resolve(strict=True)
    _normalized, bindings = _normalized_edit(
        root=root,
        tool_name=tool_name,
        tool_input=tool_input,
        configured_tools=configured_tools,
    )
    return tuple(item.relative_path for item in bindings)


def _normalized_edit(
    *,
    root: Path,
    tool_name: str,
    tool_input: Mapping[str, Any],
    configured_tools: frozenset[str],
) -> tuple[dict[str, Any], tuple[_PathBinding, ...]]:
    if tool_name not in configured_tools:
        raise NativeEditNormalizationError("native edit tool is not configured")
    if not isinstance(tool_input, Mapping):
        raise NativeEditNormalizationError("native edit input is invalid")
    try:
        normalized = json.loads(json.dumps(dict(tool_input), sort_keys=True))
    except (TypeError, ValueError, RecursionError) as exc:
        raise NativeEditNormalizationError("native edit input is invalid") from exc
    if tool_name == "apply_patch":
        if set(normalized) != {"patchText"} or not isinstance(
            normalized.get("patchText"), str
        ):
            raise NativeEditNormalizationError("native patch input is invalid")
        return normalized, _apply_patch_bindings(root, normalized["patchText"])

    paths: set[str] = set()
    for field in _PATH_FIELDS:
        value = normalized.get(field)
        if value is None:
            continue
        relative = _relative_path(root, value)
        normalized[field] = relative
        paths.add(relative)
    if not paths:
        raise NativeEditNormalizationError("native edit has no supported file path")
    return normalized, tuple(_PathBinding(path) for path in sorted(paths))


def _relative_path(root: Path, value: object) -> str:
    if not isinstance(value, str) or not value:
        raise NativeEditNormalizationError("native edit path is invalid")
    try:
        candidate = Path(value)
        resolved = (candidate if candidate.is_absolute() else root / candidate).resolve(
            strict=False
        )
        relative = resolved.relative_to(root).as_posix()
    except (OSError, RuntimeError, ValueError) as exc:
        raise NativeEditNormalizationError(
            "native edit path is outside workspace"
        ) from exc
    # A backslash is a separator only on Windows (as_posix() converts it
    # there); on POSIX it is a filename character the bridge cannot represent.
    if not relative or relative == "." or len(relative) > 1024 or "\\" in relative:
        raise NativeEditNormalizationError("native edit path is invalid")
    return relative


def _apply_patch_bindings(root: Path, patch_text: str) -> tuple[_PathBinding, ...]:
    """Parse the strict, unambiguous subset of OpenCode 1.18.21 patch grammar."""

    if not patch_text or len(patch_text.encode()) > _MAX_PATCH_BYTES:
        raise NativeEditNormalizationError("native patch input is invalid")
    if "\r" in patch_text:
        raise NativeEditNormalizationError("native patch line endings are invalid")
    lines = patch_text.strip().split("\n")
    if len(lines) < 3 or lines[0] != "*** Begin Patch" or lines[-1] != "*** End Patch":
        raise NativeEditNormalizationError("native patch markers are invalid")

    bindings: dict[str, str] = {}

    def add_path(value: str, state: str) -> None:
        relative = _relative_path(root, value.strip())
        if relative in bindings:
            raise NativeEditNormalizationError("native patch path is duplicated")
        bindings[relative] = state

    index = 1
    headers = 0
    while index < len(lines) - 1:
        line = lines[index]
        kinds = (
            ("*** Add File:", "add"),
            ("*** Delete File:", "delete"),
            ("*** Update File:", "update"),
        )
        selected = next((item for item in kinds if line.startswith(item[0])), None)
        if selected is None:
            raise NativeEditNormalizationError("native patch syntax is unsupported")
        prefix, kind = selected
        path_value = line[len(prefix) :].strip()
        if not path_value:
            raise NativeEditNormalizationError("native patch path is invalid")
        add_path(path_value, "missing" if kind == "add" else "file")
        headers += 1
        index += 1

        moved = False
        if (
            kind == "update"
            and index < len(lines) - 1
            and lines[index].startswith("*** Move to:")
        ):
            move_value = lines[index][len("*** Move to:") :].strip()
            if not move_value:
                raise NativeEditNormalizationError("native patch path is invalid")
            add_path(move_value, "missing")
            moved = True
            index += 1

        chunk_count = 0
        content_count = 0
        while index < len(lines) - 1 and (
            not lines[index].startswith("***") or lines[index] == "*** End of File"
        ):
            content = lines[index]
            if kind == "add":
                if not content.startswith("+"):
                    raise NativeEditNormalizationError(
                        "native patch add syntax is unsupported"
                    )
                content_count += 1
            elif kind == "delete":
                raise NativeEditNormalizationError(
                    "native patch delete syntax is unsupported"
                )
            elif content.startswith("@@"):
                chunk_count += 1
            elif not (
                chunk_count
                and (
                    content.startswith((" ", "+", "-")) or content == "*** End of File"
                )
            ):
                raise NativeEditNormalizationError(
                    "native patch update syntax is unsupported"
                )
            index += 1
        if kind == "add" and content_count == 0:
            raise NativeEditNormalizationError("native patch add is empty")
        if kind == "update" and chunk_count == 0 and not moved:
            raise NativeEditNormalizationError("native patch update is empty")

    if headers == 0 or len(bindings) > 32:
        raise NativeEditNormalizationError("native patch has no bounded file changes")
    return tuple(
        _PathBinding(relative, state) for relative, state in sorted(bindings.items())
    )


def _preimage(root: Path, relative_path: str) -> FilePreimage:
    target = root / relative_path
    try:
        target.resolve(strict=False).relative_to(root)
        if not target.is_file():
            return FilePreimage(relative_path, "missing", None, 0)
        with target.open("rb") as source:
            digest = hashlib.sha256()
            size = 0
            while chunk := source.read(64 * 1024):
                size += len(chunk)
                if size > _MAX_PREIMAGE_BYTES:
                    raise NativeEditNormalizationError(
                        "native edit preimage is too large"
                    )
                digest.update(chunk)
    except NativeEditNormalizationError:
        raise
    except OSError as exc:
        raise NativeEditNormalizationError(
            "native edit preimage is unavailable"
        ) from exc
    return FilePreimage(relative_path, "file", digest.hexdigest(), size)


__all__ = [
    "NativeEditNormalizationError",
    "configured_native_edit_tools",
    "native_edit_relative_paths",
    "native_edit_request",
]
