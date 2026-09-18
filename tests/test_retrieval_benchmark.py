"""Deterministic, dependency-free retrieval benchmark contract tests."""

from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import shutil
import sqlite3
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from typing import Any

from benchmarks.retrieval_benchmark import (
    BenchmarkInputError,
    FixtureValidationError,
    calculate_quality_metrics,
    calculate_ranking_metrics,
    load_retrieval_fixtures,
    main,
    run_benchmark,
    serialize_benchmark_report,
)

FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "retrieval"
FIXTURE_FILES = ("events.jsonl", "qdrant_fake.py", "queries.jsonl", "records.jsonl")
EXPECTED_FIXTURE_DIGEST = (
    "7aa535e9a2d80add4e179d70bade096600d09e2039c320c3e4bf466df412d269"
)


def _raw_fixture_digest(root: Path) -> str:
    """Independent literal implementation of the documented byte contract."""
    digest = hashlib.sha256()
    for name in FIXTURE_FILES:
        payload = (root / name).read_bytes().replace(b"\r\n", b"\n")
        digest.update(name.encode("utf-8"))
        digest.update(b"\0")
        digest.update(payload)
        digest.update(b"\0")
    return digest.hexdigest()


def _result(
    returned: list[str],
    *,
    citations: list[str] | None = None,
    statuses: dict[str, str] | None = None,
    abstained: bool = False,
    tokens: int = 0,
    providers: dict[str, str] | None = None,
    timings: dict[str, int] | None = None,
) -> dict[str, Any]:
    return {
        "abstained": abstained,
        "citation_record_ids": citations if citations is not None else returned,
        "citation_statuses": statuses or {},
        "provider_statuses": providers or {"lexical": "ready"},
        "provider_timings_ns": timings or {"lexical": 7},
        "rendered_tokens": tokens,
        "returned_record_ids": returned,
    }


def _constant_retriever(result: dict[str, Any]):
    def retrieve(query: dict[str, Any]) -> dict[str, Any]:
        return result

    return retrieve


def _rewrite_jsonl_row(path: Path, index: int, value: dict[str, Any]) -> None:
    lines = path.read_text(encoding="utf-8").splitlines()
    lines[index] = json.dumps(value, sort_keys=True, separators=(",", ":"))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")


def _rehash_event(event: dict[str, Any]) -> None:
    columns = {
        key: event[key]
        for key in (
            "actor_id",
            "actor_type",
            "causation_event_id",
            "correlation_id",
            "event_schema_version",
            "event_type",
            "occurred_at_us",
            "payload_hash",
            "previous_event_hash",
            "recorded_at_us",
            "stream_id",
            "stream_kind",
            "stream_version",
            "workspace_id",
        )
    }
    encoded = json.dumps(columns, sort_keys=True, separators=(",", ":")).encode()
    event["event_hash"] = hashlib.sha256(encoded).hexdigest()
    event["event_id"] = f"evt_{event['event_hash']}"


