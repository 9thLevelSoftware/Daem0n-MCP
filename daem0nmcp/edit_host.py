"""Host-only bridge configuration and cross-process native-edit state."""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import ssl
import stat
import urllib.parse
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Literal

from .config import Settings
from .edit_bridge import BridgeIdentity
from .edit_bridge_transport import (
    EDIT_BRIDGE_CREDENTIAL_FILE_ENV,
    EDIT_BRIDGE_MODE_ENV,
    EDIT_BRIDGE_RUNTIME_DIR_ENV,
    LocalBridgeClient,
    RemoteBridgeHTTPSClient,
    load_bridge_credential,
    local_authority_principal,
    local_bridge_address,
    local_bridge_authkey,
    provision_bridge_credential,
)
from .event_store import canonical_json_bytes, sha256_json
from .protected_files import (
    ProtectedPathError,
    ensure_owner_only_directory,
    reject_linked_ancestry,
    verify_owner_only_file,
    write_new_owner_only_file,
)

EDIT_BRIDGE_REMOTE_URL_ENV = "DAEM0NMCP_EDIT_BRIDGE_REMOTE_URL"
EDIT_BRIDGE_CA_FILE_ENV = "DAEM0NMCP_EDIT_BRIDGE_CA_FILE"
EDIT_BRIDGE_ORIGIN_ENV = "DAEM0NMCP_EDIT_BRIDGE_ORIGIN"
EDIT_HOST_STATE_FILE_ENV = "DAEM0NMCP_EDIT_HOST_STATE_FILE"
EDIT_HOST_WORKSPACE_BINDING_FILE_ENV = "DAEM0NMCP_EDIT_HOST_WORKSPACE_BINDING_FILE"

_OPAQUE_ID = re.compile(r"^(?:hst|edt)_[0-9a-f]{64}$")
_HASH = re.compile(r"^[0-9a-f]{64}$")
_WORKSPACE_ID = re.compile(r"^ws_[0-9a-f]{24}$")
_TOOL_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9_.:-]{0,79}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_MAX_CA_BYTES = 1024 * 1024


class EditHostStateError(RuntimeError):
    code = "EDIT_HOST_STATE_UNAVAILABLE"


def _canonical_project_root(value: str | Path) -> Path:
    path = Path(value)
    reject_linked_ancestry(path)
    resolved = path.resolve(strict=True)
    if not resolved.is_dir():
        raise ValueError("edit host project root is not a directory")
    return resolved


def _canonical_root_text(value: str | Path) -> str:
    return os.path.normcase(os.path.normpath(str(_canonical_project_root(value))))


def _remote_binding_name(root: str | Path) -> str:
    root_hash = hashlib.sha256(_canonical_root_text(root).encode("utf-8")).hexdigest()[
        :24
    ]
    return f"workspace-{root_hash}.json"


@dataclass(frozen=True, slots=True)
class DirectoryIdentity:
    scheme: Literal["posix-dev-inode", "windows-volume-file-id"]
    volume: str
    file_id: str

    def __post_init__(self) -> None:
        if self.scheme == "windows-volume-file-id":
            valid = bool(
                re.fullmatch(r"[0-9a-f]{16}", self.volume)
                and re.fullmatch(r"[0-9a-f]{32}", self.file_id)
            )
        else:
            valid = bool(
                re.fullmatch(r"[0-9a-f]+", self.volume)
                and re.fullmatch(r"[0-9a-f]+", self.file_id)
            )
        if not valid:
            raise ValueError("remote workspace directory identity is invalid")

    def canonical(self) -> dict[str, str]:
        return {
            "scheme": self.scheme,
            "volume": self.volume,
            "file_id": self.file_id,
        }


if os.name == "nt":
    import ctypes
    from ctypes import wintypes

    _KERNEL32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _FILE_READ_ATTRIBUTES = 0x0080
    _FILE_SHARE_ALL = 0x00000001 | 0x00000002 | 0x00000004
    _OPEN_EXISTING = 3
    _FILE_FLAG_BACKUP_SEMANTICS = 0x02000000
    _FILE_FLAG_OPEN_REPARSE_POINT = 0x00200000
    _FILE_ID_INFO_CLASS = 18
    _FILE_ATTRIBUTE_TAG_INFO_CLASS = 9
    _FILE_ATTRIBUTE_REPARSE_POINT = 0x00000400
    _INVALID_HANDLE_VALUE = wintypes.HANDLE(-1).value

    class _FileId128(ctypes.Structure):
        _fields_ = [("Identifier", ctypes.c_ubyte * 16)]

    class _FileIdInfo(ctypes.Structure):
        _fields_ = [
            ("VolumeSerialNumber", ctypes.c_ulonglong),
            ("FileId", _FileId128),
        ]

    class _FileAttributeTagInfo(ctypes.Structure):
        _fields_ = [
            ("FileAttributes", wintypes.DWORD),
            ("ReparseTag", wintypes.DWORD),
        ]

    _KERNEL32.CreateFileW.argtypes = (
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    )
    _KERNEL32.CreateFileW.restype = wintypes.HANDLE
    _KERNEL32.GetFileInformationByHandleEx.argtypes = (
        wintypes.HANDLE,
        ctypes.c_int,
        wintypes.LPVOID,
        wintypes.DWORD,
    )
    _KERNEL32.GetFileInformationByHandleEx.restype = wintypes.BOOL
    _KERNEL32.CloseHandle.argtypes = (wintypes.HANDLE,)
    _KERNEL32.CloseHandle.restype = wintypes.BOOL


