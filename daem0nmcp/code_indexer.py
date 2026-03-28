"""
Code Indexer - Multi-language AST parsing for code understanding.

Phase 2 of Cognitive Architecture: Daem0n understands project structure
and can answer "what depends on X?"

Uses tree-sitter-language-pack for cross-language parsing without compilation.
(Supports Python 3.14+ with pre-built wheels)
"""

import asyncio
import hashlib
import logging
from collections.abc import Generator
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import settings

logger = logging.getLogger(__name__)

# Language configuration: file extension -> tree-sitter language name
LANGUAGE_CONFIG = {
    ".py": "python",
    ".js": "javascript",
    ".mjs": "javascript",
    ".cjs": "javascript",
    ".ts": "typescript",
    ".tsx": "tsx",
    ".go": "go",
    ".rs": "rust",
    ".java": "java",
    ".kt": "kotlin",
    ".kts": "kotlin",
    ".rb": "ruby",
    ".php": "php",
    ".c": "c",
    ".h": "c",
    ".cpp": "cpp",
    ".hpp": "cpp",
    ".cc": "cpp",
    ".cs": "c_sharp",
}

# Tree-sitter queries for extracting entities (language-specific)
# These patterns capture function definitions, class definitions, etc.
ENTITY_QUERIES = {
    "python": """
        (class_definition
            name: (identifier) @class.name) @class.def
        (function_definition
            name: (identifier) @function.name) @function.def
    """,
    "typescript": """
        (class_declaration
            name: (type_identifier) @class.name) @class.def
        (function_declaration
            name: (identifier) @function.name) @function.def
        (method_definition
            name: (property_identifier) @method.name) @method.def
        (interface_declaration
            name: (type_identifier) @interface.name) @interface.def
    """,
    "javascript": """
        (class_declaration
            name: (identifier) @class.name) @class.def
        (function_declaration
            name: (identifier) @function.name) @function.def
        (method_definition
            name: (property_identifier) @method.name) @method.def
    """,
    "tsx": """
        (class_declaration
            name: (type_identifier) @class.name) @class.def
        (function_declaration
            name: (identifier) @function.name) @function.def
        (method_definition
            name: (property_identifier) @method.name) @method.def
    """,
    "go": """
        (type_declaration
            (type_spec
                name: (type_identifier) @class.name)) @class.def
        (function_declaration
            name: (identifier) @function.name) @function.def
        (method_declaration
            name: (field_identifier) @method.name) @method.def
    """,
    "rust": """
        (struct_item
            name: (type_identifier) @class.name) @class.def
        (enum_item
            name: (type_identifier) @enum.name) @enum.def
        (impl_item
            type: (type_identifier) @impl.name) @impl.def
        (function_item
            name: (identifier) @function.name) @function.def
        (trait_item
            name: (type_identifier) @trait.name) @trait.def
    """,
    "java": """
        (class_declaration
            name: (identifier) @class.name) @class.def
        (interface_declaration
            name: (identifier) @interface.name) @interface.def
        (method_declaration
            name: (identifier) @method.name) @method.def
    """,
    "c": """
        (function_definition
            declarator: (function_declarator
                declarator: (identifier) @function.name)) @function.def
        (struct_specifier
            name: (type_identifier) @struct.name) @struct.def
    """,
    "cpp": """
        (function_definition
            declarator: (function_declarator
                declarator: (identifier) @function.name)) @function.def
        (class_specifier
            name: (type_identifier) @class.name) @class.def
        (struct_specifier
            name: (type_identifier) @struct.name) @struct.def
    """,
    "c_sharp": """
        (class_declaration
            name: (identifier) @class.name) @class.def
        (interface_declaration
            name: (identifier) @interface.name) @interface.def
        (method_declaration
            name: (identifier) @method.name) @method.def
    """,
    "ruby": """
        (class
            name: (constant) @class.name) @class.def
        (method
            name: (identifier) @method.name) @method.def
        (singleton_method
            name: (identifier) @method.name) @method.def
    """,
    "php": """
        (class_declaration
            name: (name) @class.name) @class.def
        (function_definition
            name: (name) @function.name) @function.def
        (method_declaration
            name: (name) @method.name) @method.def
    """,
    "kotlin": """
        (class_declaration
            (type_identifier) @class.name) @class.def
        (object_declaration
            (type_identifier) @class.name) @class.def
        (function_declaration
            (simple_identifier) @function.name) @function.def
    """,
}

