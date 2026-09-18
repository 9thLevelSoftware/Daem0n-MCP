"""Production assembly and durable projection work for v7 retrieval."""

from __future__ import annotations

import asyncio
import logging
import math
import os
import re
import sqlite3
import threading
import time
import warnings
from collections.abc import Mapping
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from ..bounded_workers import BoundedWorkerBusyError, BoundedWorkerPool
from .composer import EvidenceComposer
from .jobs import ProjectionJobRun, ProjectionJobRunner
from .planner import RetrievalPlanner
from .providers import DenseProvider, LexicalProvider
from .repository import SQLiteRetrievalRepository, sqlite_read_connection_factory
from .rerank import EmbeddingSimilarityReranker
from .service import RetrievalService
from .specialized import (
    GraphProvider,
    OutcomeProvider,
    ProcedureProvider,
    TemporalProvider,
)

_TOKEN = re.compile(r"\w+|[^\w\s]", re.UNICODE)
_WORKSPACE_ID = re.compile(r"^ws_[0-9a-f]{24}$")
_RUNTIME_JOB_WORKERS = BoundedWorkerPool(
    max_workers=1,
    thread_name_prefix="daem0nmcp-projection-runtime",
)
_RUNTIME_CORE_JOB_WORKERS = BoundedWorkerPool(
    max_workers=1,
    thread_name_prefix="daem0nmcp-projection-core",
)
_RUNTIME_FILTER_WORKERS = BoundedWorkerPool(
    max_workers=2,
    thread_name_prefix="daem0nmcp-retrieval-filter",
)


@dataclass(slots=True)
class _ProjectionDrainState:
    wake: asyncio.Event
    task: asyncio.Task[None] | None = None
    stopping: bool = False
    continuous: bool = False
    cancellation: threading.Event = field(default_factory=threading.Event)


_PROJECTION_DRAIN_STATES: dict[Path, _ProjectionDrainState] = {}
logger = logging.getLogger(__name__)

_PROJECTION_RETRY_INITIAL_SECONDS = 0.05
_PROJECTION_RETRY_MAX_SECONDS = 5.0
_PROJECTION_IDLE_POLL_SECONDS = 1.0


class _ProjectionBuilderRegistry(dict[str, Any]):
    """Projection callables plus the slice-owned resources behind them."""

    def __init__(self) -> None:
        super().__init__()
        self._resources: list[object] = []
        self.dense_builder: object | None = None

    def own(self, resource: object) -> None:
        self._resources.append(resource)

    def close(self) -> None:
        for resource in reversed(self._resources):
            close = getattr(resource, "close", None)
            if callable(close):
                with suppress(Exception):
                    close()
        self._resources.clear()


def _effective_capability_statuses(
    statuses: Mapping[str, str] | None,
) -> Mapping[str, str]:
    if statuses is not None:
        return statuses
    from ..capabilities import CapabilityRegistry

    return {
        name: str(value["status"]) for name, value in CapabilityRegistry().all().items()
    }


def _profile_ready(statuses: Mapping[str, str], name: str) -> bool:
    return statuses.get(name) == "ready"


def _dense_gc_schema_available(connection: sqlite3.Connection) -> bool:
    return (
        connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' "
            "AND name='dense_generation_gc_jobs'"
        ).fetchone()
        is not None
    )


class CoreTokenizer:
    """Dependency-free deterministic tokenizer used by the base profile."""

    def count_tokens(self, text: str) -> int:
        if not isinstance(text, str):
            raise ValueError("text must be a string")
        return len(_TOKEN.findall(text))


def warn_legacy_hybrid_weight() -> None:
    """Mark the retained v6-only hybrid scoring setting as deprecated."""

    warnings.warn(
        "hybrid_vector_weight is a v6-only compatibility setting and is "
        "ignored by v7 retrieval",
        DeprecationWarning,
        stacklevel=2,
    )


