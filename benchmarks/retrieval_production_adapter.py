"""No-network production adapter for the checked-in retrieval corpus."""

from __future__ import annotations

import asyncio
import atexit
import hashlib
import importlib.util
import sqlite3
import tempfile
import threading
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from benchmarks.retrieval_benchmark import (
    BENCHMARK_MODES,
    RetrievalFixtures,
    load_retrieval_fixtures,
)
from daem0nmcp.event_store import EventCommand, EventStore
from daem0nmcp.migrations.schema import MIGRATIONS
from daem0nmcp.retrieval.composer import EvidenceComposer
from daem0nmcp.retrieval.dense_projection import DenseProjectionBuilder
from daem0nmcp.retrieval.planner import RetrievalPlanner
from daem0nmcp.retrieval.projections import LexicalProjectionBuilder
from daem0nmcp.retrieval.providers import DenseProvider, LexicalProvider
from daem0nmcp.retrieval.repository import (
    SQLiteRetrievalRepository,
    sqlite_read_connection_factory,
)
from daem0nmcp.retrieval.runtime import CoreTokenizer
from daem0nmcp.retrieval.service import RetrievalService
from daem0nmcp.retrieval.specialized import (
    GraphProvider,
    OutcomeProvider,
    ProcedureProvider,
    TemporalProvider,
)
from daem0nmcp.retrieval.specialized_projection import (
    SpecializedProjectionBuilder,
)
from daem0nmcp.retrieval.types import ProviderResult, RetrievalQuery

_DEFAULT_FIXTURE_ROOT = (
    Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "retrieval"
)
_FAULT_QUERY_ID = "q12_lexical_unavailable"
_MODEL_ID = "fixture-semantic-v1"
_QUERY_PREFIX = "search_query: "
_DOCUMENT_PREFIX = "search_document: "


def _apply_schema(connection: sqlite3.Connection) -> None:
    for version, _description, statements in MIGRATIONS:
        if version < 16:
            continue
        for statement in statements:
            connection.execute(statement)
    connection.commit()


def _utc_from_microseconds(value: object) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("fixture time must be an integer or null")
    return datetime.fromtimestamp(value / 1_000_000, timezone.utc)


def _optional_filter(values: object) -> frozenset[str] | None:
    if not isinstance(values, list) or not all(
        isinstance(value, str) and value for value in values
    ):
        raise ValueError("fixture filter must be a string list")
    return frozenset(values) or None


class _FixtureEncoder:
    model_id = _MODEL_ID
    dimension = 3
    backend = "fixture"
    max_seq_length = 512

    def __init__(self, prefix: str) -> None:
        self.prefix = prefix

    def encode(self, text: str) -> list[float]:
        digest = hashlib.sha256(f"{self.prefix}{text}".encode()).digest()
        return [
            int.from_bytes(digest[index : index + 4], "big") / (2**32)
            for index in (0, 4, 8)
        ]


class _Distance:
    COSINE = "Cosine"


class _VectorParams:
    def __init__(self, *, size: int, distance: object) -> None:
        self.size = size
        self.distance = distance


class _PointStruct:
    def __init__(
        self,
        *,
        id: str,
        vector: list[float],
        payload: dict[str, object],
    ) -> None:
        self.id = id
        self.vector = vector
        self.payload = payload


class _QdrantModels:
    Distance = _Distance
    PointStruct = _PointStruct
    VectorParams = _VectorParams


def _load_rank_fake(root: Path) -> object:
    path = root / "qdrant_fake.py"
    specification = importlib.util.spec_from_file_location(
        "daem0nmcp_retrieval_rank_fixture",
        path,
    )
    if specification is None or specification.loader is None:
        raise RuntimeError("RETRIEVAL_RANK_FIXTURE_UNAVAILABLE")
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    return module.DeterministicRankListFake()


