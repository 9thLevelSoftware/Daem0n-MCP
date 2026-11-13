"""Installed native guidance must not create an unnecessary briefing ritual."""

from __future__ import annotations

import json
import re

import pytest

from daem0nmcp.api.v7.factory import build_inspectable_v7_server
from daem0nmcp.client_install import ClientInstallError, install_client
from daem0nmcp.native_reminders import REMINDER, reminder_result
from tests.api_v7.test_tool_descriptions import _handlers, _Resources


def assert_guided_order_and_scope(text):
    lower = text.lower()
    assert "optional" in lower and "guided" in lower
    assert "not a prerequisite" in lower
    assert "current task" in lower
    assert "session_brief" in text and "workspace_id" in text
    assert "never {}" in text
    assert "strict mode" in lower
    assert "indexing" in lower


@pytest.mark.parametrize("client", ["omp", "pi"])
def test_installed_covenant_has_optional_guided_brief_and_direct_typed_preference(
    tmp_path, client
):
    install_client(client, tmp_path)
    text = (tmp_path / "AGENTS.md").read_text()
    assert_guided_order_and_scope(text)
    assert "Prefer directly listed typed tools" in text
    assert "unlisted" in text


@pytest.mark.parametrize("host", ["codex", "agy"])
def test_effective_native_reminder_keeps_order_scope_and_no_extra_index_ritual(host):
    output = reminder_result(host, "SessionStart", {})
    text = (
        output["hookSpecificOutput"]["additionalContext"]
        if host == "codex"
        else output["injectSteps"][0]["systemMessage"]["systemMessage"]
    )
    assert text == REMINDER
    assert_guided_order_and_scope(text)


def test_manifest_exposes_optional_full_brief_without_guided_start_command():
    server = build_inspectable_v7_server(_handlers(), _Resources())
    description = next(
        tool["description"]
        for tool in server.list_tools()
        if tool["name"] == "session_brief"
    )
    assert "Optional in guided mode" in description
    assert "workspace_id" in description and "never {}" in description
    assert "strict mode" in description
    assert "Start or refresh the session" not in description
    assert len(description) <= 300


@pytest.mark.parametrize("client", ["omp", "pi"])
@pytest.mark.parametrize("enabled", [True, False])
def test_owned_legacy_reminder_upgrade_is_quoted_idempotent_and_preserves_customization(
    tmp_path, client, enabled
):
    extension = tmp_path / f".{client}/extensions/daem0n.ts"
    extension.parent.mkdir(parents=True)
    package = (
        "@oh-my-pi/pi-coding-agent"
        if client == "omp"
        else "@earendil-works/pi-coding-agent"
    )
    legacy = "Daem0n: use the installed workspace covenant, recall relevant warnings and decisions before edits, and record verified outcomes with stable idempotency keys."
    old = (
        "// Managed by Daem0n install-client: native reminder v1\n"
        + f"import type {{ ExtensionAPI }} from {json.dumps(package)};\n"
        + f"const ENABLED = {str(enabled).lower()};\n"
        + 'export default function (pi: ExtensionAPI) {\n  if (!ENABLED) return;\n  pi.on("before_agent_start", async () => ({\n'
        + '    message: { customType: "daem0n-covenant-reminder", '
        + f"content: {json.dumps(legacy)}, display: false }}\n"
        + "  }));\n}\n"
    )
    extension.write_bytes(old.encode())
    install_client(client, tmp_path, automation="remind")
    upgraded = extension.read_text()
    literal = re.search(r'content: ("(?:[^"\\]|\\.)*"), display:', upgraded)
    assert literal is not None
    assert json.loads(literal.group(1)) == REMINDER
    assert_guided_order_and_scope(json.loads(literal.group(1)))
    install_client(client, tmp_path, automation="remind")
    assert extension.read_text() == upgraded
    customized = upgraded.replace(
        "const ENABLED = true;", "const ENABLED = true; // custom"
    )
    extension.write_bytes(customized.encode())
    with pytest.raises(ClientInstallError, match="customized"):
        install_client(client, tmp_path, automation="remind")
    assert extension.read_text() == customized
