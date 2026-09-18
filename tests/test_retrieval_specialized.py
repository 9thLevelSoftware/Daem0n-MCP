"""Specialized v7 retrieval providers over rebuildable SQLite projections."""

from __future__ import annotations

import asyncio
import json
import sqlite3
import tempfile
import threading
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path

from daem0nmcp.retrieval.specialized_contract import (
    SPECIALIZED_PROJECTIONS,
    specialized_projection_contract,
)

WORKSPACE_ID = "ws_0123456789abcdef01234567"
OTHER_WORKSPACE_ID = "ws_76543210fedcba9876543210"
ROOT_HASH = "a" * 64
OTHER_ROOT_HASH = "b" * 64
T0_US = 1_767_225_600_000_000


def _record_id(digit: str) -> str:
    return "mem_" + digit * 64


def _event_id(digit: str) -> str:
    return "evt_" + digit * 64


def _fact_id(digit: str) -> str:
    return "fact_" + digit * 64


def _relation_id(digit: str) -> str:
    return "rel_" + digit * 64


def _hash(digit: str) -> str:
    return digit * 64


def _at(microseconds: int) -> datetime:
    return datetime.fromtimestamp(microseconds / 1_000_000, timezone.utc)


def _seed(
    digit: str,
    provider: str = "lexical",
    *,
    generation: int = 7,
    content_digit: str | None = None,
    event_digit: str | None = None,
):
    from daem0nmcp.retrieval.types import EvidenceRef, FusedCandidate

    evidence = EvidenceRef(
        record_id=_record_id(digit),
        event_id=_event_id(event_digit or digit),
        content_hash=_hash(content_digit or digit),
        version_id=None,
        provider=provider,
    )
    return FusedCandidate(
        evidence=evidence,
        evidence_refs=(evidence,),
        score=1.0,
        channels=frozenset({provider}),
        channel_ranks=((provider, 1),),
        manifest_generations=((provider, generation),),
    )


class _SpecializedDatabaseTestCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory()
        self.database_path = Path(self._temporary.name) / "retrieval.sqlite3"
        self.connection = sqlite3.connect(self.database_path)
        self.connection.executescript(
            """
            CREATE TABLE projection_manifests (
                workspace_id TEXT NOT NULL,
                projection_name TEXT NOT NULL,
                generation INTEGER NOT NULL,
                status TEXT NOT NULL,
                source_event_count INTEGER NOT NULL,
                source_event_root_hash TEXT NOT NULL,
                row_count INTEGER NOT NULL,
                details_json TEXT NOT NULL DEFAULT '{}',
                PRIMARY KEY(workspace_id, projection_name, generation)
            );
            CREATE TABLE retrieval_documents (
                workspace_id TEXT NOT NULL,
                projection_generation INTEGER NOT NULL,
                record_id TEXT NOT NULL,
                content_hash TEXT NOT NULL,
                source_event_id TEXT NOT NULL,
                transaction_from_us INTEGER NOT NULL,
                PRIMARY KEY(workspace_id, projection_generation, record_id)
            );
            CREATE TABLE memory_fact_versions (
                fact_version_id TEXT PRIMARY KEY,
                fact_id TEXT NOT NULL,
                workspace_id TEXT NOT NULL,
                version INTEGER NOT NULL,
                subject_record_id TEXT,
                predicate TEXT NOT NULL,
                object_json TEXT NOT NULL,
                content_hash TEXT NOT NULL,
                valid_from_us INTEGER NOT NULL,
                valid_to_us INTEGER,
                transaction_from_us INTEGER NOT NULL,
                transaction_to_us INTEGER,
                asserted_by_event_id TEXT NOT NULL,
                retracted_by_event_id TEXT
            );
            CREATE TABLE memory_relationship_versions (
                relationship_version_id TEXT PRIMARY KEY,
                relationship_id TEXT NOT NULL,
                workspace_id TEXT NOT NULL,
                version INTEGER NOT NULL,
                source_record_id TEXT NOT NULL,
                target_record_id TEXT NOT NULL,
                relationship_type TEXT NOT NULL,
                content_hash TEXT NOT NULL,
                valid_from_us INTEGER NOT NULL,
                valid_to_us INTEGER,
                transaction_from_us INTEGER NOT NULL,
                transaction_to_us INTEGER,
                asserted_by_event_id TEXT NOT NULL,
                retracted_by_event_id TEXT
            );
            CREATE TABLE record_procedures (
                workspace_id TEXT NOT NULL,
                projection_generation INTEGER NOT NULL,
                record_id TEXT NOT NULL,
                ordinal INTEGER NOT NULL,
                step_text TEXT NOT NULL,
                step_hash TEXT NOT NULL,
                source_event_id TEXT NOT NULL,
                PRIMARY KEY(workspace_id, projection_generation, record_id, ordinal)
            );
            CREATE TABLE record_outcome_view (
                workspace_id TEXT NOT NULL,
                projection_generation INTEGER NOT NULL,
                record_id TEXT NOT NULL,
                worked INTEGER,
                outcome_text TEXT,
                outcome_event_id TEXT NOT NULL,
                transaction_at_us INTEGER NOT NULL,
                PRIMARY KEY(workspace_id, projection_generation, record_id)
            );
            """
        )
        self._manifest("lexical", generation=7, row_count=0)
        self.connection.commit()

    def tearDown(self) -> None:
        self.connection.close()
        self._temporary.cleanup()

    def _manifest(
        self,
        projection: str,
        *,
        generation: int,
        row_count: int,
        workspace_id: str = WORKSPACE_ID,
        root_hash: str = ROOT_HASH,
        status: str = "active",
        event_count: int = 20,
        details_json: str | None = None,
    ) -> None:
        if details_json is None:
            details: dict[str, object] = {}
            if projection in SPECIALIZED_PROJECTIONS:
                details = specialized_projection_contract(
                    workspace_id,
                    projection,
                    generation,
                    _hash("f"),
                )[2]
            details_json = json.dumps(
                details,
                separators=(",", ":"),
                sort_keys=True,
            )
        self.connection.execute(
            "INSERT INTO projection_manifests VALUES (?,?,?,?,?,?,?,?)",
            (
                workspace_id,
                projection,
                generation,
                status,
                event_count,
                root_hash,
                row_count,
                details_json,
            ),
        )

    def _document(
        self,
        digit: str,
        *,
        workspace_id: str = WORKSPACE_ID,
        generation: int = 7,
        transaction_from_us: int = T0_US,
    ) -> None:
        self.connection.execute(
            "INSERT INTO retrieval_documents VALUES (?,?,?,?,?,?)",
            (
                workspace_id,
                generation,
                _record_id(digit),
                _hash(digit),
                _event_id(digit),
                transaction_from_us,
            ),
        )

    def _tamper_manifest_detail(
        self,
        projection: str,
        key: str,
        value: object,
    ) -> None:
        row = self.connection.execute(
            "SELECT details_json FROM projection_manifests "
            "WHERE workspace_id=? AND projection_name=? AND status='active'",
            (WORKSPACE_ID, projection),
        ).fetchone()
        assert row is not None
        details = json.loads(str(row[0]))
        details[key] = value
        self.connection.execute(
            "UPDATE projection_manifests SET details_json=? "
            "WHERE workspace_id=? AND projection_name=? AND status='active'",
            (
                json.dumps(details, separators=(",", ":"), sort_keys=True),
                WORKSPACE_ID,
                projection,
            ),
        )
        self.connection.commit()

    def _query(self, **changes):
        from daem0nmcp.retrieval.types import RetrievalQuery

        values = {
            "workspace_id": WORKSPACE_ID,
            "text": "production endpoint",
            "limit": 10,
            "candidate_limit": 20,
        }
        values.update(changes)
        return RetrievalQuery(**values)


