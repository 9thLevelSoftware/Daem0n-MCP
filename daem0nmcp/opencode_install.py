"""Installer for OpenCode integration.

Creates .opencode/ directory structure, ensures opencode.json exists
at project root, and writes the TypeScript covenant enforcement plugin
for MCP server connectivity and hook discipline.
"""

import argparse
import copy
import importlib.resources
import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

from .edit_host import BridgeInstallation, provision_client_bridge_installation


def select_opencode_interface(version: str, requested: str | None = None) -> str:
    """Select the contract that exposes native and MCP tool execution hooks."""
    if requested == "v2":
        raise ValueError(
            "OpenCode plugin V2 is released but does not expose tool execution hooks"
        )
    if requested not in {None, "v1"} or not version.startswith("1."):
        raise ValueError("OpenCode version does not support the released V1 interface")
    return "v1"


OPENCODE_JSON_TEMPLATE: dict[str, Any] = {
    "$schema": "https://opencode.ai/config.json",
    "mcp": {
        "daem0nmcp": {
            "type": "local",
            "command": ["python", "-m", "daem0nmcp"],
            "enabled": True,
            "environment": {"PYTHONUNBUFFERED": "1"},
        }
    },
}

# Subdirectories to scaffold inside .opencode/
_OPENCODE_SUBDIRS = ["commands", "plugins", "agents"]

# The packaged TypeScript resource is the sole installer source of truth.
PLUGIN_TEMPLATE = (
    importlib.resources.files("daem0nmcp.opencode_assets")
    .joinpath("daem0n.ts")
    .read_text(encoding="utf-8")
)


def detect_clients(project_path: Path) -> dict[str, Any]:
    """Detect installed AI coding clients and their configuration status.

    Checks for Claude Code and OpenCode binaries and configuration files.
    Does NOT gate installation on binary detection -- reports status only.

    Returns nested dict with ``binary_found``, individual config checks,
    and a summary ``configured`` boolean for each client.
    """
    claude_binary = shutil.which("claude") is not None
    claude_mcp_json = (project_path / ".mcp.json").exists()
    claude_dir = (project_path / ".claude").is_dir()

    opencode_binary = shutil.which("opencode") is not None
    opencode_json = (project_path / "opencode.json").exists()
    opencode_dir = (project_path / ".opencode").is_dir()
    agents_md = (project_path / "AGENTS.md").exists()

    return {
        "claude_code": {
            "binary_found": claude_binary,
            "mcp_json": claude_mcp_json,
            "claude_dir": claude_dir,
            "configured": claude_mcp_json or claude_dir,
        },
        "opencode": {
            "binary_found": opencode_binary,
            "opencode_json": opencode_json,
            "opencode_dir": opencode_dir,
            "agents_md": agents_md,
            "configured": opencode_json or opencode_dir,
        },
    }


def _ensure_dir(path: Path, dry_run: bool) -> str:
    """Ensure a directory exists. Returns status string."""
    if path.is_dir():
        return "[exists]"
    if not dry_run:
        path.mkdir(parents=True, exist_ok=True)
    return "[create]"


def _ensure_file(path: Path, content: str, dry_run: bool, force: bool) -> str:
    """Ensure a file exists with given content. Returns status string."""
    if path.exists():
        if force:
            if not dry_run:
                path.write_text(content, encoding="utf-8")
            return "[overwrite]"
        return "[exists]"
    if not dry_run:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    return "[create]"


def _configured_template(
    installation: BridgeInstallation | None,
) -> dict[str, Any]:
    template = copy.deepcopy(OPENCODE_JSON_TEMPLATE)
    template["mcp"]["daem0nmcp"]["command"] = [
        sys.executable,
        "-m",
        "daem0nmcp",
    ]
    if installation is not None:
        installation_environment = installation.environment()
        if installation_environment.get("DAEM0NMCP_EDIT_BRIDGE_MODE") != "remote-https":
            environment = template["mcp"]["daem0nmcp"]["environment"]
            environment.update(installation_environment)
            environment["DAEM0NMCP_PYTHON_EXECUTABLE"] = sys.executable
    return template


