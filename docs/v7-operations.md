# Daem0nMCP v7 operations guide

This guide describes the current development surface. It is not a release
certificate: the P0–P10 ledger remains pending in
[the release inventory](release/v7/inventory.md). The source of truth for
input and output shapes is the live MCP `tools/list` schema; the inventory is
generated with `python scripts/v7_release_inventory.py --write`.

## Install and configure the server

v7 is not published yet. Install from a source checkout or a locally built
wheel while working with this branch:

```bash
pip install .
# or: python -m build && pip install dist/daem0nmcp-7.0.0.dev0-py3-none-any.whl
```

After v7 is published, `pip install daem0nmcp` will be the small-core command.

Install profiles only for enabled capabilities:

| Profile | Enables |
| --- | --- |
| `tasks` | FastMCP durable-task support; also requires an authenticated loopback Redis URL. |
| `local` | Local Qdrant and BM25 support. |
| `graph` | Graph, communities, and LangGraph dependencies. |
| `apps` | Pinned URL ingestion, HTML extraction, watcher, and language-pack support. |
| `models-local` | Local sentence-transformer/ONNX vectors and compression. |
| `models-hosted` | Hosted-model token support. |
| `agency-e2b` | E2B sandbox execution. |
| `observability` | OpenTelemetry exporters. |
| `dev` | Test and lint tools. |
| `tracing` | Compatibility alias for the observability dependencies. |

For example: `pip install "daem0nmcp[apps,graph]"`. `models-local` remains
optional and requires Python 3.11 or newer. Its secure ONNX runtime dependency
has no CPython 3.10 distribution. On Python 3.10, the resolver omits this
profile's dependencies and the runtime reports the Python-version remediation;
core installation remains supported.

The server, not an MCP caller, establishes the workspace set. Configure the
default root and any allowed additional roots before launch:

```bash
export DAEM0NMCP_PROJECT_ROOT=/srv/project-a
export DAEM0NMCP_WORKSPACE_ROOTS='["/srv/project-b"]'
# Keep storage derived per workspace. Do not set one shared STORAGE_PATH here.
python -m daem0nmcp.server
```

`session_brief` requires an ID and therefore cannot be used to discover one.
`system_health()` without an ID deliberately reports service state only. The
operator obtains the stable ID from the same configured root with:

```bash
python -c "from daem0nmcp.config import Settings; from daem0nmcp.workspace import WorkspaceRegistry; print(WorkspaceRegistry.from_settings(Settings()).default.workspace_id)"
```

Give that ID to an authorized client through its managed local configuration or
protected bridge pairing. A remote client must not derive it from a filesystem
path or enumerate roots. `session_brief(workspace_id="ws_<opaque>")` confirms
the configured selection after connection.

`DAEM0NMCP_SYNC_TIMEOUT_SECONDS` controls foreground work and accepts 1–60
seconds, defaulting to 15. Calls which exceed their foreground admission limits
need the durable-task capability. To enable it, install the `tasks` profile and
set `DAEM0NMCP_TASK_REDIS_URL` to an authenticated loopback Redis endpoint.
Without it, the server reports `TASKS_UNAVAILABLE`; a tool must not pretend a
disabled task path completed.

Use stdio by default. The reviewed HTTP launcher is local by default:

```bash
python -m daem0nmcp.server --transport streamable-http --host 127.0.0.1 --port 9876
```

It uses Streamable HTTP. Remote binds need the configured authentication and
HTTP security layers. There is no redirect URL or URL-based pairing shortcut.

For a remote HTTPS deployment, terminate TLS at a reverse proxy and make the
backend listener and proxy addresses explicit. This is an environment example,
not an authorization grant:

```bash
export FASTMCP_SERVER_AUTH=fastmcp.server.auth.providers.jwt.JWTVerifier
export FASTMCP_SERVER_AUTH_JWT_JWKS_URI=https://issuer.example/jwks
export FASTMCP_SERVER_AUTH_JWT_ISSUER=https://issuer.example
export FASTMCP_SERVER_AUTH_JWT_AUDIENCE=daem0nmcp
export DAEM0NMCP_ALLOWED_HOSTS=mcp.example
export DAEM0NMCP_ALLOWED_ORIGINS=https://client.example
export DAEM0NMCP_TRUSTED_PROXY_IPS=127.0.0.1
export DAEM0NMCP_WORKSPACE_ACCESS_FILE=/secure/daem0n/workspace-access.json
python -m daem0nmcp.server --transport streamable-http --host 127.0.0.1 --port 8765
```

