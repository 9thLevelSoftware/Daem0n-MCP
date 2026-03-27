"""Tests for the Claude Code hook installer."""

import json

import pytest

from daem0nmcp.claude_hooks.install import (
    _is_daem0n_entry,
    install_claude_hooks,
    uninstall_claude_hooks,
)


@pytest.fixture
def fake_settings(tmp_path, monkeypatch):
    """Redirect settings to a temp dir and return the settings file path."""
    settings_dir = tmp_path / ".claude"
    settings_dir.mkdir()
    settings_file = settings_dir / "settings.json"

    monkeypatch.setattr(
        "daem0nmcp.claude_hooks.install._settings_path",
        lambda: settings_file,
    )

    return settings_file


class TestIsDevilEntry:
    def test_detects_new_hooks(self):
        entry = {
            "matcher": "Edit|Write",
            "hooks": [
                {
                    "type": "command",
                    "command": '"python" -m daem0nmcp.claude_hooks.pre_edit',
                }
            ],
        }
        assert _is_daem0n_entry(entry) is True

    def test_detects_legacy_hooks(self):
        entry = {
            "matcher": "Edit",
            "hooks": [
                {"type": "command", "command": "python hooks/daem0n_pre_edit_hook.py"}
            ],
        }
        assert _is_daem0n_entry(entry) is True

    def test_ignores_other_hooks(self):
        entry = {
            "matcher": "Edit",
            "hooks": [{"type": "command", "command": "eslint --fix"}],
        }
        assert _is_daem0n_entry(entry) is False


class TestInstall:
    def test_fresh_settings(self, fake_settings):
        ok, msg = install_claude_hooks()
        assert ok
        assert "Installed" in msg

        data = json.loads(fake_settings.read_text())
        hooks = data["hooks"]
        assert "SessionStart" in hooks
        assert "PreToolUse" in hooks
        assert "PostToolUse" in hooks
        assert "Stop" in hooks
        assert "SubagentStop" in hooks

        # Verify PreToolUse has 2 entries (Edit + Bash)
        assert len(hooks["PreToolUse"]) == 2

    def test_preserves_existing(self, fake_settings):
        # Write existing settings with a GSD hook
        existing = {
            "hooks": {
                "PreToolUse": [
                    {
                        "matcher": "Edit",
                        "hooks": [{"type": "command", "command": "gsd-check"}],
                    }
                ]
            }
        }
        fake_settings.write_text(json.dumps(existing))

        ok, msg = install_claude_hooks()
        assert ok

        data = json.loads(fake_settings.read_text())
        pre_tool = data["hooks"]["PreToolUse"]
        # Should have: GSD + 2 Daem0n
        assert len(pre_tool) == 3
        # GSD should be first (preserved)
        assert "gsd-check" in pre_tool[0]["hooks"][0]["command"]

    def test_replaces_legacy(self, fake_settings):
        existing = {
            "hooks": {
                "PreToolUse": [
                    {
                        "matcher": "Edit",
                        "hooks": [
                            {
                                "type": "command",
                                "command": "python hooks/daem0n_pre_edit_hook.py",
                            }
                        ],
                    }
                ]
            }
        }
        fake_settings.write_text(json.dumps(existing))

        ok, msg = install_claude_hooks()
        assert ok

        data = json.loads(fake_settings.read_text())
        pre_tool = data["hooks"]["PreToolUse"]
        # Legacy should be gone, only new Daem0n entries
        for entry in pre_tool:
            for hook in entry.get("hooks", []):
                assert "daem0n_pre_edit_hook" not in hook["command"]

    def test_removes_old_session_start_on_reinstall(self, fake_settings):
        existing = {
            "hooks": {
                "SessionStart": [
                    {
                        "matcher": "",
                        "hooks": [
                            {
                                "type": "command",
                                "command": '"python" -m daem0nmcp.claude_hooks.session_start',
                            }
                        ],
                    },
                    {
                        "matcher": "",
                        "hooks": [
                            {"type": "command", "command": "node gsd-check-update.js"}
                        ],
                    },
                ]
            }
        }
        fake_settings.write_text(json.dumps(existing))

        ok, _msg = install_claude_hooks()
        assert ok

        data = json.loads(fake_settings.read_text())
        session_start = data["hooks"].get("SessionStart", [])
        assert len(session_start) == 2
        # Non-Daem0n GSD entry is preserved
        assert "gsd-check-update.js" in session_start[0]["hooks"][0]["command"]
        # New Daem0n SessionStart entry is added
        assert (
            "daem0nmcp.claude_hooks.session_start"
            in session_start[1]["hooks"][0]["command"]
        )

    def test_dry_run_no_write(self, fake_settings):
        ok, msg = install_claude_hooks(dry_run=True)
        assert ok
        assert "[dry-run]" in msg
        # File should NOT exist (no write)
        assert not fake_settings.exists()


class TestUninstall:
    def test_removes_daem0n(self, fake_settings):
        # Install first
        install_claude_hooks()
        assert fake_settings.exists()

        ok, msg = uninstall_claude_hooks()
        assert ok
        assert "Removed" in msg

        data = json.loads(fake_settings.read_text())
        hooks = data.get("hooks", {})
        # All events should be empty (only had Daem0n entries)
        for event, entries in hooks.items():
            for entry in entries:
                assert not _is_daem0n_entry(entry)

    def test_preserves_others(self, fake_settings):
        # Install Daem0n + custom
        existing = {
            "hooks": {
                "PreToolUse": [
                    {
                        "matcher": "Edit",
                        "hooks": [{"type": "command", "command": "eslint --fix"}],
                    }
                ]
            }
        }
        fake_settings.write_text(json.dumps(existing))
        install_claude_hooks()

        ok, msg = uninstall_claude_hooks()
        assert ok

        data = json.loads(fake_settings.read_text())
        pre_tool = data["hooks"].get("PreToolUse", [])
        assert len(pre_tool) == 1
        assert "eslint" in pre_tool[0]["hooks"][0]["command"]

    def test_nothing_to_remove(self, fake_settings):
        fake_settings.write_text(json.dumps({"hooks": {}}))
        ok, msg = uninstall_claude_hooks()
        assert ok
        assert "No Daem0n hooks found" in msg

    def test_dry_run_no_write(self, fake_settings):
        install_claude_hooks()
        original = fake_settings.read_text()

        ok, msg = uninstall_claude_hooks(dry_run=True)
        assert ok
        assert "[dry-run]" in msg

        # File should be unchanged
        assert fake_settings.read_text() == original
