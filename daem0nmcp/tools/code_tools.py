"""Code understanding tools: index_project, find_code, analyze_impact, scan_todos, propose_refactor."""

import logging
import os
import re
from pathlib import Path
from typing import Any

try:
    from .. import __version__
    from ..config import settings
    from ..context_manager import (
        _default_project_path,
        _missing_project_path_error,
        _resolve_within_project,
        get_project_context,
    )
    from ..logging_config import with_request_id
    from ..mcp_instance import mcp
    from ..models import Memory
except ImportError:
    from daem0nmcp import __version__
    from daem0nmcp.config import settings
    from daem0nmcp.context_manager import (
        _default_project_path,
        _missing_project_path_error,
        _resolve_within_project,
        get_project_context,
    )
    from daem0nmcp.logging_config import with_request_id
    from daem0nmcp.mcp_instance import mcp
    from daem0nmcp.models import Memory

from sqlalchemy import select

from ._deprecation import add_deprecation

logger = logging.getLogger(__name__)


# ============================================================================
# Helper: TODO/FIXME Scanner
# ============================================================================
# Pattern matches TODO, FIXME, HACK, XXX, BUG, NOTE with optional colon and content
TODO_PATTERN = re.compile(
    r'(?:#|//|/\*|\*|--|<!--|\'\'\'|""")\s*'  # Comment markers
    r"(TODO|FIXME|HACK|XXX|BUG)\s*"  # Keywords (NOT matching NOTE - too noisy)
    r":?\s*"  # Optional colon
    r'(.+?)(?:\*/|-->|\'\'\'|"""|$)',  # Content until end marker
    re.IGNORECASE,
)

# File extensions to scan
SCANNABLE_EXTENSIONS = {
    ".py",
    ".js",
    ".ts",
    ".jsx",
    ".tsx",
    ".java",
    ".c",
    ".cpp",
    ".h",
    ".hpp",
    ".go",
    ".rs",
    ".rb",
    ".php",
    ".swift",
    ".kt",
    ".scala",
    ".sh",
    ".bash",
    ".html",
    ".css",
    ".scss",
    ".sass",
    ".less",
    ".vue",
    ".svelte",
    ".sql",
    ".yaml",
    ".yml",
    ".toml",
    ".json",
    ".md",
    ".rst",
    ".txt",
}

# Directories to skip
SKIP_DIRS = {
    ".git",
    ".svn",
    ".hg",
    "node_modules",
    "__pycache__",
    ".pytest_cache",
    "venv",
    ".venv",
    "env",
    ".env",
    "dist",
    "build",
    ".tox",
    ".eggs",
    "*.egg-info",
    ".mypy_cache",
    ".coverage",
    "htmlcov",
    ".daem0nmcp",
}

# Directories to exclude when scanning project structure (shared with briefing)
BOOTSTRAP_EXCLUDED_DIRS = {
    "node_modules",
    ".git",
    "__pycache__",
    ".venv",
    "venv",
    "dist",
    "build",
    ".next",
    "target",
    ".idea",
    ".vscode",
    ".eggs",
    "eggs",
    ".tox",
    ".nox",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    "htmlcov",
    ".coverage",
    "site-packages",
}


