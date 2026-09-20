"""
Installer for Claude Code hooks.

Manages entries in ``~/.claude/settings.json`` to register Daem0n-MCP
hook scripts. Also handles uninstallation and legacy hook replacement.
"""

import argparse
import json
import shutil
import sys
from pathlib import Path
from typing import Any

from ..edit_host import (
    local_bridge_layout,
    provision_client_bridge_installation,
)
from ..protected_files import reject_linked_ancestry

# Env keys only pairing writes into <project>/.claude/settings.local.json.
# Pairing always writes the credential key, so it marks a paired project.
_PAIRING_MARKER = "DAEM0NMCP_EDIT_BRIDGE_CREDENTIAL_FILE"
_BRIDGE_ENV_KEYS = (
    _PAIRING_MARKER,
    "DAEM0NMCP_EDIT_BRIDGE_RUNTIME_DIR",
    "DAEM0NMCP_EDIT_BRIDGE_MODE",
    "DAEM0NMCP_EDIT_HOST_WORKSPACE_BINDING_FILE",
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


def _read_settings() -> dict[str, Any]:
    """Read ``~/.claude/settings.json``; raise ValueError rather than lose it."""
    path = _settings_path()
    if not path.exists():
        return {}
    settings = _read_json_object(path)
    hooks = settings.get("hooks", {})
    if not isinstance(hooks, dict) or not all(
        isinstance(entries, list) for entries in hooks.values()
    ):
        raise ValueError(f"{path}: 'hooks' must map each event to a list")
    return settings


def _write_settings(data: dict[str, Any]) -> None:
    path = _settings_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def _is_daem0n_entry(entry: dict) -> bool:
    """Check if a hook entry belongs to Daem0n (current or legacy)."""
    hooks = entry.get("hooks") if isinstance(entry, dict) else None
    for hook in hooks if isinstance(hooks, list) else []:
        cmd = hook.get("command") if isinstance(hook, dict) else None
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


def _build_hook_definitions() -> dict[str, Any]:
    """Build the hook definitions using the current Python interpreter."""
    python = sys.executable
    # Quote the path for safety (spaces in Windows paths)
    q = f'"{python}"'

    return {
        "hooks": {
            "PreToolUse": [
                {
                    "matcher": "Edit|Write|NotebookEdit",
                    "hooks": [
                        {
                            "type": "command",
                            "command": f"{q} -m daem0nmcp.claude_hooks.pre_edit",
                        }
                    ],
                },
                {
                    "matcher": "Bash",
                    "hooks": [
                        {
                            "type": "command",
                            "command": f"{q} -m daem0nmcp.claude_hooks.pre_bash",
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
                            "command": f"{q} -m daem0nmcp.claude_hooks.post_edit_preflight",
                        }
                    ],
                },
                {
                    "matcher": "Edit|Write|NotebookEdit",
                    "hooks": [
                        {
                            "type": "command",
                            "command": f"{q} -m daem0nmcp.claude_hooks.post_edit",
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
                            "command": f"{q} -m daem0nmcp.claude_hooks.stop",
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
                            "command": f"{q} -m daem0nmcp.claude_hooks.stop",
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
                            "command": f"{q} -m daem0nmcp.claude_hooks.session_start",
                        }
                    ],
                },
            ],
        }
    }


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
    try:
        settings = _read_settings()
    except ValueError as exc:
        return False, f"{exc}; fix or move it and retry. Nothing was changed."
    new_defs = _build_hook_definitions()
    project_root = (
        None if project_path is None else Path(project_path).resolve(strict=True)
    )
    installation = None
    if project_root is not None and not dry_run:
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
            project_settings = _configure_project_bridge(
                project_root, installation.environment()
            )
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            return False, f"Edit bridge pairing failed: {exc}"

    hooks_section = settings.setdefault("hooks", {})

    # First remove all Daem0n / legacy entries from every event. This cleans up
    # deprecated events (e.g. older SessionStart installs) on upgrade.
    for event in list(hooks_section.keys()):
        filtered = [e for e in hooks_section[event] if not _is_daem0n_entry(e)]
        if filtered:
            hooks_section[event] = filtered
        else:
            del hooks_section[event]

    for event, new_entries in new_defs["hooks"].items():
        existing = hooks_section.get(event, [])
        existing.extend(new_entries)
        hooks_section[event] = existing

    settings["hooks"] = hooks_section

    if dry_run:
        formatted = json.dumps(settings, indent=2)
        return True, f"[dry-run] Would write to {_settings_path()}:\n{formatted}"

    try:
        _write_settings(settings)
    except OSError as exc:
        return False, f"Failed to write settings: {exc}"

    events = ", ".join(sorted(new_defs["hooks"]))
    message = f"Installed Daem0n hooks for: {events}\nSettings: {_settings_path()}"
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
        filtered = [e for e in entries if not _is_daem0n_entry(e)]
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
            remote_workspace_id=args.remote_workspace_id,
            remote_credential_path=args.remote_credential_file,
            remote_base_url=args.remote_url,
            remote_ca_file=args.remote_ca_file,
            remote_origin=args.remote_origin,
        )

    print(msg)
    sys.exit(0 if ok else 1)
