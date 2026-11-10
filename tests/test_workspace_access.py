"""Independent workspace access survives briefing and is revoked live."""

from __future__ import annotations

import json

import pytest

from daem0nmcp.covenant import InvocationScope
from daem0nmcp.protected_files import write_new_owner_only_file
from daem0nmcp.workspace_access import WorkspaceAccessPolicy


@pytest.fixture
def access_policy(tmp_path):
    path = tmp_path / "protected" / "access.json"
    root = tmp_path / "workspace"
    root.mkdir()
    identifier = "ws_" + "a" * 24
    policy = WorkspaceAccessPolicy(
        workspaces={str(root): identifier}, local_principal="local", path=path
    )
    return policy, path, root, identifier


def test_local_owner_remote_default_denial_and_live_revocation(access_policy):
    policy, path, root, identifier = access_policy
    local = InvocationScope("local", "session", str(root))
    remote = InvocationScope("oauth-sub:alice", "session", str(root))
    assert policy(local)
    assert not policy(remote)
    write_new_owner_only_file(
        path,
        json.dumps(
            {"schema_version": 1, "grants": {remote.principal_id: [identifier]}}
        ).encode(),
    )
    assert policy(remote)
    assert not policy(InvocationScope("oauth-sub:bob", "session", str(root)))
    assert not policy(InvocationScope("local", "session", str(root.parent)))
    path.write_text('{"schema_version":1,"grants":{}}', encoding="utf-8")
    assert not policy(remote)
    assert policy(local)


@pytest.mark.parametrize(
    "body",
    [
        b'{"schema_version":1,"schema_version":1,"grants":{}}',
        b'{"schema_version":true,"grants":{}}',
        b'{"schema_version":1,"grants":{"oauth-sub:alice":["*"]}}',
        b'{"schema_version":1,"grants":{"oauth-sub:alice":true}}',
        b'{"schema_version":1,"grants":{},"fallback":"allow"}',
        b" " * 65_537,
    ],
    ids=[
        "duplicate-key",
        "boolean-version",
        "wildcard",
        "invalid-list",
        "extra-field",
        "oversized",
    ],
)
def test_malformed_policy_denies_without_details(access_policy, body):
    policy, path, root, _ = access_policy
    write_new_owner_only_file(path, body)
    assert not policy(InvocationScope("oauth-sub:alice", "session", str(root)))


def test_production_gate_cannot_turn_briefing_into_access(access_policy):
    from daem0nmcp.api.v7.production import build_production_surface
    from daem0nmcp.config import Settings
    from daem0nmcp.covenant import WorkspaceAccessDenied

    _, path, root, _ = access_policy
    surface = build_production_surface(
        "stdio",
        settings=Settings(project_root=str(root), workspace_roots=[]),
        environ={"DAEM0NMCP_WORKSPACE_ACCESS_FILE": str(path)},
    )
    scope = InvocationScope("oauth-sub:alice", "session", str(root))
    with pytest.raises(WorkspaceAccessDenied):
        surface.gate.record_briefing(scope)
    # Even an old briefing cannot bypass a currently missing grant.
    surface.gate.state_store.mark_briefed(scope)
    denied = surface.gate.authorize(
        "memory_recall",
        {
            "workspace_id": surface.workspace_resolver.default.workspace_id,
            "query": "test",
        },
        scope,
    )
    assert denied["violation"] == "UNAUTHORIZED_WORKSPACE"
