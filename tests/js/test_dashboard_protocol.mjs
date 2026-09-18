import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import test from "node:test";
import vm from "node:vm";
import { fileURLToPath } from "node:url";

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../..");

class Element {
  constructor(tag, document) { this.tagName = tag; this.ownerDocument = document; this.children = []; this.attributes = new Map(); this.listeners = new Map(); this.textContent = ""; this.className = ""; this.hidden = false; }
  append(...children) { this.children.push(...children); }
  replaceChildren(...children) { this.children = children; }
  addEventListener(name, listener) { this.listeners.set(name, listener); }
  get classList() { return { add() {}, remove() {} }; }
  dispatch(name) { this.listeners.get(name)?.({ preventDefault() {} }); }
  setAttribute(name, value) { this.attributes.set(name, String(value)); }
  getAttribute(name) { return this.attributes.get(name) ?? null; }
}

function makeDocument(appId, payload = {}) {
  const document = {
    documentElement: new Element("html", null),
    data: new Element("script", null),
    mount: new Element("main", null),
    createElement(tag) { return new Element(tag, document); },
    createElementNS(_ns, tag) { return new Element(tag, document); },
    getElementById(id) { return id === "app" ? document.mount : null; },
    querySelectorAll(selector) { return selector === "#app-data" ? [document.data] : []; },
  };
  document.documentElement.ownerDocument = document;
  document.documentElement.setAttribute("data-daem0n-app", appId);
  document.data.textContent = JSON.stringify(payload);
  return document;
}

function descendants(node) {
  return [node, ...node.children.flatMap(descendants)];
}

function makeView(appId, parentOrigin = "null", protocolVersion = "2026-01-26") {
  const listeners = new Map();
  const outbound = [];
  const document = makeDocument(appId);
  document.referrer = parentOrigin === "null" ? "" : parentOrigin + "/sandbox-proxy";
  const parent = {
    postMessage(message) {
      outbound.push(message);
      if (message.method === "ui/initialize") {
        listeners.get("message")?.({
          source: parent,
          origin: parentOrigin,
          data: { jsonrpc: "2.0", id: message.id, result: { protocolVersion, hostCapabilities: { serverTools: {} } } },
        });
      } else if (message.method === "tools/call") {
        listeners.get("message")?.({
          source: parent,
          origin: parentOrigin,
          data: {
            jsonrpc: "2.0",
            id: message.id,
            result: { structuredContent: { ok: true, data: { items: [] } } },
          },
        });
      }
    },
  };
  const context = vm.createContext({ console, document, URL, TextEncoder, setTimeout, clearTimeout });
  context.window = context;
  context.parent = parent;
  context.addEventListener = (name, listener) => listeners.set(name, listener);
  context.removeEventListener = name => listeners.delete(name);
  for (const asset of ["daem0nmcp/ui/static/messenger.js", "daem0nmcp/ui/static/runtime.js"]) {
    vm.runInContext(fs.readFileSync(path.join(root, asset), "utf8"), context, { filename: asset });
  }
  return {
    context, document, outbound,
    deliver(data, source = parent, origin = parentOrigin) { listeners.get("message")?.({ source, origin, data }); },
  };
}

function registerCapture(view, appId, captured) {
  view.context.Daem0nUI.register(appId, value => value, value => captured.push(value));
}

test("briefing uses canonical category counts and preserves Git statuses", () => {
  const view = makeView("briefing");
  const captured = [];
  registerCapture(view, "briefing", captured);
  view.context.Daem0nUI.applyToolResult({ structuredContent: { ok: true, data: {
    workspace_statistics: { records: 9, decisions: 2, warnings: 1, patterns: 3, learnings: 3, successful_outcomes: 3, failed_outcomes: 1 },
    git_changes: [{ relative_file_path: "new.py", status: "added" }, { relative_file_path: "old.py", status: "deleted" }],
  } } });
  assert.deepEqual(JSON.parse(JSON.stringify(captured.at(-1).statistics)), {
    total_memories: 9, by_category: { decision: 2, warning: 1, pattern: 3, learning: 3 }, outcome_rates: { success_rate: 0.75 },
  });
  assert.deepEqual(JSON.parse(JSON.stringify(captured.at(-1).git_changes.files)), [{ path: "new.py", status: "A" }, { path: "old.py", status: "D" }]);
  view.context.Daem0nUI.applyToolResult({ structuredContent: { ok: true, data: { workspace_statistics: { records: 1 } } } });
  assert.equal(captured.at(-1).statistics.outcome_rates.success_rate, null);
});

test("MCP Apps handshake routes a v7 tool result and refreshes with its exact read arguments", async () => {
  const view = makeView("search");
  const captured = [];
  registerCapture(view, "search", captured);
  await new Promise(setImmediate);
  assert.deepEqual(JSON.parse(JSON.stringify(view.outbound.map(message => message.method))), ["ui/initialize", "ui/notifications/initialized"]);

  view.deliver({ jsonrpc: "2.0", method: "ui/notifications/tool-input", params: { arguments: { workspace_id: "ws_1", query: "exact query" } } });
  view.deliver({ jsonrpc: "2.0", method: "ui/notifications/tool-result", params: { structuredContent: { ok: true, data: { items: [{ record: { record_id: "rec_1", excerpt: "real v7 decision", record_type: "decision", tags: ["x"], created_at: "2026-01-01T00:00:00Z" } }] } } } });
  await new Promise(setImmediate);
  assert.equal(captured.at(-1).decisions[0].content, "real v7 decision");

  const refresh = view.context.Daem0nUI.refreshButton();
  descendants(refresh).find(node => node.textContent === "Refresh").dispatch("click");
  const call = view.outbound.at(-1);
  assert.equal(call.method, "tools/call");
  assert.deepEqual(JSON.parse(JSON.stringify(call.params)), { name: "memory_recall", arguments: { workspace_id: "ws_1", query: "exact query" } });
});

