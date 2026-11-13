"""Explicit automation keeps native registration and existing policy bounded."""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

from daem0nmcp.client_install import ClientInstallError, install_client


def _install(root: Path, **kwargs):
    return install_client("opencode", root, client_version="1.18.21", **kwargs)


def _tree(root: Path):
    return {
        str(path.relative_to(root)): path.read_bytes()
        for path in root.rglob("*")
        if path.is_file()
    }


@pytest.mark.parametrize("client", ["codex", "agy", "omp", "pi"])
def test_unvalidated_adapters_are_rejected_before_writes(tmp_path, client):
    with pytest.raises(ClientInstallError) as caught:
        install_client(client, tmp_path, automation="enforce")
    assert caught.value.code == "UNSUPPORTED_AUTOMATION"
    assert list(tmp_path.iterdir()) == []


def test_omitted_automation_and_explicit_off_do_not_install_a_plugin(tmp_path):
    omitted = _install(tmp_path)
    assert omitted["automation"] == {
        "mode": "unchanged",
        "covered_tools": [],
        "enforced": None,
        "events": [],
    }
    before = _tree(tmp_path)
    off = _install(tmp_path, automation="off")
    assert off["automation"]["mode"] == "off"
    assert _tree(tmp_path) == before
    assert not (tmp_path / ".opencode").exists()


def test_reminder_install_is_idempotent_and_preserves_host_siblings(tmp_path):
    host = tmp_path / ".opencode/daem0n-host.json"
    host.parent.mkdir()
    host.write_text('{"sentinel":"keep", "environment":{"KEEP":"yes"}}\n')
    result = _install(tmp_path, automation="remind")
    assert result["automation"]["covered_tools"] == ["edit", "write", "apply_patch"]
    value = json.loads(host.read_text())
    assert value["sentinel"] == "keep"
    assert value["environment"]["KEEP"] == "yes"
    assert value["environment"]["DAEM0NMCP_AUTOMATION_MODE"] == "remind"
    assert (tmp_path / ".opencode/plugins/daem0n.ts").is_file()
    assert not any("CREDENTIAL" in key for key in value["environment"])
    before = _tree(tmp_path)
    repeated = _install(tmp_path, automation="remind")
    assert all(item["action"] == "unchanged" for item in repeated["files"])
    assert _tree(tmp_path) == before


def test_explicit_off_disables_owned_plugin_without_deleting_it(tmp_path):
    _install(tmp_path, automation="remind")
    plugin = tmp_path / ".opencode/plugins/daem0n.ts"
    result = _install(tmp_path, automation="off")
    host = json.loads((tmp_path / ".opencode/daem0n-host.json").read_text())
    assert host["environment"]["DAEM0NMCP_AUTOMATION_MODE"] == "off"
    assert 'INSTALLED_AUTOMATION_MODE: string | undefined = "off"' in plugin.read_text()
    assert result["automation"]["covered_tools"] == []


def test_automation_refuses_modified_plugin_and_preserves_all_files(tmp_path):
    _install(tmp_path, automation="remind")
    plugin = tmp_path / ".opencode/plugins/daem0n.ts"
    plugin.write_text(plugin.read_text() + "\n// local customization\n")
    before = _tree(tmp_path)
    with pytest.raises(ClientInstallError) as caught:
        _install(
            tmp_path,
            automation="enforce",
            bridge_config_root=tmp_path.parent / "private",
        )
    assert caught.value.code == "CONFIG_CONFLICT"
    assert _tree(tmp_path) == before
    assert not (tmp_path.parent / "private").exists()


