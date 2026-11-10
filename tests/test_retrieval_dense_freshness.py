"""Dense freshness: what invalidates a generation and what a catch-up costs."""

from __future__ import annotations

import json
import sqlite3
import unittest

import pytest

from tests.test_retrieval_dense_client_sharing import (
    MODEL_ID,
    STATUSES,
    WORKSPACE_ID,
    _DenseRuntimeCase,
    _EmbeddedFolderRuntimeCase,
    _Encoder,
    _record,
)


def _all_v7_schema_versions() -> tuple[int, ...]:
    from daem0nmcp.schema_version import CURRENT_SCHEMA_VERSION

    return tuple(range(16, CURRENT_SCHEMA_VERSION + 1))


class _FreshnessSteps:
    """Event and inspection steps shared by the fake and real provider cases."""

    schema_versions = _all_v7_schema_versions()

    def setUp(self) -> None:
        super().setUp()
        self._states: dict[str, dict[str, object]] = {}

    def _store(self, suffix: str, content: str) -> str:
        record_id = self._append(suffix, content)
        self._states[record_id] = _record(content)
        return record_id

    def _change(self, record_id: str, event_type: str, **changes: object) -> None:
        from daem0nmcp.event_store import EventCommand, EventStore

        self._clock += 100
        self._states[record_id] = {**self._states[record_id], **changes}
        EventStore(self.connection).append_and_project(
            EventCommand(
                workspace_id=WORKSPACE_ID,
                stream_id=record_id,
                stream_kind="memory",
                event_type=event_type,
                occurred_at_us=self._clock,
                recorded_at_us=self._clock,
                actor_type="system",
                payload={"record": self._states[record_id]},
            )
        )
        self.connection.commit()

    async def _built(self, count: int = 2):
        records = [
            self._store(str(index), f"durable dense evidence {index}")
            for index in range(1, count + 1)
        ]
        self._enqueue("lexical", "dense")
        await self._drain()
        service = self._service()
        self.assertEqual(("ready", None, count), await self._dense(service))
        return records, service

    def _manifest(self) -> tuple[int, int, str, int, str, dict[str, object]]:
        row = self.connection.execute(
            "SELECT generation,source_event_count,source_event_root_hash,"
            "cursor_recorded_at_us,cursor_event_id,details_json "
            "FROM projection_manifests WHERE projection_name='dense' "
            "AND status='active'"
        ).fetchone()
        return (*row[:5], json.loads(row[5]))

    def _event_root(self) -> tuple[int, int, str]:
        count = self.connection.execute("SELECT COUNT(*) FROM memory_events").fetchone()
        cursor = self.connection.execute(
            "SELECT recorded_at_us,event_id FROM memory_events "
            "ORDER BY recorded_at_us DESC,event_id DESC LIMIT 1"
        ).fetchone()
        return int(count[0]), int(cursor[0]), str(cursor[1])

    def _document_encodes(self) -> int:
        return sum(
            1
            for prefix, _text in _Encoder.encoded
            if prefix == self.config.embedding_document_prefix
        )


