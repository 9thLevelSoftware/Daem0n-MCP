(function (ui) {
  "use strict";
  const p = ui.primitives;
  const statusClasses = Object.freeze({ ready: "daemon-badge daemon-badge--success", error: "daemon-badge daemon-badge--error", degraded: "daemon-badge daemon-badge--warning", neutral: "daemon-badge" });
  const severityClasses = Object.freeze({ high: "daemon-badge daemon-badge--error", medium: "daemon-badge daemon-badge--warning", low: "daemon-badge daemon-badge--success", neutral: "daemon-badge" });
  const gitClasses = Object.freeze({ A: "git-status git-status--added", M: "git-status git-status--modified", D: "git-status git-status--deleted", "?": "git-status" });

  function itemContent(value) {
    const item = p.object(value);
    return p.text(item.content || item.summary, 16384);
  }

  function normalize(data) {
    const stats = p.object(data.statistics);
    const categories = p.object(stats.by_category);
    const rates = p.object(stats.outcome_rates);
    const git = p.object(data.git_changes);
    const rawFiles = Array.isArray(git.files) ? git.files : git.uncommitted_changes;
    let rawFocus = data.focus_areas;
    if (rawFocus && typeof rawFocus === "object" && !Array.isArray(rawFocus)) rawFocus = Object.keys(rawFocus).map(topic => ({ topic }));
    const status = ["ready", "error", "degraded"].includes(data.status) ? data.status : "neutral";
    return {
      status,
      statistics: {
        total_memories: p.count(stats.total_memories),
        by_category: {
          decision: p.count(categories.decision), warning: p.count(categories.warning),
          pattern: p.count(categories.pattern), learning: p.count(categories.learning),
        },
        outcome_rates: { success_rate: typeof rates.success_rate === "number" && Number.isFinite(rates.success_rate) ? p.ratio(rates.success_rate) : null },
      },
      recent_decisions: p.list(data.recent_decisions, 20).map(value => {
        const item = p.object(value);
        return { content: itemContent(item), worked: p.optionalBoolean(item.worked), created_at: p.date(item.created_at) };
      }),
      active_warnings: p.list(data.active_warnings, 20).map(value => {
        const item = p.object(value);
        return { content: itemContent(item), severity: ["high", "medium", "low"].includes(item.severity) ? item.severity : "neutral" };
      }),
      failed_approaches: p.list(data.failed_approaches, 20).map(value => ({ content: itemContent(value) })),
      git_changes: {
        total: p.count(git.total, Array.isArray(rawFiles) ? rawFiles.length : 0),
        files: p.list(rawFiles, 20).map(value => {
          const item = p.object(value);
          return { status: ["A", "M", "D", "R", "U"].includes(item.status) ? item.status : "?", path: p.text(item.path || item.file, 4096) };
        }),
      },
      focus_areas: p.list(rawFocus, 20).map(value => ({ topic: p.text(p.object(value).topic) })),
      message: p.text(data.message, 16384),
    };
  }

  function section(title, rows) {
    const details = ui.element("details", "briefing-section");
    details.append(ui.element("summary", "briefing-section__title", title));
    const body = ui.element("div", "briefing-section__body");
    if (!rows.length) body.append(ui.element("p", "daemon-empty", "None"));
    else body.append(...rows);
    details.append(body);
    return details;
  }

  function render(data, mount) {
    const header = ui.element("header", "daemon-app-header");
    header.append(ui.element("h1", "daemon-app-title", "Session Briefing"), ui.element("span", statusClasses[data.status], data.status.toUpperCase()), ui.refreshButton());
    const stats = ui.element("section", "daemon-stats");
    const values = [
      [data.statistics.total_memories, "Total Memories"],
      [data.statistics.by_category.decision, "Decisions"],
      [data.statistics.by_category.warning, "Warnings"],
      [data.statistics.by_category.pattern, "Patterns"],
      [data.statistics.outcome_rates.success_rate === null ? "—" : Math.round(data.statistics.outcome_rates.success_rate * 100) + "%", "Success Rate"],
    ];
    for (const value of values) {
      const card = ui.element("div", "daemon-stat");
      card.append(ui.element("strong", "daemon-stat__value", String(value[0])), ui.element("span", "daemon-stat__label", value[1]));
      stats.append(card);
    }
    const decisions = data.recent_decisions.map(item => {
      const row = ui.element("article", "briefing-row");
      const label = item.worked === true ? "SUCCESS" : item.worked === false ? "FAILED" : "PENDING";
      const cls = item.worked === true ? "daemon-badge daemon-badge--success" : item.worked === false ? "daemon-badge daemon-badge--error" : "daemon-badge";
      row.append(ui.element("span", cls, label), ui.element("span", "briefing-row__content", item.content));
      if (item.created_at) row.append(ui.element("time", "daemon-muted", item.created_at));
      return row;
    });
    const warnings = data.active_warnings.map(item => {
      const row = ui.element("article", "briefing-row");
      row.append(ui.element("span", severityClasses[item.severity], item.severity.toUpperCase()), ui.element("span", "briefing-row__content", item.content));
      return row;
    });
    const failed = data.failed_approaches.map(item => ui.element("p", "briefing-row", item.content));
    const files = data.git_changes.files.map(item => {
      const row = ui.element("p", "briefing-row");
      row.append(ui.element("span", gitClasses[item.status] || "git-status", item.status), ui.element("code", "git-path", item.path));
      return row;
    });
    const focus = data.focus_areas.map(item => {
      const button = ui.element("button", "daemon-btn daemon-btn--secondary", item.topic || "Focus area");
      button.type = "button";
      button.addEventListener("click", function () { ui.callTool(ui.actions.briefingFocus.tool, { query: item.topic, limit: 10 }); });
      return button;
    });
    const check = ui.element("button", "daemon-btn", "Check Context");
    check.type = "button";
    check.addEventListener("click", function () { ui.callTool(ui.actions.contextCheck.tool, {}); });
    mount.replaceChildren(
      header,
      stats,
      data.message ? ui.element("p", "briefing-message", data.message) : ui.element("span", "daemon-empty", ""),
      section("Recent Decisions", decisions),
      section("Active Warnings", warnings),
      section("Failed Approaches", failed),
      section("Git Changes (" + data.git_changes.total + ")", files),
      section("Focus Areas", focus),
      check
    );
  }

  ui.register("briefing", normalize, render);
})(globalThis.Daem0nUI);
