"""Wire-model contract tests for the v7 MCP surface.

Each test names a boundary regression that would otherwise leak into every
tool schema.  The expectations are literals from the Task 9 wire contract,
not values derived from the production registry.
"""

from __future__ import annotations

import importlib
import json
import math
import unittest
from datetime import datetime, timedelta, timezone

from pydantic import TypeAdapter, ValidationError

WORKSPACE_ID = "ws_0123456789abcdef01234567"
RECORD_ID = "mem_" + "1" * 64
EVENT_ID = "evt_" + "2" * 64
RELATIONSHIP_ID = "rel_" + "3" * 64
REQUEST_ID = "req_0123456789abcdef01234567"
NOW = datetime(2026, 8, 8, 12, 0, tzinfo=timezone.utc)


def _load(testcase: unittest.TestCase):
    """Load the wished-for public modules while keeping missing code RED."""

    try:
        errors = importlib.import_module("daem0nmcp.api.v7.errors")
        models = importlib.import_module("daem0nmcp.api.v7.models")
    except ModuleNotFoundError as exc:
        testcase.fail(f"v7 wire modules are missing: {exc}")
    return errors, models


def _meta(models):
    return models.ResponseMeta(
        request_id=REQUEST_ID,
        workspace_id=WORKSPACE_ID,
        started_at=NOW,
        duration_ms=7,
    )


def _record(models):
    return models.RecordSummary(
        record_id=RECORD_ID,
        record_type="decision",
        excerpt="Use an explicit v7 composition root.",
        tags=["api", "security"],
        relative_file_path="daem0nmcp/api/v7/models.py",
        current_status="current",
        content_hash="a" * 64,
        created_at=NOW,
        updated_at=NOW,
    )


class ErrorRegistryTests(unittest.TestCase):
    def test_error_registry_is_exact_and_rejects_unknown_codes(self) -> None:
        errors, models = _load(self)
        expected = (
            "INVALID_ARGUMENT",
            "NOT_FOUND",
            "CONFLICT",
            "IDEMPOTENCY_CONFLICT",
            "UNAUTHORIZED_WORKSPACE",
            "WORKSPACE_PATH_ESCAPE",
            "STALE_PROJECTION_ID",
            "CAPABILITY_DISABLED",
            "CAPABILITY_DEGRADED",
            "LEXICAL_UNAVAILABLE",
            "COMMUNION_REQUIRED",
            "COUNSEL_REQUIRED",
            "IDENTITY_UNAVAILABLE",
            "UNKNOWN_COVENANT_OPERATION",
            "TOKEN_MISSING",
            "TOKEN_TAMPERED",
            "TOKEN_EXPIRED",
            "TOKEN_SCOPE_MISMATCH",
            "TOKEN_OPERATION_MISMATCH",
            "TOKEN_ARGUMENT_MISMATCH",
            "TOKEN_REPLAYED",
            "TOKEN_LEGACY_UNSUPPORTED",
            "PREFLIGHT_TARGET_NOT_PROTECTED",
            "DEADLINE_EXCEEDED",
            "TASK_REQUIRED",
            "TASKS_UNAVAILABLE",
            "CANCELLED",
            "DATABASE_IN_USE",
            "EVENT_STREAM_CONFLICT",
            "IMPORT_INVALID",
            "CROSS_WORKSPACE_IMPORT_UNSUPPORTED",
            "INTERNAL_ERROR",
        )
        self.assertEqual(expected, errors.STABLE_ERROR_CODES)
        self.assertEqual(len(expected), len(set(expected)))
        self.assertTrue(errors.is_stable_error_code("NOT_FOUND"))
        self.assertFalse(errors.is_stable_error_code("NEW_UNREVIEWED_ERROR"))

        with self.assertRaises(ValidationError):
            models.ApiError(
                code="NEW_UNREVIEWED_ERROR",
                message="No such code.",
                retryable=False,
                correlation_id=REQUEST_ID,
            )

    def test_internal_error_cannot_carry_diagnostic_details(self) -> None:
        errors, models = _load(self)
        safe = models.ApiError(
            code="INTERNAL_ERROR",
            message=errors.INTERNAL_ERROR_MESSAGE,
            retryable=False,
            correlation_id=REQUEST_ID,
        )
        self.assertEqual(errors.INTERNAL_ERROR_MESSAGE, safe.message)

        unsafe_cases = (
            {"message": "sqlite failed at D:/secret/project.db"},
            {"field_errors": [{"field": "query", "code": "BAD", "message": "bad"}]},
            {
                "remedy": {
                    "tool": "session_brief",
                    "arguments": {"workspace_id": WORKSPACE_ID},
                }
            },
            {"retry_after_ms": 100},
        )
        for override in unsafe_cases:
            values = {
                "code": "INTERNAL_ERROR",
                "message": errors.INTERNAL_ERROR_MESSAGE,
                "retryable": False,
                "correlation_id": REQUEST_ID,
                **override,
            }
            with self.subTest(override=override), self.assertRaises(ValidationError):
                models.ApiError(**values)


