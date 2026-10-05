"""Task-conditioned evidence packing, shadow isolation, and source transport."""

from __future__ import annotations

import unittest
from contextlib import closing
from dataclasses import replace

from daem0nmcp.retrieval.composer import (
    EvidenceComposer,
    RetentionPolicy,
    query_identifiers,
)
from daem0nmcp.retrieval.service import RetrievalService
from daem0nmcp.retrieval.types import RetrievalQuery
from tests.api_v7.test_runtime_services import _RuntimeServiceFixtures
from tests.test_retrieval_composer import WordTokenizer, _source
from tests.test_retrieval_service import (
    WORKSPACE_ID,
    CanonicalRepository,
    StaticProvider,
    _candidate,
    _provider_result,
)


def _sources():
    return tuple(
        _source(digit, "background detail " * 150, score=1.0) for digit in "1234"
    ) + (
        _source(
            "5",
            "deploy release rollout",
            score=0.5,
            procedure_steps=(
                "freeze release writes",
                "apply release rollout",
                "verify release health",
            ),
        ),
    )


class RetentionComposerTests(unittest.TestCase):
    def test_path_and_symbol_queries_preserve_bound_evidence_under_budget(self):
        composer = EvidenceComposer(tokenizer=WordTokenizer())
        background = tuple(
            _source(digit, "background detail " * 150, score=1.0) for digit in "1234"
        )
        bound = replace(
            _source(
                "5", "Acquire a lease before replacing the routing snapshot.", score=0.1
            ),
            code_bindings=(("src/http/router.ts", "pkg.Router.apply_routes"),),
        )
        cases = (
            ("src/http/router.ts", True),
            ("router.ts", True),
            ("pkg.Router.apply_routes", True),
            ("apply_routes", True),
            ("src/http/other.ts", False),
            ("unrelated.Symbol", False),
            ("src/", False),
        )
        for intent in ("implement", "debug"):
            for identifier, expected in cases:
                with self.subTest(intent=intent, identifier=identifier):
                    result = composer.compose(
                        background + (bound,),
                        token_budget=256,
                        retention=RetentionPolicy(
                            intent, query_identifiers("Inspect " + identifier)
                        ),
                    )
                    retained_ids = {
                        item.evidence_refs[0].record_id for item in result.items
                    }
                    self.assertEqual(
                        expected, bound.candidate.record_id in retained_ids
                    )
                    self.assertLessEqual(result.context.rendered_tokens, 256)

    def test_implement_retains_late_procedure_under_256_token_budget(self):
        composer = EvidenceComposer(tokenizer=WordTokenizer())
        baseline = composer.compose(_sources(), token_budget=256)
        applied = composer.compose(
            _sources(),
            token_budget=256,
            retention=RetentionPolicy("implement", frozenset()),
        )
        self.assertNotIn("freeze release writes", baseline.context.text)
        self.assertIn("freeze release writes", applied.context.text)
        self.assertLessEqual(applied.context.rendered_tokens, 256)
        ids = [item.evidence_refs[0].record_id[-1] for item in applied.items]
        self.assertEqual(sorted(ids), ids)
        self.assertEqual("5", ids[-1])
        self.assertEqual(
            [f"[E{index}]" for index in range(1, len(ids) + 1)],
            [item.citation for item in applied.items],
        )

    def test_none_and_explore_keep_exact_legacy_composition(self):
        composer = EvidenceComposer(tokenizer=WordTokenizer())
        baseline = composer.compose(_sources(), token_budget=256)
        for retention in (None, RetentionPolicy("explore", frozenset({"release"}))):
            self.assertEqual(
                baseline,
                composer.compose(_sources(), token_budget=256, retention=retention),
            )

    def test_binding_priorities_and_ordinary_excerpt_caps(self):
        composer = EvidenceComposer(tokenizer=WordTokenizer())
        source = replace(
            _sources()[0],
            code_bindings=(("src/cache.py", "pkg.Cache.handle_call"),),
        )
        for intent in ("implement", "debug"):
            for identifier in ("cache.py", "handle_call"):
                policy = RetentionPolicy(intent, frozenset({identifier}))
                item = composer.compose(
                    (source,), token_budget=4000, retention=policy
                ).items[0]
                self.assertEqual(1200, len(item.excerpt))
            ordinary = composer.compose(
                (source,),
                token_budget=4000,
                retention=RetentionPolicy(intent, frozenset({"unrelated"})),
            ).items[0]
            self.assertLessEqual(len(ordinary.excerpt), 600)
        for stale in (
            replace(
                source,
                applicability="needs_revalidation",
                changed_bindings=("src/cache.py",),
            ),
            replace(
                source, status="superseded", superseded_by_version_id="fact_" + "a" * 64
            ),
        ):
            for intent in ("implement", "debug", "review"):
                item = composer.compose(
                    (stale,),
                    token_budget=4000,
                    retention=RetentionPolicy(intent, frozenset({"cache.py"})),
                ).items[0]
                self.assertLessEqual(len(item.excerpt), 300)

    def test_intent_priority_reservation_fractions(self):
        warning = _source("a", "x " * 1000, score=1.0, category="warning")
        composer = EvidenceComposer(tokenizer=WordTokenizer(), max_excerpt_chars=4000)
        for intent, reserve in (
            ("explore", 154),
            ("implement", 308),
            ("debug", 359),
            ("review", 205),
        ):
            with self.subTest(intent=intent):
                result = composer.compose(
                    (warning,),
                    token_budget=1024,
                    retention=RetentionPolicy(intent, frozenset()),
                )
                self.assertEqual(reserve, result.context.rendered_tokens)

    def test_retention_preserves_applicability_labels(self):
        source = replace(
            _sources()[-1],
            applicability="needs_revalidation",
            changed_bindings=("src/cache.py::handle_call",),
        )
        result = EvidenceComposer(tokenizer=WordTokenizer()).compose(
            (source,),
            token_budget=256,
            retention=RetentionPolicy("implement", frozenset()),
            label_applicability=True,
        )
        self.assertIn(
            "Needs revalidation: src/cache.py::handle_call", result.context.text
        )
        self.assertIn("freeze release writes", result.context.text)


