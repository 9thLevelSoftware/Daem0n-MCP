"""Graded coding-memory evaluation using canonical writes and production recall."""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import random
import shutil
import sqlite3
import tempfile
from contextlib import closing
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from time import perf_counter_ns
from typing import Any, Literal

from benchmarks.retrieval_benchmark import calculate_ranking_metrics
from daem0nmcp.api.v7.discovery_operations import default_code_indexer_factory
from daem0nmcp.api.v7.models import RetrievalData
from daem0nmcp.api.v7.pinned import MemoryOutcomeCommand, MemoryStoreCommand
from daem0nmcp.api.v7.runtime_services import (
    SQLiteMemoryEventWriter,
    Task8RecallService,
)
from daem0nmcp.capabilities import CapabilityRegistry
from daem0nmcp.config import Settings
from daem0nmcp.migrations.schema import MIGRATIONS
from daem0nmcp.retrieval.runtime import drain_projection_jobs
from daem0nmcp.retrieval.types import RetrievalQuery
from daem0nmcp.schema_version import CURRENT_SCHEMA_VERSION
from daem0nmcp.storage_activation import ActiveDatabasePointer, write_active_pointer
from daem0nmcp.workspace import Workspace, WorkspaceRegistry

Mode = Literal["lexical_only", "fully_enabled"]
_START = datetime(2026, 1, 1, tzinfo=timezone.utc)
_RETENTION_NOTE_TAG_COUNT = 16
_UTILITY_WEIGHT = 0.2


@dataclass(frozen=True)
class _Store:
    key: str
    content: str
    record_type: str = "observation"
    informed_by: tuple[str, ...] = ()
    steps: tuple[str, ...] = ()
    code_refs: tuple[tuple[str, str | None], ...] = ()
    tags: tuple[str, ...] = ()


@dataclass(frozen=True)
class _Outcome:
    key: str
    record_key: str
    worked: bool


@dataclass(frozen=True)
class _Query:
    key: str
    family: str
    text: str
    relevant: tuple[tuple[str, int], ...]
    required_fact: str | None = None


@dataclass
class _Corpus:
    writes: list[_Store | _Outcome] = field(default_factory=list)
    queries: list[_Query] = field(default_factory=list)
    bindings: dict[str, tuple[bool, bool]] = field(default_factory=dict)


class _Clock:
    def __init__(self) -> None:
        self.value = _START

    def __call__(self) -> datetime:
        value = self.value
        self.value += timedelta(seconds=1)
        return value


def _workspace(temporary: Path) -> tuple[Workspace, Path]:
    root = temporary / "workspace"
    root.mkdir()
    storage = root / ".daem0nmcp" / "storage"
    storage.mkdir(parents=True)
    database = storage / "daem0nmcp.db"
    migrations = {version: statements for version, _, statements in MIGRATIONS}
    with closing(sqlite3.connect(database)) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("CREATE TABLE schema_version (version INTEGER PRIMARY KEY)")
        for version in range(16, CURRENT_SCHEMA_VERSION + 1):
            for statement in migrations[version]:
                connection.execute(statement)
            connection.execute("INSERT INTO schema_version VALUES (?)", (version,))
        connection.commit()
    write_active_pointer(
        storage, ActiveDatabasePointer(7, 1, database.name, None, None)
    )
    return WorkspaceRegistry([root], default_root=root).default, database


