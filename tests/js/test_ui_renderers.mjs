import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import test from "node:test";
import vm from "node:vm";
import { fileURLToPath } from "node:url";

class FakeCanvasContext {
  constructor() {
    this.operations = [];
  }

  arc(x, y, radius) { this.operations.push(["arc", x, y, radius]); }
  beginPath() {}
  clearRect() { this.operations.push(["clear"]); }
  closePath() {}
  fill() { this.operations.push(["fill", this.fillStyle]); }
  lineTo(x, y) { this.operations.push(["line", x, y]); }
  moveTo(x, y) { this.operations.push(["move", x, y]); }
  restore() { this.operations.push(["restore"]); }
  save() { this.operations.push(["save"]); }
  scale(x, y) { this.operations.push(["scale", x, y]); }
  setLineDash(value) { this.operations.push(["dash", ...value]); }
  setTransform(a, b, c, d, e, f) { this.operations.push(["transform", a, b, c, d, e, f]); }
  stroke() { this.operations.push(["stroke", this.strokeStyle, this.lineWidth]); }
  translate(x, y) { this.operations.push(["translate", x, y]); }
}

class FakeElement {
  constructor(tagName, ownerDocument) {
    this.tagName = tagName;
    this.ownerDocument = ownerDocument;
    this.children = [];
    this.attributes = new Map();
    this.listeners = new Map();
    this.className = "";
    this.hidden = false;
    this.textContent = "";
    this.parentNode = null;
  }

  get classList() {
    const node = this;
    const names = () => node.className.split(/\s+/).filter(Boolean);
    return {
      add(name) { if (!names().includes(name)) node.className = [...names(), name].join(" "); },
      contains(name) { return names().includes(name); },
      remove(name) { node.className = names().filter(value => value !== name).join(" "); },
    };
  }

  get firstChild() { return this.children[0] || null; }

  addEventListener(name, listener) {
    if (!this.listeners.has(name)) this.listeners.set(name, []);
    this.listeners.get(name).push(listener);
  }

  append(...children) {
    for (const child of children) {
      child.parentNode = this;
      this.children.push(child);
    }
  }

  dispatch(name, values = {}) {
    const event = {
      button: 0,
      currentTarget: this,
      pointerId: 1,
      preventDefault() {},
      ...values,
    };
    for (const listener of this.listeners.get(name) || []) listener(event);
  }

  getAttribute(name) { return this.attributes.get(name) ?? null; }
  getBoundingClientRect() {
    if (this.classList.contains("graph-frame")) return { ...this.ownerDocument.frameRect };
    if (this.classList.contains("graph-canvas")) return { ...this.ownerDocument.canvasRect };
    return { left: 0, top: 0, width: 0, height: 0 };
  }
  getContext(kind) { return this.tagName === "canvas" && kind === "2d" ? this.ownerDocument.canvasContext : null; }
  prepend(...children) {
    for (const child of children.reverse()) {
      child.parentNode = this;
      this.children.unshift(child);
    }
  }
  querySelectorAll(selector) {
    return findAll(this, node => selector.startsWith(".") && node.classList.contains(selector.slice(1)));
  }
  releasePointerCapture() {}
  replaceChildren(...children) {
    this.children = [];
    this.append(...children);
  }
  setAttribute(name, value) { this.attributes.set(name, String(value)); }
  setPointerCapture() {}
}

class FakeDocument {
  constructor(appId, payload, options = {}) {
    this.canvasContext = new FakeCanvasContext();
    this.frameRect = { left: 0, top: 0, width: 1000, height: 500, ...(options.frameRect || {}) };
    this.canvasRect = { left: 0, top: 0, width: 500, height: 250, ...(options.canvasRect || {}) };
    this.documentElement = new FakeElement("html", this);
    this.documentElement.setAttribute("data-daem0n-app", appId);
    this.data = new FakeElement("script", this);
    this.data.textContent = JSON.stringify(payload);
    this.mount = new FakeElement("main", this);
  }

