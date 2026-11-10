"""Production-backed, non-oracle acceptance for the Task 8 corpus."""

from __future__ import annotations

import copy
import subprocess
import sys
import unittest
from pathlib import Path

from benchmarks.retrieval_benchmark import (
    calculate_quality_metrics,
    calculate_ranking_metrics,
    load_retrieval_fixtures,
)

FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "retrieval"


def _quality(result: dict[str, object]) -> dict[str, object]:
    return {key: value for key, value in result.items() if key != "provider_timings_ns"}


class ProductionRetrievalAdapterTests(unittest.TestCase):
    def setUp(self) -> None:
        from benchmarks.retrieval_production_adapter import (
            ProductionRetrievalAdapter,
        )

        self.fixtures = load_retrieval_fixtures(FIXTURE_ROOT)
        self.adapter = ProductionRetrievalAdapter(
            FIXTURE_ROOT,
            fixtures=self.fixtures,
        )

    def tearDown(self) -> None:
        self.adapter.close()

    def test_fully_enabled_mode_runs_real_service_and_fixture_rank_fake(self):
        results = {
            query["query_id"]: self.adapter.retrieve(
                "fully_enabled",
                query,
            )
            for query in self.fixtures.queries
        }

        for query in self.fixtures.queries:
            query_id = query["query_id"]
            result = results[query_id]
            self.assertEqual(query["expected_abstention"], result["abstained"])
            self.assertTrue(
                set(query["required_citations"]).issubset(
                    result["citation_record_ids"]
                ),
                query_id,
            )
            self.assertTrue(
                set(query["expected_excluded_citations"]).isdisjoint(
                    result["citation_record_ids"]
                ),
                query_id,
            )
            if query["expected_relevant"]:
                self.assertEqual(
                    query["expected_relevant"][0]["record_id"],
                    result["returned_record_ids"][0],
                    query_id,
                )

    def test_adapter_output_is_independent_of_expected_answer_fields(self):
        query = copy.deepcopy(self.fixtures.queries[0])
        baseline = _quality(self.adapter.retrieve("fully_enabled", query))
        query["expected_relevant"] = []
        query["required_citations"] = []
        query["expected_excluded_citations"] = [self.fixtures.records[-1]["record_id"]]
        query["expected_abstention"] = True
        query["expected_provider_degradation"] = {
            "fully_enabled": ["lexical"],
            "lexical_only": ["lexical"],
        }

        self.assertEqual(
            baseline,
            _quality(self.adapter.retrieve("fully_enabled", query)),
        )

    def test_production_corpus_has_pinned_quality_metrics(self):
        reports = {
            mode: {
                query["query_id"]: self.adapter.retrieve(mode, query)
                for query in self.fixtures.queries
            }
            for mode in ("fully_enabled", "lexical_only")
        }

        fully_ranking = calculate_ranking_metrics(
            self.fixtures.queries,
            reports["fully_enabled"],
        )
        fully_quality = calculate_quality_metrics(
            self.fixtures.queries,
            reports["fully_enabled"],
        )
        self.assertEqual(
            {"1": 1.0, "3": 1.0, "5": 1.0, "10": 1.0},
            fully_ranking["recall_at"],
        )
        self.assertEqual(1.0, fully_ranking["mrr_at_10"])
        self.assertEqual(1.0, fully_ranking["ndcg_at_10"])
        self.assertEqual(1.0, fully_quality["contradiction_handling"])
        self.assertEqual(
            1.0,
            fully_quality["excluded_citation_exclusion_rate"],
        )
        self.assertEqual(1.0, fully_quality["tokens"]["evidence_coverage"])

        lexical_ranking = calculate_ranking_metrics(
            self.fixtures.queries,
            reports["lexical_only"],
        )
        lexical_quality = calculate_quality_metrics(
            self.fixtures.queries,
            reports["lexical_only"],
        )
        # Lexical-only intentionally misses only the dense synonym query.
        self.assertEqual(0.9, lexical_ranking["recall_at"]["1"])
        self.assertEqual(0.9, lexical_ranking["mrr_at_10"])
        self.assertEqual(0.9, lexical_ranking["ndcg_at_10"])
        self.assertEqual(0.5, lexical_quality["contradiction_handling"])
        self.assertEqual(
            10 / 11,
            lexical_quality["tokens"]["evidence_coverage"],
        )

    def test_adapter_source_never_reads_fixture_answer_fields(self):
        source = (
            Path(__file__).parents[1] / "benchmarks" / "retrieval_production_adapter.py"
        ).read_text(encoding="utf-8")

        for forbidden in (
            "expected_abstention",
            "expected_excluded_citations",
            "expected_provider_degradation",
            "expected_relevant",
            "required_citations",
        ):
            self.assertNotIn(forbidden, source)

    def test_lazy_default_adapter_closes_before_temporary_directory_exit(self):
        completed = subprocess.run(
            [
                sys.executable,
                "-W",
                "error",
                "-c",
                "from benchmarks.retrieval_production_adapter import "
                "_default_adapter; _default_adapter()",
            ],
            cwd=Path(__file__).parents[1],
            capture_output=True,
            check=False,
            text=True,
            timeout=30,
        )

        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertNotIn("ResourceWarning", completed.stderr)
        self.assertNotIn("Exception ignored", completed.stderr)


if __name__ == "__main__":
    unittest.main()
