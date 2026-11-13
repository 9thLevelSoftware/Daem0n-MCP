"""Explicit typed-tool profiles preserve unrelated native configuration."""

import hashlib
import json

import pytest
import tomlkit

from daem0nmcp.client_install import ClientInstallError, install_client
from tests.test_client_install_cli import _run

PROFILES = [
    ("codex", ".codex/config.toml", {}),
    ("agy", ".agents/mcp_config.json", {}),
    ("omp", ".omp/mcp.json", {}),
    ("pi", ".pi/mcp.json", {}),
    ("opencode", "opencode.json", {"client_version": "1.18.21"}),
    (
        "opencode",
        "opencode.json",
        {"client_version": "2.2.7", "opencode_interface": "v2"},
    ),
]


def _document(path, client):
    text = path.read_text(encoding="utf-8")
    return tomlkit.parse(text).unwrap() if client == "codex" else json.loads(text)


def _environment(document, client, options):
    if client == "codex":
        return document["mcp_servers"]["daem0nmcp"]["env"]
    if client == "opencode":
        servers = document["mcp"]
        if options.get("opencode_interface") == "v2":
            servers = servers["servers"]
        return servers["daem0nmcp"]["environment"]
    return document["mcpServers"]["daem0nmcp"]["env"]


@pytest.mark.parametrize("client,relative,options", PROFILES)
def test_explicit_surface_and_omission_preserve_existing_configuration(
    tmp_path, client, relative, options
):
    installed = install_client(client, tmp_path, tool_surface="full", **options)
    assert installed["tool_surface"] == "full"
    path = tmp_path / relative
    document = _document(path, client)
    environment = _environment(document, client, options)
    environment["UNRELATED_SETTING"] = "retain-this"
    environment["DAEM0NMCP_COVENANT_MODE"] = "strict"
    document["user_sentinel"] = "unchanged"
    path.write_text(
        tomlkit.dumps(document)
        if client == "codex"
        else json.dumps(document, indent=2),
        encoding="utf-8",
    )
    before = path.read_bytes()
    preserved = install_client(client, tmp_path, **options)
    assert preserved["tool_surface"] == "full"
    assert path.read_bytes() == before
    preview = install_client(
        client, tmp_path, tool_surface="core", dry_run=True, **options
    )
    assert preview["tool_surface"] == "core"
    assert path.read_bytes() == before
    changed = install_client(client, tmp_path, tool_surface="core", **options)
    assert changed["tool_surface"] == "core"
    expected = json.loads(json.dumps(document))
    _environment(expected, client, options)["DAEM0NMCP_TOOL_SURFACE"] = "core"
    assert _document(path, client) == expected
    assert not (tmp_path / ".daem0nmcp/storage").exists()


def test_omitted_surface_defaults_to_core_for_new_binding(tmp_path):
    result = install_client("pi", tmp_path)
    assert result["tool_surface"] == "core"


def test_invalid_surface_is_rejected_before_touching_project(tmp_path):
    project = tmp_path / "missing"
    with pytest.raises(ClientInstallError) as error:
        install_client("pi", project, tool_surface="guess")
    assert error.value.code == "INVALID_TOOL_SURFACE"
    assert not project.exists()


def test_cli_surface_preview_does_not_publish_configuration(tmp_path):
    result = _run(
        [
            "install-client",
            "pi",
            "--tool-surface",
            "full",
            "--dry-run",
            "--project-path",
            str(tmp_path),
        ],
        tmp_path,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["tool_surface"] == "full"
    assert not (tmp_path / ".pi").exists()


def test_surface_change_preserves_jsonc_comments_and_existing_bridge(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    install_client(
        "opencode",
        project,
        client_version="1.18.21",
        automation="enforce",
        bridge_config_root=tmp_path / "private-bridge",
    )
    config = project / "opencode.json"
    config.write_text(
        "// preserve user comment\n" + config.read_text(), encoding="utf-8"
    )
    preserved = {
        path: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in tmp_path.rglob("*")
        if path.is_file() and path != config and "client-install" not in path.parts
    }
    result = install_client(
        "opencode", project, client_version="1.18.21", tool_surface="full"
    )
    assert result["tool_surface"] == "full"
    assert result["automation"]["mode"] == "unchanged"
    assert config.read_text().startswith("// preserve user comment\n")
    assert all(
        hashlib.sha256(path.read_bytes()).hexdigest() == digest
        for path, digest in preserved.items()
    )
