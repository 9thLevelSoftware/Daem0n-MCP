"""Production-path coding-memory evaluation headroom and deterministic reports."""

from __future__ import annotations

import asyncio
import copy
import math
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from benchmarks.coding_memory_eval import (
    _Corpus,
    _corpus,
    _metrics,
    _Query,
    run_evaluation,
)
from daem0nmcp.api.v7.discovery_operations import default_code_indexer_factory
from daem0nmcp.schema_version import CURRENT_SCHEMA_VERSION


class CodingMemoryEvaluationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        async def evaluate_twice():
            first = await run_evaluation(mode="lexical_only", topics=6, seed=20261004)
            second = await run_evaluation(mode="lexical_only", topics=6, seed=20261004)
            return first, second

        cls.first, cls.second = asyncio.run(evaluate_twice())

    def test_non_latency_report_is_identical_across_real_runs(self):
        first, second = copy.deepcopy(self.first), copy.deepcopy(self.second)
        for report in (first, second):
            for arm in report["arms"].values():
                del arm["latency_ms"]
        self.assertEqual(first, second)

    def test_baseline_has_graded_outcome_headroom(self):
        self.assertLess(
            self.first["arms"]["baseline"]["ndcg_at_10"]["outcome_reuse"], 0.9
        )

    def test_verified_outcomes_improve_reuse(self):
        arms = self.first["arms"]
        self.assertGreaterEqual(
            arms["utility_trace"]["ndcg_at_10"]["outcome_reuse"],
            arms["baseline"]["ndcg_at_10"]["outcome_reuse"] + 0.05,
        )

    def test_trace_credit_improves_over_single_step_on_chains(self):
        arms = self.first["arms"]
        self.assertGreater(
            arms["utility_trace"]["ndcg_at_10"]["provenance_chain"],
            arms["utility_single"]["ndcg_at_10"]["provenance_chain"],
        )

    def test_validity_detects_file_edits_even_without_symbol_parsing(self):
        validity = self.first["arms"]["validity_apply"]["validity"]
        self.assertEqual(1.0, validity["stale_flag_recall"])
        self.assertEqual(0.0, validity["false_flag_rate"])
        baseline = self.first["arms"]["baseline"]["validity"]
        self.assertIsNone(baseline["stale_flag_recall"])
        self.assertIsNone(baseline["false_flag_rate"])
        if not getattr(default_code_indexer_factory(), "available", False):
            self.assertIsNone(validity["symbol"])

    def test_validity_detects_bound_symbols_not_sibling_edits(self):
        if not getattr(default_code_indexer_factory(), "available", False):
            self.skipTest(
                "tree-sitter grammars unavailable; file bindings still evaluated"
            )
        validity = self.first["arms"]["validity_apply"]["validity"]
        self.assertEqual(1.0, validity["stale_flag_recall"])
        self.assertEqual(0.0, validity["false_flag_rate"])
        self.assertEqual(
            {"stale_flag_recall": 1.0, "false_flag_rate": 0.0}, validity["symbol"]
        )

    def test_all_arms_and_metadata_are_reported(self):
        self.assertEqual(
            {
                "baseline",
                "utility_single",
                "utility_trace",
                "validity_apply",
                "retention_apply",
                "all_apply",
            },
            set(self.first["arms"]),
        )
        metadata = self.first["metadata"]
        self.assertEqual("lexical_only", metadata["mode"])
        self.assertEqual(6, metadata["topics"])
        self.assertEqual(20261004, metadata["seed"])
        self.assertEqual(CURRENT_SCHEMA_VERSION, metadata["schema_version"])
        self.assertEqual(
            {
                "local": "disabled",
                "models-local": "disabled",
                "graph": "disabled",
                "late-interaction": "disabled",
            },
            metadata["capability_statuses"],
        )
        self.assertEqual(16, metadata["retention_note_tag_count"])
        self.assertEqual("procedure", metadata["retention_background_note_record_type"])
        self.assertIn("category-diversity", metadata["retention_stress_reason"])
        self.assertEqual(0.2, metadata["utility_weight"])
        self.assertEqual(set(self.first["arms"]), set(metadata["arm_overrides"]))
        for name, arm in self.first["arms"].items():
            with self.subTest(arm=name):
                self.assertEqual(
                    {
                        "ndcg_at_10",
                        "mrr_at_10",
                        "validity",
                        "retention",
                        "tokens",
                        "latency_ms",
                    },
                    set(arm),
                )
                self.assertEqual(
                    {"outcome_reuse", "provenance_chain", "overall"},
                    set(arm["ndcg_at_10"]),
                )
                for metric in (
                    *arm["ndcg_at_10"].values(),
                    arm["mrr_at_10"]["overall"],
                    arm["retention"]["required_fact_retention"],
                ):
                    self.assertGreaterEqual(metric, 0.0)
                    self.assertLessEqual(metric, 1.0)
                self.assertTrue(math.isfinite(arm["tokens"]["rendered_mean"]))
                self.assertGreater(arm["tokens"]["rendered_mean"], 0)
                self.assertGreaterEqual(arm["latency_ms"]["p50"], 0)
                self.assertGreaterEqual(
                    arm["latency_ms"]["p95"], arm["latency_ms"]["p50"]
                )
                self.assertTrue(math.isfinite(arm["latency_ms"]["p95"]))

    def test_exclusively_owned_workspace_is_removed_after_runs(self):
        scratch = (
            Path(tempfile.gettempdir())
            / "daem0nmcp-coding-eval-lexical_only-6-20261004"
        )
        self.assertFalse(scratch.exists())


