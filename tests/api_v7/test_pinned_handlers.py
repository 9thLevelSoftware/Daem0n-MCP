from __future__ import annotations

import asyncio
import importlib
import inspect
import os
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import MappingProxyType
from unittest.mock import patch

from daem0nmcp.api.v7.responses import ResponseFactory
from daem0nmcp.covenant import (
    CapabilityAuthority,
    CovenantGate,
    CovenantStateStore,
    InvocationScope,
)
from daem0nmcp.workspace import Workspace, WorkspaceAccessError

EXPECTED_PINNED_HANDLERS = frozenset(
    {
        "session_brief",
        "memory_preflight",
        "memory_recall",
        "memory_store",
        "memory_record_outcome",
        "system_health",
    }
)

WORKSPACE_ID = "ws_" + "a" * 24
NOW = datetime(2026, 8, 8, 12, 0, tzinfo=timezone.utc)


class _WorkspaceResolver:
    def __init__(self, workspace: Workspace) -> None:
        self.workspace = workspace

    def resolve(self, workspace_id: str) -> Workspace:
        if workspace_id != self.workspace.workspace_id:
            raise WorkspaceAccessError()
        return self.workspace


class _BriefingService:
    def __init__(self, gate: CovenantGate, scope: InvocationScope) -> None:
        self._gate = gate
        self._scope = scope

    async def assemble(self, workspace: Workspace, request: object) -> object:
        from daem0nmcp.api.v7.tools import SessionBriefData

        if self._gate.state_store.is_briefed(self._scope):
            raise AssertionError("communion was recorded before assembly")
        return SessionBriefData(
            workspace_id=workspace.workspace_id,
            briefed_at=NOW,
            workspace_statistics={
                "focus_count": len(request.focus_areas),
            },
        )


class _FailingBriefingService:
    async def assemble(self, workspace: Workspace, request: object) -> object:
        del workspace, request
        raise RuntimeError(r"D:\private\workspace\prompt-secret")


class _PreflightService:
    async def guidance(
        self,
        workspace: Workspace,
        target_tool: str,
        normalized_arguments: object,
        description: str | None,
    ) -> object:
        from daem0nmcp.api.v7.tools import PreflightGuidance

        expected = {
            "record_type": "decision",
            "content": "Use event streams.",
            "rationale": None,
            "context": {},
            "tags": [],
            "relative_file_path": None,
            "happened_at": None,
            "procedure_steps": [],
            "idempotency_key": "decision-0001",
        }
        if (
            workspace.workspace_id != WORKSPACE_ID
            or target_tool != "memory_store"
            or normalized_arguments != expected
            or description != "Persist the decision."
        ):
            raise AssertionError("preflight service received non-canonical arguments")
        return PreflightGuidance(
            must_do=["Use the exact normalized request."],
        )


class _FailingPreflightService:
    async def guidance(self, *args: object, **kwargs: object) -> object:
        del args, kwargs
        raise RuntimeError(r"D:\private\guidance-secret")


class _LeakyPreflightService:
    async def guidance(self, *args: object, **kwargs: object) -> object:
        from daem0nmcp.api.v7.tools import PreflightGuidance

        del args, kwargs
        return PreflightGuidance(
            warnings=[r"Inspect D:\private\workspace\policy.txt"],
        )


class _ExplodingRecallService:
    async def retrieve(self, *args: object, **kwargs: object) -> object:
        del args, kwargs
        raise AssertionError("recall ran before admission")


class _FailingRecallService:
    async def retrieve(self, *args: object, **kwargs: object) -> object:
        del args, kwargs
        raise RuntimeError(r"D:\private\retrieval-prompt")


class _RecallService:
    async def retrieve(
        self,
        workspace: Workspace,
        query: object,
        linked_workspace_ids: frozenset[str],
    ) -> object:
        from daem0nmcp.api.v7.models import RetrievalData, TokenUsage
        from daem0nmcp.retrieval import RetrievalQuery

        if not isinstance(query, RetrievalQuery):
            raise AssertionError("recall did not use the Task 8 query seam")
        if (
            workspace.workspace_id != WORKSPACE_ID
            or query.workspace_id != WORKSPACE_ID
            or query.text != "deployment decision"
            or query.limit != 7
            or query.candidate_limit != 21
            or query.categories != frozenset({"decision"})
            or query.tags != frozenset({"release"})
            or linked_workspace_ids != frozenset()
        ):
            raise AssertionError("recall request was not normalized exactly")
        return RetrievalData(
            abstained=True,
            abstention_reason="NO_POLICY_VALID_EVIDENCE",
            token_usage=TokenUsage(
                budget=512,
                requested=0,
                selected=0,
                rendered=0,
                dropped=0,
            ),
        )


class _MemoryEventWriter:
    def __init__(self, result_type: type[object]) -> None:
        self._result_type = result_type

    async def store(self, workspace: Workspace, command: object) -> object:
        from daem0nmcp.api.v7.models import RecordSummary
        from daem0nmcp.event_store import AppendedEvent

        expected = {
            "record_type": "decision",
            "content": "Use append-only events.",
            "rationale": "Deterministic history is auditable.",
            "context": {"component": "memory"},
            "tags": ("events", "v7"),
            "relative_file_path": "daem0nmcp/event_store.py",
            "happened_at": NOW,
            "procedure_steps": (),
            "idempotency_key": "decision-1001",
        }
        actual = {name: getattr(command, name) for name in expected}
        if (
            workspace.workspace_id != WORKSPACE_ID
            or actual != expected
            or hasattr(command, "preflight_token")
            or hasattr(command, "workspace_id")
        ):
            raise AssertionError("store writer received an unsafe command")
        record = RecordSummary(
            record_id="mem_" + "1" * 64,
            record_type="decision",
            excerpt="Use append-only events.",
            tags=["events", "v7"],
            relative_file_path="daem0nmcp/event_store.py",
            current_status="current",
            content_hash="c" * 64,
            created_at=NOW,
            updated_at=NOW,
        )
        event = AppendedEvent(
            event_id="evt_" + "e" * 64,
            event_hash="e" * 64,
            payload_hash="f" * 64,
            stream_version=1,
            previous_event_hash=None,
        )
        return self._result_type(
            record=record,
            event=event,
            idempotent_replay=False,
        )

    async def record_outcome(self, workspace: Workspace, command: object) -> object:
        del workspace, command
        raise AssertionError("outcome writer was not expected")


class _FailingMemoryEventWriter:
    def __init__(self, error: Exception) -> None:
        self._error = error

    async def store(self, workspace: Workspace, command: object) -> object:
        del workspace, command
        raise self._error

    async def record_outcome(self, workspace: Workspace, command: object) -> object:
        del workspace, command
        raise self._error


class _OutcomeEventWriter:
    def __init__(self, result_type: type[object]) -> None:
        self._result_type = result_type

    async def store(self, workspace: Workspace, command: object) -> object:
        del workspace, command
        raise AssertionError("store writer was not expected")

    async def record_outcome(self, workspace: Workspace, command: object) -> object:
        from daem0nmcp.event_store import AppendedEvent

        expected = {
            "record_id": "mem_" + "2" * 64,
            "outcome_text": "The rollout stayed healthy.",
            "worked": True,
            "happened_at": NOW,
            "idempotency_key": "outcome-1001",
        }
        actual = {name: getattr(command, name) for name in expected}
        if (
            workspace.workspace_id != WORKSPACE_ID
            or actual != expected
            or hasattr(command, "workspace_id")
            or hasattr(command, "preflight_token")
        ):
            raise AssertionError("outcome writer received an unsafe command")
        event = AppendedEvent(
            event_id="evt_" + "a" * 64,
            event_hash="a" * 64,
            payload_hash="b" * 64,
            stream_version=2,
            previous_event_hash="c" * 64,
        )
        return self._result_type(
            record_id=expected["record_id"],
            event=event,
            worked=True,
            idempotent_replay=False,
        )


class _HealthService:
    async def inspect(
        self,
        workspace: Workspace | None,
        include_components: bool,
    ) -> object:
        from daem0nmcp.api.v7.models import CapabilityState
        from daem0nmcp.api.v7.tools import HealthData

        if workspace is not None or include_components:
            raise AssertionError("health request was not normalized")
        return HealthData(
            package_version="0.7.0",
            protocol_version="2025-11-25",
            storage_format_version=7,
            storage_schema_version=19,
            supported_transports={"stdio", "streamable-http"},
            task_support=CapabilityState(
                name="tasks",
                status="disabled",
                reason_code="TASKS_UNAVAILABLE",
                remediation="Install the reviewed tasks profile.",
            ),
            auth_mode="loopback",
            capability_states=[],
        )


