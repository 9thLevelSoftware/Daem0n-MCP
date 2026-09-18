"""Briefing remains bounded when Git descendants retain output handles."""

from __future__ import annotations

import os
import subprocess
import sys
import time

import pytest

from daem0nmcp.api.v7.resource_repository import SQLiteResourceRepository
from daem0nmcp.workspace import WorkspaceRegistry


def test_nested_workspace_git_changes_exclude_siblings_and_use_workspace_paths(
    tmp_path,
):
    subprocess.run(
        ["git", "init", "-q", str(tmp_path)], check=True, capture_output=True
    )
    nested = tmp_path / "nested"
    nested.mkdir()
    (nested / "inside.txt").write_text("inside", encoding="utf-8")
    (tmp_path / "outside-secret-name.txt").write_text("outside", encoding="utf-8")
    subprocess.run(
        ["git", "-C", str(tmp_path), "add", "."], check=True, capture_output=True
    )
    workspace = WorkspaceRegistry([nested], default_root=nested).default
    assert SQLiteResourceRepository._read_git_changes_sync(workspace) == [
        {"relative_file_path": "inside.txt", "status": "added"}
    ]


def test_git_output_filters_unexpected_paths_even_with_scoped_pathspec(
    tmp_path, monkeypatch
):
    results = iter([b"nested/\n", b" M sibling/private.txt\0 M nested/inside.txt\0"])
    monkeypatch.setattr(
        SQLiteResourceRepository, "_read_git_output_sync", lambda *_: next(results)
    )
    workspace = WorkspaceRegistry([tmp_path], default_root=tmp_path).default
    assert SQLiteResourceRepository._read_git_changes_sync(workspace) == [
        {"relative_file_path": "inside.txt", "status": "modified"}
    ]


@pytest.mark.skipif(
    os.name != "nt", reason="Windows subprocess pipe cleanup regression"
)
def test_git_timeout_does_not_wait_for_descendant_output_handles(tmp_path, monkeypatch):
    popen = subprocess.Popen
    child_pid = tmp_path / "git-descendant.pid"
    child_code = (
        "import subprocess,sys,time; "
        f"child=subprocess.Popen([sys.executable,'-c','import time; time.sleep(30)'], "
        "creationflags=subprocess.CREATE_NO_WINDOW); "
        f"open({str(child_pid)!r},'w').write(str(child.pid)); time.sleep(10)"
    )

    def git_with_descendant(_arguments, **options):
        return popen([sys.executable, "-c", child_code], **options)

    monkeypatch.setattr(subprocess, "Popen", git_with_descendant)
    workspace = WorkspaceRegistry([tmp_path], default_root=tmp_path).default
    started = time.monotonic()
    assert SQLiteResourceRepository._read_git_changes_sync(workspace) == []
    assert time.monotonic() - started < 4.5
    descendant = int(child_pid.read_text(encoding="utf-8"))
    assert_process_exited(descendant)


def assert_process_exited(pid):
    # os.kill(pid, 0) is destructive on Windows, so use a wait-only handle.
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel32.WaitForSingleObject.restype = wintypes.DWORD
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    handle = kernel32.OpenProcess(0x00100000, False, pid)  # SYNCHRONIZE only
    if not handle:
        assert ctypes.get_last_error() == 87  # PID no longer exists
        return
    try:
        assert kernel32.WaitForSingleObject(handle, 2000) == 0, (
            "Git descendant survived"
        )
    finally:
        kernel32.CloseHandle(handle)


@pytest.mark.skipif(os.name != "nt", reason="Windows suspended process ownership")
def test_job_setup_failure_reaps_suspended_process(tmp_path, monkeypatch):
    from daem0nmcp.api.v7 import resource_repository

    processes = []
    popen = subprocess.Popen

    def capture(_arguments, **options):
        process = popen(
            [sys.executable, "-c", "import time; time.sleep(30)"], **options
        )
        processes.append(process)
        return process

    class FailedJob:
        def __init__(self, _process):
            raise OSError("simulated ownership setup failure")

    monkeypatch.setattr(subprocess, "Popen", capture)
    monkeypatch.setattr(resource_repository, "_WindowsKillOnCloseJob", FailedJob)
    workspace = WorkspaceRegistry([tmp_path], default_root=tmp_path).default
    assert SQLiteResourceRepository._read_git_changes_sync(workspace) == []
    assert len(processes) == 1
    assert processes[0].poll() is not None
    assert_process_exited(processes[0].pid)
