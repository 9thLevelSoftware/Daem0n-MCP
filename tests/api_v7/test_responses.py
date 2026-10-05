from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from daem0nmcp.api.v7.errors import ErrorCode
from daem0nmcp.api.v7.models import WireModel


class _Data(WireModel):
    value: str


class _Clock:
    def __init__(self) -> None:
        self.current = datetime(2026, 1, 2, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        result = self.current
        self.current += timedelta(milliseconds=7)
        return result


class ResponseFactoryTests(unittest.TestCase):
    def test_success_and_failure_share_one_request_context(self) -> None:
        from daem0nmcp.api.v7.responses import ResponseFactory

        factory = ResponseFactory(clock=_Clock(), request_id=lambda: "req_test_request")
        context = factory.begin("ws_" + "a" * 24)
        success = context.success(_Data(value="ok"))
        self.assertTrue(success.ok)
        self.assertEqual(success.data.value, "ok")
        self.assertEqual(success.meta.request_id, "req_test_request")
        self.assertEqual(success.meta.duration_ms, 7)

        failure = context.failure(
            ErrorCode.COMMUNION_REQUIRED,
            "A session briefing is required.",
            remedy_tool="session_brief",
            remedy_arguments={"workspace_id": "ws_" + "a" * 24},
        )
        self.assertFalse(failure.ok)
        self.assertEqual(failure.error.code, ErrorCode.COMMUNION_REQUIRED)
        self.assertEqual(failure.error.correlation_id, "req_test_request")
        self.assertEqual(failure.error.remedy.tool, "session_brief")
        self.assertNotIn("project_path", failure.model_dump_json())

    def test_internal_error_cannot_echo_private_exception_details(self) -> None:
        from daem0nmcp.api.v7.responses import ResponseFactory

        factory = ResponseFactory(clock=_Clock(), request_id=lambda: "req_test_request")
        response = factory.begin(None).internal_error(
            RuntimeError("D:/private/root secret-token")
        )
        encoded = response.model_dump_json()
        self.assertIn('"message":"Internal error."', encoded)
        self.assertNotIn("private", encoded)
        self.assertNotIn("secret-token", encoded)

    def test_covenant_context_and_relay_preserve_public_diagnostics(self) -> None:
        from daem0nmcp.api.v7.models import (
            ApiWarning,
            CapabilityState,
            CounselChallenge,
            CovenantNotice,
            ErrorRemedy,
            FieldError,
            InlineCounsel,
            PreflightGuidance,
        )
        from daem0nmcp.api.v7.responses import ResponseFactory

        factory = ResponseFactory(
            clock=_Clock(), request_id=lambda: "req_gateway_request"
        )
        original = factory.begin(None)
        notice = CovenantNotice(counsel=InlineCounsel(target_tool="memory_store"))
        context = original.with_covenant(notice)
        self.assertIsNone(original.covenant)
        self.assertEqual(original.request_id, context.request_id)
        self.assertEqual(original.started_at, context.started_at)
        self.assertEqual(notice, context.success(_Data(value="ok")).meta.covenant)
        challenge = CounselChallenge(
            guidance=PreflightGuidance(must_not=["Avoid unsafe changes."]),
            preflight_token="a" * 16,
            expires_at=datetime(2026, 1, 2, tzinfo=timezone.utc),
            reasons=["RULE_MUST_NOT"],
        )
        target = (
            ResponseFactory(clock=_Clock(), request_id=lambda: "req_target_request")
            .begin(None)
            .failure(
                ErrorCode.COUNSEL_REQUIRED,
                "Review counsel.",
                retryable=True,
                retry_after_ms=100,
                field_errors=[
                    FieldError(field="content", code="RISK", message="Review.")
                ],
                remedy_tool="memory_store",
                remedy_arguments={"preflight_token": challenge.preflight_token},
                counsel=challenge,
            )
        )
        remedy = ErrorRemedy(
            tool="daem0n_tool_call", arguments={"tool": "memory_store"}
        )
        warning = ApiWarning(code="DEGRADED", message="Provider unavailable.")
        capability = CapabilityState(name="dense", status="disabled")
        relayed = context.relay_failure(
            target.error,
            remedy=remedy,
            warnings=[warning],
            capability_states=[capability],
        )
        self.assertEqual("req_gateway_request", relayed.error.correlation_id)
        self.assertEqual(remedy, relayed.error.remedy)
        self.assertEqual(challenge, relayed.error.counsel)
        self.assertEqual(target.error.code, relayed.error.code)
        self.assertEqual(target.error.message, relayed.error.message)
        self.assertTrue(relayed.error.retryable)
        self.assertEqual(100, relayed.error.retry_after_ms)
        self.assertEqual(target.error.field_errors, relayed.error.field_errors)
        self.assertEqual(notice, relayed.meta.covenant)
        self.assertEqual([warning], relayed.meta.warnings)
        self.assertEqual([capability], relayed.meta.capability_states)
        self.assertIsNone(context.with_covenant(None).covenant)

    def test_unknown_error_code_is_rejected(self) -> None:
        from daem0nmcp.api.v7.responses import ResponseFactory

        factory = ResponseFactory(clock=_Clock(), request_id=lambda: "req_test_request")
        with self.assertRaises(ValueError):
            factory.begin(None).failure("NEW_UNREVIEWED_CODE", "no")


if __name__ == "__main__":
    unittest.main()