class TemporalProviderTests(_SpecializedDatabaseTestCase):
    def setUp(self) -> None:
        super().setUp()
        self._manifest("temporal", generation=3, row_count=3)
        self._document("1")
        self._document("2")
        self.connection.executemany(
            """
            INSERT INTO memory_fact_versions VALUES (
                ?,?,?,?,?,?,?,?,?,?,?,?,?,?
            )
            """,
            (
                (
                    _fact_id("1"),
                    _fact_id("a"),
                    WORKSPACE_ID,
                    1,
                    _record_id("1"),
                    "production_api_endpoint",
                    '"old.internal"',
                    _hash("a"),
                    T0_US - 1_000,
                    None,
                    T0_US + 100,
                    T0_US + 200,
                    _event_id("a"),
                    _event_id("b"),
                ),
                (
                    _fact_id("2"),
                    _fact_id("a"),
                    WORKSPACE_ID,
                    2,
                    _record_id("1"),
                    "production_api_endpoint",
                    '"old.internal"',
                    _hash("b"),
                    T0_US - 1_000,
                    T0_US + 500,
                    T0_US + 200,
                    None,
                    _event_id("b"),
                    _event_id("b"),
                ),
                (
                    _fact_id("3"),
                    _fact_id("c"),
                    WORKSPACE_ID,
                    1,
                    _record_id("2"),
                    "production_api_endpoint",
                    '"new.internal"',
                    _hash("c"),
                    T0_US + 500,
                    None,
                    T0_US + 200,
                    None,
                    _event_id("c"),
                    None,
                ),
            ),
        )
        self.connection.commit()

    async def test_honors_valid_and_transaction_time_independently(self) -> None:
        from daem0nmcp.retrieval.specialized import TemporalProvider

        provider = TemporalProvider(self.connection)
        before_retraction = await provider.search(
            self._query(
                as_of_valid_time=_at(T0_US + 600),
                as_of_transaction_time=_at(T0_US + 150),
            ),
            10,
        )
        after_retraction = await provider.search(
            self._query(
                as_of_valid_time=_at(T0_US + 300),
                as_of_transaction_time=_at(T0_US + 300),
            ),
            10,
        )

        self.assertEqual(
            (_record_id("1"),),
            tuple(item.evidence.record_id for item in before_retraction.candidates),
        )
        self.assertEqual(
            _fact_id("1"),
            before_retraction.candidates[0].evidence.version_id,
        )
        self.assertEqual(
            (_record_id("1"),),
            tuple(item.evidence.record_id for item in after_retraction.candidates),
        )
        self.assertEqual(
            _fact_id("2"),
            after_retraction.candidates[0].evidence.version_id,
        )

    async def test_invalidated_facts_are_excluded_or_explicitly_labeled(self) -> None:
        from daem0nmcp.retrieval.specialized import TemporalProvider

        provider = TemporalProvider(self.connection)
        query_values = {
            "as_of_valid_time": _at(T0_US + 600),
            "as_of_transaction_time": _at(T0_US + 300),
        }
        excluded = await provider.search(self._query(**query_values), 10)
        included = await provider.search(
            self._query(include_invalidated=True, **query_values), 10
        )

        self.assertEqual(
            (_record_id("2"),),
            tuple(item.evidence.record_id for item in excluded.candidates),
        )
        by_record = {item.evidence.record_id: item for item in included.candidates}
        superseded = by_record[_record_id("1")]
        self.assertEqual(_fact_id("1"), superseded.evidence.version_id)
        self.assertIn("SUPERSEDED", superseded.policy_notes)
        self.assertIn(f"SUPERSEDED_BY:{_fact_id('2')}", superseded.policy_notes)

    async def test_invalidation_opt_in_includes_closed_transaction_history(
        self,
    ) -> None:
        from daem0nmcp.retrieval.specialized import TemporalProvider

        result = await TemporalProvider(self.connection).search(
            self._query(
                include_invalidated=True,
                as_of_valid_time=_at(T0_US + 300),
                as_of_transaction_time=_at(T0_US + 300),
            ),
            10,
        )

        by_version = {item.evidence.version_id: item for item in result.candidates}
        self.assertIn(_fact_id("1"), by_version)
        self.assertIn(_fact_id("2"), by_version)
        closed = by_version[_fact_id("1")]
        self.assertIn(f"SUPERSEDED_BY:{_fact_id('2')}", closed.policy_notes)
        self.assertNotIn("SUPERSEDED", by_version[_fact_id("2")].policy_notes)

    async def test_implicit_snapshot_is_captured_once_for_both_axes(self) -> None:
        from daem0nmcp.retrieval.specialized import TemporalProvider

        calls = 0

        def one_snapshot() -> int:
            nonlocal calls
            calls += 1
            if calls > 1:
                raise RuntimeError("snapshot was recaptured")
            return T0_US + 300

        result = await TemporalProvider(self.connection, clock_us=one_snapshot).search(
            self._query(), 10
        )

        self.assertEqual("ready", result.status)
        self.assertEqual(
            (_record_id("1"),),
            tuple(item.evidence.record_id for item in result.candidates),
        )

    async def test_as_of_queries_supply_candidates_without_text_overlap(
        self,
    ) -> None:
        from daem0nmcp.retrieval.specialized import TemporalProvider

        result = await TemporalProvider(self.connection).search(
            self._query(
                text="no matching terms",
                as_of_valid_time=_at(T0_US + 300),
                as_of_transaction_time=_at(T0_US + 300),
            ),
            10,
        )

        self.assertEqual(
            (_record_id("1"),),
            tuple(item.evidence.record_id for item in result.candidates),
        )

    async def test_candidate_uses_active_content_authority_without_text(self) -> None:
        from daem0nmcp.retrieval.specialized import TemporalProvider

        result = await TemporalProvider(self.connection).search(
            self._query(
                as_of_valid_time=_at(T0_US + 300),
                as_of_transaction_time=_at(T0_US + 300),
            ),
            10,
        )

        candidate = result.candidates[0]
        self.assertEqual(_hash("1"), candidate.evidence.content_hash)
        self.assertEqual(_event_id("b"), candidate.evidence.event_id)
        self.assertEqual((), candidate.highlights)
        self.assertNotIn("old.internal", repr(candidate))
        self.assertEqual(3, result.manifest_generation)

    async def test_stale_or_non_active_manifest_fails_closed(self) -> None:
        from daem0nmcp.retrieval.specialized import TemporalProvider

        self.connection.execute(
            "UPDATE projection_manifests SET source_event_root_hash=? "
            "WHERE projection_name='temporal'",
            (OTHER_ROOT_HASH,),
        )
        self.connection.commit()
        stale = await TemporalProvider(self.connection).search(self._query(), 10)

        self.assertEqual("unavailable", stale.status)
        self.assertEqual("TEMPORAL_STALE", stale.reason)
        self.assertEqual((), stale.candidates)

    async def test_builder_contract_mismatch_is_stale(self) -> None:
        from daem0nmcp.retrieval.specialized import TemporalProvider

        self._tamper_manifest_detail("temporal", "builder_contract_hash", _hash("0"))

        result = await TemporalProvider(self.connection).search(self._query(), 10)

        self.assertEqual("unavailable", result.status)
        self.assertEqual("TEMPORAL_STALE", result.reason)
        self.assertEqual((), result.candidates)

    async def test_event_store_rebuild_marker_makes_active_manifest_stale(
        self,
    ) -> None:
        from daem0nmcp.retrieval.specialized import TemporalProvider

        self.connection.execute(
            "UPDATE projection_manifests SET details_json=? "
            "WHERE projection_name='temporal'",
            (
                json.dumps(
                    {
                        "rebuild_required_at_us": T0_US + 900,
                        "rebuild_required_event_id": _event_id("f"),
                    },
                    separators=(",", ":"),
                    sort_keys=True,
                ),
            ),
        )
        self.connection.commit()
        result = await TemporalProvider(self.connection).search(self._query(), 10)

        self.assertEqual("unavailable", result.status)
        self.assertEqual("TEMPORAL_STALE", result.reason)
        self.assertEqual((), result.candidates)