@pytest.mark.parametrize(
    "client,relative", [("codex", ".codex/hooks.json"), ("agy", ".agents/hooks.json")]
)
def test_native_reminder_install_preserves_other_hooks_and_off_removes_own(
    tmp_path, client, relative
):
    hooks = tmp_path / relative
    hooks.parent.mkdir()
    sibling = {
        "matcher": "keep",
        "hooks": [{"type": "command", "command": "keep-command"}],
    }
    value = (
        {"hooks": {"PreToolUse": [sibling]}}
        if client == "codex"
        else {"unrelated": {"PreToolUse": [sibling]}}
    )
    hooks.write_text(json.dumps(value))
    result = install_client(client, tmp_path, automation="remind")
    assert result["automation"]["events"] == ["SessionStart", "PreToolUse", "Stop"]
    assert result["automation"]["enforced"] is False
    assert "daem0nmcp.native_reminders" in hooks.read_text()
    first = _tree(tmp_path)
    install_client(client, tmp_path, automation="remind")
    assert _tree(tmp_path) == first
    install_client(client, tmp_path, automation="off")
    actual = json.loads(hooks.read_text())
    assert "daem0nmcp.native_reminders" not in hooks.read_text()
    assert actual == value


def test_agy_reminder_matches_observed_native_edit_aliases_without_read_coverage(
    tmp_path,
):
    installed = install_client("agy", tmp_path, automation="remind")
    hooks = json.loads((tmp_path / ".agents/hooks.json").read_text())
    matcher = re.compile(hooks["daem0nmcp"]["PreToolUse"][0]["matcher"])
    # Actual native hook payloads from agy 1.4.7 use these names and arguments.
    native_calls = [
        {"name": "write_file", "args": {"Path": "owned.txt", "Content": "after\n"}},
        {
            "name": "edit_file",
            "args": {"Path": "owned.txt", "Target": "before", "Replacement": "after"},
        },
    ]
    assert all(matcher.fullmatch(call["name"]) for call in native_calls)
    assert all(
        matcher.fullmatch(name)
        for name in (
            "write_to_file",
            "replace_file_content",
            "multi_replace_file_content",
        )
    )
    assert not any(
        matcher.fullmatch(name)
        for name in ("read_file", "run_command", "notebook_edit")
    )
    assert set(installed["automation"]["covered_tools"]) == {
        "write_to_file",
        "replace_file_content",
        "multi_replace_file_content",
        "write_file",
        "edit_file",
    }
    assert installed["automation"]["enforced"] is False


@pytest.mark.parametrize(
    "client,package",
    [("omp", "@oh-my-pi/pi-coding-agent"), ("pi", "@earendil-works/pi-coding-agent")],
)
def test_extension_reminders_are_scoped_and_disabled_without_deletion(
    tmp_path, client, package
):
    result = install_client(client, tmp_path, automation="remind")
    extension = tmp_path / f".{client}/extensions/daem0n.ts"
    content = extension.read_text()
    assert package in content and 'pi.on("before_agent_start"' in content
    assert "const ENABLED = true" in content
    assert result["automation"]["covered_tools"] == []
    assert result["automation"]["events"] == ["before_agent_start"]
    first = _tree(tmp_path)
    install_client(client, tmp_path, automation="remind")
    assert _tree(tmp_path) == first
    install_client(client, tmp_path, automation="off")
    assert extension.exists() and "const ENABLED = false" in extension.read_text()
    assert not (tmp_path / ".daem0nmcp/storage").exists()


@pytest.mark.parametrize("client", ["omp", "pi"])
def test_customized_reminder_extension_is_never_overwritten(tmp_path, client):
    install_client(client, tmp_path, automation="remind")
    extension = tmp_path / f".{client}/extensions/daem0n.ts"
    extension.write_text(extension.read_text() + "// customization\n")
    first = _tree(tmp_path)
    with pytest.raises(ClientInstallError) as caught:
        install_client(client, tmp_path, automation="off")
    assert caught.value.code == "CONFIG_CONFLICT"
    assert _tree(tmp_path) == first


