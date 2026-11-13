"""Native Unix restart and unsafe-endpoint cases for the local edit bridge."""

from __future__ import annotations

import concurrent.futures
import errno
import os
import socket
import stat
import subprocess
import sys
import threading
from types import SimpleNamespace

import pytest

from daem0nmcp import edit_bridge_transport as transport
from daem0nmcp.protected_files import ProtectedPathError

pytestmark = pytest.mark.skipif(os.name == "nt", reason="native Unix socket recovery")
AUTHKEY = b"unix-recovery-auth-key-" + b"x" * 32


class _Protocol:
    async def handle(self, *, body, **_kwargs):
        return transport.BridgeResponse(200, {"ok": True, "data": body})


def _server(address):
    return transport.LocalBridgeServer(
        protocol=_Protocol(), address=str(address), authkey=AUTHKEY
    )


def _stale(address, *, mode=0o600):
    peer = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        peer.bind(str(address))
        os.chmod(address, mode)
    finally:
        peer.close()


def _call(address, *, authkey=AUTHKEY):
    return transport.LocalBridgeClient(
        address=str(address), authkey=authkey, bearer="test-secret"
    ).call("/test", {"nonce": "native-restart-proof"})


def test_stale_socket_recovers_and_preserves_authenticated_wire_protocol(
    socket_tmp_path,
):
    address = socket_tmp_path / "bridge.sock"
    _stale(address)
    server = _server(address)
    server.start()
    try:
        assert server.is_ready
        assert _call(address).body == {
            "ok": True,
            "data": {"nonce": "native-restart-proof"},
        }
        assert _call(address, authkey=b"wrong-auth-key-" + b"y" * 32).status == 401
        metadata = address.lstat()
        assert stat.S_IMODE(metadata.st_mode) == 0o600
        assert metadata.st_uid == os.getuid()
    finally:
        server.close()
    assert not address.exists()
    lock = address.with_name(address.name + ".lock")
    assert lock.stat().st_mode & 0o777 == 0o600
    restarted = _server(address)
    restarted.start()
    try:
        assert _call(address).status == 200
    finally:
        restarted.close()


