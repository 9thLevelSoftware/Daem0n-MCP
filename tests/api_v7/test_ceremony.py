"""Guided admission retains the real gate's binding and single-use semantics."""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from unittest.mock import patch

from daem0nmcp.api.v7.ceremony import (
    CounselMode,
    CovenantCeremony,
    _content_tokens,
    counsel_reasons,
)
from daem0nmcp.api.v7.models import PreflightGuidance, RecordSummary, RuleView
from daem0nmcp.api.v7.policy import V7_COVENANT_POLICY
from daem0nmcp.covenant import (
    CapabilityAuthority,
    CovenantGate,
    CovenantLevel,
    CovenantStateCapacityError,
    CovenantStateStore,
    InvocationScope,
)
from daem0nmcp.workspace import Workspace

NOW = datetime(2026, 10, 4, tzinfo=timezone.utc)


def normalize(
    operation: str, arguments: Mapping[str, Any] | None, workspace: str
) -> dict[str, Any]:
    del operation, workspace
    return {
        key: value
        for key, value in (arguments or {}).items()
        if key not in {"workspace_id", "preflight_token"}
    }


def rule(**constraints: Any) -> RuleView:
    return RuleView(
        rule_id="rule_" + "a" * 64,
        trigger="authentication",
        priority=10,
        enabled=True,
        created_at=NOW,
        **constraints,
    )


def record(
    excerpt: str, *, warning: bool = False, path: str | None = None
) -> RecordSummary:
    return RecordSummary(
        record_id="mem_" + "a" * 64,
        record_type="warning" if warning else "decision",
        excerpt=excerpt,
        relative_file_path=path,
        current_status="current",
        content_hash="b" * 64,
        created_at=NOW,
        updated_at=NOW,
    )


class GuidanceService:
    def __init__(self) -> None:
        self.value: object = PreflightGuidance()
        self.calls: list[tuple[str, Mapping[str, Any], str | None]] = []

    def guidance(
        self,
        workspace: Workspace,
        target_tool: str,
        normalized_arguments: Mapping[str, Any],
        description: str | None,
    ) -> object:
        del workspace
        self.calls.append((target_tool, normalized_arguments, description))
        return self.value


class CeremonyTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.now = 1_000
        self.workspace = Workspace("ws_" + "a" * 24, Path(self.temp.name).resolve())
        self.scope = InvocationScope("principal", "session", str(self.workspace.root))
        self.store = CovenantStateStore(clock=lambda: self.now, ttl_seconds=300)
        self.authority = CapabilityAuthority(
            secret=b"test-covenant-key-is-at-least-32-bytes!!",
            kid="test",
            clock=lambda: self.now,
            ttl_seconds=300,
        )
        self.gate = CovenantGate(
            state_store=self.store,
            authority=self.authority,
            policy=V7_COVENANT_POLICY,
            argument_normalizer=normalize,
        )
        self.service = GuidanceService()
        self.brief_calls = 0
        self.args = {
            "workspace_id": self.workspace.workspace_id,
            "content": "authentication session rotation",
            "record_type": "decision",
            "idempotency_key": "ceremony-test",
        }

        async def brief_reader(workspace: Workspace) -> Mapping[str, object]:
            self.assertEqual(self.workspace, workspace)
            self.brief_calls += 1
            return {"briefed_at": NOW, "workspace_statistics": {"records": 3}}

        self.ceremony = CovenantCeremony(
            covenant_gate=self.gate,
            argument_normalizer=normalize,
            preflight_service=self.service,
            brief_reader=brief_reader,
        )

    async def admit(
        self,
        tool: str = "memory_store",
        *,
        token: str | None = None,
        mode: CounselMode = "inline",
        consume: bool = True,
        scope: InvocationScope | None = None,
    ):
        return await self.ceremony.admit(
            tool,
            self.args,
            self.workspace,
            scope or self.scope,
            preflight_token=token,
            consume_capability=consume,
            counsel_mode=mode,
        )

    async def test_first_communion_call_briefs_and_admits(self) -> None:
        outcome = await self.admit("memory_recall", mode="off")
        self.assertIsNone(outcome.violation)
        self.assertIsNone(outcome.challenge)
        self.assertEqual(3, outcome.notice.auto_brief.workspace_statistics["records"])
        self.assertTrue(self.store.is_briefed(self.scope))
        self.assertIsNone((await self.admit("memory_recall")).notice)
        self.assertEqual(1, self.brief_calls)

    async def test_brief_read_and_validation_fail_closed(self) -> None:
        original = self.gate.authorize("memory_store", self.args, self.scope)
        for effect in (RuntimeError("unavailable"), {"briefed_at": "invalid"}):
            with self.subTest(effect=effect):

                async def failed_reader(workspace, effect=effect):
                    if isinstance(effect, Exception):
                        raise effect
                    return effect

                with patch.object(self.ceremony, "_brief_reader", failed_reader):
                    outcome = await self.admit()
                self.assertEqual(original, outcome.violation)
                self.assertIsNone(outcome.notice)
                self.assertFalse(self.store.is_briefed(self.scope))

    async def test_benign_counsel_echoes_bounded_must_do_and_consumes(self) -> None:
        self.service.value = PreflightGuidance(
            rules=[rule(must_do=["Run targeted checks."])],
            must_do=[f"Instruction {index}" for index in range(25)],
            warnings=["Global warnings are not risk evidence."],
        )
        issued: list[str] = []
        real_issue = self.gate.issue_preflight

        def issue(*args):
            token = real_issue(*args)
            issued.append(token)
            return token

        with patch.object(self.gate, "issue_preflight", issue):
            outcome = await self.admit()
        self.assertIsNone(outcome.violation)
        self.assertEqual(20, len(outcome.notice.counsel.must_do))
        self.assertEqual("memory_store", outcome.notice.counsel.target_tool)
        self.assertIsNotNone(outcome.notice.auto_brief)
        self.assertEqual(1, len(issued))
        self.assertEqual(
            "TOKEN_REPLAYED",
            self.gate.authorize(
                "memory_store", self.args, self.scope, preflight_token=issued[0]
            )["violation"],
        )
        digest = self.gate.fingerprint("memory_store", self.args, self.scope)
        self.assertFalse(
            self.store.has_matching_grant(self.scope, "memory_store", digest)
        )
        self.assertEqual(
            ("memory_store", normalize("memory_store", self.args, ""), None),
            self.service.calls[0],
        )

    async def test_rule_challenge_retry_once_and_replay_refresh(self) -> None:
        self.service.value = PreflightGuidance(
            rules=[rule(must_not=["Do not disable authentication."])]
        )
        first = await self.admit()
        self.assertEqual(["RULE_MUST_NOT"], first.challenge.reasons)
        token = first.challenge.preflight_token
        self.assertEqual(
            datetime.fromtimestamp(self.now + 300, timezone.utc),
            first.challenge.expires_at,
        )
        self.assertIsNone((await self.admit(token=token)).violation)
        self.assertEqual(
            "TOKEN_REPLAYED",
            self.gate.authorize(
                "memory_store", self.args, self.scope, preflight_token=token
            )["violation"],
        )
        third = await self.admit(token=token)
        self.assertEqual(["RULE_MUST_NOT"], third.challenge.reasons)
        self.assertNotEqual(token, third.challenge.preflight_token)

    async def test_destructive_always_challenges(self) -> None:
        outcome = await self.admit("memory_prune")
        self.assertEqual(["DESTRUCTIVE_OPERATION"], outcome.challenge.reasons)

    async def test_related_records_escalate_only_content_or_equal_path(self) -> None:
        cases = [
            (
                record("authentication rotation was unsuccessful"),
                {},
                ["RELATED_FAILURE"],
            ),
            (
                record("authentication rotation warning", warning=True),
                {},
                ["RELATED_WARNING"],
            ),
            (
                record("content relative_file_path record_type failed memory warning"),
                {},
                [],
            ),
            (record("authentication only"), {}, []),
            (
                record("entirely unrelated", path="src/auth.py"),
                {"relative_file_path": "src/auth.py"},
                ["RELATED_FAILURE"],
            ),
            (
                record("ceremony-test workspace"),
                {"idempotency_key": "ceremony-test workspace"},
                [],
            ),
        ]
        for item, extra, expected in cases:
            with self.subTest(excerpt=item.excerpt):
                self.service.value = PreflightGuidance(records=[item])
                self.args.update(extra)
                outcome = await self.admit()
                if expected:
                    self.assertEqual(expected, outcome.challenge.reasons)
                else:
                    self.assertIsNone(outcome.challenge)
                    self.assertIsNone(outcome.violation)
                self.args.pop("relative_file_path", None)

    async def test_unrecoverable_tokens_return_original_without_counsel(self) -> None:
        self.gate.record_briefing(self.scope)
        token = self.gate.issue_preflight(self.scope, "memory_store", self.args)
        other = InvocationScope("other", "session", str(self.workspace.root))
        self.gate.record_briefing(other)
        for supplied, scope, tool, expected in (
            (token, other, "memory_store", "TOKEN_SCOPE_MISMATCH"),
            (token + "x", self.scope, "memory_store", "TOKEN_TAMPERED"),
            (token, self.scope, "rule_create", "TOKEN_OPERATION_MISMATCH"),
            ("{}", self.scope, "memory_store", "TOKEN_LEGACY_UNSUPPORTED"),
        ):
            with self.subTest(expected=expected):
                original = self.gate.authorize(
                    tool, self.args, scope, preflight_token=supplied
                )
                outcome = await self.admit(tool, token=supplied, scope=scope)
                self.assertEqual(expected, outcome.violation["violation"])
                self.assertEqual(original, outcome.violation)
                self.assertIsNone(outcome.challenge)
        self.assertEqual([], self.service.calls)

    async def test_expired_and_argument_mismatch_tokens_recompute_counsel(self) -> None:
        self.gate.record_briefing(self.scope)
        self.service.value = PreflightGuidance(
            rules=[rule(ask_first=["Confirm rotation."])]
        )
        token = self.gate.issue_preflight(self.scope, "memory_store", self.args)
        self.args["content"] = "different content"
        self.assertEqual(
            "TOKEN_ARGUMENT_MISMATCH",
            self.gate.authorize(
                "memory_store", self.args, self.scope, preflight_token=token
            )["violation"],
        )
        mismatch = await self.admit(token=token)
        self.assertEqual(["RULE_ASK_FIRST"], mismatch.challenge.reasons)
        self.now += 301
        self.assertEqual(
            "TOKEN_EXPIRED",
            self.gate.authorize(
                "memory_store",
                self.args,
                self.scope,
                preflight_token=mismatch.challenge.preflight_token,
            )["violation"],
        )
        expired = await self.admit(token=mismatch.challenge.preflight_token)
        self.assertIsNotNone(expired.notice.auto_brief)
        self.assertEqual(["RULE_ASK_FIRST"], expired.challenge.reasons)

    async def test_challenge_mode_mints_without_consumption_and_off_stays_strict(
        self,
    ) -> None:
        outcome = await self.admit(mode="challenge", consume=False)
        self.assertEqual(["TASK_ADMISSION"], outcome.challenge.reasons)
        token = outcome.challenge.preflight_token
        self.assertIsNone(
            self.gate.authorize(
                "memory_store",
                self.args,
                self.scope,
                preflight_token=token,
                consume_capability=False,
            )
        )
        self.assertIsNone(
            self.gate.authorize(
                "memory_store", self.args, self.scope, preflight_token=token
            )
        )
        calls = len(self.service.calls)
        original = self.gate.authorize("memory_store", self.args, self.scope)
        off = await self.admit(mode="off")
        self.assertEqual(original, off.violation)
        self.assertEqual("COUNSEL_REQUIRED", off.violation["violation"])
        self.assertEqual(calls, len(self.service.calls))

    async def test_token_missing_is_counseled(self) -> None:
        self.gate.record_briefing(self.scope)
        self.gate.issue_preflight(self.scope, "memory_store", self.args)
        self.assertEqual(
            "TOKEN_MISSING",
            self.gate.authorize("memory_store", self.args, self.scope)["violation"],
        )
        self.assertIsNone((await self.admit()).violation)

    async def test_counsel_failures_retain_violation_and_auto_brief(self) -> None:
        for stage, effect in (
            ("guidance", RuntimeError("unavailable")),
            ("guidance", {"unexpected": True}),
            ("issue_preflight", CovenantStateCapacityError("full")),
            ("verify", RuntimeError("unavailable")),
        ):
            with self.subTest(stage=stage):
                owner = (
                    self.service
                    if stage == "guidance"
                    else self.authority
                    if stage == "verify"
                    else self.gate
                )
                options = (
                    {"side_effect": effect}
                    if isinstance(effect, Exception)
                    else {"return_value": effect}
                )
                with patch.object(owner, stage, **options):
                    outcome = await self.admit()
                self.assertIn(
                    outcome.violation["violation"],
                    {"COUNSEL_REQUIRED", "TOKEN_MISSING"},
                )
                self.assertIsNone(outcome.challenge)
                self.assertTrue(self.store.is_briefed(self.scope))
                if stage == "guidance" and isinstance(effect, Exception):
                    self.assertIsNotNone(outcome.notice.auto_brief)

    async def test_async_guidance_and_cancellation_propagate(self) -> None:
        async def guidance(*args):
            return PreflightGuidance()

        with patch.object(self.service, "guidance", guidance):
            self.assertIsNone((await self.admit()).violation)

        async def cancelled_guidance(*args):
            raise asyncio.CancelledError

        with (
            patch.object(self.service, "guidance", cancelled_guidance),
            self.assertRaises(asyncio.CancelledError),
        ):
            await self.admit()
        for owner, method in (
            (self.service, "guidance"),
            (self.gate, "issue_preflight"),
        ):
            with (
                patch.object(owner, method, side_effect=asyncio.CancelledError),
                self.assertRaises(asyncio.CancelledError),
            ):
                await self.admit()

        async def cancelled(workspace):
            raise asyncio.CancelledError

        self.scope = InvocationScope(
            "principal", "new-session", str(self.workspace.root)
        )
        with (
            patch.object(self.ceremony, "_brief_reader", cancelled),
            self.assertRaises(asyncio.CancelledError),
        ):
            await self.admit()
        self.assertFalse(self.store.is_briefed(self.scope))

    async def test_missing_scope_remains_fail_closed(self) -> None:
        outcome = await self.ceremony.admit(
            "memory_store",
            self.args,
            self.workspace,
            None,
            preflight_token=None,
            consume_capability=True,
            counsel_mode="inline",
        )
        self.assertEqual("IDENTITY_UNAVAILABLE", outcome.violation["violation"])
        self.assertIsNone(outcome.notice)
        self.assertEqual([], self.service.calls)

    async def test_normalization_failure_retains_original_violation(self) -> None:
        self.gate.record_briefing(self.scope)
        original = self.gate.authorize("memory_store", self.args, self.scope)
        with patch.object(
            self.ceremony, "_normalizer", side_effect=ValueError("invalid")
        ):
            outcome = await self.admit()
        self.assertEqual(original, outcome.violation)
        self.assertIsNone(outcome.challenge)
        self.assertEqual([], self.service.calls)

    async def test_inline_admission_only_preserves_internal_grant(self) -> None:
        outcome = await self.admit(consume=False)
        self.assertIsNone(outcome.violation)
        digest = self.gate.fingerprint("memory_store", self.args, self.scope)
        self.assertTrue(
            self.store.has_matching_grant(self.scope, "memory_store", digest)
        )

    async def test_post_brief_scope_mismatch_is_not_recovered(self) -> None:
        self.gate.record_briefing(self.scope)
        token = self.gate.issue_preflight(self.scope, "memory_store", self.args)
        self.scope = InvocationScope(
            "principal", "new-session", str(self.workspace.root)
        )
        self.args["content"] = "changed authentication rotation"
        outcome = await self.admit(token=token)
        self.assertIsNotNone(outcome.notice.auto_brief)
        self.assertEqual("TOKEN_SCOPE_MISMATCH", outcome.violation["violation"])
        self.assertEqual([], self.service.calls)

    async def test_post_brief_argument_mismatch_recomputes_counsel(self) -> None:
        self.gate.record_briefing(self.scope)
        token = self.gate.issue_preflight(self.scope, "memory_store", self.args)
        self.args["content"] = "changed authentication rotation"
        real_is_briefed = self.store.is_briefed
        checks = 0

        def is_briefed(scope):
            nonlocal checks
            checks += 1
            return checks > 1 and real_is_briefed(scope)

        with patch.object(self.store, "is_briefed", is_briefed):
            outcome = await self.admit(token=token)
        self.assertIsNone(outcome.violation)
        self.assertIsNotNone(outcome.notice.auto_brief)
        self.assertIsNotNone(outcome.notice.counsel)

    async def test_preflight_argument_is_not_normalized_or_bound(self) -> None:
        self.args["preflight_token"] = "ignored-stale-token"
        outcome = await self.admit()
        self.assertIsNone(outcome.violation)
        self.assertNotIn("preflight_token", self.service.calls[0][1])