# Import extraction queries per language
IMPORT_QUERIES = {
    "python": """
        (import_statement
            name: (dotted_name) @import.name) @import.def
        (import_from_statement
            module_name: (dotted_name) @import.module) @import.def
    """,
    "typescript": """
        (import_statement
            source: (string) @import.source) @import.def
    """,
    "javascript": """
        (import_statement
            source: (string) @import.source) @import.def
    """,
}


def _check_tree_sitter_available() -> bool:
    """Check if tree-sitter-language-pack is available."""
    import importlib.util

    return importlib.util.find_spec("tree_sitter_language_pack") is not None


def is_available() -> bool:
    """Check if code indexer is available (tree-sitter installed)."""
    return _check_tree_sitter_available()


class TreeSitterIndexer:
    """
    Universal code indexer using tree-sitter.

    Supports multiple languages through tree-sitter-language-pack package.
    Extracts code entities (classes, functions, methods) for indexing.
    """

    def __init__(self):
        self._parsers: dict[str, Any] = {}
        self._languages: dict[str, Any] = {}
        self._available = _check_tree_sitter_available()
        # Parse tree cache
        self._parse_cache: dict[str, tuple[str, Any]] = {}  # path -> (hash, tree)
        self._cache_maxsize: int = settings.parse_tree_cache_maxsize
        self._cache_hits: int = 0
        self._cache_misses: int = 0

    @property
    def available(self) -> bool:
        """Check if tree-sitter is available."""
        return self._available

    def get_parser(self, lang: str):
        """Get or create parser and language for the given language."""
        if not self._available:
            return None, None

        if lang not in self._parsers:
            try:
                from tree_sitter_language_pack import get_language, get_parser

                self._parsers[lang] = get_parser(lang)
                self._languages[lang] = get_language(lang)
            except Exception as e:
                logger.warning(f"Failed to get parser for {lang}: {e}")
                return None, None

        return self._parsers.get(lang), self._languages.get(lang)

    def _get_cached_tree(self, file_path: Path, source: bytes, lang: str):
        """Get parse tree from cache or parse and cache."""
        content_hash = hashlib.md5(source).hexdigest()
        cache_key = str(file_path)

        # Check cache
        if cache_key in self._parse_cache:
            cached_hash, cached_tree = self._parse_cache[cache_key]
            if cached_hash == content_hash:
                self._cache_hits += 1
                return cached_tree

        # Cache miss - parse
        self._cache_misses += 1
        parser, language = self.get_parser(lang)
        if parser is None:
            return None

        tree = parser.parse(source)

        # Evict oldest if at capacity
        if len(self._parse_cache) >= self._cache_maxsize:
            oldest_key = next(iter(self._parse_cache))
            del self._parse_cache[oldest_key]

        self._parse_cache[cache_key] = (content_hash, tree)
        return tree

    @property
    def cache_stats(self) -> dict[str, Any]:
        """Return cache statistics."""
        total = self._cache_hits + self._cache_misses
        return {
            "size": len(self._parse_cache),
            "maxsize": self._cache_maxsize,
            "hits": self._cache_hits,
            "misses": self._cache_misses,
            "hit_rate": self._cache_hits / total if total > 0 else 0.0,
        }

    def clear_cache(self) -> int:
        """Clear the parse tree cache."""
        count = len(self._parse_cache)
        self._parse_cache.clear()
        return count

    def _extract_imports(self, tree, language, lang: str, source: bytes) -> list[str]:
        """Extract import statements from a parsed file."""
        import tree_sitter

        query_text = IMPORT_QUERIES.get(lang)
        if not query_text:
            return []

        try:
            query = tree_sitter.Query(language, query_text)
            cursor = tree_sitter.QueryCursor(query)
            matches = list(cursor.matches(tree.root_node))
        except Exception as e:
            logger.debug(f"Import query failed for {lang}: {e}")
            return []

        imports = []
        for _pattern_index, captures_dict in matches:
            for capture_name, nodes in captures_dict.items():
                if capture_name in ("import.name", "import.module", "import.source"):
                    for node in nodes:
                        text = source[node.start_byte : node.end_byte].decode(
                            "utf-8", errors="replace"
                        )
                        text = text.strip("\"'")
                        if text and text not in imports:
                            imports.append(text)

        return imports

    def get_supported_extensions(self) -> list[str]:
        """Get list of supported file extensions."""
        return list(LANGUAGE_CONFIG.keys())

    def index_file(
        self, file_path: Path, project_path: Path
    ) -> Generator[dict[str, Any], None, None]:
        """
        Index a single file and yield code entities.

        Args:
            file_path: Absolute path to the file
            project_path: Project root for relative path calculation

        Yields:
            CodeEntity dictionaries ready for database insertion
        """
        if not self._available:
            return

        suffix = file_path.suffix.lower()
        if suffix not in LANGUAGE_CONFIG:
            return

        lang = LANGUAGE_CONFIG[suffix]

        try:
            source = file_path.read_bytes()
            # Use cached tree if available
            tree = self._get_cached_tree(file_path, source, lang)
            if tree is None:
                return
            # Get language for query operations
            _, language = self.get_parser(lang)
            if language is None:
                return
        except Exception as e:
            logger.debug(f"Failed to parse {file_path}: {e}")
            return

        try:
            relative_path = file_path.relative_to(project_path)
        except ValueError:
            # File is not under project path
            relative_path = file_path

        # Extract file-level imports
        file_imports = self._extract_imports(tree, language, lang, source)

        # Extract entities using tree-sitter queries
        for entity in self._extract_entities(
            tree, language, lang, source, str(relative_path)
        ):
            entity["project_path"] = str(project_path)
            entity["file_path"] = str(relative_path)
            entity["imports"] = file_imports  # Attach imports to all entities
            yield self._make_entity_dict(**entity)

    def _extract_entities(
        self, tree, language, lang: str, source: bytes, file_path: str = ""
    ) -> Generator[dict[str, Any], None, None]:
        """Extract entities using language-specific queries."""
        import tree_sitter

        query_text = ENTITY_QUERIES.get(lang)

        if not query_text:
            # Fallback: walk tree manually for basic extraction
            yield from self._walk_tree_fallback(tree.root_node, source, lang, file_path)
            return

        try:
            # Use new tree-sitter 0.25+ API with Query constructor and QueryCursor
            query = tree_sitter.Query(language, query_text)
            cursor = tree_sitter.QueryCursor(query)
            matches = list(cursor.matches(tree.root_node))
        except Exception as e:
            logger.debug(f"Query failed for {lang}: {e}")
            yield from self._walk_tree_fallback(tree.root_node, source, lang, file_path)
            return

        # Process matches - each match is (pattern_index, captures_dict)
        processed_defs = set()

        for _pattern_index, captures_dict in matches:
            # Find the definition capture (ends with .def)
            def_capture = None
            def_nodes = []
            name_nodes = []

            for capture_name, nodes in captures_dict.items():
                if capture_name.endswith(".def"):
                    def_capture = capture_name
                    def_nodes = nodes
                elif capture_name.endswith(".name"):
                    name_nodes = nodes

            if not def_nodes:
                continue

            for def_node in def_nodes:
                # Skip if already processed
                node_id = (def_node.start_byte, def_node.end_byte)
                if node_id in processed_defs:
                    continue
                processed_defs.add(node_id)

                entity_type = def_capture.split(".")[0] if def_capture else "unknown"

                # Find the corresponding name node
                name = "anonymous"
                for name_node in name_nodes:
                    if self._is_descendant(def_node, name_node):
                        name = name_node.text.decode("utf-8", errors="replace")
                        break

                # Get first line as signature (up to 200 chars)
                signature = self._extract_signature(def_node, source)
                docstring = self._extract_docstring(def_node, source, lang)

                yield {
                    "entity_type": entity_type,
                    "name": name,
                    "qualified_name": self._compute_qualified_name(
                        def_node, source, lang, file_path
                    ),
                    "line_start": def_node.start_point[0] + 1,  # 1-indexed
                    "line_end": def_node.end_point[0] + 1,
                    "signature": signature,
                    "docstring": docstring,
                }

    def _is_descendant(self, ancestor, node) -> bool:
        """Check if node is a descendant of ancestor."""
        current = node
        while current is not None:
            if current == ancestor:
                return True
            current = current.parent
        return False

    def _extract_signature(self, node, source: bytes) -> str:
        """Extract the first line of a definition as signature."""
        try:
            start = node.start_byte
            end = min(start + 500, node.end_byte)  # Get enough for first line
            text = source[start:end].decode("utf-8", errors="replace")
            # Get first line, limit to 200 chars
            first_line = text.split("\n")[0]
            return first_line[:200]
        except Exception:
            return ""

    def _extract_docstring(self, node, source: bytes, lang: str) -> str | None:
        """Extract docstring from a definition node."""
        try:
            # Language-specific docstring extraction
            if lang == "python":
                return self._extract_python_docstring(node, source)
            elif lang in ("javascript", "typescript", "tsx", "java", "c_sharp"):
                return self._extract_jsdoc(node, source)
            elif lang == "go":
                return self._extract_go_comment(node, source)
            return None
        except Exception:
            return None

    def _extract_python_docstring(self, node, source: bytes) -> str | None:
        """Extract Python docstring (first string literal in function/class body)."""
        for child in node.children:
            if child.type == "block":
                for block_child in child.children:
                    if block_child.type == "expression_statement":
                        for expr_child in block_child.children:
                            if expr_child.type == "string":
                                text = source[
                                    expr_child.start_byte : expr_child.end_byte
                                ]
                                return text.decode("utf-8", errors="replace").strip(
                                    "\"' \n\r"
                                )
                break
        return None

    def _extract_jsdoc(self, node, source: bytes) -> str | None:
        """Extract JSDoc comment preceding a definition."""
        # Look at previous sibling for comment
        prev = node.prev_sibling
        while prev and prev.type == "comment":
            text = source[prev.start_byte : prev.end_byte].decode(
                "utf-8", errors="replace"
            )
            if text.startswith("/**"):
                # Clean up JSDoc
                lines = text.split("\n")
                cleaned = []
                for line in lines:
                    line = line.strip()
                    if line.startswith("/**") or line.startswith("*/"):
                        continue
                    if line.startswith("*"):
                        line = line[1:].strip()
                    cleaned.append(line)
                return " ".join(cleaned)
            prev = prev.prev_sibling
        return None

    def _extract_go_comment(self, node, source: bytes) -> str | None:
        """Extract Go comment preceding a definition."""
        prev = node.prev_sibling
        comments: list[str] = []
        while prev and prev.type == "comment":
            text = source[prev.start_byte : prev.end_byte].decode(
                "utf-8", errors="replace"
            )
            # Remove // prefix
            if text.startswith("//"):
                text = text[2:].strip()
            comments.insert(0, text)
            prev = prev.prev_sibling
        return " ".join(comments) if comments else None

    def _walk_tree_fallback(
        self, node, source: bytes, lang: str = "", file_path: str = ""
    ) -> Generator[dict[str, Any], None, None]:
        """
        Fallback tree walker for languages without specific queries.

        Looks for common node types that typically represent definitions.
        """
        definition_types = {
            "function_definition",
            "function_declaration",
            "method_definition",
            "class_definition",
            "class_declaration",
            "class_specifier",
            "struct_specifier",
            "interface_declaration",
            "trait_item",
            "impl_item",
            "enum_item",
            "method_declaration",
        }

        if node.type in definition_types:
            name = self._extract_name_from_node(node, source)
            if name:
                entity_type = (
                    "function"
                    if "function" in node.type or "method" in node.type
                    else "class"
                )
                yield {
                    "entity_type": entity_type,
                    "name": name,
                    "qualified_name": self._compute_qualified_name(
                        node, source, lang, file_path
                    ),
                    "line_start": node.start_point[0] + 1,
                    "line_end": node.end_point[0] + 1,
                    "signature": self._extract_signature(node, source),
                    "docstring": None,
                }

        for child in node.children:
            yield from self._walk_tree_fallback(child, source, lang, file_path)

    def _extract_name_from_node(self, node, source: bytes) -> str | None:
        """Try to extract a name from a node by looking for identifier children."""
        name_types = {
            "identifier",
            "type_identifier",
            "field_identifier",
            "property_identifier",
            "constant",
            "name",
            "simple_identifier",
        }

        for child in node.children:
            if child.type in name_types:
                return source[child.start_byte : child.end_byte].decode(
                    "utf-8", errors="replace"
                )
            # Some languages nest the name
            if child.type in ("declarator", "function_declarator", "type_spec"):
                for grandchild in child.children:
                    if grandchild.type in name_types:
                        return source[
                            grandchild.start_byte : grandchild.end_byte
                        ].decode("utf-8", errors="replace")
        return None

    def _compute_qualified_name(
        self, node, source: bytes, lang: str, file_path: str
    ) -> str:
        """
        Compute fully qualified name by walking parent scopes.

        Examples:
          - Python class method: module.ClassName.method_name
          - Nested class: module.Outer.Inner.method
          - Top-level function: module.function_name
        """
        parts: list[str] = []

        # Walk up the tree collecting scope names
        current = node.parent
        while current is not None:
            scope_name = None

            if lang == "python":
                if current.type in ("class_definition", "function_definition"):
                    scope_name = self._extract_name_from_node(current, source)
            elif lang in ("typescript", "javascript", "tsx"):
                if current.type in (
                    "class_declaration",
                    "function_declaration",
                    "method_definition",
                ):
                    scope_name = self._extract_name_from_node(current, source)
            elif lang == "go":
                if current.type in (
                    "type_declaration",
                    "function_declaration",
                    "method_declaration",
                ):
                    scope_name = self._extract_name_from_node(current, source)
            elif lang == "rust":
                if current.type in ("impl_item", "function_item"):
                    scope_name = self._extract_name_from_node(current, source)
            elif lang in ("kotlin", "java") and current.type in (
                "class_declaration",
                "object_declaration",
                "function_declaration",
            ):
                scope_name = self._extract_name_from_node(current, source)

            if scope_name:
                parts.insert(0, scope_name)

            current = current.parent

        # Add the entity's own name
        entity_name = self._extract_name_from_node(node, source)
        if entity_name:
            parts.append(entity_name)

        # Prepend module name from file path
        module_name = self._file_path_to_module(file_path)
        if module_name:
            parts.insert(0, module_name)

        return ".".join(parts) if parts else entity_name or "anonymous"

    def _file_path_to_module(self, file_path: str) -> str:
        """Convert file path to module name."""
        p = Path(file_path)
        stem = p.stem
        if stem == "__init__":
            return p.parent.name if p.parent.name != "." else ""
        return stem

    def _make_entity_dict(self, **kwargs) -> dict[str, Any]:
        """Create a CodeEntity-compatible dictionary."""
        # Use qualified_name + signature for stable IDs that handle overloaded functions
        # Signature differs between overloads but doesn't change when lines shift
        identifier = kwargs.get("qualified_name") or kwargs["name"]
        signature = kwargs.get("signature", "")
        id_string = f"{kwargs['project_path']}:{kwargs['file_path']}:{identifier}:{kwargs['entity_type']}:{signature}"
        entity_id = hashlib.sha256(id_string.encode()).hexdigest()[:16]

        return {
            "id": entity_id,
            "project_path": kwargs["project_path"],
            "entity_type": kwargs["entity_type"],
            "name": kwargs["name"],
            "qualified_name": kwargs.get("qualified_name"),
            "file_path": kwargs["file_path"],
            "line_start": kwargs.get("line_start"),
            "line_end": kwargs.get("line_end"),
            "signature": kwargs.get("signature"),
            "docstring": kwargs.get("docstring"),
            "calls": [],
            "called_by": [],
            "imports": kwargs.get("imports", []),
            "inherits": [],
            "indexed_at": datetime.now(timezone.utc),
        }


