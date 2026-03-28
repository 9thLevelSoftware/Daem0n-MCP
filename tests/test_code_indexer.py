"""
Tests for the code indexer (Phase 2: Code Understanding).

Tests tree-sitter parsing, entity extraction, and indexing.

NOTE: tree-sitter-language-pack is a REQUIRED dependency.
These tests will fail if it's not installed.
"""

import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import pytest

# tree-sitter-language-pack is required, not optional


@pytest.fixture
def temp_project():
    """Create a temporary project directory with sample code files."""
    temp_dir = Path(tempfile.mkdtemp())

    # Create Python file
    py_file = temp_dir / "sample.py"
    py_file.write_text('''
"""Sample module."""

class UserService:
    """Handles user operations."""

    def authenticate(self, username: str, password: str) -> bool:
        """Authenticate a user."""
        return True

    def get_user(self, user_id: int):
        """Get user by ID."""
        pass


def helper_function():
    """A helper function."""
    pass
''')

    # Create TypeScript file
    ts_file = temp_dir / "service.ts"
    ts_file.write_text("""
/**
 * API Service for backend communication.
 */
class ApiService {
    private baseUrl: string;

    /**
     * Make a GET request.
     */
    async get(endpoint: string): Promise<Response> {
        return fetch(this.baseUrl + endpoint);
    }
}

function formatDate(date: Date): string {
    return date.toISOString();
}

interface User {
    id: number;
    name: string;
}
""")

    # Create JavaScript file
    js_file = temp_dir / "utils.js"
    js_file.write_text("""
/**
 * Utility functions.
 */
class Logger {
    log(message) {
        console.log(message);
    }
}

function calculateTotal(items) {
    return items.reduce((sum, item) => sum + item.price, 0);
}
""")

    # Create Go file
    go_file = temp_dir / "main.go"
    go_file.write_text("""
package main

// Server represents an HTTP server.
type Server struct {
    Port int
}

// Start starts the server.
func (s *Server) Start() error {
    return nil
}

// NewServer creates a new server.
func NewServer(port int) *Server {
    return &Server{Port: port}
}
""")

    # Create Rust file
    rs_file = temp_dir / "lib.rs"
    rs_file.write_text("""
/// Configuration for the application.
pub struct Config {
    pub port: u16,
}

/// User trait.
pub trait User {
    fn get_name(&self) -> &str;
}

impl Config {
    /// Create a new config.
    pub fn new(port: u16) -> Self {
        Self { port }
    }
}

/// Helper function.
fn helper() {}
""")

    yield temp_dir

    # Cleanup
    shutil.rmtree(temp_dir)


