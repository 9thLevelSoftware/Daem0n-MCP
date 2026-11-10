(function (ui) {
  "use strict";
  const p = ui.primitives;
  const phases = Object.freeze(["commune", "counsel", "inscribe", "seal"]);
  const statuses = Object.freeze(["valid", "issued", "expired", "none"]);
  const phaseClasses = Object.freeze({ commune: "covenant-phase covenant-phase--commune", counsel: "covenant-phase covenant-phase--counsel", inscribe: "covenant-phase covenant-phase--inscribe", seal: "covenant-phase covenant-phase--seal", unknown: "covenant-phase" });
  const statusClasses = Object.freeze({ valid: "daemon-badge daemon-badge--success", issued: "daemon-badge daemon-badge--success", expired: "daemon-badge daemon-badge--error", none: "daemon-badge", unknown: "daemon-badge" });

  function normalize(data) {
    const preflight = p.object(data.preflight);
    return {
      phase: phases.includes(data.phase) ? data.phase : "unknown",
      phase_label: p.text(data.phase_label),
      phase_description: p.text(data.phase_description, 16384),
      preflight: {
        status: statuses.includes(preflight.status) ? preflight.status : "unknown",
        expires_at: p.date(preflight.expires_at),
        remaining_seconds: p.integer(preflight.remaining_seconds, 0, 86400, 0),
      },
      can_mutate: typeof data.can_mutate === "boolean" ? data.can_mutate : false,
      message: p.text(data.message, 16384),
    };
  }

  function diagram(active) {
    const svg = ui.svgElement("svg", "covenant-diagram");
    svg.setAttribute("viewBox", "0 0 640 120");
    svg.setAttribute("role", "img");
    svg.setAttribute("aria-label", "Covenant phases");
    phases.forEach(function (phase, index) {
      if (index < phases.length - 1) {
        const line = ui.svgElement("line", "covenant-link");
        line.setAttribute("x1", String(100 + index * 150));
        line.setAttribute("y1", "50");
        line.setAttribute("x2", String(200 + index * 150));
        line.setAttribute("y2", "50");
        svg.append(line);
      }
      const group = ui.svgElement("g", phase === active ? "covenant-node covenant-node--active" : "covenant-node");
      const circle = ui.svgElement("circle", "covenant-node__circle");
      circle.setAttribute("cx", String(50 + index * 150));
      circle.setAttribute("cy", "50");
      circle.setAttribute("r", "28");
      const label = ui.svgElement("text", "covenant-node__label");
      label.setAttribute("x", String(50 + index * 150));
      label.setAttribute("y", "100");
      label.setAttribute("text-anchor", "middle");
      label.textContent = phase.toUpperCase();
      group.append(circle, label);
      svg.append(group);
    });
    return svg;
  }

  function render(data, mount) {
    const header = ui.element("header", "daemon-app-header");
    header.append(ui.element("h1", "daemon-app-title", "Covenant Status"), ui.refreshButton());
    const status = ui.element("section", phaseClasses[data.phase]);
    status.append(
      ui.element("h2", "covenant-phase__label", data.phase_label || data.phase.toUpperCase()),
      ui.element("p", "covenant-phase__description", data.phase_description),
      ui.element("span", statusClasses[data.preflight.status], data.preflight.status.toUpperCase())
    );
    if (data.preflight.expires_at) status.append(ui.element("time", "covenant-expiry", data.preflight.expires_at));
    let remaining = data.preflight.remaining_seconds;
    const countdown = ui.element("p", "covenant-countdown", remaining + " seconds remaining");
    countdown.hidden = !remaining;
    status.append(countdown);
    if (remaining > 0) {
      const timer = globalThis.setInterval(function () {
        remaining = Math.max(0, remaining - 1);
        countdown.textContent = remaining + " seconds remaining";
        countdown.hidden = remaining === 0;
        if (remaining === 0) globalThis.clearInterval(timer);
      }, 1000);
    }
    const refresh = ui.element("button", "daemon-btn", "Refresh Token");
    refresh.type = "button";
    refresh.addEventListener("click", function () {
      ui.callTool(ui.actions.contextCheck.tool, {});
    });
    mount.replaceChildren(header, diagram(data.phase), status, ui.element("p", "covenant-message", data.message), refresh);
  }

  ui.register("covenant", normalize, render);
})(globalThis.Daem0nUI);
