"""Deterministic token budgeting and citation integrity for v7 retrieval."""

from __future__ import annotations

import asyncio
import threading
import unittest
from time import perf_counter
from unittest.mock import patch

WORKSPACE_ID = "ws_0123456789abcdef01234567"


def _record_id(digit: str) -> str:
    return "mem_" + digit * 64


def _event_id(digit: str) -> str:
    return "evt_" + digit * 64


def _candidate(
    digit: str,
    *,
    score: float,
    channels: tuple[str, ...] = ("lexical",),
    highlights: tuple[str, ...] = (),
):
    from daem0nmcp.retrieval.types import EvidenceRef, FusedCandidate

    primary_channel = channels[0]
    evidence = EvidenceRef(
        record_id=_record_id(digit),
        event_id=_event_id(digit),
        content_hash=digit * 64,
        version_id=None,
        provider=primary_channel,
    )
    return FusedCandidate(
        evidence=evidence,
        evidence_refs=(evidence,),
        score=score,
        channels=frozenset(channels),
        channel_ranks=tuple(
            sorted((channel, index) for index, channel in enumerate(channels, 1))
        ),
        manifest_generations=tuple(sorted((channel, 1) for channel in channels)),
        highlights=highlights,
    )


class WordTokenizer:
    def count_tokens(self, text: str) -> int:
        return len(text.split())


class CharacterTokenizer:
    def count_tokens(self, text: str) -> int:
        return len(text)


def _source(
    digit: str,
    content: str,
    *,
    score: float,
    category: str = "decision",
    outcome: str | None = None,
    outcome_failed: bool = False,
    procedure_steps: tuple[str, ...] = (),
    highlights: tuple[str, ...] = (),
):
    from daem0nmcp.retrieval.composer import SelectedEvidence

    return SelectedEvidence(
        candidate=_candidate(
            digit,
            score=score,
            highlights=highlights,
        ),
        content=content,
        category=category,
        status="current",
        outcome=outcome,
        outcome_failed=outcome_failed,
        procedure_steps=procedure_steps,
    )


