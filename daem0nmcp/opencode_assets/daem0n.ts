/**
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
## The Daem0n v7 Covenant

This project is bound to Daem0n for persistent AI memory. When daem0nmcp tools
are available, use the exact workspace-scoped v7 tools. The core names are
session_brief, memory_preflight, memory_recall, memory_store,
memory_record_outcome, and system_health.

### 1. SESSION START (Non-Negotiable)
IMMEDIATELY call:
daem0nmcp_session_brief(workspace_id="<workspace_id>")

Use daem0nmcp_memory_recall(workspace_id="<workspace_id>", query="...", limit=10)
for relevant history. Before a protected operation call:
daem0nmcp_memory_preflight(workspace_id="<workspace_id>", target_tool="<exact-tool>", target_arguments={<exact arguments>})
Respect warnings, failed approaches, and must_not constraints. A preflight token
is valid only for the exact workspace, principal, session, tool, and arguments.

### 3. AFTER MAKING DECISIONS
Call daem0nmcp_memory_store with the same target arguments, a stable
idempotency_key, and the returned preflight_token. Save its record_id.

### 4. AFTER IMPLEMENTATION
Call: daem0nmcp_memory_record_outcome(workspace_id="<workspace_id>", record_id="<mem_id>", outcome_text="...", worked=true|false, idempotency_key="<stable-key>")
Failures are valuable. Record worked=false with an explanation.

Use daem0nmcp_system_health(workspace_id="<workspace_id>") for diagnostics.
Read-only resources use memory://workspaces/{workspace_id}/warnings, /failures,
/rules, and /active-context. Supported transports are stdio and Streamable HTTP
at /mcp. Migration mapping: docs/v6-to-v7-tools.json.
</daem0n-covenant>`;

const COVENANT_RULES_SIMPLIFIED = `<daem0n-covenant mode="simplified">
## Memory Protocol (Required Steps)

This project uses Daem0n for persistent AI memory. Follow these 4 steps:

1. START: daem0nmcp_session_brief(workspace_id="<workspace_id>")
2. RECALL: daem0nmcp_memory_recall(workspace_id="<workspace_id>", query="...", limit=10)
3. PREFLIGHT: daem0nmcp_memory_preflight(workspace_id="<workspace_id>", target_tool="memory_store", target_arguments={<exact arguments>})
4. STORE: daem0nmcp_memory_store(workspace_id="<workspace_id>", record_type="decision", content="...", idempotency_key="<stable-key>", preflight_token="<token>")
5. OUTCOME: daem0nmcp_memory_record_outcome(workspace_id="<workspace_id>", record_id="<mem_id>", outcome_text="...", worked=true|false, idempotency_key="<stable-key>")

Rules:
- Never use paths as workspace selectors.
- Reuse an idempotency key when retrying the same write.
- Use daem0nmcp_system_health for diagnostics.
- Exact host-prefixed names are accepted; lookalike substrings are not.
- Migration mapping: docs/v6-to-v7-tools.json.
</daem0n-covenant>`;

// ---------------------------------------------------------------------------
// Shell-out helper
// ---------------------------------------------------------------------------

type HookResult = { exitCode: number; stdout: string; stderr: string };

const BRIDGE_ENV_KEYS = new Set([
  "DAEM0NMCP_EDIT_BRIDGE_CREDENTIAL_FILE",
  "DAEM0NMCP_EDIT_BRIDGE_MODE",
  "DAEM0NMCP_EDIT_BRIDGE_RUNTIME_DIR",
  "DAEM0NMCP_EDIT_BRIDGE_REMOTE_URL",
  "DAEM0NMCP_EDIT_BRIDGE_CA_FILE",
  "DAEM0NMCP_EDIT_BRIDGE_ORIGIN",
  "DAEM0NMCP_EDIT_HOST_WORKSPACE_BINDING_FILE",
  "DAEM0NMCP_EDIT_HOST_STATE_FILE",
  "DAEM0NMCP_PROJECT_ROOT",
  "DAEM0NMCP_STORAGE_PATH",
  "DAEM0NMCP_PYTHON_EXECUTABLE",
]);

async function loadBridgeEnvironment(directory: string): Promise<Record<string, string>> {
  try {
    let configured: unknown;
    const hostFile = Bun.file(`${directory}/.opencode/daem0n-host.json`);
    if (await hostFile.exists()) {
      configured = (await hostFile.json())?.environment;
    } else {
      const config = await Bun.file(`${directory}/opencode.json`).json();
      configured = config?.mcp?.daem0nmcp?.environment;
    }
    if (!configured || typeof configured !== "object" || Array.isArray(configured)) return {};
    return Object.fromEntries(
      Object.entries(configured).filter(
        (entry): entry is [string, string] =>
          BRIDGE_ENV_KEYS.has(entry[0]) && typeof entry[1] === "string",
      ),
    );
  } catch {
    return {};
  }
}

/**
 * Run a Python hook module via BunShell. Returns a normalized result.
 * On ANY failure (Python missing, timeout, crash), returns exitCode 0
 * so the host IDE is never broken by hook infrastructure.
 */
