"""Authenticated local-IPC and HTTPS transports for the native-edit bridge."""

from __future__ import annotations

import asyncio
import concurrent.futures
import hashlib
import hmac
import json
import multiprocessing.connection
import os
import queue
import secrets
import socket
import ssl
import stat
import sys
import threading
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping
from contextlib import suppress
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Protocol

from .capture_candidates import (
    CaptureCandidateError,
    CaptureCandidateRequest,
    CaptureCandidateStore,
)
from .edit_bridge import (
    MAX_BRIDGE_BODY_BYTES,
    BridgeCredentialRegistry,
    BridgeIdentity,
    BridgeTransport,
    EditApprovalBroker,
    EditBridgeError,
    FilePreimage,
    NativeEditRequest,
)
from .event_store import canonical_json_bytes
from .protected_files import (
    ensure_owner_only_directory,
    verify_owner_only_file,
    write_new_owner_only_file,
)
from .workspace import Workspace

_RECOVERABLE_MESSAGE = (
    "Native edit approval is unavailable. Stage a fresh edit request and retry."
)

EDIT_BRIDGE_CREDENTIAL_FILE_ENV = "DAEM0NMCP_EDIT_BRIDGE_CREDENTIAL_FILE"
EDIT_BRIDGE_MODE_ENV = "DAEM0NMCP_EDIT_BRIDGE_MODE"
EDIT_BRIDGE_RUNTIME_DIR_ENV = "DAEM0NMCP_EDIT_BRIDGE_RUNTIME_DIR"
EDIT_BRIDGE_REMOTE_HOST_ENV = "DAEM0NMCP_EDIT_BRIDGE_REMOTE_HOST"
EDIT_BRIDGE_REMOTE_PORT_ENV = "DAEM0NMCP_EDIT_BRIDGE_REMOTE_PORT"
EDIT_BRIDGE_TLS_CERT_ENV = "DAEM0NMCP_EDIT_BRIDGE_TLS_CERT"
EDIT_BRIDGE_TLS_KEY_ENV = "DAEM0NMCP_EDIT_BRIDGE_TLS_KEY"
EDIT_BRIDGE_ALLOWED_HOSTS_ENV = "DAEM0NMCP_EDIT_BRIDGE_ALLOWED_HOSTS"
EDIT_BRIDGE_ALLOWED_ORIGINS_ENV = "DAEM0NMCP_EDIT_BRIDGE_ALLOWED_ORIGINS"

BRIDGE_IO_TIMEOUT_SECONDS = 2.0
BRIDGE_CLIENT_TIMEOUT_SECONDS = 5.0
BRIDGE_MAX_ACTIVE_REQUESTS = 4
BRIDGE_MAX_QUEUED_REQUESTS = 8
_BRIDGE_CONTEXT = "native-host-v1"


@dataclass(frozen=True, slots=True)
class BridgeResponse:
    status: int
    body: Mapping[str, Any]

    def encoded(self) -> bytes:
        return canonical_json_bytes(dict(self.body))


class BridgeConnectionError(RuntimeError):
    """Recoverable fail-closed transport error safe for host adapters."""

    code = "EDIT_BRIDGE_UNAVAILABLE"

    def __init__(self) -> None:
        super().__init__(_RECOVERABLE_MESSAGE)


def _reject_duplicate_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _reject_json_constant(_value: str) -> None:
    raise ValueError("non-finite JSON value")


def _strict_json_loads(raw: bytes | str) -> object:
    return json.loads(
        raw,
        object_pairs_hook=_reject_duplicate_pairs,
        parse_constant=_reject_json_constant,
    )


def _local_proof(authkey: bytes, payload: Mapping[str, Any]) -> str:
    return hmac.new(
        authkey,
        b"daem0nmcp-v7-local-request\x00" + canonical_json_bytes(dict(payload)),
        hashlib.sha256,
    ).hexdigest()


class _LocalConnection(Protocol):
    def close(self) -> None: ...
    def poll(self, timeout: float = 0.0) -> bool: ...
    def recv_bytes(self, maxlength: int | None = None) -> bytes: ...
    def send_bytes(
        self, buf: bytes, offset: int = 0, size: int | None = None
    ) -> None: ...


def _connect_local(address: str, *, timeout_seconds: float) -> _LocalConnection:
    if sys.platform == "win32":
        import _winapi

        timeout_ms = max(1, int(timeout_seconds * 1_000))
        _winapi.WaitNamedPipe(address, timeout_ms)
        handle = _winapi.CreateFile(
            address,
            _winapi.GENERIC_READ | _winapi.GENERIC_WRITE,
            0,
            _winapi.NULL,
            _winapi.OPEN_EXISTING,
            _winapi.FILE_FLAG_OVERLAPPED,
            _winapi.NULL,
        )
        _winapi.SetNamedPipeHandleState(
            handle, _winapi.PIPE_READMODE_MESSAGE, None, None
        )
        return multiprocessing.connection.PipeConnection(handle)
    peer = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        peer.settimeout(timeout_seconds)
        peer.connect(address)
        return multiprocessing.connection.Connection(peer.detach())
    except Exception:
        peer.close()
        raise


