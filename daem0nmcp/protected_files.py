"""Cross-platform owner-only files for host credentials and local state."""

from __future__ import annotations

import os
import stat
from pathlib import Path


class ProtectedPathError(PermissionError):
    """A local secret or state path is not protected for the current user."""


def _absolute_unresolved(path: Path) -> Path:
    return path if path.is_absolute() else Path.cwd() / path


def reject_linked_ancestry(path: str | Path) -> None:
    """Reject symlink/reparse components without resolving through them."""

    current = _absolute_unresolved(Path(path))
    while True:
        try:
            metadata = current.lstat()
        except FileNotFoundError:
            pass
        else:
            attributes = getattr(metadata, "st_file_attributes", 0)
            reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
            if stat.S_ISLNK(metadata.st_mode) or attributes & reparse:
                raise ProtectedPathError(
                    "protected path contains a link or reparse point"
                )
        parent = current.parent
        if parent == current:
            return
        current = parent


if os.name == "nt":
    import ctypes
    from ctypes import wintypes

    _ADVAPI32 = ctypes.WinDLL("advapi32", use_last_error=True)
    _KERNEL32 = ctypes.WinDLL("kernel32", use_last_error=True)

    _TOKEN_QUERY = 0x0008
    _TOKEN_USER = 1
    _SDDL_REVISION_1 = 1
    _OWNER_SECURITY_INFORMATION = 0x00000001
    _DACL_SECURITY_INFORMATION = 0x00000004
    _PROTECTED_DACL_SECURITY_INFORMATION = 0x80000000
    _SECURITY_INFORMATION = (
        _DACL_SECURITY_INFORMATION | _PROTECTED_DACL_SECURITY_INFORMATION
    )
    _FILE_READ_ATTRIBUTES = 0x0080
    _FILE_SHARE_READ_WRITE = 0x00000001 | 0x00000002
    _OPEN_EXISTING = 3
    _FILE_FLAG_BACKUP_SEMANTICS = 0x02000000
    _FILE_FLAG_OPEN_REPARSE_POINT = 0x00200000
    _FILE_ID_INFO_CLASS = 18
    _FILE_ATTRIBUTE_TAG_INFO_CLASS = 9
    _FILE_ATTRIBUTE_REPARSE_POINT = 0x00000400
    _INVALID_HANDLE_VALUE = wintypes.HANDLE(-1).value

    class _SidAndAttributes(ctypes.Structure):
        _fields_ = [("Sid", wintypes.LPVOID), ("Attributes", wintypes.DWORD)]

    class _TokenUser(ctypes.Structure):
        _fields_ = [("User", _SidAndAttributes)]

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

    _ADVAPI32.OpenProcessToken.argtypes = (
        wintypes.HANDLE,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.HANDLE),
    )
    _ADVAPI32.OpenProcessToken.restype = wintypes.BOOL
    _ADVAPI32.GetTokenInformation.argtypes = (
        wintypes.HANDLE,
        ctypes.c_int,
        wintypes.LPVOID,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
    )
    _ADVAPI32.GetTokenInformation.restype = wintypes.BOOL
    _ADVAPI32.ConvertSidToStringSidW.argtypes = (
        wintypes.LPVOID,
        ctypes.POINTER(wintypes.LPWSTR),
    )
    _ADVAPI32.ConvertSidToStringSidW.restype = wintypes.BOOL
    _ADVAPI32.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = (
        wintypes.LPCWSTR,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.LPVOID),
        ctypes.POINTER(wintypes.ULONG),
    )
    _ADVAPI32.ConvertStringSecurityDescriptorToSecurityDescriptorW.restype = (
        wintypes.BOOL
    )
    _ADVAPI32.ConvertSecurityDescriptorToStringSecurityDescriptorW.argtypes = (
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.LPWSTR),
        ctypes.POINTER(wintypes.ULONG),
    )
    _ADVAPI32.ConvertSecurityDescriptorToStringSecurityDescriptorW.restype = (
        wintypes.BOOL
    )
    _ADVAPI32.SetFileSecurityW.argtypes = (
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.LPVOID,
    )
    _ADVAPI32.SetFileSecurityW.restype = wintypes.BOOL
    _ADVAPI32.GetFileSecurityW.argtypes = (
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
    )
    _ADVAPI32.GetFileSecurityW.restype = wintypes.BOOL
    _KERNEL32.GetCurrentProcess.restype = wintypes.HANDLE
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
    _KERNEL32.LocalFree.argtypes = (wintypes.HLOCAL,)
    _KERNEL32.LocalFree.restype = wintypes.HLOCAL

    def _win_error(action: str) -> ProtectedPathError:
        return ProtectedPathError(
            f"{action} failed: {ctypes.WinError(ctypes.get_last_error())}"
        )

    def _current_user_sid() -> str:
        token = wintypes.HANDLE()
        if not _ADVAPI32.OpenProcessToken(
            _KERNEL32.GetCurrentProcess(), _TOKEN_QUERY, ctypes.byref(token)
        ):
            raise _win_error("opening the current process token")
        try:
            required = wintypes.DWORD()
            _ADVAPI32.GetTokenInformation(
                token, _TOKEN_USER, None, 0, ctypes.byref(required)
            )
            if required.value == 0:
                raise _win_error("sizing the current user token")
            buffer = ctypes.create_string_buffer(required.value)
            if not _ADVAPI32.GetTokenInformation(
                token,
                _TOKEN_USER,
                buffer,
                required,
                ctypes.byref(required),
            ):
                raise _win_error("reading the current user token")
            user = ctypes.cast(buffer, ctypes.POINTER(_TokenUser)).contents
            value = wintypes.LPWSTR()
            if not _ADVAPI32.ConvertSidToStringSidW(user.User.Sid, ctypes.byref(value)):
                raise _win_error("formatting the current user SID")
            try:
                result = value.value
                if result is None:
                    raise ProtectedPathError("current user SID is unavailable")
                return result
            finally:
                _KERNEL32.LocalFree(value)
        finally:
            _KERNEL32.CloseHandle(token)

    def _descriptor_sddl(descriptor: wintypes.LPVOID) -> str:
        value = wintypes.LPWSTR()
        if not _ADVAPI32.ConvertSecurityDescriptorToStringSecurityDescriptorW(
            descriptor,
            _SDDL_REVISION_1,
            _OWNER_SECURITY_INFORMATION | _DACL_SECURITY_INFORMATION,
            ctypes.byref(value),
            None,
        ):
            raise _win_error("formatting protected path security")
        try:
            result = value.value
            if result is None:
                raise ProtectedPathError("protected path security is unavailable")
            return result
        finally:
            _KERNEL32.LocalFree(value)

    def _desired_sddl(*, directory: bool) -> str:
        sid = _current_user_sid()
        inheritance = "OICI" if directory else ""
        return f"O:{sid}D:P(A;{inheritance};FA;;;{sid})(A;{inheritance};FA;;;SY)"

    def _converted_sddl(value: str) -> tuple[wintypes.LPVOID, str]:
        descriptor = wintypes.LPVOID()
        if not _ADVAPI32.ConvertStringSecurityDescriptorToSecurityDescriptorW(
            value,
            _SDDL_REVISION_1,
            ctypes.byref(descriptor),
            None,
        ):
            raise _win_error("building protected path security")
        return descriptor, _descriptor_sddl(descriptor)

    def _actual_sddl(path: Path) -> str:
        required = wintypes.DWORD()
        _ADVAPI32.GetFileSecurityW(
            str(path),
            _OWNER_SECURITY_INFORMATION | _DACL_SECURITY_INFORMATION,
            None,
            0,
            ctypes.byref(required),
        )
        if required.value == 0:
            raise _win_error("sizing protected path security")
        buffer = ctypes.create_string_buffer(required.value)
        if not _ADVAPI32.GetFileSecurityW(
            str(path),
            _OWNER_SECURITY_INFORMATION | _DACL_SECURITY_INFORMATION,
            buffer,
            required,
            ctypes.byref(required),
        ):
            raise _win_error("reading protected path security")
        return _descriptor_sddl(ctypes.cast(buffer, wintypes.LPVOID))

    def _protect_windows(path: Path, *, directory: bool) -> None:
        descriptor, expected = _converted_sddl(_desired_sddl(directory=directory))
        try:
            if not _ADVAPI32.SetFileSecurityW(
                str(path), _SECURITY_INFORMATION, descriptor
            ):
                raise _win_error("setting protected path security")
            # An elevated administrator's new files are owned by
            # BUILTIN\Administrators (BA).  The owner-only DACL just applied
            # grants this user WRITE_OWNER, so take ownership from BA only;
            # any other foreign owner is refused by the check below.
            actual = _actual_sddl(path)
            owner = expected.split("D:", 1)[0] + "D:"
            if (
                not actual.startswith(owner)
                and actual.startswith("O:BAD:")
                and not _ADVAPI32.SetFileSecurityW(
                    str(path), _OWNER_SECURITY_INFORMATION, descriptor
                )
            ):
                raise _win_error("setting protected path owner")
        finally:
            _KERNEL32.LocalFree(descriptor)
        if _actual_sddl(path) != expected:
            raise ProtectedPathError("protected path ACL verification failed")

    def _verify_windows(path: Path, *, directory: bool) -> None:
        descriptor, expected = _converted_sddl(_desired_sddl(directory=directory))
        _KERNEL32.LocalFree(descriptor)
        if _actual_sddl(path) != expected:
            raise ProtectedPathError("protected path ACL is not owner-only")


