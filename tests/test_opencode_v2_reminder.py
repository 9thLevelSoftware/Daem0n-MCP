"""Exercise the V2 context callback with its declared plugin registration API."""

import json
import shutil
import subprocess
import sys

import pytest

from daem0nmcp.opencode_install import render_v2_plugin


@pytest.mark.parametrize("enabled", [True, False])
def test_v2_reminder_registers_context_without_executing_tools(tmp_path, enabled):
    bun = shutil.which("bun")
    if bun is None:
        pytest.skip("Bun runtime is not installed")
    source = render_v2_plugin(sys.executable, automation="remind" if enabled else "off")
    plugin = tmp_path / "plugin.ts"
    plugin.write_text(source)
    probe = tmp_path / "probe.ts"
    probe.write_text(
        'import plugin from "./plugin.ts";\n'
        "const callbacks = new Map();\n"
        f"await plugin.setup({{location:{{directory:{json.dumps(str(tmp_path))}}},session:{{hook: async (name, callback) => callbacks.set(name, callback)}}}});\n"
        "const event = {system:[{type:'text',text:'existing context'}]};\n"
        "if (callbacks.has('context')) callbacks.get('context')(event);\n"
        "console.log(JSON.stringify({events:[...callbacks.keys()],system:event.system}));\n"
    )
    completed = subprocess.run(
        [bun, "run", str(probe)], capture_output=True, text=True, timeout=15
    )
    assert completed.returncode == 0, completed.stderr
    result = json.loads(completed.stdout)
    assert result["events"] == (["context"] if enabled else [])
    assert result["system"][0] == {"type": "text", "text": "existing context"}
    assert len(result["system"]) == (2 if enabled else 1)
    if enabled:
        assert result["system"][1]["text"].startswith("Daem0n native V2 reminder:")


def test_v2_enforce_missing_sidecar_or_bridge_denies_before_native_execution(tmp_path):
    bun = shutil.which("bun")
    if bun is None:
        pytest.skip("Bun runtime is not installed")
    (tmp_path / ".daem0nmcp").mkdir()
    (tmp_path / "plugin.ts").write_text(
        render_v2_plugin(sys.executable, automation="enforce")
    )
    probe = tmp_path / "enforce.ts"
    probe.write_text(
        'import plugin from "./plugin.ts";\n'
        "const callbacks = new Map();\n"
        f"await plugin.setup({{location:{{directory:{json.dumps(str(tmp_path))}}},session:{{hook:async(name,cb)=>callbacks.set(name,cb)}},tool:{{hook:async(name,cb)=>callbacks.set(name,cb)}}}});\n"
        "let denied=false;\n"
        "try { await callbacks.get('execute.before')({id:'shared-outer-id',sessionID:'native',tool:'write',input:{path:'target.py',content:'x'}}); } catch { denied=true; }\n"
        "console.log(JSON.stringify({denied,events:[...callbacks.keys()]}));\n"
    )
    completed = subprocess.run(
        [bun, "run", str(probe)], text=True, capture_output=True, timeout=15
    )
    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout) == {
        "denied": True,
        "events": ["context", "execute.before", "execute.after"],
    }
    assert not (tmp_path / "target.py").exists()