def test_enforcement_dry_run_and_parse_failure_do_not_pair_bridge(tmp_path):
    private = tmp_path.parent / (tmp_path.name + "-credentials")
    preview = _install(
        tmp_path, automation="enforce", bridge_config_root=private, dry_run=True
    )
    assert preview["automation"]["enforced"] is True
    assert list(tmp_path.iterdir()) == []
    assert not private.exists()
    (tmp_path / "AGENTS.md").write_text("<!-- daem0nmcp:v7:start -->\nunfinished")
    before = _tree(tmp_path)
    with pytest.raises(ClientInstallError) as caught:
        _install(tmp_path, automation="enforce", bridge_config_root=private)
    assert caught.value.code == "INSTRUCTION_CONFLICT"
    assert _tree(tmp_path) == before
    assert not private.exists()


def test_enforcement_pairs_outside_project_and_merges_existing_jsonc(tmp_path):
    private = tmp_path.parent / (tmp_path.name + "-credentials")
    config = tmp_path / "opencode.jsonc"
    config.write_text(
        '{\n  // unrelated policy\n  "permissions":{"edit":"ask"},\n  "mcp":{"sentinel":{"type":"local","command":["keep"]}}\n}\n'
    )
    _install(tmp_path)
    result = _install(tmp_path, automation="enforce", bridge_config_root=private)
    from daem0nmcp.client_install import _json_document

    value = _json_document(config.read_text(), comments=True).value
    assert "// unrelated policy" in config.read_text()
    assert value["permissions"] == {"edit": "ask"}
    assert value["mcp"]["sentinel"]["command"] == ["keep"]
    environment = value["mcp"]["daem0nmcp"]["environment"]
    credential = Path(environment["DAEM0NMCP_EDIT_BRIDGE_CREDENTIAL_FILE"])
    assert credential.is_file() and credential.is_relative_to(private)
    assert environment["DAEM0NMCP_AUTOMATION_MODE"] == "enforce"
    assert result["automation"]["enforced"] is True
    before = _tree(tmp_path)
    repeated = _install(tmp_path, automation="enforce", bridge_config_root=private)
    assert _tree(tmp_path) == before
    assert all(item["action"] == "unchanged" for item in repeated["files"])


@pytest.mark.parametrize("code_mode", [None, "direct"])
def test_v2_uses_explicit_schema_and_preserves_unrelated_settings(tmp_path, code_mode):
    config = tmp_path / "opencode.jsonc"
    config.write_text(
        '{\n// V2 sentinel\n"mcp":{"timeout":{"execution":60000},"servers":{"other":{"type":"local","command":["keep"]}}}\n}\n'
    )
    kwargs = {"opencode_code_mode": code_mode} if code_mode is not None else {}
    result = install_client(
        "opencode", tmp_path, client_version="2.0.0", opencode_interface="v2", **kwargs
    )
    from daem0nmcp.client_install import _json_document

    value = _json_document(config.read_text(), comments=True).value
    assert "// V2 sentinel" in config.read_text()
    assert value["mcp"]["timeout"] == {"execution": 60000}
    assert value["mcp"]["servers"]["other"]["command"] == ["keep"]
    server = value["mcp"]["servers"]["daem0nmcp"]
    assert server["disabled"] is False and "enabled" not in server
    assert server["pool"] is False and server["protocol"] == "legacy"
    assert server["codemode"] is (code_mode != "direct")
    assert server["timeout"]["execution"] == 600_000
    assert result["opencode_interface"] == "v2"
    before = _tree(tmp_path)
    install_client(
        "opencode", tmp_path, client_version="2.0.0", opencode_interface="v2", **kwargs
    )
    assert _tree(tmp_path) == before


