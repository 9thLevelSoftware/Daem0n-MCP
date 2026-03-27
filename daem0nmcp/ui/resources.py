"""
UI Resource Registration for MCP Apps

Registers HTML UI resources with FastMCP using the ui:// URI scheme
and text/html;profile=mcp-app MIME type.

This module provides:
- Template loading from templates/ directory
- Asset injection (CSS and JavaScript)
- Resource registration via register_ui_resources(mcp)

Resource registrations:
- ui://daem0n/test - Test UI for infrastructure validation (06-03)
- ui://daem0n/search - Search results UI (Phase 7)
- ui://daem0n/briefing - Briefing dashboard (Phase 8)
- ui://daem0n/covenant - Covenant status dashboard (Phase 9)
- ui://daem0n/community - Community cluster map (Phase 10)
- ui://daem0n/graph - Memory graph viewer (Phase 11)
"""

import json
import re
from datetime import datetime
from html import escape as _html_escape
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from fastmcp import FastMCP

# MIME type for MCP Apps (SEP-1865)
# Signals to MCP hosts that this resource should be rendered as HTML UI
MCP_APPS_MIME = "text/html;profile=mcp-app"

# Directory paths for template and static assets
_UI_DIR = Path(__file__).parent
TEMPLATES_DIR = _UI_DIR / "templates"
STATIC_DIR = _UI_DIR / "static"
BUILD_DIR = _UI_DIR / "build"


def _load_template(name: str) -> str:
    """Load an HTML template from the templates directory."""
    template_path = TEMPLATES_DIR / name
    if not template_path.exists():
        raise FileNotFoundError(f"Template not found: {name}")
    return template_path.read_text(encoding="utf-8")


def _load_static(name: str) -> str:
    """Load a static asset from the static directory."""
    static_path = STATIC_DIR / name
    if not static_path.exists():
        return ""  # Return empty if asset doesn't exist yet
    return static_path.read_text(encoding="utf-8")


def _inject_assets(html: str, include_d3: bool = False) -> str:
    """
    Inject CSS and JavaScript assets into HTML template.

    Replaces:
    - {{CSS}} with daemon.css contents
    - {{SCRIPT}} with D3 bundle (if include_d3=True) + custom script
    """
    css = _load_static("daemon.css")
    html = html.replace("{{CSS}}", css)

    if include_d3:
        d3_bundle = _load_static("d3.bundle.js")
        html = html.replace("{{SCRIPT}}", d3_bundle)
    else:
        html = html.replace("{{SCRIPT}}", "// No D3 required for this template")

    return html


def _highlight_keywords(text: str, query: str) -> str:
    """
    Wrap query keywords in <mark> tags for highlighting.

    HTML-escapes the text first to prevent XSS, then applies
    safe highlighting with <mark> tags.

    Args:
        text: The text content to highlight within
        query: Space-separated query words

    Returns:
        HTML-escaped text with matching keywords wrapped in <mark> tags
    """
    if not query or not text:
        return _html_escape(text) if text else text

    # Escape HTML entities first to prevent injection
    result = _html_escape(text)

    words = query.split()
    for word in words:
        if len(word) > 2:  # Skip short words
            escaped_word = _html_escape(word)
            pattern = re.compile(re.escape(escaped_word), re.IGNORECASE)
            result = pattern.sub(f"<mark>{escaped_word}</mark>", result)
    return result


def _format_date(date_str: str) -> str:
    """Format an ISO date string for display."""
    try:
        dt = datetime.fromisoformat(date_str.replace("Z", "+00:00"))
        return dt.strftime("%Y-%m-%d %H:%M")
    except (ValueError, AttributeError):
        return str(date_str) if date_str else ""


def _outcome_indicator(worked: bool | None) -> str:
    """Return badge HTML for decision outcome status."""
    if worked is True:
        return '<span class="daemon-badge daemon-badge--success">Success</span>'
    elif worked is False:
        return '<span class="daemon-badge daemon-badge--error">Failed</span>'
    else:
        return '<span class="daemon-badge">Pending</span>'


