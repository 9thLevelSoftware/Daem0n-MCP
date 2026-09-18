"""Compatibility-facing tests for the scoped Covenant capability contract."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from daem0nmcp.covenant import (
    CapabilityAuthority,
    CovenantGate,
    CovenantStateStore,
    InvocationScope,
    authorize_workflow_call,
)


class ScopedCovenantContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.workspace = str(Path(self.temp.name).resolve())
        self.clock_value = 1_000
        self.scope = InvocationScope("principal", "session", self.workspace)
        self.gate = CovenantGate(
            state_store=CovenantStateStore(clock=lambda: self.clock_value),
            authority=CapabilityAuthority(
                secret=b"legacy-test-module-migrated-key-32-bytes",
                kid="test",
                clock=lambda: self.clock_value,
            ),
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_communion_is_scoped_to_principal_session_and_workspace(self) -> None:
        self.gate.record_briefing(self.scope)
        self.assertIsNone(
            self.gate.authorize("consult.recall", {"topic": "auth"}, self.scope)
        )
        variants = (
            InvocationScope("other", "session", self.workspace),
            InvocationScope("principal", "other", self.workspace),
            InvocationScope("principal", "session", str(Path(self.workspace, "other"))),
        )
        for scope in variants:
            with self.subTest(scope=scope):
                violation = self.gate.authorize(
                    "consult.recall", {"topic": "auth"}, scope
                )
                self.assertEqual("COMMUNION_REQUIRED", violation["violation"])

    def test_capability_is_exact_expiring_and_one_use(self) -> None:
        arguments = {"category": "decision", "content": "bound"}
        self.gate.record_briefing(self.scope)
        token = self.gate.issue_preflight(self.scope, "inscribe.remember", arguments)
        mismatch = self.gate.authorize(
            "inscribe.remember",
            {**arguments, "content": "changed"},
            self.scope,
            preflight_token=token,
        )
        self.assertEqual("TOKEN_ARGUMENT_MISMATCH", mismatch["violation"])
        self.assertIsNone(
            self.gate.authorize(
                "inscribe.remember",
                arguments,
                self.scope,
                preflight_token=token,
            )
        )
        replay = self.gate.authorize(
            "inscribe.remember",
            arguments,
            self.scope,
            preflight_token=token,
        )
        self.assertEqual("TOKEN_REPLAYED", replay["violation"])

        expiring = self.gate.issue_preflight(self.scope, "inscribe.remember", arguments)
        self.clock_value += 300
        expired = self.gate.authorize(
            "inscribe.remember",
            arguments,
            self.scope,
            preflight_token=expiring,
        )
        self.assertEqual("TOKEN_EXPIRED", expired["violation"])

    def test_direct_protected_call_without_installed_scope_fails_closed(self) -> None:
        violation = authorize_workflow_call(
            "inscribe",
            "remember",
            {"category": "decision", "content": "x"},
        )
        self.assertEqual("IDENTITY_UNAVAILABLE", violation["violation"])


if __name__ == "__main__":
    unittest.main()