def _directory_identity(value: str | Path) -> DirectoryIdentity:
    root = _canonical_project_root(value)
    if os.name == "nt":
        handle = _KERNEL32.CreateFileW(  # type: ignore[name-defined]
            str(root),
            _FILE_READ_ATTRIBUTES,  # type: ignore[name-defined]
            _FILE_SHARE_ALL,  # type: ignore[name-defined]
            None,
            _OPEN_EXISTING,  # type: ignore[name-defined]
            _FILE_FLAG_BACKUP_SEMANTICS  # type: ignore[name-defined]
            | _FILE_FLAG_OPEN_REPARSE_POINT,  # type: ignore[name-defined]
            None,
        )
        if handle == _INVALID_HANDLE_VALUE:  # type: ignore[name-defined]
            raise ValueError("remote workspace root identity is unavailable")
        try:
            attributes = _FileAttributeTagInfo()  # type: ignore[name-defined]
            if (
                not _KERNEL32.GetFileInformationByHandleEx(  # type: ignore[name-defined]
                    handle,
                    _FILE_ATTRIBUTE_TAG_INFO_CLASS,  # type: ignore[name-defined]
                    ctypes.byref(attributes),  # type: ignore[name-defined]
                    ctypes.sizeof(attributes),  # type: ignore[name-defined]
                )
                or attributes.FileAttributes & _FILE_ATTRIBUTE_REPARSE_POINT
            ):  # type: ignore[name-defined]
                raise ValueError("remote workspace root is a reparse point")
            information = _FileIdInfo()  # type: ignore[name-defined]
            if not _KERNEL32.GetFileInformationByHandleEx(  # type: ignore[name-defined]
                handle,
                _FILE_ID_INFO_CLASS,  # type: ignore[name-defined]
                ctypes.byref(information),  # type: ignore[name-defined]
                ctypes.sizeof(information),  # type: ignore[name-defined]
            ):
                raise ValueError("remote workspace root identity is unavailable")
            return DirectoryIdentity(
                "windows-volume-file-id",
                f"{information.VolumeSerialNumber:016x}",
                bytes(information.FileId.Identifier).hex(),
            )
        finally:
            _KERNEL32.CloseHandle(handle)  # type: ignore[name-defined]
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    descriptor = os.open(root, flags)
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISDIR(metadata.st_mode):
            raise ValueError("remote workspace root is not a directory")
        return DirectoryIdentity(
            "posix-dev-inode", f"{metadata.st_dev:x}", f"{metadata.st_ino:x}"
        )
    finally:
        os.close(descriptor)


def _normalized_https_origin(value: str, *, label: str) -> str:
    try:
        parsed = urllib.parse.urlsplit(value)
        port = parsed.port
    except ValueError:
        raise ValueError(f"{label} is invalid") from None
    if (
        parsed.scheme.casefold() != "https"
        or parsed.hostname is None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
        or port == 0
    ):
        raise ValueError(f"{label} is invalid")
    try:
        hostname = parsed.hostname.encode("idna").decode("ascii").casefold()
    except UnicodeError:
        raise ValueError(f"{label} is invalid") from None
    if not hostname or any(
        character.isspace() or ord(character) < 32 or ord(character) == 127
        for character in hostname
    ):
        raise ValueError(f"{label} is invalid")
    if ":" in hostname:
        hostname = f"[{hostname}]"
    authority = hostname if port in {None, 443} else f"{hostname}:{port}"
    return f"https://{authority}"


def _normalized_remote_url(value: str) -> str:
    return _normalized_https_origin(value, label="remote edit bridge URL")


def _normalized_origin(value: str | None) -> str | None:
    if value is None:
        return None
    return _normalized_https_origin(value, label="remote edit bridge Origin")


def _ca_sha256(path: str | Path) -> tuple[Path, str, bytes]:
    configured = Path(path)
    reject_linked_ancestry(configured)
    resolved = configured.resolve(strict=True)
    metadata = resolved.stat()
    if not stat.S_ISREG(metadata.st_mode) or not 0 < metadata.st_size <= _MAX_CA_BYTES:
        raise ValueError("remote bridge CA file is invalid")
    try:
        contents = resolved.read_bytes()
    except OSError as exc:
        raise ValueError("remote bridge CA file is invalid") from exc
    if len(contents) != metadata.st_size:
        raise ValueError("remote bridge CA file changed while reading")
    return resolved, hashlib.sha256(contents).hexdigest(), contents


def _ssl_ca_data(contents: bytes) -> str | bytes:
    if contents.lstrip().startswith(b"-----BEGIN"):
        try:
            return contents.decode("ascii")
        except UnicodeDecodeError:
            raise ValueError("remote bridge CA file is invalid") from None
    return contents


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for name, item in pairs:
        if name in value:
            raise ValueError("duplicate JSON member")
        value[name] = item
    return value


@dataclass(frozen=True, slots=True)
class RemoteWorkspaceBinding:
    """Owner-configured local checkout to remote opaque workspace binding."""

    local_root: Path
    root_identity: DirectoryIdentity
    workspace_id: str
    credential_id: str
    remote_base_url: str
    ca_file: Path
    ca_sha256: str
    origin: str | None
    binding_path: Path

    def __post_init__(self) -> None:
        if not _WORKSPACE_ID.fullmatch(self.workspace_id):
            raise ValueError("remote workspace ID is invalid")
        if (
            not self.credential_id
            or len(self.credential_id) > 128
            or any(
                ord(character) < 32 or ord(character) == 127
                for character in self.credential_id
            )
        ):
            raise ValueError("remote workspace credential ID is invalid")
        if not _SHA256.fullmatch(self.ca_sha256):
            raise ValueError("remote workspace CA digest is invalid")

    def matches(self, project_root: str | Path) -> bool:
        return (
            _canonical_root_text(project_root) == _canonical_root_text(self.local_root)
            and _directory_identity(project_root) == self.root_identity
        )

    def validate_recipient(
        self,
        *,
        remote_base_url: str | None = None,
        ca_file: str | Path | None = None,
        origin: str | None = None,
    ) -> tuple[Path, bytes]:
        if (
            remote_base_url is not None
            and _normalized_remote_url(remote_base_url) != self.remote_base_url
        ):
            raise ValueError("remote edit bridge authority conflicts with binding")
        if origin is not None and _normalized_origin(origin) != self.origin:
            raise ValueError("remote edit bridge Origin conflicts with binding")
        selected_ca = self.ca_file if ca_file is None else Path(ca_file)
        resolved_ca, digest, contents = _ca_sha256(selected_ca)
        if resolved_ca != self.ca_file or digest != self.ca_sha256:
            raise ValueError("remote edit bridge CA conflicts with binding")
        return resolved_ca, contents