class ConfiguredEmbeddingEncoder:
    """Lazy SentenceTransformer adapter with explicit v7 prefixes/dimension."""

    def __init__(
        self,
        *,
        model_id: str,
        dimension: int,
        prefix: str,
        backend: str,
    ) -> None:
        if not isinstance(model_id, str) or not model_id.strip():
            raise ValueError("model_id must be non-empty")
        if (
            isinstance(dimension, bool)
            or not isinstance(dimension, int)
            or dimension < 1
        ):
            raise ValueError("dimension must be positive")
        if not isinstance(prefix, str) or len(prefix) > 256:
            raise ValueError("embedding prefix is invalid")
        if not isinstance(backend, str) or not backend.strip():
            raise ValueError("embedding backend is invalid")
        self.model_id = model_id
        self.dimension = dimension
        self.prefix = prefix
        self.backend = backend
        self._model: object | None = None
        self.artifact_fingerprint: str | None = None
        self._lock = threading.Lock()

    def _load_model_locked(self) -> object:
        model = self._model
        if model is not None:
            return model
        try:
            if self.backend == "onnx":
                from .onnx_encoder import load_pooled_onnx_model

                model = load_pooled_onnx_model(self.model_id, self.dimension)
            if model is None:
                from huggingface_hub import snapshot_download
                from sentence_transformers import SentenceTransformer

                from .onnx_encoder import fingerprint_model_directory

                configured = Path(self.model_id)
                model_root = (
                    configured.resolve(strict=True)
                    if configured.is_dir()
                    else Path(snapshot_download(self.model_id)).resolve(strict=True)
                )
                before = fingerprint_model_directory(model_root)
                model = SentenceTransformer(
                    str(model_root),
                    truncate_dim=self.dimension,
                    backend=self.backend,
                    model_kwargs={"file_name": "onnx/model_quantized.onnx"}
                    if self.backend == "onnx"
                    else {},
                )
                after = fingerprint_model_directory(model_root)
                if after != before:
                    raise RuntimeError("embedding artifacts changed during load")
                self.artifact_fingerprint = after
            else:
                fingerprint = getattr(model, "artifact_fingerprint", None)
                if not isinstance(fingerprint, str) or len(fingerprint) != 64:
                    raise RuntimeError("embedding artifact identity is unavailable")
                self.artifact_fingerprint = fingerprint
        except Exception as exc:
            raise RuntimeError("DENSE_ENCODER_UNAVAILABLE") from exc
        self._model = model
        return model

    def prepare_artifact_identity(self) -> str:
        """Load the configured model and return its exact artifact fingerprint."""

        with self._lock:
            self._load_model_locked()
            fingerprint = self.artifact_fingerprint
        if not isinstance(fingerprint, str) or len(fingerprint) != 64:
            raise RuntimeError("DENSE_ENCODER_UNAVAILABLE")
        return fingerprint

    def encode_many(self, texts: list[str]) -> list[list[float]]:
        if not texts or any(not isinstance(text, str) for text in texts):
            raise ValueError("embedding texts must be a non-empty string list")
        with self._lock:
            model = self._load_model_locked()
            encode_many = getattr(model, "encode_many", None)
            encode = getattr(model, "encode", None)
            if callable(encode_many):
                raw = encode_many(
                    [f"{self.prefix}{text}" for text in texts],
                    convert_to_numpy=True,
                )
            elif callable(encode):
                raw = encode(
                    [f"{self.prefix}{text}" for text in texts],
                    convert_to_numpy=True,
                )
            else:
                raise RuntimeError("DENSE_ENCODER_UNAVAILABLE")
        if isinstance(raw, (str, bytes, Mapping)) or len(raw) != len(texts):
            raise RuntimeError("DENSE_ENCODER_INVALID")
        results: list[list[float]] = []
        for vector in raw:
            if isinstance(vector, (str, bytes, Mapping)):
                raise RuntimeError("DENSE_ENCODER_INVALID")
            try:
                values = [float(value) for value in vector]
            except (OverflowError, TypeError, ValueError) as exc:
                raise RuntimeError("DENSE_ENCODER_INVALID") from exc
            if len(values) != self.dimension or not all(map(math.isfinite, values)):
                raise RuntimeError("DENSE_ENCODER_INVALID")
            results.append(values)
        return results

    def encode(self, text: str) -> list[float]:
        if not isinstance(text, str):
            raise ValueError("embedding text must be a string")
        return self.encode_many([text])[0]

    def close(self) -> None:
        """Close the lazily created model when its backend exposes cleanup."""

        with self._lock:
            model = self._model
            self._model = None
        close = getattr(model, "close", None)
        if callable(close):
            close()


