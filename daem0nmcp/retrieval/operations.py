"""Dependency-free operator operations for retrieval projections."""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Callable, Mapping
from dataclasses import asdict
from typing import Any

from .projections import (
    COLLECTABLE_PROJECTIONS,
    LexicalProjectionBuilder,
    ProjectionBuildError,
    collect_superseded_generations,
)

_WORKSPACE_ID = re.compile(r"^ws_[0-9a-f]{24}$")
_PROJECTIONS = frozenset(
    {"lexical", "dense", "graph", "temporal", "procedure", "outcome"}
)


class ProjectionOperationError(RuntimeError):
    """Owned operator-facing projection error."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def _validate_workspace(workspace_id: str) -> None:
    if (
        not isinstance(workspace_id, str)
        or _WORKSPACE_ID.fullmatch(workspace_id) is None
    ):
        raise ProjectionOperationError("INVALID_WORKSPACE_ID")


def rebuild_projection(
    connection: sqlite3.Connection,
    *,
    workspace_id: str,
    projection: str,
    dry_run: bool = False,
    builders: Mapping[str, Callable[..., Any]] | None = None,
) -> dict[str, Any]:
    """Build one staging projection or report its exact dry-run inventory."""

    if not isinstance(connection, sqlite3.Connection):
        raise ProjectionOperationError("PROJECTION_DATABASE_INVALID")
    _validate_workspace(workspace_id)
    if projection not in _PROJECTIONS:
        raise ProjectionOperationError("UNKNOWN_PROJECTION")
    if not isinstance(dry_run, bool):
        raise ProjectionOperationError("INVALID_DRY_RUN")
    registry: dict[str, Callable[..., Any]] = {
        "lexical": LexicalProjectionBuilder(connection).rebuild
    }
    if builders is not None:
        if not isinstance(builders, Mapping):
            raise ProjectionOperationError("PROJECTION_BUILDERS_INVALID")
        for name, builder in builders.items():
            if name not in _PROJECTIONS or not callable(builder):
                raise ProjectionOperationError("PROJECTION_BUILDERS_INVALID")
            registry[name] = builder
    builder = registry.get(projection)
    if builder is None:
        raise ProjectionOperationError("PROJECTION_BUILDER_UNAVAILABLE")
    try:
        result = builder(workspace_id, dry_run=dry_run)
    except ProjectionBuildError as exc:
        raise ProjectionOperationError(exc.code) from exc
    except ProjectionOperationError:
        raise
    except Exception as exc:
        raise ProjectionOperationError("PROJECTION_REBUILD_FAILED") from exc
    try:
        payload = asdict(result)
    except (TypeError, ValueError) as exc:
        raise ProjectionOperationError("PROJECTION_RESULT_INVALID") from exc
    if dry_run and payload.get("capability_status") == "ready":
        payload["status"] = "dry_run"
    return payload


def projection_status(
    connection: sqlite3.Connection,
    workspace_id: str,
) -> dict[str, Any]:
    """Return a path-free, deterministic projection and job status snapshot."""

    if not isinstance(connection, sqlite3.Connection):
        raise ProjectionOperationError("PROJECTION_DATABASE_INVALID")
    _validate_workspace(workspace_id)
    try:
        manifest_rows = connection.execute(
            """
            SELECT projection_name,generation,status,row_count,
                   details_json
            FROM projection_manifests
            WHERE workspace_id=?
            ORDER BY projection_name ASC,generation ASC
            """,
            (workspace_id,),
        ).fetchall()
        job_rows = connection.execute(
            """
            SELECT status,attempts,max_attempts,payload_json
            FROM background_jobs
            WHERE workspace_id=? AND job_type='retrieval.projection_rebuild'
            ORDER BY created_at_us ASC,job_id ASC
            """,
            (workspace_id,),
        ).fetchall()
    except sqlite3.Error as exc:
        raise ProjectionOperationError("PROJECTION_STATUS_UNAVAILABLE") from exc
    manifests: list[dict[str, Any]] = []
    for row in manifest_rows:
        try:
            details = json.loads(str(row[4]))
        except (TypeError, ValueError, RecursionError) as exc:
            raise ProjectionOperationError("PROJECTION_MANIFEST_INVALID") from exc
        if not isinstance(details, dict):
            raise ProjectionOperationError("PROJECTION_MANIFEST_INVALID")
        manifests.append(
            {
                "active": str(row[2]) == "active",
                "build_config_hash": details.get("build_config_hash"),
                "generation": int(row[1]),
                "projection": str(row[0]),
                "rebuild_required": details.get("rebuild_required_event_id")
                is not None,
                "row_count": int(row[3]),
                "status": str(row[2]),
            }
        )
    jobs: list[dict[str, Any]] = []
    for row in job_rows:
        try:
            payload = json.loads(str(row[3]))
        except (TypeError, ValueError, RecursionError) as exc:
            raise ProjectionOperationError("PROJECTION_JOB_INVALID") from exc
        names = payload.get("projection_names") if isinstance(payload, dict) else None
        if not isinstance(names, list) or not all(
            name in _PROJECTIONS for name in names
        ):
            raise ProjectionOperationError("PROJECTION_JOB_INVALID")
        jobs.append(
            {
                "attempts": int(row[1]),
                "max_attempts": int(row[2]),
                "projection_names": names,
                "status": str(row[0]),
            }
        )
    return {
        "jobs": jobs,
        "manifests": manifests,
        "workspace_id": workspace_id,
    }


def compact_projections(
    connection: sqlite3.Connection,
    workspace_id: str,
    *,
    vacuum: bool = True,
) -> dict[str, Any]:
    """Drain every superseded generation offline, then reclaim the file.

    Online activations collect at most five generations each, so a database
    that grew before the garbage collector existed needs about a hundred
    writes to drain — and a ``VACUUM`` before that only rewrites the dead
    generations. This runs the same collector in a loop until it reports
    nothing left, so the following ``VACUUM`` actually reclaims the space.
    The caller owns the offline exclusive lock; no activation may run
    concurrently.
    """

    if not isinstance(connection, sqlite3.Connection):
        raise ProjectionOperationError("PROJECTION_DATABASE_INVALID")
    _validate_workspace(workspace_id)
    if connection.in_transaction:
        raise ProjectionOperationError("PROJECTION_DATABASE_INVALID")
    collected: dict[str, int] = {}
    try:
        before = _generation_counts(connection, workspace_id)
        for projection in sorted(COLLECTABLE_PROJECTIONS):
            removed = 0
            while True:
                passed = collect_superseded_generations(
                    connection, workspace_id, projection
                )
                if passed == 0:
                    break
                removed += passed
            collected[projection] = removed
        if vacuum:
            connection.execute("VACUUM")
        after = _generation_counts(connection, workspace_id)
    except sqlite3.Error as exc:
        raise ProjectionOperationError("PROJECTION_COMPACTION_FAILED") from exc
    return {
        "collected": collected,
        "superseded_manifests_after": after[0],
        "superseded_manifests_before": before[0],
        "vacuumed": bool(vacuum),
        "workspace_id": workspace_id,
    }


def _generation_counts(
    connection: sqlite3.Connection,
    workspace_id: str,
) -> tuple[int, int]:
    placeholders = ",".join("?" for _ in COLLECTABLE_PROJECTIONS)
    projections = sorted(COLLECTABLE_PROJECTIONS)
    superseded = int(
        connection.execute(
            "SELECT COUNT(*) FROM projection_manifests WHERE workspace_id=? "
            f"AND projection_name IN ({placeholders}) AND status<>'active'",
            (workspace_id, *projections),
        ).fetchone()[0]
    )
    total = int(
        connection.execute(
            "SELECT COUNT(*) FROM projection_manifests WHERE workspace_id=? "
            f"AND projection_name IN ({placeholders})",
            (workspace_id, *projections),
        ).fetchone()[0]
    )
    return superseded, total


__all__ = [
    "ProjectionOperationError",
    "compact_projections",
    "projection_status",
    "rebuild_projection",
]
