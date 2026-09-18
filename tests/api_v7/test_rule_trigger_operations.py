from __future__ import annotations

import asyncio
import inspect
import re
import sqlite3
import tempfile
import threading
import unittest
from contextlib import closing, contextmanager
from datetime import datetime, timezone
from pathlib import Path
from types import MappingProxyType

from daem0nmcp.api.v7.application import AdmittedRequest

NOW = datetime(2026, 8, 8, 12, 0, tzinfo=timezone.utc)
PREFLIGHT_TOKEN = "t" * 32


def _apply_v7_schema(connection: sqlite3.Connection) -> None:
    from daem0nmcp.migrations.schema import MIGRATIONS
    from daem0nmcp.schema_version import CURRENT_SCHEMA_VERSION

    connection.executescript(
        """
        CREATE TABLE schema_version (version INTEGER PRIMARY KEY);
        CREATE TABLE rules (
            id INTEGER PRIMARY KEY,
            trigger TEXT NOT NULL,
            must_do TEXT NOT NULL,
            must_not TEXT NOT NULL,
            ask_first TEXT NOT NULL,
            warnings TEXT NOT NULL,
            priority INTEGER NOT NULL,
            enabled INTEGER NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE TABLE context_triggers (
            id INTEGER PRIMARY KEY,
            project_path TEXT NOT NULL,
            trigger_type TEXT NOT NULL,
            pattern TEXT NOT NULL,
            recall_topic TEXT NOT NULL,
            recall_categories TEXT NOT NULL,
            is_active INTEGER NOT NULL,
            priority INTEGER NOT NULL,
            created_at TEXT NOT NULL,
            trigger_count INTEGER NOT NULL,
            last_triggered TEXT
        );
        """
    )
    for version in range(16, CURRENT_SCHEMA_VERSION + 1):
        migration = next(item for item in MIGRATIONS if item[0] == version)
        for statement in migration[2]:
            connection.execute(statement)
        connection.execute("INSERT INTO schema_version(version) VALUES (?)", (version,))
    connection.commit()


def _request(tool_name: str, **arguments: object) -> AdmittedRequest:
    from daem0nmcp.api.v7.tools import TOOL_INPUT_MODELS

    model = TOOL_INPUT_MODELS[tool_name].model_validate(arguments)
    effective = model.model_dump(mode="python")
    effective.pop("preflight_token", None)
    return AdmittedRequest(tool_name, MappingProxyType(effective))


