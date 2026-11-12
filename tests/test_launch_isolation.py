"""Launch commands must never import code from the project they run in.

Claude Code, OpenCode and git start Daem0n's hooks and the MCP server with the
opened project as the working directory.  ``python -m`` puts that directory
first on ``sys.path``, so every command this project writes or documents is
executed here inside a project that ships its own ``daem0nmcp`` package.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from daem0nmcp.claude_hooks.install import (
    _is_daem0n_entry,
    install_claude_hooks,
    module_launch_args,
    uninstall_claude_hooks,
)
from daem0nmcp.hooks import PRE_COMMIT_HOOK
from daem0nmcp.opencode_install import (
    OPENCODE_JSON_TEMPLATE,
    PLUGIN_TEMPLATE,
    install_opencode,
)

ROOT = Path(__file__).resolve().parents[1]

HOOK_MODULES = (
    "pre_edit",
    "pre_bash",
    "post_edit_preflight",
    "post_edit",
    "stop",
    "session_start",
)

# Every document that tells a user which command an MCP host should launch.
REGISTRATION_DOCUMENTS = (
    "README.md",
    "Summon_Daem0n.md",
    "Summon_Daem0n_OpenCode.md",
    "Banish_Daem0n.md",
    "docs/index.html",
)


@pytest.fixture
def shadowing_project(tmp_path: Path) -> tuple[Path, Path]:
    """A Daem0n project whose own modules record being imported.

    It ships a ``daem0nmcp`` package and top-level modules named like the
    standard-library ones a launch touches first.
    """
    project = tmp_path / "project"
    (project / ".daem0nmcp").mkdir(parents=True)
    marker = tmp_path / "imported-from-project.txt"
    source = (
        f"with open({str(marker)!r}, 'a', encoding='utf-8') as log:\n"
        "    log.write(__name__ + '\\n')\n"
    )
    modules = (
        "runpy",
        "json",
        "sitecustomize",
        "usercustomize",
        "daem0nmcp/__init__",
        "daem0nmcp/__main__",
        "daem0nmcp/cli",
        "daem0nmcp/server",
        "daem0nmcp/claude_hooks/__init__",
        *(f"daem0nmcp/claude_hooks/{name}" for name in HOOK_MODULES),
    )
    for module in modules:
        path = project / f"{module}.py"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(source, encoding="utf-8")
    return project, marker


def _environment(home: Path, project: Path) -> dict[str, str]:
    environment = {
        **os.environ,
        # The checkout stays importable however the suite was installed; HOME
        # bounds the hooks' project search and keeps their state in the test.
        "PYTHONPATH": str(ROOT),
        "HOME": str(home),
        "USERPROFILE": str(home),
        "CLAUDE_PROJECT_DIR": str(project),
    }
    # The commands must be safe by themselves, on every supported interpreter.
    environment.pop("PYTHONSAFEPATH", None)
    return environment


def _run(
    command: str | list[str], project: Path, home: Path, event: dict | None = None
) -> subprocess.CompletedProcess[str]:
    """Run *command* inside *project*.

    A string is a hook command exactly as the installer wrote it.  Claude Code
    hands those to ``sh -c``, or to Git Bash on Windows; there the command is
    split by the same quoting rules rather than needing a bash on ``PATH``.
    """
    shell = isinstance(command, str) and os.name != "nt"
    if isinstance(command, str) and not shell:
        command = shlex.split(command)
    return subprocess.run(
        command,
        shell=shell,
        cwd=project,
        env=_environment(home, project),
        input=json.dumps(event or {}),
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )


def _event(project: Path) -> dict:
    return {
        "session_id": "s-1",
        "cwd": str(project),
        "hook_event_name": "PreToolUse",
        "tool_name": "Edit",
        "tool_input": {"file_path": str(project / "app.py")},
        "tool_use_id": "toolu_1",
    }


def _imported(marker: Path) -> str:
    return marker.read_text(encoding="utf-8") if marker.exists() else ""


@pytest.fixture
def fake_settings(tmp_path: Path, monkeypatch) -> Path:
    settings_file = tmp_path / "claude-home" / ".claude" / "settings.json"
    monkeypatch.setattr(
        "daem0nmcp.claude_hooks.install._settings_path", lambda: settings_file
    )
    return settings_file


def _hook_commands(settings_file: Path) -> list[str]:
    hooks = json.loads(settings_file.read_text(encoding="utf-8"))["hooks"]
    return [
        hook["command"]
        for entries in hooks.values()
        for entry in entries
        for hook in entry["hooks"]
    ]


def _command_for(commands: list[str], module: str) -> str:
    pattern = re.compile(rf"daem0nmcp\.claude_hooks\.{module}(?![a-z_])")
    return next(command for command in commands if pattern.search(command))


def _plugin_launch_code(plugin: str, module: str) -> str:
    """Return the Python source the OpenCode plugin passes to ``-c`` for *module*."""
    literals = re.findall(r"`(import sys;[^`]*)`", plugin)
    assert len(literals) == 1, "the plugin has no cwd-free hook launcher"
    return literals[0].replace("${module}", module)


def _legacy_entry(module: str) -> dict:
    """A hook entry exactly as versions before the safe launch form wrote it."""
    return {
        "matcher": "",
        "hooks": [
            {
                "type": "command",
                "command": f'"{sys.executable}" -m daem0nmcp.claude_hooks.{module}',
            }
        ],
    }


# ─── Claude Code hooks ─────────────────────────────────────────────


def test_installed_hook_commands_ignore_a_package_in_the_project(
    fake_settings, shadowing_project, tmp_path
):
    project, marker = shadowing_project
    ok, message = install_claude_hooks()
    assert ok, message
    commands = _hook_commands(fake_settings)
    assert len(commands) == 7

    for command in commands:
        result = _run(command, project, tmp_path, _event(project))

        assert _imported(marker) == "", command
        assert result.returncode == 0, (command, result.stderr)


def test_installed_hooks_still_report_from_a_project_that_shadows_the_package(
    fake_settings, shadowing_project, tmp_path
):
    project, marker = shadowing_project
    ok, message = install_claude_hooks()
    assert ok, message
    commands = _hook_commands(fake_settings)

    pre_edit = _run(
        _command_for(commands, "pre_edit"), project, tmp_path, _event(project)
    )
    session_start = _run(_command_for(commands, "session_start"), project, tmp_path)

    assert _imported(marker) == ""
    context = json.loads(pre_edit.stdout)["hookSpecificOutput"]["additionalContext"]
    assert '"relative_file_path": "app.py"' in context
    assert "[Daem0n] IMPORTANT" in session_start.stdout
    assert 'workspace_id="ws_' in session_start.stdout
    # A current install has nothing to migrate, so it stays plain text.
    assert "install-claude-hooks" not in session_start.stdout


def test_reinstall_replaces_the_module_commands_older_versions_wrote(fake_settings):
    foreign = {"matcher": "Edit", "hooks": [{"type": "command", "command": "lint"}]}
    fake_settings.parent.mkdir(parents=True)
    fake_settings.write_text(
        json.dumps(
            {
                "hooks": {
                    "PreToolUse": [
                        foreign,
                        _legacy_entry("pre_edit"),
                        _legacy_entry("pre_bash"),
                    ],
                    "PostToolUse": [
                        _legacy_entry("post_edit_preflight"),
                        _legacy_entry("post_edit"),
                    ],
                    "Stop": [_legacy_entry("stop")],
                    "SubagentStop": [_legacy_entry("stop")],
                    "SessionStart": [_legacy_entry("session_start")],
                }
            }
        ),
        encoding="utf-8",
    )

    ok, message = install_claude_hooks()

    assert ok, message
    commands = _hook_commands(fake_settings)
    assert commands.count("lint") == 1
    daem0n = [command for command in commands if "daem0nmcp" in command]
    assert len(daem0n) == 7
    assert not [command for command in daem0n if " -m " in command]
    assert "Replaced 7 older hook commands" in message

    ok, again = install_claude_hooks()

    assert ok, again
    assert "Replaced" not in again
    assert _hook_commands(fake_settings) == commands


def test_dry_run_reports_the_older_commands_it_would_replace(fake_settings):
    fake_settings.parent.mkdir(parents=True)
    older = json.dumps({"hooks": {"Stop": [_legacy_entry("stop")]}})
    fake_settings.write_text(older, encoding="utf-8")

    ok, message = install_claude_hooks(dry_run=True)

    assert ok, message
    assert "Would replace 1 older hook command " in message
    assert fake_settings.read_text(encoding="utf-8") == older


def test_first_install_does_not_claim_to_replace_anything(fake_settings):
    ok, message = install_claude_hooks()

    assert ok, message
    assert "Replaced" not in message


def test_uninstall_removes_both_the_older_and_the_current_commands(fake_settings):
    ok, message = install_claude_hooks()
    assert ok, message
    settings = json.loads(fake_settings.read_text(encoding="utf-8"))
    current = settings["hooks"]["SessionStart"][0]
    legacy = _legacy_entry("session_start")
    assert _is_daem0n_entry(current) and _is_daem0n_entry(legacy)
    settings["hooks"]["SessionStart"].append(legacy)
    fake_settings.write_text(json.dumps(settings), encoding="utf-8")

    ok, message = uninstall_claude_hooks()

    assert ok, message
    assert json.loads(fake_settings.read_text(encoding="utf-8"))["hooks"] == {}


def test_hook_started_by_an_older_command_asks_for_a_reinstall(tmp_path):
    """Settings written before the fix keep ``-m`` until the installer is re-run."""
    project = tmp_path / "project"
    (project / ".daem0nmcp").mkdir(parents=True)

    result = _run(
        [sys.executable, "-m", "daem0nmcp.claude_hooks.session_start"],
        project,
        tmp_path,
    )

    assert result.returncode == 0, result.stderr
    output = json.loads(result.stdout)
    assert "python -m daem0nmcp.cli install-claude-hooks" in output["systemMessage"]
    # Typed inside this project, that command would import from it as well.
    assert "from a directory you trust" in output["systemMessage"]
    context = output["hookSpecificOutput"]
    assert context["hookEventName"] == "SessionStart"
    assert "[Daem0n] IMPORTANT" in context["additionalContext"]
    assert 'workspace_id="ws_' in context["additionalContext"]


def test_older_command_asks_for_a_reinstall_outside_daem0n_projects_too(tmp_path):
    """The settings are the user's: every project they open is exposed."""
    project = tmp_path / "elsewhere"
    project.mkdir()

    result = _run(
        [sys.executable, "-m", "daem0nmcp.claude_hooks.session_start"],
        project,
        tmp_path,
    )

    assert result.returncode == 0, result.stderr
    output = json.loads(result.stdout)
    assert list(output) == ["systemMessage"]
    assert "install-claude-hooks" in output["systemMessage"]

    current = _run(
        [sys.executable, *module_launch_args("daem0nmcp.claude_hooks.session_start")],
        project,
        tmp_path,
    )

    assert current.returncode == 0, current.stderr
    assert current.stdout == ""


