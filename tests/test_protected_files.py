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
