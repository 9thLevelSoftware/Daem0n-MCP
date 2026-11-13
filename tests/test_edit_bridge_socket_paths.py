from __future__ import annotations

import os
import socket
from pathlib import Path
from types import SimpleNamespace

import pytest

from daem0nmcp import edit_bridge_transport as transport
from daem0nmcp.protected_files import ProtectedPathError


@pytest.fixture
def simulated_unix(monkeypatch):
    directories = []

    def protect(path):
        directories.append(path)
        return path

    monkeypatch.setattr(transport, "sys", SimpleNamespace(platform="darwin"))
    monkeypatch.setattr(
        transport, "os", SimpleNamespace(fsencode=os.fsencode, getuid=lambda: 501)
    )
    monkeypatch.setattr(transport, "ensure_owner_only_directory", protect)
    return directories


def test_short_unix_endpoint_remains_compatible(simulated_unix):
    runtime = Path("/safe/run")
    address = transport.local_bridge_address(runtime, "credential-one")
    assert Path(address).parent == runtime
    assert simulated_unix == [runtime]
    assert len(os.fsencode(address)) <= 103


@pytest.mark.parametrize(
    "runtime",
    [
        Path("/Users/christopherwilloughby/.daem0nmcp/edit-bridges/run/" + "a" * 16),
        Path("/private/" + "a" * 160),
        Path("/private/" + "\u754c" * 30),
    ],
)
def test_long_unix_endpoint_is_bounded_and_bound_to_runtime_and_credential(
    simulated_unix, runtime
):
    address = transport.local_bridge_address(runtime, "credential-one")
    assert len(os.fsencode(address)) <= 103
    assert Path(address).parent == Path("/private/tmp/daem0nmcp-501")
    assert simulated_unix[:2] == [runtime, Path("/private/tmp/daem0nmcp-501")]
    assert transport.local_bridge_address(runtime, "credential-one") == address
    assert transport.local_bridge_address(runtime, "credential-two") != address
    assert (
        transport.local_bridge_address(runtime / "other", "credential-one") != address
    )


def test_long_endpoint_does_not_bypass_runtime_protection(simulated_unix, monkeypatch):
    def reject(_path):
        raise ProtectedPathError("untrusted runtime")

    monkeypatch.setattr(transport, "ensure_owner_only_directory", reject)
    with pytest.raises(ProtectedPathError, match="untrusted runtime"):
        transport.local_bridge_address(Path("/private/" + "a" * 160), "credential")


@pytest.mark.skipif(os.name == "nt", reason="requires a native Unix socket")
def test_long_native_endpoint_binds_and_has_private_parent(tmp_path):
    runtime = tmp_path / ("long-runtime-" * 12)
    address = transport.local_bridge_address(runtime, "native-path-test")
    assert len(os.fsencode(address)) <= 103
    assert Path(address).parent.stat().st_mode & 0o777 == 0o700
    assert Path(address).parent.stat().st_uid == os.getuid()
    peer = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    bound = False
    try:
        peer.bind(address)
        bound = True
    finally:
        peer.close()
        if bound:
            Path(address).unlink()


@pytest.mark.skipif(os.name == "nt", reason="requires native Unix path protection")
def test_long_native_endpoint_rejects_linked_fallback(
    tmp_path, socket_tmp_path, monkeypatch
):
    temporary_root = socket_tmp_path / "short"
    temporary_root.mkdir()
    target = tmp_path / "untouched"
    target.mkdir(mode=0o755)
    original_mode = target.stat().st_mode
    (temporary_root / f"daem0nmcp-{os.getuid()}").symlink_to(
        target, target_is_directory=True
    )
    monkeypatch.setattr(transport, "Path", lambda _path: temporary_root)
    with pytest.raises(ProtectedPathError, match="link"):
        transport.local_bridge_address(tmp_path / ("long-runtime-" * 12), "credential")
    assert target.stat().st_mode == original_mode
