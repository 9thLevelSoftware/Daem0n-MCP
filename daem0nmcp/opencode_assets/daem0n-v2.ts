/** Daem0n native adapter for the pinned OpenCode V2 hook contract. */
import { createHash } from "node:crypto";
import { spawn } from "node:child_process";
import { readFile } from "node:fs/promises";

const PYTHON_EXECUTABLE = "python";
const AUTOMATION_MODE = "remind";
const REMINDER = `Daem0n native V2 reminder: use the installed workspace covenant and supplied opaque workspace_id for every scoped call.
Follow the current task's allowed operations and call order. system_health is optional; source indexing does not require a health check.
It is not a prerequisite for memory or code tools. Use it only for requested diagnostics or to investigate an observed failure within that scope; do not add a startup health probe.
Recall relevant warnings and decisions before edits. Guided memory_recall, memory_preflight, memory_store and memory_record_outcome automatically brief an unbriefed session; diagnostics do not.
The full session_brief is optional in guided mode, not a prerequisite; strict mode keeps its explicit briefing requirements.
On COUNSEL_REQUIRED read error.counsel and retry exactly error.remedy in the same MCP session. Record verified outcomes with stable idempotency keys; indexing or searching alone does not require inventing memory writes or outcomes.
Prefer directly listed typed tools. Use the native tool catalog or daem0n_tools_search for hidden or unlisted tools. Resource discovery is not tool discovery; do not read resource contents merely to find tools.
When the task needs Daem0n resource metadata, call opencode_list_mcp_resources({"server":"daem0nmcp"}). Do not omit the server filter: that enumerates unrelated configured servers, including disabled entries.
Only read resources when the task requires their contents, retaining the discovered server and exact workspace resource URI.`;

const EDIT_TOOLS = new Set(["edit", "write", "patch"]);
const BRIDGE_KEYS = new Set([
  "DAEM0NMCP_EDIT_BRIDGE_CREDENTIAL_FILE", "DAEM0NMCP_EDIT_BRIDGE_MODE",
  "DAEM0NMCP_EDIT_BRIDGE_RUNTIME_DIR", "DAEM0NMCP_EDIT_BRIDGE_REMOTE_URL",
  "DAEM0NMCP_EDIT_BRIDGE_CA_FILE", "DAEM0NMCP_EDIT_BRIDGE_ORIGIN",
  "DAEM0NMCP_EDIT_HOST_WORKSPACE_BINDING_FILE", "DAEM0NMCP_EDIT_HOST_STATE_FILE",
  "DAEM0NMCP_PROJECT_ROOT", "DAEM0NMCP_STORAGE_PATH",
]);

function stable(value: unknown): unknown {
  if (Array.isArray(value)) return value.map(stable);
  if (value && typeof value === "object") {
    return Object.fromEntries(Object.entries(value).sort(([a], [b]) => a.localeCompare(b)).map(([key, item]) => [key, stable(item)]));
  }
  return value;
}

function nativeCallID(event: { id: string; tool: string; input: unknown }): string {
  // Inner Code Mode tools share an outer ID. Also bind their exact tool/input.
  return createHash("sha256").update(JSON.stringify(stable([event.id, event.tool, event.input]))).digest("hex");
}

async function bridgeEnvironment(directory: string): Promise<Record<string, string>> {
  try {
    const configured = JSON.parse(await readFile(`${directory}/.opencode/daem0n-v2-host.json`, "utf8"))?.environment;
    if (!configured || typeof configured !== "object" || Array.isArray(configured)) return {};
    return Object.fromEntries(Object.entries(configured).filter(([key, value]) => BRIDGE_KEYS.has(key) && typeof value === "string")) as Record<string, string>;
  } catch { return {}; }
}

