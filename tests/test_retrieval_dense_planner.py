"""Dependency-free tests for v7 dense retrieval and intent planning."""

from __future__ import annotations

import asyncio
import hashlib
import importlib
import json
import sqlite3
import tempfile
import threading
import unittest
import uuid
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

WORKSPACE_ID = "ws_" + "1" * 24
MODEL_ID = "model-v1"
PROVIDER_KEY = "qdrant"
CONTENT_A = "a" * 64
CONTENT_B = "b" * 64
CONTENT_STALE = "c" * 64
RECORD_A = "mem_" + "a" * 64
RECORD_B = "mem_" + "b" * 64
RECORD_UNKNOWN = "mem_" + "c" * 64
EVENT_A = "evt_" + "a" * 64
EVENT_B = "evt_" + "b" * 64


class _ManifestDocumentEncoder:
    def __init__(self, handler=None) -> None:
        self._handler = handler

    def encode(self, text: str) -> list[float]:
        if self._handler is not None:
            return self._handler(text)
        return [0.0, 0.0, 0.0]


def _dense_point_id(record_id: str) -> str:
    return str(
        uuid.uuid5(
            uuid.NAMESPACE_URL,
            f"daem0nmcp:{WORKSPACE_ID}:{record_id}",
        )
    )


def _dense_manifest_details(
    *,
    generation: int = 4,
    model_id: str = MODEL_ID,
    provider_key: str = PROVIDER_KEY,
    dimension: int = 3,
    collection_prefix: str = "daem0nmcp",
    distance: str = "cosine",
    backend: str | None = None,
) -> dict[str, object]:
    model_digest = hashlib.sha256(model_id.encode("utf-8")).hexdigest()[:12]
    collection_name = (
        f"{collection_prefix}-{WORKSPACE_ID}-{provider_key}-"
        f"g{generation}-{model_digest}"
    )
    configuration = {
        "collection_name": collection_name,
        "collection_prefix": collection_prefix,
        "dimension": dimension,
        "distance": distance,
        "model_id": model_id,
        "provider_key": provider_key,
        "schema_version": 1,
    }
    config_hash = hashlib.sha256(
        json.dumps(
            configuration,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()
    encoder_contract = {
        "artifact_fingerprint": None,
        "backend": backend,
        "document_prefix": None,
        "encoder_type": (
            f"{_ManifestDocumentEncoder.__module__}."
            f"{_ManifestDocumentEncoder.__qualname__}"
        ),
        "input_source": "memory_records.content",
        "max_sequence_length": None,
        "model_id": model_id,
        "output_dimension": dimension,
        "query_prefix": None,
        "truncate_dimension": None,
    }
    builder_contract_hash = hashlib.sha256(
        json.dumps(
            {
                "build_config_hash": config_hash,
                "builder_version": "retrieval-dense-2",
                "encoder_contract": encoder_contract,
                "projection": "dense",
            },
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()
    return {
        **configuration,
        "build_config_hash": config_hash,
        "builder_contract_hash": builder_contract_hash,
        "encoder_contract": encoder_contract,
        "projection": "dense",
        "vector_format": "qdrant-cosine-f32-le-v1",
        "vector_space_hash": None,
    }


class RetrievalPlannerTests(unittest.TestCase):
    """The planner preserves lexical recall while adding ready intent channels."""

    def _planner_types(self):
        try:
            module = importlib.import_module("daem0nmcp.retrieval.planner")
        except ModuleNotFoundError:
            self.fail("the v7 retrieval planner module is missing")
        planner_type = getattr(module, "RetrievalPlanner", None)
        self.assertIsNotNone(planner_type, "RetrievalPlanner is missing")
        from daem0nmcp.retrieval.types import RetrievalQuery

        return planner_type, RetrievalQuery

    def test_lexical_is_always_first_and_dense_requires_ready_state(self):
        planner_type, query_type = self._planner_types()
        planner = planner_type(optional_candidate_limit=7)
        query = query_type(
            workspace_id=WORKSPACE_ID,
            text="authentication architecture",
            candidate_limit=50,
        )

        disabled = planner.plan(query, ready_providers=frozenset())
        enabled = planner.plan(query, ready_providers=frozenset({"dense"}))

        self.assertEqual(("lexical",), disabled.provider_names)
        self.assertEqual(("lexical", "dense"), enabled.provider_names)
        self.assertEqual((50,), tuple(item.limit for item in disabled.requests))
        self.assertEqual((50, 7), tuple(item.limit for item in enabled.requests))

    def test_overlapping_intents_add_each_ready_provider_once_in_fixed_order(self):
        planner_type, query_type = self._planner_types()
        planner = planner_type(optional_candidate_limit=6)
        query = query_type(
            workspace_id=WORKSPACE_ID,
            text=(
                "Why did authentication fail last week, and what steps "
                "should this procedure follow?"
            ),
            candidate_limit=30,
        )

        plan = planner.plan(
            query,
            ready_providers=frozenset(
                {"outcome", "procedure", "temporal", "graph", "dense"}
            ),
        )

        self.assertEqual(
            (
                "lexical",
                "dense",
                "graph",
                "temporal",
                "procedure",
                "outcome",
            ),
            plan.provider_names,
        )
        self.assertEqual((30, 6, 6, 6, 6, 6), tuple(r.limit for r in plan.requests))

    def test_explicit_as_of_time_requests_temporal_without_time_words(self):
        planner_type, query_type = self._planner_types()
        query = query_type(
            workspace_id=WORKSPACE_ID,
            text="authentication",
            as_of_valid_time=datetime(2026, 1, 1, tzinfo=timezone.utc),
        )

        plan = planner_type().plan(query, ready_providers=frozenset({"temporal"}))

        self.assertEqual(("lexical", "temporal"), plan.provider_names)

    def test_textual_as_of_date_requests_temporal_provider(self):
        planner_type, query_type = self._planner_types()
        query = query_type(
            workspace_id=WORKSPACE_ID,
            text="authentication configuration as of 2025-01-01",
        )

        plan = planner_type().plan(query, ready_providers=frozenset({"temporal"}))

        self.assertEqual(("lexical", "temporal"), plan.provider_names)

    def test_intent_scan_is_bounded_and_unknown_provider_names_are_ignored(self):
        planner_type, query_type = self._planner_types()
        query = query_type(
            workspace_id=WORKSPACE_ID,
            text=("x" * 5000) + " failure steps last week because",
        )

        plan = planner_type().plan(
            query,
            ready_providers=frozenset(
                {"dense", "graph", "temporal", "procedure", "outcome", "mystery"}
            ),
        )

        self.assertEqual(("lexical", "dense"), plan.provider_names)


class DenseProviderTests(unittest.IsolatedAsyncioTestCase):
    """Qdrant is an optional rank source; SQLite remains the validity authority."""

    def setUp(self) -> None:
        self._database_directory = tempfile.TemporaryDirectory()
        database_path = Path(self._database_directory.name) / "dense.sqlite3"
        self.connection = sqlite3.connect(database_path)
        self.connection.row_factory = sqlite3.Row
        self.connection.executescript(
            """
            CREATE TABLE projection_manifests (
                workspace_id TEXT NOT NULL,
                projection_name TEXT NOT NULL,
                generation INTEGER NOT NULL,
                status TEXT NOT NULL,
                source_event_count INTEGER NOT NULL,
                source_event_root_hash TEXT NOT NULL,
                cursor_recorded_at_us INTEGER,
                cursor_event_id TEXT,
                details_json TEXT NOT NULL
            );
            CREATE TABLE memory_events (
                event_id TEXT PRIMARY KEY,
                workspace_id TEXT NOT NULL,
                event_hash TEXT NOT NULL,
                recorded_at_us INTEGER NOT NULL
            );
            CREATE TABLE memory_records (
                record_id TEXT PRIMARY KEY,
                workspace_id TEXT NOT NULL,
                content TEXT NOT NULL,
                content_hash TEXT NOT NULL,
                source_event_id TEXT NOT NULL,
                updated_at_us INTEGER NOT NULL,
                deleted_at_us INTEGER
            );
            CREATE TABLE dense_projection_refs (
                workspace_id TEXT NOT NULL,
                provider_key TEXT NOT NULL,
                projection_generation INTEGER NOT NULL,
                record_id TEXT NOT NULL,
                content_hash TEXT NOT NULL,
                model_id TEXT NOT NULL,
                dimension INTEGER NOT NULL,
                state TEXT NOT NULL,
                updated_event_id TEXT NOT NULL,
                failure_code TEXT,
                updated_at_us INTEGER NOT NULL,
                PRIMARY KEY (
                    workspace_id, provider_key,
                    projection_generation, record_id
                )
            );
            """
        )
        self._event_hash_a = "1" * 64
        self.connection.execute(
            "INSERT INTO memory_events VALUES (?,?,?,?)",
            (EVENT_A, WORKSPACE_ID, self._event_hash_a, 100),
        )
        self.connection.commit()
        self.document_encoder = _ManifestDocumentEncoder()

    def tearDown(self) -> None:
        self.connection.close()
        self._database_directory.cleanup()

    def _dense_symbols(self):
        module = importlib.import_module("daem0nmcp.retrieval.providers")
        dense_provider = getattr(module, "DenseProvider", None)
        payload_builder = getattr(module, "build_dense_point_payload", None)
        client_builder = getattr(module, "create_qdrant_client", None)
        self.assertIsNotNone(dense_provider, "DenseProvider is missing")
        self.assertIsNotNone(payload_builder, "build_dense_point_payload is missing")
        self.assertIsNotNone(client_builder, "create_qdrant_client is missing")
        return dense_provider, payload_builder, client_builder

    def test_encoder_contract_binds_callable_identity(self):
        from daem0nmcp.retrieval.providers import dense_encoder_contract

        def first_encoder(_text: str) -> list[float]:
            return [0.0, 0.0, 0.0]

        def second_encoder(_text: str) -> list[float]:
            return [0.0, 0.0, 0.0]

        first = dense_encoder_contract(
            encoder=first_encoder,
            model_id=MODEL_ID,
            dimension=3,
            query_prefix=None,
        )
        second = dense_encoder_contract(
            encoder=second_encoder,
            model_id=MODEL_ID,
            dimension=3,
            query_prefix=None,
        )

        self.assertNotEqual(first["encoder_type"], second["encoder_type"])

    def test_query_contract_rejects_changed_artifact_under_same_model_name(self):
        from daem0nmcp.retrieval.providers import (
            dense_encoder_contract,
            dense_query_encoder_matches_contract,
        )

        document = _ManifestDocumentEncoder()
        document.artifact_fingerprint = "a" * 64
        query = _ManifestDocumentEncoder()
        query.artifact_fingerprint = "b" * 64
        contract = dense_encoder_contract(
            encoder=document,
            model_id=MODEL_ID,
            dimension=3,
            query_prefix=None,
        )

        self.assertFalse(
            dense_query_encoder_matches_contract(
                query_encoder=query,
                encoder_contract=contract,
                model_id=MODEL_ID,
                dimension=3,
                query_prefix=None,
            )
        )

    def _activate_manifest(
        self,
        *,
        generation: int = 4,
        model_id: str = MODEL_ID,
        provider_key: str = PROVIDER_KEY,
        dimension: int = 3,
        collection_prefix: str = "daem0nmcp",
        distance: str = "cosine",
        backend: str | None = None,
    ) -> None:
        event_root = hashlib.sha256(bytes.fromhex(self._event_hash_a)).hexdigest()
        self.connection.execute(
            "INSERT INTO projection_manifests VALUES (?,?,?,?,?,?,?,?,?)",
            (
                WORKSPACE_ID,
                "dense",
                generation,
                "active",
                1,
                event_root,
                100,
                EVENT_A,
                json.dumps(
                    _dense_manifest_details(
                        generation=generation,
                        model_id=model_id,
                        provider_key=provider_key,
                        dimension=dimension,
                        collection_prefix=collection_prefix,
                        distance=distance,
                        backend=backend,
                    ),
                    sort_keys=True,
                ),
            ),
        )
        self.connection.commit()

    def _add_record(
        self,
        record_id: str,
        content_hash: str,
        event_id: str,
        *,
        generation: int = 4,
    ) -> None:
        self.connection.execute(
            "INSERT INTO memory_records VALUES (?,?,?,?,?,?,NULL)",
            (
                record_id,
                WORKSPACE_ID,
                "canonical text stays in SQLite",
                content_hash,
                event_id,
                1_700_000_000_000_000,
            ),
        )
        self.connection.execute(
            "INSERT INTO dense_projection_refs VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                WORKSPACE_ID,
                PROVIDER_KEY,
                generation,
                record_id,
                content_hash,
                MODEL_ID,
                3,
                "ready",
                event_id,
                None,
                1_700_000_000_000_000,
            ),
        )
        self.connection.commit()

    @staticmethod
    def _payload(record_id: str, content_hash: str, **extra):
        payload = {
            "workspace_id": WORKSPACE_ID,
            "record_id": record_id,
            "content_hash": content_hash,
            "projection_generation": 4,
            "model_id": MODEL_ID,
        }
        payload.update(extra)
        return payload

    async def test_validates_points_against_manifest_refs_and_canonical_records(self):
        dense_provider, _, _ = self._dense_symbols()
        self._activate_manifest(collection_prefix="daemon")
        self._add_record(RECORD_A, CONTENT_A, EVENT_A)
        self._add_record(RECORD_B, CONTENT_B, EVENT_B)
        self.connection.commit()

        class FakeClient:
            def __init__(self):
                self.calls = []

            def query_points(self, **kwargs):
                self.calls.append(kwargs)
                return SimpleNamespace(
                    points=[
                        SimpleNamespace(
                            id=_dense_point_id(RECORD_A),
                            score=0.91,
                            payload=DenseProviderTests._payload(RECORD_A, CONTENT_A),
                        ),
                        SimpleNamespace(
                            id=_dense_point_id(RECORD_B),
                            score=0.90,
                            payload=DenseProviderTests._payload(
                                RECORD_B, CONTENT_STALE
                            ),
                        ),
                        SimpleNamespace(
                            id=_dense_point_id(RECORD_UNKNOWN),
                            score=0.89,
                            payload=DenseProviderTests._payload(
                                RECORD_UNKNOWN, CONTENT_STALE
                            ),
                        ),
                        SimpleNamespace(
                            id=_dense_point_id(RECORD_B),
                            score=0.88,
                            payload=DenseProviderTests._payload(
                                RECORD_B, CONTENT_B, content="must reject"
                            ),
                        ),
                        SimpleNamespace(
                            id=_dense_point_id(RECORD_B),
                            score=0.87,
                            payload=DenseProviderTests._payload(RECORD_B, CONTENT_B),
                        ),
                    ]
                )

        client = FakeClient()
        provider = dense_provider(
            self.connection,
            provider_key=PROVIDER_KEY,
            model_id=MODEL_ID,
            dimension=3,
            encoder=_ManifestDocumentEncoder(lambda _text: [0.25, 0.5, 0.75]),
            document_encoder=self.document_encoder,
            client=client,
            collection_prefix="daemon",
        )
        from daem0nmcp.retrieval.types import RetrievalQuery

        changes_before = self.connection.total_changes
        result = await provider.search(
            RetrievalQuery(
                workspace_id=WORKSPACE_ID,
                text="authentication design",
                candidate_limit=20,
            ),
            limit=2,
        )

        self.assertEqual("ready", result.status)
        self.assertEqual(4, result.manifest_generation)
        self.assertEqual(
            (RECORD_A, RECORD_B),
            tuple(candidate.evidence.record_id for candidate in result.candidates),
        )
        self.assertEqual((1, 2), tuple(c.rank for c in result.candidates))
        self.assertEqual(
            (EVENT_A, EVENT_B), tuple(c.evidence.event_id for c in result.candidates)
        )
        self.assertEqual(changes_before, self.connection.total_changes)
        self.assertEqual(
            [
                {
                    "collection_name": (
                        "daemon-ws_111111111111111111111111-qdrant-g4-1a1f4502024d"
                    ),
                    "query": [0.25, 0.5, 0.75],
                    "limit": 6,
                    "with_payload": True,
                    "with_vectors": False,
                }
            ],
            client.calls,
        )

    async def test_manifest_mismatch_and_missing_manifest_fail_closed(self):
        dense_provider, _, _ = self._dense_symbols()
        calls = []
        provider = dense_provider(
            self.connection,
            provider_key=PROVIDER_KEY,
            model_id=MODEL_ID,
            dimension=3,
            encoder=lambda text: calls.append("encoded") or [0.0, 0.0, 0.0],
            document_encoder=self.document_encoder,
            client=object(),
        )
        from daem0nmcp.retrieval.types import RetrievalQuery

        query = RetrievalQuery(workspace_id=WORKSPACE_ID, text="query")
        missing = await provider.search(query, 5)
        self._activate_manifest(model_id="other-model")
        mismatch = await provider.search(query, 5)

        self.assertEqual(
            ("unavailable", "DENSE_UNAVAILABLE"), (missing.status, missing.reason)
        )
        self.assertEqual(
            ("unavailable", "DENSE_MANIFEST_MISMATCH"),
            (mismatch.status, mismatch.reason),
        )
        self.assertEqual([], calls)

    async def test_manifest_rejects_document_encoder_or_builder_contract_drift(self):
        dense_provider, _, _ = self._dense_symbols()
        self._activate_manifest()
        calls = []

        class FakeClient:
            def query_points(self, **kwargs):
                calls.append(kwargs)
                return SimpleNamespace(points=[])

        provider = dense_provider(
            self.connection,
            provider_key=PROVIDER_KEY,
            model_id=MODEL_ID,
            dimension=3,
            encoder=lambda _text: [0.0, 0.0, 0.0],
            document_encoder=self.document_encoder,
            client=FakeClient(),
        )
        from daem0nmcp.retrieval.types import RetrievalQuery

        query = RetrievalQuery(workspace_id=WORKSPACE_ID, text="query")
        row = self.connection.execute(
            "SELECT details_json FROM projection_manifests"
        ).fetchone()
        details = json.loads(str(row[0]))
        details["encoder_contract"]["document_prefix"] = "changed: "
        self.connection.execute(
            "UPDATE projection_manifests SET details_json=?",
            (json.dumps(details, sort_keys=True),),
        )
        self.connection.commit()

        encoder_drift = await provider.search(query, 5)

        details = _dense_manifest_details()
        details["builder_contract_hash"] = "f" * 64
        self.connection.execute(
            "UPDATE projection_manifests SET details_json=?",
            (json.dumps(details, sort_keys=True),),
        )
        self.connection.commit()
        builder_drift = await provider.search(query, 5)

        self.document_encoder.backend = "fallback-backend"
        details = _dense_manifest_details(backend="configured-backend")
        self.connection.execute(
            "UPDATE projection_manifests SET details_json=?",
            (json.dumps(details, sort_keys=True),),
        )
        self.connection.commit()
        backend_drift = await provider.search(query, 5)

        self.assertEqual(
            ("unavailable", "DENSE_MANIFEST_MISMATCH"),
            (encoder_drift.status, encoder_drift.reason),
        )
        self.assertEqual(
            ("unavailable", "DENSE_MANIFEST_MISMATCH"),
            (builder_drift.status, builder_drift.reason),
        )
        self.assertEqual(
            ("unavailable", "DENSE_MANIFEST_MISMATCH"),
            (backend_drift.status, backend_drift.reason),
        )
        self.assertEqual([], calls)

    async def test_manifest_rejects_incompatible_query_encoder_contract(self):
        dense_provider, _, _ = self._dense_symbols()
        self._activate_manifest()
        calls = []

        class IncompatibleQueryEncoder:
            def encode(self, _text: str) -> list[float]:
                calls.append("encoded")
                return [0.0, 0.0, 0.0]

        class FakeClient:
            def query_points(self, **kwargs):
                calls.append(kwargs)
                return SimpleNamespace(points=[])

        result = await dense_provider(
            self.connection,
            provider_key=PROVIDER_KEY,
            model_id=MODEL_ID,
            dimension=3,
            encoder=IncompatibleQueryEncoder(),
            document_encoder=self.document_encoder,
            client=FakeClient(),
        ).search(
            __import__(
                "daem0nmcp.retrieval.types", fromlist=["RetrievalQuery"]
            ).RetrievalQuery(workspace_id=WORKSPACE_ID, text="query"),
            5,
        )

        self.assertEqual("unavailable", result.status)
        self.assertEqual("DENSE_MANIFEST_MISMATCH", result.reason)
        self.assertEqual((), result.candidates)
        self.assertEqual([], calls)

    async def test_query_encoder_backend_change_cannot_query_collection(self):
        dense_provider, _, _ = self._dense_symbols()
        configured_backend = "configured-backend"
        self._activate_manifest(backend=configured_backend)
        self.document_encoder.backend = configured_backend
        calls = []
        query_encoder = _ManifestDocumentEncoder()
        query_encoder.backend = configured_backend

        def fallback(_text: str) -> list[float]:
            query_encoder.backend = "fallback-backend"
            return [0.0, 0.0, 0.0]

        query_encoder._handler = fallback

        class FakeClient:
            def query_points(self, **kwargs):
                calls.append(kwargs)
                return SimpleNamespace(points=[])

        result = await dense_provider(
            self.connection,
            provider_key=PROVIDER_KEY,
            model_id=MODEL_ID,
            dimension=3,
            encoder=query_encoder,
            document_encoder=self.document_encoder,
            client=FakeClient(),
        ).search(
            __import__(
                "daem0nmcp.retrieval.types", fromlist=["RetrievalQuery"]
            ).RetrievalQuery(workspace_id=WORKSPACE_ID, text="query"),
            5,
        )

        self.assertEqual("unavailable", result.status)
        self.assertEqual("DENSE_MANIFEST_MISMATCH", result.reason)
        self.assertEqual((), result.candidates)
        self.assertEqual([], calls)

    async def test_stale_active_manifest_is_explicitly_degraded(self):
        dense_provider, _, _ = self._dense_symbols()
        self._activate_manifest()
        row = self.connection.execute(
            "SELECT details_json FROM projection_manifests WHERE status='active'"
        ).fetchone()
        details = json.loads(row[0])
        details["rebuild_required_event_id"] = EVENT_B
        self.connection.execute(
            "UPDATE projection_manifests SET details_json=? WHERE status='active'",
            (json.dumps(details),),
        )
        self.connection.commit()

        result = await dense_provider(
            self.connection,
            provider_key=PROVIDER_KEY,
            model_id=MODEL_ID,
            dimension=3,
            encoder=lambda _text: (_ for _ in ()).throw(
                AssertionError("empty stale query must not encode")
            ),
            document_encoder=self.document_encoder,
            client=object(),
        ).search(
            __import__(
                "daem0nmcp.retrieval.types", fromlist=["RetrievalQuery"]
            ).RetrievalQuery(workspace_id=WORKSPACE_ID, text=""),
            5,
        )

        self.assertEqual("degraded", result.status)
        self.assertEqual("DENSE_REBUILD_REQUIRED", result.reason)
        self.assertEqual(4, result.manifest_generation)

    async def test_nonempty_stale_manifest_never_encodes_or_queries(self):
        dense_provider, _, _ = self._dense_symbols()
        self._activate_manifest()
        row = self.connection.execute(
            "SELECT details_json FROM projection_manifests WHERE status='active'"
        ).fetchone()
        details = json.loads(row[0])
        details["rebuild_required_event_id"] = EVENT_B
        details["rebuild_required_at_us"] = 200
        self.connection.execute(
            "UPDATE projection_manifests SET details_json=? WHERE status='active'",
            (json.dumps(details),),
        )
        self.connection.commit()
        calls = []

        class FakeClient:
            def query_points(self, **kwargs):
                calls.append(("client", kwargs))
                return SimpleNamespace(points=[])

        result = await dense_provider(
            self.connection,
            provider_key=PROVIDER_KEY,
            model_id=MODEL_ID,
            dimension=3,
            encoder=lambda _text: calls.append(("encoder", None)) or [0.0, 0.0, 0.0],
            document_encoder=self.document_encoder,
            client=FakeClient(),
        ).search(
            __import__(
                "daem0nmcp.retrieval.types", fromlist=["RetrievalQuery"]
            ).RetrievalQuery(workspace_id=WORKSPACE_ID, text="stale query"),
            5,
        )

        self.assertEqual("degraded", result.status)
        self.assertEqual("DENSE_REBUILD_REQUIRED", result.reason)
        self.assertEqual((), result.candidates)
        self.assertEqual([], calls)

    async def test_canonical_source_advance_never_queries_stale_generation(self):
        dense_provider, _, _ = self._dense_symbols()
        self._activate_manifest()
        self.connection.execute(
            "INSERT INTO memory_events VALUES (?,?,?,?)",
            (EVENT_B, WORKSPACE_ID, "2" * 64, 200),
        )
        self.connection.commit()
        calls = []

        class FakeClient:
            def query_points(self, **kwargs):
                calls.append(("client", kwargs))
                return SimpleNamespace(points=[])

        result = await dense_provider(
            self.connection,
            provider_key=PROVIDER_KEY,
            model_id=MODEL_ID,
            dimension=3,
            encoder=lambda _text: calls.append(("encoder", None)) or [0.0, 0.0, 0.0],
            document_encoder=self.document_encoder,
            client=FakeClient(),
        ).search(
            __import__(
                "daem0nmcp.retrieval.types", fromlist=["RetrievalQuery"]
            ).RetrievalQuery(workspace_id=WORKSPACE_ID, text="advanced query"),
            5,
        )

        self.assertEqual("degraded", result.status)
        self.assertEqual("DENSE_REBUILD_REQUIRED", result.reason)
        self.assertEqual((), result.candidates)
        self.assertEqual([], calls)

    async def test_collection_schema_and_distance_are_manifest_bound(self):
        dense_provider, _, _ = self._dense_symbols()
        self._activate_manifest(collection_prefix="other", distance="dot")
        calls = []

        class FakeClient:
            def query_points(self, **kwargs):
                calls.append(kwargs)
                return SimpleNamespace(points=[])

        result = await dense_provider(
            self.connection,
            provider_key=PROVIDER_KEY,
            model_id=MODEL_ID,
            dimension=3,
            encoder=lambda text: [0.0, 0.0, 0.0],
            document_encoder=self.document_encoder,
            client=FakeClient(),
        ).search(
            __import__(
                "daem0nmcp.retrieval.types", fromlist=["RetrievalQuery"]
            ).RetrievalQuery(workspace_id=WORKSPACE_ID, text="query"),
            5,
        )

        self.assertEqual("unavailable", result.status)
        self.assertEqual("DENSE_MANIFEST_MISMATCH", result.reason)
        self.assertEqual([], calls)

    async def test_dense_timeout_is_off_loop_and_degrades_only_the_query(self):
        dense_provider, _, _ = self._dense_symbols()
        self._activate_manifest()
        started = threading.Event()
        release = threading.Event()
        heartbeat = []

        def blocking_encoder(text):
            started.set()
            release.wait(timeout=2)
            return [0.0, 0.0, 0.0]

        provider = dense_provider(
            self.connection,
            provider_key=PROVIDER_KEY,
            model_id=MODEL_ID,
            dimension=3,
            encoder=_ManifestDocumentEncoder(blocking_encoder),
            document_encoder=self.document_encoder,
            client=object(),
            timeout_seconds=0.02,
        )
        from daem0nmcp.retrieval.types import RetrievalQuery

        async def pulse():
            for _ in range(3):
                await asyncio.sleep(0.005)
                heartbeat.append("tick")

        pulse_task = asyncio.create_task(pulse())
        try:
            result = await provider.search(
                RetrievalQuery(workspace_id=WORKSPACE_ID, text="query"),
                5,
            )
        finally:
            release.set()
            await pulse_task

        self.assertTrue(started.is_set())
        self.assertTrue(heartbeat)
        self.assertEqual("degraded", result.status)
        self.assertEqual("DENSE_PROVIDER_TIMEOUT", result.reason)

    async def test_timeout_defers_owned_client_close_until_worker_exits(self):
        dense_provider, _, _ = self._dense_symbols()
        self._activate_manifest()
        started = threading.Event()
        release = threading.Event()
        closed = threading.Event()

        class BlockingClient:
            def __init__(self):
                self.in_use = False
                self.closed = 0
                self.closed_while_in_use = False

            def query_points(self, **_kwargs):
                self.in_use = True
                started.set()
                release.wait(timeout=2)
                self.in_use = False
                return SimpleNamespace(points=[])

            def close(self):
                self.closed_while_in_use = self.in_use
                self.closed += 1
                closed.set()

        client = BlockingClient()
        provider = dense_provider(
            self.connection,
            provider_key=PROVIDER_KEY,
            model_id=MODEL_ID,
            dimension=3,
            encoder=_ManifestDocumentEncoder(),
            document_encoder=self.document_encoder,
            qdrant_path="unused-local-path",
            client_factory=lambda **_kwargs: client,
            timeout_seconds=0.02,
        )
        from daem0nmcp.retrieval.types import RetrievalQuery

        result = await provider.search(
            RetrievalQuery(workspace_id=WORKSPACE_ID, text="query"),
            5,
        )
        self.assertTrue(started.is_set())
        self.assertEqual("DENSE_PROVIDER_TIMEOUT", result.reason)

        provider.close()
        provider.close()
        self.assertEqual(0, client.closed)
        release.set()
        self.assertTrue(await asyncio.to_thread(closed.wait, 2))
        self.assertEqual(1, client.closed)
        self.assertFalse(client.closed_while_in_use)

    async def test_encoder_and_client_failures_degrade_only_the_query(self):
        dense_provider, _, _ = self._dense_symbols()
        self._activate_manifest()
        from daem0nmcp.retrieval.types import RetrievalQuery

        query = RetrievalQuery(workspace_id=WORKSPACE_ID, text="query")

        def broken_encoder(text):
            raise RuntimeError("model path and secret must not escape")

        encoder_failure = await dense_provider(
            self.connection,
            provider_key=PROVIDER_KEY,
            model_id=MODEL_ID,
            dimension=3,
            encoder=_ManifestDocumentEncoder(broken_encoder),
            document_encoder=self.document_encoder,
            client=object(),
        ).search(query, 5)

        client_factory_calls = []

        def missing_client(**kwargs):
            client_factory_calls.append(kwargs)
            raise ModuleNotFoundError("optional qdrant_client is absent")

        client_failure_provider = dense_provider(
            self.connection,
            provider_key=PROVIDER_KEY,
            model_id=MODEL_ID,
            dimension=3,
            encoder=_ManifestDocumentEncoder(),
            document_encoder=self.document_encoder,
            qdrant_path="unused-local-path",
            client_factory=missing_client,
        )
        client_failure = await client_failure_provider.search(query, 5)

        self.assertEqual(
            ("degraded", "DENSE_ENCODER_FAILED"),
            (encoder_failure.status, encoder_failure.reason),
        )
        self.assertEqual(
            ("degraded", "DENSE_PROVIDER_FAILED"),
            (client_failure.status, client_failure.reason),
        )
        self.assertEqual([{"path": "unused-local-path"}], client_factory_calls)

    async def test_lazy_client_initializes_once_and_only_owned_resources_close(self):
        dense_provider, _, _ = self._dense_symbols()
        self._activate_manifest()
        from daem0nmcp.retrieval.types import RetrievalQuery

        class Resource:
            def __init__(self):
                self.closed = 0

            def close(self):
                self.closed += 1

        class Client(Resource):
            def query_points(self, **_kwargs):
                return SimpleNamespace(points=[])

        created: list[Client] = []
        factory_started = threading.Event()
        factory_release = threading.Event()

        def factory(**_kwargs):
            factory_started.set()
            factory_release.wait(timeout=2)
            client = Client()
            created.append(client)
            return client

        external_encoder = _ManifestDocumentEncoder()
        provider = dense_provider(
            self.connection,
            provider_key=PROVIDER_KEY,
            model_id=MODEL_ID,
            dimension=3,
            encoder=external_encoder,
            document_encoder=self.document_encoder,
            qdrant_path="unused-local-path",
            client_factory=factory,
        )
        query = RetrievalQuery(workspace_id=WORKSPACE_ID, text="query")
        first = asyncio.create_task(provider.search(query, 5))
        await asyncio.to_thread(factory_started.wait, 2)
        second = asyncio.create_task(provider.search(query, 5))
        await asyncio.sleep(0.05)
        factory_release.set()
        results = await asyncio.gather(first, second)
        provider.close()
        provider.close()

        self.assertEqual(["ready", "ready"], [item.status for item in results])
        self.assertEqual(1, len(created))
        self.assertEqual(1, created[0].closed)
        self.assertFalse(hasattr(external_encoder, "closed"))

        external_client = Client()
        externally_owned = dense_provider(
            self.connection,
            provider_key=PROVIDER_KEY,
            model_id=MODEL_ID,
            dimension=3,
            encoder=external_encoder,
            document_encoder=self.document_encoder,
            client=external_client,
        )
        externally_owned.close()
        self.assertEqual(0, external_client.closed)

    async def test_streaming_client_failure_during_point_iteration_degrades(self):
        dense_provider, _, _ = self._dense_symbols()
        self._activate_manifest()
        from daem0nmcp.retrieval.types import RetrievalQuery

        class BrokenPoints:
            def __iter__(self):
                yield SimpleNamespace(
                    id=_dense_point_id(RECORD_UNKNOWN),
                    score=0.5,
                    payload=self,
                )
                raise RuntimeError("remote stream failed with secret material")

        class BrokenClient:
            def query_points(self, **kwargs):
                return SimpleNamespace(points=BrokenPoints())

        result = await dense_provider(
            self.connection,
            provider_key=PROVIDER_KEY,
            model_id=MODEL_ID,
            dimension=3,
            encoder=_ManifestDocumentEncoder(),
            document_encoder=self.document_encoder,
            client=BrokenClient(),
        ).search(
            RetrievalQuery(workspace_id=WORKSPACE_ID, text="query"),
            5,
        )

        self.assertEqual(
            ("degraded", "DENSE_PROVIDER_FAILED", ()),
            (result.status, result.reason, result.candidates),
        )

    def test_point_identity_and_payload_are_deterministic_and_content_free(self):
        _, payload_builder, _ = self._dense_symbols()

        point_id, payload = payload_builder(
            workspace_id=WORKSPACE_ID,
            record_id=RECORD_A,
            content_hash=CONTENT_A,
            projection_generation=4,
            model_id=MODEL_ID,
        )

        self.assertEqual(_dense_point_id(RECORD_A), point_id)
        self.assertEqual(5, uuid.UUID(point_id).version)
        self.assertEqual(RECORD_A, payload["record_id"])
        self.assertEqual(
            {
                "workspace_id": WORKSPACE_ID,
                "record_id": RECORD_A,
                "content_hash": CONTENT_A,
                "projection_generation": 4,
                "model_id": MODEL_ID,
            },
            payload,
        )
        self.assertNotIn("content", payload)
        self.assertNotIn("vector", payload)

    def test_remote_client_precedes_local_path_and_local_mode_uses_only_path(self):
        _, _, client_builder = self._dense_symbols()
        calls = []

        def factory(**kwargs):
            calls.append(kwargs)
            return "client"

        with tempfile.TemporaryDirectory() as temporary:
            local_path = Path(temporary) / "must-not-be-created"
            remote = client_builder(
                qdrant_url="https://qdrant.example.test",
                qdrant_api_key="api-secret",
                qdrant_path=local_path,
                timeout_seconds=2.5,
                client_factory=factory,
            )
            self.assertEqual("client", remote)
            self.assertFalse(local_path.exists())

            local = client_builder(
                qdrant_url=None,
                qdrant_api_key="ignored-without-remote",
                qdrant_path=local_path,
                timeout_seconds=99.0,
                client_factory=factory,
            )
            self.assertEqual("client", local)

        self.assertEqual(
            [
                {
                    "url": "https://qdrant.example.test",
                    "api_key": "api-secret",
                    "timeout": 2.5,
                },
                {"path": str(local_path)},
            ],
            calls,
        )

    def test_client_builder_maps_oversized_timeout_to_value_error(self):
        _, _, client_builder = self._dense_symbols()

        try:
            client_builder(
                qdrant_url="https://qdrant.example.test",
                qdrant_api_key=None,
                qdrant_path=None,
                timeout_seconds=10**400,
                client_factory=lambda **kwargs: kwargs,
            )
        except Exception as exc:
            self.assertIs(ValueError, type(exc))
        else:
            self.fail("oversized Qdrant timeout was accepted")


if __name__ == "__main__":
    unittest.main()
