#!/usr/bin/env python3
r"""
Emergency repair: unblock Claude Code when startup hangs.

Modes:
    python fix_sessionstart.py                  # dry-run (show everything found)
    python fix_sessionstart.py --apply          # nuke hooks, plugins, MCP refs
    python fix_sessionstart.py --restore        # undo all changes from --apply
    python fix_sessionstart.py --project /path  # also scan a project directory

What it does:
  1. Removes SessionStart hooks from all settings files (global + project)
  2. Disables ALL plugins in enabledPlugins (any can register hooks at runtime)
  3. Disables daem0nmcp MCP server entries in .mcp.json files
  4. Backs up every file before touching it (.json.bak)
  5. --restore puts every .bak file back

Auto-detection:
  - If cwd has .claude/ or .mcp.json, it's included automatically
  - Immediate child directories of cwd are also scanned, so running
    from a parent dir (e.g. D:\Data\git\repos) finds all repos underneath

No dependencies outside the Python standard library.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------

def find_claude_settings_files(project_dirs: list[str] | None = None) -> list[Path]:
    """Find all Claude Code settings files that could contain hooks."""
    home = Path.home()
    candidates = [
        home / ".claude" / "settings.json",
        home / ".claude" / "settings.local.json",
        home / ".claude" / "settings.managed.json",
    ]

    for proj in (project_dirs or []):
        p = Path(proj).resolve()
        candidates.append(p / ".claude" / "settings.json")
        candidates.append(p / ".claude" / "settings.local.json")
        # Also check subdirectories one level deep (multi-repo parent dirs)
        if p.is_dir():
            for child in p.iterdir():
                if child.is_dir():
                    claude_dir = child / ".claude"
                    if claude_dir.exists():
                        candidates.append(claude_dir / "settings.json")
                        candidates.append(claude_dir / "settings.local.json")

    return [f for f in candidates if f.exists()]


def find_mcp_json_files(project_dirs: list[str] | None = None) -> list[Path]:
    """Find all .mcp.json files that could define MCP servers."""
    home = Path.home()
    candidates = [
        home / ".claude" / ".mcp.json",
        home / ".mcp.json",
    ]

    for proj in (project_dirs or []):
        p = Path(proj).resolve()
        candidates.append(p / ".mcp.json")
        # Also check subdirectories one level deep (monorepo projects)
        if p.is_dir():
            for child in p.iterdir():
                if child.is_dir():
                    candidate = child / ".mcp.json"
                    if candidate.exists():
                        candidates.append(candidate)

    return [f for f in candidates if f.exists()]


def find_plugin_hooks(home: Path | None = None) -> list[tuple[str, Path, list[str]]]:
    """
    Find plugins that define SessionStart hooks.

    Returns list of (plugin_name, plugin_json_path, hook_files).
    """
    home = home or Path.home()
    results = []

    plugins_dir = home / ".claude" / "plugins"
    if not plugins_dir.exists():
        return results

    for plugin_json in plugins_dir.rglob("plugin.json"):
        try:
            data = json.loads(plugin_json.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue

        name = data.get("name", plugin_json.parent.name)
        hook_files = []

        hooks = data.get("hooks", {})
        if "SessionStart" in hooks:
            hook_files.append(str(plugin_json))

        # Check all json files in plugin dir for hook definitions
        for hook_file in plugin_json.parent.rglob("*.json"):
            if hook_file == plugin_json:
                continue
            try:
                hook_data = json.loads(hook_file.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                continue
            if isinstance(hook_data, dict) and "SessionStart" in hook_data.get("hooks", {}):
                hook_files.append(str(hook_file))

        if hook_files:
            results.append((name, plugin_json, hook_files))

    return results


# ---------------------------------------------------------------------------
# Repair actions
# ---------------------------------------------------------------------------

def _backup(file_path: Path) -> Path:
    """Back up a file. Returns backup path."""
    backup = file_path.with_suffix(file_path.suffix + ".bak")
    shutil.copy2(file_path, backup)
    return backup


def _ensure_backup(file_path: Path) -> Path:
    """Back up only if not already backed up. Returns backup path."""
    backup = file_path.with_suffix(file_path.suffix + ".bak")
    if not backup.exists():
        shutil.copy2(file_path, backup)
    return backup


def remove_session_start_hooks(file_path: Path, dry_run: bool = True) -> tuple[bool, str]:
    """Remove SessionStart entries from a settings file's hooks section."""
    try:
        text = file_path.read_text(encoding="utf-8")
        data = json.loads(text)
    except (json.JSONDecodeError, OSError) as e:
        return False, "  SKIP  {} (cannot read: {})".format(file_path, e)

    hooks = data.get("hooks", {})
    if "SessionStart" not in hooks:
        return False, "  OK    {} (no SessionStart hooks)".format(file_path)

    entries = hooks["SessionStart"]
    cmds = []
    for entry in entries:
        for h in entry.get("hooks", []):
            cmds.append(h.get("command", "<unknown>"))
    cmd_summary = "\n          ".join(cmds) if cmds else "<empty>"

    if dry_run:
        return True, (
            "  FOUND {}\n"
            "        {} SessionStart hook(s):\n"
            "          {}\n"
            "        (use --apply to remove)"
        ).format(file_path, len(entries), cmd_summary)

    _ensure_backup(file_path)

    del hooks["SessionStart"]
    if not hooks:
        del data["hooks"]

    file_path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")

    return True, (
        "  FIXED {}\n"
        "        Removed {} SessionStart hook(s)"
    ).format(file_path, len(entries))


