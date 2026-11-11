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
from typing import Any, Literal

from sqlalchemy import event
from sqlalchemy.engine import URL
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from .models import (  # noqa: F401 - MemoryVersion imported for table creation
    Base,
    MemoryVersion,
)
from .schema_version import CURRENT_SCHEMA_VERSION
from .storage_activation import (
    ACTIVE_POINTER_MISSING_CODE,
    ACTIVE_POINTER_MISSING_DETAIL,
    LOCK_NAME,
    ActiveDatabasePointer,
    CandidateUnreadableError,
    DatabaseFileLock,
    DatabaseInUseError,
    active_candidate_pointer_missing_detail,
    active_candidate_without_pointer,
    has_canonical_v7_state,
    resolve_active_database,
    write_active_pointer,
)
from .workspace import WorkspaceRegistry

logger = logging.getLogger(__name__)

# How long a start waits for the storage lock under which a store is created,
# and how often it looks again: the patience of production workspace bootstrap.
_CREATION_LOCK_TIMEOUT_SECONDS = 30.0
_CREATION_LOCK_RETRY_SECONDS = 0.05


class FreshDatabaseRequiredError(RuntimeError):
    """The locked storage generation is not an empty bootstrap target."""

    code = "FRESH_DATABASE_REQUIRED"

    def __init__(self) -> None:
        super().__init__(self.code)


class SchemaUpgradeRequiredError(RuntimeError):
    """An active format-7 store is on an older schema than this build serves."""

    code = "MIGRATION_REQUIRED"

    def __init__(self, schema_version: int) -> None:
        super().__init__(
            f"{self.code}: this workspace's v7 database is at schema "
            f"{schema_version} and this build needs {CURRENT_SCHEMA_VERSION}. "
            "Stop the server, then run: python -m daem0nmcp.cli "
            "--project-path <project root> migrate-v7 --apply"
        )


class ActivePointerMissingError(RuntimeError):
    """A database holding format-7 state has lost its activation pointer."""

    code = ACTIVE_POINTER_MISSING_CODE

    def __init__(self, detail: str = ACTIVE_POINTER_MISSING_DETAIL) -> None:
        super().__init__(f"{self.code}: {detail}")


def _is_concurrent_ddl_refusal(error: OperationalError) -> bool:
    """Whether SQLite refused a statement because another start changed the schema."""

    message = str(error.orig)
    return "already exists" in message or "database schema has changed" in message


_WAL_SWITCH_ATTEMPTS = 100
_WAL_SWITCH_PAUSE_SECONDS = 0.05


