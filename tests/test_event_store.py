"""Dependency-free golden tests for the v7 canonical event contract."""

from __future__ import annotations

import importlib.util
import math
import sqlite3
import tempfile
import threading
import unittest
from dataclasses import replace
from pathlib import Path


class CanonicalEventGoldenTests(unittest.TestCase):
    """Catch any byte-, ID-, or hash-level drift in the public v7 contract."""

    def _helpers(self):
        try:
            from daem0nmcp.event_store import (
                canonical_json_bytes,
                deterministic_id,
                event_hash_for,
                event_id_for_hash,
                sha256_json,
            )
        except ModuleNotFoundError as exc:
            self.fail(f"v7 canonical event helpers are missing: {exc}")
        return (
            canonical_json_bytes,
            deterministic_id,
            event_hash_for,
            event_id_for_hash,
            sha256_json,
        )

    def test_exact_golden_canonical_bytes_ids_and_hashes(self) -> None:
        """Changing normalization or hash fields must break the published fixture."""
        (
            canonical_json_bytes,
            deterministic_id,
            event_hash_for,
            event_id_for_hash,
            sha256_json,
        ) = self._helpers()
        workspace_id = "ws_0123456789abcdef01234567"
        payload = {
            "legacy": {"id": "42", "table": "memories"},
            "record": {
                "content": "Use SQLite",
                "context": {},
                "rationale": None,
                "record_type": "decision",
                "tags": ["db"],
            },
        }
        expected_payload_bytes = (
            b'{"legacy":{"id":"42","table":"memories"},'
            b'"record":{"content":"Use SQLite","context":{},'
            b'"rationale":null,"record_type":"decision","tags":["db"]}}'
        )
        expected_payload_hash = (
            "2a0bde7c1db3026e7009399e089078242da633e6af0ce12df8cd5532f916f418"
        )
        expected_record_id = (
            "mem_8d8a1599b9fedbe559b8bab3eca7253578d64ba8b04e5dd82216e4c6daf1699e"
        )
        event_columns = {
            "actor_id": None,
            "actor_type": "migration",
            "causation_event_id": None,
            "correlation_id": "mig_fixture",
            "event_schema_version": 1,
            "event_type": "memory.created",
            "occurred_at_us": 1736942400000000,
            "payload_hash": expected_payload_hash,
            "previous_event_hash": None,
            "recorded_at_us": 1736942400000000,
            "stream_id": expected_record_id,
            "stream_kind": "memory",
            "stream_version": 1,
            "workspace_id": workspace_id,
        }
        expected_event_hash = (
            "3138d5e4c16bb3a03f08604cdf15493f1b7de7a730e4acef8d9da171c7f9d81f"
        )

        self.assertEqual(expected_payload_bytes, canonical_json_bytes(payload))
        self.assertEqual(expected_payload_hash, sha256_json(payload))
        self.assertEqual(
            expected_record_id,
            deterministic_id("mem", "memory", workspace_id, "legacy", "memories", "42"),
        )
        self.assertEqual(expected_event_hash, event_hash_for(event_columns))
        self.assertEqual(
            f"evt_{expected_event_hash}", event_id_for_hash(expected_event_hash)
        )

    def test_normalization_is_semantic_but_not_lossy(self) -> None:
        """Removing NFC/newline normalization or trimming/reordering must fail."""
        canonical_json_bytes, _, _, _, sha256_json = self._helpers()
        self.assertEqual(
            canonical_json_bytes({"text": "caf\u00e9\nline"}),
            canonical_json_bytes({"text": "cafe\u0301\r\nline"}),
        )
        self.assertNotEqual(
            sha256_json({"text": "value"}), sha256_json({"text": "value "})
        )
        self.assertNotEqual(
            sha256_json({"tags": ["a", "b"]}),
            sha256_json({"tags": ["b", "a"]}),
        )

    def test_noncanonical_python_values_are_rejected(self) -> None:
        """Allowing NaN, infinities, non-string keys, or sets must fail."""
        canonical_json_bytes, _, _, _, _ = self._helpers()
        for value in (
            {"number": math.nan},
            {"number": math.inf},
            {1: "non-string-key"},
            {"unsupported": {"set"}},
        ):
            with (
                self.subTest(value=repr(value)),
                self.assertRaises((TypeError, ValueError)),
            ):
                canonical_json_bytes(value)


