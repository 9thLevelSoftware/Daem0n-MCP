"""Tests for the Claude Code hook installer."""

import json
import shutil
import ssl
from pathlib import Path

import pytest

from daem0nmcp.claude_hooks.install import (
    _is_daem0n_entry,
    install_claude_hooks,
    uninstall_claude_hooks,
)
from daem0nmcp.edit_bridge_transport import provision_bridge_credential


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
        staging = hooks["PostToolUse"][0]
        assert staging["matcher"] == "mcp__.*__edit_preflight"
        assert "post_edit_preflight" in staging["hooks"][0]["command"]
        assert hooks["PostToolUse"][1]["matcher"] == hooks["PreToolUse"][0]["matcher"]
        assert "NotebookEdit" in hooks["PostToolUse"][1]["matcher"].split("|")

    def test_project_pairing_writes_paths_without_secret(self, fake_settings, tmp_path):
        project = tmp_path / "project"
        project.mkdir()
        host_config = tmp_path / "host-config"

        ok, message = install_claude_hooks(
            project_path=project,
            bridge_config_root=host_config,
        )
        assert ok, message
        local_settings = project / ".claude" / "settings.local.json"
        environment = json.loads(local_settings.read_text(encoding="utf-8"))["env"]
        credential = Path(environment["DAEM0NMCP_EDIT_BRIDGE_CREDENTIAL_FILE"])
        assert credential.is_file()
        secret = json.loads(credential.read_text(encoding="utf-8"))["secret"]
        assert secret not in local_settings.read_text(encoding="utf-8")
        assert environment["DAEM0NMCP_PROJECT_ROOT"] == str(project.resolve())

    def test_remote_pairing_writes_protected_workspace_binding(
        self, fake_settings, tmp_path, monkeypatch
    ):
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

        ok, message = install_claude_hooks(
            project_path=project,
            remote_workspace_id="ws_" + "7" * 24,
            remote_credential_path=credential,
            remote_base_url="https://server.example:7443",
            remote_ca_file=ca_file,
            remote_origin="https://desktop.example",
        )

        assert ok, message
        settings = project / ".claude" / "settings.local.json"
        environment = json.loads(settings.read_text(encoding="utf-8"))["env"]
        assert environment["DAEM0NMCP_EDIT_BRIDGE_MODE"] == "remote-https"
        for name in (
            "DAEM0NMCP_EDIT_BRIDGE_REMOTE_URL",
            "DAEM0NMCP_EDIT_BRIDGE_CA_FILE",
            "DAEM0NMCP_EDIT_BRIDGE_ORIGIN",
        ):
            assert name not in environment
        binding = Path(environment["DAEM0NMCP_EDIT_HOST_WORKSPACE_BINDING_FILE"])
        assert binding.parent == credential.parent.resolve()
        assert secret not in settings.read_text(encoding="utf-8")
        assert secret not in binding.read_text(encoding="utf-8")

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
        for _event, entries in hooks.items():
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

    def test_removes_legacy_manual_hook_entries(self, fake_settings):
        fake_settings.write_text(
            json.dumps(
                {
                    "hooks": {
                        name: [
                            {
                                "matcher": "",
                                "hooks": [
                                    {
                                        "type": "command",
                                        "command": f'python3 "$HOME/Daem0nMCP/hooks/{script}"',
                                    }
                                ],
                            }
                        ]
                        for name, script in (
                            ("UserPromptSubmit", "daem0n_prompt_hook.py"),
                            ("PreToolUse", "daem0n_pre_edit_hook.py"),
                            ("PostToolUse", "daem0n_post_edit_hook.py"),
                            ("Stop", "daem0n_stop_hook.py"),
                        )
                    }
                }
            )
        )

        ok, _msg = uninstall_claude_hooks()

        assert ok
        assert json.loads(fake_settings.read_text())["hooks"] == {}

    def test_project_cleanup_strips_pairing_env_and_credentials(
        self, fake_settings, tmp_path
    ):
        project = tmp_path / "project"
        project.mkdir()
        host_config = tmp_path / "host-config"
        ok, message = install_claude_hooks(
            project_path=project, bridge_config_root=host_config
        )
        assert ok, message
        local_settings = project / ".claude" / "settings.local.json"
        value = json.loads(local_settings.read_text(encoding="utf-8"))
        value["env"]["USER_KEY"] = "kept"
        value["permissions"] = {"allow": ["Bash(ls)"]}
        local_settings.write_text(json.dumps(value), encoding="utf-8")
        credential = Path(value["env"]["DAEM0NMCP_EDIT_BRIDGE_CREDENTIAL_FILE"])
        runtime = Path(value["env"]["DAEM0NMCP_EDIT_BRIDGE_RUNTIME_DIR"])
        runtime.mkdir(parents=True)

        ok, message = uninstall_claude_hooks(
            dry_run=True,
            project_path=project,
            remove_credentials=True,
            bridge_config_root=host_config,
        )
        assert ok, message
        assert credential.is_file() and runtime.is_dir()
        assert "DAEM0NMCP_PROJECT_ROOT" in local_settings.read_text(encoding="utf-8")

        ok, message = uninstall_claude_hooks(
            project_path=project,
            remove_credentials=True,
            bridge_config_root=host_config,
        )

        assert ok, message
        assert json.loads(local_settings.read_text(encoding="utf-8")) == {
            "env": {"USER_KEY": "kept"},
            "permissions": {"allow": ["Bash(ls)"]},
        }
        assert not credential.parent.exists()
        assert not runtime.exists()
        assert runtime.parent.name == "run"

    def test_project_cleanup_without_credentials_keeps_them(
        self, fake_settings, tmp_path
    ):
        project = tmp_path / "project"
        project.mkdir()
        host_config = tmp_path / "host-config"
        install_claude_hooks(project_path=project, bridge_config_root=host_config)
        local_settings = project / ".claude" / "settings.local.json"
        credential = Path(
            json.loads(local_settings.read_text(encoding="utf-8"))["env"][
                "DAEM0NMCP_EDIT_BRIDGE_CREDENTIAL_FILE"
            ]
        )

        ok, message = uninstall_claude_hooks(project_path=project)

        assert ok, message
        assert json.loads(local_settings.read_text(encoding="utf-8")) == {}
        assert credential.is_file()