class _LeakyHealthService:
    async def inspect(self, workspace: object, include_components: bool) -> object:
        from daem0nmcp.api.v7.models import CapabilityState
        from daem0nmcp.api.v7.tools import HealthData

        del workspace, include_components
        return HealthData(
            package_version="0.7.0",
            protocol_version="2025-11-25",
            storage_format_version=7,
            storage_schema_version=19,
            supported_transports={"stdio"},
            task_support=CapabilityState(
                name="tasks",
                status="failed",
                reason_code="TASKS_UNAVAILABLE",
                remediation=r"Inspect D:\private\workspace\worker.log",
            ),
            auth_mode="process",
        )


class _UnusedService:
    def __getattr__(self, name: str) -> object:
        raise AssertionError(f"unused service was invoked: {name}")


def _gate() -> CovenantGate:
    from daem0nmcp.api.v7.policy import V7_COVENANT_POLICY
    from daem0nmcp.api.v7.tools import build_argument_normalizer

    def seconds() -> int:
        return int(NOW.timestamp())

    return CovenantGate(
        state_store=CovenantStateStore(clock=seconds),
        authority=CapabilityAuthority(
            secret=b"p" * 32,
            kid="pinned-tests",
            clock=seconds,
        ),
        policy=V7_COVENANT_POLICY,
        argument_normalizer=build_argument_normalizer(),
    )


def _dependencies(
    *,
    gate: CovenantGate,
    scope: InvocationScope,
    workspace: Workspace,
    briefing_service: object,
    preflight_service: object | None = None,
    recall_service: object | None = None,
    memory_event_writer: object | None = None,
    health_service: object | None = None,
):
    from daem0nmcp.api.v7.pinned import PinnedDependencies
    from daem0nmcp.api.v7.tools import build_argument_normalizer

    unused = _UnusedService()
    return PinnedDependencies(
        workspace_resolver=_WorkspaceResolver(workspace),
        covenant_gate=gate,
        argument_normalizer=build_argument_normalizer(),
        briefing_service=briefing_service,
        preflight_service=preflight_service or unused,
        recall_service=recall_service or unused,
        memory_event_writer=memory_event_writer or unused,
        health_service=health_service or unused,
        response_factory=ResponseFactory(
            clock=lambda: NOW,
            request_id=lambda: "req_pinned_tests",
        ),
        scope_provider=lambda: scope,
        clock=lambda: NOW,
    )