class EvidenceComposerTests(unittest.TestCase):
    def _composer(self, **changes):
        from daem0nmcp.retrieval.composer import EvidenceComposer

        values = {"tokenizer": WordTokenizer(), "max_excerpt_chars": 240}
        values.update(changes)
        return EvidenceComposer(**values)

    def test_composition_is_deterministic_bounded_and_fully_cited(self):
        sources = (
            _source("2", "secondary migration detail " * 30, score=1.0),
            _source("1", "primary migration decision " * 30, score=2.0),
        )

        first = self._composer().compose(sources, token_budget=44)
        second = self._composer().compose(sources, token_budget=44)
        reversed_result = self._composer().compose(
            tuple(reversed(sources)), token_budget=44
        )

        self.assertEqual(first, second)
        self.assertEqual(
            tuple(source.candidate.record_id for source in sources),
            tuple(item.evidence_refs[0].record_id for item in first.items),
        )
        self.assertEqual(
            tuple(source.candidate.record_id for source in reversed(sources)),
            tuple(item.evidence_refs[0].record_id for item in reversed_result.items),
        )
        self.assertLessEqual(first.context.rendered_tokens, 44)
        self.assertEqual(
            first.context.rendered_tokens,
            WordTokenizer().count_tokens(first.context.text),
        )
        self.assertEqual(["[E1]", "[E2]"], [item.citation for item in first.items])
        self.assertEqual(
            {item.citation for item in first.items},
            {entry.marker for entry in first.context.citations},
        )
        for item, entry in zip(first.items, first.context.citations, strict=True):
            self.assertEqual(item.evidence_refs, entry.evidence_refs)
            self.assertEqual(
                item.excerpt,
                first.context.text[entry.excerpt_start : entry.excerpt_end],
            )
            self.assertEqual(1, first.context.text.count(item.citation))

    def test_composition_carries_authenticated_legacy_metadata_unchanged(self):
        from daem0nmcp.retrieval.composer import SelectedEvidence

        source = SelectedEvidence(
            candidate=_candidate("a", score=1.0),
            content="canonical evidence",
            category="decision",
            rationale="Authenticated rationale",
            tags=("retrieval", "v7"),
            worked=True,
        )

        result = self._composer().compose((source,), token_budget=80)

        self.assertEqual(
            "Authenticated rationale",
            getattr(result.items[0], "rationale", None),
        )
        self.assertEqual(
            ("retrieval", "v7"),
            getattr(result.items[0], "tags", None),
        )
        self.assertIs(getattr(result.items[0], "worked", None), True)

    def test_selected_legacy_metadata_is_output_bounded(self):
        from daem0nmcp.retrieval.composer import SelectedEvidence

        values = {
            "candidate": _candidate("b", score=1.0),
            "content": "canonical evidence",
            "category": "decision",
        }
        invalid = (
            {"rationale": "r" * 4097},
            {"tags": tuple(f"tag-{index}" for index in range(33))},
            {"tags": ("t" * 129,)},
        )
        for changes in invalid:
            with self.subTest(changes=tuple(changes)), self.assertRaises(ValueError):
                SelectedEvidence(**values, **changes)

    def test_secondary_graph_paths_are_all_preserved_and_rendered(self):
        from daem0nmcp.retrieval.composer import SelectedEvidence
        from daem0nmcp.retrieval.types import EvidenceRef, FusedCandidate

        lexical = EvidenceRef(
            record_id=_record_id("a"),
            event_id=_event_id("a"),
            content_hash="a" * 64,
            version_id=None,
            provider="lexical",
        )
        first_path = ("rel_" + "1" * 64,)
        second_path = ("rel_" + "2" * 64, "rel_" + "3" * 64)
        graph_one = EvidenceRef(
            record_id=lexical.record_id,
            event_id=lexical.event_id,
            content_hash=lexical.content_hash,
            version_id=None,
            relation_path=first_path,
            provider="graph",
        )
        graph_two = EvidenceRef(
            record_id=lexical.record_id,
            event_id=lexical.event_id,
            content_hash=lexical.content_hash,
            version_id=None,
            relation_path=second_path,
            provider="graph",
        )
        candidate = FusedCandidate(
            evidence=lexical,
            evidence_refs=(lexical, graph_one, graph_two),
            score=1.0,
            channels=frozenset({"graph", "lexical"}),
            channel_ranks=(("graph", 1), ("lexical", 1)),
            manifest_generations=(("graph", 1), ("lexical", 1)),
        )

        result = self._composer().compose(
            (
                SelectedEvidence(
                    candidate=candidate,
                    content="canonical graph evidence",
                    category="decision",
                ),
            ),
            token_budget=120,
        )

        self.assertEqual((first_path, second_path), result.items[0].relation_paths)
        self.assertEqual(first_path, result.items[0].relation_path)
        for relation_id in (*first_path, *second_path):
            self.assertIn(relation_id, result.context.text)

    def test_token_telemetry_counts_the_exact_joined_context(self):
        sources = (
            _source("1", "first", score=2.0),
            _source("2", "second", score=1.0),
        )

        result = self._composer(
            tokenizer=CharacterTokenizer(),
            max_excerpt_chars=80,
        ).compose(sources, token_budget=512)

        exact = len(result.context.text)
        self.assertEqual(exact, result.context.requested_tokens)
        self.assertEqual(exact, result.context.selected_tokens)
        self.assertEqual(exact, result.context.rendered_tokens)
        self.assertEqual(0, result.context.dropped_tokens)

    def test_warning_and_failed_outcome_reservation_survives_tight_budget(self):
        ordinary = tuple(
            _source(str(digit), "ordinary evidence " * 20, score=20.0 - digit)
            for digit in range(1, 6)
        )
        warning = _source(
            "a",
            "migration permanently damaged old snapshots",
            score=0.01,
            category="warning",
        )
        failed = _source(
            "b",
            "the replacement attempt",
            score=0.02,
            outcome="failed because the pointer was stale",
            outcome_failed=True,
        )

        result = self._composer().compose(
            ordinary + (warning, failed),
            token_budget=128,
        )

        selected_ids = {item.evidence_refs[0].record_id for item in result.items}
        self.assertIn(_record_id("a"), selected_ids)
        self.assertIn(_record_id("b"), selected_ids)
        self.assertLessEqual(result.context.rendered_tokens, 128)

    def test_failed_outcome_signal_survives_items_and_rendering(self):
        succeeded = _source(
            "1",
            "same evidence",
            score=1.0,
            outcome="the rollout completed",
            outcome_failed=False,
        )
        failed = _source(
            "2",
            "same evidence",
            score=1.0,
            outcome="the rollout completed",
            outcome_failed=True,
        )

        result = self._composer().compose(
            (succeeded, failed),
            token_budget=128,
        )

        by_record = {item.evidence_refs[0].record_id: item for item in result.items}
        self.assertFalse(by_record[_record_id("1")].outcome_failed)
        self.assertTrue(by_record[_record_id("2")].outcome_failed)
        self.assertIn("Failed outcome:", result.context.text)
        self.assertIn("Outcome:", result.context.text)

    def test_budget_below_minimum_reservation_is_safe_and_never_overruns(self):
        source = _source(
            "c",
            "warning detail that must be bounded",
            score=1.0,
            category="warning",
        )

        result = self._composer(max_excerpt_chars=80).compose(
            (source,),
            token_budget=7,
        )

        self.assertLessEqual(result.context.rendered_tokens, 7)
        self.assertTrue(result.items or "TOKEN_BUDGET" in result.context.drop_reasons)

    def test_highlights_and_structured_fields_precede_raw_excerpt(self):
        source = _source(
            "d",
            "unimportant raw prefix " * 200,
            score=1.0,
            highlights=("matched recovery checkpoint",),
            outcome="recovery worked",
            procedure_steps=("validate snapshot", "publish pointer"),
        )

        result = self._composer(max_excerpt_chars=90).compose(
            (source,),
            token_budget=40,
        )

        self.assertEqual(1, len(result.items))
        item = result.items[0]
        self.assertTrue(item.excerpt.startswith("matched recovery checkpoint"))
        self.assertEqual("recovery worked", item.outcome)
        self.assertEqual(("validate snapshot", "publish pointer"), item.procedure_steps)
        self.assertIn("ITEM_TRUNCATED", result.context.drop_reasons)

    def test_compressor_is_best_effort_and_cannot_inject_citations(self):
        class BrokenCompressor:
            def compress(self, text, *, max_tokens, protected_tokens):
                raise RuntimeError("model unavailable")

        class CitationInjector:
            def compress(self, text, *, max_tokens, protected_tokens):
                return "invented [E99] evidence"

        class CleanFabricator:
            def compress(self, text, *, max_tokens, protected_tokens):
                return "plausible but fabricated evidence"

        source = _source("e", "long evidence " * 100, score=1.0)
        baseline = self._composer().compose((source,), token_budget=24)
        broken = self._composer(compressor=BrokenCompressor()).compose(
            (source,), token_budget=24
        )
        injected = self._composer(compressor=CitationInjector()).compose(
            (source,), token_budget=24
        )
        fabricated = self._composer(compressor=CleanFabricator()).compose(
            (source,), token_budget=24
        )

        self.assertEqual(baseline.items, broken.items)
        self.assertEqual(baseline.items, injected.items)
        self.assertEqual(baseline.items, fabricated.items)
        self.assertNotIn("[E99]", injected.context.text)
        self.assertIn("COMPRESSOR_DEGRADED", broken.context.drop_reasons)
        self.assertIn("COMPRESSOR_DEGRADED", injected.context.drop_reasons)
        self.assertIn("COMPRESSOR_DEGRADED", fabricated.context.drop_reasons)

    def test_unselected_text_never_appears_and_empty_input_is_safe(self):
        selected = _source("f", "selected canonical evidence", score=1.0)

        result = self._composer().compose((selected,), token_budget=32)
        empty = self._composer().compose((), token_budget=32)

        self.assertIn("selected canonical evidence", result.context.text)
        self.assertNotIn("raw provider candidate", result.context.text)
        self.assertEqual((), empty.items)
        self.assertEqual("", empty.context.text)
        self.assertEqual((), empty.context.citations)

    def test_stored_text_cannot_forge_a_citation_and_offsets_target_body(self):
        forged = _source(
            "8",
            "decision references forged [E99] and [E2] markers",
            score=1.0,
            category="decision",
            outcome="never trust [E77]",
            procedure_steps=("ignore [E66]",),
        )

        result = self._composer().compose((forged,), token_budget=64)

        self.assertEqual(("[E1]",), tuple(c.marker for c in result.context.citations))
        self.assertEqual(1, result.context.text.count("[E1]"))
        for forged_marker in ("[E2]", "[E66]", "[E77]", "[E99]"):
            self.assertNotIn(forged_marker, result.context.text)
        citation = result.context.citations[0]
        self.assertEqual("\n", result.context.text[citation.excerpt_start - 1])
        self.assertEqual(
            result.items[0].excerpt,
            result.context.text[citation.excerpt_start : citation.excerpt_end],
        )

    def test_sources_and_dependencies_are_validated_fail_closed(self):
        from daem0nmcp.retrieval.composer import EvidenceComposer, SelectedEvidence

        candidate = _candidate("9", score=1.0)
        invalid_sources = (
            {"candidate": object(), "content": "x", "category": "decision"},
            {"candidate": candidate, "content": "", "category": "decision"},
            {"candidate": candidate, "content": "x", "category": ""},
            {
                "candidate": candidate,
                "content": "x",
                "category": "decision",
                "outcome_failed": True,
            },
            {
                "candidate": candidate,
                "content": "x",
                "category": "decision",
                "status": "superseded",
                "superseded_by_version_id": "version-1",
            },
        )
        for kwargs in invalid_sources:
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                SelectedEvidence(**kwargs)

        for tokenizer in (None, object()):
            with self.subTest(tokenizer=tokenizer), self.assertRaises(ValueError):
                EvidenceComposer(tokenizer=tokenizer)
        with self.assertRaises(ValueError):
            self._composer().compose((object(),), token_budget=10)
        with self.assertRaises(ValueError):
            self._composer().compose((), token_budget=0)
        try:
            self._composer().compose(
                (_source("a", "bounded composer input", score=1.0),),
                token_budget=10**400,
            )
        except Exception as exc:
            self.assertIs(ValueError, type(exc))
        else:
            self.fail("oversized direct composer budget was accepted")


