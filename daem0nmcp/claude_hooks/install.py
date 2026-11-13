"""
Installer for Claude Code hooks.

Manages entries in ``~/.claude/settings.json`` to register Daem0n-MCP
hook scripts. Also handles uninstallation and legacy hook replacement.
"""

import argparse
import json
import re
import shutil
import sys
from pathlib import Path
from typing import Any

from ..edit_host import (
    local_bridge_layout,
    provision_client_bridge_installation,
)
from ..protected_files import reject_linked_ancestry

# Hosts start hooks and the MCP server inside whatever project is open, and
# ``python -m`` puts that directory first on ``sys.path``: a project shipping
# its own ``daem0nmcp`` package would run instead of the installed one.  This
# runs the module the same way after dropping that entry.  ``-P`` needs Python
# 3.11, and ``-I`` would also hide ``--user`` and ``PYTHONPATH`` installs.
_LAUNCH_CODE = (
    "import sys; sys.path = [p for p in sys.path if p]; "
    "import runpy; runpy.run_module('{module}', run_name='__main__', alter_sys=True)"
)
_MODULE_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*")
# What installers before that form wrote; kept only to report its replacement.
_MODULE_SWITCH_COMMAND = re.compile(r"\s-m\s+daem0nmcp\.claude_hooks\.")

# Env keys only pairing writes into <project>/.claude/settings.local.json.
# Pairing always writes the credential key, so it marks a paired project.
_PAIRING_MARKER = "DAEM0NMCP_EDIT_BRIDGE_CREDENTIAL_FILE"
_BRIDGE_ENV_KEYS = (
    _PAIRING_MARKER,
    "DAEM0NMCP_EDIT_BRIDGE_RUNTIME_DIR",
    "DAEM0NMCP_EDIT_BRIDGE_MODE",
    "DAEM0NMCP_EDIT_HOST_WORKSPACE_BINDING_FILE",
    "DAEM0NMCP_AUTOMATION_MODE",
)
# General Daem0n keys that pairing also sets; removed only from paired projects.
_SHARED_ENV_KEYS = ("DAEM0NMCP_PROJECT_ROOT", "DAEM0NMCP_STORAGE_PATH")


def _settings_path() -> Path:
    return Path.home() / ".claude" / "settings.json"


def _read_json_object(path: Path) -> dict[str, Any]:
    """Read a JSON object (a UTF-8 BOM is fine); raise ValueError otherwise."""
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"{path} is not readable JSON ({exc})") from exc
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return value


def _read_settings(path: Path | None = None) -> dict[str, Any]:
    """Read ``~/.claude/settings.json``; raise ValueError rather than lose it."""
    path = _settings_path() if path is None else path
    if not path.exists():
        return {}
    settings = _read_json_object(path)
    return _validated_settings(settings, path)


def _validated_settings(settings: Any, path: Path) -> dict[str, Any]:
    if not isinstance(settings, dict):
        raise ValueError(f"{path} must contain a JSON object")
    hooks = settings.get("hooks", {})
    if not isinstance(hooks, dict) or not all(
        isinstance(entries, list) for entries in hooks.values()
    ):
        raise ValueError(f"{path}: 'hooks' must map each event to a list")
    return settings


def _write_settings(data: dict[str, Any], path: Path | None = None) -> None:
    path = _settings_path() if path is None else path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def _is_daem0n_entry(entry: dict) -> bool:
    """Check if a hook entry belongs to Daem0n (current or legacy)."""
    hooks = entry.get("hooks") if isinstance(entry, dict) else None
    for hook in hooks if isinstance(hooks, list) else []:
        cmd = hook.get("command") if isinstance(hook, dict) else None
        args = hook.get("args") if isinstance(hook, dict) else None
        if isinstance(args, list) and any(
            isinstance(arg, str) and "run_module('daem0nmcp.claude_hooks." in arg
            for arg in args
        ):
            return True
        if not isinstance(cmd, str):
            continue
        if "daem0nmcp.claude_hooks" in cmd:
            return True
        # Legacy hook scripts
        if (
            "daem0n_pre_edit_hook" in cmd
            or "daem0n_stop_hook" in cmd
            or "daem0n_post_edit_hook" in cmd
            or "daem0n_prompt_hook" in cmd
        ):
            return True
    return False


