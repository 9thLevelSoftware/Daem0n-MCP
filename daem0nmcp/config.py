"""
Centralized configuration using Pydantic Settings.

All settings are loaded from environment variables with DAEM0NMCP_ prefix.
Example: DAEM0NMCP_LOG_LEVEL=DEBUG
"""

import math
import re
import shutil
from collections.abc import Mapping
from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from .workspace import resolve_derived_path

_DEFAULT_EMBEDDING_BACKEND = "onnx"
_RETRIEVAL_PROVIDERS = frozenset(
    {"lexical", "dense", "graph", "temporal", "procedure", "outcome"}
)
_QDRANT_COLLECTION_PREFIX = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,31}$")


class Settings(BaseSettings):
    """Daem0nMCP configuration settings."""

    model_config = SettingsConfigDict(
        env_prefix="DAEM0NMCP_", env_file=".env", env_file_encoding="utf-8"
    )

    # Core paths
    project_root: str = "."
    workspace_roots: list[str] = Field(default_factory=list)
    storage_path: str | None = None  # Auto-detect if not set

    # Server
    log_level: str = "INFO"
    ui_rendering_enabled: bool = True
    sync_timeout_seconds: float = Field(
        default=15.0, ge=1.0, le=60.0, allow_inf_nan=False
    )

    @field_validator("sync_timeout_seconds", mode="before")
    @classmethod
    def validate_sync_deadline(cls, value):
        if isinstance(value, bool):
            raise ValueError("sync_timeout_seconds must be between 1 and 60")
        return value

    # Context management
    max_project_contexts: int = 10  # Maximum cached project contexts
    context_ttl_seconds: int = 3600  # 1 hour TTL for unused contexts

    # Enforcement settings
    pending_decision_threshold_hours: int = (
        24  # Hours before pending decisions block commits
    )

    # Ingestion limits
    max_content_size: int = 1_000_000  # 1MB max content
    max_chunks: int = 50  # Maximum chunks per ingestion
    ingest_timeout: int = 30  # Request timeout in seconds
    allowed_url_schemes: list[str] = ["http", "https"]

    # TODO scanner config
    todo_skip_dirs: list[str] = [
        "node_modules",
        ".git",
        ".venv",
        "venv",
        "__pycache__",
        "dist",
        "build",
        ".tox",
        ".mypy_cache",
        ".pytest_cache",
        ".eggs",
        ".coverage",
        "htmlcov",
        ".daem0nmcp",
        ".svn",
        ".hg",
    ]
    todo_skip_extensions: list[str] = [".pyc", ".pyo", ".so", ".dylib"]
    todo_max_files: int = 500

    # Qdrant vector storage
    qdrant_path: str | None = (
        None  # Path for local Qdrant storage, auto-detect if not set
    )
    qdrant_url: str | None = None  # Optional remote Qdrant URL (overrides local path)
    qdrant_api_key: str | None = None  # API key for remote Qdrant (if using cloud)
    qdrant_timeout_seconds: float = Field(default=10.0, gt=0.0, le=60.0)
    qdrant_collection_prefix: str = "daem0nmcp"

    # v7 retrieval orchestration
    retrieval_candidate_limit: int = Field(default=50, ge=1, le=1000)
    retrieval_token_budget: int = Field(default=2400, ge=1, le=131_072)
    retrieval_rrf_weights: dict[str, float] = Field(
        default_factory=lambda: {
            "lexical": 1.0,
            "dense": 1.0,
            "graph": 0.7,
            "temporal": 0.85,
            "procedure": 0.8,
            "outcome": 0.9,
        }
    )
    retrieval_graph_max_depth: int = Field(default=2, ge=1, le=8)
    retrieval_graph_max_branching: int = Field(default=50, ge=1, le=100)
    retrieval_rerank_enabled: bool = False
    retrieval_rerank_candidate_limit: int = Field(default=25, ge=1, le=1000)

    # File Watcher (Phase 1: Proactive Layer)
    watcher_enabled: bool = False  # Enable file watcher daemon
    watcher_debounce_seconds: float = 1.0  # Debounce interval for same file
    watcher_system_notifications: bool = True  # Enable desktop notifications
    watcher_log_file: bool = True  # Enable log file channel
    watcher_editor_poll: bool = True  # Enable editor poll channel
    watcher_skip_patterns: list[
        str
    ] = []  # Additional patterns to skip (added to defaults)
    watcher_watch_extensions: list[str] = []  # File extensions to watch (empty = all)

    # Search tuning
    hybrid_vector_weight: float = Field(
        default=0.3, ge=0.0, le=1.0
    )  # 0.0 = TF-IDF only, 1.0 = vectors only
    search_diversity_max_per_file: int = Field(
        default=3, ge=0
    )  # Max results from same source file (0=unlimited)

    # Embedding Model
    embedding_model: str = "nomic-ai/modernbert-embed-base"
    embedding_dimension: int = 256
    embedding_backend: str = _DEFAULT_EMBEDDING_BACKEND
    embedding_query_prefix: str = "search_query: "
    embedding_document_prefix: str = "search_document: "

    # BM25 tuning parameters
    bm25_k1: float = Field(default=1.5, ge=0.0, le=3.0)  # Term frequency saturation
    bm25_b: float = Field(default=0.75, ge=0.0, le=1.0)  # Document length normalization

    # RRF fusion parameter
    rrf_k: int = Field(default=60, ge=1, le=1_000_000)  # Dampening constant

    @field_validator("retrieval_rrf_weights", mode="before")
    @classmethod
    def validate_retrieval_rrf_weights(cls, value: object) -> dict[str, float]:
        if not isinstance(value, Mapping) or set(value) != _RETRIEVAL_PROVIDERS:
            raise ValueError("retrieval RRF weights must name every provider")
        validated: dict[str, float] = {}
        for provider in sorted(_RETRIEVAL_PROVIDERS):
            weight = value[provider]
            if isinstance(weight, bool) or not isinstance(weight, (int, float)):
                raise ValueError("retrieval RRF weights must be finite and positive")
            try:
                converted = float(weight)
            except (OverflowError, ValueError) as exc:
                raise ValueError(
                    "retrieval RRF weights must be finite and positive"
                ) from exc
            if not math.isfinite(converted) or converted <= 0:
                raise ValueError("retrieval RRF weights must be finite and positive")
            validated[provider] = converted
        return validated

    @field_validator("qdrant_collection_prefix")
    @classmethod
    def validate_qdrant_collection_prefix(cls, value: str) -> str:
        if _QDRANT_COLLECTION_PREFIX.fullmatch(value) is None:
            raise ValueError("qdrant collection prefix is invalid")
        return value

    # Surprise scoring
    surprise_k_nearest: int = Field(default=5, ge=1)  # Neighbors for surprise calc
    surprise_boost_threshold: float = Field(
        default=0.7, ge=0.0, le=1.0
    )  # Boost if above

    # Recall planner limits
    recall_simple_max_memories: int = Field(default=5, ge=1)
    recall_medium_max_memories: int = Field(default=10, ge=1)
    recall_complex_max_memories: int = Field(default=20, ge=1)

    # Fact promotion
    fact_promotion_threshold: int = Field(
        default=3, ge=1
    )  # Successful outcomes to promote

    # Auto-Zoom retrieval routing
    auto_zoom_enabled: bool = False  # Master switch (shadow mode when False)
    auto_zoom_shadow: bool = True  # Log classifications without routing
    auto_zoom_confidence_threshold: float = 0.25  # Below this -> hybrid fallback
    auto_zoom_graph_expansion_depth: int = 2  # Multi-hop depth for complex queries

    # Background Dreaming
    dream_enabled: bool = True  # Master switch for dreaming
    dream_idle_timeout: float = Field(default=60.0, ge=0.05, le=86_400.0)
    dream_max_concurrency: int = Field(default=2, ge=1, le=8)
    dream_max_decisions_per_session: int = Field(
        default=5, ge=1, le=100
    )  # Max failed decisions to re-evaluate per session
    dream_yield_check_interval: float = Field(default=0.0, ge=0.0, le=60.0)
    dream_min_decision_age_hours: int = Field(default=1, ge=0, le=87_600)
    dream_review_cooldown_hours: int = Field(default=72, ge=0, le=87_600)

    # ConnectionDiscovery strategy
    dream_connection_lookback_hours: int = Field(default=168, ge=1, le=87_600)
    dream_connection_max_per_session: int = Field(default=20, ge=1, le=100)
    dream_connection_min_shared_entities: int = Field(default=2, ge=1, le=32)
    dream_connection_confidence: float = Field(default=0.7, ge=0.0, le=1.0)

    # PendingOutcomeResolver strategy
    dream_pending_max_per_session: int = Field(default=3, ge=1, le=100)
    dream_pending_min_age_hours: int = Field(default=24, ge=0, le=87_600)
    dream_pending_cooldown_hours: int = Field(default=168, ge=0, le=87_600)
    dream_pending_evidence_threshold: int = Field(default=3, ge=1, le=10)
    dream_pending_dry_run: bool = True  # Ships inert -- must opt-in to auto-resolve

    # CommunityRefresh strategy
    dream_community_staleness_threshold: int = Field(default=10, ge=1, le=10_000)

    # Cognitive Tools
    cognitive_debate_max_rounds: int = 5
    cognitive_debate_convergence_threshold: float = 0.05
    cognitive_debate_min_evidence: int = 2
    cognitive_evolve_max_rules: int = 10
    cognitive_staleness_age_weight: float = 0.3

    # Code Indexing
    parse_tree_cache_maxsize: int = 200
    index_languages: list[str] = []  # Empty = all supported

    def _migrate_legacy_storage(self, project_path: Path, new_storage: Path) -> bool:
        """
        Migrate data from legacy .devilmcp directory to .daem0nmcp.

        Returns True if migration occurred.
        """
        import logging

        logger = logging.getLogger(__name__)

        legacy_storage = resolve_derived_path(project_path, ".devilmcp", "storage")
        legacy_db = legacy_storage / "devilmcp.db"
        new_db = new_storage / "daem0nmcp.db"

        # Check if legacy storage exists and new doesn't have data yet
        if not legacy_storage.exists():
            return False

        if new_db.exists():
            logger.info("New database already exists, skipping legacy migration")
            return False

        # Create new storage directory
        new_storage.mkdir(parents=True, exist_ok=True)

        # Migrate database file
        if legacy_db.exists():
            shutil.copy2(legacy_db, new_db)
            logger.info(f"Migrated database: {legacy_db} -> {new_db}")

        # Also check for any other .db files (e.g., daem0nmcp.db in old location)
        for db_file in legacy_storage.glob("*.db"):
            if db_file.name != "devilmcp.db":
                dest = new_storage / db_file.name
                if not dest.exists():
                    shutil.copy2(db_file, dest)
                    logger.info(f"Migrated database: {db_file} -> {dest}")

        logger.info(
            f"Legacy migration complete. You can safely delete: {project_path / '.devilmcp'}"
        )
        return True

    def get_storage_path(self) -> str:
        """
        Determine storage path with project isolation.

        Priority:
        1. storage_path setting (explicit override via DAEM0NMCP_STORAGE_PATH)
        2. project_root/.daem0nmcp/storage (if project_root is set)
        3. <cwd>/.daem0nmcp/storage (current working directory)
        4. ./storage (fallback for centralized storage)

        Also handles automatic migration from legacy .devilmcp storage.
        """
        import logging

        logger = logging.getLogger(__name__)

        # Check for explicit storage path override
        if self.storage_path:
            Path(self.storage_path).mkdir(parents=True, exist_ok=True)
            return self.storage_path

        # Get project root
        project_path = Path(self.project_root).resolve()
        server_path = Path(__file__).parent.resolve()

        # If we're running from the Daem0nMCP server directory itself, use centralized storage
        if project_path == server_path:
            storage = resolve_derived_path(server_path, "storage", "centralized")
            logger.info("Using centralized storage (running from Daem0nMCP directory)")
        else:
            # Use project-specific storage
            storage = resolve_derived_path(project_path, ".daem0nmcp", "storage")

            # Check for and migrate legacy .devilmcp storage
            self._migrate_legacy_storage(project_path, storage)

            logger.info(f"Project detected: {project_path.name}")
            logger.info(f"Using project-specific storage: {storage}")

        # Create directory if it doesn't exist
        storage.mkdir(parents=True, exist_ok=True)

        return str(storage)

    def get_qdrant_path(self) -> str | None:
        """
        Determine Qdrant storage path for local mode.

        Returns None if qdrant_url is set (remote mode).

        Priority for local mode:
        1. qdrant_path setting (explicit override via DAEM0NMCP_QDRANT_PATH)
        2. <storage_path>/qdrant (next to the SQLite database)
        """
        # Remote mode - no local path needed
        if self.qdrant_url:
            return None

        if self.qdrant_path:
            Path(self.qdrant_path).mkdir(parents=True, exist_ok=True)
            return self.qdrant_path

        # Use subdirectory of main storage
        storage = Path(self.get_storage_path())
        qdrant_dir = storage / "qdrant"
        qdrant_dir.mkdir(parents=True, exist_ok=True)
        return str(qdrant_dir)

    def get_watcher_log_path(self) -> Path:
        """
        Get the path for the watcher log file.

        Returns:
            Path to watcher.log in the storage directory
        """
        storage = Path(self.get_storage_path())
        return storage / "watcher.log"

    def get_watcher_poll_path(self) -> Path:
        """
        Get the path for the editor poll file.

        Returns:
            Path to editor-poll.json in the storage directory
        """
        storage = Path(self.get_storage_path())
        return storage / "editor-poll.json"


# Singleton instance
settings = Settings()
