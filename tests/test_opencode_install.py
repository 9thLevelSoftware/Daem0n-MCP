"""Tests for OpenCode installer plugin installation behavior."""

from __future__ import annotations

import json
import ssl
import sys
from pathlib import Path

import pytest

from daem0nmcp.edit_bridge_transport import provision_bridge_credential
from daem0nmcp.opencode_install import (
    COMMAND_TEMPLATES,
    PLUGIN_TEMPLATE,
    install_opencode,
    select_opencode_interface,
)


def test_installer_selects_only_released_v1_and_rejects_v2_plan():
    assert select_opencode_interface("1.18.21") == "v1"
    assert select_opencode_interface("1.18.21", "v1") == "v1"
    with pytest.raises(ValueError, match="V2"):
        select_opencode_interface("1.18.21", "v2")


def test_live_installer_rejects_v2_without_native_tool_hooks(tmp_path):
    ok, message = install_opencode(
        str(tmp_path),
        interface="v2",
        opencode_version="1.18.21",
        bridge_config_root=tmp_path / "host-config",
    )
    assert not ok
    assert "does not expose tool execution hooks" in message
    assert not (tmp_path / ".opencode").exists()


def test_install_creates_plugin_file(tmp_path):
    """Running install-opencode creates the plugin file automatically."""
    ok, msg = install_opencode(
        str(tmp_path), bridge_config_root=tmp_path / "host-config"
    )
    assert ok, msg

    plugin = tmp_path / ".opencode" / "plugins" / "daem0n.ts"
    assert plugin.exists(), "Plugin file should be created"

    content = plugin.read_text(encoding="utf-8")
    assert "Daem0nPlugin" in content
    assert "experimental.chat.system.transform" in content
    assert "pre_edit" in content
    assert "pre_bash" in content

    config = json.loads((tmp_path / "opencode.json").read_text(encoding="utf-8"))
    environment = config["mcp"]["daem0nmcp"]["environment"]
    assert config["mcp"]["daem0nmcp"]["command"] == [
        sys.executable,
        "-m",
        "daem0nmcp",
    ]
    assert environment["DAEM0NMCP_PYTHON_EXECUTABLE"] == sys.executable
    credential = Path(environment["DAEM0NMCP_EDIT_BRIDGE_CREDENTIAL_FILE"])
    assert credential.is_file()
    secret = json.loads(credential.read_text(encoding="utf-8"))["secret"]
    assert secret not in (tmp_path / "opencode.json").read_text(encoding="utf-8")
    assert environment["DAEM0NMCP_PROJECT_ROOT"] == str(tmp_path.resolve())


def test_install_remote_pairing_passes_only_protected_host_paths(tmp_path, monkeypatch):
    project = tmp_path / "desktop-project"
    project.mkdir()
    credential = tmp_path / "host" / "credential.json"
    secret, _identity = provision_bridge_credential(
        credential,
        principal_id="remote-principal",
        transports=frozenset({"remote-https"}),
    )
    ca_file = tmp_path / "ca.pem"
    ca_file.write_text("test CA", encoding="utf-8")
    monkeypatch.setattr(
        "daem0nmcp.edit_host.ssl.create_default_context",
        lambda **_kwargs: ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT),
    )

    ok, message = install_opencode(
        str(project),
        remote_workspace_id="ws_" + "6" * 24,
        remote_credential_path=credential,
        remote_base_url="https://server.example:7443",
        remote_ca_file=ca_file,
        remote_origin="https://desktop.example",
        opencode_version="1.18.21",
    )

    assert ok, message
    config = json.loads((project / "opencode.json").read_text(encoding="utf-8"))
    host_config = project / ".opencode" / "daem0n-host.json"
    environment = json.loads(host_config.read_text(encoding="utf-8"))["environment"]
    assert environment["DAEM0NMCP_EDIT_BRIDGE_MODE"] == "remote-https"
    for name in (
        "DAEM0NMCP_EDIT_BRIDGE_REMOTE_URL",
        "DAEM0NMCP_EDIT_BRIDGE_CA_FILE",
        "DAEM0NMCP_EDIT_BRIDGE_ORIGIN",
    ):
        assert name not in environment
    assert "DAEM0NMCP_EDIT_BRIDGE_MODE" not in config["mcp"]["daem0nmcp"]["environment"]
    binding = Path(environment["DAEM0NMCP_EDIT_HOST_WORKSPACE_BINDING_FILE"])
    assert secret not in (project / "opencode.json").read_text(encoding="utf-8")
    assert secret not in host_config.read_text(encoding="utf-8")
    assert secret not in binding.read_text(encoding="utf-8")


