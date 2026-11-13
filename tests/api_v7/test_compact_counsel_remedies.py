"""Sparse exact remedies preserve full canonical capability binding."""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import BaseModel, ConfigDict, Field, model_validator

from daem0nmcp.api.v7.application import V7ApplicationDependencies, V7ToolRouter
from daem0nmcp.api.v7.ceremony import CovenantCeremony
from daem0nmcp.api.v7.models import (
    MutationReceipt,
    PreflightGuidance,
    RecordSummary,
    RuleView,
)
from daem0nmcp.api.v7.opaque_capabilities import OpaqueCapabilityAuthority
from daem0nmcp.api.v7.pinned import PinnedDependencies, PinnedHandlers, StoredMemory
from daem0nmcp.api.v7.policy import V7_COVENANT_POLICY
from daem0nmcp.api.v7.responses import ResponseFactory
from daem0nmcp.api.v7.tools import build_argument_normalizer
from daem0nmcp.covenant import (
    CapabilityAuthority,
    CovenantGate,
    CovenantStateStore,
    InvocationScope,
)
from daem0nmcp.event_store import AppendedEvent
from daem0nmcp.workspace import Workspace


def _fixture(tmp_path, kind):
    now = [1_000]

    def clock():
        return now[0]

    workspace = Workspace("ws_" + "a" * 24, tmp_path.resolve())
    scope = InvocationScope("principal", "session", str(workspace.root))
    gate = CovenantGate(
        state_store=CovenantStateStore(clock=clock),
        authority=OpaqueCapabilityAuthority(
            CapabilityAuthority(secret=b"r" * 32, kid="compact", clock=clock),
            clock=clock,
        ),
        policy=V7_COVENANT_POLICY,
        argument_normalizer=build_argument_normalizer(),
    )
    gate.record_briefing(scope)
    calls = []

    class Resolver:
        def resolve(self, workspace_id):
            if workspace_id != workspace.workspace_id:
                raise LookupError("Unknown workspace")
            return workspace

    class Guidance:
        def guidance(self, *_args):
            return PreflightGuidance(
                rules=[
                    RuleView(
                        rule_id="rule_" + "a" * 64,
                        trigger="guard",
                        priority=1,
                        enabled=True,
                        must_not=["Do not bypass the reviewed constraint."],
                        created_at=datetime.fromtimestamp(now[0], timezone.utc),
                    )
                ]
            )

    async def brief(_workspace):
        return {
            "briefed_at": datetime.fromtimestamp(now[0], timezone.utc),
            "workspace_statistics": {},
        }

    ceremony = CovenantCeremony(
        covenant_gate=gate,
        argument_normalizer=build_argument_normalizer(),
        preflight_service=Guidance(),
        brief_reader=brief,
    )
    common = {
        "workspace_resolver": Resolver(),
        "covenant_gate": gate,
        "scope_provider": lambda: scope,
        "response_factory": ResponseFactory(),
        "ceremony": ceremony,
    }
    if kind == "pinned":

        class Writer:
            def store(self, workspace, command):
                calls.append(command)
                instant = datetime.fromtimestamp(now[0], timezone.utc)
                return StoredMemory(
                    record=RecordSummary(
                        record_id="mem_" + "a" * 64,
                        record_type=command.record_type,
                        excerpt=command.content,
                        current_status="current",
                        content_hash="a" * 64,
                        created_at=instant,
                        updated_at=instant,
                    ),
                    event=AppendedEvent(
                        event_id="evt_" + "a" * 64,
                        event_hash="a" * 64,
                        payload_hash="a" * 64,
                        stream_version=1,
                        previous_event_hash=None,
                    ),
                    idempotent_replay=False,
                )

        handler = PinnedHandlers(
            PinnedDependencies(
                **common,
                argument_normalizer=build_argument_normalizer(),
                briefing_service=object(),
                preflight_service=Guidance(),
                recall_service=object(),
                memory_event_writer=Writer(),
                health_service=object(),
            )
        ).memory_store
        arguments = {
            "workspace_id": workspace.workspace_id,
            "record_type": "decision",
            "content": "Preserve compact exact counsel replay",
            "rationale": "Explicit nondefault rationale",
            "idempotency_key": "compact-remedy-store-0001",
        }
        tool = "memory_store"
    else:

        async def operation(*, workspace, request):
            calls.append(request)
            return MutationReceipt(
                operation_id="op_compact_link", idempotent_replay=False
            )

        tool = "memory_link"
        handler = V7ToolRouter(
            V7ApplicationDependencies(**common, operations={tool: operation})
        ).handler(tool)
        arguments = {
            "workspace_id": workspace.workspace_id,
            "source_record_id": "mem_" + "a" * 64,
            "target_record_id": "mem_" + "b" * 64,
            "relationship_type": "related_to",
            "idempotency_key": "compact-remedy-link-0001",
        }
    return SimpleNamespace(
        handler=handler,
        tool=tool,
        arguments=arguments,
        gate=gate,
        scope=scope,
        calls=calls,
        now=now,
    )


