"""Production-path coding-memory evaluation headroom and deterministic reports."""

from __future__ import annotations

import asyncio
import copy
import math
import tempfile
import unittest
from pathlib import Path

from benchmarks.coding_memory_eval import run_evaluation
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


class CodingMemoryEvaluationInputTests(unittest.IsolatedAsyncioTestCase):
    async def test_invalid_parameters_fail_before_creating_workspace(self):
        for arguments in (
            {"mode": "scripted"},
            {"topics": 0},
            {"topics": True},
            {"seed": True},
        ):
            with self.subTest(arguments=arguments), self.assertRaises(ValueError):
                await run_evaluation(**arguments)
