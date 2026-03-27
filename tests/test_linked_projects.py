# tests/test_linked_projects.py
"""Tests for linked projects feature."""

import pytest


class TestProjectLinkModel:
    """Test the ProjectLink model exists and has correct fields."""

    def test_project_link_model_exists(self):
        """ProjectLink model should be importable."""
        from daem0nmcp.models import ProjectLink

        assert hasattr(ProjectLink, "__tablename__")
        assert ProjectLink.__tablename__ == "project_links"

    def test_project_link_has_required_fields(self):
        """ProjectLink should have source_path, linked_path, relationship."""
        from daem0nmcp.models import ProjectLink

        # Check columns exist
        columns = {c.name for c in ProjectLink.__table__.columns}
        assert "id" in columns
        assert "source_path" in columns
        assert "linked_path" in columns
        assert "relationship" in columns
        assert "created_at" in columns


class TestProjectLinkMigration:
    """Test the migration creates the project_links table."""

    @pytest.fixture
    def db_manager(self, tmp_path):
        from daem0nmcp.database import DatabaseManager

        return DatabaseManager(str(tmp_path / "storage"))

    @pytest.mark.asyncio
    async def test_migration_creates_project_links_table(self, db_manager):
        """Migration should create project_links table."""
        await db_manager.init_db()

        import sqlite3

        conn = sqlite3.connect(str(db_manager.db_path))
        cursor = conn.cursor()
        cursor.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='project_links'"
        )
        result = cursor.fetchone()
        conn.close()

        assert result is not None, "project_links table should exist"


class TestLinkManager:
    """Test the LinkManager class."""

    @pytest.fixture
    def db_manager(self, tmp_path):
        from daem0nmcp.database import DatabaseManager

        return DatabaseManager(str(tmp_path / "storage"))

    @pytest.fixture
    def link_manager(self, db_manager):
        from daem0nmcp.links import LinkManager

        return LinkManager(db_manager)

    @pytest.mark.asyncio
    async def test_link_projects(self, db_manager, link_manager):
        """Should create a link between two projects."""
        await db_manager.init_db()

        result = await link_manager.link_projects(
            source_path="/repos/backend",
            linked_path="/repos/client",
            relationship="same-project",
        )

        assert result["status"] == "linked"
        assert result["source_path"] == "/repos/backend"
        assert result["linked_path"] == "/repos/client"

    @pytest.mark.asyncio
    async def test_list_linked_projects(self, db_manager, link_manager):
        """Should list all linked projects."""
        await db_manager.init_db()

        await link_manager.link_projects(
            "/repos/backend", "/repos/client", "same-project"
        )
        await link_manager.link_projects("/repos/backend", "/repos/shared", "upstream")

        links = await link_manager.list_linked_projects("/repos/backend")

        assert len(links) == 2
        paths = {link["linked_path"] for link in links}
        assert "/repos/client" in paths
        assert "/repos/shared" in paths

    @pytest.mark.asyncio
    async def test_unlink_projects(self, db_manager, link_manager):
        """Should remove a link between projects."""
        await db_manager.init_db()

        await link_manager.link_projects(
            "/repos/backend", "/repos/client", "same-project"
        )
        result = await link_manager.unlink_projects("/repos/backend", "/repos/client")

        assert result["status"] == "unlinked"

        links = await link_manager.list_linked_projects("/repos/backend")
        assert len(links) == 0

    @pytest.mark.asyncio
    async def test_duplicate_link_rejected(self, db_manager, link_manager):
        """Should not allow duplicate links."""
        await db_manager.init_db()

        await link_manager.link_projects(
            "/repos/backend", "/repos/client", "same-project"
        )
        result = await link_manager.link_projects(
            "/repos/backend", "/repos/client", "same-project"
        )

        assert result["status"] == "already_linked"


