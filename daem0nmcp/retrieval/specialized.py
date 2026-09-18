"""Dependency-free providers for specialized v7 retrieval projections.

Every provider opens its own read-only SQLite connection inside a bounded
worker.  Candidate payloads contain opaque provenance and ranking metadata,
never canonical record, procedure, or outcome text.
"""

from __future__ import annotations

import asyncio
import json
import math
import re
import sqlite3
import time
from collections import deque
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from time import perf_counter_ns

from ..bounded_workers import BoundedWorkerBusyError, BoundedWorkerPool
from .specialized_contract import (
    PROCEDURE_FTS_BUILD_CONFIG_HASH,
    procedure_fts_table_name,
    specialized_manifest_matches_contract,
)
from .types import (
    Candidate,
    EvidenceRef,
    FusedCandidate,
    ProviderResult,
    ProviderStatus,
    RetrievalQuery,
)

_HASH = re.compile(r"^[0-9a-f]{64}$")
_TOKEN = re.compile(r"[^\W_]+", re.UNICODE)
_MAX_TERMS = 64
_MAX_TERM_CHARS = 64
MAX_GRAPH_DEPTH = 8
MAX_GRAPH_BRANCHING = 100
_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
_SPECIALIZED_WORKERS = BoundedWorkerPool(
    max_workers=4,
    thread_name_prefix="daem0nmcp-specialized",
)
_STOP_WORDS = frozenset(
    {
        "a",
        "an",
        "and",
        "as",
        "at",
        "be",
        "did",
        "do",
        "for",
        "how",
        "in",
        "is",
        "it",
        "of",
        "on",
        "or",
        "the",
        "to",
        "was",
        "what",
        "when",
        "which",
        "with",
    }
)
_OUTCOME_INTENT_TERMS = frozenset(
    {
        "fail",
        "failed",
        "failure",
        "outcome",
        "outcomes",
        "result",
        "results",
        "success",
        "successful",
        "work",
        "worked",
    }
)


@dataclass(frozen=True, slots=True)
class _ProjectionSnapshot:
    generation: int
    lexical_generation: int
    row_count: int
    details: Mapping[str, object]
    source_event_count: int
    source_event_root_hash: str


