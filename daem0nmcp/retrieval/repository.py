"""Canonical, dependency-free SQLite reads for the retrieval service.

Policy reads intentionally omit canonical and projected prose.  Content is
hydrated only after the service has supplied the already policy-selected
candidate identities to :meth:`load_selected_evidence`.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import os
import re
import sqlite3
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from ..bounded_workers import BoundedWorkerBusyError, BoundedWorkerPool
from ..event_store import canonical_json_bytes, sha256_json
from .composer import SelectedEvidence
from .policy import PolicyRecord, apply_retrieval_policy
from .specialized_contract import (
    SPECIALIZED_PROJECTIONS,
    specialized_manifest_matches_contract,
)
from .types import EvidenceRef, FusedCandidate, RetrievalQuery, _aware_datetime

_HASH = re.compile(r"^[0-9a-f]{64}$")
_SUPPORTED_CHANNELS = frozenset(
    {"lexical", "dense", "temporal", "procedure", "outcome", "graph"}
)
_MAX_PROVIDER_FANOUT = len(_SUPPORTED_CHANNELS)
_REQUIRED_TABLES = frozenset(
    {
        "dense_projection_refs",
        "enrichment_decisions",
        "memory_events",
        "memory_fact_versions",
        "memory_records",
        "memory_relationship_versions",
        "projection_manifests",
        "record_outcome_view",
        "record_procedures",
        "retrieval_documents",
    }
)
_REBUILD_MARKERS = frozenset({"rebuild_required_at_us", "rebuild_required_event_id"})
_MAX_RELATION_PATH = 8
_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
_REPOSITORY_WORKERS = BoundedWorkerPool(
    max_workers=4,
    thread_name_prefix="daem0nmcp-repository",
)


class RetrievalRepositoryError(RuntimeError):
    """Sanitized fail-closed repository error safe for diagnostics."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class _EventRootCacheMissError(RuntimeError):
    """Internal signal that a read snapshot needs a freshly verified root."""


@dataclass(frozen=True, slots=True)
class _CachedEventRoot:
    revision: int
    value: tuple[int, str, int | None, str | None]


@dataclass(frozen=True, slots=True)
class _Manifest:
    generation: int
    row_count: int
    details: Mapping[str, object]
    marked_stale: bool = False


@dataclass(frozen=True, slots=True)
class _Event:
    event_id: str
    stream_id: str
    stream_kind: str
    stream_version: int
    recorded_at_us: int


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


def _database_path(value: object) -> Path:
    if not isinstance(value, (str, os.PathLike)):
        raise ValueError("database_path must identify a SQLite file")
    try:
        path = Path(value).expanduser().resolve()
    except (OSError, RuntimeError, ValueError) as exc:
        raise ValueError("database_path must identify a SQLite file") from exc
    if not path.is_file():
        raise ValueError("database_path must identify a SQLite file")
    return path


def sqlite_read_connection_factory(
    database_path: str | os.PathLike[str],
    *,
    busy_timeout_seconds: float = 2.0,
) -> Callable[[], sqlite3.Connection]:
    """Return a factory that opens a new read-only connection per worker."""

    path = _database_path(database_path)
    timeout = _bounded_timeout(busy_timeout_seconds)
    uri = f"{path.as_uri()}?mode=ro"

    def open_connection() -> sqlite3.Connection:
        return sqlite3.connect(uri, uri=True, timeout=timeout)

    return open_connection


def _datetime_us(value: datetime) -> int:
    delta = value.astimezone(timezone.utc) - _EPOCH
    result = (delta.days * 86_400 + delta.seconds) * 1_000_000 + delta.microseconds
    if result < -(2**63) or result > 2**63 - 1:
        raise ValueError("snapshot time is outside SQLite's supported range")
    return result


def _to_datetime(value: int | None) -> datetime | None:
    if value is None:
        return None
    try:
        return _EPOCH + timedelta(microseconds=value)
    except (OverflowError, TypeError, ValueError) as exc:
        raise RetrievalRepositoryError("POLICY_STATE_UNAVAILABLE") from exc


