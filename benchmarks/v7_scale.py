"""Reproducible synthetic scale fixture and real MCP lexical measurements.

Run from the repository with an installed package. This is development evidence,
not a substitute for frozen relevance judgments or final-commit certification.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import math
import os
import platform
import secrets
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

from daem0nmcp.database import DatabaseManager
from daem0nmcp.event_store import EventCommand, EventStore, deterministic_id
from daem0nmcp.retrieval.projections import LexicalProjectionBuilder
from daem0nmcp.storage_activation import resolve_active_database
from daem0nmcp.workspace import WorkspaceRegistry
from tests.api_v7.process_client import process_client, succeed

TOPICS = (
    "connection pooling database requests",
    "authentication token expiry rotation",
    "message queue retry idempotency",
    "cache invalidation deployment consistency",
    "file upload validation size limits",
    "background task cancellation recovery",
    "search index generation activation",
    "workspace authorization principal isolation",
)


def emit(value):
    print(json.dumps(value, sort_keys=True), flush=True)


def source_fingerprint() -> str:
    digest = hashlib.sha256()
    for path in sorted(Path("daem0nmcp").rglob("*.py")):
        digest.update(path.as_posix().encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()


def canonical_counts(root: Path, workspace_id: str) -> dict:
    active = resolve_active_database(root / ".daem0nmcp" / "storage")
    connection = sqlite3.connect(f"{active.path.as_uri()}?mode=ro", uri=True)
    try:
        connection.execute("BEGIN")
        return {
            "records": connection.execute(
                "SELECT COUNT(*) FROM memory_records WHERE workspace_id=?",
                (workspace_id,),
            ).fetchone()[0],
            "events": connection.execute(
                "SELECT COUNT(*) FROM memory_events WHERE workspace_id=?",
                (workspace_id,),
            ).fetchone()[0],
        }
    finally:
        connection.close()


async def write_visibility(session, scope: dict) -> dict:
    """Measure one real authorized write and autonomous projection convergence."""
    marker = "scalevisibility" + secrets.token_hex(12)
    arguments = {
        "record_type": "learning",
        "content": f"{marker} records the scale benchmark visibility probe.",
        "idempotency_key": "scale-visibility-" + marker,
    }
    preflight = await succeed(
        session,
        "memory_preflight",
        {
            **scope,
            "target_tool": "memory_store",
            "target_arguments": arguments,
            "description": "Measure new-write visibility in the synthetic scale fixture",
        },
    )
    started = time.perf_counter()
    stored = await succeed(
        session,
        "memory_store",
        {
            **scope,
            **arguments,
            "preflight_token": preflight["preflight_token"],
        },
    )
    committed = time.perf_counter()
    record_id = stored["record"]["record_id"]
    lexical_seconds = None
    specialized_seconds = None
    deadline = time.monotonic() + 90
    while time.monotonic() < deadline:
        if lexical_seconds is None:
            data = await succeed(
                session,
                "memory_recall",
                {
                    **scope,
                    "query": marker,
                    "limit": 5,
                    "rerank": False,
                },
            )
            if any(item["record"]["record_id"] == record_id for item in data["items"]):
                lexical_seconds = time.perf_counter() - committed
        if specialized_seconds is None:
            health = await succeed(session, "system_health", scope)
            rows = [
                row
                for row in health["runtime_diagnostics"]
                if row["component"] in {"temporal", "procedure", "outcome"}
            ]
            if len(rows) == 3 and all(
                row["status"] == "ready"
                and not row["counts"].get("pending_jobs")
                and not row["counts"].get("running_jobs")
                for row in rows
            ):
                specialized_seconds = time.perf_counter() - committed
        if lexical_seconds is not None and specialized_seconds is not None:
            break
        await asyncio.sleep(0.25)
    return {
        "write_response_seconds": committed - started,
        "lexical_seconds_after_write_response": lexical_seconds,
        "specialized_seconds_after_write_response": specialized_seconds,
        # The client cannot observe the commit instant. Request-to-observation
        # is a conservative upper bound; response-to-observation understates it.
        "lexical_seconds_from_write_request": (
            None if lexical_seconds is None else lexical_seconds + committed - started
        ),
        "specialized_seconds_from_write_request": (
            None
            if specialized_seconds is None
            else specialized_seconds + committed - started
        ),
        "visibility_target_basis": "write_request_to_observation_upper_bound",
        "meets_lexical_visibility_target": lexical_seconds is not None
        and lexical_seconds + committed - started <= 5,
        "meets_specialized_visibility_target": specialized_seconds is not None
        and specialized_seconds + committed - started <= 60,
        "dense": "not_enabled",
    }


async def seed(root: Path, records: int, versions: int) -> dict:
    root.mkdir(parents=True, exist_ok=False)
    storage = root / ".daem0nmcp" / "storage"
    manager = DatabaseManager(str(storage))
    await manager.init_db()
    await manager.close()
    workspace = WorkspaceRegistry([root], default_root=root).default
    started = time.perf_counter()
    connection = sqlite3.connect(storage / "daem0nmcp.db")
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA synchronous=FULL")
    store = EventStore(connection)
    base = time.time_ns() // 1000 - records * versions - 1
    try:
        for index in range(records):
            record_id = deterministic_id(
                "mem", "scale-fixture-v1", workspace.workspace_id, index
            )
            for version in range(1, versions + 1):
                state = {
                    "record_type": ("decision", "pattern", "warning", "learning")[
                        index % 4
                    ],
                    "legacy_type": None,
                    "content": f"Service component{index:06d}: {TOPICS[index % len(TOPICS)]}. Validate the operation before committing state and record the resulting decision.",
                    "rationale": f"Synthetic historical revision {version}; reproducible scale fixture.",
                    "context": {"fixture_version": 1, "component": index},
                    "tags": ["scale-fixture", f"domain-{index % 32}"],
                    "file_path": None,
                    "file_path_relative": None,
                    "keywords": None,
                    "is_permanent": False,
                    "pinned": False,
                    "archived": False,
                    "outcome": None,
                    "worked": None,
                    "recall_count": 0,
                    "surprise_score": None,
                    "importance_score": None,
                    "source_client": "scale-fixture",
                    "source_model": None,
                    "deleted_at_us": None,
                }
                timestamp = base + index * versions + version
                store.append_and_project(
                    EventCommand(
                        workspace_id=workspace.workspace_id,
                        stream_id=record_id,
                        stream_kind="memory",
                        event_type="memory.created"
                        if version == 1
                        else "memory.updated",
                        occurred_at_us=timestamp,
                        recorded_at_us=timestamp,
                        actor_type="import",
                        actor_id="scale-fixture-v1",
                        payload={"record": state},
                        expected_stream_version=version,
                    )
                )
            if (index + 1) % 1000 == 0:
                connection.commit()
                emit(
                    {
                        "phase": "seed",
                        "records": index + 1,
                        "events": (index + 1) * versions,
                        "elapsed_seconds": time.perf_counter() - started,
                    }
                )
        connection.commit()
        seeded = time.perf_counter()
        LexicalProjectionBuilder(connection).rebuild(workspace.workspace_id)
        connection.commit()
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        connection.close()
    result = {
        "fixture": "synthetic-scale-v1",
        "workspace_id": workspace.workspace_id,
        "records": records,
        "events": records * versions,
        "seed_seconds": seeded - started,
        "initial_lexical_index_seconds": time.perf_counter() - seeded,
        "database_bytes": (storage / "daem0nmcp.db").stat().st_size,
    }
    (root / "scale-fixture.json").write_text(
        json.dumps(result, indent=2), encoding="utf-8"
    )
    return result


def host_snapshot() -> dict:
    """Optional host context, without collecting process names or credentials."""
    try:
        import psutil
    except ImportError:
        return {"available": False}
    memory = psutil.virtual_memory()
    return {
        "available": True,
        "available_memory_bytes": memory.available,
        "memory_percent": memory.percent,
        "cpu_percent": psutil.cpu_percent(interval=0.1),
    }


def latency_summary(samples: list[float]) -> dict:
    ordered = sorted(samples)
    p95 = ordered[math.ceil(len(ordered) * 0.95) - 1] if ordered else None
    return {
        "requests": len(samples),
        "p50_seconds": (
            ordered[math.ceil(len(ordered) * 0.50) - 1] if ordered else None
        ),
        "p95_seconds": p95,
        "max_seconds": ordered[-1] if ordered else None,
        "lexical_target_seconds": 0.5,
        "meets_lexical_target": p95 is not None and p95 <= 0.5,
    }


async def _measure_phases(
    root: Path, transport: str, rounds: int, result: dict, samples: list[float]
) -> None:
    scope = {"workspace_id": result["fixture"]["workspace_id"]}
    result["phase"] = "startup"
    startup = time.perf_counter()
    async with process_client(
        root, transport, environment_overrides={"DAEM0NMCP_DREAM_ENABLED": "false"}
    ) as session:
        await succeed(session, "session_brief", scope)
        result["startup_seconds"] = time.perf_counter() - startup
        result["phase"] = "projection_convergence"
        deadline = time.monotonic() + 600
        while True:
            health = await succeed(session, "system_health", scope)
            rows = [
                row
                for row in health["runtime_diagnostics"]
                if row["component"] in {"lexical", "temporal", "procedure", "outcome"}
            ]
            if len(rows) == 4 and all(
                row["status"] == "ready"
                and not row["counts"].get("pending_jobs")
                and not row["counts"].get("running_jobs")
                for row in rows
            ):
                break
            if time.monotonic() >= deadline:
                raise RuntimeError("core projections did not converge")
            await asyncio.sleep(1)

        async def recall(index: int, timed: bool):
            started = time.perf_counter()
            data = await succeed(
                session,
                "memory_recall",
                {
                    **scope,
                    "query": TOPICS[index % len(TOPICS)],
                    "limit": 10,
                    "rerank": False,
                },
            )
            elapsed = time.perf_counter() - started
            if not data["items"]:
                raise RuntimeError("scale recall returned no evidence")
            if timed:
                samples.append(elapsed)

        for batch in range(5 + rounds):
            result["phase"] = "warmup" if batch < 5 else "warm_recall"
            completed = await asyncio.gather(
                *(recall(batch * 4 + index, batch >= 5) for index in range(4)),
                return_exceptions=True,
            )
            for outcome in completed:
                if isinstance(outcome, BaseException):
                    raise outcome
        result.update(latency_summary(samples))
        emit({"phase": "warm_recall_complete", **latency_summary(samples)})
        result["phase"] = "write_visibility"
        result["new_write_visibility"] = await write_visibility(session, scope)
        result["phase"] = "shutdown"


async def measure(root: Path, transport: str, rounds: int) -> dict:
    initial_source_hash = source_fingerprint()
    fixture = json.loads((root / "scale-fixture.json").read_text(encoding="utf-8"))
    samples: list[float] = []
    run_id = f"{time.time_ns()}-{secrets.token_hex(4)}"
    result = {
        "certified_release": False,
        "run_id": run_id,
        "status": "incomplete",
        "phase": "inventory",
        "base_commit": subprocess.check_output(
            ["git", "rev-parse", "HEAD"], text=True
        ).strip(),
        "source_sha256": initial_source_hash,
        "harness_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "python": sys.version,
        "platform": platform.platform(),
        "processor": platform.processor(),
        "logical_cpu_count": os.cpu_count(),
        "transport": transport,
        "concurrency": 4,
        "fixture": fixture,
        "host_before": host_snapshot(),
        "startup_seconds": None,
        "new_write_visibility": None,
    }
    try:
        result["canonical_counts_before"] = canonical_counts(
            root, fixture["workspace_id"]
        )
        await _measure_phases(root, transport, rounds, result, samples)
        result["status"] = "completed"
        result["phase"] = "complete"
        return result
    except BaseException as exc:
        # Keep provider messages and request data out of the evidence bundle.
        result["failure_type"] = type(exc).__name__
        raise
    finally:
        result.update(latency_summary(samples))
        result["warm_recall_complete"] = len(samples) == rounds * 4
        if not result["warm_recall_complete"]:
            result["meets_lexical_target"] = False
        result["source_unchanged_during_run"] = (
            initial_source_hash == source_fingerprint()
        )
        result["host_after"] = host_snapshot()
        try:
            result["canonical_counts_after"] = canonical_counts(
                root, fixture["workspace_id"]
            )
        except Exception as exc:
            result["counts_after_failure_type"] = type(exc).__name__
        payload = json.dumps(result, indent=2)
        # Retain every attempt, including failures, rather than replacing the
        # only evidence file after each run. The conventional path is latest.
        (root / f"scale-measure-{transport}-{run_id}.json").write_text(
            payload, encoding="utf-8"
        )
        (root / f"scale-measure-{transport}.json").write_text(payload, encoding="utf-8")
        emit(result)


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("seed", "measure"))
    parser.add_argument("workspace", type=Path)
    parser.add_argument("--records", type=int, default=100_000)
    parser.add_argument("--versions", type=int, default=10)
    parser.add_argument("--rounds", type=int, default=50)
    parser.add_argument(
        "--transport", choices=("stdio", "streamable-http"), default="stdio"
    )
    args = parser.parse_args()
    if (
        not 1 <= args.records <= 100_000
        or not 1 <= args.versions <= 10
        or not 1 <= args.rounds <= 1000
    ):
        parser.error("record/version/round bounds exceeded")
    root = args.workspace.resolve()
    result = (
        await seed(root, args.records, args.versions)
        if args.action == "seed"
        else await measure(root, args.transport, args.rounds)
    )
    if args.action == "seed":
        destination = root / f"scale-{args.action}-{args.transport}.json"
        destination.write_text(json.dumps(result, indent=2), encoding="utf-8")
    emit(result)


if __name__ == "__main__":
    asyncio.run(main())