def _database_path(value: str | os.PathLike[str]) -> Path:
    try:
        path = Path(value).resolve()
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        raise ValueError("database_path must identify a SQLite file") from exc
    if not path.is_file():
        raise ValueError("database_path must identify a SQLite file")
    return path


def _datetime_us(value: datetime | None, field_name: str) -> int | None:
    if value is None:
        return None
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError(f"{field_name} must be timezone-aware")
    try:
        delta = value.astimezone(timezone.utc) - datetime(
            1970, 1, 1, tzinfo=timezone.utc
        )
        result = (delta.days * 86_400 + delta.seconds) * 1_000_000 + delta.microseconds
    except (OverflowError, ValueError) as exc:
        raise ValueError(f"{field_name} is outside the supported range") from exc
    if result < -(2**63) or result > 2**63 - 1:
        raise ValueError(f"{field_name} is outside the supported range")
    return result


def _resolve_legacy_record_filter_sync(
    path: Path,
    workspace_id: str,
    file_paths: tuple[str, ...],
    since_us: int | None,
    until_us: int | None,
) -> frozenset[str]:
    clauses = ["workspace_id=?", "deleted_at_us IS NULL"]
    parameters: list[object] = [workspace_id]
    if since_us is not None:
        clauses.append("created_at_us>=?")
        parameters.append(since_us)
    if until_us is not None:
        clauses.append("created_at_us<=?")
        parameters.append(until_us)
    connection = sqlite3.connect(path, timeout=2.0)
    try:
        rows = connection.execute(
            "SELECT record_id,file_path,file_path_relative FROM memory_records "
            f"WHERE {' AND '.join(clauses)} "
            "ORDER BY created_at_us DESC,record_id ASC LIMIT 256",
            tuple(parameters),
        ).fetchall()
    finally:
        connection.close()
    normalized_paths = tuple(path.replace("\\", "/") for path in file_paths)

    def path_matches(row: tuple[object, ...]) -> bool:
        if not normalized_paths:
            return True
        values = tuple(
            str(value).replace("\\", "/") for value in row[1:] if value is not None
        )
        return any(
            candidate == requested
            or candidate.endswith(requested)
            or requested.endswith(candidate)
            for candidate in values
            for requested in normalized_paths
        )

    return frozenset(str(row[0]) for row in rows if path_matches(row))


async def resolve_legacy_record_filter(
    database_path: str | os.PathLike[str],
    *,
    workspace_id: str,
    file_paths: tuple[str, ...] = (),
    since: datetime | None = None,
    until: datetime | None = None,
) -> frozenset[str]:
    """Resolve legacy path/date filters to bounded canonical record IDs."""

    path = _database_path(database_path)
    if (
        not isinstance(workspace_id, str)
        or _WORKSPACE_ID.fullmatch(workspace_id) is None
    ):
        raise ValueError("workspace_id is invalid")
    if (
        not isinstance(file_paths, tuple)
        or len(file_paths) > 2
        or any(
            not isinstance(value, str) or not value or len(value) > 4096
            for value in file_paths
        )
    ):
        raise ValueError("file_paths are invalid")
    since_us = _datetime_us(since, "since")
    until_us = _datetime_us(until, "until")
    if since_us is not None and until_us is not None and since_us > until_us:
        raise ValueError("since must not be later than until")
    return await _RUNTIME_FILTER_WORKERS.run(
        lambda: _resolve_legacy_record_filter_sync(
            path,
            workspace_id,
            file_paths,
            since_us,
            until_us,
        )
    )