class CodingMemoryFixtureTests(unittest.TestCase):
    def _corpus(self, topics: int):
        with tempfile.TemporaryDirectory() as root:
            return _corpus(Path(root), topics, True)

    def test_no_query_text_is_shared_by_two_goldens(self):
        corpus = self._corpus(24)
        goldens: dict[str, set[tuple[tuple[str, int], ...]]] = {}
        for query in corpus.queries:
            goldens.setdefault(query.text, set()).add(query.relevant)
        self.assertEqual(
            {}, {text: keys for text, keys in goldens.items() if len(keys) > 1}
        )
        self.assertEqual(
            24, len({q.text for q in corpus.queries if q.family == "retention"})
        )

    def test_no_query_repeats_a_golden_record_verbatim(self):
        corpus = self._corpus(24)
        contents = {
            item.key: item.content for item in corpus.writes if hasattr(item, "content")
        }
        verbatim = [
            query.key
            for query in corpus.queries
            if query.family == "validity"
            and any(contents[key] == query.text for key, _ in query.relevant)
        ]
        self.assertEqual([], verbatim)
        self.assertEqual(
            48, len([query for query in corpus.queries if query.family == "validity"])
        )

    def _retention(self, items: list[tuple[str, str]], context: str) -> float:
        """Score one retention query against a stubbed recall result."""

        corpus = _Corpus(
            queries=[
                _Query("reuse", "outcome_reuse", "reuse", (("good", 3),)),
                _Query("chain", "provenance_chain", "chain", (("background", 3),)),
                _Query(
                    "retention",
                    "retention",
                    "deploy release rollout",
                    (("expected", 3),),
                    "freeze release writes",
                ),
            ]
        )

        def result(pairs: list[tuple[str, str]], rendered: str) -> SimpleNamespace:
            return SimpleNamespace(
                items=[
                    SimpleNamespace(
                        citation=citation, record=SimpleNamespace(record_id=record)
                    )
                    for citation, record in pairs
                ],
                rendered_context=rendered,
                token_usage=SimpleNamespace(rendered=1),
            )

        results = {
            "reuse": result([("[E1]", "mem_good")], "[E1] decision\nreuse"),
            "chain": result([("[E1]", "mem_background")], "[E1] note\nchain"),
            "retention": result(items, context),
        }
        record_ids = {
            "good": "mem_good",
            "background": "mem_background",
            "expected": "mem_expected",
            "other": "mem_other",
        }
        report = _metrics(corpus, record_ids, results, [1.0], False)
        return report["retention"]["required_fact_retention"]

    def test_required_fact_in_another_record_is_not_retention(self):
        steps = "Steps: freeze release writes | apply release rollout"
        self.assertEqual(
            0.0,
            self._retention(
                [("[E1]", "mem_other")],
                f"[E1] procedure\ndeploy release rollout\n{steps}",
            ),
        )
        self.assertEqual(
            0.0,
            self._retention(
                [("[E1]", "mem_expected"), ("[E2]", "mem_other")],
                "[E1] procedure\ndeploy release rollout\n\n"
                f"[E2] procedure\ndeploy release rollout\n{steps}",
            ),
        )

    def test_required_fact_in_the_expected_record_is_retention(self):
        steps = "Steps: freeze release writes | apply release rollout"
        self.assertEqual(
            1.0,
            self._retention(
                [("[E1]", "mem_other"), ("[E2]", "mem_expected")],
                "[E1] procedure\ndeploy release rollout\n\n"
                f"[E2] procedure\ndeploy release rollout\n{steps}",
            ),
        )


class CodingMemoryEvaluationInputTests(unittest.IsolatedAsyncioTestCase):
    async def test_invalid_parameters_fail_before_creating_workspace(self):
        for arguments in (
            {"mode": "scripted"},
            {"topics": 0},
            {"topics": True},
            {"topics": 25},
            {"seed": True},
        ):
            with self.subTest(arguments=arguments), self.assertRaises(ValueError):
                await run_evaluation(**arguments)