def _scan_for_todos(
    root_path: str,
    max_files: int = 500,
    skip_dirs: list[str] | None = None,
    skip_extensions: list[str] | None = None,
) -> list[dict[str, Any]]:
    """
    Scan directory for TODO/FIXME/HACK comments with deduplication.

    Supports:
    - Single-line comments (# // --)
    - Multi-line block comments (/* */ ''' \"\"\")
    - Content hashing to avoid duplicates
    - Configurable skip lists

    Args:
        root_path: Directory to scan
        max_files: Maximum files to scan (default: 500)
        skip_dirs: Directories to skip (default: from settings)
        skip_extensions: File extensions to skip (default: from settings)

    Returns:
        List of TODO items with file, line, type, content, and hash
    """
    import hashlib

    # Use settings defaults if not provided
    if skip_dirs is None:
        skip_dirs = settings.todo_skip_dirs
    if skip_extensions is None:
        skip_extensions = settings.todo_skip_extensions

    todos = []
    seen_hashes = set()
    files_scanned = 0
    root = Path(root_path)

    if not root.exists():
        return []

    # Convert skip_dirs to set for faster lookup
    skip_dirs_set = set(skip_dirs)
    skip_exts_set = set(skip_extensions)

    for file_path in root.rglob("*"):
        # Skip directories
        if file_path.is_dir():
            continue

        # Check if any parent is a skip directory
        skip = False
        for part in file_path.parts:
            if part in skip_dirs_set or part.endswith(".egg-info"):
                skip = True
                break
        if skip:
            continue

        # Check extension
        if file_path.suffix.lower() in skip_exts_set:
            continue

        if file_path.suffix.lower() not in SCANNABLE_EXTENSIONS:
            continue

        # Limit files scanned
        files_scanned += 1
        if files_scanned > max_files:
            break

        try:
            content = file_path.read_text(encoding="utf-8", errors="ignore")
            for line_num, line in enumerate(content.split("\n"), 1):
                matches = TODO_PATTERN.findall(line)
                for match in matches:
                    keyword, text = match
                    text = text.strip()
                    if text and len(text) > 3:  # Skip empty or very short todos
                        rel_path = str(file_path.relative_to(root))

                        # Deduplicate by content hash
                        content_hash = hashlib.md5(
                            f"{rel_path}:{text}".encode()
                        ).hexdigest()[:8]

                        if content_hash not in seen_hashes:
                            seen_hashes.add(content_hash)
                            todos.append(
                                {
                                    "type": keyword.upper(),
                                    "content": text[:200],  # Truncate long content
                                    "file": rel_path,
                                    "line": line_num,
                                    "full_line": line.strip()[:300],
                                    "hash": content_hash,
                                }
                            )
        except (OSError, UnicodeDecodeError):
            continue

    return todos


# ============================================================================
# Tool 13: SCAN_TODOS - Find tech debt in codebase
# ============================================================================
@mcp.tool(version=__version__)
@with_request_id
async def scan_todos(
    path: str | None = None,
    auto_remember: bool = False,
    types: list[str] | None = None,
    project_path: str | None = None,
) -> dict[str, Any]:
    """
    [DEPRECATED] Use understand(action='todos') instead.

    Scan codebase for TODO/FIXME/HACK/XXX/BUG comments.

    Args:
        path: Directory to scan
        auto_remember: Save as warning memories
        types: Filter to specific types
        project_path: Project root
    """
    # Require project_path for multi-project support
    if not project_path and not _default_project_path:
        return _missing_project_path_error()

    ctx = await get_project_context(project_path)

    # Use provided path, or fall back to project path
    scan_path = path or ctx.project_path
    resolved_scan_path, error = _resolve_within_project(ctx.project_path, scan_path)
    if error or resolved_scan_path is None:
        return {"error": error or "Invalid scan path", "path": scan_path}
    found_todos = _scan_for_todos(
        str(resolved_scan_path), max_files=settings.todo_max_files
    )

    # Filter by types if specified
    if types:
        types_upper = [t.upper() for t in types]
        found_todos = [t for t in found_todos if t["type"] in types_upper]

    # Group by type
    by_type: dict[str, list] = {}
    for todo in found_todos:
        todo_type = todo["type"]
        if todo_type not in by_type:
            by_type[todo_type] = []
        by_type[todo_type].append(todo)

    # Get existing todo-related memories to avoid duplicates
    existing_todos = set()
    async with ctx.db_manager.get_session() as session:
        result = await session.execute(
            select(Memory).where(
                Memory.tags.contains('"tech_debt"')
            )  # JSON contains check
        )
        for mem in result.scalars().all():
            # Create a simple signature to check duplicates
            if mem.file_path:
                existing_todos.add(f"{mem.file_path}:{mem.content[:50]}")

    # Auto-remember if requested
    new_memories = []
    if auto_remember:
        for todo in found_todos:
            sig = f"{todo['file']}:{todo['content'][:50]}"
            if sig not in existing_todos:
                memory = await ctx.memory_manager.remember(
                    category="warning",
                    content=f"{todo['type']}: {todo['content']}",
                    rationale=f"Found in codebase at {todo['file']}:{todo['line']}",
                    tags=["tech_debt", "auto_scanned", todo["type"].lower()],
                    file_path=todo["file"],
                    project_path=ctx.project_path,
                )
                new_memories.append(memory)
                existing_todos.add(sig)  # Prevent duplicates in same scan

    # Build summary
    summary_parts = []
    for todo_type in ["FIXME", "HACK", "BUG", "XXX", "TODO"]:
        if todo_type in by_type:
            count = len(by_type[todo_type])
            summary_parts.append(f"{count} {todo_type}")

    result = {
        "total_found": len(found_todos),
        "by_type": by_type,
        "summary": ", ".join(summary_parts) if summary_parts else "No tech debt found",
        "new_memories_created": len(new_memories) if auto_remember else 0,
        "message": (
            f"Found {len(found_todos)} tech debt items"
            + (f", created {len(new_memories)} new warnings" if new_memories else "")
        ),
    }
    return add_deprecation(result, "scan_todos", "understand(action='todos')")