def _configured_qdrant_path(path: Path, config: object) -> Path | str | None:
    if getattr(config, "qdrant_url", None) is not None:
        return None
    configured = getattr(config, "qdrant_path", None)
    return configured if configured is not None else path.parent / "qdrant"


def _embedding_encoder(
    config: object,
    purpose: Literal["query", "document"],
) -> ConfiguredEmbeddingEncoder:
    prefix_name = (
        "embedding_query_prefix" if purpose == "query" else "embedding_document_prefix"
    )
    return ConfiguredEmbeddingEncoder(
        model_id=config.embedding_model,
        dimension=config.embedding_dimension,
        prefix=getattr(config, prefix_name),
        backend=config.embedding_backend,
    )


def normalize_legacy_category_filter(
    categories: list[str] | tuple[str, ...] | None,
    include_warnings: bool,
) -> frozenset[str] | None:
    """Preserve legacy 'include warnings' semantics without narrowing all recalls."""

    if not isinstance(include_warnings, bool):
        raise ValueError("include_warnings must be boolean")
    if categories is None:
        return None
    if not isinstance(categories, (list, tuple)):
        raise ValueError("categories must contain non-empty strings")
    if not categories:
        return None
    if any(
        not isinstance(category, str) or not category.strip() for category in categories
    ):
        raise ValueError("categories must contain non-empty strings")
    selected = set(categories)
    if include_warnings:
        selected.add("warning")
    return frozenset(selected)


def create_retrieval_service(
    database_path: str | os.PathLike[str],
    *,
    config: object | None = None,
    capability_statuses: Mapping[str, str] | None = None,
) -> RetrievalService:
    """Assemble every v7 provider around worker-local canonical SQLite reads."""

    if config is None:
        from ..config import settings

        config = settings
    path = _database_path(database_path)
    timeout = config.qdrant_timeout_seconds
    connection_factory = sqlite_read_connection_factory(
        path,
        busy_timeout_seconds=min(float(timeout), 60.0),
    )
    statuses = _effective_capability_statuses(capability_statuses)
    dense_ready = _profile_ready(statuses, "local") and _profile_ready(
        statuses, "models-local"
    )
    providers: dict[str, Any] = {
        "lexical": LexicalProvider(
            connection_factory=connection_factory,
            timeout_seconds=min(float(timeout), 60.0),
        ),
        "temporal": TemporalProvider(connection_factory=connection_factory),
        "procedure": ProcedureProvider(connection_factory=connection_factory),
        "outcome": OutcomeProvider(connection_factory=connection_factory),
    }
    query_encoder = None
    document_encoder = None
    if dense_ready:
        query_encoder = _embedding_encoder(config, "query")
        document_encoder = _embedding_encoder(config, "document")
        providers["dense"] = DenseProvider(
            connection_factory=connection_factory,
            provider_key="qdrant",
            model_id=config.embedding_model,
            dimension=config.embedding_dimension,
            encoder=query_encoder,
            document_encoder=document_encoder,
            query_prefix=config.embedding_query_prefix,
            qdrant_path=_configured_qdrant_path(path, config),
            qdrant_url=config.qdrant_url,
            qdrant_api_key=config.qdrant_api_key,
            timeout_seconds=timeout,
            collection_prefix=config.qdrant_collection_prefix,
            own_encoder=True,
            own_document_encoder=True,
        )
    if _profile_ready(statuses, "graph"):
        providers["graph"] = GraphProvider(
            connection_factory=connection_factory,
            max_depth=config.retrieval_graph_max_depth,
            max_branching=config.retrieval_graph_max_branching,
        )
    reranker = None
    if config.retrieval_rerank_enabled and dense_ready:
        reranker = EmbeddingSimilarityReranker(
            query_encoder=query_encoder,
            document_encoder=document_encoder,
        )
    return RetrievalService(
        providers=providers,
        repository=SQLiteRetrievalRepository(
            path,
            initialization_timeout_seconds=min(
                float(getattr(config, "sync_timeout_seconds", 15.0)),
                60.0,
            ),
        ),
        composer=EvidenceComposer(tokenizer=CoreTokenizer()),
        planner=RetrievalPlanner(
            optional_candidate_limit=config.retrieval_candidate_limit
        ),
        reranker=reranker,
        rerank_enabled=config.retrieval_rerank_enabled,
        rerank_candidate_limit=config.retrieval_rerank_candidate_limit,
        provider_timeout_seconds=timeout,
        weights=config.retrieval_rrf_weights,
        rrf_k=config.rrf_k,
    )


