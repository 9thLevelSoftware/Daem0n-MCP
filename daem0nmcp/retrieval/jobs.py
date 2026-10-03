"""Durable claim/lease lifecycle for retrieval projection rebuild jobs."""

from __future__ import annotations

import hashlib
import json
import re
import secrets
import sqlite3
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from ..event_store import canonical_json_bytes

_JOB_TYPE = "retrieval.projection_rebuild"
_PROJECTIONS = frozenset(
    {"lexical", "dense", "graph", "temporal", "procedure", "outcome"}
)
_WORKER = re.compile(r"^[A-Za-z0-9._:-]{1,64}$")
_LEASE_TOKEN = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
_ERROR_CODE = re.compile(r"^[A-Z][A-Z0-9_]{1,63}$")
_HEARTBEAT_BUSY_TIMEOUT_MS = 100
_HEARTBEAT_SHUTDOWN_GRACE_SECONDS = 1.5


def is_lock_contention(exc: BaseException) -> bool:
    """Return whether *exc* is SQLite lock contention a caller may retry."""

    if not isinstance(exc, sqlite3.OperationalError):
        return False
    code = getattr(exc, "sqlite_errorcode", None)
    if isinstance(code, int):
        return code & 0xFF in {
            sqlite3.SQLITE_BUSY,
            sqlite3.SQLITE_LOCKED,
        }
    # sqlite_errorcode is unavailable on supported Python 3.10 builds.
    # Match only SQLite's fixed lock diagnostics, never arbitrary detail.
    return str(exc).casefold() in {
        "database is locked",
        "database table is locked",
        "database schema is locked",
    }