def _corpus(root: Path, topics: int, symbols_available: bool) -> _Corpus:
    corpus = _Corpus()
    (root / "src").mkdir()
    (root / "docs").mkdir()
    for t in range(topics):
        component = (
            "cache",
            "session",
            "webhook",
            "scheduler",
            "parser",
            "migration",
            "exporter",
            "indexer",
            "uploader",
            "notifier",
            "throttle",
            "ledger",
        )[t % 12]
        problem = ("timeout", "deadlock", "overflow", "drift", "leak", "race")[
            (t // 12) % 6
        ]
        bad, good = f"reuse-{t}-bad", f"reuse-{t}-good"
        corpus.writes.extend(
            (
                _Store(
                    bad,
                    f"{component} {problem} fix: retry the {component} {problem} fix with a longer timeout.",
                    "decision",
                ),
                _Store(
                    good,
                    f"{component} {problem} fix: invalidate the stale entry before the write completes.",
                    "decision",
                ),
            )
        )
        for worked, source in ((True, good), (False, bad)):
            for k in range(3):
                key = f"reuse-{t}-{worked}-{k}"
                corpus.writes.append(
                    _Store(
                        key,
                        f"Applied guidance for ticket {t}-{k}",
                        "decision",
                        (source,),
                    )
                )
                corpus.writes.append(_Outcome(f"{key}-outcome", key, worked))
        corpus.queries.append(
            _Query(
                f"reuse-{t}",
                "outcome_reuse",
                f"{component} {problem} fix",
                ((good, 3), (bad, 1)),
            )
        )

        component = ("router", "catalog", "billing", "gateway", "renderer", "sandbox")[
            t % 6
        ]
        problem = ("quota", "encoding", "latency", "rollback")[(t // 6) % 4]
        background, distractor, learning = (
            f"chain-{t}-background",
            f"chain-{t}-draft",
            f"chain-{t}-learning",
        )
        corpus.writes.extend(
            (
                _Store(
                    background,
                    f"{component} {problem} background: the {component} owner documented the {problem} limits.",
                ),
                _Store(
                    distractor,
                    f"{component} {problem} background {component} {problem} background notes from an unrelated draft.",
                ),
                _Store(
                    learning, f"Summary for ticket chain-{t}", "learning", (background,)
                ),
            )
        )
        for k in range(3):
            key = f"chain-{t}-{k}"
            corpus.writes.append(
                _Store(key, f"Shipped change chain-{t}-{k}", "decision", (learning,))
            )
            corpus.writes.append(_Outcome(f"{key}-outcome", key, True))
        corpus.queries.append(
            _Query(
                f"chain-{t}",
                "provenance_chain",
                f"{component} {problem} background",
                ((background, 3), (distractor, 1)),
            )
        )

        source = f"src/topic_{t}.py"
        document = f"docs/topic_{t}.md"
        (root / source).write_text(
            f"def handle_topic_{t}(value):\n    return value + {t}\n\n\ndef helper_topic_{t}(value):\n    return value * {t}\n",
            encoding="utf-8",
            newline="\n",
        )
        (root / document).write_text(
            f"topic {t} runbook\n", encoding="utf-8", newline="\n"
        )
        if symbols_available:
            key = f"validity-{t}-symbol"
            corpus.writes.append(
                _Store(
                    key,
                    f"handle_topic_{t} handler update",
                    code_refs=((source, f"handle_topic_{t}"),),
                )
            )
            corpus.bindings[key] = (t % 2 == 0, True)
            corpus.queries.append(
                _Query(key, "validity", f"handle_topic_{t} handler update", ((key, 3),))
            )
        key = f"validity-{t}-file"
        corpus.writes.append(
            _Store(key, f"topic {t} runbook pointer", code_refs=((document, None),))
        )
        corpus.bindings[key] = (t % 3 == 0, False)
        corpus.queries.append(
            _Query(key, "validity", f"topic {t} runbook pointer", ((key, 3),))
        )

        component = ("release", "canary", "schemas", "toggles", "bundles", "workers")[
            t % 6
        ]
        procedure = f"retention-{t}-procedure"
        steps = (
            f"freeze {component} writes",
            f"apply {component} rollout",
            f"verify {component} health",
        )
        corpus.writes.append(
            _Store(procedure, f"deploy {component} rollout", "procedure", steps=steps)
        )
        for k in range(4):
            # Task tags stress weighted BM25; same-category background drafts
            # prevent category diversity from trivially rescuing the runbook.
            # Empty steps are valid canonical input, not actionable guidance.
            tags = tuple(
                f"deploy-{component}-rollout-task-{k}-{index}"
                for index in range(_RETENTION_NOTE_TAG_COUNT)
            )
            corpus.writes.append(
                _Store(
                    f"retention-{t}-note-{k}",
                    f"deploy {component} rollout deploy {component} rollout "
                    + "background detail " * 60,
                    "procedure",
                    tags=tags,
                )
            )
        corpus.queries.append(
            _Query(
                f"retention-{t}",
                "retention",
                f"deploy {component} rollout",
                ((procedure, 3),),
                steps[0],
            )
        )
    return corpus


async def _seed(
    writer: SQLiteMemoryEventWriter, workspace: Workspace, corpus: _Corpus, seed: int
) -> dict[str, str]:
    pending = list(corpus.writes)
    random.Random(seed).shuffle(pending)
    record_ids: dict[str, str] = {}
    while pending:
        remaining: list[_Store | _Outcome] = []
        for item in pending:
            dependencies = (
                item.informed_by if isinstance(item, _Store) else (item.record_key,)
            )
            if any(key not in record_ids for key in dependencies):
                remaining.append(item)
                continue
            if isinstance(item, _Store):
                result = await writer.store(
                    workspace,
                    MemoryStoreCommand(
                        record_type=item.record_type,
                        content=item.content,
                        rationale=None,
                        context={},
                        tags=item.tags,
                        relative_file_path=None,
                        happened_at=None,
                        procedure_steps=item.steps,
                        idempotency_key=f"coding-eval-{item.key}",
                        informed_by=tuple(record_ids[key] for key in item.informed_by),
                        code_refs=item.code_refs,
                    ),
                )
                record_ids[item.key] = result.record.record_id
            else:
                await writer.record_outcome(
                    workspace,
                    MemoryOutcomeCommand(
                        record_id=record_ids[item.record_key],
                        outcome_text="Verified episode outcome",
                        worked=item.worked,
                        happened_at=None,
                        idempotency_key=f"coding-eval-{item.key}",
                        verification={
                            "kind": "test",
                            "exit_code": 0 if item.worked else 1,
                        },
                    ),
                )
        if len(remaining) == len(pending):
            raise RuntimeError(
                "coding evaluation corpus contains unresolved dependencies"
            )
        pending = remaining
    return record_ids


def _edit_bindings(root: Path, topics: int) -> None:
    for t in range(topics):
        path = root / "src" / f"topic_{t}.py"
        source = path.read_text(encoding="utf-8")
        source = (
            source.replace(f"+ {t}", f"- {t}")
            if t % 2 == 0
            else source.replace(f"* {t}", f"** {t}")
        )
        path.write_text(source, encoding="utf-8", newline="\n")
        if t % 3 == 0:
            document = root / "docs" / f"topic_{t}.md"
            document.write_text(
                document.read_text(encoding="utf-8") + "updated runbook\n",
                encoding="utf-8",
                newline="\n",
            )


def _arms(mode: Mode, statuses: dict[str, str]) -> dict[str, dict[str, Any]]:
    defaults: dict[str, Any] = {
        "retrieval_utility_mode": "off",
        "retrieval_utility_credit": "trace",
        "retrieval_utility_weight": _UTILITY_WEIGHT,
        "memory_validity_mode": "off",
        "retrieval_retention_mode": "off",
        "retrieval_rerank_enabled": False,
        "retrieval_reranker": "embedding",
    }
    overrides: dict[str, dict[str, Any]] = {
        "baseline": {},
        "utility_single": {
            "retrieval_utility_mode": "apply",
            "retrieval_utility_credit": "single_step",
        },
        "utility_trace": {"retrieval_utility_mode": "apply"},
        "validity_apply": {"memory_validity_mode": "apply"},
        "retention_apply": {"retrieval_retention_mode": "apply"},
        "all_apply": {
            "retrieval_utility_mode": "apply",
            "memory_validity_mode": "apply",
            "retrieval_retention_mode": "apply",
        },
    }
    if mode == "fully_enabled":
        overrides["rerank_embedding"] = {"retrieval_rerank_enabled": True}
        if statuses.get("late-interaction") == "ready":
            overrides["rerank_late_interaction"] = {
                "retrieval_rerank_enabled": True,
                "retrieval_reranker": "late_interaction",
            }
    return {name: defaults | override for name, override in overrides.items()}


def _settings(workspace: Workspace, overrides: dict[str, Any]) -> Settings:
    # Ignore ambient tool/server settings, especially remote Qdrant endpoints.
    defaults = {
        name: item.get_default(call_default_factory=True)
        for name, item in Settings.model_fields.items()
    }
    return Settings(
        _env_file=None,
        **(
            defaults
            | {
                "project_root": str(workspace.root),
                "workspace_roots": [str(workspace.root)],
                "qdrant_path": str(
                    workspace.root / ".daem0nmcp" / "storage" / "qdrant"
                ),
            }
            | overrides
        ),
    )


def _flag_metrics(observations: list[tuple[bool, bool]]) -> dict[str, float | None]:
    stale = [flag for expected, flag in observations if expected]
    current = [flag for expected, flag in observations if not expected]
    return {
        "stale_flag_recall": sum(stale) / len(stale) if stale else None,
        "false_flag_rate": sum(current) / len(current) if current else None,
    }


def _percentile(values: list[float], quantile: float) -> float:
    ordered = sorted(values)
    position = (len(ordered) - 1) * quantile
    low, high = math.floor(position), math.ceil(position)
    return ordered[low] + (ordered[high] - ordered[low]) * (position - low)


def _metrics(
    corpus: _Corpus,
    record_ids: dict[str, str],
    results: dict[str, RetrievalData],
    latencies: list[float],
    symbols_available: bool,
) -> dict[str, Any]:
    queries = [
        {
            "query_id": query.key,
            "expected_relevant": [
                {"record_id": record_ids[key], "grade": grade}
                for key, grade in query.relevant
            ],
        }
        for query in corpus.queries
    ]
    ranking_results = {
        key: {"returned_record_ids": [item.record.record_id for item in result.items]}
        for key, result in results.items()
    }
    overall = calculate_ranking_metrics(queries, ranking_results)
    ndcg = {"overall": overall["ndcg_at_10"]}
    for family in ("outcome_reuse", "provenance_chain"):
        keys = {query.key for query in corpus.queries if query.family == family}
        selected = [query for query in queries if query["query_id"] in keys]
        ndcg[family] = calculate_ranking_metrics(
            selected, {key: ranking_results[key] for key in keys}
        )["ndcg_at_10"]
    bindings = {record_ids[key]: value for key, value in corpus.bindings.items()}
    flags: list[tuple[bool, bool]] = []
    symbol_flags: list[tuple[bool, bool]] = []
    for query in corpus.queries:
        if query.family != "validity":
            continue
        for item in results[query.key].items:
            binding = bindings.get(item.record.record_id)
            if binding is None or item.applicability is None:
                continue
            observation = (binding[0], item.applicability == "needs_revalidation")
            flags.append(observation)
            if binding[1]:
                symbol_flags.append(observation)
    validity: dict[str, Any] = _flag_metrics(flags)
    validity["symbol"] = _flag_metrics(symbol_flags) if symbols_available else None
    retention = [query for query in corpus.queries if query.family == "retention"]
    retained = sum(
        query.required_fact in (results[query.key].rendered_context or "")
        for query in retention
    )
    return {
        "ndcg_at_10": ndcg,
        "mrr_at_10": {"overall": overall["mrr_at_10"]},
        "validity": validity,
        "retention": {"required_fact_retention": retained / len(retention)},
        "tokens": {
            "rendered_mean": sum(
                result.token_usage.rendered for result in results.values()
            )
            / len(results)
        },
        "latency_ms": {
            "p50": _percentile(latencies, 0.5),
            "p95": _percentile(latencies, 0.95),
        },
    }


async def run_evaluation(
    *, mode: Mode = "lexical_only", topics: int = 24, seed: int = 20261004
) -> dict[str, Any]:
    if mode not in {"lexical_only", "fully_enabled"}:
        raise ValueError("unknown coding evaluation mode")
    if isinstance(topics, bool) or not isinstance(topics, int) or topics < 1:
        raise ValueError("topics must be a positive integer")
    if isinstance(seed, bool) or not isinstance(seed, int):
        raise ValueError("seed must be an integer")
    statuses = (
        {
            "local": "disabled",
            "models-local": "disabled",
            "graph": "disabled",
            "late-interaction": "disabled",
        }
        if mode == "lexical_only"
        else {
            name: value["status"] for name, value in CapabilityRegistry().all().items()
        }
    )
    arms = _arms(mode, statuses)
    symbols_available = bool(
        getattr(default_code_indexer_factory(), "available", False)
    )
    # Stable registry-derived IDs also stabilize real provider score ties. An
    # exclusive mkdir fails closed rather than touching any existing fixture.
    temporary = (
        Path(tempfile.gettempdir()) / f"daem0nmcp-coding-eval-{mode}-{topics}-{seed}"
    )
    temporary.mkdir()
    try:
        workspace, database = _workspace(temporary)
        corpus = _corpus(workspace.root, topics, symbols_available)
        clock = _Clock()
        writer = SQLiteMemoryEventWriter(
            clock=clock, projection_scheduler=lambda path: None
        )
        try:
            record_ids = await _seed(writer, workspace, corpus, seed)
        finally:
            writer.close()
        projection_settings = _settings(workspace, arms["baseline"])
        while await drain_projection_jobs(
            database,
            config=projection_settings,
            max_jobs=100,
            include_optional=True,
            capability_statuses=statuses,
        ):
            pass
        _edit_bindings(workspace.root, topics)
        report: dict[str, Any] = {
            "metadata": {
                "mode": mode,
                "topics": topics,
                "seed": seed,
                "schema_version": CURRENT_SCHEMA_VERSION,
                "capability_statuses": statuses,
                "symbol_bindings_available": symbols_available,
                "retention_note_tag_count": _RETENTION_NOTE_TAG_COUNT,
                "retention_background_note_record_type": "procedure",
                "retention_stress_reason": "same-category background drafts prevent category-diversity rescue of actionable steps",
                "utility_weight": _UTILITY_WEIGHT,
                "arm_overrides": arms,
                "workspace_identity": "exclusive_deterministic_scratch_registry",
            },
            "arms": {},
        }
        snapshot = clock.value
        for name, overrides in arms.items():
            service = Task8RecallService(
                config=_settings(workspace, overrides), capability_statuses=statuses
            )
            results: dict[str, RetrievalData] = {}
            latencies: list[float] = []
            try:
                for query in corpus.queries:
                    started = perf_counter_ns()
                    results[query.key] = await service.retrieve(
                        workspace,
                        RetrievalQuery(
                            workspace_id=workspace.workspace_id,
                            text=query.text,
                            as_of_valid_time=snapshot,
                            as_of_transaction_time=snapshot,
                            token_budget=256 if query.family == "retention" else 2400,
                            intent="implement" if query.family == "retention" else None,
                            rerank=overrides["retrieval_rerank_enabled"],
                        ),
                        frozenset(),
                    )
                    latencies.append((perf_counter_ns() - started) / 1_000_000)
            finally:
                service.close()
            report["arms"][name] = _metrics(
                corpus, record_ids, results, latencies, symbols_available
            )
        return report
    finally:
        shutil.rmtree(temporary)


def _positive_topics(value: str) -> int:
    topics = int(value)
    if topics < 1:
        raise argparse.ArgumentTypeError("topics must be positive")
    return topics


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--mode", choices=("lexical_only", "fully_enabled"), default="lexical_only"
    )
    parser.add_argument("--topics", type=_positive_topics, default=24)
    parser.add_argument("--seed", type=int, default=20261004)
    parser.add_argument("--output", type=Path, required=True)
    arguments = parser.parse_args(argv)
    report = asyncio.run(
        run_evaluation(
            mode=arguments.mode, topics=arguments.topics, seed=arguments.seed
        )
    )
    arguments.output.parent.mkdir(parents=True, exist_ok=True)
    arguments.output.write_text(
        json.dumps(report, sort_keys=True, indent=2, allow_nan=False) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