def test_install_plugin_idempotent(tmp_path):
    """Running install-opencode twice reports [exists] on second run."""
    ok1, _msg1 = install_opencode(
        str(tmp_path), bridge_config_root=tmp_path / "host-config"
    )
    assert ok1

    plugin = tmp_path / ".opencode" / "plugins" / "daem0n.ts"
    content_after_first = plugin.read_text(encoding="utf-8")

    ok2, msg2 = install_opencode(
        str(tmp_path), bridge_config_root=tmp_path / "host-config"
    )
    assert ok2

    assert "[exists] .opencode/plugins/daem0n.ts" in msg2
    assert plugin.read_text(encoding="utf-8") == content_after_first


def test_install_plugin_force_overwrites(tmp_path):
    """Running install-opencode --force overwrites an existing plugin file."""
    ok1, _msg1 = install_opencode(
        str(tmp_path), bridge_config_root=tmp_path / "host-config"
    )
    assert ok1

    plugin = tmp_path / ".opencode" / "plugins" / "daem0n.ts"
    plugin.write_text("// corrupted content", encoding="utf-8")

    ok2, msg2 = install_opencode(
        str(tmp_path), force=True, bridge_config_root=tmp_path / "host-config"
    )
    assert ok2
    assert "[overwrite] .opencode/plugins/daem0n.ts" in msg2

    restored = plugin.read_text(encoding="utf-8")
    assert restored == PLUGIN_TEMPLATE


def test_install_dry_run_no_plugin_file(tmp_path):
    """Dry-run mentions daem0n.ts but does NOT create the file."""
    ok, msg = install_opencode(str(tmp_path), dry_run=True)
    assert ok, msg

    plugin = tmp_path / ".opencode" / "plugins" / "daem0n.ts"
    assert not plugin.exists(), "Plugin file should NOT be created in dry-run"
    assert "daem0n.ts" in msg


def test_plugin_template_matches_canonical():
    """PLUGIN_TEMPLATE contains all key structural markers."""
    # Key export and hook names
    assert "Daem0nPlugin" in PLUGIN_TEMPLATE
    assert "experimental.chat.system.transform" in PLUGIN_TEMPLATE
    assert "tool.execute.before" in PLUGIN_TEMPLATE
    assert "tool.execute.after" in PLUGIN_TEMPLATE
    assert "pre_edit" in PLUGIN_TEMPLATE
    assert "pre_bash" in PLUGIN_TEMPLATE
    assert "post_edit_preflight" in PLUGIN_TEMPLATE
    assert 'stdin: "pipe"' in PLUGIN_TEMPLATE
    assert "child.stdin.write(JSON.stringify(event))" in PLUGIN_TEMPLATE
    assert "child.stdin.end()" in PLUGIN_TEMPLATE
    for name in (
        "DAEM0NMCP_EDIT_BRIDGE_MODE",
        "DAEM0NMCP_EDIT_BRIDGE_REMOTE_URL",
        "DAEM0NMCP_EDIT_BRIDGE_CA_FILE",
        "DAEM0NMCP_EDIT_HOST_WORKSPACE_BINDING_FILE",
    ):
        assert name in PLUGIN_TEMPLATE
    assert (
        'new Set(["edit", "write", "notebookedit", "apply_patch"])' in PLUGIN_TEMPLATE
    )
    assert 't.includes("edit")' not in PLUGIN_TEMPLATE
    assert "COVENANT_RULES" in PLUGIN_TEMPLATE
    assert "daem0n-covenant" in PLUGIN_TEMPLATE

    # Must be TypeScript, not Python
    assert "import asyncio" not in PLUGIN_TEMPLATE
    assert "from daem0nmcp" not in PLUGIN_TEMPLATE