def _positive_timeout(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("timeout_seconds must be a positive finite number")
    try:
        timeout = float(value)
    except OverflowError as exc:
        raise ValueError("timeout_seconds must be a positive finite number") from exc
    if not math.isfinite(timeout) or timeout <= 0 or timeout > 60:
        raise ValueError("timeout_seconds must be a positive finite number")
    return timeout


def _positive_limit(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError("limit must be a positive integer")
    return value


def _bounded_positive_integer(
    value: object,
    field_name: str,
    maximum: int,
) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or value < 1
        or value > maximum
    ):
        raise ValueError(f"{field_name} must be between 1 and {maximum}")
    return value


def _sqlite_read_factory(
    connection: sqlite3.Connection | None,
    connection_factory: Callable[[], sqlite3.Connection] | None,
) -> Callable[[], sqlite3.Connection]:
    if connection_factory is not None:
        if connection is not None or not callable(connection_factory):
            raise ValueError(
                "provide exactly one SQLite connection or connection_factory"
            )
        return connection_factory
    if not isinstance(connection, sqlite3.Connection):
        raise ValueError("a SQLite connection or connection_factory is required")
    database_row = connection.execute("PRAGMA database_list").fetchone()
    database_path = "" if database_row is None else str(database_row[2])
    if not database_path:
        raise ValueError("in-memory SQLite requires a worker-local connection_factory")

    def open_connection() -> sqlite3.Connection:
        return sqlite3.connect(database_path, timeout=5.0)

    return open_connection


def _open_read_connection(
    factory: Callable[[], sqlite3.Connection],
) -> sqlite3.Connection:
    connection = factory()
    if not isinstance(connection, sqlite3.Connection):
        raise TypeError("connection_factory must return a SQLite connection")
    connection.execute("PRAGMA query_only=ON")
    return connection


def _datetime_us(value: datetime) -> int:
    if value is None:
        raise ValueError("datetime is required")
    delta = value.astimezone(timezone.utc) - _EPOCH
    return (delta.days * 86_400 + delta.seconds) * 1_000_000 + delta.microseconds


def _transaction_datetime(value: object) -> datetime | None:
    try:
        return datetime.fromtimestamp(int(value) / 1_000_000, timezone.utc)
    except (OSError, OverflowError, TypeError, ValueError):
        return None


def _query_terms(text: str) -> tuple[str, ...]:
    unique: list[str] = []
    seen: set[str] = set()
    for term in _TOKEN.findall(text.casefold()):
        if term in _STOP_WORDS or len(term) > _MAX_TERM_CHARS or term in seen:
            continue
        seen.add(term)
        unique.append(term)
        if len(unique) == _MAX_TERMS:
            break
    return tuple(unique)


class _SQLiteProjectionProvider:
    name = ""

    def __init__(
        self,
        connection: sqlite3.Connection | None = None,
        *,
        connection_factory: Callable[[], sqlite3.Connection] | None = None,
        timeout_seconds: float = 2.0,
        worker_pool: BoundedWorkerPool | None = None,
        clock_us: Callable[[], int] | None = None,
    ) -> None:
        self._connection_factory = _sqlite_read_factory(connection, connection_factory)
        self._timeout_seconds = _positive_timeout(timeout_seconds)
        if worker_pool is not None and not isinstance(worker_pool, BoundedWorkerPool):
            raise ValueError("worker_pool must be a BoundedWorkerPool")
        if clock_us is not None and not callable(clock_us):
            raise ValueError("clock_us must be callable")
        self._worker_pool = worker_pool or _SPECIALIZED_WORKERS
        self._clock_us = clock_us or (lambda: time.time_ns() // 1_000)

    def _capture_now_us(self) -> int:
        value = self._clock_us()
        if (
            isinstance(value, bool)
            or not isinstance(value, int)
            or value < -(2**63)
            or value > 2**63 - 1
        ):
            raise ValueError("clock_us must return a signed 64-bit integer")
        return value

    async def search(
        self,
        query: RetrievalQuery,
        limit: int,
    ) -> ProviderResult:
        return await self._run(query, limit)

    async def _run(
        self,
        query: RetrievalQuery,
        limit: int,
        *extra: object,
    ) -> ProviderResult:
        if not isinstance(query, RetrievalQuery):
            raise ValueError("query must be a RetrievalQuery")
        bounded_limit = min(_positive_limit(limit), query.candidate_limit)
        started = perf_counter_ns()
        try:
            return await asyncio.wait_for(
                self._worker_pool.run(
                    lambda: self._search_sync(query, bounded_limit, started, *extra)
                ),
                timeout=self._timeout_seconds,
            )
        except asyncio.TimeoutError:
            return self._result(
                started,
                status="degraded",
                reason=f"{self.name.upper()}_PROVIDER_TIMEOUT",
            )
        except BoundedWorkerBusyError:
            return self._result(
                started,
                status="degraded",
                reason=f"{self.name.upper()}_PROVIDER_BUSY",
            )
        except Exception:
            return self._result(
                started,
                status="degraded",
                reason=f"{self.name.upper()}_PROVIDER_FAILED",
            )

    def _search_sync(
        self,
        query: RetrievalQuery,
        limit: int,
        started: int,
        *extra: object,
    ) -> ProviderResult:
        connection = _open_read_connection(self._connection_factory)
        try:
            connection.execute("BEGIN")
            snapshot, reason = self._active_snapshot(connection, query.workspace_id)
            if snapshot is None:
                return self._result(
                    started,
                    status="unavailable",
                    reason=reason,
                )
            return self._search_connection(
                connection, query, limit, started, snapshot, *extra
            )
        finally:
            connection.close()

    def _active_snapshot(
        self,
        connection: sqlite3.Connection,
        workspace_id: str,
    ) -> tuple[_ProjectionSnapshot | None, str]:
        named_rows = connection.execute(
            "SELECT generation,source_event_count,source_event_root_hash,"
            "row_count,details_json FROM projection_manifests "
            "WHERE workspace_id=? "
            "AND projection_name=? AND status='active'",
            (workspace_id, self.name),
        ).fetchall()
        if len(named_rows) != 1:
            return None, f"{self.name.upper()}_UNAVAILABLE"
        lexical_rows = connection.execute(
            "SELECT generation,source_event_count,source_event_root_hash,"
            "details_json "
            "FROM projection_manifests WHERE workspace_id=? "
            "AND projection_name='lexical' AND status='active'",
            (workspace_id,),
        ).fetchall()
        if len(lexical_rows) != 1:
            return None, f"{self.name.upper()}_CONTENT_UNAVAILABLE"

        named = named_rows[0]
        lexical = lexical_rows[0]
        values = (named[0], named[1], named[3], lexical[0], lexical[1])
        if any(
            isinstance(value, bool)
            or not isinstance(value, int)
            or value < (1 if index in {0, 3} else 0)
            for index, value in enumerate(values)
        ):
            return None, f"{self.name.upper()}_STALE"
        if (
            not isinstance(named[2], str)
            or _HASH.fullmatch(named[2]) is None
            or not isinstance(lexical[2], str)
            or _HASH.fullmatch(lexical[2]) is None
            or named[1] != lexical[1]
            or named[2] != lexical[2]
        ):
            return None, f"{self.name.upper()}_STALE"
        try:
            named_details = json.loads(str(named[4]))
            lexical_details = json.loads(str(lexical[3]))
        except (TypeError, ValueError, RecursionError):
            return None, f"{self.name.upper()}_STALE"
        if not isinstance(named_details, Mapping) or not isinstance(
            lexical_details, Mapping
        ):
            return None, f"{self.name.upper()}_STALE"
        rebuild_markers = {
            "rebuild_required_at_us",
            "rebuild_required_event_id",
        }
        if not specialized_manifest_matches_contract(
            named_details,
            workspace_id,
            self.name,
            named[0],
        ) or rebuild_markers.intersection(lexical_details):
            return None, f"{self.name.upper()}_STALE"
        try:
            if self.name == "procedure":
                count_row = connection.execute(
                    "SELECT count(*) FROM record_procedures "
                    "WHERE workspace_id=? AND projection_generation=?",
                    (workspace_id, named[0]),
                ).fetchone()
            elif self.name == "outcome":
                count_row = connection.execute(
                    "SELECT count(*) FROM record_outcome_view "
                    "WHERE workspace_id=? AND projection_generation=?",
                    (workspace_id, named[0]),
                ).fetchone()
            elif self.name == "temporal":
                count_row = connection.execute(
                    "SELECT count(*) FROM memory_fact_versions WHERE workspace_id=?",
                    (workspace_id,),
                ).fetchone()
            elif self.name == "graph":
                count_row = connection.execute(
                    "SELECT "
                    "(SELECT count(*) FROM memory_relationship_versions "
                    " WHERE workspace_id=?) + "
                    "(SELECT count(*) FROM memory_fact_versions "
                    " WHERE workspace_id=? AND object_kind='record_ref')",
                    (workspace_id, workspace_id),
                ).fetchone()
            else:
                return None, f"{self.name.upper()}_UNAVAILABLE"
        except sqlite3.Error:
            return None, f"{self.name.upper()}_STALE"
        if count_row is None or count_row[0] != named[3]:
            return None, f"{self.name.upper()}_STALE"
        return (
            _ProjectionSnapshot(
                generation=named[0],
                lexical_generation=lexical[0],
                row_count=named[3],
                details=dict(named_details),
                source_event_count=named[1],
                source_event_root_hash=named[2],
            ),
            "",
        )

    def _search_connection(
        self,
        connection: sqlite3.Connection,
        query: RetrievalQuery,
        limit: int,
        started: int,
        snapshot: _ProjectionSnapshot,
        *extra: object,
    ) -> ProviderResult:
        raise NotImplementedError

    def _result(
        self,
        started: int,
        *,
        candidates: tuple[Candidate, ...] = (),
        status: ProviderStatus = "ready",
        reason: str | None = None,
        generation: int | None = None,
    ) -> ProviderResult:
        return ProviderResult(
            provider=self.name,
            candidates=candidates,
            status=status,
            manifest_generation=generation,
            elapsed_ms=(perf_counter_ns() - started) / 1_000_000,
            reason=reason,
        )


class TemporalProvider(_SQLiteProjectionProvider):
    """Bitemporal fact candidates from the active temporal projection."""

    name = "temporal"

    def _search_connection(
        self,
        connection: sqlite3.Connection,
        query: RetrievalQuery,
        limit: int,
        started: int,
        snapshot: _ProjectionSnapshot,
        *extra: object,
    ) -> ProviderResult:
        del extra
        captured_now = self._capture_now_us()
        valid_at = (
            captured_now
            if query.as_of_valid_time is None
            else _datetime_us(query.as_of_valid_time)
        )
        transaction_at = (
            captured_now
            if query.as_of_transaction_time is None
            else _datetime_us(query.as_of_transaction_time)
        )
        explicit_as_of = (
            query.as_of_valid_time is not None
            or query.as_of_transaction_time is not None
        )
        terms = _query_terms(query.text)
        score_parts = tuple(
            "CASE WHEN instr(lower(fact.predicate || ' ' || "
            "fact.object_json), ?) > 0 THEN 1 ELSE 0 END"
            for _term in terms
        )
        score_sql = " + ".join(score_parts) if score_parts else "0"
        text_filter = ""
        if terms and not explicit_as_of:
            text_filter = (
                " AND ("
                + " OR ".join(
                    "instr(lower(fact.predicate || ' ' || fact.object_json), ?) > 0"
                    for _term in terms
                )
                + ")"
            )
        parameters: list[object] = [
            transaction_at,
            *terms,
            snapshot.lexical_generation,
            query.workspace_id,
            transaction_at,
            int(query.include_invalidated),
            transaction_at,
            valid_at,
            int(query.include_invalidated),
            valid_at,
        ]
        if text_filter:
            parameters.extend(terms)
        scan_limit = min(4_000, max(limit * 4, limit))
        parameters.extend((transaction_at, valid_at, scan_limit))
        rows = connection.execute(
            f"""
            SELECT fact.fact_version_id,fact.fact_id,fact.version,
                   fact.subject_record_id,fact.valid_to_us,
                   fact.transaction_from_us,fact.transaction_to_us,
                   fact.asserted_by_event_id,fact.retracted_by_event_id,
                   document.content_hash,
                   (
                       SELECT successor.fact_version_id
                       FROM memory_fact_versions AS successor
                       WHERE successor.workspace_id=fact.workspace_id
                         AND successor.fact_id=fact.fact_id
                         AND successor.version>fact.version
                         AND successor.transaction_from_us<=?
                       ORDER BY successor.version
                       LIMIT 1
                   ) AS successor_version_id,
                   {score_sql} AS text_score
            FROM memory_fact_versions AS fact
            JOIN retrieval_documents AS document
              ON document.workspace_id=fact.workspace_id
             AND document.record_id=fact.subject_record_id
             AND document.projection_generation=?
            WHERE fact.workspace_id=?
              AND fact.transaction_from_us<=?
              AND (?=1 OR fact.transaction_to_us IS NULL
                   OR ?<fact.transaction_to_us)
              AND fact.valid_from_us<=?
              AND (?=1 OR fact.valid_to_us IS NULL
                   OR ?<fact.valid_to_us)
              {text_filter}
            ORDER BY text_score DESC,
                     CASE WHEN fact.transaction_to_us IS NOT NULL
                               AND fact.transaction_to_us<=?
                          THEN 1 ELSE 0 END,
                     CASE WHEN fact.valid_to_us IS NOT NULL
                               AND fact.valid_to_us<=?
                          THEN 1 ELSE 0 END,
                     fact.transaction_from_us DESC,
                     fact.fact_version_id
            LIMIT ?
            """,
            tuple(parameters),
        ).fetchall()

        candidates: list[Candidate] = []
        for row in rows:
            version_id = str(row[0])
            invalidated = (row[4] is not None and int(row[4]) <= valid_at) or (
                row[6] is not None and int(row[6]) <= transaction_at
            )
            if invalidated:
                successor_id = row[10]
                if successor_id is None:
                    continue
                policy_notes = (
                    "SUPERSEDED",
                    f"SUPERSEDED_BY:{successor_id}",
                )
            else:
                policy_notes = ()
            candidates.append(
                Candidate(
                    evidence=EvidenceRef(
                        record_id=str(row[3]),
                        event_id=str(row[7]),
                        content_hash=str(row[9]),
                        version_id=version_id,
                        provider=self.name,
                    ),
                    rank=len(candidates) + 1,
                    raw_score=float(row[11]),
                    channels=frozenset({self.name}),
                    policy_notes=policy_notes,
                    transaction_time=_transaction_datetime(row[5]),
                )
            )
            if len(candidates) == limit:
                break
        return self._result(
            started,
            candidates=tuple(candidates),
            generation=snapshot.generation,
        )


class ProcedureProvider(_SQLiteProjectionProvider):
    """Structured procedure-step ranker; prose never invents procedures."""

    name = "procedure"

    def _search_connection(
        self,
        connection: sqlite3.Connection,
        query: RetrievalQuery,
        limit: int,
        started: int,
        snapshot: _ProjectionSnapshot,
        *extra: object,
    ) -> ProviderResult:
        del extra
        terms = _query_terms(query.text)
        if not terms:
            return self._result(started, generation=snapshot.generation)
        fts_table = procedure_fts_table_name(query.workspace_id, snapshot.generation)
        if (
            snapshot.details.get("fts_table") != fts_table
            or snapshot.details.get("build_config_hash")
            != PROCEDURE_FTS_BUILD_CONFIG_HASH
            or not self._valid_fts_partition(
                connection, query.workspace_id, snapshot, fts_table
            )
        ):
            return self._result(
                started,
                status="unavailable",
                reason="PROCEDURE_STALE",
                generation=snapshot.generation,
            )
        fts_query = " OR ".join(f'"{term}"' for term in terms)
        scan_limit = min(10_000, max(limit * _MAX_TERMS, limit))
        rows = connection.execute(
            f"""
            SELECT procedure.record_id,procedure.ordinal,
                   procedure.step_hash,
                   procedure.source_event_id,document.content_hash,
                   document.transaction_from_us,
                   bm25(
                       "{fts_table}",0.0,0.0,0.0,0.0,1.0
                   ) AS text_score
            FROM "{fts_table}"
            JOIN record_procedures AS procedure
              ON procedure.workspace_id=?
             AND procedure.projection_generation=?
             AND procedure.record_id="{fts_table}".record_id
             AND procedure.ordinal=CAST("{fts_table}".ordinal AS INTEGER)
             AND procedure.step_hash="{fts_table}".step_hash
             AND procedure.source_event_id="{fts_table}".source_event_id
            JOIN retrieval_documents AS document
              ON document.workspace_id=procedure.workspace_id
             AND document.record_id=procedure.record_id
             AND document.projection_generation=?
            WHERE "{fts_table}" MATCH ?
            ORDER BY text_score ASC,procedure.record_id,procedure.ordinal
            LIMIT ?
            """,
            (
                query.workspace_id,
                snapshot.generation,
                snapshot.lexical_generation,
                fts_query,
                scan_limit,
            ),
        ).fetchall()

        grouped: dict[str, dict[str, object]] = {}
        for row in rows:
            record_id = str(row[0])
            entry = grouped.setdefault(
                record_id,
                {
                    "content_hash": str(row[4]),
                    "event_id": str(row[3]),
                    "best_score": float(row[6]),
                    "steps": [],
                    "transaction_from_us": row[5],
                },
            )
            steps = entry["steps"]
            if not isinstance(steps, list):
                raise TypeError("invalid procedure aggregation")
            score = float(row[6])
            if not math.isfinite(score):
                raise ValueError("invalid procedure FTS score")
            entry["best_score"] = min(float(entry["best_score"]), score)
            step_hash = str(row[2])
            if _HASH.fullmatch(step_hash) is None:
                raise ValueError("invalid procedure step hash")
            steps.append((int(row[1]), step_hash, str(row[3])))

        ranked: list[tuple[float, str, dict[str, object]]] = []
        for record_id, entry in grouped.items():
            ranked.append((float(entry["best_score"]), record_id, entry))
        ranked.sort(key=lambda item: (item[0], item[1]))

        candidates: list[Candidate] = []
        for best_score, record_id, entry in ranked[:limit]:
            steps = entry["steps"]
            if not isinstance(steps, list):
                raise TypeError("invalid procedure step aggregation")
            ordered_steps = sorted(steps)
            event_id = str(ordered_steps[0][2])
            candidates.append(
                Candidate(
                    evidence=EvidenceRef(
                        record_id=record_id,
                        event_id=event_id,
                        content_hash=str(entry["content_hash"]),
                        version_id=None,
                        provider=self.name,
                    ),
                    rank=len(candidates) + 1,
                    raw_score=max(0.0, -best_score),
                    channels=frozenset({self.name}),
                    policy_notes=tuple(
                        f"PROCEDURE_STEP:{ordinal}:{step_hash}"
                        for ordinal, step_hash, _event_id_value in ordered_steps
                    ),
                    transaction_time=_transaction_datetime(
                        entry["transaction_from_us"]
                    ),
                )
            )
        return self._result(
            started,
            candidates=tuple(candidates),
            generation=snapshot.generation,
        )

    @staticmethod
    def _valid_fts_partition(
        connection: sqlite3.Connection,
        workspace_id: str,
        snapshot: _ProjectionSnapshot,
        fts_table: str,
    ) -> bool:
        definition = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name=?",
            (fts_table,),
        ).fetchone()
        if (
            definition is None
            or not isinstance(definition[0], str)
            or "using fts5" not in definition[0].casefold()
        ):
            return False
        total = connection.execute(f'SELECT count(*) FROM "{fts_table}"').fetchone()
        if total is None or total[0] != snapshot.row_count:
            return False
        matched = connection.execute(
            f"""
            SELECT count(*) FROM (
                SELECT indexed.record_id,indexed.ordinal
                FROM "{fts_table}" AS indexed
                JOIN record_procedures AS procedure
                  ON procedure.workspace_id=?
                 AND procedure.projection_generation=?
                 AND procedure.record_id=indexed.record_id
                 AND procedure.ordinal=CAST(indexed.ordinal AS INTEGER)
                 AND procedure.step_hash=indexed.step_hash
                 AND procedure.source_event_id=indexed.source_event_id
                 AND procedure.step_text=indexed.step_text
                GROUP BY indexed.record_id,indexed.ordinal
                HAVING count(*)=1
            )
            """,
            (workspace_id, snapshot.generation),
        ).fetchone()
        return matched is not None and matched[0] == snapshot.row_count


class OutcomeProvider(_SQLiteProjectionProvider):
    """Latest outcome candidates with policy metadata but no score boost."""

    name = "outcome"

    def _search_connection(
        self,
        connection: sqlite3.Connection,
        query: RetrievalQuery,
        limit: int,
        started: int,
        snapshot: _ProjectionSnapshot,
        *extra: object,
    ) -> ProviderResult:
        del extra
        captured_now = self._capture_now_us()
        transaction_at = (
            captured_now
            if query.as_of_transaction_time is None
            else _datetime_us(query.as_of_transaction_time)
        )
        terms = tuple(
            term
            for term in _query_terms(query.text)
            if term not in _OUTCOME_INTENT_TERMS
        )
        score_sql = (
            " + ".join(
                "CASE WHEN instr(lower(outcome.outcome_text), ?) > 0 THEN 1 ELSE 0 END"
                for _term in terms
            )
            if terms
            else "0"
        )
        text_filter = ""
        if terms:
            text_filter = (
                " AND (outcome.worked=0 OR "
                + " OR ".join(
                    "instr(lower(outcome.outcome_text), ?) > 0" for _term in terms
                )
                + ")"
            )
        rows = connection.execute(
            f"""
            SELECT outcome.record_id,outcome.worked,
                   outcome.outcome_event_id,outcome.transaction_at_us,
                   document.content_hash,{score_sql} AS text_score
            FROM record_outcome_view AS outcome
            JOIN retrieval_documents AS document
              ON document.workspace_id=outcome.workspace_id
             AND document.record_id=outcome.record_id
             AND document.projection_generation=?
            WHERE outcome.workspace_id=?
              AND outcome.projection_generation=?
              AND outcome.transaction_at_us<=?
              {text_filter}
            ORDER BY text_score DESC,outcome.transaction_at_us DESC,
                     outcome.record_id
            LIMIT ?
            """,
            (
                *terms,
                snapshot.lexical_generation,
                query.workspace_id,
                snapshot.generation,
                transaction_at,
                *(terms if text_filter else ()),
                limit,
            ),
        ).fetchall()
        candidates: list[Candidate] = []
        for row in rows:
            worked = row[1]
            if worked not in {None, 0, 1}:
                raise ValueError("invalid outcome state")
            state = (
                "OUTCOME_UNKNOWN"
                if worked is None
                else "OUTCOME_SUCCEEDED"
                if worked == 1
                else "OUTCOME_FAILED"
            )
            candidates.append(
                Candidate(
                    evidence=EvidenceRef(
                        record_id=str(row[0]),
                        event_id=str(row[2]),
                        content_hash=str(row[4]),
                        version_id=None,
                        provider=self.name,
                    ),
                    rank=len(candidates) + 1,
                    raw_score=None,
                    channels=frozenset({self.name}),
                    policy_notes=(state,),
                    transaction_time=_transaction_datetime(row[3]),
                )
            )
        return self._result(
            started,
            candidates=tuple(candidates),
            generation=snapshot.generation,
        )


class GraphProvider(_SQLiteProjectionProvider):
    """Bounded active relationship traversal from explicit ranked seeds."""

    name = "graph"

    def __init__(
        self,
        connection: sqlite3.Connection | None = None,
        *,
        connection_factory: Callable[[], sqlite3.Connection] | None = None,
        max_depth: int = 2,
        max_branching: int = 25,
        timeout_seconds: float = 2.0,
        worker_pool: BoundedWorkerPool | None = None,
        clock_us: Callable[[], int] | None = None,
    ) -> None:
        self.max_depth = _bounded_positive_integer(
            max_depth, "max_depth", MAX_GRAPH_DEPTH
        )
        self.max_branching = _bounded_positive_integer(
            max_branching, "max_branching", MAX_GRAPH_BRANCHING
        )
        super().__init__(
            connection,
            connection_factory=connection_factory,
            timeout_seconds=timeout_seconds,
            worker_pool=worker_pool,
            clock_us=clock_us,
        )

    async def search(
        self,
        query: RetrievalQuery,
        limit: int,
        *,
        seeds: tuple[FusedCandidate, ...],
    ) -> ProviderResult:
        if not isinstance(query, RetrievalQuery):
            raise ValueError("query must be a RetrievalQuery")
        seed_values = self._validated_seeds(seeds, query.candidate_limit)
        return await self._run(query, limit, seed_values)

    @staticmethod
    def _validated_seeds(
        seeds: tuple[FusedCandidate, ...],
        candidate_limit: int,
    ) -> tuple[FusedCandidate, ...]:
        if (
            not isinstance(seeds, tuple)
            or not seeds
            or len(seeds) > candidate_limit
            or not all(isinstance(seed, FusedCandidate) for seed in seeds)
        ):
            raise ValueError("graph seeds must be a bounded non-empty tuple")
        allowed = frozenset({"lexical", "dense"})
        for seed in seeds:
            if (
                not seed.channels.issubset(allowed)
                or not seed.channels
                or seed.version_id is not None
                or seed.evidence.relation_path
                or any(
                    evidence.provider not in allowed
                    or evidence.relation_path
                    or evidence.version_id is not None
                    for evidence in seed.evidence_refs
                )
            ):
                raise ValueError(
                    "graph seeds must originate only from lexical or dense"
                )

        def seed_key(seed: FusedCandidate) -> tuple[object, ...]:
            ranks = dict(seed.channel_ranks)
            return (
                min(ranks.values()),
                ranks.get("lexical", candidate_limit + 1),
                seed.record_id,
            )

        ordered: list[FusedCandidate] = []
        seen: set[str] = set()
        for seed in sorted(seeds, key=seed_key):
            if seed.record_id not in seen:
                seen.add(seed.record_id)
                ordered.append(seed)
        return tuple(ordered)

    def _search_connection(
        self,
        connection: sqlite3.Connection,
        query: RetrievalQuery,
        limit: int,
        started: int,
        snapshot: _ProjectionSnapshot,
        *extra: object,
    ) -> ProviderResult:
        if len(extra) != 1 or not isinstance(extra[0], tuple):
            raise ValueError("graph traversal requires validated seeds")
        seeds = extra[0]
        if not all(isinstance(seed, FusedCandidate) for seed in seeds):
            raise ValueError("graph traversal requires validated seeds")

        seed_ids = tuple(seed.record_id for seed in seeds)
        active_documents: dict[str, tuple[str, str]] = {}
        for offset in range(0, len(seed_ids), 400):
            chunk = seed_ids[offset : offset + 400]
            placeholders = ",".join("?" for _value in chunk)
            rows = connection.execute(
                "SELECT record_id,content_hash,source_event_id "
                "FROM retrieval_documents "
                "WHERE workspace_id=? AND projection_generation=? "
                f"AND record_id IN ({placeholders})",
                (
                    query.workspace_id,
                    snapshot.lexical_generation,
                    *chunk,
                ),
            ).fetchall()
            active_documents.update(
                (str(row[0]), (str(row[1]), str(row[2]))) for row in rows
            )
        active_generations = {"lexical": snapshot.lexical_generation}
        if any("dense" in seed.channels for seed in seeds):
            dense_rows = connection.execute(
                "SELECT generation,source_event_count,"
                "source_event_root_hash,details_json "
                "FROM projection_manifests WHERE workspace_id=? "
                "AND projection_name='dense' AND status='active'",
                (query.workspace_id,),
            ).fetchall()
            if len(dense_rows) == 1:
                dense = dense_rows[0]
                try:
                    dense_details = json.loads(str(dense[3]))
                except (TypeError, ValueError, RecursionError):
                    dense_details = None
                if (
                    isinstance(dense[0], int)
                    and not isinstance(dense[0], bool)
                    and dense[0] > 0
                    and dense[1] == snapshot.source_event_count
                    and dense[2] == snapshot.source_event_root_hash
                    and isinstance(dense_details, Mapping)
                    and "rebuild_required_at_us" not in dense_details
                    and "rebuild_required_event_id" not in dense_details
                ):
                    active_generations["dense"] = dense[0]

        active_seed_ids: set[str] = set()
        for seed in seeds:
            document = active_documents.get(seed.record_id)
            manifest_generations = dict(seed.manifest_generations)
            if (
                document is None
                or any(
                    active_generations.get(channel) != generation
                    for channel, generation in manifest_generations.items()
                )
                or any(
                    evidence.record_id != seed.record_id
                    or evidence.content_hash != document[0]
                    or evidence.event_id != document[1]
                    for evidence in seed.evidence_refs
                )
            ):
                continue
            active_seed_ids.add(seed.record_id)
        stale_seeds = set(seed_ids).difference(active_seed_ids)
        if not active_seed_ids:
            return self._result(
                started,
                status="degraded",
                reason="GRAPH_SEEDS_STALE",
                generation=snapshot.generation,
            )

        captured_now = self._capture_now_us()
        valid_at = (
            captured_now
            if query.as_of_valid_time is None
            else _datetime_us(query.as_of_valid_time)
        )
        transaction_at = (
            captured_now
            if query.as_of_transaction_time is None
            else _datetime_us(query.as_of_transaction_time)
        )
        visited = set(seed_ids)
        queue: deque[tuple[str, tuple[str, ...], int]] = deque(
            (seed.record_id, (), 0)
            for seed in seeds
            if seed.record_id in active_seed_ids
        )
        candidates: list[Candidate] = []
        while queue and len(candidates) < limit:
            record_id, path, depth = queue.popleft()
            if depth >= self.max_depth:
                continue
            scan_limit = min(
                query.candidate_limit + self.max_branching,
                self.max_branching + len(visited),
            )
            rows = connection.execute(
                """
                SELECT edge.edge_id,edge.other_id,
                       edge.transaction_from_us,
                       document.content_hash,document.source_event_id
                FROM (
                    SELECT relationship.relationship_version_id AS edge_id,
                           CASE WHEN relationship.source_record_id=?
                                THEN relationship.target_record_id
                                ELSE relationship.source_record_id
                           END AS other_id,
                           relationship.transaction_from_us
                    FROM memory_relationship_versions AS relationship
                    WHERE relationship.workspace_id=?
                      AND (relationship.source_record_id=?
                           OR relationship.target_record_id=?)
                      AND relationship.source_record_id<>
                          relationship.target_record_id
                      AND relationship.valid_from_us<=?
                      AND (relationship.valid_to_us IS NULL
                           OR ?<relationship.valid_to_us)
                      AND relationship.transaction_from_us<=?
                      AND (relationship.transaction_to_us IS NULL
                           OR ?<relationship.transaction_to_us)
                    UNION ALL
                    SELECT fact.fact_version_id AS edge_id,
                           CASE WHEN fact.subject_record_id=?
                                THEN json_extract(fact.object_json,'$')
                                ELSE fact.subject_record_id
                           END AS other_id,
                           fact.transaction_from_us
                    FROM memory_fact_versions AS fact
                    WHERE fact.workspace_id=?
                      AND fact.object_kind='record_ref'
                      AND json_valid(fact.object_json)=1
                      AND json_type(fact.object_json)='text'
                      AND (fact.subject_record_id=?
                           OR json_extract(fact.object_json,'$')=?)
                      AND fact.subject_record_id<>
                          json_extract(fact.object_json,'$')
                      AND fact.valid_from_us<=?
                      AND (fact.valid_to_us IS NULL
                           OR ?<fact.valid_to_us)
                      AND fact.transaction_from_us<=?
                      AND (fact.transaction_to_us IS NULL
                           OR ?<fact.transaction_to_us)
                ) AS edge
                JOIN retrieval_documents AS document
                  ON document.workspace_id=?
                 AND document.record_id=edge.other_id
                 AND document.projection_generation=?
                ORDER BY edge.edge_id,edge.other_id
                LIMIT ?
                """,
                (
                    record_id,
                    query.workspace_id,
                    record_id,
                    record_id,
                    valid_at,
                    valid_at,
                    transaction_at,
                    transaction_at,
                    record_id,
                    query.workspace_id,
                    record_id,
                    record_id,
                    valid_at,
                    valid_at,
                    transaction_at,
                    transaction_at,
                    query.workspace_id,
                    snapshot.lexical_generation,
                    scan_limit,
                ),
            ).fetchall()
            branches = 0
            for row in rows:
                other_id = str(row[1])
                if other_id in visited:
                    continue
                visited.add(other_id)
                relation_id = str(row[0])
                relation_path = (*path, relation_id)
                next_depth = depth + 1
                candidates.append(
                    Candidate(
                        evidence=EvidenceRef(
                            record_id=other_id,
                            event_id=str(row[4]),
                            content_hash=str(row[3]),
                            version_id=None,
                            relation_path=relation_path,
                            provider=self.name,
                        ),
                        rank=len(candidates) + 1,
                        raw_score=1.0 / next_depth,
                        channels=frozenset({self.name}),
                        policy_notes=(f"GRAPH_DEPTH:{next_depth}",),
                        transaction_time=_transaction_datetime(row[2]),
                    )
                )
                branches += 1
                if next_depth < self.max_depth:
                    queue.append((other_id, relation_path, next_depth))
                if branches == self.max_branching or len(candidates) == limit:
                    break
        return self._result(
            started,
            candidates=tuple(candidates),
            status="degraded" if stale_seeds else "ready",
            reason="GRAPH_SEEDS_STALE" if stale_seeds else None,
            generation=snapshot.generation,
        )


__all__ = [
    "GraphProvider",
    "MAX_GRAPH_BRANCHING",
    "MAX_GRAPH_DEPTH",
    "OutcomeProvider",
    "ProcedureProvider",
    "PROCEDURE_FTS_BUILD_CONFIG_HASH",
    "TemporalProvider",
    "procedure_fts_table_name",
]
