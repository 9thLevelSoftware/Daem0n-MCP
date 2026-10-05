"""Offline autoresearch entrypoint for the existing coding-memory workload."""

from __future__ import annotations

import asyncio
import math

from benchmarks.coding_memory_eval import run_evaluation


async def main() -> None:
    report = await run_evaluation(mode="lexical_only", topics=24, seed=20261004)
    baseline = report["arms"]["baseline"]
    enhanced = report["arms"]["all_apply"]
    metrics = {
        "coding_memory_ndcg": enhanced["ndcg_at_10"]["overall"],
        "baseline_ndcg": baseline["ndcg_at_10"]["overall"],
        "outcome_reuse_ndcg": enhanced["ndcg_at_10"]["outcome_reuse"],
        "provenance_chain_ndcg": enhanced["ndcg_at_10"]["provenance_chain"],
        "required_fact_retention": enhanced["retention"]["required_fact_retention"],
        "stale_flag_recall": enhanced["validity"]["stale_flag_recall"],
        "false_flag_rate": enhanced["validity"]["false_flag_rate"],
        "rendered_tokens_mean": enhanced["tokens"]["rendered_mean"],
    }
    for name, value in metrics.items():
        if not isinstance(value, (int, float)) or not math.isfinite(value):
            raise RuntimeError(f"Invalid benchmark metric: {name}={value!r}")
    for name, value in metrics.items():
        print(f"METRIC {name}={value:.12f}", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