class PrimitiveBoundaryTests(unittest.TestCase):
    def test_public_ids_are_exact_lowercase_opaque_strings(self) -> None:
        _, models = _load(self)
        cases = (
            (models.WorkspaceId, WORKSPACE_ID),
            (models.RecordId, RECORD_ID),
            (models.EventId, EVENT_ID),
            (models.FactId, "fact_" + "4" * 64),
            (models.RelationshipId, RELATIONSHIP_ID),
            (models.RuleId, "rule_" + "5" * 64),
            (models.TriggerId, "trg_" + "6" * 64),
            (models.EntityId, "ent_" + "7" * 64),
            (models.CommunityId, "com_" + "8" * 64),
            (models.CodeEntityId, "code_" + "9" * 64),
            (models.ActiveContextId, "act_" + "a" * 64),
        )
        for annotation, valid in cases:
            adapter = TypeAdapter(annotation)
            with self.subTest(valid=valid):
                self.assertEqual(valid, adapter.validate_python(valid, strict=True))
                for invalid in (1, valid.upper(), valid[:-1], valid + "0"):
                    with self.assertRaises(ValidationError):
                        adapter.validate_python(invalid, strict=True)

    def test_relative_path_fails_closed_and_preserves_normalized_posix_form(
        self,
    ) -> None:
        _, models = _load(self)
        adapter = TypeAdapter(models.RelativePath)
        for valid in (".", "src", "src/api/models.py", ".hidden/config"):
            with self.subTest(valid=valid):
                self.assertEqual(valid, adapter.validate_python(valid, strict=True))

        invalid_paths = (
            "",
            "/etc/passwd",
            "C:/Users/me/secret.txt",
            "C:\\Users\\me\\secret.txt",
            "../secret",
            "src/../secret",
            "src/./models.py",
            "./src/models.py",
            "src//models.py",
            "src/models.py/",
            "~/.ssh/id_rsa",
            "src\x00models.py",
        )
        for invalid in invalid_paths:
            with self.subTest(invalid=invalid), self.assertRaises(ValidationError):
                adapter.validate_python(invalid, strict=True)

    def test_datetimes_require_rfc3339_offsets_and_response_start_is_utc(self) -> None:
        _, models = _load(self)
        aware = TypeAdapter(models.AwareDateTime)
        self.assertEqual(
            timezone.utc,
            aware.validate_python("2026-08-08T12:00:00Z").tzinfo,
        )
        self.assertEqual(
            timedelta(hours=-4),
            aware.validate_python("2026-08-08T08:00:00-04:00").utcoffset(),
        )
        for invalid in (
            "2026-08-08 12:00:00Z",
            "2026-08-08T12:00:00",
            "2026-08-08",
            1_786_190_400,
            datetime(2026, 8, 8, 12, 0),
        ):
            with self.subTest(invalid=invalid), self.assertRaises(ValidationError):
                aware.validate_python(invalid)

        with self.assertRaises(ValidationError):
            models.ResponseMeta(
                request_id=REQUEST_ID,
                workspace_id=WORKSPACE_ID,
                started_at="2026-08-08T12:00:00+01:00",
                duration_ms=0,
            )

    def test_json_values_are_strict_finite_bounded_and_duplicate_safe(self) -> None:
        _, models = _load(self)
        remedy = models.ErrorRemedy(
            tool="memory_store",
            arguments={"nested": [None, True, 4, 1.5, "value"]},
        )
        self.assertEqual(4, remedy.arguments["nested"][2])

        invalid_arguments = (
            {1: "integer key"},
            {"value": math.nan},
            {"value": math.inf},
            {"value": object()},
        )
        for arguments in invalid_arguments:
            with self.subTest(arguments=arguments), self.assertRaises(ValidationError):
                models.ErrorRemedy(tool="memory_store", arguments=arguments)

        duplicate = json.dumps({"tool": "memory_store"})[:-1]
        duplicate += ', "arguments": {"x": 1, "x": 2}}'
        with self.assertRaises(ValidationError):
            models.ErrorRemedy.model_validate_json(duplicate)
        with self.assertRaises(ValidationError):
            models.ErrorRemedy.model_validate_json(
                '{"tool":"memory_store","arguments":{"x":NaN}}'
            )

    def test_wire_models_reject_absolute_paths_outside_user_text(self) -> None:
        _, models = _load(self)

        with self.assertRaises(ValidationError):
            models.ErrorRemedy(
                tool="memory_store",
                arguments={
                    "context": {
                        "note": "Inspect C:\\private\\secret.txt before release"
                    }
                },
            )

        summary = {
            "record_id": RECORD_ID,
            "record_type": "warning",
            "excerpt": "Do not read /home/private/.ssh/id_rsa",
            "tags": ["C:\\proj"],
            "relative_file_path": None,
            "current_status": "current",
            "content_hash": "a" * 64,
            "created_at": NOW,
            "updated_at": NOW,
        }
        # Excerpts and tags are user text (UD-3); the path field is not.
        models.RecordSummary(**summary)
        with self.assertRaises(ValidationError):
            models.RecordSummary(**{**summary, "relative_file_path": "/home/x"})