def create_projection_builders(
    connection: sqlite3.Connection,
    database_path: str | os.PathLike[str],
    *,
    config: object | None = None,
    include_optional: bool = True,
    capability_statuses: Mapping[str, str] | None = None,
    cancellation_event: threading.Event | None = None,
) -> dict[str, Any]:
    """Build the complete operator/job registry for one active v7 database."""

    if not isinstance(connection, sqlite3.Connection):
        raise ValueError("connection must be a SQLite connection")
    if not isinstance(include_optional, bool):
        raise ValueError("include_optional must be boolean")
    if config is None:
        from ..config import settings

        config = settings
    path = _database_path(database_path)
    from .projections import LexicalProjectionBuilder

    builders = _ProjectionBuilderRegistry()
    builders["lexical"] = LexicalProjectionBuilder(connection).rebuild
    if not include_optional:
        return builders

    from .dense_projection import DenseProjectionBuilder
    from .specialized_projection import SpecializedProjectionBuilder

    statuses = _effective_capability_statuses(capability_statuses)
    if _profile_ready(statuses, "local") and _profile_ready(statuses, "models-local"):
        dense = DenseProjectionBuilder(
            connection,
            provider_key="qdrant",
            model_id=config.embedding_model,
            dimension=config.embedding_dimension,
            encoder=_embedding_encoder(config, "document"),
            qdrant_path=_configured_qdrant_path(path, config),
            qdrant_url=config.qdrant_url,
            qdrant_api_key=config.qdrant_api_key,
            timeout_seconds=config.qdrant_timeout_seconds,
            collection_prefix=config.qdrant_collection_prefix,
            query_prefix=config.embedding_query_prefix,
            cancelled=(
                cancellation_event.is_set if cancellation_event is not None else None
            ),
            own_encoder=True,
        )
        builders.own(dense)
        builders.dense_builder = dense
        builders["dense"] = dense.rebuild
    specialized = SpecializedProjectionBuilder(connection)
    for projection_name in ("graph", "temporal", "procedure", "outcome"):
        if projection_name == "graph" and not _profile_ready(statuses, "graph"):
            continue
        if projection_name == "graph":
            from ..graph_projection import GraphProjectionBuilder

            graph = GraphProjectionBuilder(connection)
            builders[projection_name] = (
                lambda workspace_id, dry_run=False, graph=graph: (
                    specialized.rebuild(workspace_id, "graph", dry_run=True)
                    if dry_run
                    else graph.rebuild(workspace_id)
                )
            )
            continue
        builders[projection_name] = (
            lambda workspace_id, dry_run=False, name=projection_name: (
                specialized.rebuild(
                    workspace_id,
                    name,
                    dry_run=dry_run,
                )
            )
        )
    return builders