def _enter_wal_mode(cursor: Any) -> None:
    """Switch the database to WAL, waiting out another connection doing the same.

    Changing the journal mode of a database that is not yet in WAL needs every
    other connection out of the file, and SQLite refuses at once, without
    consulting the busy timeout, when two connections each wait on the other.
    The switch is retried; a database already in WAL answers without a lock.
    """

    for attempt in range(_WAL_SWITCH_ATTEMPTS):
        try:
            cursor.execute("PRAGMA journal_mode=WAL")
        except sqlite3.OperationalError as exc:
            if "locked" not in str(exc) or attempt == _WAL_SWITCH_ATTEMPTS - 1:
                raise
            time.sleep(_WAL_SWITCH_PAUSE_SECONDS)
        else:
            return


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
            [workspace_root], default_root=workspace_root, live_storage=True
        ).default.workspace_id
        if db_name != "daem0nmcp.db":
            raise ValueError(
                "custom database names are incompatible with active-db selection"
            )
        if defer_fresh_activation and not require_fresh:
            raise ValueError("deferred activation requires fresh storage")
        self._defer_fresh_activation = defer_fresh_activation
        self._lock_mode: Literal["shared", "exclusive"] = lock_mode
        self._require_fresh = require_fresh
        self._storage_lock_lost = False
        self._database_lock = DatabaseFileLock(self.storage_path, lock_mode).acquire()
        try:
            self._resolve_storage()
        except Exception:
            self._database_lock.release()
            raise
        self._migrated = False
        self._initialized = False
        self._init_lock = asyncio.Lock()
        self._engine = None
        self._session_factory = None
        self._close_callbacks: list[Callable[[], None]] = []
        self._foreground_projection_tasks: set[asyncio.Task] = set()

    def _resolve_storage(self) -> None:
        """Read which database the locked storage selects, without writing.

        Runs at construction, and again once ``init_db`` holds the lock under
        which a store is created, because another process may have created it
        in between.
        """

        db_name = "daem0nmcp.db"
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
        if self._require_fresh and not self._fresh_at_construction:
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
            # A store without a pointer reads as format 6, where every
            # write bypasses the event log.  Format-7 state in it means
            # the pointer was lost, unless the database is a brand-new one
            # whose creation stopped before publishing it.
            if self._active_database.pointer is None:
                if has_canonical_v7_state(self.db_path):
                    if not self._is_unpublished_fresh_database():
                        raise ActivePointerMissingError()
                else:
                    # A store migrated from format 6 runs on a candidate
                    # only the pointer names; this file is its retained
                    # source and must not take the writes instead.
                    try:
                        candidate = active_candidate_without_pointer(self.storage_path)
                    except CandidateUnreadableError as exc:
                        raise ActivePointerMissingError(str(exc)) from exc
                    if candidate is not None:
                        raise ActivePointerMissingError(
                            active_candidate_pointer_missing_detail(candidate)
                        )
        # The path is handed over as a path: inside URL text a "?" in it would
        # start the query and move the database out of the storage directory.
        self.db_url = URL.create("sqlite+aiosqlite", database=str(self.db_path))

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
                # 30 second busy timeout, before anything that takes a lock
                cursor.execute("PRAGMA busy_timeout=30000")
                # WAL mode for better concurrent access
                _enter_wal_mode(cursor)
                # Faster syncs (still safe with WAL)
                cursor.execute("PRAGMA synchronous=NORMAL")
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

    def _v6_migration_ceiling(self, previous_schema_version: int) -> int | None:
        """The migration ceiling for a retained format-6 store, else ``None``.

        A brand-new database whose creation crashed part-way through its own
        migration run is pointerless and therefore reads as format 6, but it
        holds no user rows and is finished by the recovery branch in
        ``init_db``; capping it would leave it permanently incomplete.
        """

        from .migrations.schema import _LAST_V6_SCHEMA_VERSION

        if self.format_version != 6:
            return None
        if previous_schema_version >= 16 and not self._has_user_rows():
            return None
        return _LAST_V6_SCHEMA_VERSION

    def _start_creates_store(self) -> bool:
        """Whether this start creates the database or publishes its pointer.

        True for empty storage and for the pointerless, rowless database an
        interrupted first start leaves behind.  Any other pointerless database
        is a format-6 store, which a start only ever opens.
        """

        if self._fresh_at_construction:
            return True
        return (
            self._active_database is not None
            and self._active_database.pointer is None
            and self._schema_version() >= 16
            and not self._has_user_rows()
        )

    async def _acquire_creation_lock(self) -> bool:
        """Swap the shared storage lock for the exclusive one a creation needs.

        Shared holders do not exclude one another, so two first starts would
        create the same tables and publish the same pointer at once.  The
        shared lock is given up and the exclusive one polled for.  Whenever
        only the shared lock can be had the storage is read again, because the
        process holding it exclusively may have finished the store meanwhile.
        Other shared holders in this process, such as a manager that was
        constructed and never started, do not stand in the way: the exclusive
        lock is taken over them, and they read the storage again when they
        start.  A holder in another process does, until it lets go.

        Returns ``True`` holding the exclusive lock, with the storage re-read
        under it and still to be created; ``False`` holding the shared lock
        over a store another process created.  Raises ``DatabaseInUseError``,
        holding no lock, when neither happens in time.
        """

        loop = asyncio.get_running_loop()
        deadline = loop.time() + _CREATION_LOCK_TIMEOUT_SECONDS
        self._storage_lock_lost = True
        self._database_lock.release()
        while True:
            try:
                self._database_lock = DatabaseFileLock(
                    self.storage_path, "exclusive", over_process_holders=True
                ).acquire()
            except DatabaseInUseError:
                pass
            else:
                try:
                    self._resolve_storage()
                    if self._start_creates_store():
                        self._storage_lock_lost = False
                        return True
                except BaseException:
                    self._restore_shared_lock()
                    raise
                self._restore_shared_lock()
                self._storage_lock_lost = False
                return False
            try:
                self._database_lock = DatabaseFileLock(
                    self.storage_path, "shared"
                ).acquire()
            except DatabaseInUseError:
                pass
            else:
                self._resolve_storage()
                if not self._start_creates_store():
                    self._storage_lock_lost = False
                    return False
                self._database_lock.release()
            if loop.time() >= deadline:
                raise DatabaseInUseError()
            await asyncio.sleep(_CREATION_LOCK_RETRY_SECONDS)

    def _restore_shared_lock(self) -> None:
        """Turn the exclusive creation lock into the lifetime shared one.

        The lock is converted rather than released and taken again, so the
        start always ends up holding the storage, and an error raised by the
        creation is not replaced by a failure to get the lock back.
        """

        self._database_lock.demote()

    async def init_db(self):
        """Initialize the database tables and run migrations."""
        # Skip if already initialized
        if self._initialized:
            return
        # One start at a time: a second caller waits for the first instead of
        # swapping the storage lock the first is creating the store under.
        async with self._init_lock:
            if not self._initialized:
                await self._init_db_locked()

    async def _init_db_locked(self) -> None:
        if self._storage_lock_lost:
            # An earlier attempt gave the storage lock up and could not get it
            # back, so whatever it read about the storage may be stale, and an
            # engine it built may point at a database no longer selected.
            if not self._database_lock.acquired:
                self._database_lock = DatabaseFileLock(
                    self.storage_path, "shared"
                ).acquire()
            self._resolve_storage()
            self._engine = None
            self._session_factory = None
            self._migrated = False
            self._storage_lock_lost = False
        # A store is created, and its pointer published, only under the
        # exclusive storage lock, held for no longer than the creation takes.
        creating = (
            self._lock_mode == "shared"
            and self._start_creates_store()
            and await self._acquire_creation_lock()
        )
        try:
            await self._initialize_storage()
        finally:
            if creating:
                self._restore_shared_lock()

        self._initialized = True
        if self.format_version == 7 and not self._defer_fresh_activation:
            from .retrieval.runtime import schedule_projection_job_drain

            schedule_projection_job_drain(self.db_path, max_jobs=5)
        logger.info("Database initialized")

    async def _initialize_storage(self) -> None:
        """Create or open the selected database under the lock now held."""

        # Check if this is a fresh database.  This fact is captured before any
        # engine construction so a failed pointer publication cannot be mistaken
        # for an ordinary v6 database within this manager lifetime.
        is_new_db = self._fresh_at_construction and not self.db_path.exists()
        previous_schema_version = self._schema_version() if self.db_path.exists() else 0

        # An active format-7 store on an older schema is upgraded only by
        # `migrate-v7 --apply`, which works on a verified copy, retains the
        # source and a snapshot, and repairs what the additive ledger alone
        # cannot.  Applying the ledger here would rewrite, without a backup,
        # the very database a rollback returns to, so the start is refused
        # before anything is written.
        if (
            not is_new_db
            and self._active_database is not None
            and self._active_database.pointer is not None
            and self.format_version == 7
            and previous_schema_version < CURRENT_SCHEMA_VERSION
        ):
            raise SchemaUpgradeRequiredError(previous_schema_version)

        # Run migrations first for existing databases (sync operation)
        # This happens BEFORE we create the async engine to avoid lock conflicts
        if not is_new_db:
            # A format-6 store is what `migrate-v7` snapshots and can roll back
            # to.  Applying the v7 ledger to it in place would rewrite the
            # source without a backup, and its governance backfill then goes
            # stale against later v6 edits, so cap it at the last v6 version.
            self._run_migrations(
                maximum_version=self._v6_migration_ceiling(previous_schema_version)
            )

        # Then create any new tables
        await self._create_missing_tables()

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

    async def _create_missing_tables(self) -> None:
        """Create the model tables the database does not have yet.

        An existing store is opened under the shared storage lock, so another
        start may be adding the same tables to it: each looks for a table and
        then creates it, and the one that comes second is refused.  The table
        it was refused over now exists, so it looks again; every refusal is
        over a different table, which bounds the number of passes.  The look
        itself can be refused the same way: SQLite reports "database schema
        has changed" for a statement prepared before the other start's DDL
        committed, and that too is answered by looking again.
        """

        for _ in range(2 * len(Base.metadata.tables)):
            try:
                async with self.engine.begin() as conn:
                    await conn.run_sync(Base.metadata.create_all)
            except OperationalError as exc:
                if not _is_concurrent_ddl_refusal(exc):
                    raise
            else:
                return
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

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
            dense_encoder_changed = self._dense_encoder_changed(connection)
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
                    # A finished rebuild job is left alone, except that vectors
                    # built by another encoder cannot answer queries and no
                    # write is coming to queue their replacement.
                    requeue_existing=(
                        projection_name == "dense" and dense_encoder_changed
                    ),
                )
            connection.commit()
        except Exception as exc:
            connection.rollback()
            raise RuntimeError("LEXICAL_BOOTSTRAP_FAILED") from exc
        finally:
            connection.close()

    def _dense_encoder_changed(self, connection: sqlite3.Connection) -> bool:
        """Whether the active dense projection was built for other settings."""

        import json

        from .config import settings

        row = connection.execute(
            "SELECT details_json FROM projection_manifests WHERE workspace_id=? "
            "AND projection_name='dense' AND status='active'",
            (self.workspace_id,),
        ).fetchone()
        if row is None:
            return False
        try:
            details = json.loads(str(row[0]))
        except ValueError:
            return False
        if (
            not isinstance(details, dict)
            or "model_id" not in details
            or "dimension" not in details
        ):
            return False
        return (
            details["model_id"] != settings.embedding_model
            or details["dimension"] != settings.embedding_dimension
        )

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

    def _is_unpublished_fresh_database(self) -> bool:
        """Whether a pointerless database can only be an unfinished first start.

        Nothing was ever recorded in it and no migration run exists beside it,
        so no pointer can have named another database and publishing the
        generation-one pointer loses nothing.
        """

        migrations = self.storage_path / "migrations"
        if migrations.exists() or migrations.is_symlink():
            return False
        if self._schema_version() < 16 or self._has_user_rows():
            return False
        with closing(sqlite3.connect(self.db_path)) as connection:
            for table in (
                "memory_events",
                "governance_events",
                "workspace_link_events",
                "memory_capture_candidates",
                "v7_migration_runs",
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
                    return False
        return True

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
                    "memory_provenance_edges",
                    "memory_outcome_signals",
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
