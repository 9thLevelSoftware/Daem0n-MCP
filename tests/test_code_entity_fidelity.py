"""Tests for Iteration 2: Code Entity Fidelity."""

import pytest


@pytest.fixture
def temp_project(tmp_path):
    """Create a temporary project directory."""
    return tmp_path


@pytest.fixture
def nested_python(temp_project):
    """Create a Python file with nested classes."""
    code = '''
class User:
    """User model."""

    class Profile:
        """Nested profile."""

        def update(self):
            pass

    def save(self):
        pass

def helper():
    pass
'''
    py_file = temp_project / "models.py"
    py_file.write_text(code)
    return py_file


class TestQualifiedNames:
    """Test qualified name computation."""

    def test_qualified_name_nested_class_method(self, temp_project, nested_python):
        """Nested method has fully qualified name."""
        from daem0nmcp.code_indexer import TreeSitterIndexer

        indexer = TreeSitterIndexer()
        if not indexer.available:
            pytest.skip("tree-sitter not available")

        entities = list(indexer.index_file(nested_python, temp_project))

        update_method = next((e for e in entities if e["name"] == "update"), None)
        assert update_method is not None
        assert "qualified_name" in update_method
        assert "User" in update_method["qualified_name"]
        assert "Profile" in update_method["qualified_name"]

    def test_qualified_name_top_level_function(self, temp_project, nested_python):
        """Top-level function has module-prefixed name."""
        from daem0nmcp.code_indexer import TreeSitterIndexer

        indexer = TreeSitterIndexer()
        if not indexer.available:
            pytest.skip("tree-sitter not available")

        entities = list(indexer.index_file(nested_python, temp_project))

        helper_func = next((e for e in entities if e["name"] == "helper"), None)
        assert helper_func is not None
        assert "qualified_name" in helper_func
        assert "models" in helper_func["qualified_name"]


class TestNestedFunctionQualifiedNames:
    """Test qualified names for nested functions (duplicate ID bug fix)."""

    def test_nested_functions_same_name_get_unique_ids(self, temp_project):
        """Nested functions with the same name in different parents must have unique IDs."""
        from daem0nmcp.code_indexer import TreeSitterIndexer

        indexer = TreeSitterIndexer()
        if not indexer.available:
            pytest.skip("tree-sitter not available")

        py_file = temp_project / "utils.py"
        py_file.write_text("""
def outer_one():
    def helper():
        pass

def outer_two():
    def helper():
        pass
""")
        entities = list(indexer.index_file(py_file, temp_project))

        helpers = [e for e in entities if e["name"] == "helper"]
        assert len(helpers) == 2, f"Expected 2 helper entities, got {len(helpers)}"

        # Qualified names must differ
        qnames = {e["qualified_name"] for e in helpers}
        assert len(qnames) == 2, f"Qualified names collide: {qnames}"
        assert any("outer_one" in q for q in qnames), (
            f"Missing outer_one scope: {qnames}"
        )
        assert any("outer_two" in q for q in qnames), (
            f"Missing outer_two scope: {qnames}"
        )

        # IDs must differ
        ids = {e["id"] for e in helpers}
        assert len(ids) == 2, f"Entity IDs collide: {ids}"

    def test_nested_function_qualified_name_includes_parent(self, temp_project):
        """A function nested inside another includes the parent in its qualified name."""
        from daem0nmcp.code_indexer import TreeSitterIndexer

        indexer = TreeSitterIndexer()
        if not indexer.available:
            pytest.skip("tree-sitter not available")

        py_file = temp_project / "service.py"
        py_file.write_text("""
def process():
    def validate():
        pass
""")
        entities = list(indexer.index_file(py_file, temp_project))

        validate = next((e for e in entities if e["name"] == "validate"), None)
        assert validate is not None
        assert "process" in validate["qualified_name"], (
            f"Expected 'process' in qualified_name, got: {validate['qualified_name']}"
        )

    def test_deeply_nested_functions(self, temp_project):
        """Triple-nested functions get fully qualified names."""
        from daem0nmcp.code_indexer import TreeSitterIndexer

        indexer = TreeSitterIndexer()
        if not indexer.available:
            pytest.skip("tree-sitter not available")

        py_file = temp_project / "deep.py"
        py_file.write_text("""
def level_one():
    def level_two():
        def level_three():
            pass
""")
        entities = list(indexer.index_file(py_file, temp_project))

        l3 = next((e for e in entities if e["name"] == "level_three"), None)
        assert l3 is not None
        qn = l3["qualified_name"]
        assert "level_one" in qn, f"Missing level_one in: {qn}"
        assert "level_two" in qn, f"Missing level_two in: {qn}"
        assert "level_three" in qn, f"Missing level_three in: {qn}"


class TestStableEntityIDs:
    """Test entity ID stability across line changes."""

    def test_entity_id_stable_after_line_change(self, temp_project):
        """Adding lines should NOT change entity IDs."""
        from daem0nmcp.code_indexer import TreeSitterIndexer

        indexer = TreeSitterIndexer()
        if not indexer.available:
            pytest.skip("tree-sitter not available")

        py_file = temp_project / "service.py"
        py_file.write_text("class UserService:\n    def authenticate(self): pass")

        entities1 = list(indexer.index_file(py_file, temp_project))

        # Add lines before (shifts line numbers)
        py_file.write_text(
            "# comment\n# another\nclass UserService:\n    def authenticate(self): pass"
        )

        entities2 = list(indexer.index_file(py_file, temp_project))

        # IDs should be the same
        for e1 in entities1:
            matching = [
                e2
                for e2 in entities2
                if e2["name"] == e1["name"] and e2["entity_type"] == e1["entity_type"]
            ]
            assert len(matching) == 1, f"No match for {e1['name']}"
            assert matching[0]["id"] == e1["id"], f"ID changed for {e1['name']}"


class TestImportExtraction:
    """Test import extraction from AST."""

    def test_python_imports_extracted(self, temp_project):
        """Python imports should be extracted."""
        from daem0nmcp.code_indexer import TreeSitterIndexer

        indexer = TreeSitterIndexer()
        if not indexer.available:
            pytest.skip("tree-sitter not available")

        py_file = temp_project / "app.py"
        py_file.write_text("import os\nfrom pathlib import Path\ndef main(): pass")

        entities = list(indexer.index_file(py_file, temp_project))

        main_func = next((e for e in entities if e["name"] == "main"), None)
        assert main_func is not None

        imports = main_func.get("imports", [])
        assert len(imports) > 0, "No imports extracted"
        assert "os" in imports or any("os" in i for i in imports)