@pytest.mark.parametrize("kind", ["pinned", "routed"])
async def test_actual_counsel_emitter_omits_only_redundant_defaults(tmp_path, kind):
    state = _fixture(tmp_path, kind)
    challenged = await state.handler(**state.arguments)
    assert challenged.error.code == "COUNSEL_REQUIRED"
    remedy = challenged.error.remedy
    assert remedy.tool == state.tool
    assert set(remedy.arguments) == set(state.arguments) | {"preflight_token"}
    assert {
        key: value
        for key, value in remedy.arguments.items()
        if key != "preflight_token"
    } == state.arguments
    assert state.calls == []


@pytest.mark.parametrize("kind", ["pinned", "routed"])
async def test_compact_exact_retry_preserves_bound_args_scope_expiry_and_single_use(
    tmp_path, kind
):
    from daem0nmcp.api.v7.tools import TOOL_INPUT_MODELS

    state = _fixture(tmp_path, kind)
    challenged = await state.handler(**state.arguments)
    arguments = challenged.error.remedy.arguments
    token = arguments["preflight_token"]
    canonical = (
        TOOL_INPUT_MODELS[state.tool].model_validate(arguments).model_dump(mode="json")
    )
    digest = state.gate.fingerprint(state.tool, canonical, state.scope)
    assert digest == state.gate.fingerprint(state.tool, arguments, state.scope)
    assert state.gate.authority.verify(token)["args_sha256"] == digest
    changed = (
        {**arguments, "content": "Changed bound content"}
        if kind == "pinned"
        else {**arguments, "relationship_type": "depends_on"}
    )
    rejection = state.gate.authorize(
        state.tool, changed, state.scope, preflight_token=token
    )
    assert rejection["violation"] == "TOKEN_ARGUMENT_MISMATCH"
    for scope in (
        InvocationScope("other-principal", "session", state.scope.canonical_workspace),
        InvocationScope("principal", "other-session", state.scope.canonical_workspace),
        InvocationScope("principal", "session", str(tmp_path / "other")),
    ):
        state.gate.record_briefing(scope)
        rejection = state.gate.authorize(
            state.tool, arguments, scope, preflight_token=token
        )
        assert rejection["violation"] == "TOKEN_SCOPE_MISMATCH"
    tampered = token[:-1] + ("a" if token[-1] != "a" else "b")
    rejection = state.gate.authorize(
        state.tool, arguments, state.scope, preflight_token=tampered
    )
    assert rejection["violation"] == "TOKEN_TAMPERED"
    assert state.calls == []
    result = await state.handler(**arguments)
    assert result.ok, result.error
    assert len(state.calls) == 1
    rejection = state.gate.authorize(
        state.tool, arguments, state.scope, preflight_token=token
    )
    assert rejection["violation"] == "TOKEN_REPLAYED"


