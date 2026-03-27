"""Installer for OpenCode integration.

Creates .opencode/ directory structure, ensures opencode.json exists
at project root, and writes the TypeScript covenant enforcement plugin
for MCP server connectivity and hook discipline.
"""

import argparse
import json
import shutil
import sys
from pathlib import Path
from typing import Any

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

# TypeScript plugin installed into .opencode/plugins/daem0n.ts
# This is the canonical source -- kept in sync with the repo copy.
PLUGIN_TEMPLATE: str = r"""/**
 * Daem0n Covenant Enforcement Plugin for OpenCode
 *
 * Mirrors the 5-hook discipline from Claude Code's hooks system:
 *   1. System prompt injection (covenant rules in every LLM call)
 *   2. Pre-edit enforcement (preflight token required)
 *   3. Pre-bash enforcement (must_not rule checking)
 *   4. Post-edit suggestions (informational, never blocks)
 *   5. Session lifecycle events (best-effort, never blocks)
 *
 * All enforcement logic lives in Python hook modules.
 * This TypeScript file is ONLY a shell-out wrapper -- zero duplication.
 */

import type { Plugin } from "@opencode-ai/plugin";

// ---------------------------------------------------------------------------
// Covenant rules injected into every system prompt
// ---------------------------------------------------------------------------

const COVENANT_RULES_FULL = `<daem0n-covenant>
## The Daem0n's Covenant (Sacred Protocol)

This project is bound to Daem0n for persistent AI memory. When daem0nmcp tools
are available, follow this protocol. Memory without discipline is noise.

### 1. SESSION START (Non-Negotiable)
IMMEDIATELY call: daem0nmcp_commune(action="briefing")
Do NOT skip. Do NOT ask the user first. The briefing loads past decisions,
warnings, failed approaches, patterns, and git changes.

### 2. BEFORE ANY CODE CHANGES
Call: daem0nmcp_consult(action="preflight", description="what you plan to do")
For specific files: daem0nmcp_consult(action="recall_file", file_path="...")
Respect warnings, failed approaches, and must_not constraints.

### 3. AFTER MAKING DECISIONS
Call: daem0nmcp_inscribe(action="remember", category="decision|pattern|warning|learning", content="...", rationale="...", file_path="...", tags=[...])
Save the memory ID for the outcome step.

### 4. AFTER IMPLEMENTATION
Call: daem0nmcp_reflect(action="outcome", memory_id=<id>, outcome_text="...", worked=true|false)
Failures are valuable -- record worked=false with explanation.

### Enforcement
- Skip briefing: ALL tools return COMMUNION_REQUIRED with remedy
- Skip preflight: Mutating tools return COUNSEL_REQUIRED with remedy
- Each block includes the exact tool call to fix it
</daem0n-covenant>`;

const COVENANT_RULES_SIMPLIFIED = `<daem0n-covenant mode="simplified">
## Memory Protocol (Required Steps)

This project uses Daem0n for persistent AI memory. Follow these 4 steps:

1. START: Call daem0nmcp_commune(action="briefing") first
2. BEFORE CHANGES: Call daem0nmcp_consult(action="preflight", description="<plan>")
3. SAVE DECISIONS: Call daem0nmcp_inscribe(action="remember", category="decision", content="<what>", rationale="<why>", tags=["tag1","tag2"])
4. RECORD RESULT: Call daem0nmcp_reflect(action="outcome", memory_id=<id>, outcome_text="<result>", worked=true|false)

Rules:
- Step 1 is required before any other steps
- Step 2 is required before editing files
- Step 3 requires: content (what happened), rationale (why it matters), tags (at least one)
- Step 4 requires the memory_id returned from step 3
</daem0n-covenant>`;

// Module-level state tracking current LLM model (safe: one model per plugin session)
let currentModel: { providerID: string; modelID: string } | null = null;

// ---------------------------------------------------------------------------
// Shell-out helper
// ---------------------------------------------------------------------------

type HookResult = { exitCode: number; stdout: string; stderr: string };

/**
 * Run a Python hook module via BunShell. Returns a normalized result.
 * On ANY failure (Python missing, timeout, crash), returns exitCode 0
 * so the host IDE is never broken by hook infrastructure.
 */
async function runHook(
  $: Parameters<Plugin>[0]["$"],
  directory: string,
  module: string,
  env: Record<string, string>,
  _timeoutMs?: number,
): Promise<HookResult> {
  try {
    const hookEnv: Record<string, string> = {
      CLAUDE_PROJECT_DIR: directory,
      PYTHONUNBUFFERED: "1",
      PYTHONIOENCODING: "utf-8",
      ...env,
    };
    const shell = $.nothrow().env(hookEnv);
    // BunShell template literals require static strings for the command.
    // Build the full module path as a variable and interpolate it.
    const mod = `daem0nmcp.claude_hooks.${module}`;
    const result = await shell`python -m ${mod}`.quiet();
    return {
      exitCode: result.exitCode,
      stdout: result.stdout.toString().trim(),
      stderr: result.stderr.toString().trim(),
    };
  } catch {
    // Graceful degradation: Python not found, timeout, or any other error.
    // Never crash the host IDE.
    return { exitCode: 0, stdout: "", stderr: "" };
  }
}

// ---------------------------------------------------------------------------
// Tool name classification helpers
// ---------------------------------------------------------------------------

function isEditTool(tool: string): boolean {
  const t = tool.toLowerCase();
  return t.includes("edit") || t.includes("write") || t.includes("notebookedit");
}

function isBashTool(tool: string): boolean {
  const t = tool.toLowerCase();
  return t.includes("bash") || t.includes("shell");
}

// ---------------------------------------------------------------------------
// Plugin export
// ---------------------------------------------------------------------------

export const Daem0nPlugin: Plugin = async ({ $, directory }) => {
  return {
    // -----------------------------------------------------------------------
    // HOOK 1: System prompt injection
    // Every LLM call sees the covenant rules.
    // -----------------------------------------------------------------------
    "experimental.chat.system.transform": async (input, output) => {
      const provider = input.model?.providerID ?? "unknown";
      const modelId = input.model?.id ?? "unknown";
      const isClaude = provider === "anthropic" || modelId.toLowerCase().includes("claude");

      output.system.push(isClaude ? COVENANT_RULES_FULL : COVENANT_RULES_SIMPLIFIED);

      // Track model for _client_meta injection in tool calls
      currentModel = { providerID: provider, modelID: modelId };
    },

    // -----------------------------------------------------------------------
    // HOOK 2: Pre-tool enforcement (pre-edit + pre-bash)
    // Blocks edits without preflight token (exit 2 from Python).
    // Blocks bash commands matching must_not rules (exit 2 from Python).
    // -----------------------------------------------------------------------
    "tool.execute.before": async (input, output) => {
      // Inject client metadata for daem0nmcp tools (server-side provenance tracking)
      const toolLower = input.tool?.toLowerCase() ?? "";
      if (toolLower.startsWith("daem0nmcp_") || toolLower.includes("mcp__daem0nmcp__")) {
        if (currentModel && output.args) {
          output.args._client_meta = JSON.stringify({
            client: "opencode",
            providerID: currentModel.providerID,
            modelID: currentModel.modelID,
          });
        }
      }

      if (isEditTool(input.tool)) {
        const result = await runHook($, directory, "pre_edit", {
          TOOL_INPUT: JSON.stringify(output.args ?? {}),
        });
        if (result.exitCode === 2) {
          throw new Error(
            result.stderr || result.stdout || "[Daem0n blocks] Preflight required",
          );
        }
      }

      if (isBashTool(input.tool)) {
        const result = await runHook($, directory, "pre_bash", {
          TOOL_INPUT: JSON.stringify(output.args ?? {}),
        });
        if (result.exitCode === 2) {
          throw new Error(
            result.stderr || result.stdout || "[Daem0n blocks] Rule violation",
          );
        }
      }
    },

    // -----------------------------------------------------------------------
    // HOOK 3: Post-edit suggestions (informational, never blocks)
    // Suggests remembering significant changes via inscribe().
    // -----------------------------------------------------------------------
    "tool.execute.after": async (input, output) => {
      try {
        if (isEditTool(input.tool)) {
          const result = await runHook(
            $,
            directory,
            "post_edit",
            { TOOL_INPUT: JSON.stringify({}) },
            5000,
          );
          if (result.stdout) {
            output.output = (output.output || "") + "\n" + result.stdout;
          }
        }
      } catch {
        // Never throw from post-edit. Informational only.
      }
    },

    // -----------------------------------------------------------------------
    // HOOK 4: Session lifecycle events (best-effort, never blocks)
    // session.created  -> session_start hook (auto-briefing)
    // session.idle     -> stop hook (auto-capture decisions)
    // -----------------------------------------------------------------------
    event: async ({ event }) => {
      try {
        if (event.type === "session.created") {
          currentModel = null;
          await runHook($, directory, "session_start", {}, 5000);
        } else if (event.type === "session.idle") {
          await runHook(
            $,
            directory,
            "stop",
            { CLAUDE_TRANSCRIPT_PATH: "" },
            15000,
          );
        }
      } catch {
        // Never throw from event hooks. Best-effort only.
      }
    },
  };
};
"""


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


def install_opencode(
    project_path: str,
    dry_run: bool = False,
    force: bool = False,
) -> tuple[bool, str]:
    """Install OpenCode integration for a project.

    Creates ``.opencode/`` directory structure and ensures ``opencode.json``
    exists at the project root.

    Returns ``(success, message)`` tuple.
    """
    root = Path(project_path).resolve()
    lines: list[str] = []

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
        json_content = json.dumps(OPENCODE_JSON_TEMPLATE, indent=2) + "\n"
        json_path = root / "opencode.json"
        status = _ensure_file(json_path, json_content, dry_run, force)
        lines.append(f"  {status} opencode.json")

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
    args = parser.parse_args()

    ok, msg = install_opencode(
        args.project_path,
        dry_run=args.dry_run,
        force=args.force,
    )
    print(msg)
    sys.exit(0 if ok else 1)
