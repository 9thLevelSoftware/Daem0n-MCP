"""Daem0nMCP v7 composition root and protocol-clean entry point.

The wire server is composed exclusively by :mod:`daem0nmcp.api.v7`.  Legacy
Python helpers remain available as lazy re-exports for migration code, but
their decorator-bearing modules are never imported while the v7 registry is
being built.
"""

from __future__ import annotations

import threading
from importlib import import_module
from typing import TYPE_CHECKING, Any

from .api.v7.launcher import parse_server_options, run_server
from .api.v7.production import create_v7_server

if TYPE_CHECKING:
    from fastmcp import FastMCP

    mcp: FastMCP

_SUPPORTED_TRANSPORTS = frozenset({"stdio", "streamable-http"})


def create_server(
    transport_mode: str,
    *,
    host: str | None = None,
) -> Any:
    """Create a fresh v7 server for one reviewed transport profile."""

    if transport_mode not in _SUPPORTED_TRANSPORTS:
        raise ValueError("transport_mode must be stdio or streamable-http")
    return create_v7_server(transport_mode, host=host)


# Standard MCP hosts access ``mcp`` as a module attribute.  Resolve it lazily so
# command-line launchers that ask for ``create_server`` do not also allocate an
# unused stdio runtime whose lifespan can never run.
_default_mcp: Any | None = None
_default_mcp_lock = threading.Lock()