def _preserved_entries(entries: list[Any]) -> list[Any]:
    """Remove owned commands without dropping unrelated commands in an entry."""
    preserved = []
    for entry in entries:
        if not _is_daem0n_entry(entry):
            preserved.append(entry)
            continue
        hooks = [
            hook for hook in entry["hooks"] if not _is_daem0n_entry({"hooks": [hook]})
        ]
        if hooks:
            preserved.append({**entry, "hooks": hooks})
    return preserved


def module_launch_args(module: str) -> list[str]:
    """Return interpreter arguments that run *module* like ``-m``, minus the cwd."""
    if not _MODULE_NAME.fullmatch(module):
        raise ValueError(f"not a module name: {module!r}")
    return ["-c", _LAUNCH_CODE.format(module=module)]


def _module_switch_commands(entry: object) -> int:
    """Count *entry*'s commands in the ``python -m`` form older installers wrote."""
    hooks = entry.get("hooks") if isinstance(entry, dict) else None
    return sum(
        isinstance(hook, dict)
        and isinstance(hook.get("command"), str)
        and _MODULE_SWITCH_COMMAND.search(hook["command"]) is not None
        for hook in (hooks if isinstance(hooks, list) else [])
    )


def _build_hook_definitions(*, enforcing: bool = False) -> dict[str, Any]:
    """Build the hook definitions using the current Python interpreter."""
    python = sys.executable
    # Quote the path for safety (spaces in Windows paths)
    q = f'"{python}"'

    def command(hook: str) -> str:
        flag, code = module_launch_args(f"daem0nmcp.claude_hooks.{hook}")
        # The code holds no double quote, ``$``, backtick or backslash, so
        # plain double quotes are enough for sh, bash and cmd.exe alike.
        return f'{q} {flag} "{code}"'

    definitions = {
        "hooks": {
            "PreToolUse": [
                {
                    "matcher": "Edit|Write|NotebookEdit",
                    "hooks": [
                        {
                            "type": "command",
                            "command": command("pre_edit"),
                        }
                    ],
                },
                {
                    "matcher": "Bash",
                    "hooks": [
                        {
                            "type": "command",
                            "command": command("pre_bash"),
                        }
                    ],
                },
            ],
            "PostToolUse": [
                {
                    "matcher": "mcp__.*__edit_preflight",
                    "hooks": [
                        {
                            "type": "command",
                            "command": command("post_edit_preflight"),
                        }
                    ],
                },
                {
                    "matcher": "Edit|Write|NotebookEdit",
                    "hooks": [
                        {
                            "type": "command",
                            "command": command("post_edit"),
                        }
                    ],
                },
            ],
            "Stop": [
                {
                    "matcher": "",
                    "hooks": [
                        {
                            "type": "command",
                            "command": command("stop"),
                        }
                    ],
                },
            ],
            "SubagentStop": [
                {
                    "matcher": "",
                    "hooks": [
                        {
                            "type": "command",
                            "command": command("stop"),
                        }
                    ],
                },
            ],
            "SessionStart": [
                {
                    "matcher": "",
                    "hooks": [
                        {
                            "type": "command",
                            "command": command("session_start"),
                        }
                    ],
                },
            ],
        }
    }
    if enforcing:
        for event_name, entries in definitions["hooks"].items():
            for entry in entries:
                for handler in entry["hooks"]:
                    matched = re.search(r"run_module\('([^']+)'", handler["command"])
                    if matched is None:
                        raise ValueError("managed hook launch module is unavailable")
                    handler["command"] = python
                    handler["args"] = [
                        "-E",
                        "-u",
                        "-X",
                        "utf8",
                        *module_launch_args(matched.group(1)),
                    ]
                    handler["timeout"] = 10
                    if event_name == "PreToolUse":
                        handler["onFailure"] = "block"
    return definitions


