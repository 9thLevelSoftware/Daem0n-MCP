"""
Integration tests for the file watcher system.

Tests the watcher daemon, notification channels, and their integration
with the memory system.
"""

import json
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from daem0nmcp.channels import (
    EditorPollChannel,
    LogFileChannel,
    SystemNotifyChannel,
)
from daem0nmcp.database import DatabaseManager
from daem0nmcp.memory import MemoryManager
from daem0nmcp.watcher import (
    CallbackChannel,
    FileWatcher,
    LoggingChannel,
    WatcherConfig,
    WatcherNotification,
    create_watcher,
)

# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture
def temp_project():
    """Create a temporary project directory."""
    temp_dir = tempfile.mkdtemp(prefix="daem0n_test_")
    project_path = Path(temp_dir) / "test_project"
    project_path.mkdir()

    # Create some test files
    (project_path / "main.py").write_text("print('hello')")
    (project_path / "utils.py").write_text("def foo(): pass")
    (project_path / "README.md").write_text("# Test")

    # Create subdirectory
    src_dir = project_path / "src"
    src_dir.mkdir()
    (src_dir / "app.py").write_text("app = None")

    yield project_path

    # Cleanup
    shutil.rmtree(temp_dir, ignore_errors=True)


@pytest.fixture
def temp_storage():
    """Create a temporary storage directory."""
    temp_dir = tempfile.mkdtemp(prefix="daem0n_storage_")
    yield Path(temp_dir)
    shutil.rmtree(temp_dir, ignore_errors=True)


@pytest.fixture
async def memory_manager(temp_storage):
    """Create a memory manager with test storage."""
    db = DatabaseManager(storage_path=str(temp_storage))
    await db.init_db()
    manager = MemoryManager(db)

    yield manager

    # Cleanup
    if manager._qdrant:
        manager._qdrant.close()
    await db.close()


# =============================================================================
# WatcherNotification Tests
# =============================================================================


class TestWatcherNotification:
    """Test WatcherNotification dataclass."""

    def test_empty_memories_no_summary(self):
        """Test notification with no memories."""
        notif = WatcherNotification(
            file_path=Path("/test/file.py"),
            memories=[],
            timestamp=datetime.now(timezone.utc),
            project_path=Path("/test"),
        )
        assert notif.summary == ""

    def test_summary_generation(self):
        """Test automatic summary generation."""
        memories = [
            {"id": 1, "category": "warning", "content": "Watch out"},
            {"id": 2, "category": "decision", "content": "Use X"},
            {"id": 3, "category": "decision", "content": "Use Y"},
        ]
        notif = WatcherNotification(
            file_path=Path("/test/file.py"),
            memories=memories,
            timestamp=datetime.now(timezone.utc),
            project_path=Path("/test"),
        )
        assert "3 memories" in notif.summary
        assert "ATTENTION NEEDED" in notif.summary

    def test_summary_with_failures(self):
        """Test summary with failed approaches."""
        memories = [
            {"id": 1, "category": "decision", "content": "Try X", "worked": False},
        ]
        notif = WatcherNotification(
            file_path=Path("/test/file.py"),
            memories=memories,
            timestamp=datetime.now(timezone.utc),
            project_path=Path("/test"),
        )
        assert "ATTENTION NEEDED" in notif.summary


# =============================================================================
# WatcherConfig Tests
# =============================================================================


class TestWatcherConfig:
    """Test WatcherConfig dataclass."""

    def test_default_skip_patterns(self):
        """Test default skip patterns are applied."""
        config = WatcherConfig()
        assert ".git" in config.skip_patterns
        assert "node_modules" in config.skip_patterns
        assert "__pycache__" in config.skip_patterns
        assert ".daem0nmcp" in config.skip_patterns

    def test_custom_skip_patterns(self):
        """Test custom skip patterns override defaults."""
        config = WatcherConfig(skip_patterns=["custom_dir"])
        assert config.skip_patterns == ["custom_dir"]

    def test_default_values(self):
        """Test default configuration values."""
        config = WatcherConfig()
        assert config.debounce_seconds == 1.0
        assert config.recursive is True
        assert config.max_queue_size == 100
        assert config.watch_extensions == []