class RetentionServiceTests(unittest.IsolatedAsyncioTestCase):
    def _service(self, mode):
        sources = _sources()
        repository = CanonicalRepository(
            contents={source.candidate.record_id: source.content for source in sources},
            selected_changes={
                sources[-1].candidate.record_id: {
                    "procedure_steps": sources[-1].procedure_steps
                }
            },
        )
        provider = StaticProvider(
            "lexical",
            _provider_result(
                "lexical",
                *(
                    _candidate(digit, "lexical", index)
                    for index, digit in enumerate("12345", 1)
                ),
            ),
            [],
        )
        return RetrievalService(
            providers={"lexical": provider},
            repository=repository,
            composer=EvidenceComposer(tokenizer=WordTokenizer()),
            retention_mode=mode,
        )

    async def test_shadow_is_byte_identical_and_apply_retains_procedure(self):
        query = RetrievalQuery(
            workspace_id=WORKSPACE_ID,
            text="deploy release rollout",
            limit=5,
            token_budget=256,
            intent="implement",
        )
        baseline = await self._service("off").retrieve(query)
        shadow = await self._service("shadow").retrieve(query)
        applied = await self._service("apply").retrieve(query)
        self.assertFalse(baseline.abstained)
        self.assertEqual(baseline.items, shadow.items)
        self.assertEqual(baseline.context, shadow.context)
        self.assertNotIn("freeze release writes", baseline.context.text)
        self.assertIn("freeze release writes", applied.context.text)
        self.assertEqual("RETENTION_SHADOW", shadow.providers[-1].reason)
        self.assertEqual("RETENTION_APPLIED", applied.providers[-1].reason)
        self.assertEqual(len(applied.items), shadow.providers[-1].returned_count)

    async def test_none_does_not_compute_or_change_context(self):
        query = RetrievalQuery(
            workspace_id=WORKSPACE_ID, text="deploy", limit=5, token_budget=256
        )
        baseline = await self._service("off").retrieve(query)
        for mode in ("shadow", "apply"):
            result = await self._service(mode).retrieve(query)
            self.assertEqual(baseline.context, result.context)
            self.assertEqual(baseline.items, result.items)
            self.assertFalse(
                any(item.provider == "retention" for item in result.providers)
            )

    def test_query_and_mode_validation(self):
        with self.assertRaisesRegex(ValueError, "intent"):
            RetrievalQuery(workspace_id=WORKSPACE_ID, text="deploy", intent="plan")
        with self.assertRaisesRegex(ValueError, "retention_mode"):
            self._service("enabled")