class CodeIndexManager:
    """
    Manages code indexing across a project.

    Orchestrates the TreeSitterIndexer, stores results in SQLite,
    and indexes embeddings in Qdrant for semantic search.
    """

    # Default patterns for all supported languages
    DEFAULT_PATTERNS = [
        "**/*.py",
        "**/*.js",
        "**/*.mjs",
        "**/*.ts",
        "**/*.tsx",
        "**/*.go",
        "**/*.rs",
        "**/*.java",
        "**/*.kt",
        "**/*.kts",
        "**/*.rb",
        "**/*.php",
        "**/*.c",
        "**/*.h",
        "**/*.cpp",
        "**/*.cs",
    ]

    # Directories to skip during indexing
    SKIP_DIRS = {
        ".git",
        "node_modules",
        "__pycache__",
        ".venv",
        "venv",
        "dist",
        "build",
        ".tox",
        ".eggs",
        "*.egg-info",
        "target",
        ".cargo",
        ".rustup",
        "vendor",
        ".bundle",
        ".next",
        ".nuxt",
        ".output",
        "coverage",
        ".nyc_output",
        ".daem0nmcp",
        ".devilmcp",
    }

    def __init__(self, db=None, qdrant=None):
        """
        Initialize CodeIndexManager.

        Args:
            db: DatabaseManager instance (optional)
            qdrant: QdrantVectorStore instance (optional)
        """
        self.db = db
        self.qdrant = qdrant
        self.indexer = TreeSitterIndexer()

    @property
    def available(self) -> bool:
        """Check if code indexing is available."""
        return self.indexer.available

    def _should_skip(self, path: Path) -> bool:
        """Check if a path should be skipped during indexing."""
        parts = set(path.parts)
        for skip_dir in self.SKIP_DIRS:
            if skip_dir in parts:
                return True
            # Handle wildcards
            if skip_dir.startswith("*"):
                suffix = skip_dir[1:]
                if any(p.endswith(suffix) for p in parts):
                    return True
        return False

    def _index_project_sync(
        self, project: Path, patterns: list[str]
    ) -> tuple[list[dict], int, int]:
        """
        Synchronous file indexing - runs in thread pool to avoid blocking event loop.

        Args:
            project: Resolved project path
            patterns: Glob patterns for files to index

        Returns:
            Tuple of (entities, files_processed, files_skipped)
        """
        entities = []
        files_processed = 0
        files_skipped = 0

        for pattern in patterns:
            for file_path in project.glob(pattern):
                if self._should_skip(file_path):
                    files_skipped += 1
                    continue

                if not file_path.is_file():
                    continue

                for entity in self.indexer.index_file(file_path, project):
                    entities.append(entity)
                files_processed += 1

        return entities, files_processed, files_skipped

    async def index_project(
        self, project_path: str, patterns: list[str] | None = None
    ) -> dict[str, Any]:
        """
        Index all code entities in a project.

        Args:
            project_path: Root path of the project
            patterns: Glob patterns for files to index (default: all supported)

        Returns:
            Dict with indexing statistics
        """
        if not self.indexer.available:
            return {
                "error": "tree-sitter-languages not installed",
                "indexed": 0,
                "project": project_path,
            }

        project = Path(project_path).resolve()
        patterns = patterns or self.DEFAULT_PATTERNS

        # Run CPU-bound tree-sitter parsing in thread pool to avoid blocking event loop
        entities, files_processed, files_skipped = await asyncio.to_thread(
            self._index_project_sync, project, patterns
        )

        # Store in database if available
        if self.db is not None:
            await self._store_entities(entities, str(project))

        # Index in Qdrant if available
        if self.qdrant is not None:
            await self._index_in_qdrant(entities)

        return {
            "indexed": len(entities),
            "files_processed": files_processed,
            "files_skipped": files_skipped,
            "project": str(project),
        }

    async def _store_entities(self, entities: list[dict], project_path: str):
        """Store entities in SQLite database."""
        from sqlalchemy import delete

        from .models import CodeEntity

        async with self.db.get_session() as session:
            # Clear existing entities for this project
            await session.execute(
                delete(CodeEntity).where(CodeEntity.project_path == project_path)
            )
            # Flush the delete so merge() sees the cleared state
            await session.flush()

            # Upsert entities (merge handles stale rows that survive deletion)
            for entity_dict in entities:
                entity = CodeEntity(
                    id=entity_dict["id"],
                    project_path=entity_dict["project_path"],
                    entity_type=entity_dict["entity_type"],
                    name=entity_dict["name"],
                    qualified_name=entity_dict.get("qualified_name"),
                    file_path=entity_dict["file_path"],
                    line_start=entity_dict.get("line_start"),
                    line_end=entity_dict.get("line_end"),
                    signature=entity_dict.get("signature"),
                    docstring=entity_dict.get("docstring"),
                    calls=entity_dict.get("calls", []),
                    called_by=entity_dict.get("called_by", []),
                    imports=entity_dict.get("imports", []),
                    inherits=entity_dict.get("inherits", []),
                    indexed_at=entity_dict.get("indexed_at"),
                )
                await session.merge(entity)

            await session.commit()

    async def _index_in_qdrant(self, entities: list[dict]):
        """Index entities in Qdrant for semantic search."""
        from . import vectors

        if not vectors.is_available():
            return

        points = []
        for entity in entities:
            # Create searchable text from signature and docstring
            text = f"{entity['name']} {entity.get('signature', '')} {entity.get('docstring', '')}"
            text = text.strip()
            if not text:
                continue

            embedding = vectors.encode_document(text)
            if embedding is None:
                continue

            # Decode the embedding bytes to list
            embedding_list = vectors.decode(embedding)
            if embedding_list is None:
                continue

            points.append(
                {
                    "id": entity["id"],
                    "vector": embedding_list,
                    "payload": {
                        "entity_type": entity["entity_type"],
                        "name": entity["name"],
                        "file_path": entity["file_path"],
                        "project_path": entity["project_path"],
                        "signature": entity.get("signature", ""),
                    },
                }
            )

        if points and self.qdrant is not None:
            try:
                self.qdrant.client.upsert(
                    collection_name="daem0n_code_entities",
                    points=points,
                )
            except Exception as e:
                logger.warning(f"Failed to index in Qdrant: {e}")

    async def find_entity(
        self,
        name: str,
        project_path: str | None = None,
        entity_type: str | None = None,
    ) -> dict[str, Any] | None:
        """
        Find a code entity by name.

        Args:
            name: Entity name to find
            project_path: Limit search to a specific project
            entity_type: Limit search to a specific type

        Returns:
            Entity dictionary or None
        """
        if self.db is None:
            return None

        from sqlalchemy import select

        from .models import CodeEntity

        async with self.db.get_session() as session:
            query = select(CodeEntity).where(CodeEntity.name == name)

            if project_path:
                query = query.where(CodeEntity.project_path == project_path)
            if entity_type:
                query = query.where(CodeEntity.entity_type == entity_type)

            result = await session.execute(query)
            entity = result.scalars().first()

            if entity:
                return {
                    "id": entity.id,
                    "name": entity.name,
                    "entity_type": entity.entity_type,
                    "file_path": entity.file_path,
                    "line_start": entity.line_start,
                    "line_end": entity.line_end,
                    "signature": entity.signature,
                    "docstring": entity.docstring,
                }
            return None

    async def search_entities(
        self,
        query: str,
        project_path: str | None = None,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        """
        Search across code entities.

        Uses semantic search via Qdrant if available, falls back to
        SQLite text matching on name and signature.

        Args:
            query: Search query
            project_path: Limit search to a specific project
            limit: Maximum results to return

        Returns:
            List of matching entities with scores
        """
        from . import vectors

        # Try semantic search first if Qdrant is available
        if vectors.is_available() and self.qdrant is not None:
            results = await self._semantic_search(query, project_path, limit)
            if results:
                return results

        # Fall back to SQLite text search
        return await self._text_search(query, project_path, limit)

    async def _text_search(
        self,
        query: str,
        project_path: str | None = None,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        """Text-based search using SQLite LIKE queries."""
        if self.db is None:
            return []

        from sqlalchemy import or_, select

        from .models import CodeEntity

        async with self.db.get_session() as session:
            # Search name and signature for query terms
            search_pattern = f"%{query}%"
            stmt = select(CodeEntity).where(
                or_(
                    CodeEntity.name.ilike(search_pattern),
                    CodeEntity.signature.ilike(search_pattern),
                    CodeEntity.docstring.ilike(search_pattern),
                )
            )

            if project_path:
                stmt = stmt.where(CodeEntity.project_path == project_path)

            stmt = stmt.limit(limit)
            result = await session.execute(stmt)
            entities = result.scalars().all()

            return [
                {
                    "id": e.id,
                    "name": e.name,
                    "entity_type": e.entity_type,
                    "file_path": e.file_path,
                    "line_start": e.line_start,
                    "signature": e.signature,
                    "score": 1.0,  # No relevance score for text search
                }
                for e in entities
            ]

    async def _semantic_search(
        self,
        query: str,
        project_path: str | None = None,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        """Semantic search using Qdrant vector similarity."""
        from . import vectors

        if not vectors.is_available() or self.qdrant is None:
            return []

        # Encode query
        embedding = vectors.encode_query(query)
        if embedding is None:
            return []

        embedding_list = vectors.decode(embedding)
        if embedding_list is None:
            return []

        try:
            # Build filter if project_path specified
            filter_conditions = None
            if project_path:
                filter_conditions = {
                    "must": [{"key": "project_path", "match": {"value": project_path}}]
                }

            results = self.qdrant.client.search(
                collection_name="daem0n_code_entities",
                query_vector=embedding_list,
                limit=limit,
                query_filter=filter_conditions,
            )

            return [
                {
                    "id": r.id,
                    "score": r.score,
                    **r.payload,
                }
                for r in results
            ]
        except Exception as e:
            logger.warning(f"Qdrant search failed: {e}")
            return []

    async def analyze_impact(
        self,
        entity_name: str,
        project_path: str | None = None,
    ) -> dict[str, Any]:
        """
        Analyze what would be affected by changing a code entity.

        Args:
            entity_name: Name of the entity to analyze
            project_path: Limit analysis to a specific project

        Returns:
            Impact analysis with affected files and entities
        """
        if self.db is None:
            return {"error": "Database not initialized"}

        from sqlalchemy import select

        from .models import CodeEntity

        async with self.db.get_session() as session:
            # Find the entity
            query = select(CodeEntity).where(CodeEntity.name == entity_name)
            if project_path:
                query = query.where(CodeEntity.project_path == project_path)

            result = await session.execute(query)
            entity = result.scalars().first()

            if not entity:
                return {
                    "entity": entity_name,
                    "found": False,
                    "affected_files": [],
                    "affected_entities": [],
                }

            # Find entities that call this one or import it
            affected = []
            affected_files = set()

            # Query for entities that reference this one in their calls or imports
            # This is a simple implementation - in production, we'd want to analyze
            # the actual code to find call sites
            all_entities_query = select(CodeEntity)
            if project_path:
                all_entities_query = all_entities_query.where(
                    CodeEntity.project_path == project_path
                )

            all_result = await session.execute(all_entities_query)
            all_entities = all_result.scalars().all()

            for other in all_entities:
                if other.id == entity.id:
                    continue

                # Check if this entity's name appears in the other's calls
                calls = other.calls or []
                imports = other.imports or []

                if entity_name in calls or entity_name in imports:
                    affected.append(
                        {
                            "name": other.name,
                            "type": other.entity_type,
                            "file": other.file_path,
                            "line": other.line_start,
                        }
                    )
                    affected_files.add(other.file_path)

            return {
                "entity": entity_name,
                "found": True,
                "entity_type": entity.entity_type,
                "file_path": entity.file_path,
                "line_start": entity.line_start,
                "affected_files": list(affected_files),
                "affected_entities": affected,
                "message": f"Found {len(affected)} entities that may be affected",
            }

    async def index_file_if_changed(
        self, file_path: Path, project_path: Path, force: bool = False
    ) -> dict[str, Any]:
        """
        Index a single file only if its content has changed.

        Returns:
            Dict with changed, entities_count, reason, or error
        """
        if file_path.suffix.lower() not in LANGUAGE_CONFIG:
            return {"changed": False, "reason": "unsupported_extension"}

        if self._should_skip(file_path):
            return {"changed": False, "reason": "excluded_directory"}

        try:
            rel_path = str(file_path.relative_to(project_path))
        except ValueError:
            rel_path = str(file_path)

        project_str = str(project_path.resolve())

        # Compute current hash
        try:
            current_hash = hashlib.sha256(file_path.read_bytes()).hexdigest()
        except OSError as e:
            return {"changed": False, "error": str(e)}

        # Check stored hash
        if not force and self.db is not None:
            stored_hash = await self._get_stored_hash(project_str, rel_path)
            if stored_hash == current_hash:
                return {"changed": False, "reason": "unchanged"}

        # Re-index
        entities = list(self.indexer.index_file(file_path, project_path))

        if self.db is not None:
            await self._store_file_entities(
                entities, project_str, rel_path, current_hash
            )

        if self.qdrant is not None:
            await self._index_in_qdrant(entities)

        return {"changed": True, "entities_count": len(entities)}

    async def _get_stored_hash(self, project_path: str, file_path: str) -> str | None:
        """Get stored content hash for a file."""
        from sqlalchemy import and_, select

        from .models import FileHash

        async with self.db.get_session() as session:
            result = await session.execute(
                select(FileHash.content_hash).where(
                    and_(
                        FileHash.project_path == project_path,
                        FileHash.file_path == file_path,
                    )
                )
            )
            row = result.scalar_one_or_none()
            return row

    async def _store_file_entities(
        self, entities: list[dict], project_path: str, file_path: str, content_hash: str
    ) -> None:
        """Store entities for a single file, replacing existing."""
        from sqlalchemy import and_, delete, select

        from .models import CodeEntity, FileHash

        async with self.db.get_session() as session:
            # Delete existing entities for this file only
            await session.execute(
                delete(CodeEntity).where(
                    and_(
                        CodeEntity.project_path == project_path,
                        CodeEntity.file_path == file_path,
                    )
                )
            )
            # Flush the delete so merge() sees the cleared state
            await session.flush()

            # Upsert entities (merge handles stale rows that survive deletion)
            for entity_dict in entities:
                entity = CodeEntity(
                    id=entity_dict["id"],
                    project_path=entity_dict["project_path"],
                    entity_type=entity_dict["entity_type"],
                    name=entity_dict["name"],
                    qualified_name=entity_dict.get("qualified_name"),
                    file_path=entity_dict["file_path"],
                    line_start=entity_dict.get("line_start"),
                    line_end=entity_dict.get("line_end"),
                    signature=entity_dict.get("signature"),
                    docstring=entity_dict.get("docstring"),
                    calls=entity_dict.get("calls", []),
                    called_by=entity_dict.get("called_by", []),
                    imports=entity_dict.get("imports", []),
                    inherits=entity_dict.get("inherits", []),
                    indexed_at=entity_dict.get("indexed_at"),
                )
                await session.merge(entity)

            # Upsert file hash
            existing = await session.execute(
                select(FileHash).where(
                    and_(
                        FileHash.project_path == project_path,
                        FileHash.file_path == file_path,
                    )
                )
            )
            fh = existing.scalar_one_or_none()
            if fh:
                fh.content_hash = content_hash
                fh.indexed_at = datetime.now(timezone.utc)
            else:
                session.add(
                    FileHash(
                        project_path=project_path,
                        file_path=file_path,
                        content_hash=content_hash,
                    )
                )

            await session.commit()