class _FixtureQdrantClient:
    def __init__(
        self,
        rank_fake: object,
        query_vectors: Mapping[tuple[float, ...], str],
    ) -> None:
        self._rank_fake = rank_fake
        self._query_vectors = dict(query_vectors)
        self._collections: dict[str, dict[str, dict[str, object]]] = {}

    def collection_exists(self, collection_name: str) -> bool:
        return collection_name in self._collections

    def delete_collection(self, collection_name: str) -> None:
        self._collections.pop(collection_name, None)

    def create_collection(
        self,
        *,
        collection_name: str,
        vectors_config: object,
    ) -> None:
        if not isinstance(vectors_config, _VectorParams):
            raise TypeError("anonymous vector configuration is invalid")
        self._collections[collection_name] = {}

    def upsert(
        self,
        *,
        collection_name: str,
        points: list[object],
        wait: bool,
    ) -> None:
        if wait is not True:
            raise ValueError("fixture upserts must wait")
        collection = self._collections[collection_name]
        for point in points:
            if not isinstance(point, _PointStruct):
                raise TypeError("fixture points must use PointStruct")
            collection[point.id] = {
                "id": point.id,
                "payload": dict(point.payload),
                "vector": list(point.vector),
            }

    def retrieve(
        self,
        *,
        collection_name: str,
        ids: list[str],
        with_payload: bool,
        with_vectors: bool,
    ) -> list[SimpleNamespace]:
        del with_payload, with_vectors
        collection = self._collections[collection_name]
        return [
            SimpleNamespace(**collection[point_id])
            for point_id in ids
            if point_id in collection
        ]

    def count(self, *, collection_name: str, exact: bool) -> SimpleNamespace:
        if exact is not True:
            raise ValueError("fixture counts must be exact")
        return SimpleNamespace(count=len(self._collections[collection_name]))

    def query_points(
        self,
        *,
        collection_name: str,
        query: list[float],
        limit: int,
        with_payload: bool,
        with_vectors: bool,
    ) -> SimpleNamespace:
        del with_payload, with_vectors
        query_id = self._query_vectors.get(tuple(query))
        search = getattr(self._rank_fake, "search", None)
        if query_id is None or not callable(search):
            return SimpleNamespace(points=[])
        stored = self._collections[collection_name]
        by_record = {
            str(point["payload"]["record_id"]): point for point in stored.values()
        }
        points = []
        for ranked in search(query_id, limit=limit):
            record_id = str(ranked["payload"]["record_id"])
            point = by_record.get(record_id)
            if point is None:
                continue
            points.append(
                SimpleNamespace(
                    id=point["id"],
                    payload=dict(point["payload"]),
                    score=float(ranked["score"]),
                )
            )
        return SimpleNamespace(points=points)


class _ScenarioLexicalProvider:
    name = "lexical"

    def __init__(self, delegate: LexicalProvider) -> None:
        self._delegate = delegate
        self.query_id: str | None = None

    async def search(
        self,
        query: RetrievalQuery,
        limit: int,
    ) -> ProviderResult:
        if self.query_id == _FAULT_QUERY_ID:
            return ProviderResult(
                provider=self.name,
                status="unavailable",
                reason="LEXICAL_UNAVAILABLE",
                elapsed_ms=0.0,
            )
        return await self._delegate.search(query, limit)


class _UnavailableProvider:
    def __init__(self, name: str) -> None:
        self.name = name

    async def search(
        self,
        query: RetrievalQuery,
        limit: int,
        **_kwargs: object,
    ) -> ProviderResult:
        del query, limit
        return ProviderResult(
            provider=self.name,
            status="unavailable",
            reason=f"{self.name.upper()}_UNAVAILABLE",
            elapsed_ms=0.0,
        )


