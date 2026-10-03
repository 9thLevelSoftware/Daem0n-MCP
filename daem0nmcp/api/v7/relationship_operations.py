"""Canonical v7 relationship mutation and graph-read adapters."""

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import threading
from collections import deque
from collections.abc import Callable, Mapping
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import MappingProxyType
from typing import Any

from ...bounded_workers import BoundedWorkerBusyError, BoundedWorkerPool
from ...event_store import (
    EventCommand,
    EventStore,
    EventStreamConflict,
    canonical_json_bytes,
    deterministic_id,
    sha256_json,
)
from ...schema_version import CURRENT_SCHEMA_VERSION
from ...storage_activation import ResolvedActiveDatabase
from ...workspace import Workspace, WorkspaceRegistry
from .application import AdmittedRequest
from .errors import STABLE_ERROR_CODE_SET
from .models import EvidenceRef, MutationReceipt, RecordSummary
from .runtime_protocols import ActiveStorageResolver, WorkerPool
from .runtime_services import WorkspaceStorageResolver
from .tasks import await_task_terminal
from .tools import (
    KnowledgeGraphData,
    KnowledgeGraphEdge,
    KnowledgeGraphNode,
    KnowledgeGraphRenderData,
    MemoryChainTraceData,
    MemoryRelatedData,
    ProjectionManifest,
    RelationshipPath,
    RelationshipView,
)

_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
_REQUIRED_TABLES = frozenset(
    {
        "background_jobs",
        "memory_events",
        "memory_records",
        "memory_relationship_versions",
        "projection_manifests",
        "retrieval_documents",
        "schema_version",
    }
)
_PUBLIC_RELATIONSHIP_TYPES = frozenset(
    {
        "led_to",
        "supersedes",
        "depends_on",
        "conflicts_with",
        "related_to",
        "evidence_for",
        "derived_from",
        "invalidates",
    }
)
_RECORD_COLUMNS = (
    "record_id,record_type,content,content_hash,tags_json,file_path,"
    "file_path_relative,archived,deleted_at_us,created_at_us,updated_at_us"
)
_RELATIONSHIP_COLUMNS = (
    "relation.relationship_id AS relationship_id,"
    "relation.source_record_id AS source_record_id,"
    "relation.target_record_id AS target_record_id,"
    "relation.relationship_type AS relationship_type,"
    "relation.legacy_type AS legacy_type,"
    "relation.description AS description,"
    "relation.confidence AS confidence,"
    "relation.metadata_json AS metadata_json,"
    "relation.content_hash AS content_hash,"
    "relation.valid_from_us AS valid_from_us,"
    "relation.valid_to_us AS valid_to_us,"
    "relation.asserted_by_event_id AS asserted_by_event_id,"
    "event.payload_json AS source_payload_json,"
    "event.payload_hash AS source_payload_hash"
)
_MAX_TRAVERSAL_EDGES = 2_000
_MAX_CHAIN_STATES = 20_000


class RelationshipOperationError(RuntimeError):
    """Stable, path-free failure understood by the shared v7 router."""

    def __init__(self, code: str) -> None:
        if code not in STABLE_ERROR_CODE_SET:
            raise ValueError("relationship operation error code is not stable")
        self.code = code
        super().__init__(code)


class _WorkerCancelledError(RuntimeError):
    pass


def _default_clock() -> datetime:
    return datetime.now(timezone.utc)


def _default_worker_pool() -> BoundedWorkerPool:
    return BoundedWorkerPool(
        max_workers=4,
        thread_name_prefix="daem0nmcp-v7-relationship",
    )


@dataclass(frozen=True, slots=True)
class RelationshipOperationDependencies:
    """Owned dependencies for canonical relationship operations."""

    storage_resolver: ActiveStorageResolver = field(
        default_factory=WorkspaceStorageResolver
    )
    clock: Callable[[], datetime] = field(default=_default_clock)
    worker_pool: WorkerPool = field(default_factory=_default_worker_pool)

    def __post_init__(self) -> None:
        if not callable(getattr(self.storage_resolver, "locked_active", None)):
            raise TypeError("storage_resolver must provide locked_active")
        if not callable(self.clock):
            raise TypeError("clock must be callable")
        if not callable(getattr(self.worker_pool, "run", None)) or not callable(
            getattr(self.worker_pool, "shutdown", None)
        ):
            raise TypeError("worker_pool must provide run and shutdown")

    def close(self) -> None:
        """Join and release the dependency-owned bounded worker pool."""

        self.worker_pool.shutdown()


def _authorize(
    workspace: Workspace,
    request: AdmittedRequest,
    tool_name: str,
) -> None:
    if (
        not isinstance(workspace, Workspace)
        or not isinstance(request, AdmittedRequest)
        or request.tool_name != tool_name
        or request.workspace_id != workspace.workspace_id
    ):
        raise RelationshipOperationError("UNAUTHORIZED_WORKSPACE")
    try:
        canonical = workspace.root.resolve(strict=True)
        registered = WorkspaceRegistry([canonical], default_root=canonical).default
        exact_root = os.path.normcase(str(workspace.root)) == os.path.normcase(
            str(canonical)
        )
    except (OSError, RuntimeError, TypeError, ValueError):
        raise RelationshipOperationError("UNAUTHORIZED_WORKSPACE") from None
    if registered.workspace_id != workspace.workspace_id or not exact_root:
        raise RelationshipOperationError("UNAUTHORIZED_WORKSPACE")