The access file and its parent directory must be owner-only. Its exact JSON
shape is `{"schema_version":1,"grants":{"oauth-sub:<subject>":["ws_<opaque>"]}}`.
This example runs the HTTPS proxy on the same host and keeps the backend on
loopback. Preserve the public Host and Origin headers when proxying. Trusted
proxy settings control forwarded-header interpretation; they are not a network
firewall. A proxy on another host requires a private backend interface and a
firewall allowing only that proxy to reach it. The server applies Host policy,
strict JSON body limits, and origin policy as three HTTP middleware layers.

## Session, recall, and exact authorization

The six pinned tools are a startup and diagnostics set, not the whole schema:
`session_brief`, `memory_preflight`, `memory_recall`, `memory_store`,
`memory_record_outcome`, and `system_health`. They belong to the full 75-tool
registry shown below.

Start with the workspace identifier supplied by the operator as described above:

```text
session_brief(workspace_id="ws_<opaque>", focus_areas=["authentication"])
memory_recall(workspace_id="ws_<opaque>", query="authentication", limit=10)
```

For a protected write, preflight matches the normalized target arguments
exactly. Do not include `workspace_id` or `preflight_token` inside
`target_arguments`; do include every other supplied argument, including the
stable idempotency key.

```text
memory_preflight(
  workspace_id="ws_<opaque>",
  target_tool="memory_store",
  target_arguments={
    "record_type":"decision",
    "content":"Use signed session cookies",
    "rationale":"Avoid server-side session state",
    "idempotency_key":"decision-auth-cookie-0001"
  },
  description="Record the authentication decision"
)

memory_store(
  workspace_id="ws_<opaque>",
  record_type="decision",
  content="Use signed session cookies",
  rationale="Avoid server-side session state",
  idempotency_key="decision-auth-cookie-0001",
  preflight_token="<returned token>"
)
```

The token cannot authorize an edited payload, changed key, or another tool.
Record a verified result with `memory_record_outcome`, using a new stable
idempotency key. For authorized federation, `memory_recall` accepts
`linked_workspace_ids`; returned evidence remains attributed to its source
workspace.

## Resources and diagnostics

The four bounded data resources require an authorized workspace session:

- `memory://workspaces/{workspace_id}/warnings`
- `memory://workspaces/{workspace_id}/failures`
- `memory://workspaces/{workspace_id}/rules`
- `memory://workspaces/{workspace_id}/active-context`

Six UI resources expose dashboard shells: `ui://daem0n/briefing`,
`ui://daem0n/community`, `ui://daem0n/covenant`, `ui://daem0n/graph`,
`ui://daem0n/search`, and `ui://daem0n/test`.

A useful diagnostic walk-through is: call `session_brief`; read warnings and
failures; call bounded `memory_recall`; use an exact preflight before a write;
record its outcome; then call `system_health(workspace_id="ws_<opaque>")`.
`system_health` reports the enabled services and capability states. Missing
profiles are remediation signals, not successful execution.

## Migration, projections, exports, and consolidation

Use the explicit v7 lifecycle commands against a registered project root:

```bash
python -m daem0nmcp.cli --project-path /srv/project-a migrate-v7
python -m daem0nmcp.cli --project-path /srv/project-a migrate-v7 --apply --batch-size 500
python -m daem0nmcp.cli --project-path /srv/project-a verify-v7 --workspace-id ws_<opaque>
python -m daem0nmcp.cli --project-path /srv/project-a verify-v7 --workspace-id ws_<opaque> --repair-projections
python -m daem0nmcp.cli --project-path /srv/project-a migrate-v7 --rollback latest
```

