"""Atomic, coalesced scheduling for retrieval projection rebuilds."""

from __future__ import annotations

import hashlib
import re
from typing import Any, Protocol

from ..event_store import canonical_json_bytes, deterministic_id

_EVENT_ID = re.compile(r"^evt_[0-9a-f]{64}$")
_WORKSPACE_ID = re.compile(r"^ws_[0-9a-f]{24}$")
_PROJECTIONS = frozenset(
    {"lexical", "dense", "graph", "temporal", "procedure", "outcome"}
)


class SQLiteConnectionProtocol(Protocol):
    """Synchronous SQLite surface shared by stdlib and adapted connections."""

    @property
    def in_transaction(self) -> bool: ...

    def cursor(self) -> Any: ...

    def execute(self, statement: str, parameters: Any = ()) -> Any: ...


def enqueue_projection_rebuild(
    connection: SQLiteConnectionProtocol,
    *,
    workspace_id: str,
    projection_name: str,
    source_event_id: str | None,
    recorded_at_us: int,
    available_at_us: int | None = None,
    requeue_existing: bool = True,
) -> None:
    """Queue one bounded rebuild, coalescing later events for the same channel."""

    if (
        not callable(getattr(connection, "execute", None))
        or not callable(getattr(connection, "cursor", None))
        or not isinstance(getattr(connection, "in_transaction", None), bool)
    ):
        raise ValueError("connection must provide the synchronous SQLite protocol")
    if (
        not isinstance(workspace_id, str)
        or _WORKSPACE_ID.fullmatch(workspace_id) is None
    ):
        raise ValueError("workspace_id is invalid")
    if projection_name not in _PROJECTIONS:
        raise ValueError("projection_name is invalid")
    if source_event_id is not None and (
        not isinstance(source_event_id, str)
        or _EVENT_ID.fullmatch(source_event_id) is None
    ):
        raise ValueError("source_event_id is invalid")
    if (
        isinstance(recorded_at_us, bool)
        or not isinstance(recorded_at_us, int)
        or recorded_at_us < 0
    ):
        raise ValueError("recorded_at_us is invalid")
    if available_at_us is None:
        available_at_us = recorded_at_us
    if (
        isinstance(available_at_us, bool)
        or not isinstance(available_at_us, int)
        or available_at_us < recorded_at_us
        or available_at_us > 9_223_372_036_854_775_807
    ):
        raise ValueError("available_at_us is invalid")
    if not isinstance(requeue_existing, bool):
        raise ValueError("requeue_existing must be boolean")

    job_payload = {
        "projection_names": [projection_name],
        "source_event_id": source_event_id,
        "workspace_id": workspace_id,
    }
    payload_text = canonical_json_bytes(job_payload).decode("utf-8")
    payload_hash = hashlib.sha256(payload_text.encode("utf-8")).hexdigest()
    job_id = deterministic_id(
        "job",
        "retrieval-projection-rebuild",
        workspace_id,
        projection_name,
    )
    idempotency_key = f"active-projection:{projection_name}"
    priority = 100 if projection_name == "lexical" else 50
    conflict = (
        """
        DO UPDATE SET
            payload_json=excluded.payload_json,
            payload_hash=excluded.payload_hash,
            source_event_id=excluded.source_event_id,
            updated_at_us=excluded.updated_at_us,
            status=CASE
                WHEN background_jobs.status='running' THEN 'running'
                ELSE 'queued'
            END,
            attempts=CASE
                WHEN background_jobs.status='running'
                THEN background_jobs.attempts ELSE 0
            END,
            available_at_us=CASE
                WHEN background_jobs.status='running'
                THEN background_jobs.available_at_us
                ELSE excluded.available_at_us
            END,
            lease_owner=CASE
                WHEN background_jobs.status='running'
                THEN background_jobs.lease_owner ELSE NULL
            END,
            lease_token=CASE
                WHEN background_jobs.status='running'
                THEN background_jobs.lease_token ELSE NULL
            END,
            lease_expires_at_us=CASE
                WHEN background_jobs.status='running'
                THEN background_jobs.lease_expires_at_us ELSE NULL
            END,
            last_error_json=CASE
                WHEN background_jobs.status='running'
                THEN background_jobs.last_error_json ELSE NULL
            END,
            result_json=CASE
                WHEN background_jobs.status='running'
                THEN background_jobs.result_json ELSE NULL
            END
        """
        if requeue_existing
        else "DO NOTHING"
    )
    connection.execute(
        f"""
        INSERT INTO background_jobs (
            job_id, workspace_id, job_type, idempotency_key,
            payload_json, payload_hash, status, priority, attempts,
            max_attempts, available_at_us, source_event_id,
            created_at_us, updated_at_us
        ) VALUES (?, ?, 'retrieval.projection_rebuild', ?, ?, ?,
                  'queued', ?, 0, 3, ?, ?, ?, ?)
        ON CONFLICT(workspace_id, job_type, idempotency_key)
        {conflict}
        """,
        (
            job_id,
            workspace_id,
            idempotency_key,
            payload_text,
            payload_hash,
            priority,
            available_at_us,
            source_event_id,
            recorded_at_us,
            recorded_at_us,
        ),
    )


__all__ = ["SQLiteConnectionProtocol", "enqueue_projection_rebuild"]