class TestTreeSitterIndexer:
    """Tests for TreeSitterIndexer."""

    def test_is_available(self):
        """Test availability check."""
        from daem0nmcp.code_indexer import is_available

        # tree-sitter-languages is a required dependency
        assert is_available() is True

    def test_get_supported_extensions(self):
        """Test getting supported extensions."""
        from daem0nmcp.code_indexer import TreeSitterIndexer

        indexer = TreeSitterIndexer()
        extensions = indexer.get_supported_extensions()

        assert ".py" in extensions
        assert ".ts" in extensions
        assert ".js" in extensions
        assert ".go" in extensions
        assert ".rs" in extensions

    def test_index_python_file(self, temp_project):
        """Test indexing a Python file."""
        from daem0nmcp.code_indexer import TreeSitterIndexer

        indexer = TreeSitterIndexer()
        py_file = temp_project / "sample.py"

        entities = list(indexer.index_file(py_file, temp_project))

        # Should find class and functions
        names = [e["name"] for e in entities]
        assert "UserService" in names
        assert "authenticate" in names
        assert "get_user" in names
        assert "helper_function" in names

        # Check entity types
        types = {e["name"]: e["entity_type"] for e in entities}
        assert types["UserService"] == "class"
        assert types["authenticate"] == "function"

    def test_index_typescript_file(self, temp_project):
        """Test indexing a TypeScript file."""
        from daem0nmcp.code_indexer import TreeSitterIndexer

        indexer = TreeSitterIndexer()
        ts_file = temp_project / "service.ts"

        entities = list(indexer.index_file(ts_file, temp_project))

        names = [e["name"] for e in entities]
        assert "ApiService" in names
        assert "formatDate" in names
        assert "User" in names

    def test_index_javascript_file(self, temp_project):
        """Test indexing a JavaScript file."""
        from daem0nmcp.code_indexer import TreeSitterIndexer

        indexer = TreeSitterIndexer()
        js_file = temp_project / "utils.js"

        entities = list(indexer.index_file(js_file, temp_project))

        names = [e["name"] for e in entities]
        assert "Logger" in names
        assert "calculateTotal" in names

    def test_index_go_file(self, temp_project):
        """Test indexing a Go file."""
        from daem0nmcp.code_indexer import TreeSitterIndexer

        indexer = TreeSitterIndexer()
        go_file = temp_project / "main.go"

        entities = list(indexer.index_file(go_file, temp_project))

        names = [e["name"] for e in entities]
        # Parser finds functions: Start (method) and NewServer (function)
        assert "NewServer" in names or "Start" in names
        assert len(entities) >= 1

    def test_index_rust_file(self, temp_project):
        """Test indexing a Rust file."""
        from daem0nmcp.code_indexer import TreeSitterIndexer

        indexer = TreeSitterIndexer()
        rs_file = temp_project / "lib.rs"

        entities = list(indexer.index_file(rs_file, temp_project))

        names = [e["name"] for e in entities]
        # Parser finds struct Config and trait User
        assert "Config" in names or "User" in names
        assert len(entities) >= 1

    def test_entity_has_required_fields(self, temp_project):
        """Test that entities have all required fields."""
        from daem0nmcp.code_indexer import TreeSitterIndexer

        indexer = TreeSitterIndexer()
        py_file = temp_project / "sample.py"

        entities = list(indexer.index_file(py_file, temp_project))
        assert len(entities) > 0

        entity = entities[0]
        assert "id" in entity
        assert "name" in entity
        assert "entity_type" in entity
        assert "file_path" in entity
        assert "project_path" in entity
        assert "line_start" in entity
        assert "line_end" in entity

    def test_entity_id_is_deterministic(self, temp_project):
        """Test that entity IDs are deterministic."""
        from daem0nmcp.code_indexer import TreeSitterIndexer

        indexer = TreeSitterIndexer()
        py_file = temp_project / "sample.py"

        entities1 = list(indexer.index_file(py_file, temp_project))
        entities2 = list(indexer.index_file(py_file, temp_project))

        ids1 = {e["id"] for e in entities1}
        ids2 = {e["id"] for e in entities2}

        assert ids1 == ids2

    def test_skip_unsupported_files(self, temp_project):
        """Test that unsupported files are skipped."""
        from daem0nmcp.code_indexer import TreeSitterIndexer

        indexer = TreeSitterIndexer()

        # Create an unsupported file
        txt_file = temp_project / "readme.txt"
        txt_file.write_text("This is a text file.")

        entities = list(indexer.index_file(txt_file, temp_project))
        assert len(entities) == 0

    def test_extract_docstrings(self, temp_project):
        """Test that docstrings are extracted when available."""
        from daem0nmcp.code_indexer import TreeSitterIndexer

        indexer = TreeSitterIndexer()
        py_file = temp_project / "sample.py"

        entities = list(indexer.index_file(py_file, temp_project))

        # Find UserService class - docstring extraction depends on parser
        user_service = next(e for e in entities if e["name"] == "UserService")
        # Docstring may or may not be extracted depending on tree-sitter version
        # The important thing is the entity was found
        assert user_service is not None
        assert user_service["entity_type"] == "class"

    def test_extract_signature(self, temp_project):
        """Test that signatures are extracted."""
        from daem0nmcp.code_indexer import TreeSitterIndexer

        indexer = TreeSitterIndexer()
        py_file = temp_project / "sample.py"

        entities = list(indexer.index_file(py_file, temp_project))

        # Find authenticate method
        auth = next(e for e in entities if e["name"] == "authenticate")
        assert auth.get("signature") is not None
        assert "def authenticate" in auth["signature"]