def _plain_int(value: object, *, minimum: int | None = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise RetrievalRepositoryError("POLICY_STATE_UNAVAILABLE")
    if minimum is not None and value < minimum:
        raise RetrievalRepositoryError("POLICY_STATE_UNAVAILABLE")
    return value


def _safe_hash(value: object) -> str:
    if not isinstance(value, str) or _HASH.fullmatch(value) is None:
        raise RetrievalRepositoryError("POLICY_STATE_UNAVAILABLE")
    return value


def _validated_candidates(
    query: RetrievalQuery,
    candidates: tuple[FusedCandidate, ...],
    snapshot_time: datetime,
    *,
    allow_merged_evidence: bool = False,
) -> tuple[FusedCandidate, ...]:
    if not isinstance(query, RetrievalQuery):
        raise ValueError("query must be a RetrievalQuery")
    if not isinstance(candidates, tuple) or not all(
        isinstance(candidate, FusedCandidate) for candidate in candidates
    ):
        raise ValueError("candidates must be a tuple of FusedCandidate values")
    maximum_candidates = query.candidate_limit * _MAX_PROVIDER_FANOUT
    if len(candidates) > maximum_candidates:
        raise ValueError("candidates exceed the bounded provider fanout")
    identities = tuple(
        (candidate.record_id, candidate.version_id) for candidate in candidates
    )
    if len(identities) != len(set(identities)):
        raise ValueError("candidate identities must be unique")
    if any(
        not candidate.channels.issubset(_SUPPORTED_CHANNELS)
        or any(
            evidence.provider not in _SUPPORTED_CHANNELS
            or (
                evidence.provider == "temporal"
                and (
                    evidence.version_id is None
                    or not evidence.version_id.startswith("fact_")
                )
            )
            or (evidence.provider != "temporal" and evidence.version_id is not None)
            or (
                not allow_merged_evidence
                and (
                    evidence.record_id != candidate.record_id
                    or evidence.version_id != candidate.version_id
                )
            )
            for evidence in candidate.evidence_refs
        )
        for candidate in candidates
    ):
        raise ValueError("candidate evidence is unsupported")
    _aware_datetime(snapshot_time, "snapshot_time")
    if snapshot_time is None:
        raise ValueError("snapshot_time is required")
    _datetime_us(snapshot_time)
    return candidates


class SQLiteRetrievalRepository:
    """Authoritative v7 policy metadata and selected-content repository."""

    def __init__(
        self,
        database_path: str | os.PathLike[str] | None = None,
        *,
        connection_factory: Callable[[], sqlite3.Connection] | None = None,
        timeout_seconds: float = 2.0,
        initialization_timeout_seconds: float = 15.0,
        worker_pool: BoundedWorkerPool | None = None,
        visibility_authorizer: Callable[[RetrievalQuery, str], bool] | None = None,
    ) -> None:
        timeout = _bounded_timeout(timeout_seconds)
        initialization_timeout = _bounded_timeout(initialization_timeout_seconds)
        if connection_factory is None:
            if database_path is None:
                raise ValueError("database_path or connection_factory must be supplied")
            path = _database_path(database_path)
            connection_factory = sqlite_read_connection_factory(
                path, busy_timeout_seconds=timeout
            )
            self.database_path: Path | None = path
        else:
            if database_path is not None or not callable(connection_factory):
                raise ValueError(
                    "provide exactly one database_path or connection_factory"
                )
            self.database_path = None
        if worker_pool is not None and not isinstance(worker_pool, BoundedWorkerPool):
            raise ValueError("worker_pool must be a BoundedWorkerPool")
        if visibility_authorizer is not None and not callable(visibility_authorizer):
            raise ValueError("visibility_authorizer must be callable")
        self._timeout_seconds = timeout
        self._initialization_timeout_seconds = initialization_timeout
        self._worker_pool = worker_pool or _REPOSITORY_WORKERS
        self._visibility_authorizer = (
            visibility_authorizer
            if visibility_authorizer is not None
            else lambda _query, visibility: visibility == "workspace"
        )
        assert connection_factory is not None
        self._connection_factory: Callable[[], sqlite3.Connection] = connection_factory
        self._revision_lock = threading.RLock()
        self._revision_connection: sqlite3.Connection | None = None
        self._event_root_cache: dict[str, _CachedEventRoot] = {}
        self._initialization_lock = threading.Lock()
        self._initializations: dict[str, asyncio.Task[None]] = {}
        self._lifecycle = threading.Condition()
        self._active_operations = 0
        self._closed = False

    def close(self) -> None:
        """Close the revision observer after admitted reads have finished."""

        with self._lifecycle:
            if self._closed:
                return
            self._closed = True
            while self._active_operations:
                self._lifecycle.wait()
        with self._revision_lock:
            connection = self._revision_connection
            self._revision_connection = None
            self._event_root_cache.clear()
            if connection is not None:
                connection.close()

    async def load_policy_records(
        self,
        query: RetrievalQuery,
        candidates: tuple[FusedCandidate, ...],
        *,
        snapshot_time: datetime,
    ) -> tuple[PolicyRecord, ...]:
        candidate_values = _validated_candidates(query, candidates, snapshot_time)
        if not candidate_values:
            return ()
        return await self._run_with_verified_root(
            query.workspace_id,
            lambda: self._load_policy_records_sync(
                query, candidate_values, snapshot_time
            ),
            "POLICY_STATE_UNAVAILABLE",
        )

    async def load_selected_evidence(
        self,
        query: RetrievalQuery,
        candidates: tuple[FusedCandidate, ...],
        *,
        snapshot_time: datetime,
    ) -> tuple[SelectedEvidence, ...]:
        candidate_values = _validated_candidates(
            query,
            candidates,
            snapshot_time,
            allow_merged_evidence=True,
        )
        if not candidate_values:
            return ()
        return await self._run_with_verified_root(
            query.workspace_id,
            lambda: self._load_selected_evidence_sync(
                query, candidate_values, snapshot_time
            ),
            "EVIDENCE_CONTENT_UNAVAILABLE",
        )

    async def _run_with_verified_root(
        self,
        workspace_id: str,
        operation: Callable[[], object],
        code: str,
    ):
        if self.database_path is None:
            return await self._run(operation, code)
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self._initialization_timeout_seconds
        while True:
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise RetrievalRepositoryError("REPOSITORY_TIMEOUT") from None
            try:
                await self._ensure_event_root(workspace_id, timeout=remaining)
                return await self._run(operation, code)
            except _EventRootCacheMissError:
                continue
            except asyncio.TimeoutError:
                raise RetrievalRepositoryError("REPOSITORY_TIMEOUT") from None
            except BoundedWorkerBusyError:
                raise RetrievalRepositoryError("REPOSITORY_BUSY") from None
            except RetrievalRepositoryError as exc:
                if exc.code in {
                    "POLICY_STATE_UNAVAILABLE",
                    "EVIDENCE_CONTENT_UNAVAILABLE",
                }:
                    raise RetrievalRepositoryError(code) from None
                raise

    async def _ensure_event_root(self, workspace_id: str, *, timeout: float) -> None:
        loop = asyncio.get_running_loop()
        with self._initialization_lock:
            task = self._initializations.get(workspace_id)
            if task is None or task.done():
                task = loop.create_task(
                    self._worker_pool.run(
                        lambda: self._tracked_operation(
                            lambda: self._initialize_event_root_sync(workspace_id)
                        )
                    )
                )
                self._initializations[workspace_id] = task

                def finish_initialization(completed: asyncio.Task[None]) -> None:
                    self._finish_initialization(workspace_id, completed)

                task.add_done_callback(finish_initialization)
        await asyncio.wait_for(asyncio.shield(task), timeout=timeout)

    def _finish_initialization(
        self, workspace_id: str, task: asyncio.Task[None]
    ) -> None:
        with self._initialization_lock:
            if self._initializations.get(workspace_id) is task:
                self._initializations.pop(workspace_id, None)
        if task.cancelled():
            return
        # Retrieve the exception even when every waiter timed out or was cancelled.
        task.exception()

    def _tracked_operation(self, operation: Callable[[], object]):
        with self._lifecycle:
            if self._closed:
                raise RetrievalRepositoryError("POLICY_STATE_UNAVAILABLE")
            self._active_operations += 1
        try:
            return operation()
        finally:
            with self._lifecycle:
                self._active_operations -= 1
                self._lifecycle.notify_all()

    async def _run(self, operation: Callable[[], object], code: str):
        try:
            return await asyncio.wait_for(
                self._worker_pool.run(lambda: self._tracked_operation(operation)),
                timeout=self._timeout_seconds,
            )
        except asyncio.TimeoutError:
            raise RetrievalRepositoryError("REPOSITORY_TIMEOUT") from None
        except BoundedWorkerBusyError:
            raise RetrievalRepositoryError("REPOSITORY_BUSY") from None
        except _EventRootCacheMissError:
            raise
        except RetrievalRepositoryError as exc:
            if exc.code in {
                "POLICY_STATE_UNAVAILABLE",
                "EVIDENCE_CONTENT_UNAVAILABLE",
            }:
                raise RetrievalRepositoryError(code) from None
            raise
        except Exception:
            raise RetrievalRepositoryError(code) from None

    def _open_connection(self) -> sqlite3.Connection:
        connection = self._connection_factory()
        if not isinstance(connection, sqlite3.Connection):
            raise TypeError("connection_factory must return a SQLite connection")
        try:
            database_row = connection.execute("PRAGMA database_list").fetchone()
            if database_row is None or not str(database_row[2]):
                raise TypeError("connection_factory must open a file-backed database")
            connection.row_factory = sqlite3.Row
            connection.execute(
                f"PRAGMA busy_timeout={int(self._timeout_seconds * 1_000)}"
            )
            connection.execute("PRAGMA query_only=ON")
            connection.execute("PRAGMA foreign_keys=ON")
        except Exception:
            connection.close()
            raise
        return connection

    def _load_policy_records_sync(
        self,
        query: RetrievalQuery,
        candidates: tuple[FusedCandidate, ...],
        snapshot_time: datetime,
    ) -> tuple[PolicyRecord, ...]:
        connection = self._open_connection()
        try:
            revision = self._begin_read_snapshot(connection)
            root = self._event_root(connection, query.workspace_id, revision)
            manifests: dict[str, _Manifest] = {}
            records = tuple(
                self._load_policy_record(
                    connection,
                    query,
                    candidate,
                    snapshot_time,
                    root,
                    manifests,
                )
                for candidate in candidates
            )
            connection.rollback()
            return records
        finally:
            connection.close()

    def _load_selected_evidence_sync(
        self,
        query: RetrievalQuery,
        candidates: tuple[FusedCandidate, ...],
        snapshot_time: datetime,
    ) -> tuple[SelectedEvidence, ...]:
        connection = self._open_connection()
        try:
            revision = self._begin_read_snapshot(connection)
            root = self._event_root(connection, query.workspace_id, revision)
            manifests: dict[str, _Manifest] = {}
            retained_states: list[PolicyRecord] = []
            probe_count = 0
            maximum_probes = query.candidate_limit * _MAX_PROVIDER_FANOUT
            for candidate in candidates:
                probes = self._evidence_probes(candidate)
                probe_count += len(probes)
                if probe_count > maximum_probes:
                    raise RetrievalRepositoryError("EVIDENCE_CONTENT_UNAVAILABLE")
                states: dict[tuple[str, str | None], PolicyRecord] = {}
                for probe in probes:
                    state = self._load_policy_record(
                        connection,
                        query,
                        probe,
                        snapshot_time,
                        root,
                        manifests,
                        verify_structured_content=True,
                    )
                    policy_result = apply_retrieval_policy(
                        query,
                        (probe,),
                        (state,),
                        snapshot_time=snapshot_time,
                    )
                    if (
                        policy_result.rejections
                        or len(policy_result.candidates) != 1
                        or policy_result.candidates[0].record_id != probe.record_id
                        or policy_result.candidates[0].version_id != probe.version_id
                    ):
                        raise RetrievalRepositoryError("EVIDENCE_CONTENT_UNAVAILABLE")
                    states[state.identity] = state
                retained_identity = (
                    candidate.record_id,
                    candidate.version_id,
                )
                retained_state = states.get(retained_identity)
                if retained_state is None or any(
                    state.content_hash != retained_state.content_hash
                    for state in states.values()
                ):
                    raise RetrievalRepositoryError("EVIDENCE_CONTENT_UNAVAILABLE")
                retained_states.append(retained_state)
            selected = tuple(
                self._hydrate_selected(
                    connection,
                    query,
                    candidate,
                    state,
                    snapshot_time,
                    root,
                    manifests,
                )
                for candidate, state in zip(candidates, retained_states, strict=True)
            )
            connection.rollback()
            return selected
        finally:
            connection.close()

    @staticmethod
    def _evidence_probes(
        candidate: FusedCandidate,
    ) -> tuple[FusedCandidate, ...]:
        grouped: dict[tuple[str, str | None], list[EvidenceRef]] = {}
        for evidence in candidate.evidence_refs:
            grouped.setdefault((evidence.record_id, evidence.version_id), []).append(
                evidence
            )
        ranks = dict(candidate.channel_ranks)
        generations = dict(candidate.manifest_generations)
        evidence_channels = {evidence.provider for evidence in candidate.evidence_refs}
        if evidence_channels != set(candidate.channels):
            raise RetrievalRepositoryError("EVIDENCE_CONTENT_UNAVAILABLE")
        probes: list[FusedCandidate] = []
        retained_identity = (candidate.record_id, candidate.version_id)
        identities = sorted(
            grouped,
            key=lambda identity: (
                identity != retained_identity,
                identity[0],
                identity[1] or "",
            ),
        )
        for identity in identities:
            grouped_refs = tuple(grouped[identity])
            channels = frozenset(evidence.provider for evidence in grouped_refs)
            if not channels or any(
                channel not in ranks or channel not in generations
                for channel in channels
            ):
                raise RetrievalRepositoryError("EVIDENCE_CONTENT_UNAVAILABLE")
            primary = (
                candidate.evidence
                if candidate.evidence in grouped_refs
                else min(
                    grouped_refs,
                    key=lambda evidence: (
                        evidence.provider,
                        evidence.event_id,
                        evidence.relation_path,
                    ),
                )
            )
            evidence_refs = (
                primary,
                *(ref for ref in grouped_refs if ref != primary),
            )
            probes.append(
                FusedCandidate(
                    evidence=primary,
                    evidence_refs=evidence_refs,
                    score=candidate.score,
                    channels=channels,
                    channel_ranks=tuple(
                        sorted((channel, ranks[channel]) for channel in channels)
                    ),
                    manifest_generations=tuple(
                        sorted((channel, generations[channel]) for channel in channels)
                    ),
                    highlights=candidate.highlights,
                    policy_notes=candidate.policy_notes,
                    transaction_time=candidate.transaction_time,
                )
            )
        return tuple(probes)

    def _hydrate_selected(
        self,
        connection: sqlite3.Connection,
        query: RetrievalQuery,
        candidate: FusedCandidate,
        state: PolicyRecord,
        snapshot_time: datetime,
        root: tuple[int, str, int | None, str | None],
        manifests: dict[str, _Manifest],
    ) -> SelectedEvidence:
        record = connection.execute(
            "SELECT content,record_type,content_hash,source_event_id,"
            "outcome,worked,rationale,tags_json FROM memory_records "
            "WHERE workspace_id=? AND record_id=? AND deleted_at_us IS NULL",
            (query.workspace_id, candidate.record_id),
        ).fetchone()
        if record is None:
            raise RetrievalRepositoryError("EVIDENCE_CONTENT_UNAVAILABLE")
        document_metadata = self._document_legacy_metadata(
            connection,
            query.workspace_id,
            candidate.record_id,
            manifests,
        )
        if not (
            isinstance(record[0], str)
            and record[0].strip()
            and str(record[1]) == state.category
            and _safe_hash(record[2]) == state.content_hash
            and (
                str(record[3]) in state.source_event_ids
                or str(record[3]) == document_metadata[0]
            )
        ):
            raise RetrievalRepositoryError("EVIDENCE_CONTENT_UNAVAILABLE")
        rationale = record[6]
        try:
            tags = json.loads(str(record[7]))
        except (TypeError, ValueError, RecursionError) as exc:
            raise RetrievalRepositoryError("EVIDENCE_CONTENT_UNAVAILABLE") from exc
        if (
            (rationale is not None and not isinstance(rationale, str))
            or not isinstance(tags, list)
            or not all(
                isinstance(tag, str) and tag and tag == tag.strip() for tag in tags
            )
            or len(tags) != len(set(tags))
            or frozenset(tags) != state.tags
            or document_metadata[1] != (rationale or "")
            or document_metadata[2] != "\n".join(tags)
        ):
            raise RetrievalRepositoryError("EVIDENCE_CONTENT_UNAVAILABLE")
        supersession = tuple(
            note.removeprefix("SUPERSEDED_BY:")
            for note in candidate.policy_notes
            if note.startswith("SUPERSEDED_BY:")
        )
        if supersession:
            if (
                len(supersession) != 1
                or state.superseded_by_version_id != supersession[0]
            ):
                raise RetrievalRepositoryError("EVIDENCE_CONTENT_UNAVAILABLE")
            status = "superseded"
            superseded_by = supersession[0]
        else:
            status = "current"
            superseded_by = None

        transaction_at_us = _datetime_us(query.as_of_transaction_time or snapshot_time)
        procedure_steps = self._selected_procedure_steps(
            connection,
            query,
            candidate.record_id,
            transaction_at_us,
            root,
            manifests,
        )
        outcome, outcome_failed, worked = self._selected_outcome(
            connection,
            query,
            candidate.record_id,
            transaction_at_us,
            record[4],
            record[5],
            root,
            manifests,
        )
        return SelectedEvidence(
            candidate=candidate,
            content=str(record[0]),
            category=state.category,
            rationale=rationale,
            tags=tuple(tags),
            worked=worked,
            status=status,
            superseded_by_version_id=superseded_by,
            outcome=outcome,
            outcome_failed=outcome_failed,
            procedure_steps=procedure_steps,
        )

    @staticmethod
    def _document_legacy_metadata(
        connection: sqlite3.Connection,
        workspace_id: str,
        record_id: str,
        manifests: dict[str, _Manifest],
    ) -> tuple[str, str, str]:
        lexical = manifests.get("lexical")
        if lexical is None:
            raise RetrievalRepositoryError("EVIDENCE_CONTENT_UNAVAILABLE")
        row = connection.execute(
            "SELECT source_event_id,rationale,tags_text "
            "FROM retrieval_documents "
            "WHERE workspace_id=? AND projection_generation=? AND record_id=?",
            (workspace_id, lexical.generation, record_id),
        ).fetchone()
        if row is None or not all(isinstance(value, str) for value in row):
            raise RetrievalRepositoryError("EVIDENCE_CONTENT_UNAVAILABLE")
        return str(row[0]), str(row[1]), str(row[2])

    def _optional_manifest(
        self,
        connection: sqlite3.Connection,
        workspace_id: str,
        channel: str,
        root: tuple[int, str, int | None, str | None],
        manifests: dict[str, _Manifest],
    ) -> _Manifest | None:
        existing = manifests.get(channel)
        if existing is not None:
            return existing
        row = connection.execute(
            "SELECT count(*) FROM projection_manifests WHERE workspace_id=? "
            "AND projection_name=? AND status='active'",
            (workspace_id, channel),
        ).fetchone()
        if row is None or row[0] == 0:
            return None
        try:
            return self._active_manifest(
                connection,
                workspace_id,
                channel,
                root,
                manifests,
            )
        except RetrievalRepositoryError:
            return None

    def _selected_procedure_steps(
        self,
        connection: sqlite3.Connection,
        query: RetrievalQuery,
        record_id: str,
        transaction_at_us: int,
        root: tuple[int, str, int | None, str | None],
        manifests: dict[str, _Manifest],
    ) -> tuple[str, ...]:
        manifest = self._optional_manifest(
            connection,
            query.workspace_id,
            "procedure",
            root,
            manifests,
        )
        if manifest is None:
            return ()
        rows = connection.execute(
            "SELECT procedure.ordinal,procedure.step_text,"
            "procedure.step_hash,procedure.source_event_id "
            "FROM record_procedures AS procedure "
            "JOIN memory_events AS event "
            "ON event.workspace_id=procedure.workspace_id "
            "AND event.event_id=procedure.source_event_id "
            "WHERE procedure.workspace_id=? "
            "AND procedure.projection_generation=? "
            "AND procedure.record_id=? AND event.recorded_at_us<=? "
            "ORDER BY procedure.ordinal",
            (
                query.workspace_id,
                manifest.generation,
                record_id,
                transaction_at_us,
            ),
        ).fetchall()
        if not rows:
            return ()
        steps: list[str] = []
        for expected_ordinal, row in enumerate(rows):
            ordinal = _plain_int(row[0], minimum=0)
            step_text = row[1]
            if (
                ordinal != expected_ordinal
                or not isinstance(step_text, str)
                or not step_text.strip()
            ):
                raise RetrievalRepositoryError("EVIDENCE_CONTENT_UNAVAILABLE")
            _safe_hash(row[2])
            if sha256_json(step_text) != str(row[2]):
                raise RetrievalRepositoryError("EVIDENCE_CONTENT_UNAVAILABLE")
            event = self._source_event(connection, query.workspace_id, str(row[3]))
            if (
                event.stream_id != record_id
                or event.stream_kind != "memory"
                or event.recorded_at_us > transaction_at_us
            ):
                raise RetrievalRepositoryError("EVIDENCE_CONTENT_UNAVAILABLE")
            steps.append(step_text)
        return tuple(steps)

    def _selected_outcome(
        self,
        connection: sqlite3.Connection,
        query: RetrievalQuery,
        record_id: str,
        transaction_at_us: int,
        canonical_outcome: object,
        canonical_worked: object,
        root: tuple[int, str, int | None, str | None],
        manifests: dict[str, _Manifest],
    ) -> tuple[str | None, bool, bool | None]:
        manifest = self._optional_manifest(
            connection,
            query.workspace_id,
            "outcome",
            root,
            manifests,
        )
        if manifest is None:
            return None, False, None
        row = connection.execute(
            "SELECT worked,outcome_text,outcome_event_id,transaction_at_us "
            "FROM record_outcome_view WHERE workspace_id=? "
            "AND projection_generation=? AND record_id=? "
            "AND transaction_at_us<=?",
            (
                query.workspace_id,
                manifest.generation,
                record_id,
                transaction_at_us,
            ),
        ).fetchone()
        if row is None:
            return None, False, None
        worked = row[0]
        outcome = row[1]
        if (
            worked not in {None, 0, 1}
            or worked != canonical_worked
            or outcome != canonical_outcome
            or (outcome is not None and not isinstance(outcome, str))
        ):
            raise RetrievalRepositoryError("EVIDENCE_CONTENT_UNAVAILABLE")
        worked_value = None if worked is None else worked == 1
        event = self._source_event(connection, query.workspace_id, str(row[2]))
        if not (
            event.stream_id == record_id
            and event.stream_kind == "memory"
            and event.recorded_at_us == _plain_int(row[3])
            and event.recorded_at_us <= transaction_at_us
        ):
            raise RetrievalRepositoryError("EVIDENCE_CONTENT_UNAVAILABLE")
        if outcome is None or not outcome.strip():
            if worked == 0:
                raise RetrievalRepositoryError("EVIDENCE_CONTENT_UNAVAILABLE")
            return None, False, worked_value
        return (
            outcome,
            worked == 0,
            worked_value,
        )

    @staticmethod
    def _require_schema(connection: sqlite3.Connection) -> None:
        rows = connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
        available = {str(row[0]) for row in rows}
        if not _REQUIRED_TABLES.issubset(available):
            raise RetrievalRepositoryError("POLICY_STATE_UNAVAILABLE")

    def _begin_read_snapshot(self, connection: sqlite3.Connection) -> int | None:
        if self.database_path is None:
            connection.execute("BEGIN")
            self._require_schema(connection)
            return None
        with self._revision_lock:
            before = self._observer_revision_locked()
            connection.execute("BEGIN")
            self._require_schema(connection)
            # Reading the canonical table pins this connection's WAL snapshot.
            connection.execute("SELECT 1 FROM memory_events LIMIT 1").fetchone()
            after = self._observer_revision_locked()
            if before != after:
                raise _EventRootCacheMissError
            return before

    def _observer_revision_locked(self) -> int:
        connection = self._revision_connection
        if connection is None:
            path = self.database_path
            if path is None:
                raise _EventRootCacheMissError
            connection = sqlite3.connect(
                f"{path.as_uri()}?mode=ro",
                uri=True,
                timeout=self._timeout_seconds,
                check_same_thread=False,
                isolation_level=None,
            )
            try:
                connection.execute(
                    f"PRAGMA busy_timeout={int(self._timeout_seconds * 1_000)}"
                )
                connection.execute("PRAGMA query_only=ON")
            except Exception:
                connection.close()
                raise
            self._revision_connection = connection
        row = connection.execute("PRAGMA data_version").fetchone()
        if row is None:
            raise RetrievalRepositoryError("POLICY_STATE_UNAVAILABLE")
        return _plain_int(row[0], minimum=1)

    def _initialize_event_root_sync(self, workspace_id: str) -> None:
        connection = self._open_connection()
        try:
            revision = self._begin_read_snapshot(connection)
            if revision is None:
                return
            with self._revision_lock:
                cached = self._event_root_cache.get(workspace_id)
                if cached is not None and cached.revision == revision:
                    connection.rollback()
                    return
            root = self._calculate_event_root(connection, workspace_id)
            with self._revision_lock:
                if self._observer_revision_locked() != revision:
                    raise _EventRootCacheMissError
                self._event_root_cache[workspace_id] = _CachedEventRoot(
                    revision=revision,
                    value=root,
                )
            connection.rollback()
        finally:
            if connection.in_transaction:
                connection.rollback()
            connection.close()

    def _event_root(
        self,
        connection: sqlite3.Connection,
        workspace_id: str,
        revision: int | None,
    ) -> tuple[int, str, int | None, str | None]:
        if revision is None:
            return self._calculate_event_root(connection, workspace_id)
        with self._revision_lock:
            cached = self._event_root_cache.get(workspace_id)
            if cached is None or cached.revision != revision:
                raise _EventRootCacheMissError
            return cached.value

    @staticmethod
    def _calculate_event_root(
        connection: sqlite3.Connection,
        workspace_id: str,
    ) -> tuple[int, str, int | None, str | None]:
        digest = hashlib.sha256()
        count = 0
        rows = connection.execute(
            "SELECT event_hash FROM memory_events WHERE workspace_id=? "
            "ORDER BY event_id",
            (workspace_id,),
        )
        for row in rows:
            event_hash = _safe_hash(row[0])
            try:
                digest.update(bytes.fromhex(event_hash))
            except ValueError as exc:
                raise RetrievalRepositoryError("POLICY_STATE_UNAVAILABLE") from exc
            count += 1
        cursor = connection.execute(
            "SELECT recorded_at_us,event_id FROM memory_events "
            "WHERE workspace_id=? ORDER BY recorded_at_us DESC,event_id DESC "
            "LIMIT 1",
            (workspace_id,),
        ).fetchone()
        if cursor is None:
            return count, digest.hexdigest(), None, None
        return (
            count,
            digest.hexdigest(),
            _plain_int(cursor[0]),
            str(cursor[1]),
        )

    def _active_manifest(
        self,
        connection: sqlite3.Connection,
        workspace_id: str,
        channel: str,
        root: tuple[int, str, int | None, str | None],
        cache: dict[str, _Manifest],
        *,
        allow_marked_stale_lexical: bool = False,
    ) -> _Manifest:
        cached = cache.get(channel)
        if cached is not None:
            if cached.marked_stale and not (
                channel == "lexical" and allow_marked_stale_lexical
            ):
                raise RetrievalRepositoryError("POLICY_STATE_UNAVAILABLE")
            return cached
        rows = connection.execute(
            "SELECT generation,source_event_count,source_event_root_hash,"
            "cursor_recorded_at_us,cursor_event_id,row_count,details_json "
            "FROM projection_manifests WHERE workspace_id=? "
            "AND projection_name=? AND status='active'",
            (workspace_id, channel),
        ).fetchall()
        if len(rows) != 1:
            raise RetrievalRepositoryError("POLICY_STATE_UNAVAILABLE")
        row = rows[0]
        generation = _plain_int(row[0], minimum=1)
        source_count = _plain_int(row[1], minimum=0)
        source_root = _safe_hash(row[2])
        cursor_us = None if row[3] is None else _plain_int(row[3])
        cursor_event = None if row[4] is None else str(row[4])
        row_count = _plain_int(row[5], minimum=0)
        try:
            details = json.loads(str(row[6]))
        except (TypeError, ValueError, RecursionError) as exc:
            raise RetrievalRepositoryError("POLICY_STATE_UNAVAILABLE") from exc
        if not isinstance(details, dict):
            raise RetrievalRepositoryError("POLICY_STATE_UNAVAILABLE")
        marked_stale = bool(_REBUILD_MARKERS.intersection(details))
        exact_source = (
            source_count,
            source_root,
            cursor_us,
            cursor_event,
        ) == root
        if marked_stale or not exact_source:
            if not (
                channel == "lexical"
                and allow_marked_stale_lexical
                and marked_stale
                and source_count < root[0]
                and set(_REBUILD_MARKERS).issubset(details)
            ):
                raise RetrievalRepositoryError("POLICY_STATE_UNAVAILABLE")
            marker_at = _plain_int(details["rebuild_required_at_us"])
            marker_event_id = details["rebuild_required_event_id"]
            if not isinstance(marker_event_id, str):
                raise RetrievalRepositoryError("POLICY_STATE_UNAVAILABLE")
            marker_event = self._source_event(connection, workspace_id, marker_event_id)
            if marker_event.recorded_at_us != marker_at:
                raise RetrievalRepositoryError("POLICY_STATE_UNAVAILABLE")
        if (
            channel in SPECIALIZED_PROJECTIONS
            and not specialized_manifest_matches_contract(
                details,
                workspace_id,
                channel,
                generation,
            )
        ):
            raise RetrievalRepositoryError("POLICY_STATE_UNAVAILABLE")
        self._validate_manifest_count(
            connection,
            workspace_id,
            channel,
            generation,
            row_count,
            details,
        )
        manifest = _Manifest(generation, row_count, details, marked_stale)
        cache[channel] = manifest
        return manifest

    @staticmethod
    def _validate_manifest_count(
        connection: sqlite3.Connection,
        workspace_id: str,
        channel: str,
        generation: int,
        row_count: int,
        details: Mapping[str, object],
    ) -> None:
        if channel == "lexical":
            sql = (
                "SELECT count(*) FROM retrieval_documents "
                "WHERE workspace_id=? AND projection_generation=?"
            )
            parameters: tuple[object, ...] = (workspace_id, generation)
        elif channel == "dense":
            provider_key = details.get("provider_key")
            if not isinstance(provider_key, str) or not provider_key:
                raise RetrievalRepositoryError("POLICY_STATE_UNAVAILABLE")
            sql = (
                "SELECT count(*) FROM dense_projection_refs "
                "WHERE workspace_id=? AND projection_generation=? "
                "AND provider_key=?"
            )
            parameters = (workspace_id, generation, provider_key)
        elif channel == "procedure":
            sql = (
                "SELECT count(*) FROM record_procedures "
                "WHERE workspace_id=? AND projection_generation=?"
            )
            parameters = (workspace_id, generation)
        elif channel == "outcome":
            sql = (
                "SELECT count(*) FROM record_outcome_view "
                "WHERE workspace_id=? AND projection_generation=?"
            )
            parameters = (workspace_id, generation)
        elif channel == "temporal":
            sql = "SELECT count(*) FROM memory_fact_versions WHERE workspace_id=?"
            parameters = (workspace_id,)
        elif channel == "graph":
            sql = (
                "SELECT "
                "(SELECT count(*) FROM memory_relationship_versions "
                " WHERE workspace_id=?) + "
                "(SELECT count(*) FROM memory_fact_versions "
                " WHERE workspace_id=? AND object_kind='record_ref')"
            )
            parameters = (workspace_id, workspace_id)
        else:
            raise RetrievalRepositoryError("POLICY_STATE_UNAVAILABLE")
        row = connection.execute(sql, parameters).fetchone()
        if row is None or row[0] != row_count:
            raise RetrievalRepositoryError("POLICY_STATE_UNAVAILABLE")

    @staticmethod
    def _source_event(
        connection: sqlite3.Connection,
        workspace_id: str,
        event_id: str,
    ) -> _Event:
        row = connection.execute(
            "SELECT event_id,stream_id,stream_kind,stream_version,"
            "recorded_at_us,event_hash FROM memory_events "
            "WHERE workspace_id=? AND event_id=?",
            (workspace_id, event_id),
        ).fetchone()
        if row is None or str(row[0]).removeprefix("evt_") != _safe_hash(row[5]):
            raise RetrievalRepositoryError("POLICY_STATE_UNAVAILABLE")
        return _Event(
            event_id=str(row[0]),
            stream_id=str(row[1]),
            stream_kind=str(row[2]),
            stream_version=_plain_int(row[3], minimum=1),
            recorded_at_us=_plain_int(row[4]),
        )

    def _load_policy_record(
        self,
        connection: sqlite3.Connection,
        query: RetrievalQuery,
        candidate: FusedCandidate,
        snapshot_time: datetime,
        root: tuple[int, str, int | None, str | None],
        manifests: dict[str, _Manifest],
        *,
        verify_structured_content: bool = False,
    ) -> PolicyRecord:
        row = connection.execute(
            "SELECT workspace_id,record_type,content_hash,"
            "json_type(context_json),"
            "json_type(context_json,'$.visibility'),"
            "json_extract(context_json,'$.visibility'),tags_json,archived,"
            "stream_version,source_event_id,created_at_us,updated_at_us,"
            "deleted_at_us FROM memory_records "
            "WHERE workspace_id=? AND record_id=?",
            (query.workspace_id, candidate.record_id),
        ).fetchone()
        if row is None or row[12] is not None:
            raise RetrievalRepositoryError("POLICY_STATE_UNAVAILABLE")
        content_hash = _safe_hash(row[2])
        try:
            tags_json = json.loads(str(row[6]))
        except (TypeError, ValueError, RecursionError) as exc:
            raise RetrievalRepositoryError("POLICY_STATE_UNAVAILABLE") from exc
        if (
            row[3] != "object"
            or not isinstance(tags_json, list)
            or not all(
                isinstance(tag, str) and tag and tag == tag.strip() for tag in tags_json
            )
            or len(tags_json) != len(set(tags_json))
        ):
            raise RetrievalRepositoryError("POLICY_STATE_UNAVAILABLE")
        if row[4] is None:
            visibility = "workspace"
        elif row[4] == "text" and isinstance(row[5], str):
            visibility = row[5]
        else:
            raise RetrievalRepositoryError("POLICY_STATE_UNAVAILABLE")
        if visibility not in {"workspace", "private", "shared"}:
            raise RetrievalRepositoryError("POLICY_STATE_UNAVAILABLE")
        archived_value = row[7]
        if archived_value not in {0, 1}:
            raise RetrievalRepositoryError("POLICY_STATE_UNAVAILABLE")
        category = str(row[1])
        record_event = self._source_event(connection, query.workspace_id, str(row[9]))
        if (
            record_event.stream_id != candidate.record_id
            or record_event.stream_kind != "memory"
            or record_event.stream_version != _plain_int(row[8], minimum=1)
            or record_event.recorded_at_us != _plain_int(row[11])
        ):
            raise RetrievalRepositoryError("POLICY_STATE_UNAVAILABLE")

        stale_lexical_candidate = candidate.channels == frozenset({"lexical"}) and all(
            evidence.provider == "lexical" for evidence in candidate.evidence_refs
        )
        lexical = self._active_manifest(
            connection,
            query.workspace_id,
            "lexical",
            root,
            manifests,
            allow_marked_stale_lexical=stale_lexical_candidate,
        )
        document = connection.execute(
            "SELECT category,valid_from_us,valid_to_us,transaction_from_us,"
            "transaction_to_us,visibility,archived,content_hash,"
            "source_event_id,tags_text FROM retrieval_documents "
            "WHERE workspace_id=? AND projection_generation=? AND record_id=?",
            (query.workspace_id, lexical.generation, candidate.record_id),
        ).fetchone()
        if document is None:
            raise RetrievalRepositoryError("POLICY_STATE_UNAVAILABLE")
        if not (
            str(document[0]) == category
            and str(document[5]) == visibility
            and document[6] == archived_value
            and _safe_hash(document[7]) == content_hash
            and str(document[8]) == record_event.event_id
            and str(document[9]) == "\n".join(tags_json)
            and document[1] == _plain_int(row[10])
            and document[2] is None
            and document[3] == _plain_int(row[11])
            and document[4] is None
        ):
            raise RetrievalRepositoryError("POLICY_STATE_UNAVAILABLE")

        source_event_ids: set[str] = set()
        projection_hashes: dict[str, str] = {}
        active_generations: dict[str, int] = {}
        valid_from_us = None if document[1] is None else _plain_int(document[1])
        valid_to_us = None if document[2] is None else _plain_int(document[2])
        transaction_from = _plain_int(document[3])
        transaction_to_us = None if document[4] is None else _plain_int(document[4])
        superseded_by_version_id: str | None = None
        has_unresolved_contradiction = False
        for channel, generation in candidate.manifest_generations:
            manifest = self._active_manifest(
                connection,
                query.workspace_id,
                channel,
                root,
                manifests,
                allow_marked_stale_lexical=(
                    channel == "lexical" and stale_lexical_candidate
                ),
            )
            if generation != manifest.generation:
                raise RetrievalRepositoryError("POLICY_STATE_UNAVAILABLE")
            active_generations[channel] = manifest.generation
            projection_hashes[channel] = content_hash

        for evidence in candidate.evidence_refs:
            if evidence.content_hash != content_hash:
                raise RetrievalRepositoryError("POLICY_STATE_UNAVAILABLE")
            event = self._source_event(
                connection, query.workspace_id, evidence.event_id
            )
            if evidence.provider == "lexical":
                if event.event_id != str(document[8]):
                    raise RetrievalRepositoryError("POLICY_STATE_UNAVAILABLE")
            elif evidence.provider == "dense":
                dense_manifest = manifests.get("dense")
                if dense_manifest is None:
                    raise RetrievalRepositoryError("POLICY_STATE_UNAVAILABLE")
                provider_key = dense_manifest.details.get("provider_key")
                if not isinstance(provider_key, str) or not provider_key:
                    raise RetrievalRepositoryError("POLICY_STATE_UNAVAILABLE")
                dense = connection.execute(
                    "SELECT content_hash,model_id,dimension,state,"
                    "updated_event_id,updated_at_us FROM dense_projection_refs "
                    "WHERE workspace_id=? AND provider_key=? "
                    "AND projection_generation=? AND record_id=?",
                    (
                        query.workspace_id,
                        provider_key,
                        dense_manifest.generation,
                        candidate.record_id,
                    ),
                ).fetchone()
                if dense is None or not (
                    _safe_hash(dense[0]) == content_hash
                    and isinstance(dense[1], str)
                    and dense[1]
                    and _plain_int(dense[2], minimum=1) > 0
                    and dense[3] == "ready"
                    and str(dense[4]) == event.event_id == record_event.event_id
                    and _plain_int(dense[5]) >= event.recorded_at_us
                    and dense_manifest.details.get("model_id") == dense[1]
                    and dense_manifest.details.get("dimension") == dense[2]
                ):
                    raise RetrievalRepositoryError("POLICY_STATE_UNAVAILABLE")
            elif evidence.provider == "procedure":
                procedure_manifest = manifests.get("procedure")
                if procedure_manifest is None:
                    raise RetrievalRepositoryError("POLICY_STATE_UNAVAILABLE")
                procedure_columns = "ordinal,step_hash,source_event_id"
                if verify_structured_content:
                    procedure_columns += ",step_text"
                steps = connection.execute(
                    f"SELECT {procedure_columns} FROM record_procedures "
                    "WHERE workspace_id=? AND projection_generation=? "
                    "AND record_id=? ORDER BY ordinal",
                    (
                        query.workspace_id,
                        procedure_manifest.generation,
                        candidate.record_id,
                    ),
                ).fetchall()
                if not steps:
                    raise RetrievalRepositoryError("POLICY_STATE_UNAVAILABLE")
                selected_events: set[str] = set()
                selected_recorded_at: list[int] = []
                for expected_ordinal, step in enumerate(steps):
                    if _plain_int(step[0], minimum=0) != expected_ordinal:
                        raise RetrievalRepositoryError("POLICY_STATE_UNAVAILABLE")
                    step_hash = _safe_hash(step[1])
                    if verify_structured_content:
                        step_text = step[3]
                        if (
                            not isinstance(step_text, str)
                            or not step_text.strip()
                            or sha256_json(step_text) != step_hash
                        ):
                            raise RetrievalRepositoryError("POLICY_STATE_UNAVAILABLE")
                    step_event = self._source_event(
                        connection, query.workspace_id, str(step[2])
                    )
                    if (
                        step_event.stream_id != candidate.record_id
                        or step_event.stream_kind != "memory"
                    ):
                        raise RetrievalRepositoryError("POLICY_STATE_UNAVAILABLE")
                    selected_events.add(step_event.event_id)
                    selected_recorded_at.append(step_event.recorded_at_us)
                if event.event_id not in selected_events:
                    raise RetrievalRepositoryError("POLICY_STATE_UNAVAILABLE")
                transaction_from = max(transaction_from, *selected_recorded_at)
            elif evidence.provider == "outcome":
                outcome_manifest = manifests.get("outcome")
                if outcome_manifest is None:
                    raise RetrievalRepositoryError("POLICY_STATE_UNAVAILABLE")
                outcome_columns = "worked,outcome_event_id,transaction_at_us"
                if verify_structured_content:
                    outcome_columns += ",outcome_text"
                outcome = connection.execute(
                    f"SELECT {outcome_columns} "
                    "FROM record_outcome_view WHERE workspace_id=? "
                    "AND projection_generation=? AND record_id=?",
                    (
                        query.workspace_id,
                        outcome_manifest.generation,
                        candidate.record_id,
                    ),
                ).fetchone()
                if outcome is None or outcome[0] not in {None, 0, 1}:
                    raise RetrievalRepositoryError("POLICY_STATE_UNAVAILABLE")
                if not (
                    event.event_id == str(outcome[1])
                    and event.stream_id == candidate.record_id
                    and event.stream_kind == "memory"
                    and event.recorded_at_us == _plain_int(outcome[2])
                ):
                    raise RetrievalRepositoryError("POLICY_STATE_UNAVAILABLE")
                if verify_structured_content:
                    canonical_outcome = connection.execute(
                        "SELECT worked,outcome FROM memory_records "
                        "WHERE workspace_id=? AND record_id=? "
                        "AND deleted_at_us IS NULL",
                        (query.workspace_id, candidate.record_id),
                    ).fetchone()
                    if canonical_outcome is None or not (
                        canonical_outcome[0] == outcome[0]
                        and canonical_outcome[1] == outcome[3]
                        and (
                            outcome[3] is None
                            or (isinstance(outcome[3], str) and outcome[3].strip())
                        )
                    ):
                        raise RetrievalRepositoryError("POLICY_STATE_UNAVAILABLE")
            elif evidence.provider == "temporal":
                if candidate.version_id is None or not candidate.version_id.startswith(
                    "fact_"
                ):
                    raise RetrievalRepositoryError("POLICY_STATE_UNAVAILABLE")
                fact = connection.execute(
                    "SELECT fact_id,version,subject_record_id,metadata_json,"
                    "valid_from_us,valid_to_us,transaction_from_us,"
                    "transaction_to_us,asserted_by_event_id,"
                    "retracted_by_event_id,content_hash "
                    "FROM memory_fact_versions "
                    "WHERE workspace_id=? AND fact_version_id=?",
                    (query.workspace_id, candidate.version_id),
                ).fetchone()
                if fact is None:
                    raise RetrievalRepositoryError("POLICY_STATE_UNAVAILABLE")
                try:
                    fact_metadata = json.loads(str(fact[3]))
                except (TypeError, ValueError, RecursionError) as exc:
                    raise RetrievalRepositoryError("POLICY_STATE_UNAVAILABLE") from exc
                if not isinstance(fact_metadata, dict):
                    raise RetrievalRepositoryError("POLICY_STATE_UNAVAILABLE")
                _safe_hash(fact[10])
                contradiction_value = fact_metadata.get(
                    "has_unresolved_contradiction", False
                )
                if not isinstance(contradiction_value, bool):
                    raise RetrievalRepositoryError("POLICY_STATE_UNAVAILABLE")
                fact_version = _plain_int(fact[1], minimum=1)
                if not (
                    str(fact[2]) == candidate.record_id
                    and str(fact[8]) == event.event_id
                    and event.stream_id == str(fact[0])
                    and event.stream_kind == "fact"
                    and event.stream_version == fact_version
                    and event.recorded_at_us == _plain_int(fact[6])
                ):
                    raise RetrievalRepositoryError("POLICY_STATE_UNAVAILABLE")
                valid_from_us = _plain_int(fact[4])
                valid_to_us = None if fact[5] is None else _plain_int(fact[5])
                transaction_from = _plain_int(fact[6])
                transaction_to_us = None if fact[7] is None else _plain_int(fact[7])
                has_unresolved_contradiction = contradiction_value
                transaction_at_us = _datetime_us(
                    query.as_of_transaction_time or snapshot_time
                )
                successor = connection.execute(
                    "SELECT fact_version_id,asserted_by_event_id,version,"
                    "transaction_from_us "
                    "FROM memory_fact_versions WHERE workspace_id=? "
                    "AND fact_id=? AND version>? AND transaction_from_us<=? "
                    "ORDER BY version LIMIT 1",
                    (
                        query.workspace_id,
                        str(fact[0]),
                        fact_version,
                        transaction_at_us,
                    ),
                ).fetchone()
                if successor is not None:
                    successor_event = self._source_event(
                        connection, query.workspace_id, str(successor[1])
                    )
                    if (
                        successor_event.stream_id != str(fact[0])
                        or successor_event.stream_kind != "fact"
                        or successor_event.stream_version
                        != _plain_int(successor[2], minimum=1)
                        or successor_event.recorded_at_us != _plain_int(successor[3])
                        or (
                            fact[9] is not None
                            and str(fact[9]) != successor_event.event_id
                        )
                    ):
                        raise RetrievalRepositoryError("POLICY_STATE_UNAVAILABLE")
                    superseded_by_version_id = str(successor[0])
            elif evidence.provider == "graph":
                if (
                    event.event_id != str(document[8])
                    or not evidence.relation_path
                    or len(evidence.relation_path) > _MAX_RELATION_PATH
                ):
                    raise RetrievalRepositoryError("POLICY_STATE_UNAVAILABLE")
                valid_at_us = _datetime_us(query.as_of_valid_time or snapshot_time)
                transaction_at_us = _datetime_us(
                    query.as_of_transaction_time or snapshot_time
                )
                current_record = candidate.record_id
                relation_valid_from: list[int] = []
                relation_valid_to: list[int] = []
                relation_transaction_from: list[int] = []
                relation_transaction_to: list[int] = []
                for edge_version_id in reversed(evidence.relation_path):
                    (
                        source_id,
                        target_id,
                        edge_valid_from,
                        edge_valid_to,
                        edge_transaction_from,
                        edge_transaction_to,
                    ) = self._graph_edge_state(
                        connection,
                        query.workspace_id,
                        edge_version_id,
                        lexical.generation,
                    )
                    if current_record == source_id:
                        current_record = target_id
                    elif current_record == target_id:
                        current_record = source_id
                    else:
                        raise RetrievalRepositoryError("POLICY_STATE_UNAVAILABLE")
                    relation_valid_from.append(edge_valid_from)
                    if edge_valid_to is not None:
                        relation_valid_to.append(edge_valid_to)
                    relation_transaction_from.append(edge_transaction_from)
                    if edge_transaction_to is not None:
                        relation_transaction_to.append(edge_transaction_to)
                    if not (
                        relation_valid_from[-1] <= valid_at_us
                        and (
                            edge_valid_to is None or valid_at_us < relation_valid_to[-1]
                        )
                        and relation_transaction_from[-1] <= transaction_at_us
                        and (
                            edge_transaction_to is None
                            or transaction_at_us < relation_transaction_to[-1]
                        )
                    ):
                        raise RetrievalRepositoryError("POLICY_STATE_UNAVAILABLE")
                valid_from_us = max(
                    value
                    for value in (
                        valid_from_us,
                        *relation_valid_from,
                    )
                    if value is not None
                )
                if relation_valid_to:
                    valid_to_us = min(
                        value
                        for value in (valid_to_us, *relation_valid_to)
                        if value is not None
                    )
                transaction_from = max(transaction_from, *relation_transaction_from)
                if relation_transaction_to:
                    transaction_to_us = min(
                        value
                        for value in (
                            transaction_to_us,
                            *relation_transaction_to,
                        )
                        if value is not None
                    )
            else:
                raise RetrievalRepositoryError("POLICY_STATE_UNAVAILABLE")
            source_event_ids.add(event.event_id)
            transaction_from = max(transaction_from, event.recorded_at_us)

        fact_supersession: str | None = None
        fact_contradiction = False
        if candidate.version_id is None:
            fact_state = self._record_fact_policy_state(
                connection,
                query,
                candidate.record_id,
                snapshot_time,
            )
            if fact_state is not None:
                (
                    fact_valid_from,
                    fact_valid_to,
                    fact_transaction_from,
                    fact_transaction_to,
                    fact_supersession,
                    fact_contradiction,
                ) = fact_state
                valid_at_us = _datetime_us(query.as_of_valid_time or snapshot_time)
                document_valid_at = _plain_int(document[1]) <= valid_at_us and (
                    document[2] is None or valid_at_us < _plain_int(document[2])
                )
                fact_valid_at = fact_valid_from <= valid_at_us and (
                    fact_valid_to is None or valid_at_us < fact_valid_to
                )
                if fact_supersession is not None or document_valid_at != fact_valid_at:
                    valid_from_us = fact_valid_from
                    valid_to_us = fact_valid_to
                    transaction_from = fact_transaction_from
                    transaction_to_us = fact_transaction_to

        relationship_supersession, relationship_contradiction = (
            self._relationship_policy_state(
                connection,
                query,
                candidate.record_id,
                snapshot_time,
            )
        )
        if candidate.version_id is None:
            superseded_by_version_id = (
                fact_supersession
                if fact_supersession is not None
                else relationship_supersession
            )
        has_unresolved_contradiction = (
            has_unresolved_contradiction
            or fact_contradiction
            or relationship_contradiction
        )

        try:
            allowed = self._visibility_authorizer(query, visibility)
        except Exception as exc:
            raise RetrievalRepositoryError("POLICY_STATE_UNAVAILABLE") from exc
        if not isinstance(allowed, bool):
            raise RetrievalRepositoryError("POLICY_STATE_UNAVAILABLE")
        del snapshot_time
        return PolicyRecord(
            workspace_id=str(row[0]),
            record_id=candidate.record_id,
            version_id=candidate.version_id,
            content_hash=content_hash,
            source_event_ids=frozenset(source_event_ids),
            visibility=visibility,
            visibility_allowed=allowed,
            archived=bool(archived_value),
            category=category,
            tags=frozenset(tags_json),
            valid_from=_to_datetime(valid_from_us),
            valid_to=_to_datetime(valid_to_us),
            transaction_from=_to_datetime(transaction_from),
            transaction_to=_to_datetime(transaction_to_us),
            superseded_by_version_id=superseded_by_version_id,
            has_unresolved_contradiction=has_unresolved_contradiction,
            projection_content_hashes=tuple(sorted(projection_hashes.items())),
            active_manifest_generations=tuple(sorted(active_generations.items())),
        )

    def _graph_edge_state(
        self,
        connection: sqlite3.Connection,
        workspace_id: str,
        edge_version_id: str,
        lexical_generation: int,
    ) -> tuple[str, str, int, int | None, int, int | None]:
        if edge_version_id.startswith("rel_"):
            row = connection.execute(
                "SELECT relationship_id,version,source_record_id,"
                "target_record_id,valid_from_us,valid_to_us,"
                "transaction_from_us,transaction_to_us,"
                "asserted_by_event_id,retracted_by_event_id,content_hash "
                "FROM memory_relationship_versions WHERE workspace_id=? "
                "AND relationship_version_id=?",
                (workspace_id, edge_version_id),
            ).fetchone()
            if row is None:
                raise RetrievalRepositoryError("POLICY_STATE_UNAVAILABLE")
            stream_id = str(row[0])
            stream_kind = "relationship"
            version = _plain_int(row[1], minimum=1)
            source_id = str(row[2])
            target_id = str(row[3])
            valid_from = _plain_int(row[4])
            valid_to = None if row[5] is None else _plain_int(row[5])
            transaction_from = _plain_int(row[6])
            transaction_to = None if row[7] is None else _plain_int(row[7])
            asserted_event_id = str(row[8])
            retracted_event_id = None if row[9] is None else str(row[9])
            _safe_hash(row[10])
        elif edge_version_id.startswith("fact_"):
            row = connection.execute(
                "SELECT fact_id,version,subject_record_id,object_kind,"
                "object_json,valid_from_us,valid_to_us,transaction_from_us,"
                "transaction_to_us,asserted_by_event_id,"
                "retracted_by_event_id,content_hash "
                "FROM memory_fact_versions WHERE workspace_id=? "
                "AND fact_version_id=?",
                (workspace_id, edge_version_id),
            ).fetchone()
            if row is None or row[3] != "record_ref":
                raise RetrievalRepositoryError("POLICY_STATE_UNAVAILABLE")
            try:
                target = json.loads(str(row[4]))
            except (TypeError, ValueError, RecursionError) as exc:
                raise RetrievalRepositoryError("POLICY_STATE_UNAVAILABLE") from exc
            if (
                not isinstance(target, str)
                or canonical_json_bytes(target).decode("utf-8") != row[4]
            ):
                raise RetrievalRepositoryError("POLICY_STATE_UNAVAILABLE")
            stream_id = str(row[0])
            stream_kind = "fact"
            version = _plain_int(row[1], minimum=1)
            source_id = str(row[2])
            target_id = target
            valid_from = _plain_int(row[5])
            valid_to = None if row[6] is None else _plain_int(row[6])
            transaction_from = _plain_int(row[7])
            transaction_to = None if row[8] is None else _plain_int(row[8])
            asserted_event_id = str(row[9])
            retracted_event_id = None if row[10] is None else str(row[10])
            _safe_hash(row[11])
        else:
            raise RetrievalRepositoryError("POLICY_STATE_UNAVAILABLE")
        if source_id == target_id:
            raise RetrievalRepositoryError("POLICY_STATE_UNAVAILABLE")
        active_endpoints = connection.execute(
            "SELECT count(*) FROM memory_records AS record "
            "JOIN retrieval_documents AS document "
            "ON document.workspace_id=record.workspace_id "
            "AND document.record_id=record.record_id "
            "AND document.projection_generation=? "
            "WHERE record.workspace_id=? AND record.record_id IN (?,?) "
            "AND record.deleted_at_us IS NULL",
            (
                lexical_generation,
                workspace_id,
                source_id,
                target_id,
            ),
        ).fetchone()
        if active_endpoints is None or active_endpoints[0] != 2:
            raise RetrievalRepositoryError("POLICY_STATE_UNAVAILABLE")
        asserted = self._source_event(connection, workspace_id, asserted_event_id)
        if not (
            asserted.stream_id == stream_id
            and asserted.stream_kind == stream_kind
            and asserted.stream_version == version
            and asserted.recorded_at_us == transaction_from
        ):
            raise RetrievalRepositoryError("POLICY_STATE_UNAVAILABLE")
        if retracted_event_id is not None:
            retracted = self._source_event(connection, workspace_id, retracted_event_id)
            if not (
                retracted.stream_id == stream_id
                and retracted.stream_kind == stream_kind
                and (
                    (
                        transaction_to is None
                        and retracted_event_id == asserted_event_id
                        and retracted.recorded_at_us == transaction_from
                    )
                    or (
                        transaction_to is not None
                        and retracted.recorded_at_us == transaction_to
                        and retracted.stream_version > version
                    )
                )
            ):
                raise RetrievalRepositoryError("POLICY_STATE_UNAVAILABLE")
        return (
            source_id,
            target_id,
            valid_from,
            valid_to,
            transaction_from,
            transaction_to,
        )

    def _record_fact_policy_state(
        self,
        connection: sqlite3.Connection,
        query: RetrievalQuery,
        record_id: str,
        snapshot_time: datetime,
    ) -> tuple[int, int | None, int, int | None, str | None, bool] | None:
        valid_at_us = _datetime_us(query.as_of_valid_time or snapshot_time)
        transaction_at_us = _datetime_us(query.as_of_transaction_time or snapshot_time)
        rows = connection.execute(
            "SELECT fact_version_id,fact_id,version,object_kind,object_json,"
            "evidence_json,metadata_json,content_hash,valid_from_us,"
            "valid_to_us,transaction_from_us,transaction_to_us,"
            "asserted_by_event_id,retracted_by_event_id "
            "FROM memory_fact_versions WHERE workspace_id=? "
            "AND subject_record_id=? AND transaction_from_us<=? "
            "AND (transaction_to_us IS NULL OR ?<transaction_to_us) "
            "ORDER BY fact_id,version",
            (
                query.workspace_id,
                record_id,
                transaction_at_us,
                transaction_at_us,
            ),
        ).fetchall()
        if not rows:
            return None
        seen_fact_ids: set[str] = set()
        valid_from_values: list[int] = []
        valid_to_values: list[int] = []
        transaction_from_values: list[int] = []
        transaction_to_values: list[int] = []
        supersessions: set[str] = set()
        contradiction = False
        for row in rows:
            fact_version_id = str(row[0])
            fact_id = str(row[1])
            if fact_id in seen_fact_ids:
                raise RetrievalRepositoryError("POLICY_STATE_UNAVAILABLE")
            seen_fact_ids.add(fact_id)
            version = _plain_int(row[2], minimum=1)
            try:
                object_value = json.loads(str(row[4]))
                evidence_value = json.loads(str(row[5]))
                metadata = json.loads(str(row[6]))
            except (TypeError, ValueError, RecursionError) as exc:
                raise RetrievalRepositoryError("POLICY_STATE_UNAVAILABLE") from exc
            if (
                not isinstance(row[3], str)
                or not row[3]
                or canonical_json_bytes(object_value).decode("utf-8") != row[4]
                or not isinstance(evidence_value, list)
                or canonical_json_bytes(evidence_value).decode("utf-8") != row[5]
                or not isinstance(metadata, dict)
                or canonical_json_bytes(metadata).decode("utf-8") != row[6]
            ):
                raise RetrievalRepositoryError("POLICY_STATE_UNAVAILABLE")
            _safe_hash(row[7])
            contradiction_value = metadata.get("has_unresolved_contradiction", False)
            if not isinstance(contradiction_value, bool):
                raise RetrievalRepositoryError("POLICY_STATE_UNAVAILABLE")
            contradiction = contradiction or contradiction_value
            valid_from = _plain_int(row[8])
            valid_to = None if row[9] is None else _plain_int(row[9])
            transaction_from = _plain_int(row[10])
            transaction_to = None if row[11] is None else _plain_int(row[11])
            asserted = self._source_event(connection, query.workspace_id, str(row[12]))
            if not (
                asserted.stream_id == fact_id
                and asserted.stream_kind == "fact"
                and asserted.stream_version == version
                and asserted.recorded_at_us == transaction_from
            ):
                raise RetrievalRepositoryError("POLICY_STATE_UNAVAILABLE")
            if row[13] is not None:
                retracted = self._source_event(
                    connection, query.workspace_id, str(row[13])
                )
                if not (
                    retracted.stream_id == fact_id
                    and retracted.stream_kind == "fact"
                    and retracted.stream_version >= version
                ):
                    raise RetrievalRepositoryError("POLICY_STATE_UNAVAILABLE")
            valid_from_values.append(valid_from)
            if valid_to is not None:
                valid_to_values.append(valid_to)
                if valid_at_us >= valid_to:
                    supersessions.add(fact_version_id)
            transaction_from_values.append(transaction_from)
            if transaction_to is not None:
                transaction_to_values.append(transaction_to)
        if len(supersessions) > 1:
            raise RetrievalRepositoryError("POLICY_STATE_UNAVAILABLE")
        return (
            max(valid_from_values),
            min(valid_to_values) if valid_to_values else None,
            max(transaction_from_values),
            min(transaction_to_values) if transaction_to_values else None,
            next(iter(supersessions), None),
            contradiction,
        )

    def _relationship_policy_state(
        self,
        connection: sqlite3.Connection,
        query: RetrievalQuery,
        record_id: str,
        snapshot_time: datetime,
    ) -> tuple[str | None, bool]:
        valid_at_us = _datetime_us(query.as_of_valid_time or snapshot_time)
        transaction_at_us = _datetime_us(query.as_of_transaction_time or snapshot_time)
        interval_parameters = (
            valid_at_us,
            valid_at_us,
            transaction_at_us,
            transaction_at_us,
        )
        superseding = connection.execute(
            "SELECT relationship_version_id,relationship_id,version,"
            "source_record_id,transaction_from_us,asserted_by_event_id,"
            "content_hash "
            "FROM memory_relationship_versions WHERE workspace_id=? "
            "AND target_record_id=? "
            "AND relationship_type IN ('supersedes','invalidates') "
            "AND source_record_id<>target_record_id "
            "AND valid_from_us<=? AND (valid_to_us IS NULL OR ?<valid_to_us) "
            "AND transaction_from_us<=? "
            "AND (transaction_to_us IS NULL OR ?<transaction_to_us) "
            "ORDER BY relationship_version_id LIMIT 2",
            (
                query.workspace_id,
                record_id,
                *interval_parameters,
            ),
        ).fetchall()
        if len(superseding) > 1:
            raise RetrievalRepositoryError("POLICY_STATE_UNAVAILABLE")
        superseding_id: str | None = None
        if superseding:
            relationship = superseding[0]
            _safe_hash(relationship[6])
            source_workspace = connection.execute(
                "SELECT workspace_id FROM memory_records WHERE record_id=?",
                (str(relationship[3]),),
            ).fetchone()
            event = self._source_event(
                connection, query.workspace_id, str(relationship[5])
            )
            if not (
                source_workspace is not None
                and str(source_workspace[0]) == query.workspace_id
                and event.stream_id == str(relationship[1])
                and event.stream_kind == "relationship"
                and event.stream_version == _plain_int(relationship[2], minimum=1)
                and event.recorded_at_us == _plain_int(relationship[4])
            ):
                raise RetrievalRepositoryError("POLICY_STATE_UNAVAILABLE")
            superseding_id = str(relationship[0])

        conflicts = connection.execute(
            "SELECT relationship_id,version,source_record_id,target_record_id,"
            "asserted_by_event_id,transaction_from_us,content_hash "
            "FROM memory_relationship_versions "
            "WHERE workspace_id=? AND relationship_type='conflicts_with' "
            "AND (source_record_id=? OR target_record_id=?) "
            "AND source_record_id<>target_record_id "
            "AND valid_from_us<=? AND (valid_to_us IS NULL OR ?<valid_to_us) "
            "AND transaction_from_us<=? "
            "AND (transaction_to_us IS NULL OR ?<transaction_to_us)",
            (
                query.workspace_id,
                record_id,
                record_id,
                *interval_parameters,
            ),
        ).fetchall()
        for relationship in conflicts:
            _safe_hash(relationship[6])
            endpoints = connection.execute(
                "SELECT count(*) FROM memory_records WHERE workspace_id=? "
                "AND record_id IN (?,?)",
                (
                    query.workspace_id,
                    str(relationship[2]),
                    str(relationship[3]),
                ),
            ).fetchone()
            event = self._source_event(
                connection, query.workspace_id, str(relationship[4])
            )
            if not (
                endpoints is not None
                and endpoints[0] == 2
                and event.stream_id == str(relationship[0])
                and event.stream_kind == "relationship"
                and event.stream_version == _plain_int(relationship[1], minimum=1)
                and event.recorded_at_us == _plain_int(relationship[5])
            ):
                raise RetrievalRepositoryError("POLICY_STATE_UNAVAILABLE")
        decisions = connection.execute(
            "SELECT status,created_at_us,decided_at_us "
            "FROM enrichment_decisions WHERE workspace_id=? "
            "AND target_record_id=? AND has_unresolved_contradiction=1 "
            "AND status IN ('proposed','accepted')",
            (query.workspace_id, record_id),
        ).fetchall()
        active_decision = False
        for decision in decisions:
            created_at = _plain_int(decision[1])
            if decision[0] == "proposed":
                active_decision = active_decision or created_at <= transaction_at_us
                continue
            if decision[0] != "accepted" or decision[2] is None:
                raise RetrievalRepositoryError("POLICY_STATE_UNAVAILABLE")
            decided_at = _plain_int(decision[2])
            active_decision = active_decision or (
                created_at <= decided_at <= transaction_at_us
            )
        return superseding_id, bool(conflicts or active_decision)


__all__ = [
    "RetrievalRepositoryError",
    "SQLiteRetrievalRepository",
    "sqlite_read_connection_factory",
]