def _configure_project_bridge(
    project_root: Path,
    environment: dict[str, str],
) -> Path:
    path = project_root / ".claude" / "settings.local.json"
    value = _read_json_object(path) if path.exists() else {}
    configured = value.setdefault("env", {})
    if not isinstance(configured, dict):
        raise ValueError("project-local Claude env settings must be an object")
    configured.update(environment)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")
    return path


def install_claude_hooks(
    dry_run: bool = False,
    *,
    project_path: str | Path | None = None,
    enable_edit_bridge: bool | None = None,
    automation: str | None = None,
    client_version: str | None = None,
    bridge_config_root: Path | None = None,
    remote_workspace_id: str | None = None,
    remote_credential_path: str | Path | None = None,
    remote_base_url: str | None = None,
    remote_ca_file: str | Path | None = None,
    remote_origin: str | None = None,
) -> tuple[bool, str]:
    """
    Install Claude Code hooks for Daem0n enforcement.

    Replaces any existing Daem0n or legacy entries while preserving
    all other hooks.

    Returns (success, message).
    """
    if automation not in {None, "off", "remind", "enforce"}:
        return False, "Choose automation off, remind, or enforce. Nothing was changed."
    if automation is not None and project_path is None:
        return (
            False,
            "Explicit automation requires a project path. Nothing was changed.",
        )
    if automation == "enforce":
        version = re.fullmatch(r"(\d+)\.(\d+)\.(\d+)", client_version or "")
        if version is None or tuple(map(int, version.groups())) < (2, 1, 295):
            return (
                False,
                "UNSUPPORTED_AUTOMATION: Claude edit enforcement requires an observed "
                "--client-version 2.1.295 or later with onFailure=block support. "
                "Use --automation remind on older clients. Nothing was changed.",
            )
    project_snapshot = None
    try:
        if automation is not None:
            # Preserve the unresolved path until links/reparse ancestry have
            # been rejected. Resolving first would hide a linked project root.
            reject_linked_ancestry(Path(project_path))
        project_root = (
            None if project_path is None else Path(project_path).resolve(strict=True)
        )
        target_settings = (
            project_root / ".claude" / "settings.local.json"
            if automation is not None and project_root is not None
            else _settings_path()
        )
        if automation is None:
            settings = _read_settings(target_settings)
        else:
            # Reuse the multi-client installer's bounded no-follow snapshot and
            # anchored publisher, including checks for changes before commit.
            from ..client_install import _read

            project_snapshot = _read(target_settings)
            value = (
                {}
                if project_snapshot.data is None
                else json.loads(project_snapshot.data.decode("utf-8-sig"))
            )
            settings = _validated_settings(value, target_settings)
    except (OSError, ValueError) as exc:
        return False, f"{exc}; fix or move it and retry. Nothing was changed."
    new_defs = (
        {"hooks": {}}
        if automation == "off"
        else _build_hook_definitions(enforcing=automation == "enforce")
    )
    if automation == "enforce":
        new_defs["hooks"]["PostToolUseFailure"] = [
            {
                **entry,
                "hooks": [dict(hook) for hook in entry["hooks"]],
            }
            for entry in new_defs["hooks"]["PostToolUse"]
            if entry["matcher"] == "Edit|Write|NotebookEdit"
        ]
    should_pair_bridge = (
        enable_edit_bridge
        if enable_edit_bridge is not None
        else any(
            item is not None
            for item in (
                bridge_config_root,
                remote_workspace_id,
                remote_credential_path,
                remote_base_url,
                remote_ca_file,
                remote_origin,
            )
        )
    )
    if automation == "enforce":
        should_pair_bridge = True
    elif automation in {"off", "remind"}:
        should_pair_bridge = False
    if automation is not None:
        environment = settings.setdefault("env", {})
        if not isinstance(environment, dict):
            return False, "Project env settings must be an object. Nothing was changed."
        environment["DAEM0NMCP_AUTOMATION_MODE"] = automation
    installation = None
    if project_root is not None and should_pair_bridge and not dry_run:
        try:
            installation = provision_client_bridge_installation(
                project_root,
                config_root=bridge_config_root,
                remote_workspace_id=remote_workspace_id,
                remote_credential_path=remote_credential_path,
                remote_base_url=remote_base_url,
                remote_ca_file=remote_ca_file,
                remote_origin=remote_origin,
            )
            if automation is None:
                project_settings = _configure_project_bridge(
                    project_root, installation.environment()
                )
            else:
                settings["env"].update(installation.environment())
                project_settings = target_settings
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            return False, f"Edit bridge pairing failed: {exc}"

    hooks_section = settings.setdefault("hooks", {})
    replaced = sum(
        _module_switch_commands(entry)
        for entries in hooks_section.values()
        for entry in entries
    )

    # First remove all Daem0n / legacy entries from every event. This cleans up
    # deprecated events (e.g. older SessionStart installs) on upgrade.
    for event in list(hooks_section.keys()):
        filtered = _preserved_entries(hooks_section[event])
        if filtered:
            hooks_section[event] = filtered
        else:
            del hooks_section[event]

    for event, new_entries in new_defs["hooks"].items():
        existing = hooks_section.get(event, [])
        existing.extend(new_entries)
        hooks_section[event] = existing

    settings["hooks"] = hooks_section

    replaced_note = (
        f" {replaced} older hook command{'s' if replaced != 1 else ''}"
        ' that ran "python -m", which also imports from the project a'
        " session is opened in."
    )
    if dry_run:
        formatted = json.dumps(settings, indent=2)
        message = f"[dry-run] Would write to {target_settings}:\n{formatted}"
        if replaced:
            message += f"\nWould replace{replaced_note}"
        return True, message

    try:
        if project_snapshot is None:
            _write_settings(settings, target_settings)
        else:
            from ..client_install import _Plan, _publish

            _publish(
                project_root,
                [
                    _Plan(
                        project_snapshot,
                        (json.dumps(settings, indent=2) + "\n").encode("utf-8"),
                    )
                ],
                [project_snapshot],
            )
    except (OSError, ValueError) as exc:
        message = f"Failed to write settings: {exc}"
        if installation is not None:
            message += f"\nPrepared bridge credential: {installation.credential_path}"
        return False, message

    events = ", ".join(sorted(new_defs["hooks"]))
    message = (
        f"Installed Daem0n hooks for: {events or 'none'}\nSettings: {target_settings}"
    )
    if automation is not None:
        message += f"\nAutomation: {automation}; covered native tools: " + (
            "none" if automation == "off" else "Edit, Write, NotebookEdit"
        )
    if replaced:
        message += f"\nReplaced{replaced_note}"
    if installation is not None:
        state = "created" if installation.created else "reused"
        message += (
            f"\nNative edit bridge: {state} host credential "
            f"{installation.credential_path}\nProject settings: {project_settings}"
        )
    return True, message