def _build_search_ui(data: dict[str, Any]) -> str:
    """
    Build the search results UI HTML from recall data.

    Args:
        data: Recall output containing:
            - topic: Search query string
            - decisions: List of decision results
            - patterns: List of pattern results
            - warnings: List of warning results
            - learnings: List of learning results
            - total_count: Total results count
            - offset: Pagination offset
            - limit: Pagination limit
            - has_more: Whether more results exist

    Returns:
        Complete HTML string for the search results UI
    """
    template = _load_template("search.html")

    topic = data.get("topic", "")
    decisions = data.get("decisions", [])
    patterns = data.get("patterns", [])
    warnings = data.get("warnings", [])
    learnings = data.get("learnings", [])

    # Calculate totals
    total_results = len(decisions) + len(patterns) + len(warnings) + len(learnings)
    total_count = data.get("total_count", total_results)

    # Build result cards
    cards_html = []

    # Helper to render a single card
    def render_card(result: dict[str, Any], category: str) -> str:
        content = result.get("content", "")
        highlighted_content = _highlight_keywords(content, topic)
        relevance = result.get("relevance", result.get("score", 0))
        semantic_match = result.get(
            "semantic_match", relevance
        )  # Fallback to relevance if not provided
        recency_weight = result.get(
            "recency_weight", 1.0
        )  # Default to 1.0 if not provided
        score_width = int(relevance * 100) if relevance <= 1 else int(relevance)
        created_at = _format_date(result.get("created_at", ""))
        tags = result.get("tags", [])
        memory_id = result.get("id")
        worked = result.get("worked")

        tags_html = ""
        if tags:
            tag_spans = [
                f'<span class="result-card__tag">{_html_escape(str(tag))}</span>'
                for tag in tags
            ]
            tags_html = f'<div class="result-card__tags">{" ".join(tag_spans)}</div>'

        # Format percentages for display
        semantic_pct = (
            f"{semantic_match:.0%}" if semantic_match <= 1 else f"{semantic_match}%"
        )
        recency_pct = (
            f"{recency_weight:.0%}" if recency_weight <= 1 else f"{recency_weight}%"
        )
        relevance_pct = f"{relevance:.0%}" if relevance <= 1 else f"{relevance}%"

        # Record Outcome button for decisions without outcome yet
        actions_html = ""
        if category == "decision" and worked is None and memory_id is not None:
            actions_html = f'''
    <div class="daemon-card__actions">
        <button class="daemon-btn daemon-btn--small daemon-btn--secondary"
                data-action="record-outcome"
                data-memory-id="{memory_id}">
            Record Outcome
        </button>
    </div>'''

        return f'''
<div class="daemon-card result-card" data-category="{category}">
    <div class="result-card__header">
        <span class="daemon-badge daemon-badge--{category}">{category}</span>
        <div class="daemon-score">
            <span class="daemon-score__value">{relevance:.2f}</span>
            <span class="daemon-score__label">relevance</span>
        </div>
    </div>
    <div class="daemon-score-bar">
        <div class="daemon-score-bar__track">
            <div class="daemon-score-bar__fill" style="width: {score_width}%;"></div>
        </div>
    </div>
    <div class="result-card__content">{highlighted_content}</div>
    <details class="daemon-score-breakdown">
        <summary>Score breakdown</summary>
        <div class="daemon-score-components">
            <div class="daemon-score-component">
                <span class="daemon-score-component__label">Semantic match:</span>
                <span class="daemon-score-component__value">{semantic_pct}</span>
            </div>
            <div class="daemon-score-component">
                <span class="daemon-score-component__label">Recency weight:</span>
                <span class="daemon-score-component__value">{recency_pct}</span>
            </div>
            <div class="daemon-score-component">
                <span class="daemon-score-component__label">Final relevance:</span>
                <span class="daemon-score-component__value">{relevance_pct}</span>
            </div>
        </div>
    </details>
    <div class="result-card__meta">
        <span class="result-card__date">{created_at}</span>
        {tags_html}
    </div>{actions_html}
</div>'''

    # Render all categories
    for result in decisions:
        cards_html.append(render_card(result, "decision"))
    for result in warnings:
        cards_html.append(render_card(result, "warning"))
    for result in patterns:
        cards_html.append(render_card(result, "pattern"))
    for result in learnings:
        cards_html.append(render_card(result, "learning"))

    # Handle empty state
    if not cards_html:
        cards_html.append("""
<div class="daemon-empty" style="grid-column: 1 / -1;">
    <div class="daemon-empty__icon">&#x1F50D;</div>
    <p class="daemon-empty__title">No results found</p>
    <p class="daemon-empty__description">
        No memories matched your search query. Try different keywords or broaden your search.
    </p>
</div>""")

    # Pagination data
    offset = data.get("offset", 0)
    limit = data.get("limit", 10)
    has_more = data.get("has_more", False)

    # Build result count text
    if total_count == total_results:
        result_count_text = (
            f"Showing {total_results} result{'s' if total_results != 1 else ''}"
        )
    else:
        result_count_text = f"Showing {total_results} of {total_count} results"

    # Build pagination HTML (only if needed)
    pagination_html = ""
    if has_more or offset > 0:
        # Calculate display range
        range_start = offset + 1
        range_end = offset + total_results

        prev_disabled = "disabled" if offset == 0 else ""
        next_disabled = "disabled" if not has_more else ""

        pagination_html = f'''
            <div class="daemon-pagination" data-offset="{offset}" data-limit="{limit}">
                <button class="daemon-pagination__btn" data-action="prev" {prev_disabled}>
                    Previous
                </button>
                <span class="daemon-pagination__info">
                    Showing {range_start}-{range_end} of {total_count}
                </span>
                <button class="daemon-pagination__btn" data-action="next" {next_disabled}>
                    Next
                </button>
            </div>'''

    # Inject into template
    html = template.replace(
        "{{TITLE}}", f"Search: {topic}" if topic else "Search Results"
    )
    html = html.replace("{{TOPIC}}", topic if topic else "All")
    html = html.replace("{{RESULT_COUNT}}", result_count_text)
    html = html.replace("{{CONTENT}}", "\n".join(cards_html))
    html = html.replace("{{PAGINATION}}", pagination_html)
    html = _inject_assets(html, include_d3=False)

    return html


