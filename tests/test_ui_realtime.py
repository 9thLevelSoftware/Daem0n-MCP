"""
Tests for real-time update detection functionality.

Tests the check_for_updates tool and related change detection.
"""

import tempfile
from datetime import datetime, timezone

import pytest

from daem0nmcp.database import DatabaseManager
from daem0nmcp.memory import MemoryManager
from daem0nmcp.ui.ui_tools import check_for_updates


class TestCheckForUpdates:
    """Tests for check_for_updates function."""

    @pytest.mark.asyncio
    async def test_first_call_returns_has_changes_true(self):
        """First call without timestamp should return has_changes=True."""
        with tempfile.TemporaryDirectory() as temp_dir:
            db = DatabaseManager(temp_dir)
            await db.init_db()

            try:
                result = await check_for_updates(db)

                assert result["has_changes"] is True
                assert "last_update" in result
                assert "checked_at" in result
                assert result["recommended_interval"] == 10
            finally:
                await db.close()

    @pytest.mark.asyncio
    async def test_no_changes_returns_false(self):
        """When no changes since timestamp, returns has_changes=False."""
        with tempfile.TemporaryDirectory() as temp_dir:
            db = DatabaseManager(temp_dir)
            await db.init_db()

            try:
                # Create a memory so we have a baseline timestamp
                mm = MemoryManager(db)
                await mm.remember(
                    content="Initial memory",
                    category="pattern",
                )

                # Get current state
                first = await check_for_updates(db)
                assert first["last_update"] is not None

                # Immediately check again with that timestamp
                second = await check_for_updates(db, since=first["last_update"])

                # No changes should have occurred
                assert second["has_changes"] is False
            finally:
                await db.close()

    @pytest.mark.asyncio
    async def test_changes_detected_after_remember(self):
        """After creating memory, has_changes should be True."""
        with tempfile.TemporaryDirectory() as temp_dir:
            db = DatabaseManager(temp_dir)
            await db.init_db()

            try:
                mm = MemoryManager(db)

                # Create initial memory
                await mm.remember(
                    content="Initial memory",
                    category="pattern",
                )

                # Get baseline
                baseline = await check_for_updates(db)
                baseline_time = baseline["last_update"]

                # Create a new memory
                await mm.remember(
                    content="Test memory for real-time update",
                    category="pattern",
                )

                # Check for updates
                result = await check_for_updates(db, since=baseline_time)

                assert result["has_changes"] is True
                assert result["last_update"] > baseline_time
            finally:
                await db.close()

    @pytest.mark.asyncio
    async def test_interval_bounds_enforced(self):
        """Interval should be clamped to 5-60 range."""
        with tempfile.TemporaryDirectory() as temp_dir:
            db = DatabaseManager(temp_dir)
            await db.init_db()

            try:
                # Too low
                result_low = await check_for_updates(db, interval_seconds=1)
                assert result_low["recommended_interval"] == 5

                # Too high
                result_high = await check_for_updates(db, interval_seconds=120)
                assert result_high["recommended_interval"] == 60

                # Valid
                result_valid = await check_for_updates(db, interval_seconds=30)
                assert result_valid["recommended_interval"] == 30
            finally:
                await db.close()

    @pytest.mark.asyncio
    async def test_invalid_timestamp_treated_as_none(self):
        """Invalid timestamp string should be treated as None."""
        with tempfile.TemporaryDirectory() as temp_dir:
            db = DatabaseManager(temp_dir)
            await db.init_db()

            try:
                result = await check_for_updates(db, since="not-a-date")

                # Should succeed, treating it as first call
                assert result["has_changes"] is True
            finally:
                await db.close()

    @pytest.mark.asyncio
    async def test_checked_at_is_current(self):
        """checked_at should be close to current time."""
        with tempfile.TemporaryDirectory() as temp_dir:
            db = DatabaseManager(temp_dir)
            await db.init_db()

            try:
                before = datetime.now(timezone.utc)
                result = await check_for_updates(db)
                after = datetime.now(timezone.utc)

                checked = datetime.fromisoformat(
                    result["checked_at"].replace("Z", "+00:00")
                )

                assert before <= checked <= after
            finally:
                await db.close()

    @pytest.mark.asyncio
    async def test_returns_all_expected_fields(self):
        """Response should contain all expected fields."""
        with tempfile.TemporaryDirectory() as temp_dir:
            db = DatabaseManager(temp_dir)
            await db.init_db()

            try:
                result = await check_for_updates(db)

                assert "has_changes" in result
                assert "last_update" in result
                assert "recommended_interval" in result
                assert "checked_at" in result

                # Verify types
                assert isinstance(result["has_changes"], bool)
                assert isinstance(result["recommended_interval"], int)
                assert isinstance(result["checked_at"], str)
                # last_update can be None if no data
            finally:
                await db.close()