def disable_all_plugins(file_path: Path, dry_run: bool = True) -> tuple[bool, str]:
    """Set all enabledPlugins to false in a settings file."""
    try:
        text = file_path.read_text(encoding="utf-8")
        data = json.loads(text)
    except (json.JSONDecodeError, OSError) as e:
        return False, "  SKIP  {} (cannot read: {})".format(file_path, e)

    plugins = data.get("enabledPlugins", {})
    enabled = {k: v for k, v in plugins.items() if v}

    if not enabled:
        return False, "  OK    {} (no enabled plugins)".format(file_path)

    if dry_run:
        names = "\n          ".join(enabled.keys())
        return True, (
            "  FOUND {}\n"
            "        {} enabled plugin(s):\n"
            "          {}\n"
            "        (use --apply to disable all)"
        ).format(file_path, len(enabled), names)

    _ensure_backup(file_path)

    for key in plugins:
        plugins[key] = False
    data["enabledPlugins"] = plugins

    file_path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")

    return True, (
        "  FIXED {}\n"
        "        Disabled {} plugin(s)"
    ).format(file_path, len(enabled))


def disable_daem0n_mcp(file_path: Path, dry_run: bool = True) -> tuple[bool, str]:
    """
    Disable daem0nmcp server entries in a .mcp.json file.

    Renames the key from "daem0nmcp" to "daem0nmcp_DISABLED" so Claude Code
    won't try to start it, but the config is preserved for --restore.
    """
    try:
        text = file_path.read_text(encoding="utf-8")
        data = json.loads(text)
    except (json.JSONDecodeError, OSError) as e:
        return False, "  SKIP  {} (cannot read: {})".format(file_path, e)

    servers = data.get("mcpServers", {})
    daem0n_keys = [k for k in servers if "daem0n" in k.lower() and "_DISABLED" not in k]

    if not daem0n_keys:
        return False, "  OK    {} (no daem0n MCP servers)".format(file_path)

    # Show what we found
    details = []
    for k in daem0n_keys:
        server = servers[k]
        cmd = server.get("command", "")
        args = " ".join(server.get("args", []))
        details.append("{}: {} {}".format(k, cmd, args))
    detail_str = "\n          ".join(details)

    if dry_run:
        return True, (
            "  FOUND {}\n"
            "        {} daem0n MCP server(s):\n"
            "          {}\n"
            "        (use --apply to disable)"
        ).format(file_path, len(daem0n_keys), detail_str)

    _ensure_backup(file_path)

    for k in daem0n_keys:
        servers[k + "_DISABLED"] = servers.pop(k)
    data["mcpServers"] = servers

    file_path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")

    return True, (
        "  FIXED {}\n"
        "        Disabled {} daem0n MCP server(s): {}"
    ).format(file_path, len(daem0n_keys), ", ".join(daem0n_keys))


# ---------------------------------------------------------------------------
# Restore
# ---------------------------------------------------------------------------