def test_plugin_template_is_the_exact_v7_repo_plugin_without_forged_metadata():
    canonical = (
        Path(__file__).resolve().parents[1] / ".opencode" / "plugins" / "daem0n.ts"
    ).read_text(encoding="utf-8")
    assert canonical == PLUGIN_TEMPLATE
    for retired in (
        "daem0nmcp_commune",
        "daem0nmcp_consult",
        "daem0nmcp_inscribe",
        "daem0nmcp_reflect",
        "_client_meta",
    ):
        assert retired not in PLUGIN_TEMPLATE


def test_install_preserves_existing_opencode_json(tmp_path):
    """Existing opencode.json is preserved; plugin file still created."""
    # Write a custom opencode.json before installing
    custom_json = '{"custom": true}\n'
    json_path = tmp_path / "opencode.json"
    json_path.write_text(custom_json, encoding="utf-8")

    ok, msg = install_opencode(
        str(tmp_path), bridge_config_root=tmp_path / "host-config"
    )
    assert ok, msg

    # opencode.json should be untouched (no --force)
    assert json_path.read_text(encoding="utf-8") == custom_json
    assert "[exists] opencode.json" in msg

    # Plugin file should still be created independently
    plugin = tmp_path / ".opencode" / "plugins" / "daem0n.ts"
    assert plugin.exists(), (
        "Plugin file should be created even when opencode.json exists"
    )


def test_install_creates_repo_command_files_without_overwriting(tmp_path):
    """The installer ships the repo's slash commands and keeps user edits."""
    expected = _repo_commands()
    assert set(expected) == {"commune.md", "counsel.md", "inscribe.md", "recall.md"}
    commands = tmp_path / ".opencode" / "commands"
    commands.mkdir(parents=True)
    (commands / "recall.md").write_text("my recall", encoding="utf-8")

    ok, msg = install_opencode(
        str(tmp_path), bridge_config_root=tmp_path / "host-config"
    )

    assert ok, msg
    assert "[exists] .opencode/commands/recall.md" in msg
    assert "[create] .opencode/commands/commune.md" in msg
    assert (commands / "recall.md").read_text(encoding="utf-8") == "my recall"
    for name in ("commune.md", "counsel.md", "inscribe.md"):
        assert (commands / name).read_text(encoding="utf-8") == expected[name]


def _repo_commands() -> dict[str, str]:
    repo_commands = Path(__file__).resolve().parents[1] / ".opencode" / "commands"
    return {
        path.name: path.read_text(encoding="utf-8")
        for path in repo_commands.glob("*.md")
    }


def test_packaged_commands_match_the_repo_commands():
    """The shipped copies must not drift from the ones the repo uses."""
    assert _repo_commands() == COMMAND_TEMPLATES


def test_force_overwrites_commands_and_dry_run_writes_nothing(tmp_path):
    ok, msg = install_opencode(
        str(tmp_path), dry_run=True, bridge_config_root=tmp_path / "host-config"
    )
    assert ok, msg
    assert "[create] .opencode/commands/recall.md" in msg
    assert not (tmp_path / ".opencode" / "commands" / "recall.md").exists()

    assert install_opencode(str(tmp_path), bridge_config_root=tmp_path / "host-config")[
        0
    ]
    recall = tmp_path / ".opencode" / "commands" / "recall.md"
    recall.write_text("edited", encoding="utf-8")

    ok, msg = install_opencode(
        str(tmp_path), force=True, bridge_config_root=tmp_path / "host-config"
    )

    assert ok, msg
    assert "[overwrite] .opencode/commands/recall.md" in msg
    assert recall.read_text(encoding="utf-8") == COMMAND_TEMPLATES["recall.md"]


def test_installer_refuses_to_write_through_a_symlink(tmp_path):
    """A cloned repo must not steer the installer outside the project."""
    outside = tmp_path / "outside"
    outside.mkdir()
    project = tmp_path / "project"
    (project / ".opencode" / "commands").mkdir(parents=True)
    link = project / ".opencode" / "commands" / "recall.md"
    try:
        link.symlink_to(outside / "stolen.md")
    except (OSError, NotImplementedError):
        pytest.skip("symlinks are unavailable")

    ok, msg = install_opencode(
        str(project), bridge_config_root=tmp_path / "host-config"
    )

    assert not ok
    assert "link" in msg
    assert not (outside / "stolen.md").exists()
