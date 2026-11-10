"""Production adapters for complete v7 entity/community graph rebuilds."""

from __future__ import annotations

import asyncio
import sqlite3
import threading
from collections.abc import Callable, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any

from ...bounded_workers import BoundedWorkerBusyError, BoundedWorkerPool
from ...event_store import canonical_json_bytes, deterministic_id, sha256_json
from ...graph_projection import (
    GraphProjectionBuilder,
    GraphProjectionBuildError,
    GraphProjectionBuildResult,
)
from ...workspace import Workspace
from .application import AdmittedRequest
from .discovery_operations import (
    DiscoveryOperationError,
    _authorize,
    _database_path,
    _datetime_from_us,
    _open_writable_database,
)
from .errors import STABLE_ERROR_CODE_SET
from .runtime_protocols import ActiveStorageResolver, WorkerPool
from .runtime_services import WorkspaceStorageResolver
from .tools import (
    CommunityRebuildData,
    CommunitySummary,
    EntityBackfillData,
    ProjectionManifest,
)


class GraphOperationError(RuntimeError):
    """Stable, path-free graph mutation failure."""

    def __init__(self, code: str) -> None:
        if code not in STABLE_ERROR_CODE_SET:
            code = "CAPABILITY_DEGRADED"
        self.code = code
        super().__init__(code)


def _default_pool() -> BoundedWorkerPool:
    return BoundedWorkerPool(max_workers=2, thread_name_prefix="daem0nmcp-v7-graph")


@dataclass(frozen=True, slots=True)
class GraphOperationDependencies:
    storage_resolver: ActiveStorageResolver = field(
        default_factory=WorkspaceStorageResolver
    )
    capability_statuses: Mapping[str, str] = field(default_factory=dict)
    worker_pool: WorkerPool = field(default_factory=_default_pool)
    builder_factory: Callable[[sqlite3.Connection], GraphProjectionBuilder] = (
        GraphProjectionBuilder
    )

    def __post_init__(self) -> None:
        if not callable(getattr(self.storage_resolver, "locked_active", None)):
            raise TypeError("storage_resolver must provide locked_active")
        if not callable(getattr(self.worker_pool, "run", None)) or not callable(
            getattr(self.worker_pool, "shutdown", None)
        ):
            raise TypeError("worker_pool must provide run and shutdown")
        if not callable(self.builder_factory):
            raise TypeError("builder_factory must be callable")
        object.__setattr__(
            self,
            "capability_statuses",
            MappingProxyType(dict(self.capability_statuses)),
        )

    def close(self) -> None:
        self.worker_pool.shutdown()


def _translate(error: Exception) -> GraphOperationError:
    if isinstance(error, GraphOperationError):
        return error
    if isinstance(error, DiscoveryOperationError):
        return GraphOperationError(error.code)
    code = getattr(error, "code", None)
    if isinstance(code, str) and code in STABLE_ERROR_CODE_SET:
        return GraphOperationError(code)
    return GraphOperationError("CAPABILITY_DEGRADED")


def _manifest(result: GraphProjectionBuildResult) -> ProjectionManifest:
    return ProjectionManifest(
        projection="graph",
        generation=result.generation,
        source_root_hash=result.source_event_root_hash,
        built_at=_datetime_from_us(result.built_at_us),
    )


def _entity_response(result: GraphProjectionBuildResult) -> EntityBackfillData:
    return EntityBackfillData(
        manifest=_manifest(result),
        scanned=result.scanned,
        extracted=result.extracted,
        skipped=result.skipped,
    )


def _community_response(result: GraphProjectionBuildResult) -> CommunityRebuildData:
    communities = [
        CommunitySummary(
            community_id=result.community_ids_by_source_key[seed.source_key],
            label=seed.label,
            level=seed.level,
            member_count=len(seed.member_record_ids),
            parent_community_id=None,
            manifest_generation=result.generation,
        )
        for seed in result.communities
    ]
    return CommunityRebuildData(
        manifest=_manifest(result),
        communities=communities[:500],
        modularity=result.modularity,
    )


