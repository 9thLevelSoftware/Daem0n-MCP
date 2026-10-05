"""Deterministic outcome evidence weighting and credit folding tests."""

from __future__ import annotations

import unittest
from dataclasses import replace
from datetime import datetime, timezone

from tests.api_v7.test_runtime_services import _RuntimeServiceFixtures
from tests.test_retrieval_service import (
    SNAPSHOT,
    CanonicalRepository,
    ReversingReranker,
    StaticProvider,
    _candidate,
    _provider_result,
    _query,
    _record_id,
    _service,
)


class VerificationWeightTests(unittest.TestCase):
    def test_absent_and_self_report_have_half_weight(self):
        from daem0nmcp.retrieval.utility import verification_weight

        for worked in (False, True):
            self.assertEqual(0.5, verification_weight(worked, None))
            self.assertEqual(0.5, verification_weight(worked, {"kind": "self_report"}))

    def test_review_has_three_quarter_weight(self):
        from daem0nmcp.retrieval.utility import verification_weight

        for worked in (False, True):
            self.assertEqual(0.75, verification_weight(worked, {"kind": "review"}))

    def test_command_evidence_covers_matching_and_conflicting_results(self):
        from daem0nmcp.retrieval.utility import verification_weight

        for kind in ("test", "command"):
            for worked in (False, True):
                for verification in ({"kind": kind}, {"kind": kind, "exit_code": None}):
                    self.assertEqual(0.75, verification_weight(worked, verification))
                for exit_code in (0, 1, -1):
                    with self.subTest(kind=kind, worked=worked, exit_code=exit_code):
                        self.assertEqual(
                            1.0 if (exit_code == 0) == worked else 0.25,
                            verification_weight(
                                worked, {"kind": kind, "exit_code": exit_code}
                            ),
                        )


class UtilityFoldingTests(unittest.TestCase):
    def test_empty_evidence_is_neutral(self):
        from daem0nmcp.retrieval.utility import (
            UtilityEstimate,
            fold_utility,
            utility_signal,
        )

        estimate = fold_utility(())
        self.assertEqual(UtilityEstimate(0.5, 0.5, 0), estimate)
        for credit in ("single_step", "trace"):
            self.assertEqual(0.0, utility_signal(estimate, credit))

    def test_direct_and_first_hop_evidence_updates_both_estimates(self):
        from daem0nmcp.retrieval.utility import UtilityContribution, fold_utility

        for depth in (0, 1):
            with self.subTest(depth=depth):
                estimate = fold_utility(
                    (UtilityContribution(1, "evt_a", depth, 1.0, 0.5),)
                )
                self.assertEqual(0.575, estimate.q_single)
                self.assertEqual(0.575, estimate.q_trace)
                self.assertEqual(1, estimate.evidence_count)

    def test_second_hop_has_trace_factor_of_point_five_six(self):
        from daem0nmcp.retrieval.utility import UtilityContribution, fold_utility

        estimate = fold_utility((UtilityContribution(1, "evt_a", 2, 1.0, 1.0),))
        self.assertEqual(0.5, estimate.q_single)
        self.assertEqual(0.584, estimate.q_trace)
        self.assertEqual(1, estimate.evidence_count)

    def test_deeper_trace_evidence_decays_and_rounds_at_the_end(self):
        from daem0nmcp.retrieval.utility import UtilityContribution, fold_utility

        estimate = fold_utility((UtilityContribution(1, "evt_a", 4, 0.0, 0.75),))
        self.assertEqual(0.5, estimate.q_single)
        self.assertEqual(round(0.5 + 0.3 * 0.75 * 0.56**3 * -0.5, 6), estimate.q_trace)

    def test_fold_orders_by_time_then_event_id_not_input_order(self):
        from daem0nmcp.retrieval.utility import UtilityContribution, fold_utility

        contributions = (
            UtilityContribution(2, "evt_c", 0, 1.0, 1.0),
            UtilityContribution(1, "evt_b", 1, 0.0, 1.0),
            UtilityContribution(1, "evt_a", 0, 1.0, 1.0),
        )
        estimate = fold_utility(contributions)
        self.assertEqual(estimate, fold_utility(reversed(contributions)))
        self.assertEqual(0.6185, estimate.q_single)
        self.assertEqual(0.6185, estimate.q_trace)
        self.assertEqual(3, estimate.evidence_count)

    def test_signal_uses_selected_credit_and_evidence_support(self):
        from daem0nmcp.retrieval.utility import UtilityEstimate, utility_signal

        self.assertAlmostEqual(
            0.1, utility_signal(UtilityEstimate(0.65, 0.5, 1), "single_step")
        )
        self.assertEqual(0.0, utility_signal(UtilityEstimate(0.65, 0.5, 1), "trace"))
        self.assertEqual(
            1.0, utility_signal(UtilityEstimate(1.0, 0.0, 3), "single_step")
        )
        self.assertEqual(-1.0, utility_signal(UtilityEstimate(1.0, 0.0, 3), "trace"))
        self.assertEqual(1.0, utility_signal(UtilityEstimate(1.0, 1.0, 10), "trace"))