def _strip_pairing_env(path: Path) -> dict[str, Any] | None:
    """Return *path*'s settings without the pairing env keys, or None if unpaired."""
    if not path.is_file():
        return None
    value = _read_json_object(path)
    configured = value.get("env")
    if not isinstance(configured, dict) or _PAIRING_MARKER not in configured:
        return None
    for key in _BRIDGE_ENV_KEYS + _SHARED_ENV_KEYS:
        configured.pop(key, None)
    if not configured:
        del value["env"]
    return value


def _uninstall_project_automation(
    project_path: str | Path,
    *,
    dry_run: bool,
    remove_credentials: bool,
    bridge_config_root: Path | None,
) -> tuple[bool, str] | None:
    """Remove an explicit project install using the same protected publication."""
    from ..client_install import _Plan, _publish, _read

    try:
        reject_linked_ancestry(project_path)
        root = Path(project_path).resolve(strict=False)
        if not root.is_dir():
            return None
        path = root / ".claude" / "settings.local.json"
        snapshot = _read(path)
        if snapshot.data is None:
            return None
        settings = _validated_settings(
            json.loads(snapshot.data.decode("utf-8-sig")), path
        )
        environment = settings.get("env", {})
        if not isinstance(environment, dict):
            raise ValueError("project-local Claude env settings must be an object")
        hooks = settings.get("hooks", {})
        explicit = "DAEM0NMCP_AUTOMATION_MODE" in environment or any(
            _is_daem0n_entry(entry) for entries in hooks.values() for entry in entries
        )
        if not explicit:
            return None
        for event, entries in list(hooks.items()):
            kept = _preserved_entries(entries)
            if kept:
                hooks[event] = kept
            else:
                hooks.pop(event)
        paired = _PAIRING_MARKER in environment
        environment.pop("DAEM0NMCP_AUTOMATION_MODE", None)
        if paired:
            for key in _BRIDGE_ENV_KEYS + _SHARED_ENV_KEYS:
                environment.pop(key, None)
        if not environment:
            settings.pop("env", None)
        if not hooks:
            settings.pop("hooks", None)
        directories = []
        if remove_credentials:
            _, _, *layout = local_bridge_layout(root, bridge_config_root)
            directories = [directory for directory in layout if directory.is_dir()]
        if not dry_run:
            _publish(
                root,
                [_Plan(snapshot, (json.dumps(settings, indent=2) + "\n").encode())],
                [snapshot],
            )
        prefix = "[dry-run] Would remove" if dry_run else "Removed"
        message = f"{prefix} project Daem0n automation from {path}"
        for directory in directories:
            reject_linked_ancestry(directory)
            if not dry_run:
                shutil.rmtree(directory)
            message += f"\n{prefix} {directory}"
        return True, message
    except json.JSONDecodeError:
        # Preserve the legacy uninstaller's reported partial cleanup for an
        # unreadable project document; the safe snapshot already rejected links.
        return None
    except (OSError, ValueError) as exc:
        return False, f"Project automation removal failed: {exc}"


