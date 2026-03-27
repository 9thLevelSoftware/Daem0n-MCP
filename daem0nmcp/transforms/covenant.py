"""
CovenantTransform - FastMCP 3.0 Transform for Sacred Covenant Enforcement.

This transform implements middleware-style interception of MCP tool calls
to enforce the Sacred Covenant protocol:

    COMMUNE (get_briefing) -> SEEK COUNSEL (context_check) -> INSCRIBE (remember) -> SEAL (record_outcome)

The transform can be used in two ways:
1. Standalone: Call check_tool_access() directly to validate tool calls
2. FastMCP Middleware: Use on_call_tool() hook when integrated with FastMCP 3.0

The Sacred Covenant flow ensures:
- AI agents commune with the Daem0n before any work (get briefing context)
- AI agents seek counsel before mutations (check for conflicts/duplicates)
- Decisions are inscribed with proper context
- Outcomes are sealed to track what worked/failed
"""

import json
import logging
from collections.abc import Callable
from contextvars import ContextVar
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)

# ContextVar for client metadata extracted from tool args by CovenantMiddleware.
# This allows transport-level metadata (_client_meta) to be stripped before
# FastMCP's Pydantic validation and accessed by tool dispatch functions.
client_meta_var: ContextVar[dict[str, Any] | None] = ContextVar(
    "client_meta_var", default=None
)

# TTL for context checks (5 minutes default, same as existing covenant.py)
COUNSEL_TTL_SECONDS = 300


# ============================================================================
# TOOL CLASSIFICATION
# ============================================================================

# Tools exempt from all covenant enforcement (entry points and diagnostics)
COVENANT_EXEMPT_TOOLS: set[str] = {
    "get_briefing",  # Entry point - starts communion
    "health",  # Diagnostic - always available
    "context_check",  # Part of the covenant flow
    "recall",  # Read-only query
    "recall_for_file",  # Read-only query
    "search_memories",  # Read-only query
    "find_related",  # Read-only query
    "check_rules",  # Read-only query
    "list_rules",  # Read-only query
    "find_code",  # Read-only query
    "analyze_impact",  # Read-only analysis
    "export_data",  # Read-only export
    "scan_todos",  # Read-only scan (unless auto_remember=True)
    "propose_refactor",  # Read-only analysis
    "get_graph",  # Read-only query
    "trace_chain",  # Read-only query
    "recall_hierarchical",  # Read-only query
    "list_communities",  # Read-only query
    "get_community_details",  # Read-only query
    "recall_by_entity",  # Read-only query
    "list_entities",  # Read-only query
    "get_memory_versions",  # Read-only query
    "get_memory_at_time",  # Read-only query
    "list_context_triggers",  # Read-only query
    "check_context_triggers",  # Read-only query
    "get_active_context",  # Read-only query
    "list_linked_projects",  # Read-only query
    # Cognitive tools (Phase 17) -- analytical, read-only instruments
    "simulate_decision",
    "evolve_rule",
    "debate_internal",
}

# Tools that REQUIRE communion (must call get_briefing first)
# These are all mutating tools - they change state
COMMUNION_REQUIRED_TOOLS: set[str] = {
    "remember",
    "remember_batch",
    "add_rule",
    "update_rule",
    "record_outcome",
    "link_memories",
    "unlink_memories",
    "pin_memory",
    "archive_memory",
    "prune_memories",
    "cleanup_memories",
    "compact_memories",
    "import_data",
    "rebuild_index",
    "index_project",
    "ingest_doc",
    "set_active_context",
    "remove_from_active_context",
    "clear_active_context",
    "link_projects",
    "unlink_projects",
    "consolidate_linked_databases",
    "rebuild_communities",
    "backfill_entities",
    "add_context_trigger",
    "remove_context_trigger",
}

# Tools that REQUIRE counsel (must call context_check before mutating)
# These are the most important mutations that could cause conflicts
COUNSEL_REQUIRED_TOOLS: set[str] = {
    "remember",
    "remember_batch",
    "add_rule",
    "update_rule",
    "prune_memories",
    "cleanup_memories",
    "compact_memories",
    "import_data",
    "ingest_doc",
}