class TestLinkTools:
    """Test the MCP tools for link management."""

    @pytest.fixture
    def db_manager(self, tmp_path):
        from daem0nmcp.database import DatabaseManager

        return DatabaseManager(str(tmp_path / "storage"))

    @pytest.mark.asyncio
    async def test_link_projects_tool(self, db_manager):
        """link_projects MCP tool should create a link."""
        await db_manager.init_db()

        from daem0nmcp import server

        server._project_contexts.clear()

        project_path = str(db_manager.storage_path.parent.parent)

        # Briefing first (for communion)
        await server.get_briefing(project_path=project_path)

        result = await server.link_projects(
            linked_path="/repos/client",
            relationship="same-project",
            project_path=project_path,
        )

        assert result["status"] == "linked"

    @pytest.mark.asyncio
    async def test_list_linked_projects_tool(self, db_manager):
        """list_linked_projects MCP tool should return links."""
        await db_manager.init_db()

        from daem0nmcp import server

        server._project_contexts.clear()

        project_path = str(db_manager.storage_path.parent.parent)

        await server.get_briefing(project_path=project_path)
        await server.link_projects(
            linked_path="/repos/client",
            relationship="same-project",
            project_path=project_path,
        )

        result = await server.list_linked_projects(project_path=project_path)

        assert len(result["links"]) == 1
        assert result["links"][0]["linked_path"] == "/repos/client"

    @pytest.mark.asyncio
    async def test_unlink_projects_tool(self, db_manager):
        """unlink_projects MCP tool should remove a link."""
        await db_manager.init_db()

        from daem0nmcp import server

        server._project_contexts.clear()

        project_path = str(db_manager.storage_path.parent.parent)

        await server.get_briefing(project_path=project_path)
        await server.link_projects(
            linked_path="/repos/client",
            relationship="same-project",
            project_path=project_path,
        )

        result = await server.unlink_projects(
            linked_path="/repos/client", project_path=project_path
        )

        assert result["status"] == "unlinked"


class TestCrossProjectRecall:
    """Test recall with include_linked parameter."""

    @pytest.fixture
    def backend_db(self, tmp_path):
        from daem0nmcp.database import DatabaseManager

        db = DatabaseManager(str(tmp_path / "backend" / ".daem0nmcp"))
        return db

    @pytest.fixture
    def client_db(self, tmp_path):
        from daem0nmcp.database import DatabaseManager

        db = DatabaseManager(str(tmp_path / "client" / ".daem0nmcp"))
        return db

    @pytest.mark.asyncio
    async def test_recall_includes_linked_memories(
        self, tmp_path, backend_db, client_db
    ):
        """recall with include_linked=True should span linked projects."""
        await backend_db.init_db()
        await client_db.init_db()

        from daem0nmcp.links import LinkManager
        from daem0nmcp.memory import MemoryManager

        backend_memory = MemoryManager(backend_db)
        client_memory = MemoryManager(client_db)

        # Add memory to client
        await client_memory.remember(
            category="pattern",
            content="Use React Query for API calls",
            project_path=str(tmp_path / "client"),
        )

        # Add memory to backend
        await backend_memory.remember(
            category="pattern",
            content="Use FastAPI for REST endpoints",
            project_path=str(tmp_path / "backend"),
        )

        # Link backend -> client
        backend_links = LinkManager(backend_db)
        await backend_links.link_projects(
            source_path=str(tmp_path / "backend"),
            linked_path=str(tmp_path / "client"),
            relationship="same-project",
        )

        # Recall from backend with include_linked
        result = await backend_memory.recall(
            topic="API", project_path=str(tmp_path / "backend"), include_linked=True
        )

        # Should find memories from both projects
        all_content = str(result)
        assert "FastAPI" in all_content or "React Query" in all_content


class TestLinkedBriefing:
    """Test get_briefing includes linked project context."""

    @pytest.mark.asyncio
    async def test_briefing_shows_linked_projects(self, tmp_path):
        """get_briefing should mention linked projects."""
        from pathlib import Path

        from daem0nmcp import server
        from daem0nmcp.database import DatabaseManager
        from daem0nmcp.links import LinkManager
        from daem0nmcp.memory import MemoryManager

        server._project_contexts.clear()

        # Use the same storage path pattern as the server
        backend_path = str(Path(tmp_path / "backend").resolve())
        client_path = str(Path(tmp_path / "client").resolve())

        # Create storage directories using server's pattern
        backend_storage = str(Path(backend_path) / ".daem0nmcp" / "storage")
        client_storage = str(Path(client_path) / ".daem0nmcp" / "storage")

        # Initialize backend DB
        backend_db = DatabaseManager(backend_storage)
        await backend_db.init_db()

        # Initialize client DB
        client_db = DatabaseManager(client_storage)
        await client_db.init_db()

        # Add warning to client
        client_memory = MemoryManager(client_db)
        await client_memory.remember(
            category="warning",
            content="Don't use localStorage for auth tokens",
            project_path=client_path,
        )

        # Link backend -> client (use normalized path to match get_project_context)
        backend_links = LinkManager(backend_db)
        await backend_links.link_projects(
            source_path=backend_path,
            linked_path=client_path,
            relationship="same-project",
        )

        # Get briefing for backend
        result = await server.get_briefing(project_path=backend_path)

        assert "linked_projects" in result
        assert len(result["linked_projects"]) == 1

        # Verify the linked project data structure is correctly populated
        linked = result["linked_projects"][0]
        assert linked["path"] == client_path
        assert (
            linked["available"] is True
        )  # Now should work with correct .daem0nmcp path
        assert linked["relationship"] == "same-project"
        # With correct storage path, we should see the warning we added
        assert linked["warning_count"] == 1
        assert linked["memory_count"] == 1