def _open_guarded_directory(path: Path) -> tuple[object, tuple[object, ...]]:
    if os.name == "nt":
        handle = _KERNEL32.CreateFileW(  # type: ignore[name-defined]
            str(path),
            _FILE_READ_ATTRIBUTES,  # type: ignore[name-defined]
            _FILE_SHARE_READ_WRITE,  # type: ignore[name-defined]
            None,
            _OPEN_EXISTING,  # type: ignore[name-defined]
            _FILE_FLAG_BACKUP_SEMANTICS  # type: ignore[name-defined]
            | _FILE_FLAG_OPEN_REPARSE_POINT,  # type: ignore[name-defined]
            None,
        )
        if handle == _INVALID_HANDLE_VALUE:  # type: ignore[name-defined]
            raise ProtectedPathError("protected directory cannot be guarded")
        try:
            attributes = _FileAttributeTagInfo()  # type: ignore[name-defined]
            if not _KERNEL32.GetFileInformationByHandleEx(  # type: ignore[name-defined]
                handle,
                _FILE_ATTRIBUTE_TAG_INFO_CLASS,  # type: ignore[name-defined]
                ctypes.byref(attributes),  # type: ignore[name-defined]
                ctypes.sizeof(attributes),  # type: ignore[name-defined]
            ):
                raise ProtectedPathError("protected directory identity is unavailable")
            if attributes.FileAttributes & _FILE_ATTRIBUTE_REPARSE_POINT:
                raise ProtectedPathError(
                    "protected path contains a link or reparse point"
                )
            information = _FileIdInfo()  # type: ignore[name-defined]
            if not _KERNEL32.GetFileInformationByHandleEx(  # type: ignore[name-defined]
                handle,
                _FILE_ID_INFO_CLASS,  # type: ignore[name-defined]
                ctypes.byref(information),  # type: ignore[name-defined]
                ctypes.sizeof(information),  # type: ignore[name-defined]
            ):
                raise ProtectedPathError("protected directory identity is unavailable")
            identity: tuple[object, ...] = (
                "windows-volume-file-id",
                int(information.VolumeSerialNumber),
                bytes(information.FileId.Identifier),
            )
            return handle, identity
        except Exception:
            _KERNEL32.CloseHandle(handle)  # type: ignore[name-defined]
            raise

    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise ProtectedPathError("protected directory cannot be guarded") from exc
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISDIR(metadata.st_mode):
            raise ProtectedPathError("protected directory is not a directory")
        return descriptor, ("posix-dev-inode", metadata.st_dev, metadata.st_ino)
    except Exception:
        os.close(descriptor)
        raise