def _drain_projection_jobs_sync(
    path: Path,
    config: object,
    max_jobs: int,
    include_optional: bool,
    capability_statuses: Mapping[str, str] | None,
    cancellation_event: threading.Event | None = None,
) -> tuple[ProjectionJobRun, ...]:
    connection = sqlite3.connect(path, timeout=5.0)
    connection.execute("PRAGMA foreign_keys=ON")
    builders: dict[str, Any] | None = None
    try:
        builders = create_projection_builders(
            connection,
            path,
            config=config,
            include_optional=include_optional,
            capability_statuses=capability_statuses,
            cancellation_event=cancellation_event,
        )
        runner = ProjectionJobRunner(
            connection,
            builders=builders,
            cancelled=(
                cancellation_event.is_set if cancellation_event is not None else None
            ),
        )
        runs: list[ProjectionJobRun] = []
        for _ in range(max_jobs if include_optional else 1):
            if cancellation_event is not None and cancellation_event.is_set():
                break
            result = runner.run_once()
            if result is None:
                break
            runs.append(result)
        dense_builder = getattr(builders, "dense_builder", None)
        gc_due = None
        if include_optional and _dense_gc_schema_available(connection):
            now_us = time.time_ns() // 1_000
            from .dense_generation_gc import reconcile_inactive_generation_gc

            reconcile_inactive_generation_gc(connection, now_us=now_us)
            gc_due = connection.execute(
                "SELECT 1 FROM dense_generation_gc_jobs WHERE "
                "(status='queued' AND available_at_us<=?) OR "
                "(status='running' AND claim_expires_at_us<=?) LIMIT 1",
                (now_us, now_us),
            ).fetchone()
        if include_optional and dense_builder is not None and gc_due is not None:
            from .dense_generation_gc import DenseGenerationGarbageCollector

            get_client = getattr(dense_builder, "_get_client", None)
            if not callable(get_client):
                raise RuntimeError("dense projection provider is unavailable")
            DenseGenerationGarbageCollector(
                connection,
                client=get_client(),
                cancelled=(
                    cancellation_event.is_set
                    if cancellation_event is not None
                    else None
                ),
            ).run_slice(max_generations=max_jobs)
        elif include_optional and gc_due is not None:
            # The configured profile cannot currently construct the provider.
            # Preserve the durable job and give the scheduler a bounded retry
            # deadline instead of spinning on an immediately-due row.
            deferred_at_us = time.time_ns() // 1_000
            retry_at_us = deferred_at_us + int(
                _PROJECTION_RETRY_MAX_SECONDS * 1_000_000
            )
            connection.execute(
                "UPDATE dense_generation_gc_jobs SET status='queued',"
                "available_at_us=?,claim_owner=NULL,claim_token=NULL,"
                "claim_expires_at_us=NULL,"
                "last_error_code='DENSE_GENERATION_GC_CAPABILITY_DISABLED',"
                "updated_at_us=? WHERE "
                "(status='queued' AND available_at_us<=?) OR "
                "(status='running' AND claim_expires_at_us<=?)",
                (retry_at_us, deferred_at_us, deferred_at_us, deferred_at_us),
            )
            connection.commit()
        return tuple(runs)
    finally:
        close = getattr(builders, "close", None)
        if callable(close):
            with suppress(Exception):
                close()
        connection.close()


async def drain_projection_jobs(
    database_path: str | os.PathLike[str],
    *,
    config: object | None = None,
    max_jobs: int = 1,
    include_optional: bool = False,
    capability_statuses: Mapping[str, str] | None = None,
    _cancellation_event: threading.Event | None = None,
) -> tuple[ProjectionJobRun, ...]:
    """Run bounded durable work; cancelling this direct call requests job cancellation.

    Scheduled coalesced drains pass their own token, which is set only by the
    scheduler shutdown path rather than by cancellation of an observing waiter.
    """

    if (
        isinstance(max_jobs, bool)
        or not isinstance(max_jobs, int)
        or max_jobs < 1
        or max_jobs > 100
    ):
        raise ValueError("max_jobs must be between 1 and 100")
    if not isinstance(include_optional, bool):
        raise ValueError("include_optional must be boolean")
    if config is None:
        from ..config import settings

        config = settings
    path = _database_path(database_path)
    workers = _RUNTIME_JOB_WORKERS if include_optional else _RUNTIME_CORE_JOB_WORKERS
    cancellation_event = _cancellation_event or threading.Event()
    return await workers.run_with_cancellation_cleanup(
        lambda: _drain_projection_jobs_sync(
            path,
            config,
            max_jobs,
            include_optional,
            capability_statuses,
            cancellation_event,
        ),
        request_cancel=cancellation_event.set,
    )