# ============================================================================
# Code Understanding Tools (Phase 2)
# ============================================================================
@mcp.tool(version=__version__)
@with_request_id
async def index_project(
    path: str | None = None,
    patterns: list[str] | None = None,
    project_path: str | None = None,
) -> dict[str, Any]:
    """
    [DEPRECATED] Use understand(action='index') instead.

    Index code structure using tree-sitter. Extracts classes, functions, methods with signatures.

    Args:
        path: Path to index
        patterns: Glob patterns for files
        project_path: Project root
    """
    try:
        from ..code_indexer import CodeIndexManager, is_available
    except ImportError:
        from daem0nmcp.code_indexer import CodeIndexManager, is_available

    if not is_available():
        return {
            "error": "Code indexing not available - install tree-sitter-languages",
            "indexed": 0,
        }

    if not project_path and not _default_project_path:
        return _missing_project_path_error()

    ctx = await get_project_context(project_path)

    # Get Qdrant store if available
    qdrant = None
    try:
        from ..qdrant_store import QdrantVectorStore

        qdrant_path = str(Path(ctx.storage_path) / "qdrant")
        qdrant = QdrantVectorStore(path=qdrant_path)
    except Exception:
        pass

    indexer = CodeIndexManager(db=ctx.db_manager, qdrant=qdrant)

    target_path = path or ctx.project_path
    result = await indexer.index_project(target_path, patterns)

    index_result = {
        "result": result,
        "message": f"Indexed {result.get('indexed', 0)} code entities from {result.get('files_processed', 0)} files",
    }
    return add_deprecation(index_result, "index_project", "understand(action='index')")


@mcp.tool(version=__version__)
@with_request_id
async def find_code(
    query: str, project_path: str | None = None, limit: int = 20
) -> dict[str, Any]:
    """
    [DEPRECATED] Use understand(action='find') instead.

    Semantic search across indexed code entities using vector similarity.

    Args:
        query: Natural language query
        limit: Max results
        project_path: Project root
    """
    try:
        from ..code_indexer import CodeIndexManager, is_available
    except ImportError:
        from daem0nmcp.code_indexer import CodeIndexManager, is_available

    if not is_available():
        return {
            "error": "Code indexing not available - install tree-sitter-languages",
            "results": [],
        }

    if not project_path and not _default_project_path:
        return _missing_project_path_error()

    ctx = await get_project_context(project_path)

    # Get Qdrant store if available
    qdrant = None
    try:
        from ..qdrant_store import QdrantVectorStore

        qdrant_path = str(Path(ctx.storage_path) / "qdrant")
        qdrant = QdrantVectorStore(path=qdrant_path)
    except Exception:
        pass

    indexer = CodeIndexManager(db=ctx.db_manager, qdrant=qdrant)

    results = await indexer.search_entities(query, ctx.project_path, limit)

    find_result = {"query": query, "results": results, "count": len(results)}
    return add_deprecation(find_result, "find_code", "understand(action='find')")


@mcp.tool(version=__version__)
@with_request_id
async def analyze_impact(
    entity_name: str, project_path: str | None = None
) -> dict[str, Any]:
    """
    [DEPRECATED] Use understand(action='impact') instead.

    Analyze blast radius of changing a code entity. Finds affected files and dependents.

    Args:
        entity_name: Function/class/method name
        project_path: Project root
    """
    try:
        from ..code_indexer import CodeIndexManager, is_available
    except ImportError:
        from daem0nmcp.code_indexer import CodeIndexManager, is_available

    if not is_available():
        return {
            "error": "Code indexing not available - install tree-sitter-languages",
            "entity": entity_name,
            "found": False,
        }

    if not project_path and not _default_project_path:
        return _missing_project_path_error()

    ctx = await get_project_context(project_path)

    indexer = CodeIndexManager(db=ctx.db_manager, qdrant=None)

    result = await indexer.analyze_impact(entity_name, ctx.project_path)

    return add_deprecation(
        {"result": result}, "analyze_impact", "understand(action='impact')"
    )


