"""Tests for Iteration 3: Incremental Indexing."""

import pytest


@pytest.fixture
async def temp_project_with_db(tmp_path):
    """Create a temporary project with database."""
    from daem0nmcp.code_indexer import CodeIndexManager
    from daem0nmcp.database import DatabaseManager

    storage = tmp_path / ".daem0nmcp" / "storage"
    storage.mkdir(parents=True)

    db = DatabaseManager(str(storage))
    await db.init_db()

    indexer = CodeIndexManager(db=db, qdrant=None)

    # Create a sample Python file
    py_file = tmp_path / "main.py"
    py_file.write_text("def hello(): pass")

    yield {
        "project": tmp_path,
        "db": db,
        "indexer": indexer,
        "py_file": py_file,
    }

    await db.close()


class TestFileHashModel:
    """Test FileHash model."""

    def test_file_hash_has_required_fields(self):
        """FileHash should have all required fields."""
        from daem0nmcp.models import FileHash

        fh = FileHash(
            project_path="/test/project", file_path="src/main.py", content_hash="abc123"
        )

        assert fh.project_path == "/test/project"
        assert fh.file_path == "src/main.py"
        assert fh.content_hash == "abc123"


class TestIndexFileIfChanged:
    """Test incremental file indexing."""

    @pytest.mark.asyncio
    async def test_first_index_marks_changed(self, temp_project_with_db):
        """First index should report changed=True."""
        ctx = temp_project_with_db
        result = await ctx["indexer"].index_file_if_changed(
            ctx["py_file"], ctx["project"]
        )
        assert result["changed"] is True

    @pytest.mark.asyncio
    async def test_unchanged_file_not_reindexed(self, temp_project_with_db):
        """Unchanged file should not be re-indexed."""
        ctx = temp_project_with_db

        await ctx["indexer"].index_file_if_changed(ctx["py_file"], ctx["project"])
        result = await ctx["indexer"].index_file_if_changed(
            ctx["py_file"], ctx["project"]
        )

        assert result["changed"] is False
        assert result["reason"] == "unchanged"

    @pytest.mark.asyncio
    async def test_modified_file_reindexed(self, temp_project_with_db):
        """Modified file should be re-indexed."""
        ctx = temp_project_with_db

        await ctx["indexer"].index_file_if_changed(ctx["py_file"], ctx["project"])
        ctx["py_file"].write_text("class NewClass: pass")
        result = await ctx["indexer"].index_file_if_changed(
            ctx["py_file"], ctx["project"]
        )

        assert result["changed"] is True
