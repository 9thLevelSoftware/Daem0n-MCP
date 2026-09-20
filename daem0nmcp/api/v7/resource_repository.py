"""Dependency-free SQLite readers for the bounded v7 JSON resources.

The repository is deliberately a read-only adapter.  Its caller owns active
database resolution for a registered :class:`~daem0nmcp.workspace.Workspace`;
all pointer and SQLite work then runs in a bounded worker with a fresh
connection.  Legacy integer keys are confined to this module and can reach a
wire model only through the immutable schema-19 public-ID mapping.
"""

from __future__ import annotations

import asyncio
import json
import math
import os
import posixpath
import re
import sqlite3
import subprocess
import sys
import tempfile
from collections.abc import Awaitable, Callable, Mapping
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TypeVar, cast

from ...bounded_workers import BoundedWorkerPool
from ...schema_version import CURRENT_SCHEMA_VERSION
from ...storage_activation import DatabaseFileLock, ResolvedActiveDatabase
from ...workspace import Workspace, normalize_resolved_path
from .errors import is_database_busy
from .models import RecordSummary
from .public_ids import PublicObjectIdRepository
from .resources import (
    RESOURCE_FETCH_LIMIT,
    ActiveContextItem,
    ResourceReader,
    ResourceReadRequest,
    ResourceRow,
    RuleView,
)


class _WindowsKillOnCloseJob:
    """Own a Windows process tree without an optional pywin32 dependency."""

    def __init__(self, process: subprocess.Popen[bytes]) -> None:
        self._handle: object | None = None
        self._close: Callable[[object], object] | None = None
        if sys.platform != "win32":
            return
        import ctypes
        from ctypes import wintypes

        class _BasicLimit(ctypes.Structure):
            _fields_ = [
                ("PerProcessUserTimeLimit", ctypes.c_longlong),
                ("PerJobUserTimeLimit", ctypes.c_longlong),
                ("LimitFlags", wintypes.DWORD),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ctypes.c_size_t),
                ("PriorityClass", wintypes.DWORD),
                ("SchedulingClass", wintypes.DWORD),
            ]

        class _ExtendedLimit(ctypes.Structure):
            _fields_ = [
                ("BasicLimitInformation", _BasicLimit),
                ("IoInfo", ctypes.c_byte * 48),
                ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t),
            ]

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        kernel32.CreateJobObjectW.restype = wintypes.HANDLE
        kernel32.SetInformationJobObject.argtypes = [
            wintypes.HANDLE,
            ctypes.c_int,
            ctypes.c_void_p,
            wintypes.DWORD,
        ]
        kernel32.SetInformationJobObject.restype = wintypes.BOOL
        kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL
        job = kernel32.CreateJobObjectW(None, None)
        if not job:
            raise OSError(ctypes.get_last_error(), "CreateJobObjectW failed")
        try:
            info = _ExtendedLimit()
            info.BasicLimitInformation.LimitFlags = 0x00002000  # KILL_ON_JOB_CLOSE
            if not kernel32.SetInformationJobObject(
                job, 9, ctypes.byref(info), ctypes.sizeof(info)
            ):
                raise OSError(ctypes.get_last_error(), "SetInformationJobObject failed")
            if not kernel32.AssignProcessToJobObject(job, process._handle):  # type: ignore[attr-defined]
                raise OSError(
                    ctypes.get_last_error(), "AssignProcessToJobObject failed"
                )
        except Exception:
            kernel32.CloseHandle(job)
            raise
        self._handle = job
        self._close = kernel32.CloseHandle

    def resume(self, process_id: int) -> None:
        """Resume the primary thread only after the suspended process is owned."""
        if sys.platform != "win32":
            return
        import ctypes
        from ctypes import wintypes

        class ThreadEntry(ctypes.Structure):
            _fields_ = [
                ("dwSize", wintypes.DWORD),
                ("cntUsage", wintypes.DWORD),
                ("th32ThreadID", wintypes.DWORD),
                ("th32OwnerProcessID", wintypes.DWORD),
                ("tpBasePri", wintypes.LONG),
                ("tpDeltaPri", wintypes.LONG),
                ("dwFlags", wintypes.DWORD),
            ]

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
        kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
        for name in ("Thread32First", "Thread32Next"):
            method = getattr(kernel32, name)
            method.argtypes = [wintypes.HANDLE, ctypes.POINTER(ThreadEntry)]
            method.restype = wintypes.BOOL
        kernel32.OpenThread.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.OpenThread.restype = wintypes.HANDLE
        kernel32.ResumeThread.argtypes = [wintypes.HANDLE]
        kernel32.ResumeThread.restype = wintypes.DWORD
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL
        snapshot = kernel32.CreateToolhelp32Snapshot(4, 0)  # TH32CS_SNAPTHREAD
        if snapshot == ctypes.c_void_p(-1).value:
            raise OSError(ctypes.get_last_error(), "Thread snapshot failed")
        try:
            entry = ThreadEntry()
            entry.dwSize = ctypes.sizeof(entry)
            found = kernel32.Thread32First(snapshot, ctypes.byref(entry))
            thread_ids = []
            while found:
                if entry.th32OwnerProcessID == process_id:
                    thread_ids.append(entry.th32ThreadID)
                found = kernel32.Thread32Next(snapshot, ctypes.byref(entry))
            if len(thread_ids) != 1:
                raise OSError("Suspended process primary thread is ambiguous")
            thread = kernel32.OpenThread(
                2, False, thread_ids[0]
            )  # THREAD_SUSPEND_RESUME
            if not thread:
                raise OSError(ctypes.get_last_error(), "OpenThread failed")
            try:
                if kernel32.ResumeThread(thread) != 1:
                    raise OSError("Unexpected primary thread suspend count")
            finally:
                kernel32.CloseHandle(thread)
        finally:
            kernel32.CloseHandle(snapshot)

    def close(self) -> None:
        if self._handle is not None and self._close is not None:
            self._close(self._handle)
            self._handle = None