@pytest.mark.parametrize("kind", ["pinned", "routed"])
async def test_compact_remedy_expired_capability_does_not_admit_write(tmp_path, kind):
    state = _fixture(tmp_path, kind)
    challenged = await state.handler(**state.arguments)
    arguments = challenged.error.remedy.arguments
    state.now[0] += 301
    rejection = state.gate.authorize(
        state.tool, arguments, state.scope, preflight_token=arguments["preflight_token"]
    )
    assert rejection["violation"] == "TOKEN_EXPIRED"
    assert state.calls == []


def test_compaction_keeps_json_types_required_fields_and_bound_defaults():
    from daem0nmcp.api.v7.remedies import compact_remedy_arguments

    class Input(BaseModel):
        model_config = ConfigDict(strict=True)
        workspace_id: str = "workspace-default"
        preflight_token: str | None = None
        idempotency_key: str | None = None
        required_null: None
        value: Any = 1
        optional: None = None

    for value in (True, 1.0):
        arguments = {
            "workspace_id": "workspace-default",
            "preflight_token": None,
            "idempotency_key": None,
            "required_null": None,
            "value": value,
            "optional": None,
        }
        result = compact_remedy_arguments(Input, arguments)
        assert result == {
            key: item for key, item in arguments.items() if key != "optional"
        }
        assert type(result["value"]) is type(value)
        assert arguments["optional"] is None


def test_compaction_never_executes_unknown_factories_or_loses_their_values():
    from daem0nmcp.api.v7.remedies import compact_remedy_arguments

    calls = []

    def dynamic_factory():
        calls.append(True)
        return "generated"

    class Input(BaseModel):
        dynamic: str = Field(default_factory=dynamic_factory)
        optional: None = None

    omitted = {"optional": None}
    assert compact_remedy_arguments(Input, omitted) == omitted
    supplied = {"dynamic": "generated", "optional": None}
    assert compact_remedy_arguments(Input, supplied) == supplied
    assert calls == []


def test_compaction_retains_payload_when_omission_changes_validation():
    from daem0nmcp.api.v7.remedies import compact_remedy_arguments

    class Input(BaseModel):
        optional: None = None

        @model_validator(mode="after")
        def require_explicit(self):
            if "optional" not in self.model_fields_set:
                raise ValueError("An explicit value is required")
            return self

    original = {"optional": None}
    assert compact_remedy_arguments(Input, original) == original


def test_compaction_retains_alias_and_nested_data_without_mutating_input():
    from daem0nmcp.api.v7.remedies import compact_remedy_arguments

    class Input(BaseModel):
        optional: None = Field(default=None, alias="public_optional")
        data: dict[str, Any]

    original = {
        "public_optional": None,
        "data": {"intentional_null": None, "empty": []},
    }
    assert compact_remedy_arguments(Input, original) == original
    assert original == {
        "public_optional": None,
        "data": {"intentional_null": None, "empty": []},
    }


def test_alias_cannot_bypass_unknown_factory_fail_closed_guard():
    from daem0nmcp.api.v7.remedies import compact_remedy_arguments

    calls = []

    def dynamic_factory():
        calls.append(True)
        return "generated"

    class Input(BaseModel):
        dynamic: str = Field(alias="public_dynamic", default_factory=dynamic_factory)
        optional: None = None

    original = {"dynamic": "supplied-by-field-name", "optional": None}
    result = compact_remedy_arguments(Input, original)
    assert calls == []
    assert result == original


def test_compaction_never_omits_challenge_capability_field():
    from daem0nmcp.api.v7.remedies import compact_remedy_arguments

    class Input(BaseModel):
        challenge_token: str | None = None
        optional: None = None

    original = {"challenge_token": None, "optional": None}
    assert compact_remedy_arguments(Input, original) == {"challenge_token": None}
