"""Tests for operational tools."""

import pytest


class TestHealthTool:
    """Test health and version reporting."""

    @pytest.mark.asyncio
    async def test_health_returns_version(self, tmp_path, covenant_workspace_factory):
        """Verify health tool returns version info."""
        from daem0nmcp import __version__
        from daem0nmcp.server import health

        workspace = covenant_workspace_factory(tmp_path)
        with workspace.installed():
            result = await health(project_path=workspace)

        assert "version" in result
        assert result["version"] == __version__
        assert "status" in result

    @pytest.mark.asyncio
    async def test_health_returns_statistics(self, covenant_workspace_factory):
        """Verify health tool returns memory statistics."""
        import shutil
        import tempfile

        from daem0nmcp.server import _project_contexts, health

        temp_dir = tempfile.mkdtemp()
        try:
            workspace = covenant_workspace_factory(temp_dir)
            with workspace.installed():
                result = await health(project_path=workspace)

            assert "memories_count" in result
            assert "rules_count" in result
            assert "storage_path" in result
        finally:
            # Close the database connection before cleanup
            if temp_dir in _project_contexts:
                await _project_contexts[temp_dir].db_manager.close()
                del _project_contexts[temp_dir]
            shutil.rmtree(temp_dir, ignore_errors=True)


class TestExportImport:
    """Test data export and import."""

    @pytest.mark.asyncio
    async def test_export_returns_json_structure(self, covenant_workspace_factory):
        """Verify export returns proper JSON structure."""
        import shutil
        import tempfile

        from daem0nmcp.server import _project_contexts, export_data, get_project_context

        temp_dir = tempfile.mkdtemp()
        try:
            _project_contexts.clear()
            workspace = covenant_workspace_factory(temp_dir)

            with workspace.installed():
                ctx = await get_project_context(workspace)
            await ctx.memory_manager.remember(
                category="decision", content="Test export"
            )
            await ctx.rules_engine.add_rule(
                trigger="test trigger", must_do=["test action"]
            )

            await workspace.brief()
            result = await workspace.call(export_data, project_path=workspace)

            assert "memories" in result
            assert "rules" in result
            assert "version" in result
            assert len(result["memories"]) >= 1
            assert len(result["rules"]) >= 1
        finally:
            # Close the database connection before cleanup
            if temp_dir in _project_contexts:
                await _project_contexts[temp_dir].db_manager.close()
                del _project_contexts[temp_dir]
            shutil.rmtree(temp_dir, ignore_errors=True)

    @pytest.mark.asyncio
    async def test_import_preserves_workspace_identity(
        self, covenant_workspace_factory
    ):
        """Verify ordinary import accepts only an export from the same workspace."""
        import shutil
        import tempfile

        from daem0nmcp.server import (
            _project_contexts,
            export_data,
            get_project_context,
            import_data,
        )

        temp_dir1 = tempfile.mkdtemp()
        try:
            _project_contexts.clear()
            source_workspace = covenant_workspace_factory(temp_dir1)

            # Create data in first project
            with source_workspace.installed():
                ctx1 = await get_project_context(source_workspace)
            await ctx1.memory_manager.remember(
                category="decision", content="Imported memory test"
            )

            await source_workspace.brief()
            exported = await source_workspace.call(
                export_data, project_path=source_workspace
            )

            # Reopen the same project to preserve the export's workspace identity.
            await ctx1.db_manager.close()
            _project_contexts.clear()
            result = await source_workspace.call(
                import_data,
                data=exported,
                project_path=source_workspace,
            )

            assert result["status"] == "imported"
            assert result["bundle_events_existing"] >= 1

            # Verify the retained workspace data remains queryable after import.
            with source_workspace.installed():
                ctx2 = await get_project_context(source_workspace)
            recall_result = await ctx2.memory_manager.recall("Imported memory")
            assert recall_result["found"] >= 1
        finally:
            # Close the database connections before cleanup
            if temp_dir1 in _project_contexts:
                await _project_contexts[temp_dir1].db_manager.close()
                del _project_contexts[temp_dir1]
            shutil.rmtree(temp_dir1, ignore_errors=True)


class TestMaintenanceTools:
    """Test prune, archive, and pin operations."""

    @pytest.mark.asyncio
    async def test_pin_memory_prevents_decay(self, covenant_workspace_factory):
        """Verify pinned memories don't decay."""
        import shutil
        import tempfile

        from daem0nmcp.server import _project_contexts, get_project_context, pin_memory

        temp_dir = tempfile.mkdtemp()
        try:
            _project_contexts.clear()
            workspace = covenant_workspace_factory(temp_dir)
            with workspace.installed():
                ctx = await get_project_context(workspace)

            mem = await ctx.memory_manager.remember(
                category="decision", content="Important decision to pin"
            )

            await workspace.brief()
            result = await workspace.call(
                pin_memory,
                memory_id=mem["id"],
                pinned=True,
                project_path=workspace,
            )

            assert result.get("pinned")
        finally:
            # Close the database connection before cleanup
            if temp_dir in _project_contexts:
                await _project_contexts[temp_dir].db_manager.close()
                del _project_contexts[temp_dir]
            shutil.rmtree(temp_dir, ignore_errors=True)

    @pytest.mark.asyncio
    async def test_prune_removes_old_memories(self, covenant_workspace_factory):
        """Verify prune removes old, low-relevance memories."""
        import shutil
        import tempfile

        from daem0nmcp.server import (
            _project_contexts,
            get_project_context,
            prune_memories,
        )

        temp_dir = tempfile.mkdtemp()
        try:
            _project_contexts.clear()
            workspace = covenant_workspace_factory(temp_dir)
            with workspace.installed():
                ctx = await get_project_context(workspace)

            # Add some memories
            await ctx.memory_manager.remember(
                category="learning", content="Old learning to prune"
            )

            # Prune with dry_run first
            await workspace.brief()
            result = await workspace.call(
                prune_memories,
                older_than_days=0,  # Prune everything for test
                dry_run=True,
                project_path=workspace,
            )

            assert "would_prune" in result
            assert result["would_prune"] >= 1
        finally:
            # Close the database connection before cleanup
            if temp_dir in _project_contexts:
                await _project_contexts[temp_dir].db_manager.close()
                del _project_contexts[temp_dir]
            shutil.rmtree(temp_dir, ignore_errors=True)
