"""Validated settings for the v7 retrieval runtime."""

from __future__ import annotations

import math
import unittest


class RetrievalSettingsTests(unittest.TestCase):
    def test_retrieval_defaults_match_the_public_runtime_contract(self):
        from daem0nmcp.config import Settings

        settings = Settings(_env_file=None)

        self.assertEqual(50, getattr(settings, "retrieval_candidate_limit", None))
        self.assertEqual(2400, getattr(settings, "retrieval_token_budget", None))
        self.assertEqual(
            {
                "dense": 1.0,
                "graph": 0.7,
                "lexical": 1.0,
                "outcome": 0.9,
                "procedure": 0.8,
                "temporal": 0.85,
            },
            getattr(settings, "retrieval_rrf_weights", None),
        )
        self.assertEqual(2, getattr(settings, "retrieval_graph_max_depth", None))
        self.assertEqual(50, getattr(settings, "retrieval_graph_max_branching", None))
        self.assertFalse(getattr(settings, "retrieval_rerank_enabled", None))
        self.assertEqual(
            25, getattr(settings, "retrieval_rerank_candidate_limit", None)
        )
        self.assertEqual(10.0, getattr(settings, "qdrant_timeout_seconds", None))
        self.assertEqual(
            "daem0nmcp", getattr(settings, "qdrant_collection_prefix", None)
        )

    def test_retrieval_numeric_settings_reject_unsafe_bounds(self):
        from daem0nmcp.config import Settings

        invalid = (
            {"retrieval_candidate_limit": 0},
            {"retrieval_candidate_limit": 1001},
            {"retrieval_token_budget": 0},
            {"retrieval_token_budget": 131_073},
            {"retrieval_graph_max_depth": 0},
            {"retrieval_graph_max_depth": 9},
            {"retrieval_graph_max_branching": 0},
            {"retrieval_graph_max_branching": 101},
            {"retrieval_rerank_candidate_limit": 0},
            {"retrieval_rerank_candidate_limit": 1001},
            {"qdrant_timeout_seconds": 0},
            {"qdrant_timeout_seconds": 60.0001},
            {"qdrant_timeout_seconds": math.inf},
            {"rrf_k": 1_000_001},
        )
        for values in invalid:
            with self.subTest(values=values), self.assertRaises(ValueError):
                Settings(_env_file=None, **values)

    def test_rrf_weights_and_collection_prefix_are_closed_and_bounded(self):
        from daem0nmcp.config import Settings

        valid_weights = {
            "dense": 1.0,
            "graph": 0.7,
            "lexical": 1.0,
            "outcome": 0.9,
            "procedure": 0.8,
            "temporal": 0.85,
        }
        invalid_weights = (
            {key: value for key, value in valid_weights.items() if key != "graph"},
            {**valid_weights, "unknown": 1.0},
            {**valid_weights, "dense": 0.0},
            {**valid_weights, "dense": math.nan},
            {**valid_weights, "dense": True},
        )
        for weights in invalid_weights:
            with self.subTest(weights=weights), self.assertRaises(ValueError):
                Settings(_env_file=None, retrieval_rrf_weights=weights)
        try:
            Settings(
                _env_file=None,
                retrieval_rrf_weights={
                    **valid_weights,
                    "lexical": 10**400,
                },
            )
        except Exception as exc:
            self.assertIsInstance(exc, ValueError)
        else:
            self.fail("unbounded RRF weight was accepted")
        for prefix in ("", " bad", "bad prefix", "a" * 33, "bad/segment"):
            with self.subTest(prefix=prefix), self.assertRaises(ValueError):
                Settings(_env_file=None, qdrant_collection_prefix=prefix)


if __name__ == "__main__":
    unittest.main()