_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
_PUBLIC_RECORD_TYPES = frozenset(
    {"decision", "pattern", "warning", "learning", "procedure", "observation"}
)
_MAX_DATABASE_JSON_BYTES = 65_536
_MAX_GIT_OUTPUT_BYTES = 1_048_576
_MAX_GIT_CHANGES = 200
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_PROJECTION_NAMES = frozenset(
    {"lexical", "dense", "graph", "temporal", "procedure", "outcome", "code"}
)
_REQUIRED_TABLES = frozenset(
    {
        "active_context",
        "active_context_entries",
        "legacy_id_map",
        "memory_records",
        "governance_rules",
        "projection_manifests",
        "public_object_ids",
        "schema_version",
        "v7_migration_runs",
    }
)
_RESOURCE_WORKERS = BoundedWorkerPool(
    max_workers=4,
    thread_name_prefix="daem0nmcp-v7-resources",
)


class ResourceRepositoryError(RuntimeError):
    """Invariant, path-free failure raised for every repository fault."""

    code = "RESOURCE_REPOSITORY_UNAVAILABLE"

    def __init__(self, code: str = "RESOURCE_REPOSITORY_UNAVAILABLE") -> None:
        self.code = code
        super().__init__(code)


ActiveDatabaseResolver = Callable[[Workspace], ResolvedActiveDatabase]


@dataclass(frozen=True, slots=True)
class ResourceRepositorySnapshot:
    """Four briefing sections read under one storage-generation lock."""

    warnings: list[ResourceRow[RecordSummary]]
    failures: list[ResourceRow[RecordSummary]]
    rules: list[RuleView]
    active_context: list[ResourceRow[ActiveContextItem]]
    decisions: list[ResourceRow[RecordSummary]] = field(default_factory=list)
    git_changes: list[object] = field(default_factory=list)
    projection_freshness: list[object] = field(default_factory=list)
    workspace_statistics: dict[str, int] = field(default_factory=dict)
    stale_projection_count: int = 0


@dataclass(frozen=True, slots=True)
class ResourceRepositoryReaders:
    """The four bound readers consumed directly by ``ResourceDependencies``."""

    warning_reader: ResourceReader
    failure_reader: ResourceReader
    rule_reader: ResourceReader
    active_context_reader: ResourceReader
    briefing_snapshot_reader: (
        Callable[..., Awaitable[ResourceRepositorySnapshot]] | None
    ) = None


