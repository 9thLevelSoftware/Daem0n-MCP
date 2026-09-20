from __future__ import annotations

import concurrent.futures
import http.client
import json
import multiprocessing.connection
import os
import socket
import ssl
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from ipaddress import ip_address
from unittest.mock import patch

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from daem0nmcp.api.v7.runtime_services import WorkspaceStorageResolver
from daem0nmcp.capture_candidates import CaptureCandidateStore
from daem0nmcp.claude_hooks.native_edit import native_edit_request
from daem0nmcp.claude_hooks.post_edit import handle_post_edit
from daem0nmcp.claude_hooks.post_edit_preflight import (
    handle_edit_preflight_response,
)
from daem0nmcp.covenant import InvocationScope
from daem0nmcp.database import DatabaseManager
from daem0nmcp.edit_bridge import (
    BridgeCredentialRegistry,
    BridgeIdentity,
    EditApprovalBroker,
)
from daem0nmcp.edit_bridge_transport import (
    BridgeConnectionError,
    EditBridgeProtocol,
    LocalBridgeClient,
    LocalBridgeServer,
    RemoteBridgeHTTPSClient,
    RemoteBridgeHTTPSServer,
    local_bridge_address,
    provision_bridge_credential,
)
from daem0nmcp.edit_host import (
    EditHostConfig,
    EditHostStateStore,
    provision_remote_bridge_installation,
)
from daem0nmcp.workspace import WorkspaceRegistry
from tests.native_edit_host import drive_native_edit

SECRET = "bridge-secret-" + "x" * 48
AUTHKEY = b"local-auth-key-" + b"y" * 32


@pytest.fixture
async def transport_context(tmp_path):
    storage = tmp_path / ".daem0nmcp" / "storage"
    storage.mkdir(parents=True)
    manager = DatabaseManager(str(storage))
    try:
        await manager.init_db()
    finally:
        await manager.close()
    registry = WorkspaceRegistry([tmp_path], default_root=tmp_path)
    identity = BridgeIdentity(
        credential_id="credential-one",
        principal_id="principal-one",
        transports=frozenset({"local-ipc", "remote-https"}),
    )
    resolver = WorkspaceStorageResolver()
    broker = EditApprovalBroker(storage_resolver=resolver, signing_key=b"s" * 32)
    protocol = EditBridgeProtocol(
        broker=broker,
        candidates=CaptureCandidateStore(storage_resolver=resolver),
        credentials=BridgeCredentialRegistry({SECRET: identity}),
        workspace_resolver=registry.resolve,
        workspace_authorizer=lambda workspace, principal: (
            workspace == registry.default and principal == identity
        ),
    )
    return registry.default, identity, broker, protocol


def _edit(content: str = "after") -> dict[str, object]:
    return {
        "tool_name": "Edit",
        "arguments": {"file_path": "src/app.py", "new_string": content},
        "preimages": [
            {
                "relative_file_path": "src/app.py",
                "state": "file",
                "sha256": "a" * 64,
                "byte_count": 12,
            }
        ],
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("path", "body"),
    [
        ("/v1/sessions", {}),
        ("/v1/edits", {"host_session_id": "hst_" + "a" * 64, "edit": _edit()}),
        (
            "/v1/receipts/stage",
            {
                "host_session_id": "hst_" + "a" * 64,
                "tool_name": "edit_preflight",
                "actual_mcp_response": {},
            },
        ),
        (
            "/v1/receipts/consume",
            {
                "host_session_id": "hst_" + "a" * 64,
                "edit_request_id": "edt_" + "b" * 64,
                "edit": _edit(),
            },
        ),
        (
            "/v1/captures",
            {
                "host_session_id": "hst_" + "a" * 64,
                "source_kind": "native_edit",
                "record": {},
                "provenance": {},
                "idempotency_key": "capture-key",
            },
        ),
    ],
)
async def test_every_scoped_route_rechecks_workspace_authority(
    transport_context, path, body
):
    workspace, _identity, _broker, protocol = transport_context
    protocol._workspace_authorizer = lambda _workspace, _principal: False

    response = await protocol.handle(
        path=path,
        authorization=f"Bearer {SECRET}",
        body={"workspace_id": workspace.workspace_id, **body},
        transport="local-ipc",
        local_peer_verified=True,
    )

    assert response.status == 403
    assert response.body["error"]["code"] == "UNAUTHORIZED_WORKSPACE"


