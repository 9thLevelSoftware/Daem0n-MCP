"""Offline autoresearch entrypoint for the existing coding-memory workload."""

from __future__ import annotations

import asyncio
import math
import tempfile
from pathlib import Path
from typing import Any

from fastmcp import Client

from benchmarks.coding_memory_eval import _settings, run_evaluation
from daem0nmcp.api.v7.production import create_v7_server
from daem0nmcp.database import DatabaseManager
from daem0nmcp.workspace import WorkspaceRegistry


async def _ergonomics_metrics() -> dict[str, int]:
    metrics: dict[str, int] = {}
    for mode in ("guided", "strict"):
        with tempfile.TemporaryDirectory(
            prefix=f"daem0nmcp-ergonomics-{mode}-"
        ) as root:
            workspace_root = Path(root) / "workspace"
            storage = workspace_root / ".daem0nmcp" / "storage"
            storage.mkdir(parents=True)
            manager = DatabaseManager(str(storage))
            try:
                await manager.init_db()
            finally:
                await manager.close()
            workspace = WorkspaceRegistry(
                [workspace_root], default_root=workspace_root
            ).default
            settings = _settings(
                workspace,
                {"covenant_mode": mode, "tool_surface": "core", "dream_enabled": False},
            )
            environment = {
                "DAEM0NMCP_LOCAL_ENABLED": "false",
                "DAEM0NMCP_MODELS_LOCAL_ENABLED": "false",
                "DAEM0NMCP_GRAPH_ENABLED": "false",
                "DAEM0NMCP_LATE_INTERACTION_ENABLED": "false",
            }
            server = create_v7_server("stdio", settings=settings, environ=environment)
            scope = {"workspace_id": workspace.workspace_id}
            arguments = {
                **scope,
                "record_type": "decision",
                "content": "Keep benchmark memory isolated and replay-safe.",
                "idempotency_key": "ergonomics-first-write",
            }
            if mode == "strict":
                async with Client(server) as probe:
                    rejected = await probe.call_tool(
                        "memory_store", arguments, raise_on_error=False
                    )
                    envelope = rejected.structured_content
                    if (
                        envelope is None
                        or envelope["ok"]
                        or envelope["error"]["code"] != "COMMUNION_REQUIRED"
                        or envelope["error"]["remedy"]["tool"] != "session_brief"
                    ):
                        raise RuntimeError(
                            "Strict direct write did not require briefing"
                        )
                server = create_v7_server(
                    "stdio", settings=settings, environ=environment
                )
            calls = 0
            async with Client(server) as client:

                async def invoke(tool: str, values: dict[str, Any]) -> dict[str, Any]:
                    nonlocal calls
                    calls += 1
                    result = await client.call_tool(tool, values, raise_on_error=False)
                    envelope = result.structured_content
                    if envelope is None:
                        raise RuntimeError(f"Missing benchmark response: {tool}")
                    return envelope

                if mode == "guided":
                    metrics["listed_tool_count"] = len(await client.list_tools())
                else:
                    brief = await invoke("session_brief", scope)
                    if not brief["ok"]:
                        raise RuntimeError("Strict benchmark briefing failed")
                    counsel = await invoke(
                        "memory_preflight",
                        {
                            **scope,
                            "target_tool": "memory_store",
                            "target_arguments": {
                                key: value
                                for key, value in arguments.items()
                                if key != "workspace_id"
                            },
                        },
                    )
                    if not counsel["ok"]:
                        raise RuntimeError("Strict benchmark counsel failed")
                    arguments["preflight_token"] = counsel["data"]["preflight_token"]
                stored = await invoke("memory_store", arguments)
                if not stored["ok"]:
                    raise RuntimeError(f"{mode} benchmark write failed")
                if mode == "guided" and not stored["meta"]["covenant"]["auto_brief"]:
                    raise RuntimeError("Guided benchmark did not brief automatically")
                metrics[
                    "guided_write_calls"
                    if mode == "guided"
                    else "strict_explicit_write_calls"
                ] = calls
                if mode == "guided":
                    preview = await invoke(
                        "daem0n_tool_call",
                        {**scope, "tool": "memory_prune_preview", "arguments": {}},
                    )
                    if not preview["ok"]:
                        raise RuntimeError("Destructive benchmark preview failed")
                    start = calls
                    challenge = await invoke(
                        "daem0n_tool_call",
                        {
                            **scope,
                            "tool": "memory_prune",
                            "arguments": {
                                "selection_token": preview["data"]["data"][
                                    "selection_token"
                                ]
                            },
                        },
                    )
                    if (
                        challenge["ok"]
                        or challenge["error"]["code"] != "COUNSEL_REQUIRED"
                        or "DESTRUCTIVE_OPERATION"
                        not in challenge["error"]["counsel"]["reasons"]
                    ):
                        raise RuntimeError("Destructive benchmark did not escalate")
                    remedy = challenge["error"]["remedy"]
                    retried = await invoke(remedy["tool"], remedy["arguments"])
                    if not retried["ok"]:
                        raise RuntimeError("Exact destructive benchmark retry failed")
                    metrics["destructive_challenge_calls"] = calls - start
    expected = {
        "listed_tool_count": 9,
        "guided_write_calls": 1,
        "strict_explicit_write_calls": 3,
        "destructive_challenge_calls": 2,
    }
    if metrics != expected:
        raise RuntimeError(
            f"Ergonomics contract changed: {metrics!r}, expected {expected!r}"
        )
    return metrics


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
    metrics.update(await _ergonomics_metrics())
    for name, value in metrics.items():
        if not isinstance(value, (int, float)) or not math.isfinite(value):
            raise RuntimeError(f"Invalid benchmark metric: {name}={value!r}")
    for name, value in metrics.items():
        print(f"METRIC {name}={value:.12f}", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
