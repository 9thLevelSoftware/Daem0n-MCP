"""Durable read leases and provider-first garbage collection for dense generations."""

from __future__ import annotations

import json
import re
import secrets
import sqlite3
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Protocol, cast

from .providers import dense_manifest_details, qdrant_collection_exists

_WORKSPACE_ID = re.compile(r"^ws_[0-9a-f]{24}$")
_OWNER_ID = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
_CLAIM_VALUE = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
_PROVIDER_KEY = re.compile(r"^[A-Za-z0-9._:-]{1,64}$")

DEFAULT_READ_LEASE_US = 120_000_000
DEFAULT_GC_CLAIM_US = 120_000_000
DEFAULT_GC_RETRY_US = 1_000_000
DEFAULT_RETAIN_INACTIVE = 1
MAX_GC_ADMISSIONS_PER_PASS = 100
MAX_GC_RECONCILE_WORKSPACES = 16


class DenseGenerationLifecycleError(RuntimeError):
    """A dense generation lease or cleanup transition was rejected."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class _DenseCollectionClient(Protocol):
    def delete_collection(self, collection_name: str) -> object: ...


@dataclass(frozen=True, slots=True)
class DenseGenerationReadLease:
    workspace_id: str
    provider_key: str
    generation: int
    manifest_id: str
    owner_id: str
    fencing_token: int
    expires_at_us: int


@dataclass(frozen=True, slots=True)
class DenseGenerationGCResult:
    workspace_id: str
    provider_key: str
    generation: int
    status: str
    reason: str | None = None

    @property
    def job_id(self) -> str:
        return f"dense-gc:{self.workspace_id}:{self.provider_key}:{self.generation}"


@dataclass(frozen=True, slots=True)
class _GCClaim:
    workspace_id: str
    provider_key: str
    generation: int
    collection_name: str
    attempts: int
    max_attempts: int
    claim_token: str


def _now_us(clock_us: Callable[[], int] | None) -> int:
    value = (clock_us or (lambda: time.time_ns() // 1_000))()
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not 0 <= value <= 9_223_372_036_854_775_807
    ):
        raise DenseGenerationLifecycleError("DENSE_GENERATION_CLOCK_INVALID")
    return value


def _positive_duration(value: int, name: str) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not 1 <= value <= 3_600_000_000
    ):
        raise ValueError(f"{name} must be between 1 and 3600000000 microseconds")
    return value


def _validate_identity(workspace_id: str, provider_key: str) -> None:
    if (
        not isinstance(workspace_id, str)
        or _WORKSPACE_ID.fullmatch(workspace_id) is None
    ):
        raise ValueError("workspace_id is invalid")
    if (
        not isinstance(provider_key, str)
        or _PROVIDER_KEY.fullmatch(provider_key) is None
    ):
        raise ValueError("provider_key is invalid")


def _validate_owner(owner_id: str) -> None:
    if not isinstance(owner_id, str) or _OWNER_ID.fullmatch(owner_id) is None:
        raise ValueError("owner_id is invalid")


def _begin_immediate(connection: sqlite3.Connection) -> None:
    if connection.in_transaction:
        raise DenseGenerationLifecycleError("DENSE_GENERATION_TRANSACTION_OPEN")
    connection.execute("BEGIN IMMEDIATE")


def _generation_collection(
    workspace_id: str,
    provider_key: str,
    generation: int,
    details_json: object,
) -> str:
    try:
        details = json.loads(str(details_json))
        if not isinstance(details, Mapping):
            raise ValueError
        expected = dense_manifest_details(
            workspace_id=workspace_id,
            provider_key=provider_key,
            generation=generation,
            model_id=str(details["model_id"]),
            dimension=int(details["dimension"]),
            collection_prefix=str(details["collection_prefix"]),
        )
    except (
        KeyError,
        TypeError,
        ValueError,
        json.JSONDecodeError,
        RecursionError,
    ) as exc:
        raise DenseGenerationLifecycleError(
            "DENSE_GENERATION_COLLECTION_IDENTITY_INVALID"
        ) from exc
    if any(details.get(key) != value for key, value in expected.items()):
        raise DenseGenerationLifecycleError(
            "DENSE_GENERATION_COLLECTION_IDENTITY_INVALID"
        )
    collection_name = expected["collection_name"]
    if not isinstance(collection_name, str) or not 1 <= len(collection_name) <= 255:
        raise DenseGenerationLifecycleError(
            "DENSE_GENERATION_COLLECTION_IDENTITY_INVALID"
        )
    return collection_name


def acquire_active_generation_lease(
    connection: sqlite3.Connection,
    *,
    workspace_id: str,
    provider_key: str,
    owner_id: str,
    clock_us: Callable[[], int] | None = None,
    lease_duration_us: int = DEFAULT_READ_LEASE_US,
) -> DenseGenerationReadLease:
    """Lease the exact active dense manifest in one fenced transaction."""

    _validate_identity(workspace_id, provider_key)
    _validate_owner(owner_id)
    duration = _positive_duration(lease_duration_us, "lease_duration_us")
    now = _now_us(clock_us)
    _begin_immediate(connection)
    try:
        row = connection.execute(
            "SELECT manifest_id,generation,details_json FROM projection_manifests "
            "WHERE workspace_id=? AND projection_name='dense' AND status='active' "
            "ORDER BY generation DESC LIMIT 1",
            (workspace_id,),
        ).fetchone()
        if row is None:
            raise DenseGenerationLifecycleError("DENSE_GENERATION_ACTIVE_MISSING")
        manifest_id = str(row[0])
        generation = int(row[1])
        _generation_collection(workspace_id, provider_key, generation, row[2])
        if (
            connection.execute(
                "SELECT 1 FROM dense_generation_gc_jobs WHERE workspace_id=? "
                "AND provider_key=? AND projection_generation=?",
                (workspace_id, provider_key, generation),
            ).fetchone()
            is not None
        ):
            raise DenseGenerationLifecycleError("DENSE_GENERATION_GC_ENROLLED")
        existing = connection.execute(
            "SELECT fencing_token FROM dense_generation_read_leases "
            "WHERE workspace_id=? AND provider_key=? AND projection_generation=? "
            "AND owner_id=?",
            (workspace_id, provider_key, generation, owner_id),
        ).fetchone()
        fencing_token = 1 if existing is None else int(existing[0]) + 1
        expires = now + duration
        if expires > 9_223_372_036_854_775_807:
            raise DenseGenerationLifecycleError("DENSE_GENERATION_CLOCK_INVALID")
        connection.execute(
            "INSERT INTO dense_generation_read_leases("
            "workspace_id,projection_name,provider_key,projection_generation,"
            "owner_id,fencing_token,acquired_at_us,renewed_at_us,expires_at_us) "
            "VALUES (?,'dense',?,?,?,?,?,?,?) ON CONFLICT(workspace_id,provider_key,"
            "projection_generation,owner_id) DO UPDATE SET "
            "fencing_token=excluded.fencing_token,acquired_at_us=excluded.acquired_at_us,"
            "renewed_at_us=excluded.renewed_at_us,expires_at_us=excluded.expires_at_us",
            (
                workspace_id,
                provider_key,
                generation,
                owner_id,
                fencing_token,
                now,
                now,
                expires,
            ),
        )
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    return DenseGenerationReadLease(
        workspace_id,
        provider_key,
        generation,
        manifest_id,
        owner_id,
        fencing_token,
        expires,
    )


def renew_generation_lease(
    connection: sqlite3.Connection,
    lease: DenseGenerationReadLease,
    *,
    clock_us: Callable[[], int] | None = None,
    lease_duration_us: int = DEFAULT_READ_LEASE_US,
) -> DenseGenerationReadLease:
    """Renew only the still-live owner/fence; an expired lease cannot revive."""

    duration = _positive_duration(lease_duration_us, "lease_duration_us")
    now = _now_us(clock_us)
    expires = now + duration
    _begin_immediate(connection)
    try:
        if (
            connection.execute(
                "SELECT 1 FROM dense_generation_gc_jobs WHERE workspace_id=? "
                "AND provider_key=? AND projection_generation=?",
                (lease.workspace_id, lease.provider_key, lease.generation),
            ).fetchone()
            is not None
        ):
            raise DenseGenerationLifecycleError("DENSE_GENERATION_LEASE_LOST")
        changed = connection.execute(
            "UPDATE dense_generation_read_leases SET renewed_at_us=?,expires_at_us=? "
            "WHERE workspace_id=? AND provider_key=? AND projection_generation=? "
            "AND owner_id=? AND fencing_token=? AND expires_at_us>?",
            (
                now,
                expires,
                lease.workspace_id,
                lease.provider_key,
                lease.generation,
                lease.owner_id,
                lease.fencing_token,
                now,
            ),
        ).rowcount
        if changed != 1:
            raise DenseGenerationLifecycleError("DENSE_GENERATION_LEASE_LOST")
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    return DenseGenerationReadLease(
        lease.workspace_id,
        lease.provider_key,
        lease.generation,
        lease.manifest_id,
        lease.owner_id,
        lease.fencing_token,
        expires,
    )


def release_generation_lease(
    connection: sqlite3.Connection,
    lease: DenseGenerationReadLease,
    *,
    clock_us: Callable[[], int] | None = None,
) -> bool:
    """Delete an owner/fence without allowing a stale holder to release a successor."""

    now = _now_us(clock_us)
    _begin_immediate(connection)
    try:
        changed = connection.execute(
            "DELETE FROM dense_generation_read_leases "
            "WHERE workspace_id=? AND provider_key=? AND projection_generation=? "
            "AND owner_id=? AND fencing_token=? AND expires_at_us>?",
            (
                lease.workspace_id,
                lease.provider_key,
                lease.generation,
                lease.owner_id,
                lease.fencing_token,
                now,
            ),
        ).rowcount
        if changed == 1:
            enqueue_inactive_generation_gc(
                connection,
                workspace_id=lease.workspace_id,
                provider_key=lease.provider_key,
                now_us=now,
            )
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    return changed == 1


def enqueue_inactive_generation_gc(
    connection: sqlite3.Connection,
    *,
    workspace_id: str,
    provider_key: str,
    now_us: int,
    retain_inactive: int = DEFAULT_RETAIN_INACTIVE,
) -> int:
    """Enroll old ready generations; the caller may already own a transaction."""

    _validate_identity(workspace_id, provider_key)
    if isinstance(now_us, bool) or not isinstance(now_us, int) or now_us < 0:
        raise ValueError("now_us is invalid")
    if (
        isinstance(retain_inactive, bool)
        or not isinstance(retain_inactive, int)
        or not 0 <= retain_inactive <= 16
    ):
        raise ValueError("retain_inactive must be between 0 and 16")
    owns_transaction = not connection.in_transaction
    if owns_transaction:
        connection.execute("BEGIN IMMEDIATE")
    inserted = 0
    try:
        rows = connection.execute(
            "WITH ranked AS (SELECT generation,details_json,"
            "ROW_NUMBER() OVER (ORDER BY generation DESC) AS inactive_rank "
            "FROM projection_manifests WHERE workspace_id=? "
            "AND projection_name='dense' AND status='ready' "
            "AND json_extract(details_json,'$.provider_key')=?) "
            "SELECT ranked.generation,ranked.details_json FROM ranked "
            "WHERE ranked.inactive_rank>? AND NOT EXISTS (SELECT 1 FROM "
            "dense_generation_gc_jobs gc WHERE gc.workspace_id=? "
            "AND gc.provider_key=? AND gc.projection_generation=ranked.generation) "
            "AND NOT EXISTS (SELECT 1 FROM dense_generation_read_leases lease "
            "WHERE lease.workspace_id=? AND lease.provider_key=? "
            "AND lease.projection_generation=ranked.generation "
            "AND lease.expires_at_us>?) ORDER BY ranked.generation DESC LIMIT ?",
            (
                workspace_id,
                provider_key,
                retain_inactive,
                workspace_id,
                provider_key,
                workspace_id,
                provider_key,
                now_us,
                MAX_GC_ADMISSIONS_PER_PASS,
            ),
        ).fetchall()
        for generation_value, details_json in rows:
            generation = int(generation_value)
            try:
                collection_name = _generation_collection(
                    workspace_id, provider_key, generation, details_json
                )
                status = "queued"
                error = None
            except DenseGenerationLifecycleError as exc:
                collection_name = None
                status = "dead_letter"
                error = exc.code
            changed = connection.execute(
                "INSERT INTO dense_generation_gc_jobs("
                "workspace_id,projection_name,provider_key,projection_generation,"
                "collection_name,status,attempts,max_attempts,available_at_us,"
                "claim_owner,claim_token,claim_expires_at_us,last_error_code,"
                "created_at_us,updated_at_us) VALUES (?,'dense',?,?,?,?,0,3,?,"
                "NULL,NULL,NULL,?,?,?) ON CONFLICT(workspace_id,provider_key,"
                "projection_generation) DO NOTHING",
                (
                    workspace_id,
                    provider_key,
                    generation,
                    collection_name,
                    status,
                    now_us,
                    error,
                    now_us,
                    now_us,
                ),
            ).rowcount
            inserted += int(changed)
        if owns_transaction:
            connection.commit()
    except Exception:
        if owns_transaction:
            connection.rollback()
        raise
    return inserted


def reconcile_inactive_generation_gc(
    connection: sqlite3.Connection,
    *,
    now_us: int,
    max_workspaces: int = MAX_GC_RECONCILE_WORKSPACES,
) -> int:
    """Boundedly admit old generations skipped while a read lease was live."""

    if isinstance(now_us, bool) or not isinstance(now_us, int) or now_us < 0:
        raise ValueError("now_us is invalid")
    if (
        isinstance(max_workspaces, bool)
        or not isinstance(max_workspaces, int)
        or not 1 <= max_workspaces <= 100
    ):
        raise ValueError("max_workspaces must be between 1 and 100")
    rows = connection.execute(
        "WITH ranked AS (SELECT workspace_id,generation,"
        "json_extract(details_json,'$.provider_key') AS provider_key,"
        "ROW_NUMBER() OVER (PARTITION BY workspace_id,"
        "json_extract(details_json,'$.provider_key') "
        "ORDER BY generation DESC) AS inactive_rank FROM projection_manifests "
        "WHERE projection_name='dense' AND status='ready'), eligible AS ("
        "SELECT ranked.workspace_id,ranked.provider_key FROM ranked WHERE "
        "ranked.inactive_rank>? AND typeof(ranked.provider_key)='text' "
        "AND NOT EXISTS (SELECT 1 FROM dense_generation_gc_jobs gc WHERE "
        "gc.workspace_id=ranked.workspace_id AND "
        "gc.provider_key=ranked.provider_key AND "
        "gc.projection_generation=ranked.generation) AND NOT EXISTS ("
        "SELECT 1 FROM dense_generation_read_leases lease WHERE "
        "lease.workspace_id=ranked.workspace_id AND "
        "lease.provider_key=ranked.provider_key AND "
        "lease.projection_generation=ranked.generation AND "
        "lease.expires_at_us>?) GROUP BY ranked.workspace_id,"
        "ranked.provider_key) SELECT workspace_id,provider_key FROM eligible "
        "ORDER BY workspace_id,provider_key LIMIT ?",
        (DEFAULT_RETAIN_INACTIVE, now_us, max_workspaces),
    ).fetchall()
    admitted = 0
    for workspace_value, provider_value in rows:
        if not isinstance(provider_value, str):
            continue
        admitted += enqueue_inactive_generation_gc(
            connection,
            workspace_id=str(workspace_value),
            provider_key=provider_value,
            now_us=now_us,
        )
    return admitted


def cancel_queued_gc_for_reactivation(
    connection: sqlite3.Connection,
    *,
    workspace_id: str,
    provider_key: str,
    generation: int,
) -> bool:
    """Atomically withdraw a queued cleanup before a deliberate rollback."""

    _validate_identity(workspace_id, provider_key)
    if (
        isinstance(generation, bool)
        or not isinstance(generation, int)
        or generation < 1
    ):
        raise ValueError("generation is invalid")
    changed = connection.execute(
        "DELETE FROM dense_generation_gc_jobs WHERE workspace_id=? AND provider_key=? "
        "AND projection_generation=? AND status='queued'",
        (workspace_id, provider_key, generation),
    ).rowcount
    return changed == 1


class DenseGenerationGarbageCollector:
    """Claim bounded cleanup work and remove provider data before SQLite metadata."""

    def __init__(
        self,
        connection: sqlite3.Connection,
        *,
        client: object,
        clock_us: Callable[[], int] | None = None,
        claim_owner: str = "daem0nmcp-dense-gc",
        token_factory: Callable[[], str] | None = None,
        claim_duration_us: int = DEFAULT_GC_CLAIM_US,
        retry_delay_us: int = DEFAULT_GC_RETRY_US,
        cancelled: Callable[[], bool] | None = None,
    ) -> None:
        if not isinstance(connection, sqlite3.Connection):
            raise ValueError("connection must be a SQLite connection")
        if (
            not isinstance(claim_owner, str)
            or _CLAIM_VALUE.fullmatch(claim_owner) is None
        ):
            raise ValueError("claim_owner is invalid")
        self.connection = connection
        self.client = cast(_DenseCollectionClient, client)
        self._clock_us = clock_us
        self._claim_owner = claim_owner
        self._token_factory = token_factory or (lambda: secrets.token_hex(24))
        self._claim_duration_us = _positive_duration(
            claim_duration_us, "claim_duration_us"
        )
        self._retry_delay_us = _positive_duration(retry_delay_us, "retry_delay_us")
        if cancelled is not None and not callable(cancelled):
            raise ValueError("cancelled must be callable")
        self._cancelled = cancelled or (lambda: False)
        if not callable(getattr(client, "delete_collection", None)) or not any(
            callable(getattr(client, name, None))
            for name in ("collection_exists", "get_collections", "get_collection")
        ):
            raise ValueError("client does not provide dense collection lifecycle")

    def run_slice(
        self, max_generations: int = 1
    ) -> tuple[DenseGenerationGCResult, ...]:
        if (
            isinstance(max_generations, bool)
            or not isinstance(max_generations, int)
            or not 1 <= max_generations <= 100
        ):
            raise ValueError("max_generations must be between 1 and 100")
        results: list[DenseGenerationGCResult] = []
        for _ in range(max_generations):
            if self._cancelled():
                break
            claim = self._claim()
            if claim is None:
                break
            results.append(self._execute(claim))
        return tuple(results)

    def _claim(self) -> _GCClaim | None:
        now = _now_us(self._clock_us)
        token = self._token_factory()
        if not isinstance(token, str) or _CLAIM_VALUE.fullmatch(token) is None:
            raise DenseGenerationLifecycleError("DENSE_GENERATION_GC_TOKEN_INVALID")
        _begin_immediate(self.connection)
        try:
            row = self.connection.execute(
                "SELECT workspace_id,provider_key,projection_generation,"
                "collection_name,attempts,max_attempts FROM dense_generation_gc_jobs "
                "WHERE (status='queued' AND available_at_us<=?) OR "
                "(status='running' AND claim_expires_at_us<=?) "
                "ORDER BY available_at_us,workspace_id,provider_key,"
                "projection_generation LIMIT 1",
                (now, now),
            ).fetchone()
            if row is None:
                self.connection.commit()
                return None
            workspace_id, provider_key, generation = (
                str(row[0]),
                str(row[1]),
                int(row[2]),
            )
            manifest = self.connection.execute(
                "SELECT status,details_json FROM projection_manifests WHERE workspace_id=? "
                "AND projection_name='dense' AND generation=?",
                (workspace_id, generation),
            ).fetchone()
            if manifest is None:
                self.connection.execute(
                    "DELETE FROM dense_generation_gc_jobs WHERE workspace_id=? "
                    "AND provider_key=? AND projection_generation=?",
                    (workspace_id, provider_key, generation),
                )
                self.connection.commit()
                return None
            if str(manifest[0]) != "ready":
                raise DenseGenerationLifecycleError("DENSE_GENERATION_GC_STATE_INVALID")
            collection_name = _generation_collection(
                workspace_id, provider_key, generation, manifest[1]
            )
            if row[3] != collection_name:
                raise DenseGenerationLifecycleError(
                    "DENSE_GENERATION_COLLECTION_IDENTITY_INVALID"
                )
            lease_row = self.connection.execute(
                "SELECT MIN(expires_at_us) FROM dense_generation_read_leases "
                "WHERE workspace_id=? AND provider_key=? AND projection_generation=? "
                "AND expires_at_us>?",
                (workspace_id, provider_key, generation, now),
            ).fetchone()
            if lease_row is not None and lease_row[0] is not None:
                self.connection.execute(
                    "UPDATE dense_generation_gc_jobs SET status='queued',"
                    "available_at_us=?,claim_owner=NULL,claim_token=NULL,"
                    "claim_expires_at_us=NULL,updated_at_us=? WHERE workspace_id=? "
                    "AND provider_key=? AND projection_generation=?",
                    (
                        int(lease_row[0]),
                        now,
                        workspace_id,
                        provider_key,
                        generation,
                    ),
                )
                self.connection.commit()
                return None
            attempts = int(row[4]) + 1
            changed = self.connection.execute(
                "UPDATE dense_generation_gc_jobs SET status='running',attempts=?,"
                "claim_owner=?,claim_token=?,claim_expires_at_us=?,"
                "last_error_code=NULL,updated_at_us=? WHERE workspace_id=? "
                "AND provider_key=? AND projection_generation=? AND "
                "((status='queued' AND available_at_us<=?) OR "
                "(status='running' AND claim_expires_at_us<=?))",
                (
                    attempts,
                    self._claim_owner,
                    token,
                    now + self._claim_duration_us,
                    now,
                    workspace_id,
                    provider_key,
                    generation,
                    now,
                    now,
                ),
            ).rowcount
            if changed != 1:
                self.connection.rollback()
                return None
            self.connection.commit()
        except DenseGenerationLifecycleError as exc:
            self.connection.rollback()
            self._dead_letter_unclaimed(row if "row" in locals() else None, exc.code)
            return None
        except Exception:
            self.connection.rollback()
            raise
        return _GCClaim(
            workspace_id,
            provider_key,
            generation,
            collection_name,
            attempts,
            int(row[5]),
            token,
        )

    def _dead_letter_unclaimed(self, row: object, code: str) -> None:
        if not isinstance(row, sqlite3.Row) and not isinstance(row, tuple):
            return
        try:
            workspace_id, provider_key, generation = (
                str(row[0]),
                str(row[1]),
                int(row[2]),
            )
        except (IndexError, TypeError, ValueError):
            return
        now = _now_us(self._clock_us)
        _begin_immediate(self.connection)
        try:
            self.connection.execute(
                "UPDATE dense_generation_gc_jobs SET status='dead_letter',"
                "claim_owner=NULL,claim_token=NULL,claim_expires_at_us=NULL,"
                "last_error_code=?,updated_at_us=? WHERE workspace_id=? "
                "AND provider_key=? AND projection_generation=?",
                (code, now, workspace_id, provider_key, generation),
            )
            self.connection.commit()
        except Exception:
            self.connection.rollback()
            raise

    def _execute(self, claim: _GCClaim) -> DenseGenerationGCResult:
        if self._cancelled():
            return self._record_failure(
                claim, "DENSE_GENERATION_GC_CANCELLED", consume=False
            )
        try:
            exists = qdrant_collection_exists(self.client, claim.collection_name)
            if self._cancelled():
                return self._record_failure(
                    claim, "DENSE_GENERATION_GC_CANCELLED", consume=False
                )
            if exists:
                self.client.delete_collection(claim.collection_name)
            if self._cancelled():
                return self._record_failure(
                    claim, "DENSE_GENERATION_GC_CANCELLED", consume=False
                )
            still_exists = qdrant_collection_exists(self.client, claim.collection_name)
            if self._cancelled():
                return self._record_failure(
                    claim, "DENSE_GENERATION_GC_CANCELLED", consume=False
                )
            if still_exists:
                return self._record_failure(
                    claim, "DENSE_GENERATION_GC_PROVIDER_AMBIGUOUS"
                )
        except Exception:
            return self._record_failure(
                claim, "DENSE_GENERATION_GC_PROVIDER_UNAVAILABLE"
            )
        try:
            return self._finalize(claim)
        except DenseGenerationLifecycleError as exc:
            # Provider absence is already established.  Do not guess that a
            # lost/expired database claim still owns finalization: leave the
            # running row for its fenced expiry and a replay-safe retry.
            return DenseGenerationGCResult(
                claim.workspace_id,
                claim.provider_key,
                claim.generation,
                "running",
                exc.code,
            )

    def _finalize(self, claim: _GCClaim) -> DenseGenerationGCResult:
        now = _now_us(self._clock_us)
        _begin_immediate(self.connection)
        try:
            job = self.connection.execute(
                "SELECT claim_token,claim_expires_at_us FROM dense_generation_gc_jobs "
                "WHERE workspace_id=? AND provider_key=? AND projection_generation=? "
                "AND status='running'",
                (claim.workspace_id, claim.provider_key, claim.generation),
            ).fetchone()
            manifest = self.connection.execute(
                "SELECT status,details_json FROM projection_manifests WHERE workspace_id=? "
                "AND projection_name='dense' AND generation=?",
                (claim.workspace_id, claim.generation),
            ).fetchone()
            leased = self.connection.execute(
                "SELECT 1 FROM dense_generation_read_leases WHERE workspace_id=? "
                "AND provider_key=? AND projection_generation=? AND expires_at_us>? LIMIT 1",
                (claim.workspace_id, claim.provider_key, claim.generation, now),
            ).fetchone()
            if (
                job is None
                or str(job[0]) != claim.claim_token
                or int(job[1]) <= now
                or manifest is None
                or str(manifest[0]) != "ready"
                or leased is not None
                or _generation_collection(
                    claim.workspace_id,
                    claim.provider_key,
                    claim.generation,
                    manifest[1],
                )
                != claim.collection_name
            ):
                raise DenseGenerationLifecycleError("DENSE_GENERATION_GC_LEASE_LOST")
            if self._cancelled():
                self.connection.rollback()
                return self._record_failure(
                    claim, "DENSE_GENERATION_GC_CANCELLED", consume=False
                )
            self.connection.execute(
                "DELETE FROM dense_projection_refs WHERE workspace_id=? "
                "AND provider_key=? AND projection_generation=?",
                (claim.workspace_id, claim.provider_key, claim.generation),
            )
            changed = self.connection.execute(
                "DELETE FROM projection_manifests WHERE workspace_id=? "
                "AND projection_name='dense' AND generation=? AND status='ready'",
                (claim.workspace_id, claim.generation),
            ).rowcount
            if changed != 1:
                raise DenseGenerationLifecycleError("DENSE_GENERATION_GC_STATE_INVALID")
            self.connection.commit()
        except Exception:
            self.connection.rollback()
            raise
        return DenseGenerationGCResult(
            claim.workspace_id,
            claim.provider_key,
            claim.generation,
            "succeeded",
        )

    def _record_failure(
        self,
        claim: _GCClaim,
        code: str,
        *,
        consume: bool = True,
    ) -> DenseGenerationGCResult:
        now = _now_us(self._clock_us)
        dead = consume and claim.attempts >= claim.max_attempts
        status = "dead_letter" if dead else "queued"
        _begin_immediate(self.connection)
        try:
            changed = self.connection.execute(
                "UPDATE dense_generation_gc_jobs SET status=?,attempts=?,"
                "available_at_us=?,claim_owner=NULL,claim_token=NULL,"
                "claim_expires_at_us=NULL,last_error_code=?,updated_at_us=? "
                "WHERE workspace_id=? AND provider_key=? AND projection_generation=? "
                "AND status='running' AND claim_token=?",
                (
                    status,
                    claim.attempts if consume else max(0, claim.attempts - 1),
                    now + self._retry_delay_us,
                    code,
                    now,
                    claim.workspace_id,
                    claim.provider_key,
                    claim.generation,
                    claim.claim_token,
                ),
            ).rowcount
            if changed != 1:
                raise DenseGenerationLifecycleError("DENSE_GENERATION_GC_LEASE_LOST")
            self.connection.commit()
        except Exception:
            self.connection.rollback()
            raise
        return DenseGenerationGCResult(
            claim.workspace_id,
            claim.provider_key,
            claim.generation,
            status,
            code,
        )


def retry_dense_generation_gc(
    connection: sqlite3.Connection,
    *,
    workspace_id: str,
    provider_key: str,
    generation: int,
    clock_us: Callable[[], int] | None = None,
) -> bool:
    """Explicitly requeue one dead letter after an operator/provider repair."""

    _validate_identity(workspace_id, provider_key)
    now = _now_us(clock_us)
    _begin_immediate(connection)
    try:
        changed = connection.execute(
            "UPDATE dense_generation_gc_jobs SET status='queued',attempts=0,"
            "available_at_us=?,claim_owner=NULL,claim_token=NULL,"
            "claim_expires_at_us=NULL,last_error_code=NULL,updated_at_us=? "
            "WHERE workspace_id=? AND provider_key=? AND projection_generation=? "
            "AND status='dead_letter'",
            (now, now, workspace_id, provider_key, generation),
        ).rowcount
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    return changed == 1


def dense_generation_gc_diagnostics(
    connection: sqlite3.Connection,
    *,
    workspace_id: str | None = None,
) -> dict[str, object]:
    """Return bounded status counts and stable errors without collection contents."""

    if workspace_id is not None and (
        not isinstance(workspace_id, str)
        or _WORKSPACE_ID.fullmatch(workspace_id) is None
    ):
        raise ValueError("workspace_id is invalid")
    where = "" if workspace_id is None else " WHERE workspace_id=?"
    params: tuple[object, ...] = () if workspace_id is None else (workspace_id,)
    rows = connection.execute(
        "SELECT status,COUNT(*) FROM dense_generation_gc_jobs"
        + where
        + " GROUP BY status ORDER BY status",
        params,
    ).fetchall()
    errors = connection.execute(
        "SELECT last_error_code,COUNT(*) FROM dense_generation_gc_jobs"
        + where
        + (" WHERE " if not where else " AND ")
        + "last_error_code IS NOT NULL GROUP BY last_error_code "
        "ORDER BY last_error_code LIMIT 16",
        params,
    ).fetchall()
    return {
        "counts": {str(row[0]): int(row[1]) for row in rows},
        "errors": {str(row[0]): int(row[1]) for row in errors},
    }


def next_dense_generation_gc_deadline(connection: sqlite3.Connection) -> int | None:
    row = connection.execute(
        "SELECT MIN(CASE WHEN status='queued' THEN available_at_us "
        "ELSE claim_expires_at_us END) FROM dense_generation_gc_jobs "
        "WHERE status IN ('queued','running')"
    ).fetchone()
    return None if row is None or row[0] is None else int(row[0])


__all__ = [
    "DEFAULT_READ_LEASE_US",
    "DenseGenerationGarbageCollector",
    "DenseGenerationGCResult",
    "DenseGenerationLifecycleError",
    "DenseGenerationReadLease",
    "acquire_active_generation_lease",
    "cancel_queued_gc_for_reactivation",
    "dense_generation_gc_diagnostics",
    "enqueue_inactive_generation_gc",
    "next_dense_generation_gc_deadline",
    "release_generation_lease",
    "reconcile_inactive_generation_gc",
    "renew_generation_lease",
    "retry_dense_generation_gc",
]