The first command inspects; `--apply` applies or resumes. On a retained format 6
database it creates format 7. On an active format 7 database with an older,
supported schema it creates a separate upgraded candidate, verifies its
authoritative ledgers and replayed projections, and advances `active-db.json` by
one generation. The source database and a SQLite snapshot remain in
`.daem0nmcp/storage/migrations/v7/<migration-id>/`; no authoritative event is
rewritten in place. Running `migrate-v7 --apply` again recovers a stopped run or
reports `already_active` when the active schema is current.

Keep enough free space for the retained source, snapshot, and candidate shown by
the dry-run inventory. `FUTURE_V7_SCHEMA`, `UNSUPPORTED_V7_SCHEMA`, and
`SCHEMA_UPGRADE_AUTHORITY_INVALID` require operator inspection or a newer
Daem0nMCP build; they are not repaired automatically. After an interrupted run,
repeat the same `migrate-v7 --apply` command. Use `migrate-v7 --rollback latest`
to return to the retained predecessor and `--apply` to reactivate its verified
candidate. The generic `migrate` command remains an in-place additive migration
entry point; use `migrate-v7 --apply` when retention, verification, atomic
activation, and rollback are required.

`verify-v7` checks authority and projections; `--repair-projections` performs
offline replay and atomic activation. Rollback retains the v7 write history
required by the migration service. For dense retrieval use the v7 projection
command rather than the old embedding migration:

```bash
python -m daem0nmcp.cli rebuild-projection --projection dense --workspace-id ws_<opaque>
```

`workspace_export` and `workspace_import` operate on the same workspace
identity. A v7 import bundle from another workspace is rejected with
`CROSS_WORKSPACE_IMPORT_UNSUPPORTED`; use `workspace_consolidation_preview`,
then `workspace_consolidate` (or the archive-sources variant) with the returned
`selection_token`, the same `source_workspace_ids`, an `idempotency_key`, and
an exact preflight. Vector and compatibility data travel through the versioned
export/import bundle and canonical legacy projection exports, not an ad-hoc
database copy.

Exports paginate. Start at page zero; pass `include_vectors=true` only when
the vector export is wanted, and preserve every returned bundle page and cursor:

```text
workspace_export(workspace_id="ws_<opaque>", include_legacy_projection=true,
  include_vectors=true, page_index=0)
workspace_export(workspace_id="ws_<opaque>", export_session_id="xpt_<returned session>",
  page_index=1, cursor="<returned cursor>")
```

For each import page, preflight the exact `workspace_import` request (bundle,
`merge`, `finalize`, and `idempotency_key`) before submitting it. A paginated
import ends with `workspace_import(workspace_id="ws_<opaque>",
import_session_id="ipt_<returned session>", finalize=true, idempotency_key="...",
preflight_token="...")`; that finalization has no `bundle`. A format-1 bundle
is one atomic import and cannot carry an import session.

Check and reactivate projections with the flags the CLI actually supports:

```bash
python -m daem0nmcp.cli projection-status --workspace-id ws_<opaque>
python -m daem0nmcp.cli rebuild-projection --workspace-id ws_<opaque> --projection dense --dry-run
python -m daem0nmcp.cli rebuild-projection --workspace-id ws_<opaque> --projection dense
python -m daem0nmcp.cli verify-v7 --workspace-id ws_<opaque> --repair-projections
```

There is no `--retry` flag. Re-run the same `rebuild-projection` command after
examining status, or use `verify-v7 --repair-projections` for offline repair
and atomic reactivation.

## Claude Code and OpenCode

Install Claude hooks for one project:

```bash
python -m daem0nmcp.cli --project-path /srv/project-a install-claude-hooks
```

Install OpenCode V1 locally:

```bash
python -m daem0nmcp.cli --project-path /srv/project-a install-opencode --interface v1
```

The installer copies the packaged `daem0n.ts` asset and provisions host-side
credentials. Native edit flow is exact: the host creates an edit request,
`edit_preflight` returns a one-use receipt for that native edit, the identical
edit is retried, capture is listed with `memory_capture_list`, and promotion
uses `memory_capture_promote` after a matching `memory_preflight`. Do not add
forgeable `_client_meta` fields to a plugin or tool call.

