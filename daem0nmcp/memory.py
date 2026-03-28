"""
Memory Manager - The core of Daem0nMCP's AI memory system.

This module handles:
- Storing memories (decisions, patterns, warnings, learnings)
- Semantic retrieval using TF-IDF similarity
- Time-based memory decay
- Conflict detection
- Outcome tracking for learning
- GraphRAG integration via KnowledgeGraph
"""

import logging
import os
import re
import sys
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .compression import AdaptiveCompressor, HierarchicalContextManager
from pathlib import Path

from qdrant_client.http.exceptions import ResponseHandlingException, UnexpectedResponse
from sqlalchemy import desc, func, or_, select

from . import vectors
from .cache import get_recall_cache, make_cache_key
from .config import settings
from .database import DatabaseManager
from .graph import KnowledgeGraph
from .models import Memory, MemoryRelationship, MemoryVersion
from .recall_planner import RecallPlanner
from .similarity import (
    TFIDFIndex,
    calculate_memory_decay,
    detect_conflict,
    extract_keywords,
)

# Valid relationship types for graph edges
VALID_RELATIONSHIPS = frozenset(
    {
        "led_to",  # A caused or resulted in B
        "supersedes",  # A replaces B (B is now outdated)
        "depends_on",  # A requires B to be valid
        "conflicts_with",  # A contradicts B
        "related_to",  # General association (weaker)
    }
)

logger = logging.getLogger(__name__)

# =============================================================================
# Constants for scoring and relevance calculations
# =============================================================================

# Boost multipliers for memory relevance scoring
FAILED_DECISION_BOOST = 1.5  # Failed decisions are valuable warnings
WARNING_BOOST = 1.2  # Warnings get moderate boost


def _normalize_file_path(
    file_path: str | None, project_path: str
) -> tuple[str | None, str | None]:
    """
    Normalize a file path to both absolute and project-relative forms.

    On Windows, also case-folds for consistent matching.

    Args:
        file_path: The file path to normalize (can be absolute or relative)
        project_path: The project root path

    Returns:
        Tuple of (absolute_path, relative_path)
        Returns (None, None) if file_path is empty
    """
    if not file_path:
        return None, None

    path = Path(file_path)

    # Make absolute if not already
    if not path.is_absolute():
        path = Path(project_path) / path

    resolved = path.resolve()
    absolute = str(resolved)

    # Compute relative path from project root
    try:
        project_root = Path(project_path).resolve()
        relative = resolved.relative_to(project_root).as_posix()
    except ValueError:
        # Path is outside project root, keep a stable path for matching
        try:
            relative = os.path.relpath(resolved, start=project_root).replace("\\", "/")
        except ValueError:
            relative = resolved.as_posix()

    # Case-fold on Windows for consistent matching
    if sys.platform == "win32":
        absolute = absolute.lower()
        relative = relative.lower()

    return absolute, relative


def _not_archived_condition():
    """Treat NULL archived values as not archived for legacy rows."""
    return or_(Memory.archived == False, Memory.archived.is_(None))  # noqa: E712


def _infer_tags(
    content: str, category: str, existing_tags: list[str] | None = None
) -> list[str]:
    """
    Infer semantic tags from memory content and category.

    Auto-detects common patterns to improve search recall:
    - bugfix: mentions of fixing bugs, errors, issues
    - tech-debt: TODOs, hacks, workarounds, temporary solutions
    - perf: performance, optimization, speed improvements
    - warning: category-based or explicit warnings

    Uses word-boundary matching (regex) to avoid false positives
    like "prefix" triggering "bugfix" or "breakfast" triggering "perf".

    Args:
        content: The memory content text
        category: Memory category (decision, pattern, warning, learning)
        existing_tags: Already-provided tags (won't duplicate)

    Returns:
        List of inferred tags (excludes duplicates from existing_tags)
    """
    inferred: list[str] = []
    existing = set(t.lower() for t in (existing_tags or []))
    content_lower = content.lower()

    # Bugfix patterns - use word boundaries to avoid false positives
    # e.g., "prefix" contains "fix" but shouldn't trigger bugfix
    bugfix_pattern = r"\b(fix|bug|error|issue|broken|crash|failure)\b"
    if re.search(bugfix_pattern, content_lower) and "bugfix" not in existing:
        inferred.append("bugfix")

    # Tech debt patterns - use word boundaries
    debt_pattern = (
        r"\b(todo|hack|workaround|temporary|quick\s*fix|tech\s*debt|refactor\s*later)\b"
    )
    if re.search(debt_pattern, content_lower) and "tech-debt" not in existing:
        inferred.append("tech-debt")

    # Performance patterns - use word boundaries
    # e.g., "breakfast" contains "fast" but shouldn't trigger perf
    perf_pattern = r"\b(perf|performance|slow|fast|optim|speed|latency|cache|caching)\b"
    if re.search(perf_pattern, content_lower) and "perf" not in existing:
        inferred.append("perf")

    # Warning category auto-tag
    if category == "warning" and "warning" not in existing:
        inferred.append("warning")

    # Explicit warning mentions in non-warning categories - use word boundaries
    warning_pattern = r"\b(warn|avoid)\b|don\'t"
    if category != "warning" and re.search(warning_pattern, content_lower) and "warning" not in existing:
        inferred.append("warning")

    return inferred