def _load_receipt(
    connection: sqlite3.Connection,
    *,
    workspace_id: str,
    job_type: str,
    idempotency_key: str,
    payload_hash: str,
    response_model: type[Any],
) -> Any | None:
    row = connection.execute(
        "SELECT payload_hash,status,result_json FROM background_jobs "
        "WHERE workspace_id=? AND job_type=? AND idempotency_key=? LIMIT 2",
        (workspace_id, job_type, idempotency_key),
    ).fetchall()
    if not row:
        return None
    if len(row) != 1 or str(row[0][0]) != payload_hash:
        raise GraphOperationError("IDEMPOTENCY_CONFLICT")
    if str(row[0][1]) != "succeeded" or not isinstance(row[0][2], str):
        raise GraphOperationError("CONFLICT")
    try:
        return response_model.model_validate_json(str(row[0][2]))
    except Exception:
        raise GraphOperationError("CAPABILITY_DEGRADED") from None


def _insert_receipt(
    connection: sqlite3.Connection,
    *,
    workspace_id: str,
    job_type: str,
    idempotency_key: str,
    payload_text: str,
    payload_hash: str,
    response: Any,
    recorded_at_us: int,
) -> None:
    result_text = canonical_json_bytes(response.model_dump(mode="json")).decode("utf-8")
    connection.execute(
        "INSERT INTO background_jobs("
        "job_id,workspace_id,job_type,idempotency_key,payload_json,payload_hash,"
        "status,priority,attempts,max_attempts,available_at_us,result_json,"
        "created_at_us,updated_at_us,finished_at_us) "
        "VALUES (?,?,?,?,?,?,'succeeded',0,1,1,?,?,?,?,?)",
        (
            deterministic_id(
                "job",
                "graph-operation-receipt",
                workspace_id,
                job_type,
                idempotency_key,
            ),
            workspace_id,
            job_type,
            idempotency_key,
            payload_text,
            payload_hash,
            recorded_at_us,
            result_text,
            recorded_at_us,
            recorded_at_us,
            recorded_at_us,
        ),
    )


def _rebuild_sync(
    dependencies: GraphOperationDependencies,
    workspace: Workspace,
    *,
    min_community_size: int,
    resolution: float,
    force: bool,
    job_type: str,
    idempotency_key: str,
    receipt_payload: Mapping[str, object],
    response_model: type[Any],
    response_builder: Callable[[GraphProjectionBuildResult], Any],
    cancelled: threading.Event,
) -> Any:
    if dependencies.capability_statuses.get("graph") != "ready":
        raise GraphOperationError("CAPABILITY_DEGRADED")
    try:
        with dependencies.storage_resolver.locked_active(workspace) as active:
            active_path = _database_path(workspace, active)
            active_pointer = active.pointer_bytes
            active_generation = active.generation
            connection = _open_writable_database(active_path)

        try:
            payload_text = canonical_json_bytes(receipt_payload).decode("utf-8")
            payload_hash = sha256_json(receipt_payload)
            existing = _load_receipt(
                connection,
                workspace_id=workspace.workspace_id,
                job_type=job_type,
                idempotency_key=idempotency_key,
                payload_hash=payload_hash,
                response_model=response_model,
            )
        except Exception:
            connection.close()
            raise
        if existing is not None:
            connection.close()
            return existing

        @contextmanager
        def publication_guard():
            with dependencies.storage_resolver.locked_active(workspace) as current:
                if (
                    _database_path(workspace, current) != active_path
                    or current.pointer_bytes != active_pointer
                    or current.generation != active_generation
                ):
                    raise GraphProjectionBuildError("PROJECTION_BUILD_SUPERSEDED")
                yield

        try:
            builder = dependencies.builder_factory(connection)
            last_error: GraphProjectionBuildError | None = None
            committed_response: Any | None = None

            def store_receipt(result: GraphProjectionBuildResult) -> None:
                nonlocal committed_response
                committed_response = response_builder(result)
                _insert_receipt(
                    connection,
                    workspace_id=workspace.workspace_id,
                    job_type=job_type,
                    idempotency_key=idempotency_key,
                    payload_text=payload_text,
                    payload_hash=payload_hash,
                    response=committed_response,
                    recorded_at_us=result.built_at_us,
                )

            for _attempt in range(3):
                if cancelled.is_set():
                    raise GraphOperationError("CANCELLED")
                try:
                    result = builder.rebuild(
                        workspace.workspace_id,
                        min_community_size=min_community_size,
                        resolution=resolution,
                        force=force,
                        cancelled=cancelled,
                        publication_guard=publication_guard,
                        before_commit=store_receipt,
                    )
                    if committed_response is not None:
                        return committed_response
                    response = response_builder(result)
                    with publication_guard():
                        connection.execute("BEGIN IMMEDIATE")
                        try:
                            replay = _load_receipt(
                                connection,
                                workspace_id=workspace.workspace_id,
                                job_type=job_type,
                                idempotency_key=idempotency_key,
                                payload_hash=payload_hash,
                                response_model=response_model,
                            )
                            if replay is not None:
                                connection.rollback()
                                return replay
                            _insert_receipt(
                                connection,
                                workspace_id=workspace.workspace_id,
                                job_type=job_type,
                                idempotency_key=idempotency_key,
                                payload_text=payload_text,
                                payload_hash=payload_hash,
                                response=response,
                                recorded_at_us=result.built_at_us,
                            )
                            connection.commit()
                            return response
                        except Exception:
                            if connection.in_transaction:
                                connection.rollback()
                            raise
                except GraphProjectionBuildError as error:
                    if error.code != "PROJECTION_BUILD_SUPERSEDED":
                        raise
                    last_error = error
                    replay = _load_receipt(
                        connection,
                        workspace_id=workspace.workspace_id,
                        job_type=job_type,
                        idempotency_key=idempotency_key,
                        payload_hash=payload_hash,
                        response_model=response_model,
                    )
                    if replay is not None:
                        return replay
            if last_error is not None:
                raise GraphOperationError("CONFLICT")
            raise GraphOperationError("CAPABILITY_DEGRADED")
        finally:
            if connection.in_transaction:
                connection.rollback()
            connection.close()
    except Exception as error:
        raise _translate(error) from None