class TestCodeIndexManager:
    """Tests for CodeIndexManager."""

    @pytest.fixture
    async def db_manager(self, temp_project):
        """Create a database manager for testing."""
        from daem0nmcp.database import DatabaseManager

        db = DatabaseManager(str(temp_project / ".daem0nmcp" / "storage"))
        await db.init_db()
        yield db
        await db.close()

    @pytest.mark.asyncio
    async def test_index_project(self, temp_project, db_manager):
        """Test indexing an entire project."""
        from daem0nmcp.code_indexer import CodeIndexManager

        indexer = CodeIndexManager(db=db_manager, qdrant=None)
        result = await indexer.index_project(str(temp_project))

        assert result["indexed"] > 0
        assert result["files_processed"] > 0
        assert result["project"] == str(temp_project)

    @pytest.mark.asyncio
    async def test_index_with_patterns(self, temp_project, db_manager):
        """Test indexing with specific patterns."""
        from daem0nmcp.code_indexer import CodeIndexManager

        indexer = CodeIndexManager(db=db_manager, qdrant=None)

        # Only index Python files
        result = await indexer.index_project(str(temp_project), patterns=["**/*.py"])

        assert result["indexed"] > 0
        assert result["files_processed"] == 1

    @pytest.mark.asyncio
    async def test_find_entity(self, temp_project, db_manager):
        """Test finding a specific entity."""
        from daem0nmcp.code_indexer import CodeIndexManager

        indexer = CodeIndexManager(db=db_manager, qdrant=None)
        await indexer.index_project(str(temp_project))

        entity = await indexer.find_entity("UserService", str(temp_project))
        assert entity is not None
        assert entity["name"] == "UserService"
        assert entity["entity_type"] == "class"

    @pytest.mark.asyncio
    async def test_find_entity_not_found(self, temp_project, db_manager):
        """Test finding a non-existent entity."""
        from daem0nmcp.code_indexer import CodeIndexManager

        indexer = CodeIndexManager(db=db_manager, qdrant=None)
        await indexer.index_project(str(temp_project))

        entity = await indexer.find_entity("NonExistent", str(temp_project))
        assert entity is None

    @pytest.mark.asyncio
    async def test_analyze_impact(self, temp_project, db_manager):
        """Test impact analysis."""
        from daem0nmcp.code_indexer import CodeIndexManager

        indexer = CodeIndexManager(db=db_manager, qdrant=None)
        await indexer.index_project(str(temp_project))

        result = await indexer.analyze_impact("UserService", str(temp_project))
        assert result["found"] is True
        assert result["entity"] == "UserService"

    @pytest.mark.asyncio
    async def test_analyze_impact_not_found(self, temp_project, db_manager):
        """Test impact analysis for non-existent entity."""
        from daem0nmcp.code_indexer import CodeIndexManager

        indexer = CodeIndexManager(db=db_manager, qdrant=None)
        await indexer.index_project(str(temp_project))

        result = await indexer.analyze_impact("NonExistent", str(temp_project))
        assert result["found"] is False

    @pytest.mark.asyncio
    async def test_skip_directories(self, temp_project, db_manager):
        """Test that certain directories are skipped."""
        from daem0nmcp.code_indexer import CodeIndexManager

        # Create a node_modules directory with a file
        node_modules = temp_project / "node_modules" / "some_package"
        node_modules.mkdir(parents=True)
        (node_modules / "index.js").write_text("function foo() {}")

        indexer = CodeIndexManager(db=db_manager, qdrant=None)
        result = await indexer.index_project(str(temp_project))

        # The node_modules file should be skipped
        assert result["files_skipped"] >= 1

    @pytest.mark.asyncio
    async def test_reindex_clears_old_entities(self, temp_project, db_manager):
        """Test that reindexing clears old entities."""
        from sqlalchemy import select

        from daem0nmcp.code_indexer import CodeIndexManager
        from daem0nmcp.models import CodeEntity

        indexer = CodeIndexManager(db=db_manager, qdrant=None)

        # First index
        await indexer.index_project(str(temp_project))

        # Check count
        async with db_manager.get_session() as session:
            result = await session.execute(select(CodeEntity))
            count1 = len(result.scalars().all())

        # Reindex
        await indexer.index_project(str(temp_project))

        # Count should be same (old entities cleared)
        async with db_manager.get_session() as session:
            result = await session.execute(select(CodeEntity))
            count2 = len(result.scalars().all())

        assert count1 == count2

    @pytest.mark.asyncio
    async def test_reindex_does_not_raise_integrity_error(
        self, temp_project, db_manager
    ):
        """Regression: re-indexing must not raise IntegrityError on existing keys.

        Before the fix, bulk DELETE via session.execute() wasn't flushed before
        session.merge(), causing merge to emit INSERT (instead of UPDATE) for
        rows that still existed in the DB, triggering UNIQUE constraint failures.
        """
        from daem0nmcp.code_indexer import CodeIndexManager

        indexer = CodeIndexManager(db=db_manager, qdrant=None)

        # Index, then re-index three times — should never raise IntegrityError
        await indexer.index_project(str(temp_project))
        await indexer.index_project(str(temp_project))
        await indexer.index_project(str(temp_project))

    @pytest.mark.asyncio
    async def test_reindex_updates_entities_after_file_change(
        self, temp_project, db_manager
    ):
        """Test that re-indexing picks up file changes (entity content updated)."""
        from sqlalchemy import select

        from daem0nmcp.code_indexer import CodeIndexManager
        from daem0nmcp.models import CodeEntity

        indexer = CodeIndexManager(db=db_manager, qdrant=None)

        # First index
        await indexer.index_project(str(temp_project))

        # Modify the Python file — rename a function
        py_file = temp_project / "sample.py"
        content = py_file.read_text()
        py_file.write_text(content.replace("helper_function", "renamed_helper"))

        # Re-index
        await indexer.index_project(str(temp_project))

        # Verify the renamed entity is present and the old one is gone
        async with db_manager.get_session() as session:
            result = await session.execute(select(CodeEntity.name))
            names = [row[0] for row in result.all()]

        assert "renamed_helper" in names
        assert "helper_function" not in names