class RetentionWireTests(_RuntimeServiceFixtures, unittest.IsolatedAsyncioTestCase):
    async def test_real_source_transport_preserves_procedures_bindings_and_intent(self):
        import sqlite3

        from daem0nmcp.api.v7.federated_retrieval import compose_federated_results
        from daem0nmcp.api.v7.runtime_services import (
            Task8RecallService,
            WorkspaceStorageResolver,
        )
        from daem0nmcp.config import Settings
        from daem0nmcp.retrieval.runtime import drain_projection_jobs

        (self.root / "runbook.txt").write_text("release runbook\n", encoding="utf-8")
        stored = await self._writer().store(
            self.workspace,
            self._store_command(
                record_type="procedure",
                content="deploy release rollout",
                procedure_steps=("freeze release writes",),
                code_refs=(("runbook.txt", None),),
            ),
        )
        settings = Settings(
            retrieval_utility_mode="off", retrieval_retention_mode="apply"
        )
        statuses = {
            "local": "disabled",
            "models-local": "disabled",
            "graph": "disabled",
        }
        while await drain_projection_jobs(
            self.database,
            config=settings,
            max_jobs=100,
            include_optional=True,
            capability_statuses=statuses,
        ):
            pass
        with closing(sqlite3.connect(self.database)) as connection, connection:
            connection.execute(
                "DELETE FROM projection_manifests WHERE workspace_id=? "
                "AND projection_name='procedure'",
                (self.workspace.workspace_id,),
            )
        service = Task8RecallService(config=settings, capability_statuses=statuses)
        query = RetrievalQuery(
            workspace_id=self.workspace.workspace_id,
            text="deploy release rollout",
            token_budget=256,
            intent="implement",
        )
        try:
            with WorkspaceStorageResolver().locked_active(self.workspace) as active:
                source = await service._retrieve_source(self.workspace, active, query)
            direct = await service.retrieve(self.workspace, query, frozenset())
        finally:
            service.close()
        self.assertEqual(1, len(source.candidates))
        candidate = source.candidates[0]
        self.assertEqual(("freeze release writes",), candidate.procedure_steps)
        self.assertEqual((("runbook.txt", None),), candidate.code_bindings)
        self.assertEqual(stored.record.record_id, candidate.record.record_id)
        self.assertNotIn("procedure", candidate.channels)
        baseline = compose_federated_results(
            {self.workspace.workspace_id: source}, query
        )
        shadow = compose_federated_results(
            {self.workspace.workspace_id: source},
            query,
            retention_mode="shadow",
        )
        applied = compose_federated_results(
            {self.workspace.workspace_id: source},
            query,
            retention_mode="apply",
        )
        self.assertEqual(baseline.items, shadow.items)
        self.assertEqual(baseline.rendered_context, shadow.rendered_context)
        self.assertEqual(baseline.token_usage, shadow.token_usage)
        self.assertIn("freeze release writes", applied.rendered_context)
        self.assertIn("freeze release writes", direct.rendered_context)
        self.assertEqual("RETENTION_APPLIED", direct.provider_diagnostics[-1].reason)
        self.assertEqual("RETENTION_SHADOW", shadow.provider_diagnostics[-1].reason)
        self.assertEqual(
            self.workspace.workspace_id,
            applied.items[0].evidence_refs[0].origin_workspace_id,
        )

    def test_wire_intent_validation(self):
        from pydantic import ValidationError

        from daem0nmcp.api.v7.tools import MemoryRecallInput

        with self.assertRaises(ValidationError):
            MemoryRecallInput(
                workspace_id=self.workspace.workspace_id, query="deploy", intent="plan"
            )


