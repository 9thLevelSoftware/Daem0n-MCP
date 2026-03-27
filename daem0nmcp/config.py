"""
Centralized configuration using Pydantic Settings.

All settings are loaded from environment variables with DAEM0NMCP_ prefix.
Example: DAEM0NMCP_LOG_LEVEL=DEBUG
"""

import shutil
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

_DEFAULT_EMBEDDING_BACKEND = "onnx"


class Settings(BaseSettings):
    """Daem0nMCP configuration settings."""

    model_config = SettingsConfigDict(
        env_prefix="DAEM0NMCP_", env_file=".env", env_file_encoding="utf-8"
    )

    # Core paths
    project_root: str = "."
    storage_path: str | None = None  # Auto-detect if not set

    # Server
    log_level: str = "INFO"

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
    qdrant_url: str | None = (
        None  # Optional remote Qdrant URL (overrides local path)
    )
    qdrant_api_key: str | None = None  # API key for remote Qdrant (if using cloud)

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
    rrf_k: int = Field(default=60, ge=1)  # Dampening constant for RRF

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
    dream_idle_timeout: float = 60.0  # Seconds of idle before dreaming starts
    dream_max_decisions_per_session: int = (
        5  # Max failed decisions to re-evaluate per session
    )
    dream_yield_check_interval: float = (
        0.0  # Seconds between yield checks (0 = every step)
    )
    dream_min_decision_age_hours: int = (
        1  # Min age of decision before re-evaluation eligible
    )
    dream_review_cooldown_hours: int = 72  # Skip decisions reviewed within this window

    # ConnectionDiscovery strategy
    dream_connection_lookback_hours: int = 168  # 7-day lookback for entity sharing
    dream_connection_max_per_session: int = 20  # Max connections per dream session
    dream_connection_min_shared_entities: int = 2  # Min shared entities to create link
    dream_connection_confidence: float = 0.7  # Confidence for inferred relationships

    # PendingOutcomeResolver strategy
    dream_pending_max_per_session: int = (
        3  # Max pending decisions to resolve per session
    )
    dream_pending_min_age_hours: int = (
        24  # Min age before a pending decision is eligible
    )
    dream_pending_cooldown_hours: int = 168  # 7 days cooldown between re-evaluations
    dream_pending_evidence_threshold: int = (
        3  # Min directional evidence for auto-resolve
    )
    dream_pending_dry_run: bool = True  # Ships inert -- must opt-in to auto-resolve

    # CommunityRefresh strategy
    dream_community_staleness_threshold: int = (
        10  # New memories before community rebuild
    )

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

        legacy_storage = project_path / ".devilmcp" / "storage"
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
            storage = server_path / "storage" / "centralized"
            logger.info("Using centralized storage (running from Daem0nMCP directory)")
        else:
            # Use project-specific storage
            storage = project_path / ".daem0nmcp" / "storage"

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