def _close_guarded_directory(resource: object) -> None:
    if os.name == "nt":
        _KERNEL32.CloseHandle(resource)  # type: ignore[name-defined,arg-type]
    else:
        if not isinstance(resource, int):
            raise ProtectedPathError("protected directory guard is invalid")
        os.close(resource)


class DirectoryAncestryGuard:
    """Hold trustworthy directory identities across one critical operation."""

    def __init__(
        self,
        target: Path,
        entries: tuple[tuple[Path, object, tuple[object, ...]], ...],
    ) -> None:
        self._target = target
        self._entries = entries
        self._closed = False

    def verify(self) -> None:
        if self._closed:
            raise ProtectedPathError("protected directory guard is closed")
        reject_linked_ancestry(self._target)
        for path, _, expected in self._entries:
            resource, actual = _open_guarded_directory(path)
            try:
                if actual != expected:
                    raise ProtectedPathError("protected directory identity changed")
            finally:
                _close_guarded_directory(resource)

    def duplicate_target_descriptor(self) -> int:
        """Duplicate the guarded target directory descriptor on POSIX.

        The duplicate continues to name the directory object that was
        validated when the guard was built, even if its pathname is replaced.
        Callers own the returned descriptor.  Windows callers use the retained
        native handles directly for rename prevention and must not call this.
        """

        if self._closed:
            raise ProtectedPathError("protected directory guard is closed")
        if os.name == "nt" or not self._entries:
            raise ProtectedPathError("directory descriptors are unavailable")
        resource = self._entries[-1][1]
        if not isinstance(resource, int):
            raise ProtectedPathError("protected directory guard is invalid")
        return os.dup(resource)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        for _, resource, _ in reversed(self._entries):
            _close_guarded_directory(resource)

    def __enter__(self) -> DirectoryAncestryGuard:
        self.verify()
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.close()