def _load_remote_workspace_binding(path: str | Path) -> RemoteWorkspaceBinding:
    binding_path = verify_owner_only_file(path, max_bytes=4096)
    try:
        value = json.loads(
            binding_path.read_text(encoding="utf-8"),
            object_pairs_hook=_unique_object,
        )
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
        raise ValueError("remote workspace binding is invalid") from exc
    if (
        not isinstance(value, dict)
        or set(value)
        != {
            "version",
            "local_root",
            "root_identity",
            "workspace_id",
            "credential_id",
            "remote_base_url",
            "ca_file",
            "ca_sha256",
            "origin",
        }
        or value.get("version") != 2
        or not isinstance(value.get("local_root"), str)
        or not isinstance(value.get("root_identity"), dict)
        or not isinstance(value.get("workspace_id"), str)
        or not isinstance(value.get("credential_id"), str)
        or not isinstance(value.get("remote_base_url"), str)
        or not isinstance(value.get("ca_file"), str)
        or not isinstance(value.get("ca_sha256"), str)
        or value.get("origin") is not None
        and not isinstance(value.get("origin"), str)
    ):
        raise ValueError("remote workspace binding is invalid")
    local_root = _canonical_project_root(value["local_root"])
    if value["local_root"] != _canonical_root_text(local_root):
        raise ValueError("remote workspace binding root is not canonical")
    workspace_id = value["workspace_id"]
    if not _WORKSPACE_ID.fullmatch(workspace_id):
        raise ValueError("remote workspace binding is invalid")
    identity = value["root_identity"]
    if (
        set(identity) != {"scheme", "volume", "file_id"}
        or identity.get("scheme") not in {"posix-dev-inode", "windows-volume-file-id"}
        or not isinstance(identity.get("volume"), str)
        or not isinstance(identity.get("file_id"), str)
    ):
        raise ValueError("remote workspace binding identity is invalid")
    root_identity = DirectoryIdentity(
        identity["scheme"], identity["volume"], identity["file_id"]
    )
    ca_file, actual_ca_digest, _ca_contents = _ca_sha256(value["ca_file"])
    if value["ca_file"] != str(ca_file) or value["ca_sha256"] != actual_ca_digest:
        raise ValueError("remote workspace binding CA changed")
    binding = RemoteWorkspaceBinding(
        local_root=local_root,
        root_identity=root_identity,
        workspace_id=workspace_id,
        credential_id=value["credential_id"],
        remote_base_url=_normalized_remote_url(value["remote_base_url"]),
        ca_file=ca_file,
        ca_sha256=value["ca_sha256"],
        origin=_normalized_origin(value["origin"]),
        binding_path=binding_path,
    )
    if value["remote_base_url"] != binding.remote_base_url:
        raise ValueError("remote workspace binding URL is not canonical")
    if value["origin"] != binding.origin:
        raise ValueError("remote workspace binding Origin is not canonical")
    if not binding.matches(local_root):
        raise ValueError("remote workspace root identity changed")
    binding.validate_recipient()
    return binding


@dataclass(frozen=True, slots=True)
class LocalBridgeInstallation:
    """Host-only files and public environment paths for one local authority."""

    credential_path: Path
    runtime_directory: Path
    project_root: Path
    storage_path: Path
    principal_id: str
    created: bool

    def environment(self) -> dict[str, str]:
        return {
            EDIT_BRIDGE_CREDENTIAL_FILE_ENV: str(self.credential_path),
            EDIT_BRIDGE_RUNTIME_DIR_ENV: str(self.runtime_directory),
            "DAEM0NMCP_PROJECT_ROOT": str(self.project_root),
            "DAEM0NMCP_STORAGE_PATH": str(self.storage_path),
        }


@dataclass(frozen=True, slots=True)
class RemoteBridgeInstallation:
    """Protected client configuration for one remote workspace checkout."""

    credential_path: Path
    project_root: Path
    workspace_id: str
    binding_path: Path
    remote_base_url: str
    ca_file: Path
    origin: str | None
    created: bool

    def environment(self) -> dict[str, str]:
        environment = {
            EDIT_BRIDGE_CREDENTIAL_FILE_ENV: str(self.credential_path),
            EDIT_BRIDGE_MODE_ENV: "remote-https",
            EDIT_HOST_WORKSPACE_BINDING_FILE_ENV: str(self.binding_path),
            "DAEM0NMCP_PROJECT_ROOT": str(self.project_root),
        }
        return environment


BridgeInstallation = LocalBridgeInstallation | RemoteBridgeInstallation