  createElement(tagName) { return new FakeElement(tagName, this); }
  createElementNS(_namespace, tagName) { return new FakeElement(tagName, this); }
  createTextNode(value) {
    const node = new FakeElement("#text", this);
    node.textContent = String(value);
    return node;
  }
  getElementById(id) { return id === "app" ? this.mount : null; }
  querySelectorAll(selector) { return selector === "#app-data" ? [this.data] : []; }
}

function findAll(rootNode, predicate) {
  const matches = [];
  const visit = node => {
    if (predicate(node)) matches.push(node);
    for (const child of node.children || []) visit(child);
  };
  visit(rootNode);
  return matches;
}

function findByClass(rootNode, className) {
  return findAll(rootNode, node => node.classList && node.classList.contains(className))[0] || null;
}

function findByText(rootNode, value) {
  return findAll(rootNode, node => node.textContent === value)[0] || null;
}

function fakeD3(trace) {
  function position(nodes) {
    nodes.forEach((node, index) => {
      if (!Number.isFinite(node.x)) node.x = nodes.length === 1 ? 400 : 40 + (index % 20) * 30;
      if (!Number.isFinite(node.y)) node.y = nodes.length === 1 ? 250 : 40 + Math.floor(index / 20) * 30;
    });
  }

  return {
    forceCenter(x, y) { return { x, y }; },
    forceCollide(radius) { return { radius }; },
    forceManyBody() {
      return { strength() { return this; } };
    },
    forceLink(initialLinks) {
      return {
        current: initialLinks,
        distance() { return this; },
        id() { return this; },
        links(nextLinks) {
          if (nextLinks === undefined) return this.current;
          this.current = nextLinks;
          trace.linkCounts.push(nextLinks.length);
          return this;
        },
      };
    },
    forceSimulation(initialNodes) {
      const forces = new Map();
      position(initialNodes);
      trace.nodeCounts.push(initialNodes.length);
      const simulation = {
        currentNodes: initialNodes,
        alpha() { return this; },
        alphaTarget() { return this; },
        force(name, value) {
          if (value === undefined) return forces.get(name);
          forces.set(name, value);
          return this;
        },
        nodes(nextNodes) {
          if (nextNodes === undefined) return this.currentNodes;
          this.currentNodes = nextNodes;
          position(nextNodes);
          trace.nodeCounts.push(nextNodes.length);
          return this;
        },
        on(name, listener) {
          if (name === "tick") {
            trace.tick = listener;
            listener();
          }
          return this;
        },
        restart() { return this; },
      };
      trace.simulation = simulation;
      return simulation;
    },
  };
}

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../..");
const context = vm.createContext({ console });
for (const asset of [
  "daem0nmcp/ui/static/runtime.js",
  "daem0nmcp/ui/static/renderers/test.js",
  "daem0nmcp/ui/static/renderers/search.js",
  "daem0nmcp/ui/static/renderers/briefing.js",
  "daem0nmcp/ui/static/renderers/covenant.js",
  "daem0nmcp/ui/static/renderers/community.js",
  "daem0nmcp/ui/static/renderers/graph.js",
]) {
  vm.runInContext(fs.readFileSync(path.join(root, asset), "utf8"), context, { filename: asset });
}

const ui = context.Daem0nUI;

