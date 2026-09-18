"""Durable, credential-free dispatch for MCP task-augmented tool calls."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import inspect
import ipaddress
import json
import math
import os
import re
import secrets
import sqlite3
from collections.abc import Awaitable, Callable, Mapping
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from types import MappingProxyType
from typing import Any, Literal
from urllib.parse import urlsplit

from pydantic import ValidationError

from ...covenant import CovenantGate, InvocationScope
from ...protected_files import (
    ensure_owner_only_directory,
    protect_owner_only_file,
    reject_linked_ancestry,
    write_new_owner_only_file,
)
from ...workspace import Workspace
from .registry import ToolSpec, V7Manifest
from .tasks import (
    DurableTaskExecution,
    await_task_terminal,
    durable_task_arguments_sha256,
    durable_task_execution_var,
)

_TASK_ID_RE = re.compile(r"^tsk_[0-9a-f]{64}$")
_ACTIVE_STATES = ("authorizing", "queued", "running", "cancel_requested")
_TERMINAL_STATES = ("completed", "failed", "cancelled")
_DEFAULT_TTL_MS = 3_600_000
_MIN_TTL_MS = 60_000
_MAX_TTL_MS = 86_400_000
_DEFAULT_EXECUTION_TIMEOUT_SECONDS = 300.0
_MAX_EXECUTION_TIMEOUT_SECONDS = 3_600.0
_QUEUED_RECONCILE_INTERVAL_US = 1_000_000
_QUEUED_RECONCILE_BATCH = 128
_INTERRUPTED_ADMISSION_MESSAGE = (
    "Admission was interrupted; obtain a new preflight and resubmit"
)
_CURSOR_RE = re.compile(r"^[A-Za-z0-9_-]+$")
TaskStatus = Literal["working", "completed", "failed", "cancelled"]


class TaskDispatcherError(RuntimeError):
    """Sanitized dispatcher failure for the MCP adapter."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class TaskView:
    task_id: str
    status: TaskStatus
    status_message: str | None
    created_at: datetime
    updated_at: datetime
    ttl_ms: int
    poll_interval_ms: int = 500


@dataclass(frozen=True, slots=True)
class TaskPage:
    tasks: tuple[TaskView, ...]
    next_cursor: str | None


@dataclass(frozen=True, slots=True)
class _StoredTask:
    task_id: str
    principal_id: str
    transport_session_id: str | None
    workspace_id: str
    canonical_workspace: str
    tool_name: str
    arguments: Mapping[str, Any]
    arguments_sha256: str
    state: str
    status_message: str | None
    result: Mapping[str, Any] | None
    created_at_us: int
    updated_at_us: int
    deadline_at_us: int
    ttl_ms: int
    replay_safe: bool


def validate_task_redis_url(value: object) -> str:
    """Accept only authenticated loopback Redis/Valkey endpoints."""

    if not isinstance(value, str) or not value.strip():
        raise ValueError("TASK_REDIS_URL_REQUIRED")
    candidate = value.strip()
    parsed = urlsplit(candidate)
    if parsed.scheme not in {"redis", "rediss"}:
        raise ValueError("TASK_REDIS_URL_INVALID")
    if not parsed.hostname or parsed.password is None or not parsed.password:
        raise ValueError("TASK_REDIS_AUTH_REQUIRED")
    if parsed.query or parsed.fragment:
        raise ValueError("TASK_REDIS_URL_INVALID")
    try:
        if not ipaddress.ip_address(parsed.hostname).is_loopback:
            raise ValueError("TASK_REDIS_LOOPBACK_REQUIRED")
    except ValueError as exc:
        if str(exc) == "TASK_REDIS_LOOPBACK_REQUIRED":
            raise
        if parsed.hostname.casefold() != "localhost":
            raise ValueError("TASK_REDIS_LOOPBACK_REQUIRED") from None
    return candidate


def _now_us() -> int:
    return int(datetime.now(timezone.utc).timestamp() * 1_000_000)


def _datetime_from_us(value: int) -> datetime:
    return datetime.fromtimestamp(value / 1_000_000, timezone.utc)


def _json_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def _canonical_root(workspace: Workspace) -> str:
    return os.path.normcase(str(Path(workspace.root).resolve()))


def _mcp_status(state: str) -> TaskStatus:
    if state == "completed":
        return "completed"
    if state == "failed":
        return "failed"
    if state == "cancelled":
        return "cancelled"
    return "working"


def _encode_cursor(created_at_us: int, task_id: str) -> str:
    encoded = _json_bytes([created_at_us, task_id])
    return base64.urlsafe_b64encode(encoded).decode("ascii").rstrip("=")


def _decode_cursor(cursor: str) -> tuple[int, str]:
    if not cursor or not _CURSOR_RE.fullmatch(cursor):
        raise TaskDispatcherError("INVALID_CURSOR")
    try:
        padding = "=" * (-len(cursor) % 4)
        decoded = base64.b64decode(
            cursor + padding,
            altchars=b"-_",
            validate=True,
        )
        value = json.loads(decoded)
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError):
        raise TaskDispatcherError("INVALID_CURSOR") from None
    if (
        not isinstance(value, list)
        or len(value) != 2
        or isinstance(value[0], bool)
        or not isinstance(value[0], int)
        or value[0] < 0
        or not isinstance(value[1], str)
        or not _TASK_ID_RE.fullmatch(value[1])
    ):
        raise TaskDispatcherError("INVALID_CURSOR")
    return value[0], value[1]


