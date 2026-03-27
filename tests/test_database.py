"""Tests for database configuration."""

import tempfile

import pytest


class TestSQLitePragmas:
    """Test SQLite PRAGMA settings."""

    @pytest.mark.asyncio
    async def test_wal_mode_enabled(self):
        """Verify WAL mode is enabled."""
        from daem0nmcp.database import DatabaseManager

        with tempfile.TemporaryDirectory() as temp_dir:
            db = DatabaseManager(temp_dir)
            await db.init_db()

            async with db.get_session() as session:
                from sqlalchemy import text

                result = await session.execute(text("PRAGMA journal_mode"))
                mode = result.scalar()
                assert mode.lower() == "wal"

            await db.close()

    @pytest.mark.asyncio
    async def test_foreign_keys_enabled(self):
        """Verify foreign keys are enabled."""
        from daem0nmcp.database import DatabaseManager

        with tempfile.TemporaryDirectory() as temp_dir:
            db = DatabaseManager(temp_dir)
            await db.init_db()

            async with db.get_session() as session:
                from sqlalchemy import text

                result = await session.execute(text("PRAGMA foreign_keys"))
                enabled = result.scalar()
                assert enabled == 1

            await db.close()
