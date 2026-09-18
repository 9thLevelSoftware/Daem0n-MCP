from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from daem0nmcp.covenant import InvocationScope
from daem0nmcp.database import DatabaseManager
from daem0nmcp.edit_bridge import (
    BridgeIdentity,
    EditApprovalBroker,
    EditBridgeError,
    FilePreimage,
    NativeEditRequest,
)
from daem0nmcp.workspace import WorkspaceRegistry


class Clock:
    def __init__(self) -> None:
        self.value = datetime(2026, 9, 17, 12, 0, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        return self.value

    def advance(self, seconds: int) -> None:
        self.value += timedelta(seconds=seconds)


@pytest.fixture
async def bridge_context(tmp_path):
    storage = tmp_path / ".daem0nmcp" / "storage"
    storage.mkdir(parents=True)
    manager = DatabaseManager(str(storage))
    try:
        await manager.init_db()
    finally:
        await manager.close()
    workspace = WorkspaceRegistry([tmp_path], default_root=tmp_path).default
    clock = Clock()
    broker = EditApprovalBroker(signing_key=b"e" * 32, clock=clock)
    identity = BridgeIdentity(
        credential_id="host-credential-1",
        principal_id="oauth-sub:user-1",
        transports=frozenset({"local-ipc", "remote-https"}),
    )
    scope = InvocationScope(
        identity.principal_id,
        "mcp-session:session-1",
        str(workspace.root),
    )
    return workspace, clock, broker, identity, scope


def edit(*, digest: str = "a" * 64, content: str = "updated") -> NativeEditRequest:
    return NativeEditRequest(
        tool_name="Edit",
        arguments={"file_path": "src/app.py", "new_string": content},
        preimages=(
            FilePreimage(
                relative_file_path="src/app.py",
                state="file",
                sha256=digest,
                byte_count=12,
            ),
        ),
    )


async def approved_receipt(context):
    workspace, _, broker, identity, scope = context
    host = await broker.begin_host_session(workspace, identity, "local-ipc")
    pending = await broker.create_pending_edit(
        workspace, identity, host.host_session_id, edit()
    )
    receipt = await broker.issue_receipt(
        workspace,
        scope,
        edit_request_id=pending.edit_request_id,
        description="Apply the reviewed source change",
    )
    return host, pending, receipt


@pytest.mark.asyncio
async def test_receipt_is_bound_staged_consumed_once_and_expires_in_120_seconds(
    bridge_context,
):
    workspace, clock, broker, identity, _ = bridge_context
    host, pending, receipt = await approved_receipt(bridge_context)

    assert receipt.expires_at == clock.value + timedelta(seconds=120)
    await broker.stage_actual_mcp_response(
        workspace,
        identity,
        host_session_id=host.host_session_id,
        edit_request_id=pending.edit_request_id,
        receipt=receipt.receipt,
    )
    approval = await broker.consume_retry(
        workspace,
        identity,
        host_session_id=host.host_session_id,
        edit_request_id=pending.edit_request_id,
        edit=edit(),
    )
    assert approval.allowed

    with pytest.raises(EditBridgeError, match="TOKEN_REPLAYED") as replayed:
        await broker.consume_retry(
            workspace,
            identity,
            host_session_id=host.host_session_id,
            edit_request_id=pending.edit_request_id,
            edit=edit(),
        )
    assert replayed.value.code == "TOKEN_REPLAYED"


@pytest.mark.asyncio
async def test_argument_or_preimage_change_denies_without_consuming(bridge_context):
    workspace, _, broker, identity, _ = bridge_context
    host, pending, receipt = await approved_receipt(bridge_context)
    await broker.stage_actual_mcp_response(
        workspace,
        identity,
        host_session_id=host.host_session_id,
        edit_request_id=pending.edit_request_id,
        receipt=receipt.receipt,
    )

    with pytest.raises(EditBridgeError) as changed:
        await broker.consume_retry(
            workspace,
            identity,
            host_session_id=host.host_session_id,
            edit_request_id=pending.edit_request_id,
            edit=edit(digest="b" * 64),
        )
    assert changed.value.code == "TOKEN_ARGUMENT_MISMATCH"

    approval = await broker.consume_retry(
        workspace,
        identity,
        host_session_id=host.host_session_id,
        edit_request_id=pending.edit_request_id,
        edit=edit(),
    )
    assert approval.allowed


@pytest.mark.asyncio
async def test_principal_and_mcp_session_pairing_are_exact(bridge_context):
    workspace, _, broker, identity, scope = bridge_context
    host = await broker.begin_host_session(workspace, identity, "remote-https")
    first = await broker.create_pending_edit(
        workspace, identity, host.host_session_id, edit(content="first")
    )
    await broker.issue_receipt(
        workspace,
        scope,
        edit_request_id=first.edit_request_id,
        description="First exact edit",
    )

    second = await broker.create_pending_edit(
        workspace, identity, host.host_session_id, edit(content="second")
    )
    changed_session = InvocationScope(
        identity.principal_id,
        "mcp-session:session-2",
        str(workspace.root),
    )
    with pytest.raises(EditBridgeError) as mismatch:
        await broker.issue_receipt(
            workspace,
            changed_session,
            edit_request_id=second.edit_request_id,
            description="Second exact edit",
        )
    assert mismatch.value.code == "TOKEN_SCOPE_MISMATCH"

    wrong_principal = InvocationScope(
        "oauth-sub:other-user",
        scope.transport_session_id,
        str(workspace.root),
    )
    with pytest.raises(EditBridgeError) as principal:
        await broker.issue_receipt(
            workspace,
            wrong_principal,
            edit_request_id=second.edit_request_id,
            description="Second exact edit",
        )
    assert principal.value.code == "TOKEN_SCOPE_MISMATCH"


def test_bridge_identity_preserves_principal_whitespace_and_rejects_controls():
    exact = BridgeIdentity(
        credential_id="host-credential-exact",
        principal_id="oauth-sub:alice ",
        transports=frozenset({"remote-https"}),
    )

    assert exact.principal_id == "oauth-sub:alice "
    assert exact.principal_id != "oauth-sub:alice"
    with pytest.raises(ValueError, match="incomplete"):
        BridgeIdentity(
            credential_id="host-credential-control",
            principal_id="oauth-sub:alice\n",
            transports=frozenset({"remote-https"}),
        )


@pytest.mark.asyncio
async def test_expired_receipt_denies_and_does_not_mutate(bridge_context):
    workspace, clock, broker, identity, _ = bridge_context
    host, pending, receipt = await approved_receipt(bridge_context)
    clock.advance(121)

    with pytest.raises(EditBridgeError) as expired:
        await broker.stage_actual_mcp_response(
            workspace,
            identity,
            host_session_id=host.host_session_id,
            edit_request_id=pending.edit_request_id,
            receipt=receipt.receipt,
        )
    assert expired.value.code == "TOKEN_EXPIRED"


@pytest.mark.asyncio
async def test_pending_rows_store_hash_commitments_not_raw_edit_arguments(
    bridge_context,
):
    workspace, _, broker, identity, _ = bridge_context
    host = await broker.begin_host_session(workspace, identity, "local-ipc")
    pending = await broker.create_pending_edit(
        workspace, identity, host.host_session_id, edit(content="private replacement")
    )

    database = workspace.root / ".daem0nmcp" / "storage" / "daem0nmcp.db"
    with sqlite3.connect(database) as connection:
        row = connection.execute(
            "SELECT arguments_hash,relative_paths_json,preimages_json FROM "
            "native_edit_pending WHERE pending_edit_id=?",
            (pending.edit_request_id,),
        ).fetchone()
    assert row is not None
    assert len(row[0]) == 64
    assert "private replacement" not in "".join(row)
    assert "src/app.py" in row[1]

    with (
        sqlite3.connect(database) as connection,
        pytest.raises(sqlite3.IntegrityError, match="immutable"),
    ):
        connection.execute(
            "UPDATE native_edit_pending SET arguments_hash=? WHERE pending_edit_id=?",
            ("f" * 64, pending.edit_request_id),
        )
