"""Regression coverage for the v7 retrieval-router compatibility boundary."""

from __future__ import annotations

import sys
import types
import unittest

import pytest

pytest.importorskip("numpy")

_previous_package = sys.modules.get("sentence_transformers")
_previous_util = sys.modules.get("sentence_transformers.util")
_util = types.ModuleType("sentence_transformers.util")
_util.cos_sim = lambda _left, _right: 0.0
_package = types.ModuleType("sentence_transformers")
_package.util = _util
sys.modules["sentence_transformers"] = _package
sys.modules["sentence_transformers.util"] = _util
try:
    from daem0nmcp.retrieval_router import RetrievalRouter
finally:
    if _previous_package is None:
        sys.modules.pop("sentence_transformers", None)
    else:
        sys.modules["sentence_transformers"] = _previous_package
    if _previous_util is None:
        sys.modules.pop("sentence_transformers.util", None)
    else:
        sys.modules["sentence_transformers.util"] = _previous_util


class RetrievalRouterV7Tests(unittest.IsolatedAsyncioTestCase):
    async def test_route_and_compress_never_performs_global_string_compression(self):
        router = object.__new__(RetrievalRouter)

        async def route_search(query, top_k=10, **_kwargs):
            return {
                "classification": None,
                "community_context": None,
                "results": [(1, 1.0)],
                "strategy_used": "hybrid",
            }

        router.route_search = route_search

        fake_jit = types.ModuleType("daem0nmcp.compression.jit")

        class Compressor:
            def compress_if_needed(self, _text):
                return {
                    "compressed_tokens": 1,
                    "compression_rate": 0.01,
                    "original_tokens": 100,
                    "text": "fabricated compressed text",
                    "threshold_triggered": "soft",
                }

        fake_jit.get_jit_compressor = lambda: Compressor()
        previous_jit = sys.modules.get("daem0nmcp.compression.jit")
        sys.modules["daem0nmcp.compression.jit"] = fake_jit
        try:
            result = await router.route_and_compress(
                "query", result_text="canonical evidence " * 100
            )
        finally:
            if previous_jit is None:
                sys.modules.pop("daem0nmcp.compression.jit", None)
            else:
                sys.modules["daem0nmcp.compression.jit"] = previous_jit

        self.assertNotIn("compressed_text", result)
        self.assertNotIn("compression_metadata", result)
        self.assertEqual([(1, 1.0)], result["results"])


if __name__ == "__main__":
    unittest.main()
