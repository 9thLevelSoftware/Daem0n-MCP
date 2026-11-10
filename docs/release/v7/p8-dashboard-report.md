# P8 dashboard resource report

The v7 manifest now registers exactly six static MCP App shell resources:
`ui://daem0n/test`, `ui://daem0n/search`, `ui://daem0n/briefing`,
`ui://daem0n/covenant`, `ui://daem0n/community`, and `ui://daem0n/graph`.
They are generated from `ui.rendering.APP_SPECS`, have
`text/html;profile=mcp-app` MIME types, and contain no workspace data. The
four existing `memory://workspaces/{workspace_id}/...` JSON templates remain
the only resource templates.

The resource manifest rejects every other static URI. The FastMCP and
middleware adapters admit only those fixed shell URIs without workspace scope;
all dynamic data remains in ordinary authenticated v7 tool responses. No
data-bearing compatibility URI is registered by the v7 server. Existing
rendering normalizes bounded presentation data, escapes JSON embedded in HTML,
and emits hash-based restrictive CSP. The package-data configuration already
ships the template, CSS, runtime, and renderer assets used by every shell.

Focused manifest and adapter coverage passed on 2026-09-16:

```text
.venv/Scripts/python.exe -m pytest tests/api_v7/test_dashboard_resources.py tests/api_v7/test_resources.py tests/api_v7/test_factory.py tests/api_v7/test_composition.py tests/api_v7/test_production.py -q
28 passed
```

Real production discovery and reads passed for both stdio and Streamable HTTP:

```text
.venv/Scripts/python.exe -m pytest tests/api_v7/test_dashboard_process_resources.py -q
2 passed
```

An actual MCP App renderer client has not yet been run against these shells.
That remains a release gate; the passing checks establish server registration,
safe static rendering, and MCP resource delivery only.

The five data dashboards declare their registered shell through official
`_meta.ui.resourceUri` tool metadata. Their production runtime uses the MCP
Apps `ui/initialize` → `ui/notifications/initialized` lifecycle, accepts the
standard tool-input and tool-result notifications, and projects bounded v7
structured-content envelopes into the existing renderers. Refresh and paging
issue the corresponding authenticated v7 read through `tools/call` with only
the host-delivered workspace ID and original read arguments. The Covenant view
is read-only: a recorded briefing never becomes a preflight grant or mutation
permission. Graph relationship edges are retained. The source-identity and
origin checks reject messages not sent by `window.parent`, including opaque
`null` origins, and bridge messages are limited to 1 MiB.

The D3 dashboards load the same hardened `messenger.js` as all other shells;
the generated D3 bundle no longer contains a stale second bridge. A simulated
MCP Apps host test covers the lifecycle, a populated v7 search result, exact
refresh arguments, populated graph edges, the unapproved Covenant state, and
spoofed/oversized messages. This is protocol coverage, not supported-client
certification. An actual supported MCP Apps host still must be observed
rendering and refreshing a dashboard before this release gate can close.