async def _run_mutation(
    dependencies: GraphOperationDependencies,
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
        while not worker.done():
            try:
                await asyncio.shield(worker)
            except asyncio.CancelledError:
                continue
            except Exception:
                break
        if worker.done():
            try:
                return worker.result()
            except Exception:
                raise cancellation from None
        raise cancellation
    except BoundedWorkerBusyError:
        raise GraphOperationError("TASK_REQUIRED") from None


def build_graph_operations(
    dependencies: GraphOperationDependencies,
) -> Mapping[str, Callable[..., Any]]:
    if not isinstance(dependencies, GraphOperationDependencies):
        raise TypeError("dependencies must be GraphOperationDependencies")

    async def entity_backfill(
        *, workspace: Workspace, request: AdmittedRequest
    ) -> EntityBackfillData:
        _authorize(workspace, request, "entity_backfill")
        return await _run_mutation(
            dependencies,
            lambda cancelled: _rebuild_sync(
                dependencies,
                workspace,
                min_community_size=2,
                resolution=1.0,
                force=request.force,
                job_type="graph.entity_backfill",
                idempotency_key=request.idempotency_key,
                receipt_payload={
                    "force": request.force,
                    "operation": "entity_backfill",
                    "workspace_id": workspace.workspace_id,
                },
                response_model=EntityBackfillData,
                response_builder=_entity_response,
                cancelled=cancelled,
            ),
        )

    async def community_rebuild(
        *, workspace: Workspace, request: AdmittedRequest
    ) -> CommunityRebuildData:
        _authorize(workspace, request, "community_rebuild")
        return await _run_mutation(
            dependencies,
            lambda cancelled: _rebuild_sync(
                dependencies,
                workspace,
                min_community_size=request.min_community_size,
                resolution=request.resolution,
                force=False,
                job_type="graph.community_rebuild",
                idempotency_key=request.idempotency_key,
                receipt_payload={
                    "min_community_size": request.min_community_size,
                    "operation": "community_rebuild",
                    "resolution": request.resolution,
                    "workspace_id": workspace.workspace_id,
                },
                response_model=CommunityRebuildData,
                response_builder=_community_response,
                cancelled=cancelled,
            ),
        )

    return MappingProxyType(
        {
            "community_rebuild": community_rebuild,
            "entity_backfill": entity_backfill,
        }
    )


__all__ = [
    "GraphOperationDependencies",
    "GraphOperationError",
    "build_graph_operations",
]
