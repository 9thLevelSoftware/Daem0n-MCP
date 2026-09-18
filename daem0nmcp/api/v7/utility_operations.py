"""Deterministic, workspace-bounded v7 utility operations."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import os
import re
from collections.abc import Callable, Mapping
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Any

from pydantic import ValidationError

from ...bounded_workers import BoundedWorkerBusyError, BoundedWorkerPool
from ...event_store import canonical_json_bytes, parse_canonical_json, sha256_json
from ...workspace import Workspace, WorkspaceRegistry
from .application import AdmittedRequest
from .errors import STABLE_ERROR_CODE_SET
from .models import Page, contains_absolute_filesystem_path
from .runtime_protocols import WorkerPool
from .tasks import await_task_terminal
from .tools import ContextCompressData, RefactorProposalData, TodoFinding

_TODO_RE = re.compile(r"\b(TODO|FIXME|HACK|XXX|NOTE)\b", re.IGNORECASE)
_CURSOR_RE = re.compile(r"^cur_v1_([A-Za-z0-9_-]+)_([0-9a-f]{64})$")
_CODE_SUFFIXES = frozenset(
    {
        ".c",
        ".cc",
        ".cpp",
        ".go",
        ".h",
        ".hpp",
        ".java",
        ".js",
        ".jsx",
        ".md",
        ".ps1",
        ".py",
        ".rs",
        ".sh",
        ".toml",
        ".ts",
        ".tsx",
        ".yaml",
        ".yml",
    }
)
_IGNORED_DIRECTORIES = frozenset(
    {
        ".daem0nmcp",
        ".git",
        ".hg",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".svn",
        ".tox",
        ".venv",
        "__pycache__",
        "build",
        "dist",
        "node_modules",
        "vendor",
    }
)
_MAX_SOURCE_FILES = 20_000
_MAX_SOURCE_BYTES = 16 * 1024 * 1024
_MAX_FILE_BYTES = 1024 * 1024


class UtilityOperationError(RuntimeError):
    """Stable, path-free failure understood by the shared v7 router."""

    def __init__(self, code: str) -> None:
        if code not in STABLE_ERROR_CODE_SET:
            raise ValueError("utility operation error code is not stable")
        self.code = code
        super().__init__(code)


def _default_worker_pool() -> BoundedWorkerPool:
    return BoundedWorkerPool(
        max_workers=4,
        thread_name_prefix="daem0nmcp-v7-utility",
    )


@dataclass(frozen=True, slots=True)
class UtilityOperationDependencies:
    """Owned dependencies for deterministic utility operations."""

    cursor_secret: bytes
    worker_pool: WorkerPool = field(default_factory=_default_worker_pool)

    def __post_init__(self) -> None:
        if not isinstance(self.cursor_secret, bytes) or len(self.cursor_secret) < 32:
            raise ValueError("cursor_secret must contain at least 32 bytes")
        if not callable(getattr(self.worker_pool, "run", None)) or not callable(
            getattr(self.worker_pool, "shutdown", None)
        ):
            raise TypeError("worker_pool must provide run and shutdown")

    def close(self) -> None:
        self.worker_pool.shutdown()


def _authorize(
    workspace: Workspace,
    request: AdmittedRequest,
    tool_name: str,
) -> Path:
    if (
        not isinstance(workspace, Workspace)
        or not isinstance(request, AdmittedRequest)
        or request.tool_name != tool_name
        or request.workspace_id != workspace.workspace_id
    ):
        raise UtilityOperationError("UNAUTHORIZED_WORKSPACE")
    try:
        root = workspace.root.resolve(strict=True)
        registered = WorkspaceRegistry([root], default_root=root).default
        exact = os.path.normcase(str(root)) == os.path.normcase(str(workspace.root))
    except (OSError, RuntimeError, TypeError, ValueError):
        raise UtilityOperationError("UNAUTHORIZED_WORKSPACE") from None
    if registered.workspace_id != workspace.workspace_id or not exact:
        raise UtilityOperationError("UNAUTHORIZED_WORKSPACE")
    return root


def _relative_target(
    root: Path,
    relative_path: str,
    *,
    directory: bool,
) -> Path:
    try:
        candidate = root
        if relative_path != ".":
            for component in relative_path.split("/"):
                candidate = candidate / component
                if candidate.is_symlink():
                    raise ValueError
        resolved = candidate.resolve(strict=True)
        resolved.relative_to(root)
        if directory and not resolved.is_dir():
            raise ValueError
        if not directory and not resolved.is_file():
            raise ValueError
        return resolved
    except (OSError, RuntimeError, TypeError, ValueError):
        raise UtilityOperationError("WORKSPACE_PATH_ESCAPE") from None


async def _run_read(
    dependencies: UtilityOperationDependencies,
    operation: Callable[[], Any],
) -> Any:
    worker = asyncio.create_task(dependencies.worker_pool.run(operation))
    try:
        return await asyncio.shield(worker)
    except asyncio.CancelledError as cancellation:
        with suppress(asyncio.CancelledError, Exception):
            await await_task_terminal(worker)
        raise cancellation
    except BoundedWorkerBusyError as exc:
        raise UtilityOperationError("TASK_REQUIRED") from exc


async def _compress_text(
    text: str,
    rate: float,
    preserve_code: bool,
) -> ContextCompressData:
    token_ends: list[int] = []
    in_token = False
    for index, character in enumerate(text):
        if character.isspace():
            if in_token:
                token_ends.append(index)
                in_token = False
        else:
            in_token = True
        if index and index % 4096 == 0:
            await asyncio.sleep(0)
    if in_token:
        token_ends.append(len(text))
    original_tokens = len(token_ends)
    if not token_ends or rate >= 1:
        rendered = text
    else:
        target = max(1, int(original_tokens * rate))
        end = token_ends[min(target, original_tokens) - 1]
        if preserve_code and text[:end].count("```") % 2:
            closing = text.find("```", end)
            if closing >= 0:
                end = closing + 3
        rendered = text[:end].rstrip()
        if not rendered:
            rendered = text
    rendered_tokens = sum(end <= len(rendered) for end in token_ends)
    ratio = 1.0 if original_tokens == 0 else rendered_tokens / original_tokens
    return ContextCompressData(
        text=rendered,
        original_tokens=original_tokens,
        rendered_tokens=rendered_tokens,
        ratio=min(1.0, max(ratio, 1 / max(original_tokens, 1))),
        provider="deterministic-extractive-v1",
    )


def _scan_files(scan_root: Path, workspace_root: Path) -> list[Path]:
    files: list[Path] = []
    stack = [scan_root]
    while stack:
        directory = stack.pop()
        try:
            entries = sorted(directory.iterdir(), key=lambda item: item.name)
        except OSError:
            raise UtilityOperationError("CAPABILITY_DEGRADED") from None
        child_directories: list[Path] = []
        for entry in entries:
            try:
                if entry.is_symlink():
                    continue
                resolved = entry.resolve(strict=True)
                resolved.relative_to(workspace_root)
                if resolved.is_dir():
                    if entry.name not in _IGNORED_DIRECTORIES:
                        child_directories.append(resolved)
                elif (
                    resolved.is_file() and resolved.suffix.casefold() in _CODE_SUFFIXES
                ):
                    files.append(resolved)
                    if len(files) > _MAX_SOURCE_FILES:
                        raise UtilityOperationError("CAPABILITY_DEGRADED")
            except UtilityOperationError:
                raise
            except (OSError, RuntimeError, ValueError):
                continue
        stack.extend(reversed(child_directories))
    return sorted(files, key=lambda item: item.relative_to(workspace_root).as_posix())


def _findings(
    workspace_root: Path,
    scan_root: Path,
    selected_types: frozenset[str],
) -> list[TodoFinding]:
    findings: list[TodoFinding] = []
    consumed = 0
    for path in _scan_files(scan_root, workspace_root):
        try:
            size = path.stat().st_size
        except OSError:
            continue
        if size > _MAX_FILE_BYTES:
            continue
        consumed += size
        if consumed > _MAX_SOURCE_BYTES:
            raise UtilityOperationError("CAPABILITY_DEGRADED")
        try:
            text = path.read_text(encoding="utf-8", errors="strict")
        except (OSError, UnicodeError):
            continue
        relative = path.relative_to(workspace_root).as_posix()
        for line_number, line in enumerate(text.splitlines(), start=1):
            match = _TODO_RE.search(line)
            if match is None:
                continue
            todo_type = match.group(1).casefold()
            if todo_type not in selected_types:
                continue
            rendered = line.strip()[:2000]
            if not rendered or contains_absolute_filesystem_path(rendered):
                continue
            try:
                findings.append(
                    TodoFinding.model_validate(
                        {
                            "relative_file_path": relative,
                            "line": line_number,
                            "todo_type": todo_type,
                            "text": rendered,
                        }
                    )
                )
            except ValidationError:
                continue
    return findings


def _selector_digest(
    workspace_id: str,
    relative_root: str,
    selected_types: frozenset[str],
) -> str:
    return sha256_json(
        [
            "daem0nmcp",
            "v7",
            "code-todos-scan",
            workspace_id,
            relative_root,
            sorted(selected_types),
        ]
    )


def _cursor(secret: bytes, selector_digest: str, offset: int) -> str:
    payload = canonical_json_bytes(
        {"selector": selector_digest, "offset": offset, "version": 1}
    )
    encoded = base64.urlsafe_b64encode(payload).rstrip(b"=").decode("ascii")
    signature = hmac.new(secret, payload, hashlib.sha256).hexdigest()
    return f"cur_v1_{encoded}_{signature}"


def _cursor_offset(secret: bytes, cursor: str, selector_digest: str) -> int:
    match = _CURSOR_RE.fullmatch(cursor)
    if match is None:
        raise UtilityOperationError("INVALID_ARGUMENT")
    encoded, supplied_signature = match.groups()
    try:
        padding = "=" * (-len(encoded) % 4)
        payload = base64.b64decode(
            encoded + padding,
            altchars=b"-_",
            validate=True,
        )
        expected_signature = hmac.new(secret, payload, hashlib.sha256).hexdigest()
        decoded = parse_canonical_json(payload.decode("utf-8"))
    except Exception:
        raise UtilityOperationError("INVALID_ARGUMENT") from None
    if not hmac.compare_digest(supplied_signature, expected_signature):
        raise UtilityOperationError("INVALID_ARGUMENT")
    if (
        not isinstance(decoded, dict)
        or set(decoded) != {"offset", "selector", "version"}
        or decoded.get("version") != 1
        or decoded.get("selector") != selector_digest
        or isinstance(decoded.get("offset"), bool)
        or not isinstance(decoded.get("offset"), int)
        or not 0 < decoded["offset"] <= _MAX_SOURCE_FILES
    ):
        raise UtilityOperationError("INVALID_ARGUMENT")
    return int(decoded["offset"])


def _todo_page(
    dependencies: UtilityOperationDependencies,
    workspace: Workspace,
    request: AdmittedRequest,
) -> Page[TodoFinding]:
    root = _authorize(workspace, request, "code_todos_scan")
    scan_root = _relative_target(root, request.relative_root, directory=True)
    selected_types = frozenset(
        request.types or {"todo", "fixme", "hack", "xxx", "note"}
    )
    selector = _selector_digest(
        workspace.workspace_id,
        request.relative_root,
        selected_types,
    )
    offset = (
        0
        if request.cursor is None
        else _cursor_offset(dependencies.cursor_secret, request.cursor, selector)
    )
    findings = _findings(root, scan_root, selected_types)
    if offset > len(findings):
        raise UtilityOperationError("INVALID_ARGUMENT")
    selected = findings[offset : offset + request.limit]
    next_offset = offset + len(selected)
    truncated = next_offset < len(findings)
    return Page[TodoFinding](
        items=selected,
        next_cursor=(
            _cursor(dependencies.cursor_secret, selector, next_offset)
            if truncated
            else None
        ),
        truncated=truncated,
    )


def _refactor_proposal(
    workspace: Workspace,
    request: AdmittedRequest,
) -> RefactorProposalData:
    root = _authorize(workspace, request, "code_refactor_propose")
    path = _relative_target(root, request.relative_file_path, directory=False)
    try:
        if path.stat().st_size > _MAX_FILE_BYTES:
            raise UtilityOperationError("INVALID_ARGUMENT")
        text = path.read_text(encoding="utf-8", errors="strict")
    except UtilityOperationError:
        raise
    except (OSError, UnicodeError):
        raise UtilityOperationError("CAPABILITY_DEGRADED") from None
    lines = text.splitlines()
    debt_count = sum(1 for line in lines if _TODO_RE.search(line))
    long_lines = sum(1 for line in lines if len(line) > 100)
    warnings: list[str] = []
    if debt_count:
        warnings.append(f"Review {debt_count} existing debt marker(s) before editing.")
    if long_lines:
        warnings.append(f"Review {long_lines} long line(s) while preserving behavior.")
    objective = request.objective or "Improve cohesion while preserving behavior"
    proposal = (
        f"Refactor {request.relative_file_path} toward this objective: {objective}. "
        f"The bounded static review found {len(lines)} lines, {debt_count} debt "
        f"marker(s), and {long_lines} line(s) over 100 characters. Extract focused "
        "units, keep public behavior stable, and add tests before each structural change."
    )
    return RefactorProposalData(
        proposal=proposal,
        affected_entities=[],
        warnings=warnings,
        evidence_refs=[],
    )


def build_utility_operations(
    dependencies: UtilityOperationDependencies,
) -> Mapping[str, Callable[..., Any]]:
    """Return the immutable deterministic utility operation registry."""

    if not isinstance(dependencies, UtilityOperationDependencies):
        raise TypeError("utility operation dependencies are required")

    async def context_compress(
        *, workspace: Workspace, request: AdmittedRequest
    ) -> ContextCompressData:
        _authorize(workspace, request, "context_compress")
        rate = 0.5 if request.rate is None else float(request.rate)
        return await _compress_text(
            request.text,
            rate,
            bool(request.preserve_code),
        )

    async def code_todos_scan(
        *, workspace: Workspace, request: AdmittedRequest
    ) -> Page[TodoFinding]:
        _authorize(workspace, request, "code_todos_scan")
        return await _run_read(
            dependencies,
            lambda: _todo_page(dependencies, workspace, request),
        )

    async def code_refactor_propose(
        *, workspace: Workspace, request: AdmittedRequest
    ) -> RefactorProposalData:
        _authorize(workspace, request, "code_refactor_propose")
        return await _run_read(
            dependencies,
            lambda: _refactor_proposal(workspace, request),
        )

    context_compress.__dict__["__daem0nmcp_sync_fallback_safe__"] = True
    return MappingProxyType(
        {
            "code_refactor_propose": code_refactor_propose,
            "code_todos_scan": code_todos_scan,
            "context_compress": context_compress,
        }
    )


__all__ = [
    "UtilityOperationDependencies",
    "UtilityOperationError",
    "build_utility_operations",
]