def provision_remote_bridge_installation(
    project_root: str | Path,
    *,
    credential_path: str | Path,
    remote_base_url: str,
    ca_file: str | Path,
    workspace_id: str,
    origin: str | None = None,
    binding_path: str | Path | None = None,
) -> RemoteBridgeInstallation:
    """Create an immutable protected local-root to remote-workspace binding."""

    root = _canonical_project_root(project_root)
    root_identity = _directory_identity(root)
    if not _WORKSPACE_ID.fullmatch(workspace_id):
        raise ValueError("remote workspace ID is invalid")
    configured_credential = verify_owner_only_file(credential_path, max_bytes=4096)
    _, identity = load_bridge_credential(configured_credential)
    if "remote-https" not in identity.transports:
        raise ValueError("credential does not authorize remote HTTPS")
    normalized_url = _normalized_remote_url(remote_base_url)
    normalized_origin = _normalized_origin(origin)
    configured_ca, ca_digest, ca_contents = _ca_sha256(ca_file)
    # Validate transport inputs before creating any persistent binding.
    RemoteBridgeHTTPSClient(
        base_url=normalized_url,
        bearer="x" * 32,
        ssl_context=ssl.create_default_context(cadata=_ssl_ca_data(ca_contents)),
        origin=normalized_origin,
    )
    if binding_path is None:
        target = configured_credential.parent / _remote_binding_name(root)
    else:
        target = Path(binding_path)
    reject_linked_ancestry(target)
    target = target.absolute()
    expected_target = configured_credential.parent / _remote_binding_name(root)
    if target.resolve(strict=False) != expected_target.resolve(strict=False):
        raise ValueError("remote workspace binding path is not canonical")
    payload = canonical_json_bytes(
        {
            "version": 2,
            "local_root": _canonical_root_text(root),
            "root_identity": root_identity.canonical(),
            "workspace_id": workspace_id,
            "credential_id": identity.credential_id,
            "remote_base_url": normalized_url,
            "ca_file": str(configured_ca),
            "ca_sha256": ca_digest,
            "origin": normalized_origin,
        }
    )
    created = False
    if target.exists():
        existing = _load_remote_workspace_binding(target)
        if (
            not existing.matches(root)
            or existing.root_identity != root_identity
            or existing.workspace_id != workspace_id
            or existing.credential_id != identity.credential_id
            or existing.remote_base_url != normalized_url
            or existing.ca_file != configured_ca
            or existing.ca_sha256 != ca_digest
            or existing.origin != normalized_origin
        ):
            raise ValueError("existing remote workspace binding conflicts")
    else:
        write_new_owner_only_file(target, payload)
        created = True
    binding = _load_remote_workspace_binding(target)
    return RemoteBridgeInstallation(
        credential_path=configured_credential,
        project_root=root,
        workspace_id=workspace_id,
        binding_path=binding.binding_path,
        remote_base_url=normalized_url,
        ca_file=configured_ca,
        origin=normalized_origin,
        created=created,
    )


def provision_client_bridge_installation(
    project_root: str | Path,
    *,
    config_root: Path | None = None,
    remote_workspace_id: str | None = None,
    remote_credential_path: str | Path | None = None,
    remote_base_url: str | None = None,
    remote_ca_file: str | Path | None = None,
    remote_origin: str | None = None,
) -> BridgeInstallation:
    """Select local pairing or an explicit complete remote host binding."""

    remote_values = (
        remote_workspace_id,
        remote_credential_path,
        remote_base_url,
        remote_ca_file,
        remote_origin,
    )
    if not any(value is not None for value in remote_values):
        return provision_local_bridge_installation(
            project_root, config_root=config_root
        )
    if config_root is not None:
        raise ValueError("local bridge config root cannot be used for remote pairing")
    if (
        remote_workspace_id is None
        or remote_credential_path is None
        or remote_base_url is None
        or remote_ca_file is None
    ):
        raise ValueError("remote edit bridge pairing is incomplete")
    return provision_remote_bridge_installation(
        project_root,
        credential_path=remote_credential_path,
        remote_base_url=remote_base_url,
        ca_file=remote_ca_file,
        workspace_id=remote_workspace_id,
        origin=remote_origin,
    )


def local_bridge_layout(
    root: Path, config_root: Path | None
) -> tuple[Path, str, Path, Path]:
    """Return storage path, principal, credential dir and socket dir for *root*."""

    settings = Settings(project_root=str(root))
    storage_path = Path(settings.get_storage_path()).resolve(strict=False)
    principal_id = local_authority_principal(storage_path)
    authority_id = principal_id.removeprefix("process-authority:")
    base = (
        config_root
        if config_root is not None
        else Path.home() / ".daem0nmcp" / "edit-bridges"
    )
    reject_linked_ancestry(base)
    # AF_UNIX socket paths are limited to 104-108 bytes, and the authority
    # directory name alone is 64 characters, so sockets live in a short
    # sibling directory instead.
    return (
        storage_path,
        principal_id,
        base.absolute() / authority_id,
        base.absolute() / "run" / authority_id[:16],
    )


def provision_local_bridge_installation(
    project_root: str | Path,
    *,
    config_root: Path | None = None,
) -> LocalBridgeInstallation:
    """Provision or validate the local host credential outside the workspace."""

    root = Path(project_root).resolve(strict=True)
    storage_path, principal_id, authority, runtime_directory = local_bridge_layout(
        root, config_root
    )
    authority_directory = ensure_owner_only_directory(authority)
    credential_path = authority_directory / "credential.json"
    created = False
    if credential_path.exists():
        _, identity = load_bridge_credential(credential_path)
        if (
            identity.principal_id != principal_id
            or "local-ipc" not in identity.transports
        ):
            raise ValueError("existing edit bridge credential does not match project")
    else:
        provision_bridge_credential(
            credential_path,
            principal_id=principal_id,
            transports=frozenset({"local-ipc"}),
        )
        created = True
    return LocalBridgeInstallation(
        credential_path=credential_path.resolve(strict=True),
        runtime_directory=runtime_directory.resolve(strict=False),
        project_root=root,
        storage_path=storage_path,
        principal_id=principal_id,
        created=created,
    )