class DenseFreshnessTests(_FreshnessSteps, _EmbeddedFolderRuntimeCase):
    async def test_pin_and_archive_events_catch_up_without_a_rebuild(self):
        records, service = await self._built()
        client = self.folders.opened[1]
        mutations, encodes = client.mutations, self._document_encodes()
        collections = dict(self.folders.collections[self.folder_key])

        for record_id, event_type, change in (
            (records[0], "memory.pin_set", {"pinned": True}),
            (records[1], "memory.archive_set", {"archived": True}),
            (records[0], "memory.pin_set", {"pinned": False}),
        ):
            self._change(record_id, event_type, **change)
            # A generation behind the event log is never queried.
            self.assertEqual(
                ("degraded", "DENSE_REBUILD_REQUIRED", 0), await self._dense(service)
            )
            self.assertEqual(("succeeded", None), (await self._drain())["dense"])

            self.assertEqual([(1, "active")], self._generations())
            generation, count, _root, cursor_us, cursor_event, details = (
                self._manifest()
            )
            self.assertEqual(1, generation)
            self.assertEqual(self._event_root(), (count, cursor_us, cursor_event))
            self.assertNotIn("rebuild_required_event_id", details)
            self.assertNotIn("rebuild_required_at_us", details)
            self.assertEqual(("ready", None, 2), await self._dense(service))

        self.assertEqual(mutations, client.mutations)
        self.assertEqual(encodes, self._document_encodes())
        self.assertEqual(collections, self.folders.collections[self.folder_key])
        self.assertEqual(
            [],
            self.connection.execute(
                "SELECT ref.record_id FROM dense_projection_refs ref "
                "JOIN memory_records record ON record.record_id=ref.record_id "
                "WHERE ref.updated_event_id<>record.source_event_id "
                "OR ref.updated_at_us<record.updated_at_us"
            ).fetchall(),
        )

    async def test_caught_up_generation_passes_the_full_currency_check(self):
        from daem0nmcp.retrieval.runtime import create_projection_builders

        records, _service = await self._built()
        self._change(records[0], "memory.pin_set", pinned=True)
        await self._drain()

        builders = create_projection_builders(
            self.connection,
            self.path,
            config=self.config,
            capability_statuses=STATUSES,
        )
        self.addCleanup(builders.close)
        self.assertTrue(builders.dense_builder.active_is_current(WORKSPACE_ID))
        result = builders["dense"](WORKSPACE_ID)
        self.assertEqual(
            (1, "active", True), (result.generation, result.status, result.reused)
        )

    def _corrupt_stored_vector(self, record_id: str) -> None:
        from daem0nmcp.retrieval.providers import dense_point_id

        collection_name = self._manifest()[5]["collection_name"]
        point = self.folders.collections[self.folder_key][str(collection_name)][
            dense_point_id(WORKSPACE_ID, record_id)
        ]
        point["vector"] = [9.0, 1.0, 9.0]

    async def test_generation_with_untrusted_vectors_is_rebuilt_not_advanced(self):
        from daem0nmcp.retrieval.runtime import create_projection_builders

        records, service = await self._built()
        builders = create_projection_builders(
            self.connection,
            self.path,
            config=self.config,
            capability_statuses=STATUSES,
        )
        self.addCleanup(builders.close)

        # Already at the event root, but a stored vector lost its attestation.
        self._corrupt_stored_vector(records[1])
        self.assertFalse(builders.dense_builder.active_is_current(WORKSPACE_ID))
        result = builders["dense"](WORKSPACE_ID)
        self.assertEqual((2, False), (result.generation, result.reused))
        self.assertTrue(builders.dense_builder.active_is_current(WORKSPACE_ID))

        # Behind the event root, and the record the event touched is the one
        # whose stored vector no longer matches its attestation.
        self._corrupt_stored_vector(records[0])
        self._change(records[0], "memory.pin_set", pinned=True)
        self.assertEqual(("succeeded", None), (await self._drain())["dense"])
        self.assertEqual((3, "active"), self._generations()[-1])
        self.assertTrue(builders.dense_builder.active_is_current(WORKSPACE_ID))
        self.assertEqual(("ready", None, 2), await self._dense(service))

    async def test_content_membership_and_outcome_changes_still_rebuild(self):
        records, service = await self._built()

        self._change(
            records[0], "memory.outcome_recorded", outcome="held up", worked=True
        )
        self.assertEqual(
            ("degraded", "DENSE_REBUILD_REQUIRED", 0), await self._dense(service)
        )
        self.assertEqual(("succeeded", None), (await self._drain())["dense"])
        self.assertEqual([(1, "ready"), (2, "active")], self._generations())
        self.assertEqual(("ready", None, 2), await self._dense(service))

        self._change(records[1], "memory.updated", content="rewritten dense evidence")
        await self._drain()
        self.assertEqual((3, "active"), self._generations()[-1])
        self.assertEqual(("ready", None, 2), await self._dense(service))

        self._change(records[1], "memory.deleted", deleted_at_us=self._clock + 100)
        await self._drain()
        self.assertEqual((4, "active"), self._generations()[-1])
        self.assertEqual(("ready", None, 1), await self._dense(service))

        self._store("9", "durable dense evidence nine")
        await self._drain()
        self.assertEqual((5, "active"), self._generations()[-1])
        self.assertEqual(("ready", None, 2), await self._dense(service))

    async def test_rule_and_active_context_events_do_not_touch_dense(self):
        from daem0nmcp.event_store import (
            GovernanceEventCommand,
            GovernanceEventStore,
        )

        _records, service = await self._built()
        jobs_before = self.connection.execute(
            "SELECT status,attempts,updated_at_us FROM background_jobs ORDER BY job_id"
        ).fetchall()
        store = GovernanceEventStore(self.connection)
        store.append_and_project(
            GovernanceEventCommand(
                workspace_id=WORKSPACE_ID,
                stream_id="rule_" + "d" * 64,
                stream_kind="rule",
                event_type="rule.created",
                occurred_at_us=self._clock + 1,
                recorded_at_us=self._clock + 1,
                actor_type="system",
                expected_stream_version=1,
                payload={
                    "rule_id": "rule_" + "d" * 64,
                    "trigger": "dense freshness",
                    "must_do": [],
                    "must_not": [],
                    "ask_first": [],
                    "warnings": [],
                    "priority": 1,
                    "enabled": True,
                    "created_at_us": self._clock + 1,
                    "updated_at_us": self._clock + 1,
                },
            )
        )
        store.append_and_project(
            GovernanceEventCommand(
                workspace_id=WORKSPACE_ID,
                stream_id="act_" + "e" * 64,
                stream_kind="active_context",
                event_type="active_context.added",
                occurred_at_us=self._clock + 2,
                recorded_at_us=self._clock + 2,
                actor_type="system",
                payload={"record_id": "mem_" + "1" * 64},
            )
        )
        self.connection.commit()

        self.assertEqual(
            2,
            self.connection.execute(
                "SELECT COUNT(*) FROM governance_events"
            ).fetchone()[0],
        )
        self.assertEqual(("ready", None, 2), await self._dense(service))
        self.assertEqual(
            jobs_before,
            self.connection.execute(
                "SELECT status,attempts,updated_at_us FROM background_jobs "
                "ORDER BY job_id"
            ).fetchall(),
        )

    async def test_recall_checks_freshness_without_hashing_the_event_log(self):
        from daem0nmcp.retrieval.providers import DenseProvider
        from daem0nmcp.retrieval.types import RetrievalQuery

        await self._built()
        statements: list[str] = []

        def connect() -> sqlite3.Connection:
            connection = sqlite3.connect(self.path)
            connection.set_trace_callback(statements.append)
            return connection

        provider = DenseProvider(
            connection_factory=connect,
            provider_key="qdrant",
            model_id=MODEL_ID,
            dimension=3,
            encoder=_Encoder(self.config.embedding_query_prefix),
            document_encoder=_Encoder(self.config.embedding_document_prefix),
            query_prefix=self.config.embedding_query_prefix,
            qdrant_path=self.folder,
            timeout_seconds=30.0,
        )
        self.addCleanup(provider.close)
        query = RetrievalQuery(workspace_id=WORKSPACE_ID, text="durable dense evidence")

        result = await provider.search(query, 10)
        self.assertEqual(("ready", 2), (result.status, len(result.candidates)))
        self.assertEqual([], [sql for sql in statements if "event_hash" in sql])

        # An event that bypassed the write path's stale marker and does not
        # move the newest-event cursor is still detected.
        columns = [
            str(row[1])
            for row in self.connection.execute("PRAGMA table_info(memory_events)")
        ]
        event = dict(
            zip(
                columns,
                self.connection.execute("SELECT * FROM memory_events").fetchone(),
                strict=True,
            )
        )
        event.update(
            event_id="evt_" + "f" * 64,
            stream_id="mem_" + "f" * 64,
            event_hash="f" * 64,
            recorded_at_us=1,
        )
        self.connection.execute(
            f"INSERT INTO memory_events VALUES ({','.join('?' for _ in columns)})",
            tuple(event[name] for name in columns),
        )
        self.connection.commit()
        queries = len(_Encoder.encoded)
        stale = await provider.search(query, 10)
        self.assertEqual(
            ("degraded", "DENSE_REBUILD_REQUIRED", ()),
            (stale.status, stale.reason, stale.candidates),
        )
        self.assertEqual(queries, len(_Encoder.encoded))

    async def test_catch_up_is_abandoned_when_a_write_lands_during_it(self):
        records, service = await self._built()
        self._change(records[0], "memory.pin_set", pinned=True)
        landed: list[str] = []

        def land_a_write() -> None:
            from daem0nmcp.event_store import EventCommand, EventStore

            if landed:
                return
            record_id = "mem_" + "7" * 64
            landed.append(record_id)
            # The provider call runs on its own thread; write from there.
            writer = sqlite3.connect(self.path)
            try:
                EventStore(writer).append_and_project(
                    EventCommand(
                        workspace_id=WORKSPACE_ID,
                        stream_id=record_id,
                        stream_kind="memory",
                        event_type="memory.created",
                        occurred_at_us=self._clock + 50,
                        recorded_at_us=self._clock + 50,
                        actor_type="system",
                        payload={"record": _record("durable dense evidence seven")},
                    )
                )
                writer.commit()
            finally:
                writer.close()

        succeeded = ("succeeded", None)
        self.assertEqual({"lexical": succeeded}, await self._drain(max_jobs=1))
        self.folders.before_count = land_a_write
        first = await self._drain(max_jobs=1)
        self.folders.before_count = None

        self.assertEqual(1, len(landed))
        self.assertEqual(
            3,
            self.connection.execute("SELECT COUNT(*) FROM memory_records").fetchone()[
                0
            ],
        )
        self.assertEqual({"dense": ("queued", "PROJECTION_VALIDATION_FAILED")}, first)
        self.assertEqual([(1, "active")], self._generations())
        self.assertIn("rebuild_required_event_id", self._manifest()[5])
        self.assertEqual(
            ("degraded", "DENSE_REBUILD_REQUIRED", 0), await self._dense(service)
        )

        self.assertEqual({"lexical": succeeded}, await self._drain(max_jobs=1))
        self.assertEqual({"dense": succeeded}, await self._drain(max_jobs=1))
        self.assertEqual([(1, "ready"), (2, "active")], self._generations())
        self.assertEqual(("ready", None, 3), await self._dense(service))