def _build_briefing_ui(data: dict[str, Any]) -> str:
    """
    Build the briefing dashboard UI HTML from get_briefing data.

    Args:
        data: get_briefing output containing:
            - status: "ready" or error
            - statistics: dict with memory counts, outcome rates
            - recent_decisions: list of recent decisions
            - active_warnings: list of active warnings
            - failed_approaches: list of failed approaches
            - git_changes: dict with added/modified/deleted counts and files
            - focus_areas: optional list of pre-fetched focus memories
            - message: actionable briefing message

    Returns:
        Complete HTML string for the briefing dashboard UI
    """
    template = _load_template("briefing.html")

    status = data.get("status", "unknown")
    statistics = data.get("statistics", {})
    recent_decisions = data.get("recent_decisions", [])
    active_warnings = data.get("active_warnings", [])
    failed_approaches = data.get("failed_approaches", [])
    git_changes = data.get("git_changes", {})
    focus_areas = data.get("focus_areas")
    message = data.get("message", "")

    # Build statistics panel
    total_memories = statistics.get("total_memories", 0)
    decisions_count = statistics.get("by_category", {}).get("decision", 0)
    warnings_count = statistics.get("by_category", {}).get("warning", 0)
    patterns_count = statistics.get("by_category", {}).get("pattern", 0)
    success_rate = statistics.get("outcome_rates", {}).get("success_rate", 0)

    stats_html = f"""
<div class="daemon-stats">
    <div class="daemon-stat">
        <div class="daemon-stat__value">{total_memories}</div>
        <div class="daemon-stat__label">Total Memories</div>
    </div>
    <div class="daemon-stat">
        <div class="daemon-stat__value">{decisions_count}</div>
        <div class="daemon-stat__label">Decisions</div>
    </div>
    <div class="daemon-stat">
        <div class="daemon-stat__value">{warnings_count}</div>
        <div class="daemon-stat__label">Warnings</div>
    </div>
    <div class="daemon-stat">
        <div class="daemon-stat__value">{patterns_count}</div>
        <div class="daemon-stat__label">Patterns</div>
    </div>
    <div class="daemon-stat daemon-stat--success">
        <div class="daemon-stat__value">{success_rate:.0%}</div>
        <div class="daemon-stat__label">Success Rate</div>
    </div>
</div>"""

    # Build message block (if present)
    message_html = ""
    if message:
        message_html = f'<div class="briefing-message">{_html_escape(message)}</div>'

    # Build accordion sections
    sections = []

    # Recent Decisions section
    if recent_decisions:
        decisions_html = []
        for d in recent_decisions:
            outcome = _outcome_indicator(d.get("worked"))
            content = _html_escape(d.get("content", "")[:200])  # Truncate for display
            date = _format_date(d.get("created_at", ""))
            decisions_html.append(f"""
<div class="decision-item">
    <div class="decision-item__header">
        {outcome}
        <span class="decision-item__meta">{date}</span>
    </div>
    <div class="decision-item__content">{content}</div>
</div>""")

        sections.append(f"""
<details class="daemon-accordion__item" open>
    <summary class="daemon-accordion__header">
        <span class="daemon-accordion__title">
            Recent Decisions
            <span class="daemon-accordion__count">({len(recent_decisions)})</span>
        </span>
        <span class="daemon-accordion__icon">&#9654;</span>
    </summary>
    <div class="daemon-accordion__content">
        {"".join(decisions_html)}
    </div>
</details>""")

    # Active Warnings section
    if active_warnings:
        warnings_html = []
        for w in active_warnings:
            severity = w.get("severity", "medium").lower()
            content = _html_escape(w.get("content", ""))
            warnings_html.append(f"""
<div class="warning-item warning-item--{severity}">
    <div class="warning-item__content">{content}</div>
</div>""")

        sections.append(f"""
<details class="daemon-accordion__item" open>
    <summary class="daemon-accordion__header">
        <span class="daemon-accordion__title">
            Active Warnings
            <span class="daemon-accordion__count">({len(active_warnings)})</span>
        </span>
        <span class="daemon-accordion__icon">&#9654;</span>
    </summary>
    <div class="daemon-accordion__content">
        {"".join(warnings_html)}
    </div>
</details>""")

    # Failed Approaches section
    if failed_approaches:
        failed_html = []
        for f in failed_approaches:
            content = _html_escape(f.get("content", ""))
            failed_html.append(f"""
<div class="warning-item warning-item--high">
    <div class="warning-item__content">{content}</div>
</div>""")

        sections.append(f"""
<details class="daemon-accordion__item">
    <summary class="daemon-accordion__header">
        <span class="daemon-accordion__title">
            Failed Approaches
            <span class="daemon-accordion__count">({len(failed_approaches)})</span>
        </span>
        <span class="daemon-accordion__icon">&#9654;</span>
    </summary>
    <div class="daemon-accordion__content">
        {"".join(failed_html)}
    </div>
</details>""")

    # Git Changes section
    git_files = git_changes.get("files", [])
    if git_files:
        changes_html = []
        for file_info in git_files[:20]:  # Limit to 20 files
            status_char = file_info.get("status", "M")
            path = file_info.get("path", "")
            css_class = {
                "A": "git-change--added",
                "M": "git-change--modified",
                "D": "git-change--deleted",
            }.get(status_char, "")
            changes_html.append(
                f'<div class="git-change {css_class}">{status_char} {path}</div>'
            )

        total_changes = git_changes.get("total", len(git_files))
        sections.append(f"""
<details class="daemon-accordion__item">
    <summary class="daemon-accordion__header">
        <span class="daemon-accordion__title">
            Git Changes
            <span class="daemon-accordion__count">({total_changes} files)</span>
        </span>
        <span class="daemon-accordion__icon">&#9654;</span>
    </summary>
    <div class="daemon-accordion__content">
        {"".join(changes_html)}
    </div>
</details>""")

    # Focus Areas section (quick-access buttons)
    if focus_areas:
        buttons_html = []
        for area in focus_areas:
            topic = _html_escape(area.get("topic", ""))
            buttons_html.append(f'''
<button class="daemon-btn daemon-btn--small daemon-btn--secondary focus-area-btn"
        data-action="focus-area"
        data-topic="{topic}">
    {topic}
</button>''')

        sections.append(f"""
<details class="daemon-accordion__item" open>
    <summary class="daemon-accordion__header">
        <span class="daemon-accordion__title">
            Focus Areas
            <span class="daemon-accordion__count">({len(focus_areas)})</span>
        </span>
        <span class="daemon-accordion__icon">&#9654;</span>
    </summary>
    <div class="daemon-accordion__content">
        {"".join(buttons_html)}
    </div>
</details>""")

    # Inject into template
    html = template.replace("{{TITLE}}", "Session Briefing")
    html = html.replace("{{STATUS}}", status.upper())
    html = html.replace("{{STATS}}", stats_html)
    html = html.replace("{{MESSAGE}}", message_html)
    html = html.replace(
        "{{CONTENT}}",
        "\n".join(sections)
        if sections
        else '<p class="daemon-muted">No active context.</p>',
    )
    html = _inject_assets(html, include_d3=False)

    return html