function renderApp(appId, payload, options = {}) {
  const document = new FakeDocument(appId, payload, options);
  const handlers = new Map();
  const sent = [];
  const idle = [];
  const resizeCallbacks = [];
  const animationFrames = new Map();
  const cancelledFrames = [];
  let nextAnimationFrame = 0;
  const trace = { linkCounts: [], nodeCounts: [], simulation: null, tick: null };
  context.document = document;
  context.devicePixelRatio = options.devicePixelRatio ?? 1;
  context.D3 = options.d3 === false ? undefined : fakeD3(trace);
  context.requestAnimationFrame = options.animationFrames ? callback => {
    nextAnimationFrame += 1;
    animationFrames.set(nextAnimationFrame, callback);
    return nextAnimationFrame;
  } : undefined;
  context.cancelAnimationFrame = frameId => {
    cancelledFrames.push(frameId);
    animationFrames.delete(frameId);
  };
  context.requestIdleCallback = callback => { idle.push(callback); return idle.length; };
  context.cancelIdleCallback = () => {};
  context.setTimeout = () => 1;
  context.clearTimeout = () => {};
  context.addEventListener = () => {};
  const messenger = {
    on(name, handler) { handlers.set(name, handler); },
    request(method, value) { sent.push({ method, value }); return Promise.resolve({ structuredContent: { ok: false } }); },
  };
  if (options.messengerMode === "notify") messenger.notify = (method, value) => { sent.push({ method, value }); };
  else messenger.send = (method, value) => { sent.push({ method, value }); };
  context.SecureMessenger = messenger;
  context.ResizeObserver = class {
    constructor(callback) { resizeCallbacks.push(callback); }
    disconnect() {}
    observe() {}
  };
  ui.bootstrap();
  if (options.workspaceId) {
    handlers.get("ui/notifications/tool-input")?.({
      toolName: options.initialTool || { search: "memory_recall", covenant: "covenant_status" }[appId],
      arguments: { workspace_id: options.workspaceId, ...(options.initialArgs || {}) },
    });
  }
  return { animationFrames, cancelledFrames, document, handlers, idle, resizeCallbacks, sent, trace };
}

function runNextAnimationFrame(rendered, timestamp) {
  const entry = rendered.animationFrames.entries().next().value;
  assert.ok(entry, "an animation frame must be pending");
  const [frameId, callback] = entry;
  rendered.animationFrames.delete(frameId);
  callback(timestamp);
  return { callback, frameId };
}

test("all six fixed renderers register without a DOM", () => {
  assert.deepEqual(Array.from(ui.appIds()), ["test", "search", "briefing", "covenant", "community", "graph"]);
});

test("search normalizer clamps numeric values and rejects boolean IDs", () => {
  const result = ui.normalize("search", {
    decisions: [{ id: true, content: "ok", relevance: 95, semantic_match: -1, recency_weight: Infinity }],
    limit: 999,
  });
  assert.equal(result.decisions[0].id, null);
  assert.equal(result.decisions[0].relevance, 0.95);
  assert.equal(result.decisions[0].semantic_match, 0);
  assert.equal(result.decisions[0].recency_weight, null, "an unavailable weight is not fabricated");
  assert.equal(result.limit, undefined, "server pagination options do not enter the view model");
});

test("briefing and covenant normalizers use neutral enum fallbacks", () => {
  assert.equal(ui.normalize("briefing", { status: "evil", active_warnings: [{ severity: "critical" }] }).status, "neutral");
  assert.equal(ui.normalize("briefing", { active_warnings: [{ severity: "critical" }] }).active_warnings[0].severity, "neutral");
  assert.equal(ui.normalize("covenant", { phase: "evil", preflight: { status: "evil", remaining_seconds: -10 } }).phase, "unknown");
  assert.equal(ui.normalize("covenant", { phase: "evil", preflight: { status: "evil", remaining_seconds: -10 } }).preflight.remaining_seconds, 0);
});

test("community normalizer removes bad hierarchy but projects safe breadcrumbs independently", () => {
  const result = ui.normalize("community", {
    communities: [
      { id: 1, name: "root" },
      { id: 1, name: "duplicate" },
      { id: 2, parent_community_id: 999, name: "orphan" },
    ],
    path: [
      { id: 1, name: "root" },
      { id: "ancestor", name: "not in this page" },
      { id: "bad id", name: "unsafe" },
    ],
  });
  assert.deepEqual(Array.from(result.communities, item => item.id), [1, 2]);
  assert.equal(result.communities[1].parent_community_id, null);
  assert.deepEqual(JSON.parse(JSON.stringify(result.path)), [
    { id: 1, name: "root" },
    { id: "ancestor", name: "not in this page" },
  ]);
});

