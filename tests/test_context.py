"""Tests for project context management."""

import asyncio
import shutil
import tempfile
from unittest.mock import patch

import pytest


class TestProjectContextConcurrency:
    """Test concurrent access to project contexts."""

    @pytest.fixture
    def temp_projects(self):
        """Create temporary project directories."""
        dirs = [tempfile.mkdtemp() for _ in range(3)]
        yield dirs
        for d in dirs:
            shutil.rmtree(d, ignore_errors=True)

    @pytest.mark.asyncio
    async def test_concurrent_context_creation_uses_lock(self, temp_projects):
        """Verify that concurrent calls to get_project_context don't race."""
        from daem0nmcp.server import (
            _context_locks,
            _project_contexts,
            get_project_context,
        )

        # Clear existing contexts and locks
        _project_contexts.clear()
        _context_locks.clear()

        project_path = temp_projects[0]

        # Track how many times init_db is called
        init_count = 0
        original_init = None

        async def counting_init(self):
            nonlocal init_count
            init_count += 1
            await asyncio.sleep(0.1)  # Simulate slow init
            if original_init:
                await original_init(self)

        # Patch init_db to count calls
        from daem0nmcp.database import DatabaseManager

        original_init = DatabaseManager.init_db

        with patch.object(DatabaseManager, "init_db", counting_init):
            # Launch concurrent requests
            tasks = [get_project_context(project_path) for _ in range(5)]
            contexts = await asyncio.gather(*tasks)

        # All should return the same context
        assert all(c is contexts[0] for c in contexts)
        # init_db should only be called once due to locking
        assert init_count == 1, f"init_db called {init_count} times, expected 1"


class TestProjectContextEviction:
    """Test LRU/TTL eviction for project contexts."""

    @pytest.fixture
    def temp_projects(self):
        """Create multiple temporary project directories."""
        from daem0nmcp.server import MAX_PROJECT_CONTEXTS

        # Create MAX_PROJECT_CONTEXTS + 3 directories to test eviction
        dirs = [tempfile.mkdtemp() for _ in range(MAX_PROJECT_CONTEXTS + 3)]
        yield dirs
        for d in dirs:
            shutil.rmtree(d, ignore_errors=True)

    @pytest.mark.asyncio
    async def test_lru_eviction_when_max_contexts_exceeded(self, temp_projects):
        """Verify oldest contexts are evicted when max is exceeded."""
        from daem0nmcp.server import (
            MAX_PROJECT_CONTEXTS,
            _project_contexts,
            evict_stale_contexts,
            get_project_context,
        )

        _project_contexts.clear()

        # Create contexts up to max + 2
        for i, project_path in enumerate(temp_projects[: MAX_PROJECT_CONTEXTS + 2]):
            ctx = await get_project_context(project_path)
            ctx.last_accessed = i  # Simulate access order

        # Evict stale contexts
        evicted = await evict_stale_contexts()

        # Should have evicted oldest contexts
        assert len(_project_contexts) <= MAX_PROJECT_CONTEXTS
        assert evicted >= 2

    @pytest.mark.asyncio
    async def test_ttl_eviction_for_old_contexts(self, temp_projects):
        """Verify contexts older than TTL are evicted."""
        import time

        from daem0nmcp.server import (
            CONTEXT_TTL_SECONDS,
            _project_contexts,
            evict_stale_contexts,
            get_project_context,
        )

        _project_contexts.clear()

        # Create a context with old last_accessed time
        ctx = await get_project_context(temp_projects[0])
        ctx.last_accessed = time.time() - CONTEXT_TTL_SECONDS - 100

        # Create a recent context
        await get_project_context(temp_projects[1])

        # Evict
        await evict_stale_contexts()

        # Old context should be gone, new one should remain
        assert len(_project_contexts) == 1


class TestPathResolution:
    """Test path normalization and resolution."""

    def test_normalize_path_handles_windows_paths(self):
        """Verify Windows-style paths are normalized."""
        from daem0nmcp.server import _normalize_path

        # Test various path formats
        paths = [
            "C:\\Users\\test\\project",
            "C:/Users/test/project",
            "/home/user/project",
        ]

        for path in paths:
            result = _normalize_path(path)
            assert result is not None
            assert len(result) > 0

    def test_normalize_path_resolves_relative(self):
        """Verify relative paths are resolved."""
        import os

        from daem0nmcp.server import _normalize_path

        result = _normalize_path(".")
        assert os.path.isabs(result)

    @pytest.mark.asyncio
    async def test_different_projects_get_different_contexts(self):
        """Verify each project gets its own context."""
        import tempfile

        from daem0nmcp.server import _project_contexts, get_project_context

        _project_contexts.clear()

        with tempfile.TemporaryDirectory() as dir1:
            with tempfile.TemporaryDirectory() as dir2:
                ctx1 = await get_project_context(dir1)
                ctx2 = await get_project_context(dir2)

                assert ctx1 is not ctx2
                assert ctx1.project_path != ctx2.project_path
                assert len(_project_contexts) == 2

                # Clean up database connections
                await ctx1.db_manager.close()
                await ctx2.db_manager.close()