def _configure_existing_daem0n(
    path: Path,
    installation: BridgeInstallation,
) -> bool:
    """Add host-only bridge paths when an existing config owns our MCP entry."""

    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            return False
        server = value["mcp"]["daem0nmcp"]
        if not isinstance(server, dict):
            return False
        installation_environment = installation.environment()
        if installation_environment.get("DAEM0NMCP_EDIT_BRIDGE_MODE") == "remote-https":
            return True
        environment = server.setdefault("environment", {})
        if not isinstance(environment, dict):
            return False
        environment.update(installation_environment)
        command = server.get("command")
        executable = (
            command[0]
            if isinstance(command, list)
            and command
            and isinstance(command[0], str)
            and command[0]
            else sys.executable
        )
        environment["DAEM0NMCP_PYTHON_EXECUTABLE"] = executable
        path.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")
        return True
    except (KeyError, TypeError, OSError, json.JSONDecodeError):
        return False


def _installed_opencode_version() -> str:
    binary = shutil.which("opencode")
    if binary is None:
        return "1.0"
    try:
        result = subprocess.run(
            [binary, "--version"],
            check=True,
            capture_output=True,
            text=True,
            timeout=2,
        )
        return result.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return "1.0"


def install_opencode(
    project_path: str,
    dry_run: bool = False,
    force: bool = False,
    *,
    bridge_config_root: Path | None = None,
    interface: str | None = None,
    opencode_version: str | None = None,
    remote_workspace_id: str | None = None,
    remote_credential_path: str | Path | None = None,
    remote_base_url: str | None = None,
    remote_ca_file: str | Path | None = None,
    remote_origin: str | None = None,
) -> tuple[bool, str]:
    """Install OpenCode integration for a project.

    Creates ``.opencode/`` directory structure and ensures ``opencode.json``
    exists at the project root.

    Returns ``(success, message)`` tuple.
    """
    root = Path(project_path).resolve()
    lines: list[str] = []
    installation = None

    try:
        selected_interface = select_opencode_interface(
            opencode_version or _installed_opencode_version(), interface
        )
    except ValueError as exc:
        return False, f"OpenCode integration unavailable: {exc}"

    if not dry_run:
        try:
            installation = provision_client_bridge_installation(
                root,
                config_root=bridge_config_root,
                remote_workspace_id=remote_workspace_id,
                remote_credential_path=remote_credential_path,
                remote_base_url=remote_base_url,
                remote_ca_file=remote_ca_file,
                remote_origin=remote_origin,
            )
        except (OSError, ValueError) as exc:
            return False, f"Edit bridge pairing failed: {exc}"

    if dry_run:
        lines.append("[dry-run] Showing planned changes (no files will be modified)\n")

    # -- Client detection ------------------------------------------------
    clients = detect_clients(root)

    lines.append("Client detection:")
    cc = clients["claude_code"]
    lines.append(
        f"  Claude Code binary: {'found' if cc['binary_found'] else 'not found'}"
    )
    lines.append(f"  .mcp.json: {'found' if cc['mcp_json'] else 'not found'}")
    lines.append(f"  .claude/: {'found' if cc['claude_dir'] else 'not found'}")

    oc = clients["opencode"]
    lines.append(f"  OpenCode binary: {'found' if oc['binary_found'] else 'not found'}")
    lines.append(f"  opencode.json: {'found' if oc['opencode_json'] else 'not found'}")
    lines.append(f"  .opencode/: {'found' if oc['opencode_dir'] else 'not found'}")
    lines.append(f"  AGENTS.md: {'found' if oc['agents_md'] else 'not found'}")
    lines.append("")
    lines.append(f"Plugin interface: {selected_interface}")
    lines.append("")

    # -- Scaffold .opencode/ directories ---------------------------------
    try:
        lines.append("Directory scaffolding:")
        opencode_root = root / ".opencode"
        status = _ensure_dir(opencode_root, dry_run)
        lines.append(f"  {status} .opencode/")

        for subdir in _OPENCODE_SUBDIRS:
            status = _ensure_dir(opencode_root / subdir, dry_run)
            lines.append(f"  {status} .opencode/{subdir}/")
        lines.append("")

        # -- Ensure opencode.json at project root ------------------------
        lines.append("Configuration:")
        json_content = json.dumps(_configured_template(installation), indent=2) + "\n"
        json_path = root / "opencode.json"
        status = _ensure_file(json_path, json_content, dry_run, force)
        lines.append(f"  {status} opencode.json")
        if (
            installation is not None
            and status == "[exists]"
            and _configure_existing_daem0n(json_path, installation)
            and installation.environment().get("DAEM0NMCP_EDIT_BRIDGE_MODE")
            != "remote-https"
        ):
            lines.append("  [update] opencode.json Daem0n bridge environment")

        if (
            installation is not None
            and installation.environment().get("DAEM0NMCP_EDIT_BRIDGE_MODE")
            == "remote-https"
        ):
            host_environment = installation.environment()
            host_environment["DAEM0NMCP_PYTHON_EXECUTABLE"] = sys.executable
            host_path = opencode_root / "daem0n-host.json"
            if not dry_run:
                host_path.write_text(
                    json.dumps({"environment": host_environment}, indent=2) + "\n",
                    encoding="utf-8",
                )
            lines.append("  [configure] .opencode/daem0n-host.json")

        # -- Ensure plugin file ------------------------------------------
        plugin_path = opencode_root / "plugins" / "daem0n.ts"
        status = _ensure_file(plugin_path, PLUGIN_TEMPLATE, dry_run, force)
        lines.append(f"  {status} .opencode/plugins/daem0n.ts")

        # -- AGENTS.md status (do NOT create) ----------------------------
        if oc["agents_md"]:
            lines.append("  [exists] AGENTS.md")
        else:
            lines.append("  [skip]   AGENTS.md (create via Phase 18 or manually)")
        lines.append("")

        if installation is not None:
            state = "created" if installation.created else "reused"
            lines.append("Native edit bridge:")
            lines.append(f"  [{state}] host credential: {installation.credential_path}")
            lines.append(
                f"  [configured] {installation.environment().get('DAEM0NMCP_EDIT_BRIDGE_MODE', 'local')} pairing environment"
            )
            lines.append("")

    except OSError as exc:
        return False, f"Installation failed: {exc}"

    # -- Claude Code preservation report ---------------------------------
    if cc["configured"]:
        lines.append("Claude Code preservation:")
        if cc["mcp_json"]:
            lines.append("  .mcp.json -- preserved (not modified)")
        if cc["claude_dir"]:
            lines.append("  .claude/ -- preserved (not modified)")
        lines.append("")

    # -- Summary ---------------------------------------------------------
    if dry_run:
        lines.append("No changes were made. Remove --dry-run to apply.")
    else:
        lines.append("OpenCode integration installed successfully.")
        lines.append("Next: Launch OpenCode in this project directory.")

    return True, "\n".join(lines)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Install OpenCode integration for Daem0n-MCP"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be created without making changes",
    )
    parser.add_argument(
        "--force",
        "-f",
        action="store_true",
        help="Overwrite existing configuration files",
    )
    parser.add_argument(
        "--project-path",
        default=".",
        help="Project root path (default: current directory)",
    )
    parser.add_argument(
        "--interface",
        choices=("v1", "v2"),
        help="Plugin interface (native edit hooks require v1)",
    )
    parser.add_argument("--remote-workspace-id")
    parser.add_argument("--remote-credential-file")
    parser.add_argument("--remote-url")
    parser.add_argument("--remote-ca-file")
    parser.add_argument("--remote-origin")
    args = parser.parse_args()

    ok, msg = install_opencode(
        args.project_path,
        dry_run=args.dry_run,
        force=args.force,
        interface=args.interface,
        remote_workspace_id=args.remote_workspace_id,
        remote_credential_path=args.remote_credential_file,
        remote_base_url=args.remote_url,
        remote_ca_file=args.remote_ca_file,
        remote_origin=args.remote_origin,
    )
    print(msg)
    sys.exit(0 if ok else 1)