async function runHook(directory: string, module: string, event: object, environment: Record<string, string>, failClosed = false): Promise<{ code: number; error: string }> {
  return await new Promise((resolve) => {
    let finished = false;
    let error = "";
    const complete = (code: number) => {
      if (finished) return;
      finished = true;
      clearTimeout(timer);
      resolve({code: failClosed && code !== 0 && code !== 2 ? 2 : code, error});
    };
    const bootstrap = `import sys; sys.path = [p for p in sys.path if p]; import runpy; runpy.run_module('daem0nmcp.claude_hooks.${module}', run_name='__main__', alter_sys=True)`;
    const child = spawn(PYTHON_EXECUTABLE, ["-E", "-u", "-X", "utf8", "-c", bootstrap], {
      cwd: directory,
      env: {...process.env, ...environment, DAEM0NMCP_AUTOMATION_MODE: AUTOMATION_MODE, CLAUDE_PROJECT_DIR: directory},
      stdio: ["pipe", "ignore", "pipe"], windowsHide: true,
    });
    const timer = setTimeout(() => { child.kill(); error = "EDIT_BRIDGE_UNAVAILABLE: hook timed out"; complete(failClosed ? 2 : 0); }, 10_000);
    child.stderr.on("data", (chunk) => { if (error.length < 16_384) error += String(chunk).slice(0, 16_384 - error.length); });
    child.on("error", () => { error = "EDIT_BRIDGE_UNAVAILABLE: hook could not start"; complete(failClosed ? 2 : 0); });
    child.on("close", (code) => complete(code ?? (failClosed ? 2 : 0)));
    child.stdin.on("error", () => { error = "EDIT_BRIDGE_UNAVAILABLE: hook input unavailable"; child.kill(); complete(failClosed ? 2 : 0); });
    child.stdin.end(JSON.stringify(event));
  });
}

function preflightInput(event: { tool: string; input: Record<string, unknown> }): Record<string, unknown> | undefined {
  if (/^(?:edit_preflight|[A-Za-z0-9_.-]+_edit_preflight)$/.test(event.tool)) return event.input;
  if (event.tool.endsWith("daem0n_tool_call") && event.input.tool === "edit_preflight" && event.input.arguments && typeof event.input.arguments === "object") {
    return {workspace_id: event.input.workspace_id, ...event.input.arguments as object};
  }
  return undefined;
}

export default {
  id: "daem0n-covenant-reminder",
  async setup(ctx) {
    if (AUTOMATION_MODE === "off") return;
    const directory = ctx.location.directory;
    const environment = await bridgeEnvironment(directory);
    await ctx.session.hook("context", (event) => {
      event.system.push({ type: "text", text: REMINDER });
    });
    if (AUTOMATION_MODE !== "enforce") return;
    await ctx.tool.hook("execute.before", async (event) => {
      if (!EDIT_TOOLS.has(event.tool)) return;
      const response = await runHook(directory, "pre_edit", {
        session_id: event.sessionID, cwd: directory, hook_event_name: "PreToolUse",
        tool_name: event.tool, tool_input: event.input, tool_use_id: nativeCallID(event),
      }, environment, true);
      if (response.code !== 0) throw new Error(response.error || "EDIT_BRIDGE_UNAVAILABLE");
    });
    await ctx.tool.hook("execute.after", async (event) => {
      const actualInput = preflightInput(event);
      if (actualInput && event.status === "completed") {
        await runHook(directory, "post_edit_preflight", {
          session_id: event.sessionID, cwd: directory, tool_name: "edit_preflight",
          tool_input: actualInput, tool_response: {structuredContent: event.result.output},
          tool_use_id: nativeCallID(event), hook_event_name: "PostToolUse",
        }, environment);
      } else if (EDIT_TOOLS.has(event.tool)) {
        const succeeded = event.status === "completed" && event.result?.output?.isError !== true;
        await runHook(directory, "post_edit", {
          session_id: event.sessionID, cwd: directory, tool_name: event.tool,
          tool_input: event.input, tool_use_id: nativeCallID(event),
          tool_response: succeeded ? {success: true} : {success: false},
          hook_event_name: succeeded ? "PostToolUse" : "PostToolUseFailure",
          execution_status: succeeded ? "succeeded" : "failed",
        }, environment);
      }
    });
  },
};