class UtilityRepository(CanonicalRepository):
    def __init__(self, contributions=None, *, fail=False):
        super().__init__()
        self.contributions = contributions or {}
        self.fail = fail
        self.utility_calls = []

    async def load_utility_contributions(
        self, workspace_id, record_ids, transaction_at_us
    ):
        self.utility_calls.append((workspace_id, record_ids, transaction_at_us))
        if self.fail:
            raise RuntimeError("private outcome database detail")
        return self.contributions


class UtilityStageTests(unittest.IsolatedAsyncioTestCase):
    def _service(self, repository, **changes):
        return _service(
            providers={
                "lexical": StaticProvider(
                    "lexical",
                    _provider_result(
                        "lexical",
                        _candidate("1", "lexical", 1),
                        _candidate("2", "lexical", 2),
                        _candidate("3", "lexical", 3),
                    ),
                    [],
                ),
            },
            repository=repository,
            **changes,
        )

    def _positive_evidence(self, depth=1):
        from daem0nmcp.retrieval.utility import UtilityContribution

        return {
            _record_id("2"): tuple(
                UtilityContribution(index, f"evt_{index}", depth, 1.0, 1.0)
                for index in range(3)
            )
        }

    async def test_shadow_keeps_order_and_context_but_attaches_raw_utility(self):
        repository = UtilityRepository(self._positive_evidence())
        baseline = await self._service(repository).retrieve(_query(limit=3))
        shadow = await self._service(repository, utility_mode="shadow").retrieve(
            _query(limit=3)
        )
        self.assertEqual(
            tuple(item.evidence_refs for item in baseline.items),
            tuple(item.evidence_refs for item in shadow.items),
        )
        self.assertEqual(baseline.context, shadow.context)
        self.assertEqual(0.8285, shadow.items[1].utility)
        self.assertIsNone(shadow.items[0].utility)
        diagnostic = shadow.providers[-1]
        self.assertEqual(
            ("utility", "ready", "UTILITY_SHADOW_REORDER", 1),
            (
                diagnostic.provider,
                diagnostic.status,
                diagnostic.reason,
                diagnostic.returned_count,
            ),
        )
        self.assertIsNone(diagnostic.manifest_generation)

    async def test_apply_reorders_before_selection_and_preserves_tail(self):
        repository = UtilityRepository(self._positive_evidence())
        result = await self._service(
            repository, utility_mode="apply", utility_candidate_limit=2
        ).retrieve_candidates(_query(limit=3))
        self.assertEqual(
            ("2", "1", "3"),
            tuple(source.candidate.record_id[-1] for source in result.selected),
        )
        self.assertEqual(0.8285, result.selected[0].utility)
        self.assertEqual("UTILITY_APPLIED", result.providers[-1].reason)
        self.assertEqual(
            (_record_id("1"), _record_id("2")), repository.utility_calls[0][1]
        )

    async def test_exception_degrades_and_keeps_original_order(self):
        result = await self._service(
            UtilityRepository(fail=True), utility_mode="apply"
        ).retrieve(_query(limit=3))
        self.assertFalse(result.abstained)
        self.assertEqual(
            ("1", "2", "3"),
            tuple(item.evidence_refs[0].record_id[-1] for item in result.items),
        )
        self.assertTrue(all(item.utility is None for item in result.items))
        diagnostic = result.providers[-1]
        self.assertEqual("degraded", diagnostic.status)
        self.assertEqual("UTILITY_FAILED", diagnostic.reason)
        self.assertEqual(0, diagnostic.returned_count)
        self.assertNotIn("private outcome", repr(result))

    async def test_no_evidence_reports_shadow_same_and_leaves_utility_unset(self):
        result = await self._service(
            UtilityRepository(), utility_mode="shadow"
        ).retrieve(_query())
        self.assertEqual("UTILITY_SHADOW_SAME", result.providers[-1].reason)
        self.assertEqual(0, result.providers[-1].returned_count)
        self.assertTrue(all(item.utility is None for item in result.items))

    async def test_transaction_cutoff_uses_explicit_time_or_snapshot(self):
        for cutoff in (None, datetime(2026, 1, 2, tzinfo=timezone.utc)):
            repository = UtilityRepository()
            query = _query(as_of_transaction_time=cutoff)
            await self._service(repository, utility_mode="shadow").retrieve_candidates(
                query
            )
            delta = (cutoff or SNAPSHOT) - datetime(1970, 1, 1, tzinfo=timezone.utc)
            expected = (
                delta.days * 86400 + delta.seconds
            ) * 1000000 + delta.microseconds
            self.assertEqual(
                (query.workspace_id, tuple(_record_id(d) for d in "123"), expected),
                repository.utility_calls[0],
            )

    async def test_credit_selects_raw_single_or_trace_estimate(self):
        from daem0nmcp.retrieval.utility import fold_utility

        contributions = self._positive_evidence(depth=2)
        estimate = fold_utility(contributions[_record_id("2")])
        for credit, expected in (
            ("single_step", estimate.q_single),
            ("trace", estimate.q_trace),
        ):
            result = await self._service(
                UtilityRepository(contributions),
                utility_mode="shadow",
                utility_credit=credit,
            ).retrieve(_query())
            self.assertEqual(expected, result.items[1].utility)

    async def test_utility_runs_after_reranker(self):
        repository = UtilityRepository()
        result = await self._service(
            repository,
            utility_mode="shadow",
            reranker=ReversingReranker(),
            rerank_enabled=True,
        ).retrieve_candidates(_query(rerank=True))
        self.assertEqual(
            tuple(_record_id(d) for d in "321"), repository.utility_calls[0][1]
        )
        self.assertEqual(
            ("reranker", "utility"),
            tuple(diagnostic.provider for diagnostic in result.providers[-2:]),
        )

    def test_constructor_validates_modes_weight_limit_and_repository(self):
        for changes in (
            {"utility_mode": "invalid"},
            {"utility_credit": "invalid"},
            {"utility_weight": -0.1},
            {"utility_weight": 1.1},
            {"utility_weight": float("nan")},
            {"utility_weight": float("inf")},
            {"utility_weight": True},
            {"utility_candidate_limit": 0},
            {"utility_candidate_limit": 201},
            {"utility_candidate_limit": True},
            {"utility_candidate_limit": 1.5},
        ):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self._service(UtilityRepository(), **changes)
        with self.assertRaises(ValueError):
            self._service(CanonicalRepository(), utility_mode="shadow")
        self._service(CanonicalRepository())
        for weight in (0.0, 1.0):
            self._service(UtilityRepository(), utility_weight=weight)

    async def test_selected_and_composed_utility_validate_finite_unit_interval(self):
        selected = (
            await self._service(UtilityRepository()).retrieve_candidates(_query())
        ).selected[0]
        item = (await self._service(UtilityRepository()).retrieve(_query())).items[0]
        for value in (-0.1, 1.1, float("nan"), float("inf"), True):
            for evidence in (selected, item):
                with self.subTest(value=value), self.assertRaises(ValueError):
                    replace(evidence, utility=value)