class FederatedRetentionTests(unittest.TestCase):
    def test_late_procedure_retention_and_shadow_context_isolation(self):
        from daem0nmcp.api.v7.federated_retrieval import (
            FederatedSourceResult,
            compose_federated_results,
            sliced_queries,
        )
        from tests.api_v7.test_federated_retrieval import FederatedRetrievalTests

        notes = FederatedRetrievalTests._source(
            WORKSPACE_ID,
            ("1", "2", "3", "4"),
            content="background detail " * 150,
        )
        procedure = FederatedRetrievalTests._source(
            WORKSPACE_ID,
            ("5",),
            content="deploy release rollout",
        ).candidates[0]
        procedure = replace(procedure, procedure_steps=("freeze release writes",))
        results = {
            WORKSPACE_ID: FederatedSourceResult(
                candidates=(*notes.candidates, procedure)
            ),
        }
        query = RetrievalQuery(
            workspace_id=WORKSPACE_ID,
            text="deploy release rollout",
            limit=5,
            token_budget=256,
            intent="implement",
        )
        baseline = compose_federated_results(results, query)
        shadow = compose_federated_results(results, query, retention_mode="shadow")
        applied = compose_federated_results(results, query, retention_mode="apply")
        self.assertEqual(baseline.items, shadow.items)
        self.assertEqual(baseline.rendered_context, shadow.rendered_context)
        self.assertEqual(baseline.token_usage, shadow.token_usage)
        self.assertNotIn("freeze release writes", baseline.rendered_context)
        self.assertIn("freeze release writes", applied.rendered_context)
        ids = [item.record.record_id[-1] for item in applied.items]
        self.assertEqual(sorted(ids), ids)
        self.assertLessEqual(applied.token_usage.rendered, 256)
        self.assertEqual(
            len(applied.items), shadow.provider_diagnostics[-1].returned_count
        )
        single = compose_federated_results(
            results,
            replace(query, limit=1),
            retention_mode="apply",
        )
        self.assertEqual("5", single.items[0].record.record_id[-1])
        for intent in (None, "explore"):
            unchanged = compose_federated_results(
                results,
                replace(query, intent=intent),
                retention_mode="apply",
            )
            self.assertEqual(baseline.items, unchanged.items)
            self.assertEqual(baseline.rendered_context, unchanged.rendered_context)
        slices = sliced_queries(query, WORKSPACE_ID, ["ws_" + "b" * 24])
        self.assertTrue(all(item.intent == "implement" for item in slices.values()))

    def test_shadow_policy_failure_does_not_change_federated_context(self):
        from unittest.mock import patch

        from daem0nmcp.api.v7.federated_retrieval import compose_federated_results
        from tests.api_v7.test_federated_retrieval import FederatedRetrievalTests

        results = {WORKSPACE_ID: FederatedRetrievalTests._source(WORKSPACE_ID, ("1",))}
        query = RetrievalQuery(
            workspace_id=WORKSPACE_ID, text="deploy", intent="implement"
        )
        baseline = compose_federated_results(results, query)
        with patch(
            "daem0nmcp.api.v7.federated_retrieval._compose_retained",
            side_effect=RuntimeError("private failure"),
        ):
            shadow = compose_federated_results(results, query, retention_mode="shadow")
        self.assertEqual(baseline.items, shadow.items)
        self.assertEqual(baseline.rendered_context, shadow.rendered_context)
        self.assertEqual("RETENTION_FAILED", shadow.provider_diagnostics[-1].reason)
        self.assertEqual("degraded", shadow.provider_diagnostics[-1].status)
        self.assertEqual(0, shadow.provider_diagnostics[-1].returned_count)


