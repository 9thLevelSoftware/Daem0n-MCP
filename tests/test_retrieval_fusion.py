"""Deterministic weighted reciprocal-rank fusion tests."""

from __future__ import annotations

import math
import os
import subprocess
import sys
import textwrap
import unittest
from datetime import datetime, timedelta, timezone


def _record_id(digit: str) -> str:
    return "mem_" + digit * 64


def _event_id(digit: str) -> str:
    return "evt_" + digit * 64


CONTENT_HASH = "a" * 64


def _candidate(
    record_digit: str,
    provider: str,
    rank: int,
    *,
    raw_score: float = 0.0,
    transaction_time: datetime | None = None,
):
    from daem0nmcp.retrieval.types import Candidate, EvidenceRef

    return Candidate(
        evidence=EvidenceRef(
            record_id=_record_id(record_digit),
            event_id=_event_id(record_digit),
            content_hash=CONTENT_HASH,
            version_id=None,
            provider=provider,
        ),
        rank=rank,
        raw_score=raw_score,
        channels=frozenset({provider}),
        highlights=(f"{provider} excerpt",),
        transaction_time=transaction_time,
    )


def _result(provider: str, *candidates, generation: int = 1):
    from daem0nmcp.retrieval.types import ProviderResult

    return ProviderResult(
        provider=provider,
        candidates=tuple(candidates),
        status="ready",
        manifest_generation=generation,
    )


