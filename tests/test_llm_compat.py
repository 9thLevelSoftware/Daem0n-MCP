"""Tests for Phase 22: LLM Compatibility -- provenance tracking.

Covers:
- Memory model provenance columns
- Migration 15
- MemoryManager.remember() with/without provenance
- Inscribe dispatch _client_meta extraction
- CovenantMiddleware on_initialize
- Plugin template validation
"""

import json
import shutil
import tempfile
from unittest.mock import AsyncMock, patch

import pytest

from daem0nmcp.database import DatabaseManager
from daem0nmcp.memory import MemoryManager
from daem0nmcp.migrations.schema import MIGRATIONS
from daem0nmcp.models import Memory
from daem0nmcp.transforms.covenant import CovenantMiddleware, client_meta_var
from tests.covenant_test_support import CovenantTestWorkspace

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def temp_storage():
    """Create a temporary storage directory."""
    temp_dir = tempfile.mkdtemp()
    yield temp_dir
    shutil.rmtree(temp_dir, ignore_errors=True)


@pytest.fixture
async def memory_manager(temp_storage):
    """Create a memory manager with temporary storage."""
    db = DatabaseManager(temp_storage)
    await db.init_db()
    manager = MemoryManager(db)
    yield manager
    if manager._qdrant:
        manager._qdrant.close()
    await db.close()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestMemoryModelProvenance:
    """Verify Memory model has provenance columns."""

    def test_memory_model_has_provenance_columns(self):
        """Memory model must have source_client and source_model as Column attributes."""
        assert hasattr(Memory, "source_client")
        assert hasattr(Memory, "source_model")
        # Verify column names are in the table
        col_names = [c.name for c in Memory.__table__.columns]
        assert "source_client" in col_names
        assert "source_model" in col_names


class TestMigration15:
    """Verify migration 15 exists with correct SQL."""

    def test_migration_15_exists(self):
        """Migration 15 must be in MIGRATIONS list with source_client and source_model ALTER TABLE statements."""
        m15_list = [m for m in MIGRATIONS if m[0] == 15]
        assert len(m15_list) == 1, "Exactly one migration 15 expected"

        version, description, stmts = m15_list[0]
        assert version == 15
        assert "source_client" in description.lower() or "llm" in description.lower()

        # Should have ALTER TABLE for source_client and source_model, plus index
        sql_text = " ".join(stmts)
        assert "source_client" in sql_text
        assert "source_model" in sql_text
        assert "ALTER TABLE" in sql_text
        assert "CREATE INDEX" in sql_text


class TestRememberProvenance:
    """Test MemoryManager.remember() with provenance params."""

    @pytest.mark.asyncio
    async def test_remember_stores_provenance(self, memory_manager):
        """Calling remember() with source_client and source_model should store and return them."""
        result = await memory_manager.remember(
            category="decision",
            content="Use FastAPI for the API layer",
            rationale="Async support and automatic OpenAPI docs",
            tags=["api"],
            source_client="opencode",
            source_model="anthropic/claude-sonnet-4",
        )

        assert "id" in result
        assert result["source_client"] == "opencode"
        assert result["source_model"] == "anthropic/claude-sonnet-4"

    @pytest.mark.asyncio
    async def test_remember_provenance_defaults_to_none(self, memory_manager):
        """Calling remember() without provenance params should default to None (backward compat)."""
        result = await memory_manager.remember(
            category="learning",
            content="SQLite WAL mode improves concurrency",
            rationale="Discovered during load testing",
            tags=["database"],
        )

        assert "id" in result
        assert result["source_client"] is None
        assert result["source_model"] is None