def _build_community_ui(data: dict[str, Any]) -> str:
    """
    Build the community cluster map UI HTML from list_communities data.

    Args:
        data: list_communities output containing:
            - count: Total community count
            - communities: List of community dicts with id, name, summary, member_count, level
            - path: Optional current navigation path (for drill-down)

    Returns:
        Complete HTML string for the community cluster map UI
    """
    template = _load_template("community.html")

    communities = data.get("communities", [])
    count = data.get("count", len(communities))
    path = data.get("path", [])

    # Transform flat communities list into hierarchical structure for D3
    # Root node contains all top-level communities as children
    def build_hierarchy(community_list: list) -> dict[str, Any]:
        """Build hierarchy from flat list, grouping by parent_community_id."""
        # Index by id for quick lookup
        by_id: dict[int, dict[str, Any]] = {}
        for c in community_list:
            by_id[c.get("id")] = {
                "id": c.get("id"),
                "name": c.get("name", f"Community {c.get('id')}"),
                "summary": c.get("summary", ""),
                "member_count": c.get("member_count", 0),
                "level": c.get("level", 0),
                "children": [],
            }

        # Build parent-child relationships
        roots = []
        for c in community_list:
            cid = c.get("id")
            parent_id = c.get("parent_community_id")
            node = by_id.get(cid)
            if node:
                if parent_id and parent_id in by_id:
                    by_id[parent_id]["children"].append(node)
                else:
                    roots.append(node)

        # Clean up empty children arrays for leaf nodes
        for node in by_id.values():
            if not node["children"]:
                del node["children"]

        return {
            "name": "All Communities",
            "children": roots,
        }

    hierarchy = build_hierarchy(communities)

    # Build breadcrumb HTML
    breadcrumb_items = []
    if not path:
        # At root level
        breadcrumb_items.append(
            '<span class="treemap-breadcrumb__item treemap-breadcrumb__item--current" aria-current="page">All Communities</span>'
        )
    else:
        # Has navigation path
        breadcrumb_items.append(
            '<span class="treemap-breadcrumb__item" data-id="root">All Communities</span>'
        )
        for i, item in enumerate(path):
            breadcrumb_items.append(
                '<span class="treemap-breadcrumb__separator">/</span>'
            )
            if i == len(path) - 1:
                # Current (last) item
                breadcrumb_items.append(
                    f'<span class="treemap-breadcrumb__item treemap-breadcrumb__item--current" aria-current="page">{item.get("name", "")}</span>'
                )
            else:
                # Clickable ancestor
                breadcrumb_items.append(
                    f'<span class="treemap-breadcrumb__item" data-id="{item.get("id", "")}">{item.get("name", "")}</span>'
                )

    breadcrumb_html = "\n                ".join(breadcrumb_items)

    # JSON-encode hierarchical data for D3
    treemap_data_json = json.dumps(hierarchy)
    current_path_json = json.dumps(path)

    # Inject into template
    html = template.replace("{{TITLE}}", "Community Cluster Map")
    html = html.replace("{{COMMUNITY_COUNT}}", str(count))
    html = html.replace("{{BREADCRUMB}}", breadcrumb_html)
    html = html.replace("{{TREEMAP_DATA}}", treemap_data_json)
    html = html.replace("{{CURRENT_PATH}}", current_path_json)
    html = _inject_assets(html, include_d3=True)

    return html