async def _run_scheduled_projection_drain(
    path: Path,
    config: object | None,
    max_jobs: int,
    state: _ProjectionDrainState,
    capability_statuses: Mapping[str, str] | None,
) -> None:
    failures = 0
    try:
        while True:
            if state.stopping:
                return
            state.wake.clear()
            try:
                child = asyncio.create_task(
                    drain_projection_jobs(
                        path,
                        config=config,
                        max_jobs=max_jobs,
                        include_optional=True,
                        capability_statuses=capability_statuses,
                        _cancellation_event=state.cancellation,
                    ),
                    name="daem0nmcp-projection-drain-slice",
                )
                try:
                    runs = await asyncio.shield(child)
                except asyncio.CancelledError:
                    state.stopping = True
                    state.cancellation.set()
                    with suppress(asyncio.CancelledError, Exception):
                        await asyncio.shield(child)
                    raise
            except Exception as exc:
                if not _transient_projection_drain_failure(exc):
                    logger.warning(
                        "Optional retrieval projection refresh remains queued"
                    )
                    return
                delay = min(
                    _PROJECTION_RETRY_INITIAL_SECONDS * (2 ** min(failures, 7)),
                    _PROJECTION_RETRY_MAX_SECONDS,
                )
                failures += 1
                await _wait_for_projection_retry(state, delay)
                continue
            failures = 0
            if state.stopping:
                return
            if len(runs) >= max_jobs:
                if state.continuous:
                    # A continuously scheduled drain must leave time for
                    # foreground SQLite writers after each bounded slice.
                    state.wake.clear()
                    await _wait_for_projection_retry(
                        state, _PROJECTION_IDLE_POLL_SECONDS
                    )
                    continue
                await asyncio.sleep(0)
                continue
            if state.wake.is_set():
                continue
            try:
                deadline_us = await _RUNTIME_JOB_WORKERS.run_with_cancellation_cleanup(
                    lambda: _next_projection_job_deadline_sync(path),
                    request_cancel=state.cancellation.set,
                )
            except Exception as exc:
                if not _transient_projection_drain_failure(exc):
                    logger.warning(
                        "Optional retrieval projection refresh remains queued"
                    )
                    return
                delay = min(
                    _PROJECTION_RETRY_INITIAL_SECONDS * (2 ** min(failures, 7)),
                    _PROJECTION_RETRY_MAX_SECONDS,
                )
                failures += 1
                await _wait_for_projection_retry(state, delay)
                continue
            if state.wake.is_set():
                continue
            if deadline_us is None:
                if not state.continuous:
                    return
                await _wait_for_projection_retry(state, _PROJECTION_IDLE_POLL_SECONDS)
                continue
            delay = max(0.0, (deadline_us - time.time_ns() // 1_000) / 1_000_000)
            with suppress(asyncio.TimeoutError):
                await asyncio.wait_for(state.wake.wait(), timeout=delay)
    except asyncio.CancelledError:
        raise
    except Exception:
        # Jobs are durable and remain eligible for the next scheduled drain or
        # an operator rebuild.  Never turn optional refresh failure into an
        # unhandled background-task exception.
        logger.warning("Optional retrieval projection refresh remains queued")
    finally:
        current = _PROJECTION_DRAIN_STATES.get(path)
        if current is state:
            _PROJECTION_DRAIN_STATES.pop(path, None)


def _transient_projection_drain_failure(exc: BaseException) -> bool:
    if isinstance(exc, (BoundedWorkerBusyError, OSError)):
        return True
    if not isinstance(exc, sqlite3.OperationalError):
        return False
    message = str(exc).casefold()
    return any(
        marker in message
        for marker in (
            "busy",
            "locked",
            "disk i/o",
            "unable to open",
            "readonly",
            "temporarily unavailable",
        )
    )


async def _wait_for_projection_retry(
    state: _ProjectionDrainState, delay: float
) -> None:
    if state.stopping or state.wake.is_set():
        return
    with suppress(asyncio.TimeoutError):
        await asyncio.wait_for(state.wake.wait(), timeout=delay)


def _next_projection_job_deadline_sync(path: Path) -> int | None:
    connection = sqlite3.connect(path, timeout=2.0)
    try:
        row = connection.execute(
            """
            SELECT MIN(
                CASE
                    WHEN status='queued' THEN available_at_us
                    ELSE lease_expires_at_us
                END
            )
            FROM background_jobs
            WHERE job_type='retrieval.projection_rebuild'
              AND status IN ('queued','running')
            """
        ).fetchone()
        gc_row = (
            connection.execute(
                "SELECT MIN(CASE WHEN status='queued' THEN available_at_us "
                "ELSE claim_expires_at_us END) FROM dense_generation_gc_jobs "
                "WHERE status IN ('queued','running')"
            ).fetchone()
            if _dense_gc_schema_available(connection)
            else None
        )
    finally:
        connection.close()
    deadlines = [
        int(value)
        for value in (
            None if row is None else row[0],
            None if gc_row is None else gc_row[0],
        )
        if value is not None
    ]
    return min(deadlines) if deadlines else None


def schedule_projection_job_drain(
    database_path: str | os.PathLike[str],
    *,
    config: object | None = None,
    max_jobs: int = 5,
    capability_statuses: Mapping[str, str] | None = None,
    continuous: bool = False,
) -> asyncio.Task[None]:
    """Start one coalesced, off-loop optional projection drain per database."""

    if (
        isinstance(max_jobs, bool)
        or not isinstance(max_jobs, int)
        or not 1 <= max_jobs <= 100
    ):
        raise ValueError("max_jobs must be between 1 and 100")
    if not isinstance(continuous, bool):
        raise ValueError("continuous must be boolean")
    path = _database_path(database_path)
    active = _PROJECTION_DRAIN_STATES.get(path)
    if active is not None and active.task is not None and not active.task.done():
        active.continuous = active.continuous or continuous
        active.wake.set()
        return active.task
    state = _ProjectionDrainState(asyncio.Event(), continuous=continuous)
    task = asyncio.create_task(
        _run_scheduled_projection_drain(
            path, config, max_jobs, state, capability_statuses
        ),
        name="daem0nmcp-projection-drain",
    )
    state.task = task
    _PROJECTION_DRAIN_STATES[path] = state
    return task


async def await_projection_job_drains(
    database_paths: tuple[str | os.PathLike[str], ...] | None = None,
) -> None:
    """Quiesce scheduled drains after their currently admitted slice finishes."""

    selected = (
        None
        if database_paths is None
        else frozenset(_database_path(value) for value in database_paths)
    )
    cancellation: asyncio.CancelledError | None = None
    while True:
        states = tuple(
            state
            for path, state in _PROJECTION_DRAIN_STATES.items()
            if selected is None or path in selected
        )
        for state in states:
            state.stopping = True
            state.cancellation.set()
            state.wake.set()
        tasks = tuple(
            state.task
            for state in states
            if state.task is not None and not state.task.done()
        )
        if not tasks:
            if cancellation is not None:
                raise cancellation from None
            return
        try:
            await asyncio.gather(*(asyncio.shield(task) for task in tasks))
        except asyncio.CancelledError as exc:
            if cancellation is None:
                cancellation = exc


__all__ = [
    "ConfiguredEmbeddingEncoder",
    "CoreTokenizer",
    "create_projection_builders",
    "create_retrieval_service",
    "drain_projection_jobs",
    "await_projection_job_drains",
    "normalize_legacy_category_filter",
    "resolve_legacy_record_filter",
    "schedule_projection_job_drain",
    "warn_legacy_hybrid_weight",
]