@pytest.mark.asyncio
async def test_local_ipc_requires_transport_auth_and_consumes_exact_retry(
    socket_tmp_path, transport_context
):
    workspace, identity, broker, protocol = transport_context
    address = local_bridge_address(socket_tmp_path / "run", identity.credential_id)
    server = LocalBridgeServer(protocol=protocol, address=address, authkey=AUTHKEY)
    server.start()
    client = LocalBridgeClient(address=address, authkey=AUTHKEY, bearer=SECRET)
    try:
        session = client.call("/v1/sessions", {"workspace_id": workspace.workspace_id})
        assert session.status == 201
        host_id = session.body["data"]["host_session_id"]
        pending = client.call(
            "/v1/edits",
            {
                "workspace_id": workspace.workspace_id,
                "host_session_id": host_id,
                "edit": _edit(),
            },
        )
        edit_id = pending.body["data"]["edit_request_id"]
        assert pending.body["data"]["remedy"]["tool"] == "edit_preflight"
        receipt = await broker.issue_receipt(
            workspace,
            InvocationScope(
                identity.principal_id, "mcp-session-one", str(workspace.root)
            ),
            edit_request_id=edit_id,
            description="Apply this exact native edit",
        )
        actual_response = {
            "api_version": "7",
            "ok": True,
            "data": {
                "edit_request_id": edit_id,
                "edit_receipt": receipt.receipt,
                "expires_at": receipt.expires_at.isoformat(),
            },
            "error": None,
            "meta": {"workspace_id": workspace.workspace_id},
        }
        mismatched = {
            **actual_response,
            "data": {
                **actual_response["data"],
                "edit_request_id": "edt_" + "c" * 64,
            },
        }
        denied_stage = client.call(
            "/v1/receipts/stage",
            {
                "workspace_id": workspace.workspace_id,
                "host_session_id": host_id,
                "tool_name": "edit_preflight",
                "actual_mcp_response": mismatched,
            },
        )
        assert denied_stage.body["error"]["code"] == "TOKEN_ARGUMENT_MISMATCH"
        staged = client.call(
            "/v1/receipts/stage",
            {
                "workspace_id": workspace.workspace_id,
                "host_session_id": host_id,
                "tool_name": "edit_preflight",
                "actual_mcp_response": actual_response,
            },
        )
        assert staged.status == 200

        changed = client.call(
            "/v1/receipts/consume",
            {
                "workspace_id": workspace.workspace_id,
                "host_session_id": host_id,
                "edit_request_id": edit_id,
                "edit": _edit("changed"),
            },
        )
        assert changed.status == 409
        assert changed.body["error"]["code"] == "TOKEN_ARGUMENT_MISMATCH"
        allowed = client.call(
            "/v1/receipts/consume",
            {
                "workspace_id": workspace.workspace_id,
                "host_session_id": host_id,
                "edit_request_id": edit_id,
                "edit": _edit(),
            },
        )
        assert allowed.body["data"]["allowed"] is True
        replay = client.call(
            "/v1/receipts/consume",
            {
                "workspace_id": workspace.workspace_id,
                "host_session_id": host_id,
                "edit_request_id": edit_id,
                "edit": _edit(),
            },
        )
        assert replay.body["error"]["code"] == "TOKEN_REPLAYED"
    finally:
        server.close()
    with pytest.raises(BridgeConnectionError) as unavailable:
        client.call("/v1/sessions", {"workspace_id": workspace.workspace_id})
    assert unavailable.value.code == "EDIT_BRIDGE_UNAVAILABLE"


def _certificate(tmp_path):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
    now = datetime.now(timezone.utc)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=1))
        .not_valid_after(now + timedelta(days=1))
        .add_extension(
            x509.SubjectAlternativeName(
                [x509.DNSName("localhost"), x509.IPAddress(ip_address("127.0.0.1"))]
            ),
            critical=False,
        )
        .sign(key, hashes.SHA256())
    )
    cert_path = tmp_path / "cert.pem"
    key_path = tmp_path / "key.pem"
    cert_path.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    return cert_path, key_path


def _local_raw_client(address):
    family = "AF_PIPE" if sys.platform == "win32" else "AF_UNIX"
    return multiprocessing.connection.Client(address, family=family)


