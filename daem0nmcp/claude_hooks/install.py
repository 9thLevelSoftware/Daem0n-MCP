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


def _build_hook_definitions() -> dict[str, list[dict]]:
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
                    "matcher": "Edit|Write",
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


def install_claude_hooks(dry_run: bool = False) -> tuple[bool, str]:
    """
    Install Claude Code hooks for Daem0n enforcement.

    Replaces any existing Daem0n or legacy entries while preserving
    all other hooks.

    Returns (success, message).
    """
    settings = _read_settings()
    new_defs = _build_hook_definitions()

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
    return True, f"Installed Daem0n hooks for: {events}\nSettings: {_settings_path()}"


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
    args = parser.parse_args()

    if args.uninstall:
        ok, msg = uninstall_claude_hooks(dry_run=args.dry_run)
    else:
        ok, msg = install_claude_hooks(dry_run=args.dry_run)

    print(msg)
    sys.exit(0 if ok else 1)