class WeightedReciprocalRankFusionTests(unittest.TestCase):
    def test_default_weights_and_k_use_only_provider_ranks(self):
        from daem0nmcp.retrieval.fusion import (
            DEFAULT_RRF_K,
            DEFAULT_RRF_WEIGHTS,
            weighted_reciprocal_rank_fusion,
        )

        lexical = _result("lexical", _candidate("1", "lexical", 1, raw_score=-99))
        graph = _result("graph", _candidate("1", "graph", 2, raw_score=1e200))
        dense = _result("dense", _candidate("2", "dense", 1, raw_score=1e200))

        fused = weighted_reciprocal_rank_fusion((dense, graph, lexical))

        self.assertEqual(60, DEFAULT_RRF_K)
        self.assertEqual(
            {
                "lexical": 1.0,
                "dense": 1.0,
                "graph": 0.7,
                "temporal": 0.85,
                "procedure": 0.8,
                "outcome": 0.9,
            },
            dict(DEFAULT_RRF_WEIGHTS),
        )
        self.assertEqual(
            [_record_id("1"), _record_id("2")], [item.record_id for item in fused]
        )
        self.assertAlmostEqual(1.0 / 61.0 + 0.7 / 62.0, fused[0].score)
        self.assertAlmostEqual(1.0 / 61.0, fused[1].score)
        self.assertEqual(frozenset({"graph", "lexical"}), fused[0].channels)
        self.assertEqual(
            ("lexical", "graph"), tuple(ref.provider for ref in fused[0].evidence_refs)
        )

        changed_diagnostics = weighted_reciprocal_rank_fusion(
            (
                _result("dense", _candidate("2", "dense", 1, raw_score=-1e200)),
                _result("graph", _candidate("1", "graph", 2, raw_score=-1e200)),
                _result("lexical", _candidate("1", "lexical", 1, raw_score=1e200)),
            )
        )
        self.assertEqual(
            [(item.record_id, item.score) for item in fused],
            [(item.record_id, item.score) for item in changed_diagnostics],
        )

    def test_fusion_is_input_order_independent_and_preserves_provenance(self):
        from daem0nmcp.retrieval.fusion import weighted_reciprocal_rank_fusion

        lexical = _result("lexical", _candidate("3", "lexical", 3), generation=7)
        dense = _result("dense", _candidate("3", "dense", 1), generation=9)

        forward = weighted_reciprocal_rank_fusion((lexical, dense))
        reverse = weighted_reciprocal_rank_fusion((dense, lexical))

        self.assertEqual(forward, reverse)
        self.assertEqual(1, len(forward))
        self.assertEqual((("dense", 1), ("lexical", 3)), forward[0].channel_ranks)
        self.assertEqual(
            (("dense", 9), ("lexical", 7)), forward[0].manifest_generations
        )
        self.assertEqual(2, len(forward[0].evidence_refs))

    def test_conflicting_blank_provider_evidence_has_hash_seed_stable_order(self):
        script = textwrap.dedent(
            """
            from daem0nmcp.retrieval.fusion import weighted_reciprocal_rank_fusion
            from daem0nmcp.retrieval.types import Candidate, EvidenceRef, ProviderResult

            record_id = "mem_" + "1" * 64
            event_id = "evt_" + "2" * 64

            def result(provider, content_hash):
                evidence = EvidenceRef(
                    record_id=record_id,
                    event_id=event_id,
                    content_hash=content_hash,
                    version_id=None,
                    provider="",
                )
                candidate = Candidate(
                    evidence=evidence,
                    rank=1,
                    raw_score=None,
                    channels=frozenset({provider}),
                )
                return ProviderResult(provider=provider, candidates=(candidate,))

            fused = weighted_reciprocal_rank_fusion((
                result("lexical", "b" * 64),
                result("dense", "a" * 64),
            ))
            print(",".join(item.content_hash[0] for item in fused[0].evidence_refs))
            """
        )
        outputs = set()
        for seed in range(1, 33):
            environment = os.environ.copy()
            environment["PYTHONHASHSEED"] = str(seed)
            environment["PYTHONDONTWRITEBYTECODE"] = "1"
            completed = subprocess.run(
                [sys.executable, "-c", script],
                check=True,
                capture_output=True,
                text=True,
                env=environment,
            )
            outputs.add(completed.stdout.strip())
        self.assertEqual({"a,b"}, outputs)

    def test_ties_use_channel_count_then_lexical_rank(self):
        from daem0nmcp.retrieval.fusion import weighted_reciprocal_rank_fusion

        more_channels = weighted_reciprocal_rank_fusion(
            (
                _result("lexical", _candidate("4", "lexical", 1)),
                _result("dense", _candidate("5", "dense", 1)),
                _result("graph", _candidate("5", "graph", 1)),
            ),
            weights={"lexical": 1.0, "dense": 0.5, "graph": 0.5},
        )
        self.assertEqual(_record_id("5"), more_channels[0].record_id)

        lexical_first = weighted_reciprocal_rank_fusion(
            (
                _result("lexical", _candidate("6", "lexical", 2)),
                _result("dense", _candidate("7", "dense", 2)),
            ),
            weights={"lexical": 1.0, "dense": 1.0},
        )
        self.assertEqual(_record_id("6"), lexical_first[0].record_id)

    def test_remaining_ties_use_newest_transaction_then_record_id(self):
        from daem0nmcp.retrieval.fusion import weighted_reciprocal_rank_fusion

        old = datetime(2026, 1, 1, tzinfo=timezone.utc)
        new = old + timedelta(seconds=1)
        newest = weighted_reciprocal_rank_fusion(
            (
                _result("dense", _candidate("8", "dense", 3, transaction_time=old)),
                _result("graph", _candidate("9", "graph", 3, transaction_time=new)),
            ),
            weights={"dense": 1.0, "graph": 1.0},
        )
        self.assertEqual(_record_id("9"), newest[0].record_id)

        same_time = weighted_reciprocal_rank_fusion(
            (
                _result("dense", _candidate("b", "dense", 4, transaction_time=new)),
                _result("graph", _candidate("a", "graph", 4, transaction_time=new)),
            ),
            weights={"dense": 1.0, "graph": 1.0},
        )
        self.assertEqual(_record_id("a"), same_time[0].record_id)

    def test_invalid_configuration_and_duplicate_provider_results_fail_closed(self):
        from daem0nmcp.retrieval.fusion import weighted_reciprocal_rank_fusion

        lexical = _result("lexical", _candidate("c", "lexical", 1))
        custom = _result("custom", _candidate("d", "custom", 1))
        for kwargs in (
            {"weights": {"lexical": 0.0}},
            {"weights": {"lexical": math.inf}},
            {"weights": {"lexical": math.nan}},
            {"weights": {"lexical": 10**400}},
            {"weights": {"lexical": 1.0, "bad channel": 1.0}},
            {"k": 0},
            {"k": 10**400},
        ):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                weighted_reciprocal_rank_fusion((lexical,), **kwargs)
        with self.assertRaises(ValueError):
            weighted_reciprocal_rank_fusion((custom,))
        with self.assertRaises(ValueError):
            weighted_reciprocal_rank_fusion((lexical, lexical))


if __name__ == "__main__":
    unittest.main()