@pytest.mark.asyncio
async def test_local_ipc_bounds_idle_bad_auth_duplicate_json_and_shutdown(
    socket_tmp_path, transport_context
):
    workspace, identity, _, protocol = transport_context
    address = local_bridge_address(
        socket_tmp_path / "adversarial", identity.credential_id
    )
    server = LocalBridgeServer(protocol=protocol, address=address, authkey=AUTHKEY)
    server.start()
    client = LocalBridgeClient(
        address=address,
        authkey=AUTHKEY,
        bearer=SECRET,
        timeout_seconds=5,
    )
    idle = [_local_raw_client(address) for _ in range(4)]
    try:
        started = time.monotonic()
        response = client.call("/v1/sessions", {"workspace_id": workspace.workspace_id})
        assert response.status == 201
        assert time.monotonic() - started < 4

        family = "AF_PIPE" if sys.platform == "win32" else "AF_UNIX"
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
            bad = executor.submit(
                multiprocessing.connection.Client,
                address,
                family,
                b"wrong-auth-key-" + b"z" * 32,
            )
            with pytest.raises((EOFError, multiprocessing.AuthenticationError)):
                bad.result(timeout=4)

        duplicate = _local_raw_client(address)
        try:
            duplicate.send_bytes(
                b'{"path":"/v1/sessions","path":"/v1/edits",'
                b'"authorization":"Bearer x","body":{},"proof":"x"}'
            )
            assert duplicate.poll(3)
            payload = json.loads(duplicate.recv_bytes())
            assert payload["status"] == 400
        finally:
            duplicate.close()

        assert (
            client.call("/v1/sessions", {"workspace_id": workspace.workspace_id}).status
            == 201
        )
    finally:
        for connection in idle:
            connection.close()
        shutdown_started = time.monotonic()
        server.close()
        assert time.monotonic() - shutdown_started < 4


def test_local_client_deadline_interrupts_a_stalled_server(socket_tmp_path) -> None:
    address = local_bridge_address(socket_tmp_path / "stalled", "credential-stalled")
    family = "AF_PIPE" if sys.platform == "win32" else "AF_UNIX"
    listener = multiprocessing.connection.Listener(address, family=family)
    release = threading.Event()

    def hold_connection() -> None:
        connection = listener.accept()
        try:
            release.wait(5)
        finally:
            connection.close()

    thread = threading.Thread(target=hold_connection, daemon=True)
    thread.start()
    client = LocalBridgeClient(
        address=address,
        authkey=AUTHKEY,
        bearer=SECRET,
        timeout_seconds=0.25,
    )
    started = time.monotonic()
    try:
        with pytest.raises(BridgeConnectionError):
            client.call("/v1/sessions", {"workspace_id": "ws_" + "1" * 24})
        assert time.monotonic() - started < 1.5
    finally:
        release.set()
        listener.close()
        thread.join(timeout=2)


@pytest.mark.asyncio
async def test_remote_https_requires_tls_and_authenticated_principal(
    tmp_path, transport_context
):
    workspace, _, _, protocol = transport_context
    cert_path, key_path = _certificate(tmp_path)
    server_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server_context.minimum_version = ssl.TLSVersion.TLSv1_2
    server_context.load_cert_chain(cert_path, key_path)
    server = RemoteBridgeHTTPSServer(
        protocol=protocol,
        ssl_context=server_context,
        allowed_hosts=frozenset({"127.0.0.1"}),
        allowed_origins=frozenset({"https://trusted.example"}),
    )
    server.start()
    client_context = ssl.create_default_context(cafile=str(cert_path))
    try:
        url = f"https://127.0.0.1:{server.port}"
        denied = RemoteBridgeHTTPSClient(
            base_url=url,
            bearer="wrong-" + "z" * 40,
            ssl_context=client_context,
        ).call(
            "/v1/sessions",
            {"workspace_id": workspace.workspace_id},
        )
        assert denied.status == 401
        assert denied.body["error"]["code"] == "IDENTITY_UNAVAILABLE"
        wrong_origin = RemoteBridgeHTTPSClient(
            base_url=url,
            bearer=SECRET,
            ssl_context=client_context,
            origin="https://evil.example",
        ).call("/v1/sessions", {"workspace_id": workspace.workspace_id})
        assert wrong_origin.status == 403
        created = RemoteBridgeHTTPSClient(
            base_url=url,
            bearer=SECRET,
            ssl_context=client_context,
            origin="https://trusted.example",
        ).call(
            "/v1/sessions",
            {"workspace_id": workspace.workspace_id},
        )
        assert created.status == 201
        assert created.body["data"]["workspace_id"] == workspace.workspace_id
    finally:
        server.close()