# ============================================================================
# COVENANT VIOLATION RESPONSES
# ============================================================================


class CovenantViolation:
    """
    Standard violation response structures for covenant breaches.

    Returns structured dicts that block tool execution and guide
    the AI toward proper covenant adherence. The responses include
    clear explanations and remedies.
    """

    @staticmethod
    def communion_required(project_path: str) -> dict[str, Any]:
        """
        Response when tool is called without prior get_briefing().

        The Sacred Covenant demands communion before any meaningful work.
        This ensures the AI has context about existing memories, warnings,
        and rules before making changes.

        Args:
            project_path: The project path being accessed

        Returns:
            Blocking response with remedy instructions
        """
        return {
            "status": "blocked",
            "violation": "COMMUNION_REQUIRED",
            "message": (
                "The Sacred Covenant demands communion before work begins. "
                "You must first call get_briefing() to commune with the Daem0n "
                "and receive context about this realm's memories, warnings, and rules."
            ),
            "project_path": project_path,
            "remedy": {
                "tool": "get_briefing",
                "args": {"project_path": project_path},
                "description": "Begin communion with the Daem0n",
            },
        }

    @staticmethod
    def counsel_required(tool_name: str, project_path: str) -> dict[str, Any]:
        """
        Response when mutating tool is called without prior context_check().

        Before inscribing new memories, one must seek counsel on what
        already exists to avoid contradictions and duplications.

        Args:
            tool_name: The tool that was blocked
            project_path: The project path being accessed

        Returns:
            Blocking response with remedy instructions
        """
        return {
            "status": "blocked",
            "violation": "COUNSEL_REQUIRED",
            "message": (
                f"The Sacred Covenant requires seeking counsel before using '{tool_name}'. "
                f"You must first call context_check() to understand existing memories "
                f"and rules related to your intended action. This prevents contradictions "
                f"and honors the wisdom already inscribed."
            ),
            "project_path": project_path,
            "tool_blocked": tool_name,
            "remedy": {
                "tool": "context_check",
                "args": {
                    "description": f"About to use {tool_name}",
                    "project_path": project_path,
                },
                "description": f"Seek counsel before {tool_name}",
            },
        }

    @staticmethod
    def counsel_expired(
        tool_name: str, project_path: str, age_seconds: int
    ) -> dict[str, Any]:
        """
        Response when context_check was done but has expired.

        Counsel becomes stale after COUNSEL_TTL_SECONDS (default 5 minutes).
        The context may have changed, requiring fresh consultation.

        Args:
            tool_name: The tool that was blocked
            project_path: The project path being accessed
            age_seconds: How old the most recent counsel is

        Returns:
            Blocking response with remedy instructions
        """
        return {
            "status": "blocked",
            "violation": "COUNSEL_EXPIRED",
            "message": (
                f"Your counsel has grown stale ({age_seconds}s old, limit is {COUNSEL_TTL_SECONDS}s). "
                f"The context may have changed. Please seek fresh counsel before '{tool_name}'."
            ),
            "project_path": project_path,
            "tool_blocked": tool_name,
            "remedy": {
                "tool": "context_check",
                "args": {
                    "description": f"Refreshing counsel before {tool_name}",
                    "project_path": project_path,
                },
                "description": "Seek fresh counsel",
            },
        }


# ============================================================================
# COVENANT TRANSFORM
# ============================================================================