def test_v2_does_not_reinterpret_v1_servers_or_claim_enforcement(tmp_path):
    config = tmp_path / "opencode.json"
    config.write_text('{"mcp":{"other":{"type":"local","command":["keep"]}}}')
    before = _tree(tmp_path)
    with pytest.raises(ClientInstallError) as caught:
        install_client(
            "opencode", tmp_path, client_version="2.0.0", opencode_interface="v2"
        )
    assert caught.value.code == "UNSUPPORTED_CLIENT_SCHEMA"
    with pytest.raises(ClientInstallError) as caught:
        install_client(
            "opencode",
            tmp_path,
            client_version="2.0.0",
            opencode_interface="v2",
            automation="enforce",
        )
    assert caught.value.code == "UNSUPPORTED_CLIENT_VERSION"
    assert _tree(tmp_path) == before


def test_v2_reminder_uses_separate_context_plugin_and_preserves_v1_asset(tmp_path):
    old = tmp_path / ".opencode/plugins/daem0n.ts"
    old.parent.mkdir(parents=True)
    old.write_text("// existing V1 user asset\n")
    installed = install_client(
        "opencode",
        tmp_path,
        client_version="2.2.7",
        opencode_interface="v2",
        automation="remind",
    )
    plugin = tmp_path / ".opencode/plugins/daem0n-v2.ts"
    source = plugin.read_text()
    assert "export default {" in source
    assert 'ctx.session.hook("context"' in source
    assert 'const AUTOMATION_MODE = "remind";' in source
    assert installed["automation"] == {
        "mode": "remind",
        "enforced": False,
        "covered_tools": [],
        "events": ["session.context"],
    }
    assert old.read_text() == "// existing V1 user asset\n"
    before = _tree(tmp_path)
    install_client(
        "opencode",
        tmp_path,
        client_version="2.2.7",
        opencode_interface="v2",
        automation="remind",
    )
    assert _tree(tmp_path) == before
    install_client(
        "opencode",
        tmp_path,
        client_version="2.2.7",
        opencode_interface="v2",
        automation="off",
    )
    assert 'const AUTOMATION_MODE = "off";' in plugin.read_text()
    assert old.read_text() == "// existing V1 user asset\n"


def test_v2_reminder_preview_and_customization_are_preserved(tmp_path):
    arguments = {
        "client_version": "2.2.7",
        "opencode_interface": "v2",
        "automation": "remind",
    }
    result = install_client("opencode", tmp_path, dry_run=True, **arguments)
    assert result["automation"]["events"] == ["session.context"]
    assert list(tmp_path.iterdir()) == []
    install_client("opencode", tmp_path, **arguments)
    plugin = tmp_path / ".opencode/plugins/daem0n-v2.ts"
    plugin.write_text(plugin.read_text() + "// user extension\n")
    before = _tree(tmp_path)
    with pytest.raises(ClientInstallError, match="managed copy"):
        install_client("opencode", tmp_path, **arguments)
    assert _tree(tmp_path) == before


def test_v2_enforcement_pairs_separate_asset_and_reports_native_tool_names(tmp_path):
    private = tmp_path.parent / (tmp_path.name + "-private-v2")
    result = install_client(
        "opencode",
        tmp_path,
        client_version="2.2.7",
        opencode_interface="v2",
        automation="enforce",
        bridge_config_root=private,
    )
    assert result["automation"] == {
        "mode": "enforce",
        "enforced": True,
        "covered_tools": ["edit", "write", "patch"],
        "events": ["session.context", "tool.execute.before", "tool.execute.after"],
    }
    source = (tmp_path / ".opencode/plugins/daem0n-v2.ts").read_text()
    assert 'const AUTOMATION_MODE = "enforce";' in source
    assert 'new Set(["edit", "write", "patch"])' in source
    assert not (tmp_path / ".opencode/plugins/daem0n.ts").exists()
    host = json.loads((tmp_path / ".opencode/daem0n-v2-host.json").read_text())
    assert Path(host["environment"]["DAEM0NMCP_EDIT_BRIDGE_CREDENTIAL_FILE"]).is_file()


@pytest.mark.parametrize("client", ["codex", "agy", "omp", "pi"])
def test_native_reminder_preview_is_nonmutating(tmp_path, client):
    result = install_client(client, tmp_path, automation="remind", dry_run=True)
    assert result["automation"]["mode"] == "remind"
    assert list(tmp_path.iterdir()) == []