class RetentionShadowFailureTests(unittest.IsolatedAsyncioTestCase):
    _service = RetentionServiceTests._service

    async def test_shadow_policy_failure_keeps_baseline_context(self):
        service = self._service("shadow")
        delegate = service._composer

        class FailingRetentionComposer:
            async def compose_async(
                self,
                selected,
                *,
                token_budget,
                retention=None,
                label_applicability=False,
            ):
                if retention is not None:
                    raise RuntimeError("private failure")
                return await delegate.compose_async(
                    selected,
                    token_budget=token_budget,
                    retention=retention,
                    label_applicability=label_applicability,
                )

        service._composer = FailingRetentionComposer()
        query = RetrievalQuery(
            workspace_id=WORKSPACE_ID,
            text="deploy",
            limit=5,
            token_budget=256,
            intent="implement",
        )
        baseline = await self._service("off").retrieve(query)
        shadow = await service.retrieve(query)
        self.assertEqual(baseline.items, shadow.items)
        self.assertEqual(baseline.context, shadow.context)
        self.assertEqual("RETENTION_FAILED", shadow.providers[-1].reason)
        self.assertEqual("degraded", shadow.providers[-1].status)
        self.assertEqual(0, shadow.providers[-1].returned_count)


class CanonicalProcedureHydrationTests(
    _RuntimeServiceFixtures,
    unittest.IsolatedAsyncioTestCase,
):
    async def test_canonical_steps_enforce_source_context_and_transaction_cutoff(self):
        import json
        import sqlite3
        from datetime import timedelta

        from daem0nmcp.config import Settings
        from daem0nmcp.retrieval.repository import (
            RetrievalRepositoryError,
            SQLiteRetrievalRepository,
        )
        from daem0nmcp.retrieval.runtime import (
            create_retrieval_service,
            drain_projection_jobs,
        )
        from tests.api_v7.test_runtime_services import WORKED_AT

        stored = await self._writer().store(
            self.workspace,
            self._store_command(
                record_type="procedure",
                content="deploy release rollout",
                procedure_steps=("freeze release writes",),
            ),
        )
        config = Settings(
            retrieval_utility_mode="off",
            memory_validity_mode="off",
            retrieval_retention_mode="off",
        )
        statuses = {
            "local": "disabled",
            "models-local": "disabled",
            "graph": "disabled",
        }
        while await drain_projection_jobs(
            self.database,
            config=config,
            max_jobs=100,
            include_optional=True,
            capability_statuses=statuses,
        ):
            pass
        service = create_retrieval_service(
            self.database,
            config=config,
            capability_statuses=statuses,
        )
        self.health_services.append(service)
        query = RetrievalQuery(workspace_id=self.workspace.workspace_id, text="deploy")
        candidates = await service.retrieve_candidates(query)
        self.assertFalse(candidates.abstained)
        self.assertEqual(1, len(candidates.selected))
        candidate = candidates.selected[0].candidate
        repository = SQLiteRetrievalRepository(self.database)
        self.health_services.append(repository)
        snapshot = WORKED_AT + timedelta(days=1)
        evidence = await repository.load_selected_evidence(
            query,
            (candidate,),
            snapshot_time=snapshot,
        )
        self.assertEqual(("freeze release writes",), evidence[0].procedure_steps)
        with self.assertRaisesRegex(
            RetrievalRepositoryError,
            "EVIDENCE_CONTENT_UNAVAILABLE",
        ):
            await repository.load_selected_evidence(
                replace(
                    query, as_of_transaction_time=WORKED_AT - timedelta(microseconds=1)
                ),
                (candidate,),
                snapshot_time=snapshot,
            )
        with closing(sqlite3.connect(self.database)) as connection, connection:
            context_text = connection.execute(
                "SELECT context_json FROM memory_records "
                "WHERE workspace_id=? AND record_id=?",
                (self.workspace.workspace_id, stored.record.record_id),
            ).fetchone()[0]
            context = json.loads(context_text)
            context["steps"] = ["fabricated instruction"]
            connection.execute(
                "UPDATE memory_records SET context_json=? "
                "WHERE workspace_id=? AND record_id=?",
                (
                    json.dumps(context),
                    self.workspace.workspace_id,
                    stored.record.record_id,
                ),
            )
        with self.assertRaisesRegex(
            RetrievalRepositoryError,
            "EVIDENCE_CONTENT_UNAVAILABLE",
        ):
            await repository.load_selected_evidence(
                query,
                (candidate,),
                snapshot_time=snapshot,
            )