# ============================================================================
# Tool 15: PROPOSE_REFACTOR - Generate refactor suggestions
# ============================================================================
@mcp.tool(version=__version__)
@with_request_id
async def propose_refactor(
    file_path: str, project_path: str | None = None
) -> dict[str, Any]:
    """
    Generate refactor suggestions combining file memories, causal history, TODOs, and rules.

    Args:
        file_path: File to analyze
        project_path: Project root
    """
    # Require project_path for multi-project support
    if not project_path and not _default_project_path:
        return _missing_project_path_error()

    ctx = await get_project_context(project_path)

    result = {
        "file_path": file_path,
        "memories": {},
        "causal_history": [],
        "todos": [],
        "rules": {},
        "constraints": [],
        "opportunities": [],
    }

    # Get file-specific memories
    file_memories = await ctx.memory_manager.recall_for_file(
        file_path, project_path=ctx.project_path
    )
    result["memories"] = file_memories

    # Trace causal chains for each memory to understand WHY the code evolved this way
    seen_chain_ids: set[int] = set()
    for category in ["decisions", "patterns", "warnings", "learnings"]:
        for mem in file_memories.get(category, []):
            mem_id = mem.get("id")
            if mem_id and mem_id not in seen_chain_ids:
                # Trace backward to find what led to this decision
                chain_result = await ctx.memory_manager.trace_chain(
                    memory_id=mem_id,
                    direction="backward",
                    max_depth=3,  # Keep chains concise
                )
                if chain_result.get("chain"):
                    result["causal_history"].append(
                        {
                            "memory_id": mem_id,
                            "memory_content": mem.get("content", "")[:100],
                            "ancestors": [
                                {
                                    "id": c["id"],
                                    "category": c.get("category"),
                                    "content": c.get("content", "")[:100],
                                    "relationship": c.get("relationship"),
                                    "depth": c.get("depth"),
                                }
                                for c in chain_result["chain"]
                            ],
                        }
                    )
                    # Track IDs to avoid duplicate chain traces
                    seen_chain_ids.add(mem_id)
                    for c in chain_result["chain"]:
                        seen_chain_ids.add(c["id"])

    # Resolve file path relative to project directory
    absolute_file_path, error = _resolve_within_project(ctx.project_path, file_path)
    if error or absolute_file_path is None:
        result["error"] = error or "Invalid file path"
        return result

    # Scan for TODOs in this specific file
    if absolute_file_path.exists():
        # Scan the file's directory and filter to just this file
        scan_dir = str(absolute_file_path.parent)
        file_todos = _scan_for_todos(scan_dir, max_files=100)
        target_filename = absolute_file_path.name
        result["todos"] = [
            t
            for t in file_todos
            if t["file"] == target_filename
            or t["file"].endswith(os.sep + target_filename)
        ]

    # Check relevant rules
    filename = os.path.basename(file_path)
    rules = await ctx.rules_engine.check_rules(f"refactoring {filename}")
    result["rules"] = rules

    # Extract constraints from warnings and failed approaches
    for cat in ["warnings", "decisions", "patterns"]:
        for mem in file_memories.get(cat, []):
            if mem.get("worked") is False:
                result["constraints"].append(
                    {
                        "type": "failed_approach",
                        "content": mem["content"],
                        "outcome": mem.get("outcome"),
                        "action": "AVOID this approach",
                    }
                )
            elif cat == "warnings":
                result["constraints"].append(
                    {
                        "type": "warning",
                        "content": mem["content"],
                        "action": "Consider this warning",
                    }
                )

    # Identify opportunities from TODOs
    for todo in result["todos"]:
        result["opportunities"].append(
            {
                "type": todo["type"],
                "content": todo["content"],
                "line": todo["line"],
                "action": f"Address this {todo['type']}",
            }
        )

    # Build summary message
    num_constraints = len(result["constraints"])
    num_opportunities = len(result["opportunities"])
    num_memories = file_memories.get("found", 0)
    num_causal_chains = len(result["causal_history"])

    result["message"] = (
        f"Analysis for {file_path}: "
        f"{num_memories} memories, "
        f"{num_constraints} constraints, "
        f"{num_opportunities} opportunities"
    )

    if num_causal_chains > 0:
        result["message"] += (
            f" | {num_causal_chains} causal chains explain WHY code evolved this way"
        )

    if num_constraints > 0:
        result["message"] += " | Review constraints before refactoring!"

    return result
