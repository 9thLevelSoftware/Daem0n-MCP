"""Actual production MCP and native bridge acceptance for P6."""

from __future__ import annotations

import os
from unittest.mock import patch

import pytest

from daem0nmcp.claude_hooks.native_edit import native_edit_request
from daem0nmcp.claude_hooks.post_edit import handle_post_edit
from daem0nmcp.claude_hooks.post_edit_preflight import (
    handle_edit_preflight_response,
)
from daem0nmcp.database import DatabaseManager
from daem0nmcp.edit_host import (
    EditHostConfig,
    EditHostStateStore,
    provision_local_bridge_installation,
)
from daem0nmcp.native_edit_gate import approve_native_edit
from daem0nmcp.workspace import WorkspaceRegistry
from tests.api_v7.process_client import call, process_client, succeed


@pytest.fixture
async def initialized_workspace(tmp_path):
    storage = tmp_path / ".daem0nmcp" / "storage"
    storage.mkdir(parents=True)
    manager = DatabaseManager(str(storage))
    try:
        await manager.init_db()
    finally:
        await manager.close()
    return WorkspaceRegistry([tmp_path], default_root=tmp_path).default


async def test_actual_stdio_mcp_shares_bridge_authority_and_promotes_capture(
    initialized_workspace,
    socket_tmp_path,
):
    workspace = initialized_workspace
    installation = provision_local_bridge_installation(
        workspace.root,
        config_root=socket_tmp_path / "host",
    )
    environment = installation.environment()
    scope = {"workspace_id": workspace.workspace_id}
    target = workspace.root / "src" / "service.py"
    target.parent.mkdir()
    target.write_text("before", encoding="utf-8")
    native_session_id = "claude-session-process-1"
    first_event = {
        "session_id": native_session_id,
        "tool_use_id": "tool-use-denied-1",
        "tool_name": "Edit",
        "tool_input": {
            "file_path": str(target),
            "old_string": "before",
            "new_string": "after",
        },
    }

    async with process_client(
        workspace.root,
        "stdio",
        environment_overrides=environment,
    ) as session:
        await succeed(session, "session_brief", scope)
        denied = approve_native_edit(
            first_event, str(workspace.root), environ=environment
        )
        assert not denied.allowed
        assert denied.code == "EDIT_APPROVAL_REQUIRED"
        assert denied.remedy is not None
        assert target.read_text() == "before"
        normalized = native_edit_request(
            project_path=workspace.root,
            tool_name="Edit",
            tool_input=first_event["tool_input"],
            configured_tools=frozenset({"Edit"}),
        )
        config = EditHostConfig.from_environment(environment)
        pending = EditHostStateStore(config).get_pending(
            workspace_id=workspace.workspace_id,
            native_session_id=native_session_id,
            credential_id=config.identity.credential_id,
            edit_hash=normalized.edit_hash,
        )
        assert pending is not None
        assert pending.native_input_hash is not None
        edit_request_id = pending.edit_request_id
        # Equivalent normalized paths still have different original arguments.
        changed = {
            **first_event,
            "tool_use_id": "changed-native-arguments",
            "tool_input": {**first_event["tool_input"], "file_path": "src/service.py"},
        }
        changed_decision = approve_native_edit(
            changed, str(workspace.root), environ=environment
        )
        assert not changed_decision.allowed
        assert changed_decision.code == "EDIT_NATIVE_ARGUMENT_MISMATCH"

        actual_preflight = await call(
            session,
            "edit_preflight",
            {
                **scope,
                "edit_request_id": edit_request_id,
                "description": "Apply the exact host-observed edit",
            },
        )
        assert actual_preflight["ok"]
        assert handle_edit_preflight_response(
            {
                "session_id": native_session_id,
                "cwd": str(workspace.root),
                "tool_name": "mcp__daem0n__edit_preflight",
                "tool_input": {
                    **scope,
                    "edit_request_id": edit_request_id,
                    "description": "Apply the exact host-observed edit",
                },
                "tool_response": {"structuredContent": actual_preflight},
            },
            environ=environment,
        )
        retry_event = {**first_event, "tool_use_id": "tool-use-allowed-2"}
        allowed = approve_native_edit(
            retry_event, str(workspace.root), environ=environment
        )
        assert allowed.allowed
        target.write_text("after", encoding="utf-8")
        completion = {
            **retry_event,
            "hook_event_name": "PostToolUse",
            "tool_response": {"success": True},
        }
        with patch.dict(os.environ, environment, clear=False):
            assert not handle_post_edit(
                {
                    **completion,
                    "tool_input": {**retry_event["tool_input"], "new_string": "forged"},
                },
                str(workspace.root),
            )
            assert handle_post_edit(completion, str(workspace.root))
            assert not handle_post_edit(completion, str(workspace.root))

        candidates = await succeed(session, "memory_capture_list", scope)
        assert len(candidates["items"]) == 1
        candidate_id = candidates["items"][0]["candidate_id"]

        promotion_arguments = {
            "candidate_id": candidate_id,
            "record_type": "learning",
            "content": "Reviewed: service edits require deterministic preimage checks.",
            "rationale": "Reviewed native edit evidence",
            "context": {"component": "service", "reviewed": True},
            "tags": ["capture", "reviewed"],
            "idempotency_key": "process-capture-promotion-0001",
        }
        preflight = await succeed(
            session,
            "memory_preflight",
            {
                **scope,
                "target_tool": "memory_capture_promote",
                "target_arguments": promotion_arguments,
                "description": "Promote the reviewed capture candidate",
            },
        )
        promoted = await succeed(
            session,
            "memory_capture_promote",
            {
                **scope,
                **promotion_arguments,
                "preflight_token": preflight["preflight_token"],
            },
        )
        assert promoted["candidate_id"] == candidate_id
        assert promoted["event_id"].startswith("evt_")
        assert (await succeed(session, "memory_capture_list", scope))["items"] == []