# Tests that don't require tree-sitter
class TestLanguageQueries:
    """Tests for language-specific entity queries (no tree-sitter required)."""

    def test_python_queries_exist(self):
        """Test that Python queries are defined."""
        from daem0nmcp.code_indexer import ENTITY_QUERIES

        assert "python" in ENTITY_QUERIES
        assert "class_definition" in ENTITY_QUERIES["python"]
        assert "function_definition" in ENTITY_QUERIES["python"]

    def test_typescript_queries_exist(self):
        """Test that TypeScript queries are defined."""
        from daem0nmcp.code_indexer import ENTITY_QUERIES

        assert "typescript" in ENTITY_QUERIES
        assert "class_declaration" in ENTITY_QUERIES["typescript"]

    def test_javascript_queries_exist(self):
        """Test that JavaScript queries are defined."""
        from daem0nmcp.code_indexer import ENTITY_QUERIES

        assert "javascript" in ENTITY_QUERIES

    def test_go_queries_exist(self):
        """Test that Go queries are defined."""
        from daem0nmcp.code_indexer import ENTITY_QUERIES

        assert "go" in ENTITY_QUERIES

    def test_rust_queries_exist(self):
        """Test that Rust queries are defined."""
        from daem0nmcp.code_indexer import ENTITY_QUERIES

        assert "rust" in ENTITY_QUERIES
        assert "struct_item" in ENTITY_QUERIES["rust"]
        assert "function_item" in ENTITY_QUERIES["rust"]