class CovenantTransform:
    """
    FastMCP 3.0 Transform for Sacred Covenant enforcement.

    This transform intercepts tool calls to enforce the covenant protocol:
    1. Exempt tools (health, get_briefing, read-only) always pass
    2. Communion-required tools block until get_briefing() is called
    3. Counsel-required tools block until context_check() is called

    The transform tracks state per-project, allowing multiple projects
    to be served concurrently with independent covenant states.

    Usage (standalone):
        transform = CovenantTransform()

        result = transform.check_tool_access(
            tool_name="remember",
            project_path="/my/project",
            get_state=lambda p: {"briefed": True, "context_checks": [...]}
        )

        if result is not None:
            return result  # Blocked, return violation response
        # Otherwise proceed with tool execution

    Usage (FastMCP middleware):
        # Will be implemented in Task 2.2 when integrating with server.py
    """

    def __init__(
        self,
        counsel_ttl_seconds: int = COUNSEL_TTL_SECONDS,
        exempt_tools: set[str] | None = None,
        communion_required_tools: set[str] | None = None,
        counsel_required_tools: set[str] | None = None,
    ):
        """
        Initialize the CovenantTransform.

        Args:
            counsel_ttl_seconds: How long context_check remains valid
            exempt_tools: Override the default exempt tools set
            communion_required_tools: Override the default communion tools set
            counsel_required_tools: Override the default counsel tools set
        """
        self.counsel_ttl_seconds = counsel_ttl_seconds
        self.exempt_tools = exempt_tools or COVENANT_EXEMPT_TOOLS
        self.communion_required_tools = (
            communion_required_tools or COMMUNION_REQUIRED_TOOLS
        )
        self.counsel_required_tools = counsel_required_tools or COUNSEL_REQUIRED_TOOLS

    def check_tool_access(
        self,
        tool_name: str,
        project_path: str | None,
        get_state: Callable[[str | None], dict[str, Any] | None],
    ) -> dict[str, Any] | None:
        """
        Check if a tool call is allowed under the Sacred Covenant.

        This is the main enforcement method. It checks tool classification
        and session state to determine if the call should proceed.

        Args:
            tool_name: Name of the tool being called
            project_path: Project root path (may be None for some tools)
            get_state: Callback to get session state for a project path.
                      Should return dict with 'briefed' (bool) and
                      'context_checks' (list of dicts with 'timestamp')

        Returns:
            None if tool is allowed, or a violation dict if blocked.
            The violation dict includes status, violation type, message,
            and remedy instructions.
        """
        # Step 1: Check if tool is exempt
        if tool_name in self.exempt_tools:
            logger.debug(f"Tool '{tool_name}' is exempt from covenant enforcement")
            return None  # Allowed

        # Step 2: Get session state
        state = get_state(project_path)

        # Step 3: Check communion requirement
        if tool_name in self.communion_required_tools:
            if state is None or not state.get("briefed", False):
                logger.info(
                    f"Communion required for tool '{tool_name}' on project '{project_path}'"
                )
                return CovenantViolation.communion_required(project_path or "unknown")

        # Step 4: Check counsel requirement
        if tool_name in self.counsel_required_tools:
            # Must first check communion (counsel implies communion)
            if state is None or not state.get("briefed", False):
                logger.info(f"Communion required before counsel for tool '{tool_name}'")
                return CovenantViolation.communion_required(project_path or "unknown")

            # Check for fresh counsel
            context_checks = state.get("context_checks", [])

            if not context_checks:
                logger.info(f"Counsel required for tool '{tool_name}'")
                return CovenantViolation.counsel_required(
                    tool_name, project_path or "unknown"
                )

            # Find the most recent context check with a valid timestamp
            most_recent_age = self._get_freshest_counsel_age(context_checks)

            if most_recent_age is None:
                # No valid timestamped checks
                logger.info(f"No valid timestamped counsel found for '{tool_name}'")
                return CovenantViolation.counsel_required(
                    tool_name, project_path or "unknown"
                )

            if most_recent_age > self.counsel_ttl_seconds:
                logger.info(
                    f"Counsel expired ({most_recent_age:.0f}s old) for '{tool_name}'"
                )
                return CovenantViolation.counsel_expired(
                    tool_name, project_path or "unknown", int(most_recent_age)
                )

        # All checks passed
        logger.debug(f"Tool '{tool_name}' allowed under covenant")
        return None

    def _get_freshest_counsel_age(
        self,
        context_checks: list,
    ) -> float | None:
        """
        Find the age of the most recent valid context check.

        Args:
            context_checks: List of context check entries

        Returns:
            Age in seconds of the freshest check, or None if no valid checks
        """
        now = datetime.now(timezone.utc)
        most_recent_age: float | None = None

        for check in context_checks:
            if isinstance(check, dict) and "timestamp" in check:
                try:
                    check_time = datetime.fromisoformat(check["timestamp"])
                    # Handle timezone-naive timestamps
                    if check_time.tzinfo is None:
                        check_time = check_time.replace(tzinfo=timezone.utc)
                    age = (now - check_time).total_seconds()
                    if most_recent_age is None or age < most_recent_age:
                        most_recent_age = age
                except (ValueError, TypeError) as e:
                    logger.warning(f"Invalid timestamp in context check: {e}")
                    continue
            elif isinstance(check, str):
                # Legacy format - treat as valid (backwards compatibility)
                return 0.0  # Age 0 = always fresh for legacy

        return most_recent_age

    def __repr__(self) -> str:
        return (
            f"CovenantTransform("
            f"counsel_ttl={self.counsel_ttl_seconds}s, "
            f"exempt={len(self.exempt_tools)}, "
            f"communion={len(self.communion_required_tools)}, "
            f"counsel={len(self.counsel_required_tools)}"
            f")"
        )


