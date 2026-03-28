"""Tests for the TODO/FIXME scanner functionality."""

# Import the scanner function directly
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from daem0nmcp.tools.code_tools import TODO_PATTERN, _scan_for_todos


class TestTodoPattern:
    """Test the regex pattern for matching TODOs."""

    def test_matches_python_todo(self):
        """Test matching Python-style TODO comments."""
        line = "# TODO: Fix this bug later"
        matches = TODO_PATTERN.findall(line)
        assert len(matches) == 1
        assert matches[0][0].upper() == "TODO"
        assert "Fix this bug" in matches[0][1]

    def test_matches_fixme(self):
        """Test matching FIXME comments."""
        line = "# FIXME: This is broken"
        matches = TODO_PATTERN.findall(line)
        assert len(matches) == 1
        assert matches[0][0].upper() == "FIXME"

    def test_matches_hack(self):
        """Test matching HACK comments."""
        line = "// HACK: Temporary workaround"
        matches = TODO_PATTERN.findall(line)
        assert len(matches) == 1
        assert matches[0][0].upper() == "HACK"

    def test_matches_without_colon(self):
        """Test matching without colon."""
        line = "# TODO fix authentication"
        matches = TODO_PATTERN.findall(line)
        assert len(matches) == 1
        assert matches[0][0].upper() == "TODO"

    def test_matches_js_style(self):
        """Test matching JS-style comments."""
        line = "// FIXME: Handle edge case"
        matches = TODO_PATTERN.findall(line)
        assert len(matches) == 1

    def test_matches_multiline_start(self):
        """Test matching multiline comment start."""
        line = "/* TODO: Refactor this */"
        matches = TODO_PATTERN.findall(line)
        assert len(matches) == 1

    def test_case_insensitive(self):
        """Test case insensitive matching."""
        line = "# todo: lowercase todo"
        matches = TODO_PATTERN.findall(line)
        assert len(matches) == 1
        assert matches[0][0].upper() == "TODO"


class TestScanForTodos:
    """Test the scanner function."""

    def test_scan_empty_directory(self):
        """Test scanning an empty directory."""
        with tempfile.TemporaryDirectory() as tmpdir:
            result = _scan_for_todos(tmpdir)
            assert result == []

    def test_scan_finds_todos(self):
        """Test that scanner finds TODO comments."""
        with tempfile.TemporaryDirectory() as tmpdir:
            # Create a Python file with TODOs
            test_file = Path(tmpdir) / "test.py"
            test_file.write_text("""
# TODO: Add error handling
def foo():
    pass  # FIXME: Implement this

# HACK: Temporary fix
x = 1
""")
            result = _scan_for_todos(tmpdir)
            assert len(result) == 3
            types = {r["type"] for r in result}
            assert "TODO" in types
            assert "FIXME" in types
            assert "HACK" in types

    def test_scan_includes_line_numbers(self):
        """Test that scanner includes line numbers."""
        with tempfile.TemporaryDirectory() as tmpdir:
            test_file = Path(tmpdir) / "test.py"
            test_file.write_text("# Line 1\n# TODO: On line 2\n# Line 3\n")
            result = _scan_for_todos(tmpdir)
            assert len(result) == 1
            assert result[0]["line"] == 2

    def test_scan_skips_node_modules(self):
        """Test that scanner skips node_modules."""
        with tempfile.TemporaryDirectory() as tmpdir:
            # Create file in root
            root_file = Path(tmpdir) / "index.js"
            root_file.write_text("// TODO: Root todo")

            # Create file in node_modules
            nm_dir = Path(tmpdir) / "node_modules" / "some_pkg"
            nm_dir.mkdir(parents=True)
            nm_file = nm_dir / "index.js"
            nm_file.write_text("// TODO: Should be ignored")

            result = _scan_for_todos(tmpdir)
            assert len(result) == 1
            assert result[0]["file"] == "index.js"

    def test_scan_skips_pycache(self):
        """Test that scanner skips __pycache__."""
        with tempfile.TemporaryDirectory() as tmpdir:
            # Create file in __pycache__
            cache_dir = Path(tmpdir) / "__pycache__"
            cache_dir.mkdir()
            cache_file = cache_dir / "module.cpython-39.py"
            cache_file.write_text("# TODO: Ignored")

            result = _scan_for_todos(tmpdir)
            assert len(result) == 0

    def test_scan_respects_extensions(self):
        """Test that scanner only scans known extensions."""
        with tempfile.TemporaryDirectory() as tmpdir:
            # Create files with different extensions
            py_file = Path(tmpdir) / "code.py"
            py_file.write_text("# TODO: Python todo")

            bin_file = Path(tmpdir) / "data.bin"
            bin_file.write_text("# TODO: Should be ignored")

            result = _scan_for_todos(tmpdir)
            assert len(result) == 1
            assert result[0]["file"] == "code.py"

    def test_scan_truncates_long_content(self):
        """Test that scanner truncates very long TODO content."""
        with tempfile.TemporaryDirectory() as tmpdir:
            test_file = Path(tmpdir) / "test.py"
            long_text = "x" * 300
            test_file.write_text(f"# TODO: {long_text}")

            result = _scan_for_todos(tmpdir)
            assert len(result) == 1
            assert len(result[0]["content"]) <= 200

    def test_scan_nonexistent_path(self):
        """Test scanning a path that doesn't exist."""
        result = _scan_for_todos("/nonexistent/path/12345")
        assert result == []