# ─── documented and generated server commands ──────────────────────


def _registered_commands(text: str) -> list[list[str]]:
    """Return every stdio launch command *text* tells an MCP host to run."""
    commands = []
    for line in text.splitlines():
        if "claude mcp add" in line and " -- " in line:
            # Drop a closing Markdown backtick and anything after it.
            tail = line.split(" -- ", 1)[1].split("`", 1)[0]
            commands.append(shlex.split(tail))
    decoder = json.JSONDecoder()
    for match in re.finditer(r'"command":\s*(?=\[)', text):
        command, _end = decoder.raw_decode(text, match.end())
        if any("daem0nmcp" in part for part in command):
            commands.append(command)
    # Claude project configuration splits the interpreter and arguments into
    # separate fields. Exercise these documented launches alongside CLI and
    # OpenCode registrations instead of silently omitting them from isolation.
    for block in re.findall(r"```json\s*\n(.*?)\n```", text, re.DOTALL):
        if '"mcpServers"' not in block or not re.search(r'"command":\s*"', block):
            continue
        configuration = json.loads(block)
        for server in configuration.get("mcpServers", {}).values():
            interpreter = server.get("command")
            arguments = server.get("args", [])
            if isinstance(interpreter, str) and any(
                "daem0nmcp" in argument for argument in arguments
            ):
                commands.append([interpreter, *arguments])
    return commands