def test_abrupt_process_exit_releases_lease_for_next_server(socket_tmp_path):
    address = socket_tmp_path / "killed.sock"
    script = """
import os,sys
from daem0nmcp.edit_bridge_transport import LocalBridgeServer
server=LocalBridgeServer(protocol=None,address=sys.argv[1],authkey=b'x'*32)
server.start()
os._exit(0)
"""
    completed = subprocess.run(
        [sys.executable, "-c", script, str(address)],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert address.is_socket()
    server = _server(address)
    server.start()
    try:
        assert _call(address).status == 200
    finally:
        server.close()


def test_unmanaged_live_listener_is_never_removed(socket_tmp_path):
    address = socket_tmp_path / "live.sock"
    peer = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        peer.bind(str(address))
        os.chmod(address, 0o600)
        peer.listen(2)
        before = address.lstat()
        with pytest.raises(FileExistsError, match="live"):
            _server(address).start()
        assert os.path.samestat(before, address.lstat())
        accepted, _ = peer.accept()
        accepted.close()
    finally:
        peer.close()


@pytest.mark.parametrize("kind", ["file", "directory", "symlink", "open-mode-socket"])
def test_unsafe_endpoint_is_preserved(socket_tmp_path, kind):
    address = socket_tmp_path / "unsafe.sock"
    target = socket_tmp_path / "target"
    target.write_bytes(b"untouched")
    if kind == "file":
        address.write_bytes(b"keep")
    elif kind == "directory":
        address.mkdir()
    elif kind == "symlink":
        address.symlink_to(target)
    else:
        _stale(address, mode=0o666)
    before = address.lstat()
    with pytest.raises(ProtectedPathError, match="owner-only socket"):
        _server(address).start()
    assert os.path.samestat(before, address.lstat())
    assert target.read_bytes() == b"untouched"
    if kind == "file":
        assert address.read_bytes() == b"keep"


def test_wrong_owner_socket_is_preserved(socket_tmp_path, monkeypatch):
    address = socket_tmp_path / "other-owner.sock"
    _stale(address)
    before = address.lstat()
    real_metadata = transport._UnixEndpointLease.metadata

    def other_owner(self):
        actual = real_metadata(self)
        return SimpleNamespace(
            st_mode=actual.st_mode, st_uid=actual.st_uid + 1, st_nlink=actual.st_nlink
        )

    monkeypatch.setattr(transport._UnixEndpointLease, "metadata", other_owner)
    with pytest.raises(ProtectedPathError, match="owner-only socket"):
        _server(address).start()
    assert os.path.samestat(before, address.lstat())


def test_changed_socket_identity_is_not_reclaimed(socket_tmp_path, monkeypatch):
    address = socket_tmp_path / "changed.sock"
    _stale(address)
    original_socket = socket.socket
    replacement = []

    class Probe:
        def settimeout(self, _timeout):
            pass

        def connect_ex(self, _address):
            address.unlink()
            peer = original_socket(socket.AF_UNIX, socket.SOCK_STREAM)
            try:
                peer.bind(str(address))
                os.chmod(address, 0o600)
                replacement.append(address.lstat())
            finally:
                peer.close()
            return errno.ECONNREFUSED

        def close(self):
            pass

    monkeypatch.setattr(transport.socket, "socket", lambda *_args, **_kwargs: Probe())
    with pytest.raises(ProtectedPathError, match="identity changed"):
        _server(address).start()
    assert replacement and os.path.samestat(replacement[0], address.lstat())


def test_simultaneous_startup_has_exactly_one_live_owner(socket_tmp_path):
    address = socket_tmp_path / "concurrent.sock"
    servers = [_server(address), _server(address)]
    barrier = threading.Barrier(2)

    def start(server):
        barrier.wait(timeout=5)
        try:
            server.start()
        except FileExistsError:
            return False
        return True

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        started = list(pool.map(start, servers))
    assert sorted(started) == [False, True]
    winner = servers[started.index(True)]
    try:
        assert _call(address).status == 200
    finally:
        winner.close()


@pytest.mark.parametrize("kind", ["symlink", "directory", "open-mode", "hard-link"])
def test_unsafe_lock_does_not_remove_existing_endpoint(socket_tmp_path, kind):
    address = socket_tmp_path / "lock-unsafe.sock"
    _stale(address)
    before = address.lstat()
    lock = address.with_name(address.name + ".lock")
    target = socket_tmp_path / "lock-target"
    target.write_bytes(b"preserved")
    target.chmod(0o600)
    if kind == "symlink":
        lock.symlink_to(target)
    elif kind == "directory":
        lock.mkdir()
    elif kind == "open-mode":
        lock.write_bytes(b"preserved")
        lock.chmod(0o666)
    else:
        os.link(target, lock)
    with pytest.raises((OSError, ProtectedPathError)):
        _server(address).start()
    assert os.path.samestat(before, address.lstat())
    assert target.read_bytes() == b"preserved"


def test_close_does_not_unlink_replacement_entry(socket_tmp_path):
    address = socket_tmp_path / "replace-on-close.sock"
    moved = socket_tmp_path / "original.sock"
    server = _server(address)
    server.start()
    address.rename(moved)
    address.write_bytes(b"replacement")
    server._stop.set()
    wake = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        wake.connect(str(moved))
    finally:
        wake.close()
    server._thread.join(timeout=5)
    assert not server._thread.is_alive()
    with pytest.raises(ProtectedPathError, match="identity changed"):
        server.close()
    assert address.read_bytes() == b"replacement"


@pytest.mark.parametrize("failure_index", [0, 1, 3])
def test_thread_start_failure_releases_endpoint_lease(
    socket_tmp_path, monkeypatch, failure_index
):
    address = socket_tmp_path / "failed-start.sock"
    original_start = threading.Thread.start
    starts = 0

    def fail_selected_start(thread):
        nonlocal starts
        if thread.name.startswith("daem0nmcp-edit-bridge-ipc"):
            index = starts
            starts += 1
            if index == failure_index:
                raise RuntimeError("injected thread startup failure")
        return original_start(thread)

    with monkeypatch.context() as selected:
        selected.setattr(threading.Thread, "start", fail_selected_start)
        with pytest.raises(RuntimeError, match="injected thread startup failure"):
            _server(address).start()
    assert not address.exists()
    restarted = _server(address)
    restarted.start()
    try:
        assert _call(address).status == 200
    finally:
        restarted.close()


def test_permission_change_never_follows_replaced_symlink(socket_tmp_path, monkeypatch):
    address = socket_tmp_path / "chmod-race.sock"
    target = socket_tmp_path / "unrelated"
    target.write_bytes(b"untouched")
    target.chmod(0o640)
    before = target.stat().st_mode
    original_metadata = transport._UnixEndpointLease.metadata
    replaced = []

    def replace_before_chmod(self):
        actual = original_metadata(self)
        if stat.S_ISSOCK(actual.st_mode) and not replaced:
            address.rename(socket_tmp_path / "bound-original.sock")
            address.symlink_to(target)
            replaced.append(address.lstat())
        return actual

    monkeypatch.setattr(transport._UnixEndpointLease, "metadata", replace_before_chmod)
    with pytest.raises((OSError, NotImplementedError)):
        _server(address).start()
    assert replaced and os.path.samestat(replaced[0], address.lstat())
    assert target.stat().st_mode == before
    assert target.read_bytes() == b"untouched"