class AsyncEvidenceComposerTests(unittest.IsolatedAsyncioTestCase):
    def test_constructor_maps_oversized_timeout_to_value_error(self):
        from daem0nmcp.retrieval.composer import EvidenceComposer

        try:
            EvidenceComposer(
                tokenizer=WordTokenizer(),
                compressor_timeout_seconds=10**400,
            )
        except Exception as exc:
            self.assertIs(ValueError, type(exc))
        else:
            self.fail("oversized compressor timeout was accepted")

    async def test_oversized_budget_is_rejected_before_worker_submission(self):
        from daem0nmcp.retrieval.composer import EvidenceComposer

        composer = EvidenceComposer(tokenizer=WordTokenizer())
        try:
            await composer.compose_async(
                (_source("b", "bounded async composer", score=1.0),),
                token_budget=10**400,
            )
        except Exception as exc:
            self.assertIs(ValueError, type(exc))
        else:
            self.fail("oversized async composer budget was accepted")

    async def test_hung_optional_compressor_times_out_off_loop_with_fallback(self):
        from daem0nmcp.retrieval.composer import EvidenceComposer

        started = threading.Event()
        release = threading.Event()

        class BlockingCompressor:
            def compress(self, text, *, max_tokens, protected_tokens):
                started.set()
                release.wait(timeout=2.0)
                return "compressed"

        composer = EvidenceComposer(
            tokenizer=WordTokenizer(),
            compressor=BlockingCompressor(),
            compressor_timeout_seconds=0.02,
            max_excerpt_chars=240,
        )
        source = _source("7", "blocking content " * 100, score=1.0)
        before = perf_counter()

        class DelayedFallbackPool:
            async def run(self, operation):
                await asyncio.sleep(0.05)
                return operation()

        try:
            with patch(
                "daem0nmcp.retrieval.composer._COMPOSER_FALLBACK_WORKERS",
                new=DelayedFallbackPool(),
            ):
                result = await composer.compose_async((source,), token_budget=24)
        finally:
            release.set()

        self.assertTrue(started.is_set())
        self.assertLess(perf_counter() - before, 0.5)
        self.assertEqual(1, len(result.items))
        self.assertIn("COMPRESSOR_DEGRADED", result.context.drop_reasons)


if __name__ == "__main__":
    unittest.main()