@pytest.mark.asyncio
async def test_remote_hooks_bind_different_desktop_root_to_server_workspace(
    tmp_path,
):
    server_root = tmp_path / "server" / "workspace"
    client_root = tmp_path / "desktop" / "checkout"
    storage = server_root / ".daem0nmcp" / "storage"
    storage.mkdir(parents=True)
    client_root.mkdir(parents=True)
    manager = DatabaseManager(str(storage))
    try:
        await manager.init_db()
    finally:
        await manager.close()
    registry = WorkspaceRegistry([server_root], default_root=server_root)
    workspace = registry.default

    credential_path = tmp_path / "desktop-config" / "credential.json"
    secret, identity = provision_bridge_credential(
        credential_path,
        principal_id="remote-desktop-principal",
        transports=frozenset({"remote-https"}),
    )
    resolver = WorkspaceStorageResolver()
    broker = EditApprovalBroker(storage_resolver=resolver, signing_key=b"r" * 32)
    candidates = CaptureCandidateStore(storage_resolver=resolver)
    protocol = EditBridgeProtocol(
        broker=broker,
        candidates=candidates,
        credentials=BridgeCredentialRegistry({secret: identity}),
        workspace_resolver=registry.resolve,
        workspace_authorizer=lambda candidate, principal: (
            candidate == workspace and principal == identity
        ),
    )
    cert_path, key_path = _certificate(tmp_path)
    server_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server_context.minimum_version = ssl.TLSVersion.TLSv1_2
    server_context.load_cert_chain(cert_path, key_path)
    server = RemoteBridgeHTTPSServer(
        protocol=protocol,
        ssl_context=server_context,
        allowed_hosts=frozenset({"127.0.0.1"}),
        allowed_origins=frozenset({"https://desktop.example"}),
    )
    server.start()
    installation = provision_remote_bridge_installation(
        client_root,
        credential_path=credential_path,
        remote_base_url=f"https://127.0.0.1:{server.port}",
        ca_file=cert_path,
        workspace_id=workspace.workspace_id,
        origin="https://desktop.example",
    )
    environment = installation.environment()
    target = client_root / "src" / "app.py"
    target.parent.mkdir()
    target.write_text("before", encoding="utf-8")
    native_session_id = "remote-client-session"
    first_event = {
        "session_id": native_session_id,
        "tool_use_id": "remote-denied-1",
        "tool_name": "Edit",
        "tool_input": {
            "file_path": str(target),
            "old_string": "before",
            "new_string": "after",
        },
    }
    try:
        with patch.dict(os.environ, environment, clear=False):
            denied = drive_native_edit(first_event, str(client_root))
        assert not denied
        assert (
            workspace.workspace_id
            != WorkspaceRegistry(default_root=client_root).default.workspace_id
        )
        normalized = native_edit_request(
            project_path=client_root,
            tool_name="Edit",
            tool_input=first_event["tool_input"],
            configured_tools=frozenset({"Edit"}),
        )
        config = EditHostConfig.from_environment(environment)
        pending = EditHostStateStore(config).get_pending(
            workspace_id=workspace.workspace_id,
            native_session_id=native_session_id,
            credential_id=identity.credential_id,
            edit_hash=normalized.edit_hash,
        )
        assert pending is not None
        receipt = await broker.issue_receipt(
            workspace,
            InvocationScope(
                identity.principal_id,
                "actual-mcp-session",
                str(server_root),
            ),
            edit_request_id=pending.edit_request_id,
            description="Apply the exact remote desktop edit",
        )
        actual_response = {
            "api_version": "7",
            "ok": True,
            "data": {
                "edit_request_id": pending.edit_request_id,
                "edit_receipt": receipt.receipt,
                "expires_at": receipt.expires_at.isoformat(),
            },
            "error": None,
            "meta": {"workspace_id": workspace.workspace_id},
        }
        assert handle_edit_preflight_response(
            {
                "session_id": native_session_id,
                "cwd": str(client_root),
                "tool_name": "mcp__daem0n__edit_preflight",
                "tool_input": {
                    "workspace_id": workspace.workspace_id,
                    "edit_request_id": pending.edit_request_id,
                    "description": "Apply the exact remote desktop edit",
                },
                "tool_response": {"structuredContent": actual_response},
            },
            environ=environment,
        )
        retry_event = {**first_event, "tool_use_id": "remote-allowed-2"}
        with patch.dict(os.environ, environment, clear=False):
            allowed = drive_native_edit(retry_event, str(client_root))
        assert allowed
        target.write_text("after", encoding="utf-8")
        with patch.dict(os.environ, environment, clear=False):
            assert handle_post_edit(retry_event, str(client_root))
        staged, boundary = await candidates.list_pending(
            workspace, limit=10, before=None
        )
        assert boundary is None
        assert len(staged) == 1
        assert staged[0].workspace_id == workspace.workspace_id
        assert staged[0].provenance["relative_file_paths"] == ["src/app.py"]
    finally:
        server.close()