class RetrievalFixtureTests(unittest.TestCase):
    def test_loader_accepts_complete_twelve_record_and_query_corpus(self) -> None:
        fixtures = load_retrieval_fixtures(FIXTURE_ROOT)

        self.assertEqual(12, len(fixtures.records))
        self.assertEqual(18, len(fixtures.events))
        self.assertEqual(12, len(fixtures.queries))
        self.assertEqual(_raw_fixture_digest(FIXTURE_ROOT), fixtures.digest)
        self.assertEqual(EXPECTED_FIXTURE_DIGEST, fixtures.digest)
        self.assertRegex(fixtures.digest, r"^[0-9a-f]{64}$")

        traits = [
            trait for record in fixtures.records for trait in record["fixture_traits"]
        ]
        self.assertEqual(1, traits.count("lexical_synonym_miss"))
        self.assertEqual(1, traits.count("dense_semantic_match"))
        self.assertEqual(1, traits.count("tag_match"))
        self.assertEqual(1, traits.count("rationale_match"))
        self.assertEqual(1, traits.count("archived"))
        self.assertEqual(2, traits.count("duplicate_content"))
        self.assertEqual(1, traits.count("current_fact"))
        self.assertEqual(1, traits.count("superseded_fact"))
        self.assertEqual(2, traits.count("procedure"))
        self.assertEqual(1, traits.count("successful_decision"))
        self.assertEqual(1, traits.count("failed_outcome_warning"))

        event_types = {event["event_type"] for event in fixtures.events}
        self.assertTrue(
            {
                "fact.asserted",
                "fact.retracted",
                "memory.created",
                "memory.outcome_recorded",
                "relationship.created",
            }.issubset(event_types)
        )
        contradiction_queries = [
            query for query in fixtures.queries if query["contradiction"] is not None
        ]
        self.assertEqual(2, len(contradiction_queries))
        self.assertEqual(
            2, sum(query["expected_abstention"] for query in fixtures.queries)
        )
        lexical_unavailable = next(
            query
            for query in fixtures.queries
            if query["query_id"] == "q12_lexical_unavailable"
        )
        self.assertEqual(
            ["lexical"],
            lexical_unavailable["expected_provider_degradation"]["fully_enabled"],
        )

    def test_loader_rejects_content_tampering_and_unknown_fields(self) -> None:
        with tempfile.TemporaryDirectory() as raw_temp:
            root = Path(raw_temp)
            for name in FIXTURE_FILES:
                shutil.copyfile(FIXTURE_ROOT / name, root / name)

            record_lines = (
                (root / "records.jsonl").read_text(encoding="utf-8").splitlines()
            )
            record = json.loads(record_lines[0])
            record["content"] += " tampered"
            record_lines[0] = json.dumps(record, sort_keys=True, separators=(",", ":"))
            (root / "records.jsonl").write_text(
                "\n".join(record_lines) + "\n", encoding="utf-8", newline="\n"
            )
            with self.assertRaisesRegex(
                FixtureValidationError, "CONTENT_HASH_MISMATCH"
            ):
                load_retrieval_fixtures(root)

            shutil.copyfile(FIXTURE_ROOT / "records.jsonl", root / "records.jsonl")
            query_lines = (
                (root / "queries.jsonl").read_text(encoding="utf-8").splitlines()
            )
            query = json.loads(query_lines[0])
            query["unexpected"] = True
            query_lines[0] = json.dumps(query, sort_keys=True, separators=(",", ":"))
            (root / "queries.jsonl").write_text(
                "\n".join(query_lines) + "\n", encoding="utf-8", newline="\n"
            )
            with self.assertRaisesRegex(FixtureValidationError, "UNKNOWN_FIELDS"):
                load_retrieval_fixtures(root)

    def test_loader_rejects_trait_metadata_that_disagrees_with_record_state(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as raw_temp:
            root = Path(raw_temp)
            for name in FIXTURE_FILES:
                shutil.copyfile(FIXTURE_ROOT / name, root / name)
            records_path = root / "records.jsonl"
            records = records_path.read_text(encoding="utf-8").splitlines()
            archived = json.loads(records[3])
            archived["archived"] = False
            _rewrite_jsonl_row(records_path, 3, archived)

            with self.assertRaisesRegex(FixtureValidationError, "TRAIT_CONTRACT"):
                load_retrieval_fixtures(root)

    def test_loader_rejects_unknown_event_semantics_and_forward_causation(self) -> None:
        for mutation in ("unknown_type", "forward_causation"):
            with (
                self.subTest(mutation=mutation),
                tempfile.TemporaryDirectory() as raw_temp,
            ):
                root = Path(raw_temp)
                for name in FIXTURE_FILES:
                    shutil.copyfile(FIXTURE_ROOT / name, root / name)
                events_path = root / "events.jsonl"
                event_lines = events_path.read_text(encoding="utf-8").splitlines()
                relationship = json.loads(event_lines[15])
                if mutation == "unknown_type":
                    relationship["event_type"] = "relationship.unknown"
                else:
                    future = json.loads(event_lines[16])
                    relationship["causation_event_id"] = future["event_id"]
                _rehash_event(relationship)
                _rewrite_jsonl_row(events_path, 15, relationship)

                code = (
                    "INVALID_EVENT_TYPE"
                    if mutation == "unknown_type"
                    else "FORWARD_CAUSATION"
                )
                with self.assertRaisesRegex(FixtureValidationError, code):
                    load_retrieval_fixtures(root)

    def test_qdrant_rank_fake_is_repeatable_and_has_minimal_payload(self) -> None:
        fake_path = FIXTURE_ROOT / "qdrant_fake.py"
        spec = importlib.util.spec_from_file_location(
            "retrieval_qdrant_fake", fake_path
        )
        self.assertIsNotNone(spec)
        self.assertIsNotNone(spec.loader if spec else None)
        module = importlib.util.module_from_spec(spec)
        assert spec is not None and spec.loader is not None
        spec.loader.exec_module(module)

        fake = module.DeterministicRankListFake()
        first = fake.search("q03_dense_semantic", limit=3)
        second = fake.search("q03_dense_semantic", limit=3)
        self.assertEqual(first, second)
        self.assertEqual([], fake.search("unknown", limit=3))
        self.assertEqual(3, len(first))
        for point in first:
            self.assertEqual(
                {
                    "content_hash",
                    "model_id",
                    "projection_generation",
                    "record_id",
                    "workspace_id",
                },
                set(point["payload"]),
            )

    def test_events_replay_exactly_through_the_task_seven_event_store(self) -> None:
        from daem0nmcp.event_store import EventCommand, EventStore

        schema_path = (
            Path(__file__).parents[1] / "daem0nmcp" / "migrations" / "schema.py"
        )
        spec = importlib.util.spec_from_file_location("benchmark_schema", schema_path)
        self.assertIsNotNone(spec)
        self.assertIsNotNone(spec.loader if spec else None)
        schema = importlib.util.module_from_spec(spec)
        assert spec is not None and spec.loader is not None
        spec.loader.exec_module(schema)
        statements = next(row[2] for row in schema.MIGRATIONS if row[0] == 16)
        connection = sqlite3.connect(":memory:")
        connection.row_factory = sqlite3.Row
        self.addCleanup(connection.close)
        connection.execute("PRAGMA foreign_keys=ON")
        for statement in statements:
            connection.execute(statement)

        fixtures = load_retrieval_fixtures(FIXTURE_ROOT)
        store = EventStore(connection)
        for event in fixtures.events:
            appended = store.append_and_project(
                EventCommand(
                    workspace_id=event["workspace_id"],
                    stream_id=event["stream_id"],
                    stream_kind=event["stream_kind"],
                    event_type=event["event_type"],
                    occurred_at_us=event["occurred_at_us"],
                    recorded_at_us=event["recorded_at_us"],
                    actor_type=event["actor_type"],
                    actor_id=event["actor_id"],
                    payload=event["payload"],
                    causation_event_id=event["causation_event_id"],
                    correlation_id=event["correlation_id"],
                    event_schema_version=event["event_schema_version"],
                    expected_stream_version=event["stream_version"],
                )
            )
            self.assertEqual(event["event_id"], appended.event_id)

        projected = {
            row["record_id"]: row["content_hash"]
            for row in connection.execute(
                "SELECT record_id,content_hash FROM memory_records"
            ).fetchall()
        }
        self.assertEqual(
            {
                record["record_id"]: record["content_hash"]
                for record in fixtures.records
            },
            projected,
        )
        self.assertEqual(
            3,
            connection.execute("SELECT count(*) FROM memory_fact_versions").fetchone()[
                0
            ],
        )
        self.assertEqual(
            1,
            connection.execute(
                "SELECT count(*) FROM memory_relationship_versions"
            ).fetchone()[0],
        )


class ExactMetricTests(unittest.TestCase):
    def test_ranking_metrics_follow_fixture_order_and_exact_cutoffs(self) -> None:
        queries = (
            {
                "query_id": "q1",
                "expected_relevant": [
                    {"record_id": "a", "grade": 3},
                    {"record_id": "b", "grade": 1},
                ],
            },
            {
                "query_id": "q2",
                "expected_relevant": [{"record_id": "c", "grade": 2}],
            },
            {"query_id": "q3", "expected_relevant": []},
        )
        results = {
            "q1": _result(["x", "b", "a"]),
            "q2": _result(["z"]),
            "q3": _result([]),
        }

        metrics = calculate_ranking_metrics(queries, results)

        self.assertEqual(2, metrics["evaluated_queries"])
        self.assertEqual(1, metrics["queries_without_relevant"])
        self.assertEqual(
            {"1": 0.0, "3": 0.5, "5": 0.5, "10": 0.5},
            metrics["recall_at"],
        )
        self.assertEqual(0.25, metrics["mrr_at_10"])
        self.assertAlmostEqual(0.2706701468217607, metrics["ndcg_at_10"], places=15)

    def test_grade_zero_judgments_are_not_treated_as_relevant(self) -> None:
        queries = (
            {
                "query_id": "graded",
                "expected_relevant": [
                    {"record_id": "a", "grade": 3},
                    {"record_id": "noise", "grade": 0},
                ],
            },
            {
                "query_id": "zero-only",
                "expected_relevant": [{"record_id": "z", "grade": 0}],
            },
        )
        results = {
            "graded": _result(["noise", "a"]),
            "zero-only": _result(["z"]),
        }

        metrics = calculate_ranking_metrics(queries, results)

        self.assertEqual(1, metrics["evaluated_queries"])
        self.assertEqual(1, metrics["queries_without_relevant"])
        self.assertEqual(0.5, metrics["mrr_at_10"])

    def test_quality_metrics_cover_temporal_contradiction_abstention_and_tokens(
        self,
    ) -> None:
        queries = (
            {
                "query_id": "exclude",
                "expected_abstention": False,
                "required_citations": ["current"],
                "temporal_expectation": {
                    "include_invalidated": False,
                    "known_invalidated_record_ids": ["old"],
                    "valid_record_ids": ["current"],
                },
                "contradiction": {"mode": "exclude", "record_ids": ["old"]},
            },
            {
                "query_id": "label",
                "expected_abstention": False,
                "required_citations": ["old"],
                "temporal_expectation": {
                    "include_invalidated": True,
                    "known_invalidated_record_ids": ["old"],
                    "valid_record_ids": ["old"],
                },
                "contradiction": {"mode": "label", "record_ids": ["old"]},
            },
            {
                "query_id": "no-answer-hit",
                "expected_abstention": True,
                "required_citations": [],
                "temporal_expectation": None,
                "contradiction": None,
            },
            {
                "query_id": "no-answer-missed",
                "expected_abstention": True,
                "required_citations": [],
                "temporal_expectation": None,
                "contradiction": None,
            },
        )
        results = {
            "exclude": _result(["current"], tokens=20),
            "label": _result(["old"], statuses={"old": "superseded"}, tokens=10),
            "no-answer-hit": _result([], abstained=True),
            "no-answer-missed": _result(["noise"]),
        }

        metrics = calculate_quality_metrics(queries, results)

        self.assertEqual(1.0, metrics["temporal"]["valid_return_ratio"])
        self.assertEqual(1.0, metrics["temporal"]["invalidated_exclusion_rate"])
        self.assertEqual(1.0, metrics["contradiction_handling"])
        self.assertEqual(
            {
                "false_negative": 1,
                "false_positive": 0,
                "precision": 1.0,
                "recall": 0.5,
                "true_negative": 2,
                "true_positive": 1,
            },
            metrics["abstention"],
        )
        self.assertEqual(30, metrics["tokens"]["rendered_tokens"])
        self.assertEqual(15.0, metrics["tokens"]["tokens_per_relevant_citation"])
        self.assertEqual(1.0, metrics["tokens"]["evidence_coverage"])

    def test_excluded_temporal_evidence_cannot_leak_as_a_merged_citation(self) -> None:
        queries = (
            {
                "query_id": "temporal",
                "expected_abstention": False,
                "expected_excluded_citations": ["old"],
                "required_citations": ["current"],
                "temporal_expectation": {
                    "include_invalidated": False,
                    "known_invalidated_record_ids": ["old"],
                    "valid_record_ids": ["current"],
                },
                "contradiction": {"mode": "exclude", "record_ids": ["old"]},
            },
        )
        results = {
            "temporal": _result(["current"], citations=["current", "old"], tokens=5)
        }

        metrics = calculate_quality_metrics(queries, results)

        self.assertEqual(0.0, metrics["temporal"]["invalidated_exclusion_rate"])
        self.assertEqual(0.0, metrics["contradiction_handling"])
        self.assertEqual(0.0, metrics["excluded_citation_exclusion_rate"])

    def test_abstention_precision_is_zero_when_nothing_abstains(self) -> None:
        queries = (
            {
                "query_id": "no-answer",
                "expected_abstention": True,
                "required_citations": [],
                "temporal_expectation": None,
                "contradiction": None,
            },
        )
        metrics = calculate_quality_metrics(queries, {"no-answer": _result(["noise"])})

        self.assertEqual(0.0, metrics["abstention"]["precision"])
        self.assertEqual(0.0, metrics["abstention"]["recall"])


class BenchmarkRunnerTests(unittest.TestCase):
    def test_runner_uses_five_warmups_and_thirty_timed_iterations_per_mode(
        self,
    ) -> None:
        fixtures = load_retrieval_fixtures(FIXTURE_ROOT)
        calls = {"fully_enabled": 0, "lexical_only": 0}

        def make_retriever(mode: str):
            def retrieve(query: dict[str, Any]) -> dict[str, Any]:
                calls[mode] += 1
                returned = [item["record_id"] for item in query["expected_relevant"]]
                if query["expected_abstention"]:
                    returned = []
                providers = {"lexical": "ready"}
                timings = {"lexical": 7}
                if mode == "fully_enabled":
                    providers["dense"] = "ready"
                    timings["dense"] = 11
                for provider in query["expected_provider_degradation"][mode]:
                    providers[provider] = "unavailable"
                    timings.setdefault(provider, 0)
                statuses = {}
                contradiction = query["contradiction"]
                if contradiction and contradiction["mode"] == "label":
                    statuses.update(
                        dict.fromkeys(contradiction["record_ids"], "superseded")
                    )
                return _result(
                    returned,
                    citations=list(query["required_citations"]),
                    statuses=statuses,
                    abstained=query["expected_abstention"],
                    tokens=min(query["token_budget"], len(returned) * 17),
                    providers=providers,
                    timings=timings,
                )

            return retrieve

        tick = -13

        def clock_ns() -> int:
            nonlocal tick
            tick += 13
            return tick

        report = run_benchmark(
            fixtures,
            {
                "fully_enabled": make_retriever("fully_enabled"),
                "lexical_only": make_retriever("lexical_only"),
            },
            version_identifier="7.0.0.dev0+fixture",
            manifests={"lexical": "lexical-generation-1"},
            config_hashes={"retrieval": "a" * 64},
            provider_availability={
                "dense": "unavailable",
                "graph": "unavailable",
                "lexical": "ready",
            },
            clock_ns=clock_ns,
        )

        expected_calls = 12 * (5 + 30)
        self.assertEqual(
            {"fully_enabled": expected_calls, "lexical_only": expected_calls}, calls
        )
        self.assertEqual(
            {"timed_iterations": 30, "warmup_iterations": 5}, report["benchmark"]
        )
        self.assertEqual(fixtures.digest, report["metadata"]["fixture_digest"])
        self.assertEqual(
            "unavailable", report["metadata"]["provider_availability"]["dense"]
        )
        for mode in ("fully_enabled", "lexical_only"):
            latency = report["modes"][mode]["latency_ns"]
            self.assertEqual({"p50": 13, "p95": 13, "p99": 13}, latency["end_to_end"])
            self.assertEqual(12, len(report["modes"][mode]["per_query"]))
            ranking = report["modes"][mode]["metrics"]["ranking"]
            self.assertEqual(
                {"1": 1.0, "3": 1.0, "5": 1.0, "10": 1.0},
                ranking["recall_at"],
            )
            self.assertEqual(1.0, ranking["mrr_at_10"])
            self.assertEqual(1.0, ranking["ndcg_at_10"])
            quality = report["modes"][mode]["metrics"]["quality"]
            self.assertEqual(1.0, quality["temporal"]["valid_return_ratio"])
            self.assertEqual(1.0, quality["contradiction_handling"])
            self.assertEqual(1.0, quality["abstention"]["precision"])
            self.assertEqual(1.0, quality["abstention"]["recall"])
            self.assertEqual(170, quality["tokens"]["rendered_tokens"])
            self.assertEqual(1.0, quality["tokens"]["evidence_coverage"])

        self.assertEqual(
            {
                "expected": 1,
                "missing": [],
                "observed_expected": 1,
                "unexpected": [],
            },
            report["modes"]["fully_enabled"]["provider_degradation"],
        )
        self.assertEqual(
            {
                "expected": 8,
                "missing": [],
                "observed_expected": 8,
                "unexpected": [],
            },
            report["modes"]["lexical_only"]["provider_degradation"],
        )

        rendered = serialize_benchmark_report(report)
        self.assertEqual(rendered, serialize_benchmark_report(report))
        self.assertEqual(report, json.loads(rendered))
        self.assertNotIn("generated_at", report["metadata"])

    def test_runner_rejects_record_ids_outside_the_fixture_authority(self) -> None:
        fixtures = load_retrieval_fixtures(FIXTURE_ROOT)

        def invalid_retriever(query: dict[str, Any]) -> dict[str, Any]:
            return _result(["mem_" + "f" * 64])

        with self.assertRaisesRegex(BenchmarkInputError, "UNKNOWN_RESULT_RECORD"):
            run_benchmark(
                fixtures,
                {
                    "fully_enabled": invalid_retriever,
                    "lexical_only": invalid_retriever,
                },
                version_identifier="fixture",
                manifests={"lexical": "g1"},
                config_hashes={"retrieval": "a" * 64},
                provider_availability={"lexical": "ready"},
                clock_ns=lambda: 0,
            )

    def test_runner_rejects_evidence_or_tokens_on_an_abstained_result(self) -> None:
        fixtures = load_retrieval_fixtures(FIXTURE_ROOT)
        record_id = fixtures.records[0]["record_id"]
        cases = (
            {"citations": [record_id], "statuses": {}, "tokens": 0},
            {
                "citations": [record_id],
                "statuses": {record_id: "current"},
                "tokens": 0,
            },
            {"citations": [], "statuses": {}, "tokens": 1},
        )
        for case in cases:
            with self.subTest(case=case):
                invalid_retriever = _constant_retriever(
                    _result(
                        [],
                        citations=case["citations"],
                        statuses=case["statuses"],
                        abstained=True,
                        tokens=case["tokens"],
                    )
                )

                with self.assertRaisesRegex(
                    BenchmarkInputError, "INVALID_ABSTENTION_RESULT"
                ):
                    run_benchmark(
                        fixtures,
                        {
                            "fully_enabled": invalid_retriever,
                            "lexical_only": invalid_retriever,
                        },
                        version_identifier="fixture",
                        manifests={"lexical": "g1"},
                        config_hashes={"retrieval": "a" * 64},
                        provider_availability={"lexical": "ready"},
                        clock_ns=lambda: 0,
                    )

    def test_cli_emits_one_deterministic_json_report_to_stdout(self) -> None:
        def abstaining_retriever(query: dict[str, Any]) -> dict[str, Any]:
            return _result([], citations=[], abstained=True)

        output = io.StringIO()
        with redirect_stdout(output):
            exit_code = main(
                [
                    "--fixture-root",
                    str(FIXTURE_ROOT),
                    "--version-identifier",
                    "7.0.0.dev0+cli-test",
                    "--manifest",
                    "lexical=g1",
                    "--config-hash",
                    f"retrieval={'a' * 64}",
                    "--provider-availability",
                    "lexical=ready",
                ],
                retrievers={
                    "fully_enabled": abstaining_retriever,
                    "lexical_only": abstaining_retriever,
                },
                clock_ns=lambda: 0,
            )

        self.assertEqual(0, exit_code)
        report = json.loads(output.getvalue())
        self.assertEqual(
            "7.0.0.dev0+cli-test", report["metadata"]["version_identifier"]
        )
        self.assertEqual(EXPECTED_FIXTURE_DIGEST, report["metadata"]["fixture_digest"])
        self.assertEqual(30, report["benchmark"]["timed_iterations"])

    def test_end_to_end_clock_excludes_benchmark_result_validation(self) -> None:
        fixtures = load_retrieval_fixtures(FIXTURE_ROOT)
        tick = -10

        def clock_ns() -> int:
            nonlocal tick
            tick += 10
            return tick

        class ClockTouchingResult(dict[str, Any]):
            def __getitem__(self, key: str) -> Any:
                clock_ns()
                return super().__getitem__(key)

        def retrieve(query: dict[str, Any]) -> dict[str, Any]:
            return ClockTouchingResult(_result([]))

        report = run_benchmark(
            fixtures,
            {"fully_enabled": retrieve, "lexical_only": retrieve},
            version_identifier="fixture",
            manifests={"lexical": "g1"},
            config_hashes={"retrieval": "a" * 64},
            provider_availability={"lexical": "ready"},
            clock_ns=clock_ns,
        )

        for mode in ("fully_enabled", "lexical_only"):
            self.assertEqual(
                {"p50": 10, "p95": 10, "p99": 10},
                report["modes"][mode]["latency_ns"]["end_to_end"],
            )

    def test_runner_requires_lexical_status_and_abstains_when_lexical_is_down(
        self,
    ) -> None:
        fixtures = load_retrieval_fixtures(FIXTURE_ROOT)
        record_id = fixtures.records[0]["record_id"]
        cases = (
            (
                "LEXICAL_STATUS_REQUIRED",
                _result(
                    [],
                    citations=[],
                    providers={"dense": "ready"},
                    timings={"dense": 1},
                ),
            ),
            (
                "LEXICAL_UNAVAILABLE_MUST_ABSTAIN",
                _result(
                    [record_id],
                    providers={"lexical": "unavailable"},
                    timings={"lexical": 1},
                ),
            ),
        )
        for code, invalid_result in cases:
            with self.subTest(code=code):
                invalid_retriever = _constant_retriever(invalid_result)

                with self.assertRaisesRegex(BenchmarkInputError, code):
                    run_benchmark(
                        fixtures,
                        {
                            "fully_enabled": invalid_retriever,
                            "lexical_only": invalid_retriever,
                        },
                        version_identifier="fixture",
                        manifests={"lexical": "g1"},
                        config_hashes={"retrieval": "a" * 64},
                        provider_availability={"lexical": "ready"},
                        clock_ns=lambda: 0,
                    )

    def test_runner_requires_provider_status_and_timing_key_parity(self) -> None:
        fixtures = load_retrieval_fixtures(FIXTURE_ROOT)
        cases = (
            _result(
                [],
                providers={"dense": "ready", "lexical": "ready"},
                timings={"lexical": 1},
            ),
            _result(
                [],
                providers={"lexical": "ready"},
                timings={"lexical": 1, "phantom": 2},
            ),
        )
        for invalid_result in cases:
            with self.subTest(invalid_result=invalid_result):
                invalid_retriever = _constant_retriever(invalid_result)

                with self.assertRaisesRegex(
                    BenchmarkInputError, "PROVIDER_TIMING_STATUS_MISMATCH"
                ):
                    run_benchmark(
                        fixtures,
                        {
                            "fully_enabled": invalid_retriever,
                            "lexical_only": invalid_retriever,
                        },
                        version_identifier="fixture",
                        manifests={"lexical": "g1"},
                        config_hashes={"retrieval": "a" * 64},
                        provider_availability={"lexical": "ready"},
                        clock_ns=lambda: 0,
                    )


if __name__ == "__main__":
    unittest.main()