def _utc_us(value: datetime) -> int:
    if value.tzinfo is None:
        raise ValueError("host expiry must be timezone aware")
    return int(value.astimezone(timezone.utc).timestamp() * 1_000_000)


def _from_us(value: int) -> datetime:
    return datetime.fromtimestamp(value / 1_000_000, tz=timezone.utc)


def _hash(value: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError("host binding value is required")
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _binding_key(workspace_id: str, native_session_id: str, credential_id: str) -> str:
    if not _WORKSPACE_ID.fullmatch(workspace_id):
        raise ValueError("workspace ID is invalid")
    return sha256_json(
        [
            "daem0nmcp",
            "edit-host-binding-v1",
            workspace_id,
            _hash(native_session_id),
            _hash(credential_id),
        ]
    )


@dataclass(frozen=True, slots=True)
class EditHostConfig:
    credential_path: Path
    identity: BridgeIdentity
    secret: str = field(repr=False)
    mode: Literal["local", "remote-https"] = "local"
    runtime_directory: Path | None = None
    remote_base_url: str | None = None
    ca_file: Path | None = None
    origin: str | None = None
    state_file: Path | None = None
    remote_workspace: RemoteWorkspaceBinding | None = None

    @classmethod
    def from_environment(cls, environ: Mapping[str, str]) -> EditHostConfig:
        configured = environ.get(EDIT_BRIDGE_CREDENTIAL_FILE_ENV)
        if configured is None:
            raise ValueError("edit bridge credential file is not configured")
        configured_path = Path(configured)
        secret, identity = load_bridge_credential(configured_path)
        credential_path = configured_path.resolve(strict=True)
        mode = environ.get(EDIT_BRIDGE_MODE_ENV, "local")
        configured_state_file = Path(
            environ.get(
                EDIT_HOST_STATE_FILE_ENV,
                str(credential_path.parent / "edit-host-state.sqlite3"),
            )
        )
        reject_linked_ancestry(configured_state_file)
        state_file = configured_state_file.resolve(strict=False)
        if state_file.parent != credential_path.parent:
            raise ValueError("edit host state must remain beside the credential")
        if mode == "local":
            if "local-ipc" not in identity.transports:
                raise ValueError("credential does not authorize local IPC")
            runtime = Path(
                environ.get(
                    EDIT_BRIDGE_RUNTIME_DIR_ENV,
                    str(credential_path.parent / "run"),
                )
            ).resolve(strict=False)
            return cls(
                credential_path=credential_path,
                identity=identity,
                secret=secret,
                mode="local",
                runtime_directory=runtime,
                state_file=state_file,
            )
        if mode != "remote-https" or "remote-https" not in identity.transports:
            raise ValueError("edit bridge mode is invalid")
        remote_url = environ.get(EDIT_BRIDGE_REMOTE_URL_ENV)
        ca_file = environ.get(EDIT_BRIDGE_CA_FILE_ENV)
        origin = environ.get(EDIT_BRIDGE_ORIGIN_ENV)
        binding_file = environ.get(EDIT_HOST_WORKSPACE_BINDING_FILE_ENV)
        if binding_file is None:
            raise ValueError("remote edit bridge client configuration is incomplete")
        remote_workspace = _load_remote_workspace_binding(binding_file)
        if (
            remote_workspace.credential_id != identity.credential_id
            or remote_workspace.binding_path.parent != credential_path.parent
            or remote_workspace.binding_path
            != credential_path.parent
            / _remote_binding_name(remote_workspace.local_root)
        ):
            raise ValueError("remote workspace binding credential does not match")
        selected_ca, _ca_contents = remote_workspace.validate_recipient(
            remote_base_url=remote_url,
            ca_file=ca_file,
            origin=origin,
        )
        return cls(
            credential_path=credential_path,
            identity=identity,
            secret=secret,
            mode="remote-https",
            remote_base_url=remote_workspace.remote_base_url,
            ca_file=selected_ca,
            origin=remote_workspace.origin,
            state_file=state_file,
            remote_workspace=remote_workspace,
        )

    def workspace_id(self, project_root: str | Path) -> str:
        """Resolve the exact server workspace authorized for this checkout."""

        if self.mode == "local":
            from .workspace import WorkspaceRegistry

            return WorkspaceRegistry(default_root=project_root).default.workspace_id
        binding = self.remote_workspace
        if binding is None or not binding.matches(project_root):
            raise ValueError("remote workspace binding does not match project root")
        return binding.workspace_id

    def build_client(self) -> LocalBridgeClient | RemoteBridgeHTTPSClient:
        if self.mode == "local":
            if self.runtime_directory is None:
                raise ValueError("local bridge runtime directory is missing")
            return LocalBridgeClient(
                address=local_bridge_address(
                    self.runtime_directory, self.identity.credential_id
                ),
                authkey=local_bridge_authkey(self.secret),
                bearer=self.secret,
            )
        if self.remote_base_url is None or self.ca_file is None:
            raise ValueError("remote bridge client configuration is missing")
        binding = self.remote_workspace
        if (
            binding is None
            or not binding.matches(binding.local_root)
            or binding.remote_base_url != self.remote_base_url
            or binding.origin != self.origin
        ):
            raise ValueError("remote bridge client binding changed")
        ca_file, ca_contents = binding.validate_recipient(
            remote_base_url=self.remote_base_url,
            ca_file=self.ca_file,
            origin=self.origin,
        )
        return RemoteBridgeHTTPSClient(
            base_url=self.remote_base_url,
            bearer=self.secret,
            ssl_context=ssl.create_default_context(cadata=_ssl_ca_data(ca_contents)),
            origin=self.origin,
        )


@dataclass(frozen=True, slots=True)
class CreatedHostSession:
    host_session_id: str
    expires_at: datetime

    def __post_init__(self) -> None:
        if not _OPAQUE_ID.fullmatch(
            self.host_session_id
        ) or not self.host_session_id.startswith("hst_"):
            raise ValueError("host session ID is invalid")
        _utc_us(self.expires_at)


@dataclass(frozen=True, slots=True)
class HostSessionBinding:
    workspace_id: str
    host_session_id: str
    expires_at: datetime

    def __post_init__(self) -> None:
        if (
            not _WORKSPACE_ID.fullmatch(self.workspace_id)
            or not _OPAQUE_ID.fullmatch(self.host_session_id)
            or not self.host_session_id.startswith("hst_")
        ):
            raise ValueError("host session binding is invalid")
        _utc_us(self.expires_at)


@dataclass(frozen=True, slots=True)
class PendingEditBinding:
    workspace_id: str
    host_session_id: str
    edit_request_id: str
    edit_hash: str
    expires_at: datetime

    def __post_init__(self) -> None:
        if (
            not _WORKSPACE_ID.fullmatch(self.workspace_id)
            or not _OPAQUE_ID.fullmatch(self.host_session_id)
            or not self.host_session_id.startswith("hst_")
            or not _OPAQUE_ID.fullmatch(self.edit_request_id)
            or not self.edit_request_id.startswith("edt_")
            or not _HASH.fullmatch(self.edit_hash)
        ):
            raise ValueError("pending edit binding is invalid")
        _utc_us(self.expires_at)


class EditHostStateStore:
    """Owner-only SQLite state shared by short-lived native hook processes."""

    def __init__(self, config: EditHostConfig) -> None:
        if config.state_file is None:
            raise ValueError("edit host state file is missing")
        self._path = config.state_file
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self._path,
            timeout=5,
            isolation_level=None,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=5000")
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA synchronous=FULL")
        return connection

    def _initialize(self) -> None:
        parent = self._path.parent
        try:
            ensure_owner_only_directory(parent)
            if self._path.exists():
                verify_owner_only_file(self._path)
            else:
                try:
                    write_new_owner_only_file(self._path, b"")
                except FileExistsError:
                    verify_owner_only_file(self._path)
        except (OSError, ProtectedPathError) as exc:
            raise EditHostStateError("host state path is unsafe") from exc
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS host_sessions (
                    binding_key TEXT PRIMARY KEY,
                    workspace_id TEXT NOT NULL,
                    native_session_hash TEXT NOT NULL,
                    credential_id_hash TEXT NOT NULL,
                    host_session_id TEXT NOT NULL,
                    expires_at_us INTEGER NOT NULL,
                    updated_at_us INTEGER NOT NULL
                ) WITHOUT ROWID;
                CREATE TABLE IF NOT EXISTS pending_edits (
                    binding_key TEXT NOT NULL REFERENCES host_sessions(binding_key)
                        ON DELETE CASCADE,
                    edit_hash TEXT NOT NULL,
                    native_request_hash TEXT NOT NULL,
                    workspace_id TEXT NOT NULL,
                    host_session_id TEXT NOT NULL,
                    edit_request_id TEXT NOT NULL UNIQUE,
                    expires_at_us INTEGER NOT NULL,
                    status TEXT NOT NULL CHECK(status IN ('pending','consumed')),
                    consumed_at_us INTEGER,
                    updated_at_us INTEGER NOT NULL,
                    PRIMARY KEY(binding_key,edit_hash),
                    UNIQUE(binding_key,native_request_hash),
                    CHECK((status='pending' AND consumed_at_us IS NULL)
                       OR (status='consumed' AND consumed_at_us IS NOT NULL))
                ) WITHOUT ROWID;
                """
            )

    def load_or_create_session(
        self,
        *,
        workspace_id: str,
        native_session_id: str,
        credential_id: str,
        create: Callable[[], CreatedHostSession],
        now: datetime | None = None,
    ) -> HostSessionBinding:
        key = _binding_key(workspace_id, native_session_id, credential_id)
        native_hash = _hash(native_session_id)
        credential_hash = _hash(credential_id)
        now_us = _utc_us(now or datetime.now(timezone.utc))
        try:
            with self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                connection.execute(
                    "DELETE FROM host_sessions WHERE expires_at_us<=?", (now_us,)
                )
                row = connection.execute(
                    "SELECT * FROM host_sessions WHERE binding_key=?",
                    (key,),
                ).fetchone()
                if row is not None and int(row["expires_at_us"]) > now_us:
                    connection.commit()
                    return HostSessionBinding(
                        workspace_id=workspace_id,
                        host_session_id=str(row["host_session_id"]),
                        expires_at=_from_us(int(row["expires_at_us"])),
                    )
                created = create()
                if not isinstance(created, CreatedHostSession):
                    raise TypeError("session creator returned an invalid value")
                expires_at_us = _utc_us(created.expires_at)
                if expires_at_us <= now_us:
                    raise ValueError("created host session is already expired")
                connection.execute(
                    "INSERT INTO host_sessions(binding_key,workspace_id,"
                    "native_session_hash,credential_id_hash,host_session_id,"
                    "expires_at_us,updated_at_us) VALUES (?,?,?,?,?,?,?) "
                    "ON CONFLICT(binding_key) DO UPDATE SET host_session_id=excluded.host_session_id,"
                    "expires_at_us=excluded.expires_at_us,updated_at_us=excluded.updated_at_us",
                    (
                        key,
                        workspace_id,
                        native_hash,
                        credential_hash,
                        created.host_session_id,
                        expires_at_us,
                        now_us,
                    ),
                )
                connection.execute(
                    "DELETE FROM pending_edits WHERE binding_key=?", (key,)
                )
                connection.commit()
                return HostSessionBinding(
                    workspace_id=workspace_id,
                    host_session_id=created.host_session_id,
                    expires_at=created.expires_at.astimezone(timezone.utc),
                )
        except (OSError, sqlite3.Error) as exc:
            raise EditHostStateError("host session state unavailable") from exc

    def save_pending(
        self,
        *,
        native_session_id: str,
        native_request_id: str,
        credential_id: str,
        pending: PendingEditBinding,
        now: datetime | None = None,
    ) -> None:
        key = _binding_key(pending.workspace_id, native_session_id, credential_id)
        native_request_hash = _hash(native_request_id)
        now_us = _utc_us(now or datetime.now(timezone.utc))
        try:
            with self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                session = connection.execute(
                    "SELECT host_session_id,expires_at_us FROM host_sessions "
                    "WHERE binding_key=?",
                    (key,),
                ).fetchone()
                if (
                    session is None
                    or str(session["host_session_id"]) != pending.host_session_id
                    or int(session["expires_at_us"]) <= now_us
                    or _utc_us(pending.expires_at) <= now_us
                ):
                    raise ValueError("pending edit does not match a live host session")
                existing = connection.execute(
                    "SELECT status FROM pending_edits WHERE binding_key=? AND edit_hash=?",
                    (key, pending.edit_hash),
                ).fetchone()
                if existing is not None and str(existing["status"]) != "pending":
                    raise ValueError("consumed edit state cannot be replaced")
                updated = connection.execute(
                    "INSERT INTO pending_edits(binding_key,edit_hash,native_request_hash,"
                    "workspace_id,host_session_id,edit_request_id,expires_at_us,status,"
                    "consumed_at_us,updated_at_us) VALUES (?,?,?,?,?,?,?,?,?,?) "
                    "ON CONFLICT(binding_key,edit_hash) DO UPDATE "
                    "SET native_request_hash=excluded.native_request_hash,"
                    "host_session_id=excluded.host_session_id,"
                    "edit_request_id=excluded.edit_request_id,"
                    "expires_at_us=excluded.expires_at_us,status='pending',"
                    "consumed_at_us=NULL,updated_at_us=excluded.updated_at_us "
                    "WHERE pending_edits.status='pending'",
                    (
                        key,
                        pending.edit_hash,
                        native_request_hash,
                        pending.workspace_id,
                        pending.host_session_id,
                        pending.edit_request_id,
                        _utc_us(pending.expires_at),
                        "pending",
                        None,
                        now_us,
                    ),
                )
                if updated.rowcount != 1:
                    raise ValueError("pending edit state conflicted")
                connection.commit()
        except (OSError, sqlite3.Error) as exc:
            raise EditHostStateError("pending edit state unavailable") from exc

    def get_pending(
        self,
        *,
        workspace_id: str,
        native_session_id: str,
        credential_id: str,
        edit_hash: str,
        now: datetime | None = None,
    ) -> PendingEditBinding | None:
        if not _HASH.fullmatch(edit_hash):
            raise ValueError("edit hash is invalid")
        key = _binding_key(workspace_id, native_session_id, credential_id)
        now_us = _utc_us(now or datetime.now(timezone.utc))
        try:
            with self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                connection.execute(
                    "DELETE FROM pending_edits WHERE expires_at_us<=? AND status='pending'",
                    (now_us,),
                )
                row = connection.execute(
                    "SELECT * FROM pending_edits WHERE binding_key=? AND edit_hash=? "
                    "AND status='pending'",
                    (key, edit_hash),
                ).fetchone()
                connection.commit()
            if row is None:
                return None
            return PendingEditBinding(
                workspace_id=str(row["workspace_id"]),
                host_session_id=str(row["host_session_id"]),
                edit_request_id=str(row["edit_request_id"]),
                edit_hash=str(row["edit_hash"]),
                expires_at=_from_us(int(row["expires_at_us"])),
            )
        except (OSError, sqlite3.Error) as exc:
            raise EditHostStateError("pending edit state unavailable") from exc

    def get_pending_by_request(
        self,
        *,
        workspace_id: str,
        native_session_id: str,
        credential_id: str,
        edit_request_id: str,
        now: datetime | None = None,
    ) -> PendingEditBinding | None:
        if not _OPAQUE_ID.fullmatch(edit_request_id) or not edit_request_id.startswith(
            "edt_"
        ):
            raise ValueError("edit request ID is invalid")
        key = _binding_key(workspace_id, native_session_id, credential_id)
        now_us = _utc_us(now or datetime.now(timezone.utc))
        try:
            with self._connect() as connection:
                row = connection.execute(
                    "SELECT * FROM pending_edits WHERE binding_key=? AND edit_request_id=? "
                    "AND status='pending' AND expires_at_us>?",
                    (key, edit_request_id, now_us),
                ).fetchone()
            if row is None:
                return None
            return PendingEditBinding(
                workspace_id=str(row["workspace_id"]),
                host_session_id=str(row["host_session_id"]),
                edit_request_id=str(row["edit_request_id"]),
                edit_hash=str(row["edit_hash"]),
                expires_at=_from_us(int(row["expires_at_us"])),
            )
        except (OSError, sqlite3.Error) as exc:
            raise EditHostStateError("pending edit state unavailable") from exc

    def mark_consumed(
        self,
        *,
        workspace_id: str,
        native_session_id: str,
        native_request_id: str,
        credential_id: str,
        edit_request_id: str,
        now: datetime | None = None,
    ) -> PendingEditBinding:
        if not _OPAQUE_ID.fullmatch(edit_request_id) or not edit_request_id.startswith(
            "edt_"
        ):
            raise ValueError("edit request ID is invalid")
        key = _binding_key(workspace_id, native_session_id, credential_id)
        native_request_hash = _hash(native_request_id)
        now_us = _utc_us(now or datetime.now(timezone.utc))
        try:
            with self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                row = connection.execute(
                    "SELECT * FROM pending_edits WHERE binding_key=? AND edit_request_id=? "
                    "AND status='pending' AND expires_at_us>?",
                    (key, edit_request_id, now_us),
                ).fetchone()
                if row is None:
                    raise ValueError("live pending edit is unavailable")
                updated = connection.execute(
                    "UPDATE pending_edits SET native_request_hash=?,status='consumed',"
                    "consumed_at_us=?,updated_at_us=? WHERE binding_key=? AND edit_request_id=? "
                    "AND status='pending'",
                    (native_request_hash, now_us, now_us, key, edit_request_id),
                )
                if updated.rowcount != 1:
                    raise ValueError("pending edit consumption conflicted")
                connection.commit()
            return PendingEditBinding(
                workspace_id=str(row["workspace_id"]),
                host_session_id=str(row["host_session_id"]),
                edit_request_id=str(row["edit_request_id"]),
                edit_hash=str(row["edit_hash"]),
                expires_at=_from_us(int(row["expires_at_us"])),
            )
        except (OSError, sqlite3.Error) as exc:
            raise EditHostStateError("pending edit state unavailable") from exc

    def get_consumed(
        self,
        *,
        workspace_id: str,
        native_session_id: str,
        credential_id: str,
        native_request_id: str,
    ) -> PendingEditBinding | None:
        key = _binding_key(workspace_id, native_session_id, credential_id)
        native_request_hash = _hash(native_request_id)
        try:
            with self._connect() as connection:
                row = connection.execute(
                    "SELECT * FROM pending_edits WHERE binding_key=? "
                    "AND native_request_hash=? AND status='consumed'",
                    (key, native_request_hash),
                ).fetchone()
            if row is None:
                return None
            return PendingEditBinding(
                workspace_id=str(row["workspace_id"]),
                host_session_id=str(row["host_session_id"]),
                edit_request_id=str(row["edit_request_id"]),
                edit_hash=str(row["edit_hash"]),
                expires_at=_from_us(int(row["expires_at_us"])),
            )
        except (OSError, sqlite3.Error) as exc:
            raise EditHostStateError("consumed edit state unavailable") from exc

    def mark_captured(
        self,
        *,
        workspace_id: str,
        native_session_id: str,
        credential_id: str,
        edit_request_id: str,
    ) -> bool:
        if not _OPAQUE_ID.fullmatch(edit_request_id) or not edit_request_id.startswith(
            "edt_"
        ):
            raise ValueError("edit request ID is invalid")
        key = _binding_key(workspace_id, native_session_id, credential_id)
        try:
            with self._connect() as connection:
                result = connection.execute(
                    "DELETE FROM pending_edits WHERE binding_key=? AND edit_request_id=? "
                    "AND status='consumed'",
                    (key, edit_request_id),
                )
                return result.rowcount == 1
        except (OSError, sqlite3.Error) as exc:
            raise EditHostStateError("pending edit state unavailable") from exc


def native_edit_capture_body(
    *,
    workspace_id: str,
    host_session_id: str,
    edit_request_id: str,
    tool_name: str,
    relative_paths: Sequence[str],
    result: Literal["succeeded", "failed"],
) -> dict[str, Any]:
    """Map a native result to bounded capture data without transcript content."""

    if (
        not _WORKSPACE_ID.fullmatch(workspace_id)
        or not _OPAQUE_ID.fullmatch(host_session_id)
        or not host_session_id.startswith("hst_")
        or not _OPAQUE_ID.fullmatch(edit_request_id)
        or not edit_request_id.startswith("edt_")
        or not _TOOL_NAME.fullmatch(tool_name)
        or result not in {"succeeded", "failed"}
    ):
        raise ValueError("native capture identity is invalid")
    paths = tuple(relative_paths)
    if not paths or len(paths) > 32 or len(paths) != len(set(paths)):
        raise ValueError("native capture paths are invalid")
    for value in paths:
        path = PurePosixPath(value)
        if (
            not value
            or value.startswith(("/", "~"))
            or "\\" in value
            or path.as_posix() != value
            or any(part in {"", ".", ".."} for part in path.parts)
        ):
            raise ValueError("native capture path is invalid")
    paths = tuple(sorted(paths))
    identity = [workspace_id, edit_request_id, tool_name, list(paths), result]
    return {
        "workspace_id": workspace_id,
        "host_session_id": host_session_id,
        "source_kind": "native_edit",
        "record": {
            "record_type": "learning" if result == "succeeded" else "failed_attempt",
            "content": (
                f"Native {tool_name} {result} for {len(paths)} "
                "workspace-relative path(s)."
            ),
            "context": {"native_result": result, "path_count": len(paths)},
            "tags": ["native-edit", result],
        },
        "provenance": {
            "source_operation": tool_name,
            "edit_request_id": edit_request_id,
            "relative_file_paths": list(paths),
            "native_result": result,
        },
        "idempotency_key": "native-" + sha256_json(identity),
    }


__all__ = [
    "CreatedHostSession",
    "EditHostConfig",
    "EditHostStateError",
    "EditHostStateStore",
    "BridgeInstallation",
    "HostSessionBinding",
    "LocalBridgeInstallation",
    "PendingEditBinding",
    "RemoteBridgeInstallation",
    "RemoteWorkspaceBinding",
    "native_edit_capture_body",
    "provision_local_bridge_installation",
    "provision_client_bridge_installation",
    "provision_remote_bridge_installation",
]