class TestLinkedProjectsE2E:
    """End-to-end test of the complete linked projects flow."""

    @pytest.mark.asyncio
    async def test_complete_linked_workflow(self, tmp_path):
        """Test: link -> briefing -> recall -> unlink."""
        from daem0nmcp import server
        from daem0nmcp.database import DatabaseManager
        from daem0nmcp.memory import MemoryManager

        # Setup two project directories with CORRECT storage paths
        backend_path = tmp_path / "backend"
        client_path = tmp_path / "client"
        backend_path.mkdir()
        client_path.mkdir()

        # Use correct storage path: .daem0nmcp/storage
        backend_db = DatabaseManager(str(backend_path / ".daem0nmcp" / "storage"))
        client_db = DatabaseManager(str(client_path / ".daem0nmcp" / "storage"))
        await backend_db.init_db()
        await client_db.init_db()

        server._project_contexts.clear()

        # Add memories to each project
        backend_mem = MemoryManager(backend_db)
        client_mem = MemoryManager(client_db)

        await backend_mem.remember(
            category="decision",
            content="Use PostgreSQL for data persistence",
            project_path=str(backend_path),
        )

        await client_mem.remember(
            category="warning",
            content="Never store tokens in localStorage",
            project_path=str(client_path),
        )

        await client_mem.remember(
            category="pattern",
            content="Use HttpOnly cookies for auth",
            project_path=str(client_path),
        )

        # 1. COMMUNION - get briefing
        briefing = await server.get_briefing(project_path=str(backend_path))
        assert briefing["status"] == "ready"
        assert briefing["linked_projects"] == []  # No links yet

        # 2. LINK PROJECTS
        link_result = await server.link_projects(
            linked_path=str(client_path),
            relationship="same-project",
            label="Frontend client",
            project_path=str(backend_path),
        )
        assert link_result["status"] == "linked"

        # 3. VERIFY BRIEFING SHOWS LINK
        briefing2 = await server.get_briefing(project_path=str(backend_path))
        assert len(briefing2["linked_projects"]) == 1
        assert briefing2["linked_projects"][0]["path"] == str(client_path)
        assert briefing2["linked_projects"][0]["warning_count"] == 1

        # 4. RECALL WITH LINKED
        await server.context_check(
            description="checking auth patterns", project_path=str(backend_path)
        )

        recall_result = await server.recall(
            topic="auth tokens", include_linked=True, project_path=str(backend_path)
        )

        # Should find client's warning about localStorage
        all_content = str(recall_result)
        assert "localStorage" in all_content or "HttpOnly" in all_content

        # 5. LIST LINKS
        links = await server.list_linked_projects(project_path=str(backend_path))
        assert len(links["links"]) == 1

        # 6. UNLINK
        unlink_result = await server.unlink_projects(
            linked_path=str(client_path), project_path=str(backend_path)
        )
        assert unlink_result["status"] == "unlinked"

        # 7. VERIFY UNLINKED
        links2 = await server.list_linked_projects(project_path=str(backend_path))
        assert len(links2["links"]) == 0