class MemoryManager:
    """
    Manages AI memories - storing, retrieving, and learning from them.

    Uses TF-IDF similarity for semantic matching instead of naive keyword overlap.
    Optionally uses vector embeddings for better semantic understanding.
    Applies memory decay to favor recent memories.
    Detects conflicts with existing memories.
    Integrates with KnowledgeGraph for GraphRAG capabilities.
    """

    def __init__(self, db_manager: DatabaseManager):
        self.db = db_manager
        self._index: TFIDFIndex | None = None
        self._index_loaded = False
        self._vectors_enabled = vectors.is_available()
        self._index_built_at: datetime | None = None

        # GraphRAG: Knowledge graph instance (lazy-loaded)
        self._knowledge_graph: KnowledgeGraph | None = None

        # Auto-Zoom: Retrieval router (lazy-loaded)
        self._retrieval_router = None

        # Phase 4: Context compression (lazy initialized)
        self._compressor: AdaptiveCompressor | None = None
        self._hierarchical_context: HierarchicalContextManager | None = None
        self.recall_planner = RecallPlanner()

        # Initialize Qdrant vector store if available
        self._qdrant = None
        if self._vectors_enabled:
            # Prefer database manager's storage path for Qdrant (co-locates with SQLite)
            # This ensures tests with temp storage get their own Qdrant instance
            qdrant_path = str(Path(db_manager.storage_path) / "qdrant")
            Path(qdrant_path).mkdir(parents=True, exist_ok=True)

            # Check if remote mode is configured (overrides local)
            if settings.qdrant_url:
                # Remote mode placeholder - not implemented yet
                logger.warning(
                    f"Qdrant remote mode (URL: {settings.qdrant_url}) not yet implemented. "
                    "Falling back to TF-IDF only for vector search."
                )
            else:
                try:
                    from .qdrant_store import QdrantVectorStore

                    self._qdrant = QdrantVectorStore(path=qdrant_path)
                    logger.info(f"Initialized Qdrant vector store at: {qdrant_path}")
                except RuntimeError as e:
                    error_str = str(e)
                    if "already accessed by another instance" in error_str:
                        # Common case: multiple Claude Code sessions for the same project
                        # TF-IDF fallback works well, so only log at INFO level
                        logger.info(
                            "Qdrant locked by another session (falling back to TF-IDF). "
                            "This is normal with multiple Claude Code sessions for the same project."
                        )
                    else:
                        # Unexpected error - log with full details
                        logger.warning(
                            f"Could not initialize Qdrant (falling back to TF-IDF only): {e}"
                        )

    async def get_knowledge_graph(self) -> KnowledgeGraph:
        """
        Get or create the knowledge graph for GraphRAG operations.

        Uses lazy loading - graph is only built when first accessed.
        Call invalidate_graph_cache() when memories change to force reload.

        Returns:
            KnowledgeGraph instance synchronized with SQLite
        """
        if self._knowledge_graph is None:
            self._knowledge_graph = KnowledgeGraph(self.db)

        await self._knowledge_graph.ensure_loaded()
        return self._knowledge_graph

    def invalidate_graph_cache(self) -> None:
        """
        Invalidate the knowledge graph cache.

        Call this after remember() or any operation that modifies
        entities, memories, or relationships in the database.
        Forces next get_knowledge_graph() to reload from SQLite.
        """
        if self._knowledge_graph is not None:
            self._knowledge_graph._loaded = False
            logger.debug("Knowledge graph cache invalidated")

    def _get_retrieval_router(self):
        """Lazy-load the Auto-Zoom retrieval router."""
        if self._retrieval_router is None:
            from .retrieval_router import RetrievalRouter

            self._retrieval_router = RetrievalRouter(
                memory_manager=self,
                knowledge_graph=getattr(self, "_knowledge_graph", None),
            )
        return self._retrieval_router

    @property
    def compressor(self) -> "AdaptiveCompressor":
        """Lazy-load compressor on first use."""
        if self._compressor is None:
            from .compression import AdaptiveCompressor, HierarchicalContextManager

            self._compressor = AdaptiveCompressor()
            self._hierarchical_context = HierarchicalContextManager(
                compressor=self._compressor,
                recall_planner=self.recall_planner,
            )
        return self._compressor

    async def _check_index_freshness(self) -> bool:
        """
        Check if index needs rebuilding due to external DB changes.
        Returns True if index was rebuilt.
        """
        if not self._index_loaded:
            return False

        if await self.db.has_changes_since(self._index_built_at):
            logger.info("Database changed since index was built, rebuilding...")
            self._index_loaded = False
            self._index = None
            # Qdrant is persistent and doesn't need rebuilding
            await self._ensure_index()
            return True

        return False

    async def _ensure_index(self) -> TFIDFIndex:
        """Ensure the TF-IDF index is loaded with all memories."""
        if self._index is None:
            self._index = TFIDFIndex()

        if not self._index_loaded:
            async with self.db.get_session() as session:
                result = await session.execute(
                    select(Memory).where(_not_archived_condition())
                )
                memories = result.scalars().all()

                for mem in memories:
                    text = mem.content
                    if mem.rationale:
                        text += " " + mem.rationale
                    self._index.add_document(mem.id, text, mem.tags)
                    # Vectors are loaded from Qdrant (persistent), not SQLite

                self._index_loaded = True
                self._index_built_at = datetime.now(timezone.utc)
                qdrant_count = self._qdrant.get_count() if self._qdrant else 0
                logger.info(
                    f"Loaded {len(memories)} memories into TF-IDF index ({qdrant_count} vectors in Qdrant)"
                )

        return self._index

    def _hybrid_search(
        self,
        query: str,
        top_k: int = 10,
        tfidf_threshold: float = 0.1,
        vector_threshold: float = 0.3,
    ) -> list[tuple[int, float]]:
        """
        Hybrid search combining TF-IDF and Qdrant vector similarity.

        Uses the same weighted combination as the original HybridSearch:
        final_score = (1 - 0.3) * tfidf_score + 0.3 * vector_score

        Args:
            query: Query text
            top_k: Maximum results
            tfidf_threshold: Minimum TF-IDF score
            vector_threshold: Minimum vector similarity score

        Returns:
            List of (doc_id, score) tuples sorted by score descending
        """
        vector_weight = settings.hybrid_vector_weight

        # Get TF-IDF results
        if self._index is None:
            raise RuntimeError("MemoryManager: TF-IDF index not initialized")
        tfidf_results = self._index.search(
            query, top_k=top_k * 2, threshold=tfidf_threshold
        )
        tfidf_scores = dict(tfidf_results)

        # If Qdrant is available, get vector results
        if self._qdrant and self._qdrant.get_count() > 0:
            # Encode query to vector
            query_embedding_bytes = vectors.encode_query(query)
            if query_embedding_bytes:
                query_vector = vectors.decode(query_embedding_bytes)
                if query_vector:
                    try:
                        qdrant_results = self._qdrant.search(
                            query_vector=query_vector, limit=top_k * 2
                        )
                    except (
                        ResponseHandlingException,
                        UnexpectedResponse,
                        RuntimeError,
                    ) as e:
                        # Handle Qdrant API errors gracefully
                        logger.debug(
                            f"Qdrant search failed, falling back to TF-IDF: {e}"
                        )
                        return tfidf_results[:top_k]

                    # Filter by threshold
                    vector_scores = {
                        doc_id: score
                        for doc_id, score in qdrant_results
                        if score >= vector_threshold
                    }

                    # Combine scores
                    all_docs = set(tfidf_scores.keys()) | set(vector_scores.keys())
                    combined = []

                    for doc_id in all_docs:
                        tfidf_score = tfidf_scores.get(doc_id, 0.0)
                        vector_score = vector_scores.get(doc_id, 0.0)

                        # Weighted combination
                        final_score = (
                            1 - vector_weight
                        ) * tfidf_score + vector_weight * vector_score

                        combined.append((doc_id, final_score))

                    combined.sort(key=lambda x: x[1], reverse=True)
                    return combined[:top_k]

        # Fall back to TF-IDF only if no Qdrant or no vectors
        return tfidf_results[:top_k]

    async def remember(
        self,
        category: str,
        content: str,
        rationale: str | None = None,
        context: dict[str, Any] | None = None,
        tags: list[str] | None = None,
        file_path: str | None = None,
        project_path: str | None = None,
        happened_at: datetime | None = None,
        source_client: str | None = None,
        source_model: str | None = None,
    ) -> dict[str, Any]:
        """
        Store a new memory with conflict detection.

        Args:
            category: One of 'decision', 'pattern', 'warning', 'learning'
            content: The actual content to remember
            rationale: Why this is important / the reasoning
            context: Structured context (files, alternatives, etc.)
            tags: Tags for retrieval
            file_path: Optional file path to associate this memory with
            project_path: Optional project root path for normalizing file paths
            happened_at: When this fact was true in reality (default: now).
                        Use for backfilling: "User told me last week they prefer Python"
                        Pass a datetime with timezone or naive datetime (treated as UTC).
            source_client: MCP client name (e.g., "opencode", "claude-code")
            source_model: LLM model identifier (e.g., "anthropic/claude-sonnet-4")

        Returns:
            The created memory as a dict, with any detected conflicts
        """
        valid_categories = {"decision", "pattern", "warning", "learning"}
        if category not in valid_categories:
            return {"error": f"Invalid category. Must be one of: {valid_categories}"}

        # Infer semantic tags from content
        inferred_tags = _infer_tags(content, category, tags)
        if inferred_tags:
            tags = list(tags or []) + inferred_tags

        # Extract keywords for backward compat (legacy search)
        keywords = extract_keywords(content, tags)
        if rationale:
            keywords = keywords + " " + extract_keywords(rationale)

        # Check for conflicts before storing
        conflicts = await self._check_conflicts(content, tags)

        # Semantic memories (patterns, warnings) are permanent - they don't decay
        # They represent project facts, not episodic events
        is_permanent = category in {"pattern", "warning"}

        # Compute vector embedding if available
        text_for_embedding = content
        if rationale:
            text_for_embedding += " " + rationale
        vector_embedding = (
            vectors.encode_document(text_for_embedding)
            if self._vectors_enabled
            else None
        )

        # Normalize file path if provided
        file_path_abs = file_path
        file_path_rel = None
        if file_path and project_path:
            file_path_abs, file_path_rel = _normalize_file_path(file_path, project_path)

        memory = Memory(
            category=category,
            content=content,
            rationale=rationale,
            context=context or {},
            tags=tags or [],
            keywords=keywords.strip(),
            file_path=file_path_abs,
            file_path_relative=file_path_rel,
            is_permanent=is_permanent,
            vector_embedding=vector_embedding,
            source_client=source_client,
            source_model=source_model,
        )

        async with self.db.get_session() as session:
            session.add(memory)
            await session.flush()
            memory_id = memory.id

            # Create initial version (version 1) with bi-temporal tracking
            from .graph.temporal import create_temporal_version

            # Handle happened_at timezone (valid_from for bi-temporal)
            valid_from = happened_at
            if valid_from is not None and valid_from.tzinfo is None:
                valid_from = valid_from.replace(tzinfo=timezone.utc)

            version = await create_temporal_version(
                session=session,
                memory_id=memory.id,
                version_number=1,
                content=content,
                rationale=rationale,
                context=context or {},
                tags=tags or [],
                outcome=None,
                worked=None,
                change_type="created",
                change_description="Initial creation",
                valid_from=valid_from,
            )

            # Add to TF-IDF index
            index = await self._ensure_index()
            text = content
            if rationale:
                text += " " + rationale
            index.add_document(memory_id, text, tags)

            # Upsert to Qdrant if available
            if self._qdrant and vector_embedding:
                embedding_list = vectors.decode(vector_embedding)
                if embedding_list:
                    self._qdrant.upsert_memory(
                        memory_id=memory_id,
                        embedding=embedding_list,
                        metadata={
                            "category": category,
                            "tags": tags or [],
                            "file_path": file_path_abs,
                            "worked": None,  # Will be updated via record_outcome
                            "is_permanent": is_permanent,
                        },
                    )

            logger.info(
                f"Stored {category}: {content[:50]}..."
                + (" [+qdrant]" if vector_embedding and self._qdrant else "")
            )

            result = {
                "id": memory_id,
                "category": category,
                "content": content,
                "rationale": rationale,
                "tags": tags or [],
                "file_path": file_path,
                "is_permanent": is_permanent,
                "created_at": memory.created_at.isoformat(),
                "valid_from": version.valid_from.isoformat()
                if version.valid_from
                else None,
                "source_client": source_client,
                "source_model": source_model,
            }

            # Add conflict warnings if any
            if conflicts:
                result["conflicts"] = conflicts
                result["warning"] = (
                    f"Found {len(conflicts)} potential conflict(s) with existing memories"
                )

        # Clear recall cache since memories changed
        get_recall_cache().clear()

        # Invalidate knowledge graph cache (new memory added)
        self.invalidate_graph_cache()

        # Auto-extract entities if project_path provided
        if project_path:
            try:
                from .entity_manager import EntityManager

                ent_manager = EntityManager(self.db)
                await ent_manager.process_memory(
                    memory_id=memory_id,
                    content=content,
                    project_path=project_path,
                    rationale=rationale,
                )
            except Exception as e:
                logger.debug(f"Entity extraction failed (non-fatal): {e}")

        # Track in session state for enforcement
        if category == "decision" and project_path:
            try:
                from .enforcement import SessionManager

                session_mgr = SessionManager(self.db)
                await session_mgr.add_pending_decision(project_path, result["id"])
            except Exception as e:
                logger.debug(f"Session tracking failed (non-fatal): {e}")

        return result

    async def remember_batch(
        self, memories: list[dict[str, Any]], project_path: str | None = None
    ) -> dict[str, Any]:
        """
        Store multiple memories in a single transaction.

        More efficient than calling remember() multiple times, especially for
        bootstrapping or bulk imports. All memories are stored atomically.

        Args:
            memories: List of memory dicts, each with:
                - category: One of 'decision', 'pattern', 'warning', 'learning'
                - content: The actual content to remember
                - rationale: (optional) Why this is important
                - tags: (optional) List of tags
                - file_path: (optional) Associated file path
            project_path: Project root path for normalizing file paths

        Returns:
            Summary dict with created_count, error_count, ids, and any errors
        """
        valid_categories = {"decision", "pattern", "warning", "learning"}

        results = {"created_count": 0, "error_count": 0, "ids": [], "errors": []}

        if not memories:
            return results

        # Pre-validate all memories
        validated_memories = []
        for i, mem in enumerate(memories):
            category = mem.get("category")
            content = mem.get("content")

            if not category or category not in valid_categories:
                results["errors"].append(
                    {
                        "index": i,
                        "error": f"Invalid or missing category. Must be one of: {valid_categories}",
                    }
                )
                results["error_count"] += 1
                continue

            if not content or not content.strip():
                results["errors"].append(
                    {"index": i, "error": "Content is required and cannot be empty"}
                )
                results["error_count"] += 1
                continue

            validated_memories.append((i, mem))

        if not validated_memories:
            return results

        # Ensure index is loaded before batch operation
        index = await self._ensure_index()

        async with self.db.get_session() as session:
            created_ids = []

            for i, mem in validated_memories:
                category = mem["category"]
                content = mem["content"]
                rationale = mem.get("rationale")
                tags = mem.get("tags") or []
                file_path = mem.get("file_path")
                context = mem.get("context") or {}

                try:
                    # Extract keywords
                    keywords = extract_keywords(content, tags)
                    if rationale:
                        keywords = keywords + " " + extract_keywords(rationale)

                    # Semantic memories are permanent
                    is_permanent = category in {"pattern", "warning"}

                    # Compute vector embedding if available
                    text_for_embedding = content
                    if rationale:
                        text_for_embedding += " " + rationale
                    vector_embedding = (
                        vectors.encode_document(text_for_embedding)
                        if self._vectors_enabled
                        else None
                    )

                    # Normalize file path if provided
                    file_path_abs = file_path
                    file_path_rel = None
                    if file_path and project_path:
                        file_path_abs, file_path_rel = _normalize_file_path(
                            file_path, project_path
                        )

                    memory = Memory(
                        category=category,
                        content=content,
                        rationale=rationale,
                        context=context,
                        tags=tags,
                        keywords=keywords.strip(),
                        file_path=file_path_abs,
                        file_path_relative=file_path_rel,
                        is_permanent=is_permanent,
                        vector_embedding=vector_embedding,
                    )

                    session.add(memory)
                    await session.flush()  # Get ID without committing

                    # Add to TF-IDF index
                    text = content
                    if rationale:
                        text += " " + rationale
                    index.add_document(memory.id, text, tags)

                    # Upsert to Qdrant if available
                    if self._qdrant and vector_embedding:
                        embedding_list = vectors.decode(vector_embedding)
                        if embedding_list:
                            self._qdrant.upsert_memory(
                                memory_id=memory.id,
                                embedding=embedding_list,
                                metadata={
                                    "category": category,
                                    "tags": tags,
                                    "file_path": file_path_abs,
                                    "worked": None,
                                    "is_permanent": is_permanent,
                                },
                            )

                    created_ids.append(memory.id)
                    results["created_count"] += 1

                except Exception as e:
                    results["errors"].append({"index": i, "error": str(e)})
                    results["error_count"] += 1

            # Transaction commits here when exiting context manager
            results["ids"] = created_ids

        # Track decisions in session state for enforcement (after commit)
        if project_path:
            try:
                from .enforcement import SessionManager

                session_mgr = SessionManager(self.db)

                decision_ids = [
                    created_ids[j]
                    for j, (i, mem) in enumerate(validated_memories)
                    if j < len(created_ids) and mem.get("category") == "decision"
                ]

                for decision_id in decision_ids:
                    await session_mgr.add_pending_decision(project_path, decision_id)
            except Exception as e:
                logger.debug(f"Session tracking failed (non-fatal): {e}")

        # Clear recall cache since memories changed
        if results["created_count"] > 0:
            get_recall_cache().clear()

        logger.info(
            f"Batch stored {results['created_count']} memories "
            f"({results['error_count']} errors)"
        )

        return results

    async def get_memory_versions(
        self, memory_id: int, limit: int = 50
    ) -> list[dict[str, Any]]:
        """
        Get all versions of a memory in chronological order.

        Args:
            memory_id: The memory to get versions for
            limit: Maximum versions to return

        Returns:
            List of version dicts ordered by version_number ascending
        """
        async with self.db.get_session() as session:
            result = await session.execute(
                select(MemoryVersion)
                .where(MemoryVersion.memory_id == memory_id)
                .order_by(MemoryVersion.version_number.asc())
                .limit(limit)
            )
            versions = result.scalars().all()

            return [
                {
                    "id": v.id,
                    "memory_id": v.memory_id,
                    "version_number": v.version_number,
                    "content": v.content,
                    "rationale": v.rationale,
                    "context": v.context,
                    "tags": v.tags,
                    "outcome": v.outcome,
                    "worked": v.worked,
                    "change_type": v.change_type,
                    "change_description": v.change_description,
                    "changed_at": v.changed_at.isoformat() if v.changed_at else None,
                }
                for v in versions
            ]

    async def get_memory_at_time(
        self, memory_id: int, point_in_time: datetime
    ) -> dict[str, Any] | None:
        """
        Get the state of a memory as it was at a specific point in time.

        Uses version history to reconstruct the memory state.

        Args:
            memory_id: The memory to query
            point_in_time: The timestamp to query at

        Returns:
            Memory state dict at that time, or None if memory didn't exist
        """
        # Normalize to UTC for comparison
        if point_in_time.tzinfo:
            query_time = point_in_time.astimezone(timezone.utc).replace(tzinfo=None)
        else:
            query_time = point_in_time

        async with self.db.get_session() as session:
            # Find the latest version that existed at or before point_in_time
            result = await session.execute(
                select(MemoryVersion)
                .where(
                    MemoryVersion.memory_id == memory_id,
                    MemoryVersion.changed_at <= query_time,
                )
                .order_by(MemoryVersion.version_number.desc())
                .limit(1)
            )
            version = result.scalar_one_or_none()

            if not version:
                return None

            return {
                "id": memory_id,
                "version_number": version.version_number,
                "content": version.content,
                "rationale": version.rationale,
                "context": version.context,
                "tags": version.tags,
                "outcome": version.outcome,
                "worked": version.worked,
                "as_of": point_in_time.isoformat(),
                "version_created_at": version.changed_at.isoformat()
                if version.changed_at
                else None,
            }

    async def get_memory_evolution(
        self,
        entity_name: str,
        entity_type: str | None = None,
        include_invalidated: bool = True,
    ) -> dict[str, Any]:
        """
        Trace how understanding of an entity evolved over time.

        Returns a timeline of memory versions that mention this entity,
        including bi-temporal information (when true, when learned, invalidations).

        Answers: "How has our understanding of X changed?"

        Args:
            entity_name: Name of the entity to trace (e.g., "UserService", "auth")
            entity_type: Optional filter by entity type (e.g., "class", "concept")
            include_invalidated: Whether to include invalidated versions (default True)

        Returns:
            Dict with:
            - found: Whether entity was found
            - entity: Entity details (name, type)
            - timeline: List of version entries with temporal info
            - current_beliefs: Versions still valid (not invalidated)
            - invalidation_chain: Which versions invalidated which

        Example:
            >>> evolution = await manager.get_memory_evolution("UserService")
            >>> for belief in evolution["timeline"]:
            ...     print(f"{belief['valid_from']}: {belief['content_preview']}")
        """
        from .graph.temporal import trace_knowledge_evolution
        from .models import ExtractedEntity

        async with self.db.get_session() as session:
            # Find entity by name (and optionally type)
            query = select(ExtractedEntity).where(ExtractedEntity.name == entity_name)
            if entity_type:
                query = query.where(ExtractedEntity.entity_type == entity_type)

            result = await session.execute(query.limit(1))
            entity = result.scalar_one_or_none()

            if not entity:
                return {
                    "found": False,
                    "error": f"Entity '{entity_name}' not found",
                    "entity": None,
                    "timeline": [],
                    "current_beliefs": [],
                    "invalidation_chain": [],
                }

            # Use the trace_knowledge_evolution function from temporal module
            return await trace_knowledge_evolution(
                session=session,
                entity_id=entity.id,
                include_invalidated=include_invalidated,
            )

    async def _check_conflicts(
        self, content: str, tags: list[str] | None = None
    ) -> list[dict]:
        """
        Check for conflicts with existing memories using deep semantic search.

        Uses Qdrant vectors (if available) or TF-IDF to find semantically similar
        memories across the ENTIRE database, not just recent ones. This catches
        conflicts with decisions made long ago that might still be relevant.
        """
        await self._check_index_freshness()
        await self._ensure_index()

        # Use hybrid search (TF-IDF + Qdrant vectors if available)
        search_results = self._hybrid_search(content, top_k=50, tfidf_threshold=0.3)

        if not search_results:
            return []

        # Get IDs of similar memories
        similar_ids = [doc_id for doc_id, score in search_results if score >= 0.4]

        if not similar_ids:
            return []

        # Fetch full memory details only for similar ones
        async with self.db.get_session() as session:
            result = await session.execute(
                select(Memory).where(
                    Memory.id.in_(similar_ids), _not_archived_condition()
                )
            )
            existing = [
                {
                    "id": m.id,
                    "content": m.content,
                    "category": m.category,
                    "worked": m.worked,
                    "outcome": m.outcome,
                    "tags": m.tags,
                }
                for m in result.scalars().all()
            ]

        return detect_conflict(content, existing, similarity_threshold=0.5)

    def _truncate_content(self, content: str, max_length: int = 150) -> str:
        """Truncate content to max_length, adding ellipsis if truncated."""
        if len(content) <= max_length:
            return content
        return content[:max_length] + "..."

    async def _increment_recall_counts(self, memory_ids: list[int]) -> None:
        """Increment recall_count for accessed memories (for saliency-based pruning)."""
        if not memory_ids:
            return

        async with self.db.get_session() as session:
            await session.execute(
                Memory.__table__.update()
                .where(Memory.id.in_(memory_ids))
                .values(recall_count=Memory.recall_count + 1)
            )

    async def recall(
        self,
        topic: str,
        categories: list[str] | None = None,
        tags: list[str] | None = None,
        file_path: str | None = None,
        offset: int = 0,
        limit: int = 10,
        since: datetime | None = None,
        until: datetime | None = None,
        project_path: str | None = None,
        include_warnings: bool = True,
        decay_half_life_days: float = 30.0,
        include_linked: bool = False,
        condensed: bool = False,  # Endless Mode compression
        as_of_time: datetime | None = None,
    ) -> dict[str, Any]:
        """
        Recall memories relevant to a topic using semantic similarity.

        This is the core "active memory" function. It:
        1. Uses TF-IDF to find semantically similar memories
        2. Applies time decay to favor recent memories
        3. Boosts failed decisions (they're important warnings)
        4. Organizes by category

        Results are cached for 5 seconds to avoid repeated searches.
        Cache hits still update recall_count for saliency tracking.

        Pagination behavior:
        - offset/limit apply to the raw scored results BEFORE category distribution
        - The actual number of returned results may vary due to per-category limits
        - This design is intentional for efficiency (avoids fetching all memories just to paginate)
        - has_more indicates if there are more memories beyond offset+limit in the raw results

        Args:
            topic: What you're looking for
            categories: Limit to specific categories (default: all)
            tags: Filter to memories with these tags
            file_path: Filter to memories for this file
            offset: Number of results to skip (for pagination)
            limit: Max memories per category
            since: Only include memories created after this date
            until: Only include memories created before this date
            project_path: Optional project root for file path normalization
            include_warnings: Always include warnings even if not in categories
            decay_half_life_days: How quickly old memories lose relevance
            include_linked: If True, also search linked projects (read-only)
            condensed: If True, return compressed output (strips rationale, context,
                       truncates content). Reduces token usage by ~75%. Default: False.
            as_of_time: Return knowledge state as of this time (default: current).
                       Filters to memories where valid_from <= as_of_time AND
                       (valid_to IS NULL OR valid_to > as_of_time).
                       Use for: "What did we know about auth on 2025-12-01?"

        Returns:
            Dict with categorized memories and relevance scores
        """
        # Check cache first
        cache = get_recall_cache()
        cache_key = make_cache_key(
            topic,
            categories,
            tags,
            file_path,
            offset,
            limit,
            since.isoformat() if since else None,
            until.isoformat() if until else None,
            include_warnings,
            decay_half_life_days,
            include_linked,
            condensed,  # Include condensed in cache key for separate caching
            as_of_time.isoformat() if as_of_time else None,
        )
        found, cached_result = cache.get(cache_key)
        if found and cached_result is not None:
            logger.debug(f"recall cache hit for topic: {topic[:50]}...")
            # Still update recall_count for saliency tracking (side effect)
            recalled_ids = [
                m["id"]
                for cat in ["decisions", "patterns", "warnings", "learnings"]
                for m in cached_result.get(cat, [])
            ]
            await self._increment_recall_counts(recalled_ids)
            return cached_result

        await self._check_index_freshness()
        await self._ensure_index()

        # Auto-Zoom: use retrieval router if enabled or in shadow mode
        if settings.auto_zoom_enabled or settings.auto_zoom_shadow:
            router = self._get_retrieval_router()
            route_result = await router.route_search(topic, top_k=limit * 4)
            search_results = route_result["results"]
        else:
            search_results = self._hybrid_search(
                topic, top_k=limit * 4, tfidf_threshold=0.05
            )

        if not search_results and not include_linked:
            return {
                "memories": [],
                "message": "No relevant memories found",
                "topic": topic,
            }

        # Get full memory objects (may be empty if include_linked is True but no local results)
        memory_ids = [doc_id for doc_id, _ in search_results] if search_results else []
        dict(search_results) if search_results else {}

        async with self.db.get_session() as session:
            # Build query with date filters at database level for performance
            query = select(Memory).where(
                Memory.id.in_(memory_ids), _not_archived_condition()
            )

            def _to_utc_naive(dt_value: datetime) -> datetime:
                if dt_value.tzinfo:
                    return dt_value.astimezone(timezone.utc).replace(tzinfo=None)
                return dt_value

            if since:
                query = query.where(Memory.created_at >= _to_utc_naive(since))

            if until:
                query = query.where(Memory.created_at <= _to_utc_naive(until))

            result = await session.execute(query)
            memories = {m.id: m for m in result.scalars().all()}

        # Filter by tags if specified
        if tags:
            memories = {
                mid: mem
                for mid, mem in memories.items()
                if mem.tags and any(t in mem.tags for t in tags)
            }

        # Filter by file_path if specified
        if file_path:
            normalized_abs = None
            normalized_rel = None
            if project_path:
                normalized_abs, normalized_rel = _normalize_file_path(
                    file_path, project_path
                )

            normalized_filter = file_path.replace("\\", "/")
            if normalized_abs:
                normalized_abs = normalized_abs.replace("\\", "/")
            if normalized_rel:
                normalized_rel = normalized_rel.replace("\\", "/")

            def _matches_path(mem: Memory) -> bool:
                mem_abs = mem.file_path.replace("\\", "/") if mem.file_path else ""
                mem_rel = (
                    mem.file_path_relative.replace("\\", "/")
                    if getattr(mem, "file_path_relative", None)
                    else ""
                )

                if normalized_abs and mem_abs == normalized_abs:
                    return True
                if normalized_rel and mem_rel == normalized_rel:
                    return True
                if mem_abs and (
                    mem_abs.endswith(normalized_filter)
                    or normalized_filter.endswith(mem_abs)
                ):
                    return True
                if mem_rel and (
                    mem_rel.endswith(normalized_filter)
                    or normalized_filter.endswith(mem_rel)
                ):
                    return True
                return False

            memories = {mid: mem for mid, mem in memories.items() if _matches_path(mem)}

        # Filter by temporal validity (bi-temporal point-in-time query)
        if as_of_time is not None:
            # Normalize timezone
            query_time = as_of_time
            if query_time.tzinfo is None:
                query_time = query_time.replace(tzinfo=timezone.utc)

            # Filter memories by checking their latest version's validity
            from .graph.temporal import get_versions_at_time

            async def check_temporal_validity(memory_ids_to_check):
                """Check which memories have valid versions at the query time.

                Uses valid_time filtering only (when fact was true), not
                transaction_time (when we learned it). This supports the common
                use case of backfilling historical data with happened_at.

                For full bi-temporal "what did we know then?" queries, use
                get_versions_at_time() directly with as_of_transaction_time.
                """
                valid_ids = set()
                async with self.db.get_session() as session:
                    for mid in memory_ids_to_check:
                        # Only filter by valid_time, allowing backfilled data
                        # to be found even though it was recorded recently
                        versions = await get_versions_at_time(session, mid, query_time)
                        if versions:
                            valid_ids.add(mid)
                return valid_ids

            valid_memory_ids = await check_temporal_validity(list(memories.keys()))
            memories = {
                mid: mem for mid, mem in memories.items() if mid in valid_memory_ids
            }

        # Score with decay and organize
        scored_memories = []
        for mem_id, base_score in search_results or []:
            mem = memories.get(mem_id)
            if not mem:
                continue

            # Apply category filter
            if categories:
                cats = list(categories)
                if include_warnings and "warning" not in cats:
                    cats.append("warning")
                if mem.category not in cats:
                    continue

            # Calculate final score with decay
            # Permanent memories (patterns, warnings) don't decay - they're project facts
            if getattr(mem, "is_permanent", False) or mem.category in {
                "pattern",
                "warning",
            }:
                decay = 1.0  # No decay for semantic memories
            else:
                decay = calculate_memory_decay(mem.created_at, decay_half_life_days)

            final_score = base_score * decay

            # Boost failed decisions - they're valuable warnings
            if mem.worked is False:
                final_score *= FAILED_DECISION_BOOST

            # Boost warnings
            if mem.category == "warning":
                final_score *= WARNING_BOOST

            scored_memories.append((mem, final_score, base_score, decay))

        # Sort by final score
        scored_memories.sort(key=lambda x: x[1], reverse=True)

        # Count total before pagination
        total_count = len(scored_memories)

        # Apply pagination (offset and limit)
        paginated_memories = scored_memories[
            offset : offset + limit * 4
        ]  # limit * 4 to allow distribution across categories

        # Organize by category
        by_category = {"decisions": [], "patterns": [], "warnings": [], "learnings": []}

        for mem, final_score, base_score, decay in paginated_memories:
            cat_key = mem.category + "s"  # decision -> decisions
            if cat_key in by_category and len(by_category[cat_key]) < limit:
                # Build memory dict - condensed mode strips verbose fields
                if condensed:
                    mem_dict = {
                        "id": mem.id,
                        "content": self._truncate_content(mem.content),
                        "rationale": None,
                        "context": None,
                        "tags": mem.tags,
                        "relevance": round(final_score, 3),
                        "outcome": mem.outcome,
                        "worked": mem.worked,
                        "created_at": mem.created_at.isoformat(),
                    }
                else:
                    mem_dict = {
                        "id": mem.id,
                        "content": mem.content,
                        "rationale": mem.rationale,
                        "context": mem.context,
                        "tags": mem.tags,
                        "relevance": round(final_score, 3),
                        "semantic_match": round(base_score, 3),
                        "recency_weight": round(decay, 3),
                        "outcome": mem.outcome,
                        "worked": mem.worked,
                        "created_at": mem.created_at.isoformat(),
                    }

                # Add warning annotation for failed decisions
                if mem.worked is False:
                    mem_dict["_warning"] = (
                        f"⚠️ This approach FAILED: {mem.outcome or 'no details recorded'}"
                    )

                by_category[cat_key].append(mem_dict)

        total = sum(len(v) for v in by_category.values())

        # Generate summary
        summary_parts = []
        if by_category["warnings"]:
            summary_parts.append(f"{len(by_category['warnings'])} warnings")
        if any(m.get("worked") is False for cat in by_category.values() for m in cat):
            failed_count = sum(
                1
                for cat in by_category.values()
                for m in cat
                if m.get("worked") is False
            )
            summary_parts.append(f"{failed_count} failed approaches to avoid")
        if by_category["patterns"]:
            summary_parts.append(f"{len(by_category['patterns'])} patterns to follow")

        # Increment recall_count for accessed memories (saliency tracking)
        recalled_ids = [m["id"] for cat in by_category.values() for m in cat]
        await self._increment_recall_counts(recalled_ids)

        result = {
            "topic": topic,
            "found": total,
            "total_count": total_count,
            "offset": offset,
            "limit": limit,
            "has_more": offset + limit < total_count,
            "summary": " | ".join(summary_parts) if summary_parts else None,
            **by_category,
        }

        # Add temporal query metadata if point-in-time query was used
        if as_of_time is not None:
            result["query_time"] = as_of_time.isoformat()
            result["temporal_filter"] = "point_in_time"

        # Aggregate from linked projects if requested
        if include_linked and project_path:
            from .links import LinkManager

            link_mgr = LinkManager(self.db)

            try:
                linked_managers = await link_mgr.get_linked_db_managers(project_path)

                for linked_path, linked_db in linked_managers:
                    try:
                        linked_memory = MemoryManager(linked_db)
                        linked_result = await linked_memory.recall(
                            topic=topic,
                            categories=categories,
                            tags=tags,
                            file_path=file_path,
                            offset=0,
                            # Limit linked project results to balance with main project
                            limit=limit // 2 if limit > 1 else 1,
                            since=since,
                            until=until,
                            project_path=linked_path,
                            include_warnings=include_warnings,
                            decay_half_life_days=decay_half_life_days,
                            include_linked=False,  # Don't recurse
                        )

                        # Merge results, tagging with source
                        for category in [
                            "decisions",
                            "patterns",
                            "warnings",
                            "learnings",
                        ]:
                            if category in linked_result:
                                for memory in linked_result[category]:
                                    memory["_from_linked"] = linked_path
                                    result.setdefault(category, []).append(memory)
                    except Exception as e:
                        logger.warning(
                            f"Could not recall from linked project {linked_path}: {e}"
                        )
            except Exception as e:
                logger.warning(f"Could not get linked projects: {e}")

        # JIT compression metadata: check if assembled result text exceeds
        # token thresholds and add informational metadata to the result dict.
        # Individual memory objects remain uncompressed (structured data).
        # The metadata tells callers "this result set has X tokens, would
        # compress to Y".  JIT is an enhancement, never a gate.
        if settings.auto_zoom_enabled or settings.auto_zoom_shadow:
            try:
                all_content = "\n\n".join(
                    m.get("content", "")
                    for cat in ["decisions", "patterns", "warnings", "learnings"]
                    for m in result.get(cat, [])
                )
                if all_content:
                    from .compression.jit import get_jit_compressor

                    jit = get_jit_compressor()
                    jit_result = jit.compress_if_needed(all_content)
                    if jit_result.get("threshold_triggered") is not None:
                        result["compression_metadata"] = {
                            "original_tokens": jit_result["original_tokens"],
                            "compressed_tokens": jit_result["compressed_tokens"],
                            "compression_rate": jit_result["compression_rate"],
                            "threshold_triggered": jit_result["threshold_triggered"],
                        }
                        logger.debug(
                            "[AUTO-ZOOM] JIT metadata: %s tier, %d -> %d tokens",
                            jit_result["threshold_triggered"],
                            jit_result["original_tokens"],
                            jit_result["compressed_tokens"],
                        )
            except Exception:
                logger.warning(
                    "[AUTO-ZOOM] JIT compression metadata failed, continuing without",
                    exc_info=True,
                )

        # Cache the result
        cache.set(cache_key, result)

        return result

    async def recall_with_compression(
        self,
        query: str,
        project_path: str,
        limit: int = 10,
        include_communities: bool = True,
        **kwargs,
    ) -> dict[str, Any]:
        """
        Recall memories with optional context compression.

        Uses hierarchical strategy:
        - Simple queries: Return community summaries (pre-compressed)
        - Medium queries: Hybrid with moderate compression
        - Complex queries: Full context with adaptive compression

        Args:
            query: Search query
            project_path: Project path for community lookup
            limit: Maximum memories to return
            include_communities: Whether to include community summaries
            **kwargs: Additional args passed to recall()

        Returns:
            Dict with memories and optimized context:
                - memories: List of memory dicts
                - context: Optimized context string
                - compression_stats: Dict with compression details
        """
        # Get recall plan
        plan = self.recall_planner.plan_recall(query)

        # Get raw memories via existing recall
        result = await self.recall(
            query, limit=limit, project_path=project_path, **kwargs
        )

        # Flatten memories from categories
        memories = []
        for category in ["decisions", "patterns", "warnings", "learnings"]:
            memories.extend(result.get(category, []))

        # Get community summaries if requested
        community_summaries = None
        if include_communities and project_path:
            try:
                from .communities import CommunityManager

                cm = CommunityManager(self.db)
                communities = await cm.get_communities(project_path)
                # Extract summaries from top communities
                community_summaries = [
                    c.get("summary", "")
                    for c in communities[: plan.max_communities]
                    if c.get("summary")
                ]
            except Exception as e:
                logger.warning(f"Failed to get communities for compression: {e}")

        # Ensure compressor is initialized (triggers lazy load)
        _ = self.compressor

        # Get optimized context
        context_result = self._hierarchical_context.get_context(
            query=query,
            memories=memories,
            community_summaries=community_summaries,
            plan=plan,
        )

        return {
            "memories": memories,
            "context": context_result["context"],
            "compression_stats": {
                "strategy": context_result["strategy"],
                "compression_applied": context_result["compression_applied"],
                "token_count": context_result.get("token_count"),
                "original_tokens": context_result.get("original_tokens"),
                "compression_ratio": context_result.get("compression_ratio"),
            },
        }

    async def record_outcome(
        self,
        memory_id: int,
        outcome: str,
        worked: bool,
        project_path: str | None = None,
    ) -> dict[str, Any]:
        """
        Record the outcome of a decision/pattern to learn from it.

        Failed outcomes are especially valuable - they become implicit warnings
        that get boosted in future recalls.

        Args:
            memory_id: The memory to update
            outcome: What actually happened
            worked: Did it work out?
            project_path: Optional project path for auto-activating failed decisions

        Returns:
            Updated memory with any auto-generated warnings
        """
        # Collect data needed for response and nested operations
        memory_content = None
        memory_category = None
        memory_tags = None
        memory_file_path = None
        memory_is_permanent = None
        memory_vector_embedding = None

        async with self.db.get_session() as session:
            result = await session.execute(select(Memory).where(Memory.id == memory_id))
            memory = result.scalar_one_or_none()

            if not memory:
                return {"error": f"Memory {memory_id} not found"}

            # Cache values needed after session closes
            memory_content = memory.content
            memory_category = memory.category
            memory_tags = memory.tags
            memory_file_path = memory.file_path
            memory_is_permanent = memory.is_permanent
            memory_vector_embedding = memory.vector_embedding

            memory.outcome = outcome
            memory.worked = worked
            memory.updated_at = datetime.now(timezone.utc)

            # Get next version number and create outcome version
            result = await session.execute(
                select(func.max(MemoryVersion.version_number)).where(
                    MemoryVersion.memory_id == memory_id
                )
            )
            current_max = result.scalar() or 0

            version = MemoryVersion(
                memory_id=memory_id,
                version_number=current_max + 1,
                content=memory_content,
                rationale=memory.rationale,
                context=memory.context,
                tags=memory_tags,
                outcome=outcome,
                worked=worked,
                change_type="outcome_recorded",
                change_description=f"Outcome: {'worked' if worked else 'failed'}",
            )
            session.add(version)

            # Update Qdrant metadata with worked status
            if self._qdrant and memory_vector_embedding:
                embedding_list = vectors.decode(memory_vector_embedding)
                if embedding_list:
                    self._qdrant.upsert_memory(
                        memory_id=memory_id,
                        embedding=embedding_list,
                        metadata={
                            "category": memory_category,
                            "tags": memory_tags or [],
                            "file_path": memory_file_path,
                            "worked": worked,
                            "is_permanent": memory_is_permanent,
                        },
                    )

        # Session is now closed - safe to perform nested operations that open new sessions

        response = {
            "id": memory_id,
            "content": memory_content,
            "outcome": outcome,
            "worked": worked,
        }

        # If it failed, suggest creating an explicit warning
        if not worked:
            response["suggestion"] = {
                "action": "consider_warning",
                "message": "This failure will boost this memory in future recalls. Consider also creating an explicit warning with more context.",
                "example": f'remember("warning", "Avoid: {memory_content[:50]}...", rationale="{outcome}")',
            }
            logger.info(
                f"Memory {memory_id} marked as failed - will be boosted as warning"
            )

        response["message"] = (
            "Outcome recorded - this failure will inform future recalls"
            if not worked
            else "Outcome recorded successfully"
        )

        # Remove from pending decisions (now safe - outer session is closed)
        try:
            from .enforcement import SessionManager

            session_mgr = SessionManager(self.db)
            # Use passed project_path or fall back to current working directory
            effective_project_path = project_path or os.getcwd()
            await session_mgr.remove_pending_decision(effective_project_path, memory_id)
        except Exception as e:
            logger.debug(f"Session tracking failed (non-fatal): {e}")

        # Auto-add to active context if failed (and project_path provided)
        if not worked and project_path:
            try:
                from .active_context import ActiveContextManager

                acm = ActiveContextManager(self.db)
                truncated_outcome = (
                    outcome[:50] + "..." if len(outcome) > 50 else outcome
                )
                await acm.add_to_context(
                    project_path=project_path,
                    memory_id=memory_id,
                    reason=f"Auto-activated: Failed decision - {truncated_outcome}",
                    priority=10,  # High priority for failures
                )
            except Exception as e:
                logger.debug(f"Could not auto-activate failed decision: {e}")

        # Clear recall cache since memory outcome changed (affects scoring)
        get_recall_cache().clear()

        return response

    async def get_statistics(self) -> dict[str, Any]:
        """Get memory statistics with learning insights."""
        async with self.db.get_session() as session:
            # Count by category
            result = await session.execute(
                select(Memory.category, func.count(Memory.id)).group_by(Memory.category)
            )
            by_category = {row[0]: row[1] for row in result.all()}

            # Count outcomes
            result = await session.execute(
                select(func.count(Memory.id)).where(Memory.worked == True)  # noqa: E712
            )
            worked = result.scalar() or 0

            result = await session.execute(
                select(func.count(Memory.id)).where(Memory.worked == False)  # noqa: E712
            )
            failed = result.scalar() or 0

            total = sum(by_category.values())

            # Calculate learning rate
            outcomes_recorded = worked + failed
            learning_rate = outcomes_recorded / total if total > 0 else 0

            return {
                "total_memories": total,
                "by_category": by_category,
                "with_outcomes": {
                    "worked": worked,
                    "failed": failed,
                    "pending": total - worked - failed,
                },
                "learning_insights": {
                    "outcome_tracking_rate": round(learning_rate, 2),
                    "failure_rate": round(failed / outcomes_recorded, 2)
                    if outcomes_recorded > 0
                    else None,
                    "suggestion": (
                        "Record more outcomes to improve memory quality"
                        if learning_rate < 0.3
                        else "Good outcome tracking!"
                        if learning_rate > 0.5
                        else None
                    ),
                },
            }

    async def search(self, query: str, limit: int = 20) -> list[dict[str, Any]]:
        """
        Search across all memories using semantic similarity.
        """
        await self._check_index_freshness()
        index = await self._ensure_index()

        # Search using TF-IDF
        results = index.search(query, top_k=limit, threshold=0.05)

        if not results:
            # Fall back to text search for exact matches
            async with self.db.get_session() as session:
                result = await session.execute(
                    select(Memory)
                    .where(
                        _not_archived_condition(),
                        or_(
                            Memory.content.like(f"%{query}%"),
                            Memory.rationale.like(f"%{query}%"),
                        ),
                    )
                    .order_by(desc(Memory.created_at))
                    .limit(limit)
                )
                memories = result.scalars().all()

                return [
                    {
                        "id": m.id,
                        "category": m.category,
                        "content": m.content,
                        "rationale": m.rationale,
                        "tags": m.tags,
                        "relevance": 0.5,  # Exact match baseline
                        "created_at": m.created_at.isoformat(),
                    }
                    for m in memories
                ]

        # Get full memory objects
        memory_ids = [doc_id for doc_id, _ in results]

        async with self.db.get_session() as session:
            result = await session.execute(
                select(Memory).where(
                    Memory.id.in_(memory_ids), _not_archived_condition()
                )
            )
            memories = {m.id: m for m in result.scalars().all()}

        return [
            {
                "id": mem_id,
                "category": memories[mem_id].category,
                "content": memories[mem_id].content,
                "rationale": memories[mem_id].rationale,
                "tags": memories[mem_id].tags,
                "relevance": round(score, 3),
                "created_at": memories[mem_id].created_at.isoformat(),
            }
            for mem_id, score in results
            if mem_id in memories
        ]

    async def find_related(
        self, memory_id: int, limit: int = 5
    ) -> list[dict[str, Any]]:
        """
        Find memories related to a specific memory.

        Useful for exploring connected decisions/patterns.
        """
        async with self.db.get_session() as session:
            result = await session.execute(select(Memory).where(Memory.id == memory_id))
            source = result.scalar_one_or_none()

            if not source:
                return []

        # Search using the source memory's content
        text = source.content
        if source.rationale:
            text += " " + source.rationale

        results = await self.search(text, limit=limit + 1)

        # Filter out the source memory itself
        return [r for r in results if r["id"] != memory_id][:limit]

    async def recall_for_file(
        self, file_path: str, limit: int = 10, project_path: str | None = None
    ) -> dict[str, Any]:
        """
        Get all memories associated with a specific file.

        Use this when opening a file to see all relevant context -
        warnings, patterns, and past decisions about this file.

        Args:
            file_path: The file path to look up
            limit: Max memories to return
            project_path: Optional project root path for normalizing file paths

        Returns:
            Dict with memories organized by category
        """
        # Normalize the input path if project_path is provided
        normalized_abs = None
        normalized_rel = None
        if project_path:
            normalized_abs, normalized_rel = _normalize_file_path(
                file_path, project_path
            )

        async with self.db.get_session() as session:
            # Query both file_path and file_path_relative columns
            if normalized_abs or normalized_rel:
                # Use normalized paths with OR condition
                conditions = []
                if normalized_abs:
                    conditions.append(Memory.file_path == normalized_abs)
                if normalized_rel:
                    conditions.append(Memory.file_path_relative == normalized_rel)

                result = await session.execute(
                    select(Memory)
                    .where(_not_archived_condition(), or_(*conditions))
                    .order_by(desc(Memory.created_at))
                    .limit(limit)
                )
            else:
                # Fallback to original behavior if no project_path
                result = await session.execute(
                    select(Memory)
                    .where(_not_archived_condition(), Memory.file_path == file_path)
                    .order_by(desc(Memory.created_at))
                    .limit(limit)
                )
            direct_memories = result.scalars().all()

            # Also search for memories mentioning this file in content
            # Use os.path for cross-platform compatibility
            filename = os.path.basename(file_path) if file_path else file_path
            result = await session.execute(
                select(Memory)
                .where(
                    _not_archived_condition(),
                    or_(
                        Memory.content.like(f"%{filename}%"),
                        Memory.rationale.like(f"%{filename}%"),
                    ),
                )
                .order_by(desc(Memory.created_at))
                .limit(limit)
            )
            mentioned_memories = result.scalars().all()

        # Combine and deduplicate
        seen_ids = set()
        all_memories = []
        for mem in direct_memories:
            if mem.id not in seen_ids:
                seen_ids.add(mem.id)
                all_memories.append(mem)
        for mem in mentioned_memories:
            if mem.id not in seen_ids:
                seen_ids.add(mem.id)
                all_memories.append(mem)

        # Organize by category
        by_category = {"decisions": [], "patterns": [], "warnings": [], "learnings": []}

        for mem in all_memories[:limit]:
            cat_key = mem.category + "s"
            if cat_key in by_category:
                mem_dict = {
                    "id": mem.id,
                    "content": mem.content,
                    "rationale": mem.rationale,
                    "context": mem.context,
                    "tags": mem.tags,
                    "file_path": mem.file_path,
                    "outcome": mem.outcome,
                    "worked": mem.worked,
                    "created_at": mem.created_at.isoformat(),
                }

                if mem.worked is False:
                    mem_dict["_warning"] = (
                        f"⚠️ This approach FAILED: {mem.outcome or 'no details recorded'}"
                    )

                by_category[cat_key].append(mem_dict)

        total = sum(len(v) for v in by_category.values())

        # Increment recall_count for accessed memories (saliency tracking)
        recalled_ids = [m["id"] for cat in by_category.values() for m in cat]
        await self._increment_recall_counts(recalled_ids)

        return {
            "file_path": file_path,
            "found": total,
            "has_warnings": len(by_category["warnings"]) > 0
            or any(
                m.get("worked") is False for cat in by_category.values() for m in cat
            ),
            **by_category,
        }

    async def rebuild_index(self) -> dict[str, Any]:
        """
        Force rebuild of TF-IDF index.

        Qdrant is persistent and doesn't need rebuilding.
        Returns statistics about the rebuild.
        """
        # Clear existing TF-IDF index
        self._index = TFIDFIndex()
        self._index_loaded = False

        # Rebuild TF-IDF from SQLite
        async with self.db.get_session() as session:
            result = await session.execute(
                select(Memory).where(_not_archived_condition())
            )
            memories = result.scalars().all()

            for mem in memories:
                text = mem.content
                if mem.rationale:
                    text += " " + mem.rationale
                self._index.add_document(mem.id, text, mem.tags)
                # Qdrant is persistent and doesn't need rebuilding

        self._index_loaded = True
        self._index_built_at = datetime.now(timezone.utc)

        return {
            "memories_indexed": len(memories),
            "vectors_indexed": self._qdrant.get_count() if self._qdrant else 0,
            "built_at": self._index_built_at.isoformat(),
        }

    async def compact_memories(
        self,
        summary: str,
        limit: int = 10,
        topic: str | None = None,
        dry_run: bool = True,
    ) -> dict[str, Any]:
        """
        Compact recent episodic memories into a single summary.

        Creates a summary memory, links it to originals via 'supersedes' edges,
        and archives the originals. Preserves full history via graph edges.

        Args:
            summary: The summary text (must be >= 50 chars after trimming)
            limit: Max number of memories to compact (must be > 0)
            topic: Optional topic filter (content/rationale/tags substring match)
            dry_run: If True, preview candidates without changes (default: True)

        Returns:
            Result dict with status, summary_id, compacted_count, etc.
        """
        # Validate inputs
        summary = summary.strip() if summary else ""
        if len(summary) < 50:
            return {
                "error": "Summary must be at least 50 characters",
                "provided_length": len(summary),
            }
        if limit <= 0:
            return {"error": "Limit must be greater than 0"}

        async with self.db.get_session() as session:
            # Select candidate memories: episodic, not pinned, not permanent, not archived
            # For decisions, require outcome to be recorded (don't hide pending decisions)
            query = (
                select(Memory)
                .where(
                    Memory.category.in_(["decision", "learning"]),
                    or_(Memory.pinned == False, Memory.pinned.is_(None)),  # noqa: E712
                    or_(Memory.is_permanent == False, Memory.is_permanent.is_(None)),  # noqa: E712
                    _not_archived_condition(),
                )
                .order_by(Memory.created_at.asc())  # Oldest first
            )

            # For decisions, exclude those without outcomes (pending)
            # This is done via post-fetch filtering to keep query simple

            result = await session.execute(query)
            all_candidates = result.scalars().all()

            # Filter: decisions must have outcome recorded
            candidates = []
            for mem in all_candidates:
                if mem.category == "decision" and mem.outcome is None and mem.worked is None:
                    continue  # Skip pending decisions
                candidates.append(mem)

            # Apply topic filter if provided
            if topic:
                topic_lower = topic.lower()
                filtered = []
                for mem in candidates:
                    content_match = topic_lower in (mem.content or "").lower()
                    rationale_match = topic_lower in (mem.rationale or "").lower()
                    tags_match = any(
                        topic_lower in str(tag).lower() for tag in (mem.tags or [])
                    )
                    if content_match or rationale_match or tags_match:
                        filtered.append(mem)
                candidates = filtered

            # Apply limit
            candidates = candidates[:limit]

            if not candidates:
                reason = "topic_mismatch" if topic else "no_candidates"
                return {
                    "status": "skipped",
                    "reason": reason,
                    "topic": topic,
                    "message": "No matching memories to compact",
                }

            compacted_ids = [m.id for m in candidates]

            # Dry run - just return preview
            if dry_run:
                return {
                    "status": "dry_run",
                    "would_compact": len(candidates),
                    "candidate_ids": compacted_ids,
                    "candidates": [
                        {
                            "id": m.id,
                            "category": m.category,
                            "content": m.content[:100] + "..."
                            if len(m.content) > 100
                            else m.content,
                            "created_at": m.created_at.isoformat(),
                        }
                        for m in candidates
                    ],
                    "topic": topic,
                    "message": f"Would compact {len(candidates)} memories (dry_run=True)",
                }

            # Compute tags: ["compacted", "checkpoint"] + topic if provided
            summary_tags = ["compacted", "checkpoint"]
            if topic:
                summary_tags.append(topic)
            # Add union of common tags (appearing in > 50% of candidates)
            tag_counts: dict[str, int] = {}
            for mem in candidates:
                for tag in mem.tags or []:
                    if isinstance(tag, str) and tag not in summary_tags:
                        tag_counts[tag] = tag_counts.get(tag, 0) + 1
            threshold = len(candidates) / 2
            for tag, count in tag_counts.items():
                if count >= threshold:
                    summary_tags.append(tag)
            summary_tags = sorted(set(summary_tags))

            # Create summary memory
            keywords = extract_keywords(summary, summary_tags)
            vector_embedding = (
                vectors.encode_document(summary) if self._vectors_enabled else None
            )

            summary_memory = Memory(
                category="learning",
                content=summary,
                rationale=f"Compacted summary of {len(candidates)} memories.",
                context={"compacted_ids": compacted_ids, "topic": topic},
                tags=summary_tags,
                keywords=keywords,
                is_permanent=False,
                vector_embedding=vector_embedding,
            )
            session.add(summary_memory)
            await session.flush()  # Get the ID

            summary_id = summary_memory.id

            # Create supersedes relationships and archive originals
            for mem in candidates:
                rel = MemoryRelationship(
                    source_id=summary_id,
                    target_id=mem.id,
                    relationship="supersedes",
                    description="Session compaction",
                )
                session.add(rel)
                mem.archived = True
                # Delete from Qdrant since memory is archived
                if self._qdrant:
                    self._qdrant.delete_memory(mem.id)

        # Rebuild index to reflect archived items and new summary
        await self.rebuild_index()

        # Clear recall cache since memories have been modified
        get_recall_cache().clear()

        return {
            "status": "compacted",
            "summary_id": summary_id,
            "compacted_count": len(candidates),
            "compacted_ids": compacted_ids,
            "category": "learning",
            "tags": summary_tags,
            "topic": topic,
            "message": f"Compacted {len(candidates)} memories into summary {summary_id}",
        }

    # =========================================================================
    # Graph Memory Methods - Explicit relationship edges between memories
    # =========================================================================

    async def link_memories(
        self,
        source_id: int,
        target_id: int,
        relationship: str,
        description: str | None = None,
        confidence: float = 1.0,
    ) -> dict[str, Any]:
        """
        Create an explicit relationship edge between two memories.

        Args:
            source_id: The "from" memory ID
            target_id: The "to" memory ID
            relationship: Type of relationship (led_to, supersedes, depends_on, conflicts_with, related_to)
            description: Optional context explaining this relationship
            confidence: Strength of relationship (0.0-1.0, default 1.0)

        Returns:
            Status of the link operation
        """
        # Validate relationship type
        if relationship not in VALID_RELATIONSHIPS:
            return {
                "error": f"Invalid relationship type '{relationship}'. Valid types: {', '.join(sorted(VALID_RELATIONSHIPS))}"
            }

        # Prevent self-reference
        if source_id == target_id:
            return {"error": "Cannot link a memory to itself"}

        from sqlalchemy import and_

        async with self.db.get_session() as session:
            # Verify both memories exist
            source = await session.get(Memory, source_id)
            target = await session.get(Memory, target_id)

            if not source:
                return {"error": f"Source memory {source_id} not found"}
            if not target:
                return {"error": f"Target memory {target_id} not found"}

            # Check for existing relationship
            existing = await session.execute(
                select(MemoryRelationship).where(
                    and_(
                        MemoryRelationship.source_id == source_id,
                        MemoryRelationship.target_id == target_id,
                        MemoryRelationship.relationship == relationship,
                    )
                )
            )
            if existing.scalar_one_or_none():
                return {
                    "status": "already_exists",
                    "source_id": source_id,
                    "target_id": target_id,
                    "relationship": relationship,
                }

            # Create the relationship
            rel = MemoryRelationship(
                source_id=source_id,
                target_id=target_id,
                relationship=relationship,
                description=description,
                confidence=confidence,
            )
            session.add(rel)
            await session.flush()  # Get the ID

            # Create versions for both memories to track relationship change
            for mem_id, direction in [(source_id, "outgoing"), (target_id, "incoming")]:
                result = await session.execute(
                    select(func.max(MemoryVersion.version_number)).where(
                        MemoryVersion.memory_id == mem_id
                    )
                )
                current_max = result.scalar() or 0

                mem = await session.get(Memory, mem_id)
                version = MemoryVersion(
                    memory_id=mem_id,
                    version_number=current_max + 1,
                    content=mem.content,
                    rationale=mem.rationale,
                    context=mem.context,
                    tags=mem.tags,
                    outcome=mem.outcome,
                    worked=mem.worked,
                    change_type="relationship_changed",
                    change_description=f"Added {direction} '{relationship}' relationship",
                )
                session.add(version)

            logger.info(
                f"Created relationship: {source_id} --{relationship}--> {target_id}"
            )

            return {
                "status": "linked",
                "id": rel.id,
                "source_id": source_id,
                "target_id": target_id,
                "relationship": relationship,
                "description": description,
                "message": f"Linked memory {source_id} --{relationship}--> {target_id}",
            }

    async def unlink_memories(
        self, source_id: int, target_id: int, relationship: str | None = None
    ) -> dict[str, Any]:
        """
        Remove a relationship edge between two memories.

        Args:
            source_id: The "from" memory ID
            target_id: The "to" memory ID
            relationship: Specific relationship to remove (if None, removes all between the pair)

        Returns:
            Status of the unlink operation
        """
        from sqlalchemy import and_, delete

        async with self.db.get_session() as session:
            # Build conditions
            conditions = [
                MemoryRelationship.source_id == source_id,
                MemoryRelationship.target_id == target_id,
            ]
            if relationship:
                conditions.append(MemoryRelationship.relationship == relationship)

            # Find existing relationships
            result = await session.execute(
                select(MemoryRelationship).where(and_(*conditions))
            )
            existing = result.scalars().all()

            if not existing:
                return {
                    "status": "not_found",
                    "source_id": source_id,
                    "target_id": target_id,
                    "relationship": relationship,
                }

            # Delete the relationships
            await session.execute(delete(MemoryRelationship).where(and_(*conditions)))

            logger.info(
                f"Removed {len(existing)} relationship(s) between {source_id} and {target_id}"
            )

            return {
                "status": "unlinked",
                "source_id": source_id,
                "target_id": target_id,
                "relationship": relationship,
                "removed_count": len(existing),
                "message": f"Removed {len(existing)} relationship(s)",
            }

    async def trace_chain(
        self,
        memory_id: int,
        direction: str = "both",
        relationship_types: list[str] | None = None,
        max_depth: int = 10,
    ) -> dict[str, Any]:
        """
        Traverse the memory graph from a starting point using recursive CTE.

        Args:
            memory_id: Starting memory ID
            direction: "forward" (descendants), "backward" (ancestors), or "both"
            relationship_types: Filter to specific relationship types (default: all)
            max_depth: Maximum traversal depth (default: 10)

        Returns:
            Chain of connected memories with relationship info
        """
        if direction not in ("forward", "backward", "both"):
            return {
                "error": f"Invalid direction '{direction}'. Use: forward, backward, both"
            }

        from sqlalchemy import text

        async with self.db.get_session() as session:
            # Verify starting memory exists
            start_memory = await session.get(Memory, memory_id)
            if not start_memory:
                return {"error": f"Memory {memory_id} not found"}

            # Build recursive CTE based on direction
            if direction == "forward":
                cte_sql = """
                    WITH RECURSIVE chain AS (
                        SELECT r.target_id as id, r.relationship, r.source_id as from_id, 1 as depth
                        FROM memory_relationships r
                        WHERE r.source_id = :start_id

                        UNION ALL

                        SELECT r.target_id, r.relationship, r.source_id, c.depth + 1
                        FROM memory_relationships r
                        JOIN chain c ON r.source_id = c.id
                        WHERE c.depth < :max_depth
                    )
                    SELECT DISTINCT c.id, c.relationship, c.from_id, c.depth, m.content, m.category
                    FROM chain c
                    JOIN memories m ON c.id = m.id
                    ORDER BY c.depth
                """
            elif direction == "backward":
                cte_sql = """
                    WITH RECURSIVE chain AS (
                        SELECT r.source_id as id, r.relationship, r.target_id as from_id, 1 as depth
                        FROM memory_relationships r
                        WHERE r.target_id = :start_id

                        UNION ALL

                        SELECT r.source_id, r.relationship, r.target_id, c.depth + 1
                        FROM memory_relationships r
                        JOIN chain c ON r.target_id = c.id
                        WHERE c.depth < :max_depth
                    )
                    SELECT DISTINCT c.id, c.relationship, c.from_id, c.depth, m.content, m.category
                    FROM chain c
                    JOIN memories m ON c.id = m.id
                    ORDER BY c.depth
                """
            else:  # both
                cte_sql = """
                    WITH RECURSIVE chain AS (
                        -- Forward edges
                        SELECT r.target_id as id, r.relationship, r.source_id as from_id, 1 as depth
                        FROM memory_relationships r
                        WHERE r.source_id = :start_id

                        UNION

                        -- Backward edges
                        SELECT r.source_id as id, r.relationship, r.target_id as from_id, 1 as depth
                        FROM memory_relationships r
                        WHERE r.target_id = :start_id

                        UNION ALL

                        -- Recursive forward
                        SELECT r.target_id, r.relationship, r.source_id, c.depth + 1
                        FROM memory_relationships r
                        JOIN chain c ON r.source_id = c.id
                        WHERE c.depth < :max_depth

                        UNION ALL

                        -- Recursive backward
                        SELECT r.source_id, r.relationship, r.target_id, c.depth + 1
                        FROM memory_relationships r
                        JOIN chain c ON r.target_id = c.id
                        WHERE c.depth < :max_depth
                    )
                    SELECT DISTINCT c.id, c.relationship, c.from_id, c.depth, m.content, m.category
                    FROM chain c
                    JOIN memories m ON c.id = m.id
                    ORDER BY c.depth
                """

            result = await session.execute(
                text(cte_sql), {"start_id": memory_id, "max_depth": max_depth}
            )
            rows = result.fetchall()

            # Filter by relationship types if specified
            chain = []
            for row in rows:
                if relationship_types and row[1] not in relationship_types:
                    continue
                chain.append(
                    {
                        "id": row[0],
                        "relationship": row[1],
                        "from_id": row[2],
                        "depth": row[3],
                        "content": row[4],
                        "category": row[5],
                    }
                )

            return {
                "memory_id": memory_id,
                "direction": direction,
                "max_depth": max_depth,
                "chain": chain,
                "total_found": len(chain),
                "message": f"Found {len(chain)} connected memories",
            }

    async def get_graph(
        self,
        memory_ids: list[int] | None = None,
        topic: str | None = None,
        format: str = "json",
        include_orphans: bool = False,
    ) -> dict[str, Any]:
        """
        Get a subgraph of memories and their relationships.

        Args:
            memory_ids: Specific memory IDs to include (if None, uses topic search)
            topic: Topic to search for memories (alternative to memory_ids)
            format: Output format - "json" or "mermaid"
            include_orphans: Include memories with no relationships

        Returns:
            Graph structure with nodes and edges
        """
        async with self.db.get_session() as session:
            # Determine which memories to include
            if memory_ids:
                result = await session.execute(
                    select(Memory).where(Memory.id.in_(memory_ids))
                )
                memories = result.scalars().all()
            elif topic:
                # Use recall to find relevant memories
                recall_result = await self.recall(topic, limit=20)
                all_mems = []
                for cat in ["decisions", "patterns", "warnings", "learnings"]:
                    all_mems.extend(recall_result.get(cat, []))
                if not all_mems:
                    return {
                        "nodes": [],
                        "edges": [],
                        "message": "No memories found for topic",
                    }
                memory_ids = [m["id"] for m in all_mems]
                result = await session.execute(
                    select(Memory).where(Memory.id.in_(memory_ids))
                )
                memories = result.scalars().all()
            else:
                return {"error": "Must provide either memory_ids or topic"}

            if not memories:
                return {"nodes": [], "edges": [], "message": "No memories found"}

            mem_ids = [m.id for m in memories]

            # Get all edges between these memories
            result = await session.execute(
                select(MemoryRelationship).where(
                    or_(
                        MemoryRelationship.source_id.in_(mem_ids),
                        MemoryRelationship.target_id.in_(mem_ids),
                    )
                )
            )
            edges = result.scalars().all()

            # Filter orphans if requested
            if not include_orphans and edges:
                connected_ids = set()
                for edge in edges:
                    connected_ids.add(edge.source_id)
                    connected_ids.add(edge.target_id)
                memories = [m for m in memories if m.id in connected_ids]

            # Build output
            nodes = [
                {
                    "id": m.id,
                    "content": m.content[:100] if len(m.content) > 100 else m.content,
                    "category": m.category,
                    "tags": m.tags or [],
                }
                for m in memories
            ]

            edge_list = [
                {
                    "source_id": e.source_id,
                    "target_id": e.target_id,
                    "relationship": e.relationship,
                    "description": e.description,
                    "confidence": e.confidence,
                }
                for e in edges
                if e.source_id in mem_ids and e.target_id in mem_ids
            ]

            result_dict = {
                "nodes": nodes,
                "edges": edge_list,
                "node_count": len(nodes),
                "edge_count": len(edge_list),
            }

            # Generate mermaid if requested
            if format == "mermaid":
                result_dict["mermaid"] = self._generate_mermaid(nodes, edge_list)

            return result_dict

    def _generate_mermaid(self, nodes: list[dict], edges: list[dict]) -> str:
        """Generate a Mermaid flowchart from graph data."""
        lines = ["flowchart TD"]

        # Map category to node shape
        category_shapes = {
            "decision": ("[[", "]]"),  # Stadium shape
            "pattern": ("((", "))"),  # Circle
            "warning": (">", "]"),  # Flag
            "learning": ("(", ")"),  # Rounded
        }

        # Add nodes
        for node in nodes:
            shape = category_shapes.get(node["category"], ("[", "]"))
            # Escape special chars and truncate
            label = node["content"][:30].replace('"', "'").replace("\n", " ")
            lines.append(f'    {node["id"]}{shape[0]}"{label}"{shape[1]}')

        # Arrow styles by relationship type
        arrow_styles = {
            "led_to": "-->",
            "supersedes": "-.->",
            "depends_on": "==>",
            "conflicts_with": "--x",
            "related_to": "---",
        }

        # Add edges
        for edge in edges:
            arrow = arrow_styles.get(edge["relationship"], "-->")
            lines.append(
                f"    {edge['source_id']} {arrow}|{edge['relationship']}| {edge['target_id']}"
            )

        return "\n".join(lines)

    # Maximum tags allowed in FTS search filter (prevent query explosion)
    _FTS_MAX_TAGS = 20

    def _build_fts_tag_filter(self, tags: list[str], params: dict[str, Any]) -> str:
        """
        Build parameterized tag filter clause for FTS search.

        Args:
            tags: List of tags to filter by (max _FTS_MAX_TAGS)
            params: Parameter dict to update with tag values

        Returns:
            SQL clause string with parameterized placeholders
        """
        # Limit tags to prevent query explosion
        safe_tags = tags[: self._FTS_MAX_TAGS]

        # Build placeholder names and populate params
        placeholders = []
        for i, tag in enumerate(safe_tags):
            param_name = f"tag{i}"
            placeholders.append(f":{param_name}")
            params[param_name] = tag

        placeholder_list = ", ".join(placeholders)
        return f"""
            AND EXISTS (
                SELECT 1 FROM json_each(m.tags)
                WHERE json_each.value IN ({placeholder_list})
            )
        """

    async def recall_hierarchical(
        self,
        topic: str,
        project_path: str | None = None,
        include_members: bool = False,
        limit: int = 10,
        use_leiden: bool = True,
    ) -> dict[str, Any]:
        """
        Hierarchical recall - community summaries first, then individual memories.

        Provides a GraphRAG-style layered response:
        1. Relevant community summaries (high-level overview)
        2. Individual memories (detailed)

        Uses Leiden-based communities (detected from knowledge graph) by default.
        If no communities exist, suggests running rebuild_communities.

        Args:
            topic: What you're looking for
            project_path: Project path for community lookup
            include_members: If True, include full member content for each community
            limit: Max results per layer
            use_leiden: If True, prefer Leiden-detected communities (default: True)

        Returns:
            Dict with communities and memories sections
        """
        from .communities import CommunityManager
        from .models import MemoryCommunity

        result = {
            "topic": topic,
            "communities": [],
            "memories": [],
            "community_source": "none",
        }

        # Get relevant communities if project_path provided
        if project_path:
            cm = CommunityManager(self.db)

            async with self.db.get_session() as session:
                # Search communities by topic in name, summary, or tags
                query = select(MemoryCommunity).where(
                    MemoryCommunity.project_path == project_path
                )
                communities_result = await session.execute(query)
                all_communities = communities_result.scalars().all()

                # If no communities exist, suggest detection
                if not all_communities:
                    result["community_hint"] = (
                        "No communities detected yet. Run rebuild_communities to "
                        "enable hierarchical summaries via Leiden algorithm."
                    )
                else:
                    result["community_source"] = "leiden" if use_leiden else "tag_based"

                # Use TF-IDF for semantic matching against community content
                # This improves matching: "authentication" will match "auth + jwt"
                topic_lower = topic.lower()
                scored_communities = []

                for c in all_communities:
                    # Build searchable text from community
                    search_text = f"{c.name} {c.summary} {' '.join(c.tags or [])}"

                    # Compute relevance score using multiple signals
                    score = 0.0

                    # Direct name match (highest weight)
                    if topic_lower in c.name.lower():
                        score += 1.0

                    # Summary contains topic
                    if topic_lower in c.summary.lower():
                        score += 0.5

                    # Tag match
                    if any(topic_lower in str(t).lower() for t in (c.tags or [])):
                        score += 0.3

                    # Word overlap scoring (pseudo TF-IDF)
                    topic_words = set(topic_lower.split())
                    search_words = set(search_text.lower().split())
                    overlap = topic_words & search_words
                    if topic_words:
                        overlap_score = len(overlap) / len(topic_words)
                        score += overlap_score * 0.5

                    if score > 0:
                        scored_communities.append((c, score))

                # Sort by relevance score
                scored_communities.sort(key=lambda x: x[1], reverse=True)

                # Build result with top communities
                relevant_communities = []
                for c, score in scored_communities[:limit]:
                    comm_dict = {
                        "id": c.id,
                        "name": c.name,
                        "summary": c.summary,
                        "tags": c.tags,
                        "member_count": c.member_count,
                        "level": c.level,
                        "relevance": round(score, 3),
                    }

                    if include_members:
                        members = await cm.get_community_members(c.id)
                        comm_dict["members"] = members.get("members", [])

                    relevant_communities.append(comm_dict)

                result["communities"] = relevant_communities

        # Also get individual memories via standard recall
        memories = await self.recall(topic, limit=limit, project_path=project_path)
        result["memories"] = {
            "decisions": memories.get("decisions", []),
            "patterns": memories.get("patterns", []),
            "warnings": memories.get("warnings", []),
            "learnings": memories.get("learnings", []),
        }

        return result

    async def fts_search(
        self,
        query: str,
        tags: list[str] | None = None,
        file_path: str | None = None,
        limit: int = 20,
        highlight: bool = False,
        highlight_start: str = "<b>",
        highlight_end: str = "</b>",
        excerpt_tokens: int = 32,
    ) -> list[dict[str, Any]]:
        """
        Fast full-text search using SQLite FTS5 with optional highlighting.

        Falls back to LIKE search if FTS5 is not available.

        Args:
            query: Search query (supports FTS5 syntax)
            tags: Optional tag filter (max 20 tags)
            file_path: Optional file path filter
            limit: Maximum results
            highlight: If True, include highlighted excerpts in results
            highlight_start: Opening tag for matched terms (default: <b>)
            highlight_end: Closing tag for matched terms (default: </b>)
            excerpt_tokens: Max tokens in excerpt (default: 32)

        Returns:
            List of matching memories with relevance info.
            If highlight=True, includes 'excerpt' field with highlighted matches.
        """
        # Input validation
        if not query or not query.strip():
            return []

        limit = min(max(1, limit), 100)  # Clamp to reasonable range
        excerpt_tokens = min(max(8, excerpt_tokens), 64)  # Reasonable excerpt size

        async with self.db.get_session() as session:
            try:
                from sqlalchemy import text

                # Base FTS5 query with parameterized inputs
                # The snippet function uses column index:
                # - Column 0 = content (from FTS index)
                # - Column 1 = rationale (if indexed)
                if highlight:
                    sql_parts = [
                        f"""
                        SELECT m.*,
                               bm25(memories_fts) as rank,
                               snippet(memories_fts, 0, '{highlight_start}', '{highlight_end}', '...', {excerpt_tokens}) as content_excerpt
                        FROM memories m
                        JOIN memories_fts ON m.id = memories_fts.rowid
                        WHERE memories_fts MATCH :query
                        AND (m.archived = 0 OR m.archived IS NULL)
                        """
                    ]
                else:
                    sql_parts = [
                        """
                        SELECT m.*, bm25(memories_fts) as rank
                        FROM memories m
                        JOIN memories_fts ON m.id = memories_fts.rowid
                        WHERE memories_fts MATCH :query
                        AND (m.archived = 0 OR m.archived IS NULL)
                        """
                    ]
                params: dict[str, Any] = {"query": query.strip()}

                # Add tag filter using helper
                if tags:
                    sql_parts.append(self._build_fts_tag_filter(tags, params))

                # Add file path filter
                if file_path:
                    sql_parts.append(" AND m.file_path = :file_path")
                    params["file_path"] = file_path

                sql_parts.append(" ORDER BY rank LIMIT :limit")
                params["limit"] = limit

                sql = "".join(sql_parts)
                result = await session.execute(text(sql), params)
                rows = result.fetchall()

                results = []
                for row in rows:
                    item = {
                        "id": row.id,
                        "category": row.category,
                        "content": row.content,
                        "rationale": row.rationale,
                        "tags": row.tags,
                        "file_path": row.file_path,
                        "relevance": abs(row.rank),  # bm25 returns negative scores
                        "created_at": row.created_at
                        if isinstance(row.created_at, str)
                        else (row.created_at.isoformat() if row.created_at else None),
                    }
                    if highlight and hasattr(row, "content_excerpt"):
                        item["excerpt"] = row.content_excerpt
                    results.append(item)

                return results

            except Exception as e:
                # FTS5 not available, fall back to LIKE search
                logger.debug(f"FTS5 not available, using LIKE search: {e}")
                return await self.search(query, limit=limit)
