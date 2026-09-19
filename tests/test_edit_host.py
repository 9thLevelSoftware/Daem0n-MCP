from __future__ import annotations

import json
import os
import sqlite3
import ssl
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from daem0nmcp.claude_hooks.pre_edit import handle_pre_edit
from daem0nmcp.edit_bridge_transport import (
    RemoteBridgeHTTPSClient,
    local_bridge_address,
    provision_bridge_credential,
)
from daem0nmcp.edit_host import (
    CreatedHostSession,
    EditHostConfig,
    EditHostStateStore,
    PendingEditBinding,
    native_edit_capture_body,
    provision_client_bridge_installation,
    provision_local_bridge_installation,
    provision_remote_bridge_installation,
)
from daem0nmcp.protected_files import verify_owner_only_file
from daem0nmcp.workspace import WorkspaceRegistry


def _configuration(tmp_path):
    credential = tmp_path / "host" / "credential.json"
    _, identity = provision_bridge_credential(
        credential,
        principal_id="principal-one",
        transports=frozenset({"local-ipc"}),
    )
    environment = {
        "DAEM0NMCP_EDIT_BRIDGE_CREDENTIAL_FILE": str(credential),
        "DAEM0NMCP_EDIT_BRIDGE_RUNTIME_DIR": str(tmp_path / "run"),
        "DAEM0NMCP_EDIT_HOST_STATE_FILE": str(tmp_path / "host" / "state.sqlite3"),
    }
    return EditHostConfig.from_environment(environment), identity, environment


def test_local_installation_reuses_host_only_authority_credential(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    host_root = tmp_path / "user-config"

    first = provision_local_bridge_installation(project, config_root=host_root)
    second = provision_local_bridge_installation(project, config_root=host_root)

    assert first.created
    assert not second.created
    assert second.credential_path == first.credential_path
    assert not first.credential_path.is_relative_to(project)
    config = EditHostConfig.from_environment(first.environment())
    assert config.identity.principal_id == first.principal_id
    assert config.runtime_directory == first.runtime_directory
    # The default root is ~/.daem0nmcp/edit-bridges; with this suffix the
    # socket fits macOS's 104-byte AF_UNIX limit for usernames up to 17 chars.
    address = local_bridge_address(
        first.runtime_directory, config.identity.credential_id
    )
    if sys.platform != "win32":
        suffix = Path(address).relative_to(host_root.resolve())
        assert len(str(suffix)) <= 52, suffix
    assert (
        config.workspace_id(project)
        == WorkspaceRegistry(default_root=project).default.workspace_id
    )


def test_remote_binding_maps_exact_local_root_to_opaque_server_workspace(
    tmp_path, monkeypatch
):
    project = tmp_path / "desktop-checkout"
    other = tmp_path / "unexpected-checkout"
    project.mkdir()
    other.mkdir()
    host = tmp_path / "host"
    credential = host / "credential.json"
    secret, _identity = provision_bridge_credential(
        credential,
        principal_id="remote-principal",
        transports=frozenset({"remote-https"}),
    )
    ca_file = tmp_path / "remote-ca.pem"
    ca_file.write_text("test CA fixture", encoding="utf-8")
    monkeypatch.setattr(
        ssl,
        "create_default_context",
        lambda **_kwargs: ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT),
    )
    remote_workspace_id = "ws_" + "9" * 24

    installation = provision_remote_bridge_installation(
        project,
        credential_path=credential,
        remote_base_url="https://server.example:7443",
        ca_file=ca_file,
        workspace_id=remote_workspace_id,
        origin="https://desktop.example",
    )
    replay = provision_remote_bridge_installation(
        project,
        credential_path=credential,
        remote_base_url="https://server.example:7443",
        ca_file=ca_file,
        workspace_id=remote_workspace_id,
        origin="https://desktop.example",
    )

    assert installation.created
    assert not replay.created
    assert verify_owner_only_file(installation.binding_path, max_bytes=4096)
    assert secret not in installation.binding_path.read_text(encoding="utf-8")
    config = EditHostConfig.from_environment(installation.environment())
    assert config.workspace_id(project) == remote_workspace_id
    other_credential = host / "other-credential.json"
    provision_bridge_credential(
        other_credential,
        principal_id="other-remote-principal",
        transports=frozenset({"remote-https"}),
    )
    changed_credential_environment = {
        **installation.environment(),
        "DAEM0NMCP_EDIT_BRIDGE_CREDENTIAL_FILE": str(other_credential),
    }
    with pytest.raises(ValueError, match="credential does not match"):
        EditHostConfig.from_environment(changed_credential_environment)
    with pytest.raises(ValueError, match="does not match"):
        config.workspace_id(other)
    linked = tmp_path / "linked-checkout"
    try:
        linked.symlink_to(project, target_is_directory=True)
    except OSError:
        pass
    else:
        with pytest.raises(PermissionError, match="link or reparse"):
            config.workspace_id(linked)
    with pytest.raises(ValueError, match="conflicts"):
        provision_remote_bridge_installation(
            project,
            credential_path=credential,
            remote_base_url="https://server.example:7443",
            ca_file=ca_file,
            workspace_id="ws_" + "8" * 24,
        )
    with pytest.raises(ValueError, match="path is not canonical"):
        provision_remote_bridge_installation(
            project,
            credential_path=credential,
            remote_base_url="https://server.example:7443",
            ca_file=ca_file,
            workspace_id=remote_workspace_id,
            binding_path=host / "alternate-binding.json",
        )


