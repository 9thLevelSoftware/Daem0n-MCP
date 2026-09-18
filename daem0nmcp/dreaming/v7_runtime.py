"""Bounded production dreaming over authoritative format-7 workspace state.

This module intentionally has no dependency on the legacy dreaming managers.
Optional graph dependencies are imported only after the graph capability is ready.
"""

from __future__ import annotations

import asyncio
import base64
import json
import re
import sqlite3
import threading
import time
from _thread import LockType
from collections.abc import Callable, Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import datetime, timezone
from functools import partial
from pathlib import Path
from typing import Any, Literal

from ..api.v7.runtime_services import WorkspaceStorageResolver
from ..bounded_workers import BoundedWorkerBusyError, BoundedWorkerPool
from ..capture_candidates import (
    CaptureCandidateError,
    CaptureCandidateRequest,
    CaptureCandidateStore,
)
from ..config import Settings
from ..event_store import EventCommand, EventStore, deterministic_id, sha256_json
from ..schema_version import CURRENT_SCHEMA_VERSION
from ..workspace import Workspace

StrategyName = Literal[
    "failed_decision",
    "pending_outcome",
    "connection_discovery",
    "community_refresh",
]

STRATEGIES: tuple[StrategyName, ...] = (
    "failed_decision",
    "pending_outcome",
    "connection_discovery",
    "community_refresh",
)
_WORD = re.compile(r"[A-Za-z0-9_]{3,}")
_CURSOR = re.compile(r"^(\d{20}):(mem_[0-9a-f]{64})$")
_GRAPH_CURSOR = re.compile(
    r"^graph:(\d{1,20}):(-|[A-Za-z0-9_-]{43}):(-|[A-Za-z0-9_-]{43})$"
)
_MAX_CONTENT_CHARS = 1_024
_MAX_SOURCE_BYTES = 512 * 1_024
_MAX_EVIDENCE_ROWS = 64
_MAX_EVIDENCE_BYTES = 256 * 1_024
_MAX_EVIDENCE = 10
_MAX_GRAPH_MEMBERSHIPS = 4_096
_MAX_GRAPH_PAIR_CHECKS = 20_000
_ANALYSIS_SECONDS = 2.0