class TestDatabaseConsolidation:
    """Test merging child repo databases into parent."""

    @pytest.mark.asyncio
    async def test_consolidate_linked_databases(self, tmp_path):
        """Should merge memories from child repos into parent."""
        from daem0nmcp.database import DatabaseManager
        from daem0nmcp.links import LinkManager
        from daem0nmcp.memory import MemoryManager

        # Setup parent and two child repos
        parent_path = tmp_path / "project"
        backend_path = tmp_path / "project" / "backend"
        client_path = tmp_path / "project" / "client"

        parent_path.mkdir()
        backend_path.mkdir()
        client_path.mkdir()

        # Use CORRECT storage path: .daem0nmcp/storage
        backend_db = DatabaseManager(str(backend_path / ".daem0nmcp" / "storage"))
        client_db = DatabaseManager(str(client_path / ".daem0nmcp" / "storage"))
        await backend_db.init_db()
        await client_db.init_db()

        backend_mem = MemoryManager(backend_db)
        client_mem = MemoryManager(client_db)

        await backend_mem.remember(
            category="decision",
            content="Use PostgreSQL",
            project_path=str(backend_path),
        )
        await client_mem.remember(
            category="warning",
            content="Don't use localStorage for tokens",
            project_path=str(client_path),
        )

        # Initialize parent database
        parent_db = DatabaseManager(str(parent_path / ".daem0nmcp" / "storage"))
        await parent_db.init_db()

        # Link children to parent
        parent_links = LinkManager(parent_db)
        await parent_links.link_projects(
            str(parent_path), str(backend_path), "same-project"
        )
        await parent_links.link_projects(
            str(parent_path), str(client_path), "same-project"
        )

        # CONSOLIDATE - merge child DBs into parent
        result = await parent_links.consolidate_linked_databases(
            target_path=str(parent_path),
            archive_sources=False,  # Don't archive in test
        )

        assert result["status"] == "consolidated"
        assert result["memories_merged"] >= 2

        # Verify memories are now in parent
        parent_mem = MemoryManager(parent_db)
        stats = await parent_mem.get_statistics()
        assert stats["total_memories"] >= 2

    @pytest.mark.asyncio
    async def test_consolidate_no_links_returns_status(self, tmp_path):
        """Should return no_links status when no projects are linked."""
        from daem0nmcp.database import DatabaseManager
        from daem0nmcp.links import LinkManager

        parent_path = tmp_path / "project"
        parent_path.mkdir()

        parent_db = DatabaseManager(str(parent_path / ".daem0nmcp" / "storage"))
        await parent_db.init_db()

        parent_links = LinkManager(parent_db)
        result = await parent_links.consolidate_linked_databases(
            target_path=str(parent_path)
        )

        assert result["status"] == "no_links"

    @pytest.mark.asyncio
    async def test_consolidate_tracks_merged_from_source(self, tmp_path):
        """Merged memories should have _merged_from in context."""
        from sqlalchemy import select

        from daem0nmcp.database import DatabaseManager
        from daem0nmcp.links import LinkManager
        from daem0nmcp.memory import MemoryManager
        from daem0nmcp.models import Memory

        # Setup parent and child
        parent_path = tmp_path / "project"
        child_path = tmp_path / "project" / "child"
        parent_path.mkdir()
        child_path.mkdir()

        child_db = DatabaseManager(str(child_path / ".daem0nmcp" / "storage"))
        await child_db.init_db()
        child_mem = MemoryManager(child_db)
        await child_mem.remember(
            category="pattern",
            content="Always use async/await",
            project_path=str(child_path),
        )

        parent_db = DatabaseManager(str(parent_path / ".daem0nmcp" / "storage"))
        await parent_db.init_db()

        parent_links = LinkManager(parent_db)
        await parent_links.link_projects(
            str(parent_path), str(child_path), "same-project"
        )

        await parent_links.consolidate_linked_databases(
            target_path=str(parent_path), archive_sources=False
        )

        # Check that merged memory has _merged_from in context
        async with parent_db.get_session() as session:
            result = await session.execute(select(Memory))
            memories = result.scalars().all()

            assert len(memories) >= 1
            merged_mem = [m for m in memories if "async/await" in m.content][0]
            assert merged_mem.context is not None
            assert "_merged_from" in merged_mem.context
            assert merged_mem.context["_merged_from"] == str(child_path)

    @pytest.mark.asyncio
    async def test_consolidate_linked_databases_tool(self, tmp_path):
        """Test the MCP tool for consolidating databases."""
        from daem0nmcp import server
        from daem0nmcp.database import DatabaseManager
        from daem0nmcp.memory import MemoryManager

        server._project_contexts.clear()

        # Setup parent and child
        parent_path = tmp_path / "project"
        child_path = tmp_path / "project" / "child"
        parent_path.mkdir()
        child_path.mkdir()

        # Create child with memories
        child_db = DatabaseManager(str(child_path / ".daem0nmcp" / "storage"))
        await child_db.init_db()
        child_mem = MemoryManager(child_db)
        await child_mem.remember(
            category="decision",
            content="Use Redis for caching",
            project_path=str(child_path),
        )

        # Initialize parent
        parent_db = DatabaseManager(str(parent_path / ".daem0nmcp" / "storage"))
        await parent_db.init_db()

        # Communion and link
        await server.get_briefing(project_path=str(parent_path))
        await server.link_projects(
            linked_path=str(child_path),
            relationship="same-project",
            project_path=str(parent_path),
        )

        # Consolidate via MCP tool
        result = await server.consolidate_linked_databases(
            archive_sources=False, project_path=str(parent_path)
        )

        assert result["status"] == "consolidated"
        assert result["memories_merged"] == 1
        assert str(child_path) in result["sources_processed"]