def test_remote_mode_requires_complete_protected_workspace_binding(
    tmp_path,
):
    credential = tmp_path / "host" / "credential.json"
    provision_bridge_credential(
        credential,
        principal_id="remote-principal",
        transports=frozenset({"remote-https"}),
    )
    ca_file = tmp_path / "ca.pem"
    ca_file.write_text("unused", encoding="utf-8")
    environment = {
        "DAEM0NMCP_EDIT_BRIDGE_CREDENTIAL_FILE": str(credential),
        "DAEM0NMCP_EDIT_BRIDGE_MODE": "remote-https",
        "DAEM0NMCP_EDIT_BRIDGE_REMOTE_URL": "https://server.example",
        "DAEM0NMCP_EDIT_BRIDGE_CA_FILE": str(ca_file),
    }
    with pytest.raises(ValueError, match="incomplete"):
        EditHostConfig.from_environment(environment)
    with pytest.raises(ValueError, match="incomplete"):
        provision_client_bridge_installation(
            tmp_path,
            remote_workspace_id="ws_" + "1" * 24,
        )


def test_remote_binding_rejects_same_path_checkout_replacement_without_network(
    tmp_path, monkeypatch
):
    project = tmp_path / "desktop-checkout"
    project.mkdir()
    target = project / "target.txt"
    target.write_text("before", encoding="utf-8")
    credential = tmp_path / "host" / "credential.json"
    provision_bridge_credential(
        credential,
        principal_id="remote-principal",
        transports=frozenset({"remote-https"}),
    )
    ca_file = tmp_path / "ca.pem"
    ca_file.write_text("test CA", encoding="utf-8")
    monkeypatch.setattr(
        ssl,
        "create_default_context",
        lambda **_kwargs: ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT),
    )
    installation = provision_remote_bridge_installation(
        project,
        credential_path=credential,
        remote_base_url="https://server.example",
        ca_file=ca_file,
        workspace_id="ws_" + "4" * 24,
    )
    original_config = EditHostConfig.from_environment(installation.environment())
    calls = 0

    def unexpected_call(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        raise AssertionError("replacement root must fail before network")

    monkeypatch.setattr(RemoteBridgeHTTPSClient, "call", unexpected_call)
    project.rename(tmp_path / "original-checkout")
    project.mkdir()
    target = project / "target.txt"
    target.write_text("replacement", encoding="utf-8")

    with pytest.raises(ValueError, match="does not match"):
        original_config.workspace_id(project)
    with pytest.raises(ValueError, match="identity changed"):
        EditHostConfig.from_environment(installation.environment())
    with monkeypatch.context() as context:
        for name, value in installation.environment().items():
            context.setenv(name, value)
        result = handle_pre_edit(
            {
                "session_id": "replacement-session",
                "tool_use_id": "replacement-request",
                "tool_name": "Edit",
                "tool_input": {
                    "file_path": str(target),
                    "old_string": "replacement",
                    "new_string": "changed",
                },
            },
            str(project),
        )
    assert not result.allowed
    assert result.message == "EDIT_BRIDGE_UNAVAILABLE"
    assert calls == 0
    project.rename(tmp_path / "replacement-checkout")
    (tmp_path / "original-checkout").rename(project)
    assert original_config.workspace_id(project) == installation.workspace_id


@pytest.mark.parametrize(
    ("changed_name", "changed_value"),
    [
        (
            "DAEM0NMCP_EDIT_BRIDGE_REMOTE_URL",
            "https://attacker.example",
        ),
        (
            "DAEM0NMCP_EDIT_BRIDGE_ORIGIN",
            "https://attacker.example",
        ),
        ("DAEM0NMCP_EDIT_BRIDGE_CA_FILE", "{attacker_ca}"),
    ],
)
@pytest.mark.parametrize("alternate_ca_contents", ["attacker CA", "trusted CA"])
def test_remote_recipient_substitution_fails_before_network(
    tmp_path, monkeypatch, changed_name, changed_value, alternate_ca_contents
):
    project = tmp_path / "desktop-checkout"
    project.mkdir()
    target = project / "target.txt"
    target.write_text("before", encoding="utf-8")
    credential = tmp_path / "host" / "credential.json"
    provision_bridge_credential(
        credential,
        principal_id="remote-principal",
        transports=frozenset({"remote-https"}),
    )
    ca_file = tmp_path / "ca.pem"
    ca_file.write_text("trusted CA", encoding="utf-8")
    attacker_ca = tmp_path / "attacker-ca.pem"
    attacker_ca.write_text(alternate_ca_contents, encoding="utf-8")
    monkeypatch.setattr(
        ssl,
        "create_default_context",
        lambda **_kwargs: ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT),
    )
    installation = provision_remote_bridge_installation(
        project,
        credential_path=credential,
        remote_base_url="https://server.example",
        ca_file=ca_file,
        workspace_id="ws_" + "5" * 24,
        origin="https://desktop.example",
    )
    environment = installation.environment()
    environment[changed_name] = changed_value.format(attacker_ca=attacker_ca)
    calls = 0

    def unexpected_call(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        raise AssertionError("recipient substitution must fail before network")

    monkeypatch.setattr(RemoteBridgeHTTPSClient, "call", unexpected_call)
    with pytest.raises(ValueError, match="conflicts with binding"):
        EditHostConfig.from_environment(environment)
    with monkeypatch.context() as context:
        for name, value in environment.items():
            context.setenv(name, value)
        result = handle_pre_edit(
            {
                "session_id": "substitution-session",
                "tool_use_id": "substitution-request",
                "tool_name": "Edit",
                "tool_input": {
                    "file_path": str(target),
                    "old_string": "before",
                    "new_string": "after",
                },
            },
            str(project),
        )
    assert not result.allowed
    assert result.message == "EDIT_BRIDGE_UNAVAILABLE"
    assert calls == 0

    with pytest.raises(ValueError, match="conflicts"):
        provision_remote_bridge_installation(
            project,
            credential_path=credential,
            remote_base_url=(
                "https://other.example"
                if changed_name == "DAEM0NMCP_EDIT_BRIDGE_REMOTE_URL"
                else "https://server.example"
            ),
            ca_file=(
                attacker_ca
                if changed_name == "DAEM0NMCP_EDIT_BRIDGE_CA_FILE"
                else ca_file
            ),
            workspace_id="ws_" + "5" * 24,
            origin=(
                "https://other.example"
                if changed_name == "DAEM0NMCP_EDIT_BRIDGE_ORIGIN"
                else "https://desktop.example"
            ),
        )


def test_remote_ca_change_after_config_load_blocks_client_construction(
    tmp_path, monkeypatch
):
    project = tmp_path / "desktop-checkout"
    project.mkdir()
    credential = tmp_path / "host" / "credential.json"
    provision_bridge_credential(
        credential,
        principal_id="remote-principal",
        transports=frozenset({"remote-https"}),
    )
    ca_file = tmp_path / "ca.pem"
    ca_file.write_text("trusted CA", encoding="utf-8")
    monkeypatch.setattr(
        ssl,
        "create_default_context",
        lambda **_kwargs: ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT),
    )
    installation = provision_remote_bridge_installation(
        project,
        credential_path=credential,
        remote_base_url="https://server.example",
        ca_file=ca_file,
        workspace_id="ws_" + "6" * 24,
    )
    config = EditHostConfig.from_environment(installation.environment())
    ca_file.write_text("changed CA", encoding="utf-8")

    with pytest.raises(ValueError, match="CA conflicts with binding"):
        config.build_client()


