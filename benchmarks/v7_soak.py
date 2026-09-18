"""Bounded mixed-workload MCP soak with process resource samples.

Requires the development-only psutil package. Run a short smoke before an
eight-hour run. Results are development evidence, never automatic certification.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import statistics
import time
from collections import deque
from datetime import datetime, timezone
from pathlib import Path

from benchmarks.v7_scale import TOPICS, source_fingerprint
from daem0nmcp.workspace import WorkspaceRegistry
from tests.api_v7.process_client import process_client, succeed


def resources() -> dict:
    import psutil

    rss = threads = handles = alive = 0
    for process in psutil.Process().children(recursive=True):
        try:
            rss += process.memory_info().rss
            threads += process.num_threads()
            counter = getattr(process, "num_handles", None) or process.num_fds
            handles += counter()
            alive += 1
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return {
        "processes": alive,
        "rss_bytes": rss,
        "threads": threads,
        "handles_or_fds": handles,
    }


async def run(root: Path, hours: float, transport: str, interval: float) -> dict:
    root.mkdir(parents=True, exist_ok=True)
    workspace = WorkspaceRegistry([root], default_root=root).default
    scope = {"workspace_id": workspace.workspace_id}
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    output = root / f"soak-{transport}-{run_id}.jsonl"
    source_hash = source_fingerprint()
    start = time.monotonic()
    deadline = start + hours * 3600
    latencies: deque[float] = deque(maxlen=4096)
    counts = {
        "recalls": 0,
        "writes": 0,
        "outcomes": 0,
        "resources": 0,
        "health": 0,
        "briefings": 0,
    }
    samples = []

    with output.open("x", encoding="utf-8") as handle:

        def emit(row):
            handle.write(json.dumps(row, sort_keys=True) + "\n")
            handle.flush()

        emit(
            {
                "kind": "start",
                "hours_requested": hours,
                "transport": transport,
                "source_sha256": source_hash,
                "profile": "core",
                "concurrency": 4,
                "certified_release": False,
            }
        )
        try:
            async with process_client(
                root,
                transport,
                environment_overrides={
                    "DAEM0NMCP_DREAM_ENABLED": "false",
                },
            ) as session:
                await succeed(session, "session_brief", scope)
                counts["briefings"] += 1

                # A new workspace gets one real authorized seed; existing scale
                # workspaces retain their authoritative history unchanged here.
                async def write(index):
                    arguments = {
                        "record_type": "decision",
                        "content": f"{TOPICS[index % len(TOPICS)]}: soak observation {run_id} {index}.",
                        "idempotency_key": f"soak-{run_id}-{index:08d}",
                    }
                    token = await succeed(
                        session,
                        "memory_preflight",
                        {
                            **scope,
                            "target_tool": "memory_store",
                            "target_arguments": arguments,
                            "description": "Record synthetic soak observation",
                        },
                    )
                    stored = await succeed(
                        session,
                        "memory_store",
                        {
                            **scope,
                            **arguments,
                            "preflight_token": token["preflight_token"],
                        },
                    )
                    counts["writes"] += 1
                    await succeed(
                        session,
                        "memory_record_outcome",
                        {
                            **scope,
                            "record_id": stored["record"]["record_id"],
                            "outcome_text": "Synthetic soak operation completed",
                            "worked": True,
                            "idempotency_key": f"soak-outcome-{run_id}-{index:08d}",
                        },
                    )
                    counts["outcomes"] += 1

                await write(0)
                next_write = time.monotonic() + 60
                next_brief = time.monotonic() + 300
                next_sample = time.monotonic()
                batch = 0

                async def recall(index):
                    tick = time.monotonic()
                    result = await succeed(
                        session,
                        "memory_recall",
                        {
                            **scope,
                            "query": TOPICS[index % len(TOPICS)],
                            "limit": 10,
                            "rerank": False,
                        },
                    )
                    if result["abstention_reason"] not in {None, "NO_CANDIDATES"}:
                        raise RuntimeError("soak recall failed its evidence policy")
                    latencies.append(time.monotonic() - tick)
                    counts["recalls"] += 1

                while time.monotonic() < deadline:
                    await asyncio.gather(*(recall(batch * 4 + i) for i in range(4)))
                    batch += 1
                    now = time.monotonic()
                    if now >= next_brief:
                        await succeed(session, "session_brief", scope)
                        counts["briefings"] += 1
                        next_brief = now + 300
                    if now >= next_write:
                        await write(batch)
                        next_write = now + 60
                    if now >= next_sample:
                        health = await succeed(session, "system_health", scope)
                        counts["health"] += 1
                        result = await session.read_resource(
                            f"memory://workspaces/{workspace.workspace_id}/warnings"
                        )
                        if not result.contents:
                            raise RuntimeError("soak resource returned no payload")
                        counts["resources"] += 1
                        ordered = sorted(latencies)
                        row = {
                            "kind": "sample",
                            "elapsed_seconds": now - start,
                            "counts": dict(counts),
                            "resource_usage": resources(),
                            "window_p95_seconds": ordered[
                                math.ceil(len(ordered) * 0.95) - 1
                            ],
                            "projections": [
                                r
                                for r in health["runtime_diagnostics"]
                                if r["component"]
                                in {"lexical", "temporal", "procedure", "outcome"}
                            ],
                        }
                        samples.append(row)
                        emit(row)
                        next_sample = now + 60
                    await asyncio.sleep(
                        min(interval, max(0, deadline - time.monotonic()))
                    )
        except BaseException:
            emit(
                {
                    "kind": "failed",
                    "elapsed_seconds": time.monotonic() - start,
                    "counts": counts,
                }
            )
            raise
        result = {
            "kind": "complete",
            "elapsed_seconds": time.monotonic() - start,
            "counts": counts,
            "certified_release": False,
            "source_unchanged_during_run": source_hash == source_fingerprint(),
            "eight_hours_completed": hours >= 8 and time.monotonic() - start >= 28_800,
            "post_shutdown_resources": resources(),
            "sample_count": len(samples),
            "median_sample_rss_bytes": statistics.median(
                r["resource_usage"]["rss_bytes"] for r in samples
            )
            if samples
            else None,
        }
        emit(result)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("workspace", type=Path)
    parser.add_argument("--hours", type=float, default=8)
    parser.add_argument(
        "--transport", choices=("stdio", "streamable-http"), default="stdio"
    )
    parser.add_argument("--interval", type=float, default=1)
    args = parser.parse_args()
    if not 0.001 <= args.hours <= 24 or not 0.1 <= args.interval <= 60:
        parser.error("hours or interval outside benchmark bounds")
    print(
        json.dumps(
            asyncio.run(
                run(args.workspace.resolve(), args.hours, args.transport, args.interval)
            ),
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
