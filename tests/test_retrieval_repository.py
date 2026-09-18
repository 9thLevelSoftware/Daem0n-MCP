"""Contract tests for canonical SQLite retrieval repository reads."""

from __future__ import annotations

import asyncio
import hashlib
import importlib.util
import json
import sqlite3
import tempfile
import threading
import unittest
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from daem0nmcp.retrieval.policy import apply_retrieval_policy
from daem0nmcp.retrieval.repository import (
    RetrievalRepositoryError,
    SQLiteRetrievalRepository,
    _EventRootCacheMissError,
    sqlite_read_connection_factory,
)
from daem0nmcp.retrieval.specialized_contract import (
    specialized_projection_contract,
)
from daem0nmcp.retrieval.types import (
    Candidate,
    EvidenceRef,
    FusedCandidate,
    ProviderResult,
    RetrievalQuery,
)

WORKSPACE_ID = "ws_0123456789abcdef01234567"
OTHER_WORKSPACE_ID = "ws_89abcdef0123456701234567"
BASE_US = 1_700_000_000_000_000
PROCEDURE_STEP_0 = "Deploy the replacement credential."
PROCEDURE_STEP_1 = "Revoke the old credential."


def _hash(character: str) -> str:
    return character * 64


def _json_hash(value: object) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _record_id(character: str) -> str:
    return f"mem_{_hash(character)}"


def _event_id(character: str) -> str:
    return f"evt_{_hash(character)}"


def _fact_id(character: str) -> str:
    return f"fact_{_hash(character)}"


def _relationship_id(character: str) -> str:
    return f"rel_{_hash(character)}"


def _snapshot(offset_us: int = 200) -> datetime:
    return datetime.fromtimestamp(
        (BASE_US + offset_us) / 1_000_000,
        timezone.utc,
    )


