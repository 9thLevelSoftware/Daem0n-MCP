"""
Installer for Claude Code hooks.

Manages entries in ``~/.claude/settings.json`` to register Daem0n-MCP
hook scripts. Also handles uninstallation and legacy hook replacement.
"""

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from ..edit_host import provision_client_bridge_installation


def _settings_path() -> Path:
    return Path.home() / ".claude" / "settings.json"


def _read_settings() -> dict[str, Any]:
    path = _settings_path()
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {}
    return {}


def _write_settings(data: dict[str, Any]) -> None:
    path = _settings_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def _is_daem0n_entry(entry: dict) -> bool:
    """Check if a hook entry belongs to Daem0n (current or legacy)."""
    for hook in entry.get("hooks", []):
        cmd = hook.get("command", "")
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
    if path.exists():
        value = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            raise ValueError("project-local Claude settings must be an object")
    else:
        value = {}
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
    settings = _read_settings()
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


def uninstall_claude_hooks(dry_run: bool = False) -> tuple[bool, str]:
    """
    Remove all Daem0n Claude Code hooks.

    Preserves all other hooks. Cleans up empty event lists.

    Returns (success, message).
    """
    settings = _read_settings()
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

    if not removed_events:
        return True, "No Daem0n hooks found to remove."

    settings["hooks"] = hooks_section

    if dry_run:
        formatted = json.dumps(settings, indent=2)
        return True, f"[dry-run] Would write to {_settings_path()}:\n{formatted}"

    try:
        _write_settings(settings)
    except OSError as exc:
        return False, f"Failed to write settings: {exc}"

    return True, f"Removed Daem0n hooks from: {', '.join(sorted(removed_events))}"


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
    args = parser.parse_args()

    if args.uninstall:
        ok, msg = uninstall_claude_hooks(dry_run=args.dry_run)
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
