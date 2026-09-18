"""Exact native-edit approval authority shared by MCP and trusted host bridges."""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Literal

from .api.v7.models import contains_absolute_filesystem_path
from .api.v7.runtime_services import WorkspaceStorageResolver
from .bounded_workers import BoundedWorkerBusyError, BoundedWorkerPool
from .covenant import InvocationScope
from .event_store import canonical_json_bytes, sha256_json
from .schema_version import CURRENT_SCHEMA_VERSION
from .workspace import Workspace

BridgeTransport = Literal["local-ipc", "remote-https"]
PreimageState = Literal["file", "missing"]

EDIT_RECEIPT_TTL_SECONDS = 120
HOST_SESSION_TTL_SECONDS = 8 * 60 * 60
PENDING_EDIT_TTL_SECONDS = 10 * 60
MAX_NATIVE_ARGUMENT_BYTES = 128 * 1024
MAX_BRIDGE_BODY_BYTES = 256 * 1024
MAX_EDIT_PATHS = 32

_BRIDGE_WORKERS = BoundedWorkerPool(
    max_workers=2,
    thread_name_prefix="daem0nmcp-v7-edit-bridge",
)
_HEX_64 = re.compile(r"^[0-9a-f]{64}$")
_TOOL_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9_.:-]{0,79}$")
_TOKEN_PREFIX = "edr_v1"


class EditBridgeError(RuntimeError):
    """Fail-closed bridge rejection safe to translate to a stable code."""

    def __init__(self, code: str, reason: str) -> None:
        self.code = code
        self.reason = reason
        super().__init__(code)


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _datetime_us(value: datetime) -> int:
    if value.tzinfo is None:
        raise ValueError("bridge clock must be timezone aware")
    return int(value.astimezone(timezone.utc).timestamp() * 1_000_000)


def _datetime_from_us(value: int) -> datetime:
    return datetime.fromtimestamp(value / 1_000_000, tz=timezone.utc)


def _relative_path(value: str) -> str:
    if (
        not isinstance(value, str)
        or not 1 <= len(value) <= 1024
        or "\\" in value
        or value.startswith(("/", "~"))
        or re.match(r"^[A-Za-z]:", value)
    ):
        raise ValueError("edit path must be workspace relative")
    path = PurePosixPath(value)
    if path.as_posix() != value or any(part in {"", ".", ".."} for part in path.parts):
        raise ValueError("edit path must be normalized")
    return value


@dataclass(frozen=True, slots=True)
class FilePreimage:
    relative_file_path: str
    state: PreimageState
    sha256: str | None
    byte_count: int

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "relative_file_path", _relative_path(self.relative_file_path)
        )
        if self.state == "missing":
            if self.sha256 is not None or self.byte_count != 0:
                raise ValueError("missing preimage cannot have content")
            return
        if self.state != "file" or not isinstance(self.sha256, str):
            raise ValueError("preimage state is invalid")
        if not _HEX_64.fullmatch(self.sha256):
            raise ValueError("preimage digest is invalid")
        if (
            isinstance(self.byte_count, bool)
            or not isinstance(self.byte_count, int)
            or not 0 <= self.byte_count <= 16 * 1024 * 1024
        ):
            raise ValueError("preimage byte count is invalid")

    def canonical(self) -> dict[str, object]:
        return {
            "relative_file_path": self.relative_file_path,
            "state": self.state,
            "sha256": self.sha256,
            "byte_count": self.byte_count,
        }


