"""Dependency-free v7 event export/import contract tests."""

from __future__ import annotations

import copy
import hashlib
import importlib.util
import sqlite3
import unittest
from pathlib import Path


def _migration_16_statements():
    path = (
        Path(__file__).resolve().parents[1] / "daem0nmcp" / "migrations" / "schema.py"
    )
    spec = importlib.util.spec_from_file_location("bundle_test_schema", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return next(item[2] for item in module.MIGRATIONS if item[0] == 16)


class EventBundleTests(unittest.TestCase):
    workspace_id = "ws_0123456789abcdef01234567"

    def _connection(self) -> sqlite3.Connection:
        connection = sqlite3.connect(":memory:")
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        for statement in _migration_16_statements():
            connection.execute(statement)
        connection.commit()
        return connection

    @staticmethod
    def _record(content: str, *, archived: bool = False) -> dict:
        return {
            "record_type": "decision",
            "legacy_type": None,
            "content": content,
            "rationale": None,
            "context": {},
            "tags": ["bundle"],
            "file_path": None,
            "file_path_relative": None,
            "keywords": None,
            "is_permanent": False,
            "pinned": False,
            "archived": archived,
            "outcome": None,
            "worked": None,
            "recall_count": 0,
            "surprise_score": None,
            "importance_score": None,
            "source_client": "test",
            "source_model": None,
            "deleted_at_us": None,
        }

    def _source(self) -> sqlite3.Connection:
        from daem0nmcp.event_store import EventCommand, EventStore

        connection = self._connection()
        store = EventStore(connection)
        stream_a = "mem_" + "a" * 64
        stream_b = "mem_" + "b" * 64
        store.append_and_project(
            EventCommand(
                workspace_id=self.workspace_id,
                stream_id=stream_a,
                stream_kind="memory",
                event_type="memory.created",
                occurred_at_us=10,
                recorded_at_us=11,
                actor_type="import",
                correlation_id="fixture",
                payload={"record": self._record("alpha")},
            )
        )
        store.append_and_project(
            EventCommand(
                workspace_id=self.workspace_id,
                stream_id=stream_a,
                stream_kind="memory",
                event_type="memory.archived_set",
                occurred_at_us=20,
                recorded_at_us=21,
                actor_type="import",
                correlation_id="fixture",
                payload={"record": self._record("alpha", archived=True)},
            )
        )
        store.append_and_project(
            EventCommand(
                workspace_id=self.workspace_id,
                stream_id=stream_b,
                stream_kind="memory",
                event_type="memory.created",
                occurred_at_us=12,
                recorded_at_us=13,
                actor_type="import",
                payload={"record": self._record("beta")},
            )
        )
        connection.commit()
        return connection

    def test_export_is_canonical_ordered_and_round_trips_exact_state(self) -> None:
        from daem0nmcp.event_store import export_event_bundle, import_event_bundle

        source = self._source()
        target = self._connection()
        self.addCleanup(source.close)
        self.addCleanup(target.close)

        bundle = export_event_bundle(source, self.workspace_id)
        self.assertEqual(self.workspace_id, bundle["workspace_id"])
        self.assertEqual(1, bundle["event_schema_version"])
        self.assertEqual(
            sorted(event["event_id"] for event in bundle["events"]),
            [event["event_id"] for event in bundle["events"]],
        )
        expected_root = hashlib.sha256(
            b"".join(bytes.fromhex(event["event_hash"]) for event in bundle["events"])
        ).hexdigest()
        self.assertEqual(expected_root, bundle["root_hash"])

        first = import_event_bundle(target, bundle, self.workspace_id)
        target.commit()
        second = import_event_bundle(target, bundle, self.workspace_id)
        target.commit()
        self.assertEqual(3, first.events_imported)
        self.assertEqual(0, second.events_imported)
        self.assertEqual(3, second.events_existing)
        self.assertEqual(
            source.execute(
                "SELECT record_id,state_hash FROM memory_records ORDER BY record_id"
            ).fetchall(),
            target.execute(
                "SELECT record_id,state_hash FROM memory_records ORDER BY record_id"
            ).fetchall(),
        )
        self.assertEqual(bundle, export_event_bundle(target, self.workspace_id))

    def test_tamper_or_cross_workspace_rejects_before_any_write(self) -> None:
        from daem0nmcp.event_store import (
            EventBundleError,
            export_event_bundle,
            import_event_bundle,
        )

        source = self._source()
        self.addCleanup(source.close)
        original = export_event_bundle(source, self.workspace_id)

        cases = []
        payload = copy.deepcopy(original)
        payload["events"][0]["payload"]["record"]["content"] = "tampered"
        cases.append(payload)
        chain = copy.deepcopy(original)
        chained = next(
            event for event in chain["events"] if event["stream_version"] == 2
        )
        chained["previous_event_hash"] = "0" * 64
        cases.append(chain)
        root = copy.deepcopy(original)
        root["root_hash"] = "0" * 64
        cases.append(root)

        for bundle in cases:
            with self.subTest(kind=bundle):
                target = self._connection()
                with self.assertRaises(EventBundleError):
                    import_event_bundle(target, bundle, self.workspace_id)
                self.assertEqual(
                    0,
                    target.execute("SELECT count(*) FROM memory_events").fetchone()[0],
                )
                target.close()

        target = self._connection()
        with self.assertRaisesRegex(
            EventBundleError, "CROSS_WORKSPACE_IMPORT_UNSUPPORTED"
        ):
            import_event_bundle(target, original, "ws_" + "9" * 24)
        self.assertEqual(
            0, target.execute("SELECT count(*) FROM memory_events").fetchone()[0]
        )
        target.close()

    def test_malformed_bundle_shapes_fail_with_stable_error_before_write(self) -> None:
        from daem0nmcp.event_store import (
            EventBundleError,
            export_event_bundle,
            import_event_bundle,
        )

        source = self._source()
        self.addCleanup(source.close)
        original = export_event_bundle(source, self.workspace_id)
        malformed = []
        missing_id = copy.deepcopy(original)
        missing_id["events"][0].pop("event_id")
        malformed.append(missing_id)
        nontext_id = copy.deepcopy(original)
        nontext_id["events"][0]["event_id"] = 7
        malformed.append(nontext_id)
        bad_version = copy.deepcopy(original)
        bad_version["events"][0]["stream_version"] = "1"
        malformed.append(bad_version)
        bad_root = copy.deepcopy(original)
        bad_root["root_hash"] = 7
        malformed.append(bad_root)

        for bundle in malformed:
            target = self._connection()
            with self.subTest(bundle=bundle):
                with self.assertRaisesRegex(EventBundleError, "INVALID_EVENT_BUNDLE"):
                    import_event_bundle(target, bundle, self.workspace_id)
                self.assertEqual(
                    0,
                    target.execute("SELECT count(*) FROM memory_events").fetchone()[0],
                )
            target.close()

    def test_cross_stream_causation_restores_before_dependent_event(self) -> None:
        from daem0nmcp.event_store import (
            EventCommand,
            EventStore,
            export_event_bundle,
            import_event_bundle,
        )

        source = self._connection()
        target = self._connection()
        self.addCleanup(source.close)
        self.addCleanup(target.close)
        store = EventStore(source)
        cause = store.append_and_project(
            EventCommand(
                self.workspace_id,
                "mem_" + "f" * 64,
                "memory",
                "memory.created",
                1,
                1,
                "import",
                {"record": self._record("cause")},
            )
        )
        store.append_and_project(
            EventCommand(
                self.workspace_id,
                "mem_" + "0" * 64,
                "memory",
                "memory.created",
                2,
                2,
                "import",
                {"record": self._record("effect")},
                causation_event_id=cause.event_id,
            )
        )
        source.commit()
        bundle = export_event_bundle(source, self.workspace_id)
        result = import_event_bundle(target, bundle, self.workspace_id)
        self.assertEqual(2, result.events_imported)
        self.assertIsNone(target.execute("PRAGMA foreign_key_check").fetchone())


if __name__ == "__main__":
    unittest.main()
