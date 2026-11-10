(function (ui) {
  "use strict";
  const p = ui.primitives;
  const levelClasses = Object.freeze([
    "community-cell community-cell--level-0", "community-cell community-cell--level-1",
    "community-cell community-cell--level-2", "community-cell community-cell--level-3",
    "community-cell community-cell--level-4", "community-cell community-cell--level-5",
  ]);

  function normalize(data) {
    const seen = new Set();
    const communities = [];
    for (const value of p.list(data.communities, 1000)) {
      const item = p.object(value);
      const id = p.safeId(item.id);
      if (id === null || seen.has(ui.idKey(id))) continue;
      seen.add(ui.idKey(id));
      communities.push({
        id,
        parent_community_id: p.safeId(item.parent_community_id === undefined ? item.parent_id : item.parent_community_id),
        name: p.text(item.name), summary: p.text(item.summary, 16384),
        member_count: p.count(item.member_count), level: p.integer(item.level, 0, 5, 0),
      });
    }
    const byId = new Map(communities.map(item => [ui.idKey(item.id), item]));
    for (const item of communities) {
      const parentKey = item.parent_community_id === null ? "" : ui.idKey(item.parent_community_id);
      if (!byId.has(parentKey) || parentKey === ui.idKey(item.id)) item.parent_community_id = null;
    }
    for (const item of communities) {
      const visited = new Set([ui.idKey(item.id)]);
      let cursor = item;
      while (cursor.parent_community_id !== null) {
        const key = ui.idKey(cursor.parent_community_id);
        if (visited.has(key)) {
          cursor.parent_community_id = null;
          break;
        }
        visited.add(key);
        cursor = byId.get(key);
      }
    }
    const path = [];
    for (const value of p.list(data.path, 32)) {
      const item = p.object(value);
      const id = p.safeId(item.id);
      if (id !== null) path.push({ id, name: p.text(item.name) });
    }
    return { count: p.count(data.count, communities.length), communities, path };
  }

  function finite(value, minimum, maximum) {
    return Number.isFinite(value) ? Math.min(Math.max(value, minimum), maximum) : minimum;
  }

  function hierarchy(data) {
    const root = { name: "Communities", children: [] };
    const entries = new Map(data.communities.map(item => [ui.idKey(item.id), { item, name: item.name, value: Math.max(1, item.member_count), children: [] }]));
    for (const entry of entries.values()) {
      const parent = entry.item.parent_community_id;
      const parentEntry = parent === null ? null : entries.get(ui.idKey(parent));
      (parentEntry ? parentEntry.children : root.children).push(entry);
    }
    return root;
  }

  function render(data, mount) {
    const header = ui.element("header", "daemon-app-header");
    header.append(ui.element("h1", "daemon-app-title", "Community Map"), ui.element("span", "daemon-badge", data.count + " communities"), ui.refreshButton());
    const breadcrumbs = ui.element("nav", "community-breadcrumbs");
    const rootButton = ui.element("button", "community-breadcrumb", "All Communities");
    rootButton.type = "button";
    rootButton.addEventListener("click", function () { ui.callTool(ui.actions.listCommunities.tool, {}); });
    breadcrumbs.append(rootButton);
    for (const item of data.path) {
      const button = ui.element("button", "community-breadcrumb", item.name || "Community");
      button.type = "button";
      button.addEventListener("click", function () { ui.callTool(ui.actions.listCommunities.tool, { parent_community_id: item.id }); });
      breadcrumbs.append(button);
    }
    const frame = ui.element("section", "community-frame");
    const svg = ui.svgElement("svg", "community-map");
    svg.setAttribute("viewBox", "0 0 960 540");
    svg.setAttribute("role", "img");
    svg.setAttribute("aria-label", "Memory communities");
    const details = ui.element("aside", "community-details", "Select a community for details.");
    details.hidden = true;
    if (data.communities.length && globalThis.D3) {
      const root = globalThis.D3.hierarchy(hierarchy(data)).sum(value => value.value || 0);
      globalThis.D3.treemap().size([960, 540]).padding(3)(root);
      for (const leaf of root.leaves()) {
        const item = leaf.data.item;
        if (!item) continue;
        const x = finite(leaf.x0, 0, 960);
        const y = finite(leaf.y0, 0, 540);
        const width = finite(leaf.x1 - leaf.x0, 0, 960);
        const height = finite(leaf.y1 - leaf.y0, 0, 540);
        const group = ui.svgElement("g", levelClasses[item.level]);
        group.setAttribute("transform", "translate(" + x + " " + y + ")");
        const rect = ui.svgElement("rect", "community-cell__rect");
        rect.setAttribute("width", String(width));
        rect.setAttribute("height", String(height));
        const label = ui.svgElement("text", "community-cell__label");
        label.setAttribute("x", "8");
        label.setAttribute("y", "22");
        label.textContent = item.name || "Unnamed";
        group.append(rect, label);
        group.addEventListener("click", function () {
          details.replaceChildren(
            ui.element("h2", "community-details__title", item.name || "Unnamed"),
            ui.element("p", "community-details__summary", item.summary),
            ui.element("p", "daemon-muted", item.member_count + " members")
          );
          details.hidden = false;
          ui.callTool(ui.actions.listCommunities.tool, { parent_community_id: item.id });
        });
        svg.append(group);
      }
    } else {
      const message = ui.svgElement("text", "community-empty");
      message.setAttribute("x", "480");
      message.setAttribute("y", "270");
      message.setAttribute("text-anchor", "middle");
      message.textContent = "No communities found.";
      svg.append(message);
    }
    frame.append(svg, details);
    mount.replaceChildren(header, breadcrumbs, frame);
  }

  ui.register("community", normalize, render);
})(globalThis.Daem0nUI);
