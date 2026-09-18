"""Focused federation budgets, link validation, and deterministic fusion tests."""

from __future__ import annotations

import sqlite3
import tempfile
import threading
import unittest
from datetime import datetime, timezone
from pathlib import Path


def _query(workspace_id: str, **overrides):
    from daem0nmcp.retrieval import RetrievalQuery

    values = {
        "workspace_id": workspace_id,
        "text": "federated decision",
        "limit": 2,
        "candidate_limit": 5,
        "token_budget": 40,
    }
    values.update(overrides)
    return RetrievalQuery(**values)


class FederatedRetrievalTests(unittest.TestCase):
    def test_origin_access_guard_linearizes_publication_before_unlink(self) -> None:
        from daem0nmcp.api.v7.federated_retrieval import federation_access_lock

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "origin.sqlite3"
            path.touch()
            read_guard = federation_access_lock(path, "shared").acquire()
            writer_started = threading.Event()
            writer_acquired = threading.Event()

            def mutate_link_ledger() -> None:
                writer_started.set()
                with federation_access_lock(path, "exclusive"):
                    writer_acquired.set()

            writer = threading.Thread(target=mutate_link_ledger)
            writer.start()
            self.assertTrue(writer_started.wait(1))
            self.assertFalse(writer_acquired.wait(0.1))
            read_guard.release()
            self.assertTrue(writer_acquired.wait(1))
            writer.join(timeout=1)
            self.assertFalse(writer.is_alive())

    def test_source_slices_share_total_budgets_and_are_stable(self) -> None:
        from daem0nmcp.api.v7.federated_retrieval import sliced_queries

        origin = "ws_" + "f" * 24
        linked = ["ws_" + "b" * 24, "ws_" + "a" * 24, "ws_" + "c" * 24]
        slices = sliced_queries(_query(origin), origin, linked)

        self.assertEqual(
            [origin, "ws_" + "a" * 24, "ws_" + "b" * 24, "ws_" + "c" * 24],
            list(slices),
        )
        self.assertEqual(5, sum(item.limit for item in slices.values()))
        self.assertEqual(5, sum(item.candidate_limit for item in slices.values()))
        self.assertTrue(all(item.token_budget == 40 for item in slices.values()))

        with self.assertRaisesRegex(Exception, "INVALID_ARGUMENT"):
            sliced_queries(_query(origin, limit=1, candidate_limit=2), origin, linked)

    def test_global_fusion_uses_source_rank_and_workspace_ties(self) -> None:
        from daem0nmcp.api.v7.federated_retrieval import (
            FederatedCandidate,
            FederatedSourceResult,
            compose_federated_results,
        )
        from daem0nmcp.api.v7.models import (
            EvidenceRef,
            RecordSummary,
        )

        now = datetime(2026, 1, 1, tzinfo=timezone.utc)

        def result(workspace_id: str, suffix: str) -> FederatedSourceResult:
            ref = EvidenceRef(
                origin_workspace_id=workspace_id,
                record_id="mem_" + suffix * 64,
                event_id="evt_" + suffix * 64,
                content_hash=suffix * 64,
                provider="dense",
            )
            item = FederatedCandidate(
                record=RecordSummary(
                    record_id=ref.record_id,
                    record_type="decision",
                    excerpt=f"decision {suffix}",
                    tags=[],
                    current_status="current",
                    content_hash=ref.content_hash,
                    created_at=now,
                    updated_at=now,
                ),
                content=f"decision {suffix}",
                channels=("dense",),
                status="current",
                evidence_refs=(ref,),
            )
            return FederatedSourceResult(candidates=(item,))

        first = "ws_" + "a" * 24
        second = "ws_" + "b" * 24
        fused = compose_federated_results(
            {second: result(second, "b"), first: result(first, "a")},
            _query(first),
        )

        self.assertEqual(
            [first, second],
            [item.evidence_refs[0].origin_workspace_id for item in fused.items],
        )
        self.assertEqual([1.0, 1.0], [item.score for item in fused.items])
        self.assertEqual(["[E1]", "[E2]"], [item.citation for item in fused.items])
        self.assertEqual(1, fused.rendered_context.count("[E1]"))
        self.assertEqual(1, fused.rendered_context.count("[E2]"))

    def test_limit_one_can_select_linked_only_relevant_source(self) -> None:
        from daem0nmcp.api.v7.federated_retrieval import (
            FederatedSourceResult,
            compose_federated_results,
        )

        origin = "ws_" + "a" * 24
        linked = "ws_" + "b" * 24
        linked_result = self._source(linked, ("b",))
        result = compose_federated_results(
            {origin: FederatedSourceResult(), linked: linked_result},
            _query(origin, limit=1, candidate_limit=2),
        )
        self.assertEqual(1, len(result.items))
        self.assertEqual(linked, result.items[0].evidence_refs[0].origin_workspace_id)

    def test_empty_origin_multiple_linked_and_tight_tokens_compose_once(self) -> None:
        from daem0nmcp.api.v7.federated_retrieval import (
            FederatedSourceResult,
            compose_federated_results,
        )
        from daem0nmcp.retrieval.runtime import CoreTokenizer

        origin = "ws_" + "a" * 24
        first = "ws_" + "b" * 24
        second = "ws_" + "c" * 24
        result = compose_federated_results(
            {
                origin: FederatedSourceResult(),
                first: self._source(first, ("b", "d", "e"), content="word " * 80),
                second: self._source(second, ("c",), content="target " * 80),
            },
            _query(origin, limit=2, candidate_limit=4, token_budget=12),
        )
        self.assertEqual(2, len(result.items))
        self.assertEqual(
            {first, second},
            {item.evidence_refs[0].origin_workspace_id for item in result.items},
        )
        self.assertEqual(
            result.token_usage.rendered,
            CoreTokenizer().count_tokens(result.rendered_context),
        )
        self.assertLessEqual(result.token_usage.rendered, 12)
        self.assertEqual(1, result.rendered_context.count("\n"))

    @staticmethod
    def _source(workspace_id: str, suffixes: tuple[str, ...], content="linked"):
        from daem0nmcp.api.v7.federated_retrieval import (
            FederatedCandidate,
            FederatedSourceResult,
        )
        from daem0nmcp.api.v7.models import EvidenceRef, RecordSummary

        now = datetime(2026, 1, 1, tzinfo=timezone.utc)
        candidates = []
        for suffix in suffixes:
            ref = EvidenceRef(
                origin_workspace_id=workspace_id,
                record_id="mem_" + suffix * 64,
                event_id="evt_" + suffix * 64,
                content_hash=suffix * 64,
                provider="lexical",
            )
            candidates.append(
                FederatedCandidate(
                    record=RecordSummary(
                        record_id=ref.record_id,
                        record_type="decision",
                        excerpt=content,
                        tags=[],
                        current_status="current",
                        content_hash=ref.content_hash,
                        created_at=now,
                        updated_at=now,
                    ),
                    content=content,
                    channels=("lexical",),
                    status="current",
                    evidence_refs=(ref,),
                )
            )
        return FederatedSourceResult(candidates=tuple(candidates))

    def test_directional_link_must_be_current_and_hash_valid(self) -> None:
        from daem0nmcp.api.v7.federated_retrieval import (
            FederatedRetrievalError,
            validate_directional_links,
        )
        from daem0nmcp.event_store import sha256_json

        origin = "ws_" + "a" * 24
        linked = "ws_" + "b" * 24
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "links.sqlite3"
            connection = sqlite3.connect(path)
            connection.execute(
                "CREATE TABLE workspace_link_events (event_id TEXT,workspace_id TEXT,"
                "linked_workspace_id TEXT,stream_version INTEGER,event_type TEXT,"
                "relationship TEXT,label TEXT,occurred_at_us INTEGER,"
                "recorded_at_us INTEGER,previous_event_hash TEXT,event_hash TEXT)"
            )
            envelope = {
                "workspace_id": origin,
                "linked_workspace_id": linked,
                "stream_version": 1,
                "event_type": "workspace.linked",
                "relationship": "related",
                "label": None,
                "occurred_at_us": 1,
                "recorded_at_us": 1,
                "previous_event_hash": None,
            }
            digest = sha256_json(envelope)
            connection.execute(
                "INSERT INTO workspace_link_events VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (
                    f"evt_{digest}",
                    origin,
                    linked,
                    1,
                    "workspace.linked",
                    "related",
                    None,
                    1,
                    1,
                    None,
                    digest,
                ),
            )
            connection.commit()
            connection.close()

            validate_directional_links(path, origin, (linked,))
            with self.assertRaises(FederatedRetrievalError):
                validate_directional_links(path, linked, (origin,))


if __name__ == "__main__":
    unittest.main()