class TestInscribeDispatchClientMeta:
    """Test inscribe.dispatch() reads provenance from client_meta_var."""

    @pytest.mark.asyncio
    async def test_inscribe_dispatch_extracts_client_meta(self, memory_manager):
        """dispatch() should read provenance from client_meta_var (set by CovenantMiddleware)."""
        from daem0nmcp.workflows import inscribe

        meta = {
            "client": "opencode",
            "providerID": "openai",
            "modelID": "gpt-5",
        }

        mock_ctx = AsyncMock()
        mock_ctx.memory_manager = memory_manager
        mock_ctx.project_path = "/tmp/test-project"

        # Simulate middleware having set the ContextVar
        token = client_meta_var.set(meta)
        try:
            with patch(
                "daem0nmcp.context_manager.get_project_context", return_value=mock_ctx
            ):
                result = await inscribe.dispatch(
                    action="remember",
                    project_path="/tmp/test-project",
                    category="decision",
                    content="Use gRPC for inter-service communication",
                    rationale="Better performance than REST",
                    tags=["architecture"],
                )
        finally:
            client_meta_var.reset(token)

        assert result["source_client"] == "opencode"
        assert result["source_model"] == "openai/gpt-5"

    @pytest.mark.asyncio
    async def test_inscribe_dispatch_no_meta_defaults_to_none(self, memory_manager):
        """dispatch() with no client_meta_var set should default provenance to None."""
        from daem0nmcp.workflows import inscribe

        mock_ctx = AsyncMock()
        mock_ctx.memory_manager = memory_manager
        mock_ctx.project_path = "/tmp/test-project"

        # Ensure ContextVar is cleared
        token = client_meta_var.set(None)
        try:
            with patch(
                "daem0nmcp.context_manager.get_project_context", return_value=mock_ctx
            ):
                result = await inscribe.dispatch(
                    action="remember",
                    project_path="/tmp/test-project",
                    category="warning",
                    content="Never use eval() on user input",
                    rationale="Security vulnerability",
                    tags=["security"],
                )
        finally:
            client_meta_var.reset(token)

        assert "id" in result
        assert result["source_client"] is None
        assert result["source_model"] is None

    @pytest.mark.asyncio
    async def test_inscribe_dispatch_null_provider_model(self, memory_manager):
        """dispatch() with None providerID/modelID should not store 'None/None'."""
        from daem0nmcp.workflows import inscribe

        meta = {"client": "opencode", "providerID": None, "modelID": None}

        mock_ctx = AsyncMock()
        mock_ctx.memory_manager = memory_manager
        mock_ctx.project_path = "/tmp/test-project"

        token = client_meta_var.set(meta)
        try:
            with patch(
                "daem0nmcp.context_manager.get_project_context", return_value=mock_ctx
            ):
                result = await inscribe.dispatch(
                    action="remember",
                    project_path="/tmp/test-project",
                    category="decision",
                    content="Test null provider handling",
                    rationale="Test",
                    tags=["test"],
                )
        finally:
            client_meta_var.reset(token)

        assert result["source_client"] == "opencode"
        assert result["source_model"] is None


