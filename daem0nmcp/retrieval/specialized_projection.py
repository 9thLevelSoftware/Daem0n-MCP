"""Generation-staged lifecycle for specialized retrieval projections."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass

from ..event_store import canonical_json_bytes, deterministic_id, sha256_json
from .jobs import ProjectionJobRunner
from .projections import collect_superseded_generations
from .specialized_contract import (
    SPECIALIZED_BUILDER_VERSION,
    specialized_projection_contract,
)

_WORKSPACE_ID = re.compile(r"^ws_[0-9a-f]{24}$")
_PROJECTIONS = frozenset({"graph", "outcome", "procedure", "temporal"})
_BUILDER_VERSION = SPECIALIZED_BUILDER_VERSION
_OUTCOME_ASSERTION_EVENT_TYPES = frozenset(
    {"legacy.memory_state_imported", "memory.outcome_recorded"}
)


class SpecializedProjectionBuildError(RuntimeError):
    """A specialized staging generation failed closed."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(f"{code}: {message}")


@dataclass(frozen=True, slots=True)
class SpecializedProjectionBuildResult:
    projection_name: str
    generation: int
    status: str
    row_count: int
    source_event_count: int
    source_event_root_hash: str
    content_digest: str
    build_config_hash: str
    builder_contract_hash: str
    staging_manifest_id: str
    storage_target: str | None = None
    dry_run: bool = False
    capability_status: str = "ready"
    capability_reason: str | None = None
    cursor_recorded_at_us: int | None = None
    cursor_event_id: str | None = None
    active_manifest_id: str | None = None
    active_generation: int | None = None
    active_status: str | None = None
    active_row_count: int = 0
    active_content_digest: str | None = None
    row_count_delta: int = 0
    content_digest_changed: bool = True
    reused: bool = False


@dataclass(frozen=True, slots=True)
class _ProcedureStep:
    record_id: str
    ordinal: int
    step_text: str
    step_hash: str
    source_event_id: str


@dataclass(frozen=True, slots=True)
class _OutcomeRow:
    record_id: str
    worked: int | None
    outcome_text: str | None
    outcome_event_id: str
    transaction_at_us: int