@pytest.mark.parametrize("failure", ["failure_hook", "failed", "skipped"])
async def test_observed_unsuccessful_edit_gets_fresh_approval_from_nested_cwd(
    initialized_workspace, socket_tmp_path, failure
):
    workspace = initialized_workspace
    installation = provision_local_bridge_installation(
        workspace.root, config_root=socket_tmp_path / "retry-host"
    )
    environment = {**installation.environment(), "DAEM0NMCP_AUTOMATION_MODE": "enforce"}
    scope = {"workspace_id": workspace.workspace_id}
    nested = workspace.root / "src"
    nested.mkdir()
    target = nested / "retry.py"
    target.write_text("before")
    event = {
        "session_id": "nested-native-session",
        "cwd": str(nested),
        "tool_use_id": "first-attempt",
        "tool_name": "Edit",
        "tool_input": {
            "file_path": str(target),
            "old_string": "before",
            "new_string": "after",
        },
    }
    config = EditHostConfig.from_environment(environment)

    async with process_client(
        workspace.root, "stdio", environment_overrides=environment
    ) as session:
        await succeed(session, "session_brief", scope)

        async def stage(decision):
            arguments = decision.remedy["arguments"]
            actual = await call(session, "edit_preflight", arguments)
            assert actual["ok"]
            assert handle_edit_preflight_response(
                {
                    **event,
                    "tool_name": "mcp__daem0n__edit_preflight",
                    "tool_input": arguments,
                    "tool_response": {"structuredContent": actual},
                },
                environ=environment,
            )
            return actual

        first = approve_native_edit(event, str(workspace.root), environ=environment)
        assert not first.allowed and first.remedy is not None
        old_request = first.remedy["arguments"]["edit_request_id"]
        await stage(first)
        allowed_event = {**event, "tool_use_id": "unsuccessful-call"}
        assert approve_native_edit(
            allowed_event, str(workspace.root), environ=environment
        ).allowed
        failed_event = {
            **allowed_event,
            "hook_event_name": "PostToolUseFailure"
            if failure == "failure_hook"
            else "PostToolUse",
            "execution_status": "failed" if failure == "failure_hook" else failure,
            "tool_response": {"success": False},
        }
        with patch.dict(os.environ, environment, clear=False):
            # A different call cannot settle the in-flight edit.
            assert not handle_post_edit(
                {**failed_event, "tool_use_id": "foreign-call"}, str(nested)
            )
            assert (
                EditHostStateStore(config).get_consumed(
                    workspace_id=workspace.workspace_id,
                    native_session_id=event["session_id"],
                    credential_id=config.identity.credential_id,
                    native_request_id=allowed_event["tool_use_id"],
                )
                is not None
            )
            assert not handle_post_edit(failed_event, str(nested))
            assert not handle_post_edit(failed_event, str(nested))
        assert target.read_text() == "before"
        assert (await succeed(session, "memory_capture_list", scope))["items"] == []
        fresh_event = {**event, "tool_use_id": "second-attempt"}
        fresh = approve_native_edit(
            fresh_event, str(workspace.root), environ=environment
        )
        assert not fresh.allowed and fresh.remedy is not None
        assert fresh.remedy["arguments"]["edit_request_id"] != old_request
        await stage(fresh)
        retry = {**event, "tool_use_id": "successful-retry"}
        assert approve_native_edit(
            retry, str(workspace.root), environ=environment
        ).allowed
        # Repeated failure for the previous call cannot clear the new consumed call.
        with patch.dict(os.environ, environment, clear=False):
            assert not handle_post_edit(failed_event, str(nested))
        target.write_text("after")
        with patch.dict(os.environ, environment, clear=False):
            assert handle_post_edit(
                {
                    **retry,
                    "hook_event_name": "PostToolUse",
                    "tool_response": {"success": True},
                },
                str(nested),
            )
        assert len((await succeed(session, "memory_capture_list", scope))["items"]) == 1