def uninstall_claude_hooks(
    dry_run: bool = False,
    *,
    project_path: str | Path | None = None,
    remove_credentials: bool = False,
    bridge_config_root: Path | None = None,
) -> tuple[bool, str]:
    """
    Remove all Daem0n Claude Code hooks.

    Preserves all other hooks. Cleans up empty event lists. With
    *project_path*, also strips the pairing env keys from that project's
    ``.claude/settings.local.json``; with *remove_credentials*, also deletes
    the project's local edit-bridge credential and socket directories.

    Returns (success, message); the message lists everything that was done,
    including after a partial failure.
    """
    if project_path is not None:
        project_result = _uninstall_project_automation(
            project_path,
            dry_run=dry_run,
            remove_credentials=remove_credentials,
            bridge_config_root=bridge_config_root,
        )
        if project_result is not None:
            return project_result
    try:
        settings = _read_settings()
    except ValueError as exc:
        return False, f"{exc}; fix or move it and retry. Nothing was changed."

    # Read everything that can fail before writing anything.
    notes: list[str] = []
    ok = True
    local_path: Path | None = None
    local_value: dict[str, Any] | None = None
    directories: list[Path] = []
    if project_path is not None:
        project_root = Path(project_path).resolve(strict=False)
        local_path = project_root / ".claude" / "settings.local.json"
        try:
            local_value = _strip_pairing_env(local_path)
        except ValueError as exc:
            notes.append(f"Left {local_path} unchanged: {exc}")
            ok = False
        if remove_credentials:
            try:
                _, _, *layout = local_bridge_layout(project_root, bridge_config_root)
            except (OSError, ValueError) as exc:
                notes.append(
                    f"Could not locate edit bridge credentials for {project_root} "
                    f"({exc}); delete its directory under "
                    "~/.daem0nmcp/edit-bridges/ by hand."
                )
                ok = False
            else:
                directories = [path for path in layout if path.is_dir()]
    elif remove_credentials:
        notes.append("No project given; edit bridge credentials kept.")

    hooks_section = settings.get("hooks", {})
    removed_events: list[str] = []
    for event in list(hooks_section.keys()):
        entries = hooks_section[event]
        filtered = _preserved_entries(entries)
        if len(filtered) < len(entries):
            removed_events.append(event)
        if filtered:
            hooks_section[event] = filtered
        else:
            del hooks_section[event]

    lines: list[str] = []
    if not removed_events:
        lines.append("No Daem0n hooks found to remove.")
    elif dry_run:
        settings["hooks"] = hooks_section
        formatted = json.dumps(settings, indent=2)
        lines.append(f"[dry-run] Would write to {_settings_path()}:\n{formatted}")
    else:
        settings["hooks"] = hooks_section
        try:
            _write_settings(settings)
        except OSError as exc:
            return False, f"Failed to write settings: {exc}"
        lines.append(f"Removed Daem0n hooks from: {', '.join(sorted(removed_events))}")
    lines.extend(notes)

    prefix = "[dry-run] Would remove" if dry_run else "Removed"
    if local_path is not None and local_value is not None:
        try:
            if not dry_run:
                local_path.write_text(
                    json.dumps(local_value, indent=2) + "\n", encoding="utf-8"
                )
            lines.append(f"{prefix} edit bridge settings from {local_path}")
        except OSError as exc:
            lines.append(f"Could not update {local_path}: {exc}")
            ok = False
    for directory in directories:
        try:
            reject_linked_ancestry(directory)
            if not dry_run:
                shutil.rmtree(directory)
            lines.append(f"{prefix} {directory}")
        except OSError as exc:
            lines.append(f"Could not remove {directory}: {exc}")
            ok = False

    return ok, "\n".join(lines)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Manage Daem0n Claude Code hooks")
    parser.add_argument("--uninstall", action="store_true", help="Remove hooks")
    parser.add_argument("--dry-run", action="store_true", help="Show what would change")
    parser.add_argument("--project-path", default=".", help="Project root")
    parser.add_argument("--automation", choices=("off", "remind", "enforce"))
    parser.add_argument("--client-version", help="Observed Claude Code version")
    parser.add_argument(
        "--enable-edit-bridge",
        action="store_true",
        help="Also provision project-local edit-bridge credentials",
    )
    parser.add_argument("--remote-workspace-id")
    parser.add_argument("--remote-credential-file")
    parser.add_argument("--remote-url")
    parser.add_argument("--remote-ca-file")
    parser.add_argument("--remote-origin")
    parser.add_argument("--remove-credentials", action="store_true")
    args = parser.parse_args()

    if args.uninstall:
        ok, msg = uninstall_claude_hooks(
            dry_run=args.dry_run,
            project_path=args.project_path,
            remove_credentials=args.remove_credentials,
        )
    else:
        ok, msg = install_claude_hooks(
            dry_run=args.dry_run,
            project_path=args.project_path,
            enable_edit_bridge=True if args.enable_edit_bridge else None,
            automation=args.automation,
            client_version=args.client_version,
            remote_workspace_id=args.remote_workspace_id,
            remote_credential_path=args.remote_credential_file,
            remote_base_url=args.remote_url,
            remote_ca_file=args.remote_ca_file,
            remote_origin=args.remote_origin,
        )

    print(msg)
    sys.exit(0 if ok else 1)