def _documented_server_commands() -> list[tuple[str, list[str]]]:
    found = []
    for name in (*REGISTRATION_DOCUMENTS, "opencode.json"):
        text = (ROOT / name).read_text(encoding="utf-8")
        found.extend((name, command) for command in _registered_commands(text))
    found.append(
        (
            "OPENCODE_JSON_TEMPLATE",
            OPENCODE_JSON_TEMPLATE["mcp"]["daem0nmcp"]["command"],
        )
    )
    return found


def test_every_registration_document_shows_a_server_command():
    documented = {name for name, _command in _documented_server_commands()}

    assert documented == {
        *REGISTRATION_DOCUMENTS,
        "opencode.json",
        "OPENCODE_JSON_TEMPLATE",
    }


def test_documented_server_commands_ignore_a_package_in_the_project(
    shadowing_project, tmp_path
):
    project, marker = shadowing_project

    for name, command in _documented_server_commands():
        # The first word is an interpreter placeholder such as <PYTHON_PATH>.
        result = _run([sys.executable, *command[1:], "--help"], project, tmp_path)

        assert _imported(marker) == "", (name, command)
        assert result.returncode == 0, (name, result.stderr)
        assert "Daem0nMCP v7 server" in result.stdout, name


def test_installed_opencode_server_command_ignores_a_package_in_the_project(
    shadowing_project, tmp_path
):
    project, marker = shadowing_project
    ok, message = install_opencode(
        str(project), bridge_config_root=tmp_path / "host-config"
    )
    assert ok, message
    config = json.loads((project / "opencode.json").read_text(encoding="utf-8"))
    command = config["mcp"]["daem0nmcp"]["command"]
    assert command[0] == sys.executable

    result = _run([*command, "--help"], project, tmp_path)

    assert _imported(marker) == ""
    assert result.returncode == 0, result.stderr
    assert "Daem0nMCP v7 server" in result.stdout