def _client_response(payload: object) -> BridgeResponse:
    try:
        envelope = _object(payload, fields=frozenset({"status", "body"}))
        status = envelope["status"]
        body = envelope["body"]
        if (
            isinstance(status, bool)
            or not isinstance(status, int)
            or not 100 <= status <= 599
            or not isinstance(body, dict)
        ):
            raise ValueError
        return BridgeResponse(status, body)
    except (EditBridgeError, TypeError, ValueError):
        raise BridgeConnectionError from None


def _object(value: object, *, fields: frozenset[str]) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != fields:
        raise EditBridgeError("INVALID_ARGUMENT", "bridge request shape rejected")
    return value


def _text(value: object, *, maximum: int = 4096) -> str:
    if not isinstance(value, str) or not 1 <= len(value) <= maximum:
        raise EditBridgeError("INVALID_ARGUMENT", "bridge text rejected")
    return value


def _native_edit(value: object) -> NativeEditRequest:
    body = _object(
        value,
        fields=frozenset({"tool_name", "arguments", "preimages"}),
    )
    raw_preimages = body["preimages"]
    if not isinstance(raw_preimages, list):
        raise EditBridgeError("INVALID_ARGUMENT", "edit preimages rejected")
    try:
        preimages = tuple(
            FilePreimage(
                relative_file_path=item["relative_file_path"],
                state=item["state"],
                sha256=item["sha256"],
                byte_count=item["byte_count"],
            )
            for item in raw_preimages
            if isinstance(item, dict)
            and set(item) == {"relative_file_path", "state", "sha256", "byte_count"}
        )
        if len(preimages) != len(raw_preimages):
            raise ValueError
        if not isinstance(body["arguments"], dict):
            raise ValueError
        return NativeEditRequest(
            tool_name=body["tool_name"],
            arguments=body["arguments"],
            preimages=preimages,
        )
    except (KeyError, TypeError, ValueError):
        raise EditBridgeError("INVALID_ARGUMENT", "native edit rejected") from None


def _actual_edit_preflight_response(
    value: object, *, workspace_id: str
) -> tuple[str, str]:
    response = value if isinstance(value, dict) else None
    if (
        response is None
        or response.get("api_version") != "7"
        or response.get("ok") is not True
        or response.get("error") is not None
        or not isinstance(response.get("data"), dict)
        or not isinstance(response.get("meta"), dict)
        or response["meta"].get("workspace_id") != workspace_id
    ):
        raise EditBridgeError(
            "TOKEN_TAMPERED", "actual edit_preflight response required"
        )
    data = response["data"]
    if set(data) != {"edit_request_id", "edit_receipt", "expires_at"}:
        raise EditBridgeError(
            "TOKEN_TAMPERED", "actual edit_preflight response required"
        )
    return _text(data["edit_request_id"], maximum=68), _text(
        data["edit_receipt"], maximum=4096
    )