test("v7 graph edges survive projection and Covenant briefing never becomes a mutation grant", async () => {
  const graph = makeView("graph");
  const graphCaptured = [];
  registerCapture(graph, "graph", graphCaptured);
  await new Promise(setImmediate);
  graph.context.Daem0nUI.applyToolResult({ structuredContent: { ok: true, data: {
    nodes: [{ record: { record_id: "rec_a", excerpt: "A", record_type: "decision", tags: [], created_at: "2026-01-01T00:00:00Z" } }, { record: { record_id: "rec_b", excerpt: "B", record_type: "learning", tags: [], created_at: "2026-01-01T00:00:00Z" } }],
    edges: [{ relationship: { source_record_id: "rec_a", target_record_id: "rec_b", relationship_type: "led_to", confidence: 0.8, description: "real edge" } }],
  } } });
  assert.equal(graphCaptured.at(-1).edges[0].relationship, "led_to");

  const covenant = makeView("covenant");
  const covenantCaptured = [];
  registerCapture(covenant, "covenant", covenantCaptured);
  await new Promise(setImmediate);
  covenant.context.Daem0nUI.applyToolResult({ structuredContent: { ok: true, data: { briefed: true, next_step: null } } });
  assert.deepEqual(JSON.parse(JSON.stringify(covenantCaptured.at(-1).preflight)), { status: "none" });
  assert.equal(covenantCaptured.at(-1).can_mutate, false);
});

test("the bridge rejects a null-origin non-parent sender and payloads above its bound", async () => {
  const view = makeView("search");
  const captured = [];
  registerCapture(view, "search", captured);
  await new Promise(setImmediate);
  const result = { jsonrpc: "2.0", method: "ui/notifications/tool-result", params: { result: { structuredContent: { ok: true, data: { items: [] } } } } };
  view.deliver(result, { postMessage() {} }, "null");
  assert.equal(captured.length, 1, "only initial shell render is allowed");
  view.deliver({ ...result, padding: "x".repeat(1_048_577) });
  assert.equal(captured.length, 1);
  view.deliver({ ...result, padding: "\u20ac".repeat(400_000) });
  assert.equal(captured.length, 1, "the bound counts UTF-8 bytes, not characters");
});

test("browser referrer pins a real sandbox proxy origin and rejects other origins", async () => {
  const view = makeView("search", "http://localhost:6274");
  const captured = [];
  registerCapture(view, "search", captured);
  await new Promise(setImmediate);
  assert.equal(view.outbound.at(-1).method, "ui/notifications/initialized");
  const result = { jsonrpc: "2.0", method: "ui/notifications/tool-result", params: { structuredContent: { ok: true, data: { items: [] } } } };
  view.deliver(result);
  await new Promise(setImmediate);
  assert.equal(captured.length, 2);
  view.deliver(result, undefined, "https://claude.ai");
  view.deliver(result, undefined, "http://localhost:6275");
  view.deliver(result, undefined, "null");
  await new Promise(setImmediate);
  assert.equal(captured.length, 2, "referrer binding must override the fallback allowlist");
});

test("new dashboard actions replace previous filters and cross-tool results preserve the briefing", async () => {
  const community = makeView("community");
  registerCapture(community, "community", []);
  community.deliver({ jsonrpc: "2.0", method: "ui/notifications/tool-input", params: { arguments: { workspace_id: "ws_1", parent_community_id: "old", cursor: "old_cursor" } } });
  await new Promise(setImmediate);
  await community.context.Daem0nUI.callTool("community_list", {});
  assert.deepEqual(JSON.parse(JSON.stringify(community.outbound.at(-1).params.arguments)), { workspace_id: "ws_1" });

  const briefing = makeView("briefing");
  const captured = [];
  registerCapture(briefing, "briefing", captured);
  briefing.context.Daem0nUI.applyToolResult({ toolName: "session_brief", structuredContent: { ok: true, data: { workspace_statistics: { records: 3 } } } });
  const count = captured.length;
  briefing.context.Daem0nUI.applyToolResult({ toolName: "covenant_status", structuredContent: { ok: true, data: { briefed: true } } });
  assert.equal(captured.length, count, "a status response must not replace the briefing with empty data");
  assert.ok(descendants(briefing.document.mount).some(node => node.textContent.startsWith("Scoped briefing is recorded")));
});

test("an incompatible host version never completes initialization", async () => {
  const view = makeView("search", "null", "unsupported");
  registerCapture(view, "search", []);
  await new Promise(setImmediate);
  assert.equal(view.outbound.length, 1);
  assert.equal(view.context.SecureMessenger.connected, false);
});