@pytest.mark.asyncio
async def test_remote_https_bounds_idle_non_tls_and_duplicate_json(
    tmp_path, transport_context
):
    workspace, _, _, protocol = transport_context
    cert_path, key_path = _certificate(tmp_path)
    server_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server_context.minimum_version = ssl.TLSVersion.TLSv1_2
    server_context.load_cert_chain(cert_path, key_path)
    server = RemoteBridgeHTTPSServer(
        protocol=protocol,
        ssl_context=server_context,
        allowed_hosts=frozenset({"127.0.0.1"}),
        allowed_origins=frozenset(),
    )
    server.start()
    client_context = ssl.create_default_context(cafile=str(cert_path))
    idle = [
        socket.create_connection(("127.0.0.1", server.port), timeout=1)
        for _ in range(4)
    ]
    try:
        started = time.monotonic()
        response = RemoteBridgeHTTPSClient(
            base_url=f"https://127.0.0.1:{server.port}",
            bearer=SECRET,
            ssl_context=client_context,
        ).call("/v1/sessions", {"workspace_id": workspace.workspace_id})
        assert response.status == 201
        assert time.monotonic() - started < 4

        for peer in idle:
            peer.close()
        idle.clear()
        time.sleep(2.2)

        header_idle = client_context.wrap_socket(
            socket.create_connection(("127.0.0.1", server.port), timeout=1),
            server_hostname="127.0.0.1",
        )
        body_idle = client_context.wrap_socket(
            socket.create_connection(("127.0.0.1", server.port), timeout=1),
            server_hostname="127.0.0.1",
        )
        body_idle.sendall(
            b"POST /v1/sessions HTTP/1.1\r\nHost: 127.0.0.1\r\n"
            b"X-Daem0n-Bridge-Context: native-host-v1\r\n"
            b"Content-Type: application/json\r\nContent-Length: 100\r\n\r\n{"
        )
        idle.extend([header_idle, body_idle])
        assert (
            RemoteBridgeHTTPSClient(
                base_url=f"https://127.0.0.1:{server.port}",
                bearer=SECRET,
                ssl_context=client_context,
            )
            .call("/v1/sessions", {"workspace_id": workspace.workspace_id})
            .status
            == 201
        )

        invalid_tls = socket.create_connection(("127.0.0.1", server.port), timeout=1)
        invalid_tls.sendall(b"not tls")
        invalid_tls.close()

        connection = http.client.HTTPSConnection(
            "127.0.0.1",
            server.port,
            context=client_context,
            timeout=5,
        )
        duplicate_body = (
            '{"workspace_id":"'
            + workspace.workspace_id
            + '","workspace_id":"'
            + workspace.workspace_id
            + '"}'
        )
        connection.request(
            "POST",
            "/v1/sessions",
            body=duplicate_body,
            headers={
                "authorization": f"Bearer {SECRET}",
                "content-type": "application/json",
                "x-daem0n-bridge-context": "native-host-v1",
            },
        )
        duplicate_response = connection.getresponse()
        assert duplicate_response.status == 400
        duplicate_response.read()
        connection.close()

        wrong_host = http.client.HTTPSConnection(
            "127.0.0.1", server.port, context=client_context, timeout=5
        )
        wrong_host.putrequest("POST", "/v1/sessions", skip_host=True)
        wrong_host.putheader("Host", "evil.example")
        wrong_host.putheader("Authorization", f"Bearer {SECRET}")
        wrong_host.putheader("X-Daem0n-Bridge-Context", "native-host-v1")
        wrong_host.putheader("Content-Length", "2")
        wrong_host.endheaders(b"{}")
        assert wrong_host.getresponse().status == 403
        wrong_host.close()
    finally:
        shutdown_started = time.monotonic()
        server.close()
        for peer in idle:
            peer.close()
        assert time.monotonic() - shutdown_started < 4