class V7DreamingError(RuntimeError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class _Record:
    record_id: str
    record_type: str
    content: str
    content_hash: str
    state_hash: str
    source_event_id: str
    stream_version: int
    created_at_us: int
    updated_at_us: int
    outcome: str | None
    worked: bool | None
    content_truncated: bool = False

    def identity(self) -> dict[str, object]:
        return {
            "record_id": self.record_id,
            "event_id": self.source_event_id,
            "content_hash": self.content_hash,
            "state_hash": self.state_hash,
        }


@dataclass(frozen=True, slots=True)
class _Proposal:
    request: CaptureCandidateRequest
    source_versions: tuple[tuple[str, str], ...]


@dataclass(frozen=True, slots=True)
class _OutcomeAction:
    source: _Record
    evidence: tuple[_Record, ...]
    worked: bool
    outcome_text: str
    identity_hash: str


@dataclass(frozen=True, slots=True)
class _Analysis:
    proposals: tuple[_Proposal, ...] = ()
    outcomes: tuple[_OutcomeAction, ...] = ()
    pending_count: int = 0
    cursor: str | None = None


@dataclass(slots=True)
class _WorkspaceRuntime:
    workspace: Workspace
    last_activity: float
    wake: asyncio.Event = field(default_factory=asyncio.Event)
    cancelled: threading.Event = field(default_factory=threading.Event)
    task: asyncio.Task[None] | None = None
    running: bool = False
    yielded: bool = False
    foreground_calls: int = 0
    available: bool = True
    activity_epoch: int = 0
    publication_lock: LockType = field(default_factory=threading.Lock)


def _now_us() -> int:
    return time.time_ns() // 1_000


def _open(path: Path, *, writable: bool) -> sqlite3.Connection:
    connection = sqlite3.connect(
        f"{path.resolve().as_uri()}?mode={'rw' if writable else 'ro'}",
        uri=True,
        timeout=5.0,
        isolation_level=None,
    )
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA busy_timeout=5000")
    connection.execute("PRAGMA foreign_keys=ON")
    if not writable:
        connection.execute("PRAGMA query_only=ON")
    version = connection.execute(
        "SELECT COALESCE(MAX(version),0) FROM schema_version"
    ).fetchone()
    if version is None or int(version[0]) < CURRENT_SCHEMA_VERSION:
        connection.close()
        raise V7DreamingError("DREAM_STORAGE_UNAVAILABLE")
    return connection


def _tokens(value: str) -> frozenset[str]:
    return frozenset(word.casefold() for word in _WORD.findall(value))


def _row_record(row: sqlite3.Row) -> _Record:
    worked = row["worked"]
    return _Record(
        record_id=str(row["record_id"]),
        record_type=str(row["record_type"]),
        content=str(row["content"]),
        content_hash=str(row["content_hash"]),
        state_hash=str(row["state_hash"]),
        source_event_id=str(row["source_event_id"]),
        stream_version=int(row["stream_version"]),
        created_at_us=int(row["created_at_us"]),
        updated_at_us=int(row["updated_at_us"]),
        outcome=None if row["outcome"] is None else str(row["outcome"]),
        worked=None if worked is None else bool(worked),
        content_truncated=(
            "content_bytes" in row.keys()  # noqa: SIM118 - sqlite3.Row membership checks values
            and int(row["content_bytes"]) > len(str(row["content"]).encode("utf-8"))
        ),
    )


def _related(
    connection: sqlite3.Connection,
    workspace_id: str,
    source: _Record,
    cancelled: threading.Event,
) -> tuple[tuple[_Record, ...], bool]:
    source_tokens = _tokens(source.content)
    ranked: list[tuple[int, int, str, _Record]] = []
    rows = connection.execute(
        "SELECT record_id,record_type,substr(content,1,?) AS content,"
        "length(CAST(content AS BLOB)) AS content_bytes,content_hash,state_hash,"
        "source_event_id,stream_version,created_at_us,updated_at_us,outcome,worked "
        "FROM memory_records WHERE workspace_id=? AND record_id<>? AND archived=0 "
        "AND deleted_at_us IS NULL ORDER BY updated_at_us DESC,record_id "
        "LIMIT ?",
        (_MAX_CONTENT_CHARS, workspace_id, source.record_id, _MAX_EVIDENCE_ROWS + 1),
    ).fetchall()
    complete = len(rows) <= _MAX_EVIDENCE_ROWS and not source.content_truncated
    admitted_bytes = 0
    for row in rows[:_MAX_EVIDENCE_ROWS]:
        if cancelled.is_set():
            return (), False
        candidate = _row_record(row)
        admitted_bytes += len(candidate.content.encode("utf-8"))
        if admitted_bytes > _MAX_EVIDENCE_BYTES:
            complete = False
            break
        complete = complete and not candidate.content_truncated
        overlap = len(source_tokens & _tokens(candidate.content))
        if overlap:
            ranked.append(
                (-overlap, -candidate.updated_at_us, candidate.record_id, candidate)
            )
    ranked.sort(key=lambda item: item[:3])
    return tuple(item[3] for item in ranked[:_MAX_EVIDENCE]), complete


def _cursor_value(updated_at_us: int, record_id: str) -> str:
    return f"{updated_at_us:020d}:{record_id}"


def _parse_cursor(value: str | None) -> tuple[int, str] | None:
    if value is None:
        return None
    match = _CURSOR.fullmatch(value)
    if match is None:
        return None
    return int(match[1]), match[2]


def _graph_record_token(record_id: str | None) -> str:
    if record_id is None:
        return "-"
    return (
        base64.urlsafe_b64encode(bytes.fromhex(record_id[4:]))
        .decode("ascii")
        .rstrip("=")
    )


def _graph_record_id(token: str) -> str | None:
    if token == "-":
        return None
    try:
        raw = base64.urlsafe_b64decode(token + "=")
    except (ValueError, TypeError):
        return None
    if len(raw) != 32:
        return None
    return "mem_" + raw.hex()


def _graph_cursor_value(
    generation: int,
    outer_record_id: str | None,
    inner_record_id: str | None,
) -> str:
    return (
        f"graph:{generation}:{_graph_record_token(outer_record_id)}:"
        f"{_graph_record_token(inner_record_id)}"
    )


def _stable_request(
    *,
    workspace_id: str,
    strategy: StrategyName,
    record_type: str,
    content: str,
    rationale: str,
    context: Mapping[str, Any],
    sources: Sequence[_Record],
    extra: Mapping[str, Any] | None = None,
) -> _Proposal:
    identities = [item.identity() for item in sources]
    provenance: dict[str, Any] = {
        "strategy": strategy,
        "strategy_version": 1,
        "sources": identities,
    }
    if extra:
        provenance.update(dict(extra))
    fingerprint = sha256_json(
        {"strategy": strategy, "workspace_id": workspace_id, "provenance": provenance}
    )
    request = CaptureCandidateRequest(
        source_kind="dreaming",
        record={
            "record_type": record_type,
            "content": content[:8_000],
            "rationale": rationale[:2_000],
            "context": dict(context),
            "tags": ["dreaming", strategy.replace("_", "-")],
        },
        provenance=provenance,
        idempotency_key=f"dream-{strategy}-{fingerprint[:64]}",
    )
    return _Proposal(
        request=request,
        source_versions=tuple(
            (item.record_id, item.source_event_id) for item in sources
        ),
    )


class V7DreamingCoordinator:
    """Production-owned, restart-safe idle analysis for registered workspaces."""

    def __init__(
        self,
        *,
        workspaces: Sequence[Workspace],
        settings: Settings,
        candidate_store: CaptureCandidateStore,
        storage_resolver: WorkspaceStorageResolver | None = None,
        capability_statuses: Mapping[str, str] | None = None,
        projection_scheduler: Callable[[Path], object] | None = None,
        clock_us: Callable[[], int] = _now_us,
    ) -> None:
        if not isinstance(settings, Settings):
            raise TypeError("settings must be Settings")
        unique = {workspace.workspace_id: workspace for workspace in workspaces}
        self._settings = settings
        self._candidates = candidate_store
        self._storage = storage_resolver or WorkspaceStorageResolver()
        self._capabilities = dict(capability_statuses or {})
        self._projection_scheduler = projection_scheduler
        self._clock_us = clock_us
        self._pool = BoundedWorkerPool(
            max_workers=settings.dream_max_concurrency,
            thread_name_prefix="daem0nmcp-v7-dream",
        )
        now = time.monotonic()
        self._states = {
            workspace_id: _WorkspaceRuntime(workspace, now)
            for workspace_id, workspace in sorted(unique.items())
        }
        self._semaphore = asyncio.Semaphore(settings.dream_max_concurrency)
        self._started = False
        self._closed = False
        self._lifecycle_lock = asyncio.Lock()
        self._publication_tasks: set[asyncio.Task[Any]] = set()

    async def start(self) -> None:
        async with self._lifecycle_lock:
            if self._started:
                return
            if self._closed:
                raise RuntimeError("dreaming coordinator is closed")
            self._started = True
            for state in self._states.values():
                try:
                    await self._initialize_state(state.workspace)
                except Exception:
                    state.available = False
                    continue
                if self._settings.dream_enabled:
                    state.task = asyncio.create_task(
                        self._workspace_loop(state),
                        name=f"v7-dream-{state.workspace.workspace_id}",
                    )

    def record_activity(self, workspace: Workspace, active: bool = True) -> None:
        """Signal admitted foreground activity without retaining caller identity."""

        state = self._states.get(workspace.workspace_id)
        if (
            state is None
            or not state.available
            or state.workspace.root != workspace.root
        ):
            return
        state.activity_epoch += 1
        if active:
            state.foreground_calls += 1
        else:
            state.foreground_calls = max(0, state.foreground_calls - 1)
        state.last_activity = time.monotonic()
        if active:
            state.yielded = state.running
            state.cancelled.set()
        state.wake.set()

    async def aclose(self) -> None:
        async with self._lifecycle_lock:
            if self._closed:
                return
            self._closed = True
            tasks = []
            for state in self._states.values():
                state.cancelled.set()
                state.wake.set()
                if state.task is not None:
                    state.task.cancel()
                    tasks.append(state.task)
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
            if self._publication_tasks:
                await asyncio.gather(
                    *(asyncio.shield(task) for task in tuple(self._publication_tasks)),
                    return_exceptions=True,
                )
            while self._pool.in_flight or any(
                state.running for state in self._states.values()
            ):
                await asyncio.sleep(0.01)
            self._pool.shutdown()

    async def health(self, workspace: Workspace | None) -> Mapping[str, Any] | None:
        if workspace is None:
            return None
        state = self._states.get(workspace.workspace_id)
        if (
            state is None
            or not state.available
            or state.workspace.root != workspace.root
        ):
            return None
        rows = await self._run_worker(lambda: self._read_state(workspace))
        return {
            "enabled": self._settings.dream_enabled,
            "running": state.running,
            "yielded": state.yielded,
            "strategies": rows,
        }

    async def run_once(self, workspace: Workspace) -> None:
        """Run one bounded pass; public for deterministic lifecycle tests."""

        state = self._states.get(workspace.workspace_id)
        if state is None:
            raise V7DreamingError("DREAM_WORKSPACE_UNAVAILABLE")
        if not state.available:
            raise V7DreamingError("DREAM_STORAGE_UNAVAILABLE")
        # Let foreground admission already queued on this loop publish its
        # activity before background work snapshots the epoch.
        await asyncio.sleep(0)
        admitted_epoch = state.activity_epoch
        async with self._semaphore:
            if (
                state.running
                or state.foreground_calls
                or state.activity_epoch != admitted_epoch
            ):
                return
            state.running = True
            state.yielded = False
            state.cancelled.clear()
            if state.foreground_calls or state.activity_epoch != admitted_epoch:
                state.cancelled.set()
                state.running = False
                return
            try:
                for strategy in STRATEGIES:
                    if state.cancelled.is_set() or self._closed:
                        state.yielded = True
                        graph_unavailable = (
                            strategy in {"connection_discovery", "community_refresh"}
                            and self._capabilities.get("graph") != "ready"
                        )
                        await self._set_state(
                            workspace,
                            strategy,
                            status="disabled" if graph_unavailable else "idle",
                            error=(
                                "GRAPH_CAPABILITY_UNAVAILABLE"
                                if graph_unavailable
                                else None
                            ),
                            yielded=True,
                        )
                        break
                    await self._run_strategy(state, strategy)
            finally:
                state.running = False
                state.last_activity = time.monotonic()

    async def _workspace_loop(self, state: _WorkspaceRuntime) -> None:
        timeout = self._settings.dream_idle_timeout
        while not self._closed:
            remaining = max(0.0, timeout - (time.monotonic() - state.last_activity))
            try:
                await asyncio.wait_for(state.wake.wait(), timeout=remaining)
                state.wake.clear()
                continue
            except asyncio.TimeoutError:
                pass
            if self._closed:
                break
            if state.foreground_calls:
                state.last_activity = time.monotonic()
                continue
            try:
                await self.run_once(state.workspace)
            except asyncio.CancelledError:
                raise
            except Exception:
                # A transient workspace/storage failure must not terminate the
                # owned scheduler or create a hot retry loop.
                state.last_activity = time.monotonic()

    async def _run_strategy(
        self, state: _WorkspaceRuntime, strategy: StrategyName
    ) -> None:
        workspace = state.workspace
        if (
            strategy in {"connection_discovery", "community_refresh"}
            and self._capabilities.get("graph") != "ready"
        ):
            await self._set_state(
                workspace,
                strategy,
                status="disabled",
                error="GRAPH_CAPABILITY_UNAVAILABLE",
            )
            return
        await self._set_state(workspace, strategy, status="running", started=True)
        try:
            analysis_cursor: str | None = None
            if strategy == "community_refresh":
                pending = await self._run_worker(
                    lambda: self._refresh_communities(workspace, state.cancelled)
                )
                if pending and self._projection_scheduler is not None:
                    with self._storage.locked_active(workspace) as active:
                        self._projection_scheduler(active.path)
            else:
                analysis = await self._run_worker(
                    lambda: self._analyze(workspace, strategy, state.cancelled)
                )
                analysis_cursor = analysis.cursor
                for action in analysis.outcomes:
                    if state.cancelled.is_set():
                        break
                    committed_source = await self._run_worker(
                        partial(
                            self._commit_outcome,
                            workspace,
                            action,
                            state.cancelled,
                            state.publication_lock,
                        )
                    )
                    proposal = self._outcome_proposal(
                        workspace,
                        action,
                        committed_source=committed_source,
                    )
                    await self._stage_validated(
                        workspace,
                        proposal,
                        state.cancelled,
                        state.publication_lock,
                    )
                    if (
                        committed_source is not None
                        and self._projection_scheduler is not None
                    ):
                        with self._storage.locked_active(workspace) as active:
                            self._projection_scheduler(active.path)
                for proposal in analysis.proposals:
                    if state.cancelled.is_set():
                        break
                    await self._stage_validated(
                        workspace,
                        proposal,
                        state.cancelled,
                        state.publication_lock,
                    )
                pending = analysis.pending_count
            if state.cancelled.is_set():
                state.yielded = True
                await self._set_state(
                    workspace, strategy, status="idle", pending=pending, yielded=True
                )
            else:
                await self._set_state(
                    workspace,
                    strategy,
                    status="idle",
                    pending=pending,
                    success=True,
                    cursor=analysis_cursor,
                )
        except (asyncio.CancelledError, GeneratorExit):
            raise
        except BoundedWorkerBusyError:
            await self._set_state(
                workspace, strategy, status="degraded", error="DREAM_CAPACITY_BUSY"
            )
        except Exception as error:
            code = getattr(error, "code", "DREAM_STRATEGY_FAILED")
            if not isinstance(code, str) or not re.fullmatch(r"[A-Z0-9_]{1,64}", code):
                code = "DREAM_STRATEGY_FAILED"
            await self._set_state(workspace, strategy, status="degraded", error=code)

    async def _run_worker(self, operation: Callable[[], Any]) -> Any:
        return await self._pool.run(operation)

    def _analyze(
        self,
        workspace: Workspace,
        strategy: StrategyName,
        cancelled: threading.Event,
    ) -> _Analysis:
        with self._storage.locked_active(workspace) as active:
            connection = _open(active.path, writable=False)
            deadline = time.monotonic() + _ANALYSIS_SECONDS
            connection.set_progress_handler(
                lambda: int(cancelled.is_set() or time.monotonic() >= deadline),
                1_000,
            )
            cursor: str | None = None
            try:
                cursor_row = connection.execute(
                    "SELECT cursor FROM dreaming_strategy_state WHERE workspace_id=? "
                    "AND strategy=?",
                    (workspace.workspace_id, strategy),
                ).fetchone()
                cursor = (
                    None if cursor_row is None else str(cursor_row[0] or "") or None
                )
                if strategy == "failed_decision":
                    return self._failed_analysis(
                        connection, workspace, cursor, cancelled
                    )
                if strategy == "pending_outcome":
                    return self._pending_analysis(
                        connection, workspace, cursor, cancelled
                    )
                return self._connection_analysis(
                    connection, workspace, cursor, cancelled
                )
            except sqlite3.OperationalError as error:
                if getattr(error, "sqlite_errorcode", None) == sqlite3.SQLITE_INTERRUPT:
                    return _Analysis(pending_count=1, cursor=cursor)
                raise
            finally:
                connection.set_progress_handler(None, 0)
                connection.close()

    def _select_decisions(
        self,
        connection: sqlite3.Connection,
        workspace: Workspace,
        *,
        strategy: Literal["failed_decision", "pending_outcome"],
        cursor: str | None,
        cancelled: threading.Event,
    ) -> tuple[tuple[_Record, ...], str | None, bool]:
        now = self._clock_us()
        if strategy == "failed_decision":
            age_hours = self._settings.dream_min_decision_age_hours
            cooldown_hours = self._settings.dream_review_cooldown_hours
            limit = self._settings.dream_max_decisions_per_session
            outcome_clause = "worked=0"
        else:
            age_hours = self._settings.dream_pending_min_age_hours
            cooldown_hours = self._settings.dream_pending_cooldown_hours
            limit = self._settings.dream_pending_max_per_session
            outcome_clause = "worked IS NULL AND outcome IS NULL"
        cutoff = now - age_hours * 3_600_000_000
        cooldown_cutoff = now - cooldown_hours * 3_600_000_000
        parsed = _parse_cursor(cursor)
        select = (
            "SELECT record_id,record_type,substr(content,1,?) AS content,"
            "length(CAST(content AS BLOB)) AS content_bytes,content_hash,state_hash,"
            "source_event_id,stream_version,created_at_us,updated_at_us,outcome,worked "
            "FROM memory_records AS source WHERE workspace_id=? AND record_type='decision' "
            "AND archived=0 AND deleted_at_us IS NULL AND created_at_us<=? AND "
            + outcome_clause
            + " AND NOT EXISTS (SELECT 1 FROM memory_capture_candidates candidate "
            "WHERE candidate.workspace_id=source.workspace_id "
            "AND candidate.source_kind='dreaming' AND candidate.created_at_us>=? "
            "AND json_extract(candidate.provenance_json,'$.strategy')=? "
            "AND json_extract(candidate.provenance_json,'$.sources[0].record_id')="
            "source.record_id) "
        )

        def fetch(after: tuple[int, str] | None) -> list[sqlite3.Row]:
            boundary = ""
            arguments: list[object] = [
                _MAX_CONTENT_CHARS,
                workspace.workspace_id,
                cutoff,
                cooldown_cutoff,
                strategy,
            ]
            if after is not None:
                boundary = "AND (updated_at_us>? OR (updated_at_us=? AND record_id>?)) "
                arguments.extend([after[0], after[0], after[1]])
            arguments.append(limit + 1)
            return connection.execute(
                select + boundary + "ORDER BY updated_at_us,record_id LIMIT ?",
                tuple(arguments),
            ).fetchall()

        def has_before(boundary: tuple[int, str]) -> bool:
            arguments: list[object] = [
                _MAX_CONTENT_CHARS,
                workspace.workspace_id,
                cutoff,
                cooldown_cutoff,
                strategy,
                boundary[0],
                boundary[0],
                boundary[1],
            ]
            return (
                connection.execute(
                    select
                    + "AND (updated_at_us<? OR (updated_at_us=? AND record_id<=?)) "
                    + "ORDER BY updated_at_us,record_id LIMIT 1",
                    tuple(arguments),
                ).fetchone()
                is not None
            )

        rows = fetch(parsed)
        wrapped = False
        if not rows and parsed is not None and not cancelled.is_set():
            rows = fetch(None)
            wrapped = True
        selected: list[_Record] = []
        admitted_bytes = 0
        for row in rows[:limit]:
            if cancelled.is_set():
                break
            record = _row_record(row)
            encoded = len(record.content.encode("utf-8"))
            if admitted_bytes + encoded > _MAX_SOURCE_BYTES:
                break
            admitted_bytes += encoded
            selected.append(record)
        next_cursor = (
            cursor
            if not selected
            else _cursor_value(selected[-1].updated_at_us, selected[-1].record_id)
        )
        work_remaining = len(rows) > len(selected)
        if not work_remaining and parsed is not None and not wrapped:
            # Work before the prior cursor may remain after this tail slice.
            work_remaining = has_before(parsed)
        return tuple(selected), next_cursor, work_remaining

    def _failed_analysis(
        self,
        connection: sqlite3.Connection,
        workspace: Workspace,
        cursor: str | None,
        cancelled: threading.Event,
    ) -> _Analysis:
        selected, next_cursor, work_remaining = self._select_decisions(
            connection,
            workspace,
            strategy="failed_decision",
            cursor=cursor,
            cancelled=cancelled,
        )
        proposals: list[_Proposal] = []
        for source in selected:
            if cancelled.is_set():
                break
            evidence, complete = _related(
                connection,
                workspace.workspace_id,
                source,
                cancelled,
            )
            successful = tuple(item for item in evidence if item.worked is True)
            if successful:
                result = "successful_alternative"
                wording = "A failed decision has related successful evidence and should be reviewed for revision."
            elif len(evidence) < 2 or not complete:
                result = "insufficient_evidence"
                wording = "A failed decision lacks enough related evidence for a reliable re-evaluation."
            else:
                result = "confirmed_failure"
                wording = "Related evidence continues to support the recorded failure; review before reuse."
            sources = (source, *evidence)
            proposals.append(
                _stable_request(
                    workspace_id=workspace.workspace_id,
                    strategy="failed_decision",
                    record_type="learning",
                    content=f"{wording} Source: {source.content[:1000]}",
                    rationale=f"Deterministic failed-decision classification: {result}.",
                    context={
                        "classification": result,
                        "source_record_id": source.record_id,
                        "evidence_count": len(evidence),
                    },
                    sources=sources,
                )
            )
        return _Analysis(
            tuple(proposals),
            pending_count=int(work_remaining),
            cursor=next_cursor,
        )

    def _pending_analysis(
        self,
        connection: sqlite3.Connection,
        workspace: Workspace,
        cursor: str | None,
        cancelled: threading.Event,
    ) -> _Analysis:
        selected, next_cursor, work_remaining = self._select_decisions(
            connection,
            workspace,
            strategy="pending_outcome",
            cursor=cursor,
            cancelled=cancelled,
        )
        threshold = self._settings.dream_pending_evidence_threshold
        proposals: list[_Proposal] = []
        outcomes: list[_OutcomeAction] = []
        for source in selected:
            if cancelled.is_set():
                break
            evidence, complete = _related(
                connection,
                workspace.workspace_id,
                source,
                cancelled,
            )
            positive = tuple(item for item in evidence if item.worked is True)
            negative = tuple(item for item in evidence if item.worked is False)
            if positive and negative:
                classification = "mixed"
            elif complete and len(positive) >= threshold:
                classification = "unanimous_positive"
            elif complete and len(negative) >= threshold:
                classification = "unanimous_negative"
            else:
                classification = "insufficient"
            directional = (
                positive
                if classification == "unanimous_positive"
                else negative
                if classification == "unanimous_negative"
                else evidence
            )
            identity_hash = sha256_json(
                {
                    "source": source.identity(),
                    "evidence": [item.identity() for item in directional],
                    "classification": classification,
                }
            )
            if (
                classification.startswith("unanimous_")
                and not self._settings.dream_pending_dry_run
            ):
                worked = classification == "unanimous_positive"
                outcomes.append(
                    _OutcomeAction(
                        source,
                        tuple(directional),
                        worked,
                        "Automatically resolved from unanimous verified related evidence.",
                        identity_hash,
                    )
                )
                continue
            wording = (
                "Proposed outcome: related evidence is unanimously positive."
                if classification == "unanimous_positive"
                else "Proposed outcome: related evidence is unanimously negative."
                if classification == "unanimous_negative"
                else "Outcome review required because related evidence is mixed."
                if classification == "mixed"
                else "Outcome review requires more directional evidence."
            )
            proposals.append(
                _stable_request(
                    workspace_id=workspace.workspace_id,
                    strategy="pending_outcome",
                    record_type="learning",
                    content=f"{wording} Source: {source.content[:1000]}",
                    rationale=f"Pending outcome classification: {classification}; no canonical outcome was committed.",
                    context={
                        "classification": classification,
                        "committed": False,
                        "source_record_id": source.record_id,
                        "evidence_count": len(evidence),
                    },
                    sources=(source, *directional),
                )
            )
        return _Analysis(
            tuple(proposals),
            tuple(outcomes),
            int(work_remaining),
            next_cursor,
        )

    def _connection_analysis(
        self,
        connection: sqlite3.Connection,
        workspace: Workspace,
        cursor: str | None,
        cancelled: threading.Event,
    ) -> _Analysis:
        now = self._clock_us()
        cutoff = now - self._settings.dream_connection_lookback_hours * 3_600_000_000
        active = connection.execute(
            "SELECT generation FROM projection_manifests WHERE workspace_id=? "
            "AND projection_name='graph' AND status='active' LIMIT 1",
            (workspace.workspace_id,),
        ).fetchone()
        if active is None:
            return _Analysis(pending_count=0)
        generation = int(active[0])
        cursor_match = _GRAPH_CURSOR.fullmatch(cursor or "")
        if cursor_match is not None and int(cursor_match[1]) == generation:
            outer_cursor = _graph_record_id(cursor_match[2])
            inner_cursor = _graph_record_id(cursor_match[3])
        else:
            outer_cursor = None
            inner_cursor = None

        # An inner cursor means the outer record is still being traversed.
        # Otherwise the outer cursor is the last completely traversed record.
        if inner_cursor is None:
            source_boundary = "" if outer_cursor is None else "AND der.record_id>? "
            source_arguments: list[object] = [
                _MAX_CONTENT_CHARS,
                workspace.workspace_id,
                generation,
                cutoff,
            ]
            if outer_cursor is not None:
                source_arguments.append(outer_cursor)
        else:
            source_boundary = "AND der.record_id=? "
            source_arguments = [
                _MAX_CONTENT_CHARS,
                workspace.workspace_id,
                generation,
                cutoff,
                outer_cursor,
            ]
        source_arguments.append(_MAX_GRAPH_MEMBERSHIPS + 1)
        source_rows = connection.execute(
            "SELECT der.record_id,der.entity_id,de.normalized_name,"
            "mr.record_type,substr(mr.content,1,?) AS content,"
            "length(CAST(mr.content AS BLOB)) AS content_bytes,mr.content_hash,"
            "mr.state_hash,mr.source_event_id,mr.stream_version,mr.created_at_us,"
            "mr.updated_at_us,mr.outcome,mr.worked "
            "FROM discovery_entity_records der "
            "JOIN discovery_entities de ON de.workspace_id=der.workspace_id "
            "AND de.graph_generation=der.graph_generation AND de.entity_id=der.entity_id "
            "JOIN memory_records mr ON mr.workspace_id=der.workspace_id "
            "AND mr.record_id=der.record_id WHERE der.workspace_id=? "
            "AND der.graph_generation=? AND mr.updated_at_us>=? AND mr.archived=0 "
            "AND mr.deleted_at_us IS NULL "
            + source_boundary
            + "ORDER BY der.record_id,der.entity_id LIMIT ?",
            tuple(source_arguments),
        ).fetchall()
        if inner_cursor is not None and outer_cursor is not None:
            # Test doubles may not implement SQL predicates; production SQL
            # already applies this exact filter.
            source_rows = [row for row in source_rows if str(row[0]) == outer_cursor]
        if not source_rows and outer_cursor is not None and inner_cursor is None:
            return self._connection_analysis(connection, workspace, None, cancelled)
        if not source_rows:
            return _Analysis(pending_count=0, cursor=cursor)
        outer_record_id = str(source_rows[0][0])
        source_rows = [row for row in source_rows if str(row[0]) == outer_record_id]
        source = _row_record(source_rows[0])
        source_entities = {str(row[2]) for row in source_rows}
        if cancelled.is_set():
            return _Analysis(pending_count=1, cursor=cursor)

        peer_after = inner_cursor or outer_record_id
        arguments: list[object] = [
            _MAX_CONTENT_CHARS,
            workspace.workspace_id,
            generation,
            cutoff,
            workspace.workspace_id,
            generation,
            outer_record_id,
            peer_after,
            _MAX_GRAPH_MEMBERSHIPS + 1,
        ]
        peer_rows = connection.execute(
            "SELECT DISTINCT der.record_id,de.normalized_name AS entity_id,"
            "de.normalized_name,"
            "mr.record_type,substr(mr.content,1,?) AS content,"
            "length(CAST(mr.content AS BLOB)) AS content_bytes,mr.content_hash,"
            "mr.state_hash,mr.source_event_id,mr.stream_version,mr.created_at_us,"
            "mr.updated_at_us,mr.outcome,mr.worked "
            "FROM discovery_entity_records der "
            "JOIN discovery_entities de ON de.workspace_id=der.workspace_id "
            "AND de.graph_generation=der.graph_generation AND de.entity_id=der.entity_id "
            "JOIN memory_records mr ON mr.workspace_id=der.workspace_id "
            "AND mr.record_id=der.record_id WHERE der.workspace_id=? "
            "AND der.graph_generation=? AND mr.updated_at_us>=? AND mr.archived=0 "
            "AND mr.deleted_at_us IS NULL AND EXISTS ("
            "SELECT 1 FROM discovery_entity_records source "
            "WHERE source.workspace_id=? AND source.graph_generation=? "
            "AND source.record_id=? AND source.entity_id=der.entity_id) "
            "AND der.record_id>? ORDER BY der.record_id,de.normalized_name LIMIT ?",
            tuple(arguments),
        ).fetchall()
        # Preserve fake-connection compatibility while independently enforcing
        # the same peer and shared-entity conditions in Python.
        peer_rows = [
            row
            for row in peer_rows
            if str(row[0]) > peer_after
            and (isinstance(row, sqlite3.Row) or str(row[2]) in source_entities)
        ]
        minimum = self._settings.dream_connection_min_shared_entities
        truncated = len(peer_rows) > _MAX_GRAPH_MEMBERSHIPS
        admitted = peer_rows[:_MAX_GRAPH_MEMBERSHIPS]
        if truncated and admitted:
            partial_id = str(peer_rows[_MAX_GRAPH_MEMBERSHIPS][0])
            partial_rows = [row for row in admitted if str(row[0]) == partial_id]
            if len({str(row[2]) for row in partial_rows}) < minimum:
                admitted = [row for row in admitted if str(row[0]) != partial_id]
        grouped: dict[str, tuple[_Record, set[str]]] = {}
        for row in admitted:
            if cancelled.is_set():
                return _Analysis(pending_count=1, cursor=cursor)
            record_id = str(row[0])
            if record_id not in grouped:
                grouped[record_id] = (_row_record(row), set())
            grouped[record_id][1].add(str(row[2]))

        deadline = time.monotonic() + _ANALYSIS_SECONDS
        proposals: list[_Proposal] = []
        last_peer = peer_after
        stopped = False
        for inspected, (right, (peer, entities)) in enumerate(
            sorted(grouped.items()),
            start=1,
        ):
            if (
                cancelled.is_set()
                or inspected > _MAX_GRAPH_PAIR_CHECKS
                or time.monotonic() >= deadline
            ):
                stopped = True
                break
            last_peer = right
            shared = tuple(sorted(entities))
            if len(shared) < minimum:
                continue
            linked = connection.execute(
                "SELECT 1 FROM memory_relationship_versions WHERE workspace_id=? "
                "AND transaction_to_us IS NULL AND valid_to_us IS NULL AND "
                "((source_record_id=? AND target_record_id=?) OR "
                "(source_record_id=? AND target_record_id=?)) LIMIT 1",
                (
                    workspace.workspace_id,
                    outer_record_id,
                    right,
                    right,
                    outer_record_id,
                ),
            ).fetchone()
            if linked is not None:
                continue
            proposals.append(
                _stable_request(
                    workspace_id=workspace.workspace_id,
                    strategy="connection_discovery",
                    record_type="observation",
                    content=f"Proposed connection between two records sharing {len(shared)} active graph entities; review before creating a relationship.",
                    rationale="The active graph generation contains enough shared entity memberships.",
                    context={
                        "source_record_ids": [outer_record_id, right],
                        "shared_entities": list(shared[:32]),
                        "confidence": self._settings.dream_connection_confidence,
                        "committed": False,
                    },
                    sources=(source, peer),
                    extra={"graph_generation": generation},
                )
            )
            if len(proposals) >= self._settings.dream_connection_max_per_session:
                stopped = True
                break

        if stopped or truncated:
            next_cursor = _graph_cursor_value(
                generation,
                outer_record_id,
                last_peer,
            )
            work_remaining = True
        else:
            next_cursor = _graph_cursor_value(generation, outer_record_id, None)
            later = connection.execute(
                "SELECT 1 FROM discovery_entity_records der "
                "JOIN memory_records mr ON mr.workspace_id=der.workspace_id "
                "AND mr.record_id=der.record_id WHERE der.workspace_id=? "
                "AND der.graph_generation=? AND mr.updated_at_us>=? "
                "AND mr.archived=0 AND mr.deleted_at_us IS NULL "
                "AND der.record_id>? LIMIT ?",
                (workspace.workspace_id, generation, cutoff, outer_record_id, 1),
            ).fetchone()
            work_remaining = later is not None
        return _Analysis(
            tuple(proposals),
            pending_count=int(work_remaining),
            cursor=next_cursor,
        )

    def _refresh_communities(
        self, workspace: Workspace, cancelled: threading.Event
    ) -> int:
        with self._storage.locked_active(workspace) as active:
            connection = _open(active.path, writable=True)
            try:
                event_count = int(
                    connection.execute(
                        "SELECT COUNT(*) FROM memory_events WHERE workspace_id=?",
                        (workspace.workspace_id,),
                    ).fetchone()[0]
                )
                manifest = connection.execute(
                    "SELECT source_event_count FROM projection_manifests WHERE workspace_id=? "
                    "AND projection_name='graph' AND status='active' LIMIT 1",
                    (workspace.workspace_id,),
                ).fetchone()
                baseline = 0 if manifest is None else int(manifest[0])
                pending = max(0, event_count - baseline)
                if pending < self._settings.dream_community_staleness_threshold:
                    return pending
                from ..graph_projection import GraphProjectionBuilder

                GraphProjectionBuilder(connection).rebuild(
                    workspace.workspace_id,
                    force=False,
                    cancelled=cancelled,
                )
                return 0
            finally:
                connection.close()

    async def _stage_validated(
        self,
        workspace: Workspace,
        proposal: _Proposal,
        cancelled: threading.Event,
        publication_lock: LockType,
    ) -> None:
        if cancelled.is_set():
            return
        publication = asyncio.create_task(
            self._candidates.stage_validated(
                workspace,
                proposal.request,
                source_versions=proposal.source_versions,
                cancelled=cancelled,
                publication_lock=publication_lock,
            ),
            name=f"v7-dream-publish-{workspace.workspace_id}",
        )
        self._publication_tasks.add(publication)
        try:
            await asyncio.shield(publication)
        except asyncio.CancelledError:
            with suppress(Exception):
                await asyncio.shield(publication)
            raise
        except CaptureCandidateError as error:
            if error.code not in {"CAPTURE_SOURCE_STALE", "CAPTURE_CANCELLED"}:
                raise
        finally:
            if publication.done():
                self._publication_tasks.discard(publication)

    def _commit_outcome(
        self,
        workspace: Workspace,
        action: _OutcomeAction,
        cancelled: threading.Event,
        publication_lock: LockType,
    ) -> _Record | None:
        correlation_id = deterministic_id(
            "job", "v7-dream-outcome", workspace.workspace_id, action.identity_hash
        )
        request_hash = sha256_json(
            {
                "source": action.source.identity(),
                "evidence": [item.identity() for item in action.evidence],
                "worked": action.worked,
                "outcome": action.outcome_text,
            }
        )
        with self._storage.locked_active(workspace) as active:
            connection = _open(active.path, writable=True)
            try:
                connection.execute("BEGIN IMMEDIATE")
                if cancelled.is_set():
                    connection.rollback()
                    return None
                existing = connection.execute(
                    "SELECT event_id FROM memory_events WHERE workspace_id=? AND correlation_id=? "
                    "AND event_type='memory.outcome_recorded' LIMIT 1",
                    (workspace.workspace_id, correlation_id),
                ).fetchone()
                if existing is not None:
                    current = connection.execute(
                        "SELECT record_id,record_type,content,content_hash,state_hash,"
                        "source_event_id,stream_version,created_at_us,updated_at_us,outcome,worked "
                        "FROM memory_records WHERE workspace_id=? AND record_id=?",
                        (workspace.workspace_id, action.source.record_id),
                    ).fetchone()
                    connection.commit()
                    return None if current is None else _row_record(current)
                expected = (
                    (action.source.record_id, action.source.source_event_id),
                    *(
                        (item.record_id, item.source_event_id)
                        for item in action.evidence
                    ),
                )
                for record_id, event_id in expected:
                    row = connection.execute(
                        "SELECT source_event_id FROM memory_records WHERE workspace_id=? AND record_id=? "
                        "AND archived=0 AND deleted_at_us IS NULL",
                        (workspace.workspace_id, record_id),
                    ).fetchone()
                    if row is None or str(row[0]) != event_id:
                        connection.rollback()
                        return None
                if cancelled.is_set():
                    connection.rollback()
                    return None
                row = connection.execute(
                    "SELECT * FROM memory_records WHERE workspace_id=? AND record_id=?",
                    (workspace.workspace_id, action.source.record_id),
                ).fetchone()
                if (
                    row is None
                    or row["worked"] is not None
                    or row["outcome"] is not None
                ):
                    connection.rollback()
                    return None
                state = {
                    "record_type": row["record_type"],
                    "legacy_type": row["legacy_type"],
                    "content": row["content"],
                    "rationale": row["rationale"],
                    "context": json.loads(str(row["context_json"])),
                    "tags": json.loads(str(row["tags_json"])),
                    "file_path": row["file_path"],
                    "file_path_relative": row["file_path_relative"],
                    "keywords": row["keywords"],
                    "is_permanent": bool(row["is_permanent"]),
                    "pinned": bool(row["pinned"]),
                    "archived": bool(row["archived"]),
                    "outcome": action.outcome_text,
                    "worked": action.worked,
                    "recall_count": int(row["recall_count"]),
                    "surprise_score": row["surprise_score"],
                    "importance_score": row["importance_score"],
                    "source_client": row["source_client"],
                    "source_model": row["source_model"],
                    "deleted_at_us": row["deleted_at_us"],
                }
                now = self._clock_us()
                with publication_lock:
                    if cancelled.is_set():
                        connection.rollback()
                        return None
                    EventStore(connection, assume_transaction=True).append_and_project(
                        EventCommand(
                            workspace_id=workspace.workspace_id,
                            stream_id=action.source.record_id,
                            stream_kind="memory",
                            event_type="memory.outcome_recorded",
                            occurred_at_us=now,
                            recorded_at_us=now,
                            actor_type="system",
                            payload={
                                "record": state,
                                "idempotency_request_hash": request_hash,
                                "dreaming": {
                                    "strategy": "pending_outcome",
                                    "evidence_event_ids": [
                                        item.source_event_id for item in action.evidence
                                    ],
                                },
                            },
                            correlation_id=correlation_id,
                            expected_stream_version=int(row["stream_version"]) + 1,
                        )
                    )
                    current = connection.execute(
                        "SELECT record_id,record_type,content,content_hash,state_hash,"
                        "source_event_id,stream_version,created_at_us,updated_at_us,outcome,worked "
                        "FROM memory_records WHERE workspace_id=? AND record_id=?",
                        (workspace.workspace_id, action.source.record_id),
                    ).fetchone()
                    connection.commit()
                return None if current is None else _row_record(current)
            except Exception:
                if connection.in_transaction:
                    connection.rollback()
                raise
            finally:
                connection.close()

    def _outcome_proposal(
        self,
        workspace: Workspace,
        action: _OutcomeAction,
        *,
        committed_source: _Record | None,
    ) -> _Proposal:
        classification = "unanimous_positive" if action.worked else "unanimous_negative"
        committed = committed_source is not None
        wording = (
            "Canonical outcome committed from unanimous verified evidence."
            if committed
            else "Proposed outcome could not commit because authoritative evidence changed."
        )
        return _stable_request(
            workspace_id=workspace.workspace_id,
            strategy="pending_outcome",
            record_type="learning",
            content=f"{wording} Source: {action.source.content[:1000]}",
            rationale=f"Pending outcome classification: {classification}.",
            context={
                "classification": classification,
                "committed": committed,
                "source_record_id": action.source.record_id,
                "evidence_count": len(action.evidence),
            },
            sources=(committed_source or action.source, *action.evidence),
        )

    async def _initialize_state(self, workspace: Workspace) -> None:
        await self._run_worker(lambda: self._initialize_state_sync(workspace))

    def _initialize_state_sync(self, workspace: Workspace) -> None:
        now = self._clock_us()
        with self._storage.locked_active(workspace) as active:
            connection = _open(active.path, writable=True)
            try:
                connection.execute("BEGIN IMMEDIATE")
                for strategy in STRATEGIES:
                    disabled = not self._settings.dream_enabled or (
                        strategy in {"connection_discovery", "community_refresh"}
                        and self._capabilities.get("graph") != "ready"
                    )
                    disabled_code = (
                        "DREAMING_DISABLED"
                        if not self._settings.dream_enabled
                        else "GRAPH_CAPABILITY_UNAVAILABLE"
                    )
                    connection.execute(
                        "INSERT INTO dreaming_strategy_state(workspace_id,strategy,status,pending_count,stable_error_code,yielded,updated_at_us) "
                        "VALUES (?,?,?,?,?,?,?) ON CONFLICT(workspace_id,strategy) DO UPDATE SET "
                        "status=CASE WHEN excluded.status='disabled' THEN 'disabled' "
                        "WHEN dreaming_strategy_state.status IN ('running','disabled') "
                        "THEN 'idle' ELSE dreaming_strategy_state.status END,"
                        "stable_error_code=CASE WHEN excluded.status='disabled' "
                        "THEN excluded.stable_error_code "
                        "WHEN dreaming_strategy_state.status='disabled' THEN NULL "
                        "ELSE dreaming_strategy_state.stable_error_code END,"
                        "updated_at_us=excluded.updated_at_us",
                        (
                            workspace.workspace_id,
                            strategy,
                            "disabled" if disabled else "idle",
                            0,
                            disabled_code if disabled else None,
                            0,
                            now,
                        ),
                    )
                connection.commit()
            finally:
                connection.close()

    async def _set_state(
        self,
        workspace: Workspace,
        strategy: StrategyName,
        *,
        status: str,
        pending: int = 0,
        error: str | None = None,
        started: bool = False,
        success: bool = False,
        yielded: bool = False,
        cursor: str | None = None,
    ) -> None:
        await self._run_worker(
            lambda: self._set_state_sync(
                workspace,
                strategy,
                status=status,
                pending=pending,
                error=error,
                started=started,
                success=success,
                yielded=yielded,
                cursor=cursor,
            )
        )

    def _set_state_sync(
        self, workspace: Workspace, strategy: StrategyName, **values: Any
    ) -> None:
        now = self._clock_us()
        cooldown_hours = (
            self._settings.dream_review_cooldown_hours
            if strategy == "failed_decision"
            else self._settings.dream_pending_cooldown_hours
            if strategy == "pending_outcome"
            else 0
        )
        cooldown_until = now + cooldown_hours * 3_600_000_000
        with self._storage.locked_active(workspace) as active:
            connection = _open(active.path, writable=True)
            try:
                connection.execute(
                    "UPDATE dreaming_strategy_state SET status=?,pending_count=?,stable_error_code=?,yielded=?,"
                    "last_started_at_us=CASE WHEN ? THEN ? ELSE last_started_at_us END,"
                    "last_success_at_us=CASE WHEN ? THEN ? ELSE last_success_at_us END,"
                    "cursor=CASE WHEN ? THEN ? ELSE cursor END,"
                    "cooldown_until_us=CASE WHEN ? THEN ? ELSE cooldown_until_us END,"
                    "updated_at_us=? "
                    "WHERE workspace_id=? AND strategy=?",
                    (
                        values["status"],
                        min(10_000, max(0, int(values["pending"]))),
                        values["error"],
                        int(values["yielded"]),
                        int(values["started"]),
                        now,
                        int(values["success"]),
                        now,
                        int(values["success"]),
                        values["cursor"],
                        int(values["success"]),
                        cooldown_until,
                        now,
                        workspace.workspace_id,
                        strategy,
                    ),
                )
                connection.commit()
            finally:
                connection.close()

    def _read_state(self, workspace: Workspace) -> list[dict[str, Any]]:
        with self._storage.locked_active(workspace) as active:
            connection = _open(active.path, writable=False)
            try:
                rows = connection.execute(
                    "SELECT strategy,status,pending_count,last_success_at_us,stable_error_code,yielded "
                    "FROM dreaming_strategy_state WHERE workspace_id=? ORDER BY strategy",
                    (workspace.workspace_id,),
                ).fetchall()
                return [
                    {
                        "strategy": str(row[0]),
                        "status": str(row[1]),
                        "pending_count": int(row[2]),
                        "work_remaining": int(row[2]) > 0,
                        "last_success_at": None
                        if row[3] is None
                        else datetime.fromtimestamp(
                            int(row[3]) / 1_000_000, tz=timezone.utc
                        ),
                        "stable_error_code": None if row[4] is None else str(row[4]),
                        "yielded": bool(row[5]),
                    }
                    for row in rows
                ]
            finally:
                connection.close()


__all__ = ["STRATEGIES", "V7DreamingCoordinator", "V7DreamingError"]