test("graph normalizer separates integer and string IDs and drops missing endpoints", () => {
  const result = ui.normalize("graph", {
    nodes: [{ id: 1, category: "decision" }, { id: "1", category: "bad" }],
    edges: [{ source: 1, target: "1", confidence: 50 }, { source: 1, target: 2 }],
    path: [1, "1", 2],
  });
  assert.equal(result.nodes[1].category, "default");
  assert.equal(result.edges.length, 1);
  assert.equal(result.edges[0].confidence, 0.5);
  assert.deepEqual(Array.from(result.path), [1, "1"]);
});

test("fixed actions never derive tool or message names from input", () => {
  assert.deepEqual(JSON.parse(JSON.stringify(ui.actions)), {
    briefingFocus: { tool: "memory_recall" },
    contextCheck: { tool: "covenant_status" },
    listCommunities: { tool: "community_list" },
    graphFocus: { tool: "knowledge_graph_get" },
  });
});

test("data_updated exposes only a fixed refresh indicator and refresh clears it", () => {
  const rendered = renderApp("search", {}, { workspaceId: "ws_1", initialArgs: { query: "q" } });
  const indicator = findByClass(rendered.document.mount, "daemon-update-badge");
  assert.ok(indicator);
  assert.equal(indicator.hidden, true);
  const marker = "<img src=x onerror=alert(1)>";
  rendered.handlers.get("data_updated")({ last_update: marker, content: marker });
  assert.equal(indicator.hidden, false);
  assert.equal(indicator.textContent, "New data available");
  assert.equal(findByText(rendered.document.mount, marker), null);
  findByText(rendered.document.mount, "Refresh").dispatch("click");
  assert.equal(indicator.hidden, true);
  assert.deepEqual(JSON.parse(JSON.stringify(rendered.sent)), [{ method: "tools/call", value: { name: "memory_recall", arguments: { workspace_id: "ws_1", query: "q" } } }]);
});

test("covenant refresh emits the exact read-only status request", () => {
  const rendered = renderApp("covenant", { preflight: { remaining_seconds: 0 } }, { workspaceId: "ws_1" });
  const refresh = findByText(rendered.document.mount, "Refresh Token");
  assert.ok(refresh);
  refresh.dispatch("click");
  assert.deepEqual(JSON.parse(JSON.stringify(rendered.sent)), [{ method: "tools/call", value: { name: "covenant_status", arguments: { workspace_id: "ws_1" } } }]);
});

test("search pagination traverses delivered results without unsupported MCP arguments", () => {
  const rendered = renderApp("search", {
    decisions: Array.from({ length: 21 }, (_, index) => ({ id: index + 1, content: "Decision " + index })),
  }, { workspaceId: "ws_1", initialArgs: { query: "q" } });
  assert.ok(findByText(rendered.document.mount, "Decision 0"));
  assert.equal(findByText(rendered.document.mount, "Decision 10"), null);
  findByText(rendered.document.mount, "Next").dispatch("click");
  assert.ok(findByText(rendered.document.mount, "Decision 10"));
  assert.equal(findByText(rendered.document.mount, "Decision 0"), null);
  findByText(rendered.document.mount, "Next").dispatch("click");
  assert.ok(findByText(rendered.document.mount, "Decision 20"));
  assert.equal(findByText(rendered.document.mount, "Next").disabled, true);
  assert.equal(rendered.sent.length, 0);
});

