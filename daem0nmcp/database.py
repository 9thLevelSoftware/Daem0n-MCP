"""
Database Manager - Simplified for the focused memory system.
"""

import logging
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path

from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from .models import (  # noqa: F401 - MemoryVersion imported for table creation
    Base,
    MemoryVersion,
)

logger = logging.getLogger(__name__)


class DatabaseManager:
    """
    Manages the SQLite database connection.

    Simplified from the original - no more tool initialization or complex migrations.
    Just creates tables and provides session management.
    Auto-migrates existing databases on startup.
    """

    def __init__(self, storage_path: str = "./storage", db_name: str = "daem0nmcp.db"):
        self.storage_path = Path(storage_path)
        self.storage_path.mkdir(parents=True, exist_ok=True)

        self.db_path = self.storage_path / db_name
        self.db_url = f"sqlite+aiosqlite:///{self.db_path}"
        self._migrated = False
        self._initialized = False
        self._engine = None
        self._session_factory = None

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
    def SessionLocal(self):
        """Property for backward compatibility."""
        self._get_engine()  # Ensure engine is created
        return self._session_factory

    def _run_migrations(self, force: bool = False):
        """Run schema migrations (sync, before async engine starts)."""
        if self._migrated and not force:
            return

        if self.db_path.exists():
            try:
                from .migrations import run_migrations

                count, applied = run_migrations(str(self.db_path))
                if count > 0:
                    logger.info(f"Applied {count} migration(s): {applied}")
            except Exception as e:
                logger.warning(f"Migration check failed: {e}")

        self._migrated = True

    async def init_db(self):
        """Initialize the database tables and run migrations."""
        # Skip if already initialized
        if self._initialized:
            return

        # Check if this is a fresh database
        is_new_db = not self.db_path.exists()

        # Run migrations first for existing databases (sync operation)
        # This happens BEFORE we create the async engine to avoid lock conflicts
        if not is_new_db:
            self._run_migrations()

        # Then create any new tables
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        # For fresh databases, run migrations after tables are created
        if is_new_db:
            self._run_migrations(force=True)

        self._initialized = True
        logger.info(f"Database initialized at {self.db_path}")

    @asynccontextmanager
    async def get_session(self):
        """Provide a transactional scope around a series of operations."""
        session = self.SessionLocal()
        try:
            yield session
            await session.commit()
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
        """Dispose of the engine."""
        if self._engine is not None:
            await self._engine.dispose()
            self._engine = None
            self._session_factory = None
            self._initialized = False