# Each entry names the ordinary Python implementation behind a historical
# server-module re-export.  Resolving one may load the legacy decorator module,
# but only after a Python caller explicitly asks for that adapter; it can never
# affect the fresh v7 registry above.
_LEGACY_EXPORTS: dict[str, tuple[str, str]] = {
    # Context management compatibility.
    "ProjectContext": ("daem0nmcp.context_manager", "ProjectContext"),
    "get_project_context": (
        "daem0nmcp.context_manager",
        "get_project_context",
    ),
    "evict_stale_contexts": (
        "daem0nmcp.context_manager",
        "evict_stale_contexts",
    ),
    "cleanup_all_contexts": (
        "daem0nmcp.context_manager",
        "cleanup_all_contexts",
    ),
    "_project_contexts": ("daem0nmcp.context_manager", "_project_contexts"),
    "_context_locks": ("daem0nmcp.context_manager", "_context_locks"),
    "_normalize_path": ("daem0nmcp.context_manager", "_normalize_path"),
    "_default_project_path": (
        "daem0nmcp.context_manager",
        "_default_project_path",
    ),
    "MAX_PROJECT_CONTEXTS": (
        "daem0nmcp.context_manager",
        "MAX_PROJECT_CONTEXTS",
    ),
    "CONTEXT_TTL_SECONDS": (
        "daem0nmcp.context_manager",
        "CONTEXT_TTL_SECONDS",
    ),
    "_missing_project_path_error": (
        "daem0nmcp.context_manager",
        "_missing_project_path_error",
    ),
    "_check_covenant_communion": (
        "daem0nmcp.context_manager",
        "_check_covenant_communion",
    ),
    "_check_covenant_counsel": (
        "daem0nmcp.context_manager",
        "_check_covenant_counsel",
    ),
    "_get_context_for_covenant": (
        "daem0nmcp.context_manager",
        "_get_context_for_covenant",
    ),
    "_get_context_state_for_middleware": (
        "daem0nmcp.context_manager",
        "_get_context_state_for_middleware",
    ),
    "_resolve_within_project": (
        "daem0nmcp.context_manager",
        "_resolve_within_project",
    ),
    "_track_task_context": (
        "daem0nmcp.context_manager",
        "_track_task_context",
    ),
    "_release_current_task_contexts": (
        "daem0nmcp.context_manager",
        "_release_current_task_contexts",
    ),
    "_contexts_lock": ("daem0nmcp.context_manager", "_contexts_lock"),
    "_task_contexts": ("daem0nmcp.context_manager", "_task_contexts"),
    "_task_contexts_lock": (
        "daem0nmcp.context_manager",
        "_task_contexts_lock",
    ),
    "_EVICTION_INTERVAL_SECONDS": (
        "daem0nmcp.context_manager",
        "_EVICTION_INTERVAL_SECONDS",
    ),
    "_maybe_schedule_eviction": (
        "daem0nmcp.context_manager",
        "_maybe_schedule_eviction",
    ),
    "_get_storage_for_project": (
        "daem0nmcp.context_manager",
        "_get_storage_for_project",
    ),
    "workspace_registry": (
        "daem0nmcp.context_manager",
        "workspace_registry",
    ),
    "hold_context": ("daem0nmcp.context_manager", "hold_context"),
    # Legacy memory operations.
    "archive_memory": ("daem0nmcp.tools.memory", "archive_memory"),
    "cleanup_memories": ("daem0nmcp.tools.memory", "cleanup_memories"),
    "compact_memories": ("daem0nmcp.tools.memory", "compact_memories"),
    "find_related": ("daem0nmcp.tools.memory", "find_related"),
    "get_memory_at_time": (
        "daem0nmcp.tools.memory",
        "get_memory_at_time",
    ),
    "get_memory_versions": (
        "daem0nmcp.tools.memory",
        "get_memory_versions",
    ),
    "get_related_memories": (
        "daem0nmcp.tools.memory",
        "get_related_memories",
    ),
    "pin_memory": ("daem0nmcp.tools.memory", "pin_memory"),
    "recall": ("daem0nmcp.tools.memory", "recall"),
    "recall_by_entity": ("daem0nmcp.tools.memory", "recall_by_entity"),
    "recall_for_file": ("daem0nmcp.tools.memory", "recall_for_file"),
    "recall_hierarchical": (
        "daem0nmcp.tools.memory",
        "recall_hierarchical",
    ),
    "recall_visual": ("daem0nmcp.tools.memory", "recall_visual"),
    "record_outcome": ("daem0nmcp.tools.memory", "record_outcome"),
    "remember": ("daem0nmcp.tools.memory", "remember"),
    "remember_batch": ("daem0nmcp.tools.memory", "remember_batch"),
    "search_memories": ("daem0nmcp.tools.memory", "search_memories"),
    # Rules, context, graph, federation, and temporal helpers.
    "add_rule": ("daem0nmcp.tools.rules", "add_rule"),
    "check_rules": ("daem0nmcp.tools.rules", "check_rules"),
    "list_rules": ("daem0nmcp.tools.rules", "list_rules"),
    "update_rule": ("daem0nmcp.tools.rules", "update_rule"),
    "add_context_trigger": (
        "daem0nmcp.tools.context_tools",
        "add_context_trigger",
    ),
    "check_context_triggers": (
        "daem0nmcp.tools.context_tools",
        "check_context_triggers",
    ),
    "clear_active_context": (
        "daem0nmcp.tools.context_tools",
        "clear_active_context",
    ),
    "get_active_context": (
        "daem0nmcp.tools.context_tools",
        "get_active_context",
    ),
    "list_context_triggers": (
        "daem0nmcp.tools.context_tools",
        "list_context_triggers",
    ),
    "remove_context_trigger": (
        "daem0nmcp.tools.context_tools",
        "remove_context_trigger",
    ),
    "remove_from_active_context": (
        "daem0nmcp.tools.context_tools",
        "remove_from_active_context",
    ),
    "set_active_context": (
        "daem0nmcp.tools.context_tools",
        "set_active_context",
    ),
    "get_graph": ("daem0nmcp.tools.graph_tools", "get_graph"),
    "get_graph_stats": ("daem0nmcp.tools.graph_tools", "get_graph_stats"),
    "get_graph_visual": ("daem0nmcp.tools.graph_tools", "get_graph_visual"),
    "get_community_details": (
        "daem0nmcp.tools.graph_tools",
        "get_community_details",
    ),
    "link_memories": ("daem0nmcp.tools.graph_tools", "link_memories"),
    "list_communities": (
        "daem0nmcp.tools.graph_tools",
        "list_communities",
    ),
    "list_communities_visual": (
        "daem0nmcp.tools.graph_tools",
        "list_communities_visual",
    ),
    "rebuild_communities": (
        "daem0nmcp.tools.graph_tools",
        "rebuild_communities",
    ),
    "trace_chain": ("daem0nmcp.tools.graph_tools", "trace_chain"),
    "unlink_memories": ("daem0nmcp.tools.graph_tools", "unlink_memories"),
    "consolidate_linked_databases": (
        "daem0nmcp.tools.federation",
        "consolidate_linked_databases",
    ),
    "link_projects": ("daem0nmcp.tools.federation", "link_projects"),
    "list_linked_projects": (
        "daem0nmcp.tools.federation",
        "list_linked_projects",
    ),
    "unlink_projects": ("daem0nmcp.tools.federation", "unlink_projects"),
    "trace_causal_path": (
        "daem0nmcp.tools.temporal",
        "trace_causal_path",
    ),
    "trace_evolution": ("daem0nmcp.tools.temporal", "trace_evolution"),
    # Briefing, code, agency, maintenance, and cognitive helpers.
    "check_for_updates": ("daem0nmcp.tools.briefing", "check_for_updates"),
    "context_check": ("daem0nmcp.tools.briefing", "context_check"),
    "get_briefing": ("daem0nmcp.tools.briefing", "get_briefing"),
    "get_briefing_visual": (
        "daem0nmcp.tools.briefing",
        "get_briefing_visual",
    ),
    "get_covenant_status": (
        "daem0nmcp.tools.briefing",
        "get_covenant_status",
    ),
    "get_covenant_status_visual": (
        "daem0nmcp.tools.briefing",
        "get_covenant_status_visual",
    ),
    "health": ("daem0nmcp.tools.briefing", "health"),
    "_bootstrap_project_context": (
        "daem0nmcp.tools.briefing",
        "_bootstrap_project_context",
    ),
    "_get_git_changes": ("daem0nmcp.tools.briefing", "_get_git_changes"),
    "_prefetch_focus_areas": (
        "daem0nmcp.tools.briefing",
        "_prefetch_focus_areas",
    ),
    "analyze_impact": ("daem0nmcp.tools.code_tools", "analyze_impact"),
    "find_code": ("daem0nmcp.tools.code_tools", "find_code"),
    "index_project": ("daem0nmcp.tools.code_tools", "index_project"),
    "propose_refactor": ("daem0nmcp.tools.code_tools", "propose_refactor"),
    "scan_todos": ("daem0nmcp.tools.code_tools", "scan_todos"),
    "_scan_for_todos": ("daem0nmcp.tools.code_tools", "_scan_for_todos"),
    "TODO_PATTERN": ("daem0nmcp.tools.code_tools", "TODO_PATTERN"),
    "compress_context": ("daem0nmcp.tools.agency_tools", "compress_context"),
    "execute_python": ("daem0nmcp.tools.agency_tools", "execute_python"),
    "ingest_doc": ("daem0nmcp.tools.agency_tools", "ingest_doc"),
    "MAX_CHUNKS": ("daem0nmcp.tools.agency_tools", "MAX_CHUNKS"),
    "export_data": ("daem0nmcp.tools.maintenance", "export_data"),
    "import_data": ("daem0nmcp.tools.maintenance", "import_data"),
    "prune_memories": ("daem0nmcp.tools.maintenance", "prune_memories"),
    "rebuild_index": ("daem0nmcp.tools.maintenance", "rebuild_index"),
    "debate_internal": (
        "daem0nmcp.tools.cognitive_tools",
        "debate_internal",
    ),
    "evolve_rule": ("daem0nmcp.tools.cognitive_tools", "evolve_rule"),
    "simulate_decision": (
        "daem0nmcp.tools.cognitive_tools",
        "simulate_decision",
    ),
    "backfill_entities": (
        "daem0nmcp.tools.entity_tools",
        "backfill_entities",
    ),
    "list_entities": ("daem0nmcp.tools.entity_tools", "list_entities"),
    "verify_facts": ("daem0nmcp.tools.verification", "verify_facts"),
    "reflect": ("daem0nmcp.tools.workflows", "reflect"),
    # Historical resource implementation imports used by Python callers.
    "_warnings_resource_impl": (
        "daem0nmcp.tools.resources",
        "_warnings_resource_impl",
    ),
    "_failed_resource_impl": (
        "daem0nmcp.tools.resources",
        "_failed_resource_impl",
    ),
    "_rules_resource_impl": (
        "daem0nmcp.tools.resources",
        "_rules_resource_impl",
    ),
    "_context_resource_impl": (
        "daem0nmcp.tools.resources",
        "_context_resource_impl",
    ),
    "get_triggered_context_resource": (
        "daem0nmcp.tools.resources",
        "get_triggered_context_resource",
    ),
    # Former composition-root class/function re-exports.
    "check_capability": ("daem0nmcp.agency", "check_capability"),
    "settings": ("daem0nmcp.config", "settings"),
    "set_context_callback": (
        "daem0nmcp.covenant",
        "set_context_callback",
    ),
    "DatabaseManager": ("daem0nmcp.database", "DatabaseManager"),
    "with_request_id": ("daem0nmcp.logging_config", "with_request_id"),
    "MemoryManager": ("daem0nmcp.memory", "MemoryManager"),
    "CodeEntity": ("daem0nmcp.models", "CodeEntity"),
    "Memory": ("daem0nmcp.models", "Memory"),
    "Rule": ("daem0nmcp.models", "Rule"),
    "RulesEngine": ("daem0nmcp.rules", "RulesEngine"),
    "RWLock": ("daem0nmcp.rwlock", "RWLock"),
    "WorkflowError": ("daem0nmcp.workflows.errors", "WorkflowError"),
}


def __getattr__(name: str) -> Any:
    """Resolve a historical Python re-export without startup side effects."""

    if name == "mcp":
        global _default_mcp
        with _default_mcp_lock:
            if _default_mcp is None:
                _default_mcp = create_server("stdio")
                globals()["mcp"] = _default_mcp
            return _default_mcp
    target = _LEGACY_EXPORTS.get(name)
    if target is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    module_name, attribute_name = target
    value = getattr(import_module(module_name), attribute_name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(_LEGACY_EXPORTS) | {"mcp"})


def main(arguments: list[str] | None = None) -> None:
    """Build and run one v7 server using the shared transport launcher."""

    options = parse_server_options(arguments)
    server = create_server(options.transport, host=options.host)
    run_server(server, options)


__all__ = [
    "create_server",
    "main",
    "mcp",
    *_LEGACY_EXPORTS,
]


if __name__ == "__main__":
    main()
