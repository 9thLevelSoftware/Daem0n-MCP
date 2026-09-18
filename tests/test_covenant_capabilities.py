"""Focused security proof for the action-aware Covenant capability gate."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import tempfile
import unittest
from pathlib import Path

import daem0nmcp.covenant as covenant
from daem0nmcp.covenant import (
    ACTION_ARGUMENT_DEFAULTS,
    COVENANT_POLICY,
    CapabilityAuthority,
    CovenantGate,
    CovenantLevel,
    CovenantStateCapacityError,
    CovenantStateStore,
    InvocationScope,
    UnknownCovenantOperation,
    authority_from_environment,
    authorize_workflow_call,
    canonical_json,
)


class MutableClock:
    def __init__(self, value: int = 1_000) -> None:
        self.value = value

    def __call__(self) -> int:
        return self.value


class CovenantPolicyTests(unittest.TestCase):
    def test_policy_inventory_matches_all_dispatchers(self) -> None:
        from daem0nmcp.workflows import (
            commune,
            consult,
            explore,
            govern,
            inscribe,
            maintain,
            reflect,
            understand,
        )

        dispatchers = {
            "commune": commune,
            "consult": consult,
            "inscribe": inscribe,
            "reflect": reflect,
            "understand": understand,
            "govern": govern,
            "explore": explore,
            "maintain": maintain,
        }
        expected = {
            f"{workflow}.{action}"
            for workflow, module in dispatchers.items()
            for action in module.VALID_ACTIONS
        }

        expected.update({"simulate_decision", "evolve_rule", "debate_internal"})
        self.assertEqual(expected, COVENANT_POLICY.operations)
        self.assertEqual(expected, set(ACTION_ARGUMENT_DEFAULTS))
        for operation in expected:
            self.assertIsInstance(COVENANT_POLICY.resolve(operation, {}), CovenantLevel)

    def test_standalone_cognitive_tools_have_explicit_policy_and_schemas(self) -> None:
        expected = {
            "simulate_decision": (
                CovenantLevel.COMMUNION,
                {"decision_id": None},
            ),
            "evolve_rule": (CovenantLevel.COMMUNION, {"rule_id": None}),
            "debate_internal": (
                CovenantLevel.COUNSEL,
                {
                    "topic": None,
                    "advocate_position": None,
                    "challenger_position": None,
                },
            ),
        }
        for operation, (level, defaults) in expected.items():
            with self.subTest(operation=operation):
                self.assertEqual(level, COVENANT_POLICY.resolve(operation, {}))
                self.assertEqual(defaults, dict(ACTION_ARGUMENT_DEFAULTS[operation]))

        self.assertEqual(
            {
                "simulate_decision": frozenset({"decision_id"}),
                "evolve_rule": frozenset(),
                "debate_internal": frozenset(
                    {"topic", "advocate_position", "challenger_position"}
                ),
            },
            {
                operation: getattr(covenant, "ACTION_REQUIRED_ARGUMENTS", {}).get(
                    operation
                )
                for operation in expected
            },
        )

    def test_argument_sensitive_policy_uses_effective_defaults(self) -> None:
        self.assertEqual(
            CovenantLevel.COMMUNION,
            COVENANT_POLICY.resolve("understand.todos", {}),
        )
        self.assertEqual(
            CovenantLevel.COUNSEL,
            COVENANT_POLICY.resolve("understand.todos", {"auto_remember": True}),
        )
        for action in ("prune", "cleanup", "compact", "purge_dream_spam"):
            operation = f"maintain.{action}"
            self.assertEqual(
                CovenantLevel.COMMUNION,
                COVENANT_POLICY.resolve(operation, {}),
            )
            self.assertEqual(
                CovenantLevel.DESTRUCTIVE,
                COVENANT_POLICY.resolve(operation, {"dry_run": False}),
            )
        self.assertEqual(
            CovenantLevel.COUNSEL,
            COVENANT_POLICY.resolve("maintain.consolidate", {}),
        )
        self.assertEqual(
            CovenantLevel.DESTRUCTIVE,
            COVENANT_POLICY.resolve("maintain.consolidate", {"archive_sources": True}),
        )

    def test_unknown_operation_fails_closed(self) -> None:
        with self.assertRaises(UnknownCovenantOperation):
            COVENANT_POLICY.resolve("inscribe.future_action", {})

    def test_argument_sensitive_booleans_reject_coercible_values(self) -> None:
        for operation, arguments in (
            ("understand.todos", {"auto_remember": 0}),
            ("maintain.cleanup", {"dry_run": 0}),
            ("maintain.consolidate", {"archive_sources": "false"}),
        ):
            with self.subTest(operation=operation), self.assertRaises(ValueError):
                COVENANT_POLICY.resolve(operation, arguments)

    def test_remote_secret_has_no_default_and_requires_32_bytes(self) -> None:
        self.assertIsNone(authority_from_environment(local_stdio=False, environ={}))
        self.assertIsNone(
            authority_from_environment(
                local_stdio=False,
                environ={"DAEM0NMCP_TOKEN_SECRET": "too-short"},
            )
        )
        authority = authority_from_environment(
            local_stdio=False,
            environ={"DAEM0NMCP_TOKEN_SECRET": "x" * 32},
        )
        self.assertIsNotNone(authority)
        self.assertEqual("primary", authority.kid)


class CapabilityGateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.workspace = str(Path(self.temp.name).resolve())
        self.clock = MutableClock()
        self.store = CovenantStateStore(clock=self.clock, ttl_seconds=300)
        self.authority = CapabilityAuthority(
            secret=b"test-covenant-key-is-at-least-32-bytes!!",
            kid="test-key",
            clock=self.clock,
            ttl_seconds=300,
        )
        self.gate = CovenantGate(
            state_store=self.store,
            authority=self.authority,
        )
        self.scope_a = InvocationScope("principal-a", "session-a", self.workspace)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _brief(self, scope: InvocationScope | None = None) -> InvocationScope:
        actual = scope or self.scope_a
        self.gate.record_briefing(actual)
        return actual

    def _issue(
        self,
        operation: str = "inscribe.remember",
        args: dict | None = None,
        scope: InvocationScope | None = None,
    ) -> str:
        actual_scope = self._brief(scope)
        return self.gate.issue_preflight(
            actual_scope,
            operation,
            args or {"category": "decision", "content": "bind this content"},
        )

    def test_consolidated_mutations_are_blocked_before_briefing(self) -> None:
        cases = {
            "inscribe.remember": {
                "category": "decision",
                "content": "x",
            },
            "govern.add_rule": {"trigger": "editing auth"},
            "maintain.import_data": {"data": {}},
            "reflect.execute": {"code": "print('x')"},
        }
        for operation, args in cases.items():
            with self.subTest(operation=operation):
                result = self.gate.authorize(operation, args, self.scope_a)
                self.assertEqual("COMMUNION_REQUIRED", result["violation"])

    def test_state_store_bounds_scopes_and_per_scope_capabilities(self) -> None:
        store = CovenantStateStore(
            clock=self.clock,
            ttl_seconds=300,
            max_scopes=2,
            max_capabilities_per_scope=3,
        )
        gate = CovenantGate(
            state_store=store,
            authority=CapabilityAuthority(
                secret=b"bounded-capability-store-secret-32b",
                kid="bounded-test",
                clock=self.clock,
                ttl_seconds=300,
            ),
        )
        scopes = [
            InvocationScope(f"principal-{index}", "session", self.workspace)
            for index in range(3)
        ]
        for scope in scopes[:2]:
            gate.record_briefing(scope)
        with self.assertRaises(CovenantStateCapacityError):
            gate.record_briefing(scopes[2])
        self.assertEqual(2, len(store._states))
        self.assertIn(scopes[0], store._states)
        self.assertTrue(store.is_briefed(scopes[0]))

        self.clock.value += 301
        gate.record_briefing(scopes[2])
        self.assertEqual([scopes[2]], list(store._states))

        scope = scopes[-1]
        tokens: list[tuple[str, dict]] = []
        issued_identity = None
        for index in range(3):
            args = {"category": "decision", "content": f"pending-{index}"}
            tokens.append(
                (gate.issue_preflight(scope, "inscribe.remember", args), args)
            )
            state = store._states[scope]
            if issued_identity is None:
                issued_identity = state.issued
            else:
                self.assertIs(issued_identity, state.issued)
        with self.assertRaises(CovenantStateCapacityError):
            gate.issue_preflight(
                scope,
                "inscribe.remember",
                {"category": "decision", "content": "over-capacity"},
            )
        self.assertEqual(3, len(store._states[scope].issued))

        oldest_token, oldest_args = tokens[0]
        self.assertIsNone(
            gate.authorize(
                "inscribe.remember",
                oldest_args,
                scope,
                preflight_token=oldest_token,
                consume_capability=False,
            )
        )
        self.assertIsNone(
            gate.authorize(
                "inscribe.remember",
                oldest_args,
                scope,
                preflight_token=oldest_token,
            )
        )

        for index in range(10):
            args = {"category": "decision", "content": f"consumed-{index}"}
            token = gate.issue_preflight(scope, "inscribe.remember", args)
            self.assertIsNone(
                gate.authorize("inscribe.remember", args, scope, preflight_token=token)
            )
        self.assertLessEqual(len(store._states[scope].issued), 3)
        self.assertLessEqual(len(store._states[scope].consumed), 3)

    def test_missing_identity_fails_closed(self) -> None:
        result = self.gate.authorize(
            "inscribe.remember",
            {"category": "decision", "content": "x"},
            None,
        )
        self.assertEqual("IDENTITY_UNAVAILABLE", result["violation"])
        direct = authorize_workflow_call(
            "inscribe",
            "remember",
            {"category": "decision", "content": "x"},
        )
        self.assertEqual("IDENTITY_UNAVAILABLE", direct["violation"])

    def test_briefing_isolated_by_session_principal_and_workspace(self) -> None:
        self._brief()
        variants = (
            InvocationScope("principal-a", "session-b", self.workspace),
            InvocationScope("principal-b", "session-a", self.workspace),
            InvocationScope(
                "principal-a", "session-a", str(Path(self.workspace, "other").resolve())
            ),
        )
        for scope in variants:
            with self.subTest(scope=scope):
                result = self.gate.authorize("consult.recall", {"topic": "auth"}, scope)
                self.assertEqual("COMMUNION_REQUIRED", result["violation"])

    def test_token_is_bound_to_scope_operation_and_arguments(self) -> None:
        args = {"category": "decision", "content": "original", "tags": ["a", "b"]}
        token = self._issue(args=args)

        scope_b = InvocationScope("principal-a", "session-b", self.workspace)
        self._brief(scope_b)
        result = self.gate.authorize(
            "inscribe.remember", args, scope_b, preflight_token=token
        )
        self.assertEqual("TOKEN_SCOPE_MISMATCH", result["violation"])

        principal_b = InvocationScope("principal-b", "session-a", self.workspace)
        self._brief(principal_b)
        result = self.gate.authorize(
            "inscribe.remember", args, principal_b, preflight_token=token
        )
        self.assertEqual("TOKEN_SCOPE_MISMATCH", result["violation"])

        other_workspace = str(Path(self.workspace, "other").resolve())
        workspace_b = InvocationScope("principal-a", "session-a", other_workspace)
        self._brief(workspace_b)
        result = self.gate.authorize(
            "inscribe.remember", args, workspace_b, preflight_token=token
        )
        self.assertEqual("TOKEN_SCOPE_MISMATCH", result["violation"])

        result = self.gate.authorize(
            "govern.add_rule",
            {"trigger": "auth"},
            self.scope_a,
            preflight_token=token,
        )
        self.assertEqual("TOKEN_OPERATION_MISMATCH", result["violation"])

        changed = {**args, "content": "changed"}
        result = self.gate.authorize(
            "inscribe.remember", changed, self.scope_a, preflight_token=token
        )
        self.assertEqual("TOKEN_ARGUMENT_MISMATCH", result["violation"])

        reordered = {**args, "tags": ["b", "a"]}
        result = self.gate.authorize(
            "inscribe.remember", reordered, self.scope_a, preflight_token=token
        )
        self.assertEqual("TOKEN_ARGUMENT_MISMATCH", result["violation"])

    def test_defaulted_arguments_are_canonicalized_consistently(self) -> None:
        token = self._issue(operation="inscribe.pin", args={"memory_id": 7})
        self.assertIsNone(
            self.gate.authorize(
                "inscribe.pin",
                {"memory_id": 7, "pinned": True},
                self.scope_a,
                preflight_token=token,
            )
        )
        changed_id = self._issue(operation="inscribe.pin", args={"memory_id": 7})
        result = self.gate.authorize(
            "inscribe.pin",
            {"memory_id": 8},
            self.scope_a,
            preflight_token=changed_id,
        )
        self.assertEqual("TOKEN_ARGUMENT_MISMATCH", result["violation"])

    def test_argument_sensitive_capability_cannot_authorize_destructive_variant(
        self,
    ) -> None:
        token = self._issue(
            operation="maintain.consolidate",
            args={"archive_sources": False},
        )
        result = self.gate.authorize(
            "maintain.consolidate",
            {"archive_sources": True},
            self.scope_a,
            preflight_token=token,
        )
        self.assertEqual("TOKEN_ARGUMENT_MISMATCH", result["violation"])

    def test_tamper_noncanonical_malformed_and_legacy_tokens_are_rejected(self) -> None:
        args = {"category": "decision", "content": "x"}
        token = self._issue(args=args)
        payload_segment, signature_segment = token.split(".")
        payload = json.loads(
            base64.urlsafe_b64decode(payload_segment + "==").decode("utf-8")
        )
        payload["operation"] = "govern.add_rule"
        tampered_segment = (
            base64.urlsafe_b64encode(canonical_json(payload))
            .rstrip(b"=")
            .decode("ascii")
        )
        result = self.gate.authorize(
            "inscribe.remember",
            args,
            self.scope_a,
            preflight_token=f"{tampered_segment}.{signature_segment}",
        )
        self.assertEqual("TOKEN_TAMPERED", result["violation"])

        noncanonical = json.dumps(payload, indent=2).encode("utf-8")
        noncanonical_segment = (
            base64.urlsafe_b64encode(noncanonical).rstrip(b"=").decode("ascii")
        )
        noncanonical_signature = (
            base64.urlsafe_b64encode(
                hmac.new(
                    b"test-covenant-key-is-at-least-32-bytes!!",
                    noncanonical,
                    hashlib.sha256,
                ).digest()
            )
            .rstrip(b"=")
            .decode("ascii")
        )
        result = self.gate.authorize(
            "inscribe.remember",
            args,
            self.scope_a,
            preflight_token=f"{noncanonical_segment}.{noncanonical_signature}",
        )
        self.assertEqual("TOKEN_TAMPERED", result["violation"])

        def signed(payload_bytes: bytes) -> str:
            payload_part = (
                base64.urlsafe_b64encode(payload_bytes).rstrip(b"=").decode("ascii")
            )
            signature_part = (
                base64.urlsafe_b64encode(
                    hmac.new(
                        b"test-covenant-key-is-at-least-32-bytes!!",
                        payload_bytes,
                        hashlib.sha256,
                    ).digest()
                )
                .rstrip(b"=")
                .decode("ascii")
            )
            return f"{payload_part}.{signature_part}"

        invalid_payloads = []
        missing = dict(payload)
        missing.pop("nonce")
        invalid_payloads.append(canonical_json(missing))
        unknown = {**payload, "unexpected": True}
        invalid_payloads.append(canonical_json(unknown))
        bad_timestamp = {**payload, "iat": "1000"}
        invalid_payloads.append(canonical_json(bad_timestamp))
        duplicate_json = canonical_json(payload)[:-1] + b',"v":1}'
        invalid_payloads.append(duplicate_json)
        for invalid_payload in invalid_payloads:
            result = self.gate.authorize(
                "inscribe.remember",
                args,
                self.scope_a,
                preflight_token=signed(invalid_payload),
            )
            self.assertEqual("TOKEN_TAMPERED", result["violation"])

        for bad_token, code in (
            ("not-a-token", "TOKEN_TAMPERED"),
            ("x" * 8193, "TOKEN_TAMPERED"),
            (
                json.dumps(
                    {
                        "action": "legacy free text",
                        "session_id": "hourly-project-id",
                    }
                ),
                "TOKEN_LEGACY_UNSUPPORTED",
            ),
        ):
            with self.subTest(code=code):
                result = self.gate.authorize(
                    "inscribe.remember",
                    args,
                    self.scope_a,
                    preflight_token=bad_token,
                )
                self.assertEqual(code, result["violation"])

    def test_expiry_and_replay_are_enforced(self) -> None:
        args = {"category": "decision", "content": "x"}
        expired = self._issue(args=args)
        self.clock.value = 1_300
        result = self.gate.authorize(
            "inscribe.remember", args, self.scope_a, preflight_token=expired
        )
        self.assertEqual("TOKEN_EXPIRED", result["violation"])

        self.clock.value = 1_301
        result = self.gate.authorize(
            "inscribe.remember", args, self.scope_a, preflight_token=expired
        )
        self.assertEqual("TOKEN_EXPIRED", result["violation"])

        self.clock.value = 2_000
        fresh = self._issue(args=args)
        self.assertIsNone(
            self.gate.authorize(
                "inscribe.remember", args, self.scope_a, preflight_token=fresh
            )
        )
        result = self.gate.authorize(
            "inscribe.remember", args, self.scope_a, preflight_token=fresh
        )
        self.assertEqual("TOKEN_REPLAYED", result["violation"])

    def test_clock_rollback_and_nonconfigured_lifetime_are_rejected(self) -> None:
        args = {"category": "decision", "content": "x"}
        issued = self._issue(args=args)

        self.clock.value = 100
        result = self.gate.authorize(
            "inscribe.remember", args, self.scope_a, preflight_token=issued
        )
        self.assertEqual("TOKEN_TAMPERED", result["violation"])

        self.clock.value = 1_000
        payload_segment, _ = issued.split(".")
        payload = json.loads(
            base64.urlsafe_b64decode(payload_segment + "==").decode("utf-8")
        )
        payload["exp"] = payload["iat"] + 600
        payload_bytes = canonical_json(payload)
        payload_part = (
            base64.urlsafe_b64encode(payload_bytes).rstrip(b"=").decode("ascii")
        )
        signature_part = (
            base64.urlsafe_b64encode(
                hmac.new(
                    b"test-covenant-key-is-at-least-32-bytes!!",
                    payload_bytes,
                    hashlib.sha256,
                ).digest()
            )
            .rstrip(b"=")
            .decode("ascii")
        )
        result = self.gate.authorize(
            "inscribe.remember",
            args,
            self.scope_a,
            preflight_token=f"{payload_part}.{signature_part}",
        )
        self.assertEqual("TOKEN_TAMPERED", result["violation"])

    def test_expiry_crossed_during_atomic_consumption_is_rejected(self) -> None:
        args = {"category": "decision", "content": "x"}
        token = self._issue(args=args)
        ticks = iter((1_299, 1_299, 1_300))

        def stepping_clock() -> int:
            return next(ticks)

        self.authority._clock = stepping_clock
        self.store._clock = stepping_clock
        result = self.gate.authorize(
            "inscribe.remember", args, self.scope_a, preflight_token=token
        )
        self.assertEqual("TOKEN_EXPIRED", result["violation"])

    def test_observed_expiry_cannot_be_resurrected_by_clock_rollback(self) -> None:
        args = {"category": "decision", "content": "x"}
        token = self._issue(args=args)

        self.clock.value = 2_000
        expired = self.gate.authorize(
            "inscribe.remember", args, self.scope_a, preflight_token=token
        )
        self.assertEqual("TOKEN_EXPIRED", expired["violation"])

        self.clock.value = 1_100
        resurrected = self.gate.authorize(
            "inscribe.remember", args, self.scope_a, preflight_token=token
        )
        self.assertEqual("TOKEN_EXPIRED", resurrected["violation"])

    def test_json_surrogates_return_stable_argument_and_token_errors(self) -> None:
        invalid_text = json.loads('"\\ud800"')
        self._brief()
        valid_args = {"category": "decision", "content": "x"}

        result = self.gate.authorize(
            "inscribe.remember",
            valid_args,
            self.scope_a,
            preflight_token=invalid_text,
        )
        self.assertEqual("TOKEN_TAMPERED", result["violation"])

        invalid_args = {"category": "decision", "content": invalid_text}
        result = self.gate.authorize("inscribe.remember", invalid_args, self.scope_a)
        self.assertEqual("TOKEN_ARGUMENT_MISMATCH", result["violation"])

        with covenant.installed_invocation(self.scope_a, self.gate):
            response = covenant.issue_current_preflight_response(
                "inscribe.remember", invalid_args
            )
        self.assertEqual("TOKEN_ARGUMENT_MISMATCH", response["violation"])

        with self.assertRaises(covenant.ArgumentNormalizationError):
            canonical_json({invalid_text: "value"})

    def test_malformed_authenticated_payloads_return_token_tampered(self) -> None:
        args = {"category": "decision", "content": "x"}
        token = self._issue(args=args)
        payload_segment, _ = token.split(".")
        payload = json.loads(
            base64.urlsafe_b64decode(payload_segment + "==").decode("utf-8")
        )

        def sign(payload_bytes: bytes) -> str:
            payload_part = (
                base64.urlsafe_b64encode(payload_bytes).rstrip(b"=").decode("ascii")
            )
            signature_part = (
                base64.urlsafe_b64encode(
                    hmac.new(
                        b"test-covenant-key-is-at-least-32-bytes!!",
                        payload_bytes,
                        hashlib.sha256,
                    ).digest()
                )
                .rstrip(b"=")
                .decode("ascii")
            )
            return f"{payload_part}.{signature_part}"

        invalid_text = json.loads('"\\ud800"')
        surrogate_payload = {**payload, "principal": invalid_text}
        surrogate_bytes = json.dumps(
            surrogate_payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        ).encode("utf-8")
        canonical_payload = canonical_json(payload).decode("utf-8")
        nonfinite_bytes = canonical_payload.replace('"iat":1000', '"iat":1e999').encode(
            "utf-8"
        )
        huge_integer_bytes = canonical_payload.replace(
            '"iat":1000', f'"iat":{("9" * 4_400)}'
        ).encode("utf-8")

        for malformed in (surrogate_bytes, nonfinite_bytes, huge_integer_bytes):
            with self.subTest(size=len(malformed)):
                result = self.gate.authorize(
                    "inscribe.remember",
                    args,
                    self.scope_a,
                    preflight_token=sign(malformed),
                )
                self.assertEqual("TOKEN_TAMPERED", result["violation"])

    def test_excessive_argument_nesting_returns_stable_mismatch(self) -> None:
        nested: dict = {}
        for _ in range(600):
            nested = {"child": nested}
        args = {"data": nested}
        self._brief()

        result = self.gate.authorize("maintain.import_data", args, self.scope_a)
        self.assertEqual("TOKEN_ARGUMENT_MISMATCH", result["violation"])

        with covenant.installed_invocation(self.scope_a, self.gate):
            response = covenant.issue_current_preflight_response(
                "maintain.import_data", args
            )
        self.assertEqual("TOKEN_ARGUMENT_MISMATCH", response["violation"])

        with self.assertRaises(covenant.ArgumentNormalizationError):
            canonical_json(nested)

    def test_concurrent_replay_allows_exactly_one_admission(self) -> None:
        args = {"category": "decision", "content": "x"}
        token = self._issue(args=args)

        async def admit() -> dict | None:
            await asyncio.sleep(0)
            return self.gate.authorize(
                "inscribe.remember", args, self.scope_a, preflight_token=token
            )

        async def run() -> list[dict | None]:
            return await asyncio.gather(admit(), admit())

        results = asyncio.run(run())
        self.assertEqual(1, sum(result is None for result in results))
        self.assertEqual(
            ["TOKEN_REPLAYED"],
            [result["violation"] for result in results if result is not None],
        )

    def test_legacy_description_does_not_create_authority(self) -> None:
        self._brief()
        result = self.gate.authorize(
            "inscribe.remember",
            {"category": "decision", "content": "x"},
            self.scope_a,
        )
        self.assertEqual("COUNSEL_REQUIRED", result["violation"])
        self.assertEqual(
            "consult",
            result["remedy"]["tool"],
        )

    def test_issued_capability_must_be_present_and_target_must_be_protected(
        self,
    ) -> None:
        self._brief()
        with self.assertRaisesRegex(ValueError, "PREFLIGHT_TARGET_NOT_PROTECTED"):
            self.gate.issue_preflight(self.scope_a, "consult.recall", {"topic": "auth"})
        self.gate.issue_preflight(
            self.scope_a,
            "inscribe.remember",
            {"category": "decision", "content": "x"},
        )
        result = self.gate.authorize(
            "inscribe.remember",
            {"category": "decision", "content": "x"},
            self.scope_a,
        )
        self.assertEqual("TOKEN_MISSING", result["violation"])

    def test_invalid_structured_preflight_args_return_stable_violation(self) -> None:
        from daem0nmcp.covenant import (
            installed_invocation,
            issue_current_preflight_response,
        )

        self._brief()
        with installed_invocation(
            self.scope_a,
            self.gate,
            workspace_resolver=lambda _selector: self.workspace,
        ):
            result = issue_current_preflight_response(
                "inscribe.remember",
                {
                    "category": "decision",
                    "content": "x",
                    "attacker_controlled_unknown": True,
                },
            )
        self.assertEqual("TOKEN_ARGUMENT_MISMATCH", result["violation"])
        self.assertNotIn("attacker_controlled_unknown", result["message"])
        self.assertNotIn("preflight_token", result)

    def test_cognitive_preflight_rejects_missing_required_arguments(self) -> None:
        from daem0nmcp.covenant import (
            installed_invocation,
            issue_current_preflight_response,
        )

        self._brief()
        invalid_targets = (
            ("debate_internal", {}),
            (
                "debate_internal",
                {
                    "topic": "auth",
                    "advocate_position": None,
                    "challenger_position": "change",
                },
            ),
        )
        with installed_invocation(
            self.scope_a,
            self.gate,
            workspace_resolver=lambda _selector: self.workspace,
        ):
            for operation, arguments in invalid_targets:
                with self.subTest(operation=operation, arguments=arguments):
                    result = issue_current_preflight_response(operation, arguments)
                    self.assertNotIn("preflight_token", result)
                    self.assertEqual("TOKEN_ARGUMENT_MISMATCH", result.get("violation"))
            valid = issue_current_preflight_response(
                "debate_internal",
                {
                    "topic": "auth",
                    "advocate_position": "keep",
                    "challenger_position": "change",
                },
            )
        self.assertIn("preflight_token", valid)


if __name__ == "__main__":
    unittest.main()