class EditBridgeProtocol:
    """One strict protocol core used by both protected transports."""

    def __init__(
        self,
        *,
        broker: EditApprovalBroker,
        candidates: CaptureCandidateStore,
        credentials: BridgeCredentialRegistry,
        workspace_resolver: Callable[[str], Workspace],
        workspace_authorizer: Callable[[Workspace, BridgeIdentity], bool],
    ) -> None:
        self._broker = broker
        self._candidates = candidates
        self._credentials = credentials
        self._workspace_resolver = workspace_resolver
        self._workspace_authorizer = workspace_authorizer

    def _identity(
        self, authorization: str, *, transport: BridgeTransport
    ) -> BridgeIdentity:
        if not isinstance(authorization, str) or not authorization.startswith(
            "Bearer "
        ):
            raise EditBridgeError("IDENTITY_UNAVAILABLE", "bridge bearer missing")
        return self._credentials.authenticate(
            authorization.removeprefix("Bearer "), transport=transport
        )

    def _workspace(self, workspace_id: object, identity: BridgeIdentity) -> Workspace:
        if not isinstance(workspace_id, str):
            raise EditBridgeError("UNAUTHORIZED_WORKSPACE", "workspace rejected")
        try:
            workspace = self._workspace_resolver(workspace_id)
        except Exception:
            raise EditBridgeError(
                "UNAUTHORIZED_WORKSPACE", "workspace rejected"
            ) from None
        if (
            not isinstance(workspace, Workspace)
            or workspace.workspace_id != workspace_id
        ):
            raise EditBridgeError("UNAUTHORIZED_WORKSPACE", "workspace rejected")
        try:
            authorized = self._workspace_authorizer(workspace, identity)
        except Exception:
            authorized = False
        if authorized is not True:
            raise EditBridgeError("UNAUTHORIZED_WORKSPACE", "workspace rejected")
        return workspace

    async def handle(
        self,
        *,
        path: str,
        authorization: str,
        body: object,
        transport: BridgeTransport,
        local_peer_verified: bool = False,
    ) -> BridgeResponse:
        try:
            if transport == "local-ipc" and not local_peer_verified:
                raise EditBridgeError(
                    "IDENTITY_UNAVAILABLE", "local peer identity unavailable"
                )
            identity = self._identity(authorization, transport=transport)
            if path == "/v1/sessions":
                request = _object(body, fields=frozenset({"workspace_id"}))
                workspace = self._workspace(request["workspace_id"], identity)
                session = await self._broker.begin_host_session(
                    workspace, identity, transport
                )
                return BridgeResponse(
                    201,
                    {
                        "ok": True,
                        "data": {
                            "host_session_id": session.host_session_id,
                            "workspace_id": session.workspace_id,
                            "expires_at": session.expires_at.isoformat().replace(
                                "+00:00", "Z"
                            ),
                        },
                    },
                )
            if path == "/v1/edits":
                request = _object(
                    body,
                    fields=frozenset({"workspace_id", "host_session_id", "edit"}),
                )
                workspace = self._workspace(request["workspace_id"], identity)
                pending = await self._broker.create_pending_edit(
                    workspace,
                    identity,
                    _text(request["host_session_id"], maximum=68),
                    _native_edit(request["edit"]),
                )
                return BridgeResponse(
                    201,
                    {
                        "ok": True,
                        "data": {
                            "edit_request_id": pending.edit_request_id,
                            "expires_at": pending.expires_at.isoformat().replace(
                                "+00:00", "Z"
                            ),
                            "remedy": {
                                "tool": "edit_preflight",
                                "arguments": {
                                    "workspace_id": pending.workspace_id,
                                    "edit_request_id": pending.edit_request_id,
                                    "description": "Describe the exact planned native edit",
                                },
                            },
                        },
                    },
                )
            if path == "/v1/receipts/stage":
                request = _object(
                    body,
                    fields=frozenset(
                        {
                            "workspace_id",
                            "host_session_id",
                            "tool_name",
                            "actual_mcp_response",
                        }
                    ),
                )
                if request["tool_name"] != "edit_preflight":
                    raise EditBridgeError(
                        "TOKEN_OPERATION_MISMATCH", "edit_preflight response required"
                    )
                workspace = self._workspace(request["workspace_id"], identity)
                edit_request_id, receipt = _actual_edit_preflight_response(
                    request["actual_mcp_response"],
                    workspace_id=workspace.workspace_id,
                )
                await self._broker.stage_actual_mcp_response(
                    workspace,
                    identity,
                    host_session_id=_text(request["host_session_id"], maximum=68),
                    edit_request_id=edit_request_id,
                    receipt=receipt,
                )
                return BridgeResponse(
                    200,
                    {"ok": True, "data": {"edit_request_id": edit_request_id}},
                )
            if path == "/v1/receipts/consume":
                request = _object(
                    body,
                    fields=frozenset(
                        {
                            "workspace_id",
                            "host_session_id",
                            "edit_request_id",
                            "edit",
                        }
                    ),
                )
                workspace = self._workspace(request["workspace_id"], identity)
                approval = await self._broker.consume_retry(
                    workspace,
                    identity,
                    host_session_id=_text(request["host_session_id"], maximum=68),
                    edit_request_id=_text(request["edit_request_id"], maximum=68),
                    edit=_native_edit(request["edit"]),
                )
                return BridgeResponse(
                    200,
                    {
                        "ok": True,
                        "data": {
                            "edit_request_id": approval.edit_request_id,
                            "allowed": approval.allowed,
                            "consumed_at": approval.consumed_at.isoformat().replace(
                                "+00:00", "Z"
                            ),
                        },
                    },
                )
            if path == "/v1/captures":
                request = _object(
                    body,
                    fields=frozenset(
                        {
                            "workspace_id",
                            "host_session_id",
                            "source_kind",
                            "record",
                            "provenance",
                            "idempotency_key",
                        }
                    ),
                )
                workspace = self._workspace(request["workspace_id"], identity)
                host_session_id = _text(request["host_session_id"], maximum=68)
                await self._broker.authorize_host_session(
                    workspace, identity, host_session_id
                )
                provenance = (
                    dict(request["provenance"])
                    if isinstance(request["provenance"], dict)
                    else None
                )
                if provenance is None or not isinstance(request["record"], dict):
                    raise EditBridgeError("INVALID_ARGUMENT", "capture rejected")
                provenance["host_session_id"] = host_session_id
                candidate = await self._candidates.stage(
                    workspace,
                    CaptureCandidateRequest(
                        source_kind=request["source_kind"],
                        record=request["record"],
                        provenance=provenance,
                        idempotency_key=_text(request["idempotency_key"], maximum=128),
                    ),
                )
                return BridgeResponse(
                    201,
                    {
                        "ok": True,
                        "data": {"candidate_id": candidate.candidate_id},
                    },
                )
            return BridgeResponse(
                404,
                {
                    "ok": False,
                    "error": {"code": "NOT_FOUND", "message": _RECOVERABLE_MESSAGE},
                },
            )
        except (CaptureCandidateError, EditBridgeError, TypeError, ValueError) as error:
            code = getattr(error, "code", "INVALID_ARGUMENT")
            status = (
                401
                if code == "IDENTITY_UNAVAILABLE"
                else 403
                if code
                in {
                    "TOKEN_SCOPE_MISMATCH",
                    "UNAUTHORIZED_WORKSPACE",
                }
                else 409
                if code
                in {
                    "TOKEN_ARGUMENT_MISMATCH",
                    "TOKEN_EXPIRED",
                    "TOKEN_REPLAYED",
                }
                else 400
            )
            return BridgeResponse(
                status,
                {
                    "ok": False,
                    "error": {"code": code, "message": _RECOVERABLE_MESSAGE},
                },
            )