def restore_backups(project_dirs: list[str] | None = None) -> int:
    """Restore all .bak files created by --apply."""
    home = Path.home()
    search_dirs = [home / ".claude"]

    for proj in (project_dirs or []):
        p = Path(proj).resolve()
        # Check project root for .mcp.json.bak
        for bak in p.glob("*.bak"):
            target = Path(str(bak)[:-4])  # strip .bak
            shutil.copy2(bak, target)
            bak.unlink()
            print("  RESTORED {}".format(target))
        # Check .claude subdir
        claude_dir = p / ".claude"
        if claude_dir.exists():
            search_dirs.append(claude_dir)

    restored = 0
    for d in search_dirs:
        for bak in d.rglob("*.bak"):
            target = Path(str(bak)[:-4])  # strip .bak
            shutil.copy2(bak, target)
            bak.unlink()
            print("  RESTORED {}".format(target))
            restored += 1

    return restored


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Emergency repair: unblock Claude Code startup hangs"
    )
    parser.add_argument(
        "--apply", action="store_true",
        help="Remove hooks, disable plugins and MCP servers (default is dry-run)"
    )
    parser.add_argument(
        "--restore", action="store_true",
        help="Undo --apply by restoring all backup files"
    )
    parser.add_argument(
        "--project", action="append", default=[],
        help="Project directory to also scan (repeatable)"
    )
    args = parser.parse_args()

    # Auto-detect: if cwd looks like a project (has .claude/ or .mcp.json),
    # include it automatically so the script works without --project.
    # Also scan immediate child directories so running from a parent dir
    # like D:\Data\git\repos finds configs in all repos underneath.
    cwd = Path.cwd()
    explicit = {str(Path(p).resolve()) for p in args.project}
    auto_dirs = []

    if str(cwd) not in explicit:
        if (cwd / ".claude").exists() or (cwd / ".mcp.json").exists():
            auto_dirs.append(str(cwd))

    # Scan one level of child directories
    if cwd.is_dir():
        for child in cwd.iterdir():
            if not child.is_dir():
                continue
            child_str = str(child.resolve())
            if child_str in explicit or child_str in auto_dirs:
                continue
            if (child / ".claude").exists() or (child / ".mcp.json").exists():
                auto_dirs.append(child_str)

    args.project = auto_dirs + args.project

    print("")
    print("=" * 60)

    # --- Restore mode ---
    if args.restore:
        print("  Claude Code Settings Restore")
        print("=" * 60)
        print("")
        count = restore_backups(args.project)
        if count == 0:
            print("  No backup files found.")
        else:
            print("")
            print("  Restored {} file(s). Restart Claude Code.".format(count))
        print("")
        print("=" * 60)
        print("")
        return

    # --- Repair mode ---
    dry_run = not args.apply
    mode = "DRY RUN" if dry_run else "APPLYING FIXES"

    print("  Claude Code Startup Repair ({})".format(mode))
    print("=" * 60)
    print("")

    settings_files = find_claude_settings_files(args.project)
    mcp_files = find_mcp_json_files(args.project)

    if not settings_files and not mcp_files:
        print("  No Claude Code config files found.")
        print("")
        print("  Looked in:")
        print("    {}".format(Path.home() / ".claude"))
        for p in args.project:
            print("    {}".format(Path(p).resolve()))
        print("")
    else:
        # Step 1: Remove SessionStart hooks
        if settings_files:
            print("Step 1: SessionStart hooks ({} file(s))".format(
                len(settings_files)))
            print("")
            for f in settings_files:
                _, msg = remove_session_start_hooks(f, dry_run=dry_run)
                print(msg)
            print("")

        # Step 2: Disable plugins
        if settings_files:
            print("Step 2: Disable plugins")
            print("")
            for f in settings_files:
                _, msg = disable_all_plugins(f, dry_run=dry_run)
                print(msg)
            print("")

        # Step 3: Disable daem0n MCP servers
        if mcp_files:
            print("Step 3: Daem0n MCP servers ({} file(s))".format(
                len(mcp_files)))
            print("")
            for f in mcp_files:
                _, msg = disable_daem0n_mcp(f, dry_run=dry_run)
                print(msg)
            print("")
        else:
            print("Step 3: No .mcp.json files found")
            if args.project:
                print("        (checked project dirs too)")
            else:
                print("        Tip: use --project /path/to/project to scan")
                print("        project directories for .mcp.json files")
            print("")

    # Step 4: Report plugin SessionStart hooks on disk
    plugin_hooks = find_plugin_hooks()
    if plugin_hooks:
        print("Step 4: Plugin SessionStart hooks found on disk")
        print("")
        for name, path, hook_files in plugin_hooks:
            print('  PLUGIN "{}"'.format(name))
            for hf in hook_files:
                print("         {}".format(hf))
        print("")
        print("  These plugins define SessionStart hooks. After --apply")
        print("  disables them, re-enable ONE AT A TIME to find the culprit.")
        print("")
    else:
        print("Step 4: No plugin SessionStart hooks found on disk")
        print("")

    # --- Summary ---
    print("=" * 60)
    if dry_run:
        print("  No changes made. Run with --apply to fix.")
        print("  After fixing, run with --restore to undo.")
    else:
        print("  All fixes applied. Restart Claude Code.")
        print("")
        print("  To undo everything:")
        restore_cmd = "python fix_sessionstart.py --restore"
        if args.project:
            restore_cmd += "".join(" --project {}".format(p) for p in args.project)
        print("    {}".format(restore_cmd))
    print("=" * 60)
    print("")


if __name__ == "__main__":
    main()