Protected remote pairing uses the installer’s explicit remote credential,
workspace, URL, CA-file, and origin options through `install_opencode`; it is
not established by a redirect. The project CLI covers local installation; use
the installer module for the protected remote arguments:

```bash
python -m daem0nmcp.opencode_install --project-path /srv/project-a --interface v1 \
  --remote-workspace-id ws_<opaque> \
  --remote-credential-file /secure/credential.json \
  --remote-url https://mcp.example:7443 \
  --remote-ca-file /secure/ca.pem \
  --remote-origin https://opencode.example
```

OpenCode V2 is currently unsupported because the released interface does not
expose native tool-execution hooks.

Claude Code uses the same protected bridge parameters through its installer
module:

```bash
python -m daem0nmcp.claude_hooks.install --project-path /srv/project-a \
  --remote-workspace-id ws_<opaque> \
  --remote-credential-file /secure/credential.json \
  --remote-url https://mcp.example:7443 \
  --remote-ca-file /secure/ca.pem \
  --remote-origin https://claude.example
```

The remote native-edit bridge is a separate authenticated HTTPS listener owned
by the MCP server lifecycle. Provision its credential as an operator, outside
the project and model context; the principal must equal the MCP JWT subject's
`oauth-sub:<subject>` identity:

```python
from pathlib import Path
from daem0nmcp.edit_bridge_transport import provision_bridge_credential

provision_bridge_credential(
    Path("/secure/daem0n/bridge-credential.json"),
    principal_id="oauth-sub:<subject>",
    transports=frozenset({"remote-https"}),
)
```

Run this as a script without printing its return value. Transfer the credential
to the paired desktop through an operator-controlled secure channel and retain
owner-only file and directory permissions. Use its desktop path in the installer
commands above. Configure the application server before launch:

```bash
export DAEM0NMCP_EDIT_BRIDGE_MODE=remote-https
export DAEM0NMCP_EDIT_BRIDGE_CREDENTIAL_FILE=/secure/daem0n/bridge-credential.json
export DAEM0NMCP_EDIT_BRIDGE_REMOTE_HOST=0.0.0.0
export DAEM0NMCP_EDIT_BRIDGE_REMOTE_PORT=7443
export DAEM0NMCP_EDIT_BRIDGE_TLS_CERT=/secure/tls/bridge-chain.pem
export DAEM0NMCP_EDIT_BRIDGE_TLS_KEY=/secure/tls/bridge-key.pem
export DAEM0NMCP_EDIT_BRIDGE_ALLOWED_HOSTS='["mcp.example"]'
export DAEM0NMCP_EDIT_BRIDGE_ALLOWED_ORIGINS='["https://claude.example","https://opencode.example"]'
```

This listener serves TLS directly. Its certificate must cover `mcp.example`,
and the desktop CA file must trust its issuer. Pairing pins the server authority,
Origin, CA path and bytes, workspace, and desktop directory identity. Editing
project configuration cannot change that recipient; intentional changes require
operator reprovisioning. A workspace grant in the server's access file is still
required. The current bridge configuration provisions one credential identity
per application server. See the [bridge implementation and verification
report](release/v7/p6-edit-capture.md) for enforcement boundaries.

## Durable-task lifecycle

When the `tasks` profile and authenticated loopback Redis configuration are
ready, `tools/list` marks supported operations as MCP `taskSupport: "optional"`.
The client submits the ordinary tool call with standard MCP task metadata; the
owned dispatcher validates the admitted normalized arguments, task TTL, and
execution timeout, then returns an MCP task object. Retrieve it with the MCP
task-get operation, list only tasks authorized for the current principal and
workspace, or request cancellation through the MCP task protocol. The normal
foreground call remains bounded by `DAEM0NMCP_SYNC_TIMEOUT_SECONDS`; a durable
task is not a client-side retry or a claim that an unavailable task service ran.

## CLI inventory

The current CLI commands are `briefing`, `check`, `index`,
`install-claude-hooks`, `install-hooks`, `install-opencode`, `migrate`,
`migrate-v7`, `pre-commit`, `projection-status`, `rebuild-projection`,
`record-outcome`, `recover-consolidation`, `remember`, `scan-todos`, `status`,
`uninstall-claude-hooks`, `uninstall-hooks`, `verify-v7`, and `watch`.
The deprecated `migrate --backfill-vectors` is format-6-only; v7 uses projection
rebuilds.