test("graph resizes its backing store, scales clicks, bounds pan, and resets view", () => {
  const rendered = renderApp("graph", {
    nodes: [{ id: 1, category: "decision", content: "node", full_content: "selected" }],
  }, { devicePixelRatio: 2 });
  const canvas = findByClass(rendered.document.mount, "graph-canvas");
  assert.equal(canvas.width, 2000);
  assert.equal(canvas.height, 1000);
  canvas.dispatch("click", { clientX: 200, clientY: 125 });
  assert.equal(findByClass(rendered.document.mount, "secure-graph-details").hidden, false);

  assert.equal(rendered.resizeCallbacks.length, 1);
  rendered.document.frameRect = { left: 0, top: 0, width: 800, height: 400 };
  rendered.document.canvasRect = { left: 0, top: 0, width: 400, height: 200 };
  rendered.resizeCallbacks[0]();
  assert.equal(canvas.width, 1600);
  assert.equal(canvas.height, 800);

  canvas.dispatch("pointerdown", { clientX: 10, clientY: 10, pointerId: 7 });
  canvas.dispatch("pointermove", { clientX: 100000, clientY: -100000, pointerId: 7 });
  canvas.dispatch("pointerup", { clientX: 100000, clientY: -100000, pointerId: 7 });
  const translations = rendered.document.canvasContext.operations.filter(operation => operation[0] === "translate");
  assert.ok(translations.length);
  const bounded = translations.at(-1);
  assert.ok(Math.abs(bounded[1]) <= 800);
  assert.ok(Math.abs(bounded[2]) <= 400);

  findByText(rendered.document.mount, "Reset view").dispatch("click");
  const reset = rendered.document.canvasContext.operations.filter(operation => operation[0] === "translate").at(-1);
  assert.deepEqual(reset, ["translate", 0, 0]);
});

test("large graphs enter the force simulation in bounded progressive batches", () => {
  const nodes = Array.from({ length: 251 }, (_, index) => ({ id: index + 1, content: String(index) }));
  const rendered = renderApp("graph", { nodes });
  assert.ok(rendered.trace.nodeCounts[0] <= 50);
  let prior = rendered.trace.nodeCounts.at(-1);
  while (rendered.idle.length) {
    const callback = rendered.idle.shift();
    callback({ didTimeout: false, timeRemaining: () => 50 });
    const current = rendered.trace.nodeCounts.at(-1);
    assert.ok(current - prior <= 50);
    prior = current;
  }
  assert.equal(rendered.trace.nodeCounts.at(-1), nodes.length);
  const loading = findByClass(rendered.document.mount, "graph-loading");
  assert.ok(loading);
  assert.ok(loading.hidden || loading.classList.contains("graph-loading--hidden"));
});

test("graph registers host path handlers with standalone and vendored messenger adapters", () => {
  for (const messengerMode of ["send", "notify"]) {
    const rendered = renderApp("graph", { nodes: [{ id: 1 }, { id: 2 }] }, { messengerMode });
    assert.equal(typeof rendered.handlers.get("show_path"), "function", messengerMode);
    assert.equal(typeof rendered.handlers.get("clear_path"), "function", messengerMode);
  }
});

test("show_path admits only present safe IDs and clamps both duration bounds", () => {
  const rendered = renderApp("graph", {
    nodes: [{ id: 1 }, { id: "1" }],
  }, { animationFrames: true });
  const showPath = rendered.handlers.get("show_path");
  assert.equal(typeof showPath, "function");

  showPath({
    path: [{ id: 1 }, { id: 999 }, { id: "bad id" }],
    duration: 5000,
  });
  assert.equal(rendered.animationFrames.size, 0);

  showPath({ path: [{ id: 1 }, { id: "1" }], duration: 50_000 });
  assert.equal(rendered.animationFrames.size, 1);
  runNextAnimationFrame(rendered, 0);
  assert.equal(rendered.animationFrames.size, 1);
  runNextAnimationFrame(rendered, 5000);
  assert.equal(rendered.animationFrames.size, 1);
  runNextAnimationFrame(rendered, 10_000);
  assert.equal(rendered.animationFrames.size, 0);
  assert.ok(rendered.document.canvasContext.operations.some(operation =>
    operation[0] === "stroke" && operation[1] === "#8b5cf6" && operation[2] === 4
  ));

  showPath({ path: [{ id: 1 }, { id: "1" }], duration: -10 });
  runNextAnimationFrame(rendered, 0);
  assert.equal(rendered.animationFrames.size, 0);
});

