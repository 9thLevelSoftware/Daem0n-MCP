from __future__ import annotations

import os
import stat
import subprocess
import sys

import pytest

from daem0nmcp.protected_files import (
    ProtectedPathError,
    ensure_owner_only_directory,
    verify_owner_only_directory,
    verify_owner_only_file,
    write_new_owner_only_file,
)


def test_owner_only_directory_and_file_round_trip(tmp_path):
    directory = ensure_owner_only_directory(tmp_path / "authority")
    target = write_new_owner_only_file(directory / "credential.json", b"secret")

    assert verify_owner_only_directory(directory) == directory
    assert verify_owner_only_file(target, max_bytes=6) == target
    with pytest.raises(ProtectedPathError, match="size"):
        verify_owner_only_file(target, max_bytes=5)
    with pytest.raises(FileExistsError):
        write_new_owner_only_file(target, b"replacement")


def test_empty_owner_only_file_retains_protection_after_creator_closes(tmp_path):
    target = write_new_owner_only_file(tmp_path / "authority" / "state.sqlite3", b"")

    assert target.stat().st_size == 0
    assert verify_owner_only_file(target, max_bytes=0) == target


@pytest.mark.skipif(sys.platform != "win32", reason="Windows DACL test")
def test_windows_rejects_extra_users_access(tmp_path):
    target = write_new_owner_only_file(tmp_path / "authority" / "secret", b"x")
    result = subprocess.run(
        [
            "icacls.exe",
            str(target),
            "/grant",
            "*S-1-5-32-545:(R)",
        ],
        check=False,
        capture_output=True,
        timeout=5,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    if result.returncode != 0:
        pytest.skip("icacls cannot mutate the test DACL")
    with pytest.raises(ProtectedPathError, match="ACL"):
        verify_owner_only_file(target)


def _fake_windows_security(monkeypatch, owners):
    """Replay owner SIDs ("USER" = current user) and record SetFileSecurityW."""
    from daem0nmcp import protected_files

    expected_descriptor, expected = protected_files._converted_sddl(
        protected_files._desired_sddl(directory=False)
    )
    protected_files._KERNEL32.LocalFree(expected_descriptor)
    dacl = expected.split("D:", 1)[1]
    user = expected.split("D:", 1)[0][2:]
    sequence = iter(
        f"O:{user if owner == 'USER' else owner}D:{dacl}" for owner in owners
    )
    calls = []
    monkeypatch.setattr(protected_files, "_actual_sddl", lambda _path: next(sequence))
    monkeypatch.setattr(
        protected_files._ADVAPI32,
        "SetFileSecurityW",
        lambda _path, information, _descriptor: calls.append(information) or True,
    )
    return protected_files, calls


@pytest.mark.skipif(sys.platform != "win32", reason="Windows owner test")
def test_windows_takes_ownership_only_from_builtin_administrators(
    tmp_path, monkeypatch
):
    target = tmp_path / "file"
    target.write_bytes(b"")
    module, calls = _fake_windows_security(monkeypatch, ["BA", "USER"])
    module._protect_windows(target, directory=False)
    assert calls[-1] == module._OWNER_SECURITY_INFORMATION


@pytest.mark.skipif(sys.platform != "win32", reason="Windows owner test")
def test_windows_refuses_any_other_foreign_owner(tmp_path, monkeypatch):
    target = tmp_path / "file"
    target.write_bytes(b"")
    foreign = "S-1-5-21-1-2-3-1001"
    module, calls = _fake_windows_security(monkeypatch, [foreign, foreign])
    with pytest.raises(ProtectedPathError, match="verification failed"):
        module._protect_windows(target, directory=False)
    assert module._OWNER_SECURITY_INFORMATION not in calls


def test_rejects_linked_ancestry(tmp_path):
    real = tmp_path / "real"
    real.mkdir()
    linked = tmp_path / "linked"
    try:
        linked.symlink_to(real, target_is_directory=True)
    except OSError:
        pytest.skip("symlink creation is unavailable")
    with pytest.raises(ProtectedPathError, match="link or reparse"):
        ensure_owner_only_directory(linked / "authority")


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission test")
def test_posix_rejects_group_readable_file(tmp_path):
    target = write_new_owner_only_file(tmp_path / "authority" / "secret", b"x")
    target.chmod(stat.S_IRUSR | stat.S_IWUSR | stat.S_IRGRP)
    with pytest.raises(ProtectedPathError, match="permissions"):
        verify_owner_only_file(target)