def _paired_project(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    host_config = tmp_path / "host-config"
    ok, message = install_claude_hooks(
        project_path=project, bridge_config_root=host_config
    )
    assert ok, message
    local_settings = project / ".claude" / "settings.local.json"
    env = json.loads(local_settings.read_text(encoding="utf-8"))["env"]
    credential_dir = Path(env["DAEM0NMCP_EDIT_BRIDGE_CREDENTIAL_FILE"]).parent
    runtime = Path(env["DAEM0NMCP_EDIT_BRIDGE_RUNTIME_DIR"])
    runtime.mkdir(parents=True)
    return project, host_config, local_settings, credential_dir, runtime


class TestUninstallRobustness:
    def test_unpaired_project_keeps_general_daem0n_keys(self, fake_settings, tmp_path):
        project = tmp_path / "project"
        (project / ".claude").mkdir(parents=True)
        local_settings = project / ".claude" / "settings.local.json"
        original = json.dumps(
            {"env": {"DAEM0NMCP_PROJECT_ROOT": "x", "DAEM0NMCP_STORAGE_PATH": "y"}}
        )
        local_settings.write_text(original, encoding="utf-8")

        ok, _message = uninstall_claude_hooks(project_path=project)

        assert ok
        assert local_settings.read_text(encoding="utf-8") == original

    def test_bom_prefixed_local_settings_are_cleaned(self, fake_settings, tmp_path):
        project, _host, local_settings, _cred, _runtime = _paired_project(tmp_path)
        text = local_settings.read_text(encoding="utf-8")
        local_settings.write_bytes(b"\xef\xbb\xbf" + text.encode("utf-8"))

        ok, message = uninstall_claude_hooks(project_path=project)

        assert ok, message
        assert json.loads(local_settings.read_text(encoding="utf-8")) == {}

    def test_malformed_local_settings_report_partial_success(
        self, fake_settings, tmp_path
    ):
        project, host, local_settings, credential_dir, runtime = _paired_project(
            tmp_path
        )
        local_settings.write_bytes(b'{"env": {bad json')

        ok, message = uninstall_claude_hooks(
            project_path=project, remove_credentials=True, bridge_config_root=host
        )

        assert not ok
        assert "Removed Daem0n hooks from" in message
        assert f"Left {local_settings.resolve()} unchanged" in message
        assert local_settings.read_bytes() == b'{"env": {bad json'
        assert json.loads(fake_settings.read_text())["hooks"] == {}
        assert not credential_dir.exists() and not runtime.exists()

    def test_deleted_project_reports_what_was_and_was_not_done(
        self, fake_settings, tmp_path
    ):
        project, host, _local, credential_dir, _runtime = _paired_project(tmp_path)
        shutil.rmtree(project)

        ok, message = uninstall_claude_hooks(
            project_path=project, remove_credentials=True, bridge_config_root=host
        )

        assert not ok
        assert "Removed Daem0n hooks from" in message
        assert "Could not locate edit bridge credentials" in message
        assert "by hand" in message
        assert credential_dir.exists()

    def test_remove_credentials_without_project_says_so(self, fake_settings):
        ok, message = uninstall_claude_hooks(remove_credentials=True)
        assert ok
        assert "No project given; edit bridge credentials kept." in message

    @pytest.mark.parametrize(
        "content",
        ['{bad json, "theme": "dark"', '{"hooks": []}', '{"hooks": {"Stop": {}}}'],
    )
    def test_malformed_user_settings_are_never_overwritten(
        self, fake_settings, content
    ):
        fake_settings.write_text(content, encoding="utf-8")

        installed, install_message = install_claude_hooks()
        removed, uninstall_message = uninstall_claude_hooks()

        assert not installed and not removed
        assert "Nothing was changed" in install_message
        assert "Nothing was changed" in uninstall_message
        assert fake_settings.read_text(encoding="utf-8") == content

    def test_non_object_hook_entries_are_kept(self, fake_settings):
        fake_settings.write_text(
            json.dumps({"hooks": {"Stop": ["odd", {"hooks": "odd"}]}}),
            encoding="utf-8",
        )
        ok, message = install_claude_hooks()
        assert ok, message
        stop = json.loads(fake_settings.read_text())["hooks"]["Stop"]
        assert stop[:2] == ["odd", {"hooks": "odd"}]