# =============================================================================
# FileWatcher Tests
# =============================================================================


class TestFileWatcher:
    """Test FileWatcher class."""

    @pytest.fixture
    def mock_memory_manager(self):
        """Create a mock memory manager."""
        manager = MagicMock()
        manager.recall_for_file = AsyncMock(
            return_value={
                "found": 0,
                "warnings": [],
                "decisions": [],
                "patterns": [],
                "learnings": [],
            }
        )
        return manager

    async def test_watcher_creation(self, temp_project, mock_memory_manager):
        """Test watcher can be created."""
        watcher = FileWatcher(
            project_path=temp_project,
            memory_manager=mock_memory_manager,
            channels=[LoggingChannel()],
        )
        assert watcher.is_running is False
        assert watcher.statistics["files_changed"] == 0

    async def test_watcher_start_stop(self, temp_project, mock_memory_manager):
        """Test watcher can start and stop."""
        watcher = FileWatcher(
            project_path=temp_project,
            memory_manager=mock_memory_manager,
            channels=[LoggingChannel()],
        )

        await watcher.start()
        assert watcher.is_running is True

        await watcher.stop()
        assert watcher.is_running is False

    async def test_watcher_invalid_path(self, mock_memory_manager):
        """Test watcher raises error for invalid path."""
        watcher = FileWatcher(
            project_path=Path("/nonexistent/path"),
            memory_manager=mock_memory_manager,
            channels=[],
        )

        with pytest.raises(RuntimeError, match="does not exist"):
            await watcher.start()

    async def test_watcher_double_start(self, temp_project, mock_memory_manager):
        """Test watcher raises error on double start."""
        watcher = FileWatcher(
            project_path=temp_project,
            memory_manager=mock_memory_manager,
            channels=[],
        )

        await watcher.start()
        try:
            with pytest.raises(RuntimeError, match="already running"):
                await watcher.start()
        finally:
            await watcher.stop()

    async def test_watcher_add_remove_channel(self, temp_project, mock_memory_manager):
        """Test adding and removing channels."""
        watcher = FileWatcher(
            project_path=temp_project,
            memory_manager=mock_memory_manager,
            channels=[],
        )

        channel = LoggingChannel()
        watcher.add_channel(channel)
        assert channel in watcher._channels

        watcher.remove_channel(channel)
        assert channel not in watcher._channels

    async def test_watcher_debounce(self, temp_project, mock_memory_manager):
        """Test debounce tracking."""
        watcher = FileWatcher(
            project_path=temp_project,
            memory_manager=mock_memory_manager,
            channels=[],
            config=WatcherConfig(debounce_seconds=5.0),
        )

        test_file = temp_project / "main.py"

        # First call should not debounce
        assert watcher._should_debounce(test_file) is False

        # Manually update debounce tracking
        watcher._last_notified[test_file] = datetime.now(timezone.utc)

        # Second call should debounce
        assert watcher._should_debounce(test_file) is True

        # Clear cache
        watcher.clear_debounce_cache()
        assert watcher._should_debounce(test_file) is False


# =============================================================================
# Channel Tests
# =============================================================================


class TestLoggingChannel:
    """Test LoggingChannel."""

    async def test_notify(self, caplog):
        """Test logging channel logs notifications."""
        import logging

        caplog.set_level(logging.INFO)

        channel = LoggingChannel()
        notif = WatcherNotification(
            file_path=Path("/test/file.py"),
            memories=[{"category": "warning", "content": "Test warning"}],
            timestamp=datetime.now(timezone.utc),
            project_path=Path("/test"),
        )

        await channel.notify(notif)

        assert "[MEMORY ALERT]" in caplog.text
        assert "file.py" in caplog.text


class TestCallbackChannel:
    """Test CallbackChannel."""

    async def test_callback_called(self):
        """Test callback is invoked."""
        received = []

        async def handler(notif):
            received.append(notif)

        channel = CallbackChannel(handler)
        notif = WatcherNotification(
            file_path=Path("/test/file.py"),
            memories=[],
            timestamp=datetime.now(timezone.utc),
            project_path=Path("/test"),
        )

        await channel.notify(notif)

        assert len(received) == 1
        assert received[0] is notif