def _bounded_timeout(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("timeout_seconds must be a positive finite number")
    try:
        timeout = float(value)
    except (OverflowError, ValueError) as exc:
        raise ValueError("timeout_seconds must be a positive finite number") from exc
    if not math.isfinite(timeout) or timeout <= 0 or timeout > 60:
        raise ValueError("timeout_seconds must be a positive finite number")
    return timeout


def _utc_clock() -> datetime:
    return datetime.now(timezone.utc)


def _plain_int(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("database integer is invalid")
    return value


def _flag(value: object) -> bool:
    integer = _plain_int(value)
    if integer not in (0, 1):
        raise ValueError("database flag is invalid")
    return bool(integer)


def _datetime_from_us(value: object) -> datetime:
    microseconds = _plain_int(value)
    try:
        return _EPOCH + timedelta(microseconds=microseconds)
    except (OverflowError, ValueError) as exc:
        raise ValueError("database timestamp is invalid") from exc


def _legacy_datetime(value: object) -> datetime:
    if not isinstance(value, str) or not value:
        raise ValueError("legacy timestamp is invalid")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (OverflowError, ValueError) as exc:
        raise ValueError("legacy timestamp is invalid") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _strict_json(value: object) -> object:
    if not isinstance(value, str):
        raise ValueError("database JSON is invalid")
    try:
        encoded = value.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise ValueError("database JSON is invalid") from exc
    if len(encoded) > _MAX_DATABASE_JSON_BYTES:
        raise ValueError("database JSON exceeds the resource bound")
    duplicate = False

    def pairs_hook(pairs: list[tuple[str, object]]) -> dict[str, object]:
        nonlocal duplicate
        result: dict[str, object] = {}
        for key, item in pairs:
            if key in result:
                duplicate = True
            result[key] = item
        return result

    def reject_constant(_name: str) -> object:
        raise ValueError("database JSON is invalid")

    try:
        decoded = json.loads(
            value,
            object_pairs_hook=pairs_hook,
            parse_constant=reject_constant,
        )
    except (json.JSONDecodeError, RecursionError, UnicodeError, ValueError) as exc:
        raise ValueError("database JSON is invalid") from exc
    if duplicate:
        raise ValueError("database JSON is invalid")
    return decoded


def _string_list(value: object) -> list[str]:
    decoded = _strict_json(value)
    if not isinstance(decoded, list) or not all(type(item) is str for item in decoded):
        raise ValueError("database JSON string list is invalid")
    return decoded


def _workspace_root_tokens(workspace: Workspace) -> tuple[str, ...]:
    root = str(workspace.root)
    variants = {root.casefold(), root.replace("\\", "/").casefold()}
    return tuple(value for value in variants if value)


def _reject_canonical_root(value: object, workspace: Workspace) -> None:
    tokens = _workspace_root_tokens(workspace)

    def walk(item: object) -> None:
        if isinstance(item, str):
            normalized = item.casefold()
            portable = item.replace("\\", "/").casefold()
            if any(token in normalized or token in portable for token in tokens):
                raise ValueError("public value contains a canonical workspace root")
        elif isinstance(item, Mapping):
            for key, child in item.items():
                walk(key)
                walk(child)
        elif isinstance(item, (list, tuple)):
            for child in item:
                walk(child)

    walk(value)


def _validated_now(clock: Callable[[], datetime]) -> datetime:
    value = clock()
    if (
        not isinstance(value, datetime)
        or value.tzinfo is None
        or value.utcoffset() is None
    ):
        raise ValueError("clock must return a timezone-aware datetime")
    return value.astimezone(timezone.utc)


def _validate_request(
    workspace: Workspace,
    request: ResourceReadRequest,
    *,
    kind: str,
    order_by: str,
) -> None:
    if not isinstance(workspace, Workspace):
        raise ValueError("workspace must be a registered Workspace")
    if not isinstance(workspace.root, Path):
        raise ValueError("workspace root must be a Path")
    if not isinstance(request, ResourceReadRequest):
        raise ValueError("request must be a ResourceReadRequest")
    if request.kind != kind or request.order_by != order_by:
        raise ValueError("resource reader request does not match its domain")
    if (
        isinstance(request.limit, bool)
        or not isinstance(request.limit, int)
        or request.limit < 1
        or request.limit > RESOURCE_FETCH_LIMIT
    ):
        raise ValueError("resource limit is outside the bounded read range")
    for value in (
        request.include_archived,
        request.include_deleted,
        request.include_expired,
    ):
        if type(value) is not bool:
            raise ValueError("resource inclusion flags must be boolean")
    if request.enabled_only is not None and type(request.enabled_only) is not bool:
        raise ValueError("enabled_only must be boolean or null")


T = TypeVar("T")


class SQLiteResourceRepository:
    """Read canonical and retained v7 resource state without blocking the loop."""

    def __init__(
        self,
        active_database_resolver: ActiveDatabaseResolver,
        *,
        clock: Callable[[], datetime] | None = None,
        timeout_seconds: float = 2.0,
        worker_pool: BoundedWorkerPool | None = None,
    ) -> None:
        if not callable(active_database_resolver):
            raise ValueError("active_database_resolver must be callable")
        if clock is not None and not callable(clock):
            raise ValueError("clock must be callable")
        if worker_pool is not None and not isinstance(worker_pool, BoundedWorkerPool):
            raise ValueError("worker_pool must be a BoundedWorkerPool")
        self._active_database_resolver = active_database_resolver
        self._clock = clock or _utc_clock
        self._timeout_seconds = _bounded_timeout(timeout_seconds)
        self._worker_pool = worker_pool or _RESOURCE_WORKERS

    async def read_warnings(
        self,
        workspace: Workspace,
        request: ResourceReadRequest,
    ) -> list[ResourceRow[RecordSummary]]:
        _validate_request(
            workspace,
            request,
            kind="warnings",
            order_by="updated_at_desc",
        )
        if request.enabled_only is not None or request.include_expired:
            raise ValueError("warning request contains unsupported flags")
        return await self._run(
            lambda: self._read_records_sync(
                workspace,
                request,
                record_type="warning",
                failed_only=False,
            )
        )

    async def read_failures(
        self,
        workspace: Workspace,
        request: ResourceReadRequest,
    ) -> list[ResourceRow[RecordSummary]]:
        _validate_request(
            workspace,
            request,
            kind="failures",
            order_by="updated_at_desc",
        )
        if request.enabled_only is not None or request.include_expired:
            raise ValueError("failure request contains unsupported flags")
        return await self._run(
            lambda: self._read_records_sync(
                workspace,
                request,
                record_type=None,
                failed_only=True,
            )
        )

    async def read_rules(
        self,
        workspace: Workspace,
        request: ResourceReadRequest,
    ) -> list[RuleView]:
        _validate_request(
            workspace,
            request,
            kind="rules",
            order_by="priority_desc",
        )
        if (
            request.include_archived
            or request.include_deleted
            or request.include_expired
        ):
            raise ValueError("rule request contains unsupported flags")
        return await self._run(lambda: self._read_rules_sync(workspace, request))

    async def read_active_context(
        self,
        workspace: Workspace,
        request: ResourceReadRequest,
    ) -> list[ResourceRow[ActiveContextItem]]:
        _validate_request(
            workspace,
            request,
            kind="active_context",
            order_by="priority_desc",
        )
        if request.enabled_only is not None:
            raise ValueError("active-context request contains unsupported flags")
        return await self._run(
            lambda: self._read_active_context_sync(workspace, request)
        )

    async def read_briefing_snapshot(
        self,
        workspace: Workspace,
        *,
        warning_limit: int,
        failure_limit: int,
        rule_limit: int = 50,
        active_context_limit: int = 50,
        include_git_changes: bool = True,
    ) -> ResourceRepositorySnapshot:
        """Read all briefing resources without a pointer-generation gap.

        Git changes cost two ``git`` subprocesses per read (plus a system-wide
        thread walk on Windows); callers that never show them skip them.
        """

        if not isinstance(workspace, Workspace):
            raise ValueError("workspace must be a registered Workspace")
        for name, value, allow_zero in (
            ("warning_limit", warning_limit, True),
            ("failure_limit", failure_limit, True),
            ("rule_limit", rule_limit, False),
            ("active_context_limit", active_context_limit, False),
        ):
            minimum = 0 if allow_zero else 1
            if (
                isinstance(value, bool)
                or not isinstance(value, int)
                or value < minimum
                or value > RESOURCE_FETCH_LIMIT
            ):
                raise ValueError(f"{name} is outside the bounded read range")
        warning_request = (
            None
            if warning_limit == 0
            else ResourceReadRequest("warnings", warning_limit, "updated_at_desc")
        )
        failure_request = (
            None
            if failure_limit == 0
            else ResourceReadRequest("failures", failure_limit, "updated_at_desc")
        )
        rule_request = ResourceReadRequest(
            "rules", rule_limit, "priority_desc", enabled_only=True
        )
        active_request = ResourceReadRequest(
            "active_context", active_context_limit, "priority_desc"
        )
        return await self._run(
            lambda: self._read_briefing_snapshot_sync(
                workspace,
                warning_request=warning_request,
                failure_request=failure_request,
                rule_request=rule_request,
                active_request=active_request,
                include_git_changes=include_git_changes is True,
            )
        )

    def _read_briefing_snapshot_sync(
        self,
        workspace: Workspace,
        *,
        warning_request: ResourceReadRequest | None,
        failure_request: ResourceReadRequest | None,
        rule_request: ResourceReadRequest,
        active_request: ResourceReadRequest,
        include_git_changes: bool = True,
    ) -> ResourceRepositorySnapshot:
        connection, storage_lock = self._open_connection(workspace)
        try:
            connection.execute("BEGIN")
            warnings = (
                []
                if warning_request is None
                else self._read_records_sync(
                    workspace,
                    warning_request,
                    record_type="warning",
                    failed_only=False,
                    _connection=connection,
                )
            )
            failures = (
                []
                if failure_request is None
                else self._read_records_sync(
                    workspace,
                    failure_request,
                    record_type=None,
                    failed_only=True,
                    _connection=connection,
                )
            )
            rules = self._read_rules_sync(
                workspace,
                rule_request,
                _connection=connection,
            )
            active_context = self._read_active_context_sync(
                workspace,
                active_request,
                _connection=connection,
            )
            decisions = self._read_records_sync(
                workspace,
                ResourceReadRequest("warnings", 50, "updated_at_desc"),
                record_type="decision",
                failed_only=False,
                _connection=connection,
            )
            projection_freshness, stale_projection_count = (
                self._read_projection_freshness_sync(
                    workspace,
                    connection,
                )
            )
            workspace_statistics = self._workspace_statistics_sync(
                workspace,
                connection,
                active_context_count=self._active_context_count_sync(
                    workspace,
                    connection,
                ),
                stale_projection_count=stale_projection_count,
            )
        finally:
            try:
                if connection.in_transaction:
                    connection.rollback()
                connection.close()
            finally:
                storage_lock.release()
        return ResourceRepositorySnapshot(
            warnings=warnings,
            failures=failures,
            rules=rules,
            active_context=active_context,
            decisions=decisions,
            git_changes=(
                self._read_git_changes_sync(workspace) if include_git_changes else []
            ),
            projection_freshness=projection_freshness,
            workspace_statistics=workspace_statistics,
            stale_projection_count=stale_projection_count,
        )

    @staticmethod
    def _read_git_output_sync(
        workspace: Workspace, arguments: list[str]
    ) -> bytes | None:
        creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        if os.name == "nt":
            creation_flags |= 0x00000004  # CREATE_SUSPENDED: contain before execution
        try:
            # A descendant may inherit captured pipe handles. On Windows,
            # captured-pipe timeout cleanup then waits indefinitely for
            # those pipes to close even after Git has been killed. A temporary
            # file keeps both process waiting and output consumption bounded.
            with tempfile.TemporaryFile() as output:
                process = subprocess.Popen(
                    [
                        "git",
                        "--no-optional-locks",
                        "-c",
                        "core.fsmonitor=false",
                        "-C",
                        str(workspace.root),
                        *arguments,
                    ],
                    stdin=subprocess.DEVNULL,
                    stdout=output,
                    stderr=subprocess.DEVNULL,
                    creationflags=creation_flags,
                    start_new_session=(os.name != "nt"),
                    env={**os.environ, "GIT_OPTIONAL_LOCKS": "0"},
                )
                job = None
                try:
                    job = _WindowsKillOnCloseJob(process)
                    job.resume(process.pid)
                    try:
                        process.wait(timeout=2.0)
                    except subprocess.TimeoutExpired:
                        return None
                    output.seek(0)
                    raw = output.read(_MAX_GIT_OUTPUT_BYTES + 1)
                finally:
                    if job is not None:
                        job.close()
                    if os.name != "nt":
                        # Even a successfully exited Git can leave descendants.
                        with suppress(ProcessLookupError):
                            getattr(os, "killpg")(process.pid, 9)  # noqa: B009 - absent in Windows stubs
                    if process.poll() is None:
                        process.kill()
                    process.wait(timeout=0.5)
        except (OSError, subprocess.SubprocessError):
            return None
        if process.returncode != 0 or len(raw) > _MAX_GIT_OUTPUT_BYTES:
            return None
        return raw

    @staticmethod
    def _read_git_changes_sync(workspace: Workspace) -> list[object]:
        # Porcelain paths are repository-relative even when Git runs from a
        # nested workspace. Filter at Git and again before exposing public paths.
        prefix_raw = SQLiteResourceRepository._read_git_output_sync(
            workspace, ["rev-parse", "--show-prefix"]
        )
        if prefix_raw is None:
            return []
        try:
            prefix = (
                prefix_raw.decode("utf-8", errors="strict")
                .removesuffix("\n")
                .removesuffix("\r")
            )
        except UnicodeDecodeError:
            return []
        if prefix and (
            not prefix.endswith("/")
            or not SQLiteResourceRepository._safe_relative_git_path(prefix[:-1])
        ):
            return []
        raw = SQLiteResourceRepository._read_git_output_sync(
            workspace,
            ["status", "--porcelain=v1", "-z", "--untracked-files=normal", "--", "."],
        )
        if raw is None:
            return []
        try:
            entries = raw.decode("utf-8", errors="strict").split("\x00")
        except UnicodeDecodeError:
            return []
        values: list[object] = []
        index = 0
        while index < len(entries) and len(values) < _MAX_GIT_CHANGES:
            entry = entries[index]
            index += 1
            if not entry:
                continue
            if len(entry) < 4 or entry[2] != " ":
                return []
            state = entry[:2]
            path = entry[3:]
            if "R" in state or "C" in state:
                if index >= len(entries) or not entries[index]:
                    return []
                index += 1
            if not path.startswith(prefix):
                continue
            path = path[len(prefix) :]
            if not SQLiteResourceRepository._safe_relative_git_path(path):
                continue
            if state == "??":
                status = "untracked"
            elif "U" in state or state in {"AA", "DD"}:
                status = "conflicted"
            elif "R" in state:
                status = "renamed"
            elif "D" in state:
                status = "deleted"
            elif "A" in state:
                status = "added"
            else:
                status = "modified"
            values.append({"relative_file_path": path, "status": status})
        return values

    @staticmethod
    def _safe_relative_git_path(value: str) -> bool:
        if (
            not value
            or len(value) > 1024
            or "\\" in value
            or "\x00" in value
            or value.startswith(("/", "~"))
        ):
            return False
        components = value.split("/")
        return (
            all(component not in {"", ".", ".."} for component in components)
            and posixpath.normpath(value) == value
        )

    @staticmethod
    def _read_projection_freshness_sync(
        workspace: Workspace,
        connection: sqlite3.Connection,
    ) -> tuple[list[object], int]:
        rows = connection.execute(
            "SELECT projection_name,generation,source_event_root_hash,"
            "COALESCE(activated_at_us,completed_at_us,started_at_us) AS built_at_us "
            "FROM projection_manifests WHERE workspace_id=? AND status='active' "
            "ORDER BY projection_name",
            (workspace.workspace_id,),
        ).fetchall()
        values: list[object] = []
        for row in rows:
            projection = row["projection_name"]
            root_hash = row["source_event_root_hash"]
            if (
                not isinstance(projection, str)
                or projection not in _PROJECTION_NAMES
                or not isinstance(root_hash, str)
                or _SHA256_RE.fullmatch(root_hash) is None
            ):
                raise ValueError("projection manifest is invalid")
            values.append(
                {
                    "projection": projection,
                    "generation": _plain_int(row["generation"]),
                    "built_at": _datetime_from_us(row["built_at_us"]),
                    "source_root_hash": root_hash,
                }
            )
        if len(values) > len(_PROJECTION_NAMES):
            raise ValueError("projection manifest set is invalid")
        state_rows = connection.execute(
            "SELECT projection_name,"
            "MAX(CASE WHEN status='active' THEN 1 ELSE 0 END) AS has_active,"
            "MAX(CASE WHEN status='active' AND "
            "json_extract(details_json,'$.rebuild_required_event_id') IS NOT NULL "
            "THEN 1 ELSE 0 END) AS marked_stale "
            "FROM projection_manifests WHERE workspace_id=? "
            "GROUP BY projection_name",
            (workspace.workspace_id,),
        ).fetchall()
        stale = 0
        for row in state_rows:
            if row["projection_name"] not in _PROJECTION_NAMES:
                raise ValueError("projection name is invalid")
            if (
                _plain_int(row["has_active"]) == 0
                or _plain_int(row["marked_stale"]) == 1
            ):
                stale += 1
        return values, stale

    def _active_context_count_sync(
        self,
        workspace: Workspace,
        connection: sqlite3.Connection,
    ) -> int:
        now = _validated_now(self._clock)
        now_text = now.isoformat()
        delta = now - _EPOCH
        now_us = (
            delta.days * 86_400_000_000 + delta.seconds * 1_000_000 + delta.microseconds
        )
        canonical = connection.execute(
            "SELECT COUNT(*) FROM active_context_entries ac "
            "JOIN memory_records record ON record.workspace_id=ac.workspace_id "
            "AND record.record_id=ac.record_id WHERE ac.workspace_id=? "
            "AND ac.removed_at_us IS NULL "
            "AND (ac.expires_at_us IS NULL OR ac.expires_at_us>?) "
            "AND record.record_type<>'legacy' AND record.archived=0 "
            "AND record.deleted_at_us IS NULL",
            (workspace.workspace_id, now_us),
        ).fetchone()
        legacy = connection.execute(
            "WITH ranked_legacy AS (SELECT record.record_id,"
            "ROW_NUMBER() OVER (PARTITION BY record.record_id "
            "ORDER BY ac.priority DESC,julianday(ac.added_at) DESC,"
            "ac.id DESC) AS duplicate_rank FROM active_context ac "
            "JOIN v7_migration_runs run ON run.workspace_id=? "
            "AND run.status='active' JOIN legacy_id_map map "
            "ON map.migration_run_id=run.migration_run_id "
            "AND map.workspace_id=? AND map.source_table='memories' "
            "AND map.target_kind='memory' "
            "AND map.legacy_id=CAST(ac.memory_id AS TEXT) "
            "JOIN memory_records record ON record.workspace_id=? "
            "AND record.record_id=map.target_id WHERE ac.project_path=? "
            "AND (ac.expires_at IS NULL OR julianday(ac.expires_at)>julianday(?)) "
            "AND record.record_type<>'legacy' AND record.archived=0 "
            "AND record.deleted_at_us IS NULL "
            "AND NOT EXISTS (SELECT 1 FROM active_context_entries canonical "
            "WHERE canonical.workspace_id=? "
            "AND canonical.record_id=record.record_id)) "
            "SELECT COUNT(*) FROM ranked_legacy WHERE duplicate_rank=1",
            (
                workspace.workspace_id,
                workspace.workspace_id,
                workspace.workspace_id,
                str(workspace.root),
                now_text,
                workspace.workspace_id,
            ),
        ).fetchone()
        if canonical is None or legacy is None:
            raise ValueError("active-context statistics are unavailable")
        return _plain_int(canonical[0]) + _plain_int(legacy[0])

    @staticmethod
    def _workspace_statistics_sync(
        workspace: Workspace,
        connection: sqlite3.Connection,
        *,
        active_context_count: int,
        stale_projection_count: int,
    ) -> dict[str, int]:
        record = connection.execute(
            "SELECT COUNT(*) AS records,"
            "SUM(CASE WHEN record_type='decision' THEN 1 ELSE 0 END) AS decisions,"
            "SUM(CASE WHEN record_type='warning' THEN 1 ELSE 0 END) AS warnings,"
            "SUM(CASE WHEN record_type='pattern' THEN 1 ELSE 0 END) AS patterns,"
            "SUM(CASE WHEN record_type='learning' THEN 1 ELSE 0 END) AS learnings,"
            "SUM(CASE WHEN worked=1 THEN 1 ELSE 0 END) AS successful_outcomes,"
            "SUM(CASE WHEN worked=0 THEN 1 ELSE 0 END) AS failed_outcomes,"
            "SUM(CASE WHEN archived=1 THEN 1 ELSE 0 END) AS archived_records "
            "FROM memory_records WHERE workspace_id=? AND record_type<>'legacy' "
            "AND deleted_at_us IS NULL",
            (workspace.workspace_id,),
        ).fetchone()
        rule = connection.execute(
            "SELECT COUNT(*) AS rules,"
            "SUM(CASE WHEN enabled=1 THEN 1 ELSE 0 END) AS enabled_rules "
            "FROM governance_rules WHERE workspace_id=?",
            (workspace.workspace_id,),
        ).fetchone()
        projection = connection.execute(
            "SELECT COUNT(*) FROM projection_manifests "
            "WHERE workspace_id=? AND status='active'",
            (workspace.workspace_id,),
        ).fetchone()
        if record is None or rule is None or projection is None:
            raise ValueError("workspace statistics are unavailable")
        return {
            "records": _plain_int(record["records"]),
            "decisions": _plain_int(record["decisions"] or 0),
            "warnings": _plain_int(record["warnings"] or 0),
            "patterns": _plain_int(record["patterns"] or 0),
            "learnings": _plain_int(record["learnings"] or 0),
            "successful_outcomes": _plain_int(record["successful_outcomes"] or 0),
            "failed_outcomes": _plain_int(record["failed_outcomes"] or 0),
            "archived_records": _plain_int(record["archived_records"] or 0),
            "rules": _plain_int(rule["rules"]),
            "enabled_rules": _plain_int(rule["enabled_rules"] or 0),
            "active_context": active_context_count,
            "active_projections": _plain_int(projection[0]),
            "stale_projections": stale_projection_count,
        }

    async def _run(self, operation: Callable[[], T]) -> T:
        worker = asyncio.create_task(self._worker_pool.run(operation))
        cancellation: asyncio.CancelledError | None = None
        timed_out = False
        try:
            return cast(
                T,
                await asyncio.wait_for(
                    asyncio.shield(worker),
                    timeout=self._timeout_seconds,
                ),
            )
        except asyncio.TimeoutError:
            timed_out = True
        except asyncio.CancelledError as exc:
            cancellation = exc
        except Exception:
            pass

        # Thread-pool work cannot be cancelled reliably after it starts.  A
        # timeout or caller cancellation therefore becomes observable only
        # after the admitted operation has released its SQLite connection and
        # shared storage-generation lock.  This prevents detached reads from
        # racing pointer activation or outliving their request.
        while not worker.done():
            try:
                await asyncio.shield(worker)
            except asyncio.CancelledError as exc:
                if cancellation is None:
                    cancellation = exc
                continue
            except Exception:
                break
        failure: BaseException | None = None
        if worker.done() and not worker.cancelled():
            failure = worker.exception()
        if cancellation is not None:
            raise cancellation
        # A read that overran its deadline but did not itself fail was slowed
        # by contention (lock waits, a saturated host), so it is retryable.
        if is_database_busy(failure) or (timed_out and failure is None):
            raise ResourceRepositoryError("DATABASE_IN_USE") from failure
        raise ResourceRepositoryError() from failure

    def _open_connection(
        self,
        workspace: Workspace,
    ) -> tuple[sqlite3.Connection, DatabaseFileLock]:
        connection: sqlite3.Connection | None = None
        try:
            root = normalize_resolved_path(workspace.root.resolve(strict=True))
            expected_storage = normalize_resolved_path(
                (root / ".daem0nmcp" / "storage").resolve(strict=True)
            )
        except (OSError, RuntimeError, ValueError) as exc:
            raise ValueError("workspace storage is unavailable") from exc

        storage_lock = DatabaseFileLock(expected_storage, "shared")
        storage_lock.acquire()
        try:
            selected = self._active_database_resolver(workspace)
            if not isinstance(selected, ResolvedActiveDatabase):
                raise TypeError(
                    "active database resolver returned an invalid selection"
                )
            if selected.format_version != 7:
                raise ValueError("active database is not architecture format 7")
            storage = normalize_resolved_path(
                selected.storage_path.resolve(strict=True)
            )
            database_path = normalize_resolved_path(selected.path.resolve(strict=True))
            database_path.relative_to(storage)
        except (OSError, RuntimeError, ValueError) as exc:
            storage_lock.release()
            raise ValueError(
                "active database selection is outside its workspace"
            ) from exc
        if storage != expected_storage or not database_path.is_file():
            storage_lock.release()
            raise ValueError("active database selection is outside its workspace")

        try:
            connection = sqlite3.connect(
                f"{database_path.as_uri()}?mode=ro",
                uri=True,
                timeout=self._timeout_seconds,
            )
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA query_only=ON")
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute(
                f"PRAGMA busy_timeout={int(self._timeout_seconds * 1_000)}"
            )
            self._validate_schema(connection)
        except Exception:
            if connection is not None:
                connection.close()
            storage_lock.release()
            raise
        return connection, storage_lock

    @staticmethod
    def _validate_schema(connection: sqlite3.Connection) -> None:
        tables = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        if not tables >= _REQUIRED_TABLES:
            raise ValueError("active database schema is incomplete")
        version_row = connection.execute(
            "SELECT COALESCE(MAX(version),0) FROM schema_version"
        ).fetchone()
        if version_row is None or _plain_int(version_row[0]) < CURRENT_SCHEMA_VERSION:
            raise ValueError("active database schema predates the v7 resource contract")
        query_only = connection.execute("PRAGMA query_only").fetchone()
        if query_only is None or _plain_int(query_only[0]) != 1:
            raise ValueError("resource database connection is not read only")

    def _read_records_sync(
        self,
        workspace: Workspace,
        request: ResourceReadRequest,
        *,
        record_type: str | None,
        failed_only: bool,
        _connection: sqlite3.Connection | None = None,
    ) -> list[ResourceRow[RecordSummary]]:
        owns_connection = _connection is None
        if _connection is None:
            connection, storage_lock = self._open_connection(workspace)
        else:
            connection = _connection
            storage_lock = None
        try:
            predicates = ["workspace_id=?", "record_type<>'legacy'"]
            parameters: list[object] = [workspace.workspace_id]
            if record_type is not None:
                predicates.append("record_type=?")
                parameters.append(record_type)
            if failed_only:
                predicates.append("worked=0")
            if not request.include_archived:
                predicates.append("archived=0")
            if not request.include_deleted:
                predicates.append("deleted_at_us IS NULL")
            parameters.append(request.limit)
            rows = connection.execute(
                "SELECT record_id,workspace_id,record_type,content,content_hash,"
                "tags_json,file_path_relative,archived,worked,created_at_us,"
                "updated_at_us,deleted_at_us FROM memory_records WHERE "
                + " AND ".join(predicates)
                + " ORDER BY updated_at_us DESC, record_id DESC LIMIT ?",
                tuple(parameters),
            ).fetchall()
            values: list[ResourceRow[RecordSummary]] = []
            for row in rows:
                if failed_only and _plain_int(row["worked"]) != 0:
                    raise ValueError("failed record state is invalid")
                item, deleted = self._record_summary(row, workspace)
                values.append(ResourceRow(item=item, deleted=deleted))
            return values
        finally:
            if owns_connection:
                try:
                    connection.close()
                finally:
                    assert storage_lock is not None
                    storage_lock.release()

    def _record_summary(
        self,
        row: Mapping[str, object],
        workspace: Workspace,
    ) -> tuple[RecordSummary, bool]:
        if row["workspace_id"] != workspace.workspace_id:
            raise ValueError("record belongs to another workspace")
        record_type = row["record_type"]
        if not isinstance(record_type, str) or record_type not in _PUBLIC_RECORD_TYPES:
            raise ValueError("record type is not public")
        content = row["content"]
        if not isinstance(content, str) or not content:
            raise ValueError("record content is invalid")
        tags = _string_list(row["tags_json"])
        archived = _flag(row["archived"])
        deleted = row["deleted_at_us"] is not None
        if deleted:
            _datetime_from_us(row["deleted_at_us"])
        relative_path = row["file_path_relative"]
        if relative_path is not None and not isinstance(relative_path, str):
            raise ValueError("relative file path is invalid")
        status = "archived" if archived else "invalidated" if deleted else "current"
        public_value = {
            "record_id": row["record_id"],
            "record_type": record_type,
            "excerpt": content[:4000],
            "tags": tags,
            "relative_file_path": relative_path,
            "current_status": status,
            "content_hash": row["content_hash"],
            "created_at": _datetime_from_us(row["created_at_us"]),
            "updated_at": _datetime_from_us(row["updated_at_us"]),
        }
        _reject_canonical_root(public_value, workspace)
        return RecordSummary.model_validate(public_value), deleted

    def _read_rules_sync(
        self,
        workspace: Workspace,
        request: ResourceReadRequest,
        *,
        _connection: sqlite3.Connection | None = None,
    ) -> list[RuleView]:
        owns_connection = _connection is None
        if _connection is None:
            connection, storage_lock = self._open_connection(workspace)
        else:
            connection = _connection
            storage_lock = None
        try:
            predicates: list[str] = ["workspace_id=?"]
            parameters: list[object] = [workspace.workspace_id]
            if request.enabled_only is True:
                predicates.append("enabled=1")
            where = " WHERE " + " AND ".join(predicates)
            parameters.append(request.limit)
            rows = connection.execute(
                "SELECT rule_id,workspace_id,trigger,must_do_json,must_not_json,"
                "ask_first_json,warnings_json,priority,enabled,created_at_us "
                "FROM governance_rules"
                + where
                + " ORDER BY priority DESC,created_at_us DESC,rule_id ASC LIMIT ?",
                tuple(parameters),
            ).fetchall()
            values: list[RuleView] = []
            for row in rows:
                if row["workspace_id"] != workspace.workspace_id:
                    raise ValueError("rule belongs to another workspace")
                public_value = {
                    "rule_id": row["rule_id"],
                    "trigger": row["trigger"],
                    "must_do": _string_list(row["must_do_json"]),
                    "must_not": _string_list(row["must_not_json"]),
                    "ask_first": _string_list(row["ask_first_json"]),
                    "warnings": _string_list(row["warnings_json"]),
                    "priority": _plain_int(row["priority"]),
                    "enabled": _flag(row["enabled"]),
                    "created_at": _datetime_from_us(row["created_at_us"]),
                }
                _reject_canonical_root(public_value, workspace)
                values.append(RuleView.model_validate(public_value))
            return values
        finally:
            if owns_connection:
                try:
                    connection.close()
                finally:
                    assert storage_lock is not None
                    storage_lock.release()

    def _read_active_context_sync(
        self,
        workspace: Workspace,
        request: ResourceReadRequest,
        *,
        _connection: sqlite3.Connection | None = None,
    ) -> list[ResourceRow[ActiveContextItem]]:
        owns_connection = _connection is None
        if _connection is None:
            connection, storage_lock = self._open_connection(workspace)
        else:
            connection = _connection
            storage_lock = None
        try:
            now = _validated_now(self._clock)
            now_text = now.isoformat()
            now_delta = now - _EPOCH
            now_us = (
                now_delta.days * 86_400_000_000
                + now_delta.seconds * 1_000_000
                + now_delta.microseconds
            )
            if now_us < 0 or now_us > 9_223_372_036_854_775_807:
                raise ValueError("clock timestamp is outside the storage range")

            canonical_orphan = connection.execute(
                "SELECT 1 FROM active_context_entries ac "
                "LEFT JOIN memory_records record "
                "ON record.workspace_id=ac.workspace_id "
                "AND record.record_id=ac.record_id "
                "WHERE ac.workspace_id=? AND record.record_id IS NULL LIMIT 1",
                (workspace.workspace_id,),
            ).fetchone()
            if canonical_orphan is not None:
                raise ValueError("canonical active-context binding is invalid")

            malformed = connection.execute(
                "SELECT 1 FROM active_context WHERE project_path=? AND "
                "(julianday(added_at) IS NULL OR "
                "(expires_at IS NOT NULL AND julianday(expires_at) IS NULL)) LIMIT 1",
                (str(workspace.root),),
            ).fetchone()
            if malformed is not None:
                raise ValueError("active-context timestamp is invalid")

            expiry_sql = (
                ""
                if request.include_expired
                else " AND (ac.expires_at IS NULL OR "
                "julianday(ac.expires_at)>julianday(?))"
            )
            expiry_parameters: list[object] = []
            if not request.include_expired:
                expiry_parameters.append(now_text)

            integrity = connection.execute(
                "SELECT ac.id FROM active_context ac "
                "LEFT JOIN v7_migration_runs run ON run.workspace_id=? "
                "AND run.status='active' "
                "LEFT JOIN legacy_id_map map ON map.migration_run_id="
                "run.migration_run_id AND map.workspace_id=? "
                "AND map.source_table='memories' AND map.target_kind='memory' "
                "AND map.legacy_id=CAST(ac.memory_id AS TEXT) "
                "LEFT JOIN memory_records record ON record.workspace_id=? "
                "AND record.record_id=map.target_id "
                "LEFT JOIN public_object_ids public ON "
                "public.workspace_id=? AND public.object_kind='active_context' "
                "AND public.source_key='i:' || CAST(ac.id AS TEXT) "
                "AND public.projection_generation=0 "
                "WHERE ac.project_path=?"
                + expiry_sql
                + " GROUP BY ac.id HAVING COUNT(map.target_id)<>1 "
                "OR COUNT(record.record_id)<>1 "
                "OR COUNT(public.public_id)<>1 LIMIT 1",
                (
                    workspace.workspace_id,
                    workspace.workspace_id,
                    workspace.workspace_id,
                    workspace.workspace_id,
                    str(workspace.root),
                    *expiry_parameters,
                ),
            ).fetchone()
            if integrity is not None:
                raise ValueError("active-context mapping is unavailable")

            canonical_predicates = [
                "ac.workspace_id=?",
                "ac.removed_at_us IS NULL",
                "record.record_type<>'legacy'",
            ]
            canonical_parameters: list[object] = [workspace.workspace_id]
            if not request.include_expired:
                canonical_predicates.append(
                    "(ac.expires_at_us IS NULL OR ac.expires_at_us>?)"
                )
                canonical_parameters.append(now_us)
            if not request.include_archived:
                canonical_predicates.append("record.archived=0")
            if not request.include_deleted:
                canonical_predicates.append("record.deleted_at_us IS NULL")
            canonical_rows = connection.execute(
                "SELECT ac.active_context_id AS active_id,"
                "ac.priority AS active_priority,ac.reason AS active_reason,"
                "ac.added_at_us AS active_added_at,"
                "ac.expires_at_us AS active_expires_at,record.record_id,"
                "record.workspace_id,record.record_type,record.content,"
                "record.content_hash,record.tags_json,record.file_path_relative,"
                "record.archived,record.worked,record.created_at_us,"
                "record.updated_at_us,record.deleted_at_us "
                "FROM active_context_entries ac JOIN memory_records record "
                "ON record.workspace_id=ac.workspace_id "
                "AND record.record_id=ac.record_id WHERE "
                + " AND ".join(canonical_predicates)
                + " ORDER BY ac.priority DESC,ac.added_at_us DESC,"
                "ac.active_context_id DESC LIMIT ?",
                (*canonical_parameters, request.limit),
            ).fetchall()

            legacy_predicates = [
                "ac.project_path=?",
                "record.record_type<>'legacy'",
            ]
            legacy_parameters: list[object] = [str(workspace.root)]
            if not request.include_expired:
                legacy_predicates.append(
                    "(ac.expires_at IS NULL OR julianday(ac.expires_at)>julianday(?))"
                )
                legacy_parameters.append(now_text)
            if not request.include_archived:
                legacy_predicates.append("record.archived=0")
            if not request.include_deleted:
                legacy_predicates.append("record.deleted_at_us IS NULL")
            legacy_predicates.append(
                "NOT EXISTS (SELECT 1 FROM active_context_entries canonical "
                "WHERE canonical.workspace_id=? "
                "AND canonical.record_id=record.record_id)"
            )
            legacy_parameters.append(workspace.workspace_id)
            legacy_rows = connection.execute(
                "WITH ranked_legacy AS (SELECT ac.id AS active_id,"
                "ac.priority AS active_priority,ac.reason AS active_reason,"
                "ac.added_at AS active_added_at,"
                "ac.expires_at AS active_expires_at,record.record_id,"
                "record.workspace_id,record.record_type,record.content,"
                "record.content_hash,record.tags_json,record.file_path_relative,"
                "record.archived,record.worked,record.created_at_us,"
                "record.updated_at_us,record.deleted_at_us,"
                "ROW_NUMBER() OVER (PARTITION BY record.record_id "
                "ORDER BY ac.priority DESC,julianday(ac.added_at) DESC,"
                "ac.id DESC) AS duplicate_rank "
                "FROM active_context ac "
                "JOIN v7_migration_runs run ON run.workspace_id=? "
                "AND run.status='active' "
                "JOIN legacy_id_map map ON map.migration_run_id=run.migration_run_id "
                "AND map.workspace_id=? AND map.source_table='memories' "
                "AND map.target_kind='memory' "
                "AND map.legacy_id=CAST(ac.memory_id AS TEXT) "
                "JOIN memory_records record ON record.workspace_id=? "
                "AND record.record_id=map.target_id WHERE "
                + " AND ".join(legacy_predicates)
                + ") SELECT active_id,active_priority,active_reason,"
                "active_added_at,active_expires_at,record_id,workspace_id,"
                "record_type,content,content_hash,tags_json,file_path_relative,"
                "archived,worked,created_at_us,updated_at_us,deleted_at_us "
                "FROM ranked_legacy WHERE duplicate_rank=1 "
                "ORDER BY active_priority DESC,julianday(active_added_at) DESC,"
                "active_id DESC LIMIT ?",
                (
                    workspace.workspace_id,
                    workspace.workspace_id,
                    workspace.workspace_id,
                    *legacy_parameters,
                    request.limit,
                ),
            ).fetchall()
            public_ids = PublicObjectIdRepository(connection)
            ordered_values: list[
                tuple[int, datetime, str, ResourceRow[ActiveContextItem]]
            ] = []
            for canonical, rows in (
                (True, canonical_rows),
                (False, legacy_rows),
            ):
                for row in rows:
                    record, deleted = self._record_summary(row, workspace)
                    if canonical:
                        active_context_id = row["active_id"]
                        added_at = _datetime_from_us(row["active_added_at"])
                        expires_at = (
                            None
                            if row["active_expires_at"] is None
                            else _datetime_from_us(row["active_expires_at"])
                        )
                    else:
                        active_context_id = public_ids.public_id_for_source(
                            workspace.workspace_id,
                            "active_context",
                            _plain_int(row["active_id"]),
                        )
                        added_at = _legacy_datetime(row["active_added_at"])
                        expires_at = (
                            None
                            if row["active_expires_at"] is None
                            else _legacy_datetime(row["active_expires_at"])
                        )
                    priority = _plain_int(row["active_priority"])
                    public_value = {
                        "active_context_id": active_context_id,
                        "record": record,
                        "priority": priority,
                        "reason": row["active_reason"],
                        "added_at": added_at,
                        "expires_at": expires_at,
                    }
                    _reject_canonical_root(public_value, workspace)
                    item = ActiveContextItem.model_validate(public_value)
                    ordered_values.append(
                        (
                            priority,
                            added_at,
                            item.active_context_id,
                            ResourceRow(item=item, deleted=deleted),
                        )
                    )

            ordered_values.sort(
                key=lambda value: (value[0], value[1], value[2]),
                reverse=True,
            )
            return [value[3] for value in ordered_values[: request.limit]]
        finally:
            if owns_connection:
                try:
                    connection.close()
                finally:
                    assert storage_lock is not None
                    storage_lock.release()


def build_sqlite_resource_readers(
    active_database_resolver: ActiveDatabaseResolver,
    *,
    clock: Callable[[], datetime] | None = None,
    timeout_seconds: float = 2.0,
    worker_pool: BoundedWorkerPool | None = None,
) -> ResourceRepositoryReaders:
    """Build the four reader callables expected by ``ResourceDependencies``."""

    repository = SQLiteResourceRepository(
        active_database_resolver,
        clock=clock,
        timeout_seconds=timeout_seconds,
        worker_pool=worker_pool,
    )
    return ResourceRepositoryReaders(
        warning_reader=repository.read_warnings,
        failure_reader=repository.read_failures,
        rule_reader=repository.read_rules,
        active_context_reader=repository.read_active_context,
        briefing_snapshot_reader=repository.read_briefing_snapshot,
    )


__all__ = [
    "ActiveDatabaseResolver",
    "ResourceRepositoryError",
    "ResourceRepositoryReaders",
    "ResourceRepositorySnapshot",
    "SQLiteResourceRepository",
    "build_sqlite_resource_readers",
]