class SpecializedProjectionBuilder:
    """Build procedure, outcome, temporal, and graph projection manifests."""

    def __init__(
        self,
        connection: sqlite3.Connection,
        *,
        clock_us: Callable[[], int] | None = None,
    ) -> None:
        if not isinstance(connection, sqlite3.Connection):
            raise ValueError("a SQLite connection is required")
        if clock_us is not None and not callable(clock_us):
            raise ValueError("clock_us must be callable")
        self.connection = connection
        self._clock_us = clock_us or (lambda: time.time_ns() // 1_000)
        self._savepoint_number = 0

    def rebuild(
        self,
        workspace_id: str,
        projection_name: str,
        *,
        dry_run: bool = False,
        force: bool = False,
        expected_snapshot: tuple[int, str, tuple[int, str] | None] | None = None,
        candidate_populator: Callable[[int], None] | None = None,
        before_activate: Callable[[], None] | None = None,
        before_commit: Callable[[SpecializedProjectionBuildResult], None] | None = None,
    ) -> SpecializedProjectionBuildResult:
        self._validate_request(workspace_id, projection_name)
        if not isinstance(force, bool):
            raise SpecializedProjectionBuildError(
                "INVALID_PROJECTION", "force must be boolean"
            )
        if candidate_populator is not None and (
            projection_name != "graph" or not callable(candidate_populator)
        ):
            raise SpecializedProjectionBuildError(
                "INVALID_PROJECTION", "candidate populator is invalid"
            )
        if before_activate is not None and not callable(before_activate):
            raise SpecializedProjectionBuildError(
                "INVALID_PROJECTION", "activation guard is invalid"
            )
        if before_commit is not None and not callable(before_commit):
            raise SpecializedProjectionBuildError(
                "INVALID_PROJECTION", "commit callback is invalid"
            )
        self._require_schema(projection_name)
        owns_transaction = not self.connection.in_transaction
        self._savepoint_number += 1
        savepoint = f"specialized_build_{self._savepoint_number}"
        try:
            # Runtime builders own their connection. Keep their expensive
            # canonical reads and digest work outside SQLite's single-writer
            # transaction, then publish from a short validated write phase.
            if not owns_transaction:
                self.connection.execute(f"SAVEPOINT {savepoint}")
            starting_identity = self._event_identity(workspace_id)
            steps: tuple[_ProcedureStep, ...] = ()
            outcomes: tuple[_OutcomeRow, ...] = ()
            typed_rows: tuple[dict[str, object], ...] = ()
            capability_ready = True
            if projection_name == "procedure":
                try:
                    self._probe_fts5()
                except sqlite3.Error:
                    capability_ready = False
                steps = self._procedure_steps(workspace_id)
                row_count = len(steps)
            elif projection_name == "outcome":
                outcomes = self._outcome_rows(workspace_id)
                row_count = len(outcomes)
            else:
                typed_rows = self._typed_rows(workspace_id, projection_name)
                row_count = len(typed_rows)
            event_count, event_root, cursor = self._event_snapshot(workspace_id)
            if (
                expected_snapshot is not None
                and (
                    event_count,
                    event_root,
                    cursor,
                )
                != expected_snapshot
            ):
                raise SpecializedProjectionBuildError(
                    "PROJECTION_BUILD_SUPERSEDED",
                    "source event snapshot changed while staging",
                )
            if starting_identity != (event_count, cursor):
                raise SpecializedProjectionBuildError(
                    "PROJECTION_BUILD_SUPERSEDED",
                    "source event snapshot changed while staging",
                )
            active = self._active_manifest(workspace_id, projection_name)
            if projection_name == "procedure":
                content_digest = self._procedure_digest(steps)
            elif projection_name == "outcome":
                content_digest = self._outcome_digest(outcomes)
            else:
                content_digest = sha256_json(list(typed_rows))
            if (
                not dry_run
                and not force
                and capability_ready
                and active is not None
                and self._active_is_current(
                    workspace_id,
                    projection_name,
                    active,
                    row_count,
                    event_count,
                    event_root,
                    content_digest,
                    steps,
                    outcomes,
                    typed_rows,
                )
            ):
                storage_target, build_config_hash, details = self._projection_contract(
                    workspace_id,
                    projection_name,
                    active[1],
                    content_digest,
                )
                self._rollback(owns_transaction, savepoint)
                return SpecializedProjectionBuildResult(
                    projection_name=projection_name,
                    generation=active[1],
                    status="active",
                    row_count=row_count,
                    source_event_count=event_count,
                    source_event_root_hash=event_root,
                    content_digest=content_digest,
                    build_config_hash=build_config_hash,
                    builder_contract_hash=str(details["builder_contract_hash"]),
                    staging_manifest_id=active[0],
                    storage_target=storage_target,
                    cursor_recorded_at_us=(cursor[0] if cursor is not None else None),
                    cursor_event_id=cursor[1] if cursor is not None else None,
                    active_manifest_id=active[0],
                    active_generation=active[1],
                    active_status=active[2],
                    active_row_count=active[3],
                    active_content_digest=active[4],
                    row_count_delta=0,
                    content_digest_changed=False,
                    reused=True,
                )
            generation = self._next_generation(workspace_id, projection_name)
            storage_target, build_config_hash, details = self._projection_contract(
                workspace_id,
                projection_name,
                generation,
                content_digest,
            )
            manifest_id = deterministic_id(
                "prj",
                "projection",
                workspace_id,
                projection_name,
                generation,
                event_root,
            )
            result = SpecializedProjectionBuildResult(
                projection_name=projection_name,
                generation=generation,
                status=(
                    "unavailable"
                    if not capability_ready
                    else ("ready" if dry_run else "active")
                ),
                row_count=row_count,
                source_event_count=event_count,
                source_event_root_hash=event_root,
                content_digest=content_digest,
                build_config_hash=build_config_hash,
                builder_contract_hash=str(details["builder_contract_hash"]),
                staging_manifest_id=manifest_id,
                storage_target=storage_target,
                dry_run=dry_run,
                capability_status=("ready" if capability_ready else "unavailable"),
                capability_reason=(
                    None if capability_ready else "PROCEDURE_UNAVAILABLE"
                ),
                cursor_recorded_at_us=cursor[0] if cursor is not None else None,
                cursor_event_id=cursor[1] if cursor is not None else None,
                active_manifest_id=active[0] if active is not None else None,
                active_generation=active[1] if active is not None else None,
                active_status=active[2] if active is not None else None,
                active_row_count=active[3] if active is not None else 0,
                active_content_digest=active[4] if active is not None else None,
                row_count_delta=row_count - (active[3] if active is not None else 0),
                content_digest_changed=(active is None or active[4] != content_digest),
            )
            if dry_run:
                self._rollback(owns_transaction, savepoint)
                return result
            if not capability_ready:
                raise SpecializedProjectionBuildError(
                    "PROCEDURE_UNAVAILABLE",
                    "procedure projection capability is unavailable",
                )

            self._rollback(owns_transaction, savepoint)
            if owns_transaction:
                self.connection.execute("BEGIN IMMEDIATE")
            else:
                self.connection.execute(f"SAVEPOINT {savepoint}")
            if self._event_identity(workspace_id) != (event_count, cursor):
                raise SpecializedProjectionBuildError(
                    "PROJECTION_BUILD_SUPERSEDED",
                    "source event snapshot changed before publication",
                )
            if (
                self._active_manifest(workspace_id, projection_name) != active
                or self._next_generation(workspace_id, projection_name) != generation
            ):
                raise SpecializedProjectionBuildError(
                    "PROJECTION_BUILD_SUPERSEDED",
                    "projection generation changed before publication",
                )
            now = self._clock_value()
            self._insert_manifest(
                manifest_id,
                workspace_id,
                projection_name,
                generation,
                event_count,
                event_root,
                cursor,
                details,
                now,
            )
            if projection_name == "procedure":
                self._populate_procedure(
                    workspace_id, generation, storage_target, steps
                )
                self._validate_procedure_staging(
                    workspace_id, generation, storage_target, steps
                )
            elif projection_name == "outcome":
                self._populate_outcomes(workspace_id, generation, outcomes)
                self._validate_outcome_staging(workspace_id, generation, outcomes)
            else:
                if self._typed_rows(workspace_id, projection_name) != typed_rows:
                    raise SpecializedProjectionBuildError(
                        "PROJECTION_VALIDATION_FAILED",
                        "canonical typed projection changed during build",
                    )
                if candidate_populator is not None:
                    candidate_populator(generation)
            self._validate_manifest(
                manifest_id,
                workspace_id,
                projection_name,
                generation,
                event_count,
                event_root,
                cursor,
                details,
                now,
            )
            if before_activate is not None:
                before_activate()
            self._activate(
                manifest_id,
                workspace_id,
                projection_name,
                row_count,
                now,
            )
            if before_commit is not None:
                before_commit(result)
            if owns_transaction:
                self.connection.commit()
            else:
                self.connection.execute(f"RELEASE SAVEPOINT {savepoint}")
        except Exception as exc:
            self._rollback(owns_transaction, savepoint)
            if isinstance(exc, SpecializedProjectionBuildError):
                raise
            if isinstance(
                exc, sqlite3.OperationalError
            ) and ProjectionJobRunner._is_lock_contention(exc):
                raise SpecializedProjectionBuildError(
                    "DATABASE_IN_USE",
                    "specialized projection build was contended",
                ) from exc
            if isinstance(exc, sqlite3.Error):
                raise SpecializedProjectionBuildError(
                    "PROJECTION_UNAVAILABLE",
                    "specialized projection build is unavailable",
                ) from exc
            raise
        if owns_transaction and projection_name != "graph":
            # Graph generations are referenced by the discovery tables.
            collect_superseded_generations(
                self.connection, workspace_id, projection_name
            )
        return result

    def active_is_current(self, workspace_id: str, projection_name: str) -> bool:
        """Return whether the active specialized generation is exact."""

        self._validate_request(workspace_id, projection_name)
        self._require_schema(projection_name)
        owns_transaction = not self.connection.in_transaction
        try:
            if owns_transaction:
                self.connection.execute("BEGIN")
            steps: tuple[_ProcedureStep, ...] = ()
            outcomes: tuple[_OutcomeRow, ...] = ()
            typed_rows: tuple[dict[str, object], ...] = ()
            if projection_name == "procedure":
                self._probe_fts5()
                steps = self._procedure_steps(workspace_id)
                row_count = len(steps)
                content_digest = self._procedure_digest(steps)
            elif projection_name == "outcome":
                outcomes = self._outcome_rows(workspace_id)
                row_count = len(outcomes)
                content_digest = self._outcome_digest(outcomes)
            else:
                typed_rows = self._typed_rows(workspace_id, projection_name)
                row_count = len(typed_rows)
                content_digest = sha256_json(list(typed_rows))
            event_count, event_root, _ = self._event_snapshot(workspace_id)
            active = self._active_manifest(workspace_id, projection_name)
            return active is not None and self._active_is_current(
                workspace_id,
                projection_name,
                active,
                row_count,
                event_count,
                event_root,
                content_digest,
                steps,
                outcomes,
                typed_rows,
            )
        except Exception:
            return False
        finally:
            if owns_transaction and self.connection.in_transaction:
                self.connection.rollback()

    @staticmethod
    def _validate_request(workspace_id: str, projection_name: str) -> None:
        if (
            not isinstance(workspace_id, str)
            or _WORKSPACE_ID.fullmatch(workspace_id) is None
        ):
            raise SpecializedProjectionBuildError(
                "INVALID_WORKSPACE_ID", "workspace identifier is invalid"
            )
        if projection_name not in _PROJECTIONS:
            raise SpecializedProjectionBuildError(
                "INVALID_PROJECTION", "projection name is invalid"
            )

    def _require_schema(self, projection_name: str) -> None:
        required = {
            "memory_events",
            "memory_records",
            "projection_manifests",
        }
        if projection_name == "procedure":
            required.add("record_procedures")
        elif projection_name == "outcome":
            required.add("record_outcome_view")
        elif projection_name == "temporal":
            required.add("memory_fact_versions")
        elif projection_name == "graph":
            required.update(
                {
                    "memory_fact_versions",
                    "memory_relationship_versions",
                }
            )
        available = {
            str(row[0])
            for row in self.connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        if not required.issubset(available):
            raise SpecializedProjectionBuildError(
                "PROJECTION_UNAVAILABLE",
                "specialized projection schema is unavailable",
            )

    def _probe_fts5(self) -> None:
        row = self.connection.execute("SELECT fts5_source_id()").fetchone()
        if row is None or not isinstance(row[0], str) or not row[0]:
            raise sqlite3.OperationalError("FTS5 is unavailable")

    def _procedure_steps(self, workspace_id: str) -> tuple[_ProcedureStep, ...]:
        steps: list[_ProcedureStep] = []
        rows = self.connection.execute(
            "SELECT record.record_id,record.record_type,record.context_json,"
            "record.source_event_id,event.workspace_id,event.stream_id,"
            "event.payload_json FROM memory_records AS record "
            "JOIN memory_events AS event ON event.event_id=record.source_event_id "
            "WHERE record.workspace_id=? AND record.deleted_at_us IS NULL "
            "ORDER BY record.record_id",
            (workspace_id,),
        )
        try:
            for row in rows:
                context = json.loads(str(row[2]))
                payload = json.loads(str(row[6]))
                event_record = payload.get("record")
                if (
                    not isinstance(context, dict)
                    or not isinstance(event_record, dict)
                    or row[4] != workspace_id
                    or row[5] != row[0]
                    or event_record.get("record_type") != row[1]
                    or event_record.get("context") != context
                ):
                    raise SpecializedProjectionBuildError(
                        "PROJECTION_VALIDATION_FAILED",
                        "procedure source record differs from its event",
                    )
                if row[1] != "procedure" or "steps" not in context:
                    continue
                structured = context["steps"]
                if (
                    not isinstance(structured, list)
                    or len(structured) > 1_000
                    or not all(
                        isinstance(step, str)
                        and bool(step.strip())
                        and len(step) <= 10_000
                        for step in structured
                    )
                ):
                    raise SpecializedProjectionBuildError(
                        "INVALID_PROCEDURE_STEPS",
                        "structured procedure steps are invalid",
                    )
                for ordinal, text in enumerate(structured):
                    steps.append(
                        _ProcedureStep(
                            record_id=str(row[0]),
                            ordinal=ordinal,
                            step_text=text,
                            step_hash=sha256_json(text),
                            source_event_id=str(row[3]),
                        )
                    )
        except (json.JSONDecodeError, TypeError, ValueError, RecursionError) as exc:
            raise SpecializedProjectionBuildError(
                "PROJECTION_VALIDATION_FAILED",
                "procedure source metadata is invalid",
            ) from exc
        return tuple(steps)

    def _outcome_rows(self, workspace_id: str) -> tuple[_OutcomeRow, ...]:
        outcomes: list[_OutcomeRow] = []
        rows = self.connection.execute(
            "SELECT record.record_id,record.outcome,record.worked,"
            "record.source_event_id,record.updated_at_us,event.workspace_id,"
            "event.stream_id,event.payload_json,event.recorded_at_us "
            "FROM memory_records AS record "
            "JOIN memory_events AS event ON event.event_id=record.source_event_id "
            "WHERE record.workspace_id=? AND record.deleted_at_us IS NULL "
            "ORDER BY record.record_id",
            (workspace_id,),
        )
        try:
            for row in rows:
                current_state = (
                    None if row[1] is None else str(row[1]),
                    None if row[2] is None else int(row[2]),
                )
                source_state = self._outcome_state(str(row[7]))
                if (
                    row[5] != workspace_id
                    or row[6] != row[0]
                    or source_state != current_state
                    or int(row[8]) != int(row[4])
                ):
                    raise SpecializedProjectionBuildError(
                        "PROJECTION_VALIDATION_FAILED",
                        "outcome source record differs from its event",
                    )
                if current_state == (None, None):
                    continue
                assertion = self._latest_outcome_assertion(
                    workspace_id,
                    str(row[0]),
                    str(row[3]),
                    current_state,
                )
                outcomes.append(
                    _OutcomeRow(
                        record_id=str(row[0]),
                        worked=current_state[1],
                        outcome_text=current_state[0],
                        outcome_event_id=assertion[0],
                        transaction_at_us=assertion[1],
                    )
                )
        except (json.JSONDecodeError, TypeError, ValueError, RecursionError) as exc:
            raise SpecializedProjectionBuildError(
                "PROJECTION_VALIDATION_FAILED",
                "outcome source metadata is invalid",
            ) from exc
        return tuple(outcomes)

    @staticmethod
    def _outcome_state(payload_json: str) -> tuple[str | None, int | None]:
        payload = json.loads(payload_json)
        if (
            not isinstance(payload, dict)
            or canonical_json_bytes(payload).decode("utf-8") != payload_json
        ):
            raise ValueError("event payload is not canonical")
        record = payload.get("record")
        if not isinstance(record, dict):
            raise ValueError("memory event has no record state")
        outcome = record.get("outcome")
        worked = record.get("worked")
        if outcome is not None and not isinstance(outcome, str):
            raise ValueError("outcome must be text or null")
        if worked is not None and not isinstance(worked, bool):
            raise ValueError("worked must be boolean or null")
        return outcome, None if worked is None else int(worked)

    def _latest_outcome_assertion(
        self,
        workspace_id: str,
        record_id: str,
        source_event_id: str,
        current_state: tuple[str | None, int | None],
    ) -> tuple[str, int]:
        previous_state: tuple[str | None, int | None] = (None, None)
        assertion: tuple[str, int] | None = None
        expected_version = 1
        latest_event_id: str | None = None
        latest_state: tuple[str | None, int | None] = (None, None)
        for row in self.connection.execute(
            "SELECT event_id,event_type,recorded_at_us,payload_json,"
            "stream_version FROM memory_events WHERE workspace_id=? "
            "AND stream_id=? AND stream_kind='memory' ORDER BY stream_version",
            (workspace_id, record_id),
        ):
            if int(row[4]) != expected_version:
                raise SpecializedProjectionBuildError(
                    "PROJECTION_VALIDATION_FAILED",
                    "outcome event stream is not contiguous",
                )
            expected_version += 1
            latest_event_id = str(row[0])
            latest_state = self._outcome_state(str(row[3]))
            if latest_state != (None, None) and (
                str(row[1]) in _OUTCOME_ASSERTION_EVENT_TYPES
                or latest_state != previous_state
            ):
                assertion = (latest_event_id, int(row[2]))
            previous_state = latest_state
        if (
            latest_event_id != source_event_id
            or latest_state != current_state
            or assertion is None
        ):
            raise SpecializedProjectionBuildError(
                "PROJECTION_VALIDATION_FAILED",
                "current outcome has no matching assertion event",
            )
        return assertion

    def _typed_rows(
        self, workspace_id: str, projection_name: str
    ) -> tuple[dict[str, object], ...]:
        if projection_name == "temporal":
            configurations = (
                (
                    "memory_fact_versions",
                    "fact_version_id",
                    "fact_id",
                    "fact",
                    ("object_json", "evidence_json", "metadata_json"),
                    "",
                    (),
                    "temporal",
                ),
            )
        elif projection_name == "graph":
            configurations = (
                (
                    "memory_fact_versions",
                    "fact_version_id",
                    "fact_id",
                    "fact",
                    ("object_json", "evidence_json", "metadata_json"),
                    " AND object_kind='record_ref'",
                    (),
                    "record_ref",
                ),
                (
                    "memory_relationship_versions",
                    "relationship_version_id",
                    "relationship_id",
                    "relationship",
                    ("metadata_json",),
                    "",
                    (),
                    "relationship",
                ),
            )
        else:
            raise SpecializedProjectionBuildError(
                "INVALID_PROJECTION", "typed projection name is invalid"
            )
        rows: list[dict[str, object]] = []
        for (
            table,
            identity,
            stream_identity,
            stream_kind,
            json_columns,
            filter_sql,
            filter_parameters,
            source_kind,
        ) in configurations:
            cursor = self.connection.execute(
                f'SELECT * FROM "{table}" WHERE workspace_id=?'
                f'{filter_sql} ORDER BY "{identity}"',
                (workspace_id, *filter_parameters),
            )
            columns = tuple(str(item[0]) for item in cursor.description)
            for raw in cursor.fetchall():
                row = dict(zip(columns, tuple(raw), strict=True))
                try:
                    parsed_json: dict[str, object] = {}
                    for column in json_columns:
                        parsed = json.loads(str(row[column]))
                        if canonical_json_bytes(parsed).decode("utf-8") != row[column]:
                            raise ValueError
                        parsed_json[column] = parsed
                except (
                    KeyError,
                    TypeError,
                    ValueError,
                    json.JSONDecodeError,
                ) as exc:
                    raise SpecializedProjectionBuildError(
                        "PROJECTION_VALIDATION_FAILED",
                        "canonical typed projection JSON is invalid",
                    ) from exc
                self._validate_event_reference(
                    workspace_id,
                    str(row[stream_identity]),
                    stream_kind,
                    str(row["asserted_by_event_id"]),
                )
                retracted = row.get("retracted_by_event_id")
                if retracted is not None:
                    self._validate_event_reference(
                        workspace_id,
                        str(row[stream_identity]),
                        stream_kind,
                        str(retracted),
                    )
                if source_kind == "record_ref":
                    self._validate_record_ref_row(
                        workspace_id,
                        row,
                        parsed_json.get("object_json"),
                    )
                row["projection_source"] = source_kind
                rows.append(row)
        return tuple(rows)

    def _validate_record_ref_row(
        self,
        workspace_id: str,
        row: Mapping[str, object],
        target_record_id: object,
    ) -> None:
        source_record_id = row.get("subject_record_id")
        if (
            not isinstance(source_record_id, str)
            or not isinstance(target_record_id, str)
            or source_record_id == target_record_id
        ):
            raise SpecializedProjectionBuildError(
                "PROJECTION_VALIDATION_FAILED",
                "record_ref fact endpoints are invalid",
            )
        endpoints = self.connection.execute(
            "SELECT record_id FROM memory_records WHERE workspace_id=? "
            "AND record_id IN (?,?) AND deleted_at_us IS NULL",
            (workspace_id, source_record_id, target_record_id),
        ).fetchall()
        if {str(endpoint[0]) for endpoint in endpoints} != {
            source_record_id,
            target_record_id,
        }:
            raise SpecializedProjectionBuildError(
                "PROJECTION_VALIDATION_FAILED",
                "record_ref fact endpoints are unavailable",
            )

    def _validate_event_reference(
        self,
        workspace_id: str,
        stream_id: str,
        stream_kind: str,
        event_id: str,
    ) -> None:
        row = self.connection.execute(
            "SELECT workspace_id,stream_id,stream_kind FROM memory_events "
            "WHERE event_id=?",
            (event_id,),
        ).fetchone()
        if row is None or tuple(row) != (
            workspace_id,
            stream_id,
            stream_kind,
        ):
            raise SpecializedProjectionBuildError(
                "PROJECTION_VALIDATION_FAILED",
                "typed projection event provenance differs",
            )

    @staticmethod
    def _procedure_digest(steps: Sequence[_ProcedureStep]) -> str:
        return sha256_json(
            [
                {
                    "ordinal": step.ordinal,
                    "record_id": step.record_id,
                    "source_event_id": step.source_event_id,
                    "step_hash": step.step_hash,
                    "step_text": step.step_text,
                }
                for step in steps
            ]
        )

    @staticmethod
    def _outcome_digest(outcomes: Sequence[_OutcomeRow]) -> str:
        return sha256_json(
            [
                {
                    "outcome_event_id": outcome.outcome_event_id,
                    "outcome_text": outcome.outcome_text,
                    "record_id": outcome.record_id,
                    "transaction_at_us": outcome.transaction_at_us,
                    "worked": outcome.worked,
                }
                for outcome in outcomes
            ]
        )

    def _populate_procedure(
        self,
        workspace_id: str,
        generation: int,
        fts_table: str,
        steps: Sequence[_ProcedureStep],
    ) -> None:
        self.connection.executemany(
            "INSERT INTO record_procedures (workspace_id,"
            "projection_generation,record_id,ordinal,step_text,step_hash,"
            "source_event_id) VALUES (?,?,?,?,?,?,?)",
            [
                (
                    workspace_id,
                    generation,
                    step.record_id,
                    step.ordinal,
                    step.step_text,
                    step.step_hash,
                    step.source_event_id,
                )
                for step in steps
            ],
        )
        self.connection.execute(
            f'CREATE VIRTUAL TABLE "{fts_table}" USING fts5('
            "record_id UNINDEXED,ordinal UNINDEXED,step_hash UNINDEXED,"
            "source_event_id UNINDEXED,step_text,"
            "tokenize='unicode61 remove_diacritics 2')"
        )
        self.connection.executemany(
            f'INSERT INTO "{fts_table}" '
            "(record_id,ordinal,step_hash,source_event_id,step_text) "
            "VALUES (?,?,?,?,?)",
            [
                (
                    step.record_id,
                    step.ordinal,
                    step.step_hash,
                    step.source_event_id,
                    step.step_text,
                )
                for step in steps
            ],
        )

    def _populate_outcomes(
        self,
        workspace_id: str,
        generation: int,
        outcomes: Sequence[_OutcomeRow],
    ) -> None:
        self.connection.executemany(
            "INSERT INTO record_outcome_view (workspace_id,"
            "projection_generation,record_id,worked,outcome_text,"
            "outcome_event_id,transaction_at_us) VALUES (?,?,?,?,?,?,?)",
            [
                (
                    workspace_id,
                    generation,
                    outcome.record_id,
                    outcome.worked,
                    outcome.outcome_text,
                    outcome.outcome_event_id,
                    outcome.transaction_at_us,
                )
                for outcome in outcomes
            ],
        )

    def _validate_outcome_staging(
        self,
        workspace_id: str,
        generation: int,
        expected: Sequence[_OutcomeRow],
    ) -> None:
        rows = self.connection.execute(
            "SELECT record_id,worked,outcome_text,outcome_event_id,"
            "transaction_at_us FROM record_outcome_view WHERE workspace_id=? "
            "AND projection_generation=? ORDER BY record_id",
            (workspace_id, generation),
        ).fetchall()
        expected_rows = [
            (
                outcome.record_id,
                outcome.worked,
                outcome.outcome_text,
                outcome.outcome_event_id,
                outcome.transaction_at_us,
            )
            for outcome in expected
        ]
        if [tuple(row) for row in rows] != expected_rows:
            raise SpecializedProjectionBuildError(
                "PROJECTION_VALIDATION_FAILED",
                "outcome staging rows differ",
            )

    def _validate_source_snapshot(
        self,
        workspace_id: str,
        projection_name: str,
        event_count: int,
        event_root: str,
        cursor: tuple[int, str] | None,
        steps: Sequence[_ProcedureStep],
        outcomes: Sequence[_OutcomeRow],
        typed_rows: tuple[dict[str, object], ...],
    ) -> None:
        if self._event_snapshot(workspace_id) != (
            event_count,
            event_root,
            cursor,
        ):
            raise SpecializedProjectionBuildError(
                "PROJECTION_VALIDATION_FAILED",
                "source event snapshot changed during build",
            )
        if projection_name == "procedure":
            current: object = self._procedure_steps(workspace_id)
            expected: object = tuple(steps)
        elif projection_name == "outcome":
            current = self._outcome_rows(workspace_id)
            expected = tuple(outcomes)
        else:
            current = self._typed_rows(workspace_id, projection_name)
            expected = typed_rows
        if current != expected:
            raise SpecializedProjectionBuildError(
                "PROJECTION_VALIDATION_FAILED",
                "specialized source rows changed during build",
            )

    def _validate_manifest(
        self,
        manifest_id: str,
        workspace_id: str,
        projection_name: str,
        generation: int,
        event_count: int,
        event_root: str,
        cursor: tuple[int, str] | None,
        details: Mapping[str, object],
        now: int,
    ) -> None:
        row = self.connection.execute(
            "SELECT workspace_id,projection_name,generation,projection_version,"
            "status,source_event_count,source_event_root_hash,"
            "cursor_recorded_at_us,cursor_event_id,row_count,builder_version,"
            "details_json,started_at_us,completed_at_us,activated_at_us "
            "FROM projection_manifests WHERE manifest_id=?",
            (manifest_id,),
        ).fetchone()
        expected = (
            workspace_id,
            projection_name,
            generation,
            1,
            "building",
            event_count,
            event_root,
            cursor[0] if cursor is not None else None,
            cursor[1] if cursor is not None else None,
            0,
            _BUILDER_VERSION,
            canonical_json_bytes(details).decode("utf-8"),
            now,
            None,
            None,
        )
        if row is None or tuple(row) != expected:
            raise SpecializedProjectionBuildError(
                "PROJECTION_VALIDATION_FAILED",
                "specialized staging manifest differs",
            )

    def _event_snapshot(
        self, workspace_id: str
    ) -> tuple[int, str, tuple[int, str] | None]:
        digest = hashlib.sha256()
        count = 0
        for row in self.connection.execute(
            "SELECT event_hash FROM memory_events WHERE workspace_id=? "
            "ORDER BY event_id",
            (workspace_id,),
        ):
            try:
                digest.update(bytes.fromhex(str(row[0])))
            except ValueError as exc:
                raise SpecializedProjectionBuildError(
                    "PROJECTION_VALIDATION_FAILED",
                    "source event hash is invalid",
                ) from exc
            count += 1
        row = self.connection.execute(
            "SELECT recorded_at_us,event_id FROM memory_events "
            "WHERE workspace_id=? ORDER BY recorded_at_us DESC,event_id DESC "
            "LIMIT 1",
            (workspace_id,),
        ).fetchone()
        cursor = None if row is None else (int(row[0]), str(row[1]))
        return count, digest.hexdigest(), cursor

    def _event_identity(self, workspace_id: str) -> tuple[int, tuple[int, str] | None]:
        """Return the append-only ledger identity without hashing its body."""

        count = int(
            self.connection.execute(
                "SELECT count(*) FROM memory_events WHERE workspace_id=?",
                (workspace_id,),
            ).fetchone()[0]
        )
        row = self.connection.execute(
            "SELECT recorded_at_us,event_id FROM memory_events "
            "WHERE workspace_id=? ORDER BY recorded_at_us DESC,event_id DESC "
            "LIMIT 1",
            (workspace_id,),
        ).fetchone()
        cursor = None if row is None else (int(row[0]), str(row[1]))
        return count, cursor

    def _next_generation(self, workspace_id: str, projection_name: str) -> int:
        return (
            int(
                self.connection.execute(
                    "SELECT COALESCE(MAX(generation),0) FROM projection_manifests "
                    "WHERE workspace_id=? AND projection_name=?",
                    (workspace_id, projection_name),
                ).fetchone()[0]
            )
            + 1
        )

    def _active_manifest(
        self, workspace_id: str, projection_name: str
    ) -> tuple[str, int, str, int, str | None] | None:
        row = self.connection.execute(
            "SELECT manifest_id,generation,status,row_count,details_json "
            "FROM projection_manifests WHERE workspace_id=? "
            "AND projection_name=? AND status='active'",
            (workspace_id, projection_name),
        ).fetchone()
        if row is None:
            return None
        digest = None
        try:
            details = json.loads(str(row[4]))
            candidate = details.get("content_digest")
            if (
                isinstance(candidate, str)
                and len(candidate) == 64
                and not set(candidate).difference("0123456789abcdef")
            ):
                digest = candidate
        except (AttributeError, TypeError, ValueError, json.JSONDecodeError):
            pass
        return str(row[0]), int(row[1]), str(row[2]), int(row[3]), digest

    @staticmethod
    def _projection_contract(
        workspace_id: str,
        projection_name: str,
        generation: int,
        content_digest: str,
    ) -> tuple[str, str, dict[str, object]]:
        return specialized_projection_contract(
            workspace_id,
            projection_name,
            generation,
            content_digest,
            builder_version=_BUILDER_VERSION,
        )

    def _active_is_current(
        self,
        workspace_id: str,
        projection_name: str,
        active: tuple[str, int, str, int, str | None],
        row_count: int,
        event_count: int,
        event_root: str,
        content_digest: str,
        steps: Sequence[_ProcedureStep],
        outcomes: Sequence[_OutcomeRow],
        typed_rows: tuple[dict[str, object], ...],
    ) -> bool:
        storage_target, _build_config_hash, expected_details = (
            self._projection_contract(
                workspace_id,
                projection_name,
                active[1],
                content_digest,
            )
        )
        row = self.connection.execute(
            "SELECT source_event_count,source_event_root_hash,row_count,"
            "details_json FROM projection_manifests WHERE manifest_id=? "
            "AND status='active'",
            (active[0],),
        ).fetchone()
        if row is None:
            return False
        try:
            details = json.loads(str(row[3]))
        except (TypeError, ValueError, json.JSONDecodeError, RecursionError):
            return False
        if (
            int(row[0]) != event_count
            or str(row[1]) != event_root
            or int(row[2]) != row_count
            or details != expected_details
        ):
            return False
        try:
            if projection_name == "procedure":
                self._validate_procedure_staging(
                    workspace_id, active[1], storage_target, steps
                )
            elif projection_name == "outcome":
                self._validate_outcome_staging(workspace_id, active[1], outcomes)
            elif self._typed_rows(workspace_id, projection_name) != typed_rows:
                return False
        except Exception:
            return False
        return True

    def _insert_manifest(
        self,
        manifest_id: str,
        workspace_id: str,
        projection_name: str,
        generation: int,
        event_count: int,
        event_root: str,
        cursor: tuple[int, str] | None,
        details: Mapping[str, object],
        now: int,
    ) -> None:
        self.connection.execute(
            "INSERT INTO projection_manifests (manifest_id,workspace_id,"
            "projection_name,generation,projection_version,status,"
            "source_event_count,source_event_root_hash,cursor_recorded_at_us,"
            "cursor_event_id,row_count,builder_version,details_json,"
            "started_at_us,completed_at_us,activated_at_us) "
            "VALUES (?,?,?,?,1,'building',?,?,?,?,0,?,?,?,NULL,NULL)",
            (
                manifest_id,
                workspace_id,
                projection_name,
                generation,
                event_count,
                event_root,
                cursor[0] if cursor is not None else None,
                cursor[1] if cursor is not None else None,
                _BUILDER_VERSION,
                canonical_json_bytes(details).decode("utf-8"),
                now,
            ),
        )

    def _validate_procedure_staging(
        self,
        workspace_id: str,
        generation: int,
        fts_table: str,
        expected: Sequence[_ProcedureStep],
    ) -> None:
        definition = self.connection.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name=?",
            (fts_table,),
        ).fetchone()
        normalized_sql = (
            ""
            if definition is None or not isinstance(definition[0], str)
            else " ".join(definition[0].casefold().split())
        )
        columns = tuple(
            str(row[1])
            for row in self.connection.execute(f'PRAGMA table_info("{fts_table}")')
        )
        if (
            "using fts5" not in normalized_sql
            or "tokenize='unicode61 remove_diacritics 2'" not in normalized_sql
            or columns
            != (
                "record_id",
                "ordinal",
                "step_hash",
                "source_event_id",
                "step_text",
            )
        ):
            raise SpecializedProjectionBuildError(
                "PROJECTION_VALIDATION_FAILED",
                "procedure FTS configuration differs",
            )
        rows = self.connection.execute(
            "SELECT record_id,ordinal,step_text,step_hash,source_event_id "
            "FROM record_procedures WHERE workspace_id=? "
            "AND projection_generation=? ORDER BY record_id,ordinal",
            (workspace_id, generation),
        ).fetchall()
        expected_rows = [
            (
                step.record_id,
                step.ordinal,
                step.step_text,
                step.step_hash,
                step.source_event_id,
            )
            for step in expected
        ]
        indexed = self.connection.execute(
            f"SELECT record_id,CAST(ordinal AS INTEGER),step_text,step_hash,"
            f'source_event_id FROM "{fts_table}" ORDER BY record_id,ordinal'
        ).fetchall()
        if [tuple(row) for row in rows] != expected_rows or [
            tuple(row) for row in indexed
        ] != expected_rows:
            raise SpecializedProjectionBuildError(
                "PROJECTION_VALIDATION_FAILED",
                "procedure staging rows differ",
            )

    def _activate(
        self,
        manifest_id: str,
        workspace_id: str,
        projection_name: str,
        row_count: int,
        now: int,
    ) -> None:
        self.connection.execute(
            "UPDATE projection_manifests SET status='ready' "
            "WHERE workspace_id=? AND projection_name=? AND status='active'",
            (workspace_id, projection_name),
        )
        changed = self.connection.execute(
            "UPDATE projection_manifests SET status='active',row_count=?,"
            "completed_at_us=?,activated_at_us=? "
            "WHERE manifest_id=? AND status='building'",
            (row_count, now, now, manifest_id),
        ).rowcount
        if changed != 1:
            raise SpecializedProjectionBuildError(
                "PROJECTION_ACTIVATION_FAILED",
                "specialized staging manifest is unavailable",
            )

    def _clock_value(self) -> int:
        value = self._clock_us()
        if (
            isinstance(value, bool)
            or not isinstance(value, int)
            or value < -(2**63)
            or value > 2**63 - 1
        ):
            raise SpecializedProjectionBuildError(
                "INVALID_CLOCK",
                "projection clock must return signed 64-bit microseconds",
            )
        return value

    def _rollback(self, owns_transaction: bool, savepoint: str) -> None:
        if owns_transaction:
            if self.connection.in_transaction:
                self.connection.rollback()
        else:
            self.connection.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
            self.connection.execute(f"RELEASE SAVEPOINT {savepoint}")


__all__ = [
    "SpecializedProjectionBuildError",
    "SpecializedProjectionBuildResult",
    "SpecializedProjectionBuilder",
]