def test_host_state_persists_exact_session_and_consumed_capture_across_processes(
    tmp_path,
):
    config, identity, environment = _configuration(tmp_path)
    store = EditHostStateStore(config)
    now = datetime.now(timezone.utc)
    calls = 0

    def create():
        nonlocal calls
        calls += 1
        return CreatedHostSession("hst_" + "a" * 64, now + timedelta(hours=1))

    first = store.load_or_create_session(
        workspace_id="ws_" + "1" * 24,
        native_session_id="actual-client-session-secret",
        credential_id=identity.credential_id,
        create=create,
        now=now,
    )
    replay = EditHostStateStore(config).load_or_create_session(
        workspace_id=first.workspace_id,
        native_session_id="actual-client-session-secret",
        credential_id=identity.credential_id,
        create=create,
        now=now,
    )
    assert replay == first
    assert calls == 1

    pending = PendingEditBinding(
        workspace_id=first.workspace_id,
        host_session_id=first.host_session_id,
        edit_request_id="edt_" + "b" * 64,
        edit_hash="c" * 64,
        expires_at=now + timedelta(minutes=10),
    )
    store.save_pending(
        native_session_id="actual-client-session-secret",
        native_request_id="denied-tool-use-41",
        credential_id=identity.credential_id,
        pending=pending,
        now=now,
    )
    assert (
        store.get_pending(
            workspace_id=first.workspace_id,
            native_session_id="different-session",
            credential_id=identity.credential_id,
            edit_hash=pending.edit_hash,
            now=now,
        )
        is None
    )
    assert (
        store.mark_consumed(
            workspace_id=first.workspace_id,
            native_session_id="actual-client-session-secret",
            native_request_id="approved-tool-use-42",
            credential_id=identity.credential_id,
            edit_request_id=pending.edit_request_id,
            now=now,
        )
        == pending
    )
    assert (
        store.get_pending(
            workspace_id=first.workspace_id,
            native_session_id="actual-client-session-secret",
            credential_id=identity.credential_id,
            edit_hash=pending.edit_hash,
            now=now,
        )
        is None
    )
    with pytest.raises(ValueError, match="consumed"):
        store.save_pending(
            native_session_id="actual-client-session-secret",
            native_request_id="denied-tool-use-41",
            credential_id=identity.credential_id,
            pending=pending,
            now=now,
        )

    script = """
import json, os
from daem0nmcp.edit_host import EditHostConfig, EditHostStateStore
c = EditHostConfig.from_environment(os.environ)
p = EditHostStateStore(c).get_consumed(
    workspace_id=os.environ['TEST_WORKSPACE_ID'],
    native_session_id=os.environ['TEST_NATIVE_SESSION_ID'],
    native_request_id=os.environ['TEST_NATIVE_REQUEST_ID'],
    credential_id=c.identity.credential_id,
)
print(json.dumps({'edit_request_id': None if p is None else p.edit_request_id}))
"""
    child_environment = {
        **os.environ,
        **environment,
        "TEST_WORKSPACE_ID": first.workspace_id,
        "TEST_NATIVE_SESSION_ID": "actual-client-session-secret",
        "TEST_NATIVE_REQUEST_ID": "approved-tool-use-42",
    }
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=tmp_path,
        env=child_environment,
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert json.loads(result.stdout) == {"edit_request_id": pending.edit_request_id}
    assert store.mark_captured(
        workspace_id=first.workspace_id,
        native_session_id="actual-client-session-secret",
        credential_id=identity.credential_id,
        edit_request_id=pending.edit_request_id,
    )
    assert (
        store.get_consumed(
            workspace_id=first.workspace_id,
            native_session_id="actual-client-session-secret",
            native_request_id="approved-tool-use-42",
            credential_id=identity.credential_id,
        )
        is None
    )

    assert b"actual-client-session-secret" not in config.state_file.read_bytes()
    with sqlite3.connect(config.state_file) as connection:
        row = connection.execute(
            "SELECT native_session_hash,credential_id_hash FROM host_sessions"
        ).fetchone()
    assert row is not None and all(len(value) == 64 for value in row)


def test_native_capture_mapping_is_bounded_and_contains_no_raw_result() -> None:
    body = native_edit_capture_body(
        workspace_id="ws_" + "1" * 24,
        host_session_id="hst_" + "a" * 64,
        edit_request_id="edt_" + "b" * 64,
        tool_name="Edit",
        relative_paths=["src/z.py", "src/a.py"],
        result="succeeded",
    )

    assert body["record"]["content"] == (
        "Native Edit succeeded for 2 workspace-relative path(s)."
    )
    assert body["provenance"]["relative_file_paths"] == ["src/a.py", "src/z.py"]
    assert set(body["provenance"]) == {
        "source_operation",
        "edit_request_id",
        "relative_file_paths",
        "native_result",
    }
    assert body["idempotency_key"].startswith("native-")