def _build_graph_ui(data: dict[str, Any]) -> str:
    """
    Build the memory graph viewer UI HTML from get_graph data.

    Args:
        data: get_graph output containing:
            - nodes: List of node dicts with id, content, category, tags, created_at
            - edges: List of edge dicts with source_id, target_id, relationship, confidence
            - node_count: Number of nodes
            - edge_count: Number of edges
            - topic: Optional search topic
            - path: Optional trace_chain path to animate

    Returns:
        Complete HTML string for the memory graph viewer UI
    """
    template = _load_template("graph.html")

    nodes = data.get("nodes", [])
    edges = data.get("edges", [])
    node_count = data.get("node_count", len(nodes))
    edge_count = data.get("edge_count", len(edges))
    topic = data.get("topic", "")
    path = data.get("path")  # Optional path for animation

    # Transform edge format for D3 (source/target instead of source_id/target_id)
    d3_edges = []
    for edge in edges:
        d3_edges.append(
            {
                "source": edge.get("source_id") or edge.get("source"),
                "target": edge.get("target_id") or edge.get("target"),
                "relationship": edge.get("relationship", "relates_to"),
                "confidence": edge.get("confidence", 1.0),
                "description": edge.get("description", ""),
            }
        )

    # Add full_content to nodes for details panel
    for node in nodes:
        if "full_content" not in node:
            node["full_content"] = node.get("content", "")

    graph_data = {
        "nodes": nodes,
        "edges": d3_edges,
    }

    # Calculate date range for temporal slider
    dates = [n.get("created_at") for n in nodes if n.get("created_at")]
    min_date = min(dates) if dates else ""
    max_date = max(dates) if dates else ""

    # JSON encode graph data for template
    graph_data_json = json.dumps(graph_data)
    path_json = json.dumps(path) if path else "null"

    # Build title
    title = f"Memory Graph: {topic}" if topic else "Memory Graph"

    # Inject into template
    html = template.replace("{{TITLE}}", title)
    html = html.replace("{{NODE_COUNT}}", str(node_count))
    html = html.replace("{{EDGE_COUNT}}", str(edge_count))
    html = html.replace("{{GRAPH_DATA}}", graph_data_json)
    html = html.replace("{{PATH_DATA}}", path_json)
    html = html.replace("{{MIN_DATE}}", min_date)
    html = html.replace("{{MAX_DATE}}", max_date)
    html = _inject_assets(html, include_d3=True)

    return html