class EnvelopeTests(unittest.TestCase):
    def test_wire_models_forbid_extra_fields_and_scalar_coercion(self) -> None:
        _, models = _load(self)
        with self.assertRaises(ValidationError):
            models.ResponseMeta(
                request_id=REQUEST_ID,
                workspace_id=WORKSPACE_ID,
                started_at=NOW,
                duration_ms="7",
            )
        with self.assertRaises(ValidationError):
            models.ResponseMeta(
                request_id=REQUEST_ID,
                workspace_id=WORKSPACE_ID,
                started_at=NOW,
                duration_ms=7,
                project_path="D:/private",
            )

    def test_api_response_enforces_exactly_one_success_or_error_branch(self) -> None:
        _, models = _load(self)
        response_type = models.ApiResponse[models.RecordSummary]
        record = _record(models)
        meta = _meta(models)
        success = response_type(ok=True, data=record, error=None, meta=meta)
        self.assertEqual("7", success.api_version)
        self.assertEqual(RECORD_ID, success.data.record_id)

        error = models.ApiError(
            code="NOT_FOUND",
            message="The requested record was not found.",
            retryable=False,
            correlation_id=REQUEST_ID,
        )
        failure = response_type(ok=False, data=None, error=error, meta=meta)
        self.assertEqual("NOT_FOUND", failure.error.code)

        invalid = (
            {"ok": True, "data": None, "error": None},
            {"ok": True, "data": record, "error": error},
            {"ok": False, "data": record, "error": error},
            {"ok": False, "data": None, "error": None},
        )
        for branches in invalid:
            with self.subTest(branches=branches), self.assertRaises(ValidationError):
                response_type(meta=meta, **branches)

    def test_api_response_schema_is_object_root_with_conditional_branches(self) -> None:
        _, models = _load(self)
        schema = models.ApiResponse[models.RecordSummary].model_json_schema()
        self.assertEqual("object", schema["type"])
        self.assertFalse(schema["additionalProperties"])
        self.assertNotIn("oneOf", schema)
        encoded = json.dumps(schema, sort_keys=True)
        self.assertIn('"if"', encoded)
        self.assertIn('"then"', encoded)
        self.assertIn('"else"', encoded)

    def test_meta_collection_defaults_are_materialized_and_not_shared(self) -> None:
        _, models = _load(self)
        first = _meta(models)
        second = _meta(models)
        self.assertEqual([], first.warnings)
        self.assertEqual([], first.capability_states)
        self.assertIsNot(first.warnings, second.warnings)
        self.assertIsNot(first.capability_states, second.capability_states)

        too_many = [
            models.ApiWarning(code="DEGRADED", message="Optional provider unavailable.")
            for _ in range(21)
        ]
        with self.assertRaises(ValidationError):
            models.ResponseMeta(
                request_id=REQUEST_ID,
                workspace_id=WORKSPACE_ID,
                started_at=NOW,
                duration_ms=0,
                warnings=too_many,
            )