# ============================================================================
# COVENANT MIDDLEWARE (FastMCP 3.0)
# ============================================================================

try:
    from fastmcp.server.middleware import CallNext, Middleware, MiddlewareContext
    from fastmcp.tools import ToolResult
    from mcp import types as mt

    _FASTMCP_MIDDLEWARE_AVAILABLE = True
except ImportError:
    _FASTMCP_MIDDLEWARE_AVAILABLE = False
    # Define stubs for type hints when FastMCP 3.0 middleware is not available
    Middleware = object  # type: ignore
    MiddlewareContext = Any  # type: ignore
    CallNext = Any  # type: ignore
    ToolResult = Any  # type: ignore


class CovenantMiddleware(Middleware if _FASTMCP_MIDDLEWARE_AVAILABLE else object):
    """
    FastMCP 3.0 Middleware for Sacred Covenant enforcement.

    This middleware intercepts tool calls via the on_call_tool() hook to enforce
    the Sacred Covenant protocol at the MCP layer, before tools are executed.

    The middleware provides centralized enforcement, complementing (or eventually
    replacing) the decorator-based approach in covenant.py.

    Usage:
        from daem0nmcp.transforms.covenant import CovenantMiddleware

        # Create middleware with state callback
        middleware = CovenantMiddleware(
            get_state=lambda project_path: {
                "briefed": True,
                "context_checks": [{"timestamp": "..."}]
            }
        )

        # Register with FastMCP server
        mcp.add_middleware(middleware)

    The middleware will:
    1. Extract tool_name and project_path from the request
    2. Check the covenant via CovenantTransform.check_tool_access()
    3. Block with violation response if covenant is broken
    4. Allow through to call_next() if covenant is satisfied
    """

    def __init__(
        self,
        get_state: Callable[[str | None], dict[str, Any] | None],
        counsel_ttl_seconds: int = COUNSEL_TTL_SECONDS,
        exempt_tools: set[str] | None = None,
        communion_required_tools: set[str] | None = None,
        counsel_required_tools: set[str] | None = None,
        dream_scheduler: Any | None = None,  # IdleDreamScheduler, typed as Any to avoid circular import
    ):
        """
        Initialize the CovenantMiddleware.

        Args:
            get_state: Callback to get session state for a project path.
                      Should return dict with 'briefed' (bool) and
                      'context_checks' (list of dicts with 'timestamp'),
                      or None if no state exists for the project.
            counsel_ttl_seconds: How long context_check remains valid (default 300s)
            exempt_tools: Override the default exempt tools set
            communion_required_tools: Override the default communion tools set
            counsel_required_tools: Override the default counsel tools set
            dream_scheduler: Optional IdleDreamScheduler for idle timer notifications.
                            Typed as Any to avoid circular imports.
        """
        if _FASTMCP_MIDDLEWARE_AVAILABLE:
            super().__init__()

        self._get_state = get_state
        self._dream_scheduler = dream_scheduler
        self._client_name: str | None = None
        self._client_version: str | None = None
        self._transform = CovenantTransform(
            counsel_ttl_seconds=counsel_ttl_seconds,
            exempt_tools=exempt_tools,
            communion_required_tools=communion_required_tools,
            counsel_required_tools=counsel_required_tools,
        )

    def set_dream_scheduler(self, scheduler) -> None:
        """Set the dream scheduler for idle timer notifications.

        Supports late binding when the scheduler is created after
        middleware registration.

        Args:
            scheduler: IdleDreamScheduler instance (or None to clear).
        """
        self._dream_scheduler = scheduler

    @property
    def client_name(self) -> str | None:
        """MCP client name from the initialize handshake (e.g., 'opencode', 'claude-code')."""
        return self._client_name

    async def on_initialize(
        self,
        context: "MiddlewareContext[mt.InitializeRequest]",
        call_next: "CallNext[mt.InitializeRequest, mt.InitializeResult | None]",
    ) -> "mt.InitializeResult | None":
        """Capture MCP client identity from the initialize handshake."""
        try:
            client_info = getattr(context.message, "clientInfo", None)
            if client_info:
                self._client_name = getattr(client_info, "name", None)
                self._client_version = getattr(client_info, "version", None)
        except Exception:
            pass  # Never block initialization
        return await call_next(context)

    async def on_call_tool(
        self,
        context: "MiddlewareContext[mt.CallToolRequestParams]",
        call_next: "CallNext[mt.CallToolRequestParams, ToolResult]",
    ) -> "ToolResult":
        """
        Intercept tool calls to enforce the Sacred Covenant.

        This method is called by FastMCP before each tool execution.
        It checks whether the covenant is satisfied and either:
        - Blocks the call with a violation response
        - Allows the call to proceed via call_next()

        Args:
            context: Middleware context containing the request message
            call_next: Callback to proceed with the tool call

        Returns:
            ToolResult - either the violation response or the actual tool result
        """
        # Reset idle timer FIRST -- before any blocking checks.
        # This ensures the dream scheduler knows a tool call arrived,
        # even if the call ends up blocked by covenant enforcement.
        if self._dream_scheduler is not None:
            self._dream_scheduler.notify_tool_call()

        # Extract tool name and arguments from the request
        tool_name = context.message.name
        arguments = context.message.arguments or {}
        project_path = arguments.get("project_path")

        # Strip _client_meta from arguments before FastMCP's Pydantic validation.
        # Client plugins (e.g. OpenCode) inject this for provenance tracking, but
        # it's not part of any tool's schema. We stash it in a ContextVar so that
        # tool dispatch functions (inscribe) can read it without polluting signatures.
        raw_meta = arguments.pop("_client_meta", None)
        if raw_meta is not None:
            try:
                parsed = json.loads(raw_meta) if isinstance(raw_meta, str) else raw_meta
            except (ValueError, TypeError):
                parsed = None
            client_meta_var.set(parsed)
        else:
            client_meta_var.set(None)

        logger.debug(
            f"CovenantMiddleware: Checking tool '{tool_name}' for project '{project_path}'"
        )

        # Check covenant via the transform
        violation = self._transform.check_tool_access(
            tool_name=tool_name,
            project_path=project_path,
            get_state=self._get_state,
        )

        if violation is not None:
            # Covenant violated - return blocking response
            logger.info(
                f"CovenantMiddleware: Blocked '{tool_name}' - {violation.get('violation', 'UNKNOWN')}"
            )
            # Return the violation as a ToolResult with structured_content
            # FastMCP 3.0 requires structured_content for tools with return type annotations
            return ToolResult(structured_content=violation)

        # Covenant satisfied - proceed with the tool call
        logger.debug(f"CovenantMiddleware: Allowed '{tool_name}'")
        return await call_next(context)

    def __repr__(self) -> str:
        return f"CovenantMiddleware({self._transform!r})"