def _migration_16_statements():
    path = (
        Path(__file__).resolve().parents[1] / "daem0nmcp" / "migrations" / "schema.py"
    )
    spec = importlib.util.spec_from_file_location("event_test_schema", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return next(item[2] for item in module.MIGRATIONS if item[0] == 16)


class SQLiteEventStoreTests(unittest.TestCase):
    """Exercise canonical append and typed projection without SQLAlchemy."""

    workspace_id = "ws_0123456789abcdef01234567"

    def setUp(self) -> None:
        self.connection = sqlite3.connect(":memory:")
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys=ON")
        for statement in _migration_16_statements():
            self.connection.execute(statement)
        self.connection.commit()

    def tearDown(self) -> None:
        self.connection.close()

    def _api(self):
        from daem0nmcp.event_store import EventCommand, EventStore

        return EventCommand, EventStore

    def _state(self, content="Use SQLite", *, outcome=None, worked=None):
        return {
            "record_type": "decision",
            "legacy_type": None,
            "content": content,
            "rationale": None,
            "context": {},
            "tags": ["db"],
            "file_path": None,
            "file_path_relative": None,
            "keywords": "sqlite",
            "is_permanent": False,
            "pinned": False,
            "archived": False,
            "outcome": outcome,
            "worked": worked,
            "recall_count": 0,
            "surprise_score": None,
            "importance_score": None,
            "source_client": "test",
            "source_model": None,
            "deleted_at_us": None,
        }

    def test_append_projects_contiguous_hash_chained_memory_state(self):
        """Two commands become immutable versions 1/2 and one current record."""
        command_type, store_type = self._api()
        record_id = "mem_" + "1" * 64
        store = store_type(self.connection)
        first = store.append_and_project(
            command_type(
                workspace_id=self.workspace_id,
                stream_id=record_id,
                stream_kind="memory",
                event_type="memory.created",
                occurred_at_us=100,
                recorded_at_us=101,
                actor_type="system",
                payload={"record": self._state()},
            )
        )
        second = store.append_and_project(
            command_type(
                workspace_id=self.workspace_id,
                stream_id=record_id,
                stream_kind="memory",
                event_type="memory.outcome_recorded",
                occurred_at_us=200,
                recorded_at_us=201,
                actor_type="system",
                payload={"record": self._state(outcome="worked", worked=True)},
            )
        )
        rows = self.connection.execute(
            "SELECT * FROM memory_events ORDER BY stream_version"
        ).fetchall()
        record = self.connection.execute(
            "SELECT * FROM memory_records WHERE record_id=?", (record_id,)
        ).fetchone()
        self.assertEqual([1, 2], [row["stream_version"] for row in rows])
        self.assertIsNone(rows[0]["previous_event_hash"])
        self.assertEqual(rows[0]["event_hash"], rows[1]["previous_event_hash"])
        self.assertEqual(first.event_id, rows[0]["event_id"])
        self.assertEqual(second.event_id, rows[1]["event_id"])
        self.assertEqual("worked", record["outcome"])
        self.assertEqual(1, record["worked"])
        self.assertEqual(2, record["stream_version"])

    def test_expected_version_is_exactly_idempotent_or_conflicts(self):
        """Same event is reusable; changed fields at one version fail closed."""
        command_type, store_type = self._api()
        from daem0nmcp.event_store import EventStreamConflict

        record_id = "mem_" + "2" * 64
        store = store_type(self.connection)
        command = command_type(
            workspace_id=self.workspace_id,
            stream_id=record_id,
            stream_kind="memory",
            event_type="memory.created",
            occurred_at_us=100,
            recorded_at_us=101,
            actor_type="import",
            correlation_id="same",
            expected_stream_version=1,
            payload={"record": self._state()},
        )
        first = store.append_and_project(command)
        self.assertEqual(first, store.append_and_project(command))
        with self.assertRaisesRegex(EventStreamConflict, "EVENT_STREAM_CONFLICT"):
            store.append_and_project(
                replace(command, payload={"record": self._state("changed")})
            )
        self.assertEqual(
            1,
            self.connection.execute("SELECT count(*) FROM memory_events").fetchone()[0],
        )

    def test_only_integrity_errors_on_insert_are_stream_conflicts(self):
        """A lock or I/O failure must not be reported as a retryable conflict."""
        command_type, store_type = self._api()
        from daem0nmcp.event_store import EventStreamConflict

        connection = self.connection

        class FailingInsert:
            error: Exception

            def execute(self, sql, *arguments):
                if "INSERT INTO memory_events" in sql:
                    raise self.error
                return connection.execute(sql, *arguments)

            def __getattr__(self, name):
                return getattr(connection, name)

        command = command_type(
            workspace_id=self.workspace_id,
            stream_id="mem_" + "c" * 64,
            stream_kind="memory",
            event_type="memory.created",
            occurred_at_us=1,
            recorded_at_us=1,
            actor_type="system",
            payload={"record": self._state()},
        )
        failing = FailingInsert()
        failing.error = sqlite3.OperationalError("disk I/O error")
        with self.assertRaisesRegex(sqlite3.OperationalError, "disk I/O error"):
            store_type(failing).append_and_project(command)
        failing.error = sqlite3.IntegrityError("UNIQUE constraint failed")
        with self.assertRaises(EventStreamConflict):
            store_type(failing).append_and_project(command)
        connection.rollback()

    def test_only_integrity_errors_on_governance_insert_are_conflicts(self):
        """The governance append follows the same conflict rule as memory."""
        from daem0nmcp.event_store import (
            EventStreamConflict,
            GovernanceEventCommand,
            GovernanceEventStore,
        )
        from daem0nmcp.migrations.schema import MIGRATIONS
        from daem0nmcp.schema_version import CURRENT_SCHEMA_VERSION

        connection = sqlite3.connect(":memory:")
        self.addCleanup(connection.close)
        connection.execute("PRAGMA foreign_keys=ON")
        for version in range(16, CURRENT_SCHEMA_VERSION + 1):
            for statement in next(m for m in MIGRATIONS if m[0] == version)[2]:
                connection.execute(statement)
        connection.commit()

        class FailingInsert:
            error: Exception

            def execute(self, sql, *arguments):
                if "INSERT INTO governance_events" in sql:
                    raise self.error
                return connection.execute(sql, *arguments)

            def __getattr__(self, name):
                return getattr(connection, name)

        command = GovernanceEventCommand(
            workspace_id=self.workspace_id,
            stream_id="rule_" + "d" * 64,
            stream_kind="rule",
            event_type="rule.created",
            occurred_at_us=1,
            recorded_at_us=1,
            actor_type="system",
            expected_stream_version=1,
            payload={
                "rule_id": "rule_" + "d" * 64,
                "trigger": "conflict mapping",
                "must_do": [],
                "must_not": [],
                "ask_first": [],
                "warnings": [],
                "priority": 1,
                "enabled": True,
                "created_at_us": 1,
                "updated_at_us": 1,
            },
        )
        failing = FailingInsert()
        failing.error = sqlite3.OperationalError("disk I/O error")
        with self.assertRaisesRegex(sqlite3.OperationalError, "disk I/O error"):
            GovernanceEventStore(failing).append_and_project(command)
        failing.error = sqlite3.IntegrityError("UNIQUE constraint failed")
        with self.assertRaises(EventStreamConflict):
            GovernanceEventStore(failing).append_and_project(command)
        connection.rollback()

    def test_projection_failure_rolls_back_event_with_savepoint(self):
        """No immutable event may survive a failed projection in caller transaction."""
        command_type, store_type = self._api()
        record_id = "mem_" + "3" * 64
        self.connection.execute("BEGIN IMMEDIATE")
        store = store_type(self.connection)
        with self.assertRaises(ValueError):
            store.append_and_project(
                command_type(
                    workspace_id=self.workspace_id,
                    stream_id=record_id,
                    stream_kind="memory",
                    event_type="memory.created",
                    occurred_at_us=1,
                    recorded_at_us=1,
                    actor_type="system",
                    payload={"record": {**self._state(), "record_type": "bad"}},
                )
            )
        self.assertEqual(
            0,
            self.connection.execute("SELECT count(*) FROM memory_events").fetchone()[0],
        )
        self.connection.rollback()

    def test_event_times_reject_values_outside_signed_64_bit_range(self):
        """SQLite time columns have one portable integer domain, not host coercion."""
        command_type, store_type = self._api()
        command = command_type(
            self.workspace_id,
            "mem_" + "b" * 64,
            "memory",
            "memory.created",
            1,
            1,
            "system",
            {"record": self._state()},
        )
        for changed in (
            replace(command, occurred_at_us=2**63),
            replace(command, recorded_at_us=-(2**63) - 1),
        ):
            with (
                self.subTest(changed=changed),
                self.assertRaisesRegex(ValueError, "signed 64-bit"),
            ):
                store_type(self.connection).append_and_project(changed)
        self.assertEqual(
            0,
            self.connection.execute("SELECT count(*) FROM memory_events").fetchone()[0],
        )

    def test_memory_projection_rejects_lossy_scalar_coercions(self):
        """Canonical state and SQLite TEXT/REAL columns must never diverge."""
        command_type, store_type = self._api()
        store = store_type(self.connection)
        cases = (
            {"file_path": 7},
            {"source_client": ["client"]},
            {"surprise_score": 2.0},
            {"importance_score": True},
            {"deleted_at_us": "now"},
        )
        for index, changes in enumerate(cases):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                store.append_and_project(
                    command_type(
                        self.workspace_id,
                        "mem_" + str(index) * 64,
                        "memory",
                        "memory.created",
                        1,
                        1,
                        "system",
                        {"record": {**self._state(), **changes}},
                    )
                )
        self.assertEqual(
            0,
            self.connection.execute("SELECT count(*) FROM memory_events").fetchone()[0],
        )

    def test_fact_and_relationship_streams_create_typed_versions(self):
        """Non-memory streams project versioned typed assertions, not mutable rows."""
        command_type, store_type = self._api()
        store = store_type(self.connection)
        left = "mem_" + "4" * 64
        right = "mem_" + "5" * 64
        for record_id in (left, right):
            store.append_and_project(
                command_type(
                    workspace_id=self.workspace_id,
                    stream_id=record_id,
                    stream_kind="memory",
                    event_type="memory.created",
                    occurred_at_us=1,
                    recorded_at_us=1,
                    actor_type="system",
                    payload={"record": self._state(record_id)},
                )
            )
        fact_id = "fact_" + "6" * 64
        relationship_id = "rel_" + "7" * 64
        fact_event = store.append_and_project(
            command_type(
                workspace_id=self.workspace_id,
                stream_id=fact_id,
                stream_kind="fact",
                event_type="fact.asserted",
                occurred_at_us=10,
                recorded_at_us=11,
                actor_type="system",
                payload={
                    "fact": {
                        "subject_record_id": left,
                        "predicate": "uses",
                        "object_kind": "text",
                        "object": "SQLite",
                        "legacy_type": None,
                        "confidence": 0.9,
                        "verification_count": 1,
                        "is_verified": True,
                        "evidence": [],
                        "metadata": {},
                        "valid_from_us": 10,
                        "valid_to_us": None,
                    }
                },
            )
        )
        relationship_event = store.append_and_project(
            command_type(
                workspace_id=self.workspace_id,
                stream_id=relationship_id,
                stream_kind="relationship",
                event_type="relationship.created",
                occurred_at_us=20,
                recorded_at_us=21,
                actor_type="system",
                payload={
                    "relationship": {
                        "source_record_id": left,
                        "target_record_id": right,
                        "relationship_type": "depends_on",
                        "legacy_type": None,
                        "description": "fixture",
                        "confidence": 1.0,
                        "metadata": {},
                        "valid_from_us": 20,
                        "valid_to_us": None,
                    }
                },
            )
        )
        fact = self.connection.execute("SELECT * FROM memory_fact_versions").fetchone()
        relation = self.connection.execute(
            "SELECT * FROM memory_relationship_versions"
        ).fetchone()
        self.assertEqual(fact_id, fact["fact_id"])
        self.assertEqual(fact_event.event_id, fact["asserted_by_event_id"])
        self.assertEqual('"SQLite"', fact["object_json"])
        self.assertEqual(relationship_id, relation["relationship_id"])
        self.assertEqual(relationship_event.event_id, relation["asserted_by_event_id"])

    def test_relationship_removal_preserves_history_and_closes_prior_version(self):
        """Removal appends history and closes the prior open transaction."""
        command_type, store_type = self._api()
        store = store_type(self.connection)
        left = "mem_" + "8" * 64
        right = "mem_" + "9" * 64
        for record_id in (left, right):
            store.append_and_project(
                command_type(
                    workspace_id=self.workspace_id,
                    stream_id=record_id,
                    stream_kind="memory",
                    event_type="memory.created",
                    occurred_at_us=1,
                    recorded_at_us=1,
                    actor_type="system",
                    payload={"record": self._state(record_id)},
                )
            )
        relation_id = "rel_" + "a" * 64
        state = {
            "source_record_id": left,
            "target_record_id": right,
            "relationship_type": "related_to",
            "legacy_type": None,
            "description": None,
            "confidence": 1.0,
            "metadata": {},
            "valid_from_us": 10,
            "valid_to_us": None,
        }
        store.append_and_project(
            command_type(
                self.workspace_id,
                relation_id,
                "relationship",
                "relationship.created",
                10,
                11,
                "system",
                {"relationship": state},
            )
        )
        removed = store.append_and_project(
            command_type(
                self.workspace_id,
                relation_id,
                "relationship",
                "relationship.removed",
                30,
                31,
                "system",
                {"relationship": {**state, "valid_to_us": 30}},
            )
        )
        versions = self.connection.execute(
            "SELECT * FROM memory_relationship_versions ORDER BY version"
        ).fetchall()
        self.assertEqual([1, 2], [row["version"] for row in versions])
        self.assertEqual(31, versions[0]["transaction_to_us"])
        self.assertEqual(removed.event_id, versions[0]["retracted_by_event_id"])
        self.assertEqual(30, versions[1]["valid_to_us"])

    def test_extended_typed_relationship_vocabulary_projects(self):
        """Schema-supported evidence/derivation/invalidation edges are canonical."""
        command_type, store_type = self._api()
        store = store_type(self.connection)
        left = "mem_" + "b" * 64
        right = "mem_" + "c" * 64
        for record_id in (left, right):
            store.append_and_project(
                command_type(
                    self.workspace_id,
                    record_id,
                    "memory",
                    "memory.created",
                    1,
                    1,
                    "system",
                    {"record": self._state(record_id)},
                )
            )
        for index, relationship_type in enumerate(
            ("evidence_for", "derived_from", "invalidates"), 1
        ):
            store.append_and_project(
                command_type(
                    self.workspace_id,
                    "rel_" + str(index) * 64,
                    "relationship",
                    "relationship.created",
                    10 + index,
                    20 + index,
                    "system",
                    {
                        "relationship": {
                            "source_record_id": left,
                            "target_record_id": right,
                            "relationship_type": relationship_type,
                            "legacy_type": None,
                            "description": None,
                            "confidence": 1.0,
                            "metadata": {},
                            "valid_from_us": 10 + index,
                            "valid_to_us": None,
                        }
                    },
                )
            )
        self.assertEqual(
            ["derived_from", "evidence_for", "invalidates"],
            [
                row[0]
                for row in self.connection.execute(
                    "SELECT relationship_type FROM memory_relationship_versions "
                    "ORDER BY relationship_type"
                )
            ],
        )

    def test_concurrent_default_appends_serialize_without_stream_gap(self):
        """BEGIN IMMEDIATE makes two head reads become versions one and two."""
        command_type, store_type = self._api()
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "events.db"
            setup = sqlite3.connect(path)
            for statement in _migration_16_statements():
                setup.execute(statement)
            setup.commit()
            setup.close()
            stream_id = "mem_" + "d" * 64
            barrier = threading.Barrier(2)
            versions = []
            failures = []

            def append(content: str, timestamp: int) -> None:
                connection = sqlite3.connect(path, timeout=5)
                try:
                    barrier.wait()
                    result = store_type(connection).append_and_project(
                        command_type(
                            self.workspace_id,
                            stream_id,
                            "memory",
                            "memory.created" if timestamp == 1 else "memory.updated",
                            timestamp,
                            timestamp,
                            "system",
                            {"record": self._state(content)},
                        )
                    )
                    connection.commit()
                    versions.append(result.stream_version)
                except Exception as exc:  # pragma: no cover - asserted below
                    failures.append(exc)
                finally:
                    connection.close()

            threads = [
                threading.Thread(target=append, args=("first", 1)),
                threading.Thread(target=append, args=("second", 2)),
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
            self.assertEqual([], failures)
            self.assertEqual([1, 2], sorted(versions))
            verify = sqlite3.connect(path)
            rows = verify.execute(
                "SELECT stream_version,previous_event_hash,event_hash "
                "FROM memory_events ORDER BY stream_version"
            ).fetchall()
            verify.close()
            self.assertEqual([1, 2], [row[0] for row in rows])
            self.assertIsNone(rows[0][1])
            self.assertEqual(rows[0][2], rows[1][1])

    def test_resolves_migrated_compatibility_streams_without_identity_split(self):
        """A bundle-restored v7 DB has events but no legacy map; writes must reuse them."""
        from daem0nmcp.event_store import (
            EventCommand,
            EventStore,
            resolve_compatibility_stream,
        )

        memory_id = "mem_" + "c" * 64
        relationship_id = "rel_" + "d" * 64
        other_id = "mem_" + "e" * 64
        store = EventStore(self.connection)
        for record_id, legacy_id in ((memory_id, 42), (other_id, 43)):
            store.append_and_project(
                EventCommand(
                    workspace_id=self.workspace_id,
                    stream_id=record_id,
                    stream_kind="memory",
                    event_type="memory.created",
                    occurred_at_us=1,
                    recorded_at_us=1,
                    actor_type="migration",
                    payload={
                        "legacy": {
                            "table": "memories",
                            "columns": [["id", legacy_id], ["content", "legacy"]],
                        },
                        "record": self._state(str(legacy_id)),
                    },
                )
            )
        store.append_and_project(
            EventCommand(
                workspace_id=self.workspace_id,
                stream_id=relationship_id,
                stream_kind="relationship",
                event_type="relationship.created",
                occurred_at_us=2,
                recorded_at_us=2,
                actor_type="migration",
                payload={
                    "legacy": {
                        "table": "memory_relationships",
                        "columns": [["id", 9]],
                    },
                    "relationship": {
                        "source_record_id": memory_id,
                        "target_record_id": other_id,
                        "relationship_type": "related_to",
                        "legacy_type": None,
                        "description": None,
                        "confidence": 1.0,
                        "metadata": {},
                        "valid_from_us": 2,
                        "valid_to_us": None,
                    },
                },
            )
        )

        self.assertEqual(
            memory_id,
            resolve_compatibility_stream(
                self.connection, self.workspace_id, "memory", "memories", 42
            ),
        )
        self.assertEqual(
            relationship_id,
            resolve_compatibility_stream(
                self.connection,
                self.workspace_id,
                "relationship",
                "memory_relationships",
                9,
            ),
        )
        self.assertIsNone(
            resolve_compatibility_stream(
                self.connection, self.workspace_id, "memory", "memories", 404
            )
        )

    def test_ambiguous_compatibility_stream_fails_closed(self):
        """Corrupt/merged histories may not pick an arbitrary legacy stream."""
        from daem0nmcp.event_store import (
            CompatibilityStreamError,
            EventCommand,
            EventStore,
            resolve_compatibility_stream,
        )

        store = EventStore(self.connection)
        for suffix in ("f", "a"):
            store.append_and_project(
                EventCommand(
                    self.workspace_id,
                    "mem_" + suffix * 64,
                    "memory",
                    "memory.created",
                    1,
                    1,
                    "import",
                    {
                        "record": self._state(suffix),
                        "compatibility": {"legacy_memory_id": 77},
                    },
                )
            )

        with self.assertRaisesRegex(
            CompatibilityStreamError, "COMPATIBILITY_STREAM_AMBIGUOUS"
        ):
            resolve_compatibility_stream(
                self.connection, self.workspace_id, "memory", "memories", 77
            )

    def test_reused_highest_legacy_ids_resolve_only_the_new_live_streams(self):
        """Tombstoned memory/edge claims cannot capture a reused SQLite row ID."""
        from daem0nmcp.event_store import (
            EventCommand,
            EventStore,
            resolve_compatibility_stream,
        )

        store = EventStore(self.connection)
        run_id = "mig_" + "7" * 64
        old_memory = "mem_" + "1" * 64
        new_memory = "mem_" + "2" * 64
        other_memory = "mem_" + "3" * 64
        old_relationship = "rel_" + "4" * 64
        new_relationship = "rel_" + "5" * 64
        old_memory_event = store.append_and_project(
            EventCommand(
                self.workspace_id,
                old_memory,
                "memory",
                "memory.created",
                1,
                1,
                "migration",
                {
                    "record": self._state("old highest row"),
                    "compatibility": {"legacy_memory_id": 42},
                },
            )
        )
        store.append_and_project(
            EventCommand(
                self.workspace_id,
                other_memory,
                "memory",
                "memory.created",
                1,
                1,
                "system",
                {"record": self._state("other endpoint")},
            )
        )
        self.connection.execute(
            """
            INSERT INTO v7_migration_runs (
                migration_run_id, workspace_id, source_db_sha256,
                source_schema_version, source_format_version,
                target_format_version, status, snapshot_name, candidate_name,
                source_inventory_json, created_at_us, updated_at_us
            ) VALUES (?, ?, ?, 16, 6, 7, 'active', 'source.snapshot.db',
                      'candidate.db', '{}', 1, 1)
            """,
            (run_id, self.workspace_id, "8" * 64),
        )
        self.connection.execute(
            """
            INSERT INTO legacy_id_map (
                migration_run_id, source_table, legacy_id, workspace_id,
                target_kind, target_id, source_row_hash, imported_event_id
            ) VALUES (?, 'memories', '42', ?, 'memory', ?, ?, ?)
            """,
            (
                run_id,
                self.workspace_id,
                old_memory,
                "9" * 64,
                old_memory_event.event_id,
            ),
        )
        store.append_and_project(
            EventCommand(
                self.workspace_id,
                old_memory,
                "memory",
                "memory.deleted",
                2,
                2,
                "system",
                {
                    "record": {
                        **self._state("old highest row"),
                        "deleted_at_us": 2,
                    },
                    "compatibility": {"legacy_memory_id": 42},
                },
            )
        )
        self.assertIsNone(
            resolve_compatibility_stream(
                self.connection, self.workspace_id, "memory", "memories", 42
            )
        )
        store.append_and_project(
            EventCommand(
                self.workspace_id,
                new_memory,
                "memory",
                "memory.created",
                3,
                3,
                "system",
                {
                    "record": self._state("new highest row"),
                    "compatibility": {"legacy_memory_id": 42},
                },
            )
        )
        self.assertEqual(
            new_memory,
            resolve_compatibility_stream(
                self.connection, self.workspace_id, "memory", "memories", 42
            ),
        )

        relationship_state = {
            "source_record_id": new_memory,
            "target_record_id": other_memory,
            "relationship_type": "related_to",
            "legacy_type": None,
            "description": None,
            "confidence": 1.0,
            "metadata": {"legacy_relationship_id": 99},
            "valid_from_us": 4,
            "valid_to_us": None,
        }
        old_relationship_event = store.append_and_project(
            EventCommand(
                self.workspace_id,
                old_relationship,
                "relationship",
                "relationship.created",
                4,
                4,
                "system",
                {"relationship": relationship_state},
            )
        )
        self.connection.execute(
            """
            INSERT INTO legacy_id_map (
                migration_run_id, source_table, legacy_id, workspace_id,
                target_kind, target_id, source_row_hash, imported_event_id
            ) VALUES (?, 'memory_relationships', '99', ?, 'relationship', ?, ?, ?)
            """,
            (
                run_id,
                self.workspace_id,
                old_relationship,
                "a" * 64,
                old_relationship_event.event_id,
            ),
        )
        store.append_and_project(
            EventCommand(
                self.workspace_id,
                old_relationship,
                "relationship",
                "relationship.removed",
                5,
                5,
                "system",
                {
                    "relationship": {
                        **relationship_state,
                        "valid_to_us": 5,
                    }
                },
            )
        )
        self.assertIsNone(
            resolve_compatibility_stream(
                self.connection,
                self.workspace_id,
                "relationship",
                "memory_relationships",
                99,
            )
        )
        store.append_and_project(
            EventCommand(
                self.workspace_id,
                new_relationship,
                "relationship",
                "relationship.created",
                6,
                6,
                "system",
                {
                    "relationship": {
                        **relationship_state,
                        "valid_from_us": 6,
                    }
                },
            )
        )
        self.assertEqual(
            new_relationship,
            resolve_compatibility_stream(
                self.connection,
                self.workspace_id,
                "relationship",
                "memory_relationships",
                99,
            ),
        )


if __name__ == "__main__":
    unittest.main()