def test_codex_off_preserves_siblings_inside_a_managed_hook_entry(tmp_path):
    install_client("codex", tmp_path, automation="remind")
    hooks = tmp_path / ".codex/hooks.json"
    value = json.loads(hooks.read_text())
    sibling = {"type": "command", "command": "keep-command"}
    mcp_sibling = {"type": "mcp", "server": "keep-server", "tool": "keep-tool"}
    value["hooks"]["PreToolUse"][0]["hooks"].extend([sibling, mcp_sibling])
    hooks.write_text(json.dumps(value))
    install_client("codex", tmp_path, automation="off")
    actual = json.loads(hooks.read_text())
    assert actual["hooks"]["PreToolUse"][0]["hooks"] == [sibling, mcp_sibling]
    assert "daem0nmcp.native_reminders" not in hooks.read_text()


def test_legacy_storage_is_not_migrated_by_enforcement_preview(tmp_path):
    legacy = tmp_path / ".devilmcp/storage/daem0nmcp.db"
    legacy.parent.mkdir(parents=True)
    legacy.write_bytes(b"legacy sentinel")
    before = _tree(tmp_path)
    with pytest.raises(ClientInstallError) as caught:
        _install(tmp_path, automation="enforce", dry_run=True)
    assert caught.value.code == "LEGACY_STORAGE_REQUIRES_MIGRATION"
    assert _tree(tmp_path) == before


def test_publication_failure_reports_prepared_bridge_credential(tmp_path, monkeypatch):
    from daem0nmcp import client_install

    private = tmp_path.parent / (tmp_path.name + "-credentials")

    def fail_publication(*args, **kwargs):
        raise OSError("simulated guarded publication failure")

    monkeypatch.setattr(client_install, "_publish", fail_publication)
    with pytest.raises(ClientInstallError) as caught:
        _install(tmp_path, automation="enforce", bridge_config_root=private)
    assert caught.value.code == "WRITE_FAILED"
    assert len(caught.value.files) == 1
    receipt = caught.value.files[0]
    assert receipt["action"] == "created"
    assert Path(receipt["path"]).is_file()
    assert Path(receipt["path"]).is_relative_to(private)
    assert not (tmp_path / "opencode.json").exists()


def test_omitted_mode_preserves_installed_reminders_without_claiming_disabled(tmp_path):
    _install(tmp_path, automation="remind")
    before = _tree(tmp_path)
    result = _install(tmp_path)
    assert _tree(tmp_path) == before
    assert result["automation"]["mode"] == "unchanged"
    assert result["automation"]["enforced"] is None


@pytest.mark.parametrize("client", ["codex", "agy"])
@pytest.mark.parametrize("event", ["SessionStart", "PreToolUse", "Stop"])
def test_rendered_native_reminder_command_executes_packaged_module(
    tmp_path, client, event
):
    from daem0nmcp.client_install import _reminder_hook_command

    command = _reminder_hook_command(sys.executable, client, event)
    completed = subprocess.run(
        command,
        shell=True,
        cwd=tmp_path,
        input=json.dumps({"cwd": str(tmp_path), "hook_event_name": event}),
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert completed.returncode == 0, completed.stderr
    output = json.loads(completed.stdout)
    if client == "codex" and event != "Stop":
        assert output["hookSpecificOutput"]["hookEventName"] == event
        assert "Daem0n" in output["hookSpecificOutput"]["additionalContext"]
    elif client == "codex":
        assert "record the verified outcome" in output["systemMessage"]
    elif event == "SessionStart":
        assert "Daem0n" in output["injectSteps"][0]["systemMessage"]["systemMessage"]
    elif event == "PreToolUse":
        assert output["decision"] == "allow" and "Daem0n" in output["reason"]
    else:
        assert output == {}
