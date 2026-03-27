"""Tests for Iteration 4: Performance & UX."""

import pytest


class TestParseTreeCache:
    """Test parse tree caching."""

    def test_cache_hit_on_unchanged_file(self, tmp_path):
        """Second parse should hit cache."""
        from daem0nmcp.code_indexer import TreeSitterIndexer

        indexer = TreeSitterIndexer()
        if not indexer.available:
            pytest.skip("tree-sitter not available")

        py_file = tmp_path / "sample.py"
        py_file.write_text("def hello(): pass")

        # First parse - miss
        list(indexer.index_file(py_file, tmp_path))
        assert indexer.cache_stats["misses"] >= 1

        # Second parse - hit
        list(indexer.index_file(py_file, tmp_path))
        assert indexer.cache_stats["hits"] >= 1

    def test_cache_invalidation_on_change(self, tmp_path):
        """Changed file should invalidate cache."""
        from daem0nmcp.code_indexer import TreeSitterIndexer

        indexer = TreeSitterIndexer()
        if not indexer.available:
            pytest.skip("tree-sitter not available")

        py_file = tmp_path / "sample.py"
        py_file.write_text("def hello(): pass")
        list(indexer.index_file(py_file, tmp_path))

        py_file.write_text("def goodbye(): pass")
        list(indexer.index_file(py_file, tmp_path))

        # Both should be misses (content changed)
        assert indexer.cache_stats["misses"] >= 2


class TestExtendedConfig:
    """Test extended configuration options."""

    def test_default_embedding_model(self):
        """Default embedding model is nomic-ai/modernbert-embed-base."""
        from daem0nmcp.config import Settings

        settings = Settings()
        assert settings.embedding_model == "nomic-ai/modernbert-embed-base"
        assert settings.embedding_dimension == 256
        assert settings.embedding_backend in ("onnx", "torch")
        assert settings.embedding_query_prefix == "search_query: "
        assert settings.embedding_document_prefix == "search_document: "

    def test_default_parse_cache_maxsize(self):
        """Default parse cache maxsize is 200."""
        from daem0nmcp.config import Settings

        settings = Settings()
        assert settings.parse_tree_cache_maxsize == 200

    def test_config_from_env(self, monkeypatch):
        """Config can be set via environment."""
        monkeypatch.setenv("DAEM0NMCP_PARSE_TREE_CACHE_MAXSIZE", "500")
        from daem0nmcp.config import Settings

        settings = Settings()
        assert settings.parse_tree_cache_maxsize == 500

    def test_default_index_languages(self):
        """Default index_languages is empty list."""
        from daem0nmcp.config import Settings

        settings = Settings()
        assert settings.index_languages == []


@pytest.fixture
async def covenant_compliant_project(tmp_path):
    """Create a covenant-compliant project for testing."""
    from daem0nmcp.database import DatabaseManager

    storage = tmp_path / ".daem0nmcp" / "storage"
    storage.mkdir(parents=True)

    db = DatabaseManager(str(storage))
    await db.init_db()

    yield str(tmp_path)

    await db.close()


class TestEnhancedHealth:
    """Test enhanced health tool."""

    @pytest.mark.asyncio
    async def test_health_includes_code_entities(self, covenant_compliant_project):
        """Health should include code entity stats."""
        from daem0nmcp import server

        result = await server.health(project_path=covenant_compliant_project)

        assert "code_entities_count" in result
        assert "last_indexed_at" in result
        assert "index_stale" in result