class DurableTaskDispatcher:
    """SQLite authority plus an opaque-ID Valkey wake-up queue."""

    def __init__(
        self,
        *,
        database_path: Path,
        redis_url: str,
        manifest: V7Manifest,
        covenant_gate: CovenantGate,
        workspace_resolver: object,
        max_workers: int = 2,
        max_pending: int = 1_000,
        max_pending_per_principal: int = 100,
        max_pending_per_workspace: int = 250,
        queue_name: str | None = None,
    ) -> None:
        if not isinstance(database_path, Path):
            raise TypeError("task database path must be a Path")
        if not isinstance(manifest, V7Manifest):
            raise TypeError("task manifest is required")
        resolver = getattr(workspace_resolver, "resolve", None)
        if not callable(resolver):
            raise TypeError("task workspace resolver is required")
        if isinstance(max_workers, bool) or not 1 <= max_workers <= 8:
            raise ValueError("task worker count must be between 1 and 8")
        if isinstance(max_pending, bool) or max_pending < 1:
            raise ValueError("task pending bound must be positive")
        for bound in (max_pending_per_principal, max_pending_per_workspace):
            if type(bound) is not int or bound < 1:
                raise ValueError("scoped task pending bounds must be positive integers")
        if queue_name is not None and (
            not isinstance(queue_name, str) or not queue_name
        ):
            raise ValueError("task queue name is required")

        reject_linked_ancestry(database_path)
        self._database_path = database_path.absolute()
        self._redis_url = validate_task_redis_url(redis_url)
        self._specs = MappingProxyType(
            {spec.name: spec for spec in manifest.tools if spec.task_mode == "optional"}
        )
        self._gate = covenant_gate
        self._workspace_resolver = workspace_resolver
        self._resolve_workspace = resolver
        self._max_workers = max_workers
        self._max_pending = max_pending
        self._max_pending_per_principal = max_pending_per_principal
        self._max_pending_per_workspace = max_pending_per_workspace
        self._admission_lock = asyncio.Lock()
        self._stop = asyncio.Event()
        self._loops: list[asyncio.Task[None]] = []
        self._active: dict[str, asyncio.Task[Mapping[str, Any]]] = {}
        self._redis_client: Any | None = None
        self._started = False
        self._queue_available = False
        self._last_prune_us = 0
        self._last_reconcile_us = 0
        self._loop_health: dict[str, dict[str, str | int]] = {}
        self._initialize_database()
        self._queue_name = queue_name or ("daem0nmcp:v7:tasks:" + self._authority_id())

    @property
    def database_path(self) -> Path:
        return self._database_path

    @property
    def queue_name(self) -> str:
        return self._queue_name

    @property
    def loop_health(self) -> Mapping[str, Mapping[str, str | int]]:
        return MappingProxyType(
            {
                name: MappingProxyType(dict(values))
                for name, values in self._loop_health.items()
            }
        )

    @property
    def is_ready(self) -> bool:
        return (
            self._started
            and not self._stop.is_set()
            and self._queue_available
            and bool(self._loops)
            and all(not task.done() for task in self._loops)
        )

    def scoped_health_counts(self, scope: InvocationScope) -> dict[str, int]:
        """Aggregate only the current principal's authorized workspace tasks."""
        if not self._gate.workspace_authorized(scope):
            raise TaskDispatcherError("TASK_NOT_FOUND")
        connection = self._connect()
        try:
            rows = connection.execute(
                "SELECT state,COUNT(*) FROM durable_tasks WHERE principal_id=? "
                "AND canonical_workspace=? GROUP BY state",
                (scope.principal_id, scope.canonical_workspace),
            ).fetchall()
            counts = {str(row[0]): int(row[1]) for row in rows}
        finally:
            connection.close()
        if not self._gate.workspace_authorized(scope):
            raise TaskDispatcherError("TASK_NOT_FOUND")
        return counts

    def _connect(self) -> sqlite3.Connection:
        self._protect_database_files()
        connection = sqlite3.connect(
            self._database_path,
            timeout=5,
            isolation_level=None,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=5000")
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    def _protect_database_files(self) -> None:
        ensure_owner_only_directory(self._database_path.parent)
        for path in (
            self._database_path,
            Path(f"{self._database_path}-wal"),
            Path(f"{self._database_path}-shm"),
        ):
            if path.exists():
                protect_owner_only_file(path)

    def _initialize_database(self) -> None:
        ensure_owner_only_directory(self._database_path.parent)
        if not self._database_path.exists():
            with suppress(FileExistsError):
                write_new_owner_only_file(self._database_path, b"")
        protect_owner_only_file(self._database_path)
        connection = self._connect()
        try:
            connection.executescript(
                """
                PRAGMA journal_mode=WAL;
                PRAGMA synchronous=FULL;
                CREATE TABLE IF NOT EXISTS durable_tasks (
                    task_id TEXT PRIMARY KEY,
                    principal_id TEXT NOT NULL,
                    transport_session_id TEXT,
                    workspace_id TEXT NOT NULL,
                    canonical_workspace TEXT NOT NULL,
                    tool_name TEXT NOT NULL,
                    arguments_json TEXT NOT NULL,
                    arguments_sha256 TEXT NOT NULL,
                    idempotency_key TEXT NOT NULL,
                    grant_digest TEXT,
                    state TEXT NOT NULL CHECK (state IN (
                        'authorizing','queued','running','cancel_requested',
                        'completed','failed','cancelled'
                    )),
                    status_message TEXT,
                    result_json TEXT,
                    created_at_us INTEGER NOT NULL,
                    updated_at_us INTEGER NOT NULL,
                    deadline_at_us INTEGER NOT NULL,
                    ttl_ms INTEGER NOT NULL,
                    replay_safe INTEGER NOT NULL CHECK (replay_safe IN (0,1)),
                    last_published_at_us INTEGER,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    UNIQUE(principal_id, workspace_id, tool_name, idempotency_key)
                );
                CREATE UNIQUE INDEX IF NOT EXISTS durable_tasks_grant
                    ON durable_tasks(grant_digest)
                    WHERE grant_digest IS NOT NULL;
                CREATE INDEX IF NOT EXISTS durable_tasks_owner
                    ON durable_tasks(principal_id, created_at_us DESC, task_id DESC);
                CREATE TABLE IF NOT EXISTS task_outbox (
                    task_id TEXT PRIMARY KEY REFERENCES durable_tasks(task_id)
                        ON DELETE CASCADE,
                    created_at_us INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS task_dispatcher_meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                """
            )
            columns = {
                str(row[1])
                for row in connection.execute("PRAGMA table_info(durable_tasks)")
            }
            if "transport_session_id" not in columns:
                # Rows admitted by an older binary cannot prove a current
                # transport identity and therefore fail live authorization.
                connection.execute(
                    "ALTER TABLE durable_tasks ADD COLUMN transport_session_id TEXT"
                )
            if "last_published_at_us" not in columns:
                connection.execute(
                    "ALTER TABLE durable_tasks ADD COLUMN last_published_at_us INTEGER"
                )
            connection.execute(
                "INSERT OR IGNORE INTO task_dispatcher_meta(key,value) "
                "VALUES ('authority_id',?)",
                (secrets.token_hex(16),),
            )
            self._protect_database_files()
        finally:
            connection.close()

    def _authority_id(self) -> str:
        connection = self._connect()
        try:
            row = connection.execute(
                "SELECT value FROM task_dispatcher_meta WHERE key='authority_id'"
            ).fetchone()
        finally:
            connection.close()
        if row is None or not re.fullmatch(r"[0-9a-f]{32}", str(row["value"])):
            raise RuntimeError("TASK_AUTHORITY_ID_INVALID")
        return str(row["value"])

    async def start(self) -> None:
        if self._started:
            return
        self._recover()
        self._stop.clear()
        self._started = True
        self._loops = [
            asyncio.create_task(self._supervise("outbox", self._outbox_loop))
        ]
        self._loops.extend(
            asyncio.create_task(self._supervise(f"worker-{index}", self._worker_loop))
            for index in range(self._max_workers)
        )

    async def _supervise(
        self,
        name: str,
        service: Callable[[], Awaitable[None]],
    ) -> None:
        restarts = 0
        delay = 0.05
        while not self._stop.is_set():
            self._loop_health[name] = {
                "status": "running",
                "restarts": restarts,
            }
            try:
                await service()
            except asyncio.CancelledError:
                raise
            except Exception:
                restarts += 1
                self._loop_health[name] = {
                    "status": "restarting",
                    "restarts": restarts,
                }
            else:
                if self._stop.is_set():
                    break
                restarts += 1
                self._loop_health[name] = {
                    "status": "restarting",
                    "restarts": restarts,
                }
            with suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self._stop.wait(), timeout=delay)
            delay = min(delay * 2, 1.0)
        self._loop_health[name] = {
            "status": "stopped",
            "restarts": restarts,
        }

    async def aclose(self) -> None:
        if not self._started:
            await self._close_redis()
            return
        self._stop.set()
        for child in tuple(self._active.values()):
            if not child.done():
                child.cancel()
        await asyncio.gather(*self._loops, return_exceptions=True)
        self._loops.clear()
        self._started = False
        await self._close_redis()

    close = aclose

    def _recover(self) -> None:
        now = _now_us()
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "UPDATE durable_tasks SET state='failed',updated_at_us=?,"
                "status_message=? "
                "WHERE state='authorizing'",
                (now, _INTERRUPTED_ADMISSION_MESSAGE),
            )
            connection.execute(
                "UPDATE durable_tasks SET state='cancelled',updated_at_us=?,"
                "status_message='Cancellation preserved across restart' "
                "WHERE state='cancel_requested'",
                (now,),
            )
            connection.execute(
                "UPDATE durable_tasks SET state='queued',updated_at_us=?,"
                "status_message='Recovered replay-safe execution' "
                "WHERE state='running' AND replay_safe=1",
                (now,),
            )
            connection.execute(
                "UPDATE durable_tasks SET state='failed',updated_at_us=?,"
                "status_message='Interrupted execution was not replay-safe' "
                "WHERE state='running' AND replay_safe=0",
                (now,),
            )
            connection.execute(
                "INSERT OR IGNORE INTO task_outbox(task_id,created_at_us) "
                "SELECT task_id,? FROM durable_tasks WHERE state='queued'",
                (now,),
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    async def _redis(self) -> Any:
        if self._redis_client is None:
            try:
                from redis.asyncio import Redis
            except ImportError as exc:
                raise TaskDispatcherError("TASKS_UNAVAILABLE") from exc
            self._redis_client = Redis.from_url(
                self._redis_url,
                decode_responses=True,
                socket_connect_timeout=1,
                socket_timeout=2,
                health_check_interval=15,
            )
        return self._redis_client

    async def _close_redis(self) -> None:
        self._queue_available = False
        client = self._redis_client
        self._redis_client = None
        if client is not None:
            with suppress(Exception):
                await client.aclose()

    async def _redis_failed(self) -> None:
        await self._close_redis()
        with suppress(asyncio.TimeoutError):
            await asyncio.wait_for(self._stop.wait(), timeout=0.2)

    async def _outbox_loop(self) -> None:
        while not self._stop.is_set():
            now = _now_us()
            if now - self._last_prune_us >= 60_000_000:
                self._prune_expired(now)
                self._last_prune_us = now
            if now - self._last_reconcile_us >= _QUEUED_RECONCILE_INTERVAL_US:
                self._reconcile_queued(now)
                self._last_reconcile_us = now
            row = self._next_outbox()
            if row is None:
                with suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(self._stop.wait(), timeout=0.1)
                continue
            task_id = str(row["task_id"])
            try:
                client = await self._redis()
                await self._publish_wakeup(client, task_id)
                self._queue_available = True
            except asyncio.CancelledError:
                raise
            except Exception:
                await self._redis_failed()
                continue
            self._record_publication(task_id, _now_us())

    async def _publish_wakeup(self, client: Any, task_id: str) -> None:
        await client.eval(
            "if redis.call('LPOS',KEYS[1],ARGV[1]) then return 0 end "
            "return redis.call('RPUSH',KEYS[1],ARGV[1])",
            1,
            self._queue_name,
            task_id,
        )

    def _reconcile_queued(self, now_us: int) -> None:
        cutoff = now_us - _QUEUED_RECONCILE_INTERVAL_US
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "INSERT OR IGNORE INTO task_outbox(task_id,created_at_us) "
                "SELECT task_id,? FROM durable_tasks "
                "WHERE state='queued' AND (last_published_at_us IS NULL "
                "OR last_published_at_us<=?) "
                "ORDER BY updated_at_us,task_id LIMIT ?",
                (now_us, cutoff, _QUEUED_RECONCILE_BATCH),
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _prune_expired(self, now_us: int) -> None:
        connection = self._connect()
        try:
            connection.execute(
                "DELETE FROM durable_tasks WHERE state IN ('completed','failed','cancelled') "
                "AND created_at_us + (ttl_ms * 1000) <= ?",
                (now_us,),
            )
        finally:
            connection.close()

    def _next_outbox(self) -> sqlite3.Row | None:
        connection = self._connect()
        try:
            return connection.execute(
                "SELECT task_id FROM task_outbox ORDER BY created_at_us,task_id LIMIT 1"
            ).fetchone()
        finally:
            connection.close()

    def _record_publication(self, task_id: str, published_at_us: int) -> None:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "UPDATE durable_tasks SET last_published_at_us=? "
                "WHERE task_id=? AND state='queued'",
                (published_at_us, task_id),
            )
            connection.execute(
                "DELETE FROM task_outbox WHERE task_id=?",
                (task_id,),
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    async def _worker_loop(self) -> None:
        while not self._stop.is_set():
            try:
                client = await self._redis()
                queued = await client.blpop(self._queue_name, timeout=1)
                self._queue_available = True
            except asyncio.CancelledError:
                raise
            except Exception:
                await self._redis_failed()
                continue
            if queued is None:
                continue
            task_id = queued[1]
            if isinstance(task_id, bytes):
                task_id = task_id.decode("ascii", "strict")
            if not isinstance(task_id, str) or not _TASK_ID_RE.fullmatch(task_id):
                continue
            if self._stop.is_set():
                self._restore_outbox(task_id)
                return
            try:
                stored = self._claim(task_id)
            except Exception:
                # BLPOP has already removed the wake-up. Restore durable
                # publication before the supervisor restarts this worker.
                try:
                    await self._publish_wakeup(client, task_id)
                except Exception:
                    with suppress(Exception):
                        self._restore_outbox(task_id)
                raise
            if stored is None:
                continue
            try:
                await self._execute(stored)
            except Exception:
                await self._recover_execution_fault(stored.task_id)

    def _restore_outbox(self, task_id: str) -> None:
        now = _now_us()
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "INSERT OR IGNORE INTO task_outbox(task_id,created_at_us) "
                "SELECT task_id,? FROM durable_tasks "
                "WHERE task_id=? AND state='queued'",
                (now, task_id),
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    async def _recover_execution_fault(self, task_id: str) -> None:
        delay = 0.05
        while not self._stop.is_set():
            try:
                self._recover_one_execution(task_id)
                return
            except Exception:
                with suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(self._stop.wait(), timeout=delay)
                delay = min(delay * 2, 1.0)

    def _recover_one_execution(self, task_id: str) -> None:
        now = _now_us()
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT state,replay_safe FROM durable_tasks WHERE task_id=?",
                (task_id,),
            ).fetchone()
            if row is None or str(row["state"]) in _TERMINAL_STATES:
                connection.rollback()
                return
            if row["state"] == "cancel_requested":
                connection.execute(
                    "UPDATE durable_tasks SET state='cancelled',updated_at_us=?,"
                    "status_message='Cancelled after worker interruption' "
                    "WHERE task_id=?",
                    (now, task_id),
                )
            elif bool(row["replay_safe"]):
                connection.execute(
                    "UPDATE durable_tasks SET state='queued',updated_at_us=?,"
                    "status_message='Recovered after worker interruption' "
                    "WHERE task_id=?",
                    (now, task_id),
                )
                connection.execute(
                    "INSERT OR IGNORE INTO task_outbox(task_id,created_at_us) "
                    "VALUES (?,?)",
                    (task_id, now),
                )
            else:
                connection.execute(
                    "UPDATE durable_tasks SET state='failed',updated_at_us=?,"
                    "status_message='Worker interruption was not replay-safe' "
                    "WHERE task_id=?",
                    (now, task_id),
                )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _claim(self, task_id: str) -> _StoredTask | None:
        now = _now_us()
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM durable_tasks WHERE task_id=?",
                (task_id,),
            ).fetchone()
            if row is None or row["state"] != "queued":
                connection.rollback()
                return None
            if now >= int(row["deadline_at_us"]):
                connection.execute(
                    "UPDATE durable_tasks SET state='failed',updated_at_us=?,"
                    "status_message='Execution deadline expired' WHERE task_id=?",
                    (now, task_id),
                )
                connection.commit()
                return None
            try:
                stored = self._stored_from_row(
                    row,
                    state="running",
                    updated_at_us=now,
                )
            except (TypeError, ValueError, json.JSONDecodeError):
                connection.execute(
                    "UPDATE durable_tasks SET state='failed',updated_at_us=?,"
                    "status_message='Persisted task record is invalid' "
                    "WHERE task_id=?",
                    (now, task_id),
                )
                connection.commit()
                return None
            connection.execute(
                "UPDATE durable_tasks SET state='running',updated_at_us=?,"
                "status_message='Running',attempts=attempts+1 WHERE task_id=?",
                (now, task_id),
            )
            connection.commit()
            return stored
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    async def _execute(self, stored: _StoredTask) -> None:
        spec = self._specs.get(stored.tool_name)
        if spec is None:
            self._finish(stored.task_id, "failed", None, "Tool unavailable")
            return
        remaining = max(0.0, (stored.deadline_at_us - _now_us()) / 1_000_000)

        start_gate = asyncio.Event()

        async def invoke() -> Mapping[str, Any]:
            await start_gate.wait()
            current_scope = InvocationScope(
                stored.principal_id,
                stored.transport_session_id or "durable-admission",
                stored.canonical_workspace,
            )
            if not self._gate.workspace_authorized(current_scope):
                raise TaskDispatcherError("UNAUTHORIZED_WORKSPACE")
            execution = DurableTaskExecution(
                task_id=stored.task_id,
                tool_name=stored.tool_name,
                workspace_id=stored.workspace_id,
                arguments_sha256=stored.arguments_sha256,
                principal_id=stored.principal_id,
                transport_session_id=stored.transport_session_id,
            )
            token = durable_task_execution_var.set(execution)
            try:
                result = spec.handler(**dict(stored.arguments))
                if inspect.isawaitable(result):
                    result = await result
                response = spec.output_model.model_validate(result)
                return response.model_dump(mode="json")
            finally:
                durable_task_execution_var.reset(token)

        child = asyncio.create_task(invoke())
        self._active[stored.task_id] = child
        try:
            if self._stop.is_set():
                child.cancel()
                with suppress(asyncio.CancelledError):
                    await await_task_terminal(child)
                self._settle_shutdown(stored.task_id)
                return
            state = self._task_state(stored.task_id)
            if state == "cancel_requested":
                child.cancel()
                with suppress(asyncio.CancelledError):
                    await await_task_terminal(child)
                self._finish(
                    stored.task_id,
                    "cancelled",
                    None,
                    "Cancelled before execution",
                )
                return
            if state != "running":
                child.cancel()
                with suppress(asyncio.CancelledError):
                    await await_task_terminal(child)
                return
            start_gate.set()
            try:
                result = await asyncio.wait_for(
                    asyncio.shield(child),
                    timeout=remaining,
                )
            except asyncio.TimeoutError:
                child.cancel()
                try:
                    result = await await_task_terminal(child)
                except (asyncio.CancelledError, Exception):
                    self._finish(
                        stored.task_id,
                        "failed",
                        None,
                        "Execution deadline exceeded",
                    )
                    return
            except asyncio.CancelledError:
                if not child.done():
                    child.cancel()
                try:
                    result = await await_task_terminal(child)
                except (asyncio.CancelledError, Exception):
                    if self._stop.is_set():
                        self._settle_shutdown(stored.task_id)
                    else:
                        self._finish(
                            stored.task_id,
                            "cancelled",
                            None,
                            "Cancelled",
                        )
                    return
            self._finish(stored.task_id, "completed", result, "Completed")
        except sqlite3.Error:
            raise
        except Exception:
            self._finish(stored.task_id, "failed", None, "Execution failed")
        finally:
            if not child.done():
                child.cancel()
                with suppress(asyncio.CancelledError, Exception):
                    await await_task_terminal(child)
            self._active.pop(stored.task_id, None)

    def _settle_shutdown(self, task_id: str) -> None:
        """Preserve accepted work when dispatcher lifecycle stops its child."""

        now = _now_us()
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT state,replay_safe FROM durable_tasks WHERE task_id=?",
                (task_id,),
            ).fetchone()
            if row is None or str(row["state"]) in _TERMINAL_STATES:
                connection.rollback()
                return
            state = str(row["state"])
            if state == "cancel_requested":
                connection.execute(
                    "UPDATE durable_tasks SET state='cancelled',updated_at_us=?,"
                    "status_message='Caller cancellation completed during shutdown' "
                    "WHERE task_id=?",
                    (now, task_id),
                )
            elif state == "running" and bool(row["replay_safe"]):
                connection.execute(
                    "UPDATE durable_tasks SET state='queued',updated_at_us=?,"
                    "status_message='Paused for dispatcher restart' "
                    "WHERE task_id=?",
                    (now, task_id),
                )
                connection.execute(
                    "INSERT OR IGNORE INTO task_outbox(task_id,created_at_us) "
                    "VALUES (?,?)",
                    (task_id, now),
                )
            elif state == "running":
                connection.execute(
                    "UPDATE durable_tasks SET state='failed',updated_at_us=?,"
                    "status_message='Shutdown interrupted a non-replay-safe execution' "
                    "WHERE task_id=?",
                    (now, task_id),
                )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _task_state(self, task_id: str) -> str:
        connection = self._connect()
        try:
            row = connection.execute(
                "SELECT state FROM durable_tasks WHERE task_id=?",
                (task_id,),
            ).fetchone()
        finally:
            connection.close()
        if row is None:
            raise TaskDispatcherError("TASK_NOT_FOUND")
        return str(row["state"])

    def _finish(
        self,
        task_id: str,
        state: str,
        result: Mapping[str, Any] | None,
        message: str,
    ) -> None:
        if state not in _TERMINAL_STATES:
            raise ValueError("terminal task state is invalid")
        encoded = None if result is None else _json_bytes(result).decode("utf-8")
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            current = connection.execute(
                "SELECT state FROM durable_tasks WHERE task_id=?",
                (task_id,),
            ).fetchone()
            if current is None or current["state"] in _TERMINAL_STATES:
                connection.rollback()
                return
            connection.execute(
                "UPDATE durable_tasks SET state=?,status_message=?,result_json=?,"
                "updated_at_us=? WHERE task_id=?",
                (state, message, encoded, _now_us(), task_id),
            )
            connection.execute("DELETE FROM task_outbox WHERE task_id=?", (task_id,))
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    async def submit(
        self,
        tool_name: str,
        arguments: Mapping[str, Any] | None,
        *,
        scope: InvocationScope | None,
        task_metadata: object,
    ) -> TaskView:
        spec = self._specs.get(tool_name)
        if spec is None:
            raise TaskDispatcherError("TASK_NOT_SUPPORTED")
        try:
            request = spec.input_model.model_validate(dict(arguments or {}))
        except (TypeError, ValueError, ValidationError) as exc:
            raise TaskDispatcherError("INVALID_ARGUMENT") from exc
        effective = request.model_dump(mode="json")
        workspace_id = effective.get("workspace_id")
        if not isinstance(workspace_id, str):
            raise TaskDispatcherError("UNAUTHORIZED_WORKSPACE")
        resolved = self._resolve_workspace(workspace_id)
        if inspect.isawaitable(resolved):
            resolved = await resolved
        if not isinstance(resolved, Workspace) or resolved.workspace_id != workspace_id:
            raise TaskDispatcherError("UNAUTHORIZED_WORKSPACE")
        if scope is None or scope.canonical_workspace != _canonical_root(resolved):
            raise TaskDispatcherError("IDENTITY_UNAVAILABLE")
        if not self._gate.workspace_authorized(scope):
            raise TaskDispatcherError("UNAUTHORIZED_WORKSPACE")

        ttl_ms, execution_timeout = self._validated_task_metadata(task_metadata)
        token = effective.get("preflight_token")
        token_value = token if isinstance(token, str) else None
        sanitized = {
            key: value for key, value in effective.items() if key != "preflight_token"
        }
        arguments_sha256 = durable_task_arguments_sha256(sanitized)
        operation_key = sanitized.get("idempotency_key")
        has_operation_key = isinstance(operation_key, str) and bool(operation_key)
        idempotency_key = (
            f"operation:{operation_key}"
            if has_operation_key
            else f"arguments:{arguments_sha256}"
        )
        replay_safe = bool(spec.annotations.get("readOnlyHint")) or has_operation_key
        if not replay_safe:
            raise TaskDispatcherError("IDEMPOTENCY_REQUIRED")

        async with self._admission_lock:
            existing = self._find_idempotent(
                scope.principal_id,
                workspace_id,
                tool_name,
                idempotency_key,
            )
            if existing is not None:
                if existing.arguments_sha256 != arguments_sha256:
                    raise TaskDispatcherError("IDEMPOTENCY_CONFLICT")
                if not self._gate.state_store.is_briefed(scope):
                    raise TaskDispatcherError("COMMUNION_REQUIRED")
                interrupted = existing.state == "authorizing" or (
                    existing.state == "failed"
                    and existing.status_message == _INTERRUPTED_ADMISSION_MESSAGE
                )
                if not interrupted:
                    return self._view(existing)
                # An authorizing row proves only that an exact grant was
                # presented. It cannot prove the process-local nonce was
                # consumed before an interruption. Require the replacement
                # request to authorize independently, then discard the
                # ambiguous reservation and perform admission from scratch.
                retry_violation = self._gate.authorize(
                    tool_name,
                    effective,
                    scope,
                    preflight_token=token_value,
                    consume_capability=False,
                )
                if retry_violation is not None:
                    raise TaskDispatcherError(
                        str(retry_violation.get("violation", "UNAUTHORIZED"))
                    )
                self._delete_interrupted(existing.task_id)

            violation = self._gate.authorize(
                tool_name,
                effective,
                scope,
                preflight_token=token_value,
                consume_capability=False,
            )
            if violation is not None:
                raise TaskDispatcherError(
                    str(violation.get("violation", "UNAUTHORIZED"))
                )
            pending = self._reserve(
                spec=spec,
                scope=scope,
                workspace_id=workspace_id,
                arguments=sanitized,
                arguments_sha256=arguments_sha256,
                idempotency_key=idempotency_key,
                token=token_value,
                ttl_ms=ttl_ms,
                execution_timeout=execution_timeout,
                replay_safe=replay_safe,
            )
            admission = asyncio.create_task(
                self._complete_admission(
                    pending,
                    effective=effective,
                    scope=scope,
                    token=token_value,
                )
            )
            try:
                return await asyncio.shield(admission)
            except asyncio.CancelledError:
                await await_task_terminal(admission)
                raise

    def _validated_task_metadata(self, metadata: object) -> tuple[int, float]:
        values: Mapping[str, Any]
        if metadata is None:
            values = {}
        elif isinstance(metadata, Mapping):
            values = metadata
        else:
            dump = getattr(metadata, "model_dump", None)
            if not callable(dump):
                raise TaskDispatcherError("INVALID_TASK_METADATA")
            values = dump(exclude_none=True)
        ttl = values.get("ttl", _DEFAULT_TTL_MS)
        timeout = values.get(
            "executionTimeout",
            _DEFAULT_EXECUTION_TIMEOUT_SECONDS,
        )
        if (
            isinstance(ttl, bool)
            or not isinstance(ttl, int)
            or not _MIN_TTL_MS <= ttl <= _MAX_TTL_MS
        ):
            raise TaskDispatcherError("INVALID_TASK_METADATA")
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)):
            raise TaskDispatcherError("INVALID_TASK_METADATA")
        timeout_value = float(timeout)
        if not math.isfinite(timeout_value) or not (
            1 <= timeout_value <= _MAX_EXECUTION_TIMEOUT_SECONDS
        ):
            raise TaskDispatcherError("INVALID_TASK_METADATA")
        return ttl, timeout_value

    def _reserve(
        self,
        *,
        spec: ToolSpec,
        scope: InvocationScope,
        workspace_id: str,
        arguments: Mapping[str, Any],
        arguments_sha256: str,
        idempotency_key: str,
        token: str | None,
        ttl_ms: int,
        execution_timeout: float,
        replay_safe: bool,
    ) -> _StoredTask:
        now = _now_us()
        task_id = "tsk_" + secrets.token_hex(32)
        grant_digest = (
            None
            if token is None
            else "token:" + hashlib.sha256(token.encode("utf-8")).hexdigest()
        )
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            active, principal_active, workspace_active = connection.execute(
                "SELECT COUNT(*), COALESCE(SUM(principal_id=?),0), "
                "COALESCE(SUM(workspace_id=?),0) "
                "FROM durable_tasks WHERE state IN (?,?,?,?)",
                (scope.principal_id, workspace_id, *_ACTIVE_STATES),
            ).fetchone()
            if (
                int(active) >= self._max_pending
                or int(principal_active) >= self._max_pending_per_principal
                or int(workspace_active) >= self._max_pending_per_workspace
            ):
                connection.rollback()
                raise TaskDispatcherError("TASK_QUEUE_FULL")
            connection.execute(
                "INSERT INTO durable_tasks("
                "task_id,principal_id,transport_session_id,workspace_id,"
                "canonical_workspace,tool_name,"
                "arguments_json,arguments_sha256,idempotency_key,grant_digest,state,"
                "status_message,result_json,created_at_us,updated_at_us,deadline_at_us,"
                "ttl_ms,replay_safe) VALUES "
                "(?,?,?,?,?,?,?,?,?,?,'authorizing',?,NULL,?,?,?,?,?)",
                (
                    task_id,
                    scope.principal_id,
                    scope.transport_session_id,
                    workspace_id,
                    scope.canonical_workspace,
                    spec.name,
                    _json_bytes(arguments).decode("utf-8"),
                    arguments_sha256,
                    idempotency_key,
                    grant_digest,
                    "Completing authorization",
                    now,
                    now,
                    now + int(execution_timeout * 1_000_000),
                    ttl_ms,
                    int(replay_safe),
                ),
            )
            connection.commit()
        except sqlite3.IntegrityError as exc:
            connection.rollback()
            raise TaskDispatcherError("IDEMPOTENCY_CONFLICT") from exc
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        return self._get_unscoped(task_id)

    async def _complete_admission(
        self,
        pending: _StoredTask,
        *,
        effective: Mapping[str, Any],
        scope: InvocationScope,
        token: str | None,
    ) -> TaskView:
        violation = self._gate.authorize(
            pending.tool_name,
            effective,
            scope,
            preflight_token=token,
            consume_capability=True,
        )
        if violation is not None:
            self._delete_pending(pending.task_id)
            raise TaskDispatcherError(str(violation.get("violation", "UNAUTHORIZED")))
        self._promote_authorizing(pending.task_id)
        return self._view(self._get_unscoped(pending.task_id))

    def _promote_authorizing(self, task_id: str) -> None:
        now = _now_us()
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "UPDATE durable_tasks SET state='queued',updated_at_us=?,"
                "status_message='Queued' WHERE task_id=? AND state='authorizing'",
                (now, task_id),
            )
            connection.execute(
                "INSERT OR IGNORE INTO task_outbox(task_id,created_at_us) VALUES (?,?)",
                (task_id, now),
            )
            connection.commit()
        except Exception:
            connection.rollback()
            # Recovery fails this unacknowledged reservation closed because the
            # process-local grant consumption cannot be proven after restart.
            raise
        finally:
            connection.close()

    def _delete_interrupted(self, task_id: str) -> None:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "DELETE FROM durable_tasks WHERE task_id=? AND ("
                "state='authorizing' OR (state='failed' AND status_message=?))",
                (task_id, _INTERRUPTED_ADMISSION_MESSAGE),
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _delete_pending(self, task_id: str) -> None:
        connection = self._connect()
        try:
            connection.execute(
                "DELETE FROM durable_tasks WHERE task_id=? AND state='authorizing'",
                (task_id,),
            )
        finally:
            connection.close()

    def _find_idempotent(
        self,
        principal_id: str,
        workspace_id: str,
        tool_name: str,
        idempotency_key: str,
    ) -> _StoredTask | None:
        connection = self._connect()
        try:
            row = connection.execute(
                "SELECT * FROM durable_tasks WHERE principal_id=? AND workspace_id=? "
                "AND tool_name=? AND idempotency_key=?",
                (principal_id, workspace_id, tool_name, idempotency_key),
            ).fetchone()
            return None if row is None else self._stored_from_row(row)
        finally:
            connection.close()

    def _get_unscoped(self, task_id: str) -> _StoredTask:
        if not _TASK_ID_RE.fullmatch(task_id):
            raise TaskDispatcherError("TASK_NOT_FOUND")
        connection = self._connect()
        try:
            row = connection.execute(
                "SELECT * FROM durable_tasks WHERE task_id=?",
                (task_id,),
            ).fetchone()
        finally:
            connection.close()
        if row is None:
            raise TaskDispatcherError("TASK_NOT_FOUND")
        return self._stored_from_row(row)

    def _stored_from_row(self, row: sqlite3.Row, **changes: Any) -> _StoredTask:
        arguments = json.loads(str(row["arguments_json"]))
        result = (
            None if row["result_json"] is None else json.loads(str(row["result_json"]))
        )
        values = {
            "task_id": str(row["task_id"]),
            "principal_id": str(row["principal_id"]),
            "transport_session_id": (
                None
                if row["transport_session_id"] is None
                else str(row["transport_session_id"])
            ),
            "workspace_id": str(row["workspace_id"]),
            "canonical_workspace": str(row["canonical_workspace"]),
            "tool_name": str(row["tool_name"]),
            "arguments": MappingProxyType(arguments),
            "arguments_sha256": str(row["arguments_sha256"]),
            "state": str(row["state"]),
            "status_message": row["status_message"],
            "result": None if result is None else MappingProxyType(result),
            "created_at_us": int(row["created_at_us"]),
            "updated_at_us": int(row["updated_at_us"]),
            "deadline_at_us": int(row["deadline_at_us"]),
            "ttl_ms": int(row["ttl_ms"]),
            "replay_safe": bool(row["replay_safe"]),
        }
        values.update(changes)
        return _StoredTask(**values)

    def _authorize_task_access(
        self,
        stored: _StoredTask,
        principal_id: str,
        transport_session_id: str,
    ) -> None:
        if stored.principal_id != principal_id:
            raise TaskDispatcherError("TASK_NOT_FOUND")
        scope = InvocationScope(
            principal_id,
            transport_session_id,
            stored.canonical_workspace,
        )
        if not self._gate.workspace_authorized(scope):
            raise TaskDispatcherError("TASK_NOT_FOUND")
        if not self._gate.state_store.is_briefed(scope):
            raise TaskDispatcherError("COMMUNION_REQUIRED")

    def _view(self, stored: _StoredTask) -> TaskView:
        return TaskView(
            task_id=stored.task_id,
            status=_mcp_status(stored.state),
            status_message=stored.status_message,
            created_at=_datetime_from_us(stored.created_at_us),
            updated_at=_datetime_from_us(stored.updated_at_us),
            ttl_ms=stored.ttl_ms,
        )

    def task_scope(
        self, task_id: str, principal_id: str, transport_session_id: str
    ) -> InvocationScope:
        """Return the authorized ledger scope for shared lifecycle quotas."""
        stored = self._get_unscoped(task_id)
        self._authorize_task_access(stored, principal_id, transport_session_id)
        return InvocationScope(
            principal_id, transport_session_id, stored.canonical_workspace
        )

    async def get_task(
        self,
        task_id: str,
        *,
        principal_id: str,
        transport_session_id: str,
    ) -> TaskView:
        stored = self._get_unscoped(task_id)
        self._authorize_task_access(stored, principal_id, transport_session_id)
        return self._view(stored)

    async def get_result(
        self,
        task_id: str,
        *,
        principal_id: str,
        transport_session_id: str,
    ) -> Mapping[str, Any]:
        stored = self._get_unscoped(task_id)
        self._authorize_task_access(stored, principal_id, transport_session_id)
        while stored.state not in _TERMINAL_STATES:
            await asyncio.sleep(0.05)
            stored = self._get_unscoped(task_id)
            self._authorize_task_access(stored, principal_id, transport_session_id)
        self._authorize_task_access(stored, principal_id, transport_session_id)
        if stored.state != "completed" or stored.result is None:
            raise TaskDispatcherError("TASK_RESULT_UNAVAILABLE")
        return stored.result

    async def cancel(
        self,
        task_id: str,
        *,
        principal_id: str,
        transport_session_id: str,
    ) -> TaskView:
        stored = self._get_unscoped(task_id)
        self._authorize_task_access(stored, principal_id, transport_session_id)
        now = _now_us()
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            current = connection.execute(
                "SELECT state FROM durable_tasks WHERE task_id=?",
                (task_id,),
            ).fetchone()
            state = str(current["state"])
            if state in {"authorizing", "queued"}:
                connection.execute(
                    "UPDATE durable_tasks SET state='cancelled',updated_at_us=?,"
                    "status_message='Cancelled before execution' WHERE task_id=?",
                    (now, task_id),
                )
                connection.execute(
                    "DELETE FROM task_outbox WHERE task_id=?",
                    (task_id,),
                )
            elif state == "running":
                connection.execute(
                    "UPDATE durable_tasks SET state='cancel_requested',updated_at_us=?,"
                    "status_message='Cancellation requested' WHERE task_id=?",
                    (now, task_id),
                )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        active = self._active.get(task_id)
        if active is not None and not active.done():
            active.cancel()
        current_task = self._get_unscoped(task_id)
        while current_task.state == "cancel_requested":
            await asyncio.sleep(0.01)
            current_task = self._get_unscoped(task_id)
            self._authorize_task_access(
                current_task, principal_id, transport_session_id
            )
        self._authorize_task_access(current_task, principal_id, transport_session_id)
        return self._view(current_task)

    async def list_tasks(
        self,
        *,
        principal_id: str,
        transport_session_id: str,
        cursor: str | None = None,
        limit: int = 100,
    ) -> TaskPage:
        if isinstance(limit, bool) or not 1 <= limit <= 100:
            raise TaskDispatcherError("INVALID_ARGUMENT")
        ordering_key = None if cursor is None else _decode_cursor(cursor)
        visible: list[_StoredTask] = []
        exhausted = False
        while len(visible) <= limit and not exhausted:
            connection = self._connect()
            try:
                if ordering_key is None:
                    rows = connection.execute(
                        "SELECT * FROM durable_tasks WHERE principal_id=? "
                        "ORDER BY created_at_us DESC,task_id DESC LIMIT 256",
                        (principal_id,),
                    ).fetchall()
                else:
                    rows = connection.execute(
                        "SELECT * FROM durable_tasks WHERE principal_id=? AND ("
                        "created_at_us < ? OR (created_at_us=? AND task_id < ?)) "
                        "ORDER BY created_at_us DESC,task_id DESC LIMIT 256",
                        (
                            principal_id,
                            ordering_key[0],
                            ordering_key[0],
                            ordering_key[1],
                        ),
                    ).fetchall()
            finally:
                connection.close()
            exhausted = len(rows) < 256
            if not rows:
                break
            for row in rows:
                stored = self._stored_from_row(row)
                ordering_key = (stored.created_at_us, stored.task_id)
                try:
                    self._authorize_task_access(
                        stored,
                        principal_id,
                        transport_session_id,
                    )
                except TaskDispatcherError:
                    continue
                visible.append(stored)
                if len(visible) > limit:
                    break
        page = visible[:limit]
        next_cursor = (
            _encode_cursor(page[-1].created_at_us, page[-1].task_id)
            if len(visible) > limit and page
            else None
        )
        return TaskPage(tuple(self._view(item) for item in page), next_cursor)


__all__ = [
    "DurableTaskDispatcher",
    "TaskDispatcherError",
    "TaskPage",
    "TaskView",
    "validate_task_redis_url",
]