class UtilityWireTests(_RuntimeServiceFixtures, unittest.IsolatedAsyncioTestCase):
    async def test_production_shadow_reports_utility_on_wire(self):
        from daem0nmcp.api.v7.pinned import MemoryOutcomeCommand
        from daem0nmcp.api.v7.runtime_services import Task8RecallService
        from daem0nmcp.config import Settings
        from daem0nmcp.retrieval.runtime import drain_projection_jobs
        from daem0nmcp.retrieval.types import RetrievalQuery

        writer = self._writer()
        stored = await writer.store(self.workspace, self._store_command())
        await writer.record_outcome(
            self.workspace,
            MemoryOutcomeCommand(
                record_id=stored.record.record_id,
                outcome_text="Canonical tests passed.",
                worked=True,
                happened_at=None,
                idempotency_key="utility-wire-outcome",
                verification={"kind": "test", "exit_code": 0},
            ),
        )
        config = Settings()
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
        service = Task8RecallService(
            config=config,
            capability_statuses=statuses,
            max_workers=1,
        )
        try:
            result = await service.retrieve(
                self.workspace,
                RetrievalQuery(
                    workspace_id=self.workspace.workspace_id,
                    text="canonical",
                ),
                frozenset(),
            )
        finally:
            service.close()
        self.assertFalse(result.abstained)
        self.assertEqual(0.65, result.items[0].utility)
        self.assertEqual(0.65, result.model_dump()["items"][0]["utility"])
        diagnostic = result.provider_diagnostics[-1]
        self.assertEqual(
            ("utility", "ready", "UTILITY_SHADOW_SAME", 1),
            (
                diagnostic.provider,
                diagnostic.status,
                diagnostic.reason,
                diagnostic.returned_count,
            ),
        )

    def test_utility_settings_participate_in_service_cache_fingerprint(self):
        from daem0nmcp.api.v7.runtime_services import (
            _RETRIEVAL_CONFIG_FIELDS,
            _retrieval_config_fingerprint,
        )
        from daem0nmcp.config import Settings

        config = Settings()
        self.assertEqual("shadow", config.retrieval_utility_mode)
        for field in (
            "retrieval_utility_mode",
            "retrieval_utility_weight",
            "retrieval_utility_credit",
            "retrieval_utility_candidate_limit",
        ):
            self.assertIn(field, _RETRIEVAL_CONFIG_FIELDS)
        baseline = _retrieval_config_fingerprint(config, {})
        for field, value in (
            ("retrieval_utility_mode", "apply"),
            ("retrieval_utility_weight", 0.2),
            ("retrieval_utility_credit", "single_step"),
            ("retrieval_utility_candidate_limit", 10),
        ):
            self.assertNotEqual(
                baseline,
                _retrieval_config_fingerprint(
                    config.model_copy(update={field: value}), {}
                ),
            )