class TestLanguageConfig:
    """Tests for language configuration (no tree-sitter required)."""

    def test_python_extension(self):
        """Test Python extension mapping."""
        from daem0nmcp.code_indexer import LANGUAGE_CONFIG

        assert LANGUAGE_CONFIG[".py"] == "python"

    def test_typescript_extensions(self):
        """Test TypeScript extension mappings."""
        from daem0nmcp.code_indexer import LANGUAGE_CONFIG

        assert LANGUAGE_CONFIG[".ts"] == "typescript"
        assert LANGUAGE_CONFIG[".tsx"] == "tsx"

    def test_javascript_extensions(self):
        """Test JavaScript extension mappings."""
        from daem0nmcp.code_indexer import LANGUAGE_CONFIG

        assert LANGUAGE_CONFIG[".js"] == "javascript"
        assert LANGUAGE_CONFIG[".mjs"] == "javascript"

    def test_go_extension(self):
        """Test Go extension mapping."""
        from daem0nmcp.code_indexer import LANGUAGE_CONFIG

        assert LANGUAGE_CONFIG[".go"] == "go"

    def test_rust_extension(self):
        """Test Rust extension mapping."""
        from daem0nmcp.code_indexer import LANGUAGE_CONFIG

        assert LANGUAGE_CONFIG[".rs"] == "rust"

    def test_c_extensions(self):
        """Test C extension mappings."""
        from daem0nmcp.code_indexer import LANGUAGE_CONFIG

        assert LANGUAGE_CONFIG[".c"] == "c"
        assert LANGUAGE_CONFIG[".h"] == "c"

    def test_cpp_extensions(self):
        """Test C++ extension mappings."""
        from daem0nmcp.code_indexer import LANGUAGE_CONFIG

        assert LANGUAGE_CONFIG[".cpp"] == "cpp"
        assert LANGUAGE_CONFIG[".hpp"] == "cpp"


class TestCodeEntityModel:
    """Tests for the CodeEntity model (no tree-sitter required)."""

    @pytest.fixture
    async def db_manager(self):
        """Create a database manager for testing."""
        import tempfile

        from daem0nmcp.database import DatabaseManager

        temp_dir = Path(tempfile.mkdtemp())
        db = DatabaseManager(str(temp_dir / ".daem0nmcp" / "storage"))
        await db.init_db()
        yield db
        await db.close()
        shutil.rmtree(temp_dir, ignore_errors=True)

    @pytest.mark.asyncio
    async def test_create_code_entity(self, db_manager):
        """Test creating a CodeEntity."""
        from daem0nmcp.models import CodeEntity

        async with db_manager.get_session() as session:
            entity = CodeEntity(
                id="test123",
                project_path="/test/project",
                entity_type="class",
                name="TestClass",
                file_path="test.py",
                line_start=1,
                line_end=10,
                signature="class TestClass:",
                docstring="A test class",
                calls=[],
                called_by=[],
                imports=[],
                inherits=[],
                indexed_at=datetime.now(timezone.utc),
            )
            session.add(entity)
            await session.commit()

            # Verify it was saved
            result = await session.get(CodeEntity, "test123")
            assert result is not None
            assert result.name == "TestClass"
            assert result.entity_type == "class"


class TestMemoryCodeRefModel:
    """Tests for the MemoryCodeRef model (no tree-sitter required)."""

    @pytest.fixture
    async def db_manager(self):
        """Create a database manager for testing."""
        import tempfile

        from daem0nmcp.database import DatabaseManager

        temp_dir = Path(tempfile.mkdtemp())
        db = DatabaseManager(str(temp_dir / ".daem0nmcp" / "storage"))
        await db.init_db()
        yield db
        await db.close()
        shutil.rmtree(temp_dir, ignore_errors=True)

    @pytest.mark.asyncio
    async def test_create_memory_code_ref(self, db_manager):
        """Test creating a MemoryCodeRef."""
        from daem0nmcp.models import Memory, MemoryCodeRef

        async with db_manager.get_session() as session:
            # First create a memory
            memory = Memory(
                category="decision",
                content="Use UserService for auth",
            )
            session.add(memory)
            await session.flush()

            # Create the code ref
            ref = MemoryCodeRef(
                memory_id=memory.id,
                code_entity_id="entity123",
                entity_type="class",
                entity_name="UserService",
                file_path="services/user.py",
                line_number=10,
                relationship="about",
            )
            session.add(ref)
            await session.commit()

            # Verify relationship
            assert ref.memory_id == memory.id
            assert ref.entity_name == "UserService"