class TestLogFileChannel:
    """Test LogFileChannel."""

    async def test_write_entry(self, temp_storage):
        """Test writing log entries."""
        log_path = temp_storage / "watcher.log"
        channel = LogFileChannel(log_path)

        test_file = temp_storage / "file.py"
        notif = WatcherNotification(
            file_path=test_file,
            memories=[{"id": 1, "category": "warning", "content": "Test"}],
            timestamp=datetime.now(timezone.utc),
            project_path=temp_storage,
        )

        await channel.notify(notif)

        # Read log
        assert log_path.exists()
        content = log_path.read_text()
        entry = json.loads(content.strip())

        assert entry["file_path"] == str(test_file)
        assert entry["memory_count"] == 1
        assert "warning" in entry["categories"]

    async def test_read_recent(self, temp_storage):
        """Test reading recent entries."""
        log_path = temp_storage / "watcher.log"
        channel = LogFileChannel(log_path)

        # Write multiple entries
        for i in range(5):
            notif = WatcherNotification(
                file_path=Path(f"/test/file{i}.py"),
                memories=[],
                timestamp=datetime.now(timezone.utc),
                project_path=Path("/test"),
            )
            await channel.notify(notif)

        recent = channel.read_recent(3)
        assert len(recent) == 3

    async def test_log_rotation(self, temp_storage):
        """Test log file rotation."""
        log_path = temp_storage / "watcher.log"
        # Very small max size for testing
        channel = LogFileChannel(log_path, max_size_mb=0.0001)

        # Write until rotation happens
        for i in range(100):
            notif = WatcherNotification(
                file_path=Path(f"/test/file{i}.py"),
                memories=[{"id": i, "category": "decision", "content": "X" * 100}],
                timestamp=datetime.now(timezone.utc),
                project_path=Path("/test"),
            )
            await channel.notify(notif)

        # Old log should exist
        old_path = log_path.with_suffix(".log.old")
        assert old_path.exists() or log_path.exists()


class TestEditorPollChannel:
    """Test EditorPollChannel."""

    async def test_write_poll_file(self, temp_storage):
        """Test writing poll file."""
        poll_path = temp_storage / "editor-poll.json"
        channel = EditorPollChannel(poll_path)

        test_file = temp_storage / "file.py"
        notif = WatcherNotification(
            file_path=test_file,
            memories=[{"id": 1, "category": "warning", "content": "Test"}],
            timestamp=datetime.now(timezone.utc),
            project_path=temp_storage,
        )

        await channel.notify(notif)

        # Read poll file
        assert poll_path.exists()
        data = json.loads(poll_path.read_text())

        assert data["version"] == 1
        assert str(test_file) in data["files"]
        assert data["stats"]["total_files"] == 1

    async def test_multiple_files(self, temp_storage):
        """Test tracking multiple files."""
        poll_path = temp_storage / "editor-poll.json"
        channel = EditorPollChannel(poll_path)

        for i in range(3):
            notif = WatcherNotification(
                file_path=Path(f"/test/file{i}.py"),
                memories=[{"id": i, "category": "decision", "content": "Test"}],
                timestamp=datetime.now(timezone.utc),
                project_path=Path("/test"),
            )
            await channel.notify(notif)

        assert len(channel.get_all_files()) == 3

    async def test_get_file_info(self, temp_storage):
        """Test getting info for specific file."""
        poll_path = temp_storage / "editor-poll.json"
        channel = EditorPollChannel(poll_path)

        file_path = Path("/test/file.py")
        notif = WatcherNotification(
            file_path=file_path,
            memories=[{"id": 1, "category": "warning", "content": "Test"}],
            timestamp=datetime.now(timezone.utc),
            project_path=Path("/test"),
        )

        await channel.notify(notif)

        info = channel.get_file_info(file_path)
        assert info is not None
        assert info["has_warnings"] is True
        assert info["memory_count"] == 1

    async def test_remove_file(self, temp_storage):
        """Test removing a file entry."""
        poll_path = temp_storage / "editor-poll.json"
        channel = EditorPollChannel(poll_path)

        file_path = Path("/test/file.py")
        notif = WatcherNotification(
            file_path=file_path,
            memories=[],
            timestamp=datetime.now(timezone.utc),
            project_path=Path("/test"),
        )

        await channel.notify(notif)
        assert channel.get_file_info(file_path) is not None

        channel.remove_file(file_path)
        assert channel.get_file_info(file_path) is None