## Full registered v7 tool inventory

This schema-derived table lists every current tool. Covenant levels are
`exempt`, `communion`, `counsel`, and `destructive`; schema fields and defaults
come from `tools/list` and the generated release inventory.

| Tool | Category | Covenant |
| --- | --- | --- |
| `active_context_add` | context | counsel |
| `active_context_clear` | context | destructive |
| `active_context_list` | context | communion |
| `active_context_remove` | context | destructive |
| `code_impact_analyze` | code | communion |
| `code_index` | code | communion |
| `code_refactor_propose` | code | communion |
| `code_search` | code | communion |
| `code_todos_scan` | code | communion |
| `code_todos_scan_and_store` | code | counsel |
| `community_get` | communities | communion |
| `community_list` | communities | communion |
| `community_rebuild` | communities | counsel |
| `context_compress` | context | communion |
| `context_trigger_create` | rules | counsel |
| `context_trigger_delete` | rules | destructive |
| `context_trigger_list` | rules | communion |
| `context_triggers_match` | context | communion |
| `covenant_status` | covenant | exempt |
| `decision_debate` | cognitive | counsel |
| `decision_simulate` | cognitive | communion |
| `document_ingest_url` | external | counsel |
| `dream_duplicates_preview` | maintenance | communion |
| `dream_duplicates_purge` | maintenance | destructive |
| `edit_preflight` | edit | communion |
| `entity_backfill` | entities | counsel |
| `entity_evolution_trace` | entities | communion |
| `entity_list` | entities | communion |
| `knowledge_graph_get` | graph | communion |
| `knowledge_graph_render` | graph | communion |
| `knowledge_graph_stats` | graph | communion |
| `memory_archive_set` | maintenance | destructive |
| `memory_at_time_get` | memory | communion |
| `memory_capture_list` | memory | communion |
| `memory_capture_promote` | memory | counsel |
| `memory_chain_trace` | memory | communion |
| `memory_compact` | maintenance | destructive |
| `memory_compaction_preview` | maintenance | communion |
| `memory_duplicates_cleanup` | maintenance | destructive |
| `memory_duplicates_preview` | maintenance | communion |
| `memory_link` | memory | counsel |
| `memory_pin_set` | memory | counsel |
| `memory_preflight` | covenant | communion |
| `memory_prune` | maintenance | destructive |
| `memory_prune_preview` | maintenance | communion |
| `memory_recall` | retrieval | communion |
| `memory_recall_entity` | retrieval | communion |
| `memory_recall_file` | retrieval | communion |
| `memory_recall_hierarchical` | retrieval | communion |
| `memory_record_outcome` | memory | communion |
| `memory_related` | memory | communion |
| `memory_search_text` | retrieval | communion |
| `memory_store` | memory | counsel |
| `memory_store_batch` | memory | counsel |
| `memory_unlink` | memory | destructive |
| `memory_verify` | memory | communion |
| `memory_versions_list` | memory | communion |
| `projection_rebuild` | projection | communion |
| `rule_check` | rules | communion |
| `rule_create` | rules | counsel |
| `rule_evolution_analyze` | cognitive | communion |
| `rule_list` | rules | communion |
| `rule_update` | rules | counsel |
| `sandbox_execute_python` | sandbox | destructive |
| `session_brief` | session | exempt |
| `session_updates_get` | session | communion |
| `system_health` | system | exempt |
| `workspace_consolidate` | workspace | counsel |
| `workspace_consolidate_and_archive_sources` | workspace | destructive |
| `workspace_consolidation_preview` | workspace | communion |
| `workspace_export` | workspace | communion |
| `workspace_import` | workspace | destructive |
| `workspace_link` | workspace | counsel |
| `workspace_links_list` | workspace | communion |
| `workspace_unlink` | workspace | destructive |

For each tool’s required and optional fields, consult the current MCP schema or
the machine-readable matrix in `docs/release/v7/requirements.json`; registration
alone is not acceptance evidence.
