"""Check the instructions actually installed and injected into native contexts."""

import json
import shutil
import subprocess
import sys

import pytest

from daem0nmcp.api.v7.factory import build_inspectable_v7_server
from daem0nmcp.client_install import install_client
from daem0nmcp.opencode_install import render_plugin, render_v2_plugin
from tests.api_v7.test_tool_descriptions import _handlers, _Resources


def assert_task_scope(text):
    assert "system_health is optional" in text
    assert "indexing does not require a health check" in text
    assert "task's allowed operations" in text
    assert "Prefer directly listed typed tools" in text


def assert_v2_discovery_scope(text):
    assert 'opencode_list_mcp_resources({"server":"daem0nmcp"})' in text
    assert "Do not omit the server filter" in text
    assert "Resource discovery is not tool discovery" in text
    assert "unrelated configured servers" in text


@pytest.mark.parametrize("interface", ["v1", "v2"])
def test_installed_instructions_limit_diagnostics_and_scope_native_discovery(
    tmp_path, interface
):
    instructions = tmp_path / "AGENTS.md"
    instructions.write_text("User policy must remain.\n")
    kwargs = {
        "opencode_interface": interface,
        "client_version": "2.2.7" if interface == "v2" else "1.18.21",
        "tool_surface": "full",
    }
    install_client("opencode", tmp_path, **kwargs)
    text = instructions.read_text()
    assert text.startswith("User policy must remain.\n")
    assert_task_scope(text)
    if interface == "v2":
        assert_v2_discovery_scope(text)
    else:
        assert "opencode_list_mcp_resources" not in text
    before = instructions.read_bytes()
    install_client("opencode", tmp_path, **kwargs)
    assert instructions.read_bytes() == before


@pytest.mark.parametrize("variant", ["v1-minimax", "v1-anthropic", "v2"])
def test_actual_plugin_context_injection_has_coherent_task_scope(tmp_path, variant):
    bun = shutil.which("bun")
    if bun is None:
        pytest.skip("Bun runtime is not installed")
    plugin = tmp_path / "plugin.ts"
    plugin.write_text(
        render_v2_plugin(sys.executable, automation="enforce")
        if variant == "v2"
        else render_plugin(sys.executable, automation="enforce")
    )
    directory = json.dumps(str(tmp_path))
    if variant == "v2":
        probe = (
            'import plugin from "./plugin.ts";\n'
            "const hooks = new Map();\n"
            f"await plugin.setup({{location:{{directory:{directory}}},"
            "session:{hook:async(name,callback)=>hooks.set(name,callback)},"
            "tool:{hook:async()=>{}}});\n"
            "const event={system:[{type:'text',text:'existing context'}]};\n"
            "await hooks.get('context')(event);\n"
            "console.log(JSON.stringify(event.system.map(value=>value.text)));\n"
        )
    else:
        provider = "anthropic" if variant == "v1-anthropic" else "minimax-coding-plan"
        probe = (
            'import {Daem0nPlugin} from "./plugin.ts";\n'
            f"const hooks=await Daem0nPlugin({{directory:{directory}}});\n"
            "const event={system:['existing context']};\n"
            "await hooks['experimental.chat.system.transform']("
            f"{{model:{{providerID:{json.dumps(provider)},id:'MiniMax-M3'}}}},event);\n"
            "console.log(JSON.stringify(event.system));\n"
        )
    (tmp_path / "probe.ts").write_text(probe)
    result = subprocess.run(
        [bun, "run", str(tmp_path / "probe.ts")],
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode == 0, result.stderr
    contexts = json.loads(result.stdout)
    assert contexts[0] == "existing context"
    assert len(contexts) == 2
    assert_task_scope(contexts[1])
    if variant == "v2":
        assert_v2_discovery_scope(contexts[1])
    else:
        assert "opencode_list_mcp_resources" not in contexts[1]


def test_public_health_schema_does_not_advertise_a_required_startup_check():
    server = build_inspectable_v7_server(_handlers(), _Resources())
    description = next(
        tool["description"]
        for tool in server.list_tools()
        if tool["name"] == "system_health"
    )
    assert "Optional diagnostics" in description
    assert "not a prerequisite" in description
    assert len(description) <= 300