def guard_directory_ancestry(
    target: str | Path,
    *,
    boundary: str | Path,
) -> DirectoryAncestryGuard:
    """Guard boundary through target against link insertion or replacement."""

    target_path = _absolute_unresolved(Path(target))
    boundary_path = _absolute_unresolved(Path(boundary))
    try:
        relative = target_path.relative_to(boundary_path)
    except ValueError as exc:
        raise ProtectedPathError("protected directory leaves its boundary") from exc
    reject_linked_ancestry(target_path)
    components = [boundary_path]
    current = boundary_path
    for part in relative.parts:
        current = current / part
        components.append(current)

    entries: list[tuple[Path, object, tuple[object, ...]]] = []
    try:
        for component in components:
            resource, identity = _open_guarded_directory(component)
            entries.append((component, resource, identity))
        guard = DirectoryAncestryGuard(target_path, tuple(entries))
        guard.verify()
        return guard
    except Exception:
        for _, resource, _ in reversed(entries):
            _close_guarded_directory(resource)
        raise


def _protect(path: Path, *, directory: bool) -> None:
    reject_linked_ancestry(path)
    if os.name == "nt":
        _protect_windows(path, directory=directory)
        return
    os.chmod(path, stat.S_IRWXU if directory else stat.S_IRUSR | stat.S_IWUSR)
    _verify_posix(path, directory=directory)


def _verify_posix(path: Path, *, directory: bool) -> None:
    metadata = path.stat()
    if hasattr(os, "getuid") and metadata.st_uid != os.getuid():
        raise ProtectedPathError("protected path owner does not match")
    expected = 0o700 if directory else 0o600
    if stat.S_IMODE(metadata.st_mode) != expected:
        raise ProtectedPathError("protected path permissions are not owner-only")


def _verify(path: Path, *, directory: bool) -> None:
    reject_linked_ancestry(path)
    if os.name == "nt":
        _verify_windows(path, directory=directory)
    else:
        _verify_posix(path, directory=directory)


def ensure_owner_only_directory(path: str | Path) -> Path:
    target = _absolute_unresolved(Path(path))
    reject_linked_ancestry(target.parent)
    target.mkdir(parents=True, exist_ok=True)
    if not target.is_dir():
        raise ProtectedPathError("protected directory is not a directory")
    _protect(target, directory=True)
    return target.resolve(strict=True)


def write_new_owner_only_file(path: str | Path, data: bytes) -> Path:
    target = _absolute_unresolved(Path(path))
    if not isinstance(data, bytes):
        raise TypeError("protected file data must be bytes")
    reject_linked_ancestry(target)
    ensure_owner_only_directory(target.parent)
    descriptor = os.open(
        target,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0),
        0o600,
    )
    try:
        remaining = memoryview(data)
        while remaining:
            written = os.write(descriptor, remaining)
            if written <= 0:
                raise OSError("protected file write made no progress")
            remaining = remaining[written:]
        os.fsync(descriptor)
    except Exception:
        os.close(descriptor)
        target.unlink(missing_ok=True)
        raise
    else:
        os.close(descriptor)
    try:
        # Apply and verify the final descriptor after the creating handle is
        # closed.  On Windows, closing that handle can otherwise restore the
        # inherited DACL control flags observed when the file was created.
        _protect(target, directory=False)
        verify_owner_only_file(target, max_bytes=max(1, len(data)))
    except Exception:
        target.unlink(missing_ok=True)
        raise
    return target.resolve(strict=True)


def protect_owner_only_file(path: str | Path) -> Path:
    target = _absolute_unresolved(Path(path))
    if not target.is_file():
        raise ProtectedPathError("protected file is not a regular file")
    _protect(target, directory=False)
    return target.resolve(strict=True)


def verify_owner_only_file(
    path: str | Path,
    *,
    max_bytes: int | None = None,
) -> Path:
    target = _absolute_unresolved(Path(path))
    reject_linked_ancestry(target)
    metadata = target.stat()
    if not stat.S_ISREG(metadata.st_mode):
        raise ProtectedPathError("protected file is not a regular file")
    if max_bytes is not None and not 0 <= metadata.st_size <= max_bytes:
        raise ProtectedPathError("protected file exceeds its size limit")
    _verify(target, directory=False)
    return target.resolve(strict=True)


def verify_owner_only_directory(path: str | Path) -> Path:
    target = _absolute_unresolved(Path(path))
    if not target.is_dir():
        raise ProtectedPathError("protected directory is not a directory")
    _verify(target, directory=True)
    return target.resolve(strict=True)


__all__ = [
    "DirectoryAncestryGuard",
    "ProtectedPathError",
    "ensure_owner_only_directory",
    "guard_directory_ancestry",
    "protect_owner_only_file",
    "reject_linked_ancestry",
    "verify_owner_only_directory",
    "verify_owner_only_file",
    "write_new_owner_only_file",
]