def _database_path(workspace: Workspace, active: ResolvedActiveDatabase) -> Path:
    try:
        root = workspace.root.resolve(strict=True)
        candidate = Path(active.path)
        if candidate.is_symlink():
            raise ValueError
        resolved = candidate.resolve(strict=True)
        resolved.relative_to(root)
        if not resolved.is_file():
            raise ValueError
        return resolved
    except (OSError, RuntimeError, TypeError, ValueError):
        raise RelationshipOperationError("WORKSPACE_PATH_ESCAPE") from None


def _open_database(path: Path, *, writable: bool) -> sqlite3.Connection:
    connection: sqlite3.Connection | None = None
    try:
        mode = "rw" if writable else "ro"
        connection = sqlite3.connect(
            f"{path.as_uri()}?mode={mode}",
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
        tables = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        if (
            version is None
            or isinstance(version[0], bool)
            or not isinstance(version[0], int)
            or version[0] < CURRENT_SCHEMA_VERSION
            or not _REQUIRED_TABLES.issubset(tables)
        ):
            raise RelationshipOperationError("CAPABILITY_DEGRADED")
        return connection
    except RelationshipOperationError:
        if connection is not None:
            connection.close()
        raise
    except Exception:
        if connection is not None:
            connection.close()
        raise RelationshipOperationError("CAPABILITY_DEGRADED") from None


def _now_us(dependencies: RelationshipOperationDependencies) -> int:
    try:
        value = dependencies.clock()
        if not isinstance(value, datetime) or value.tzinfo is None:
            raise ValueError
        delta = value.astimezone(timezone.utc) - _EPOCH
        result = (delta.days * 86_400 + delta.seconds) * 1_000_000 + delta.microseconds
    except (OverflowError, TypeError, ValueError):
        raise RelationshipOperationError("CAPABILITY_DEGRADED") from None
    if not -(2**63) <= result <= 2**63 - 1:
        raise RelationshipOperationError("CAPABILITY_DEGRADED")
    return result


def _datetime_from_us(value: object) -> datetime:
    if isinstance(value, bool) or not isinstance(value, int):
        raise RelationshipOperationError("CAPABILITY_DEGRADED")
    try:
        return _EPOCH + timedelta(microseconds=value)
    except (OverflowError, ValueError):
        raise RelationshipOperationError("CAPABILITY_DEGRADED") from None


def _operation_id(*parts: object) -> str:
    return "op_" + sha256_json(["daem0nmcp", "v7", "relationship-operation", *parts])


def _correlation(workspace_id: str, idempotency_key: str) -> str:
    return deterministic_id(
        "job",
        "memory-link-idempotency",
        workspace_id,
        idempotency_key,
    )


def _verified_payload(row: sqlite3.Row) -> dict[str, Any]:
    try:
        payload = json.loads(str(row["payload_json"]))
        if (
            not isinstance(payload, dict)
            or canonical_json_bytes(payload).decode("utf-8") != str(row["payload_json"])
            or sha256_json(payload) != str(row["payload_hash"])
        ):
            raise ValueError
        return payload
    except (KeyError, TypeError, ValueError, RecursionError):
        raise RelationshipOperationError("CAPABILITY_DEGRADED") from None


def _require_record(
    connection: sqlite3.Connection,
    workspace_id: str,
    record_id: str,
) -> None:
    rows = connection.execute(
        "SELECT deleted_at_us FROM memory_records "
        "WHERE workspace_id=? AND record_id=? LIMIT 2",
        (workspace_id, record_id),
    ).fetchall()
    if not rows or rows[0][0] is not None:
        raise RelationshipOperationError("NOT_FOUND")
    if len(rows) != 1:
        raise RelationshipOperationError("CAPABILITY_DEGRADED")


def _translate_error(error: Exception) -> RelationshipOperationError:
    if isinstance(error, RelationshipOperationError):
        return error
    if isinstance(error, EventStreamConflict):
        return RelationshipOperationError("EVENT_STREAM_CONFLICT")
    code = getattr(error, "code", None)
    if isinstance(code, str) and code in STABLE_ERROR_CODE_SET:
        return RelationshipOperationError(code)
    return RelationshipOperationError("CAPABILITY_DEGRADED")


async def _run_mutation(
    dependencies: RelationshipOperationDependencies,
    operation: Callable[[threading.Event], Any],
) -> Any:
    cancelled = threading.Event()
    worker = asyncio.create_task(
        dependencies.worker_pool.run(lambda: operation(cancelled))
    )
    try:
        return await asyncio.shield(worker)
    except asyncio.CancelledError as cancellation:
        cancelled.set()
        try:
            result = await await_task_terminal(worker)
        except (_WorkerCancelledError, BoundedWorkerBusyError):
            raise cancellation from None
        except Exception:
            raise cancellation from None
        return result
    except BoundedWorkerBusyError:
        raise RelationshipOperationError("TASK_REQUIRED") from None


async def _run_read(
    dependencies: RelationshipOperationDependencies,
    operation: Callable[[], Any],
) -> Any:
    worker = asyncio.create_task(dependencies.worker_pool.run(operation))
    try:
        return await asyncio.shield(worker)
    except asyncio.CancelledError as cancellation:
        while not worker.done():
            try:
                await asyncio.shield(worker)
            except asyncio.CancelledError:
                continue
            except Exception:
                break
        if worker.done():
            with suppress(Exception):
                worker.result()
        raise cancellation
    except BoundedWorkerBusyError:
        raise RelationshipOperationError("TASK_REQUIRED") from None


def _record_summary(row: sqlite3.Row) -> RecordSummary:
    try:
        content = row["content"]
        # A migrated v6 row keeps the host-absolute ``file_path`` v6 wrote; it
        # is legacy provenance, never emitted, so it must not deny the read.
        if not isinstance(content, str) or not content:
            raise ValueError
        tags = json.loads(str(row["tags_json"]))
        if not isinstance(tags, list):
            raise ValueError
        created_at = _datetime_from_us(row["created_at_us"])
        updated_at = _datetime_from_us(row["updated_at_us"])
        if created_at > updated_at:
            created_at = updated_at
        return RecordSummary.model_validate(
            {
                "record_id": str(row["record_id"]),
                "record_type": str(row["record_type"]),
                "excerpt": content[:4000],
                "tags": tags,
                "relative_file_path": None
                if row["file_path_relative"] is None
                else str(row["file_path_relative"]),
                "current_status": "archived" if bool(row["archived"]) else "current",
                "content_hash": str(row["content_hash"]),
                "created_at": created_at,
                "updated_at": updated_at,
            }
        )
    except RelationshipOperationError:
        raise
    except Exception:
        raise RelationshipOperationError("CAPABILITY_DEGRADED") from None


def _load_records(
    connection: sqlite3.Connection,
    workspace_id: str,
    record_ids: set[str],
) -> dict[str, sqlite3.Row]:
    if not record_ids:
        return {}
    if len(record_ids) > 501:
        raise RelationshipOperationError("TASK_REQUIRED")
    placeholders = ",".join("?" for _ in record_ids)
    rows = connection.execute(
        f"SELECT {_RECORD_COLUMNS} FROM memory_records "
        f"WHERE workspace_id=? AND record_id IN ({placeholders}) "
        "AND deleted_at_us IS NULL ORDER BY record_id",
        (workspace_id, *sorted(record_ids)),
    ).fetchall()
    indexed = {str(row["record_id"]): row for row in rows}
    if set(indexed) != record_ids or len(rows) != len(indexed):
        raise RelationshipOperationError("NOT_FOUND")
    return indexed


def _live_relationships(
    connection: sqlite3.Connection,
    workspace_id: str,
    now_us: int,
) -> list[sqlite3.Row]:
    invalid = connection.execute(
        "SELECT 1 FROM memory_relationship_versions AS relation "
        "LEFT JOIN memory_records AS source "
        "ON source.workspace_id=relation.workspace_id "
        "AND source.record_id=relation.source_record_id "
        "LEFT JOIN memory_records AS target "
        "ON target.workspace_id=relation.workspace_id "
        "AND target.record_id=relation.target_record_id "
        "LEFT JOIN memory_events AS event "
        "ON event.event_id=relation.asserted_by_event_id "
        "WHERE relation.workspace_id=? "
        "AND relation.relationship_type<>'legacy' "
        "AND relation.transaction_to_us IS NULL "
        "AND relation.valid_from_us<=? "
        "AND (relation.valid_to_us IS NULL OR relation.valid_to_us>?) "
        "AND (source.record_id IS NULL OR source.deleted_at_us IS NOT NULL "
        "OR target.record_id IS NULL OR target.deleted_at_us IS NOT NULL) "
        "OR (relation.workspace_id=? "
        "AND relation.relationship_type<>'legacy' "
        "AND relation.transaction_to_us IS NULL "
        "AND relation.valid_from_us<=? "
        "AND (relation.valid_to_us IS NULL OR relation.valid_to_us>?) "
        "AND (event.event_id IS NULL OR event.workspace_id<>relation.workspace_id "
        "OR event.stream_id<>relation.relationship_id "
        "OR event.stream_kind<>'relationship')) "
        "LIMIT 1",
        (workspace_id, now_us, now_us, workspace_id, now_us, now_us),
    ).fetchone()
    if invalid is not None:
        raise RelationshipOperationError("CAPABILITY_DEGRADED")
    rows = connection.execute(
        f"SELECT {_RELATIONSHIP_COLUMNS} "
        "FROM memory_relationship_versions AS relation "
        "JOIN memory_events AS event "
        "ON event.event_id=relation.asserted_by_event_id "
        "WHERE relation.workspace_id=? "
        "AND relation.relationship_type<>'legacy' "
        "AND relation.transaction_to_us IS NULL "
        "AND relation.valid_from_us<=? "
        "AND (relation.valid_to_us IS NULL OR relation.valid_to_us>?) "
        "ORDER BY relation.relationship_id LIMIT ?",
        (workspace_id, now_us, now_us, _MAX_TRAVERSAL_EDGES + 1),
    ).fetchall()
    if len(rows) > _MAX_TRAVERSAL_EDGES:
        raise RelationshipOperationError("TASK_REQUIRED")
    for row in rows:
        _verify_relationship_row(row)
    return rows


def _verify_relationship_row(row: sqlite3.Row) -> None:
    try:
        metadata = json.loads(str(row["metadata_json"]))
        payload = json.loads(str(row["source_payload_json"]))
        if not isinstance(metadata, dict) or not isinstance(payload, dict):
            raise ValueError
        relation = payload.get("relationship")
        expected = {
            "source_record_id": row["source_record_id"],
            "target_record_id": row["target_record_id"],
            "relationship_type": row["relationship_type"],
            "legacy_type": row["legacy_type"],
            "description": row["description"],
            "confidence": row["confidence"],
            "metadata": metadata,
            "valid_from_us": row["valid_from_us"],
            "valid_to_us": row["valid_to_us"],
        }
        if (
            canonical_json_bytes(metadata).decode("utf-8") != str(row["metadata_json"])
            or canonical_json_bytes(payload).decode("utf-8")
            != str(row["source_payload_json"])
            or sha256_json(payload) != str(row["source_payload_hash"])
            or relation != expected
            or sha256_json(expected) != str(row["content_hash"])
        ):
            raise ValueError
    except (KeyError, TypeError, ValueError, RecursionError):
        raise RelationshipOperationError("CAPABILITY_DEGRADED") from None


def _relationship_view(row: sqlite3.Row) -> RelationshipView:
    try:
        relationship_type = str(row["relationship_type"])
        if relationship_type not in _PUBLIC_RELATIONSHIP_TYPES:
            raise ValueError
        return RelationshipView.model_validate(
            {
                "relationship_id": str(row["relationship_id"]),
                "source_record_id": str(row["source_record_id"]),
                "target_record_id": str(row["target_record_id"]),
                "relationship_type": relationship_type,
                "description": None
                if row["description"] is None
                else str(row["description"]),
                "confidence": float(row["confidence"]),
            }
        )
    except Exception:
        raise RelationshipOperationError("CAPABILITY_DEGRADED") from None


def _relationship_evidence(row: sqlite3.Row) -> EvidenceRef:
    try:
        relationship_id = str(row["relationship_id"])
        return EvidenceRef(
            record_id=str(row["source_record_id"]),
            event_id=str(row["asserted_by_event_id"]),
            content_hash=str(row["content_hash"]),
            relation_path=[relationship_id],
            provider="graph",
        )
    except Exception:
        raise RelationshipOperationError("CAPABILITY_DEGRADED") from None


def _related_sync(
    dependencies: RelationshipOperationDependencies,
    workspace: Workspace,
    request: AdmittedRequest,
) -> MemoryRelatedData:
    try:
        with dependencies.storage_resolver.locked_active(workspace) as active:
            connection = _open_database(
                _database_path(workspace, active), writable=False
            )
            try:
                connection.execute("BEGIN")
                now_us = _now_us(dependencies)
                root_rows = _load_records(
                    connection,
                    workspace.workspace_id,
                    {request.record_id},
                )
                rows = _live_relationships(connection, workspace.workspace_id, now_us)
                allowed = (
                    None
                    if request.relationship_types is None
                    else set(request.relationship_types)
                )
                adjacency: dict[str, list[tuple[str, sqlite3.Row]]] = {}
                filtered: list[sqlite3.Row] = []
                for row in rows:
                    if allowed is not None and row["relationship_type"] not in allowed:
                        continue
                    filtered.append(row)
                    source = str(row["source_record_id"])
                    target = str(row["target_record_id"])
                    if request.direction in {"outgoing", "both"}:
                        adjacency.setdefault(source, []).append((target, row))
                    if request.direction in {"incoming", "both"}:
                        adjacency.setdefault(target, []).append((source, row))
                for entries in adjacency.values():
                    entries.sort(
                        key=lambda item: (str(item[1]["relationship_id"]), item[0])
                    )

                visited = {request.record_id}
                paths: dict[str, tuple[list[str], list[str]]] = {
                    request.record_id: ([request.record_id], [])
                }
                queue: deque[tuple[str, int]] = deque([(request.record_id, 0)])
                while queue and len(visited) < 501:
                    node, depth = queue.popleft()
                    if depth >= request.max_depth:
                        continue
                    for neighbor, row in adjacency.get(node, []):
                        if neighbor in visited:
                            continue
                        visited.add(neighbor)
                        parent_records, parent_relationships = paths[node]
                        paths[neighbor] = (
                            [*parent_records, neighbor],
                            [
                                *parent_relationships,
                                str(row["relationship_id"]),
                            ],
                        )
                        queue.append((neighbor, depth + 1))
                        if len(visited) >= 501:
                            break

                related_ids = visited - {request.record_id}
                record_rows = _load_records(
                    connection,
                    workspace.workspace_id,
                    visited,
                )
                relationship_rows = [
                    row
                    for row in filtered
                    if str(row["source_record_id"]) in visited
                    and str(row["target_record_id"]) in visited
                ][:1000]
                result = MemoryRelatedData(
                    root=_record_summary(root_rows[request.record_id]),
                    records=[
                        _record_summary(record_rows[record_id])
                        for record_id in sorted(related_ids)
                    ],
                    relationships=[
                        _relationship_view(row) for row in relationship_rows
                    ],
                    paths=[
                        RelationshipPath(
                            record_ids=paths[record_id][0],
                            relationship_ids=paths[record_id][1],
                        )
                        for record_id in sorted(
                            related_ids,
                            key=lambda item: (
                                len(paths[item][1]),
                                paths[item][0],
                                paths[item][1],
                            ),
                        )
                    ],
                )
                connection.rollback()
                return result
            except RelationshipOperationError:
                if connection.in_transaction:
                    connection.rollback()
                raise
            except Exception:
                if connection.in_transaction:
                    connection.rollback()
                raise RelationshipOperationError("CAPABILITY_DEGRADED") from None
            finally:
                if connection.in_transaction:
                    connection.rollback()
                connection.close()
    except RelationshipOperationError:
        raise
    except Exception as error:
        raise _translate_error(error) from None


def _chain_sync(
    dependencies: RelationshipOperationDependencies,
    workspace: Workspace,
    request: AdmittedRequest,
) -> MemoryChainTraceData:
    try:
        with dependencies.storage_resolver.locked_active(workspace) as active:
            connection = _open_database(
                _database_path(workspace, active), writable=False
            )
            try:
                connection.execute("BEGIN")
                _load_records(
                    connection,
                    workspace.workspace_id,
                    {request.start_record_id, request.end_record_id},
                )
                rows = _live_relationships(
                    connection,
                    workspace.workspace_id,
                    _now_us(dependencies),
                )
                adjacency: dict[str, list[sqlite3.Row]] = {}
                indexed: dict[str, sqlite3.Row] = {}
                for row in rows:
                    adjacency.setdefault(str(row["source_record_id"]), []).append(row)
                    indexed[str(row["relationship_id"])] = row
                for entries in adjacency.values():
                    entries.sort(
                        key=lambda row: (
                            str(row["target_record_id"]),
                            str(row["relationship_id"]),
                        )
                    )

                found: list[RelationshipPath] = []
                stack: list[tuple[str, list[str], list[str]]] = [
                    (request.start_record_id, [request.start_record_id], [])
                ]
                expanded_states = 0
                while stack and len(found) < 500:
                    expanded_states += 1
                    if expanded_states > _MAX_CHAIN_STATES:
                        raise RelationshipOperationError("TASK_REQUIRED")
                    node, record_path, relationship_path = stack.pop()
                    if node == request.end_record_id:
                        found.append(
                            RelationshipPath(
                                record_ids=record_path,
                                relationship_ids=relationship_path,
                            )
                        )
                        continue
                    if len(relationship_path) >= request.max_depth:
                        continue
                    candidates = adjacency.get(node, [])
                    for row in reversed(candidates):
                        target = str(row["target_record_id"])
                        if target in record_path:
                            continue
                        stack.append(
                            (
                                target,
                                [*record_path, target],
                                [
                                    *relationship_path,
                                    str(row["relationship_id"]),
                                ],
                            )
                        )
                        if len(stack) > _MAX_CHAIN_STATES:
                            raise RelationshipOperationError("TASK_REQUIRED")
                found.sort(
                    key=lambda path: (
                        len(path.relationship_ids),
                        path.record_ids,
                        path.relationship_ids,
                    )
                )
                evidence_ids: list[str] = []
                seen: set[str] = set()
                for path in found:
                    for relationship_id in path.relationship_ids:
                        if relationship_id not in seen:
                            seen.add(relationship_id)
                            evidence_ids.append(relationship_id)
                result = MemoryChainTraceData(
                    paths=found,
                    evidence_refs=[
                        _relationship_evidence(indexed[relationship_id])
                        for relationship_id in evidence_ids[:200]
                    ],
                )
                connection.rollback()
                return result
            except RelationshipOperationError:
                if connection.in_transaction:
                    connection.rollback()
                raise
            except Exception:
                if connection.in_transaction:
                    connection.rollback()
                raise RelationshipOperationError("CAPABILITY_DEGRADED") from None
            finally:
                if connection.in_transaction:
                    connection.rollback()
                connection.close()
    except RelationshipOperationError:
        raise
    except Exception as error:
        raise _translate_error(error) from None


def _graph_manifest(
    connection: sqlite3.Connection,
    workspace_id: str,
) -> ProjectionManifest:
    from ...retrieval.specialized_projection import SpecializedProjectionBuilder

    if not SpecializedProjectionBuilder(connection).active_is_current(
        workspace_id, "graph"
    ):
        raise RelationshipOperationError("CAPABILITY_DEGRADED")
    rows = connection.execute(
        "SELECT projection_name,generation,source_event_root_hash,"
        "activated_at_us,completed_at_us FROM projection_manifests "
        "WHERE workspace_id=? AND projection_name='graph' "
        "AND status='active' LIMIT 2",
        (workspace_id,),
    ).fetchall()
    if len(rows) != 1:
        raise RelationshipOperationError("CAPABILITY_DEGRADED")
    row = rows[0]
    built_at = row["activated_at_us"]
    if built_at is None:
        built_at = row["completed_at_us"]
    if built_at is None:
        raise RelationshipOperationError("CAPABILITY_DEGRADED")
    try:
        return ProjectionManifest.model_validate(
            {
                "projection": str(row["projection_name"]),
                "generation": int(row["generation"]),
                "built_at": _datetime_from_us(built_at),
                "source_root_hash": str(row["source_event_root_hash"]),
            }
        )
    except RelationshipOperationError:
        raise
    except Exception:
        raise RelationshipOperationError("CAPABILITY_DEGRADED") from None


def _record_ref_node_ids(
    connection: sqlite3.Connection,
    workspace_id: str,
    now_us: int,
) -> set[str]:
    rows = connection.execute(
        "SELECT subject_record_id,object_json FROM memory_fact_versions "
        "WHERE workspace_id=? AND object_kind='record_ref' "
        "AND transaction_to_us IS NULL AND valid_from_us<=? "
        "AND (valid_to_us IS NULL OR valid_to_us>?) "
        "ORDER BY fact_id LIMIT ?",
        (workspace_id, now_us, now_us, _MAX_TRAVERSAL_EDGES + 1),
    ).fetchall()
    if len(rows) > _MAX_TRAVERSAL_EDGES:
        raise RelationshipOperationError("TASK_REQUIRED")
    result: set[str] = set()
    try:
        for row in rows:
            target = json.loads(str(row["object_json"]))
            source = row["subject_record_id"]
            if not isinstance(source, str) or not isinstance(target, str):
                raise ValueError
            result.update((source, target))
    except (TypeError, ValueError, RecursionError):
        raise RelationshipOperationError("CAPABILITY_DEGRADED") from None
    return result


def _graph_record_rows(
    connection: sqlite3.Connection,
    workspace_id: str,
    request: AdmittedRequest,
    edge_rows: list[sqlite3.Row],
    now_us: int,
) -> list[sqlite3.Row]:
    public_types = (
        "decision",
        "pattern",
        "warning",
        "learning",
        "procedure",
        "observation",
    )
    type_placeholders = ",".join("?" for _ in public_types)
    parameters: list[object] = [workspace_id, *public_types]
    where = (
        "workspace_id=? AND deleted_at_us IS NULL AND file_path IS NULL "
        f"AND record_type IN ({type_placeholders})"
    )
    requested = None if request.record_ids is None else set(request.record_ids)
    if requested is not None:
        id_placeholders = ",".join("?" for _ in requested)
        where += f" AND record_id IN ({id_placeholders})"
        parameters.extend(sorted(requested))
    elif request.query is not None:
        where += " AND instr(lower(content),lower(?))>0"
        parameters.append(request.query)
    elif not request.include_orphans:
        candidates = {
            str(value)
            for row in edge_rows
            for value in (row["source_record_id"], row["target_record_id"])
        }
        candidates.update(_record_ref_node_ids(connection, workspace_id, now_us))
        if not candidates:
            return []
        id_placeholders = ",".join("?" for _ in candidates)
        where += f" AND record_id IN ({id_placeholders})"
        parameters.extend(sorted(candidates))
    query_limit = len(requested) if requested is not None else request.max_nodes + 1
    rows = connection.execute(
        f"SELECT {_RECORD_COLUMNS} FROM memory_records WHERE {where} "
        "ORDER BY record_id LIMIT ?",
        (*parameters, query_limit),
    ).fetchall()
    if requested is not None:
        available = {str(row["record_id"]) for row in rows}
        if not requested.issubset(available):
            raise RelationshipOperationError("NOT_FOUND")
    if request.query is not None and requested is not None:
        needle = request.query.casefold()
        rows = [row for row in rows if needle in str(row["content"]).casefold()]
    return rows[: request.max_nodes]


def _graph_snapshot(
    dependencies: RelationshipOperationDependencies,
    workspace: Workspace,
    request: AdmittedRequest,
) -> tuple[KnowledgeGraphData, dict[str, sqlite3.Row]]:
    try:
        with dependencies.storage_resolver.locked_active(workspace) as active:
            connection = _open_database(
                _database_path(workspace, active), writable=False
            )
            try:
                connection.execute("BEGIN")
                manifest = _graph_manifest(connection, workspace.workspace_id)
                now_us = _now_us(dependencies)
                relationship_rows = _live_relationships(
                    connection, workspace.workspace_id, now_us
                )
                record_rows = _graph_record_rows(
                    connection,
                    workspace.workspace_id,
                    request,
                    relationship_rows,
                    now_us,
                )
                selected = {str(row["record_id"]) for row in record_rows}
                edge_rows = [
                    row
                    for row in relationship_rows
                    if str(row["source_record_id"]) in selected
                    and str(row["target_record_id"]) in selected
                ][:2000]
                nodes: list[KnowledgeGraphNode] = []
                for row in record_rows:
                    summary = _record_summary(row)
                    label = " ".join(summary.excerpt.split())[:256]
                    if not label:
                        raise RelationshipOperationError("CAPABILITY_DEGRADED")
                    nodes.append(
                        KnowledgeGraphNode(
                            record=summary,
                            label=label,
                            node_type="record",
                        )
                    )
                result = KnowledgeGraphData(
                    nodes=nodes,
                    edges=[
                        KnowledgeGraphEdge(relationship=_relationship_view(row))
                        for row in edge_rows
                    ],
                    manifest=manifest,
                )
                indexed = {str(row["relationship_id"]): row for row in edge_rows}
                connection.rollback()
                return result, indexed
            except RelationshipOperationError:
                if connection.in_transaction:
                    connection.rollback()
                raise
            except Exception:
                if connection.in_transaction:
                    connection.rollback()
                raise RelationshipOperationError("CAPABILITY_DEGRADED") from None
            finally:
                if connection.in_transaction:
                    connection.rollback()
                connection.close()
    except RelationshipOperationError:
        raise
    except Exception as error:
        raise _translate_error(error) from None


def _graph_get_sync(
    dependencies: RelationshipOperationDependencies,
    workspace: Workspace,
    request: AdmittedRequest,
) -> KnowledgeGraphData:
    return _graph_snapshot(dependencies, workspace, request)[0]


def _mermaid_label(value: str) -> str:
    compact = " ".join(value.split())[:80]
    encoded: list[str] = []
    for character in compact:
        if (
            "a" <= character <= "z"
            or "A" <= character <= "Z"
            or "0" <= character <= "9"
            or character in {" ", ".", "_", "-"}
        ):
            encoded.append(character)
        else:
            encoded.append(f"&#x{ord(character):X};")
    return "".join(encoded) or "Record"


def _graph_render_sync(
    dependencies: RelationshipOperationDependencies,
    workspace: Workspace,
    request: AdmittedRequest,
) -> KnowledgeGraphRenderData:
    graph, indexed = _graph_snapshot(dependencies, workspace, request)
    aliases = {
        node.record.record_id: f"n{index:04d}"
        for index, node in enumerate(graph.nodes, start=1)
    }
    lines = ["flowchart TD"]
    for node in graph.nodes:
        lines.append(
            f'    {aliases[node.record.record_id]}["{_mermaid_label(node.label)}"]'
        )
    for edge in graph.edges:
        relation = edge.relationship
        lines.append(
            f"    {aliases[relation.source_record_id]} "
            f"-->|{relation.relationship_type}| "
            f"{aliases[relation.target_record_id]}"
        )
    text = "\n".join(lines)
    if len(text) > 500_000:
        raise RelationshipOperationError("TASK_REQUIRED")
    return KnowledgeGraphRenderData(
        text=text,
        evidence_refs=[
            _relationship_evidence(indexed[edge.relationship.relationship_id])
            for edge in graph.edges[:200]
        ],
    )


def _link_sync(
    dependencies: RelationshipOperationDependencies,
    workspace: Workspace,
    request: AdmittedRequest,
    cancelled: threading.Event,
) -> MutationReceipt:
    recorded_at_us = _now_us(dependencies)
    if cancelled.is_set():
        raise _WorkerCancelledError()
    request_state = {
        "source_record_id": request.source_record_id,
        "target_record_id": request.target_record_id,
        "relationship_type": request.relationship_type,
        "description": request.description,
        "confidence": request.confidence,
    }
    request_hash = sha256_json(request_state)
    relationship_id = deterministic_id(
        "rel",
        "v7-memory-link",
        workspace.workspace_id,
        request.idempotency_key,
    )
    correlation = _correlation(
        workspace.workspace_id,
        request.idempotency_key,
    )
    try:
        with dependencies.storage_resolver.locked_active(workspace) as active:
            connection = _open_database(
                _database_path(workspace, active), writable=True
            )
            try:
                connection.execute("BEGIN IMMEDIATE")
                if cancelled.is_set():
                    raise _WorkerCancelledError()
                existing = connection.execute(
                    "SELECT event_id,stream_id,payload_json,payload_hash "
                    "FROM memory_events WHERE workspace_id=? "
                    "AND event_type='relationship.created' "
                    "AND correlation_id=? LIMIT 2",
                    (workspace.workspace_id, correlation),
                ).fetchall()
                changed = False
                if existing:
                    if (
                        len(existing) != 1
                        or str(existing[0]["stream_id"]) != relationship_id
                    ):
                        raise RelationshipOperationError("CAPABILITY_DEGRADED")
                    payload = _verified_payload(existing[0])
                    if payload.get("idempotency_request_hash") != request_hash:
                        raise RelationshipOperationError("IDEMPOTENCY_CONFLICT")
                    event_id = str(existing[0]["event_id"])
                else:
                    _require_record(
                        connection,
                        workspace.workspace_id,
                        request.source_record_id,
                    )
                    _require_record(
                        connection,
                        workspace.workspace_id,
                        request.target_record_id,
                    )
                    duplicate = connection.execute(
                        "SELECT relationship_id FROM memory_relationship_versions "
                        "WHERE workspace_id=? AND source_record_id=? "
                        "AND target_record_id=? AND relationship_type=? "
                        "AND transaction_to_us IS NULL "
                        "AND valid_from_us<=? "
                        "AND (valid_to_us IS NULL OR valid_to_us>?) LIMIT 1",
                        (
                            workspace.workspace_id,
                            request.source_record_id,
                            request.target_record_id,
                            request.relationship_type,
                            recorded_at_us,
                            recorded_at_us,
                        ),
                    ).fetchone()
                    if duplicate is not None:
                        raise RelationshipOperationError("CONFLICT")
                    if cancelled.is_set():
                        raise _WorkerCancelledError()
                    event = EventStore(
                        connection, assume_transaction=True
                    ).append_and_project(
                        EventCommand(
                            workspace_id=workspace.workspace_id,
                            stream_id=relationship_id,
                            stream_kind="relationship",
                            event_type="relationship.created",
                            occurred_at_us=recorded_at_us,
                            recorded_at_us=recorded_at_us,
                            actor_type="client",
                            correlation_id=correlation,
                            expected_stream_version=1,
                            payload={
                                "relationship": {
                                    **request_state,
                                    "legacy_type": None,
                                    "metadata": {},
                                    "valid_from_us": recorded_at_us,
                                    "valid_to_us": None,
                                },
                                "idempotency_request_hash": request_hash,
                            },
                        )
                    )
                    event_id = event.event_id
                    changed = True
                if cancelled.is_set():
                    raise _WorkerCancelledError()
                connection.commit()
                return MutationReceipt(
                    operation_id=_operation_id(
                        workspace.workspace_id,
                        "memory_link",
                        request.idempotency_key,
                    ),
                    affected_ids=[
                        relationship_id,
                        request.source_record_id,
                        request.target_record_id,
                    ],
                    event_ids=[event_id],
                    counts={"relationships": 1, "changed": int(changed)},
                    idempotent_replay=not changed,
                )
            except (
                EventStreamConflict,
                RelationshipOperationError,
                _WorkerCancelledError,
            ):
                if connection.in_transaction:
                    connection.rollback()
                raise
            except Exception:
                if connection.in_transaction:
                    connection.rollback()
                raise RelationshipOperationError("CAPABILITY_DEGRADED") from None
            finally:
                if connection.in_transaction:
                    connection.rollback()
                connection.close()
    except _WorkerCancelledError:
        raise
    except Exception as error:
        raise _translate_error(error) from None


def _unlink_sync(
    dependencies: RelationshipOperationDependencies,
    workspace: Workspace,
    request: AdmittedRequest,
    cancelled: threading.Event,
) -> MutationReceipt:
    recorded_at_us = _now_us(dependencies)
    if cancelled.is_set():
        raise _WorkerCancelledError()
    try:
        with dependencies.storage_resolver.locked_active(workspace) as active:
            connection = _open_database(
                _database_path(workspace, active), writable=True
            )
            try:
                connection.execute("BEGIN IMMEDIATE")
                if cancelled.is_set():
                    raise _WorkerCancelledError()
                rows = connection.execute(
                    "SELECT relationship_id,version,source_record_id,"
                    "target_record_id,relationship_type,legacy_type,description,"
                    "confidence,metadata_json,valid_from_us,valid_to_us,"
                    "transaction_from_us,asserted_by_event_id "
                    "FROM memory_relationship_versions "
                    "WHERE workspace_id=? AND relationship_id=? "
                    "AND transaction_to_us IS NULL LIMIT 2",
                    (workspace.workspace_id, request.relationship_id),
                ).fetchall()
                if not rows:
                    raise RelationshipOperationError("NOT_FOUND")
                if len(rows) != 1:
                    raise RelationshipOperationError("CAPABILITY_DEGRADED")
                row = rows[0]
                changed = row["valid_to_us"] is None
                if changed:
                    try:
                        metadata = json.loads(str(row["metadata_json"]))
                    except (TypeError, ValueError, RecursionError):
                        raise RelationshipOperationError(
                            "CAPABILITY_DEGRADED"
                        ) from None
                    if not isinstance(metadata, dict):
                        raise RelationshipOperationError("CAPABILITY_DEGRADED")
                    valid_from_us = int(row["valid_from_us"])
                    event_at_us = max(
                        recorded_at_us, int(row["transaction_from_us"]) + 1
                    )
                    valid_to_us = max(event_at_us, valid_from_us + 1)
                    if cancelled.is_set():
                        raise _WorkerCancelledError()
                    event = EventStore(
                        connection, assume_transaction=True
                    ).append_and_project(
                        EventCommand(
                            workspace_id=workspace.workspace_id,
                            stream_id=request.relationship_id,
                            stream_kind="relationship",
                            event_type="relationship.removed",
                            occurred_at_us=event_at_us,
                            recorded_at_us=event_at_us,
                            actor_type="client",
                            correlation_id=deterministic_id(
                                "job",
                                "memory-unlink",
                                workspace.workspace_id,
                                request.relationship_id,
                            ),
                            expected_stream_version=int(row["version"]) + 1,
                            payload={
                                "relationship": {
                                    "source_record_id": row["source_record_id"],
                                    "target_record_id": row["target_record_id"],
                                    "relationship_type": row["relationship_type"],
                                    "legacy_type": row["legacy_type"],
                                    "description": row["description"],
                                    "confidence": row["confidence"],
                                    "metadata": metadata,
                                    "valid_from_us": valid_from_us,
                                    "valid_to_us": valid_to_us,
                                }
                            },
                        )
                    )
                    event_id = event.event_id
                else:
                    event_id = str(row["asserted_by_event_id"])
                if cancelled.is_set():
                    raise _WorkerCancelledError()
                connection.commit()
                return MutationReceipt(
                    operation_id=_operation_id(
                        workspace.workspace_id,
                        "memory_unlink",
                        request.relationship_id,
                    ),
                    affected_ids=[
                        request.relationship_id,
                        str(row["source_record_id"]),
                        str(row["target_record_id"]),
                    ],
                    event_ids=[event_id],
                    counts={"relationships": 1, "changed": int(changed)},
                    idempotent_replay=not changed,
                )
            except (
                EventStreamConflict,
                RelationshipOperationError,
                _WorkerCancelledError,
            ):
                if connection.in_transaction:
                    connection.rollback()
                raise
            except Exception:
                if connection.in_transaction:
                    connection.rollback()
                raise RelationshipOperationError("CAPABILITY_DEGRADED") from None
            finally:
                if connection.in_transaction:
                    connection.rollback()
                connection.close()
    except _WorkerCancelledError:
        raise
    except Exception as error:
        raise _translate_error(error) from None


def build_relationship_operations(
    dependencies: RelationshipOperationDependencies,
) -> Mapping[str, Callable[..., Any]]:
    """Return the exact immutable canonical relationship registry."""

    if not isinstance(dependencies, RelationshipOperationDependencies):
        raise TypeError("dependencies must be RelationshipOperationDependencies")

    async def memory_link(
        *, workspace: Workspace, request: AdmittedRequest
    ) -> MutationReceipt:
        _authorize(workspace, request, "memory_link")
        return await _run_mutation(
            dependencies,
            lambda cancelled: _link_sync(
                dependencies,
                workspace,
                request,
                cancelled,
            ),
        )

    async def memory_unlink(
        *, workspace: Workspace, request: AdmittedRequest
    ) -> MutationReceipt:
        _authorize(workspace, request, "memory_unlink")
        return await _run_mutation(
            dependencies,
            lambda cancelled: _unlink_sync(
                dependencies,
                workspace,
                request,
                cancelled,
            ),
        )

    async def memory_related(
        *, workspace: Workspace, request: AdmittedRequest
    ) -> MemoryRelatedData:
        _authorize(workspace, request, "memory_related")
        return await _run_read(
            dependencies,
            lambda: _related_sync(dependencies, workspace, request),
        )

    async def memory_chain_trace(
        *, workspace: Workspace, request: AdmittedRequest
    ) -> MemoryChainTraceData:
        _authorize(workspace, request, "memory_chain_trace")
        return await _run_read(
            dependencies,
            lambda: _chain_sync(dependencies, workspace, request),
        )

    async def knowledge_graph_get(
        *, workspace: Workspace, request: AdmittedRequest
    ) -> KnowledgeGraphData:
        _authorize(workspace, request, "knowledge_graph_get")
        return await _run_read(
            dependencies,
            lambda: _graph_get_sync(dependencies, workspace, request),
        )

    async def knowledge_graph_render(
        *, workspace: Workspace, request: AdmittedRequest
    ) -> KnowledgeGraphRenderData:
        _authorize(workspace, request, "knowledge_graph_render")
        return await _run_read(
            dependencies,
            lambda: _graph_render_sync(dependencies, workspace, request),
        )

    return MappingProxyType(
        {
            "knowledge_graph_get": knowledge_graph_get,
            "knowledge_graph_render": knowledge_graph_render,
            "memory_chain_trace": memory_chain_trace,
            "memory_link": memory_link,
            "memory_related": memory_related,
            "memory_unlink": memory_unlink,
        }
    )


__all__ = [
    "RelationshipOperationDependencies",
    "RelationshipOperationError",
    "build_relationship_operations",
]