test("restarting and clearing a host path cancels stale animation and removes its highlight", () => {
  const rendered = renderApp("graph", {
    nodes: [{ id: 1 }, { id: 2 }],
  }, { animationFrames: true, messengerMode: "notify" });
  const showPath = rendered.handlers.get("show_path");
  const clearPath = rendered.handlers.get("clear_path");
  assert.equal(typeof showPath, "function");
  assert.equal(typeof clearPath, "function");

  showPath({ path: [{ id: 1 }, { id: 2 }], duration: 10_000 });
  const first = rendered.animationFrames.entries().next().value;
  showPath({ path: [{ id: 2 }, { id: 1 }], duration: 10_000 });
  assert.ok(rendered.cancelledFrames.includes(first[0]));
  assert.equal(rendered.animationFrames.size, 1);
  const restartedFrame = rendered.animationFrames.keys().next().value;
  first[1](0);
  assert.deepEqual(Array.from(rendered.animationFrames.keys()), [restartedFrame]);

  const active = runNextAnimationFrame(rendered, 0);
  assert.equal(rendered.animationFrames.size, 1);
  const operationStart = rendered.document.canvasContext.operations.length;
  clearPath({ ignored: "<script>" });
  assert.ok(rendered.cancelledFrames.length >= 2);
  assert.equal(rendered.animationFrames.size, 0);
  const clearOperations = rendered.document.canvasContext.operations.slice(operationStart);
  assert.equal(clearOperations.some(operation => operation[0] === "stroke" && operation[2] === 4), false);
  active.callback(5000);
  assert.equal(rendered.animationFrames.size, 0);
});

test("fixed native graph buttons zoom within bounds and reset without removing wheel or pan", () => {
  const rendered = renderApp("graph", { nodes: [{ id: 1 }] });
  const zoomIn = findByText(rendered.document.mount, "Zoom in");
  const zoomOut = findByText(rendered.document.mount, "Zoom out");
  const reset = findByText(rendered.document.mount, "Reset view");
  for (const control of [zoomIn, zoomOut, reset]) {
    assert.ok(control);
    assert.equal(control.tagName, "button");
    assert.equal(control.type, "button");
  }

  zoomIn.dispatch("click");
  assert.deepEqual(rendered.document.canvasContext.operations.filter(value => value[0] === "scale").at(-1), ["scale", 1.25, 1.25]);
  for (let index = 0; index < 30; index += 1) zoomIn.dispatch("click");
  assert.equal(rendered.document.canvasContext.operations.filter(value => value[0] === "scale").at(-1)[1], 4);
  for (let index = 0; index < 60; index += 1) zoomOut.dispatch("click");
  assert.equal(rendered.document.canvasContext.operations.filter(value => value[0] === "scale").at(-1)[1], 0.1);

  const canvas = findByClass(rendered.document.mount, "graph-canvas");
  assert.ok(canvas.listeners.has("wheel"));
  assert.ok(canvas.listeners.has("pointerdown"));
  reset.dispatch("click");
  assert.deepEqual(rendered.document.canvasContext.operations.filter(value => value[0] === "scale").at(-1), ["scale", 1, 1]);
});

test("node details retain a fixed native dismiss control", () => {
  const rendered = renderApp("graph", {
    nodes: [{ id: 1, category: "decision", full_content: "selected" }],
  });
  const canvas = findByClass(rendered.document.mount, "graph-canvas");
  const details = findByClass(rendered.document.mount, "secure-graph-details");
  canvas.dispatch("click", { clientX: 200, clientY: 125 });
  assert.equal(details.hidden, false);
  const dismiss = findByText(details, "Close details");
  assert.ok(dismiss);
  assert.equal(dismiss.tagName, "button");
  assert.equal(dismiss.type, "button");
  dismiss.dispatch("click");
  assert.equal(details.hidden, true);
});