@dataclass(frozen=True, slots=True)
class NativeEditRequest:
    """Host-normalized exact edit request; raw arguments are never persisted."""

    tool_name: str
    arguments: Mapping[str, Any]
    preimages: tuple[FilePreimage, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.tool_name, str) or not _TOOL_NAME.fullmatch(
            self.tool_name
        ):
            raise ValueError("native edit tool name is invalid")
        try:
            arguments = json.loads(canonical_json_bytes(dict(self.arguments)))
            encoded = canonical_json_bytes(arguments)
        except (TypeError, ValueError, RecursionError):
            raise ValueError("native edit arguments are invalid") from None
        if len(encoded) > MAX_NATIVE_ARGUMENT_BYTES:
            raise ValueError("native edit arguments exceed the byte limit")
        if contains_absolute_filesystem_path(arguments):
            raise ValueError("native edit arguments must use workspace-relative paths")
        if not self.preimages or len(self.preimages) > MAX_EDIT_PATHS:
            raise ValueError("native edit preimages are invalid")
        preimages = tuple(self.preimages)
        paths = [item.relative_file_path for item in preimages]
        if len(paths) != len(set(paths)) or paths != sorted(paths):
            raise ValueError("native edit preimages must be unique and sorted")
        object.__setattr__(self, "arguments", arguments)
        object.__setattr__(self, "preimages", preimages)

    @property
    def arguments_hash(self) -> str:
        return hashlib.sha256(canonical_json_bytes(dict(self.arguments))).hexdigest()

    @property
    def edit_hash(self) -> str:
        return sha256_json(
            {
                "tool_name": self.tool_name,
                "arguments": dict(self.arguments),
                "preimages": [item.canonical() for item in self.preimages],
            }
        )


@dataclass(frozen=True, slots=True)
class BridgeIdentity:
    credential_id: str
    principal_id: str
    transports: frozenset[BridgeTransport]

    def __post_init__(self) -> None:
        if (
            not self.credential_id
            or not self.principal_id
            or len(self.principal_id) > 512
            or any(
                ord(character) < 32 or ord(character) == 127
                for character in self.principal_id
            )
        ):
            raise ValueError("bridge identity is incomplete")
        if not self.transports or not self.transports <= {
            "local-ipc",
            "remote-https",
        }:
            raise ValueError("bridge identity transport is invalid")


class BridgeCredentialRegistry:
    """Constant-time bearer lookup; plaintext credentials remain host-side."""

    def __init__(self, credentials: Mapping[str, BridgeIdentity]) -> None:
        entries: list[tuple[bytes, BridgeIdentity]] = []
        for secret, identity in credentials.items():
            if not isinstance(secret, str) or len(secret) < 32:
                raise ValueError(
                    "bridge credentials must contain at least 32 characters"
                )
            if not isinstance(identity, BridgeIdentity):
                raise TypeError("bridge credential identity is invalid")
            entries.append((hashlib.sha256(secret.encode("utf-8")).digest(), identity))
        if not entries:
            raise ValueError("at least one bridge credential is required")
        self._entries = tuple(entries)

    def authenticate(
        self, secret: str, *, transport: BridgeTransport
    ) -> BridgeIdentity:
        if not isinstance(secret, str):
            raise EditBridgeError("IDENTITY_UNAVAILABLE", "bridge credential missing")
        supplied = hashlib.sha256(secret.encode("utf-8")).digest()
        matched: BridgeIdentity | None = None
        for expected, identity in self._entries:
            if hmac.compare_digest(supplied, expected):
                matched = identity
        if matched is None or transport not in matched.transports:
            raise EditBridgeError("IDENTITY_UNAVAILABLE", "bridge credential rejected")
        return matched


@dataclass(frozen=True, slots=True)
class HostSession:
    host_session_id: str
    workspace_id: str
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class PendingEdit:
    edit_request_id: str
    host_session_id: str
    workspace_id: str
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class EditReceipt:
    edit_request_id: str
    receipt: str
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class EditApproval:
    edit_request_id: str
    allowed: bool
    consumed_at: datetime


