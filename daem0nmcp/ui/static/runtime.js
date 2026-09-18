(function (root) {
  "use strict";

  const allowedApps = Object.freeze(["test", "search", "briefing", "covenant", "community", "graph"]);
  const registry = new Map();
  let workspaceId = null;
  const lastCalls = new Map();
  const toolForApp = Object.freeze({
    search: "memory_recall",
    briefing: "session_brief",
    covenant: "covenant_status",
    community: "community_list",
    graph: "knowledge_graph_get",
  });
  const MAX_SAFE = Number.MAX_SAFE_INTEGER;
  const idPattern = /^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$/;

  function text(value, limit = 256) {
    return typeof value === "string" ? value.slice(0, limit) : "";
  }

  function list(value, limit) {
    return Array.isArray(value) ? value.slice(0, limit) : [];
  }

  function object(value) {
    return value && typeof value === "object" && !Array.isArray(value) ? value : {};
  }

  function count(value, fallback = 0) {
    return Number.isSafeInteger(value) ? Math.min(Math.max(value, 0), MAX_SAFE) : fallback;
  }

  function integer(value, minimum, maximum, fallback) {
    return Number.isSafeInteger(value) ? Math.min(Math.max(value, minimum), maximum) : fallback;
  }

  function ratio(value, fallback = 0) {
    if (typeof value !== "number" || !Number.isFinite(value)) return fallback;
    const adjusted = value > 1 && value <= 100 ? value / 100 : value;
    return Math.min(Math.max(adjusted, 0), 1);
  }

  function safeId(value) {
    if (Number.isSafeInteger(value) && value > 0) return value;
    if (typeof value === "string" && idPattern.test(value)) return value;
    return null;
  }

  function idKey(value) {
    return typeof value + ":" + String(value);
  }

  function date(value) {
    const bounded = text(value);
    return bounded && Number.isFinite(Date.parse(bounded)) ? bounded : "";
  }

  function optionalBoolean(value) {
    return typeof value === "boolean" ? value : null;
  }

  function element(tagName, className, value) {
    const node = document.createElement(tagName);
    if (className) node.className = className;
    if (value !== undefined) node.textContent = value;
    return node;
  }

  function svgElement(tagName, className) {
    const node = document.createElementNS("http://www.w3.org/2000/svg", tagName);
    if (className) node.setAttribute("class", className);
    return node;
  }

  function sendHost(method, payload) {
    const messenger = root.SecureMessenger;
    if (!messenger) return;
    if (typeof messenger.send === "function") messenger.send(method, payload);
    else if (typeof messenger.notify === "function") messenger.notify(method, payload);
  }

  const actions = Object.freeze({
    briefingFocus: Object.freeze({ tool: "memory_recall" }),
    contextCheck: Object.freeze({ tool: "covenant_status" }),
    listCommunities: Object.freeze({ tool: "community_list" }),
    graphFocus: Object.freeze({ tool: "knowledge_graph_get" }),
  });

  function rememberCall(tool, args) {
    if (!Object.values(toolForApp).includes(tool)) return;
    const safeArgs = object(args);
    if (typeof safeArgs.workspace_id === "string") workspaceId = safeArgs.workspace_id;
    lastCalls.set(tool, safeArgs);
  }

  function callTool(tool, args) {
    const messenger = root.SecureMessenger;
    if (!messenger || typeof messenger.request !== "function" || !workspaceId) return Promise.resolve(null);
    // A new action is a complete request. Only Refresh explicitly reuses the
    // previous arguments; retaining a cursor or graph query changes its meaning.
    const callArgs = { ...object(args), workspace_id: workspaceId };
    rememberCall(tool, callArgs);
    return messenger.request("tools/call", { name: tool, arguments: callArgs })
      .then(result => applyToolResult({ toolName: tool, arguments: callArgs, result }))
      .catch(() => { showActionStatus("Unable to update this view. Retry after checking the connection."); return null; });
  }

  function showActionStatus(message, items = []) {
    const mount = document.getElementById("app");
    if (!mount) return;
    let panel = document.getElementById("daemon-action-result");
    if (!panel) {
      panel = element("section", "daemon-card");
      panel.id = "daemon-action-result";
      panel.setAttribute("role", "status");
      mount.append(panel);
    }
    panel.replaceChildren(element("p", "", message));
    for (const item of items) panel.append(element("p", "", text(object(object(item).record).excerpt, 16384)));
  }

  function applyToolResult(value) {
    const notification = object(value);
    const appId = document.documentElement.getAttribute("data-daem0n-app");
    const tool = text(notification.toolName || notification.tool_name || toolForApp[appId], 128);
    const args = object(notification.arguments || notification.toolInput || notification.tool_input);
    if (tool && Object.keys(args).length) rememberCall(tool, args);
    const result = object(object(notification.result).structuredContent || notification.structuredContent || notification.result || notification);
    const data = object(result.data);
    const meta = object(result.meta);
    if (typeof meta.workspace_id === "string") workspaceId = meta.workspace_id;
    const entry = registry.get(appId);
    const mount = document.getElementById("app");
    if (!entry || !mount) return null;
    if (result.ok !== true) {
      showActionStatus("The request could not be completed. Check the tool response for the required next step.");
      return null;
    }
    if (tool !== toolForApp[appId]) {
      if (tool === "covenant_status") showActionStatus(data.briefed === true
        ? "Scoped briefing is recorded. Protected actions require their own authorization."
        : "A new scoped briefing is required.");
      else if (tool === "memory_recall") showActionStatus("Focus results", list(data.items, 10));
      return result;
    }
    entry.renderer(entry.normalizer(projectV7(appId, data)), mount);
    return result;
  }

  function projectV7(appId, data) {
    if (appId === "search") {
      const result = { topic: text(object(lastCalls.get("memory_recall")).query), decisions: [], warnings: [], patterns: [], learnings: [] };
      for (const item of list(data.items, 50)) {
        const record = object(object(item).record);
        const target = { decision: "decisions", warning: "warnings", pattern: "patterns", learning: "learnings" }[record.record_type] || "learnings";
        const origin = list(item.evidence_refs, 32).map(ref => text(object(ref).origin_workspace_id, 128)).find(Boolean) || "";
        result[target].push({ id: record.record_id, content: item.bounded_excerpt || record.excerpt, relevance: item.score, tags: record.tags, created_at: record.created_at, origin_workspace_id: origin, citation: item.citation });
      }
      result.total_count = result.decisions.length + result.warnings.length + result.patterns.length + result.learnings.length;
      return result;
    }
    if (appId === "briefing") {
      const stats = object(data.workspace_statistics);
      const outcomes = count(stats.successful_outcomes) + count(stats.failed_outcomes);
      const gitStates = { added: "A", modified: "M", deleted: "D", renamed: "R", untracked: "?", conflicted: "U" };
      return {
        status: "ready",
        statistics: {
          total_memories: stats.records,
          by_category: { decision: stats.decisions, warning: stats.warnings, pattern: stats.patterns, learning: stats.learnings },
          outcome_rates: { success_rate: outcomes ? count(stats.successful_outcomes) / outcomes : null },
        },
        recent_decisions: list(data.recent_decisions, 20).map(record => ({ content: object(record).excerpt, created_at: object(record).created_at })),
        active_warnings: list(data.warnings, 20).map(record => ({ content: object(record).excerpt })),
        failed_approaches: list(data.failed_outcomes, 20).map(item => ({ content: object(item).outcome_excerpt })),
        git_changes: { total: list(data.git_changes, 200).length, files: list(data.git_changes, 20).map(item => ({ path: object(item).relative_file_path, status: gitStates[object(item).status] || "?" })) },
      };
    }
    if (appId === "covenant") return { phase: "commune", preflight: { status: "none" }, can_mutate: false, message: data.briefed === true ? "Scoped briefing is recorded. Protected actions require their own authorization." : object(data.next_step).reason };
    if (appId === "community") return { count: list(data.items, 100).length, communities: list(data.items, 100).map(item => ({ id: object(item).community_id, name: object(item).label, level: object(item).level, member_count: object(item).member_count, parent_community_id: object(item).parent_community_id })) };
    if (appId === "graph") return {
      nodes: list(data.nodes, 500).map(item => { const record = object(object(item).record); return { id: record.record_id, content: record.excerpt, full_content: record.excerpt, category: record.record_type, tags: record.tags, created_at: record.created_at }; }),
      edges: list(data.edges, 2000).map(item => { const relationship = object(object(item).relationship); return { source: relationship.source_record_id, target: relationship.target_record_id, relationship: relationship.relationship_type, confidence: relationship.confidence, description: relationship.description }; }),
    };
    return data;
  }

  function refreshButton() {
    const control = element("div", "daemon-refresh-control");
    const indicator = element("span", "daemon-update-badge", "New data available");
    indicator.hidden = true;
    indicator.setAttribute("role", "status");
    indicator.setAttribute("aria-live", "polite");
    const button = element("button", "daemon-btn daemon-btn--secondary daemon-refresh", "Refresh");
    button.type = "button";
    const messenger = root.SecureMessenger;
    if (messenger && typeof messenger.on === "function") {
      messenger.on("data_updated", function () {
        indicator.hidden = false;
        indicator.classList.add("daemon-update-badge--visible");
      });
    }
    const appId = document.documentElement.getAttribute("data-daem0n-app");
    const refreshTool = toolForApp[appId];
    const cached = refreshTool ? lastCalls.get(refreshTool) : null;
    if (!refreshTool || (!cached && appId === "search")) {
      button.disabled = true;
      button.title = "Run a dashboard query before refreshing.";
    }
    button.addEventListener("click", function () {
      indicator.hidden = true;
      indicator.classList.remove("daemon-update-badge--visible");
      if (!refreshTool) return;
      button.disabled = true;
      button.textContent = "Refreshing…";
      const args = lastCalls.get(refreshTool) || {};
      callTool(refreshTool, args).finally(function () {
        button.disabled = false;
        button.textContent = "Refresh";
      });
    });
    control.append(indicator, button);
    return control;
  }

  function register(appId, normalizer, renderer) {
    if (!allowedApps.includes(appId) || registry.has(appId)) throw new Error("invalid app registration");
    registry.set(appId, Object.freeze({ normalizer, renderer }));
    if (typeof document !== "undefined") bootstrap();
  }

  function normalize(appId, data) {
    const entry = registry.get(appId);
    if (!entry) throw new Error("unknown app");
    return entry.normalizer(object(data));
  }

  function bootstrap() {
    const appId = document.documentElement.getAttribute("data-daem0n-app");
    const entry = registry.get(appId);
    if (!entry) return;
    const blocks = document.querySelectorAll("#app-data");
    const mount = document.getElementById("app");
    if (blocks.length !== 1 || !mount) return;
    let parsed;
    try {
      parsed = JSON.parse(blocks[0].textContent);
    } catch (_error) {
      mount.replaceChildren(element("p", "daemon-error", "Unable to render this view."));
      return;
    }
    mount.replaceChildren();
    entry.renderer(entry.normalizer(object(parsed)), mount);
    const messenger = root.SecureMessenger;
    if (messenger && typeof messenger.on === "function") {
      messenger.on("ui/notifications/tool-result", applyToolResult);
      messenger.on("ui/notifications/tool-input", function (value) {
        const params = object(value);
        const tool = text(params.toolName || params.tool_name || object(params.tool).name || toolForApp[appId], 128);
        const args = object(params.arguments || params.toolInput || params.tool_input);
        if (tool) rememberCall(tool, args);
      });
      if (typeof messenger.connect === "function") messenger.connect().catch(function () {});
    }
  }

  root.Daem0nUI = Object.freeze({
    actions,
    appIds: function () { return registry.keys(); },
    callTool,
    bootstrap,
    element,
    idKey,
    normalize,
    primitives: Object.freeze({ count, date, integer, list, object, optionalBoolean, ratio, safeId, text }),
    refreshButton,
    register,
    sendHost,
    applyToolResult,
    svgElement,
  });
})(globalThis);