def test_git_pre_commit_hook_ignores_a_package_in_the_project(
    shadowing_project, tmp_path
):
    project, marker = shadowing_project
    lines = [
        line for line in PRE_COMMIT_HOOK.splitlines() if line.startswith("python ")
    ]
    assert len(lines) == 1
    arguments = shlex.split(lines[0])
    assert arguments[-3:] == ["--project-path", "$PROJECT_ROOT", "pre-commit"]

    result = _run([sys.executable, *arguments[1:-3], "--help"], project, tmp_path)

    assert _imported(marker) == ""
    assert result.returncode == 0, result.stderr
    assert "Daem0nMCP CLI" in result.stdout


def test_every_generated_command_uses_the_one_launch_form(fake_settings):
    # Imported here so the behavioural tests in this file do not depend on it.
    from daem0nmcp.claude_hooks.install import module_launch_args

    ok, message = install_claude_hooks()
    assert ok, message
    for module in HOOK_MODULES:
        flag, code = module_launch_args(f"daem0nmcp.claude_hooks.{module}")
        # Plain double quotes must be enough for sh, bash and cmd.exe.
        assert not set(code) & set('"$`\\%!^&|<>')
        assert f'"{sys.executable}" {flag} "{code}"' in _hook_commands(fake_settings)
        assert _plugin_launch_code(PLUGIN_TEMPLATE, module) == code
    servers = [module_launch_args("daem0nmcp"), module_launch_args("daem0nmcp.server")]
    for name, command in _documented_server_commands():
        assert command[1:] in servers, name
    flag, code = module_launch_args("daem0nmcp.cli")
    assert f'\npython {flag} "{code}" --project-path' in PRE_COMMIT_HOOK
    with pytest.raises(ValueError):
        module_launch_args("daem0nmcp.server'); import os; os.system('id")


