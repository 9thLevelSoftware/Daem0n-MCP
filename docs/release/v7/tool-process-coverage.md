# V7 real-process tool coverage

This ledger distinguishes a subprocess MCP test from a component test.  A row
is **real process** only when the test starts `python -m daem0nmcp.server` via
`tests/api_v7/process_client.py`; FastMCP in-memory tests and direct operation
tests do not qualify.  “Open” includes component coverage where it exists, but
does not claim process acceptance.

The current bounded slice is [`test_process_surface.py`](../../../tests/api_v7/test_process_surface.py).
Its core scenario runs both `stdio` and `streamable-http`, calls
`session_brief`, obtains a distinct exact `memory_preflight` token for every
protected mutation, and asserts stored state after each transition.  The apps
TODO scenario uses only `DAEM0NMCP_APPS_ENABLED=true`; it is skipped when
`tree_sitter_language_pack` is unavailable.

| Tool | Process evidence | Status |
| --- | --- | --- |
| `active_context_add` | `test_process_surface.py::test_production_stateful_core_surface` (stdio, HTTP) | real process |
| `active_context_clear` | same; snapshot selection token and clear-state assertion | real process |
| `active_context_list` | same; add/remove/clear state assertions | real process |
| `active_context_remove` | same; returned affected ID assertion | real process |
| `code_impact_analyze` | `test_process_code_impact.py::test_production_code_index_search_and_impact` (stdio, HTTP) | real process |
| `code_index` | same; indexed file count assertion | real process |
| `code_refactor_propose` | `test_process_surface.py::test_production_core_read_batch_and_projection_surface` (stdio, HTTP) | real process |
| `code_search` | `test_process_code_impact.py::test_production_code_index_search_and_impact` | real process |
| `code_todos_scan` | `test_process_surface.py::test_production_code_todos_apps_profile` (apps, stdio, HTTP) | real process |
| `code_todos_scan_and_store` | `test_process_discovery_surface.py::test_production_code_todos_store_surface` (apps, stdio, HTTP) | real process |
| `community_get` | `test_process_discovery_surface.py::test_production_graph_discovery_surface` (graph, stdio, HTTP) | real process |
| `community_list` | `test_process_discovery_surface.py::test_production_graph_discovery_surface` (graph, stdio, HTTP) | real process |
| `community_rebuild` | `test_process_discovery_surface.py::test_production_graph_discovery_surface` (graph, stdio, HTTP) | real process |
| `context_compress` | `test_process_surface.py::test_production_core_read_batch_and_projection_surface` (stdio, HTTP) | real process |
| `context_trigger_create` | `test_process_surface.py::test_production_stateful_core_surface` (stdio, HTTP) | real process |
| `context_trigger_delete` | same; list-after-delete assertion | real process |
| `context_trigger_list` | same; create/delete state assertions | real process |
| `context_triggers_match` | same; matched trigger ID assertion | real process |
| `covenant_status` | `test_process_surface.py::test_production_core_read_batch_and_projection_surface` (stdio, HTTP) | real process |
| `decision_debate` | `test_process_discovery_surface.py::test_production_debate_and_dream_cleanup_surface` (stdio, HTTP) | real process |
| `decision_simulate` | `test_process_surface.py::test_production_intelligence_time_filter_surface` (stdio, HTTP; RFC3339 transaction time) | real process |
| `document_ingest_url` | `test_process_external_certification.py::test_actual_public_document_ingestion` (apps, stdio, HTTP; actual HTTPS, provenance and replay); `.tmp/public-ingest-process-final.log` | real process / explicit network run |
| `dream_duplicates_preview` | `test_process_discovery_surface.py::test_production_debate_and_dream_cleanup_surface` (stdio, HTTP) | real process |
| `dream_duplicates_purge` | `test_process_discovery_surface.py::test_production_debate_and_dream_cleanup_surface` (stdio, HTTP; fresh preview) | real process |
| `edit_preflight` | `test_process_edit_capture.py::test_actual_stdio_mcp_shares_bridge_authority_and_promotes_capture` | real process (stdio) |
| `entity_backfill` | `test_process_discovery_surface.py::test_production_graph_discovery_surface` (graph, stdio, HTTP) | real process |
| `entity_evolution_trace` | `test_process_discovery_surface.py::test_production_graph_discovery_surface` (graph, stdio, HTTP) | real process |
| `entity_list` | `test_process_discovery_surface.py::test_production_graph_discovery_surface` (graph, stdio, HTTP) | real process |
| `knowledge_graph_get` | `test_process_discovery_surface.py::test_production_graph_discovery_surface` (stdio, HTTP; bounded `max_nodes=2`) | real process |
| `knowledge_graph_render` | `test_process_discovery_surface.py::test_production_graph_discovery_surface` (stdio, HTTP; bounded `max_nodes=2`) | real process |
| `knowledge_graph_stats` | `test_process_discovery_surface.py::test_production_graph_discovery_surface` (stdio, HTTP) | real process |
| `memory_archive_set` | `test_process_surface.py::test_production_stateful_core_surface` (stdio, HTTP) | real process |
| `memory_at_time_get` | `test_process_surface.py::test_production_memory_at_time_accepts_its_version_timestamp` (stdio, HTTP) | real process |
| `memory_capture_list` | `test_process_edit_capture.py` and `test_process_dreaming.py` | real process (stdio) |
| `memory_capture_promote` | `test_process_edit_capture.py` | real process (stdio) |
| `memory_chain_trace` | `test_process_surface.py` asserts link path then no path after unlink | real process |
| `memory_compact` | `test_process_surface.py::test_production_maintenance_apply_from_fresh_previews` (stdio, HTTP; fresh preview) | real process |
| `memory_compaction_preview` | `test_process_surface.py` asserts selection token | real process |
| `memory_duplicates_cleanup` | `test_process_surface.py::test_production_maintenance_apply_from_fresh_previews` (stdio, HTTP; fresh preview) | real process |
| `memory_duplicates_preview` | `test_process_surface.py` asserts selection token | real process |
| `memory_link` | `test_process_surface.py` asserts created relationship path | real process |
| `memory_pin_set` | `test_process_surface.py` asserts affected record | real process |
| `memory_preflight` | `test_process_client.py`, `test_process_surface.py`, and other subprocess tests | real process |
| `memory_prune` | `test_process_portability.py` (stdio, HTTP; aged canonical record and fresh preview) | real process |
| `memory_prune_preview` | `test_process_surface.py` asserts selection token | real process |
| `memory_recall` | `test_process_client.py` (stdio, HTTP; restart and federation) | real process |
| `memory_recall_entity` | `test_process_discovery_surface.py::test_production_graph_discovery_surface` (graph, stdio, HTTP) | real process |
| `memory_recall_file` | `test_process_surface.py::test_production_core_read_batch_and_projection_surface` (stdio, HTTP) | real process |
| `memory_recall_hierarchical` | `test_process_discovery_surface.py::test_production_graph_discovery_surface` (graph, stdio, HTTP) | real process |
| `memory_record_outcome` | `test_process_client.py` (stdio, HTTP) | real process |
| `memory_related` | `test_process_surface.py::test_production_stateful_core_surface` (stdio, HTTP) | real process |
| `memory_search_text` | `test_process_surface.py::test_production_core_read_batch_and_projection_surface` (stdio, HTTP) | real process |
| `memory_store` | `test_process_client.py` and `test_process_surface.py` | real process |
| `memory_store_batch` | `test_process_surface.py::test_production_core_read_batch_and_projection_surface` (stdio, HTTP) | real process |
| `memory_unlink` | `test_process_surface.py` asserts removed chain | real process |
| `memory_verify` | `test_process_surface.py::test_production_intelligence_time_filter_surface` (stdio, HTTP; RFC3339 valid/transaction times) | real process |
| `memory_versions_list` | `test_process_surface.py` asserts canonical record version | real process |
| `projection_rebuild` | `test_process_surface.py::test_production_core_read_batch_and_projection_surface` (stdio, HTTP) | real process |
| `rule_check` | `test_process_surface.py` asserts matching rule ID | real process |
| `rule_create` | `test_process_surface.py` exact preflight and list assertion | real process |
| `rule_evolution_analyze` | `test_process_surface.py::test_production_intelligence_time_filter_surface` (stdio, HTTP) | real process |
| `rule_list` | `test_process_surface.py` asserts enabled then disabled views | real process |
| `rule_update` | `test_process_surface.py` exact preflight and disabled assertion | real process |
| `sandbox_execute_python` | no subprocess success scenario | open/component only |
| `session_brief` | `test_process_client.py` and all process suites | real process |
| `session_updates_get` | `test_process_surface.py::test_production_core_read_batch_and_projection_surface` (stdio, HTTP) | real process |
| `system_health` | `test_process_client.py` and `test_process_dreaming.py` | real process |
| `workspace_consolidate` | `test_process_client.py` / `test_process_tasks.py` task scenario (task test needs configured Valkey) | real process / environment-gated task |
| `workspace_consolidate_and_archive_sources` | `test_process_client.py` | real process |
| `workspace_consolidation_preview` | `test_process_client.py` / environment-gated task scenario | real process |
| `workspace_export` | `test_process_portability.py` (stdio, HTTP; optional legacy payload) | real process |
| `workspace_import` | `test_process_portability.py` (stdio, HTTP; paged same-identity restore/finalize) | real process |
| `workspace_link` | `test_process_client.py` and `test_process_workspace_access.py` | real process |
| `workspace_links_list` | `test_process_surface.py::test_production_workspace_link_lifecycle_surface` (stdio, HTTP) | real process |
| `workspace_unlink` | `test_process_surface.py::test_production_workspace_link_lifecycle_surface` (stdio, HTTP) | real process |

There is no registered `rule_delete` tool in the current 75-tool schema.
Deleting a context trigger is covered above; rule lifecycle has create, list,
check, and update (including `enabled: false`).

Current successful real-process coverage is **74 of 75 tools**. Public URL
ingestion passed with `DAEM0NMCP_CERTIFY_PUBLIC_INGEST=1` on both transports;
without that opt-in its skip is not certification evidence. Real E2B sandbox
execution remains open. These scenarios establish successful production paths,
not all argument, failure, platform, or final-release acceptance gates.