class TestCovenantMiddleware:
    """Test CovenantMiddleware on_initialize, client_name, and _client_meta stripping."""

    def test_covenant_middleware_has_on_initialize(self):
        """CovenantMiddleware must have an on_initialize method and client_name property."""
        middleware = CovenantMiddleware()
        assert hasattr(middleware, "on_initialize")
        assert callable(middleware.on_initialize)
        assert hasattr(middleware, "client_name")
        # client_name should be None before any initialization
        assert middleware.client_name is None

    @pytest.mark.asyncio
    async def test_middleware_strips_client_meta_from_args(self, tmp_path):
        """on_call_tool should pop _client_meta from arguments and set client_meta_var."""
        workspace = CovenantTestWorkspace(tmp_path)
        workspace.gate.record_briefing(workspace.scope)
        middleware = CovenantMiddleware(
            gate=workspace.gate,
            scope_provider=lambda _context, _workspace: workspace.scope,
            workspace_resolver=workspace._registry.resolve,
        )

        meta = {
            "client": "opencode",
            "providerID": "anthropic",
            "modelID": "claude-sonnet-4",
        }
        arguments = {
            "action": "recall",
            "project_path": workspace,
            "topic": "test",
            "_client_meta": json.dumps(meta),
        }

        # Build a mock context whose message.arguments is the mutable dict
        mock_message = type("Msg", (), {"name": "consult", "arguments": arguments})()
        mock_context = type("Ctx", (), {"message": mock_message})()

        # call_next just returns a sentinel so we can verify it was called
        sentinel = object()

        observed_meta = []

        async def mock_call_next(ctx):
            observed_meta.append(client_meta_var.get())
            return sentinel

        result = await middleware.on_call_tool(mock_context, mock_call_next)

        # _client_meta should be removed from downstream arguments.
        assert "_client_meta" not in mock_context.message.arguments
        # Metadata exists only while downstream dispatch is executing.
        assert observed_meta == [meta]
        assert client_meta_var.get() is None
        # Tool call should have proceeded
        assert result is sentinel

    @pytest.mark.asyncio
    async def test_middleware_handles_malformed_client_meta(self, tmp_path):
        """on_call_tool should handle malformed _client_meta without raising."""
        workspace = CovenantTestWorkspace(tmp_path)
        workspace.gate.record_briefing(workspace.scope)
        middleware = CovenantMiddleware(
            gate=workspace.gate,
            scope_provider=lambda _context, _workspace: workspace.scope,
            workspace_resolver=workspace._registry.resolve,
        )

        arguments = {
            "action": "recall",
            "project_path": workspace,
            "topic": "test",
            "_client_meta": "not-valid-json",
        }

        mock_message = type("Msg", (), {"name": "consult", "arguments": arguments})()
        mock_context = type("Ctx", (), {"message": mock_message})()

        observed_meta = []

        async def mock_call_next(ctx):
            observed_meta.append(client_meta_var.get())
            return "ok"

        result = await middleware.on_call_tool(mock_context, mock_call_next)

        assert "_client_meta" not in mock_context.message.arguments
        assert observed_meta == [None]
        assert client_meta_var.get() is None
        assert result == "ok"

    @pytest.mark.asyncio
    async def test_middleware_no_client_meta_clears_var(self, tmp_path):
        """on_call_tool without _client_meta should set client_meta_var to None."""
        workspace = CovenantTestWorkspace(tmp_path)
        workspace.gate.record_briefing(workspace.scope)
        middleware = CovenantMiddleware(
            gate=workspace.gate,
            scope_provider=lambda _context, _workspace: workspace.scope,
            workspace_resolver=workspace._registry.resolve,
        )

        # Pre-set the var to simulate a previous call that had metadata
        token = client_meta_var.set({"client": "stale"})

        arguments = {
            "action": "recall",
            "project_path": workspace,
            "topic": "test",
        }
        mock_message = type("Msg", (), {"name": "consult", "arguments": arguments})()
        mock_context = type("Ctx", (), {"message": mock_message})()

        observed_meta = []

        async def mock_call_next(ctx):
            observed_meta.append(client_meta_var.get())
            return "ok"

        try:
            await middleware.on_call_tool(mock_context, mock_call_next)
            assert observed_meta == [None]
            assert client_meta_var.get() == {"client": "stale"}
        finally:
            client_meta_var.reset(token)


class TestPluginTemplate:
    """Verify plugin template contains required LLM compatibility features."""

    def test_simplified_covenant_in_plugin_template(self):
        """PLUGIN_TEMPLATE must contain both COVENANT_RULES_FULL and COVENANT_RULES_SIMPLIFIED,
        and must not stage forgeable client metadata."""
        from daem0nmcp.opencode_install import PLUGIN_TEMPLATE

        assert "COVENANT_RULES_FULL" in PLUGIN_TEMPLATE, (
            "PLUGIN_TEMPLATE must reference COVENANT_RULES_FULL"
        )
        assert "COVENANT_RULES_SIMPLIFIED" in PLUGIN_TEMPLATE, (
            "PLUGIN_TEMPLATE must reference COVENANT_RULES_SIMPLIFIED"
        )
        assert "_client_meta" not in PLUGIN_TEMPLATE
        assert "currentModel" not in PLUGIN_TEMPLATE