async function runHook(
  directory: string,
  module: string,
  event: object,
  bridgeEnvironment: Record<string, string>,
  timeoutMs = 5_000,
  failClosed = false,
): Promise<HookResult> {
  let child: ReturnType<typeof Bun.spawn> | undefined;
  let timer: ReturnType<typeof setTimeout> | undefined;
  let timedOut = false;
  try {
    const mod = `daem0nmcp.claude_hooks.${module}`;
    const python = bridgeEnvironment.DAEM0NMCP_PYTHON_EXECUTABLE || "python";
    child = Bun.spawn([python, "-m", mod], {
      cwd: directory,
      env: {
        ...process.env,
        ...bridgeEnvironment,
        CLAUDE_PROJECT_DIR: directory,
        PYTHONUNBUFFERED: "1",
        PYTHONIOENCODING: "utf-8",
      },
      stdin: "pipe",
      stdout: "pipe",
      stderr: "pipe",
    });
    child.stdin.write(JSON.stringify(event));
    child.stdin.end();
    timer = setTimeout(() => {
      timedOut = true;
      child?.kill();
    }, timeoutMs);
    const [exitCode, stdout, stderr] = await Promise.all([
      child.exited,
      new Response(child.stdout).text(),
      new Response(child.stderr).text(),
    ]);
    if (failClosed && (timedOut || (exitCode !== 0 && exitCode !== 2))) {
      return { exitCode: 2, stdout: "", stderr: "EDIT_BRIDGE_UNAVAILABLE" };
    }
    return {
      exitCode,
      stdout: stdout.trim(),
      stderr: stderr.trim(),
    };
  } catch {
    return failClosed
      ? { exitCode: 2, stdout: "", stderr: "EDIT_BRIDGE_UNAVAILABLE" }
      : { exitCode: 0, stdout: "", stderr: "" };
  } finally {
    if (timer) clearTimeout(timer);
  }
}

// ---------------------------------------------------------------------------
// Tool name classification helpers
// ---------------------------------------------------------------------------

function isEditTool(tool: string): boolean {
  return new Set(["edit", "write", "notebookedit", "apply_patch"]).has(tool.toLowerCase());
}

function isBashTool(tool: string): boolean {
  return new Set(["bash", "shell"]).has(tool.toLowerCase());
}

function isEditPreflightTool(tool: string): boolean {
  return (
    tool === "edit_preflight" ||
    /^mcp__[^\s]+__edit_preflight$/.test(tool) ||
    /^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}_edit_preflight$/.test(tool)
  );
}

// ---------------------------------------------------------------------------
// Plugin export
// ---------------------------------------------------------------------------

export const Daem0nPlugin: Plugin = async ({ directory }) => {
  const bridgeEnvironment = await loadBridgeEnvironment(directory);
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

    },

    // -----------------------------------------------------------------------
    // HOOK 2: Pre-tool enforcement (pre-edit + pre-bash)
    // Blocks edits without preflight token (exit 2 from Python).
    // Blocks bash commands matching must_not rules (exit 2 from Python).
    // -----------------------------------------------------------------------
    "tool.execute.before": async (input, output) => {
      if (isEditTool(input.tool)) {
        const result = await runHook(
          directory,
          "pre_edit",
          {
            session_id: input.sessionID,
            cwd: directory,
            hook_event_name: "PreToolUse",
            tool_name: input.tool,
            tool_input: output.args ?? {},
            tool_use_id: input.callID,
          },
          bridgeEnvironment,
          5_000,
          true,
        );
        if (result.exitCode === 2) {
          throw new Error(
            result.stderr || result.stdout || "[Daem0n blocks] Preflight required",
          );
        }
      }

      if (isBashTool(input.tool)) {
        const result = await runHook(directory, "pre_bash", {
          session_id: input.sessionID,
          cwd: directory,
          hook_event_name: "PreToolUse",
          tool_name: input.tool,
          tool_input: output.args ?? {},
          tool_use_id: input.callID,
        }, bridgeEnvironment);
        if (result.exitCode === 2) {
          throw new Error(
            result.stderr || result.stdout || "[Daem0n blocks] Rule violation",
          );
        }
      }
    },

    // -----------------------------------------------------------------------
    // HOOK 3: Post-edit suggestions (informational, never blocks)
    // Suggests replay-safe v7 memory calls for significant changes.
    // -----------------------------------------------------------------------
    "tool.execute.after": async (input, output) => {
      try {
        if (isEditPreflightTool(input.tool)) {
          // OpenCode 1.18.21 passes the raw MCP CallToolResult to this hook.
          // Forward that host-observed object; never reconstruct it from text.
          await runHook(directory, "post_edit_preflight", {
            session_id: input.sessionID,
            cwd: directory,
            hook_event_name: "PostToolUse",
            tool_name: input.tool,
            tool_input: input.args ?? {},
            tool_response: output,
            tool_use_id: input.callID,
          }, bridgeEnvironment);
        } else if (isEditTool(input.tool)) {
          const result = await runHook(directory, "post_edit", {
            session_id: input.sessionID,
            cwd: directory,
            hook_event_name: "PostToolUse",
            tool_name: input.tool,
            tool_input: input.args ?? {},
            tool_response: output,
            tool_use_id: input.callID,
          }, bridgeEnvironment);
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
    // session.idle     -> stop hook (fail-closed memory suggestions)
    // -----------------------------------------------------------------------
    event: async ({ event }) => {
      try {
        if (event.type === "session.created") {
          await runHook(directory, "session_start", {
            session_id: event.properties?.sessionID,
            cwd: directory,
            hook_event_name: "SessionStart",
          }, bridgeEnvironment);
        } else if (event.type === "session.idle") {
          await runHook(directory, "stop", {
            session_id: event.properties?.sessionID,
            cwd: directory,
            hook_event_name: "Stop",
          }, bridgeEnvironment, 15_000);
        }
      } catch {
        // Never throw from event hooks. Best-effort only.
      }
    },
  };
};