def provision_bridge_credential(
    path: Path,
    *,
    principal_id: str,
    transports: frozenset[BridgeTransport],
) -> tuple[str, BridgeIdentity]:
    """Create one host-only credential file without exposing it to MCP."""

    target = path.absolute()
    if target.exists() or target.is_symlink():
        raise FileExistsError("bridge credential already exists")
    secret = secrets.token_urlsafe(48)
    identity = BridgeIdentity(
        credential_id="cred_" + secrets.token_hex(16),
        principal_id=principal_id,
        transports=transports,
    )
    payload = {
        "credential_id": identity.credential_id,
        "principal_id": identity.principal_id,
        "transports": sorted(identity.transports),
        "secret": secret,
    }
    write_new_owner_only_file(target, canonical_json_bytes(payload))
    return secret, identity


def load_bridge_credential(path: Path) -> tuple[str, BridgeIdentity]:
    """Load a host-owned credential without putting its secret in process args."""

    target = verify_owner_only_file(path, max_bytes=16_384)
    try:
        payload = _strict_json_loads(target.read_bytes())
        body = _object(
            payload,
            fields=frozenset({"credential_id", "principal_id", "transports", "secret"}),
        )
        transports = body["transports"]
        if not isinstance(transports, list):
            raise ValueError
        identity = BridgeIdentity(
            credential_id=_text(body["credential_id"], maximum=128),
            principal_id=_text(body["principal_id"], maximum=512),
            transports=frozenset(transports),
        )
        secret = _text(body["secret"], maximum=512)
        if len(secret) < 32:
            raise ValueError
        return secret, identity
    except (EditBridgeError, TypeError, ValueError, UnicodeError):
        raise ValueError("bridge credential file is invalid") from None


def local_bridge_authkey(secret: str) -> bytes:
    return hashlib.sha256(
        b"daem0nmcp-v7-local-edit-bridge\x00" + secret.encode("utf-8")
    ).digest()


def local_authority_principal(storage_path: str | Path) -> str:
    """Derive the principal shared by one local server storage authority."""

    storage = os.path.normcase(str(Path(storage_path).resolve()))
    digest = hashlib.sha256(storage.encode("utf-8")).hexdigest()
    return f"process-authority:{digest}"


def local_bridge_address(runtime_directory: Path, credential_id: str) -> str:
    digest = hashlib.sha256(credential_id.encode("utf-8")).hexdigest()[:24]
    if sys.platform == "win32":
        return rf"\\.\pipe\daem0nmcp-{digest}"
    directory = ensure_owner_only_directory(runtime_directory)
    return str(directory / f"b-{digest}.sock")


