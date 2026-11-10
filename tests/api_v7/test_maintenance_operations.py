from __future__ import annotations

import asyncio
import inspect
import sqlite3
import tempfile
import threading
import unittest
from contextlib import closing, contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import MappingProxyType

from daem0nmcp.api.v7.application import AdmittedRequest

NOW = datetime(2026, 8, 9, 12, 0, tzinfo=timezone.utc)
SELECTION_SECRET = b"maintenance-selection-secret-key!"
PREFLIGHT_TOKEN = "p" * 32


def _apply_v7_schema(connection: sqlite3.Connection) -> None:
    from daem0nmcp.migrations.schema import MIGRATIONS
    from daem0nmcp.schema_version import CURRENT_SCHEMA_VERSION

    connection.execute("CREATE TABLE schema_version (version INTEGER PRIMARY KEY)")
    for version, _description, statements in MIGRATIONS:
        if 16 <= version <= CURRENT_SCHEMA_VERSION:
            for statement in statements:
                connection.execute(statement)
            connection.execute(
                "INSERT INTO schema_version(version) VALUES (?)", (version,)
            )
    connection.commit()


def _request(tool_name: str, **arguments: object) -> AdmittedRequest:
    from daem0nmcp.api.v7.tools import TOOL_INPUT_MODELS

    model = TOOL_INPUT_MODELS[tool_name].model_validate(arguments)
    effective = model.model_dump(mode="python")
    effective.pop("preflight_token", None)
    return AdmittedRequest(tool_name, MappingProxyType(effective))


class MaintenanceOperationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        from daem0nmcp.storage_activation import (
            ActiveDatabasePointer,
            write_active_pointer,
        )
        from daem0nmcp.workspace import WorkspaceRegistry

        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.storage = self.root / ".daem0nmcp" / "storage"
        self.storage.mkdir(parents=True)
        self.database = self.storage / "daem0nmcp.db"
        with closing(sqlite3.connect(self.database)) as connection:
            connection.execute("PRAGMA foreign_keys=ON")
            _apply_v7_schema(connection)
        write_active_pointer(
            self.storage,
            ActiveDatabasePointer(7, 1, self.database.name, None, None),
        )
        self.workspace = WorkspaceRegistry([self.root], default_root=self.root).default
        self.dependencies = None

    async def asyncTearDown(self) -> None:
        if self.dependencies is not None:
            self.dependencies.close()

    def _operations(self, **changes: object):
        from daem0nmcp.api.v7.maintenance_operations import (
            MaintenanceOperationDependencies,
            build_maintenance_operations,
        )

        options: dict[str, object] = {
            "clock": lambda: NOW,
            "selection_secret": SELECTION_SECRET,
        }
        options.update(changes)
        self.dependencies = MaintenanceOperationDependencies(**options)
        return build_maintenance_operations(self.dependencies)

    def test_dependencies_own_secure_token_key_and_bounded_pool(self) -> None:
        """An implicit weak key or unowned executor would break token isolation."""
        from daem0nmcp.api.v7.maintenance_operations import (
            MaintenanceOperationDependencies,
        )

        dependencies = MaintenanceOperationDependencies()
        self.addCleanup(dependencies.close)
        self.assertGreaterEqual(len(dependencies.selection_secret), 32)
        with self.assertRaisesRegex(ValueError, "at least 32 bytes"):
            MaintenanceOperationDependencies(selection_secret=b"x" * 31)
        with self.assertRaisesRegex(ValueError, "between 1 and 3600"):
            MaintenanceOperationDependencies(
                selection_secret=SELECTION_SECRET,
                selection_ttl_seconds=0,
            )

    def _append_record(
        self,
        suffix: str,
        content: str,
        *,
        created_at: datetime,
        record_type: str = "decision",
        tags: list[str] | None = None,
        context: dict[str, object] | None = None,
        rationale: str | None = None,
        is_permanent: bool = False,
        pinned: bool = False,
        archived: bool = False,
        outcome: str | None = None,
        worked: bool | None = None,
        recall_count: int = 0,
        informed_by: list[str] | None = None,
    ) -> str:
        from daem0nmcp.event_store import EventCommand, EventStore

        record_id = "mem_" + suffix * 64
        occurred_at_us = int(created_at.timestamp() * 1_000_000)
        provenance = (
            {} if informed_by is None else {"provenance": {"informed_by": informed_by}}
        )
        with closing(sqlite3.connect(self.database)) as connection:
            connection.execute("PRAGMA foreign_keys=ON")
            EventStore(connection).append_and_project(
                EventCommand(
                    workspace_id=self.workspace.workspace_id,
                    stream_id=record_id,
                    stream_kind="memory",
                    event_type="memory.created",
                    occurred_at_us=occurred_at_us,
                    recorded_at_us=occurred_at_us,
                    actor_type="system",
                    payload={
                        "record": {
                            "record_type": record_type,
                            "legacy_type": None,
                            "content": content,
                            "rationale": rationale,
                            "context": {} if context is None else context,
                            "tags": [] if tags is None else tags,
                            "file_path": None,
                            "file_path_relative": None,
                            "keywords": None,
                            "is_permanent": is_permanent,
                            "pinned": pinned,
                            "archived": archived,
                            "outcome": outcome,
                            "worked": worked,
                            "recall_count": recall_count,
                            "surprise_score": None,
                            "importance_score": None,
                            "source_client": "maintenance-test",
                            "source_model": None,
                            "deleted_at_us": None,
                        },
                        **provenance,
                    },
                )
            )
            connection.commit()
        return record_id

    async def test_registry_is_exact_immutable_and_keyword_only(self) -> None:
        """A mutable or positional registry could bypass v7 admission."""
        operations = self._operations()

        self.assertEqual(
            {
                "dream_duplicates_preview",
                "dream_duplicates_purge",
                "memory_compact",
                "memory_compaction_preview",
                "memory_duplicates_cleanup",
                "memory_duplicates_preview",
                "memory_prune",
                "memory_prune_preview",
            },
            set(operations),
        )
        with self.assertRaises(TypeError):
            operations["memory_prune"] = object()
        for operation in operations.values():
            parameters = tuple(inspect.signature(operation).parameters.values())
            self.assertEqual(
                ("workspace", "request"), tuple(item.name for item in parameters)
            )
            self.assertTrue(
                all(item.kind is inspect.Parameter.KEYWORD_ONLY for item in parameters)
            )

    async def test_prune_preview_and_mutation_are_snapshot_bound_and_replay_safe(
        self,
    ) -> None:
        """A changed snapshot or replay must never prune a different record set."""
        from daem0nmcp.api.v7.models import DestructiveMutationReceipt, Preview

        old = NOW - timedelta(days=100)
        selected_id = self._append_record(
            "a", "Low-value old decision.", created_at=old
        )
        self._append_record("b", "Pinned old decision.", created_at=old, pinned=True)
        self._append_record(
            "c",
            "Completed old decision.",
            created_at=old,
            outcome="It worked.",
            worked=True,
        )
        self._append_record(
            "d", "Frequently recalled decision.", created_at=old, recall_count=5
        )
        self._append_record("e", "Recent decision.", created_at=NOW - timedelta(days=1))
        operations = self._operations()
        preview_request = _request(
            "memory_prune_preview",
            workspace_id=self.workspace.workspace_id,
        )

        preview = await operations["memory_prune_preview"](
            workspace=self.workspace,
            request=preview_request,
        )

        self.assertIsInstance(preview, Preview)
        self.assertEqual(1, preview.counts["selected"])
        self.assertEqual([selected_id], preview.sample_ids)
        self.assertTrue(preview.selection_token.startswith("sel_v1."))
        self.assertNotIn(str(self.root), preview.model_dump_json())
        mutation_request = _request(
            "memory_prune",
            workspace_id=self.workspace.workspace_id,
            selection_token=preview.selection_token,
            preflight_token=PREFLIGHT_TOKEN,
        )
        receipt = await operations["memory_prune"](
            workspace=self.workspace,
            request=mutation_request,
        )
        replay = await operations["memory_prune"](
            workspace=self.workspace,
            request=mutation_request,
        )

        self.assertIsInstance(receipt, DestructiveMutationReceipt)
        self.assertEqual(
            (1, 1, 0),
            (
                receipt.selected_count,
                receipt.changed_count,
                receipt.skipped_count,
            ),
        )
        self.assertEqual([selected_id], receipt.affected_ids)
        self.assertEqual(1, len(receipt.event_ids))
        self.assertFalse(receipt.idempotent_replay)
        self.assertTrue(replay.idempotent_replay)
        self.assertEqual(receipt.event_ids, replay.event_ids)
        with closing(sqlite3.connect(self.database)) as connection:
            state = connection.execute(
                "SELECT deleted_at_us FROM memory_records WHERE record_id=?",
                (selected_id,),
            ).fetchone()
            event_types = connection.execute(
                "SELECT event_type FROM memory_events WHERE stream_id=? "
                "ORDER BY stream_version",
                (selected_id,),
            ).fetchall()
        self.assertEqual(int(NOW.timestamp() * 1_000_000), state[0])
        self.assertEqual([("memory.created",), ("memory.deleted",)], event_types)

    async def test_prune_rejects_tampered_mismatched_and_stale_selection_tokens(
        self,
    ) -> None:
        """Unsigned criteria changes or post-preview events must fail closed."""
        from daem0nmcp.api.v7.maintenance_operations import (
            MaintenanceOperationError,
        )

        old = NOW - timedelta(days=100)
        self._append_record("a", "First old record.", created_at=old)
        operations = self._operations()
        preview = await operations["memory_prune_preview"](
            workspace=self.workspace,
            request=_request(
                "memory_prune_preview",
                workspace_id=self.workspace.workspace_id,
            ),
        )

        tampered = preview.selection_token[:-1] + (
            "a" if preview.selection_token[-1] != "a" else "b"
        )
        with self.assertRaises(MaintenanceOperationError) as invalid:
            await operations["memory_prune"](
                workspace=self.workspace,
                request=_request(
                    "memory_prune",
                    workspace_id=self.workspace.workspace_id,
                    selection_token=tampered,
                    preflight_token=PREFLIGHT_TOKEN,
                ),
            )
        self.assertEqual("TOKEN_TAMPERED", invalid.exception.code)
        with self.assertRaises(MaintenanceOperationError) as mismatched:
            await operations["memory_prune"](
                workspace=self.workspace,
                request=_request(
                    "memory_prune",
                    workspace_id=self.workspace.workspace_id,
                    older_than_days=91,
                    selection_token=preview.selection_token,
                    preflight_token=PREFLIGHT_TOKEN,
                ),
            )
        self.assertEqual("TOKEN_ARGUMENT_MISMATCH", mismatched.exception.code)

        self._append_record("b", "Second old record.", created_at=old)
        with self.assertRaises(MaintenanceOperationError) as stale:
            await operations["memory_prune"](
                workspace=self.workspace,
                request=_request(
                    "memory_prune",
                    workspace_id=self.workspace.workspace_id,
                    selection_token=preview.selection_token,
                    preflight_token=PREFLIGHT_TOKEN,
                ),
            )
        self.assertEqual("CONFLICT", stale.exception.code)

    async def test_selection_token_binds_workspace_tool_and_expiry(self) -> None:
        """A preview token is valid only for its exact scoped mutation window."""
        from daem0nmcp.api.v7.maintenance_operations import (
            MaintenanceOperationError,
        )
        from daem0nmcp.workspace import WorkspaceRegistry

        current = [NOW]
        self._append_record("a", "Old candidate.", created_at=NOW - timedelta(days=100))
        operations = self._operations(
            clock=lambda: current[0],
            selection_ttl_seconds=1,
        )
        preview = await operations["memory_prune_preview"](
            workspace=self.workspace,
            request=_request(
                "memory_prune_preview",
                workspace_id=self.workspace.workspace_id,
            ),
        )
        other_root = self.root / "other"
        other_root.mkdir()
        other_workspace = WorkspaceRegistry(
            [other_root], default_root=other_root
        ).default

        with self.assertRaises(MaintenanceOperationError) as wrong_workspace:
            await operations["memory_prune"](
                workspace=other_workspace,
                request=_request(
                    "memory_prune",
                    workspace_id=other_workspace.workspace_id,
                    selection_token=preview.selection_token,
                    preflight_token=PREFLIGHT_TOKEN,
                ),
            )
        self.assertEqual("TOKEN_SCOPE_MISMATCH", wrong_workspace.exception.code)
        with self.assertRaises(MaintenanceOperationError) as wrong_tool:
            await operations["dream_duplicates_purge"](
                workspace=self.workspace,
                request=_request(
                    "dream_duplicates_purge",
                    workspace_id=self.workspace.workspace_id,
                    selection_token=preview.selection_token,
                    preflight_token=PREFLIGHT_TOKEN,
                ),
            )
        self.assertEqual("TOKEN_OPERATION_MISMATCH", wrong_tool.exception.code)

        current[0] = NOW + timedelta(seconds=1)
        with self.assertRaises(MaintenanceOperationError) as expired:
            await operations["memory_prune"](
                workspace=self.workspace,
                request=_request(
                    "memory_prune",
                    workspace_id=self.workspace.workspace_id,
                    selection_token=preview.selection_token,
                    preflight_token=PREFLIGHT_TOKEN,
                ),
            )
        self.assertEqual("TOKEN_EXPIRED", expired.exception.code)

    async def test_duplicate_cleanup_merges_keeper_and_tombstones_exact_candidates(
        self,
    ) -> None:
        """Cleanup must preserve merged state without deleting canonical history."""
        old = NOW - timedelta(days=3)
        duplicate_id = self._append_record(
            "a",
            "  Use   the Durable Queue ",
            created_at=old,
            tags=["old"],
            pinned=True,
            outcome="The old attempt worked.",
            worked=True,
        )
        keeper_id = self._append_record(
            "b",
            "use the durable queue",
            created_at=NOW - timedelta(days=2),
            tags=["new"],
        )
        self._append_record(
            "c", "A distinct memory.", created_at=NOW - timedelta(days=1)
        )
        operations = self._operations()
        preview = await operations["memory_duplicates_preview"](
            workspace=self.workspace,
            request=_request(
                "memory_duplicates_preview",
                workspace_id=self.workspace.workspace_id,
            ),
        )

        self.assertEqual(
            {"duplicate_groups": 1, "eligible": 1, "remaining": 0, "selected": 1},
            preview.counts,
        )
        self.assertEqual([duplicate_id], preview.sample_ids)
        request = _request(
            "memory_duplicates_cleanup",
            workspace_id=self.workspace.workspace_id,
            selection_token=preview.selection_token,
            preflight_token=PREFLIGHT_TOKEN,
        )
        receipt = await operations["memory_duplicates_cleanup"](
            workspace=self.workspace,
            request=request,
        )
        replay = await operations["memory_duplicates_cleanup"](
            workspace=self.workspace,
            request=request,
        )

        self.assertEqual(
            (1, 1, 0),
            (
                receipt.selected_count,
                receipt.changed_count,
                receipt.skipped_count,
            ),
        )
        self.assertEqual([keeper_id, duplicate_id], receipt.affected_ids)
        self.assertEqual(2, len(receipt.event_ids))
        self.assertTrue(replay.idempotent_replay)
        self.assertEqual(receipt.event_ids, replay.event_ids)
        with closing(sqlite3.connect(self.database)) as connection:
            connection.row_factory = sqlite3.Row
            duplicate = connection.execute(
                "SELECT deleted_at_us FROM memory_records WHERE record_id=?",
                (duplicate_id,),
            ).fetchone()
            keeper = connection.execute(
                "SELECT tags_json,pinned,outcome,worked,deleted_at_us "
                "FROM memory_records WHERE record_id=?",
                (keeper_id,),
            ).fetchone()
            event_types = connection.execute(
                "SELECT stream_id,event_type FROM memory_events "
                "WHERE event_id IN (?,?) ORDER BY rowid",
                tuple(receipt.event_ids),
            ).fetchall()
        self.assertEqual(int(NOW.timestamp() * 1_000_000), duplicate[0])
        self.assertEqual('["new","old"]', keeper["tags_json"])
        self.assertEqual(
            (1, "The old attempt worked.", 1, None),
            (
                keeper["pinned"],
                keeper["outcome"],
                keeper["worked"],
                keeper["deleted_at_us"],
            ),
        )
        self.assertEqual(
            [
                (keeper_id, "memory.duplicates_merged"),
                (duplicate_id, "memory.deleted"),
            ],
            [tuple(row) for row in event_types],
        )

    async def test_duplicate_preview_with_merge_disabled_selects_nothing(
        self,
    ) -> None:
        """The explicit merge_duplicates=false criterion must remain non-mutating."""
        self._append_record("a", "Same content.", created_at=NOW - timedelta(days=2))
        self._append_record(
            "b", " same   CONTENT. ", created_at=NOW - timedelta(days=1)
        )
        operations = self._operations()
        preview = await operations["memory_duplicates_preview"](
            workspace=self.workspace,
            request=_request(
                "memory_duplicates_preview",
                workspace_id=self.workspace.workspace_id,
                merge_duplicates=False,
            ),
        )
        self.assertEqual(1, preview.counts["eligible"])
        self.assertEqual(0, preview.counts["selected"])

        receipt = await operations["memory_duplicates_cleanup"](
            workspace=self.workspace,
            request=_request(
                "memory_duplicates_cleanup",
                workspace_id=self.workspace.workspace_id,
                merge_duplicates=False,
                selection_token=preview.selection_token,
                preflight_token=PREFLIGHT_TOKEN,
            ),
        )
        self.assertEqual(
            (0, 0, [], []),
            (
                receipt.selected_count,
                receipt.changed_count,
                receipt.affected_ids,
                receipt.event_ids,
            ),
        )
        with closing(sqlite3.connect(self.database)) as connection:
            live = connection.execute(
                "SELECT count(*) FROM memory_records WHERE deleted_at_us IS NULL"
            ).fetchone()[0]
        self.assertEqual(2, live)

    async def test_compaction_appends_summary_archives_and_supersession_atomically(
        self,
    ) -> None:
        """Compaction must preserve every source through canonical event streams."""
        first_id = self._append_record(
            "a",
            "Queue retry learning.",
            created_at=NOW - timedelta(days=5),
            record_type="learning",
            tags=["queue"],
        )
        second_id = self._append_record(
            "b",
            "Queue durability decision.",
            created_at=NOW - timedelta(days=4),
            outcome="The queue stayed durable.",
            worked=True,
            tags=["queue"],
        )
        self._append_record(
            "c",
            "Pending queue decision.",
            created_at=NOW - timedelta(days=3),
        )
        self._append_record(
            "d",
            "Pinned queue learning.",
            created_at=NOW - timedelta(days=2),
            record_type="learning",
            pinned=True,
        )
        summary = "The durable queue needs bounded retries and verified outcomes."
        operations = self._operations()
        preview = await operations["memory_compaction_preview"](
            workspace=self.workspace,
            request=_request(
                "memory_compaction_preview",
                workspace_id=self.workspace.workspace_id,
                summary=summary,
                query="queue",
                limit=10,
            ),
        )
        self.assertEqual([first_id, second_id], preview.sample_ids)
        self.assertEqual(2, preview.counts["selected"])
        with closing(sqlite3.connect(self.database)) as connection:
            source_event_ids = [
                connection.execute(
                    "SELECT source_event_id FROM memory_records WHERE record_id=?",
                    (record_id,),
                ).fetchone()[0]
                for record_id in (first_id, second_id)
            ]
        request = _request(
            "memory_compact",
            workspace_id=self.workspace.workspace_id,
            summary=summary,
            query="queue",
            limit=10,
            selection_token=preview.selection_token,
            idempotency_key="compact-queue-history-0001",
            preflight_token=PREFLIGHT_TOKEN,
        )

        result = await operations["memory_compact"](
            workspace=self.workspace,
            request=request,
        )
        replay = await operations["memory_compact"](
            workspace=self.workspace,
            request=request,
        )

        self.assertEqual("learning", result.summary_record.record_type)
        self.assertEqual(summary, result.summary_record.excerpt)
        self.assertEqual(source_event_ids, result.source_event_ids)
        self.assertEqual(
            (2, 2, 0),
            (
                result.receipt.selected_count,
                result.receipt.changed_count,
                result.receipt.skipped_count,
            ),
        )
        self.assertEqual(5, len(result.receipt.event_ids))
        self.assertEqual(result.receipt.event_ids, replay.receipt.event_ids)
        self.assertTrue(replay.receipt.idempotent_replay)
        summary_id = result.summary_record.record_id
        with closing(sqlite3.connect(self.database)) as connection:
            connection.row_factory = sqlite3.Row
            sources = connection.execute(
                "SELECT record_id,archived,deleted_at_us FROM memory_records "
                "WHERE record_id IN (?,?) ORDER BY record_id",
                (first_id, second_id),
            ).fetchall()
            summary_row = connection.execute(
                "SELECT tags_json,context_json FROM memory_records WHERE record_id=?",
                (summary_id,),
            ).fetchone()
            relationships = connection.execute(
                "SELECT source_record_id,target_record_id,relationship_type "
                "FROM memory_relationship_versions WHERE workspace_id=? "
                "AND transaction_to_us IS NULL ORDER BY target_record_id",
                (self.workspace.workspace_id,),
            ).fetchall()
            emitted = connection.execute(
                "SELECT event_type FROM memory_events WHERE event_id IN ("
                + ",".join("?" for _ in result.receipt.event_ids)
                + ") ORDER BY rowid",
                tuple(result.receipt.event_ids),
            ).fetchall()
        self.assertEqual(
            [(first_id, 1, None), (second_id, 1, None)],
            [tuple(row) for row in sources],
        )
        self.assertEqual('["checkpoint","compacted"]', summary_row["tags_json"])
        self.assertIn(first_id, summary_row["context_json"])
        self.assertEqual(
            [
                (summary_id, first_id, "supersedes"),
                (summary_id, second_id, "supersedes"),
            ],
            [tuple(row) for row in relationships],
        )
        self.assertEqual(
            [
                ("memory.created",),
                ("memory.compaction_archived",),
                ("relationship.created",),
                ("memory.compaction_archived",),
                ("relationship.created",),
            ],
            [tuple(row) for row in emitted],
        )

    async def test_compaction_refuses_an_empty_selection(self) -> None:
        """A summary record must never claim to compact an empty source set."""
        from daem0nmcp.api.v7.maintenance_operations import (
            MaintenanceOperationError,
        )

        operations = self._operations()
        preview = await operations["memory_compaction_preview"](
            workspace=self.workspace,
            request=_request(
                "memory_compaction_preview",
                workspace_id=self.workspace.workspace_id,
                summary="Nothing matched this bounded compaction request.",
            ),
        )
        self.assertEqual(0, preview.counts["selected"])
        with self.assertRaises(MaintenanceOperationError) as raised:
            await operations["memory_compact"](
                workspace=self.workspace,
                request=_request(
                    "memory_compact",
                    workspace_id=self.workspace.workspace_id,
                    summary="Nothing matched this bounded compaction request.",
                    selection_token=preview.selection_token,
                    idempotency_key="compact-empty-selection-0001",
                    preflight_token=PREFLIGHT_TOKEN,
                ),
            )
        self.assertEqual("NOT_FOUND", raised.exception.code)

    async def test_dream_purge_keeps_latest_per_source_and_day(self) -> None:
        """Dream cleanup uses explicit tags and UTC days, never vectors."""
        source_id = "mem_" + "f" * 64
        old_reeval = self._append_record(
            "a",
            "Older dream re-evaluation.",
            created_at=NOW - timedelta(days=3),
            record_type="learning",
            tags=["dream", "re-evaluation", f"source-decision:{source_id}"],
        )
        new_reeval = self._append_record(
            "b",
            "Newer dream re-evaluation.",
            created_at=NOW - timedelta(days=2),
            record_type="learning",
            tags=["dream", "re-evaluation", f"source-decision:{source_id}"],
        )
        summary_day = NOW - timedelta(days=1)
        old_summary = self._append_record(
            "c",
            "Earlier dream summary.",
            created_at=summary_day.replace(hour=8),
            record_type="learning",
            tags=["dream", "dream-summary"],
        )
        new_summary = self._append_record(
            "d",
            "Later dream summary.",
            created_at=summary_day.replace(hour=9),
            record_type="learning",
            tags=["dream", "dream-summary"],
        )
        self._append_record(
            "e",
            "Dream-tagged but not a recognized duplicate kind.",
            created_at=NOW,
            record_type="learning",
            tags=["dream"],
        )
        operations = self._operations()
        preview = await operations["dream_duplicates_preview"](
            workspace=self.workspace,
            request=_request(
                "dream_duplicates_preview",
                workspace_id=self.workspace.workspace_id,
            ),
        )

        self.assertEqual(2, preview.counts["selected"])
        self.assertEqual(1, preview.counts["reevaluation_duplicates"])
        self.assertEqual(1, preview.counts["summary_duplicates"])
        self.assertEqual([old_reeval, old_summary], preview.sample_ids)
        request = _request(
            "dream_duplicates_purge",
            workspace_id=self.workspace.workspace_id,
            selection_token=preview.selection_token,
            preflight_token=PREFLIGHT_TOKEN,
        )
        receipt = await operations["dream_duplicates_purge"](
            workspace=self.workspace,
            request=request,
        )
        replay = await operations["dream_duplicates_purge"](
            workspace=self.workspace,
            request=request,
        )

        self.assertEqual([old_reeval, old_summary], receipt.affected_ids)
        self.assertEqual(2, len(receipt.event_ids))
        self.assertEqual(
            (2, 2, 0),
            (
                receipt.selected_count,
                receipt.changed_count,
                receipt.skipped_count,
            ),
        )
        self.assertTrue(replay.idempotent_replay)
        with closing(sqlite3.connect(self.database)) as connection:
            rows = connection.execute(
                "SELECT record_id,deleted_at_us FROM memory_records "
                "WHERE record_id IN (?,?,?,?) ORDER BY record_id",
                (old_reeval, new_reeval, old_summary, new_summary),
            ).fetchall()
        self.assertEqual(
            [
                (old_reeval, int(NOW.timestamp() * 1_000_000)),
                (new_reeval, None),
                (old_summary, int(NOW.timestamp() * 1_000_000)),
                (new_summary, None),
            ],
            rows,
        )

    def _append_filler(self, count: int) -> None:
        """Append distinct live learnings in one transaction."""
        from daem0nmcp.event_store import EventCommand, EventStore

        occurred_at_us = int((NOW - timedelta(days=30)).timestamp() * 1_000_000)
        with closing(sqlite3.connect(self.database)) as connection:
            connection.execute("PRAGMA foreign_keys=ON")
            store = EventStore(connection)
            for index in range(count):
                store.append_and_project(
                    EventCommand(
                        workspace_id=self.workspace.workspace_id,
                        stream_id=f"mem_{index:064x}",
                        stream_kind="memory",
                        event_type="memory.created",
                        occurred_at_us=occurred_at_us + index,
                        recorded_at_us=occurred_at_us + index,
                        actor_type="system",
                        payload={
                            "record": {
                                "record_type": "learning",
                                "legacy_type": None,
                                "content": f"Distinct filler learning {index}.",
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
                                "source_client": "maintenance-test",
                                "source_model": None,
                                "deleted_at_us": None,
                            }
                        },
                    )
                )
            connection.commit()

    @contextmanager
    def _as_durable_task(self, tool_name: str):
        """Run the block the way the owned task worker invokes a tool."""
        from daem0nmcp.api.v7.tasks import (
            DurableTaskExecution,
            durable_task_execution_var,
        )

        token = durable_task_execution_var.set(
            DurableTaskExecution(
                task_id="task-maintenance-test",
                tool_name=tool_name,
                workspace_id=self.workspace.workspace_id,
                arguments_sha256="0" * 64,
                principal_id="principal-maintenance-test",
                transport_session_id=None,
            )
        )
        try:
            yield
        finally:
            durable_task_execution_var.reset(token)

    async def _foreground_and_task(self, operations, tool_name: str, **arguments):
        """Return the task result after proving the foreground asks for a task."""
        from daem0nmcp.api.v7.maintenance_operations import (
            MaintenanceOperationError,
        )

        request = _request(
            tool_name, workspace_id=self.workspace.workspace_id, **arguments
        )
        with self.assertRaises(MaintenanceOperationError) as raised:
            await operations[tool_name](workspace=self.workspace, request=request)
        self.assertEqual("TASK_REQUIRED", raised.exception.code)
        with self._as_durable_task(tool_name):
            return await operations[tool_name](
                workspace=self.workspace, request=request
            )

    async def test_a_task_completes_the_scans_the_foreground_defers(self) -> None:
        """TASK_REQUIRED must be an answer a task can actually satisfy."""
        from daem0nmcp.api.v7.maintenance_operations import _MAX_GROUP_SCAN

        self._append_filler(_MAX_GROUP_SCAN + 1)
        source_id = "mem_" + "9" * 64
        old_dream = self._append_record(
            "a",
            "Older dream re-evaluation.",
            created_at=NOW - timedelta(days=3),
            record_type="learning",
            tags=["dream", "re-evaluation", f"source-decision:{source_id}"],
        )
        self._append_record(
            "b",
            "Newer dream re-evaluation.",
            created_at=NOW - timedelta(days=2),
            record_type="learning",
            tags=["dream", "re-evaluation", f"source-decision:{source_id}"],
        )
        old_duplicate = self._append_record(
            "c", "Use the durable queue", created_at=NOW - timedelta(days=5)
        )
        keeper = self._append_record(
            "d", "use the  durable queue", created_at=NOW - timedelta(days=4)
        )
        needle = self._append_record(
            "e",
            "Needle retry learning.",
            created_at=NOW - timedelta(days=1),
            record_type="learning",
        )
        operations = self._operations()

        dream_preview = await self._foreground_and_task(
            operations, "dream_duplicates_preview"
        )
        self.assertEqual([old_dream], dream_preview.sample_ids)
        dream_receipt = await self._foreground_and_task(
            operations,
            "dream_duplicates_purge",
            selection_token=dream_preview.selection_token,
            preflight_token=PREFLIGHT_TOKEN,
        )
        self.assertEqual([old_dream], dream_receipt.affected_ids)

        duplicate_preview = await self._foreground_and_task(
            operations, "memory_duplicates_preview"
        )
        self.assertEqual(
            {"duplicate_groups": 1, "eligible": 1, "remaining": 0, "selected": 1},
            duplicate_preview.counts,
        )
        self.assertEqual([old_duplicate], duplicate_preview.sample_ids)
        duplicate_receipt = await self._foreground_and_task(
            operations,
            "memory_duplicates_cleanup",
            selection_token=duplicate_preview.selection_token,
            preflight_token=PREFLIGHT_TOKEN,
        )
        self.assertEqual([keeper, old_duplicate], duplicate_receipt.affected_ids)

        summary = "Needle retries need a bounded budget."
        compaction_preview = await self._foreground_and_task(
            operations,
            "memory_compaction_preview",
            summary=summary,
            query="needle",
            limit=10,
        )
        self.assertEqual([needle], compaction_preview.sample_ids)
        self.assertEqual(1, compaction_preview.counts["selected"])
        compacted = await self._foreground_and_task(
            operations,
            "memory_compact",
            summary=summary,
            query="needle",
            limit=10,
            selection_token=compaction_preview.selection_token,
            idempotency_key="compact-needle-history-0001",
            preflight_token=PREFLIGHT_TOKEN,
        )
        self.assertEqual(1, compacted.receipt.changed_count)
        self._assert_replay_reproduces_projections()

    async def test_task_and_foreground_scans_select_identically(self) -> None:
        """A preview issued one way must stay valid when applied the other way."""
        self._append_record("a", "Same content.", created_at=NOW - timedelta(days=3))
        self._append_record("b", "same  content.", created_at=NOW - timedelta(days=2))
        self._append_record("c", "Same Content.", created_at=NOW - timedelta(days=1))
        self._append_record("d", "Other content.", created_at=NOW - timedelta(days=1))
        self._append_record("e", "other content.", created_at=NOW - timedelta(days=2))
        operations = self._operations()
        request = _request(
            "memory_duplicates_preview", workspace_id=self.workspace.workspace_id
        )

        foreground = await operations["memory_duplicates_preview"](
            workspace=self.workspace, request=request
        )
        with self._as_durable_task("memory_duplicates_preview"):
            task = await operations["memory_duplicates_preview"](
                workspace=self.workspace, request=request
            )

        self.assertEqual(3, foreground.counts["selected"])
        self.assertEqual(foreground, task)
        with self._as_durable_task("memory_duplicates_cleanup"):
            receipt = await operations["memory_duplicates_cleanup"](
                workspace=self.workspace,
                request=_request(
                    "memory_duplicates_cleanup",
                    workspace_id=self.workspace.workspace_id,
                    selection_token=foreground.selection_token,
                    preflight_token=PREFLIGHT_TOKEN,
                ),
            )
        self.assertEqual(3, receipt.changed_count)

    async def test_a_task_for_another_tool_does_not_lift_the_scan_bound(self) -> None:
        """Only the admitted task's own tool and workspace run unbounded."""
        from daem0nmcp.api.v7.maintenance_operations import (
            _MAX_GROUP_SCAN,
            MaintenanceOperationError,
        )

        self._append_filler(_MAX_GROUP_SCAN + 1)
        operations = self._operations()
        request = _request(
            "memory_duplicates_preview", workspace_id=self.workspace.workspace_id
        )

        with (
            self._as_durable_task("memory_prune_preview"),
            self.assertRaises(MaintenanceOperationError) as raised,
        ):
            await operations["memory_duplicates_preview"](
                workspace=self.workspace, request=request
            )
        self.assertEqual("TASK_REQUIRED", raised.exception.code)

    async def test_cancelled_prune_rolls_back_before_commit(self) -> None:
        """Cancellation observed before commit must leave authority unchanged."""
        from daem0nmcp.api.v7.runtime_services import WorkspaceStorageResolver

        record_id = self._append_record(
            "a", "Old candidate.", created_at=NOW - timedelta(days=100)
        )
        preview_operations = self._operations()
        preview = await preview_operations["memory_prune_preview"](
            workspace=self.workspace,
            request=_request(
                "memory_prune_preview",
                workspace_id=self.workspace.workspace_id,
            ),
        )
        self.dependencies.close()
        self.dependencies = None
        entered = threading.Event()
        release = threading.Event()
        delegate = WorkspaceStorageResolver()

        class BlockingResolver:
            @contextmanager
            def locked_active(self, workspace):
                entered.set()
                if not release.wait(timeout=5):
                    raise RuntimeError("test resolver timed out")
                with delegate.locked_active(workspace) as active:
                    yield active

        operation = self._operations(storage_resolver=BlockingResolver())[
            "memory_prune"
        ]
        task = asyncio.create_task(
            operation(
                workspace=self.workspace,
                request=_request(
                    "memory_prune",
                    workspace_id=self.workspace.workspace_id,
                    selection_token=preview.selection_token,
                    preflight_token=PREFLIGHT_TOKEN,
                ),
            )
        )
        self.assertTrue(await asyncio.to_thread(entered.wait, 2))
        task.cancel()
        release.set()
        with self.assertRaises(asyncio.CancelledError):
            await task
        with closing(sqlite3.connect(self.database)) as connection:
            row = connection.execute(
                "SELECT stream_version,deleted_at_us FROM memory_records "
                "WHERE record_id=?",
                (record_id,),
            ).fetchone()
        self.assertEqual((1, None), row)

    async def test_late_cancelled_prune_returns_committed_receipt(self) -> None:
        """Cancellation after commit must not make a durable deletion look failed."""
        from daem0nmcp.api.v7.runtime_services import WorkspaceStorageResolver

        record_id = self._append_record(
            "a", "Old candidate.", created_at=NOW - timedelta(days=100)
        )
        preview_operations = self._operations()
        preview = await preview_operations["memory_prune_preview"](
            workspace=self.workspace,
            request=_request(
                "memory_prune_preview",
                workspace_id=self.workspace.workspace_id,
            ),
        )
        self.dependencies.close()
        self.dependencies = None
        committed = threading.Event()
        release = threading.Event()
        delegate = WorkspaceStorageResolver()

        class ExitBlockingResolver:
            @contextmanager
            def locked_active(self, workspace):
                with delegate.locked_active(workspace) as active:
                    yield active
                    committed.set()
                    if not release.wait(timeout=5):
                        raise RuntimeError("test resolver timed out")

        operation = self._operations(storage_resolver=ExitBlockingResolver())[
            "memory_prune"
        ]
        task = asyncio.create_task(
            operation(
                workspace=self.workspace,
                request=_request(
                    "memory_prune",
                    workspace_id=self.workspace.workspace_id,
                    selection_token=preview.selection_token,
                    preflight_token=PREFLIGHT_TOKEN,
                ),
            )
        )
        self.assertTrue(await asyncio.to_thread(committed.wait, 2))
        task.cancel()
        release.set()
        receipt = await task
        self.assertEqual([record_id], receipt.affected_ids)
        self.assertFalse(receipt.idempotent_replay)
        with closing(sqlite3.connect(self.database)) as connection:
            row = connection.execute(
                "SELECT stream_version,deleted_at_us FROM memory_records "
                "WHERE record_id=?",
                (record_id,),
            ).fetchone()
        self.assertEqual((2, int(NOW.timestamp() * 1_000_000)), row)

    async def test_prune_keeps_records_that_informed_later_work(self) -> None:
        """Prune must not delete a memory that later work demonstrably used."""
        old = NOW - timedelta(days=100)
        unused_id = self._append_record("a", "Old unused decision.", created_at=old)
        cited_id = self._append_record(
            "b", "Old decision still in use.", created_at=old
        )
        self._append_record(
            "c", "Old decision with a legacy counter.", created_at=old, recall_count=5
        )
        self._append_record(
            "d",
            "Recent decision built on the old one.",
            created_at=NOW - timedelta(days=1),
            informed_by=[cited_id],
        )
        operations = self._operations()

        preview = await operations["memory_prune_preview"](
            workspace=self.workspace,
            request=_request(
                "memory_prune_preview", workspace_id=self.workspace.workspace_id
            ),
        )

        self.assertEqual([unused_id], preview.sample_ids)
        self.assertEqual(
            {
                "eligible": 1,
                "kept_cited_as_informing": 1,
                "kept_legacy_recall_count": 1,
                "remaining": 0,
                "selected": 1,
            },
            preview.counts,
        )
        receipt = await operations["memory_prune"](
            workspace=self.workspace,
            request=_request(
                "memory_prune",
                workspace_id=self.workspace.workspace_id,
                selection_token=preview.selection_token,
                preflight_token=PREFLIGHT_TOKEN,
            ),
        )
        self.assertEqual([unused_id], receipt.affected_ids)
        with closing(sqlite3.connect(self.database)) as connection:
            surviving = connection.execute(
                "SELECT deleted_at_us FROM memory_records WHERE record_id=?",
                (cited_id,),
            ).fetchone()
        self.assertEqual((None,), surviving)

    def _relationship_operations(self):
        from daem0nmcp.api.v7.relationship_operations import (
            RelationshipOperationDependencies,
            build_relationship_operations,
        )

        # Links are written at NOW; reads happen after the maintenance run.
        self.relationship_clock = NOW
        dependencies = RelationshipOperationDependencies(
            clock=lambda: self.relationship_clock
        )
        self.addCleanup(dependencies.close)
        return build_relationship_operations(dependencies)

    async def _link(
        self,
        relationships,
        source: str,
        target: str,
        relationship_type: str,
        key: str,
    ) -> str:
        receipt = await relationships["memory_link"](
            workspace=self.workspace,
            request=_request(
                "memory_link",
                workspace_id=self.workspace.workspace_id,
                source_record_id=source,
                target_record_id=target,
                relationship_type=relationship_type,
                idempotency_key=key,
                preflight_token=PREFLIGHT_TOKEN,
            ),
        )
        return next(item for item in receipt.affected_ids if item.startswith("rel_"))

    async def _related(self, relationships, record_id: str):
        self.relationship_clock = NOW + timedelta(seconds=1)
        with self.assertNoLogs("daem0nmcp.api.v7.relationship_operations"):
            return await relationships["memory_related"](
                workspace=self.workspace,
                request=_request(
                    "memory_related",
                    workspace_id=self.workspace.workspace_id,
                    record_id=record_id,
                    direction="both",
                    max_depth=3,
                ),
            )

    async def _chain(self, relationships, start: str, end: str):
        self.relationship_clock = NOW + timedelta(seconds=1)
        return await relationships["memory_chain_trace"](
            workspace=self.workspace,
            request=_request(
                "memory_chain_trace",
                workspace_id=self.workspace.workspace_id,
                start_record_id=start,
                end_record_id=end,
            ),
        )

    def _live_edges(self) -> set[tuple[str, str, str]]:
        now_us = int(NOW.timestamp() * 1_000_000)
        with closing(sqlite3.connect(self.database)) as connection:
            return {
                (str(row[0]), str(row[1]), str(row[2]))
                for row in connection.execute(
                    "SELECT source_record_id,target_record_id,relationship_type "
                    "FROM memory_relationship_versions "
                    "WHERE workspace_id=? AND transaction_to_us IS NULL "
                    "AND (valid_to_us IS NULL OR valid_to_us>?)",
                    (self.workspace.workspace_id, now_us + 10),
                )
            }

    def _assert_replay_reproduces_projections(self) -> None:
        """The projections must be exactly what the canonical events replay to."""
        from daem0nmcp.event_store import export_event_bundle, import_event_bundle

        relationships_query = (
            "SELECT relationship_version_id,relationship_id,version,"
            "source_record_id,target_record_id,relationship_type,content_hash,"
            "valid_from_us,valid_to_us,transaction_from_us,transaction_to_us,"
            "asserted_by_event_id,retracted_by_event_id "
            "FROM memory_relationship_versions ORDER BY relationship_version_id"
        )
        records_query = (
            "SELECT record_id,state_hash,deleted_at_us FROM memory_records "
            "ORDER BY record_id"
        )
        with closing(sqlite3.connect(self.database)) as connection:
            bundle = export_event_bundle(connection, self.workspace.workspace_id)
            expected = connection.execute(relationships_query).fetchall()
            expected_records = connection.execute(records_query).fetchall()
        with closing(sqlite3.connect(self.root / "replayed.db")) as replayed:
            replayed.execute("PRAGMA foreign_keys=ON")
            _apply_v7_schema(replayed)
            import_event_bundle(replayed, bundle, self.workspace.workspace_id)
            replayed.commit()
            self.assertEqual(expected, replayed.execute(relationships_query).fetchall())
            self.assertEqual(
                expected_records, replayed.execute(records_query).fetchall()
            )

    async def test_prune_closes_relationships_of_pruned_records(self) -> None:
        """A pruned endpoint must not degrade traversal for the whole workspace."""
        old = NOW - timedelta(days=100)
        recent = NOW - timedelta(days=1)
        pruned_id = self._append_record("a", "Old linked decision.", created_at=old)
        neighbour_id = self._append_record("b", "Linked neighbour.", created_at=recent)
        other_id = self._append_record("c", "Unrelated first.", created_at=recent)
        another_id = self._append_record("d", "Unrelated second.", created_at=recent)
        relationships = self._relationship_operations()
        outgoing = await self._link(
            relationships, pruned_id, neighbour_id, "led_to", "prune-edge-ab-0001"
        )
        incoming = await self._link(
            relationships, neighbour_id, pruned_id, "depends_on", "prune-edge-ba-0002"
        )
        kept = await self._link(
            relationships, other_id, another_id, "related_to", "prune-edge-cd-0003"
        )
        operations = self._operations()
        preview = await operations["memory_prune_preview"](
            workspace=self.workspace,
            request=_request(
                "memory_prune_preview", workspace_id=self.workspace.workspace_id
            ),
        )
        self.assertEqual([pruned_id], preview.sample_ids)
        request = _request(
            "memory_prune",
            workspace_id=self.workspace.workspace_id,
            selection_token=preview.selection_token,
            preflight_token=PREFLIGHT_TOKEN,
        )
        receipt = await operations["memory_prune"](
            workspace=self.workspace, request=request
        )
        replay = await operations["memory_prune"](
            workspace=self.workspace, request=request
        )

        self.assertEqual([pruned_id], receipt.affected_ids)
        self.assertTrue(replay.idempotent_replay)
        self.assertEqual(receipt.event_ids, replay.event_ids)
        self.assertEqual({(other_id, another_id, "related_to")}, self._live_edges())
        unrelated = await self._related(relationships, other_id)
        self.assertEqual([another_id], [item.record_id for item in unrelated.records])
        self.assertEqual(
            [kept], [item.relationship_id for item in unrelated.relationships]
        )
        neighbour = await self._related(relationships, neighbour_id)
        self.assertEqual([], neighbour.records)
        self.assertEqual([], neighbour.relationships)
        chain = await self._chain(relationships, other_id, another_id)
        self.assertEqual([[kept]], [path.relationship_ids for path in chain.paths])
        with closing(sqlite3.connect(self.database)) as connection:
            closing_events = connection.execute(
                "SELECT stream_id,event_type FROM memory_events "
                "WHERE stream_kind='relationship' AND stream_version=2 "
                "ORDER BY stream_id"
            ).fetchall()
        self.assertEqual(
            sorted(
                [(outgoing, "relationship.removed"), (incoming, "relationship.removed")]
            ),
            closing_events,
        )
        self._assert_replay_reproduces_projections()

    async def test_duplicate_cleanup_repoints_relationships_to_the_keeper(
        self,
    ) -> None:
        """A merged duplicate's edges must follow the surviving record."""
        duplicate_id = self._append_record(
            "a", "Use the durable queue", created_at=NOW - timedelta(days=3)
        )
        keeper_id = self._append_record(
            "b", "use the durable queue", created_at=NOW - timedelta(days=2)
        )
        cause_id = self._append_record(
            "c", "Queue outage post-mortem.", created_at=NOW - timedelta(days=4)
        )
        effect_id = self._append_record(
            "d", "Worker rollout.", created_at=NOW - timedelta(days=1)
        )
        relationships = self._relationship_operations()
        # Moves to the keeper.
        await self._link(
            relationships, cause_id, duplicate_id, "led_to", "merge-edge-ca-0001"
        )
        # Would become a self-edge on the keeper: closed, not moved.
        await self._link(
            relationships, duplicate_id, keeper_id, "related_to", "merge-edge-ab-0002"
        )
        # The keeper already has this exact edge: closed, not duplicated.
        await self._link(
            relationships, duplicate_id, effect_id, "led_to", "merge-edge-ad-0003"
        )
        existing = await self._link(
            relationships, keeper_id, effect_id, "led_to", "merge-edge-bd-0004"
        )
        operations = self._operations()
        preview = await operations["memory_duplicates_preview"](
            workspace=self.workspace,
            request=_request(
                "memory_duplicates_preview",
                workspace_id=self.workspace.workspace_id,
            ),
        )
        self.assertEqual([duplicate_id], preview.sample_ids)
        request = _request(
            "memory_duplicates_cleanup",
            workspace_id=self.workspace.workspace_id,
            selection_token=preview.selection_token,
            preflight_token=PREFLIGHT_TOKEN,
        )
        receipt = await operations["memory_duplicates_cleanup"](
            workspace=self.workspace, request=request
        )
        replay = await operations["memory_duplicates_cleanup"](
            workspace=self.workspace, request=request
        )

        self.assertEqual([keeper_id, duplicate_id], receipt.affected_ids)
        self.assertTrue(replay.idempotent_replay)
        self.assertEqual(receipt.event_ids, replay.event_ids)
        self.assertEqual(
            {(cause_id, keeper_id, "led_to"), (keeper_id, effect_id, "led_to")},
            self._live_edges(),
        )
        with closing(sqlite3.connect(self.database)) as connection:
            moved_valid_from = connection.execute(
                "SELECT valid_from_us FROM memory_relationship_versions "
                "WHERE source_record_id=? AND target_record_id=? "
                "AND transaction_to_us IS NULL",
                (cause_id, keeper_id),
            ).fetchall()
        # The moved edge keeps the validity start of the edge it replaces.
        self.assertEqual([(int(NOW.timestamp() * 1_000_000),)], moved_valid_from)
        related = await self._related(relationships, keeper_id)
        self.assertEqual(
            {cause_id, effect_id}, {item.record_id for item in related.records}
        )
        self.assertEqual(2, len(related.relationships))
        self.assertIn(
            existing, {item.relationship_id for item in related.relationships}
        )
        chain = await self._chain(relationships, cause_id, effect_id)
        self.assertEqual(
            [[cause_id, keeper_id, effect_id]],
            [path.record_ids for path in chain.paths],
        )
        self._assert_replay_reproduces_projections()

    def _append_migrated_self_edge(
        self, suffix: str, record_id: str, *, actor_type: str = "migration"
    ) -> str:
        """A v6 store may hold a relationship from a record to itself."""
        from daem0nmcp.event_store import EventCommand, EventStore

        relationship_id = "rel_" + suffix * 64
        at_us = int((NOW - timedelta(days=1)).timestamp() * 1_000_000)
        with closing(sqlite3.connect(self.database)) as connection:
            connection.execute("PRAGMA foreign_keys=ON")
            EventStore(connection).append_and_project(
                EventCommand(
                    workspace_id=self.workspace.workspace_id,
                    stream_id=relationship_id,
                    stream_kind="relationship",
                    event_type="relationship.created",
                    occurred_at_us=at_us,
                    recorded_at_us=at_us,
                    actor_type=actor_type,
                    payload={
                        "relationship": {
                            "source_record_id": record_id,
                            "target_record_id": record_id,
                            "relationship_type": "related_to",
                            "legacy_type": None,
                            "description": None,
                            "confidence": 1.0,
                            "metadata": {},
                            "valid_from_us": at_us,
                            "valid_to_us": None,
                        }
                    },
                )
            )
            connection.commit()
        return relationship_id

    def _relationship_event_types(self, relationship_id: str) -> list[str]:
        with closing(sqlite3.connect(self.database)) as connection:
            return [
                str(row[0])
                for row in connection.execute(
                    "SELECT event_type FROM memory_events WHERE workspace_id=? "
                    "AND stream_id=? ORDER BY stream_version",
                    (self.workspace.workspace_id, relationship_id),
                )
            ]

    async def test_prune_closes_a_migrated_self_edge_of_the_pruned_record(
        self,
    ) -> None:
        """A migrated self-edge must not make its record impossible to prune."""
        pruned_id = self._append_record(
            "a", "Old self-linked decision.", created_at=NOW - timedelta(days=100)
        )
        self_edge = self._append_migrated_self_edge("e", pruned_id)
        # Only closing is allowed at runtime; asserting one is still refused.
        with self.assertRaisesRegex(ValueError, "self-edges"):
            self._append_migrated_self_edge("f", pruned_id, actor_type="client")
        operations = self._operations()
        preview = await operations["memory_prune_preview"](
            workspace=self.workspace,
            request=_request(
                "memory_prune_preview", workspace_id=self.workspace.workspace_id
            ),
        )
        self.assertEqual([pruned_id], preview.sample_ids)

        receipt = await operations["memory_prune"](
            workspace=self.workspace,
            request=_request(
                "memory_prune",
                workspace_id=self.workspace.workspace_id,
                selection_token=preview.selection_token,
                preflight_token=PREFLIGHT_TOKEN,
            ),
        )

        self.assertEqual([pruned_id], receipt.affected_ids)
        self.assertEqual(set(), self._live_edges())
        self.assertEqual(
            ["relationship.created", "relationship.removed"],
            self._relationship_event_types(self_edge),
        )
        self._assert_replay_reproduces_projections()

    async def test_duplicate_cleanup_closes_a_migrated_self_edge_of_the_duplicate(
        self,
    ) -> None:
        """A self-edge on a merged duplicate is closed, never moved to the keeper."""
        duplicate_id = self._append_record(
            "a", "Use the durable queue", created_at=NOW - timedelta(days=3)
        )
        keeper_id = self._append_record(
            "b", "use the durable queue", created_at=NOW - timedelta(days=2)
        )
        self_edge = self._append_migrated_self_edge("e", duplicate_id)
        operations = self._operations()
        preview = await operations["memory_duplicates_preview"](
            workspace=self.workspace,
            request=_request(
                "memory_duplicates_preview",
                workspace_id=self.workspace.workspace_id,
            ),
        )
        self.assertEqual([duplicate_id], preview.sample_ids)

        receipt = await operations["memory_duplicates_cleanup"](
            workspace=self.workspace,
            request=_request(
                "memory_duplicates_cleanup",
                workspace_id=self.workspace.workspace_id,
                selection_token=preview.selection_token,
                preflight_token=PREFLIGHT_TOKEN,
            ),
        )

        self.assertEqual([keeper_id, duplicate_id], receipt.affected_ids)
        self.assertEqual(set(), self._live_edges())
        self.assertEqual(
            ["relationship.created", "relationship.removed"],
            self._relationship_event_types(self_edge),
        )
        self._assert_replay_reproduces_projections()

    async def test_duplicate_cleanup_leaves_an_unlinked_relationship_removed(
        self,
    ) -> None:
        """An edge the caller already removed must not come back on the keeper."""
        duplicate_id = self._append_record(
            "a", "Use the durable queue", created_at=NOW - timedelta(days=3)
        )
        self._append_record(
            "b", "use the durable queue", created_at=NOW - timedelta(days=2)
        )
        effect_id = self._append_record(
            "c", "Worker rollout.", created_at=NOW - timedelta(days=1)
        )
        relationships = self._relationship_operations()
        unlinked = await self._link(
            relationships, duplicate_id, effect_id, "led_to", "merge-unlinked-0001"
        )
        await relationships["memory_unlink"](
            workspace=self.workspace,
            request=_request(
                "memory_unlink",
                workspace_id=self.workspace.workspace_id,
                relationship_id=unlinked,
                preflight_token=PREFLIGHT_TOKEN,
            ),
        )
        operations = self._operations()
        preview = await operations["memory_duplicates_preview"](
            workspace=self.workspace,
            request=_request(
                "memory_duplicates_preview",
                workspace_id=self.workspace.workspace_id,
            ),
        )
        self.assertEqual([duplicate_id], preview.sample_ids)

        await operations["memory_duplicates_cleanup"](
            workspace=self.workspace,
            request=_request(
                "memory_duplicates_cleanup",
                workspace_id=self.workspace.workspace_id,
                selection_token=preview.selection_token,
                preflight_token=PREFLIGHT_TOKEN,
            ),
        )

        self.assertEqual(set(), self._live_edges())
        self.assertEqual(
            ["relationship.created", "relationship.removed"],
            self._relationship_event_types(unlinked),
        )
        with closing(sqlite3.connect(self.database)) as connection:
            streams = connection.execute(
                "SELECT count(DISTINCT stream_id) FROM memory_events "
                "WHERE stream_kind='relationship'"
            ).fetchone()
        self.assertEqual((1,), streams)
        self._assert_replay_reproduces_projections()

    async def test_dream_purge_closes_relationships_of_purged_records(self) -> None:
        """Purged dream duplicates must not leave dangling relationships."""
        source_id = "mem_" + "f" * 64
        tags = ["dream", "re-evaluation", f"source-decision:{source_id}"]
        purged_id = self._append_record(
            "a",
            "Older dream re-evaluation.",
            created_at=NOW - timedelta(days=3),
            record_type="learning",
            tags=tags,
        )
        self._append_record(
            "b",
            "Newer dream re-evaluation.",
            created_at=NOW - timedelta(days=2),
            record_type="learning",
            tags=tags,
        )
        other_id = self._append_record(
            "c", "Unrelated first.", created_at=NOW - timedelta(days=1)
        )
        another_id = self._append_record(
            "d", "Unrelated second.", created_at=NOW - timedelta(days=1)
        )
        relationships = self._relationship_operations()
        await self._link(
            relationships, purged_id, other_id, "evidence_for", "dream-edge-ac-0001"
        )
        kept = await self._link(
            relationships, other_id, another_id, "related_to", "dream-edge-cd-0002"
        )
        operations = self._operations()
        preview = await operations["dream_duplicates_preview"](
            workspace=self.workspace,
            request=_request(
                "dream_duplicates_preview", workspace_id=self.workspace.workspace_id
            ),
        )
        self.assertEqual([purged_id], preview.sample_ids)
        request = _request(
            "dream_duplicates_purge",
            workspace_id=self.workspace.workspace_id,
            selection_token=preview.selection_token,
            preflight_token=PREFLIGHT_TOKEN,
        )
        receipt = await operations["dream_duplicates_purge"](
            workspace=self.workspace, request=request
        )
        replay = await operations["dream_duplicates_purge"](
            workspace=self.workspace, request=request
        )

        self.assertEqual([purged_id], receipt.affected_ids)
        self.assertTrue(replay.idempotent_replay)
        self.assertEqual(receipt.event_ids, replay.event_ids)
        self.assertEqual({(other_id, another_id, "related_to")}, self._live_edges())
        related = await self._related(relationships, other_id)
        self.assertEqual([another_id], [item.record_id for item in related.records])
        self.assertEqual(
            [kept], [item.relationship_id for item in related.relationships]
        )
        self._assert_replay_reproduces_projections()


if __name__ == "__main__":
    unittest.main()