class SharedOutputModelTests(unittest.TestCase):
    def test_page_record_receipt_preview_and_capability_models_are_typed(self) -> None:
        _, models = _load(self)
        record = _record(models)
        page = models.Page[models.RecordSummary](
            items=[record], next_cursor=None, truncated=False
        )
        self.assertEqual([RECORD_ID], [item.record_id for item in page.items])

        receipt = models.MutationReceipt(
            operation_id="op_0123456789abcdef",
            affected_ids=[RECORD_ID, RELATIONSHIP_ID],
            event_ids=[EVENT_ID],
            counts={"records": 1, "relationships": 1},
            idempotent_replay=False,
        )
        self.assertEqual(1, receipt.counts["records"])
        with self.assertRaises(ValidationError):
            models.MutationReceipt(
                operation_id="op_0123456789abcdef",
                affected_ids=[RECORD_ID],
                event_ids=[EVENT_ID],
                counts={"records": -1},
                idempotent_replay=False,
            )

        preview = models.Preview(
            selection_token="sel_0123456789abcdef01234567",
            counts={"selected": 1},
            sample_ids=[RECORD_ID],
            expires_at=NOW + timedelta(minutes=5),
        )
        self.assertEqual([RECORD_ID], preview.sample_ids)

        capability = models.CapabilityState(
            name="dense_retrieval",
            status="degraded",
            reason_code="MODEL_UNAVAILABLE",
            remediation="Install the local model capability.",
        )
        self.assertEqual("degraded", capability.status)
        with self.assertRaises(ValidationError):
            models.CapabilityState(
                name="dense_retrieval",
                status="unknown",
                reason_code=None,
                remediation=None,
            )

    def test_retrieval_data_contains_selected_evidence_not_raw_candidates(self) -> None:
        _, models = _load(self)
        record = _record(models)
        evidence_ref = models.EvidenceRef(
            record_id=RECORD_ID,
            event_id=EVENT_ID,
            content_hash="a" * 64,
            version_id=None,
            relation_path=[RELATIONSHIP_ID],
            provider="lexical",
        )
        item = models.EvidenceItem(
            citation="[E1]",
            record=record,
            bounded_excerpt="Use an explicit v7 composition root.",
            channels=["lexical"],
            score=1.0,
            status="current",
            evidence_refs=[evidence_ref],
        )
        manifest = models.CitationManifestEntry(
            citation="[E1]",
            evidence_refs=[evidence_ref],
            channels=["lexical"],
        )
        retrieval = models.RetrievalData(
            items=[item],
            rendered_context="[E1] Use an explicit v7 composition root.",
            citation_manifest=[manifest],
            provider_diagnostics=[
                models.ProviderDiagnostic(
                    provider="lexical",
                    status="ready",
                    manifest_generation=1,
                    elapsed_ms=2,
                    reason=None,
                    returned_count=1,
                )
            ],
            abstained=False,
            abstention_reason=None,
            token_usage=models.TokenUsage(
                budget=2400, requested=9, selected=9, rendered=10, dropped=0
            ),
        )
        dumped = retrieval.model_dump(mode="json")
        self.assertEqual(RECORD_ID, dumped["items"][0]["record"]["record_id"])
        for forbidden in ("raw_score", "vector", "prompt", "candidate_text"):
            self.assertNotIn(forbidden, json.dumps(dumped, sort_keys=True))

        with self.assertRaises(ValidationError):
            models.RetrievalData(
                items=[item],
                rendered_context=None,
                citation_manifest=[manifest],
                provider_diagnostics=[],
                abstained=True,
                abstention_reason="NO_RESULTS",
                token_usage=models.TokenUsage(
                    budget=2400, requested=9, selected=0, rendered=0, dropped=9
                ),
            )


if __name__ == "__main__":
    unittest.main()