class LocalBridgeServer:
    """Credential-authenticated named-pipe/Unix-socket bridge."""

    def __init__(
        self,
        *,
        protocol: EditBridgeProtocol,
        address: str,
        authkey: bytes,
    ) -> None:
        if len(authkey) < 32:
            raise ValueError("local bridge auth key is too short")
        self._protocol = protocol
        self.address = address
        self._authkey = authkey
        self._listener: multiprocessing.connection.Listener | None = None
        self._thread: threading.Thread | None = None
        self._workers: list[threading.Thread] = []
        self._pending: queue.Queue[_LocalConnection | None] = queue.Queue(
            maxsize=BRIDGE_MAX_QUEUED_REQUESTS
        )
        self._active: set[_LocalConnection] = set()
        self._active_lock = threading.Lock()
        self._stop = threading.Event()

    @property
    def is_ready(self) -> bool:
        return (
            not self._stop.is_set()
            and self._thread is not None
            and self._thread.is_alive()
            and len(self._workers) == BRIDGE_MAX_ACTIVE_REQUESTS
            and all(worker.is_alive() for worker in self._workers)
        )

    def start(self) -> None:
        if self._thread is not None:
            raise RuntimeError("local bridge is already running")
        family = "AF_PIPE" if sys.platform == "win32" else "AF_UNIX"
        if family == "AF_UNIX":
            endpoint = Path(self.address)
            if endpoint.exists() or endpoint.is_symlink():
                raise FileExistsError("local bridge endpoint already exists")
        self._listener = multiprocessing.connection.Listener(
            self.address,
            family=family,
            backlog=BRIDGE_MAX_ACTIVE_REQUESTS + BRIDGE_MAX_QUEUED_REQUESTS,
        )
        if family == "AF_UNIX":
            os.chmod(self.address, stat.S_IRUSR | stat.S_IWUSR)
        self._thread = threading.Thread(
            target=self._serve,
            name="daem0nmcp-edit-bridge-ipc",
            daemon=True,
        )
        self._thread.start()
        for index in range(BRIDGE_MAX_ACTIVE_REQUESTS):
            worker = threading.Thread(
                target=self._work,
                name=f"daem0nmcp-edit-bridge-ipc-{index}",
                daemon=True,
            )
            self._workers.append(worker)
            worker.start()

    def _serve(self) -> None:
        assert self._listener is not None
        while not self._stop.is_set():
            try:
                connection = self._listener.accept()
            except (multiprocessing.AuthenticationError, OSError, EOFError):
                if self._stop.is_set():
                    break
                continue
            if self._stop.is_set():
                connection.close()
                break
            try:
                self._pending.put_nowait(connection)
            except queue.Full:
                connection.close()

    def _work(self) -> None:
        while True:
            connection = self._pending.get()
            if connection is None:
                return
            with self._active_lock:
                self._active.add(connection)
            try:
                self._handle_connection(connection)
            finally:
                with self._active_lock:
                    self._active.discard(connection)
                connection.close()

    def _handle_connection(self, connection: _LocalConnection) -> None:
        response = BridgeResponse(
            400,
            {
                "ok": False,
                "error": {
                    "code": "INVALID_ARGUMENT",
                    "message": _RECOVERABLE_MESSAGE,
                },
            },
        )
        try:
            if not connection.poll(BRIDGE_IO_TIMEOUT_SECONDS):
                return
            raw = connection.recv_bytes(MAX_BRIDGE_BODY_BYTES)
            envelope = _object(
                _strict_json_loads(raw),
                fields=frozenset({"path", "authorization", "body", "proof"}),
            )
            request_payload = {
                "path": envelope["path"],
                "authorization": envelope["authorization"],
                "body": envelope["body"],
            }
            proof = envelope["proof"]
            if not isinstance(proof, str) or not hmac.compare_digest(
                proof, _local_proof(self._authkey, request_payload)
            ):
                response = BridgeResponse(
                    401,
                    {
                        "ok": False,
                        "error": {
                            "code": "IDENTITY_UNAVAILABLE",
                            "message": _RECOVERABLE_MESSAGE,
                        },
                    },
                )
            else:
                response = asyncio.run(
                    self._protocol.handle(
                        path=_text(envelope["path"], maximum=128),
                        authorization=_text(envelope["authorization"], maximum=4096),
                        body=envelope["body"],
                        transport="local-ipc",
                        local_peer_verified=True,
                    )
                )
        except Exception:
            pass
        with suppress(Exception):
            connection.send_bytes(
                canonical_json_bytes(
                    {"status": response.status, "body": dict(response.body)}
                )
            )

    def close(self) -> None:
        self._stop.set()
        if self._listener is not None and self._thread is not None:
            with suppress(Exception):
                wake = _connect_local(
                    self.address, timeout_seconds=BRIDGE_IO_TIMEOUT_SECONDS
                )
                wake.close()
        if self._thread is not None:
            self._thread.join(timeout=5)
            if self._thread.is_alive():
                raise RuntimeError("local bridge did not stop")
        if self._listener is not None:
            self._listener.close()
        with self._active_lock:
            active = tuple(self._active)
        for connection in active:
            with suppress(Exception):
                connection.close()
        while True:
            try:
                queued = self._pending.get_nowait()
            except queue.Empty:
                break
            if queued is not None:
                queued.close()
        for _worker in self._workers:
            self._pending.put(None)
        for worker in self._workers:
            worker.join(timeout=BRIDGE_IO_TIMEOUT_SECONDS + 1)
            if worker.is_alive():
                raise RuntimeError("local bridge worker did not stop")
        if sys.platform != "win32":
            with suppress(FileNotFoundError):
                Path(self.address).unlink()