def _open_database(path: os.PathLike[str] | str) -> sqlite3.Connection:
    connection: sqlite3.Connection | None = None
    try:
        resolved = Path(path).resolve()
        connection = sqlite3.connect(
            f"{resolved.as_uri()}?mode=rw",
            uri=True,
            timeout=5.0,
            isolation_level=None,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=5000")
        connection.execute("PRAGMA foreign_keys=ON")
        version = connection.execute(
            "SELECT COALESCE(MAX(version),0) FROM schema_version"
        ).fetchone()
        tables = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name IN "
                "('native_edit_host_sessions','native_edit_pending',"
                "'native_edit_receipts')"
            )
        }
        if (
            version is None
            or int(version[0]) < CURRENT_SCHEMA_VERSION
            or tables
            != {
                "native_edit_host_sessions",
                "native_edit_pending",
                "native_edit_receipts",
            }
        ):
            raise EditBridgeError("CAPABILITY_DEGRADED", "bridge storage unavailable")
        return connection
    except EditBridgeError:
        if connection is not None:
            connection.close()
        raise
    except Exception:
        if connection is not None:
            connection.close()
        raise EditBridgeError(
            "CAPABILITY_DEGRADED", "bridge storage unavailable"
        ) from None


@dataclass(frozen=True, slots=True)
class EditApprovalBroker:
    storage_resolver: WorkspaceStorageResolver = field(
        default_factory=WorkspaceStorageResolver
    )
    signing_key: bytes = field(default_factory=lambda: secrets.token_bytes(32))
    clock: Callable[[], datetime] = field(default=_utc_now)

    def __post_init__(self) -> None:
        if not isinstance(self.signing_key, bytes) or len(self.signing_key) < 32:
            raise ValueError("edit receipt signing key must contain at least 32 bytes")

    def _token(self, payload: Mapping[str, object]) -> str:
        encoded = base64.urlsafe_b64encode(canonical_json_bytes(dict(payload))).rstrip(
            b"="
        )
        signed = _TOKEN_PREFIX.encode("ascii") + b"." + encoded
        signature = base64.urlsafe_b64encode(
            hmac.new(self.signing_key, signed, hashlib.sha256).digest()
        ).rstrip(b"=")
        return f"{_TOKEN_PREFIX}.{encoded.decode('ascii')}.{signature.decode('ascii')}"

    def _decode_token(self, token: str) -> dict[str, object]:
        try:
            prefix, encoded, signature = token.split(".")
            if prefix != _TOKEN_PREFIX or len(token) > 4096:
                raise ValueError
            signed = f"{prefix}.{encoded}".encode("ascii")
            expected = hmac.new(self.signing_key, signed, hashlib.sha256).digest()
            supplied = base64.urlsafe_b64decode(signature + "=" * (-len(signature) % 4))
            if not hmac.compare_digest(expected, supplied):
                raise ValueError
            payload = json.loads(
                base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))
            )
            if not isinstance(payload, dict) or set(payload) != {
                "edit_request_id",
                "edit_hash",
                "expires_at_us",
                "host_session_id",
                "issued_at_us",
                "mcp_session_hash",
                "principal_hash",
                "receipt_id",
                "workspace_id",
            }:
                raise ValueError
            return payload
        except (
            binascii.Error,
            TypeError,
            ValueError,
            UnicodeError,
            json.JSONDecodeError,
        ):
            raise EditBridgeError("TOKEN_TAMPERED", "edit receipt rejected") from None

    async def _run(self, operation: Callable[[], Any]) -> Any:
        try:
            return await _BRIDGE_WORKERS.run(operation)
        except BoundedWorkerBusyError as exc:
            raise EditBridgeError("TASK_REQUIRED", "edit bridge is busy") from exc

    def _begin_host_session_sync(
        self,
        workspace: Workspace,
        identity: BridgeIdentity,
        transport: BridgeTransport,
    ) -> HostSession:
        if transport not in identity.transports:
            raise EditBridgeError("IDENTITY_UNAVAILABLE", "host transport rejected")
        now_us = _datetime_us(self.clock())
        expires_at_us = now_us + HOST_SESSION_TTL_SECONDS * 1_000_000
        host_session_id = "hst_" + secrets.token_hex(32)
        with self.storage_resolver.locked_active(workspace) as active:
            connection = _open_database(active.path)
            try:
                connection.execute("BEGIN IMMEDIATE")
                connection.execute(
                    "INSERT INTO native_edit_host_sessions(host_session_id,workspace_id,"
                    "principal_hash,mcp_session_hash,transport,credential_id_hash,"
                    "created_at_us,expires_at_us) VALUES (?,?,?,?,?,?,?,?)",
                    (
                        host_session_id,
                        workspace.workspace_id,
                        _sha256_text(identity.principal_id),
                        None,
                        transport,
                        _sha256_text(identity.credential_id),
                        now_us,
                        expires_at_us,
                    ),
                )
                connection.commit()
            except Exception:
                if connection.in_transaction:
                    connection.rollback()
                raise EditBridgeError(
                    "CAPABILITY_DEGRADED", "host session failed"
                ) from None
            finally:
                connection.close()
        return HostSession(
            host_session_id=host_session_id,
            workspace_id=workspace.workspace_id,
            expires_at=_datetime_from_us(expires_at_us),
        )

    async def begin_host_session(
        self,
        workspace: Workspace,
        identity: BridgeIdentity,
        transport: BridgeTransport,
    ) -> HostSession:
        return await self._run(
            lambda: self._begin_host_session_sync(workspace, identity, transport)
        )

    def _validate_host(
        self,
        connection: sqlite3.Connection,
        *,
        workspace_id: str,
        identity: BridgeIdentity,
        host_session_id: str,
        now_us: int,
    ) -> sqlite3.Row:
        row = connection.execute(
            "SELECT * FROM native_edit_host_sessions WHERE host_session_id=? "
            "AND workspace_id=?",
            (host_session_id, workspace_id),
        ).fetchone()
        if row is None:
            raise EditBridgeError("NOT_FOUND", "host session unavailable")
        if not hmac.compare_digest(
            str(row["principal_hash"]), _sha256_text(identity.principal_id)
        ) or not hmac.compare_digest(
            str(row["credential_id_hash"]), _sha256_text(identity.credential_id)
        ):
            raise EditBridgeError("TOKEN_SCOPE_MISMATCH", "host identity changed")
        if row["revoked_at_us"] is not None or int(row["expires_at_us"]) <= now_us:
            raise EditBridgeError("TOKEN_EXPIRED", "host session expired")
        return row

    def _create_pending_sync(
        self,
        workspace: Workspace,
        identity: BridgeIdentity,
        host_session_id: str,
        edit: NativeEditRequest,
    ) -> PendingEdit:
        now_us = _datetime_us(self.clock())
        expires_at_us = now_us + PENDING_EDIT_TTL_SECONDS * 1_000_000
        edit_request_id = "edt_" + secrets.token_hex(32)
        paths = [item.relative_file_path for item in edit.preimages]
        preimages = [item.canonical() for item in edit.preimages]
        with self.storage_resolver.locked_active(workspace) as active:
            connection = _open_database(active.path)
            try:
                connection.execute("BEGIN IMMEDIATE")
                self._validate_host(
                    connection,
                    workspace_id=workspace.workspace_id,
                    identity=identity,
                    host_session_id=host_session_id,
                    now_us=now_us,
                )
                connection.execute(
                    "INSERT INTO native_edit_pending(pending_edit_id,workspace_id,"
                    "host_session_id,tool_name,arguments_hash,edit_hash,"
                    "relative_paths_json,preimages_json,created_at_us,expires_at_us) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (
                        edit_request_id,
                        workspace.workspace_id,
                        host_session_id,
                        edit.tool_name,
                        edit.arguments_hash,
                        edit.edit_hash,
                        canonical_json_bytes(paths).decode("utf-8"),
                        canonical_json_bytes(preimages).decode("utf-8"),
                        now_us,
                        expires_at_us,
                    ),
                )
                connection.commit()
            except EditBridgeError:
                if connection.in_transaction:
                    connection.rollback()
                raise
            except Exception:
                if connection.in_transaction:
                    connection.rollback()
                raise EditBridgeError(
                    "CAPABILITY_DEGRADED", "edit staging failed"
                ) from None
            finally:
                connection.close()
        return PendingEdit(
            edit_request_id=edit_request_id,
            host_session_id=host_session_id,
            workspace_id=workspace.workspace_id,
            expires_at=_datetime_from_us(expires_at_us),
        )

    async def create_pending_edit(
        self,
        workspace: Workspace,
        identity: BridgeIdentity,
        host_session_id: str,
        edit: NativeEditRequest,
    ) -> PendingEdit:
        return await self._run(
            lambda: self._create_pending_sync(
                workspace, identity, host_session_id, edit
            )
        )

    def _authorize_host_session_sync(
        self,
        workspace: Workspace,
        identity: BridgeIdentity,
        host_session_id: str,
    ) -> None:
        now_us = _datetime_us(self.clock())
        with self.storage_resolver.locked_active(workspace) as active:
            connection = _open_database(active.path)
            try:
                self._validate_host(
                    connection,
                    workspace_id=workspace.workspace_id,
                    identity=identity,
                    host_session_id=host_session_id,
                    now_us=now_us,
                )
            finally:
                connection.close()

    async def authorize_host_session(
        self,
        workspace: Workspace,
        identity: BridgeIdentity,
        host_session_id: str,
    ) -> None:
        await self._run(
            lambda: self._authorize_host_session_sync(
                workspace, identity, host_session_id
            )
        )

    def _issue_receipt_sync(
        self,
        workspace: Workspace,
        scope: InvocationScope,
        edit_request_id: str,
        description: str,
    ) -> EditReceipt:
        now_us = _datetime_us(self.clock())
        if (
            not isinstance(description, str)
            or not 1 <= len(description) <= 2_000
            or contains_absolute_filesystem_path(description)
        ):
            raise EditBridgeError("INVALID_ARGUMENT", "edit description rejected")
        if os.path.normcase(str(workspace.root.resolve())) != scope.canonical_workspace:
            raise EditBridgeError("TOKEN_SCOPE_MISMATCH", "workspace scope changed")
        principal_hash = _sha256_text(scope.principal_id)
        mcp_session_hash = _sha256_text(scope.transport_session_id)
        description_hash = _sha256_text(description)
        with self.storage_resolver.locked_active(workspace) as active:
            connection = _open_database(active.path)
            try:
                connection.execute("BEGIN IMMEDIATE")
                pending = connection.execute(
                    "SELECT pending.*,host.principal_hash AS host_principal_hash,"
                    "host.mcp_session_hash AS host_mcp_session_hash,"
                    "host.expires_at_us AS host_expires_at_us,host.revoked_at_us "
                    "FROM native_edit_pending AS pending JOIN native_edit_host_sessions "
                    "AS host ON host.host_session_id=pending.host_session_id "
                    "WHERE pending.pending_edit_id=? AND pending.workspace_id=?",
                    (edit_request_id, workspace.workspace_id),
                ).fetchone()
                if pending is None:
                    raise EditBridgeError("NOT_FOUND", "edit request unavailable")
                if (
                    int(pending["expires_at_us"]) <= now_us
                    or int(pending["host_expires_at_us"]) <= now_us
                ):
                    raise EditBridgeError("TOKEN_EXPIRED", "edit request expired")
                if pending["revoked_at_us"] is not None or not hmac.compare_digest(
                    str(pending["host_principal_hash"]), principal_hash
                ):
                    raise EditBridgeError("TOKEN_SCOPE_MISMATCH", "principal changed")
                host_mcp = pending["host_mcp_session_hash"]
                if host_mcp is None:
                    connection.execute(
                        "UPDATE native_edit_host_sessions SET mcp_session_hash=?,"
                        "paired_at_us=? WHERE host_session_id=? AND "
                        "mcp_session_hash IS NULL",
                        (mcp_session_hash, now_us, pending["host_session_id"]),
                    )
                elif not hmac.compare_digest(str(host_mcp), mcp_session_hash):
                    raise EditBridgeError("TOKEN_SCOPE_MISMATCH", "MCP session changed")
                existing = connection.execute(
                    "SELECT * FROM native_edit_receipts WHERE pending_edit_id=?",
                    (edit_request_id,),
                ).fetchone()
                if existing is None:
                    issued_at_us = now_us
                    expires_at_us = now_us + EDIT_RECEIPT_TTL_SECONDS * 1_000_000
                    receipt_id = "rcp_" + sha256_json(
                        ["daem0nmcp", "v7", "native-edit-receipt", edit_request_id]
                    )
                    payload = {
                        "receipt_id": receipt_id,
                        "edit_request_id": edit_request_id,
                        "workspace_id": workspace.workspace_id,
                        "host_session_id": str(pending["host_session_id"]),
                        "principal_hash": principal_hash,
                        "mcp_session_hash": mcp_session_hash,
                        "edit_hash": str(pending["edit_hash"]),
                        "issued_at_us": issued_at_us,
                        "expires_at_us": expires_at_us,
                    }
                    token = self._token(payload)
                    connection.execute(
                        "INSERT INTO native_edit_receipts(receipt_id,pending_edit_id,"
                        "workspace_id,host_session_id,principal_hash,mcp_session_hash,"
                        "edit_hash,description_hash,receipt_token_hash,issued_at_us,"
                        "expires_at_us) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                        (
                            receipt_id,
                            edit_request_id,
                            workspace.workspace_id,
                            pending["host_session_id"],
                            principal_hash,
                            mcp_session_hash,
                            pending["edit_hash"],
                            description_hash,
                            _sha256_text(token),
                            issued_at_us,
                            expires_at_us,
                        ),
                    )
                    connection.execute(
                        "UPDATE native_edit_pending SET preflighted_at_us=? "
                        "WHERE pending_edit_id=?",
                        (now_us, edit_request_id),
                    )
                else:
                    if (
                        not hmac.compare_digest(
                            str(existing["principal_hash"]), principal_hash
                        )
                        or not hmac.compare_digest(
                            str(existing["mcp_session_hash"]), mcp_session_hash
                        )
                        or not hmac.compare_digest(
                            str(existing["description_hash"]), description_hash
                        )
                    ):
                        raise EditBridgeError(
                            "TOKEN_ARGUMENT_MISMATCH", "edit preflight changed"
                        )
                    if int(existing["expires_at_us"]) <= now_us:
                        raise EditBridgeError("TOKEN_EXPIRED", "edit receipt expired")
                    payload = {
                        "receipt_id": str(existing["receipt_id"]),
                        "edit_request_id": edit_request_id,
                        "workspace_id": workspace.workspace_id,
                        "host_session_id": str(existing["host_session_id"]),
                        "principal_hash": principal_hash,
                        "mcp_session_hash": mcp_session_hash,
                        "edit_hash": str(existing["edit_hash"]),
                        "issued_at_us": int(existing["issued_at_us"]),
                        "expires_at_us": int(existing["expires_at_us"]),
                    }
                    token = self._token(payload)
                    if not hmac.compare_digest(
                        str(existing["receipt_token_hash"]), _sha256_text(token)
                    ):
                        raise EditBridgeError(
                            "CAPABILITY_DEGRADED", "receipt state is inconsistent"
                        )
                    expires_at_us = int(existing["expires_at_us"])
                connection.commit()
                return EditReceipt(
                    edit_request_id=edit_request_id,
                    receipt=token,
                    expires_at=_datetime_from_us(expires_at_us),
                )
            except EditBridgeError:
                if connection.in_transaction:
                    connection.rollback()
                raise
            except Exception as error:
                if connection.in_transaction:
                    connection.rollback()
                raise EditBridgeError(
                    "CAPABILITY_DEGRADED", "edit preflight failed"
                ) from error
            finally:
                connection.close()

    async def issue_receipt(
        self,
        workspace: Workspace,
        scope: InvocationScope,
        *,
        edit_request_id: str,
        description: str,
    ) -> EditReceipt:
        return await self._run(
            lambda: self._issue_receipt_sync(
                workspace, scope, edit_request_id, description
            )
        )

    def _stage_receipt_sync(
        self,
        workspace: Workspace,
        identity: BridgeIdentity,
        host_session_id: str,
        edit_request_id: str,
        token: str,
    ) -> None:
        payload = self._decode_token(token)
        now_us = _datetime_us(self.clock())
        if payload["workspace_id"] != workspace.workspace_id:
            raise EditBridgeError("TOKEN_SCOPE_MISMATCH", "workspace changed")
        if payload["host_session_id"] != host_session_id:
            raise EditBridgeError("TOKEN_SCOPE_MISMATCH", "host session changed")
        if payload["edit_request_id"] != edit_request_id:
            raise EditBridgeError("TOKEN_ARGUMENT_MISMATCH", "edit request changed")
        if (
            not isinstance(payload["expires_at_us"], int)
            or payload["expires_at_us"] <= now_us
        ):
            raise EditBridgeError("TOKEN_EXPIRED", "edit receipt expired")
        with self.storage_resolver.locked_active(workspace) as active:
            connection = _open_database(active.path)
            try:
                connection.execute("BEGIN IMMEDIATE")
                self._validate_host(
                    connection,
                    workspace_id=workspace.workspace_id,
                    identity=identity,
                    host_session_id=host_session_id,
                    now_us=now_us,
                )
                row = connection.execute(
                    "SELECT * FROM native_edit_receipts WHERE receipt_id=?",
                    (payload["receipt_id"],),
                ).fetchone()
                if row is None:
                    raise EditBridgeError("NOT_FOUND", "edit receipt unavailable")
                for name in (
                    "workspace_id",
                    "host_session_id",
                    "principal_hash",
                    "mcp_session_hash",
                    "edit_hash",
                ):
                    if not hmac.compare_digest(str(row[name]), str(payload[name])):
                        raise EditBridgeError("TOKEN_TAMPERED", "edit receipt rejected")
                if not hmac.compare_digest(
                    str(row["receipt_token_hash"]), _sha256_text(token)
                ):
                    raise EditBridgeError("TOKEN_TAMPERED", "edit receipt rejected")
                if row["consumed_at_us"] is not None:
                    raise EditBridgeError("TOKEN_REPLAYED", "edit receipt was consumed")
                connection.execute(
                    "UPDATE native_edit_receipts SET staged_at_us=COALESCE(staged_at_us,?) "
                    "WHERE receipt_id=?",
                    (now_us, payload["receipt_id"]),
                )
                connection.commit()
            except EditBridgeError:
                if connection.in_transaction:
                    connection.rollback()
                raise
            except Exception:
                if connection.in_transaction:
                    connection.rollback()
                raise EditBridgeError(
                    "CAPABILITY_DEGRADED", "receipt staging failed"
                ) from None
            finally:
                connection.close()

    async def stage_actual_mcp_response(
        self,
        workspace: Workspace,
        identity: BridgeIdentity,
        *,
        host_session_id: str,
        edit_request_id: str,
        receipt: str,
    ) -> None:
        await self._run(
            lambda: self._stage_receipt_sync(
                workspace, identity, host_session_id, edit_request_id, receipt
            )
        )

    def _consume_sync(
        self,
        workspace: Workspace,
        identity: BridgeIdentity,
        host_session_id: str,
        edit_request_id: str,
        edit: NativeEditRequest,
    ) -> EditApproval:
        now = self.clock()
        now_us = _datetime_us(now)
        with self.storage_resolver.locked_active(workspace) as active:
            connection = _open_database(active.path)
            try:
                connection.execute("BEGIN IMMEDIATE")
                self._validate_host(
                    connection,
                    workspace_id=workspace.workspace_id,
                    identity=identity,
                    host_session_id=host_session_id,
                    now_us=now_us,
                )
                row = connection.execute(
                    "SELECT receipt.*,pending.tool_name,pending.arguments_hash,"
                    "pending.preimages_json FROM native_edit_receipts AS receipt "
                    "JOIN native_edit_pending AS pending ON "
                    "pending.pending_edit_id=receipt.pending_edit_id WHERE "
                    "receipt.pending_edit_id=? AND receipt.workspace_id=? AND "
                    "receipt.host_session_id=?",
                    (edit_request_id, workspace.workspace_id, host_session_id),
                ).fetchone()
                if row is None:
                    raise EditBridgeError("NOT_FOUND", "staged edit unavailable")
                if int(row["expires_at_us"]) <= now_us:
                    raise EditBridgeError("TOKEN_EXPIRED", "edit receipt expired")
                if row["staged_at_us"] is None:
                    raise EditBridgeError("TOKEN_MISSING", "edit receipt is not staged")
                if row["consumed_at_us"] is not None:
                    raise EditBridgeError("TOKEN_REPLAYED", "edit receipt was consumed")
                if (
                    str(row["tool_name"]) != edit.tool_name
                    or not hmac.compare_digest(
                        str(row["arguments_hash"]), edit.arguments_hash
                    )
                    or not hmac.compare_digest(str(row["edit_hash"]), edit.edit_hash)
                    or canonical_json_bytes(
                        [item.canonical() for item in edit.preimages]
                    ).decode("utf-8")
                    != str(row["preimages_json"])
                ):
                    raise EditBridgeError(
                        "TOKEN_ARGUMENT_MISMATCH", "edit or preimage changed"
                    )
                updated = connection.execute(
                    "UPDATE native_edit_receipts SET consumed_at_us=? WHERE "
                    "pending_edit_id=? AND consumed_at_us IS NULL AND staged_at_us IS NOT NULL",
                    (now_us, edit_request_id),
                )
                if updated.rowcount != 1:
                    raise EditBridgeError("TOKEN_REPLAYED", "edit receipt was consumed")
                connection.commit()
                return EditApproval(
                    edit_request_id=edit_request_id,
                    allowed=True,
                    consumed_at=now,
                )
            except EditBridgeError:
                if connection.in_transaction:
                    connection.rollback()
                raise
            except Exception:
                if connection.in_transaction:
                    connection.rollback()
                raise EditBridgeError(
                    "CAPABILITY_DEGRADED", "edit approval failed"
                ) from None
            finally:
                connection.close()

    async def consume_retry(
        self,
        workspace: Workspace,
        identity: BridgeIdentity,
        *,
        host_session_id: str,
        edit_request_id: str,
        edit: NativeEditRequest,
    ) -> EditApproval:
        return await self._run(
            lambda: self._consume_sync(
                workspace,
                identity,
                host_session_id,
                edit_request_id,
                edit,
            )
        )


__all__ = [
    "BridgeCredentialRegistry",
    "BridgeIdentity",
    "BridgeTransport",
    "EDIT_RECEIPT_TTL_SECONDS",
    "EditApproval",
    "EditApprovalBroker",
    "EditBridgeError",
    "EditReceipt",
    "FilePreimage",
    "HostSession",
    "MAX_BRIDGE_BODY_BYTES",
    "NativeEditRequest",
    "PendingEdit",
]