# ─── OpenCode plugin ───────────────────────────────────────────────


def test_plugin_hook_launcher_ignores_a_package_in_the_project(
    shadowing_project, tmp_path
):
    project, marker = shadowing_project
    assert '"-m"' not in PLUGIN_TEMPLATE

    for module in HOOK_MODULES:
        code = _plugin_launch_code(PLUGIN_TEMPLATE, module)
        result = _run([sys.executable, "-c", code], project, tmp_path, _event(project))

        assert _imported(marker) == "", module
        assert result.returncode == 0, (module, result.stderr)


def test_plugin_interpreter_comes_from_the_installer_not_the_project(
    shadowing_project, tmp_path
):
    project, _marker = shadowing_project
    ok, message = install_opencode(
        str(project), bridge_config_root=tmp_path / "host-config"
    )
    assert ok, message
    plugin = (project / ".opencode" / "plugins" / "daem0n.ts").read_text(
        encoding="utf-8"
    )
    config = json.loads((project / "opencode.json").read_text(encoding="utf-8"))

    assert f"const PYTHON_EXECUTABLE = {json.dumps(sys.executable)};" in plugin
    assert plugin.count("Bun.spawn(") == 1
    assert 'Bun.spawn([PYTHON_EXECUTABLE, "-c", hookLaunchCode(module)]' in plugin
    assert "DAEM0NMCP_PYTHON_EXECUTABLE" not in plugin
    assert "process.env.DAEM0NMCP" not in plugin
    assert (
        "DAEM0NMCP_PYTHON_EXECUTABLE" not in config["mcp"]["daem0nmcp"]["environment"]
    )


_PLUGIN_DRIVER = """\
const [pluginPath, directory] = process.argv.slice(2);
const { Daem0nPlugin } = await import(pluginPath);
const hooks = await Daem0nPlugin({ directory });
await hooks.event({
  event: { type: "session.created", properties: { sessionID: "s-1" } },
});
let edit = "allowed";
try {
  await hooks["tool.execute.before"](
    { tool: "edit", sessionID: "s-1", callID: "c-1" },
    { args: { filePath: `${directory}/app.py`, oldString: "a", newString: "b" } },
  );
} catch (error) {
  edit = `blocked: ${error.message}`;
}
console.log(JSON.stringify({ edit }));
"""


@pytest.mark.skipif(shutil.which("bun") is None, reason="needs the Bun runtime")
def test_installed_plugin_under_bun_ignores_the_project_package_and_interpreter(
    shadowing_project, tmp_path
):
    project, marker = shadowing_project
    ok, message = install_opencode(
        str(project), bridge_config_root=tmp_path / "host-config"
    )
    assert ok, message
    driver = tmp_path / "drive_plugin.ts"
    driver.write_text(_PLUGIN_DRIVER, encoding="utf-8")
    plugin = project / ".opencode" / "plugins" / "daem0n.ts"

    def drive() -> subprocess.CompletedProcess[str]:
        return _run(
            [shutil.which("bun") or "bun", str(driver), str(plugin), str(project)],
            project,
            tmp_path,
        )

    result = drive()
    assert result.returncode == 0, result.stderr
    assert _imported(marker) == ""
    assert json.loads(result.stdout.splitlines()[-1]) == {"edit": "allowed"}

    # A repository can ship any opencode.json it likes; it must not be able to
    # choose the program the hooks run.
    config_path = project / "opencode.json"
    config = json.loads(config_path.read_text(encoding="utf-8"))
    config["mcp"]["daem0nmcp"]["environment"]["DAEM0NMCP_PYTHON_EXECUTABLE"] = str(
        project / "interpreter-from-the-project"
    )
    config_path.write_text(json.dumps(config), encoding="utf-8")

    result = drive()
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout.splitlines()[-1]) == {"edit": "allowed"}
