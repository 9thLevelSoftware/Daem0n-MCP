from __future__ import annotations

from types import SimpleNamespace

from daem0nmcp.claude_hooks import post_edit_preflight
from daem0nmcp.edit_bridge_transport import BridgeResponse


def test_rejects_assistant_visible_text_without_loading_credentials(monkeypatch):
    def unexpected(_environment):
        raise AssertionError("untrusted text must be rejected before host access")

    monkeypatch.setattr(
        post_edit_preflight.EditHostConfig,
        "from_environment",
        staticmethod(unexpected),
    )
    assert not post_edit_preflight.handle_edit_preflight_response(
        {
            "session_id": "native-1",
            "cwd": "/trusted/project",
            "tool_name": "daem0nmcp_edit_preflight",
            "tool_input": {
                "workspace_id": "ws_" + "a" * 24,
                "edit_request_id": "edt_" + "b" * 64,
            },
            "tool_response": {
                "output": '{"structuredContent":{"ok":true}}',
            },
        },
        environ={},
    )


def test_stages_opencode_raw_structured_mcp_response(monkeypatch):
    calls = []
    actual = {
        "api_version": "7",
        "ok": True,
        "data": {
            "edit_request_id": "edt_" + "b" * 64,
            "edit_receipt": "receipt",
            "expires_at": "2030-01-01T00:00:00Z",
        },
        "error": None,
        "meta": {"workspace_id": "ws_" + "a" * 24},
    }
    pending = SimpleNamespace(host_session_id="hst_" + "c" * 64)

    class FakeClient:
        def call(self, path, body):
            calls.append((path, body))
            return BridgeResponse(200, {"ok": True})

    config = SimpleNamespace(
        identity=SimpleNamespace(credential_id="cred_1"),
        workspace_id=lambda _path: "ws_" + "a" * 24,
        build_client=lambda: FakeClient(),
    )
    monkeypatch.setattr(
        post_edit_preflight.EditHostConfig,
        "from_environment",
        staticmethod(lambda _environment: config),
    )
    monkeypatch.setattr(
        post_edit_preflight,
        "EditHostStateStore",
        lambda _config: SimpleNamespace(
            get_pending_by_request=lambda **_kwargs: pending
        ),
    )

    assert post_edit_preflight.handle_edit_preflight_response(
        {
            "session_id": "native-1",
            "cwd": "/trusted/project",
            "tool_name": "daem0nmcp_edit_preflight",
            "tool_input": {
                "workspace_id": "ws_" + "a" * 24,
                "edit_request_id": "edt_" + "b" * 64,
            },
            "tool_response": {
                "content": [{"type": "text", "text": "model-visible"}],
                "structuredContent": actual,
            },
        },
        environ={},
    )
    assert calls == [
        (
            "/v1/receipts/stage",
            {
                "workspace_id": "ws_" + "a" * 24,
                "host_session_id": pending.host_session_id,
                "tool_name": "edit_preflight",
                "actual_mcp_response": actual,
            },
        )
    ]


def test_stages_claude_host_mcp_metadata_without_parsing_rendered_text(monkeypatch):
    calls = []
    actual = {
        "api_version": "7",
        "ok": True,
        "data": {
            "edit_request_id": "edt_" + "b" * 64,
            "edit_receipt": "receipt",
            "expires_at": "2030-01-01T00:00:00Z",
        },
        "error": None,
        "meta": {"workspace_id": "ws_" + "a" * 24},
    }
    pending = SimpleNamespace(host_session_id="hst_" + "c" * 64)

    class FakeClient:
        def call(self, path, body):
            calls.append((path, body))
            return BridgeResponse(200, {"ok": True})

    config = SimpleNamespace(
        identity=SimpleNamespace(credential_id="cred_1"),
        workspace_id=lambda _path: "ws_" + "a" * 24,
        build_client=lambda: FakeClient(),
    )
    monkeypatch.setattr(
        post_edit_preflight.EditHostConfig,
        "from_environment",
        staticmethod(lambda _environment: config),
    )
    monkeypatch.setattr(
        post_edit_preflight,
        "EditHostStateStore",
        lambda _config: SimpleNamespace(
            get_pending_by_request=lambda **_kwargs: pending
        ),
    )

    assert post_edit_preflight.handle_edit_preflight_response(
        {
            "session_id": "native-1",
            "cwd": "/trusted/project",
            "tool_name": "mcp__daem0n__edit_preflight",
            "tool_input": {
                "workspace_id": "ws_" + "a" * 24,
                "edit_request_id": "edt_" + "b" * 64,
            },
            "tool_response": '{"api_version":"7","ok":true}',
            "mcpMeta": {"structuredContent": actual},
        },
        environ={},
    )
    assert calls[0][1]["actual_mcp_response"] == actual


