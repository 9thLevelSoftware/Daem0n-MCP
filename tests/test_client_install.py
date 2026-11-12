"""Native client installation preserves state and bounds filesystem changes."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path

import pytest
import tomlkit

from daem0nmcp import client_install
from daem0nmcp.client_install import ClientInstallError, install_client

CLIENTS = ("codex", "agy", "opencode", "omp", "pi")
PATHS = {
    "codex": ".codex/config.toml",
    "agy": ".agents/mcp_config.json",
    "opencode": "opencode.json",
    "omp": ".omp/mcp.json",
    "pi": ".pi/mcp.json",
}


def _install(client: str, root: Path, **kwargs):
    if client == "opencode":
        kwargs.setdefault("client_version", "1.18.21")
    return install_client(client, root, **kwargs)


def _document(client: str, path: Path):
    text = path.read_text(encoding="utf-8")
    return tomlkit.parse(text) if client == "codex" else json.loads(text)


def _server(client: str, document):
    namespace = (
        "mcp_servers"
        if client == "codex"
        else ("mcp" if client == "opencode" else "mcpServers")
    )
    return document[namespace]["daem0nmcp"]


def _tree(root: Path) -> dict[str, bytes]:
    return {
        str(path.relative_to(root)): path.read_bytes()
        for path in root.rglob("*")
        if path.is_file()
    }


@pytest.mark.parametrize("client", CLIENTS)
def test_fresh_registration_and_repeat_are_scoped_and_identical(tmp_path, client):
    result = _install(client, tmp_path)
    assert result["ok"] and result["scope"] == "project"
    config = tmp_path / PATHS[client]
    server = _server(client, _document(client, config))
    environment = server["environment" if client == "opencode" else "env"]
    assert server["cwd"] == str(tmp_path.resolve())
    assert environment["PYTHONPATH"] == ""
    assert environment["DAEM0NMCP_PROJECT_ROOT"] == str(tmp_path.resolve())
    assert json.loads(environment["DAEM0NMCP_WORKSPACE_ROOTS"]) == [
        str(tmp_path.resolve())
    ]
    assert environment["DAEM0NMCP_COVENANT_MODE"] == "guided"
    assert environment["DAEM0NMCP_TOOL_SURFACE"] == "core"
    assert not any("BRIDGE" in key or "CREDENTIAL" in key for key in environment)
    assert "--project-path" in result["workspace_id_command"]
    assert result["workspace_id_command"][-1] == "workspace-id"
    assert not (tmp_path / ".daem0nmcp" / "workspace.json").exists()
    before = _tree(tmp_path)
    repeated = _install(client, tmp_path)
    assert _tree(tmp_path) == before
    assert all(item["action"] == "unchanged" for item in repeated["files"])


@pytest.mark.parametrize("client", CLIENTS)
def test_dry_run_creates_no_files_or_directories(tmp_path, client):
    result = _install(client, tmp_path, dry_run=True)
    assert result["dry_run"] is True
    assert all(item["action"] == "created" for item in result["files"])
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("client", CLIENTS)
def test_existing_configuration_and_instructions_are_preserved(tmp_path, client):
    config = tmp_path / PATHS[client]
    config.parent.mkdir(parents=True, exist_ok=True)
    if client == "codex":
        original = '# retain this comment\nmodel = "sentinel"\n\n[mcp_servers.other]\ncommand = "other-tool" # keep this too\n'
    else:
        namespace = "mcp" if client == "opencode" else "mcpServers"
        original = (
            json.dumps(
                {
                    "custom": {"sentinel": 17},
                    namespace: {"other": {"command": "other-tool"}},
                },
                indent=4,
            )
            + "\n"
        )
    config.write_text(original, encoding="utf-8")
    original_bytes = config.read_bytes()
    instructions = b"# Existing project policy\r\n\r\nKeep this exact text.\r\n"
    (tmp_path / "AGENTS.md").write_bytes(instructions)
    result = _install(client, tmp_path)
    value = _document(client, config)
    namespace = (
        "mcp_servers"
        if client == "codex"
        else ("mcp" if client == "opencode" else "mcpServers")
    )
    assert value[namespace]["other"]["command"] == "other-tool"
    if client == "codex":
        assert original in config.read_text(encoding="utf-8")
    else:
        assert value["custom"] == {"sentinel": 17}
        # Adding a member does not normalize the existing indentation or fields.
        assert '    "custom": {\n        "sentinel": 17\n    }' in config.read_text(
            encoding="utf-8"
        )
    assert (tmp_path / "AGENTS.md").read_bytes().startswith(instructions)
    backups = [item["backup"] for item in result["files"] if "backup" in item]
    assert len(backups) == 2
    assert sorted((tmp_path / path).read_bytes() for path in backups) == sorted(
        [original_bytes, instructions]
    )
    assert (
        tmp_path / ".daem0nmcp" / "client-install" / ".gitignore"
    ).read_bytes() == b"*\n"


def test_opencode_jsonc_keeps_comments_escaped_keys_and_trailing_commas(tmp_path):
    config = tmp_path / "opencode.jsonc"
    original = (
        "// config header\n{\n"
        '  "permission": {"*": "ask"},\n'
        '  "m\\u0063p": {\n'
        '    /* keep sibling */ "other": {"type": "local", "command": ["node", "-v"]}, // keep comma\n'
        "  },\n"
        '  "sentinel": "https://example.test/a//b/*c*/",\n'
        "}\n"
    )
    config.write_text(original, encoding="utf-8")
    result = _install("opencode", tmp_path)
    assert result["files"][0]["path"] == "opencode.jsonc"
    updated = config.read_text(encoding="utf-8")
    assert updated.startswith(
        '// config header\n{\n  "permission": {"*": "ask"},\n  "m\\u0063p": {\n'
    )
    assert (
        '    /* keep sibling */ "other": {"type": "local", "command": ["node", "-v"]}, // keep comma\n'
        in updated
    )
    assert updated.endswith(
        '  },\n  "sentinel": "https://example.test/a//b/*c*/",\n}\n'
    )
    # Independent standard JSON validation after removing this fixture's known
    # comments/trailing commas; comment-like bytes inside its string survive.
    strict = (
        updated.replace("// config header\n", "")
        .replace("/* keep sibling */", "")
        .replace("// keep comma", "")
    )
    strict = re.sub(r",(\s*[}\]])", r"\1", strict)
    value = json.loads(strict)
    assert value["sentinel"] == "https://example.test/a//b/*c*/"
    assert value["mcp"]["other"]["command"] == ["node", "-v"]
    assert value["mcp"]["daem0nmcp"]["type"] == "local"
    before = config.read_bytes()
    assert _install("opencode", tmp_path)["files"][0]["action"] == "unchanged"
    assert config.read_bytes() == before


@pytest.mark.parametrize(
    "content",
    [
        '{"mcp": {}, "m\\u0063p": {}}',
        '{"mcp": {"other": {}, "other": {}}}',
        '{"mcp": { /* unterminated',
        '{"mcp": []}',
        '[{"mcp": {}}]',
        '{"mcp": {}, "value": NaN}',
        '{"mcp": {"other": {"command": "secret-value"}}} extra',
    ],
)
def test_invalid_jsonc_is_diagnosed_without_writes_or_content_disclosure(
    tmp_path, content
):
    (tmp_path / "opencode.jsonc").write_text(content, encoding="utf-8")
    before = _tree(tmp_path)
    with pytest.raises(ClientInstallError) as captured:
        _install("opencode", tmp_path)
    assert captured.value.code == "INVALID_CONFIG"
    assert "secret-value" not in str(captured.value)
    assert _tree(tmp_path) == before
    assert not (tmp_path / ".daem0nmcp").exists()


@pytest.mark.parametrize("client", ("agy", "omp", "pi"))
def test_json_only_clients_do_not_accept_jsonc(tmp_path, client):
    config = tmp_path / PATHS[client]
    config.parent.mkdir()
    config.write_text('{"mcpServers": {}, /* comment */}', encoding="utf-8")
    with pytest.raises(ClientInstallError, match="supported JSON"):
        _install(client, tmp_path)
    assert not (tmp_path / "AGENTS.md").exists()


@pytest.mark.parametrize("client", CLIENTS)
def test_existing_custom_daem0n_policy_and_environment_remain_identical(
    tmp_path, client
):
    _install(client, tmp_path)
    config = tmp_path / PATHS[client]
    document = _document(client, config)
    server = _server(client, document)
    environment = server["environment" if client == "opencode" else "env"]
    environment["DAEM0NMCP_COVENANT_MODE"] = "strict"
    environment["DAEM0NMCP_TOOL_SURFACE"] = "full"
    environment["DAEM0NMCP_STORAGE_PATH"] = "existing-private-store"
    environment["UNRELATED_API_KEY"] = "synthetic-secret-sentinel"
    text = (
        tomlkit.dumps(document) if client == "codex" else json.dumps(document, indent=2)
    )
    config.write_text(text, encoding="utf-8")
    before = _tree(tmp_path)
    _install(client, tmp_path)
    assert _tree(tmp_path) == before


@pytest.mark.parametrize("client", CLIENTS)
def test_foreign_same_named_server_is_never_replaced(tmp_path, client):
    config = tmp_path / PATHS[client]
    config.parent.mkdir(parents=True, exist_ok=True)
    if client == "codex":
        text = '[mcp_servers.daem0nmcp]\ncommand = "foreign-server"\n'
    else:
        namespace = "mcp" if client == "opencode" else "mcpServers"
        text = json.dumps({namespace: {"daem0nmcp": {"command": "foreign-server"}}})
    config.write_text(text, encoding="utf-8")
    before = _tree(tmp_path)
    with pytest.raises(ClientInstallError) as captured:
        _install(client, tmp_path)
    assert captured.value.code == "CONFIG_CONFLICT"
    assert _tree(tmp_path) == before
    assert not (tmp_path / ".daem0nmcp").exists()


def test_codex_override_is_the_only_instruction_file_updated(tmp_path):
    ordinary = b"# Existing shared instructions\n"
    override = b"# Effective Codex override\n"
    (tmp_path / "AGENTS.md").write_bytes(ordinary)
    (tmp_path / "AGENTS.override.md").write_bytes(override)
    result = _install("codex", tmp_path)
    assert result["files"][1]["path"] == "AGENTS.override.md"
    assert (tmp_path / "AGENTS.md").read_bytes() == ordinary
    assert (tmp_path / "AGENTS.override.md").read_bytes().startswith(override)


@pytest.mark.parametrize("empty_override", (b"", b" \r\n\t"))
def test_empty_codex_override_does_not_hide_existing_policy(tmp_path, empty_override):
    ordinary = b"# Existing shared instructions\n"
    (tmp_path / "AGENTS.md").write_bytes(ordinary)
    (tmp_path / "AGENTS.override.md").write_bytes(empty_override)
    result = _install("codex", tmp_path)
    assert result["files"][1]["path"] == "AGENTS.md"
    assert (tmp_path / "AGENTS.override.md").read_bytes() == empty_override
    assert (tmp_path / "AGENTS.md").read_bytes().startswith(ordinary)


@pytest.mark.parametrize("ordinary", (None, b"", b" \n"))
def test_codex_uses_first_nonempty_project_instruction_fallback(tmp_path, ordinary):
    config = tmp_path / ".codex/config.toml"
    config.parent.mkdir()
    config.write_bytes(b'project_doc_fallback_filenames = ["TEAM.md", "SECOND.md"]\n')
    if ordinary is not None:
        (tmp_path / "AGENTS.md").write_bytes(ordinary)
    (tmp_path / "AGENTS.override.md").write_bytes(b"")
    (tmp_path / "TEAM.md").write_bytes(b"# Effective team policy\n")
    (tmp_path / "SECOND.md").write_bytes(b"# Lower priority policy\n")
    result = _install("codex", tmp_path)
    assert result["files"][1]["path"] == "TEAM.md"
    assert (tmp_path / "TEAM.md").read_bytes().startswith(b"# Effective team policy\n")
    assert (tmp_path / "SECOND.md").read_bytes() == b"# Lower priority policy\n"
    assert (tmp_path / "AGENTS.override.md").read_bytes() == b""
    if ordinary is None:
        assert not (tmp_path / "AGENTS.md").exists()
    else:
        assert (tmp_path / "AGENTS.md").read_bytes() == ordinary
    assert any("global/profile" in warning for warning in result["warnings"])


@pytest.mark.parametrize(
    "fallbacks", ("TEAM.md", ["../outside.md"], ["C:outside.md"], ["sub/TEAM.md"])
)
def test_codex_rejects_ambiguous_or_external_instruction_fallbacks(tmp_path, fallbacks):
    config = tmp_path / ".codex/config.toml"
    config.parent.mkdir()
    config.write_text(
        "project_doc_fallback_filenames = " + json.dumps(fallbacks), encoding="utf-8"
    )
    before = _tree(tmp_path)
    with pytest.raises(ClientInstallError) as captured:
        _install("codex", tmp_path)
    assert captured.value.code == "INVALID_CONFIG"
    assert _tree(tmp_path) == before
    assert not (tmp_path / ".daem0nmcp").exists()


def test_opencode_preserves_effective_claude_instructions(tmp_path):
    original = b"# Existing Claude project policy\nKeep this effective.\n"
    (tmp_path / "CLAUDE.md").write_bytes(original)
    result = _install("opencode", tmp_path)
    assert result["files"][1]["path"] == "CLAUDE.md"
    assert not (tmp_path / "AGENTS.md").exists()
    assert (tmp_path / "CLAUDE.md").read_bytes().startswith(original)


@pytest.mark.parametrize(
    ("client", "field", "expected"),
    (
        ("codex", "startup_timeout_sec", 120),
        ("codex", "tool_timeout_sec", 600),
        ("agy", "timeoutSeconds", 600),
        ("opencode", "timeout", 120_000),
        ("omp", "timeout", 120_000),
        ("pi", "timeout", 120),
    ),
)
def test_native_timeout_units_match_each_host_contract(
    tmp_path, client, field, expected
):
    _install(client, tmp_path)
    server = _server(client, _document(client, tmp_path / PATHS[client]))
    assert server[field] == expected


def test_invalid_instruction_markers_prevent_configuration_changes(tmp_path):
    (tmp_path / "AGENTS.md").write_text(
        "<!-- daem0nmcp:v7:start -->\nunfinished", encoding="utf-8"
    )
    before = _tree(tmp_path)
    with pytest.raises(ClientInstallError) as captured:
        _install("pi", tmp_path)
    assert captured.value.code == "INSTRUCTION_CONFLICT"
    assert _tree(tmp_path) == before
    assert not (tmp_path / ".pi").exists()


@pytest.mark.parametrize("version", (None, "2.0.0", "unknown"))
def test_opencode_requires_an_observed_supported_version(tmp_path, version):
    with pytest.raises(ClientInstallError) as captured:
        install_client("opencode", tmp_path, client_version=version)
    assert captured.value.code in {
        "CLIENT_VERSION_REQUIRED",
        "UNSUPPORTED_CLIENT_VERSION",
    }
    assert list(tmp_path.iterdir()) == []


def test_v2_schema_is_rejected_even_with_v1_version_argument(tmp_path):
    (tmp_path / "opencode.json").write_text('{"mcp":{"servers":{}}}', encoding="utf-8")
    before = _tree(tmp_path)
    with pytest.raises(ClientInstallError) as captured:
        _install("opencode", tmp_path)
    assert captured.value.code == "UNSUPPORTED_CLIENT_SCHEMA"
    assert _tree(tmp_path) == before


def test_opencode_uses_existing_higher_precedence_project_file(tmp_path):
    (tmp_path / "opencode.json").write_text('{"custom":"root"}', encoding="utf-8")
    project = tmp_path / ".opencode" / "opencode.json"
    project.parent.mkdir()
    project.write_text('{"custom":"project"}', encoding="utf-8")
    result = _install("opencode", tmp_path)
    assert result["files"][0]["path"] == str(Path(".opencode/opencode.json"))
    assert (tmp_path / "opencode.json").read_text() == '{"custom":"root"}'
    assert json.loads(project.read_text())["custom"] == "project"


@pytest.mark.parametrize("linked", ("config", "directory", "root"))
def test_linked_targets_and_ancestors_are_rejected(tmp_path, linked):
    project = tmp_path / "project"
    project.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    sentinel = outside / "mcp.json"
    sentinel.write_text('{"outside":true}', encoding="utf-8")
    try:
        if linked == "config":
            (project / ".pi").mkdir()
            (project / ".pi/mcp.json").symlink_to(sentinel)
        elif linked == "directory":
            (project / ".pi").symlink_to(outside, target_is_directory=True)
        else:
            alias = tmp_path / "alias"
            alias.symlink_to(project, target_is_directory=True)
            project = alias
    except OSError:
        pytest.skip("This host cannot create the test symlink.")
    with pytest.raises(ClientInstallError) as captured:
        _install("pi", project)
    assert captured.value.code == "UNSAFE_PATH"
    assert sentinel.read_text() == '{"outside":true}'
    assert not (project / "AGENTS.md").exists()


def test_concurrent_configuration_edit_is_retained(tmp_path, monkeypatch):
    config = tmp_path / ".pi/mcp.json"
    config.parent.mkdir()
    config.write_text('{"custom":"original"}', encoding="utf-8")
    original = client_install._check_snapshots
    calls = 0

    def check_then_edit(snapshots):
        nonlocal calls
        original(snapshots)
        calls += 1
        if calls == 1:
            config.write_text('{"custom":"concurrent"}', encoding="utf-8")

    monkeypatch.setattr(client_install, "_check_snapshots", check_then_edit)
    with pytest.raises(ClientInstallError) as captured:
        _install("pi", tmp_path)
    assert captured.value.code == "CONCURRENT_CHANGE"
    assert config.read_text() == '{"custom":"concurrent"}'
    assert not (tmp_path / "AGENTS.md").exists()


def test_partial_publication_is_reported_with_original_backups(tmp_path, monkeypatch):
    config = tmp_path / ".pi/mcp.json"
    config.parent.mkdir()
    config.write_text('{"custom":"original"}', encoding="utf-8")
    instructions = tmp_path / "AGENTS.md"
    instructions.write_text("Keep policy\n", encoding="utf-8")
    original_instructions = instructions.read_bytes()
    original = os.replace

    def fail_instruction_replace(source, destination, *args, **kwargs):
        if Path(destination).name == instructions.name:
            raise PermissionError("synthetic publication failure")
        return original(source, destination, *args, **kwargs)

    monkeypatch.setattr(os, "replace", fail_instruction_replace)
    with pytest.raises(ClientInstallError) as captured:
        _install("pi", tmp_path)
    assert captured.value.code == "WRITE_FAILED"
    assert len(captured.value.files) == 1
    published = captured.value.files[0]
    assert published["path"] == str(Path(".pi/mcp.json"))
    assert published["action"] == "updated"
    assert (tmp_path / published["backup"]).read_bytes() == b'{"custom":"original"}'
    assert instructions.read_text() == "Keep policy\n"
    assert "daem0nmcp" in json.loads(config.read_text())["mcpServers"]
    originals = [
        path.read_bytes()
        for path in (tmp_path / ".daem0nmcp/client-install").glob("*.bak")
    ]
    assert b'{"custom":"original"}' in originals
    assert original_instructions in originals


@pytest.mark.parametrize("cleanup_fails", (False, True))
@pytest.mark.parametrize("fail_verify", (1, 2))
def test_guard_failure_retains_publication_and_cleanup_ledger(
    tmp_path, monkeypatch, cleanup_fails, fail_verify
):
    config = tmp_path / ".pi/mcp.json"
    config.parent.mkdir()
    original_config = b'{"custom":"original"}'
    config.write_bytes(original_config)
    instructions = tmp_path / "AGENTS.md"
    instructions.write_bytes(b"Original policy\n")
    real_guard = client_install.guard_directory_ancestry

    @contextmanager
    def failing_guard(*args, **kwargs):
        with real_guard(*args, **kwargs) as guard:
            if Path(args[0]) != config.parent:
                yield guard
                return
            real_verify = guard.verify
            calls = 0

            def verify():
                nonlocal calls
                calls += 1
                real_verify()
                if calls == fail_verify:
                    raise PermissionError("synthetic post-publication guard failure")

            monkeypatch.setattr(guard, "verify", verify)
            yield guard

    monkeypatch.setattr(client_install, "guard_directory_ancestry", failing_guard)
    if cleanup_fails:
        real_unlink = client_install._unlink_temporary

        def fail_temporary_cleanup(path, descriptor):
            if path.suffix == ".tmp" and path.exists():
                raise PermissionError("synthetic cleanup failure")
            return real_unlink(path, descriptor)

        monkeypatch.setattr(client_install, "_unlink_temporary", fail_temporary_cleanup)
    with pytest.raises(ClientInstallError) as captured:
        _install("pi", tmp_path)
    error = captured.value
    assert error.code == "WRITE_FAILED"
    published = [item for item in error.files if item["action"] != "cleanup_pending"]
    if fail_verify == 2:
        assert published[0]["path"] == str(Path(".pi/mcp.json"))
        assert published[0]["action"] == "updated"
        assert (tmp_path / published[0]["backup"]).read_bytes() == original_config
        assert "daem0nmcp" in json.loads(config.read_text())["mcpServers"]
    else:
        assert published == []
        assert config.read_bytes() == original_config
    assert instructions.read_bytes() == b"Original policy\n"
    pending = [item for item in error.files if item["action"] == "cleanup_pending"]
    assert bool(pending) is cleanup_fails
    if pending:
        assert "cleanup is incomplete" in error.message
        assert all((tmp_path / item["path"]).is_file() for item in pending)


def test_cleanup_failure_after_success_reports_all_published_files(
    tmp_path, monkeypatch
):
    real_unlink = client_install._unlink_temporary

    def fail_temporary_cleanup(path, descriptor):
        if path.suffix == ".tmp" and path.exists():
            raise PermissionError("synthetic cleanup failure")
        return real_unlink(path, descriptor)

    monkeypatch.setattr(client_install, "_unlink_temporary", fail_temporary_cleanup)
    with pytest.raises(ClientInstallError) as captured:
        _install("pi", tmp_path)
    error = captured.value
    assert error.code == "CLEANUP_FAILED"
    assert {
        (item["path"], item["action"])
        for item in error.files
        if item["action"] != "cleanup_pending"
    } == {
        (str(Path(".pi/mcp.json")), "created"),
        ("AGENTS.md", "created"),
    }
    assert (
        len([item for item in error.files if item["action"] == "cleanup_pending"]) == 2
    )


@pytest.mark.parametrize("client", CLIENTS)
def test_interpreter_with_spaces_is_an_executable_not_a_shell_command(tmp_path, client):
    interpreter = str(tmp_path / "venv with spaces" / "python.exe")
    _install(client, tmp_path, python_executable=interpreter)
    server = _server(client, _document(client, tmp_path / PATHS[client]))
    if client == "opencode":
        assert server["command"][0] == interpreter
        arguments = server["command"][1:]
    else:
        assert server["command"] == interpreter
        arguments = server["args"]
    assert arguments[:5] == ["-E", "-u", "-X", "utf8", "-c"]
    assert "run_module('daem0nmcp.server'" in arguments[5]


@pytest.mark.parametrize("client", CLIENTS)
@pytest.mark.parametrize("drops_empty_environment", (False, True))
def test_actual_generated_launcher_ignores_project_shadow_modules(
    tmp_path, client, drops_empty_environment
):
    project = tmp_path / "project with spaces"
    project.mkdir()
    marker = tmp_path / "project-code-imported.txt"
    malicious = f"raise RuntimeError('project code imported: {marker.name}')\n"
    for relative in (
        "sitecustomize.py",
        "runpy.py",
        "daem0nmcp/__init__.py",
        "daem0nmcp/server.py",
    ):
        path = project / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(malicious, encoding="utf-8")
    _install(client, project)
    server = _server(client, _document(client, project / PATHS[client]))
    if client == "opencode":
        command = server["command"]
        settings = server["environment"]
    else:
        command = [server["command"], *server["args"]]
        settings = server["env"]
    settings = {
        key: value
        for key, value in settings.items()
        if value or not drops_empty_environment
    }
    result = subprocess.run(
        [*command, "--help"],
        cwd=project,
        env={**os.environ, "PYTHONPATH": str(project), **dict(settings)},
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "Daem0nMCP v7 server" in result.stdout
    assert "project code imported" not in result.stderr


def test_legacy_positive_control_confirms_dropped_empty_environment_is_unsafe(tmp_path):
    project = tmp_path / "shadow"
    project.mkdir()
    (project / "sitecustomize.py").write_text(
        "raise RuntimeError('unsafe startup control')\n", encoding="utf-8"
    )
    result = subprocess.run(
        [
            sys.executable,
            *client_install.module_launch_args("daem0nmcp.server"),
            "--help",
        ],
        cwd=project,
        env={**os.environ, "PYTHONHOME": "", "PYTHONPATH": str(project)},
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )
    assert "unsafe startup control" in result.stderr


def test_generated_workspace_lookup_ignores_inherited_python_home(tmp_path):
    result = _install("pi", tmp_path)
    probe = subprocess.run(
        result["workspace_id_command"],
        cwd=tmp_path,
        env={
            **os.environ,
            "PYTHONHOME": str(tmp_path / "not-a-python-installation"),
            "DAEM0NMCP_PROJECT_ROOT": str(tmp_path),
            "DAEM0NMCP_WORKSPACE_ROOTS": json.dumps([str(tmp_path)]),
        },
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )
    assert probe.returncode == 0, probe.stderr
    assert json.loads(probe.stdout)["workspace_id"].startswith("ws_")


@pytest.mark.skipif(os.name == "nt", reason="POSIX descriptor-relative publication")
@pytest.mark.parametrize("operation", ("link", "replace"))
def test_parent_swap_during_publication_cannot_write_outside_project(
    tmp_path, monkeypatch, operation
):
    project = tmp_path / "project"
    parent = project / ".pi"
    parent.mkdir(parents=True)
    config = parent / "mcp.json"
    if operation == "replace":
        config.write_bytes(b'{"custom":"inside"}')
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "sentinel.txt").write_bytes(b"outside sentinel")
    if operation == "replace":
        (outside / "mcp.json").write_bytes(b'{"custom":"outside"}')
    before = _tree(outside)
    parked = project / ".pi-original"
    original = getattr(os, operation)
    swapped = False

    def swap_then_publish(source, destination, *args, **kwargs):
        nonlocal swapped
        if Path(destination).name == "mcp.json" and not swapped:
            swapped = True
            parent.rename(parked)
            parent.symlink_to(outside, target_is_directory=True)
        return original(source, destination, *args, **kwargs)

    monkeypatch.setattr(os, operation, swap_then_publish)
    with pytest.raises(ClientInstallError) as captured:
        _install("pi", project)
    assert swapped
    assert captured.value.code == "WRITE_FAILED"
    assert _tree(outside) == before
    assert "daem0nmcp" in json.loads((parked / "mcp.json").read_text())["mcpServers"]
    assert captured.value.files[0]["path"] == str(Path(".pi/mcp.json"))


@pytest.mark.skipif(
    os.name == "nt", reason="POSIX descriptor-relative directory creation"
)
def test_root_swap_during_parent_creation_cannot_create_outside_directories(
    tmp_path, monkeypatch
):
    project = tmp_path / "project"
    project.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "sentinel.txt").write_bytes(b"outside sentinel")
    before = _tree(outside)
    parked = tmp_path / "original-project"
    original = os.mkdir
    swapped = False

    def swap_then_mkdir(path, *args, **kwargs):
        nonlocal swapped
        if path == ".pi" and not swapped:
            swapped = True
            project.rename(parked)
            project.symlink_to(outside, target_is_directory=True)
        return original(path, *args, **kwargs)

    monkeypatch.setattr(os, "mkdir", swap_then_mkdir)
    with pytest.raises(ClientInstallError):
        _install("pi", project)
    assert swapped
    assert _tree(outside) == before
    assert not (outside / ".pi").exists()
    assert (parked / ".pi").is_dir()


@pytest.mark.skipif(os.name == "nt", reason="POSIX descriptor-relative cleanup")
def test_staging_parent_swap_cannot_redirect_temporary_cleanup(tmp_path, monkeypatch):
    project = tmp_path / "project"
    project.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    state = project / ".daem0nmcp/client-install"
    parked = project / ".daem0nmcp/original-client-install"
    original = client_install._unlink_temporary
    swapped = False
    before = {}

    def swap_then_unlink(path, descriptor):
        nonlocal swapped, before
        if not swapped:
            swapped = True
            for staged in state.glob("*.tmp"):
                (outside / staged.name).write_bytes(b"outside sentinel")
            before = _tree(outside)
            state.rename(parked)
            state.symlink_to(outside, target_is_directory=True)
        return original(path, descriptor)

    monkeypatch.setattr(client_install, "_unlink_temporary", swap_then_unlink)
    with pytest.raises(ClientInstallError) as captured:
        _install("pi", project)
    assert swapped
    assert captured.value.code == "CLEANUP_FAILED"
    assert _tree(outside) == before
    assert list(parked.glob("*.tmp")) == []
    assert any(item["action"] == "state_changed" for item in captured.value.files)