class PinnedHandlerTests(unittest.TestCase):
    def test_module_publishes_only_the_six_pinned_handler_names(self) -> None:
        # Catches a missing pinned composition seam or an accidental seventh pin.
        try:
            pinned = importlib.import_module("daem0nmcp.api.v7.pinned")
        except ModuleNotFoundError:
            pinned = None

        self.assertIsNotNone(pinned, "the v7 pinned handler module is missing")
        assert pinned is not None
        self.assertEqual(pinned.PINNED_HANDLER_NAMES, EXPECTED_PINNED_HANDLERS)
        self.assertTrue(callable(pinned.build_pinned_handlers))

    def test_factory_returns_one_immutable_callable_per_pinned_name(self) -> None:
        # Catches fail-open handler drift and mutable post-build replacement.
        from daem0nmcp.api.v7 import pinned

        dependencies_type = getattr(pinned, "PinnedDependencies", None)
        self.assertIsNotNone(
            dependencies_type,
            "the pinned dependency contract is missing",
        )
        assert dependencies_type is not None
        dependencies = dependencies_type(
            workspace_resolver=object(),
            covenant_gate=object(),
            argument_normalizer=lambda *_args: {},
            briefing_service=object(),
            preflight_service=object(),
            recall_service=object(),
            memory_event_writer=object(),
            health_service=object(),
        )

        handlers = pinned.build_pinned_handlers(dependencies)

        self.assertIsInstance(handlers, MappingProxyType)
        self.assertEqual(frozenset(handlers), EXPECTED_PINNED_HANDLERS)
        self.assertTrue(all(callable(handler) for handler in handlers.values()))
        with self.assertRaises(TypeError):
            handlers["session_brief"] = handlers["system_health"]

    def test_memory_recall_can_authorize_without_starting_unsafe_fallback(self) -> None:
        from daem0nmcp.api.v7.errors import ErrorCode
        from daem0nmcp.api.v7.pinned import build_pinned_handlers
        from daem0nmcp.api.v7.tasks import task_admission_only_var
        from daem0nmcp.api.v7.tools import MemoryRecallOutput

        workspace = Workspace(WORKSPACE_ID, Path.cwd())
        scope = InvocationScope("principal", "session", str(workspace.root))
        gate = _gate()
        gate.record_briefing(scope)
        handlers = build_pinned_handlers(
            _dependencies(
                gate=gate,
                scope=scope,
                workspace=workspace,
                briefing_service=_UnusedService(),
                recall_service=_ExplodingRecallService(),
            )
        )
        self.assertTrue(
            getattr(
                handlers["memory_recall"],
                "__daem0nmcp_admission_aware__",
                False,
            )
        )
        token = task_admission_only_var.set(True)
        try:
            result = asyncio.run(
                handlers["memory_recall"](
                    workspace_id=WORKSPACE_ID,
                    query="bounded recall",
                )
            )
        finally:
            task_admission_only_var.reset(token)

        response = MemoryRecallOutput.model_validate(result)
        self.assertFalse(response.ok)
        self.assertEqual(response.error.code, ErrorCode.TASKS_UNAVAILABLE)

    def test_handler_signatures_follow_the_exact_input_model_field_order(self) -> None:
        # Catches composition adapters needing a broad **arguments compatibility bag.
        from daem0nmcp.api.v7 import pinned
        from daem0nmcp.api.v7.tools import TOOL_INPUT_MODELS

        dependencies = pinned.PinnedDependencies(
            workspace_resolver=object(),
            covenant_gate=object(),
            argument_normalizer=lambda *_args: {},
            briefing_service=object(),
            preflight_service=object(),
            recall_service=object(),
            memory_event_writer=object(),
            health_service=object(),
        )
        handlers = pinned.build_pinned_handlers(dependencies)

        for name, handler in handlers.items():
            with self.subTest(tool=name):
                self.assertEqual(
                    tuple(inspect.signature(handler).parameters),
                    tuple(TOOL_INPUT_MODELS[name].model_fields),
                )

    def test_session_brief_records_communion_after_valid_assembly(self) -> None:
        # Catches early communion marking and untyped briefing responses.
        from daem0nmcp.api.v7.pinned import build_pinned_handlers
        from daem0nmcp.api.v7.tools import SessionBriefOutput

        workspace = Workspace(WORKSPACE_ID, Path.cwd())
        scope = InvocationScope("principal", "session", str(workspace.root))
        gate = _gate()
        handlers = build_pinned_handlers(
            _dependencies(
                gate=gate,
                scope=scope,
                workspace=workspace,
                briefing_service=_BriefingService(gate, scope),
            )
        )

        try:
            response = asyncio.run(
                handlers["session_brief"](
                    workspace_id=WORKSPACE_ID,
                    focus_areas=["retrieval"],
                    warning_limit=2,
                    failure_limit=3,
                )
            )
        except NotImplementedError:
            response = None

        self.assertIsNotNone(response, "session briefing is not implemented")
        validated = SessionBriefOutput.model_validate(response)
        self.assertTrue(validated.ok)
        self.assertEqual(validated.data.workspace_id, WORKSPACE_ID)
        self.assertEqual(validated.data.workspace_statistics, {"focus_count": 1})
        self.assertTrue(gate.state_store.is_briefed(scope))

    def test_failed_briefing_is_opaque_and_does_not_record_communion(self) -> None:
        # Catches fail-open communion and exception/path disclosure on assembly errors.
        from daem0nmcp.api.v7.errors import ErrorCode
        from daem0nmcp.api.v7.pinned import build_pinned_handlers
        from daem0nmcp.api.v7.tools import SessionBriefOutput

        workspace = Workspace(WORKSPACE_ID, Path.cwd())
        scope = InvocationScope("principal", "session", str(workspace.root))
        gate = _gate()
        handlers = build_pinned_handlers(
            _dependencies(
                gate=gate,
                scope=scope,
                workspace=workspace,
                briefing_service=_FailingBriefingService(),
            )
        )

        try:
            response = asyncio.run(
                handlers["session_brief"](
                    workspace_id=WORKSPACE_ID,
                    focus_areas=[],
                    warning_limit=10,
                    failure_limit=10,
                )
            )
        except RuntimeError:
            response = None

        self.assertIsNotNone(response, "briefing errors are not enveloped")
        validated = SessionBriefOutput.model_validate(response)
        self.assertFalse(validated.ok)
        self.assertEqual(validated.error.code, ErrorCode.INTERNAL_ERROR)
        self.assertNotIn("private", validated.model_dump_json())
        self.assertNotIn("prompt-secret", validated.model_dump_json())
        self.assertFalse(gate.state_store.is_briefed(scope))

    def test_unknown_workspace_fails_with_an_opaque_response_before_briefing(
        self,
    ) -> None:
        # Catches workspace enumeration and resolver exception disclosure.
        from daem0nmcp.api.v7.errors import ErrorCode
        from daem0nmcp.api.v7.pinned import build_pinned_handlers
        from daem0nmcp.api.v7.tools import SessionBriefOutput

        workspace = Workspace(WORKSPACE_ID, Path.cwd())
        scope = InvocationScope("principal", "session", str(workspace.root))
        handlers = build_pinned_handlers(
            _dependencies(
                gate=_gate(),
                scope=scope,
                workspace=workspace,
                briefing_service=_UnusedService(),
            )
        )
        unknown_id = "ws_" + "f" * 24

        try:
            response = asyncio.run(
                handlers["session_brief"](
                    workspace_id=unknown_id,
                    focus_areas=[],
                    warning_limit=10,
                    failure_limit=10,
                )
            )
        except WorkspaceAccessError:
            response = None

        self.assertIsNotNone(response, "workspace failure is not enveloped")
        validated = SessionBriefOutput.model_validate(response)
        self.assertFalse(validated.ok)
        self.assertEqual(validated.error.code, ErrorCode.UNAUTHORIZED_WORKSPACE)
        self.assertNotIn(str(workspace.root), validated.model_dump_json())

    def test_every_workspace_bound_handler_envelopes_resolution_failure(self) -> None:
        # Catches one pinned vertical bypassing the common opaque workspace gate.
        from daem0nmcp.api.v7.errors import ErrorCode
        from daem0nmcp.api.v7.pinned import build_pinned_handlers
        from daem0nmcp.api.v7.tools import (
            MemoryPreflightOutput,
            MemoryRecallOutput,
            MemoryRecordOutcomeOutput,
            MemoryStoreOutput,
            SystemHealthOutput,
        )

        workspace = Workspace(WORKSPACE_ID, Path.cwd())
        scope = InvocationScope("principal", "session", str(workspace.root))
        unused = _UnusedService()
        handlers = build_pinned_handlers(
            _dependencies(
                gate=_gate(),
                scope=scope,
                workspace=workspace,
                briefing_service=unused,
            )
        )
        unknown_id = "ws_" + "f" * 24
        cases = (
            (
                "memory_preflight",
                MemoryPreflightOutput,
                {
                    "workspace_id": unknown_id,
                    "target_tool": "memory_store",
                    "target_arguments": {
                        "record_type": "decision",
                        "content": "Unknown workspace.",
                        "idempotency_key": "decision-9991",
                    },
                    "description": None,
                },
            ),
            (
                "memory_recall",
                MemoryRecallOutput,
                {
                    "workspace_id": unknown_id,
                    "query": "unknown workspace",
                    "limit": 10,
                    "candidate_limit": 50,
                    "categories": None,
                    "tags": None,
                    "record_ids": None,
                    "linked_workspace_ids": set(),
                    "as_of_valid_time": None,
                    "as_of_transaction_time": None,
                    "include_invalidated": False,
                    "include_archived": False,
                    "token_budget": 2400,
                    "rerank": False,
                },
            ),
            (
                "memory_store",
                MemoryStoreOutput,
                {
                    "workspace_id": unknown_id,
                    "record_type": "decision",
                    "content": "Unknown workspace.",
                    "idempotency_key": "decision-9992",
                    "preflight_token": "capability-token-9992",
                },
            ),
            (
                "memory_record_outcome",
                MemoryRecordOutcomeOutput,
                {
                    "workspace_id": unknown_id,
                    "record_id": "mem_" + "9" * 64,
                    "outcome_text": "Unknown workspace.",
                    "worked": False,
                    "idempotency_key": "outcome-9993",
                },
            ),
            (
                "system_health",
                SystemHealthOutput,
                {"workspace_id": unknown_id, "include_components": True},
            ),
        )

        for name, output_model, arguments in cases:
            with self.subTest(tool=name):
                try:
                    response = asyncio.run(handlers[name](**arguments))
                except WorkspaceAccessError:
                    response = None
                self.assertIsNotNone(response, f"{name} leaked resolver failure")
                validated = output_model.model_validate(response)
                self.assertFalse(validated.ok)
                self.assertEqual(
                    validated.error.code,
                    ErrorCode.UNAUTHORIZED_WORKSPACE,
                )

    def test_briefing_requires_transport_identity_before_assembly(self) -> None:
        # Catches project-global communion when no transport-derived scope exists.
        from daem0nmcp.api.v7.errors import ErrorCode
        from daem0nmcp.api.v7.pinned import PinnedDependencies, build_pinned_handlers
        from daem0nmcp.api.v7.tools import SessionBriefOutput, build_argument_normalizer

        workspace = Workspace(WORKSPACE_ID, Path.cwd())
        unused = _UnusedService()
        handlers = build_pinned_handlers(
            PinnedDependencies(
                workspace_resolver=_WorkspaceResolver(workspace),
                covenant_gate=_gate(),
                argument_normalizer=build_argument_normalizer(),
                briefing_service=unused,
                preflight_service=unused,
                recall_service=unused,
                memory_event_writer=unused,
                health_service=unused,
                response_factory=ResponseFactory(
                    clock=lambda: NOW,
                    request_id=lambda: "req_pinned_tests",
                ),
                scope_provider=lambda: None,
                clock=lambda: NOW,
            )
        )

        try:
            response = asyncio.run(
                handlers["session_brief"](
                    workspace_id=WORKSPACE_ID,
                    focus_areas=[],
                    warning_limit=10,
                    failure_limit=10,
                )
            )
        except PermissionError:
            response = None

        self.assertIsNotNone(response, "missing identity is not enveloped")
        validated = SessionBriefOutput.model_validate(response)
        self.assertFalse(validated.ok)
        self.assertEqual(validated.error.code, ErrorCode.IDENTITY_UNAVAILABLE)

    def test_every_non_health_handler_requires_transport_identity_first(self) -> None:
        # Catches a vertical using project-global state when transport scope is absent.
        from daem0nmcp.api.v7.errors import ErrorCode
        from daem0nmcp.api.v7.pinned import PinnedDependencies, build_pinned_handlers
        from daem0nmcp.api.v7.tools import (
            MemoryPreflightOutput,
            MemoryRecallOutput,
            MemoryRecordOutcomeOutput,
            MemoryStoreOutput,
            build_argument_normalizer,
        )

        workspace = Workspace(WORKSPACE_ID, Path.cwd())
        unused = _UnusedService()
        handlers = build_pinned_handlers(
            PinnedDependencies(
                workspace_resolver=_WorkspaceResolver(workspace),
                covenant_gate=_gate(),
                argument_normalizer=build_argument_normalizer(),
                briefing_service=unused,
                preflight_service=unused,
                recall_service=unused,
                memory_event_writer=unused,
                health_service=unused,
                response_factory=ResponseFactory(
                    clock=lambda: NOW,
                    request_id=lambda: "req_pinned_tests",
                ),
                scope_provider=lambda: None,
                clock=lambda: NOW,
            )
        )
        cases = (
            (
                "memory_preflight",
                MemoryPreflightOutput,
                {
                    "workspace_id": WORKSPACE_ID,
                    "target_tool": "memory_store",
                    "target_arguments": {
                        "record_type": "decision",
                        "content": "Identity required.",
                        "idempotency_key": "decision-9994",
                    },
                    "description": None,
                },
            ),
            (
                "memory_recall",
                MemoryRecallOutput,
                {
                    "workspace_id": WORKSPACE_ID,
                    "query": "identity",
                    "limit": 10,
                    "candidate_limit": 50,
                    "categories": None,
                    "tags": None,
                    "record_ids": None,
                    "linked_workspace_ids": set(),
                    "as_of_valid_time": None,
                    "as_of_transaction_time": None,
                    "include_invalidated": False,
                    "include_archived": False,
                    "token_budget": 2400,
                    "rerank": False,
                },
            ),
            (
                "memory_store",
                MemoryStoreOutput,
                {
                    "workspace_id": WORKSPACE_ID,
                    "record_type": "decision",
                    "content": "Identity required.",
                    "idempotency_key": "decision-9995",
                    "preflight_token": "capability-token-9995",
                },
            ),
            (
                "memory_record_outcome",
                MemoryRecordOutcomeOutput,
                {
                    "workspace_id": WORKSPACE_ID,
                    "record_id": "mem_" + "8" * 64,
                    "outcome_text": "Identity required.",
                    "worked": False,
                    "idempotency_key": "outcome-9996",
                },
            ),
        )
        for name, output_model, arguments in cases:
            with self.subTest(tool=name):
                try:
                    response = asyncio.run(handlers[name](**arguments))
                except PermissionError:
                    response = None
                self.assertIsNotNone(response, f"{name} did not envelope identity")
                validated = output_model.model_validate(response)
                self.assertFalse(validated.ok)
                self.assertEqual(
                    validated.error.code,
                    ErrorCode.IDENTITY_UNAVAILABLE,
                )

    def test_preflight_validates_defaults_and_issues_an_exact_scoped_token(
        self,
    ) -> None:
        # Catches tokens signed over raw/missing defaults or a broad target bag.
        from daem0nmcp.api.v7.pinned import build_pinned_handlers
        from daem0nmcp.api.v7.tools import (
            MemoryPreflightOutput,
            MemoryStoreInput,
        )

        workspace = Workspace(WORKSPACE_ID, Path.cwd())
        scope = InvocationScope("principal", "session", str(workspace.root))
        gate = _gate()
        gate.record_briefing(scope)
        handlers = build_pinned_handlers(
            _dependencies(
                gate=gate,
                scope=scope,
                workspace=workspace,
                briefing_service=_UnusedService(),
                preflight_service=_PreflightService(),
            )
        )

        try:
            response = asyncio.run(
                handlers["memory_preflight"](
                    workspace_id=WORKSPACE_ID,
                    target_tool="memory_store",
                    target_arguments={
                        "record_type": "decision",
                        "content": "Use event streams.",
                        "idempotency_key": "decision-0001",
                    },
                    description="Persist the decision.",
                )
            )
        except NotImplementedError:
            response = None

        self.assertIsNotNone(response, "memory preflight is not implemented")
        validated = MemoryPreflightOutput.model_validate(response)
        self.assertTrue(validated.ok)
        self.assertEqual(validated.data.target_tool, "memory_store")
        self.assertEqual(validated.data.expires_at, NOW + timedelta(seconds=300))
        token = validated.data.preflight_token
        self.assertIsNotNone(token)
        store_request = MemoryStoreInput(
            workspace_id=WORKSPACE_ID,
            record_type="decision",
            content="Use event streams.",
            idempotency_key="decision-0001",
            preflight_token=token,
        )
        self.assertIsNone(
            gate.authorize(
                "memory_store",
                store_request.model_dump(),
                scope,
                preflight_token=token,
            )
        )

    def test_preflight_requires_communion_before_target_guidance(self) -> None:
        # Catches pre-policy guidance reads and token issuance in a fresh scope.
        from daem0nmcp.api.v7.errors import ErrorCode
        from daem0nmcp.api.v7.pinned import build_pinned_handlers
        from daem0nmcp.api.v7.tools import MemoryPreflightOutput

        workspace = Workspace(WORKSPACE_ID, Path.cwd())
        scope = InvocationScope("principal", "session", str(workspace.root))
        gate = _gate()
        handlers = build_pinned_handlers(
            _dependencies(
                gate=gate,
                scope=scope,
                workspace=workspace,
                briefing_service=_UnusedService(),
                preflight_service=_UnusedService(),
            )
        )

        try:
            response = asyncio.run(
                handlers["memory_preflight"](
                    workspace_id=WORKSPACE_ID,
                    target_tool="memory_store",
                    target_arguments={
                        "record_type": "decision",
                        "content": "Do not read guidance yet.",
                        "idempotency_key": "decision-0004",
                    },
                    description=None,
                )
            )
        except (AssertionError, PermissionError):
            response = None

        self.assertIsNotNone(response, "preflight admission is not enveloped")
        validated = MemoryPreflightOutput.model_validate(response)
        self.assertFalse(validated.ok)
        self.assertEqual(validated.error.code, ErrorCode.COMMUNION_REQUIRED)
        self.assertEqual(gate.state_store.status(scope)["active_capabilities"], 0)

    def test_failed_preflight_guidance_is_opaque_and_issues_no_token(self) -> None:
        # Catches capability issuance before a complete guidance response exists.
        from daem0nmcp.api.v7.errors import ErrorCode
        from daem0nmcp.api.v7.pinned import build_pinned_handlers
        from daem0nmcp.api.v7.tools import MemoryPreflightOutput

        workspace = Workspace(WORKSPACE_ID, Path.cwd())
        scope = InvocationScope("principal", "session", str(workspace.root))
        gate = _gate()
        gate.record_briefing(scope)
        handlers = build_pinned_handlers(
            _dependencies(
                gate=gate,
                scope=scope,
                workspace=workspace,
                briefing_service=_UnusedService(),
                preflight_service=_FailingPreflightService(),
            )
        )

        try:
            response = asyncio.run(
                handlers["memory_preflight"](
                    workspace_id=WORKSPACE_ID,
                    target_tool="memory_store",
                    target_arguments={
                        "record_type": "decision",
                        "content": "Guidance must complete first.",
                        "idempotency_key": "decision-0005",
                    },
                    description=None,
                )
            )
        except RuntimeError:
            response = None

        self.assertIsNotNone(response, "preflight failure is not enveloped")
        validated = MemoryPreflightOutput.model_validate(response)
        self.assertFalse(validated.ok)
        self.assertEqual(validated.error.code, ErrorCode.INTERNAL_ERROR)
        self.assertNotIn("guidance-secret", validated.model_dump_json())
        self.assertEqual(gate.state_store.status(scope)["active_capabilities"], 0)

    def test_path_bearing_guidance_fails_before_capability_issue(self) -> None:
        # Catches issuing a usable token for a response that must be redacted.
        from daem0nmcp.api.v7.errors import ErrorCode
        from daem0nmcp.api.v7.pinned import build_pinned_handlers
        from daem0nmcp.api.v7.tools import MemoryPreflightOutput

        workspace = Workspace(WORKSPACE_ID, Path.cwd())
        scope = InvocationScope("principal", "session", str(workspace.root))
        gate = _gate()
        gate.record_briefing(scope)
        handlers = build_pinned_handlers(
            _dependencies(
                gate=gate,
                scope=scope,
                workspace=workspace,
                briefing_service=_UnusedService(),
                preflight_service=_LeakyPreflightService(),
            )
        )

        response = asyncio.run(
            handlers["memory_preflight"](
                workspace_id=WORKSPACE_ID,
                target_tool="memory_store",
                target_arguments={
                    "record_type": "decision",
                    "content": "No path-bearing guidance.",
                    "idempotency_key": "decision-0006",
                },
                description=None,
            )
        )

        validated = MemoryPreflightOutput.model_validate(response)
        self.assertFalse(validated.ok)
        self.assertEqual(validated.error.code, ErrorCode.INTERNAL_ERROR)
        self.assertNotIn("policy.txt", validated.model_dump_json())
        self.assertEqual(gate.state_store.status(scope)["active_capabilities"], 0)

    def test_preflight_rejects_reserved_target_fields_before_guidance_or_issue(
        self,
    ) -> None:
        # Catches caller override of workspace/token fields inside the target bag.
        from daem0nmcp.api.v7.errors import ErrorCode
        from daem0nmcp.api.v7.pinned import build_pinned_handlers
        from daem0nmcp.api.v7.tools import MemoryPreflightOutput

        workspace = Workspace(WORKSPACE_ID, Path.cwd())
        scope = InvocationScope("principal", "session", str(workspace.root))
        gate = _gate()
        gate.record_briefing(scope)
        handlers = build_pinned_handlers(
            _dependencies(
                gate=gate,
                scope=scope,
                workspace=workspace,
                briefing_service=_UnusedService(),
                preflight_service=_UnusedService(),
            )
        )

        try:
            response = asyncio.run(
                handlers["memory_preflight"](
                    workspace_id=WORKSPACE_ID,
                    target_tool="memory_store",
                    target_arguments={
                        "workspace_id": "ws_" + "b" * 24,
                        "record_type": "decision",
                        "content": "Do not run guidance.",
                        "idempotency_key": "decision-0002",
                    },
                    description=None,
                )
            )
        except Exception:
            response = None

        self.assertIsNotNone(response, "reserved target fields are not enveloped")
        validated = MemoryPreflightOutput.model_validate(response)
        self.assertFalse(validated.ok)
        self.assertEqual(validated.error.code, ErrorCode.INVALID_ARGUMENT)
        self.assertEqual(gate.state_store.status(scope)["active_capabilities"], 0)

    def test_preflight_rejects_target_arguments_that_do_not_match_the_model(
        self,
    ) -> None:
        # Catches capabilities issued for incomplete or loosely validated mutations.
        from daem0nmcp.api.v7.errors import ErrorCode
        from daem0nmcp.api.v7.pinned import build_pinned_handlers
        from daem0nmcp.api.v7.tools import MemoryPreflightOutput

        workspace = Workspace(WORKSPACE_ID, Path.cwd())
        scope = InvocationScope("principal", "session", str(workspace.root))
        gate = _gate()
        gate.record_briefing(scope)
        handlers = build_pinned_handlers(
            _dependencies(
                gate=gate,
                scope=scope,
                workspace=workspace,
                briefing_service=_UnusedService(),
                preflight_service=_UnusedService(),
            )
        )

        try:
            response = asyncio.run(
                handlers["memory_preflight"](
                    workspace_id=WORKSPACE_ID,
                    target_tool="memory_store",
                    target_arguments={"idempotency_key": "decision-0003"},
                    description=None,
                )
            )
        except ValueError:
            response = None

        self.assertIsNotNone(response, "invalid target arguments are not enveloped")
        validated = MemoryPreflightOutput.model_validate(response)
        self.assertFalse(validated.ok)
        self.assertEqual(validated.error.code, ErrorCode.INVALID_ARGUMENT)
        self.assertEqual(gate.state_store.status(scope)["active_capabilities"], 0)

    def test_description_only_preflight_returns_guidance_without_a_token(self) -> None:
        from daem0nmcp.api.v7.pinned import build_pinned_handlers
        from daem0nmcp.api.v7.tools import MemoryPreflightOutput, PreflightGuidance

        workspace = Workspace(WORKSPACE_ID, Path.cwd())
        scope = InvocationScope("principal", "session", str(workspace.root))
        gate = _gate()
        gate.record_briefing(scope)
        observed: list[tuple[str, dict[str, object], str | None]] = []

        class Guidance:
            async def guidance(
                self,
                _workspace,
                target_tool,
                normalized_arguments,
                description,
            ):
                observed.append((target_tool, dict(normalized_arguments), description))
                return PreflightGuidance(
                    records=[],
                    rules=[],
                    must_do=["Supply the complete mutation arguments."],
                    must_not=[],
                    ask_first=[],
                    warnings=[],
                )

        handlers = build_pinned_handlers(
            _dependencies(
                gate=gate,
                scope=scope,
                workspace=workspace,
                briefing_service=_UnusedService(),
                preflight_service=Guidance(),
            )
        )
        response = asyncio.run(
            handlers["memory_preflight"](
                workspace_id=WORKSPACE_ID,
                target_tool="memory_store",
                target_arguments={"idempotency_key": "decision-draft"},
                description="Plan a decision record before content is final.",
            )
        )

        validated = MemoryPreflightOutput.model_validate(response)
        self.assertTrue(validated.ok)
        self.assertIsNone(validated.data.preflight_token)
        self.assertIsNone(validated.data.expires_at)
        # An ok:true draft must say why no token was issued (F-070/F-082).
        [warning] = validated.meta.warnings
        self.assertEqual("PREFLIGHT_TOKEN_NOT_ISSUED", warning.code)
        self.assertIn("record_type (missing)", warning.message)
        self.assertIn("content (missing)", warning.message)
        self.assertIn("No preflight token issued", warning.message)
        self.assertEqual(len(observed), 1)
        self.assertEqual(observed[0][0], "memory_store")
        self.assertEqual(gate.state_store.status(scope)["active_capabilities"], 0)

    def test_recall_admission_failure_is_typed_and_does_not_run_retrieval(self) -> None:
        # Catches policy checks performed after a potentially disclosing read.
        from daem0nmcp.api.v7.errors import ErrorCode
        from daem0nmcp.api.v7.pinned import build_pinned_handlers
        from daem0nmcp.api.v7.tools import MemoryRecallOutput

        workspace = Workspace(WORKSPACE_ID, Path.cwd())
        scope = InvocationScope("principal", "session", str(workspace.root))
        gate = _gate()
        handlers = build_pinned_handlers(
            _dependencies(
                gate=gate,
                scope=scope,
                workspace=workspace,
                briefing_service=_UnusedService(),
                recall_service=_ExplodingRecallService(),
            )
        )

        try:
            response = asyncio.run(
                handlers["memory_recall"](
                    workspace_id=WORKSPACE_ID,
                    query="deployment decision",
                    limit=10,
                    candidate_limit=50,
                    categories=None,
                    tags=None,
                    record_ids=None,
                    linked_workspace_ids=set(),
                    as_of_valid_time=None,
                    as_of_transaction_time=None,
                    include_invalidated=False,
                    include_archived=False,
                    token_budget=2400,
                    rerank=False,
                )
            )
        except (AssertionError, NotImplementedError):
            response = None

        self.assertIsNotNone(response, "recall admission failure is not enveloped")
        validated = MemoryRecallOutput.model_validate(response)
        self.assertFalse(validated.ok)
        self.assertEqual(validated.error.code, ErrorCode.COMMUNION_REQUIRED)
        self.assertEqual(validated.error.remedy.tool, "session_brief")
        self.assertEqual(
            validated.error.remedy.arguments,
            {"workspace_id": WORKSPACE_ID},
        )
        encoded = validated.model_dump_json()
        self.assertNotIn(str(workspace.root), encoded)

    def test_recall_passes_a_normalized_task8_query_and_envelopes_the_result(
        self,
    ) -> None:
        # Catches ad-hoc retrieval arguments and bare Task 8 result leakage.
        from daem0nmcp.api.v7.pinned import build_pinned_handlers
        from daem0nmcp.api.v7.tools import MemoryRecallOutput

        workspace = Workspace(WORKSPACE_ID, Path.cwd())
        scope = InvocationScope("principal", "session", str(workspace.root))
        gate = _gate()
        gate.record_briefing(scope)
        handlers = build_pinned_handlers(
            _dependencies(
                gate=gate,
                scope=scope,
                workspace=workspace,
                briefing_service=_UnusedService(),
                recall_service=_RecallService(),
            )
        )

        try:
            response = asyncio.run(
                handlers["memory_recall"](
                    workspace_id=WORKSPACE_ID,
                    query="deployment decision",
                    limit=7,
                    candidate_limit=21,
                    categories={"decision"},
                    tags={"release"},
                    record_ids=None,
                    linked_workspace_ids=set(),
                    as_of_valid_time=None,
                    as_of_transaction_time=None,
                    include_invalidated=False,
                    include_archived=False,
                    token_budget=512,
                    rerank=False,
                )
            )
        except (AssertionError, NotImplementedError):
            response = None

        self.assertIsNotNone(response, "memory recall is not implemented")
        validated = MemoryRecallOutput.model_validate(response)
        self.assertTrue(validated.ok)
        self.assertTrue(validated.data.abstained)
        self.assertEqual(
            validated.data.abstention_reason,
            "NO_POLICY_VALID_EVIDENCE",
        )
        self.assertEqual(validated.data.token_usage.budget, 512)

    def test_recall_backend_failure_is_an_opaque_api_response(self) -> None:
        # Catches repository paths/prompts escaping through raised exceptions.
        from daem0nmcp.api.v7.errors import ErrorCode
        from daem0nmcp.api.v7.pinned import build_pinned_handlers
        from daem0nmcp.api.v7.tools import MemoryRecallOutput

        workspace = Workspace(WORKSPACE_ID, Path.cwd())
        scope = InvocationScope("principal", "session", str(workspace.root))
        gate = _gate()
        gate.record_briefing(scope)
        handlers = build_pinned_handlers(
            _dependencies(
                gate=gate,
                scope=scope,
                workspace=workspace,
                briefing_service=_UnusedService(),
                recall_service=_FailingRecallService(),
            )
        )

        try:
            response = asyncio.run(
                handlers["memory_recall"](
                    workspace_id=WORKSPACE_ID,
                    query="deployment decision",
                    limit=10,
                    candidate_limit=50,
                    categories=None,
                    tags=None,
                    record_ids=None,
                    linked_workspace_ids=set(),
                    as_of_valid_time=None,
                    as_of_transaction_time=None,
                    include_invalidated=False,
                    include_archived=False,
                    token_budget=2400,
                    rerank=False,
                )
            )
        except RuntimeError:
            response = None

        self.assertIsNotNone(response, "recall failure is not enveloped")
        validated = MemoryRecallOutput.model_validate(response)
        self.assertFalse(validated.ok)
        self.assertEqual(validated.error.code, ErrorCode.INTERNAL_ERROR)
        encoded = validated.model_dump_json()
        self.assertNotIn("private", encoded)
        self.assertNotIn("retrieval-prompt", encoded)

    def test_recall_maps_expected_runtime_capability_failure(self) -> None:
        from daem0nmcp.api.v7.errors import ErrorCode
        from daem0nmcp.api.v7.pinned import build_pinned_handlers
        from daem0nmcp.api.v7.runtime_services import RuntimeServiceError
        from daem0nmcp.api.v7.tools import MemoryRecallOutput

        class FederationUnavailable:
            async def retrieve(self, *args: object, **kwargs: object) -> object:
                del args, kwargs
                raise RuntimeServiceError("FEDERATION_UNAVAILABLE")

        workspace = Workspace(WORKSPACE_ID, Path.cwd())
        scope = InvocationScope("principal", "session", str(workspace.root))
        gate = _gate()
        gate.record_briefing(scope)
        handlers = build_pinned_handlers(
            _dependencies(
                gate=gate,
                scope=scope,
                workspace=workspace,
                briefing_service=_UnusedService(),
                recall_service=FederationUnavailable(),
            )
        )

        response = asyncio.run(
            handlers["memory_recall"](
                workspace_id=WORKSPACE_ID,
                query="linked decision",
                limit=10,
                candidate_limit=50,
                categories=None,
                tags=None,
                record_ids=None,
                linked_workspace_ids={"ws_" + "b" * 24},
                as_of_valid_time=None,
                as_of_transaction_time=None,
                include_invalidated=False,
                include_archived=False,
                token_budget=2400,
                rerank=False,
            )
        )

        validated = MemoryRecallOutput.model_validate(response)
        self.assertFalse(validated.ok)
        self.assertEqual(ErrorCode.CAPABILITY_DISABLED, validated.error.code)

    def test_federated_recall_requires_independent_target_briefing(self) -> None:
        from daem0nmcp.api.v7.models import RetrievalData, TokenUsage
        from daem0nmcp.api.v7.pinned import build_pinned_handlers
        from daem0nmcp.api.v7.tools import MemoryRecallOutput

        linked_id = "ws_" + "b" * 24
        origin = Workspace(WORKSPACE_ID, Path.cwd())
        linked = Workspace(linked_id, Path.cwd() / "linked")
        origin_scope = InvocationScope("principal", "session", str(origin.root))
        linked_scope = InvocationScope("principal", "session", str(linked.root))
        gate = _gate()
        gate.record_briefing(origin_scope)

        class FederatedRecall:
            async def retrieve(
                self, _workspace, _query, _linked_ids, authorizer
            ) -> object:
                await authorizer(linked)
                return RetrievalData(
                    abstained=True,
                    abstention_reason="NO_POLICY_VALID_EVIDENCE",
                    token_usage=TokenUsage(
                        budget=256,
                        requested=0,
                        selected=0,
                        rendered=0,
                        dropped=0,
                    ),
                )

        handlers = build_pinned_handlers(
            _dependencies(
                gate=gate,
                scope=origin_scope,
                workspace=origin,
                briefing_service=_UnusedService(),
                recall_service=FederatedRecall(),
            )
        )
        arguments = {
            "workspace_id": WORKSPACE_ID,
            "query": "linked decision",
            "linked_workspace_ids": {linked_id},
            "token_budget": 256,
        }

        denied = MemoryRecallOutput.model_validate(
            asyncio.run(handlers["memory_recall"](**arguments))
        )
        self.assertFalse(denied.ok)
        self.assertEqual("COMMUNION_REQUIRED", denied.error.code.value)

        gate.record_briefing(linked_scope)
        admitted = MemoryRecallOutput.model_validate(
            asyncio.run(handlers["memory_recall"](**arguments))
        )
        self.assertTrue(admitted.ok)

    def test_durable_federated_recall_restores_identity_and_rechecks_briefing(
        self,
    ) -> None:
        from daem0nmcp.api.v7.models import RetrievalData, TokenUsage
        from daem0nmcp.api.v7.pinned import build_pinned_handlers
        from daem0nmcp.api.v7.tasks import (
            DurableTaskExecution,
            durable_task_arguments_sha256,
            durable_task_execution_var,
        )
        from daem0nmcp.api.v7.tools import MemoryRecallInput, MemoryRecallOutput

        linked_id = "ws_" + "b" * 24
        origin = Workspace(WORKSPACE_ID, Path.cwd())
        linked = Workspace(linked_id, Path.cwd() / "linked")
        origin_scope = InvocationScope("principal", "session", str(origin.root))
        linked_scope = InvocationScope("principal", "session", str(linked.root))

        class FederatedRecall:
            async def retrieve(
                self, _workspace, _query, _linked_ids, authorizer
            ) -> object:
                await authorizer(linked)
                await authorizer(linked)
                return RetrievalData(
                    abstained=True,
                    abstention_reason="NO_POLICY_VALID_EVIDENCE",
                    token_usage=TokenUsage(
                        budget=256,
                        requested=0,
                        selected=0,
                        rendered=0,
                        dropped=0,
                    ),
                )

        arguments = {
            "workspace_id": WORKSPACE_ID,
            "query": "durable linked decision",
            "linked_workspace_ids": {linked_id},
            "token_budget": 256,
        }
        effective = MemoryRecallInput.model_validate(arguments).model_dump(mode="json")
        execution = DurableTaskExecution(
            task_id="tsk_" + "1" * 64,
            tool_name="memory_recall",
            workspace_id=WORKSPACE_ID,
            arguments_sha256=durable_task_arguments_sha256(effective),
            principal_id="principal",
            transport_session_id="session",
        )

        def invoke(gate: CovenantGate, current: DurableTaskExecution):
            handlers = build_pinned_handlers(
                _dependencies(
                    gate=gate,
                    # Durable execution must not borrow ambient request scope.
                    scope=InvocationScope("ambient", "wrong", str(origin.root)),
                    workspace=origin,
                    briefing_service=_UnusedService(),
                    recall_service=FederatedRecall(),
                )
            )
            token = durable_task_execution_var.set(current)
            try:
                return MemoryRecallOutput.model_validate(
                    asyncio.run(handlers["memory_recall"](**arguments))
                )
            finally:
                durable_task_execution_var.reset(token)

        admitted_gate = _gate()
        admitted_gate.record_briefing(origin_scope)
        admitted_gate.record_briefing(linked_scope)
        self.assertTrue(invoke(admitted_gate, execution).ok)

        lost_access = _gate()
        lost_access.record_briefing(origin_scope)
        denied = invoke(lost_access, execution)
        self.assertFalse(denied.ok)
        self.assertEqual("COMMUNION_REQUIRED", denied.error.code.value)

        missing_session = DurableTaskExecution(
            task_id=execution.task_id,
            tool_name=execution.tool_name,
            workspace_id=execution.workspace_id,
            arguments_sha256=execution.arguments_sha256,
            principal_id=execution.principal_id,
            transport_session_id=None,
        )
        renewed = invoke(admitted_gate, missing_session)
        self.assertFalse(renewed.ok)
        self.assertEqual("COMMUNION_REQUIRED", renewed.error.code.value)

    def test_store_consumes_admission_and_passes_only_a_sanitized_event_command(
        self,
    ) -> None:
        # Catches token forwarding and mutation responses detached from Task 7 events.
        from daem0nmcp.api.v7 import pinned
        from daem0nmcp.api.v7.tools import MemoryStoreInput, MemoryStoreOutput

        command_type = getattr(pinned, "MemoryStoreCommand", None)
        result_type = getattr(pinned, "StoredMemory", None)
        self.assertIsNotNone(command_type, "the store command seam is missing")
        self.assertIsNotNone(result_type, "the Task 7 store result seam is missing")
        assert result_type is not None

        workspace = Workspace(WORKSPACE_ID, Path.cwd())
        scope = InvocationScope("principal", "session", str(workspace.root))
        gate = _gate()
        gate.record_briefing(scope)
        target = {
            "workspace_id": WORKSPACE_ID,
            "record_type": "decision",
            "content": "Use append-only events.",
            "rationale": "Deterministic history is auditable.",
            "context": {"component": "memory"},
            "tags": ["events", "v7"],
            "relative_file_path": "daem0nmcp/event_store.py",
            "happened_at": NOW,
            "procedure_steps": [],
            "idempotency_key": "decision-1001",
        }
        token = gate.issue_preflight(scope, "memory_store", target)
        writer = _MemoryEventWriter(result_type)
        handlers = pinned.build_pinned_handlers(
            _dependencies(
                gate=gate,
                scope=scope,
                workspace=workspace,
                briefing_service=_UnusedService(),
                memory_event_writer=writer,
            )
        )
        request = MemoryStoreInput(**target, preflight_token=token)

        try:
            response = asyncio.run(handlers["memory_store"](**request.model_dump()))
        except NotImplementedError:
            response = None

        self.assertIsNotNone(response, "memory store is not implemented")
        validated = MemoryStoreOutput.model_validate(response)
        self.assertTrue(validated.ok)
        self.assertEqual(validated.data.record.record_id, "mem_" + "1" * 64)
        self.assertEqual(validated.data.event_id, "evt_" + "e" * 64)
        self.assertEqual(validated.data.stream_version, 1)
        self.assertFalse(validated.data.idempotent_replay)

    def test_store_argument_mismatch_is_rejected_before_writer_with_safe_remedy(
        self,
    ) -> None:
        # Catches post-preflight mutation and accidental capability forwarding.
        from daem0nmcp.api.v7.errors import ErrorCode
        from daem0nmcp.api.v7.pinned import build_pinned_handlers
        from daem0nmcp.api.v7.tools import MemoryStoreInput, MemoryStoreOutput

        workspace = Workspace(WORKSPACE_ID, Path.cwd())
        scope = InvocationScope("principal", "session", str(workspace.root))
        gate = _gate()
        gate.record_briefing(scope)
        original = {
            "workspace_id": WORKSPACE_ID,
            "record_type": "decision",
            "content": "Original approved content.",
            "idempotency_key": "decision-1002",
        }
        token = gate.issue_preflight(scope, "memory_store", original)
        request = MemoryStoreInput(
            **{**original, "content": "Changed after preflight."},
            preflight_token=token,
        )
        handlers = build_pinned_handlers(
            _dependencies(
                gate=gate,
                scope=scope,
                workspace=workspace,
                briefing_service=_UnusedService(),
                memory_event_writer=_UnusedService(),
            )
        )

        try:
            response = asyncio.run(handlers["memory_store"](**request.model_dump()))
        except AssertionError:
            response = None

        self.assertIsNotNone(response, "store admission did not stop the writer")
        validated = MemoryStoreOutput.model_validate(response)
        self.assertFalse(validated.ok)
        self.assertEqual(
            validated.error.code,
            ErrorCode.TOKEN_ARGUMENT_MISMATCH,
        )
        self.assertIsNotNone(validated.error.remedy)
        assert validated.error.remedy is not None
        self.assertEqual(validated.error.remedy.tool, "memory_preflight")
        self.assertEqual(
            validated.error.remedy.arguments["target_tool"],
            "memory_store",
        )
        encoded = validated.model_dump_json()
        self.assertNotIn(token, encoded)
        self.assertNotIn("preflight_token", encoded)

    def test_store_maps_typed_idempotency_and_stream_conflicts(self) -> None:
        # Catches storage conflict text leaking as untyped INTERNAL_ERROR responses.
        from daem0nmcp.api.v7 import pinned
        from daem0nmcp.api.v7.errors import ErrorCode
        from daem0nmcp.api.v7.tools import MemoryStoreInput, MemoryStoreOutput
        from daem0nmcp.event_store import EventStreamConflict

        conflict_type = getattr(pinned, "IdempotencyConflict", None)
        self.assertIsNotNone(
            conflict_type,
            "the typed idempotency conflict seam is missing",
        )
        assert conflict_type is not None
        cases = (
            (
                conflict_type(r"D:\private\idempotency-row"),
                ErrorCode.IDEMPOTENCY_CONFLICT,
            ),
            (
                EventStreamConflict(r"D:\private\stream-head"),
                ErrorCode.EVENT_STREAM_CONFLICT,
            ),
        )
        for error, expected_code in cases:
            with self.subTest(code=expected_code):
                workspace = Workspace(WORKSPACE_ID, Path.cwd())
                scope = InvocationScope(
                    "principal",
                    f"session-{expected_code.value}",
                    str(workspace.root),
                )
                gate = _gate()
                gate.record_briefing(scope)
                target = {
                    "workspace_id": WORKSPACE_ID,
                    "record_type": "decision",
                    "content": "Conflict-safe storage.",
                    "idempotency_key": "decision-1003",
                }
                token = gate.issue_preflight(scope, "memory_store", target)
                request = MemoryStoreInput(**target, preflight_token=token)
                handlers = pinned.build_pinned_handlers(
                    _dependencies(
                        gate=gate,
                        scope=scope,
                        workspace=workspace,
                        briefing_service=_UnusedService(),
                        memory_event_writer=_FailingMemoryEventWriter(error),
                    )
                )

                try:
                    response = asyncio.run(
                        handlers["memory_store"](**request.model_dump())
                    )
                except Exception:
                    response = None

                self.assertIsNotNone(response, "storage conflict was not enveloped")
                validated = MemoryStoreOutput.model_validate(response)
                self.assertFalse(validated.ok)
                self.assertEqual(validated.error.code, expected_code)
                self.assertNotIn("private", validated.model_dump_json())
                self.assertNotIn("stream-head", validated.model_dump_json())

    def test_record_outcome_uses_a_sanitized_task7_event_receipt(self) -> None:
        # Catches mutable legacy IDs and bare append results in the seal step.
        from daem0nmcp.api.v7 import pinned
        from daem0nmcp.api.v7.tools import (
            MemoryRecordOutcomeInput,
            MemoryRecordOutcomeOutput,
        )

        command_type = getattr(pinned, "MemoryOutcomeCommand", None)
        result_type = getattr(pinned, "RecordedOutcome", None)
        self.assertIsNotNone(command_type, "the outcome command seam is missing")
        self.assertIsNotNone(result_type, "the Task 7 outcome result seam is missing")
        assert result_type is not None

        workspace = Workspace(WORKSPACE_ID, Path.cwd())
        scope = InvocationScope("principal", "session", str(workspace.root))
        gate = _gate()
        gate.record_briefing(scope)
        handlers = pinned.build_pinned_handlers(
            _dependencies(
                gate=gate,
                scope=scope,
                workspace=workspace,
                briefing_service=_UnusedService(),
                memory_event_writer=_OutcomeEventWriter(result_type),
            )
        )
        request = MemoryRecordOutcomeInput(
            workspace_id=WORKSPACE_ID,
            record_id="mem_" + "2" * 64,
            outcome_text="The rollout stayed healthy.",
            worked=True,
            happened_at=NOW,
            idempotency_key="outcome-1001",
        )

        try:
            response = asyncio.run(
                handlers["memory_record_outcome"](**request.model_dump())
            )
        except NotImplementedError:
            response = None

        self.assertIsNotNone(response, "memory outcome is not implemented")
        validated = MemoryRecordOutcomeOutput.model_validate(response)
        self.assertTrue(validated.ok)
        self.assertEqual(validated.data.record_id, "mem_" + "2" * 64)
        self.assertEqual(validated.data.outcome_event_id, "evt_" + "a" * 64)
        self.assertEqual(validated.data.stream_version, 2)
        self.assertTrue(validated.data.worked)
        self.assertFalse(validated.data.idempotent_replay)

    def test_transient_writer_contention_is_retryable_database_in_use(self) -> None:
        # Pool saturation and SQLite lock timeouts clear on retry (F-020/F-027).
        from daem0nmcp.api.v7 import pinned
        from daem0nmcp.api.v7.errors import ErrorCode
        from daem0nmcp.api.v7.runtime_services import RuntimeServiceError
        from daem0nmcp.api.v7.tools import MemoryStoreInput, MemoryStoreOutput

        workspace = Workspace(WORKSPACE_ID, Path.cwd())
        scope = InvocationScope("principal", "session-busy", str(workspace.root))
        gate = _gate()
        gate.record_briefing(scope)
        target = {
            "workspace_id": WORKSPACE_ID,
            "record_type": "decision",
            "content": "Contention is transient.",
            "idempotency_key": "decision-busy-1",
        }
        token = gate.issue_preflight(scope, "memory_store", target)
        handlers = pinned.build_pinned_handlers(
            _dependencies(
                gate=gate,
                scope=scope,
                workspace=workspace,
                briefing_service=_UnusedService(),
                memory_event_writer=_FailingMemoryEventWriter(
                    RuntimeServiceError("DATABASE_IN_USE")
                ),
            )
        )

        request = MemoryStoreInput(**target, preflight_token=token).model_dump()
        response = asyncio.run(handlers["memory_store"](**request))

        validated = MemoryStoreOutput.model_validate(response)
        self.assertFalse(validated.ok)
        self.assertEqual(ErrorCode.DATABASE_IN_USE, validated.error.code)
        self.assertTrue(validated.error.retryable)
        self.assertEqual(250, validated.error.retry_after_ms)
        # authorize() spent the token before the write, so the documented
        # retry is a fresh preflight with the same idempotency_key.
        self.assertEqual("memory_preflight", validated.error.remedy.tool)
        remedy_target = validated.error.remedy.arguments["target_arguments"]
        self.assertEqual("decision-busy-1", remedy_target["idempotency_key"])
        self.assertNotIn("preflight_token", remedy_target)
        replayed = MemoryStoreOutput.model_validate(
            asyncio.run(handlers["memory_store"](**request))
        )
        self.assertEqual(ErrorCode.TOKEN_REPLAYED, replayed.error.code)
        self.assertEqual("memory_preflight", replayed.error.remedy.tool)

    def test_record_outcome_maps_runtime_not_found(self) -> None:
        from daem0nmcp.api.v7 import pinned
        from daem0nmcp.api.v7.errors import ErrorCode
        from daem0nmcp.api.v7.runtime_services import RuntimeServiceError
        from daem0nmcp.api.v7.tools import (
            MemoryRecordOutcomeInput,
            MemoryRecordOutcomeOutput,
        )

        workspace = Workspace(WORKSPACE_ID, Path.cwd())
        scope = InvocationScope("principal", "session", str(workspace.root))
        gate = _gate()
        gate.record_briefing(scope)
        handlers = pinned.build_pinned_handlers(
            _dependencies(
                gate=gate,
                scope=scope,
                workspace=workspace,
                briefing_service=_UnusedService(),
                memory_event_writer=_FailingMemoryEventWriter(
                    RuntimeServiceError("NOT_FOUND")
                ),
            )
        )
        request = MemoryRecordOutcomeInput(
            workspace_id=WORKSPACE_ID,
            record_id="mem_" + "9" * 64,
            outcome_text="No such memory.",
            worked=False,
            happened_at=NOW,
            idempotency_key="outcome-missing-1001",
        )

        response = asyncio.run(
            handlers["memory_record_outcome"](**request.model_dump())
        )

        validated = MemoryRecordOutcomeOutput.model_validate(response)
        self.assertFalse(validated.ok)
        self.assertEqual(ErrorCode.NOT_FOUND, validated.error.code)

    def test_system_health_returns_a_typed_path_free_envelope_without_workspace(
        self,
    ) -> None:
        # Catches health implementations that expose roots or require storage access.
        from daem0nmcp.api.v7.pinned import build_pinned_handlers
        from daem0nmcp.api.v7.tools import SystemHealthOutput

        workspace = Workspace(WORKSPACE_ID, Path.cwd())
        scope = InvocationScope("principal", "session", str(workspace.root))
        handlers = build_pinned_handlers(
            _dependencies(
                gate=_gate(),
                scope=scope,
                workspace=workspace,
                briefing_service=_UnusedService(),
                health_service=_HealthService(),
            )
        )

        try:
            response = asyncio.run(
                handlers["system_health"](
                    workspace_id=None,
                    include_components=False,
                )
            )
        except NotImplementedError:
            response = None

        self.assertIsNotNone(response, "system health is not implemented")
        validated = SystemHealthOutput.model_validate(response)
        self.assertTrue(validated.ok)
        self.assertIsNone(validated.meta.workspace_id)
        self.assertEqual(validated.data.storage_format_version, 7)
        self.assertEqual(validated.data.task_support.status, "disabled")
        encoded = validated.model_dump_json()
        self.assertNotIn(str(workspace.root), encoded)
        self.assertNotIn("project_path", encoded)

    def test_health_revocation_during_inspection_discards_all_data(self) -> None:
        from daem0nmcp.api.v7.pinned import build_pinned_handlers
        from daem0nmcp.api.v7.tools import SystemHealthOutput

        allowed = [True]
        gate = _gate()
        workspace = Workspace(WORKSPACE_ID, Path.cwd())
        scope = InvocationScope(
            "principal", "session", os.path.normcase(str(workspace.root))
        )

        class RevokingHealth(_HealthService):
            async def inspect(self, workspace, include_components):
                result = await super().inspect(None, False)
                allowed[0] = False
                return result

        with patch.object(
            gate, "workspace_authorized", side_effect=lambda _: allowed[0]
        ):
            handlers = build_pinned_handlers(
                _dependencies(
                    gate=gate,
                    scope=scope,
                    workspace=workspace,
                    briefing_service=_UnusedService(),
                    health_service=RevokingHealth(),
                )
            )
            response = asyncio.run(handlers["system_health"](workspace_id=WORKSPACE_ID))
        validated = SystemHealthOutput.model_validate(response)
        self.assertFalse(validated.ok)
        self.assertEqual(validated.error.code, "UNAUTHORIZED_WORKSPACE")
        self.assertIsNone(validated.data)

    def test_success_output_with_a_raw_path_fails_closed(self) -> None:
        # Catches valid-looking model text that reintroduces canonical paths.
        from daem0nmcp.api.v7.errors import ErrorCode
        from daem0nmcp.api.v7.pinned import build_pinned_handlers
        from daem0nmcp.api.v7.tools import SystemHealthOutput

        workspace = Workspace(WORKSPACE_ID, Path.cwd())
        scope = InvocationScope("principal", "session", str(workspace.root))
        handlers = build_pinned_handlers(
            _dependencies(
                gate=_gate(),
                scope=scope,
                workspace=workspace,
                briefing_service=_UnusedService(),
                health_service=_LeakyHealthService(),
            )
        )

        response = asyncio.run(
            handlers["system_health"](
                workspace_id=None,
                include_components=True,
            )
        )

        validated = SystemHealthOutput.model_validate(response)
        self.assertFalse(validated.ok)
        self.assertEqual(validated.error.code, ErrorCode.INTERNAL_ERROR)
        self.assertNotIn("private", validated.model_dump_json())
        self.assertNotIn("worker.log", validated.model_dump_json())


if __name__ == "__main__":
    unittest.main()