class ProjectionJobError(RuntimeError):
    """Owned, sanitized projection-job failure."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class ProjectionJobRun:
    """Candidate-free outcome from one job-runner iteration."""

    job_id: str
    workspace_id: str
    projections: tuple[str, ...]
    status: str
    reason: str | None = None


@dataclass(frozen=True, slots=True)
class _ClaimedJob:
    job_id: str
    workspace_id: str
    payload_json: str
    payload_hash: str
    source_event_id: str | None
    attempts: int
    max_attempts: int
    lease_token: str


class ProjectionJobRunner:
    """Claim and execute coalesced rebuild jobs with expiring leases."""

    def __init__(
        self,
        connection: sqlite3.Connection,
        *,
        builders: Mapping[str, Callable[[str], Any]],
        clock_us: Callable[[], int] | None = None,
        lease_owner: str = "daem0nmcp-projection-worker",
        token_factory: Callable[[], str] | None = None,
        lease_duration_us: int = 30_000_000,
        heartbeat_interval_us: int | None = None,
        heartbeat_connection_factory: (Callable[[], sqlite3.Connection] | None) = None,
        retry_delay_us: int = 1_000_000,
        cancelled: Callable[[], bool] | None = None,
    ) -> None:
        if not isinstance(connection, sqlite3.Connection):
            raise ValueError("connection must be a SQLite connection")
        if not isinstance(builders, Mapping) or not builders:
            raise ValueError("builders must be a non-empty mapping")
        normalized_builders: dict[str, Callable[[str], Any]] = {}
        for name, builder in builders.items():
            if name not in _PROJECTIONS or not callable(builder):
                raise ValueError("builders contain an unsupported projection")
            normalized_builders[name] = builder
        if not isinstance(lease_owner, str) or _WORKER.fullmatch(lease_owner) is None:
            raise ValueError("lease_owner is invalid")
        for value, field_name in (
            (lease_duration_us, "lease_duration_us"),
            (retry_delay_us, "retry_delay_us"),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError(f"{field_name} must be a positive integer")
        if heartbeat_interval_us is None:
            heartbeat_interval_us = max(1, lease_duration_us // 3)
        if (
            isinstance(heartbeat_interval_us, bool)
            or not isinstance(heartbeat_interval_us, int)
            or heartbeat_interval_us < 1
            or heartbeat_interval_us >= lease_duration_us
        ):
            raise ValueError("heartbeat_interval_us must be shorter than the lease")
        if heartbeat_connection_factory is not None and not callable(
            heartbeat_connection_factory
        ):
            raise ValueError("heartbeat_connection_factory must be callable")
        database_path = None
        if heartbeat_connection_factory is None:
            for row in connection.execute("PRAGMA database_list"):
                if str(row[1]) == "main" and str(row[2]):
                    database_path = str(row[2])
                    break
        self.connection = connection
        self._builders = normalized_builders
        self._clock_us = clock_us or (lambda: time.time_ns() // 1_000)
        self._lease_owner = lease_owner
        self._token_factory = token_factory or (lambda: secrets.token_hex(16))
        self._lease_duration_us = lease_duration_us
        self._heartbeat_interval_us = heartbeat_interval_us
        self._heartbeat_connection_factory = heartbeat_connection_factory
        self._heartbeat_database_path = database_path
        self._retry_delay_us = retry_delay_us
        if cancelled is not None and not callable(cancelled):
            raise ValueError("cancelled must be callable")
        self._cancelled = cancelled or (lambda: False)

    def run_once(self) -> ProjectionJobRun | None:
        """Claim at most one job and drive it to a durable next state."""

        claim = self._claim()
        if claim is None:
            return None
        projections: tuple[str, ...] = ()
        stop, heartbeat_failed, heartbeat = self._start_heartbeat(claim)
        failed = False
        failure_code = "PROJECTION_REBUILD_FAILED"
        try:
            projections = self._validate_payload(claim)
            for projection in projections:
                if self._cancelled():
                    raise ProjectionJobError("PROJECTION_CANCELLED")
                builder = self._builders.get(projection)
                if builder is None:
                    raise ProjectionJobError("PROJECTION_BUILDER_UNAVAILABLE")
                builder(claim.workspace_id)
            if heartbeat_failed.is_set():
                raise ProjectionJobError("PROJECTION_JOB_HEARTBEAT_FAILED")
        except Exception as exc:
            failed = True
            candidate = getattr(exc, "code", None)
            if isinstance(candidate, str) and _ERROR_CODE.fullmatch(candidate):
                failure_code = candidate
        finally:
            stop.set()
            heartbeat.join(timeout=2.0)
            if heartbeat.is_alive() or heartbeat_failed.is_set():
                failed = True
        if failed:
            if failure_code == "PROJECTION_CANCELLED":
                return self._record_cancelled(claim, projections)
            return self._record_failure(claim, projections, failure_code)
        return self._record_success(claim, projections)

    def _start_heartbeat(
        self, claim: _ClaimedJob
    ) -> tuple[threading.Event, threading.Event, threading.Thread]:
        stop = threading.Event()
        failed = threading.Event()
        ready = threading.Event()
        thread = threading.Thread(
            target=self._heartbeat_loop,
            args=(claim, stop, failed, ready),
            name="daem0nmcp-projection-lease",
            daemon=True,
        )
        thread.start()
        if not ready.wait(timeout=2.0):
            failed.set()
        return stop, failed, thread

    def _heartbeat_loop(
        self,
        claim: _ClaimedJob,
        stop: threading.Event,
        failed: threading.Event,
        ready: threading.Event,
    ) -> None:
        connection: sqlite3.Connection | None = None
        try:
            factory = self._heartbeat_connection_factory
            if factory is not None:
                connection = factory()
            elif self._heartbeat_database_path is not None:
                connection = sqlite3.connect(
                    self._heartbeat_database_path,
                    timeout=1.0,
                )
            if not isinstance(connection, sqlite3.Connection):
                raise TypeError("heartbeat connection is unavailable")
            connection.execute(f"PRAGMA busy_timeout={_HEARTBEAT_BUSY_TIMEOUT_MS}")
            if connection.in_transaction:
                raise RuntimeError("heartbeat connection has an open transaction")
            ready.set()
            interval_seconds = self._heartbeat_interval_us / 1_000_000
            while True:
                # A builder may hold SQLite's writer lock until after the
                # original lease deadline.  Even when completion requests a
                # stop before the next interval, acquire the lock once and
                # publish a fresh lease before the terminal state update.
                stop.wait(interval_seconds)
                stop_seen_at: float | None = None
                while True:
                    try:
                        # Builders in older projection implementations can
                        # legitimately hold SQLite's writer lock longer than
                        # one lease.  Keep the already-started heartbeat
                        # pending through that contention instead of treating
                        # a busy timeout as ownership loss.
                        connection.execute("BEGIN EXCLUSIVE")
                        changed = connection.execute(
                            "UPDATE background_jobs SET "
                            "lease_expires_at_us=lease_expires_at_us "
                            "WHERE job_id=? AND status='running' "
                            "AND lease_owner=? AND lease_token=?",
                            (
                                claim.job_id,
                                self._lease_owner,
                                claim.lease_token,
                            ),
                        ).rowcount
                        if changed != 1:
                            connection.rollback()
                            failed.set()
                            return
                        # The guarded write above forces lock acquisition.
                        # Sample only now, never before a potentially long
                        # wait behind the projection transaction.
                        now = self._now()
                        connection.execute(
                            "UPDATE background_jobs SET lease_expires_at_us="
                            "MAX(lease_expires_at_us,?) WHERE job_id=? "
                            "AND lease_token=?",
                            (
                                now + self._lease_duration_us,
                                claim.job_id,
                                claim.lease_token,
                            ),
                        )
                        connection.commit()
                        break
                    except sqlite3.OperationalError as exc:
                        connection.rollback()
                        if not is_lock_contention(exc):
                            raise
                        if stop.is_set():
                            if stop_seen_at is None:
                                stop_seen_at = time.monotonic()
                            elif (
                                time.monotonic() - stop_seen_at
                                >= _HEARTBEAT_SHUTDOWN_GRACE_SECONDS
                            ):
                                failed.set()
                                return
                        continue
                    except Exception:
                        connection.rollback()
                        raise
                if stop.is_set():
                    return
        except Exception:
            failed.set()
            ready.set()
        finally:
            if isinstance(connection, sqlite3.Connection):
                if connection.in_transaction:
                    connection.rollback()
                connection.close()

    def _claim(self) -> _ClaimedJob | None:
        if self.connection.in_transaction:
            raise ProjectionJobError("PROJECTION_JOB_TRANSACTION_OPEN")
        now = self._now()
        lease_token = self._token_factory()
        if (
            not isinstance(lease_token, str)
            or _LEASE_TOKEN.fullmatch(lease_token) is None
        ):
            raise ProjectionJobError("PROJECTION_JOB_LEASE_INVALID")
        try:
            self.connection.execute("BEGIN IMMEDIATE")
            row = self.connection.execute(
                """
                SELECT job_id,workspace_id,payload_json,payload_hash,
                       source_event_id,attempts,max_attempts
                FROM background_jobs
                WHERE job_type=? AND (
                    (status='queued' AND available_at_us<=?) OR
                    (status='running' AND lease_expires_at_us<=?)
                )
                ORDER BY priority DESC,created_at_us ASC,job_id ASC
                LIMIT 1
                """,
                (_JOB_TYPE, now, now),
            ).fetchone()
            if row is None:
                self.connection.commit()
                return None
            attempts = int(row[5]) + 1
            changed = self.connection.execute(
                """
                UPDATE background_jobs
                SET status='running',attempts=?,lease_owner=?,lease_token=?,
                    lease_expires_at_us=?,updated_at_us=?,
                    started_at_us=COALESCE(started_at_us,?),finished_at_us=NULL
                WHERE job_id=? AND (
                    (status='queued' AND available_at_us<=?) OR
                    (status='running' AND lease_expires_at_us<=?)
                )
                """,
                (
                    attempts,
                    self._lease_owner,
                    lease_token,
                    now + self._lease_duration_us,
                    now,
                    now,
                    str(row[0]),
                    now,
                    now,
                ),
            ).rowcount
            if changed != 1:
                self.connection.rollback()
                return None
            self.connection.commit()
        except Exception:
            self.connection.rollback()
            raise
        return _ClaimedJob(
            job_id=str(row[0]),
            workspace_id=str(row[1]),
            payload_json=str(row[2]),
            payload_hash=str(row[3]),
            source_event_id=str(row[4]) if row[4] is not None else None,
            attempts=attempts,
            max_attempts=int(row[6]),
            lease_token=lease_token,
        )

    @staticmethod
    def _validate_payload(claim: _ClaimedJob) -> tuple[str, ...]:
        try:
            encoded = claim.payload_json.encode("utf-8")
            payload = json.loads(claim.payload_json)
        except (UnicodeError, ValueError, RecursionError) as exc:
            raise ProjectionJobError("PROJECTION_JOB_PAYLOAD_INVALID") from exc
        if hashlib.sha256(encoded).hexdigest() != claim.payload_hash:
            raise ProjectionJobError("PROJECTION_JOB_PAYLOAD_INVALID")
        if not isinstance(payload, dict) or set(payload) != {
            "projection_names",
            "source_event_id",
            "workspace_id",
        }:
            raise ProjectionJobError("PROJECTION_JOB_PAYLOAD_INVALID")
        names = payload.get("projection_names")
        if (
            not isinstance(names, list)
            or not names
            or names != sorted(set(names))
            or any(name not in _PROJECTIONS for name in names)
            or payload.get("workspace_id") != claim.workspace_id
            or payload.get("source_event_id") != claim.source_event_id
        ):
            raise ProjectionJobError("PROJECTION_JOB_PAYLOAD_INVALID")
        return tuple(names)

    def _record_success(
        self,
        claim: _ClaimedJob,
        projections: tuple[str, ...],
    ) -> ProjectionJobRun:
        now = self._now()
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            current = self.connection.execute(
                "SELECT source_event_id,lease_token,lease_expires_at_us "
                "FROM background_jobs "
                "WHERE job_id=? AND status='running'",
                (claim.job_id,),
            ).fetchone()
            if (
                current is None
                or str(current[1]) != claim.lease_token
                or current[2] is None
                or int(current[2]) <= now
            ):
                raise ProjectionJobError("PROJECTION_JOB_LEASE_LOST")
            if current[0] != claim.source_event_id:
                self.connection.execute(
                    """
                    UPDATE background_jobs
                    SET status='queued',available_at_us=?,lease_owner=NULL,
                        lease_token=NULL,lease_expires_at_us=NULL,
                        last_error_json=NULL,result_json=NULL,updated_at_us=?
                    WHERE job_id=? AND lease_token=?
                    """,
                    (now, now, claim.job_id, claim.lease_token),
                )
                status = "queued"
            else:
                result_json = canonical_json_bytes(
                    {
                        "projection_names": list(projections),
                        "status": "succeeded",
                    }
                ).decode("utf-8")
                self.connection.execute(
                    """
                    UPDATE background_jobs
                    SET status='succeeded',lease_owner=NULL,lease_token=NULL,
                        lease_expires_at_us=NULL,last_error_json=NULL,
                        result_json=?,updated_at_us=?,finished_at_us=?
                    WHERE job_id=? AND lease_token=?
                    """,
                    (result_json, now, now, claim.job_id, claim.lease_token),
                )
                status = "succeeded"
            self.connection.commit()
        except Exception:
            self.connection.rollback()
            raise
        return ProjectionJobRun(
            claim.job_id,
            claim.workspace_id,
            projections,
            status,
        )

    def _record_failure(
        self,
        claim: _ClaimedJob,
        projections: tuple[str, ...],
        failure_code: str,
    ) -> ProjectionJobRun:
        now = self._now()
        last_error = canonical_json_bytes({"code": failure_code}).decode("utf-8")
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            current = self.connection.execute(
                "SELECT source_event_id,lease_token,lease_expires_at_us "
                "FROM background_jobs "
                "WHERE job_id=? AND status='running'",
                (claim.job_id,),
            ).fetchone()
            if (
                current is None
                or str(current[1]) != claim.lease_token
                or current[2] is None
                or int(current[2]) <= now
            ):
                raise ProjectionJobError("PROJECTION_JOB_LEASE_LOST")
            superseded = current[0] != claim.source_event_id
            terminal = failure_code.endswith("_UNAVAILABLE") or failure_code in {
                "PROJECTION_JOB_PAYLOAD_INVALID",
            }
            dead = (terminal or claim.attempts >= claim.max_attempts) and not superseded
            status = "dead_letter" if dead else "queued"
            available = now if superseded else now + self._retry_delay_us
            self.connection.execute(
                """
                UPDATE background_jobs
                SET status=?,available_at_us=?,lease_owner=NULL,
                    lease_token=NULL,lease_expires_at_us=NULL,
                    last_error_json=?,result_json=NULL,updated_at_us=?,
                    finished_at_us=CASE WHEN ?='dead_letter' THEN ? ELSE NULL END
                WHERE job_id=? AND lease_token=?
                """,
                (
                    status,
                    available,
                    last_error,
                    now,
                    status,
                    now,
                    claim.job_id,
                    claim.lease_token,
                ),
            )
            self.connection.commit()
        except Exception:
            self.connection.rollback()
            raise
        return ProjectionJobRun(
            claim.job_id,
            claim.workspace_id,
            projections,
            status,
            failure_code,
        )

    def _record_cancelled(
        self,
        claim: _ClaimedJob,
        projections: tuple[str, ...],
    ) -> ProjectionJobRun:
        """Release shutdown-cancelled work without consuming its retry budget."""

        now = self._now()
        last_error = canonical_json_bytes({"code": "PROJECTION_CANCELLED"}).decode(
            "utf-8"
        )
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            current = self.connection.execute(
                "SELECT lease_token,lease_expires_at_us FROM background_jobs "
                "WHERE job_id=? AND status='running'",
                (claim.job_id,),
            ).fetchone()
            if (
                current is None
                or str(current[0]) != claim.lease_token
                or current[1] is None
                or int(current[1]) <= now
            ):
                raise ProjectionJobError("PROJECTION_JOB_LEASE_LOST")
            changed = self.connection.execute(
                """
                UPDATE background_jobs
                SET status='queued',available_at_us=?,
                    attempts=CASE WHEN attempts>0 THEN attempts-1 ELSE 0 END,
                    lease_owner=NULL,lease_token=NULL,lease_expires_at_us=NULL,
                    last_error_json=?,result_json=NULL,updated_at_us=?,finished_at_us=NULL
                WHERE job_id=? AND lease_token=?
                """,
                (now, last_error, now, claim.job_id, claim.lease_token),
            ).rowcount
            if changed != 1:
                raise ProjectionJobError("PROJECTION_JOB_LEASE_LOST")
            self.connection.commit()
        except Exception:
            self.connection.rollback()
            raise
        return ProjectionJobRun(
            claim.job_id,
            claim.workspace_id,
            projections,
            "queued",
            "PROJECTION_CANCELLED",
        )

    def _now(self) -> int:
        value = self._clock_us()
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ProjectionJobError("PROJECTION_JOB_CLOCK_INVALID")
        return value


def create_projection_job_runner(
    connection: sqlite3.Connection,
    **options: Any,
) -> ProjectionJobRunner:
    """Create the base-profile runner with the mandatory lexical builder."""

    from .projections import LexicalProjectionBuilder

    clock_us = options.get("clock_us")
    builder = LexicalProjectionBuilder(connection, clock_us=clock_us)
    return ProjectionJobRunner(
        connection,
        builders={"lexical": builder.rebuild},
        **options,
    )


__all__ = [
    "ProjectionJobError",
    "ProjectionJobRun",
    "ProjectionJobRunner",
    "create_projection_job_runner",
    "is_lock_contention",
]