class ProcedureProviderTests(_SpecializedDatabaseTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.fts_table = "retrieval_procedure_fts_0123456789abcdef01234567_g4"
        self.connection.execute(
            f'CREATE VIRTUAL TABLE "{self.fts_table}" USING fts5('
            "record_id UNINDEXED,ordinal UNINDEXED,step_hash UNINDEXED,"
            "source_event_id UNINDEXED,step_text,"
            "tokenize='unicode61 remove_diacritics 2')"
        )
        self._manifest(
            "procedure",
            generation=4,
            row_count=3,
        )
        for digit in ("3", "4", "5"):
            self._document(digit)
        self.connection.executemany(
            "INSERT INTO record_procedures VALUES (?,?,?,?,?,?,?)",
            (
                (
                    WORKSPACE_ID,
                    4,
                    _record_id("3"),
                    0,
                    "Deploy the new credential.",
                    _hash("d"),
                    _event_id("d"),
                ),
                (
                    WORKSPACE_ID,
                    4,
                    _record_id("3"),
                    1,
                    "Revoke the old credential.",
                    _hash("e"),
                    _event_id("d"),
                ),
                (
                    WORKSPACE_ID,
                    4,
                    _record_id("4"),
                    0,
                    "Back up the database.",
                    _hash("f"),
                    _event_id("f"),
                ),
                (
                    WORKSPACE_ID,
                    2,
                    _record_id("5"),
                    0,
                    "Deploy an invented prose-only instruction.",
                    _hash("1"),
                    _event_id("1"),
                ),
            ),
        )
        self.connection.executemany(
            f'INSERT INTO "{self.fts_table}" VALUES (?,?,?,?,?)',
            (
                (
                    _record_id("3"),
                    0,
                    _hash("d"),
                    _event_id("d"),
                    "Deploy the new credential.",
                ),
                (
                    _record_id("3"),
                    1,
                    _hash("e"),
                    _event_id("d"),
                    "Revoke the old credential.",
                ),
                (
                    _record_id("4"),
                    0,
                    _hash("f"),
                    _event_id("f"),
                    "Back up the database.",
                ),
            ),
        )
        self.connection.commit()

    async def test_searches_only_active_structured_steps(self) -> None:
        from daem0nmcp.retrieval.specialized import ProcedureProvider

        result = await ProcedureProvider(self.connection).search(
            self._query(text="how deploy credential"), 10
        )

        self.assertEqual(
            (_record_id("3"),),
            tuple(item.evidence.record_id for item in result.candidates),
        )
        candidate = result.candidates[0]
        self.assertIn(f"PROCEDURE_STEP:0:{_hash('d')}", candidate.policy_notes)
        self.assertNotIn(
            _record_id("5"), {item.evidence.record_id for item in result.candidates}
        )
        self.assertEqual(4, result.manifest_generation)

    async def test_emits_step_identity_but_never_step_text(self) -> None:
        from daem0nmcp.retrieval.specialized import ProcedureProvider

        result = await ProcedureProvider(self.connection).search(
            self._query(text="credential"), 10
        )

        candidate = result.candidates[0]
        self.assertEqual(_hash("3"), candidate.evidence.content_hash)
        self.assertEqual(_event_id("d"), candidate.evidence.event_id)
        self.assertEqual((), candidate.highlights)
        self.assertNotIn("Deploy the new credential", repr(candidate))
        self.assertEqual(
            (
                f"PROCEDURE_STEP:0:{_hash('d')}",
                f"PROCEDURE_STEP:1:{_hash('e')}",
            ),
            candidate.policy_notes,
        )

    async def test_unstructured_or_unmatched_records_are_not_invented(self) -> None:
        from daem0nmcp.retrieval.specialized import ProcedureProvider

        result = await ProcedureProvider(self.connection).search(
            self._query(text="an imaginary unstructured workflow"), 10
        )

        self.assertEqual("ready", result.status)
        self.assertEqual((), result.candidates)

    async def test_unicode_fts_terms_are_preserved(self) -> None:
        from daem0nmcp.retrieval.specialized import ProcedureProvider

        self.connection.execute(
            "INSERT INTO record_procedures VALUES (?,?,?,?,?,?,?)",
            (
                WORKSPACE_ID,
                4,
                _record_id("5"),
                0,
                "Déployer le secret.",
                _hash("1"),
                _event_id("1"),
            ),
        )
        self.connection.execute(
            f'INSERT INTO "{self.fts_table}" VALUES (?,?,?,?,?)',
            (
                _record_id("5"),
                0,
                _hash("1"),
                _event_id("1"),
                "Déployer le secret.",
            ),
        )
        self.connection.execute(
            "UPDATE projection_manifests SET row_count=4 "
            "WHERE workspace_id=? AND projection_name='procedure'",
            (WORKSPACE_ID,),
        )
        self.connection.commit()
        result = await ProcedureProvider(self.connection).search(
            self._query(text="déployer"), 10
        )

        self.assertEqual(
            (_record_id("5"),),
            tuple(item.evidence.record_id for item in result.candidates),
        )

    async def test_manifest_row_count_mismatch_is_stale(self) -> None:
        from daem0nmcp.retrieval.specialized import ProcedureProvider

        self.connection.execute(
            "DELETE FROM record_procedures WHERE workspace_id=? "
            "AND projection_generation=4",
            (WORKSPACE_ID,),
        )
        self.connection.commit()
        result = await ProcedureProvider(self.connection).search(
            self._query(text="credential"), 10
        )

        self.assertEqual("unavailable", result.status)
        self.assertEqual("PROCEDURE_STALE", result.reason)
        self.assertEqual((), result.candidates)

    async def test_builder_contract_mismatch_is_stale(self) -> None:
        from daem0nmcp.retrieval.specialized import ProcedureProvider

        self._tamper_manifest_detail("procedure", "builder_contract_hash", _hash("0"))

        result = await ProcedureProvider(self.connection).search(
            self._query(text="credential"), 10
        )

        self.assertEqual("unavailable", result.status)
        self.assertEqual("PROCEDURE_STALE", result.reason)
        self.assertEqual((), result.candidates)

    async def test_fts_partition_tamper_is_stale_not_instr_fallback(self) -> None:
        from daem0nmcp.retrieval.specialized import ProcedureProvider

        self.connection.execute(
            f'DELETE FROM "{self.fts_table}" WHERE record_id=?',
            (_record_id("3"),),
        )
        self.connection.commit()
        result = await ProcedureProvider(self.connection).search(
            self._query(text="deploy credential"), 10
        )

        self.assertEqual("unavailable", result.status)
        self.assertEqual("PROCEDURE_STALE", result.reason)
        self.assertEqual((), result.candidates)

    async def test_same_projection_in_another_workspace_never_leaks(self) -> None:
        from daem0nmcp.retrieval.specialized import ProcedureProvider

        self._manifest(
            "lexical",
            generation=7,
            row_count=1,
            workspace_id=OTHER_WORKSPACE_ID,
        )
        self._manifest(
            "procedure",
            generation=4,
            row_count=1,
            workspace_id=OTHER_WORKSPACE_ID,
        )
        other_fts_table = "retrieval_procedure_fts_76543210fedcba9876543210_g4"
        self.connection.execute(
            f'CREATE VIRTUAL TABLE "{other_fts_table}" USING fts5('
            "record_id UNINDEXED,ordinal UNINDEXED,step_hash UNINDEXED,"
            "source_event_id UNINDEXED,step_text,"
            "tokenize='unicode61 remove_diacritics 2')"
        )
        self._document("6", workspace_id=OTHER_WORKSPACE_ID)
        self.connection.execute(
            "INSERT INTO record_procedures VALUES (?,?,?,?,?,?,?)",
            (
                OTHER_WORKSPACE_ID,
                4,
                _record_id("6"),
                0,
                "Deploy credential from another workspace.",
                _hash("6"),
                _event_id("6"),
            ),
        )
        self.connection.execute(
            f'INSERT INTO "{other_fts_table}" VALUES (?,?,?,?,?)',
            (
                _record_id("6"),
                0,
                _hash("6"),
                _event_id("6"),
                "Deploy credential from another workspace.",
            ),
        )
        self.connection.commit()
        result = await ProcedureProvider(self.connection).search(
            self._query(text="deploy credential"), 10
        )

        self.assertEqual(
            (_record_id("3"),),
            tuple(item.evidence.record_id for item in result.candidates),
        )


class OutcomeProviderTests(_SpecializedDatabaseTestCase):
    def setUp(self) -> None:
        super().setUp()
        self._manifest("outcome", generation=5, row_count=2)
        self._document("6")
        self._document("7")
        self.connection.executemany(
            "INSERT INTO record_outcome_view VALUES (?,?,?,?,?,?,?)",
            (
                (
                    WORKSPACE_ID,
                    5,
                    _record_id("6"),
                    1,
                    "The deployment completed successfully.",
                    _event_id("6"),
                    T0_US + 600,
                ),
                (
                    WORKSPACE_ID,
                    5,
                    _record_id("7"),
                    0,
                    "The fallback leaked archived records.",
                    _event_id("7"),
                    T0_US + 500,
                ),
            ),
        )
        self.connection.commit()

    async def test_latest_view_emits_failure_metadata_without_boost(
        self,
    ) -> None:
        from daem0nmcp.retrieval.specialized import OutcomeProvider

        result = await OutcomeProvider(self.connection).search(
            self._query(text="what outcomes worked or failed"), 10
        )

        self.assertEqual(
            (_record_id("6"), _record_id("7")),
            tuple(item.evidence.record_id for item in result.candidates),
        )
        successful, failed = result.candidates
        self.assertEqual(("OUTCOME_SUCCEEDED",), successful.policy_notes)
        self.assertEqual(("OUTCOME_FAILED",), failed.policy_notes)
        self.assertIsNone(successful.raw_score)
        self.assertIsNone(failed.raw_score)
        self.assertEqual(5, result.manifest_generation)

    async def test_emits_outcome_event_without_outcome_text(self) -> None:
        from daem0nmcp.retrieval.specialized import OutcomeProvider

        result = await OutcomeProvider(self.connection).search(
            self._query(text="failed"), 10
        )

        by_record = {item.evidence.record_id: item for item in result.candidates}
        failed = by_record[_record_id("7")]
        self.assertEqual(_event_id("7"), failed.evidence.event_id)
        self.assertEqual(_hash("7"), failed.evidence.content_hash)
        self.assertEqual((), failed.highlights)
        self.assertNotIn("fallback leaked", repr(failed))

    async def test_text_search_always_unions_failed_warning_records(self) -> None:
        from daem0nmcp.retrieval.specialized import OutcomeProvider

        result = await OutcomeProvider(self.connection).search(
            self._query(text="term absent from every successful outcome"), 10
        )

        self.assertEqual(
            (_record_id("7"),),
            tuple(item.evidence.record_id for item in result.candidates),
        )
        self.assertEqual(("OUTCOME_FAILED",), result.candidates[0].policy_notes)
        self.assertIsNone(result.candidates[0].raw_score)

    async def test_outcomes_recorded_after_transaction_snapshot_are_excluded(
        self,
    ) -> None:
        from daem0nmcp.retrieval.specialized import OutcomeProvider

        result = await OutcomeProvider(self.connection).search(
            self._query(
                text="outcome",
                as_of_transaction_time=_at(T0_US + 550),
            ),
            10,
        )

        self.assertEqual(
            (_record_id("7"),),
            tuple(item.evidence.record_id for item in result.candidates),
        )

    async def test_missing_active_projection_is_unavailable(self) -> None:
        from daem0nmcp.retrieval.specialized import OutcomeProvider

        self.connection.execute(
            "UPDATE projection_manifests SET status='rebuild_required' "
            "WHERE projection_name='outcome'"
        )
        self.connection.commit()
        result = await OutcomeProvider(self.connection).search(
            self._query(text="outcome"), 10
        )

        self.assertEqual("unavailable", result.status)
        self.assertEqual("OUTCOME_UNAVAILABLE", result.reason)
        self.assertEqual((), result.candidates)

    async def test_builder_contract_mismatch_is_stale(self) -> None:
        from daem0nmcp.retrieval.specialized import OutcomeProvider

        self._tamper_manifest_detail("outcome", "builder_contract_hash", _hash("0"))

        result = await OutcomeProvider(self.connection).search(
            self._query(text="outcome"), 10
        )

        self.assertEqual("unavailable", result.status)
        self.assertEqual("OUTCOME_STALE", result.reason)
        self.assertEqual((), result.candidates)


class GraphProviderTests(_SpecializedDatabaseTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.connection.execute(
            "ALTER TABLE memory_fact_versions ADD COLUMN object_kind TEXT "
            "NOT NULL DEFAULT 'text'"
        )
        self._manifest("graph", generation=6, row_count=6)
        for digit in ("8", "9", "a", "b", "c", "d", "e"):
            self._document(digit)
        self.connection.executemany(
            """
            INSERT INTO memory_relationship_versions VALUES (
                ?,?,?,?,?,?,?,?,?,?,?,?,?,?
            )
            """,
            (
                self._edge("1", "8", "9"),
                self._edge("2", "8", "a"),
                self._edge("3", "8", "b"),
                self._edge("4", "9", "c"),
                self._edge("5", "a", "d", valid_to_us=T0_US + 400),
                self._edge("6", "d", "e", transaction_to_us=T0_US + 400),
            ),
        )
        self.connection.commit()

    @staticmethod
    def _edge(
        relation_digit: str,
        source_digit: str,
        target_digit: str,
        *,
        workspace_id: str = WORKSPACE_ID,
        valid_to_us: int | None = None,
        transaction_to_us: int | None = None,
    ) -> tuple[object, ...]:
        return (
            _relation_id(relation_digit),
            _relation_id(relation_digit),
            workspace_id,
            1,
            _record_id(source_digit),
            _record_id(target_digit),
            "related_to",
            _hash(relation_digit),
            T0_US,
            valid_to_us,
            T0_US,
            transaction_to_us,
            _event_id(relation_digit),
            None,
        )

    def _graph_query(self):
        return self._query(
            text="why are these records related",
            as_of_valid_time=_at(T0_US + 500),
            as_of_transaction_time=_at(T0_US + 500),
        )

    async def test_requires_explicit_lexical_or_dense_fused_seeds(self) -> None:
        from daem0nmcp.retrieval.specialized import GraphProvider

        provider = GraphProvider(self.connection)
        with self.assertRaises(TypeError):
            await provider.search(self._graph_query(), 10)
        with self.assertRaises(ValueError):
            await provider.search(self._graph_query(), 10, seeds=())
        with self.assertRaises(ValueError):
            await provider.search(self._graph_query(), 10, seeds=(_seed("8", "graph"),))

    async def test_traversal_is_seeded_bounded_and_carries_complete_paths(self) -> None:
        from daem0nmcp.retrieval.specialized import GraphProvider

        result = await GraphProvider(
            self.connection, max_depth=2, max_branching=2
        ).search(self._graph_query(), 10, seeds=(_seed("8"),))

        self.assertEqual(
            (_record_id("9"), _record_id("a"), _record_id("c")),
            tuple(item.evidence.record_id for item in result.candidates),
        )
        paths = {
            item.evidence.record_id: item.evidence.relation_path
            for item in result.candidates
        }
        self.assertEqual((_relation_id("1"),), paths[_record_id("9")])
        self.assertEqual((_relation_id("2"),), paths[_record_id("a")])
        self.assertEqual(
            (_relation_id("1"), _relation_id("4")),
            paths[_record_id("c")],
        )
        self.assertNotIn(_record_id("e"), paths)
        self.assertEqual(6, result.manifest_generation)

    async def test_traverses_record_ref_facts_with_complete_mixed_paths(
        self,
    ) -> None:
        from daem0nmcp.retrieval.specialized import GraphProvider

        self._document("f")
        self._document("0")
        self.connection.execute(
            "INSERT INTO memory_fact_versions (fact_version_id,fact_id,"
            "workspace_id,version,subject_record_id,predicate,object_json,"
            "content_hash,valid_from_us,valid_to_us,transaction_from_us,"
            "transaction_to_us,asserted_by_event_id,retracted_by_event_id,"
            "object_kind) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                _fact_id("7"),
                _fact_id("7"),
                WORKSPACE_ID,
                1,
                _record_id("8"),
                "references",
                json.dumps(_record_id("f")),
                _hash("7"),
                T0_US,
                None,
                T0_US,
                None,
                _event_id("7"),
                None,
                "record_ref",
            ),
        )
        self.connection.execute(
            "INSERT INTO memory_relationship_versions VALUES "
            "(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            self._edge("8", "f", "0"),
        )
        self.connection.execute(
            "UPDATE projection_manifests SET row_count=8 "
            "WHERE workspace_id=? AND projection_name='graph'",
            (WORKSPACE_ID,),
        )
        self.connection.commit()

        result = await GraphProvider(
            self.connection, max_depth=2, max_branching=100
        ).search(self._graph_query(), 20, seeds=(_seed("8"),))

        by_record = {
            candidate.evidence.record_id: candidate for candidate in result.candidates
        }
        self.assertEqual(
            (_fact_id("7"),),
            by_record[_record_id("f")].evidence.relation_path,
        )
        self.assertEqual(
            (_fact_id("7"), _relation_id("8")),
            by_record[_record_id("0")].evidence.relation_path,
        )

    async def test_exact_depth_and_branching_caps_are_enforced(self) -> None:
        from daem0nmcp.retrieval.specialized import GraphProvider

        depth_one = await GraphProvider(
            self.connection, max_depth=1, max_branching=1
        ).search(self._graph_query(), 10, seeds=(_seed("8"),))
        depth_two = await GraphProvider(
            self.connection, max_depth=2, max_branching=1
        ).search(self._graph_query(), 10, seeds=(_seed("8"),))

        self.assertEqual(
            (_record_id("9"),),
            tuple(item.evidence.record_id for item in depth_one.candidates),
        )
        self.assertEqual(
            (_record_id("9"), _record_id("c")),
            tuple(item.evidence.record_id for item in depth_two.candidates),
        )

    async def test_unknown_seed_cannot_generate_unseeded_records(self) -> None:
        from daem0nmcp.retrieval.specialized import GraphProvider

        result = await GraphProvider(self.connection).search(
            self._graph_query(), 10, seeds=(_seed("f"),)
        )

        self.assertEqual("degraded", result.status)
        self.assertEqual("GRAPH_SEEDS_STALE", result.reason)
        self.assertEqual((), result.candidates)

    async def test_stale_seed_provenance_cannot_authorize_expansion(self) -> None:
        from daem0nmcp.retrieval.specialized import GraphProvider

        provider = GraphProvider(self.connection)
        invalid_seeds = (
            _seed("8", content_digit="f"),
            _seed("8", event_digit="f"),
            _seed("8", generation=8),
        )
        for seed in invalid_seeds:
            with self.subTest(seed=seed):
                result = await provider.search(self._graph_query(), 10, seeds=(seed,))
                self.assertEqual("degraded", result.status)
                self.assertEqual("GRAPH_SEEDS_STALE", result.reason)
                self.assertEqual((), result.candidates)

    async def test_active_dense_seed_is_an_explicit_valid_root(self) -> None:
        from daem0nmcp.retrieval.specialized import GraphProvider

        self._manifest("dense", generation=9, row_count=7)
        self.connection.commit()
        result = await GraphProvider(self.connection).search(
            self._graph_query(),
            10,
            seeds=(_seed("8", "dense", generation=9),),
        )

        self.assertEqual("ready", result.status)
        self.assertEqual(_record_id("9"), result.candidates[0].evidence.record_id)

    async def test_invalidated_relationships_never_traverse(self) -> None:
        from daem0nmcp.retrieval.specialized import GraphProvider

        result = await GraphProvider(
            self.connection, max_depth=4, max_branching=100
        ).search(self._graph_query(), 20, seeds=(_seed("8"),))

        returned = {item.evidence.record_id for item in result.candidates}
        self.assertNotIn(_record_id("d"), returned)
        self.assertNotIn(_record_id("e"), returned)

    async def test_builder_contract_mismatch_is_stale(self) -> None:
        from daem0nmcp.retrieval.specialized import GraphProvider

        self._tamper_manifest_detail("graph", "builder_contract_hash", _hash("0"))

        result = await GraphProvider(self.connection).search(
            self._graph_query(), 10, seeds=(_seed("8"),)
        )

        self.assertEqual("unavailable", result.status)
        self.assertEqual("GRAPH_STALE", result.reason)
        self.assertEqual((), result.candidates)

    def test_configuration_has_exact_hard_caps(self) -> None:
        from daem0nmcp.retrieval.specialized import (
            MAX_GRAPH_BRANCHING,
            MAX_GRAPH_DEPTH,
            GraphProvider,
        )

        for changes in (
            {"max_depth": 0},
            {"max_depth": MAX_GRAPH_DEPTH + 1},
            {"max_depth": True},
            {"max_branching": 0},
            {"max_branching": MAX_GRAPH_BRANCHING + 1},
            {"max_branching": True},
        ):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                GraphProvider(self.connection, **changes)


class SpecializedProviderAsyncBoundaryTests(_SpecializedDatabaseTestCase):
    def test_enormous_timeout_is_an_owned_validation_error(self) -> None:
        from daem0nmcp.retrieval.specialized import ProcedureProvider

        with self.assertRaises(ValueError):
            ProcedureProvider(self.connection, timeout_seconds=10**400)

    async def test_connection_factory_and_sqlite_work_run_off_event_loop(
        self,
    ) -> None:
        from daem0nmcp.retrieval.specialized import ProcedureProvider

        fts_table = "retrieval_procedure_fts_0123456789abcdef01234567_g1"
        self.connection.execute(
            f'CREATE VIRTUAL TABLE "{fts_table}" USING fts5('
            "record_id UNINDEXED,ordinal UNINDEXED,step_hash UNINDEXED,"
            "source_event_id UNINDEXED,step_text,"
            "tokenize='unicode61 remove_diacritics 2')"
        )
        self._manifest(
            "procedure",
            generation=1,
            row_count=0,
        )
        self.connection.commit()
        worker_threads: list[int] = []

        def slow_worker_connection() -> sqlite3.Connection:
            worker_threads.append(threading.get_ident())
            time.sleep(0.2)
            return sqlite3.connect(self.database_path)

        provider = ProcedureProvider(
            connection_factory=slow_worker_connection,
            timeout_seconds=1.0,
        )
        loop = asyncio.get_running_loop()
        started = loop.time()
        task = asyncio.create_task(provider.search(self._query(text="workflow"), 10))
        await asyncio.sleep(0.025)

        self.assertLess(loop.time() - started, 0.12)
        self.assertFalse(task.done())
        result = await task
        self.assertEqual("ready", result.status)
        self.assertTrue(worker_threads)
        self.assertNotEqual(threading.get_ident(), worker_threads[0])


if __name__ == "__main__":
    unittest.main()