class RuleTriggerOperationTests(unittest.IsolatedAsyncioTestCase):
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

    def _operations(self, **changes: object):
        from daem0nmcp.api.v7.rule_trigger_operations import (
            RuleTriggerOperationDependencies,
            build_rule_trigger_operations,
        )
        from daem0nmcp.trigger_security import SafeUserPattern

        options: dict[str, object] = {
            "clock": lambda: NOW,
            "cursor_secret": b"cursor-secret-for-rule-trigger-tests",
            "pattern_matcher": SafeUserPattern(
                compiler=re.compile,
                search=lambda compiled, value, *, timeout: (
                    compiled.search(value) is not None
                ),
            ),
        }
        options.update(changes)
        return build_rule_trigger_operations(
            RuleTriggerOperationDependencies(**options)
        )

    def _rule_create_request(
        self,
        *,
        key: str = "rule-create-0001",
        trigger: str = "changing database migrations",
        priority: int = 10,
        must_do: list[str] | None = None,
    ) -> AdmittedRequest:
        return _request(
            "rule_create",
            workspace_id=self.workspace.workspace_id,
            trigger=trigger,
            must_do=must_do or ["Run migration tests"],
            must_not=["Rewrite applied migrations"],
            ask_first=["Is the migration additive?"],
            warnings=["Keep rollback evidence"],
            priority=priority,
            idempotency_key=key,
            preflight_token=PREFLIGHT_TOKEN,
        )

    def _trigger_create_request(
        self,
        *,
        key: str = "trigger-create-0001",
        trigger_type: str = "file",
        pattern: str = "src/**/*.py",
        recall_query: str = "Python source guidance",
        categories: list[str] | None = None,
        enabled: bool = True,
    ) -> AdmittedRequest:
        return _request(
            "context_trigger_create",
            workspace_id=self.workspace.workspace_id,
            trigger_type=trigger_type,
            pattern=pattern,
            recall_query=recall_query,
            categories=categories,
            enabled=enabled,
            idempotency_key=key,
            preflight_token=PREFLIGHT_TOKEN,
        )

    @staticmethod
    def _retrieval(record_type: str = "decision"):
        from daem0nmcp.api.v7.models import (
            CitationManifestEntry,
            EvidenceItem,
            EvidenceRef,
            RecordSummary,
            RetrievalData,
            TokenUsage,
        )

        record = RecordSummary(
            record_id="mem_" + "a" * 64,
            record_type=record_type,
            excerpt="Retrieved trigger guidance.",
            tags=["triggered"],
            relative_file_path="src/guidance.py",
            current_status="current",
            content_hash="b" * 64,
            created_at=NOW,
            updated_at=NOW,
        )
        reference = EvidenceRef(
            record_id=record.record_id,
            event_id="evt_" + "c" * 64,
            content_hash=record.content_hash,
            provider="lexical",
        )
        return RetrievalData(
            items=[
                EvidenceItem(
                    citation="[E1]",
                    record=record,
                    bounded_excerpt=record.excerpt,
                    channels=["lexical"],
                    score=1.0,
                    status="current",
                    evidence_refs=[reference],
                )
            ],
            rendered_context="[E1] Retrieved trigger guidance.",
            citation_manifest=[
                CitationManifestEntry(
                    citation="[E1]",
                    evidence_refs=[reference],
                    channels=["lexical"],
                )
            ],
            provider_diagnostics=[],
            abstained=False,
            token_usage=TokenUsage(
                budget=2400,
                requested=4,
                selected=4,
                rendered=4,
                dropped=0,
            ),
        )

    async def test_registry_is_exact_immutable_and_keyword_only(self) -> None:
        """An incomplete or mutable registry could bypass the v7 manifest contract."""
        from daem0nmcp.api.v7.rule_trigger_operations import (
            RuleTriggerOperationDependencies,
            build_rule_trigger_operations,
        )

        operations = build_rule_trigger_operations(RuleTriggerOperationDependencies())

        self.assertEqual(
            {
                "rule_create",
                "rule_update",
                "rule_list",
                "rule_check",
                "context_trigger_create",
                "context_trigger_delete",
                "context_trigger_list",
                "context_triggers_match",
            },
            set(operations),
        )
        with self.assertRaises(TypeError):
            operations["extra"] = object()
        for operation in operations.values():
            parameters = tuple(inspect.signature(operation).parameters.values())
            self.assertEqual(
                ("workspace", "request"),
                tuple(parameter.name for parameter in parameters),
            )
            self.assertTrue(
                all(
                    parameter.kind is inspect.Parameter.KEYWORD_ONLY
                    for parameter in parameters
                )
            )

    async def test_rule_create_is_opaque_atomic_and_replay_safe(self) -> None:
        """A replay must not create a second row or expose its integer key."""
        from daem0nmcp.api.v7.resources import RuleView
        from daem0nmcp.api.v7.rule_trigger_operations import (
            RuleTriggerOperationError,
        )

        operation = self._operations()["rule_create"]
        request = self._rule_create_request()
        created = await operation(workspace=self.workspace, request=request)
        replay = await operation(workspace=self.workspace, request=request)

        self.assertIsInstance(created, RuleView)
        self.assertEqual(created, replay)
        self.assertRegex(created.rule_id, r"^rule_[0-9a-f]{64}$")
        self.assertNotIn(str(self.root), created.model_dump_json())
        with closing(sqlite3.connect(self.database)) as connection:
            self.assertEqual(
                (1, 1, 1, 1),
                (
                    connection.execute("SELECT count(*) FROM rules").fetchone()[0],
                    connection.execute(
                        "SELECT count(*) FROM public_object_ids "
                        "WHERE object_kind='rule'"
                    ).fetchone()[0],
                    connection.execute(
                        "SELECT count(*) FROM governance_events "
                        "WHERE stream_kind='rule'"
                    ).fetchone()[0],
                    connection.execute(
                        "SELECT count(*) FROM governance_rules"
                    ).fetchone()[0],
                ),
            )

        with self.assertRaises(RuleTriggerOperationError) as caught:
            await operation(
                workspace=self.workspace,
                request=self._rule_create_request(
                    trigger="same key rebound to another trigger"
                ),
            )
        self.assertEqual("IDEMPOTENCY_CONFLICT", caught.exception.code)
        self.assertNotIn(str(self.root), str(caught.exception))
        with closing(sqlite3.connect(self.database)) as connection:
            self.assertEqual(
                1,
                connection.execute("SELECT count(*) FROM governance_events").fetchone()[
                    0
                ],
            )

    async def test_governance_events_are_sql_immutable(self) -> None:
        """Rule history must remain append-only below the operation layer."""
        created = await self._operations()["rule_create"](
            workspace=self.workspace,
            request=self._rule_create_request(),
        )
        with closing(sqlite3.connect(self.database)) as connection:
            event_id = connection.execute(
                "SELECT event_id FROM governance_events WHERE stream_id=?",
                (created.rule_id,),
            ).fetchone()[0]
            with self.assertRaisesRegex(
                sqlite3.IntegrityError, "IMMUTABLE_GOVERNANCE_EVENT"
            ):
                connection.execute(
                    "UPDATE governance_events SET event_type='rule.changed' "
                    "WHERE event_id=?",
                    (event_id,),
                )
            with self.assertRaisesRegex(
                sqlite3.IntegrityError, "IMMUTABLE_GOVERNANCE_EVENT"
            ):
                connection.execute(
                    "DELETE FROM governance_events WHERE event_id=?",
                    (event_id,),
                )
            with self.assertRaisesRegex(
                sqlite3.IntegrityError, "IMMUTABLE_GOVERNANCE_EVENT"
            ):
                connection.execute(
                    "INSERT OR REPLACE INTO governance_events "
                    "SELECT * FROM governance_events WHERE event_id=?",
                    (event_id,),
                )

    async def test_rule_and_trigger_events_appear_in_session_updates(self) -> None:
        """Session polling must expose both canonical governance domains."""
        from daem0nmcp.api.v7.record_operations import (
            RecordOperationDependencies,
            build_record_operations,
        )

        session_updates = build_record_operations(
            RecordOperationDependencies(
                clock=lambda: NOW,
                projection_scheduler=lambda _path: None,
            )
        )["session_updates_get"]
        before = await session_updates(
            workspace=self.workspace,
            request=_request(
                "session_updates_get",
                workspace_id=self.workspace.workspace_id,
            ),
        )
        operations = self._operations()
        rule = await operations["rule_create"](
            workspace=self.workspace,
            request=self._rule_create_request(),
        )
        trigger = await operations["context_trigger_create"](
            workspace=self.workspace,
            request=self._trigger_create_request(),
        )
        after = await session_updates(
            workspace=self.workspace,
            request=_request(
                "session_updates_get",
                workspace_id=self.workspace.workspace_id,
                after_cursor=before.cursor,
            ),
        )

        self.assertEqual(["rule", "trigger"], [item.kind for item in after.events])
        self.assertEqual(
            [rule.rule_id, trigger.trigger_id],
            [item.object_id for item in after.events],
        )

    async def test_rule_capability_does_not_require_trigger_projection(self) -> None:
        """A missing unrelated trigger table must not disable canonical rules."""
        with closing(sqlite3.connect(self.database)) as connection:
            connection.execute("DROP TABLE context_triggers")
            connection.commit()

        created = await self._operations()["rule_create"](
            workspace=self.workspace,
            request=self._rule_create_request(),
        )

        self.assertRegex(created.rule_id, r"^rule_[0-9a-f]{64}$")

    async def test_rule_update_list_and_signed_pagination(self) -> None:
        """Opaque updates and signed cursors must preserve one deterministic page."""
        from daem0nmcp.api.v7.models import Page
        from daem0nmcp.api.v7.rule_trigger_operations import (
            RuleTriggerOperationError,
        )

        operations = self._operations()
        created = []
        for index, priority in enumerate((30, 20, 10), start=1):
            created.append(
                await operations["rule_create"](
                    workspace=self.workspace,
                    request=self._rule_create_request(
                        key=f"rule-page-000{index}",
                        trigger=f"rule trigger number {index}",
                        priority=priority,
                    ),
                )
            )

        updated = await operations["rule_update"](
            workspace=self.workspace,
            request=_request(
                "rule_update",
                workspace_id=self.workspace.workspace_id,
                rule_id=created[1].rule_id,
                patch={"warnings": ["Updated warning"], "enabled": False},
                preflight_token=PREFLIGHT_TOKEN,
            ),
        )
        self.assertFalse(updated.enabled)
        self.assertEqual(["Updated warning"], updated.warnings)
        with closing(sqlite3.connect(self.database)) as connection:
            self.assertEqual(
                (2, 2),
                connection.execute(
                    "SELECT count(*),max(stream_version) "
                    "FROM governance_events WHERE stream_id=?",
                    (updated.rule_id,),
                ).fetchone(),
            )

        first = await operations["rule_list"](
            workspace=self.workspace,
            request=_request(
                "rule_list",
                workspace_id=self.workspace.workspace_id,
                enabled_only=False,
                limit=1,
            ),
        )
        self.assertIsInstance(first, Page)
        self.assertEqual([created[0].rule_id], [item.rule_id for item in first.items])
        self.assertTrue(first.truncated)
        second = await operations["rule_list"](
            workspace=self.workspace,
            request=_request(
                "rule_list",
                workspace_id=self.workspace.workspace_id,
                enabled_only=False,
                cursor=first.next_cursor,
                limit=2,
            ),
        )
        self.assertEqual(
            {created[1].rule_id, created[2].rule_id},
            {item.rule_id for item in second.items},
        )
        self.assertFalse(second.truncated)

        assert first.next_cursor is not None
        tampered = first.next_cursor[:-1] + (
            "0" if first.next_cursor[-1] != "0" else "1"
        )
        with self.assertRaises(RuleTriggerOperationError) as caught:
            await operations["rule_list"](
                workspace=self.workspace,
                request=_request(
                    "rule_list",
                    workspace_id=self.workspace.workspace_id,
                    enabled_only=False,
                    cursor=tampered,
                    limit=1,
                ),
            )
        self.assertEqual("INVALID_ARGUMENT", caught.exception.code)

    async def test_rule_check_returns_typed_deduplicated_guidance(self) -> None:
        """Rule matching must return public views and combine guidance once."""
        from daem0nmcp.api.v7.tools import RuleCheckData

        operations = self._operations()
        for index, must_do in enumerate(
            (["Run migration tests", "Back up data"], ["Run migration tests"]),
            start=1,
        ):
            await operations["rule_create"](
                workspace=self.workspace,
                request=self._rule_create_request(
                    key=f"rule-check-000{index}",
                    trigger=f"changing database migration {index}",
                    priority=20 - index,
                    must_do=must_do,
                ),
            )

        result = await operations["rule_check"](
            workspace=self.workspace,
            request=_request(
                "rule_check",
                workspace_id=self.workspace.workspace_id,
                proposed_action="change the database migration",
                context={"component": "storage"},
            ),
        )

        self.assertIsInstance(result, RuleCheckData)
        self.assertEqual(2, len(result.matched_rules))
        self.assertEqual(["Run migration tests", "Back up data"], result.must_do)
        self.assertTrue(
            all(not item.rule_id.isdecimal() for item in result.matched_rules)
        )

    async def test_rule_check_bounds_combined_guidance(self) -> None:
        """Many matching rules must be truncated to the public guidance bound."""
        operations = self._operations()
        for index in range(26):
            await operations["rule_create"](
                workspace=self.workspace,
                request=self._rule_create_request(
                    key=f"rule-bound-{index:04d}",
                    trigger=f"database migration guidance {index}",
                    priority=index,
                    must_do=[f"First action {index}", f"Second action {index}"],
                ),
            )

        result = await operations["rule_check"](
            workspace=self.workspace,
            request=_request(
                "rule_check",
                workspace_id=self.workspace.workspace_id,
                proposed_action="database migration guidance",
            ),
        )

        self.assertEqual(50, len(result.must_do))

    async def test_rule_mutation_cancellation_rolls_back_and_joins_worker(self) -> None:
        """A cancelled rule create must not commit later in a detached thread."""
        started = threading.Event()
        release = threading.Event()

        def blocking_clock() -> datetime:
            started.set()
            release.wait(timeout=2)
            return NOW

        operation = self._operations(clock=blocking_clock)["rule_create"]
        task = asyncio.create_task(
            operation(
                workspace=self.workspace,
                request=self._rule_create_request(),
            )
        )
        self.assertTrue(await asyncio.to_thread(started.wait, 2))
        task.cancel()
        await asyncio.sleep(0.02)
        self.assertFalse(task.done())
        release.set()
        with self.assertRaises(asyncio.CancelledError):
            await task
        with closing(sqlite3.connect(self.database)) as connection:
            self.assertEqual(
                0,
                connection.execute("SELECT count(*) FROM rules").fetchone()[0],
            )

    async def test_rule_read_cancellation_joins_shared_lock_worker(self) -> None:
        """A cancelled read must not detach while holding activation state."""
        from daem0nmcp.api.v7.runtime_services import WorkspaceStorageResolver

        entered = threading.Event()
        release = threading.Event()

        class BlockingResolver(WorkspaceStorageResolver):
            @contextmanager
            def locked_active(self, workspace):
                entered.set()
                release.wait(timeout=2)
                with super().locked_active(workspace) as active:
                    yield active

        operation = self._operations(storage_resolver=BlockingResolver())["rule_list"]
        task = asyncio.create_task(
            operation(
                workspace=self.workspace,
                request=_request("rule_list", workspace_id=self.workspace.workspace_id),
            )
        )
        self.assertTrue(await asyncio.to_thread(entered.wait, 2))
        task.cancel()
        await asyncio.sleep(0.02)
        self.assertFalse(task.done())
        release.set()
        with self.assertRaises(asyncio.CancelledError):
            await task

    async def test_trigger_create_list_delete_are_opaque_and_replay_safe(self) -> None:
        """Trigger lifecycle must preserve immutable public IDs and replay deletes."""
        from daem0nmcp.api.v7.models import MutationReceipt, Page
        from daem0nmcp.api.v7.rule_trigger_operations import (
            RuleTriggerOperationError,
        )
        from daem0nmcp.api.v7.tools import TriggerView

        operations = self._operations()
        first = await operations["context_trigger_create"](
            workspace=self.workspace,
            request=self._trigger_create_request(),
        )
        replay = await operations["context_trigger_create"](
            workspace=self.workspace,
            request=self._trigger_create_request(),
        )
        second = await operations["context_trigger_create"](
            workspace=self.workspace,
            request=self._trigger_create_request(
                key="trigger-create-0002",
                trigger_type="tag",
                pattern="authentication|security",
                recall_query="Authentication guidance",
                categories=["warning"],
            ),
        )

        self.assertIsInstance(first, TriggerView)
        self.assertEqual(first, replay)
        self.assertRegex(first.trigger_id, r"^trg_[0-9a-f]{64}$")
        self.assertEqual({"warning"}, second.categories)
        with self.assertRaises(RuleTriggerOperationError) as caught:
            await operations["context_trigger_create"](
                workspace=self.workspace,
                request=self._trigger_create_request(pattern="other/**/*.py"),
            )
        self.assertEqual("IDEMPOTENCY_CONFLICT", caught.exception.code)

        page = await operations["context_trigger_list"](
            workspace=self.workspace,
            request=_request(
                "context_trigger_list",
                workspace_id=self.workspace.workspace_id,
                active_only=True,
                limit=1,
            ),
        )
        self.assertIsInstance(page, Page)
        self.assertEqual(1, len(page.items))
        self.assertTrue(page.truncated)
        following = await operations["context_trigger_list"](
            workspace=self.workspace,
            request=_request(
                "context_trigger_list",
                workspace_id=self.workspace.workspace_id,
                active_only=True,
                cursor=page.next_cursor,
                limit=2,
            ),
        )
        self.assertEqual(
            {first.trigger_id, second.trigger_id},
            {page.items[0].trigger_id, *[item.trigger_id for item in following.items]},
        )

        delete_request = _request(
            "context_trigger_delete",
            workspace_id=self.workspace.workspace_id,
            trigger_id=first.trigger_id,
            preflight_token=PREFLIGHT_TOKEN,
        )
        deleted = await operations["context_trigger_delete"](
            workspace=self.workspace,
            request=delete_request,
        )
        delete_replay = await operations["context_trigger_delete"](
            workspace=self.workspace,
            request=delete_request,
        )
        self.assertIsInstance(deleted, MutationReceipt)
        self.assertFalse(deleted.idempotent_replay)
        self.assertTrue(delete_replay.idempotent_replay)
        self.assertEqual([first.trigger_id], deleted.affected_ids)
        self.assertEqual(1, len(deleted.event_ids))
        self.assertEqual(deleted.event_ids, delete_replay.event_ids)
        self.assertRegex(deleted.event_ids[0], r"^evt_[0-9a-f]{64}$")
        with closing(sqlite3.connect(self.database)) as connection:
            self.assertEqual(
                (2, 2, 3, 1),
                (
                    connection.execute(
                        "SELECT count(*) FROM context_triggers"
                    ).fetchone()[0],
                    connection.execute(
                        "SELECT count(*) FROM public_object_ids "
                        "WHERE object_kind='trigger'"
                    ).fetchone()[0],
                    connection.execute(
                        "SELECT count(*) FROM governance_events "
                        "WHERE stream_kind='trigger'"
                    ).fetchone()[0],
                    connection.execute(
                        "SELECT count(*) FROM governance_context_triggers "
                        "WHERE deleted_at_us IS NOT NULL"
                    ).fetchone()[0],
                ),
            )

    async def test_trigger_match_uses_bounded_matcher_and_typed_recall(self) -> None:
        """Matched values must drive Task 8 recall without mutating trigger stats."""
        from daem0nmcp.api.v7.tools import TriggerMatchData

        retrieval = self._retrieval()

        class Recall:
            def retrieve(self, workspace, query, linked_workspace_ids):
                del workspace, query, linked_workspace_ids
                return retrieval

        operations = self._operations(recall_service=Recall())
        requests = (
            self._trigger_create_request(
                key="trigger-match-file-0001",
                trigger_type="file",
                pattern="src/**/*.py",
                recall_query="File guidance",
                categories=["decision"],
            ),
            self._trigger_create_request(
                key="trigger-match-tag-0002",
                trigger_type="tag",
                pattern="authentication|security",
                recall_query="Tag guidance",
            ),
            self._trigger_create_request(
                key="trigger-match-entity-0003",
                trigger_type="entity",
                pattern=r".*Service$",
                recall_query="Entity guidance",
            ),
            self._trigger_create_request(
                key="trigger-match-disabled-0004",
                trigger_type="tag",
                pattern="authentication",
                recall_query="Disabled guidance",
                enabled=False,
            ),
        )
        for request in requests:
            await operations["context_trigger_create"](
                workspace=self.workspace,
                request=request,
            )

        result = await operations["context_triggers_match"](
            workspace=self.workspace,
            request=_request(
                "context_triggers_match",
                workspace_id=self.workspace.workspace_id,
                relative_file_path="src/auth/service.py",
                tags=["authentication"],
                entities=["AuthService"],
                limit=3,
            ),
        )

        self.assertIsInstance(result, TriggerMatchData)
        self.assertFalse(result.truncated)
        self.assertEqual(3, len(result.matches))
        self.assertEqual(
            {"src/auth/service.py", "authentication", "AuthService"},
            {match.matched_value for match in result.matches},
        )
        self.assertTrue(
            all(
                match.records == [retrieval.items[0].record] for match in result.matches
            )
        )
        with closing(sqlite3.connect(self.database)) as connection:
            self.assertEqual(
                [(0, None)] * 4,
                connection.execute(
                    "SELECT trigger_count,last_triggered FROM context_triggers "
                    "ORDER BY id"
                ).fetchall(),
            )

    async def test_trigger_match_rejects_aggregate_candidate_overflow(self) -> None:
        """Separate field limits must not bypass Task 4's aggregate admission cap."""
        from daem0nmcp.api.v7.rule_trigger_operations import (
            RuleTriggerOperationError,
        )

        with self.assertRaises(RuleTriggerOperationError) as caught:
            await self._operations()["context_triggers_match"](
                workspace=self.workspace,
                request=_request(
                    "context_triggers_match",
                    workspace_id=self.workspace.workspace_id,
                    tags=[f"tag-{index}" for index in range(32)],
                    entities=["overflow"],
                ),
            )
        self.assertEqual("INVALID_ARGUMENT", caught.exception.code)

    async def test_trigger_create_rejects_unsafe_pattern_before_write(self) -> None:
        """Invalid persisted regexes must fail before a row or ID is committed."""
        from daem0nmcp.api.v7.rule_trigger_operations import (
            RuleTriggerOperationError,
        )

        with self.assertRaises(RuleTriggerOperationError) as caught:
            await self._operations()["context_trigger_create"](
                workspace=self.workspace,
                request=self._trigger_create_request(
                    trigger_type="tag",
                    pattern="(",
                ),
            )
        self.assertEqual("INVALID_ARGUMENT", caught.exception.code)
        with closing(sqlite3.connect(self.database)) as connection:
            self.assertEqual(
                (0, 0, 0),
                (
                    connection.execute(
                        "SELECT count(*) FROM context_triggers"
                    ).fetchone()[0],
                    connection.execute(
                        "SELECT count(*) FROM public_object_ids "
                        "WHERE object_kind='trigger'"
                    ).fetchone()[0],
                    connection.execute(
                        "SELECT count(*) FROM governance_events"
                    ).fetchone()[0],
                ),
            )

    async def test_trigger_create_enforces_active_admission_bound(self) -> None:
        """Persisting a 101st active trigger would exceed bounded evaluation."""
        from daem0nmcp.api.v7.rule_trigger_operations import (
            RuleTriggerOperationError,
        )

        operation = self._operations()["context_trigger_create"]
        for index in range(100):
            await operation(
                workspace=self.workspace,
                request=self._trigger_create_request(
                    key=f"trigger-bound-{index:04d}",
                    pattern=f"src/{index}/**/*.py",
                ),
            )

        with self.assertRaises(RuleTriggerOperationError) as caught:
            await operation(
                workspace=self.workspace,
                request=self._trigger_create_request(
                    key="trigger-bound-overflow-0100",
                    pattern="src/overflow/**/*.py",
                ),
            )
        self.assertEqual("INVALID_ARGUMENT", caught.exception.code)
        with closing(sqlite3.connect(self.database)) as connection:
            self.assertEqual(
                100,
                connection.execute(
                    "SELECT count(*) FROM governance_context_triggers "
                    "WHERE enabled=1 AND deleted_at_us IS NULL"
                ).fetchone()[0],
            )

    async def test_trigger_match_fails_closed_without_recall_capability(self) -> None:
        """A matching trigger must not fabricate an empty successful recall."""
        from daem0nmcp.api.v7.rule_trigger_operations import (
            RuleTriggerOperationError,
        )

        operations = self._operations()
        await operations["context_trigger_create"](
            workspace=self.workspace,
            request=self._trigger_create_request(),
        )
        with self.assertRaises(RuleTriggerOperationError) as caught:
            await operations["context_triggers_match"](
                workspace=self.workspace,
                request=_request(
                    "context_triggers_match",
                    workspace_id=self.workspace.workspace_id,
                    relative_file_path="src/module.py",
                ),
            )
        self.assertEqual("CAPABILITY_DEGRADED", caught.exception.code)


if __name__ == "__main__":
    unittest.main()