def test_stages_claude_actual_tool_response_json_string(monkeypatch):
    calls = []
    actual = {
        "api_version": "7",
        "ok": True,
        "data": {
            "edit_request_id": "edt_" + "b" * 64,
            "edit_receipt": "receipt",
            "expires_at": "2030-01-01T00:00:00Z",
        },
        "error": None,
        "meta": {"workspace_id": "ws_" + "a" * 24},
    }
    pending = SimpleNamespace(host_session_id="hst_" + "c" * 64)

    class FakeClient:
        def call(self, path, body):
            calls.append((path, body))
            return BridgeResponse(200, {"ok": True})

    config = SimpleNamespace(
        identity=SimpleNamespace(credential_id="cred_1"),
        workspace_id=lambda _path: "ws_" + "a" * 24,
        build_client=lambda: FakeClient(),
    )
    monkeypatch.setattr(
        post_edit_preflight.EditHostConfig,
        "from_environment",
        staticmethod(lambda _environment: config),
    )
    monkeypatch.setattr(
        post_edit_preflight,
        "EditHostStateStore",
        lambda _config: SimpleNamespace(
            get_pending_by_request=lambda **_kwargs: pending
        ),
    )

    import json

    assert post_edit_preflight.handle_edit_preflight_response(
        {
            "session_id": "native-1",
            "cwd": "/trusted/project",
            "tool_name": "mcp__daem0n__edit_preflight",
            "tool_input": {
                "workspace_id": "ws_" + "a" * 24,
                "edit_request_id": "edt_" + "b" * 64,
            },
            "tool_response": json.dumps(actual),
        },
        environ={},
    )
    assert calls[0][1]["actual_mcp_response"] == actual


def test_rejects_duplicate_members_in_claude_actual_response():
    assert not post_edit_preflight.handle_edit_preflight_response(
        {
            "session_id": "native-1",
            "cwd": "/trusted/project",
            "tool_name": "mcp__daem0n__edit_preflight",
            "tool_input": {
                "workspace_id": "ws_" + "a" * 24,
                "edit_request_id": "edt_" + "b" * 64,
            },
            "tool_response": '{"api_version":"7","api_version":"6"}',
        },
        environ={},
    )


def test_rejects_lookalike_tool_even_with_structured_response():
    assert not post_edit_preflight.handle_edit_preflight_response(
        {
            "session_id": "native-1",
            "cwd": "/trusted/project",
            "tool_name": "prefix_edit_preflight_suffix",
            "tool_input": {},
            "tool_response": {"structuredContent": {}},
        },
        environ={},
    )


def test_rejects_workspace_id_not_bound_to_event_cwd(monkeypatch):
    config = SimpleNamespace(
        identity=SimpleNamespace(credential_id="cred_1"),
        workspace_id=lambda _path: "ws_" + "a" * 24,
    )
    monkeypatch.setattr(
        post_edit_preflight.EditHostConfig,
        "from_environment",
        staticmethod(lambda _environment: config),
    )
    monkeypatch.setattr(
        post_edit_preflight,
        "EditHostStateStore",
        lambda _config: (_ for _ in ()).throw(
            AssertionError("mismatched workspace must not load pending state")
        ),
    )

    assert not post_edit_preflight.handle_edit_preflight_response(
        {
            "session_id": "native-1",
            "cwd": "/trusted/project",
            "tool_name": "mcp__daem0n__edit_preflight",
            "tool_input": {
                "workspace_id": "ws_" + "b" * 24,
                "edit_request_id": "edt_" + "c" * 64,
            },
            "tool_response": {
                "structuredContent": {
                    "api_version": "7",
                    "ok": True,
                    "data": {},
                    "error": None,
                    "meta": {"workspace_id": "ws_" + "b" * 24},
                }
            },
        },
        environ={},
    )
