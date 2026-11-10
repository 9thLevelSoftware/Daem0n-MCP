(function (ui) {
  "use strict";
  const p = ui.primitives;
  const categoryColors = Object.freeze({ decision: "#3b82f6", warning: "#f59e0b", pattern: "#8b5cf6", learning: "#22c55e", default: "#888888" });
  const relationshipColors = Object.freeze({ led_to: "#22c55e", supersedes: "#3b82f6", conflicts_with: "#ef4444", relates_to: "#888888", depends_on: "#f59e0b" });
  const relationships = Object.freeze(["led_to", "supersedes", "conflicts_with", "relates_to", "depends_on"]);
  const categories = Object.freeze(["decision", "warning", "pattern", "learning"]);
  const progressiveThreshold = 200;
  const progressiveBatchSize = 50;

  function normalize(data) {
    const seen = new Set();
    const nodes = [];
    for (const value of p.list(data.nodes, 1000)) {
      const item = p.object(value);
      const id = p.safeId(item.id);
      if (id === null || seen.has(ui.idKey(id))) continue;
      seen.add(ui.idKey(id));
      nodes.push({
        id, content: p.text(item.content, 16384), full_content: p.text(item.full_content === undefined ? item.content : item.full_content, 16384),
        category: categories.includes(item.category) ? item.category : "default",
        tags: p.list(item.tags, 32).filter(value => typeof value === "string").map(value => p.text(value, 128)),
        created_at: p.date(item.created_at), community_id: p.safeId(item.community_id),
      });
    }
    const edges = [];
    for (const value of p.list(data.edges, 5000)) {
      const item = p.object(value);
      const source = p.safeId(item.source === undefined ? item.source_id : item.source);
      const target = p.safeId(item.target === undefined ? item.target_id : item.target);
      if (source === null || target === null || !seen.has(ui.idKey(source)) || !seen.has(ui.idKey(target))) continue;
      edges.push({ source, target, relationship: relationships.includes(item.relationship) ? item.relationship : "relates_to", confidence: p.ratio(item.confidence), description: p.text(item.description, 16384) });
    }
    const path = p.list(data.path, 1000).map(p.safeId).filter(value => value !== null && seen.has(ui.idKey(value)));
    return { topic: p.text(data.topic), nodes, edges, path };
  }

  function cross(origin, a, b) {
    return (a[0] - origin[0]) * (b[1] - origin[1]) - (a[1] - origin[1]) * (b[0] - origin[0]);
  }

  function convexHull(points) {
    const clean = points.filter(point => Array.isArray(point) && Number.isFinite(point[0]) && Number.isFinite(point[1])).map(point => [point[0], point[1]]);
    clean.sort((a, b) => a[0] - b[0] || a[1] - b[1]);
    if (clean.length < 3) return clean;
    const lower = [];
    for (const point of clean) {
      while (lower.length >= 2 && cross(lower[lower.length - 2], lower[lower.length - 1], point) <= 0) lower.pop();
      lower.push(point);
    }
    const upper = [];
    for (let index = clean.length - 1; index >= 0; index -= 1) {
      const point = clean[index];
      while (upper.length >= 2 && cross(upper[upper.length - 2], upper[upper.length - 1], point) <= 0) upper.pop();
      upper.push(point);
    }
    lower.pop();
    upper.pop();
    return lower.concat(upper);
  }

  function clamp(value, minimum, maximum, fallback) {
    return Number.isFinite(value) ? Math.min(Math.max(value, minimum), maximum) : fallback;
  }

  function render(data, mount) {
    const header = ui.element("header", "daemon-app-header");
    header.append(ui.element("h1", "daemon-app-title", data.topic ? "Memory Graph: " + data.topic : "Memory Graph"), ui.element("span", "daemon-badge", data.nodes.length + " nodes"), ui.refreshButton());
    const controls = ui.element("section", "secure-graph-controls");
    const hullToggle = ui.element("input", "graph-toggle");
    hullToggle.type = "checkbox";
    hullToggle.checked = true;
    const dates = data.nodes.map(node => Date.parse(node.created_at)).filter(Number.isFinite);
    const minimumDate = dates.length ? Math.min(...dates) : 0;
    const maximumDate = dates.length ? Math.max(...dates) : 1;
    const from = ui.element("input", "graph-range");
    const to = ui.element("input", "graph-range");
    for (const input of [from, to]) {
      input.type = "range";
      input.min = String(minimumDate);
      input.max = String(Math.max(minimumDate + 1, maximumDate));
    }
    from.value = String(minimumDate);
    to.value = String(Math.max(minimumDate + 1, maximumDate));
    const hullLabel = ui.element("label", "graph-control-label", "Community hulls");
    hullLabel.prepend(hullToggle);
    const zoomIn = ui.element("button", "daemon-btn daemon-btn--small daemon-btn--secondary", "Zoom in");
    const zoomOut = ui.element("button", "daemon-btn daemon-btn--small daemon-btn--secondary", "Zoom out");
    const reset = ui.element("button", "daemon-btn daemon-btn--small daemon-btn--secondary", "Reset view");
    for (const button of [zoomIn, zoomOut, reset]) button.type = "button";
    controls.append(hullLabel, ui.element("label", "graph-control-label", "From"), from, ui.element("label", "graph-control-label", "To"), to, zoomIn, zoomOut, reset);
    const frame = ui.element("section", "graph-frame");
    const canvas = ui.element("canvas", "graph-canvas");
    const loading = ui.element("div", "graph-loading", "Loading graph…");
    loading.setAttribute("role", "status");
    loading.setAttribute("aria-live", "polite");
    const details = ui.element("aside", "secure-graph-details");
    const detailsDismiss = ui.element("button", "daemon-btn daemon-btn--small daemon-btn--secondary graph-details__dismiss", "Close details");
    detailsDismiss.type = "button";
    const detailsContent = ui.element("div", "graph-details__body", "Select a node for details.");
    details.append(detailsDismiss, detailsContent);
    details.hidden = true;
    frame.append(canvas, loading, details);
    mount.replaceChildren(header, controls, frame);

    const context = canvas.getContext("2d");
    if (!context || !globalThis.D3) {
      loading.hidden = true;
      return;
    }
    let width = 960;
    let height = 540;
    let pixelRatio = 1;
    let zoom = 1;
    let panX = 0;
    let panY = 0;
    let minimum = Number(from.value);
    let maximum = Number(to.value);
    let pathOffset = 0;
    let pathAnimationId = null;
    let pathAnimationGeneration = 0;
    let currentPath = [];
    let pathProgress = 0;
    let simulation = null;
    let activeNodes = [];
    let activeLinks = [];
    const nodes = data.nodes.map(node => ({ ...node }));
    const pathEdges = new Set();
    for (let index = 1; index < data.path.length; index += 1) {
      pathEdges.add(ui.idKey(data.path[index - 1]) + "->" + ui.idKey(data.path[index]));
      pathEdges.add(ui.idKey(data.path[index]) + "->" + ui.idKey(data.path[index - 1]));
    }
    const links = data.edges.map(edge => ({
      ...edge,
      path_edge: pathEdges.has(ui.idKey(edge.source) + "->" + ui.idKey(edge.target)),
      source_key: ui.idKey(edge.source),
      target_key: ui.idKey(edge.target),
    }));
    const nodesById = new Map(nodes.map(node => [ui.idKey(node.id), node]));

    function visible(node) {
      const timestamp = Date.parse(node.created_at);
      return !Number.isFinite(timestamp) || (timestamp >= minimum && timestamp <= maximum);
    }

    function clampPan() {
      panX = clamp(panX, -width, width, 0);
      panY = clamp(panY, -height, height, 0);
    }

    function drawPathHighlight() {
      if (currentPath.length < 2 || currentPath.some(node => !Number.isFinite(node.x) || !Number.isFinite(node.y))) return;
      const segments = [];
      let totalLength = 0;
      for (let index = 1; index < currentPath.length; index += 1) {
        const start = currentPath[index - 1];
        const end = currentPath[index];
        const length = Math.hypot(end.x - start.x, end.y - start.y);
        segments.push({ end, length, start });
        totalLength += length;
      }
      let remaining = pathProgress * totalLength;
      let markerX = currentPath[0].x;
      let markerY = currentPath[0].y;
      context.strokeStyle = "#8b5cf6";
      context.lineWidth = 4;
      context.setLineDash([]);
      context.globalAlpha = 1;
      context.beginPath();
      context.moveTo(markerX, markerY);
      for (const segment of segments) {
        if (remaining >= segment.length) {
          markerX = segment.end.x;
          markerY = segment.end.y;
          context.lineTo(markerX, markerY);
          remaining -= segment.length;
          continue;
        }
        const ratio = segment.length > 0 ? remaining / segment.length : 1;
        markerX = segment.start.x + (segment.end.x - segment.start.x) * ratio;
        markerY = segment.start.y + (segment.end.y - segment.start.y) * ratio;
        context.lineTo(markerX, markerY);
        break;
      }
      context.stroke();
      context.fillStyle = "#8b5cf6";
      context.beginPath();
      context.arc(markerX, markerY, 5, 0, Math.PI * 2);
      context.fill();
      context.lineWidth = 1;
    }

    function draw() {
      context.setTransform(pixelRatio, 0, 0, pixelRatio, 0, 0);
      context.clearRect(0, 0, width, height);
      context.save();
      context.translate(panX, panY);
      context.scale(zoom, zoom);
      if (hullToggle.checked) {
        const groups = new Map();
        for (const node of activeNodes) {
          if (node.community_id === null || !visible(node)) continue;
          const key = ui.idKey(node.community_id);
          if (!groups.has(key)) groups.set(key, []);
          groups.get(key).push([node.x, node.y]);
        }
        context.strokeStyle = "#8b5cf6";
        context.lineWidth = 1;
        for (const points of groups.values()) {
          const hull = convexHull(points);
          if (hull.length < 3) continue;
          context.beginPath();
          context.moveTo(hull[0][0], hull[0][1]);
          for (const point of hull.slice(1)) context.lineTo(point[0], point[1]);
          context.closePath();
          context.stroke();
        }
      }
      for (const link of activeLinks) {
        if (!link.source || typeof link.source !== "object" || !link.target || typeof link.target !== "object" || !visible(link.source) || !visible(link.target)) continue;
        context.strokeStyle = relationshipColors[link.relationship];
        context.lineWidth = link.path_edge ? 3 : 1;
        context.setLineDash(link.path_edge ? [9, 5] : link.relationship === "conflicts_with" ? [5, 4] : []);
        context.lineDashOffset = link.path_edge ? pathOffset : 0;
        context.globalAlpha = clamp(link.confidence, 0.15, 1, 0.4);
        context.beginPath();
        context.moveTo(link.source.x, link.source.y);
        context.lineTo(link.target.x, link.target.y);
        context.stroke();
      }
      context.setLineDash([]);
      context.lineDashOffset = 0;
      context.lineWidth = 1;
      context.globalAlpha = 1;
      drawPathHighlight();
      for (const node of activeNodes) {
        if (!visible(node) || !Number.isFinite(node.x) || !Number.isFinite(node.y)) continue;
        context.fillStyle = categoryColors[node.category];
        context.beginPath();
        context.arc(node.x, node.y, 7, 0, Math.PI * 2);
        context.fill();
      }
      context.restore();
    }

    function resizeCanvas() {
      const bounds = frame.getBoundingClientRect();
      width = Math.round(clamp(bounds.width, 1, 4096, 960));
      height = Math.round(clamp(bounds.height, 1, 4096, 540));
      pixelRatio = clamp(globalThis.devicePixelRatio, 1, 3, 1);
      canvas.width = Math.round(width * pixelRatio);
      canvas.height = Math.round(height * pixelRatio);
      clampPan();
      if (simulation) {
        simulation.force("center", globalThis.D3.forceCenter(width / 2, height / 2));
        simulation.alpha(0.25).restart();
      }
      draw();
    }

    const progressive = nodes.length > progressiveThreshold;
    activeNodes = progressive ? [] : nodes;
    const linkForce = globalThis.D3.forceLink([]).id(node => ui.idKey(node.id)).distance(80);
    simulation = globalThis.D3.forceSimulation(activeNodes)
      .force("link", linkForce)
      .force("charge", globalThis.D3.forceManyBody().strength(-120))
      .force("center", globalThis.D3.forceCenter(width / 2, height / 2))
      .force("collision", globalThis.D3.forceCollide(14))
      .on("tick", draw);

    function rebuildLinks() {
      const activeIds = new Set(activeNodes.map(node => ui.idKey(node.id)));
      activeLinks = links
        .filter(link => activeIds.has(link.source_key) && activeIds.has(link.target_key))
        .map(link => ({ ...link, source: link.source_key, target: link.target_key }));
      linkForce.links(activeLinks);
    }

    rebuildLinks();
    resizeCanvas();
    loading.hidden = !progressive;

    if (typeof globalThis.ResizeObserver === "function") {
      const observer = new globalThis.ResizeObserver(resizeCanvas);
      observer.observe(frame);
    } else if (typeof globalThis.addEventListener === "function") {
      globalThis.addEventListener("resize", resizeCanvas);
    }

    function scheduleBatch() {
      if (typeof globalThis.requestIdleCallback === "function") {
        globalThis.requestIdleCallback(loadBatch, { timeout: 100 });
      } else {
        globalThis.setTimeout(loadBatch, 0);
      }
    }

    function loadBatch() {
      const nextLength = Math.min(activeNodes.length + progressiveBatchSize, nodes.length);
      activeNodes = nodes.slice(0, nextLength);
      simulation.nodes(activeNodes);
      rebuildLinks();
      simulation.alpha(0.5).restart();
      draw();
      if (nextLength < nodes.length) scheduleBatch();
      else loading.hidden = true;
    }

    if (progressive) scheduleBatch();

    function cancelPathAnimation() {
      pathAnimationGeneration += 1;
      if (pathAnimationId !== null && typeof globalThis.cancelAnimationFrame === "function") {
        globalThis.cancelAnimationFrame(pathAnimationId);
      }
      pathAnimationId = null;
    }

    function clearPath() {
      cancelPathAnimation();
      currentPath = [];
      pathProgress = 0;
      draw();
    }

    function showPath(value) {
      cancelPathAnimation();
      const request = p.object(value);
      const resolved = [];
      for (const entry of p.list(request.path, 1000)) {
        const id = p.safeId(p.object(entry).id);
        const node = id === null ? null : nodesById.get(ui.idKey(id));
        if (node) resolved.push(node);
      }
      currentPath = resolved.length >= 2 ? resolved : [];
      pathProgress = 0;
      if (currentPath.length < 2) {
        draw();
        return;
      }
      const duration = typeof request.duration === "number" && Number.isFinite(request.duration)
        ? Math.min(Math.max(request.duration, 0), 10000)
        : 2000;
      if (typeof globalThis.requestAnimationFrame !== "function") {
        pathProgress = 1;
        draw();
        return;
      }
      const generation = pathAnimationGeneration;
      let started = null;
      function animate(timestamp) {
        if (generation !== pathAnimationGeneration) return;
        if (started === null) started = timestamp;
        pathProgress = duration === 0 ? 1 : Math.min(Math.max(timestamp - started, 0) / duration, 1);
        draw();
        if (pathProgress < 1) pathAnimationId = globalThis.requestAnimationFrame(animate);
        else pathAnimationId = null;
      }
      pathAnimationId = globalThis.requestAnimationFrame(animate);
    }

    const messenger = globalThis.SecureMessenger;
    if (messenger && typeof messenger.on === "function") {
      messenger.on("show_path", showPath);
      messenger.on("clear_path", clearPath);
    }

    function updateRange() {
      minimum = Math.min(Number(from.value), Number(to.value));
      maximum = Math.max(Number(from.value), Number(to.value));
      draw();
    }
    from.addEventListener("input", updateRange);
    to.addEventListener("input", updateRange);
    hullToggle.addEventListener("change", draw);
    function updateZoom(value, point) {
      const anchor = point || { x: width / 2, y: height / 2 };
      const nextZoom = clamp(value, 0.1, 4, 1);
      const worldX = (anchor.x - panX) / zoom;
      const worldY = (anchor.y - panY) / zoom;
      panX = anchor.x - worldX * nextZoom;
      panY = anchor.y - worldY * nextZoom;
      zoom = nextZoom;
      clampPan();
      draw();
    }

    canvas.addEventListener("wheel", function (event) {
      event.preventDefault();
      updateZoom(zoom * (event.deltaY < 0 ? 1.1 : 0.9), screenPoint(event));
    });

    function screenPoint(event) {
      const rect = canvas.getBoundingClientRect();
      if (!(rect.width > 0) || !(rect.height > 0)) return null;
      const x = (Number(event.clientX) - rect.left) * width / rect.width;
      const y = (Number(event.clientY) - rect.top) * height / rect.height;
      return Number.isFinite(x) && Number.isFinite(y) ? { x, y } : null;
    }

    let drag = null;
    let suppressClick = false;
    canvas.addEventListener("pointerdown", function (event) {
      if (event.button !== 0) return;
      drag = { pointerId: event.pointerId, x: event.clientX, y: event.clientY, moved: false };
      canvas.setPointerCapture(event.pointerId);
    });
    canvas.addEventListener("pointermove", function (event) {
      if (!drag || drag.pointerId !== event.pointerId) return;
      const rect = canvas.getBoundingClientRect();
      if (!(rect.width > 0) || !(rect.height > 0)) return;
      const deltaX = (event.clientX - drag.x) * width / rect.width;
      const deltaY = (event.clientY - drag.y) * height / rect.height;
      if (Math.abs(deltaX) + Math.abs(deltaY) > 2) drag.moved = true;
      drag.x = event.clientX;
      drag.y = event.clientY;
      panX += deltaX;
      panY += deltaY;
      clampPan();
      draw();
    });
    function finishDrag(event) {
      if (!drag || drag.pointerId !== event.pointerId) return;
      suppressClick = drag.moved;
      canvas.releasePointerCapture(event.pointerId);
      drag = null;
    }
    canvas.addEventListener("pointerup", finishDrag);
    canvas.addEventListener("pointercancel", finishDrag);

    zoomIn.addEventListener("click", function () { updateZoom(zoom * 1.25); });
    zoomOut.addEventListener("click", function () { updateZoom(zoom * 0.8); });
    reset.addEventListener("click", function () {
      zoom = 1;
      panX = 0;
      panY = 0;
      draw();
    });

    canvas.addEventListener("click", function (event) {
      if (suppressClick) {
        suppressClick = false;
        return;
      }
      const point = screenPoint(event);
      if (!point) return;
      const x = (point.x - panX) / zoom;
      const y = (point.y - panY) / zoom;
      let selected = null;
      let distance = 24 / zoom;
      for (const node of activeNodes) {
        if (!visible(node) || !Number.isFinite(node.x) || !Number.isFinite(node.y)) continue;
        const candidate = Math.hypot(node.x - x, node.y - y);
        if (candidate < distance) { selected = node; distance = candidate; }
      }
      if (!selected) return;
      const tagList = ui.element("ul", "graph-details__tags");
      for (const tag of selected.tags) tagList.append(ui.element("li", "graph-details__tag", tag));
      const focus = ui.element("button", "daemon-btn daemon-btn--small", "Focus node");
      focus.type = "button";
      focus.addEventListener("click", function () { ui.callTool(ui.actions.graphFocus.tool, { record_ids: [selected.id], include_orphans: true }); });
      detailsContent.replaceChildren(ui.element("h2", "graph-details__title", selected.category), ui.element("p", "graph-details__content", selected.full_content), tagList, focus);
      details.hidden = false;
    });
    detailsDismiss.addEventListener("click", function () {
      details.hidden = true;
      detailsContent.replaceChildren(ui.element("p", "daemon-muted", "Select a node for details."));
    });
    if (pathEdges.size && typeof globalThis.requestAnimationFrame === "function") {
      const started = Date.now();
      function animatePath() {
        pathOffset = -((Date.now() - started) / 80) % 14;
        draw();
        if (Date.now() - started < 10000) globalThis.requestAnimationFrame(animatePath);
      }
      globalThis.requestAnimationFrame(animatePath);
    }
    globalThis.setTimeout(function () { simulation.alphaTarget(0); }, 10000);
  }

  ui.register("graph", normalize, render);
})(globalThis.Daem0nUI);