class LocalBridgeClient:
    def __init__(
        self,
        *,
        address: str,
        authkey: bytes,
        bearer: str,
        timeout_seconds: float = BRIDGE_CLIENT_TIMEOUT_SECONDS,
    ) -> None:
        if not 0 < timeout_seconds <= 30:
            raise ValueError("local bridge timeout is invalid")
        self._address = address
        self._authkey = authkey
        self._bearer = bearer
        self._timeout_seconds = timeout_seconds

    def call(self, path: str, body: Mapping[str, Any]) -> BridgeResponse:
        request_payload = {
            "path": path,
            "authorization": f"Bearer {self._bearer}",
            "body": dict(body),
        }
        encoded = canonical_json_bytes(
            {**request_payload, "proof": _local_proof(self._authkey, request_payload)}
        )
        if len(encoded) > MAX_BRIDGE_BODY_BYTES:
            raise ValueError("bridge request exceeds the byte limit")
        connection = None
        timer = None
        try:
            connection = _connect_local(
                self._address, timeout_seconds=self._timeout_seconds
            )
            timer = threading.Timer(self._timeout_seconds, connection.close)
            timer.daemon = True
            timer.start()
            connection.send_bytes(encoded)
            if not connection.poll(self._timeout_seconds):
                raise BridgeConnectionError
            payload = _strict_json_loads(connection.recv_bytes(MAX_BRIDGE_BODY_BYTES))
            return _client_response(payload)
        except (BridgeConnectionError, EOFError, OSError, UnicodeError, ValueError):
            raise BridgeConnectionError from None
        finally:
            if timer is not None:
                timer.cancel()
            if connection is not None:
                with suppress(OSError):
                    connection.close()


class RemoteBridgeHTTPSClient:
    """Bounded authenticated HTTPS client for trusted remote host adapters."""

    def __init__(
        self,
        *,
        base_url: str,
        bearer: str,
        ssl_context: ssl.SSLContext,
        origin: str | None = None,
        timeout_seconds: float = 5.0,
    ) -> None:
        parsed_url = urllib.parse.urlsplit(base_url)
        if (
            parsed_url.scheme != "https"
            or parsed_url.hostname is None
            or parsed_url.username is not None
            or parsed_url.password is not None
            or parsed_url.path not in {"", "/"}
            or parsed_url.query
            or parsed_url.fragment
        ):
            raise ValueError("remote edit bridge URL must be HTTPS")
        if len(bearer) < 32 or not 0 < timeout_seconds <= 30:
            raise ValueError("remote edit bridge client configuration is invalid")
        self._base_url = base_url.rstrip("/")
        self._bearer = bearer
        self._ssl_context = ssl_context
        self._origin = origin
        self._timeout_seconds = timeout_seconds

    def call(self, path: str, body: Mapping[str, Any]) -> BridgeResponse:
        if not path.startswith("/v1/") or "?" in path or "#" in path:
            raise ValueError("remote edit bridge path is invalid")
        encoded = canonical_json_bytes(dict(body))
        if len(encoded) > MAX_BRIDGE_BODY_BYTES:
            raise ValueError("bridge request exceeds the byte limit")
        headers = {
            "authorization": f"Bearer {self._bearer}",
            "content-type": "application/json",
            "x-daem0n-bridge-context": _BRIDGE_CONTEXT,
        }
        if self._origin is not None:
            headers["origin"] = self._origin
        request = urllib.request.Request(
            self._base_url + path,
            method="POST",
            data=encoded,
            headers=headers,
        )
        try:
            with urllib.request.urlopen(
                request,
                context=self._ssl_context,
                timeout=self._timeout_seconds,
            ) as response:
                raw = response.read(MAX_BRIDGE_BODY_BYTES + 1)
                if len(raw) > MAX_BRIDGE_BODY_BYTES:
                    raise BridgeConnectionError
                payload = _strict_json_loads(raw)
                status = response.status
        except urllib.error.HTTPError as error:
            try:
                raw = error.read(MAX_BRIDGE_BODY_BYTES + 1)
                if len(raw) > MAX_BRIDGE_BODY_BYTES:
                    raise ValueError
                payload = _strict_json_loads(raw)
                status = error.code
            except (OSError, UnicodeError, ValueError):
                raise BridgeConnectionError from None
        except (OSError, TimeoutError, UnicodeError, ValueError, urllib.error.URLError):
            raise BridgeConnectionError from None
        if not isinstance(payload, dict):
            raise BridgeConnectionError
        return BridgeResponse(status, payload)


