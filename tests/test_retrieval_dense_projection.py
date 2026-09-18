"""Dense projection lifecycle contracts with deterministic local fakes."""

from __future__ import annotations

import asyncio
import copy
import hashlib
import importlib.util
import json
import sqlite3
import tempfile
import threading
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

WORKSPACE_ID = "ws_0123456789abcdef01234567"


def _schema_migrations():
    path = (
        Path(__file__).resolve().parents[1] / "daem0nmcp" / "migrations" / "schema.py"
    )
    spec = importlib.util.spec_from_file_location("dense_test_schema", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.MIGRATIONS


def _apply_migration(connection: sqlite3.Connection, version: int) -> None:
    migration = next(item for item in _schema_migrations() if item[0] == version)
    for statement in migration[2]:
        connection.execute(statement)


class DeterministicEncoder:
    artifact_fingerprint = hashlib.sha256(b"deterministic-test-model").hexdigest()

    def encode(self, text: str) -> list[float]:
        return [
            float(len(text)),
            float(sum(text.encode("utf-8")) % 997),
            float(text.count(" ") + 1),
        ]


class ContractEncoder:
    def __init__(self, prefix: str) -> None:
        self.model_id = "deterministic-test-model"
        self.dimension = 3
        self.prefix = prefix
        self.backend = "test-backend"
        self.max_seq_length = 512
        self.artifact_fingerprint = hashlib.sha256(
            f"contract:{prefix}".encode()
        ).hexdigest()

    def encode(self, text: str) -> list[float]:
        value = f"{self.prefix}{text}"
        return [
            float(len(value)),
            float(sum(value.encode("utf-8")) % 997),
            float(value.count(" ") + 1),
        ]


class FakeQdrantClient:
    """Small behavioral fake for the Qdrant collection boundary."""

    def __init__(self) -> None:
        self.collections: dict[str, dict[str, object]] = {}
        self.corrupt_retrieval = False
        self.object_retrieval = False

    def collection_exists(self, collection_name: str) -> bool:
        return collection_name in self.collections

    def delete_collection(self, collection_name: str) -> None:
        self.collections.pop(collection_name, None)

    def create_collection(
        self, *, collection_name: str, vectors_config: object
    ) -> None:
        self.collections[collection_name] = {
            "vectors_config": vectors_config,
            "points": {},
        }

    def upsert(
        self, *, collection_name: str, points: list[dict[str, object]], wait: bool
    ) -> None:
        del wait
        stored = self.collections[collection_name]["points"]
        assert isinstance(stored, dict)
        for point in points:
            stored[str(point["id"])] = {
                "id": point["id"],
                "payload": dict(point["payload"]),
                "vector": list(point["vector"]),
            }

    def retrieve(
        self,
        *,
        collection_name: str,
        ids: list[str],
        with_payload: bool,
        with_vectors: bool,
    ) -> list[object]:
        del with_payload, with_vectors
        stored = self.collections[collection_name]["points"]
        assert isinstance(stored, dict)
        points = [
            copy.deepcopy(stored[point_id]) for point_id in ids if point_id in stored
        ]
        if self.corrupt_retrieval and points:
            payload = points[0]["payload"]
            assert isinstance(payload, dict)
            payload["content_hash"] = "f" * 64
        if self.object_retrieval:
            return [SimpleNamespace(**point) for point in points]
        return points

    def count(self, *, collection_name: str, exact: bool) -> dict[str, int]:
        del exact
        stored = self.collections[collection_name]["points"]
        assert isinstance(stored, dict)
        return {"count": len(stored)}


class FakeDistance:
    COSINE = "Cosine"


class FakeVectorParams:
    def __init__(self, *, size: int, distance: object) -> None:
        self.size = size
        self.distance = distance


class FakePointStruct:
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


class FakeQdrantModels:
    Distance = FakeDistance
    PointStruct = FakePointStruct
    VectorParams = FakeVectorParams


class LegacyStrictQdrantClient(FakeQdrantClient):
    """Qdrant 1.7-shaped fake that rejects raw model dictionaries."""

    collection_exists = None

    def get_collections(self) -> SimpleNamespace:
        return SimpleNamespace(
            collections=[SimpleNamespace(name=name) for name in self.collections]
        )

    def create_collection(
        self, *, collection_name: str, vectors_config: object
    ) -> None:
        if not isinstance(vectors_config, FakeVectorParams):
            raise TypeError("anonymous vectors require VectorParams")
        super().create_collection(
            collection_name=collection_name,
            vectors_config=vectors_config,
        )

    def upsert(self, *, collection_name: str, points: list[object], wait: bool) -> None:
        converted: list[dict[str, object]] = []
        for point in points:
            if not isinstance(point, FakePointStruct):
                raise TypeError("points require PointStruct")
            converted.append(
                {
                    "id": point.id,
                    "vector": point.vector,
                    "payload": point.payload,
                }
            )
        super().upsert(
            collection_name=collection_name,
            points=converted,
            wait=wait,
        )


class DenseProjectionTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.database_path = Path(directory.name) / "dense.sqlite3"
        self.connection = sqlite3.connect(self.database_path)
        self.addCleanup(self.connection.close)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys=ON")
        for version in (16, 17, 18, 31, 32):
            _apply_migration(self.connection, version)
        self.connection.commit()
        self.client = FakeQdrantClient()
        self.encoder = DeterministicEncoder()
        self._record_number = 0

    @staticmethod
    def _record(content: str) -> dict[str, object]:
        return {
            "record_type": "decision",
            "legacy_type": None,
            "content": content,
            "rationale": None,
            "context": {},
            "tags": [],
            "file_path": None,
            "file_path_relative": None,
            "keywords": None,
            "is_permanent": False,
            "pinned": False,
            "archived": False,
            "outcome": None,
            "worked": None,
            "recall_count": 0,
            "surprise_score": None,
            "importance_score": None,
            "source_client": "dense-test",
            "source_model": None,
            "deleted_at_us": None,
        }

    def _append(self, suffix: str, content: str) -> str:
        from daem0nmcp.event_store import EventCommand, EventStore

        self._record_number += 1
        record_id = "mem_" + (suffix * 64 if len(suffix) == 1 else suffix)
        EventStore(self.connection).append_and_project(
            EventCommand(
                workspace_id=WORKSPACE_ID,
                stream_id=record_id,
                stream_kind="memory",
                event_type="memory.created",
                occurred_at_us=100 + self._record_number,
                recorded_at_us=200 + self._record_number,
                actor_type="system",
                payload={"record": self._record(content)},
            )
        )
        self.connection.commit()
        return record_id

    def _builder(self):
        from daem0nmcp.retrieval.dense_projection import DenseProjectionBuilder

        return DenseProjectionBuilder(
            self.connection,
            provider_key="local",
            model_id="deterministic-test-model",
            dimension=3,
            encoder=self.encoder,
            client=self.client,
            collection_prefix="test-dense",
            clock_us=lambda: 500,
        )

    def test_repeated_activation_retains_one_inactive_and_durably_collects_older(self):
        from daem0nmcp.retrieval.dense_generation_gc import (
            DenseGenerationGarbageCollector,
        )

        collections: dict[int, str] = {}
        for index, suffix in enumerate(("a", "b", "c"), start=1):
            self._append(suffix, f"dense generation {index}")
            collections[index] = self._builder().rebuild(WORKSPACE_ID).collection_name

        self.assertEqual(
            [(1, "queued")],
            [
                tuple(row)
                for row in self.connection.execute(
                    "SELECT projection_generation,status FROM "
                    "dense_generation_gc_jobs ORDER BY projection_generation"
                )
            ],
        )
        result = DenseGenerationGarbageCollector(
            self.connection,
            client=self.client,
            clock_us=lambda: 501,
            token_factory=lambda: "test-claim",
        ).run_slice(max_generations=2)

        self.assertEqual(
            [(1, "succeeded")], [(item.generation, item.status) for item in result]
        )
        self.assertNotIn(collections[1], self.client.collections)
        self.assertIn(collections[2], self.client.collections)
        self.assertIn(collections[3], self.client.collections)
        self.assertEqual(
            [(2, "ready"), (3, "active")],
            [
                tuple(row)
                for row in self.connection.execute(
                    "SELECT generation,status FROM projection_manifests "
                    "WHERE projection_name='dense' ORDER BY generation"
                )
            ],
        )

    def test_live_generation_lease_defers_gc_and_fenced_release_unblocks_retry(self):
        from daem0nmcp.retrieval.dense_generation_gc import (
            DenseGenerationGarbageCollector,
            acquire_active_generation_lease,
            release_generation_lease,
            renew_generation_lease,
        )

        self._append("d", "leased generation")
        first = self._builder().rebuild(WORKSPACE_ID)
        lease = acquire_active_generation_lease(
            self.connection,
            workspace_id=WORKSPACE_ID,
            provider_key="local",
            owner_id="export-test",
            clock_us=lambda: 500,
            lease_duration_us=100,
        )
        self._append("e", "successor generation")
        self._builder().rebuild(WORKSPACE_ID)
        self._append("f", "second successor generation")
        self._builder().rebuild(WORKSPACE_ID)
        renewed = renew_generation_lease(
            self.connection,
            lease,
            clock_us=lambda: 501,
            lease_duration_us=100,
        )
        self.assertEqual(601, renewed.expires_at_us)
        self.assertIsNone(
            self.connection.execute(
                "SELECT 1 FROM dense_generation_gc_jobs WHERE projection_generation=1"
            ).fetchone()
        )

        collector = DenseGenerationGarbageCollector(
            self.connection,
            client=self.client,
            clock_us=lambda: 501,
            token_factory=lambda: "lease-test-claim",
        )
        self.assertEqual((), collector.run_slice())
        self.assertIn(first.collection_name, self.client.collections)
        self.assertTrue(
            release_generation_lease(self.connection, renewed, clock_us=lambda: 502)
        )
        resumed = DenseGenerationGarbageCollector(
            self.connection,
            client=self.client,
            clock_us=lambda: 601,
            token_factory=lambda: "lease-test-retry",
        ).run_slice()
        self.assertEqual("succeeded", resumed[0].status)
        self.assertNotIn(first.collection_name, self.client.collections)

    def test_gc_recovers_when_provider_delete_succeeds_but_ack_is_lost(self):
        from daem0nmcp.retrieval.dense_generation_gc import (
            DenseGenerationGarbageCollector,
        )

        for suffix in ("7", "8", "9"):
            self._append(suffix, f"ambiguous provider delete {suffix}")
            self._builder().rebuild(WORKSPACE_ID)
        original_delete = self.client.delete_collection
        lost_ack = True

        def delete_then_fail(collection_name: str) -> None:
            nonlocal lost_ack
            original_delete(collection_name)
            if lost_ack:
                lost_ack = False
                raise RuntimeError("provider acknowledgement was lost")

        self.client.delete_collection = delete_then_fail  # type: ignore[method-assign]
        first = DenseGenerationGarbageCollector(
            self.connection,
            client=self.client,
            clock_us=lambda: 500,
            retry_delay_us=1,
            token_factory=lambda: "ambiguous-first",
        ).run_slice()
        self.assertEqual("queued", first[0].status)
        self.assertEqual("DENSE_GENERATION_GC_PROVIDER_UNAVAILABLE", first[0].reason)

        second = DenseGenerationGarbageCollector(
            self.connection,
            client=self.client,
            clock_us=lambda: 502,
            retry_delay_us=1,
            token_factory=lambda: "ambiguous-second",
        ).run_slice()
        self.assertEqual("succeeded", second[0].status)
        self.assertEqual(
            0,
            self.connection.execute(
                "SELECT COUNT(*) FROM dense_generation_gc_jobs"
            ).fetchone()[0],
        )

    def test_gc_dead_letter_blocks_reactivation_until_explicit_retry_or_cancel(self):
        from daem0nmcp.retrieval.dense_generation_gc import (
            cancel_queued_gc_for_reactivation,
            dense_generation_gc_diagnostics,
            retry_dense_generation_gc,
        )

        for suffix in ("4", "5", "6"):
            self._append(suffix, f"reactivation fence {suffix}")
            self._builder().rebuild(WORKSPACE_ID)
        self.connection.execute(
            "UPDATE dense_generation_gc_jobs SET status='dead_letter',attempts=3,"
            "last_error_code='DENSE_GENERATION_GC_PROVIDER_UNAVAILABLE'"
        )
        self.connection.commit()
        with self.assertRaisesRegex(
            sqlite3.IntegrityError, "DENSE_GENERATION_GC_ENROLLED"
        ):
            self.connection.execute("BEGIN IMMEDIATE")
            self.connection.execute(
                "UPDATE projection_manifests SET status='ready' WHERE "
                "workspace_id=? AND projection_name='dense' AND status='active'",
                (WORKSPACE_ID,),
            )
            self.connection.execute(
                "UPDATE projection_manifests SET status='active' WHERE "
                "workspace_id=? AND projection_name='dense' AND generation=1",
                (WORKSPACE_ID,),
            )
        self.connection.rollback()
        self.assertEqual(
            {"dead_letter": 1},
            dense_generation_gc_diagnostics(self.connection, workspace_id=WORKSPACE_ID)[
                "counts"
            ],
        )
        self.assertTrue(
            retry_dense_generation_gc(
                self.connection,
                workspace_id=WORKSPACE_ID,
                provider_key="local",
                generation=1,
                clock_us=lambda: 600,
            )
        )
        self.connection.execute("BEGIN IMMEDIATE")
        self.assertTrue(
            cancel_queued_gc_for_reactivation(
                self.connection,
                workspace_id=WORKSPACE_ID,
                provider_key="local",
                generation=1,
            )
        )
        self.connection.execute(
            "UPDATE projection_manifests SET status='ready' WHERE "
            "workspace_id=? AND projection_name='dense' AND status='active'",
            (WORKSPACE_ID,),
        )
        self.connection.execute(
            "UPDATE projection_manifests SET status='active' WHERE "
            "workspace_id=? AND projection_name='dense' AND generation=1",
            (WORKSPACE_ID,),
        )
        self.connection.commit()

    def test_gc_cancellation_after_final_provider_check_preserves_database_state(self):
        from daem0nmcp.retrieval.dense_generation_gc import (
            DenseGenerationGarbageCollector,
        )

        for suffix in ("0", "1", "2"):
            self._append(suffix, f"blocked absence check {suffix}")
            self._builder().rebuild(WORKSPACE_ID)
        cancellation = threading.Event()
        final_check_entered = threading.Event()
        release_final_check = threading.Event()
        exists_calls = 0
        original_exists = self.client.collection_exists

        def blocking_exists(collection_name: str) -> bool:
            nonlocal exists_calls
            exists_calls += 1
            value = original_exists(collection_name)
            if exists_calls == 2:
                final_check_entered.set()
                release_final_check.wait(timeout=2)
            return value

        self.client.collection_exists = blocking_exists  # type: ignore[method-assign]

        def cancel_during_check() -> None:
            self.assertTrue(final_check_entered.wait(timeout=2))
            cancellation.set()
            release_final_check.set()

        canceller = threading.Thread(target=cancel_during_check)
        canceller.start()
        result = DenseGenerationGarbageCollector(
            self.connection,
            client=self.client,
            clock_us=lambda: 500,
            token_factory=lambda: "cancel-final-check",
            cancelled=cancellation.is_set,
        ).run_slice()
        canceller.join(timeout=2)

        self.assertEqual("queued", result[0].status)
        self.assertEqual("DENSE_GENERATION_GC_CANCELLED", result[0].reason)
        self.assertEqual(
            ("queued", 0),
            tuple(
                self.connection.execute(
                    "SELECT status,attempts FROM dense_generation_gc_jobs "
                    "WHERE projection_generation=1"
                ).fetchone()
            ),
        )
        self.assertIsNotNone(
            self.connection.execute(
                "SELECT 1 FROM projection_manifests WHERE projection_name='dense' "
                "AND generation=1 AND status='ready'"
            ).fetchone()
        )
        self.assertGreater(
            self.connection.execute(
                "SELECT COUNT(*) FROM dense_projection_refs "
                "WHERE projection_generation=1"
            ).fetchone()[0],
            0,
        )

    def test_close_releases_only_resources_owned_by_builder(self):
        from daem0nmcp.retrieval.dense_projection import DenseProjectionBuilder

        class ClosableEncoder(DeterministicEncoder):
            def __init__(self) -> None:
                self.close_count = 0

            def close(self) -> None:
                self.close_count += 1

        class ClosableClient(FakeQdrantClient):
            def __init__(self) -> None:
                super().__init__()
                self.close_count = 0

            def close(self) -> None:
                self.close_count += 1

        external_encoder = ClosableEncoder()
        external_client = ClosableClient()
        external = DenseProjectionBuilder(
            self.connection,
            provider_key="local",
            model_id="deterministic-test-model",
            dimension=3,
            encoder=external_encoder,
            client=external_client,
        )
        external.close()
        self.assertEqual(0, external_encoder.close_count)
        self.assertEqual(0, external_client.close_count)

        owned_encoder = ClosableEncoder()
        owned_client = ClosableClient()
        owned = DenseProjectionBuilder(
            self.connection,
            provider_key="local",
            model_id="deterministic-test-model",
            dimension=3,
            encoder=owned_encoder,
            qdrant_path=self.database_path.parent / "vectors",
            client_factory=lambda **_kwargs: owned_client,
            own_encoder=True,
        )
        self.assertIs(owned_client, owned._get_client())
        owned.close()
        owned.close()
        self.assertEqual(1, owned_encoder.close_count)
        self.assertEqual(1, owned_client.close_count)

    def test_real_local_qdrant_normalization_preserves_valid_projection(self):
        try:
            from qdrant_client import QdrantClient
        except ImportError:
            self.skipTest("optional local Qdrant profile is unavailable")
        from daem0nmcp.retrieval.dense_projection import DenseProjectionBuilder

        self._append("e", "real cosine float32 representation")
        client = QdrantClient(location=":memory:")
        try:
            builder = DenseProjectionBuilder(
                self.connection,
                provider_key="local",
                model_id="deterministic-test-model",
                dimension=3,
                encoder=self.encoder,
                client=client,
            )
            result = builder.rebuild(WORKSPACE_ID)
            self.assertEqual("active", result.status)
            self.assertTrue(builder.active_is_current(WORKSPACE_ID))
        finally:
            client.close()

    def test_cosine_rounding_does_not_admit_corrupt_or_malformed_vectors(self):
        from daem0nmcp.retrieval.vector_validation import cosine_vectors_match

        self.assertTrue(
            cosine_vectors_match([0.6000000238418579, 0.800000011920929], [3.0, 4.0])
        )
        for bad in (
            [0.601, 0.8],
            [-0.6, -0.8],
            [0, 0],
            [0.6],
            [float("nan"), 0.8],
            [float("inf"), 0.8],
            [True, 0.8],
            "0.6,0.8",
        ):
            with self.subTest(vector=bad):
                self.assertFalse(cosine_vectors_match(bad, [3.0, 4.0]))

    def test_build_activates_only_after_points_and_refs_match_canonical_records(self):
        first_id = self._append("1", "first dense record")
        second_id = self._append("2", "second dense record")

        result = self._builder().rebuild(WORKSPACE_ID)

        self.assertEqual("active", result.status)
        self.assertEqual(1, result.generation)
        self.assertEqual(2, result.row_count)
        manifest = self.connection.execute(
            "SELECT status,row_count,source_event_count,source_event_root_hash,"
            "details_json "
            "FROM projection_manifests WHERE manifest_id=?",
            (result.staging_manifest_id,),
        ).fetchone()
        self.assertIsNotNone(manifest)
        self.assertEqual(("active", 2, 2), tuple(manifest[:3]))
        expected_root = hashlib.sha256(
            b"".join(
                bytes.fromhex(str(row[0]))
                for row in self.connection.execute(
                    "SELECT event_hash FROM memory_events "
                    "WHERE workspace_id=? ORDER BY event_id",
                    (WORKSPACE_ID,),
                )
            )
        ).hexdigest()
        self.assertEqual(expected_root, result.source_event_root_hash)
        self.assertEqual(expected_root, manifest[3])
        details = json.loads(str(manifest[4]))
        self.assertEqual(result.content_digest, details["content_digest"])
        self.assertEqual(result.build_config_hash, details["build_config_hash"])
        self.assertEqual(result.collection_name, details["collection_name"])

        refs = self.connection.execute(
            "SELECT record_id,content_hash,model_id,dimension,state,updated_event_id "
            "FROM dense_projection_refs ORDER BY record_id"
        ).fetchall()
        self.assertEqual([first_id, second_id], [row[0] for row in refs])
        self.assertTrue(all(row[2:] and row[4] == "ready" for row in refs))
        dense_columns = {
            str(row[1]).lower()
            for row in self.connection.execute(
                "PRAGMA table_info(dense_projection_refs)"
            )
        }
        self.assertFalse(dense_columns & {"content", "vector", "embedding", "blob"})
        points = self.client.collections[result.collection_name]["points"]
        self.assertIsInstance(points, dict)
        self.assertEqual(2, len(points))
        for point_id, point in points.items():
            self.assertEqual(str(uuid.UUID(point_id)), point_id)
            payload = point["payload"]
            self.assertIn(payload["record_id"], {first_id, second_id})
            self.assertNotIn("content", payload)
            self.assertNotIn("vector", payload)

    def test_dry_run_reports_exact_staging_inventory_without_writes(self):
        self._append("3", "existing record")
        active = self._builder().rebuild(WORKSPACE_ID)
        self._append("4", "new source record")
        before_changes = self.connection.total_changes
        before_collections = copy.deepcopy(self.client.collections)

        preview = self._builder().rebuild(WORKSPACE_ID, dry_run=True)

        self.assertTrue(preview.dry_run)
        self.assertEqual("ready", preview.status)
        self.assertEqual("ready", preview.capability_status)
        self.assertIsNone(preview.capability_reason)
        self.assertEqual(active.staging_manifest_id, preview.active_manifest_id)
        self.assertEqual(1, preview.active_generation)
        self.assertEqual("active", preview.active_status)
        self.assertEqual(1, preview.active_row_count)
        self.assertEqual(1, preview.row_count_delta)
        self.assertEqual(active.content_digest, preview.active_content_digest)
        self.assertTrue(preview.content_digest_changed)
        self.assertEqual(2, preview.generation)
        self.assertEqual(2, preview.source_event_count)
        self.assertEqual(2, preview.row_count)
        self.assertEqual("local", preview.provider_key)
        self.assertEqual("deterministic-test-model", preview.model_id)
        self.assertEqual(3, preview.dimension)
        self.assertRegex(preview.build_config_hash, r"^[0-9a-f]{64}$")
        self.assertRegex(preview.content_digest, r"^[0-9a-f]{64}$")
        self.assertIn("-g2-", preview.collection_name)
        self.assertIsNotNone(preview.staging_manifest_id)
        self.assertEqual(before_changes, self.connection.total_changes)
        self.assertEqual(before_collections, self.client.collections)
        self.assertEqual(
            [(1, "active")],
            [
                tuple(row)
                for row in self.connection.execute(
                    "SELECT generation,status FROM projection_manifests "
                    "WHERE workspace_id=? AND projection_name='dense'",
                    (WORKSPACE_ID,),
                )
            ],
        )

    def test_retry_of_current_source_reuses_validated_active_generation(self):
        self._append("5", "stable source")
        first = self._builder().rebuild(WORKSPACE_ID)
        before_changes = self.connection.total_changes
        before_collections = copy.deepcopy(self.client.collections)

        retried = self._builder().rebuild(WORKSPACE_ID)

        self.assertTrue(retried.reused)
        self.assertEqual(first.generation, retried.generation)
        self.assertEqual(first.staging_manifest_id, retried.staging_manifest_id)
        self.assertEqual("active", retried.status)
        self.assertEqual(before_changes, self.connection.total_changes)
        self.assertEqual(before_collections, self.client.collections)
        self.assertEqual(
            [(1, "active")],
            [
                tuple(row)
                for row in self.connection.execute(
                    "SELECT generation,status FROM projection_manifests "
                    "WHERE projection_name='dense'"
                )
            ],
        )

    def test_manifest_contract_hash_binds_encoder_and_builder_semantics(self):
        from daem0nmcp.retrieval.dense_projection import DenseProjectionBuilder

        self._append("5", "contract-bound source")
        encoder = ContractEncoder("search_document: ")
        builder = DenseProjectionBuilder(
            self.connection,
            provider_key="local",
            model_id="deterministic-test-model",
            dimension=3,
            encoder=encoder,
            query_prefix="search_query: ",
            client=self.client,
            collection_prefix="test-dense",
            clock_us=lambda: 500,
        )

        result = builder.rebuild(WORKSPACE_ID)

        details = json.loads(
            str(
                self.connection.execute(
                    "SELECT details_json FROM projection_manifests WHERE manifest_id=?",
                    (result.staging_manifest_id,),
                ).fetchone()[0]
            )
        )
        expected_encoder_contract = {
            "artifact_fingerprint": encoder.artifact_fingerprint,
            "backend": "test-backend",
            "document_prefix": "search_document: ",
            "encoder_type": (
                f"{ContractEncoder.__module__}.{ContractEncoder.__qualname__}"
            ),
            "input_source": "memory_records.content",
            "max_sequence_length": 512,
            "model_id": "deterministic-test-model",
            "output_dimension": 3,
            "query_prefix": "search_query: ",
            "truncate_dimension": 3,
        }
        expected_contract_hash = hashlib.sha256(
            json.dumps(
                {
                    "build_config_hash": result.build_config_hash,
                    "builder_version": "retrieval-dense-2",
                    "encoder_contract": expected_encoder_contract,
                    "projection": "dense",
                },
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
        ).hexdigest()
        self.assertEqual(expected_encoder_contract, details["encoder_contract"])
        self.assertEqual(expected_contract_hash, details["builder_contract_hash"])
        self.assertEqual(expected_contract_hash, result.builder_contract_hash)

        changed = DenseProjectionBuilder(
            self.connection,
            provider_key="local",
            model_id="deterministic-test-model",
            dimension=3,
            encoder=ContractEncoder("search_document: "),
            query_prefix="different_query: ",
            client=self.client,
            collection_prefix="test-dense",
            clock_us=lambda: 500,
        )
        self.assertFalse(changed.active_is_current(WORKSPACE_ID))
        preview = changed.rebuild(WORKSPACE_ID, dry_run=True)
        self.assertNotEqual(result.builder_contract_hash, preview.builder_contract_hash)

    def test_encoder_backend_fallback_cannot_activate_false_contract(self):
        from daem0nmcp.retrieval.dense_projection import (
            DenseProjectionBuilder,
            DenseProjectionBuildError,
        )

        self._append("6", "backend-bound source")

        class FallbackEncoder(ContractEncoder):
            def encode(self, text: str) -> list[float]:
                self.backend = "fallback-backend"
                return super().encode(text)

        builder = DenseProjectionBuilder(
            self.connection,
            provider_key="local",
            model_id="deterministic-test-model",
            dimension=3,
            encoder=FallbackEncoder("search_document: "),
            query_prefix="search_query: ",
            client=self.client,
            collection_prefix="test-dense",
            clock_us=lambda: 500,
        )

        with self.assertRaises(DenseProjectionBuildError) as raised:
            builder.rebuild(WORKSPACE_ID)

        self.assertEqual("PROJECTION_VALIDATION_FAILED", raised.exception.code)
        self.assertEqual(
            0,
            self.connection.execute(
                "SELECT count(*) FROM projection_manifests "
                "WHERE projection_name='dense'"
            ).fetchone()[0],
        )

    def test_dry_run_sanitizes_optional_dense_capability_unavailable(self):
        from daem0nmcp.retrieval.dense_projection import DenseProjectionBuilder

        self._append("6", "lexical remains sufficient")
        before_changes = self.connection.total_changes
        builder = DenseProjectionBuilder(
            self.connection,
            provider_key="optional",
            model_id="missing-model",
            dimension=3,
            encoder=None,
            client=None,
            collection_prefix="test-dense",
            clock_us=lambda: 500,
        )

        preview = builder.rebuild(WORKSPACE_ID, dry_run=True)

        self.assertEqual("unavailable", preview.status)
        self.assertEqual("unavailable", preview.capability_status)
        self.assertEqual("DENSE_UNAVAILABLE", preview.capability_reason)
        self.assertEqual(1, preview.row_count)
        self.assertEqual(1, preview.source_event_count)
        self.assertEqual(before_changes, self.connection.total_changes)
        self.assertEqual({}, self.client.collections)

    def test_dry_run_rejects_incomplete_explicit_client_capability(self):
        from daem0nmcp.retrieval.dense_projection import DenseProjectionBuilder

        builder = DenseProjectionBuilder(
            self.connection,
            provider_key="invalid-client",
            model_id="test-model",
            dimension=3,
            encoder=self.encoder,
            client=object(),
            collection_prefix="test-dense",
            clock_us=lambda: 500,
        )

        preview = builder.rebuild(WORKSPACE_ID, dry_run=True)

        self.assertEqual("unavailable", preview.capability_status)
        self.assertEqual("DENSE_UNAVAILABLE", preview.capability_reason)

    def test_timeout_overflow_is_rejected_as_a_bounded_validation_error(self):
        from daem0nmcp.retrieval.dense_projection import DenseProjectionBuilder

        with self.assertRaises(ValueError) as raised:
            DenseProjectionBuilder(
                self.connection,
                provider_key="local",
                model_id="test-model",
                dimension=3,
                encoder=self.encoder,
                client=self.client,
                timeout_seconds=10**400,
            )

        self.assertEqual(
            "timeout_seconds must be a positive finite number",
            str(raised.exception),
        )

    def test_required_build_fails_with_only_sanitized_unavailable_code(self):
        from daem0nmcp.retrieval.dense_projection import (
            DenseProjectionBuilder,
            DenseProjectionBuildError,
        )

        self._append("7", "source remains canonical")
        builder = DenseProjectionBuilder(
            self.connection,
            provider_key="optional",
            model_id="missing-model",
            dimension=3,
            encoder=None,
            client=None,
            collection_prefix="test-dense",
            clock_us=lambda: 500,
        )

        with self.assertRaises(DenseProjectionBuildError) as raised:
            builder.rebuild(WORKSPACE_ID)

        self.assertEqual("DENSE_UNAVAILABLE", raised.exception.code)
        self.assertEqual(
            "DENSE_UNAVAILABLE: dense projection capability is unavailable",
            str(raised.exception),
        )
        self.assertEqual(
            0,
            self.connection.execute(
                "SELECT count(*) FROM projection_manifests "
                "WHERE projection_name='dense'"
            ).fetchone()[0],
        )

    def test_client_factory_preserves_distinct_remote_and_local_semantics(self):
        from daem0nmcp.retrieval.dense_projection import DenseProjectionBuilder

        created: list[tuple[dict[str, object], FakeQdrantClient]] = []

        def factory(**kwargs: object) -> FakeQdrantClient:
            client = FakeQdrantClient()
            created.append((dict(kwargs), client))
            return client

        self._append("8", "remote source")
        remote = DenseProjectionBuilder(
            self.connection,
            provider_key="remote",
            model_id="remote-model",
            dimension=3,
            encoder=self.encoder,
            qdrant_url="https://qdrant.invalid",
            qdrant_api_key="test-key",
            timeout_seconds=4.5,
            client_factory=factory,
            collection_prefix="test-dense",
            clock_us=lambda: 500,
        ).rebuild(WORKSPACE_ID)
        self.assertEqual(
            {
                "url": "https://qdrant.invalid",
                "api_key": "test-key",
                "timeout": 4.5,
            },
            created[0][0],
        )
        self.assertIn(remote.collection_name, created[0][1].collections)

        local = DenseProjectionBuilder(
            self.connection,
            provider_key="local",
            model_id="local-model",
            dimension=3,
            encoder=self.encoder,
            qdrant_path="isolated-local-qdrant",
            timeout_seconds=2.0,
            client_factory=factory,
            collection_prefix="test-dense",
            clock_us=lambda: 600,
        ).rebuild(WORKSPACE_ID)
        self.assertEqual({"path": "isolated-local-qdrant"}, created[1][0])
        self.assertIn(local.collection_name, created[1][1].collections)

    def test_qdrant_17_adapter_uses_models_without_collection_exists(self):
        from daem0nmcp.retrieval.dense_projection import DenseProjectionBuilder

        self._append("f", "legacy client contract")
        client = LegacyStrictQdrantClient()
        builder = DenseProjectionBuilder(
            self.connection,
            provider_key="legacy-local",
            model_id="legacy-model",
            dimension=3,
            encoder=self.encoder,
            client=client,
            qdrant_models=FakeQdrantModels,
            collection_prefix="test-dense",
            clock_us=lambda: 500,
        )

        result = builder.rebuild(WORKSPACE_ID)

        collection = client.collections[result.collection_name]
        self.assertIsInstance(collection["vectors_config"], FakeVectorParams)
        self.assertEqual(3, collection["vectors_config"].size)
        self.assertEqual("Cosine", collection["vectors_config"].distance)
        self.assertEqual(1, len(collection["points"]))
        self.assertTrue(builder.active_is_current(WORKSPACE_ID))

    def test_qdrant_collection_lookup_rejects_ambiguous_provider_responses(self):
        from daem0nmcp.retrieval.providers import qdrant_collection_exists

        class MissingCollections:
            def get_collections(self):
                return {}

        class MissingName:
            def get_collections(self):
                return {"collections": [{}]}

        class StringEntry:
            def get_collections(self):
                return {"collections": ["collection-name"]}

        class NonBooleanModern:
            def collection_exists(self, _name):
                return 1

        class EmptyLookupSuccess:
            def get_collection(self, _name):
                return None

        for client in (
            MissingCollections(),
            MissingName(),
            StringEntry(),
            NonBooleanModern(),
            EmptyLookupSuccess(),
        ):
            with (
                self.subTest(client=type(client).__name__),
                self.assertRaises(TypeError),
            ):
                qdrant_collection_exists(client, "collection-name")

    def test_active_is_current_checks_manifest_ref_and_point_bindings(self):
        record_id = self._append("9", "validated evidence")
        builder = self._builder()
        builder.rebuild(WORKSPACE_ID)
        self.assertTrue(builder.active_is_current(WORKSPACE_ID))

        self.connection.execute(
            "UPDATE dense_projection_refs SET state='failed',failure_code='TEST' "
            "WHERE record_id=?",
            (record_id,),
        )
        self.connection.commit()

        self.assertFalse(builder.active_is_current(WORKSPACE_ID))

    def test_failed_staging_validation_retains_prior_active_generation(self):
        from daem0nmcp.retrieval.dense_projection import DenseProjectionBuildError

        self._append("a", "prior active evidence")
        builder = self._builder()
        first = builder.rebuild(WORKSPACE_ID)
        first_collection = copy.deepcopy(self.client.collections[first.collection_name])
        self._append("b", "new generation evidence")
        self.client.corrupt_retrieval = True

        with self.assertRaises(DenseProjectionBuildError) as raised:
            builder.rebuild(WORKSPACE_ID)

        self.assertEqual("PROJECTION_VALIDATION_FAILED", raised.exception.code)
        self.assertEqual(
            [(1, "active")],
            [
                tuple(row)
                for row in self.connection.execute(
                    "SELECT generation,status FROM projection_manifests "
                    "WHERE projection_name='dense' ORDER BY generation"
                )
            ],
        )
        self.assertEqual(
            [(1, "ready")],
            [
                tuple(row)
                for row in self.connection.execute(
                    "SELECT projection_generation,state FROM dense_projection_refs"
                )
            ],
        )
        self.assertEqual(
            first_collection, self.client.collections[first.collection_name]
        )

    def test_retry_replaces_orphan_staging_collection_at_same_generation(self):
        from daem0nmcp.retrieval.dense_projection import DenseProjectionBuildError

        self._append("c", "prior generation")
        builder = self._builder()
        builder.rebuild(WORKSPACE_ID)
        self._append("d", "retry source")
        self.client.corrupt_retrieval = True
        with self.assertRaises(DenseProjectionBuildError):
            builder.rebuild(WORKSPACE_ID)
        self.client.corrupt_retrieval = False

        retried = builder.rebuild(WORKSPACE_ID)

        self.assertEqual(2, retried.generation)
        self.assertEqual("active", retried.status)
        self.assertEqual(2, retried.row_count)
        self.assertTrue(builder.active_is_current(WORKSPACE_ID))

    def test_validation_accepts_qdrant_record_objects_not_only_mappings(self):
        self._append("e", "object response")
        self.client.object_retrieval = True

        result = self._builder().rebuild(WORKSPACE_ID)

        self.assertEqual("active", result.status)
        self.assertTrue(self._builder().active_is_current(WORKSPACE_ID))

    def test_staging_manifest_tampering_cannot_activate(self):
        from daem0nmcp.retrieval.dense_projection import (
            DenseProjectionBuilder,
            DenseProjectionBuildError,
        )

        self._append("f", "manifest-bound source")
        connection = self.connection

        class TamperingEncoder(DeterministicEncoder):
            def encode(self, text: str) -> list[float]:
                connection.execute(
                    "UPDATE projection_manifests SET source_event_root_hash=? "
                    "WHERE projection_name='dense' AND status='building'",
                    ("f" * 64,),
                )
                return super().encode(text)

        builder = DenseProjectionBuilder(
            self.connection,
            provider_key="local",
            model_id="deterministic-test-model",
            dimension=3,
            encoder=TamperingEncoder(),
            client=self.client,
            collection_prefix="test-dense",
            clock_us=lambda: 500,
        )

        with self.assertRaises(DenseProjectionBuildError) as raised:
            builder.rebuild(WORKSPACE_ID)

        self.assertEqual("PROJECTION_VALIDATION_FAILED", raised.exception.code)
        self.assertEqual(
            0,
            self.connection.execute(
                "SELECT count(*) FROM projection_manifests "
                "WHERE projection_name='dense'"
            ).fetchone()[0],
        )

    def test_source_change_during_external_build_cannot_activate_stale_points(self):
        from daem0nmcp.retrieval.dense_projection import (
            DenseProjectionBuilder,
            DenseProjectionBuildError,
        )

        self._append("0", "initial snapshot")
        test_case = self

        class AppendingEncoder(DeterministicEncoder):
            changed = False

            def encode(self, text: str) -> list[float]:
                if not self.changed:
                    self.changed = True
                    test_case._append("1", "arrived during dense build")
                return super().encode(text)

        builder = DenseProjectionBuilder(
            self.connection,
            provider_key="local",
            model_id="deterministic-test-model",
            dimension=3,
            encoder=AppendingEncoder(),
            client=self.client,
            collection_prefix="test-dense",
            clock_us=lambda: 500,
        )

        with self.assertRaises(DenseProjectionBuildError) as raised:
            builder.rebuild(WORKSPACE_ID)

        self.assertEqual("PROJECTION_VALIDATION_FAILED", raised.exception.code)
        self.assertEqual(
            0,
            self.connection.execute(
                "SELECT count(*) FROM projection_manifests "
                "WHERE projection_name='dense'"
            ).fetchone()[0],
        )

    def test_external_model_work_allows_writer_then_activation_cas_rejects(self):
        from daem0nmcp.event_store import EventCommand, EventStore
        from daem0nmcp.retrieval.dense_projection import (
            DenseProjectionBuilder,
            DenseProjectionBuildError,
        )

        self._append("2", "captured snapshot")
        test_case = self

        class ConcurrentWriterEncoder(DeterministicEncoder):
            changed = False

            def encode(self, text: str) -> list[float]:
                test_case.assertFalse(test_case.connection.in_transaction)
                if not self.changed:
                    self.changed = True
                    writer = sqlite3.connect(test_case.database_path, timeout=0.05)
                    writer.row_factory = sqlite3.Row
                    writer.execute("PRAGMA foreign_keys=ON")
                    try:
                        EventStore(writer).append_and_project(
                            EventCommand(
                                workspace_id=WORKSPACE_ID,
                                stream_id="mem_" + "3" * 64,
                                stream_kind="memory",
                                event_type="memory.created",
                                occurred_at_us=700,
                                recorded_at_us=701,
                                actor_type="system",
                                payload={
                                    "record": test_case._record(
                                        "concurrent canonical write"
                                    )
                                },
                            )
                        )
                        writer.commit()
                    finally:
                        writer.close()
                return super().encode(text)

        class TransactionAssertingClient(FakeQdrantClient):
            def create_collection(self, **kwargs: object) -> None:
                test_case.assertFalse(test_case.connection.in_transaction)
                super().create_collection(**kwargs)

            def upsert(self, **kwargs: object) -> None:
                test_case.assertFalse(test_case.connection.in_transaction)
                super().upsert(**kwargs)

            def retrieve(self, **kwargs: object) -> object:
                test_case.assertFalse(test_case.connection.in_transaction)
                return super().retrieve(**kwargs)

            def count(self, **kwargs: object) -> object:
                test_case.assertFalse(test_case.connection.in_transaction)
                return super().count(**kwargs)

        client = TransactionAssertingClient()
        builder = DenseProjectionBuilder(
            self.connection,
            provider_key="local",
            model_id="deterministic-test-model",
            dimension=3,
            encoder=ConcurrentWriterEncoder(),
            client=client,
            collection_prefix="test-dense",
            clock_us=lambda: 500,
        )

        with self.assertRaises(DenseProjectionBuildError) as raised:
            builder.rebuild(WORKSPACE_ID)

        self.assertEqual("PROJECTION_VALIDATION_FAILED", raised.exception.code)
        self.assertEqual(
            2,
            self.connection.execute(
                "SELECT count(*) FROM memory_records WHERE workspace_id=?",
                (WORKSPACE_ID,),
            ).fetchone()[0],
        )
        self.assertEqual(
            0,
            self.connection.execute(
                "SELECT count(*) FROM projection_manifests "
                "WHERE projection_name='dense'"
            ).fetchone()[0],
        )
        self.assertEqual({}, client.collections)

    def test_older_staging_generation_cannot_replace_newer_active_build(self):
        from daem0nmcp.retrieval.dense_projection import (
            DenseProjectionBuilder,
            DenseProjectionBuildError,
        )

        self._append("4", "shared unchanged snapshot")
        encoding_started = threading.Event()
        release_encoding = threading.Event()
        first_outcome: dict[str, object] = {}

        class BlockingEncoder(DeterministicEncoder):
            def encode(self, text: str) -> list[float]:
                encoding_started.set()
                if not release_encoding.wait(timeout=5.0):
                    raise RuntimeError("test encoder was not released")
                return super().encode(text)

        def run_first_builder() -> None:
            connection = sqlite3.connect(self.database_path)
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys=ON")
            try:
                builder = DenseProjectionBuilder(
                    connection,
                    provider_key="local",
                    model_id="deterministic-test-model",
                    dimension=3,
                    encoder=BlockingEncoder(),
                    client=self.client,
                    collection_prefix="test-dense",
                    clock_us=lambda: 500,
                )
                first_outcome["result"] = builder.rebuild(WORKSPACE_ID)
            except Exception as exc:  # captured for the test thread
                first_outcome["error"] = exc
            finally:
                connection.close()

        first_thread = threading.Thread(target=run_first_builder)
        first_thread.start()
        self.addCleanup(first_thread.join, 5.0)
        self.addCleanup(release_encoding.set)
        self.assertTrue(encoding_started.wait(timeout=2.0))

        second = self._builder().rebuild(WORKSPACE_ID)
        self.assertEqual(2, second.generation)
        self.assertEqual("active", second.status)
        release_encoding.set()
        first_thread.join(timeout=5.0)

        self.assertFalse(first_thread.is_alive())
        error = first_outcome.get("error")
        self.assertIsInstance(error, DenseProjectionBuildError)
        self.assertEqual("PROJECTION_VALIDATION_FAILED", error.code)
        self.assertNotIn("result", first_outcome)
        self.assertEqual(
            [(2, "active")],
            [
                tuple(row)
                for row in self.connection.execute(
                    "SELECT generation,status FROM projection_manifests "
                    "WHERE projection_name='dense' ORDER BY generation"
                )
            ],
        )
        self.assertEqual({second.collection_name}, set(self.client.collections))

    def test_provider_batches_large_upserts_and_retrievals(self):
        from daem0nmcp.retrieval.dense_projection import (
            _DENSE_PROVIDER_BATCH_SIZE,
            DenseProjectionBuilder,
        )

        class BoundedClient(FakeQdrantClient):
            def __init__(self) -> None:
                super().__init__()
                self.upsert_sizes: list[int] = []
                self.retrieve_sizes: list[int] = []

            def upsert(self, **kwargs: object) -> None:
                points = kwargs["points"]
                assert isinstance(points, list)
                self.upsert_sizes.append(len(points))
                if len(points) > _DENSE_PROVIDER_BATCH_SIZE:
                    raise AssertionError("oversized upsert batch")
                super().upsert(**kwargs)

            def retrieve(self, **kwargs: object) -> object:
                ids = kwargs["ids"]
                assert isinstance(ids, list)
                self.retrieve_sizes.append(len(ids))
                if len(ids) > _DENSE_PROVIDER_BATCH_SIZE:
                    raise AssertionError("oversized retrieve batch")
                return super().retrieve(**kwargs)

        client = BoundedClient()
        for index in range(_DENSE_PROVIDER_BATCH_SIZE * 2 + 1):
            self._append(f"{index:064x}", f"batch record {index}")
        result = DenseProjectionBuilder(
            self.connection,
            provider_key="local",
            model_id="deterministic-test-model",
            dimension=3,
            encoder=self.encoder,
            client=client,
            collection_prefix="test-dense",
            clock_us=lambda: 500,
        ).rebuild(WORKSPACE_ID)

        self.assertEqual(_DENSE_PROVIDER_BATCH_SIZE * 2 + 1, result.row_count)
        self.assertEqual([128, 128, 1], client.upsert_sizes)
        self.assertEqual([128, 128, 1, 128, 128, 1], client.retrieve_sizes)

    def test_late_provider_corruption_preserves_active_and_cleans_staging(self):
        from daem0nmcp.retrieval.dense_projection import (
            _DENSE_PROVIDER_BATCH_SIZE,
            DenseProjectionBuilder,
            DenseProjectionBuildError,
        )

        self._append("a", "active record")
        active = self._builder().rebuild(WORKSPACE_ID)
        for index in range(_DENSE_PROVIDER_BATCH_SIZE):
            self._append(f"{index + 1000:064x}", f"staged record {index}")

        class LateCorruptingClient(FakeQdrantClient):
            def __init__(self, collections: dict[str, dict[str, object]]) -> None:
                super().__init__()
                self.collections = copy.deepcopy(collections)
                self.calls = 0
                self.first_point_id: str | None = None

            def upsert(self, **kwargs: object) -> None:
                self.calls += 1
                points = kwargs["points"]
                assert isinstance(points, list) and points
                super().upsert(**kwargs)
                stored = self.collections[str(kwargs["collection_name"])]["points"]
                assert isinstance(stored, dict)
                if self.calls == 1:
                    self.first_point_id = str(points[0]["id"])
                elif self.calls == 2 and self.first_point_id is not None:
                    first = stored[self.first_point_id]
                    assert isinstance(first, dict)
                    vector = first["vector"]
                    assert isinstance(vector, list)
                    vector[0] = float(vector[0]) + 1.0

        client = LateCorruptingClient(self.client.collections)
        builder = DenseProjectionBuilder(
            self.connection,
            provider_key="local",
            model_id="deterministic-test-model",
            dimension=3,
            encoder=self.encoder,
            client=client,
            collection_prefix="test-dense",
            clock_us=lambda: 501,
        )
        with self.assertRaises(DenseProjectionBuildError) as raised:
            builder.rebuild(WORKSPACE_ID)

        self.assertEqual("PROJECTION_VALIDATION_FAILED", raised.exception.code)
        self.assertEqual({active.collection_name}, set(client.collections))
        self.assertEqual(
            [(active.generation, "active")],
            [
                tuple(row)
                for row in self.connection.execute(
                    "SELECT generation,status FROM projection_manifests "
                    "WHERE projection_name='dense' ORDER BY generation"
                )
            ],
        )

    def test_cancelled_worker_waiter_cleans_staging_and_preserves_active(self):
        from daem0nmcp.retrieval.dense_projection import DenseProjectionBuilder
        from daem0nmcp.retrieval.runtime import (
            _RUNTIME_JOB_WORKERS,
            drain_projection_jobs,
        )

        self._append("a", "active before cancellation")
        active = self._builder().rebuild(WORKSPACE_ID)
        self._append("b", "staged cancellation target")
        entered_upsert = threading.Event()
        release_upsert = threading.Event()

        class BlockingClient(FakeQdrantClient):
            def __init__(self, collections: dict[str, dict[str, object]]) -> None:
                super().__init__()
                self.collections = copy.deepcopy(collections)

            def upsert(self, **kwargs: object) -> None:
                entered_upsert.set()
                release_upsert.wait(timeout=5)
                super().upsert(**kwargs)

        client = BlockingClient(self.client.collections)

        def blocked_drain(
            path,
            _config,
            _max_jobs,
            _include_optional,
            _statuses,
            cancellation_event,
        ):
            connection = sqlite3.connect(path)
            connection.execute("PRAGMA foreign_keys=ON")
            try:
                DenseProjectionBuilder(
                    connection,
                    provider_key="local",
                    model_id="deterministic-test-model",
                    dimension=3,
                    encoder=self.encoder,
                    client=client,
                    collection_prefix="test-dense",
                    clock_us=lambda: 502,
                    cancelled=cancellation_event.is_set,
                ).rebuild(WORKSPACE_ID)
            finally:
                connection.close()
            return ()

        async def exercise() -> None:
            with patch(
                "daem0nmcp.retrieval.runtime._drain_projection_jobs_sync",
                side_effect=blocked_drain,
            ):
                task = asyncio.create_task(
                    drain_projection_jobs(
                        self.database_path,
                        config=SimpleNamespace(),
                        include_optional=True,
                    )
                )
                self.assertTrue(
                    await asyncio.to_thread(entered_upsert.wait, 2),
                    "staging upsert did not start",
                )
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
                release_upsert.set()
                deadline = asyncio.get_running_loop().time() + 3
                while (
                    _RUNTIME_JOB_WORKERS.in_flight
                    and asyncio.get_running_loop().time() < deadline
                ):
                    await asyncio.sleep(0.01)
                self.assertEqual(0, _RUNTIME_JOB_WORKERS.in_flight)

        asyncio.run(exercise())
        self.assertEqual({active.collection_name}, set(client.collections))
        self.assertEqual(
            [(active.generation, "active")],
            [
                tuple(row)
                for row in self.connection.execute(
                    "SELECT generation,status FROM projection_manifests "
                    "WHERE projection_name='dense' ORDER BY generation"
                )
            ],
        )

    def test_provider_batch_failure_keeps_active_and_cleans_staging(self):
        from daem0nmcp.retrieval.dense_projection import (
            _DENSE_PROVIDER_BATCH_SIZE,
            DenseProjectionBuilder,
            DenseProjectionBuildError,
        )

        self._append("a", "active record")
        active = self._builder().rebuild(WORKSPACE_ID)
        for index in range(_DENSE_PROVIDER_BATCH_SIZE):
            self._append(f"{index + 1000:064x}", f"staged record {index}")

        class FailingSecondBatchClient(FakeQdrantClient):
            def __init__(self, collections: dict[str, dict[str, object]]) -> None:
                super().__init__()
                self.collections = copy.deepcopy(collections)
                self.calls = 0

            def upsert(self, **kwargs: object) -> None:
                self.calls += 1
                if self.calls == 2:
                    raise RuntimeError("provider failed after first batch")
                super().upsert(**kwargs)

        client = FailingSecondBatchClient(self.client.collections)
        builder = DenseProjectionBuilder(
            self.connection,
            provider_key="local",
            model_id="deterministic-test-model",
            dimension=3,
            encoder=self.encoder,
            client=client,
            collection_prefix="test-dense",
            clock_us=lambda: 501,
        )
        with self.assertRaises(DenseProjectionBuildError) as raised:
            builder.rebuild(WORKSPACE_ID)

        self.assertEqual("DENSE_BUILD_FAILED", raised.exception.code)
        self.assertEqual({active.collection_name}, set(client.collections))
        self.assertEqual(
            [(active.generation, "active")],
            [
                tuple(row)
                for row in self.connection.execute(
                    "SELECT generation,status FROM projection_manifests "
                    "WHERE projection_name='dense' ORDER BY generation"
                )
            ],
        )

    def test_provider_batch_validation_rejects_duplicate_missing_and_unexpected_ids(
        self,
    ):
        from daem0nmcp.retrieval.dense_projection import (
            _DENSE_PROVIDER_BATCH_SIZE,
            DenseProjectionBuilder,
            DenseProjectionBuildError,
        )

        for index in range(_DENSE_PROVIDER_BATCH_SIZE + 1):
            self._append(f"{index:064x}", f"validation record {index}")

        for mode in ("duplicate", "missing", "unexpected"):
            with self.subTest(mode=mode):

                class InvalidBatchClient(FakeQdrantClient):
                    def __init__(self, invalid_mode: str) -> None:
                        super().__init__()
                        self.invalid_mode = invalid_mode
                        self.retrieve_calls = 0
                        self.first_id: str | None = None

                    def retrieve(self, **kwargs: object) -> object:
                        result = super().retrieve(**kwargs)
                        assert isinstance(result, list)
                        self.retrieve_calls += 1
                        if self.retrieve_calls == 1:
                            self.first_id = str(result[0]["id"])
                        elif (
                            self.invalid_mode == "duplicate"
                            and self.first_id is not None
                        ):
                            result.append(copy.deepcopy(result[0]))
                            result[-1]["id"] = self.first_id
                        elif self.invalid_mode == "missing":
                            result.clear()
                        elif self.invalid_mode == "unexpected":
                            result.append(copy.deepcopy(result[0]))
                            result[-1]["id"] = "mem_" + "f" * 64
                        return result

                client = InvalidBatchClient(mode)
                builder = DenseProjectionBuilder(
                    self.connection,
                    provider_key="local",
                    model_id="deterministic-test-model",
                    dimension=3,
                    encoder=self.encoder,
                    client=client,
                    collection_prefix=f"test-dense-{mode}",
                    clock_us=lambda: 600,
                )
                with self.assertRaises(DenseProjectionBuildError) as raised:
                    builder.rebuild(WORKSPACE_ID)
                self.assertEqual("PROJECTION_VALIDATION_FAILED", raised.exception.code)
                self.assertEqual({}, client.collections)

    def test_attested_generation_reuses_unchanged_and_encodes_only_new_record(self):
        from daem0nmcp.retrieval.dense_projection import DenseProjectionBuilder

        class CountingEncoder(DeterministicEncoder):
            def __init__(self) -> None:
                self.calls: list[str] = []

            def encode_many(self, texts: list[str]) -> list[list[float]]:
                self.calls.extend(texts)
                return [DeterministicEncoder.encode(self, text) for text in texts]

        encoder = CountingEncoder()
        self._append("1", "first reusable record")
        self._append("2", "second reusable record")
        builder = DenseProjectionBuilder(
            self.connection,
            provider_key="local",
            model_id="deterministic-test-model",
            dimension=3,
            encoder=encoder,
            client=self.client,
            collection_prefix="test-dense",
            clock_us=lambda: 700,
        )

        first = builder.rebuild(WORKSPACE_ID)
        self.assertEqual(2, len(encoder.calls))
        unchanged = builder.rebuild(WORKSPACE_ID)
        self.assertTrue(unchanged.reused)
        self.assertEqual(first.generation, unchanged.generation)
        self.assertEqual(2, len(encoder.calls))

        self._append("3", "one new record")
        changed = builder.rebuild(WORKSPACE_ID)
        self.assertEqual(2, changed.generation)
        self.assertEqual(3, len(encoder.calls))
        self.assertEqual("one new record", encoder.calls[-1])

    def test_corrupt_or_missing_prior_vector_reencodes_only_affected_records(self):
        from daem0nmcp.retrieval.dense_projection import DenseProjectionBuilder

        class CountingEncoder(DeterministicEncoder):
            def __init__(self) -> None:
                self.calls = 0

            def encode_many(self, texts: list[str]) -> list[list[float]]:
                self.calls += len(texts)
                return [DeterministicEncoder.encode(self, text) for text in texts]

        encoder = CountingEncoder()
        first_id = self._append("4", "first prior record")
        second_id = self._append("5", "second prior record")
        builder = DenseProjectionBuilder(
            self.connection,
            provider_key="local",
            model_id="deterministic-test-model",
            dimension=3,
            encoder=encoder,
            client=self.client,
            collection_prefix="test-dense",
            clock_us=lambda: 701,
        )
        first = builder.rebuild(WORKSPACE_ID)
        points = self.client.collections[first.collection_name]["points"]
        assert isinstance(points, dict)
        from daem0nmcp.retrieval.providers import dense_point_id

        corrupt_id = dense_point_id(WORKSPACE_ID, first_id)
        corrupt = points[corrupt_id]
        assert isinstance(corrupt, dict)
        corrupt["vector"] = [999.0, 1.0, 1.0]
        self._append("6", "new record after corrupt provider vector")

        second = builder.rebuild(WORKSPACE_ID)

        self.assertEqual(2, second.generation)
        self.assertEqual(4, encoder.calls)
        second_points = self.client.collections[second.collection_name]["points"]
        assert isinstance(second_points, dict)
        del second_points[dense_point_id(WORKSPACE_ID, second_id)]
        self._append("d", "new record after missing provider point")

        third = builder.rebuild(WORKSPACE_ID)

        self.assertEqual(3, third.generation)
        self.assertEqual(6, encoder.calls)
        self.assertTrue(builder.active_is_current(WORKSPACE_ID))

    def test_null_attestations_never_seed_reuse(self):
        from daem0nmcp.retrieval.dense_projection import DenseProjectionBuilder

        class CountingEncoder(DeterministicEncoder):
            def __init__(self) -> None:
                self.calls = 0

            def encode_many(self, texts: list[str]) -> list[list[float]]:
                self.calls += len(texts)
                return [DeterministicEncoder.encode(self, text) for text in texts]

        encoder = CountingEncoder()
        self._append("7", "legacy first")
        self._append("8", "legacy second")
        builder = DenseProjectionBuilder(
            self.connection,
            provider_key="local",
            model_id="deterministic-test-model",
            dimension=3,
            encoder=encoder,
            client=self.client,
            collection_prefix="test-dense",
            clock_us=lambda: 702,
        )
        builder.rebuild(WORKSPACE_ID)
        self.connection.execute(
            "UPDATE dense_projection_refs SET vector_format=NULL,vector_sha256=NULL"
        )
        self.connection.commit()
        self._append("9", "new after unattested import")

        builder.rebuild(WORKSPACE_ID)

        self.assertEqual(5, encoder.calls)

    def test_payload_and_source_event_substitution_cannot_seed_reuse(self):
        from daem0nmcp.retrieval.dense_projection import DenseProjectionBuilder
        from daem0nmcp.retrieval.providers import dense_point_id

        class CountingEncoder(DeterministicEncoder):
            def __init__(self) -> None:
                self.calls = 0

            def encode_many(self, texts: list[str]) -> list[list[float]]:
                self.calls += len(texts)
                return [DeterministicEncoder.encode(self, text) for text in texts]

        encoder = CountingEncoder()
        first_id = self._append("e", "payload-bound record")
        second_id = self._append("f", "source-event-bound record")
        builder = DenseProjectionBuilder(
            self.connection,
            provider_key="local",
            model_id="deterministic-test-model",
            dimension=3,
            encoder=encoder,
            client=self.client,
            collection_prefix="test-dense",
            clock_us=lambda: 702,
        )
        first = builder.rebuild(WORKSPACE_ID)
        points = self.client.collections[first.collection_name]["points"]
        assert isinstance(points, dict)
        substituted = points[dense_point_id(WORKSPACE_ID, first_id)]
        assert isinstance(substituted, dict)
        payload = substituted["payload"]
        assert isinstance(payload, dict)
        payload["content_hash"] = "0" * 64
        other_event = self.connection.execute(
            "SELECT source_event_id FROM memory_records WHERE record_id=?",
            (first_id,),
        ).fetchone()[0]
        self.connection.execute(
            "UPDATE dense_projection_refs SET updated_event_id=? WHERE record_id=?",
            (other_event, second_id),
        )
        self.connection.commit()
        self._append("1", "new record after substitutions")

        builder.rebuild(WORKSPACE_ID)

        self.assertEqual(5, encoder.calls)

    def test_artifact_fingerprint_change_invalidates_same_named_model(self):
        from daem0nmcp.retrieval.dense_projection import DenseProjectionBuilder

        class ChangedEncoder(DeterministicEncoder):
            artifact_fingerprint = hashlib.sha256(b"changed-model-bytes").hexdigest()

            def __init__(self) -> None:
                self.calls = 0

            def encode_many(self, texts: list[str]) -> list[list[float]]:
                self.calls += len(texts)
                return [DeterministicEncoder.encode(self, text) for text in texts]

        self._append("a", "artifact-bound record")
        self._builder().rebuild(WORKSPACE_ID)
        changed = ChangedEncoder()
        builder = DenseProjectionBuilder(
            self.connection,
            provider_key="local",
            model_id="deterministic-test-model",
            dimension=3,
            encoder=changed,
            client=self.client,
            collection_prefix="test-dense",
            clock_us=lambda: 703,
        )

        self.assertFalse(builder.active_is_current(WORKSPACE_ID))
        result = builder.rebuild(WORKSPACE_ID)

        self.assertEqual(2, result.generation)
        self.assertEqual(1, changed.calls)

    def test_prior_provider_outage_is_retry_safe(self):
        from daem0nmcp.retrieval.dense_projection import (
            DenseProjectionBuilder,
            DenseProjectionBuildError,
        )

        class FailingPriorRetrieveClient(FakeQdrantClient):
            fail_collection: str | None = None

            def retrieve(self, **kwargs: object) -> object:
                if kwargs["collection_name"] == self.fail_collection:
                    raise RuntimeError("provider unavailable")
                return super().retrieve(**kwargs)

        client = FailingPriorRetrieveClient()
        self._append("b", "active before outage")
        builder = DenseProjectionBuilder(
            self.connection,
            provider_key="local",
            model_id="deterministic-test-model",
            dimension=3,
            encoder=self.encoder,
            client=client,
            collection_prefix="test-dense",
            clock_us=lambda: 704,
        )
        first = builder.rebuild(WORKSPACE_ID)
        self._append("c", "write during outage")
        client.fail_collection = first.collection_name

        with self.assertRaises(DenseProjectionBuildError) as raised:
            builder.rebuild(WORKSPACE_ID)

        self.assertEqual("DENSE_BUILD_FAILED", raised.exception.code)
        self.assertEqual({first.collection_name}, set(client.collections))
        self.assertEqual(
            [(first.generation, "active")],
            [
                tuple(row)
                for row in self.connection.execute(
                    "SELECT generation,status FROM projection_manifests "
                    "WHERE projection_name='dense' ORDER BY generation"
                )
            ],
        )

    def test_inference_batches_are_bounded_and_ordered(self):
        from daem0nmcp.retrieval.dense_projection import (
            _DENSE_INFERENCE_BATCH_SIZE,
            DenseProjectionBuilder,
        )

        class BatchEncoder(DeterministicEncoder):
            def __init__(self) -> None:
                self.batches: list[list[str]] = []

            def encode_many(self, texts: list[str]) -> list[list[float]]:
                self.batches.append(list(texts))
                return [DeterministicEncoder.encode(self, text) for text in texts]

        encoder = BatchEncoder()
        for index in range(_DENSE_INFERENCE_BATCH_SIZE * 2 + 1):
            self._append(f"{index + 2000:064x}", f"ordered batch {index:03d}")

        DenseProjectionBuilder(
            self.connection,
            provider_key="local",
            model_id="deterministic-test-model",
            dimension=3,
            encoder=encoder,
            client=self.client,
            collection_prefix="test-dense",
            clock_us=lambda: 705,
        ).rebuild(WORKSPACE_ID)

        self.assertEqual([32, 32, 1], [len(batch) for batch in encoder.batches])
        self.assertEqual(
            [f"ordered batch {index:03d}" for index in range(65)],
            [text for batch in encoder.batches for text in batch],
        )


def test_gc_reconciliation_fairly_converges_across_bounded_restart_slices() -> None:
    from daem0nmcp.retrieval.dense_generation_gc import (
        reconcile_inactive_generation_gc,
    )
    from daem0nmcp.retrieval.providers import dense_manifest_details

    with tempfile.TemporaryDirectory() as temporary:
        path = Path(temporary) / "fair-gc.sqlite3"
        connection = sqlite3.connect(path)
        connection.execute("PRAGMA foreign_keys=ON")
        for version in (16, 17, 18, 31, 32):
            _apply_migration(connection, version)
        for workspace_number in range(17):
            workspace_id = f"ws_{workspace_number:024x}"
            for generation in range(1, 4):
                details = dense_manifest_details(
                    workspace_id=workspace_id,
                    provider_key="local",
                    generation=generation,
                    model_id="fairness-model",
                    dimension=3,
                    collection_prefix="fairness",
                )
                connection.execute(
                    "INSERT INTO projection_manifests(manifest_id,workspace_id,"
                    "projection_name,generation,projection_version,status,"
                    "source_event_count,source_event_root_hash,row_count,"
                    "builder_version,details_json,started_at_us) VALUES "
                    "(?,?,'dense',?,1,?,0,?,0,'test',?,1)",
                    (
                        "prj_" + f"{workspace_number * 3 + generation:064x}",
                        workspace_id,
                        generation,
                        "active" if generation == 3 else "ready",
                        "0" * 64,
                        json.dumps(details, sort_keys=True, separators=(",", ":")),
                    ),
                )
        connection.commit()
        assert reconcile_inactive_generation_gc(connection, now_us=100) == 16
        connection.close()

        restarted = sqlite3.connect(path)
        try:
            restarted.execute("PRAGMA foreign_keys=ON")
            assert reconcile_inactive_generation_gc(restarted, now_us=101) == 1
            assert reconcile_inactive_generation_gc(restarted, now_us=102) == 0
            assert (
                restarted.execute(
                    "SELECT COUNT(*) FROM dense_generation_gc_jobs"
                ).fetchone()[0]
                == 17
            )
            assert (
                restarted.execute(
                    "SELECT COUNT(*) FROM projection_manifests m WHERE "
                    "m.projection_name='dense' AND m.status='ready' AND NOT EXISTS ("
                    "SELECT 1 FROM dense_generation_gc_jobs gc WHERE "
                    "gc.workspace_id=m.workspace_id AND "
                    "gc.projection_generation=m.generation)"
                ).fetchone()[0]
                == 17
            )
        finally:
            restarted.close()


def test_schema_30_to_31_adds_nullable_dense_attestations() -> None:
    connection = sqlite3.connect(":memory:")
    try:
        for version in (16, 17, 18, 30):
            _apply_migration(connection, version)
        before = {
            str(row[1])
            for row in connection.execute("PRAGMA table_info(dense_projection_refs)")
        }
        assert "vector_format" not in before
        assert "vector_sha256" not in before

        _apply_migration(connection, 31)

        after = {
            str(row[1])
            for row in connection.execute("PRAGMA table_info(dense_projection_refs)")
        }
        assert {"vector_format", "vector_sha256"} <= after
        nullability = {
            str(row[1]): int(row[3])
            for row in connection.execute("PRAGMA table_info(dense_projection_refs)")
        }
        assert nullability["vector_format"] == 0
        assert nullability["vector_sha256"] == 0
    finally:
        connection.close()


if __name__ == "__main__":
    unittest.main()