class ContentRiskTests(unittest.TestCase):
    def test_tokens_ignore_keys_and_nonstrings_and_split_components(self) -> None:
        self.assertEqual(
            frozenset(
                {
                    "authentication-session",
                    "authentication",
                    "session",
                    "rotate_cache",
                    "rotate",
                    "cache",
                }
            ),
            _content_tokens(
                {
                    "private_key": [
                        "AUTHENTICATION-session",
                        ("rotate_cache", None, 1234),
                    ],
                    "failed": "memory warning with",
                }
            ),
        )

    def test_reasons_are_ordered_and_deduplicated(self) -> None:
        constraints = rule(
            must_not=["Never bypass."],
            ask_first=["Ask first."],
            warnings=["Take care."],
        )
        guidance = PreflightGuidance(
            rules=[constraints, constraints],
            records=[
                record("authentication rotation"),
                record("authentication rotation", warning=True),
                record("authentication rotation"),
            ],
        )
        self.assertEqual(
            [
                "DESTRUCTIVE_OPERATION",
                "RULE_MUST_NOT",
                "RULE_ASK_FIRST",
                "RULE_WARNING",
                "RELATED_FAILURE",
                "RELATED_WARNING",
            ],
            counsel_reasons(
                CovenantLevel.DESTRUCTIVE,
                guidance,
                {"content": "authentication rotation"},
            ),
        )