class RemoteBridgeHTTPSServer:
    """Small authenticated HTTPS sidecar sharing the production broker."""

    def __init__(
        self,
        *,
        protocol: EditBridgeProtocol,
        ssl_context: ssl.SSLContext,
        allowed_hosts: frozenset[str],
        allowed_origins: frozenset[str],
        host: str = "127.0.0.1",
        port: int = 0,
    ) -> None:
        if ssl_context.minimum_version < ssl.TLSVersion.TLSv1_2:
            raise ValueError("remote bridge requires TLS 1.2 or newer")
        if not allowed_hosts or any(
            not value
            or any(character.isspace() for character in value)
            or any(character in value for character in "/@?#")
            for value in allowed_hosts
        ):
            raise ValueError("remote bridge allowed hosts are invalid")
        if any(not value.startswith("https://") for value in allowed_origins):
            raise ValueError("remote bridge allowed origins are invalid")
        self._protocol = protocol
        self._ssl_context = ssl_context
        self._allowed_hosts = frozenset(
            (
                value[1:-1] if value.startswith("[") and value.endswith("]") else value
            ).casefold()
            for value in allowed_hosts
        )
        self._allowed_origins = allowed_origins
        self._host = host
        self._configured_port = port
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    def _build_server(self) -> ThreadingHTTPServer:
        bridge_protocol = self._protocol
        bridge_context = self._ssl_context
        allowed_hosts = self._allowed_hosts
        allowed_origins = self._allowed_origins

        class Handler(BaseHTTPRequestHandler):
            server_version = "Daem0nEditBridge/1"
            sys_version = ""

            def log_message(self, _format: str, *args: object) -> None:
                del args

            def setup(self) -> None:
                def close_request() -> None:
                    target = getattr(self, "connection", self.request)
                    with suppress(OSError):
                        target.close()

                self._request_deadline = threading.Timer(
                    BRIDGE_CLIENT_TIMEOUT_SECONDS, close_request
                )
                self._request_deadline.daemon = True
                self._request_deadline.start()
                self.request.settimeout(BRIDGE_IO_TIMEOUT_SECONDS)
                connection = bridge_context.wrap_socket(
                    self.request,
                    server_side=True,
                    do_handshake_on_connect=False,
                )
                connection.settimeout(BRIDGE_IO_TIMEOUT_SECONDS)
                connection.do_handshake()
                self.connection = connection
                self.rfile = connection.makefile("rb", self.rbufsize)  # type: ignore[assignment]
                self.wfile = connection.makefile("wb", self.wbufsize)  # type: ignore[assignment]

            def finish(self) -> None:
                try:
                    super().finish()
                finally:
                    self._request_deadline.cancel()

            def _trusted_request_context(self) -> bool:
                if (
                    len(self.headers.get_all("host", [])) != 1
                    or len(self.headers.get_all("origin", [])) > 1
                    or len(self.headers.get_all("x-daem0n-bridge-context", [])) != 1
                    or len(self.headers.get_all("authorization", [])) != 1
                ):
                    return False
                host = self.headers.get("host", "")
                try:
                    parsed = urllib.parse.urlsplit("//" + host)
                    hostname = parsed.hostname
                    _ = parsed.port
                except ValueError:
                    return False
                if (
                    hostname is None
                    or parsed.username is not None
                    or parsed.password is not None
                    or parsed.path
                    or parsed.query
                    or parsed.fragment
                    or hostname.casefold() not in allowed_hosts
                    or self.headers.get("x-daem0n-bridge-context") != _BRIDGE_CONTEXT
                ):
                    return False
                origin = self.headers.get("origin")
                return origin is None or origin in allowed_origins

            def do_POST(self) -> None:
                if not self._trusted_request_context():
                    response = BridgeResponse(
                        403,
                        {
                            "ok": False,
                            "error": {
                                "code": "TOKEN_SCOPE_MISMATCH",
                                "message": _RECOVERABLE_MESSAGE,
                            },
                        },
                    )
                    self._send(response)
                    return
                lengths = self.headers.get_all("content-length", [])
                try:
                    size = int(lengths[0]) if len(lengths) == 1 else -1
                except ValueError:
                    size = -1
                if (
                    size < 0
                    or size > MAX_BRIDGE_BODY_BYTES
                    or self.headers.get("transfer-encoding") is not None
                    or self.headers.get("content-type", "")
                    .split(";", 1)[0]
                    .strip()
                    .casefold()
                    != "application/json"
                ):
                    self.send_error(413)
                    return
                raw = self.rfile.read(size)
                try:
                    body = _strict_json_loads(raw)
                except (UnicodeError, ValueError):
                    response = BridgeResponse(
                        400,
                        {
                            "ok": False,
                            "error": {
                                "code": "INVALID_ARGUMENT",
                                "message": _RECOVERABLE_MESSAGE,
                            },
                        },
                    )
                else:
                    response = asyncio.run(
                        bridge_protocol.handle(
                            path=self.path,
                            authorization=self.headers.get("authorization", ""),
                            body=body,
                            transport="remote-https",
                        )
                    )
                self._send(response)

            def _send(self, response: BridgeResponse) -> None:
                encoded = response.encoded()
                self.send_response(response.status)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(encoded)))
                self.send_header("cache-control", "no-store")
                self.end_headers()
                self.wfile.write(encoded)

        class BoundedServer(ThreadingHTTPServer):
            daemon_threads = True

            def __init__(self, address: tuple[str, int]) -> None:
                self._admission = threading.BoundedSemaphore(
                    BRIDGE_MAX_ACTIVE_REQUESTS + BRIDGE_MAX_QUEUED_REQUESTS
                )
                self._executor = concurrent.futures.ThreadPoolExecutor(
                    max_workers=BRIDGE_MAX_ACTIVE_REQUESTS,
                    thread_name_prefix="daem0nmcp-edit-bridge-https",
                )
                super().__init__(address, Handler)

            def process_request(self, request: Any, address: Any) -> None:
                if not self._admission.acquire(blocking=False):
                    self.shutdown_request(request)
                    return
                try:
                    future = self._executor.submit(
                        self.process_request_thread, request, address
                    )
                    future.add_done_callback(lambda _future: self._admission.release())
                except Exception:
                    self._admission.release()
                    self.shutdown_request(request)

            def handle_error(self, request: object, client_address: object) -> None:
                del request, client_address

            def server_close(self) -> None:
                super().server_close()
                self._executor.shutdown(wait=True, cancel_futures=True)

        server = BoundedServer((self._host, self._configured_port))
        return server

    @property
    def port(self) -> int:
        if self._server is None:
            return self._configured_port
        return int(self._server.server_address[1])

    @property
    def is_ready(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        if self._thread is not None:
            raise RuntimeError("remote bridge is already running")
        self._server = self._build_server()
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            name="daem0nmcp-edit-bridge-https",
            daemon=True,
        )
        self._thread.start()

    def close(self) -> None:
        if self._server is None:
            return
        self._server.shutdown()
        self._server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=5)
            if self._thread.is_alive():
                raise RuntimeError("remote bridge did not stop")