def _migration_statements(*versions: int) -> tuple[str, ...]:
    schema_path = (
        Path(__file__).resolve().parents[1] / "daem0nmcp" / "migrations" / "schema.py"
    )
    spec = importlib.util.spec_from_file_location(
        "retrieval_repository_test_schema", schema_path
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    selected = set(versions)
    return tuple(
        statement
        for version, _description, statements in module.MIGRATIONS
        if version in selected
        for statement in statements
    )


def _candidate(
    *,
    record_id: str,
    content_hash: str,
    channel: str,
    event_id: str,
    version_id: str | None = None,
    generation: int | None = 1,
    relation_path: tuple[str, ...] = (),
    policy_notes: tuple[str, ...] = (),
) -> FusedCandidate:
    evidence = EvidenceRef(
        record_id=record_id,
        event_id=event_id,
        content_hash=content_hash,
        version_id=version_id,
        relation_path=relation_path,
        provider=channel,
    )
    return FusedCandidate(
        evidence=evidence,
        evidence_refs=(evidence,),
        score=1.0,
        channels=frozenset({channel}),
        channel_ranks=((channel, 1),),
        manifest_generations=((channel, generation),),
        policy_notes=policy_notes,
    )


def _fused_main_candidate() -> FusedCandidate:
    refs = (
        EvidenceRef(
            record_id=_record_id("1"),
            event_id=_event_id("c"),
            content_hash=_hash("1"),
            version_id=None,
            provider="lexical",
        ),
        EvidenceRef(
            record_id=_record_id("1"),
            event_id=_event_id("a"),
            content_hash=_hash("1"),
            version_id=None,
            provider="procedure",
        ),
        EvidenceRef(
            record_id=_record_id("1"),
            event_id=_event_id("b"),
            content_hash=_hash("1"),
            version_id=None,
            provider="outcome",
        ),
    )
    return FusedCandidate(
        evidence=refs[0],
        evidence_refs=refs,
        score=1.0,
        channels=frozenset({"lexical", "outcome", "procedure"}),
        channel_ranks=(("lexical", 1), ("outcome", 1), ("procedure", 1)),
        manifest_generations=(
            ("lexical", 1),
            ("outcome", 1),
            ("procedure", 1),
        ),
        policy_notes=(
            "OUTCOME_FAILED",
            f"PROCEDURE_STEP:0:{_json_hash(PROCEDURE_STEP_0)}",
            f"PROCEDURE_STEP:1:{_json_hash(PROCEDURE_STEP_1)}",
        ),
    )


class _StaticProvider:
    def __init__(self, result: ProviderResult) -> None:
        self.name = result.provider
        self._result = result

    async def search(self, query: RetrievalQuery, limit: int) -> ProviderResult:
        del query, limit
        return self._result


class _FixedClock:
    def now(self) -> datetime:
        return _snapshot()


class _WordTokenizer:
    @staticmethod
    def count_tokens(text: str) -> int:
        return len(text.split())


class _CanonicalFixture:
    def __init__(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.database_path = Path(self.temporary_directory.name) / "canonical.db"
        self.repositories: list[SQLiteRetrievalRepository] = []
        self.connection = sqlite3.connect(self.database_path)
        self.connection.execute("PRAGMA foreign_keys=ON")
        for statement in _migration_statements(16, 17, 18, 30):
            self.connection.execute(statement)
        self._insert_events()
        self._insert_records()
        self._insert_versions()
        self._insert_projection_rows()
        self._insert_manifests()
        self.connection.commit()

    def close(self) -> None:
        for repository in self.repositories:
            repository.close()
        self.connection.close()
        self.temporary_directory.cleanup()

    def _insert_event(
        self,
        workspace_id: str,
        character: str,
        *,
        stream_id: str,
        stream_kind: str,
        stream_version: int,
        recorded_offset: int,
    ) -> None:
        event_hash = _hash(character)
        self.connection.execute(
            """
            INSERT INTO memory_events (
                event_id,workspace_id,stream_id,stream_kind,stream_version,
                event_type,event_schema_version,occurred_at_us,recorded_at_us,
                actor_type,actor_id,causation_event_id,correlation_id,
                payload_json,payload_hash,previous_event_hash,event_hash
            ) VALUES (?,?,?,?,?,'memory.test',1,?,?,'system',NULL,NULL,NULL,
                      '{}',?,NULL,?)
            """,
            (
                _event_id(character),
                workspace_id,
                stream_id,
                stream_kind,
                stream_version,
                BASE_US + recorded_offset,
                BASE_US + recorded_offset,
                hashlib.sha256(b"{}").hexdigest(),
                event_hash,
            ),
        )

    def _insert_events(self) -> None:
        main = _record_id("1")
        fact = _fact_id("7")
        relationship = _relationship_id("9")
        for character, stream, kind, version, offset in (
            ("a", main, "memory", 1, 20),
            ("b", main, "memory", 2, 40),
            ("c", main, "memory", 3, 30),
            ("d", fact, "fact", 1, 50),
            ("e", fact, "fact", 2, 80),
            ("f", _record_id("2"), "memory", 1, 10),
            ("9", relationship, "relationship", 1, 60),
        ):
            self._insert_event(
                WORKSPACE_ID,
                character,
                stream_id=stream,
                stream_kind=kind,
                stream_version=version,
                recorded_offset=offset,
            )
        self._insert_event(
            OTHER_WORKSPACE_ID,
            "8",
            stream_id=_record_id("3"),
            stream_kind="memory",
            stream_version=1,
            recorded_offset=10,
        )

    def _insert_record(
        self,
        *,
        record_id: str,
        workspace_id: str,
        record_type: str,
        content: str,
        content_hash: str,
        source_event_id: str,
        stream_version: int,
        created_offset: int,
        updated_offset: int,
        outcome: str | None = None,
        worked: int | None = None,
    ) -> None:
        self.connection.execute(
            """
            INSERT INTO memory_records (
                record_id,workspace_id,record_type,legacy_type,content,
                content_hash,rationale,context_json,tags_json,file_path,
                file_path_relative,keywords,is_permanent,pinned,archived,
                outcome,worked,recall_count,surprise_score,importance_score,
                source_client,source_model,stream_version,source_event_id,
                created_at_us,updated_at_us,deleted_at_us,state_hash
            ) VALUES (?,?,?,NULL,?,?,NULL,?,?,NULL,NULL,NULL,0,0,0,?,?,0,
                      NULL,NULL,'test',NULL,?,?,?, ?,NULL,?)
            """,
            (
                record_id,
                workspace_id,
                record_type,
                content,
                content_hash,
                json.dumps({"visibility": "workspace"}),
                json.dumps(["alpha", "sqlite"]),
                outcome,
                worked,
                stream_version,
                source_event_id,
                BASE_US + created_offset,
                BASE_US + updated_offset,
                _hash("7"),
            ),
        )

    def _insert_records(self) -> None:
        self._insert_record(
            record_id=_record_id("1"),
            workspace_id=WORKSPACE_ID,
            record_type="warning",
            content="Rotate credentials safely.",
            content_hash=_hash("1"),
            source_event_id=_event_id("c"),
            stream_version=3,
            created_offset=20,
            updated_offset=30,
            outcome="The previous rotation failed safely.",
            worked=0,
        )
        self._insert_record(
            record_id=_record_id("2"),
            workspace_id=WORKSPACE_ID,
            record_type="decision",
            content="Use the replacement credential.",
            content_hash=_hash("2"),
            source_event_id=_event_id("f"),
            stream_version=1,
            created_offset=10,
            updated_offset=10,
        )
        self._insert_record(
            record_id=_record_id("3"),
            workspace_id=OTHER_WORKSPACE_ID,
            record_type="decision",
            content="Foreign workspace secret.",
            content_hash=_hash("3"),
            source_event_id=_event_id("8"),
            stream_version=1,
            created_offset=10,
            updated_offset=10,
        )

    def _insert_versions(self) -> None:
        self.connection.executemany(
            """
            INSERT INTO memory_fact_versions (
                fact_version_id,fact_id,workspace_id,version,
                subject_record_id,predicate,object_kind,object_json,
                legacy_type,content_hash,confidence,verification_count,
                is_verified,evidence_json,metadata_json,valid_from_us,
                valid_to_us,transaction_from_us,transaction_to_us,
                asserted_by_event_id,retracted_by_event_id
            ) VALUES (?,?,?,?,?,'credential_status','text','"old"',NULL,?,
                      1.0,0,1,'[]','{}',?,?,?,?,?,?)
            """,
            (
                (
                    _fact_id("d"),
                    _fact_id("7"),
                    WORKSPACE_ID,
                    1,
                    _record_id("1"),
                    _hash("d"),
                    BASE_US,
                    BASE_US + 70,
                    BASE_US + 50,
                    BASE_US + 80,
                    _event_id("d"),
                    _event_id("e"),
                ),
                (
                    _fact_id("e"),
                    _fact_id("7"),
                    WORKSPACE_ID,
                    2,
                    _record_id("1"),
                    _hash("e"),
                    BASE_US + 70,
                    None,
                    BASE_US + 80,
                    None,
                    _event_id("e"),
                    None,
                ),
            ),
        )
        self.connection.execute(
            """
            INSERT INTO memory_relationship_versions (
                relationship_version_id,relationship_id,workspace_id,version,
                source_record_id,target_record_id,relationship_type,
                legacy_type,description,confidence,metadata_json,content_hash,
                valid_from_us,valid_to_us,transaction_from_us,
                transaction_to_us,asserted_by_event_id,retracted_by_event_id
            ) VALUES (?,?,?,?,?,?,'related_to',NULL,NULL,1.0,'{}',?,?,NULL,?,
                      NULL,?,NULL)
            """,
            (
                _relationship_id("9"),
                _relationship_id("9"),
                WORKSPACE_ID,
                1,
                _record_id("1"),
                _record_id("2"),
                _hash("9"),
                BASE_US,
                BASE_US + 60,
                _event_id("9"),
            ),
        )

    def _document(
        self,
        record_id: str,
        content: str,
        content_hash: str,
        category: str,
        source_event_id: str,
        transaction_offset: int,
        valid_offset: int | None = None,
    ) -> None:
        if valid_offset is None:
            valid_offset = transaction_offset
        self.connection.execute(
            """
            INSERT INTO retrieval_documents (
                workspace_id,projection_generation,record_id,content,
                rationale,tags_text,category,valid_from_us,valid_to_us,
                transaction_from_us,transaction_to_us,visibility,archived,
                content_hash,source_event_id
            ) VALUES (?,1,?,?,'','alpha\nsqlite',?,?,NULL,?,NULL,
                      'workspace',0,?,?)
            """,
            (
                WORKSPACE_ID,
                record_id,
                content,
                category,
                BASE_US + valid_offset,
                BASE_US + transaction_offset,
                content_hash,
                source_event_id,
            ),
        )

    def _insert_projection_rows(self) -> None:
        self._document(
            _record_id("1"),
            "Rotate credentials safely.",
            _hash("1"),
            "warning",
            _event_id("c"),
            30,
            20,
        )
        self._document(
            _record_id("2"),
            "Use the replacement credential.",
            _hash("2"),
            "decision",
            _event_id("f"),
            10,
        )
        self.connection.executemany(
            "INSERT INTO record_procedures VALUES (?,?,?,?,?,?,?)",
            (
                (
                    WORKSPACE_ID,
                    1,
                    _record_id("1"),
                    0,
                    PROCEDURE_STEP_0,
                    _json_hash(PROCEDURE_STEP_0),
                    _event_id("a"),
                ),
                (
                    WORKSPACE_ID,
                    1,
                    _record_id("1"),
                    1,
                    PROCEDURE_STEP_1,
                    _json_hash(PROCEDURE_STEP_1),
                    _event_id("a"),
                ),
            ),
        )
        self.connection.execute(
            "INSERT INTO record_outcome_view VALUES (?,?,?,?,?,?,?)",
            (
                WORKSPACE_ID,
                1,
                _record_id("1"),
                0,
                "The previous rotation failed safely.",
                _event_id("b"),
                BASE_US + 40,
            ),
        )
        self.connection.execute(
            "INSERT INTO dense_projection_refs VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                WORKSPACE_ID,
                "local",
                1,
                _record_id("1"),
                _hash("1"),
                "test-model",
                3,
                "ready",
                _event_id("c"),
                None,
                BASE_US + 30,
            ),
        )

    def _event_root(self, workspace_id: str) -> tuple[int, str, int, str]:
        rows = self.connection.execute(
            "SELECT event_hash FROM memory_events WHERE workspace_id=? "
            "ORDER BY event_id",
            (workspace_id,),
        ).fetchall()
        digest = hashlib.sha256()
        for row in rows:
            digest.update(bytes.fromhex(str(row[0])))
        cursor = self.connection.execute(
            "SELECT recorded_at_us,event_id FROM memory_events "
            "WHERE workspace_id=? ORDER BY recorded_at_us DESC,event_id DESC "
            "LIMIT 1",
            (workspace_id,),
        ).fetchone()
        assert cursor is not None
        return len(rows), digest.hexdigest(), int(cursor[0]), str(cursor[1])

    def _insert_manifests(self) -> None:
        count, root, cursor_us, cursor_event = self._event_root(WORKSPACE_ID)
        details = {
            "lexical": {"projection": "lexical"},
            "dense": {
                "provider_key": "local",
                "model_id": "test-model",
                "dimension": 3,
            },
            **{
                projection: specialized_projection_contract(
                    WORKSPACE_ID,
                    projection,
                    1,
                    _hash("f"),
                )[2]
                for projection in (
                    "temporal",
                    "procedure",
                    "outcome",
                    "graph",
                )
            },
        }
        row_counts = {
            "lexical": 2,
            "dense": 1,
            "temporal": 2,
            "procedure": 2,
            "outcome": 1,
            "graph": 1,
        }
        for number, projection in enumerate(details, start=1):
            self.connection.execute(
                """
                INSERT INTO projection_manifests (
                    manifest_id,workspace_id,projection_name,generation,
                    projection_version,status,source_event_count,
                    source_event_root_hash,cursor_recorded_at_us,cursor_event_id,
                    row_count,builder_version,details_json,started_at_us,
                    completed_at_us,activated_at_us
                ) VALUES (?,? ,?,1,1,'active',?,?,?,?,?,'test',?,?,?,?)
                """,
                (
                    f"prj_{str(number) * 64}",
                    WORKSPACE_ID,
                    projection,
                    count,
                    root,
                    cursor_us,
                    cursor_event,
                    row_counts[projection],
                    json.dumps(details[projection], sort_keys=True),
                    BASE_US + 100,
                    BASE_US + 100,
                    BASE_US + 100,
                ),
            )

    def query(self, **changes: object) -> RetrievalQuery:
        values: dict[str, object] = {
            "workspace_id": WORKSPACE_ID,
            "text": "credential",
            "candidate_limit": 20,
            "include_invalidated": True,
        }
        values.update(changes)
        return RetrievalQuery(**values)

    def repository(self, **changes: object) -> SQLiteRetrievalRepository:
        repository = SQLiteRetrievalRepository(self.database_path, **changes)
        self.repositories.append(repository)
        return repository


class SQLiteRetrievalRepositoryConstructionTests(unittest.TestCase):
    def test_repository_requires_a_file_backed_sqlite_source(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            database_path = Path(temporary_directory) / "canonical.db"
            database_path.touch()

            repository = SQLiteRetrievalRepository(database_path)

            self.assertEqual(database_path.resolve(), repository.database_path)

    def test_factory_opens_a_fresh_read_only_connection(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            database_path = Path(temporary_directory) / "canonical.db"
            connection = sqlite3.connect(database_path)
            connection.execute("CREATE TABLE value (number INTEGER)")
            connection.commit()
            connection.close()
            factory = sqlite_read_connection_factory(database_path)

            reader = factory()
            try:
                row = reader.execute("SELECT count(*) FROM value").fetchone()
                self.assertEqual(0, row[0])
                with self.assertRaises(sqlite3.OperationalError):
                    reader.execute("INSERT INTO value VALUES (1)")
            finally:
                reader.close()

    def test_timeout_must_be_bounded_even_for_huge_integers(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            database_path = Path(temporary_directory) / "canonical.db"
            database_path.touch()

            with self.assertRaises(ValueError):
                SQLiteRetrievalRepository(
                    database_path,
                    timeout_seconds=10**400,
                )


class SQLiteRetrievalRepositoryPolicyTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.fixture = _CanonicalFixture()

    def tearDown(self) -> None:
        self.fixture.close()

    def _make_fact_retraction_current_version(self) -> None:
        self.fixture.connection.execute(
            "UPDATE memory_fact_versions SET valid_from_us=?,"
            "valid_to_us=?,retracted_by_event_id=asserted_by_event_id "
            "WHERE fact_version_id=?",
            (BASE_US, BASE_US + 70, _fact_id("e")),
        )
        self.fixture.connection.execute(
            "UPDATE memory_fact_versions SET valid_to_us=NULL WHERE fact_version_id=?",
            (_fact_id("d"),),
        )
        self.fixture.connection.execute(
            "UPDATE memory_records SET created_at_us=? WHERE workspace_id=? "
            "AND record_id=?",
            (BASE_US + 90, WORKSPACE_ID, _record_id("1")),
        )
        self.fixture.connection.execute(
            "UPDATE retrieval_documents SET valid_from_us=? "
            "WHERE workspace_id=? AND record_id=?",
            (BASE_US + 90, WORKSPACE_ID, _record_id("1")),
        )
        self.fixture.connection.commit()

    def test_event_root_scan_uses_the_current_covering_index(self) -> None:
        plan = self.fixture.connection.execute(
            "EXPLAIN QUERY PLAN SELECT event_hash FROM memory_events "
            "WHERE workspace_id=? ORDER BY event_id",
            (WORKSPACE_ID,),
        ).fetchall()
        rendered = " ".join(str(row[3]) for row in plan)

        self.assertIn("idx_memory_events_root_covering", rendered)
        self.assertNotIn("USE TEMP B-TREE", rendered)

    async def test_event_root_is_single_flight_and_reused_by_warm_reads(self) -> None:
        repository = self.fixture.repository()
        candidate = _candidate(
            record_id=_record_id("1"),
            content_hash=_hash("1"),
            channel="lexical",
            event_id=_event_id("c"),
        )
        original = repository._calculate_event_root
        calls = 0

        def observed(connection, workspace_id):
            nonlocal calls
            calls += 1
            return original(connection, workspace_id)

        with patch.object(repository, "_calculate_event_root", side_effect=observed):
            results = await asyncio.gather(
                *(
                    repository.load_policy_records(
                        self.fixture.query(),
                        (candidate,),
                        snapshot_time=_snapshot(),
                    )
                    for _ in range(4)
                )
            )
            selected = await repository.load_selected_evidence(
                self.fixture.query(),
                (candidate,),
                snapshot_time=_snapshot(),
            )

        self.assertTrue(all(len(result) == 1 for result in results))
        self.assertEqual(1, len(selected))
        self.assertEqual(1, calls)

    async def test_external_projection_commit_invalidates_cached_event_root(
        self,
    ) -> None:
        repository = self.fixture.repository()
        candidate = _candidate(
            record_id=_record_id("1"),
            content_hash=_hash("1"),
            channel="lexical",
            event_id=_event_id("c"),
        )
        original = repository._calculate_event_root
        calls = 0

        def observed(connection, workspace_id):
            nonlocal calls
            calls += 1
            return original(connection, workspace_id)

        with patch.object(repository, "_calculate_event_root", side_effect=observed):
            await repository.load_policy_records(
                self.fixture.query(), (candidate,), snapshot_time=_snapshot()
            )
            self.fixture.connection.execute(
                "UPDATE projection_manifests SET builder_version=? "
                "WHERE workspace_id=? AND projection_name='lexical'",
                ("test-after-commit", WORKSPACE_ID),
            )
            self.fixture.connection.commit()
            await repository.load_policy_records(
                self.fixture.query(), (candidate,), snapshot_time=_snapshot()
            )
            await repository.load_policy_records(
                self.fixture.query(), (candidate,), snapshot_time=_snapshot()
            )

        self.assertEqual(2, calls)

    def test_revision_change_while_pinning_snapshot_rejects_cached_root(self) -> None:
        repository = self.fixture.repository()
        connection = repository._open_connection()
        try:
            with (
                patch.object(
                    repository,
                    "_observer_revision_locked",
                    side_effect=(7, 8),
                ),
                self.assertRaises(_EventRootCacheMissError),
            ):
                repository._begin_read_snapshot(connection)
        finally:
            if connection.in_transaction:
                connection.rollback()
            connection.close()

    async def test_external_event_corruption_invalidates_cache_and_fails_closed(
        self,
    ) -> None:
        repository = self.fixture.repository()
        candidate = _candidate(
            record_id=_record_id("1"),
            content_hash=_hash("1"),
            channel="lexical",
            event_id=_event_id("c"),
        )
        await repository.load_policy_records(
            self.fixture.query(), (candidate,), snapshot_time=_snapshot()
        )
        # Simulate out-of-band database corruption rather than a supported write.
        self.fixture.connection.execute("DROP TRIGGER memory_events_no_update")
        self.fixture.connection.execute(
            "UPDATE memory_events SET event_hash=? WHERE workspace_id=? AND event_id=?",
            (hashlib.sha256(b"corrupt").hexdigest(), WORKSPACE_ID, _event_id("c")),
        )
        self.fixture.connection.commit()

        with self.assertRaises(RetrievalRepositoryError) as raised:
            await repository.load_policy_records(
                self.fixture.query(), (candidate,), snapshot_time=_snapshot()
            )

        self.assertEqual("POLICY_STATE_UNAVAILABLE", raised.exception.code)

    async def test_lexical_policy_state_comes_from_canonical_and_active_rows(
        self,
    ) -> None:
        candidate = _candidate(
            record_id=_record_id("1"),
            content_hash=_hash("1"),
            channel="lexical",
            event_id=_event_id("c"),
        )

        records = tuple(
            await self.fixture.repository().load_policy_records(
                self.fixture.query(),
                (candidate,),
                snapshot_time=_snapshot(),
            )
        )

        self.assertEqual(1, len(records))
        record = records[0]
        self.assertEqual(WORKSPACE_ID, record.workspace_id)
        self.assertEqual(_record_id("1"), record.record_id)
        self.assertEqual(_hash("1"), record.content_hash)
        self.assertEqual(frozenset({_event_id("c")}), record.source_event_ids)
        self.assertEqual("workspace", record.visibility)
        self.assertTrue(record.visibility_allowed)
        self.assertFalse(record.archived)
        self.assertEqual("warning", record.category)
        self.assertEqual(frozenset({"alpha", "sqlite"}), record.tags)
        self.assertEqual(
            (("lexical", _hash("1")),),
            record.projection_content_hashes,
        )
        self.assertEqual(
            (("lexical", 1),),
            record.active_manifest_generations,
        )

    async def test_lexical_channel_cannot_bypass_record_fact_invalidation(
        self,
    ) -> None:
        self._make_fact_retraction_current_version()
        self.fixture.connection.execute(
            "UPDATE memory_relationship_versions SET "
            "relationship_type='supersedes',source_record_id=?,"
            "target_record_id=? WHERE relationship_version_id=?",
            (
                _record_id("2"),
                _record_id("1"),
                _relationship_id("9"),
            ),
        )
        self.fixture.connection.commit()
        candidate = _candidate(
            record_id=_record_id("1"),
            content_hash=_hash("1"),
            channel="lexical",
            event_id=_event_id("c"),
        )
        excluded_query = self.fixture.query(
            include_invalidated=False,
            as_of_valid_time=_snapshot(200),
            as_of_transaction_time=_snapshot(200),
        )
        included_query = self.fixture.query(
            include_invalidated=True,
            as_of_valid_time=_snapshot(200),
            as_of_transaction_time=_snapshot(200),
        )

        excluded_state = tuple(
            await self.fixture.repository().load_policy_records(
                excluded_query,
                (candidate,),
                snapshot_time=_snapshot(200),
            )
        )
        excluded = apply_retrieval_policy(
            excluded_query,
            (candidate,),
            excluded_state,
            snapshot_time=_snapshot(200),
        )
        included_state = tuple(
            await self.fixture.repository().load_policy_records(
                included_query,
                (candidate,),
                snapshot_time=_snapshot(200),
            )
        )
        included = apply_retrieval_policy(
            included_query,
            (candidate,),
            included_state,
            snapshot_time=_snapshot(200),
        )

        self.assertEqual("INVALIDATED_VERSION", excluded.rejections[0].reason)
        self.assertEqual((), excluded.candidates)
        self.assertEqual(1, len(included.candidates))
        self.assertIn(
            f"SUPERSEDED_BY:{_fact_id('e')}",
            included.candidates[0].policy_notes,
        )

    async def test_lexical_channel_cannot_bypass_fact_contradiction(self) -> None:
        self.fixture.connection.execute(
            "UPDATE memory_fact_versions SET metadata_json=? WHERE fact_version_id=?",
            (
                json.dumps(
                    {"has_unresolved_contradiction": True},
                    separators=(",", ":"),
                    sort_keys=True,
                ),
                _fact_id("e"),
            ),
        )
        self.fixture.connection.commit()
        candidate = _candidate(
            record_id=_record_id("1"),
            content_hash=_hash("1"),
            channel="lexical",
            event_id=_event_id("c"),
        )
        query = self.fixture.query(include_invalidated=True)

        states = tuple(
            await self.fixture.repository().load_policy_records(
                query,
                (candidate,),
                snapshot_time=_snapshot(),
            )
        )
        result = apply_retrieval_policy(
            query,
            (candidate,),
            states,
            snapshot_time=_snapshot(),
        )

        self.assertEqual("UNRESOLVED_CONTRADICTION", result.rejections[0].reason)
        self.assertEqual((), result.candidates)

    async def test_versionless_record_uses_historical_fact_validity(
        self,
    ) -> None:
        self._make_fact_retraction_current_version()
        candidate = _candidate(
            record_id=_record_id("1"),
            content_hash=_hash("1"),
            channel="lexical",
            event_id=_event_id("c"),
        )
        query = self.fixture.query(
            include_invalidated=True,
            as_of_valid_time=_snapshot(69),
            as_of_transaction_time=_snapshot(200),
        )

        states = tuple(
            await self.fixture.repository().load_policy_records(
                query,
                (candidate,),
                snapshot_time=_snapshot(200),
            )
        )
        result = apply_retrieval_policy(
            query,
            (candidate,),
            states,
            snapshot_time=_snapshot(200),
        )

        self.assertEqual(_snapshot(0), states[0].valid_from)
        self.assertEqual(_snapshot(70), states[0].valid_to)
        self.assertEqual(
            (_record_id("1"),), tuple(item.record_id for item in result.candidates)
        )

    async def test_dense_policy_requires_the_active_ready_provider_row(self) -> None:
        candidate = _candidate(
            record_id=_record_id("1"),
            content_hash=_hash("1"),
            channel="dense",
            event_id=_event_id("c"),
        )

        records = tuple(
            await self.fixture.repository().load_policy_records(
                self.fixture.query(),
                (candidate,),
                snapshot_time=_snapshot(),
            )
        )

        self.assertEqual(frozenset({_event_id("c")}), records[0].source_event_ids)
        self.assertEqual((("dense", _hash("1")),), records[0].projection_content_hashes)

    async def test_procedure_policy_authenticates_structured_step_provenance(
        self,
    ) -> None:
        candidate = _candidate(
            record_id=_record_id("1"),
            content_hash=_hash("1"),
            channel="procedure",
            event_id=_event_id("a"),
            policy_notes=(
                f"PROCEDURE_STEP:0:{_json_hash(PROCEDURE_STEP_0)}",
                f"PROCEDURE_STEP:1:{_json_hash(PROCEDURE_STEP_1)}",
            ),
        )

        records = tuple(
            await self.fixture.repository().load_policy_records(
                self.fixture.query(),
                (candidate,),
                snapshot_time=_snapshot(),
            )
        )

        self.assertEqual(frozenset({_event_id("a")}), records[0].source_event_ids)
        self.assertEqual((("procedure", 1),), records[0].active_manifest_generations)

    async def test_outcome_policy_uses_the_active_view_event_and_transaction(
        self,
    ) -> None:
        candidate = _candidate(
            record_id=_record_id("1"),
            content_hash=_hash("1"),
            channel="outcome",
            event_id=_event_id("b"),
            policy_notes=("OUTCOME_FAILED",),
        )

        records = tuple(
            await self.fixture.repository().load_policy_records(
                self.fixture.query(),
                (candidate,),
                snapshot_time=_snapshot(),
            )
        )

        self.assertEqual(frozenset({_event_id("b")}), records[0].source_event_ids)
        self.assertEqual(_snapshot(40), records[0].transaction_from)

    async def test_temporal_policy_returns_invalidated_fact_version_and_successor(
        self,
    ) -> None:
        candidate = _candidate(
            record_id=_record_id("1"),
            content_hash=_hash("1"),
            channel="temporal",
            event_id=_event_id("d"),
            version_id=_fact_id("d"),
            policy_notes=(
                "SUPERSEDED",
                f"SUPERSEDED_BY:{_fact_id('e')}",
            ),
        )

        records = tuple(
            await self.fixture.repository().load_policy_records(
                self.fixture.query(),
                (candidate,),
                snapshot_time=_snapshot(),
            )
        )

        state = records[0]
        self.assertEqual(_fact_id("d"), state.version_id)
        self.assertEqual(_snapshot(0), state.valid_from)
        self.assertEqual(_snapshot(70), state.valid_to)
        self.assertEqual(_snapshot(50), state.transaction_from)
        self.assertEqual(_snapshot(80), state.transaction_to)
        self.assertEqual(_fact_id("e"), state.superseded_by_version_id)

    async def test_graph_policy_validates_a_complete_active_relationship_path(
        self,
    ) -> None:
        candidate = _candidate(
            record_id=_record_id("2"),
            content_hash=_hash("2"),
            channel="graph",
            event_id=_event_id("f"),
            relation_path=(_relationship_id("9"),),
        )

        records = tuple(
            await self.fixture.repository().load_policy_records(
                self.fixture.query(),
                (candidate,),
                snapshot_time=_snapshot(),
            )
        )

        state = records[0]
        self.assertEqual(frozenset({_event_id("f")}), state.source_event_ids)
        self.assertEqual(_snapshot(60), state.transaction_from)

    async def test_graph_policy_authenticates_record_ref_fact_path(self) -> None:
        self.fixture.connection.execute(
            "UPDATE memory_fact_versions SET subject_record_id=?,"
            "predicate='references',object_kind='record_ref',object_json=?,"
            "valid_to_us=?,retracted_by_event_id=asserted_by_event_id "
            "WHERE fact_version_id=?",
            (
                _record_id("1"),
                json.dumps(_record_id("2")),
                BASE_US + 250,
                _fact_id("e"),
            ),
        )
        self.fixture.connection.execute(
            "UPDATE projection_manifests SET row_count=2 "
            "WHERE workspace_id=? AND projection_name='graph'",
            (WORKSPACE_ID,),
        )
        self.fixture.connection.commit()
        candidate = _candidate(
            record_id=_record_id("2"),
            content_hash=_hash("2"),
            channel="graph",
            event_id=_event_id("f"),
            relation_path=(_fact_id("e"),),
        )

        records = tuple(
            await self.fixture.repository().load_policy_records(
                self.fixture.query(),
                (candidate,),
                snapshot_time=_snapshot(),
            )
        )

        self.assertEqual(_snapshot(70), records[0].valid_from)
        self.assertEqual(_snapshot(250), records[0].valid_to)
        self.assertEqual(_snapshot(80), records[0].transaction_from)

    async def test_active_supersedes_relationship_labels_the_target(self) -> None:
        self.fixture.connection.execute(
            "UPDATE memory_relationship_versions "
            "SET relationship_type='supersedes' "
            "WHERE relationship_version_id=?",
            (_relationship_id("9"),),
        )
        self.fixture.connection.commit()
        candidate = _candidate(
            record_id=_record_id("2"),
            content_hash=_hash("2"),
            channel="lexical",
            event_id=_event_id("f"),
        )

        records = tuple(
            await self.fixture.repository().load_policy_records(
                self.fixture.query(),
                (candidate,),
                snapshot_time=_snapshot(),
            )
        )

        self.assertEqual(_relationship_id("9"), records[0].superseded_by_version_id)

    async def test_active_conflict_is_an_unresolved_contradiction(self) -> None:
        self.fixture.connection.execute(
            "UPDATE memory_relationship_versions "
            "SET relationship_type='conflicts_with' "
            "WHERE relationship_version_id=?",
            (_relationship_id("9"),),
        )
        self.fixture.connection.commit()
        candidate = _candidate(
            record_id=_record_id("1"),
            content_hash=_hash("1"),
            channel="lexical",
            event_id=_event_id("c"),
        )

        records = tuple(
            await self.fixture.repository().load_policy_records(
                self.fixture.query(),
                (candidate,),
                snapshot_time=_snapshot(),
            )
        )

        self.assertTrue(records[0].has_unresolved_contradiction)


class SQLiteRetrievalRepositoryFailClosedTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.fixture = _CanonicalFixture()

    def tearDown(self) -> None:
        self.fixture.close()

    def _mark_lexical_manifest_stale(
        self,
        event_id: str,
        recorded_offset: int,
    ) -> None:
        row = self.fixture.connection.execute(
            "SELECT details_json FROM projection_manifests "
            "WHERE workspace_id=? AND projection_name='lexical' "
            "AND status='active'",
            (WORKSPACE_ID,),
        ).fetchone()
        assert row is not None
        details = json.loads(str(row[0]))
        details["rebuild_required_at_us"] = BASE_US + recorded_offset
        details["rebuild_required_event_id"] = event_id
        self.fixture.connection.execute(
            "UPDATE projection_manifests SET details_json=? "
            "WHERE workspace_id=? AND projection_name='lexical' "
            "AND status='active'",
            (json.dumps(details, sort_keys=True), WORKSPACE_ID),
        )
        self.fixture.connection.commit()

    async def _assert_policy_unavailable(
        self,
        candidate: FusedCandidate,
    ) -> None:
        with self.assertRaises(RetrievalRepositoryError) as raised:
            await self.fixture.repository().load_policy_records(
                self.fixture.query(),
                (candidate,),
                snapshot_time=_snapshot(),
            )
        self.assertEqual("POLICY_STATE_UNAVAILABLE", raised.exception.code)
        self.assertEqual("POLICY_STATE_UNAVAILABLE", str(raised.exception))
        self.assertNotIn(str(self.fixture.database_path), str(raised.exception))
        self.assertNotIn("secret", str(raised.exception).casefold())

    async def test_cross_workspace_record_is_never_resolved(self) -> None:
        await self._assert_policy_unavailable(
            _candidate(
                record_id=_record_id("3"),
                content_hash=_hash("3"),
                channel="lexical",
                event_id=_event_id("8"),
            )
        )

    async def test_in_memory_connection_factory_fails_closed(self) -> None:
        candidate = _candidate(
            record_id=_record_id("1"),
            content_hash=_hash("1"),
            channel="lexical",
            event_id=_event_id("c"),
        )
        repository = SQLiteRetrievalRepository(
            connection_factory=lambda: sqlite3.connect(":memory:")
        )

        with self.assertRaises(RetrievalRepositoryError) as raised:
            await repository.load_policy_records(
                self.fixture.query(),
                (candidate,),
                snapshot_time=_snapshot(),
            )

        self.assertEqual("POLICY_STATE_UNAVAILABLE", raised.exception.code)

    async def test_forged_source_event_is_rejected(self) -> None:
        await self._assert_policy_unavailable(
            _candidate(
                record_id=_record_id("1"),
                content_hash=_hash("1"),
                channel="lexical",
                event_id=_event_id("a"),
            )
        )

    async def test_forged_candidate_hash_is_rejected(self) -> None:
        await self._assert_policy_unavailable(
            _candidate(
                record_id=_record_id("1"),
                content_hash=_hash("0"),
                channel="lexical",
                event_id=_event_id("c"),
            )
        )

    async def test_non_active_generation_is_rejected(self) -> None:
        await self._assert_policy_unavailable(
            _candidate(
                record_id=_record_id("1"),
                content_hash=_hash("1"),
                channel="lexical",
                event_id=_event_id("c"),
                generation=2,
            )
        )

    async def test_forged_projection_hash_is_rejected(self) -> None:
        self.fixture.connection.execute(
            "UPDATE retrieval_documents SET content_hash=? "
            "WHERE workspace_id=? AND record_id=?",
            (_hash("0"), WORKSPACE_ID, _record_id("1")),
        )
        self.fixture.connection.commit()
        await self._assert_policy_unavailable(
            _candidate(
                record_id=_record_id("1"),
                content_hash=_hash("1"),
                channel="lexical",
                event_id=_event_id("c"),
            )
        )

    async def test_forged_projection_tags_are_rejected(self) -> None:
        self.fixture.connection.execute(
            "UPDATE retrieval_documents SET tags_text='forged' "
            "WHERE workspace_id=? AND record_id=?",
            (WORKSPACE_ID, _record_id("1")),
        )
        self.fixture.connection.commit()
        await self._assert_policy_unavailable(
            _candidate(
                record_id=_record_id("1"),
                content_hash=_hash("1"),
                channel="lexical",
                event_id=_event_id("c"),
            )
        )

    async def test_forged_projection_transaction_start_is_rejected(self) -> None:
        self.fixture.connection.execute(
            "UPDATE retrieval_documents SET transaction_from_us=? "
            "WHERE workspace_id=? AND record_id=?",
            (BASE_US + 31, WORKSPACE_ID, _record_id("1")),
        )
        self.fixture.connection.commit()
        await self._assert_policy_unavailable(
            _candidate(
                record_id=_record_id("1"),
                content_hash=_hash("1"),
                channel="lexical",
                event_id=_event_id("c"),
            )
        )

    async def test_forged_projection_valid_end_is_rejected(self) -> None:
        self.fixture.connection.execute(
            "UPDATE retrieval_documents SET valid_to_us=? "
            "WHERE workspace_id=? AND record_id=?",
            (BASE_US + 100, WORKSPACE_ID, _record_id("1")),
        )
        self.fixture.connection.commit()
        await self._assert_policy_unavailable(
            _candidate(
                record_id=_record_id("1"),
                content_hash=_hash("1"),
                channel="lexical",
                event_id=_event_id("c"),
            )
        )

    async def test_forged_projection_transaction_end_is_rejected(self) -> None:
        self.fixture.connection.execute(
            "UPDATE retrieval_documents SET transaction_to_us=? "
            "WHERE workspace_id=? AND record_id=?",
            (BASE_US + 100, WORKSPACE_ID, _record_id("1")),
        )
        self.fixture.connection.commit()
        await self._assert_policy_unavailable(
            _candidate(
                record_id=_record_id("1"),
                content_hash=_hash("1"),
                channel="lexical",
                event_id=_event_id("c"),
            )
        )

    async def test_archive_state_must_match_canonical_record(self) -> None:
        self.fixture.connection.execute(
            "UPDATE retrieval_documents SET archived=1 "
            "WHERE workspace_id=? AND record_id=?",
            (WORKSPACE_ID, _record_id("1")),
        )
        self.fixture.connection.commit()
        await self._assert_policy_unavailable(
            _candidate(
                record_id=_record_id("1"),
                content_hash=_hash("1"),
                channel="lexical",
                event_id=_event_id("c"),
            )
        )

    async def test_append_after_activation_makes_every_manifest_stale(self) -> None:
        self.fixture._insert_event(
            WORKSPACE_ID,
            "6",
            stream_id=_record_id("6"),
            stream_kind="memory",
            stream_version=1,
            recorded_offset=150,
        )
        self.fixture.connection.commit()
        await self._assert_policy_unavailable(
            _candidate(
                record_id=_record_id("1"),
                content_hash=_hash("1"),
                channel="lexical",
                event_id=_event_id("c"),
            )
        )

    async def test_marked_stale_lexical_allows_exact_unchanged_record(
        self,
    ) -> None:
        self.fixture._insert_event(
            WORKSPACE_ID,
            "6",
            stream_id=_record_id("6"),
            stream_kind="memory",
            stream_version=1,
            recorded_offset=150,
        )
        self._mark_lexical_manifest_stale(_event_id("6"), 150)
        candidate = _candidate(
            record_id=_record_id("1"),
            content_hash=_hash("1"),
            channel="lexical",
            event_id=_event_id("c"),
        )

        records = tuple(
            await self.fixture.repository().load_policy_records(
                self.fixture.query(),
                (candidate,),
                snapshot_time=_snapshot(),
            )
        )
        result = apply_retrieval_policy(
            self.fixture.query(),
            (candidate,),
            records,
            snapshot_time=_snapshot(),
        )

        self.assertEqual(
            (_record_id("1"),), tuple(item.record_id for item in result.candidates)
        )
        selected = tuple(
            await self.fixture.repository().load_selected_evidence(
                self.fixture.query(),
                result.candidates,
                snapshot_time=_snapshot(),
            )
        )
        self.assertEqual("Rotate credentials safely.", selected[0].content)

    async def test_marked_stale_lexical_rejects_changed_record(self) -> None:
        self.fixture._insert_event(
            WORKSPACE_ID,
            "6",
            stream_id=_record_id("1"),
            stream_kind="memory",
            stream_version=4,
            recorded_offset=150,
        )
        self.fixture.connection.execute(
            "UPDATE memory_records SET content_hash=?,stream_version=4,"
            "source_event_id=?,updated_at_us=? WHERE workspace_id=? "
            "AND record_id=?",
            (
                _hash("6"),
                _event_id("6"),
                BASE_US + 150,
                WORKSPACE_ID,
                _record_id("1"),
            ),
        )
        self._mark_lexical_manifest_stale(_event_id("6"), 150)

        await self._assert_policy_unavailable(
            _candidate(
                record_id=_record_id("1"),
                content_hash=_hash("1"),
                channel="lexical",
                event_id=_event_id("c"),
            )
        )

    async def test_marked_stale_lexical_rejects_deleted_record(self) -> None:
        self.fixture._insert_event(
            WORKSPACE_ID,
            "6",
            stream_id=_record_id("1"),
            stream_kind="memory",
            stream_version=4,
            recorded_offset=150,
        )
        self.fixture.connection.execute(
            "UPDATE memory_records SET deleted_at_us=?,stream_version=4,"
            "source_event_id=?,updated_at_us=? WHERE workspace_id=? "
            "AND record_id=?",
            (
                BASE_US + 150,
                _event_id("6"),
                BASE_US + 150,
                WORKSPACE_ID,
                _record_id("1"),
            ),
        )
        self._mark_lexical_manifest_stale(_event_id("6"), 150)

        await self._assert_policy_unavailable(
            _candidate(
                record_id=_record_id("1"),
                content_hash=_hash("1"),
                channel="lexical",
                event_id=_event_id("c"),
            )
        )

    async def test_specialized_builder_contract_mismatch_fails_closed(
        self,
    ) -> None:
        row = self.fixture.connection.execute(
            "SELECT details_json FROM projection_manifests "
            "WHERE workspace_id=? AND projection_name='temporal'",
            (WORKSPACE_ID,),
        ).fetchone()
        assert row is not None
        details = json.loads(str(row[0]))
        details["builder_contract_hash"] = _hash("0")
        self.fixture.connection.execute(
            "UPDATE projection_manifests SET details_json=? "
            "WHERE workspace_id=? AND projection_name='temporal'",
            (json.dumps(details, sort_keys=True), WORKSPACE_ID),
        )
        self.fixture.connection.commit()

        await self._assert_policy_unavailable(
            _candidate(
                record_id=_record_id("1"),
                content_hash=_hash("1"),
                channel="temporal",
                event_id=_event_id("e"),
                version_id=_fact_id("e"),
            )
        )

    async def test_unknown_visibility_fails_closed(self) -> None:
        self.fixture.connection.execute(
            "UPDATE memory_records SET context_json=? WHERE record_id=?",
            (json.dumps({"visibility": "secret"}), _record_id("1")),
        )
        self.fixture.connection.commit()
        await self._assert_policy_unavailable(
            _candidate(
                record_id=_record_id("1"),
                content_hash=_hash("1"),
                channel="lexical",
                event_id=_event_id("c"),
            )
        )

    async def test_private_visibility_is_denied_until_authorized(self) -> None:
        self.fixture.connection.execute(
            "UPDATE memory_records SET context_json=? WHERE record_id=?",
            (json.dumps({"visibility": "private"}), _record_id("1")),
        )
        self.fixture.connection.execute(
            "UPDATE retrieval_documents SET visibility='private' "
            "WHERE workspace_id=? AND record_id=?",
            (WORKSPACE_ID, _record_id("1")),
        )
        self.fixture.connection.commit()
        candidate = _candidate(
            record_id=_record_id("1"),
            content_hash=_hash("1"),
            channel="lexical",
            event_id=_event_id("c"),
        )

        denied = tuple(
            await self.fixture.repository().load_policy_records(
                self.fixture.query(),
                (candidate,),
                snapshot_time=_snapshot(),
            )
        )
        allowed = tuple(
            await self.fixture.repository(
                visibility_authorizer=lambda _query, value: value == "private"
            ).load_policy_records(
                self.fixture.query(),
                (candidate,),
                snapshot_time=_snapshot(),
            )
        )

        self.assertFalse(denied[0].visibility_allowed)
        self.assertTrue(allowed[0].visibility_allowed)

    async def test_policy_reads_never_touch_canonical_or_projection_prose(
        self,
    ) -> None:
        worker_threads: list[int] = []
        forbidden_columns = {
            ("memory_events", "payload_json"),
            ("memory_records", "content"),
            ("memory_records", "outcome"),
            ("record_outcome_view", "outcome_text"),
            ("record_procedures", "step_text"),
            ("retrieval_documents", "content"),
            ("retrieval_documents", "rationale"),
        }

        def connection_factory() -> sqlite3.Connection:
            worker_threads.append(threading.get_ident())
            connection = sqlite3.connect(self.fixture.database_path)

            def authorize(
                action: int,
                table: str | None,
                column: str | None,
                _database: str | None,
                _source: str | None,
            ) -> int:
                if (
                    action == sqlite3.SQLITE_READ
                    and (table, column) in forbidden_columns
                ):
                    return sqlite3.SQLITE_DENY
                return sqlite3.SQLITE_OK

            connection.set_authorizer(authorize)
            return connection

        candidate = _candidate(
            record_id=_record_id("1"),
            content_hash=_hash("1"),
            channel="procedure",
            event_id=_event_id("a"),
            policy_notes=(f"PROCEDURE_STEP:0:{_json_hash(PROCEDURE_STEP_0)}",),
        )
        main_thread = threading.get_ident()

        records = tuple(
            await SQLiteRetrievalRepository(
                connection_factory=connection_factory
            ).load_policy_records(
                self.fixture.query(),
                (candidate,),
                snapshot_time=_snapshot(),
            )
        )

        self.assertEqual(1, len(records))
        self.assertTrue(worker_threads)
        self.assertTrue(all(value != main_thread for value in worker_threads))


class SQLiteRetrievalRepositoryEvidenceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.fixture = _CanonicalFixture()

    def tearDown(self) -> None:
        self.fixture.close()

    def _make_record_two_a_near_duplicate(self) -> None:
        self.fixture.connection.execute(
            "UPDATE memory_records SET record_type='warning',content=?,"
            "content_hash=? WHERE record_id=?",
            (
                "Rotate credentials safely.",
                _hash("1"),
                _record_id("2"),
            ),
        )
        self.fixture.connection.execute(
            "UPDATE retrieval_documents SET content=?,category='warning',"
            "content_hash=? WHERE workspace_id=? AND record_id=?",
            (
                "Rotate credentials safely.",
                _hash("1"),
                WORKSPACE_ID,
                _record_id("2"),
            ),
        )
        self.fixture.connection.commit()

    async def _merged_near_duplicate(
        self,
        secondary: FusedCandidate | None = None,
    ) -> tuple[RetrievalQuery, FusedCandidate]:
        self._make_record_two_a_near_duplicate()
        query = self.fixture.query()
        candidates = (
            _candidate(
                record_id=_record_id("1"),
                content_hash=_hash("1"),
                channel="lexical",
                event_id=_event_id("c"),
            ),
            secondary
            or _candidate(
                record_id=_record_id("2"),
                content_hash=_hash("1"),
                channel="lexical",
                event_id=_event_id("f"),
            ),
        )
        policy_records = tuple(
            await self.fixture.repository().load_policy_records(
                query,
                candidates,
                snapshot_time=_snapshot(),
            )
        )
        merged = apply_retrieval_policy(
            query,
            candidates,
            policy_records,
            snapshot_time=_snapshot(),
        ).candidates
        self.assertEqual(1, len(merged))
        self.assertEqual(2, len(merged[0].evidence_refs))
        return query, merged[0]

    async def _assert_selected_unavailable(
        self,
        query: RetrievalQuery,
        candidate: FusedCandidate,
    ) -> None:
        with self.assertRaises(RetrievalRepositoryError) as raised:
            await self.fixture.repository().load_selected_evidence(
                query,
                (candidate,),
                snapshot_time=_snapshot(),
            )
        self.assertEqual("EVIDENCE_CONTENT_UNAVAILABLE", raised.exception.code)
        self.assertEqual("EVIDENCE_CONTENT_UNAVAILABLE", str(raised.exception))

    async def test_selected_evidence_hydrates_only_active_structured_fields(
        self,
    ) -> None:
        self.fixture.connection.execute(
            "UPDATE memory_records SET rationale=? WHERE workspace_id=? "
            "AND record_id=?",
            (
                "Canonical rotation rationale.",
                WORKSPACE_ID,
                _record_id("1"),
            ),
        )
        self.fixture.connection.execute(
            "UPDATE retrieval_documents SET rationale=? WHERE workspace_id=? "
            "AND projection_generation=1 AND record_id=?",
            (
                "Canonical rotation rationale.",
                WORKSPACE_ID,
                _record_id("1"),
            ),
        )
        self.fixture.connection.commit()
        candidate = _fused_main_candidate()

        selected = tuple(
            await self.fixture.repository().load_selected_evidence(
                self.fixture.query(),
                (candidate,),
                snapshot_time=_snapshot(),
            )
        )

        self.assertEqual(1, len(selected))
        evidence = selected[0]
        self.assertEqual(candidate, evidence.candidate)
        self.assertEqual("Rotate credentials safely.", evidence.content)
        self.assertEqual("warning", evidence.category)
        self.assertEqual(
            "Canonical rotation rationale.",
            getattr(evidence, "rationale", None),
        )
        self.assertEqual(
            ("alpha", "sqlite"),
            getattr(evidence, "tags", None),
        )
        self.assertIs(getattr(evidence, "worked", None), False)
        self.assertEqual("The previous rotation failed safely.", evidence.outcome)
        self.assertTrue(evidence.outcome_failed)
        self.assertEqual(
            (
                "Deploy the replacement credential.",
                "Revoke the old credential.",
            ),
            evidence.procedure_steps,
        )

    async def test_unprojected_rationale_never_reaches_selected_evidence(
        self,
    ) -> None:
        fabricated = "UNPROJECTED PRIVATE RATIONALE"
        self.fixture.connection.execute(
            "UPDATE memory_records SET rationale=? WHERE workspace_id=? "
            "AND record_id=?",
            (fabricated, WORKSPACE_ID, _record_id("1")),
        )
        self.fixture.connection.commit()
        candidate = _candidate(
            record_id=_record_id("1"),
            content_hash=_hash("1"),
            channel="lexical",
            event_id=_event_id("c"),
        )

        with self.assertRaises(RetrievalRepositoryError) as raised:
            await self.fixture.repository().load_selected_evidence(
                self.fixture.query(),
                (candidate,),
                snapshot_time=_snapshot(),
            )

        self.assertEqual("EVIDENCE_CONTENT_UNAVAILABLE", raised.exception.code)
        self.assertNotIn(fabricated, str(raised.exception))

    async def test_optional_outcome_is_not_read_past_the_exact_snapshot(self) -> None:
        candidate = _candidate(
            record_id=_record_id("1"),
            content_hash=_hash("1"),
            channel="lexical",
            event_id=_event_id("c"),
        )
        query = self.fixture.query(as_of_transaction_time=_snapshot(35))

        selected = tuple(
            await self.fixture.repository().load_selected_evidence(
                query,
                (candidate,),
                snapshot_time=_snapshot(),
            )
        )

        self.assertIsNone(selected[0].outcome)
        self.assertFalse(selected[0].outcome_failed)
        self.assertIsNone(selected[0].worked)
        self.assertEqual(
            (
                "Deploy the replacement credential.",
                "Revoke the old credential.",
            ),
            selected[0].procedure_steps,
        )

    async def test_selected_invalidated_fact_keeps_successor_provenance(self) -> None:
        candidate = _candidate(
            record_id=_record_id("1"),
            content_hash=_hash("1"),
            channel="temporal",
            event_id=_event_id("d"),
            version_id=_fact_id("d"),
            policy_notes=(f"SUPERSEDED_BY:{_fact_id('e')}",),
        )

        selected = tuple(
            await self.fixture.repository().load_selected_evidence(
                self.fixture.query(),
                (candidate,),
                snapshot_time=_snapshot(),
            )
        )

        self.assertEqual("superseded", selected[0].status)
        self.assertEqual(_fact_id("e"), selected[0].superseded_by_version_id)

    async def test_tampered_procedure_text_never_reaches_selected_evidence(
        self,
    ) -> None:
        fabricated = "Run an untrusted command instead."
        self.fixture.connection.execute(
            "UPDATE record_procedures SET step_text=? "
            "WHERE workspace_id=? AND projection_generation=1 "
            "AND record_id=? AND ordinal=0",
            (fabricated, WORKSPACE_ID, _record_id("1")),
        )
        self.fixture.connection.commit()
        candidate = _candidate(
            record_id=_record_id("1"),
            content_hash=_hash("1"),
            channel="lexical",
            event_id=_event_id("c"),
        )

        with self.assertRaises(RetrievalRepositoryError) as raised:
            await self.fixture.repository().load_selected_evidence(
                self.fixture.query(),
                (candidate,),
                snapshot_time=_snapshot(),
            )

        self.assertEqual("EVIDENCE_CONTENT_UNAVAILABLE", raised.exception.code)
        self.assertNotIn(fabricated, str(raised.exception))

    async def test_near_duplicate_secondary_provenance_is_authenticated(
        self,
    ) -> None:
        retained_content = "Rotate credentials safely."
        query, merged = await self._merged_near_duplicate()

        selected = tuple(
            await self.fixture.repository().load_selected_evidence(
                query,
                (merged,),
                snapshot_time=_snapshot(),
            )
        )

        self.assertEqual(1, len(selected))
        self.assertEqual(retained_content, selected[0].content)
        self.assertEqual(merged, selected[0].candidate)
        self.assertEqual(merged.evidence_refs, selected[0].candidate.evidence_refs)

    async def test_forged_secondary_hash_fails_closed(self) -> None:
        query, merged = await self._merged_near_duplicate()
        secondary = next(
            evidence
            for evidence in merged.evidence_refs
            if evidence.record_id == _record_id("2")
        )
        forged = replace(secondary, content_hash=_hash("0"))
        forged_candidate = replace(
            merged,
            evidence_refs=tuple(
                forged if evidence == secondary else evidence
                for evidence in merged.evidence_refs
            ),
        )

        await self._assert_selected_unavailable(query, forged_candidate)

    async def test_cross_workspace_secondary_ref_fails_closed(self) -> None:
        query, merged = await self._merged_near_duplicate()
        secondary = next(
            evidence
            for evidence in merged.evidence_refs
            if evidence.record_id == _record_id("2")
        )
        foreign = replace(
            secondary,
            record_id=_record_id("3"),
            event_id=_event_id("8"),
        )
        forged_candidate = replace(
            merged,
            evidence_refs=tuple(
                foreign if evidence == secondary else evidence
                for evidence in merged.evidence_refs
            ),
        )

        await self._assert_selected_unavailable(query, forged_candidate)

    async def test_stale_secondary_manifest_generation_fails_closed(
        self,
    ) -> None:
        self._make_record_two_a_near_duplicate()
        retained = _candidate(
            record_id=_record_id("1"),
            content_hash=_hash("1"),
            channel="lexical",
            event_id=_event_id("c"),
        )
        graph_ref = EvidenceRef(
            record_id=_record_id("2"),
            event_id=_event_id("f"),
            content_hash=_hash("1"),
            version_id=None,
            relation_path=(_relationship_id("9"),),
            provider="graph",
        )
        forged_candidate = FusedCandidate(
            evidence=retained.evidence,
            evidence_refs=(retained.evidence, graph_ref),
            score=retained.score,
            channels=frozenset({"graph", "lexical"}),
            channel_ranks=(("graph", 1), ("lexical", 1)),
            manifest_generations=(("graph", 2), ("lexical", 1)),
        )

        await self._assert_selected_unavailable(
            self.fixture.query(),
            forged_candidate,
        )

    async def test_temporal_version_merge_renders_the_superseded_version(
        self,
    ) -> None:
        current = replace(
            _candidate(
                record_id=_record_id("1"),
                content_hash=_hash("1"),
                channel="temporal",
                event_id=_event_id("e"),
                version_id=_fact_id("e"),
            ),
            score=1.0,
            channel_ranks=(("temporal", 1),),
        )
        historical = replace(
            _candidate(
                record_id=_record_id("1"),
                content_hash=_hash("1"),
                channel="temporal",
                event_id=_event_id("d"),
                version_id=_fact_id("d"),
                policy_notes=(
                    "SUPERSEDED",
                    f"SUPERSEDED_BY:{_fact_id('e')}",
                ),
            ),
            score=0.5,
            channel_ranks=(("temporal", 2),),
        )
        query = self.fixture.query()
        policy_records = tuple(
            await self.fixture.repository().load_policy_records(
                query,
                (current, historical),
                snapshot_time=_snapshot(),
            )
        )
        merged = apply_retrieval_policy(
            query,
            (current, historical),
            policy_records,
            snapshot_time=_snapshot(),
        ).candidates
        self.assertEqual(1, len(merged))
        self.assertEqual(_fact_id("d"), merged[0].version_id)

        selected = tuple(
            await self.fixture.repository().load_selected_evidence(
                query,
                merged,
                snapshot_time=_snapshot(),
            )
        )

        self.assertEqual("superseded", selected[0].status)
        self.assertEqual(_fact_id("e"), selected[0].superseded_by_version_id)
        self.assertEqual(
            {_fact_id("d"), _fact_id("e")},
            {ref.version_id for ref in selected[0].candidate.evidence_refs},
        )

    async def test_service_keeps_cross_record_duplicate_provenance(self) -> None:
        from daem0nmcp.retrieval.composer import EvidenceComposer
        from daem0nmcp.retrieval.service import RetrievalService

        self._make_record_two_a_near_duplicate()
        provider_candidates = tuple(
            Candidate(
                evidence=EvidenceRef(
                    record_id=record_id,
                    event_id=event_id,
                    content_hash=_hash("1"),
                    version_id=None,
                    provider="lexical",
                ),
                rank=rank,
                raw_score=float(3 - rank),
                channels=frozenset({"lexical"}),
            )
            for rank, (record_id, event_id) in enumerate(
                (
                    (_record_id("1"), _event_id("c")),
                    (_record_id("2"), _event_id("f")),
                ),
                start=1,
            )
        )
        lexical = ProviderResult(
            provider="lexical",
            candidates=provider_candidates,
            status="ready",
            manifest_generation=1,
        )
        service = RetrievalService(
            providers={"lexical": _StaticProvider(lexical)},
            repository=self.fixture.repository(),
            composer=EvidenceComposer(tokenizer=_WordTokenizer()),
            clock=_FixedClock(),
        )

        result = await service.retrieve(
            self.fixture.query(limit=1, include_invalidated=False)
        )

        self.assertFalse(result.abstained, result.reason)
        self.assertEqual(1, len(result.items))
        self.assertEqual(
            {_record_id("1"), _record_id("2")},
            {ref.record_id for ref in result.items[0].evidence_refs},
        )
        self.assertIn("Rotate credentials safely.", result.items[0].excerpt)

    async def test_service_keeps_retained_primary_identity_first(self) -> None:
        from daem0nmcp.retrieval.composer import EvidenceComposer
        from daem0nmcp.retrieval.service import RetrievalService

        self._make_record_two_a_near_duplicate()
        provider_candidates = tuple(
            Candidate(
                evidence=EvidenceRef(
                    record_id=record_id,
                    event_id=event_id,
                    content_hash=_hash("1"),
                    version_id=None,
                    provider="lexical",
                ),
                rank=rank,
                raw_score=float(3 - rank),
                channels=frozenset({"lexical"}),
            )
            for rank, (record_id, event_id) in enumerate(
                (
                    (_record_id("2"), _event_id("f")),
                    (_record_id("1"), _event_id("c")),
                ),
                start=1,
            )
        )
        lexical = ProviderResult(
            provider="lexical",
            candidates=provider_candidates,
            status="ready",
            manifest_generation=1,
        )
        service = RetrievalService(
            providers={"lexical": _StaticProvider(lexical)},
            repository=self.fixture.repository(),
            composer=EvidenceComposer(tokenizer=_WordTokenizer()),
            clock=_FixedClock(),
        )

        result = await service.retrieve(
            self.fixture.query(limit=1, include_invalidated=False)
        )

        self.assertFalse(result.abstained, result.reason)
        item = result.items[0]
        citation = result.context.citations[0]
        self.assertEqual(_record_id("2"), item.evidence_refs[0].record_id)
        self.assertEqual(item.evidence_refs, citation.evidence_refs)
        self.assertEqual(
            {_record_id("1"), _record_id("2")},
            {ref.record_id for ref in item.evidence_refs},
        )

    async def test_service_candidate_limit_is_per_provider(self) -> None:
        from daem0nmcp.retrieval.composer import EvidenceComposer
        from daem0nmcp.retrieval.service import RetrievalService

        self._make_record_two_a_near_duplicate()
        lexical = ProviderResult(
            provider="lexical",
            candidates=(
                Candidate(
                    evidence=EvidenceRef(
                        record_id=_record_id("2"),
                        event_id=_event_id("f"),
                        content_hash=_hash("1"),
                        version_id=None,
                        provider="lexical",
                    ),
                    rank=1,
                    raw_score=1.0,
                    channels=frozenset({"lexical"}),
                ),
            ),
            status="ready",
            manifest_generation=1,
        )
        outcome = ProviderResult(
            provider="outcome",
            candidates=(
                Candidate(
                    evidence=EvidenceRef(
                        record_id=_record_id("1"),
                        event_id=_event_id("b"),
                        content_hash=_hash("1"),
                        version_id=None,
                        provider="outcome",
                    ),
                    rank=1,
                    raw_score=1.0,
                    channels=frozenset({"outcome"}),
                    policy_notes=("OUTCOME_FAILED",),
                ),
            ),
            status="ready",
            manifest_generation=1,
        )
        service = RetrievalService(
            providers={
                "lexical": _StaticProvider(lexical),
                "outcome": _StaticProvider(outcome),
            },
            repository=self.fixture.repository(),
            composer=EvidenceComposer(tokenizer=_WordTokenizer()),
            clock=_FixedClock(),
        )

        result = await service.retrieve(
            self.fixture.query(
                text="Which outcome failed?",
                limit=1,
                candidate_limit=1,
                include_invalidated=False,
            )
        )

        self.assertFalse(result.abstained, result.reason)
        self.assertEqual(1, len(result.items))
        self.assertEqual(
            {"lexical", "outcome"},
            {ref.provider for ref in result.items[0].evidence_refs},
        )

    async def test_global_fused_candidate_bound_remains_finite(self) -> None:
        candidates = tuple(
            _candidate(
                record_id=_record_id(str(index)),
                content_hash=_hash(str(index)),
                channel="lexical",
                event_id=_event_id(str(index)),
            )
            for index in range(7)
        )

        with self.assertRaises(ValueError):
            await self.fixture.repository().load_policy_records(
                self.fixture.query(limit=1, candidate_limit=1),
                candidates,
                snapshot_time=_snapshot(),
            )

    async def test_service_renders_temporal_history_as_superseded(self) -> None:
        from daem0nmcp.retrieval.composer import EvidenceComposer
        from daem0nmcp.retrieval.service import RetrievalService

        lexical = ProviderResult(
            provider="lexical",
            candidates=(),
            status="ready",
            manifest_generation=1,
        )
        temporal = ProviderResult(
            provider="temporal",
            candidates=(
                Candidate(
                    evidence=EvidenceRef(
                        record_id=_record_id("1"),
                        event_id=_event_id("e"),
                        content_hash=_hash("1"),
                        version_id=_fact_id("e"),
                        provider="temporal",
                    ),
                    rank=1,
                    raw_score=1.0,
                    channels=frozenset({"temporal"}),
                ),
                Candidate(
                    evidence=EvidenceRef(
                        record_id=_record_id("1"),
                        event_id=_event_id("d"),
                        content_hash=_hash("1"),
                        version_id=_fact_id("d"),
                        provider="temporal",
                    ),
                    rank=2,
                    raw_score=0.5,
                    channels=frozenset({"temporal"}),
                    policy_notes=(
                        "SUPERSEDED",
                        f"SUPERSEDED_BY:{_fact_id('e')}",
                    ),
                ),
            ),
            status="ready",
            manifest_generation=1,
        )
        service = RetrievalService(
            providers={
                "lexical": _StaticProvider(lexical),
                "temporal": _StaticProvider(temporal),
            },
            repository=self.fixture.repository(),
            composer=EvidenceComposer(tokenizer=_WordTokenizer()),
            clock=_FixedClock(),
        )

        result = await service.retrieve(
            self.fixture.query(
                text="What was valid before migration?",
                limit=1,
            )
        )

        self.assertFalse(result.abstained)
        self.assertEqual("superseded", result.items[0].status)
        self.assertEqual(_fact_id("e"), result.items[0].superseded_by_version_id)
        self.assertEqual(
            {_fact_id("d"), _fact_id("e")},
            {ref.version_id for ref in result.items[0].evidence_refs},
        )

    async def test_secondary_procedure_ref_is_authenticated_without_secondary_text(
        self,
    ) -> None:
        secondary_step = "Do not hydrate this secondary procedure."
        self.fixture.connection.execute(
            "UPDATE memory_records SET record_type='warning',content=?,"
            "content_hash=? WHERE record_id=?",
            (
                "Rotate credentials safely.",
                _hash("1"),
                _record_id("2"),
            ),
        )
        self.fixture.connection.execute(
            "UPDATE retrieval_documents SET content=?,category='warning',"
            "content_hash=? WHERE workspace_id=? AND record_id=?",
            (
                "Rotate credentials safely.",
                _hash("1"),
                WORKSPACE_ID,
                _record_id("2"),
            ),
        )
        self.fixture.connection.execute(
            "INSERT INTO record_procedures VALUES (?,?,?,?,?,?,?)",
            (
                WORKSPACE_ID,
                1,
                _record_id("2"),
                0,
                secondary_step,
                _json_hash(secondary_step),
                _event_id("f"),
            ),
        )
        self.fixture.connection.execute(
            "UPDATE projection_manifests SET row_count=3 "
            "WHERE workspace_id=? AND projection_name='procedure'",
            (WORKSPACE_ID,),
        )
        self.fixture.connection.commit()
        query = self.fixture.query()
        candidates = (
            _candidate(
                record_id=_record_id("1"),
                content_hash=_hash("1"),
                channel="lexical",
                event_id=_event_id("c"),
            ),
            _candidate(
                record_id=_record_id("2"),
                content_hash=_hash("1"),
                channel="procedure",
                event_id=_event_id("f"),
                policy_notes=(f"PROCEDURE_STEP:0:{_json_hash(secondary_step)}",),
            ),
        )
        policy_records = tuple(
            await self.fixture.repository().load_policy_records(
                query,
                candidates,
                snapshot_time=_snapshot(),
            )
        )
        merged = apply_retrieval_policy(
            query,
            candidates,
            policy_records,
            snapshot_time=_snapshot(),
        ).candidates
        self.assertEqual(1, len(merged))
        self.assertEqual(2, len(merged[0].evidence_refs))

        selected = tuple(
            await self.fixture.repository().load_selected_evidence(
                query,
                merged,
                snapshot_time=_snapshot(),
            )
        )

        self.assertEqual("Rotate credentials safely.", selected[0].content)
        self.assertNotIn(secondary_step, selected[0].procedure_steps)
        self.assertEqual(merged[0].evidence_refs, selected[0].candidate.evidence_refs)
