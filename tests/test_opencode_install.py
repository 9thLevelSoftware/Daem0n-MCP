"""Tests for OpenCode installer plugin installation behavior."""

from __future__ import annotations

from daem0nmcp.opencode_install import PLUGIN_TEMPLATE, install_opencode


def test_install_creates_plugin_file(tmp_path):
    """Running install-opencode creates the plugin file automatically."""
    ok, msg = install_opencode(str(tmp_path))
    assert ok, msg

    plugin = tmp_path / ".opencode" / "plugins" / "daem0n.ts"
    assert plugin.exists(), "Plugin file should be created"

    content = plugin.read_text(encoding="utf-8")
    assert "Daem0nPlugin" in content
    assert "experimental.chat.system.transform" in content
    assert "pre_edit" in content
    assert "pre_bash" in content


def test_install_plugin_idempotent(tmp_path):
    """Running install-opencode twice reports [exists] on second run."""
    ok1, _msg1 = install_opencode(str(tmp_path))
    assert ok1

    plugin = tmp_path / ".opencode" / "plugins" / "daem0n.ts"
    content_after_first = plugin.read_text(encoding="utf-8")

    ok2, msg2 = install_opencode(str(tmp_path))
    assert ok2

    assert "[exists] .opencode/plugins/daem0n.ts" in msg2
    assert plugin.read_text(encoding="utf-8") == content_after_first


def test_install_plugin_force_overwrites(tmp_path):
    """Running install-opencode --force overwrites an existing plugin file."""
    ok1, _msg1 = install_opencode(str(tmp_path))
    assert ok1

    plugin = tmp_path / ".opencode" / "plugins" / "daem0n.ts"
    plugin.write_text("// corrupted content", encoding="utf-8")

    ok2, msg2 = install_opencode(str(tmp_path), force=True)
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
    assert "COVENANT_RULES" in PLUGIN_TEMPLATE
    assert "daem0n-covenant" in PLUGIN_TEMPLATE

    # Must be TypeScript, not Python
    assert "import asyncio" not in PLUGIN_TEMPLATE
    assert "from daem0nmcp" not in PLUGIN_TEMPLATE


def test_install_preserves_existing_opencode_json(tmp_path):
    """Existing opencode.json is preserved; plugin file still created."""
    # Write a custom opencode.json before installing
    custom_json = '{"custom": true}\n'
    json_path = tmp_path / "opencode.json"
    json_path.write_text(custom_json, encoding="utf-8")

    ok, msg = install_opencode(str(tmp_path))
    assert ok, msg

    # opencode.json should be untouched (no --force)
    assert json_path.read_text(encoding="utf-8") == custom_json
    assert "[exists] opencode.json" in msg

    # Plugin file should still be created independently
    plugin = tmp_path / ".opencode" / "plugins" / "daem0n.ts"
    assert plugin.exists(), (
        "Plugin file should be created even when opencode.json exists"
    )