def build_edit_bridge_service(
    *,
    broker: EditApprovalBroker,
    candidates: CaptureCandidateStore,
    workspace_resolver: Callable[[str], Workspace],
    workspace_authorizer: Callable[[Workspace, BridgeIdentity], bool],
    environ: Mapping[str, str],
) -> LocalBridgeServer | RemoteBridgeHTTPSServer | None:
    """Build an optional bridge service around production-owned authorities."""

    configured = environ.get(EDIT_BRIDGE_CREDENTIAL_FILE_ENV)
    if configured is None:
        return None
    secret, identity = load_bridge_credential(Path(configured))
    protocol = EditBridgeProtocol(
        broker=broker,
        candidates=candidates,
        credentials=BridgeCredentialRegistry({secret: identity}),
        workspace_resolver=workspace_resolver,
        workspace_authorizer=workspace_authorizer,
    )
    mode = environ.get(EDIT_BRIDGE_MODE_ENV, "local")
    if mode == "local":
        if "local-ipc" not in identity.transports:
            raise ValueError("bridge credential does not authorize local IPC")
        runtime = Path(
            environ.get(
                EDIT_BRIDGE_RUNTIME_DIR_ENV,
                str(Path(configured).resolve().parent / "run"),
            )
        )
        return LocalBridgeServer(
            protocol=protocol,
            address=local_bridge_address(runtime, identity.credential_id),
            authkey=local_bridge_authkey(secret),
        )
    if mode != "remote-https" or "remote-https" not in identity.transports:
        raise ValueError("edit bridge mode is invalid")
    cert = environ.get(EDIT_BRIDGE_TLS_CERT_ENV)
    key = environ.get(EDIT_BRIDGE_TLS_KEY_ENV)
    host = environ.get(EDIT_BRIDGE_REMOTE_HOST_ENV)
    raw_port = environ.get(EDIT_BRIDGE_REMOTE_PORT_ENV)
    raw_hosts = environ.get(EDIT_BRIDGE_ALLOWED_HOSTS_ENV)
    raw_origins = environ.get(EDIT_BRIDGE_ALLOWED_ORIGINS_ENV, "[]")
    if (
        cert is None
        or key is None
        or host is None
        or raw_port is None
        or raw_hosts is None
    ):
        raise ValueError("remote edit bridge TLS configuration is incomplete")
    try:
        port = int(raw_port)
        allowed_hosts_value = _strict_json_loads(raw_hosts)
        allowed_origins_value = _strict_json_loads(raw_origins)
        if not isinstance(allowed_hosts_value, list) or not isinstance(
            allowed_origins_value, list
        ):
            raise ValueError
        allowed_hosts = frozenset(allowed_hosts_value)
        allowed_origins = frozenset(allowed_origins_value)
        if not all(isinstance(item, str) for item in allowed_hosts | allowed_origins):
            raise ValueError
    except (TypeError, ValueError):
        raise ValueError("remote edit bridge allowlist is invalid") from None
    if not 1 <= port <= 65_535:
        raise ValueError("remote edit bridge port is invalid")
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.load_cert_chain(cert, key)
    return RemoteBridgeHTTPSServer(
        protocol=protocol,
        ssl_context=context,
        allowed_hosts=allowed_hosts,
        allowed_origins=allowed_origins,
        host=host,
        port=port,
    )


__all__ = [
    "BridgeResponse",
    "BridgeConnectionError",
    "EDIT_BRIDGE_CREDENTIAL_FILE_ENV",
    "EDIT_BRIDGE_MODE_ENV",
    "EditBridgeProtocol",
    "LocalBridgeClient",
    "LocalBridgeServer",
    "RemoteBridgeHTTPSServer",
    "RemoteBridgeHTTPSClient",
    "build_edit_bridge_service",
    "load_bridge_credential",
    "local_bridge_authkey",
    "local_bridge_address",
    "local_authority_principal",
    "provision_bridge_credential",
]