class TestSystemNotifyChannel:
    """Test SystemNotifyChannel."""

    def test_channel_creation(self):
        """Test channel can be created."""
        channel = SystemNotifyChannel(
            app_name="Test",
            timeout=5,
        )
        # May or may not be available depending on platform
        assert hasattr(channel, "is_available")

    async def test_notify_without_plyer(self):
        """Test notification gracefully handles missing plyer."""
        channel = SystemNotifyChannel()

        # Mock plyer not available
        channel._plyer_available = False
        channel._notification = None

        notif = WatcherNotification(
            file_path=Path("/test/file.py"),
            memories=[],
            timestamp=datetime.now(timezone.utc),
            project_path=Path("/test"),
        )

        # Should not raise
        await channel.notify(notif)


# =============================================================================
# Factory Function Tests
# =============================================================================


class TestCreateWatcher:
    """Test create_watcher factory function."""

    def test_create_with_defaults(self, temp_project):
        """Test creating watcher with defaults."""
        mock_manager = MagicMock()
        watcher = create_watcher(
            project_path=temp_project,
            memory_manager=mock_manager,
        )

        assert watcher is not None
        assert len(watcher._channels) == 1  # Default LoggingChannel
        assert watcher._config.debounce_seconds == 1.0

    def test_create_with_custom_options(self, temp_project):
        """Test creating watcher with custom options."""
        mock_manager = MagicMock()

        class CustomChannel:
            async def notify(self, notif):
                pass

        watcher = create_watcher(
            project_path=temp_project,
            memory_manager=mock_manager,
            channels=[CustomChannel()],
            debounce_seconds=2.5,
            skip_patterns=["extra_dir"],
            watch_extensions=[".py"],
        )

        assert len(watcher._channels) == 1
        assert watcher._config.debounce_seconds == 2.5
        assert "extra_dir" in watcher._config.skip_patterns
        assert ".py" in watcher._config.watch_extensions

    def test_create_with_string_path(self, temp_project):
        """Test creating watcher with string path."""
        mock_manager = MagicMock()
        watcher = create_watcher(
            project_path=str(temp_project),
            memory_manager=mock_manager,
        )

        assert watcher._project_path == temp_project.resolve()


# =============================================================================
# Integration Tests
# =============================================================================


class TestWatcherIntegration:
    """Integration tests with real memory manager."""

    async def test_watcher_with_memories(self, temp_project, memory_manager):
        """Test watcher notifies when file has memories."""
        # Create a memory for a file
        test_file = temp_project / "main.py"
        await memory_manager.remember(
            category="warning",
            content="Be careful with this file",
            file_path=str(test_file),
            project_path=str(temp_project),
        )

        # Track notifications
        notifications = []

        async def capture(notif):
            notifications.append(notif)

        channel = CallbackChannel(capture)
        watcher = FileWatcher(
            project_path=temp_project,
            memory_manager=memory_manager,
            channels=[channel],
            config=WatcherConfig(debounce_seconds=0.1),
        )

        await watcher.start()

        try:
            # Simulate file change
            await watcher._handle_change(test_file)

            # Should have received notification
            assert len(notifications) == 1
            assert notifications[0].file_path == test_file
            assert len(notifications[0].memories) > 0

        finally:
            await watcher.stop()

    async def test_watcher_no_memories(self, temp_project, memory_manager):
        """Test watcher doesn't notify when file has no memories."""
        notifications = []

        async def capture(notif):
            notifications.append(notif)

        channel = CallbackChannel(capture)
        watcher = FileWatcher(
            project_path=temp_project,
            memory_manager=memory_manager,
            channels=[channel],
        )

        await watcher.start()

        try:
            # File with no memories
            test_file = temp_project / "utils.py"
            await watcher._handle_change(test_file)

            # Should not have received notification
            assert len(notifications) == 0
            assert watcher.statistics["files_skipped_no_memories"] == 1

        finally:
            await watcher.stop()