class ProductionRetrievalAdapter:
    """Replay canonical fixtures and execute the real retrieval pipeline."""

    def __init__(
        self,
        fixture_root: str | Path = _DEFAULT_FIXTURE_ROOT,
        *,
        fixtures: RetrievalFixtures | None = None,
    ) -> None:
        self.fixture_root = Path(fixture_root).resolve()
        self.fixtures = fixtures or load_retrieval_fixtures(self.fixture_root)
        workspace_ids = {
            str(record["workspace_id"]) for record in self.fixtures.records
        }
        if len(workspace_ids) != 1:
            raise ValueError("fixtures must describe exactly one workspace")
        self.workspace_id = next(iter(workspace_ids))
        self._temporary = tempfile.TemporaryDirectory()
        self.database_path = Path(self._temporary.name) / "retrieval.sqlite3"
        self._closed = False
        self._query_encoder = _FixtureEncoder(_QUERY_PREFIX)
        self._document_encoder = _FixtureEncoder(_DOCUMENT_PREFIX)
        query_vectors = {
            tuple(self._query_encoder.encode(str(query["text"]))): str(
                query["query_id"]
            )
            for query in self.fixtures.queries
        }
        self._qdrant = _FixtureQdrantClient(
            _load_rank_fake(self.fixture_root),
            query_vectors,
        )
        try:
            self._build_database()
            self._connection_factory = sqlite_read_connection_factory(
                self.database_path
            )
            self._lexical_delegate = LexicalProvider(
                connection_factory=self._connection_factory
            )
            self._lexical = _ScenarioLexicalProvider(self._lexical_delegate)
            self._fully_enabled = self._service(
                {
                    "lexical": self._lexical,
                    "dense": DenseProvider(
                        connection_factory=self._connection_factory,
                        provider_key="qdrant",
                        model_id=_MODEL_ID,
                        dimension=3,
                        encoder=self._query_encoder,
                        document_encoder=self._document_encoder,
                        query_prefix=_QUERY_PREFIX,
                        client=self._qdrant,
                        timeout_seconds=2.0,
                        collection_prefix="fixture",
                    ),
                    "graph": GraphProvider(connection_factory=self._connection_factory),
                    "temporal": TemporalProvider(
                        connection_factory=self._connection_factory
                    ),
                    "procedure": ProcedureProvider(
                        connection_factory=self._connection_factory
                    ),
                    "outcome": OutcomeProvider(
                        connection_factory=self._connection_factory
                    ),
                }
            )
        except Exception:
            self._closed = True
            self._temporary.cleanup()
            raise

    def _build_database(self) -> None:
        connection = sqlite3.connect(self.database_path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        try:
            _apply_schema(connection)
            store = EventStore(connection)
            for event in self.fixtures.events:
                appended = store.append_and_project(
                    EventCommand(
                        workspace_id=str(event["workspace_id"]),
                        stream_id=str(event["stream_id"]),
                        stream_kind=str(event["stream_kind"]),
                        event_type=str(event["event_type"]),
                        occurred_at_us=int(event["occurred_at_us"]),
                        recorded_at_us=int(event["recorded_at_us"]),
                        actor_type=str(event["actor_type"]),
                        actor_id=str(event["actor_id"]),
                        payload=event["payload"],
                        causation_event_id=event["causation_event_id"],
                        correlation_id=str(event["correlation_id"]),
                        event_schema_version=int(event["event_schema_version"]),
                        expected_stream_version=int(event["stream_version"]),
                    )
                )
                if (
                    appended.event_id != event["event_id"]
                    or appended.event_hash != event["event_hash"]
                    or appended.payload_hash != event["payload_hash"]
                    or appended.stream_version != event["stream_version"]
                    or appended.previous_event_hash != event["previous_event_hash"]
                ):
                    raise RuntimeError("RETRIEVAL_FIXTURE_REPLAY_MISMATCH")
            workspace_id = str(self.fixtures.records[0]["workspace_id"])
            LexicalProjectionBuilder(connection).rebuild(workspace_id)
            specialized = SpecializedProjectionBuilder(connection)
            for name in ("graph", "temporal", "procedure", "outcome"):
                specialized.rebuild(workspace_id, name)
            connection.commit()
            DenseProjectionBuilder(
                connection,
                provider_key="qdrant",
                model_id=_MODEL_ID,
                dimension=3,
                encoder=self._document_encoder,
                query_prefix=_QUERY_PREFIX,
                client=self._qdrant,
                qdrant_models=_QdrantModels,
                collection_prefix="fixture",
            ).rebuild(workspace_id)
            connection.commit()
        finally:
            connection.close()

    def _service(self, providers: Mapping[str, object]) -> RetrievalService:
        return RetrievalService(
            providers=providers,
            repository=SQLiteRetrievalRepository(
                connection_factory=self._connection_factory
            ),
            composer=EvidenceComposer(tokenizer=CoreTokenizer()),
            planner=RetrievalPlanner(optional_candidate_limit=50),
            provider_timeout_seconds=5.0,
        )

    def _query(self, raw: Mapping[str, Any]) -> RetrievalQuery:
        filters = raw.get("filters")
        if not isinstance(filters, Mapping):
            raise ValueError("fixture query filters are invalid")
        return RetrievalQuery(
            workspace_id=self.workspace_id,
            text=str(raw["text"]),
            limit=10,
            candidate_limit=50,
            as_of_valid_time=_utc_from_microseconds(raw.get("as_of_valid_time_us")),
            as_of_transaction_time=_utc_from_microseconds(
                raw.get("as_of_transaction_time_us")
            ),
            categories=_optional_filter(filters.get("categories")),
            tags=_optional_filter(filters.get("tags")),
            record_ids=_optional_filter(filters.get("record_ids")),
            include_invalidated=bool(filters.get("include_invalidated")),
            include_archived=bool(filters.get("include_archived")),
            token_budget=int(raw["token_budget"]),
        )

    async def _retrieve_lexical_only(
        self,
        query_id: str,
        query: RetrievalQuery,
    ):
        providers: dict[str, object] = {
            "lexical": self._lexical,
            "graph": _UnavailableProvider("graph"),
            "temporal": _UnavailableProvider("temporal"),
            "procedure": _UnavailableProvider("procedure"),
            "outcome": _UnavailableProvider("outcome"),
        }
        if query_id != _FAULT_QUERY_ID:
            preview = await self._lexical_delegate.search(
                query,
                query.candidate_limit,
            )
            if preview.status == "ready" and not preview.candidates:
                providers["dense"] = _UnavailableProvider("dense")
        return await self._service(providers).retrieve(query)

    def retrieve(
        self,
        mode: str,
        raw_query: Mapping[str, Any],
    ) -> dict[str, object]:
        if self._closed:
            raise RuntimeError("RETRIEVAL_ADAPTER_CLOSED")
        if mode not in BENCHMARK_MODES:
            raise ValueError("benchmark mode is invalid")
        query_id = raw_query.get("query_id")
        if not isinstance(query_id, str) or not query_id:
            raise ValueError("fixture query ID is invalid")
        query = self._query(raw_query)
        self._lexical.query_id = query_id
        try:
            if mode == "fully_enabled":
                result = asyncio.run(self._fully_enabled.retrieve(query))
            else:
                result = asyncio.run(self._retrieve_lexical_only(query_id, query))
        finally:
            self._lexical.query_id = None
        citation_ids: list[str] = []
        citation_statuses: dict[str, str] = {}
        returned_ids: list[str] = []
        if not result.abstained:
            for item in result.items:
                returned_ids.append(item.evidence_refs[0].record_id)
                for reference in item.evidence_refs:
                    if reference.record_id not in citation_statuses:
                        citation_ids.append(reference.record_id)
                        citation_statuses[reference.record_id] = item.status
        return {
            "abstained": result.abstained,
            "citation_record_ids": citation_ids,
            "citation_statuses": citation_statuses,
            "provider_statuses": {
                diagnostic.provider: diagnostic.status
                for diagnostic in result.providers
            },
            "provider_timings_ns": {
                diagnostic.provider: max(
                    0,
                    int(round(diagnostic.elapsed_ms * 1_000_000)),
                )
                for diagnostic in result.providers
            },
            "rendered_tokens": (
                0 if result.abstained else result.context.rendered_tokens
            ),
            "returned_record_ids": returned_ids,
        }

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._temporary.cleanup()


_DEFAULT_LOCK = threading.Lock()
_DEFAULT_ADAPTER: ProductionRetrievalAdapter | None = None


def _default_adapter() -> ProductionRetrievalAdapter:
    global _DEFAULT_ADAPTER
    with _DEFAULT_LOCK:
        if _DEFAULT_ADAPTER is None:
            _DEFAULT_ADAPTER = ProductionRetrievalAdapter()
            # TemporaryDirectory registers its weakref exit hook lazily during
            # construction. Re-register our explicit closer afterward so the
            # adapter closes first under atexit's LIFO ordering.
            atexit.unregister(_close_default)
            atexit.register(_close_default)
        return _DEFAULT_ADAPTER


def lexical_only(raw_query: Mapping[str, Any]) -> dict[str, object]:
    return _default_adapter().retrieve("lexical_only", raw_query)


def fully_enabled(raw_query: Mapping[str, Any]) -> dict[str, object]:
    return _default_adapter().retrieve("fully_enabled", raw_query)


def _close_default() -> None:
    global _DEFAULT_ADAPTER
    with _DEFAULT_LOCK:
        if _DEFAULT_ADAPTER is not None:
            _DEFAULT_ADAPTER.close()
            _DEFAULT_ADAPTER = None


atexit.register(_close_default)


__all__ = [
    "ProductionRetrievalAdapter",
    "fully_enabled",
    "lexical_only",
]