def _build_covenant_ui(data: dict[str, Any]) -> str:
    """
    Build the covenant status dashboard UI HTML from get_covenant_status data.

    Args:
        data: get_covenant_status output containing:
            - phase: Current covenant phase (commune/counsel/inscribe/seal)
            - phase_label: Display label
            - phase_description: Description text
            - preflight: Dict with status, expires_at, remaining_seconds
            - can_mutate: Whether mutations are allowed
            - message: Status message

    Returns:
        Complete HTML string for the covenant status dashboard
    """
    template = _load_template("covenant.html")

    phase = data.get("phase", "commune")
    phase_label = data.get("phase_label", "COMMUNE")
    phase_description = data.get("phase_description", "")
    preflight = data.get("preflight", {})
    message = data.get("message", "")

    # Token status
    token_status = preflight.get("status", "none")
    token_status_labels = {
        "valid": "VALID",
        "expired": "EXPIRED",
        "none": "NONE",
    }
    token_status_label = token_status_labels.get(token_status, "NONE")

    # Token remaining time
    remaining = preflight.get("remaining_seconds")
    if remaining is not None and remaining > 0:
        minutes = remaining // 60
        seconds = remaining % 60
        token_remaining = f"{minutes}:{seconds:02d}"
        countdown_visibility = ""
    else:
        token_remaining = "0:00"
        countdown_visibility = "display: none;"

    # Token expires_at for JS countdown
    token_expires_at = preflight.get("expires_at") or ""

    # Phase info panel
    phase_info_html = f"""
    <div class="covenant-phase-info">
        <div class="covenant-phase-info__label">{phase_label}</div>
        <div class="covenant-phase-info__description">{phase_description}</div>
        <p style="margin-top: var(--daemon-space-sm); color: var(--daemon-text-muted);">{message}</p>
    </div>
    """

    # Inject into template
    html = template.replace("{{TITLE}}", "Covenant Status")
    html = html.replace("{{STATUS}}", phase_label)
    html = html.replace("{{CURRENT_PHASE}}", phase)
    # Token panel individual slots (template has inline HTML, not {{TOKEN_PANEL}} slot)
    html = html.replace("{{TOKEN_STATUS}}", token_status)
    html = html.replace("{{TOKEN_STATUS_LABEL}}", token_status_label)
    html = html.replace("{{TOKEN_REMAINING}}", token_remaining)
    html = html.replace("{{TOKEN_EXPIRES_AT}}", token_expires_at)
    html = html.replace("{{COUNTDOWN_VISIBILITY}}", countdown_visibility)
    html = html.replace("{{PHASE_INFO}}", phase_info_html)
    html = _inject_assets(html, include_d3=True)  # Include D3 for transitions

    return html