class EmbeddedQdrantFreshnessTests(_FreshnessSteps, _DenseRuntimeCase):
    """The catch-up against vectors as the real embedded client stores them."""

    def setUp(self) -> None:
        pytest.importorskip("qdrant_client")
        super().setUp()

    def _collections(self) -> list[str]:
        return sorted(item.name for item in (self.folder / "collection").iterdir())

    async def test_pin_event_advances_the_stored_generation_in_place(self):
        from daem0nmcp.retrieval.runtime import create_projection_builders

        records, service = await self._built()
        collections = self._collections()

        self._change(records[0], "memory.pin_set", pinned=True)
        self.assertEqual(
            ("degraded", "DENSE_REBUILD_REQUIRED", 0), await self._dense(service)
        )
        self.assertEqual(("succeeded", None), (await self._drain())["dense"])

        self.assertEqual([(1, "active")], self._generations())
        self.assertEqual(collections, self._collections())
        self.assertEqual(("ready", None, 2), await self._dense(service))
        builders = create_projection_builders(
            self.connection,
            self.path,
            config=self.config,
            capability_statuses=STATUSES,
        )
        self.addCleanup(builders.close)
        self.assertTrue(builders.dense_builder.active_is_current(WORKSPACE_ID))

        self._change(records[1], "memory.updated", content="rewritten dense evidence")
        self.assertEqual(("succeeded", None), (await self._drain())["dense"])
        self.assertEqual([(1, "ready"), (2, "active")], self._generations())
        self.assertEqual(("ready", None, 2), await self._dense(service))


if __name__ == "__main__":
    unittest.main()
