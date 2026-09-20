"""
Database Manager - Simplified for the focused memory system.
"""

import asyncio
import logging
import sqlite3
import time
from collections.abc import Callable
from contextlib import asynccontextmanager, closing
from datetime import datetime
from pathlib import Path
from typing import Literal

from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from .models import (  # noqa: F401 - MemoryVersion imported for table creation
    Base,
    MemoryVersion,
)
from .schema_version import CURRENT_SCHEMA_VERSION
from .storage_activation import (
    LOCK_NAME,
    ActiveDatabasePointer,
    DatabaseFileLock,
    resolve_active_database,
    write_active_pointer,
)
from .workspace import WorkspaceRegistry

logger = logging.getLogger(__name__)


class FreshDatabaseRequiredError(RuntimeError):
    """The locked storage generation is not an empty bootstrap target."""

    code = "FRESH_DATABASE_REQUIRED"

    def __init__(self) -> None:
        super().__init__(self.code)


class DatabaseManager:
    """
    Manages the SQLite database connection.

    Simplified from the original - no more tool initialization or complex migrations.
    Just creates tables and provides session management.
    Auto-migrates existing databases on startup.
    """

    def __init__(
        self,
        storage_path: str = "./storage",
        db_name: str = "daem0nmcp.db",
        *,
        lock_mode: Literal["shared", "exclusive"] = "shared",
        require_fresh: bool = False,
        fresh_workspace_root: str | Path | None = None,
        defer_fresh_activation: bool = False,
    ):
        self.storage_path = Path(storage_path)
        self.storage_path.mkdir(parents=True, exist_ok=True)
        if fresh_workspace_root is not None:
            if not require_fresh:
                raise ValueError("fresh_workspace_root requires fresh storage")
            workspace_root = Path(fresh_workspace_root)
        elif (
            self.storage_path.name == "storage"
            and self.storage_path.parent.name == ".daem0nmcp"
        ):
            workspace_root = self.storage_path.parent.parent
        else:
            workspace_root = self.storage_path.parent
        self.workspace_id = WorkspaceRegistry(
            [workspace_root], default_root=workspace_root
        ).default.workspace_id
        if db_name != "daem0nmcp.db":
            raise ValueError(
                "custom database names are incompatible with active-db selection"
            )
        if defer_fresh_activation and not require_fresh:
            raise ValueError("deferred activation requires fresh storage")
        self._defer_fresh_activation = defer_fresh_activation
        self._database_lock = DatabaseFileLock(self.storage_path, lock_mode).acquire()
        try:
            entries = iter(self.storage_path.iterdir())
            first = next(entries, None)
            self._fresh_at_construction = (
                first is not None
                and first.name == LOCK_NAME
                and not first.is_symlink()
                and first.is_file()
                and next(entries, None) is None
                and not (self.storage_path / db_name).exists()
                and not (self.storage_path / "active-db.json").exists()
            )
            if require_fresh and not self._fresh_at_construction:
                raise FreshDatabaseRequiredError()
            if self._fresh_at_construction:
                self._active_database = None
                self.db_path = self.storage_path / db_name
                self.format_version = 7
                self.active_generation = 0
                self.migration_run_id = None
            else:
                self._active_database = resolve_active_database(self.storage_path)
                self.db_path = self._active_database.path
                self.format_version = self._active_database.format_version
                self.active_generation = self._active_database.generation
                self.migration_run_id = self._active_database.migration_run_id
        except Exception:
            self._database_lock.release()
            raise
        self.db_url = f"sqlite+aiosqlite:///{self.db_path}"
        self._migrated = False
        self._initialized = False
        self._engine = None
        self._session_factory = None
        self._close_callbacks: list[Callable[[], None]] = []
        self._foreground_projection_tasks: set[asyncio.Task] = set()

    @property
    def fresh_at_construction(self) -> bool:
        """Whether locked storage contained exactly its owned lock file."""

        return self._fresh_at_construction

    def register_close_callback(self, callback: Callable[[], None]) -> None:
        """Register one idempotent resource cleanup owned by this database."""

        if not callable(callback):
            raise TypeError("close callback must be callable")
        if callback not in self._close_callbacks:
            self._close_callbacks.append(callback)

    def _track_projection_task(self, task: asyncio.Task) -> None:
        self._foreground_projection_tasks.add(task)
        task.add_done_callback(self._foreground_projection_tasks.discard)

    def _get_engine(self):
        """Lazy engine creation - ensures it's created in the right event loop context."""
        if self._engine is None:
            self._engine = create_async_engine(
                self.db_url,
                connect_args={"check_same_thread": False},
                # Use NullPool for SQLite to avoid connection issues across async contexts
                # Each operation gets a fresh connection
                poolclass=NullPool,
                pool_pre_ping=True,
            )

            # Configure SQLite PRAGMAs for performance and reliability
            @event.listens_for(self._engine.sync_engine, "connect")
            def set_sqlite_pragmas(dbapi_conn, connection_record):
                cursor = dbapi_conn.cursor()
                # WAL mode for better concurrent access
                cursor.execute("PRAGMA journal_mode=WAL")
                # Faster syncs (still safe with WAL)
                cursor.execute("PRAGMA synchronous=NORMAL")
                # 30 second busy timeout
                cursor.execute("PRAGMA busy_timeout=30000")
                # Enable foreign keys
                cursor.execute("PRAGMA foreign_keys=ON")
                # Use memory for temp tables
                cursor.execute("PRAGMA temp_store=MEMORY")
                # Larger cache (64MB)
                cursor.execute("PRAGMA cache_size=-64000")
                cursor.close()

            self._session_factory = async_sessionmaker(
                bind=self._engine, expire_on_commit=False, class_=AsyncSession
            )
        return self._engine

    @property
    def engine(self):
        """Property for backward compatibility."""
        return self._get_engine()

    @property
    def SessionLocal(self):  # noqa: N802 -- retained public Python API name
        """Property for backward compatibility."""
        self._get_engine()  # Ensure engine is created
        return self._session_factory

    def _run_migrations(
        self,
        force: bool = False,
        *,
        maximum_version: int | None = None,
    ):
        """Run schema migrations (sync, before async engine starts)."""
        if self._migrated and not force:
            return

        if self.db_path.exists():
            from .migrations import run_migrations

            count, applied = run_migrations(
                str(self.db_path),
                workspace_id=self.workspace_id,
                maximum_version=maximum_version,
            )
            if count > 0:
                logger.info(f"Applied {count} migration(s): {applied}")

        self._migrated = True

    def _v6_migration_ceiling(self) -> int | None:
        """The migration ceiling for a retained format-6 store, else ``None``."""

        if self.format_version != 6:
            return None
        from .migrations.schema import _LAST_V6_SCHEMA_VERSION

        return _LAST_V6_SCHEMA_VERSION

    async def init_db(self):
        """Initialize the database tables and run migrations."""
        # Skip if already initialized
        if self._initialized:
            return

        # Check if this is a fresh database.  This fact is captured before any
        # engine construction so a failed pointer publication cannot be mistaken
        # for an ordinary v6 database within this manager lifetime.
        is_new_db = self._fresh_at_construction and not self.db_path.exists()
        previous_schema_version = self._schema_version() if self.db_path.exists() else 0

        # Run migrations first for existing databases (sync operation)
        # This happens BEFORE we create the async engine to avoid lock conflicts
        if not is_new_db:
            # A format-6 store is what `migrate-v7` snapshots and can roll back
            # to.  Applying the v7 ledger to it in place would rewrite the
            # source without a backup, and its governance backfill then goes
            # stale against later v6 edits, so cap it at the last v6 version.
            self._run_migrations(maximum_version=self._v6_migration_ceiling())

        # Then create any new tables
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        # For fresh databases, run migrations after tables are created
        if is_new_db:
            self._run_migrations(force=True)

        if is_new_db:
            self._bootstrap_lexical_projection()
            self._validate_database(format_version=7)
            self.format_version = 7
            self.active_generation = 1
            if not self._defer_fresh_activation:
                pointer = ActiveDatabasePointer(7, 1, "daem0nmcp.db", None, None)
                write_active_pointer(self.storage_path, pointer)
                self._active_database = resolve_active_database(self.storage_path)
        elif self._active_database is None:
            raise RuntimeError("ACTIVE_DATABASE_STATE_MISSING")
        elif self._active_database.pointer is not None:
            if self.format_version == 7:
                self._bootstrap_lexical_projection()
            self._validate_database(format_version=self.format_version)
        elif previous_schema_version >= 16 and not self._has_user_rows():
            # Recovery for a crash between brand-new DB creation and pointer
            # publication is safe only before any user data exists. A populated
            # pointerless database remains architecture format 6 even after the
            # additive SQL migration 16 has been applied.
            self._bootstrap_lexical_projection()
            self._validate_database(format_version=7)
            pointer = ActiveDatabasePointer(7, 1, "daem0nmcp.db", None, None)
            write_active_pointer(self.storage_path, pointer)
            self._active_database = resolve_active_database(self.storage_path)
            self.format_version = 7
            self.active_generation = 1
        else:
            self._validate_database(format_version=6)

        self._initialized = True
        if self.format_version == 7 and not self._defer_fresh_activation:
            from .retrieval.runtime import schedule_projection_job_drain

            schedule_projection_job_drain(self.db_path, max_jobs=5)
        logger.info("Database initialized")

    async def init_legacy_v6(self) -> None:
        """Initialize a retained format-6 database without publishing v7 schema.

        Deprecated vector-copy commands only need an engine and session over the
        existing v6 tables.  Running ``init_db`` here would create current model
        tables and apply v7 migrations before the copy begins.
        """

        if self.format_version != 6 or self._fresh_at_construction:
            raise RuntimeError("LEGACY_V6_DATABASE_REQUIRED")
        if self._initialized:
            return

        from .migrations.schema import _LAST_V6_SCHEMA_VERSION

        self._run_migrations(maximum_version=_LAST_V6_SCHEMA_VERSION)
        connection = sqlite3.connect(self.db_path)
        try:
            connection.execute("PRAGMA foreign_keys=ON")
            integrity = [row[0] for row in connection.execute("PRAGMA integrity_check")]
            foreign = list(connection.execute("PRAGMA foreign_key_check"))
        finally:
            connection.close()
        if integrity != ["ok"] or foreign:
            raise RuntimeError("DATABASE_INTEGRITY_FAILED")

        self._initialized = True
        logger.info("Legacy format-6 database initialized")

    def _bootstrap_lexical_projection(self) -> None:
        """Make the dependency-free lexical baseline valid before v7 use."""

        from .event_store import deterministic_id
        from .retrieval.job_queue import enqueue_projection_rebuild
        from .retrieval.projections import LexicalProjectionBuilder

        connection = sqlite3.connect(self.db_path)
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("PRAGMA foreign_keys=ON")
            builder = LexicalProjectionBuilder(connection)
            if not builder.active_is_current(self.workspace_id):
                builder.rebuild(self.workspace_id)
            if not builder.active_is_current(self.workspace_id):
                raise RuntimeError("LEXICAL_BOOTSTRAP_FAILED")
            lexical = connection.execute(
                "SELECT source_event_count,source_event_root_hash,"
                "cursor_recorded_at_us,cursor_event_id "
                "FROM projection_manifests WHERE workspace_id=? "
                "AND projection_name='lexical' AND status='active'",
                (self.workspace_id,),
            ).fetchone()
            if lexical is None:
                raise RuntimeError("LEXICAL_BOOTSTRAP_FAILED")
            now = time.time_ns() // 1_000
            for projection_name in (
                "dense",
                "graph",
                "temporal",
                "procedure",
                "outcome",
            ):
                manifest_id = deterministic_id(
                    "prj",
                    "projection",
                    self.workspace_id,
                    projection_name,
                    1,
                    str(lexical[1]),
                )
                connection.execute(
                    "INSERT INTO projection_manifests ("
                    "manifest_id,workspace_id,projection_name,generation,"
                    "projection_version,status,source_event_count,"
                    "source_event_root_hash,cursor_recorded_at_us,"
                    "cursor_event_id,row_count,builder_version,details_json,"
                    "started_at_us) VALUES (?,?,?,1,1,'rebuild_required',"
                    "?,?,?,?,0,'v7-bootstrap-1','{}',?) "
                    "ON CONFLICT(workspace_id,projection_name,generation) "
                    "DO NOTHING",
                    (
                        manifest_id,
                        self.workspace_id,
                        projection_name,
                        int(lexical[0]),
                        str(lexical[1]),
                        lexical[2],
                        lexical[3],
                        now,
                    ),
                )
                enqueue_projection_rebuild(
                    connection,
                    workspace_id=self.workspace_id,
                    projection_name=projection_name,
                    source_event_id=(
                        str(lexical[3]) if lexical[3] is not None else None
                    ),
                    recorded_at_us=now,
                    requeue_existing=False,
                )
            connection.commit()
        except Exception as exc:
            connection.rollback()
            raise RuntimeError("LEXICAL_BOOTSTRAP_FAILED") from exc
        finally:
            connection.close()

    def _schema_version(self) -> int:
        try:
            with closing(sqlite3.connect(self.db_path)) as connection:
                exists = connection.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_version'"
                ).fetchone()
                if exists is None:
                    return 0
                return int(
                    connection.execute(
                        "SELECT COALESCE(MAX(version),0) FROM schema_version"
                    ).fetchone()[0]
                )
        except sqlite3.Error:
            return 0

    def _has_user_rows(self) -> bool:
        with closing(sqlite3.connect(self.db_path)) as connection:
            for table in (
                "memories",
                "facts",
                "memory_relationships",
                "rules",
                "context_triggers",
                "active_context",
            ):
                exists = connection.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                    (table,),
                ).fetchone()
                if (
                    exists
                    and connection.execute(
                        f'SELECT 1 FROM "{table}" LIMIT 1'
                    ).fetchone()
                ):
                    return True
        return False

    def _validate_database(self, *, format_version: int) -> None:
        with closing(sqlite3.connect(self.db_path)) as connection:
            connection.execute("PRAGMA foreign_keys=ON")
            integrity = [row[0] for row in connection.execute("PRAGMA integrity_check")]
            foreign = list(connection.execute("PRAGMA foreign_key_check"))
            if integrity != ["ok"] or foreign:
                raise RuntimeError("DATABASE_INTEGRITY_FAILED")
            from .migrations.schema import _LAST_V6_SCHEMA_VERSION

            # A retained format-6 store stops at the last v6 version on
            # purpose: `migrate-v7` owns everything above it.
            if format_version == 7:
                if self._schema_version() < CURRENT_SCHEMA_VERSION:
                    raise RuntimeError("SCHEMA_MIGRATION_INCOMPLETE")
            elif self._schema_version() < _LAST_V6_SCHEMA_VERSION:
                raise RuntimeError("SCHEMA_MIGRATION_INCOMPLETE")
            if format_version == 7:
                required = {
                    "memory_events",
                    "memory_records",
                    "memory_fact_versions",
                    "memory_relationship_versions",
                    "projection_manifests",
                    "enrichment_decisions",
                    "background_jobs",
                    "v7_migration_runs",
                    "v7_migration_checkpoints",
                    "legacy_id_map",
                    "retrieval_documents",
                    "record_procedures",
                    "record_outcome_view",
                    "dense_projection_refs",
                    "public_object_ids",
                    "active_context_entries",
                    "governance_events",
                    "governance_rules",
                    "governance_context_triggers",
                    "session_update_sequence",
                    "discovery_projection_partitions",
                    "discovery_entities",
                    "discovery_entity_records",
                    "discovery_communities",
                    "discovery_community_members",
                    "discovery_code_entities",
                    "discovery_code_edges",
                }
                present = {
                    row[0]
                    for row in connection.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    )
                }
                if not required <= present:
                    raise RuntimeError("V7_SCHEMA_INCOMPLETE")
                if self.migration_run_id is not None:
                    run = connection.execute(
                        "SELECT status FROM v7_migration_runs WHERE migration_run_id=?",
                        (self.migration_run_id,),
                    ).fetchone()
                    if run is None or run[0] != "active":
                        raise RuntimeError("V7_ACTIVATION_INCOMPLETE")

    @asynccontextmanager
    async def get_session(self):
        """Provide a transactional scope around a series of operations."""
        session = self.SessionLocal()
        session.info["daem0nmcp_format_version"] = self.format_version
        session.info["daem0nmcp_workspace_id"] = self.workspace_id
        try:
            yield session
            await session.commit()
            if self.format_version == 7 and session.info.pop(
                "daem0nmcp_v7_event_appended", False
            ):
                try:
                    from .retrieval.runtime import drain_projection_jobs

                    projection_task = asyncio.create_task(
                        drain_projection_jobs(
                            self.db_path,
                            max_jobs=1,
                            include_optional=False,
                        )
                    )
                    self._track_projection_task(projection_task)
                    await asyncio.wait_for(
                        asyncio.shield(projection_task),
                        timeout=2.0,
                    )
                except Exception:
                    # The canonical event is already committed and the durable
                    # coalesced job remains queued.  Projection refresh failure
                    # must never turn a successful write into an apparent
                    # rollback or duplicate retry.
                    logger.warning(
                        "V7 lexical projection refresh remains queued",
                        exc_info=True,
                    )
                from .retrieval.runtime import schedule_projection_job_drain

                schedule_projection_job_drain(self.db_path, max_jobs=5)
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()

    async def get_last_update_time(self) -> datetime | None:
        """Get the most recent updated_at from memories and rules."""
        from datetime import timezone as tz

        async with self.get_session() as session:
            from sqlalchemy import func, select, text

            from .models import Memory, Rule

            def _parse_meta_time(value: str | None) -> datetime | None:
                if not value:
                    return None
                try:
                    parsed = datetime.fromisoformat(value)
                except ValueError:
                    return None
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=tz.utc)
                return parsed

            meta_times = []
            try:
                meta_exists = await session.execute(
                    text(
                        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='meta'"
                    )
                )
                if meta_exists.scalar():
                    mem_meta = await session.execute(
                        text(
                            "SELECT value FROM meta WHERE key='memories_last_modified'"
                        )
                    )
                    rule_meta = await session.execute(
                        text("SELECT value FROM meta WHERE key='rules_last_modified'")
                    )
                    meta_times.extend(
                        [
                            _parse_meta_time(mem_meta.scalar()),
                            _parse_meta_time(rule_meta.scalar()),
                        ]
                    )
            except Exception:
                pass

            # Get max updated_at from memories
            mem_result = await session.execute(select(func.max(Memory.updated_at)))
            mem_time = mem_result.scalar()

            # Get max created_at from rules (rules don't have updated_at)
            rule_result = await session.execute(select(func.max(Rule.created_at)))
            rule_time = rule_result.scalar()

            # Return the most recent, ensuring timezone awareness
            times = []
            for t in meta_times + [mem_time, rule_time]:
                if t is not None:
                    # SQLite returns naive datetimes, make them UTC-aware
                    if t.tzinfo is None:
                        t = t.replace(tzinfo=tz.utc)
                    times.append(t)

            return max(times) if times else None

    async def has_changes_since(self, since: datetime | None) -> bool:
        """Check if database has changes since the given timestamp."""
        if since is None:
            return True

        current = await self.get_last_update_time()
        if current is None:
            return False

        return current > since

    async def close(self):
        """Quiesce owned work and release every database-scoped resource."""

        first_error: BaseException | None = None
        callbacks = tuple(reversed(self._close_callbacks))
        self._close_callbacks.clear()
        for callback in callbacks:
            try:
                callback()
            except BaseException as exc:
                if first_error is None:
                    first_error = exc
        foreground_tasks = tuple(self._foreground_projection_tasks)
        if foreground_tasks:
            await asyncio.gather(
                *(asyncio.shield(task) for task in foreground_tasks),
                return_exceptions=True,
            )
        if self.format_version == 7 and not self._defer_fresh_activation:
            try:
                from .retrieval.runtime import await_projection_job_drains

                await await_projection_job_drains((self.db_path,))
            except BaseException as exc:
                if first_error is None:
                    first_error = exc
        if self._engine is not None:
            try:
                await self._engine.dispose()
            except BaseException as exc:
                if first_error is None:
                    first_error = exc
            finally:
                self._engine = None
                self._session_factory = None
                self._initialized = False
        self._database_lock.release()
        if first_error is not None:
            raise first_error

    def __del__(self):
        lock = getattr(self, "_database_lock", None)
        if lock is not None:
            lock.release()