def _build_test_ui() -> str:
    """Build a test UI to validate infrastructure works."""
    base = _load_template("base.html")

    # Simple test content using daemon theme classes
    content = """
<div class="daemon-card" style="padding: 20px; text-align: center;">
    <h1 style="color: var(--daemon-accent);">Daem0n UI Infrastructure</h1>
    <p style="color: var(--daemon-text-muted);">
        If you can see this styled card, the MCP Apps infrastructure is working.
    </p>
    <div class="daemon-badge" style="display: inline-block;">
        INFRA-03 Validated
    </div>
</div>
"""

    html = base.replace("{{TITLE}}", "Daem0n Test UI")
    html = html.replace("{{CONTENT}}", content)
    html = _inject_assets(html, include_d3=False)

    return html


def register_ui_resources(mcp: "FastMCP") -> None:
    """
    Register all UI resources with the FastMCP instance.

    Called during server initialization to make UI resources available
    via the ui:// URI scheme.

    Args:
        mcp: The FastMCP server instance to register resources on.
    """

    @mcp.resource(
        uri="ui://daem0n/test",
        name="Test UI",
        description="Test UI to validate MCP Apps infrastructure",
        mime_type=MCP_APPS_MIME,
    )
    def get_test_ui() -> str:
        """Serve the test UI template."""
        return _build_test_ui()

    @mcp.resource(
        uri="ui://daem0n/search/{data}",
        name="Search Results",
        description="Visual search results with filtering and score insights",
        mime_type=MCP_APPS_MIME,
    )
    def get_search_ui(data: str) -> str:
        """
        Render search results as visual cards.

        Args:
            data: JSON string containing recall output with topic,
                  decisions, patterns, warnings, learnings, etc.

        Returns:
            Complete HTML for the search results UI
        """
        parsed = json.loads(data) if data else {}
        return _build_search_ui(parsed)

    @mcp.resource(
        uri="ui://daem0n/briefing/{data}",
        name="Session Briefing",
        description="Briefing dashboard with accordion sections for session context",
        mime_type=MCP_APPS_MIME,
    )
    def get_briefing_ui(data: str) -> str:
        """
        Render briefing data as visual dashboard.

        Args:
            data: JSON string containing get_briefing output

        Returns:
            Complete HTML for the briefing dashboard UI
        """
        parsed = json.loads(data) if data else {}
        return _build_briefing_ui(parsed)

    @mcp.resource(
        uri="ui://daem0n/covenant/{data}",
        name="Covenant Status",
        description="Sacred Covenant state machine dashboard with token countdown",
        mime_type=MCP_APPS_MIME,
    )
    def get_covenant_ui(data: str) -> str:
        """
        Render covenant status as visual dashboard.

        Args:
            data: JSON string containing get_covenant_status output

        Returns:
            Complete HTML for the covenant status dashboard
        """
        parsed = json.loads(data) if data else {}
        return _build_covenant_ui(parsed)

    @mcp.resource(
        uri="ui://daem0n/community/{data}",
        name="Community Cluster Map",
        description="Interactive treemap visualization of Leiden communities",
        mime_type=MCP_APPS_MIME,
    )
    def get_community_ui(data: str) -> str:
        """
        Render community data as visual treemap.

        Args:
            data: JSON string containing list_communities output with
                  count, communities, and optional path

        Returns:
            Complete HTML for the community cluster map UI
        """
        parsed = json.loads(data) if data else {}
        return _build_community_ui(parsed)

    @mcp.resource(
        uri="ui://daem0n/graph/{data}",
        name="Memory Graph",
        description="Interactive force-directed graph visualization of memory relationships",
        mime_type=MCP_APPS_MIME,
    )
    def get_graph_ui(data: str) -> str:
        """
        Render memory graph as interactive visualization.

        Args:
            data: JSON string containing get_graph output with
                  nodes, edges, and optional path data

        Returns:
            Complete HTML for the memory graph viewer UI
        """
        parsed = json.loads(data) if data else {}
        return _build_graph_ui(parsed)


__all__ = [
    "MCP_APPS_MIME",
    "TEMPLATES_DIR",
    "STATIC_DIR",
    "BUILD_DIR",
    "register_ui_resources",
    "_build_search_ui",
    "_build_briefing_ui",
    "_build_covenant_ui",
    "_build_community_ui",
    "_build_graph_ui",
    "_highlight_keywords",
]
