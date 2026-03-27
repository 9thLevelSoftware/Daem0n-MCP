"""
File Watcher Daemon - Proactive memory notifications for file changes.

This module provides file system monitoring that detects changes to files
and notifies when relevant memories/decisions exist for those files.
Part of Phase 1 of the Daem0n-MCP Cognitive Architecture Upgrade.

Usage:
    from daem0nmcp.watcher import FileWatcher
    from daem0nmcp.memory import MemoryManager

    watcher = FileWatcher(
        project_path=Path("/my/project"),
        memory_manager=memory_manager,
        channels=[console_channel, log_channel]
    )
    await watcher.start()
    # ... later ...
    await watcher.stop()
"""

import asyncio
import fnmatch
import logging
from abc import abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from watchdog.events import DirModifiedEvent, FileSystemEventHandler
from watchdog.observers import Observer

logger = logging.getLogger(__name__)


# =============================================================================
# Data Classes
# =============================================================================


@dataclass
class WatcherNotification:
    """
    Notification payload sent when a file with associated memories is modified.

    Attributes:
        file_path: The path to the modified file
        memories: List of memory records from recall_for_file
        timestamp: When the change was detected
        project_path: The project root path
        summary: A brief summary of relevant memories
    """

    file_path: Path
    memories: list[dict[str, Any]]
    timestamp: datetime
    project_path: Path
    summary: str = ""

    def __post_init__(self):
        """Generate summary if not provided."""
        if not self.summary and self.memories:
            # Count by category
            by_category: dict[str, int] = {}
            has_warnings = False
            has_failures = False

            for mem in self.memories:
                cat = mem.get("category", "unknown")
                by_category[cat] = by_category.get(cat, 0) + 1
                if cat == "warning":
                    has_warnings = True
                if mem.get("worked") is False:
                    has_failures = True

            parts = []
            if has_warnings or has_failures:
                parts.append("ATTENTION NEEDED")
            parts.append(f"{len(self.memories)} memories")

            cat_parts = [
                f"{count} {cat}{'s' if count > 1 else ''}"
                for cat, count in sorted(by_category.items())
            ]
            if cat_parts:
                parts.append(f"({', '.join(cat_parts)})")

            self.summary = " - ".join(parts)


@dataclass
class WatcherConfig:
    """
    Configuration for the FileWatcher.

    Attributes:
        debounce_seconds: Time to wait before processing another change to the same file
        skip_patterns: Glob patterns for files/directories to skip
        watch_extensions: File extensions to watch (empty = all)
        recursive: Whether to watch subdirectories
        max_queue_size: Maximum pending changes before dropping oldest
    """

    debounce_seconds: float = 1.0
    skip_patterns: list[str] = field(default_factory=list)
    watch_extensions: list[str] = field(default_factory=list)
    recursive: bool = True
    max_queue_size: int = 100

    def __post_init__(self):
        """Set default skip patterns if none provided."""
        if not self.skip_patterns:
            self.skip_patterns = DEFAULT_SKIP_PATTERNS.copy()


# =============================================================================
# Default Skip Patterns
# =============================================================================

DEFAULT_SKIP_PATTERNS: list[str] = [
    # Version control
    ".git",
    ".git/**",
    ".svn",
    ".svn/**",
    ".hg",
    ".hg/**",
    # Package managers / dependencies
    "node_modules",
    "node_modules/**",
    # Python cache
    "__pycache__",
    "__pycache__/**",
    ".pytest_cache",
    ".pytest_cache/**",
    "*.pyc",
    "*.pyo",
    "*.pyd",
    # Virtual environments
    ".venv",
    ".venv/**",
    "venv",
    "venv/**",
    "env",
    "env/**",
    ".env",  # Environment files (also often secrets)
    # Build outputs
    "dist",
    "dist/**",
    "build",
    "build/**",
    "*.so",
    "*.dll",
    "*.dylib",
    # IDE / Editor
    ".idea",
    ".idea/**",
    ".vscode",
    ".vscode/**",
    "*.swp",
    "*.swo",
    "*~",
    # Daem0n-MCP own storage
    ".daem0nmcp",
    ".daem0nmcp/**",
    # Other common ignores
    ".mypy_cache",
    ".mypy_cache/**",
    ".tox",
    ".tox/**",
    ".coverage",
    "htmlcov",
    "htmlcov/**",
    ".eggs",
    ".eggs/**",
    "*.egg-info",
    "*.egg-info/**",
]


# =============================================================================
# Notification Channel Protocol
# =============================================================================


@runtime_checkable
class NotificationChannel(Protocol):
    """
    Protocol for notification channels.

    Implementations should handle delivering notifications to their target
    (console, log file, WebSocket, etc.).
    """

    @abstractmethod
    async def notify(self, notification: WatcherNotification) -> None:
        """
        Send a notification about a file change with associated memories.

        Args:
            notification: The notification payload containing file path,
                         memories, and metadata.
        """
        ...


# =============================================================================
# File Event Handler (Watchdog Integration)
# =============================================================================


class _FileChangeHandler(FileSystemEventHandler):
    """
    Internal handler for watchdog file system events.

    Filters events and puts relevant ones into the async queue.
    """

    def __init__(
        self,
        queue: asyncio.Queue,
        loop: asyncio.AbstractEventLoop,
        project_path: Path,
        config: WatcherConfig,
    ):
        super().__init__()
        self._queue = queue
        self._loop = loop
        self._project_path = project_path
        self._config = config

    def _should_skip(self, path: Path) -> bool:
        """Check if the path should be skipped based on patterns."""
        # Get relative path for pattern matching
        try:
            rel_path = path.relative_to(self._project_path)
        except ValueError:
            # Path is outside project, skip it
            return True

        rel_str = str(rel_path).replace("\\", "/")

        # Check against skip patterns
        for pattern in self._config.skip_patterns:
            # Handle both directory patterns and file patterns
            if fnmatch.fnmatch(rel_str, pattern):
                return True
            # Check each path component
            for part in rel_path.parts:
                if fnmatch.fnmatch(part, pattern):
                    return True

        # Check extension filter if specified
        if self._config.watch_extensions:
            if path.suffix.lower() not in self._config.watch_extensions:
                return True

        return False

    def on_modified(self, event):
        """Handle file modification events."""
        # Skip directory events
        if isinstance(event, DirModifiedEvent):
            return

        path = Path(event.src_path)

        # Skip directories
        if path.is_dir():
            return

        # Check skip patterns
        if self._should_skip(path):
            logger.debug(f"Skipping change to: {path}")
            return

        # Queue the event for async processing
        try:
            self._loop.call_soon_threadsafe(self._queue.put_nowait, path)
        except asyncio.QueueFull:
            logger.warning(f"Event queue full, dropping change: {path}")


# =============================================================================
# Main FileWatcher Class
# =============================================================================


class FileWatcher:
    """
    Watches files and notifies when changes occur to files with memories.

    This is the core of the proactive layer - it monitors the file system
    and queries Daem0n-MCP's memory system to find relevant context for
    changed files, then notifies through registered channels.

    Example:
        ```python
        from daem0nmcp.watcher import FileWatcher, WatcherConfig

        watcher = FileWatcher(
            project_path=Path("/my/project"),
            memory_manager=memory_manager,
            channels=[console_channel],
            config=WatcherConfig(debounce_seconds=2.0)
        )

        await watcher.start()

        # Watcher runs in background...
        # When files change, channels receive notifications

        await watcher.stop()
        ```
    """

    def __init__(
        self,
        project_path: Path,
        memory_manager: Any,  # MemoryManager from daem0nmcp.memory
        channels: list[NotificationChannel],
        config: WatcherConfig | None = None,
    ):
        """
        Initialize the FileWatcher.

        Args:
            project_path: The root directory to watch
            memory_manager: MemoryManager instance for querying memories
            channels: List of notification channels to send alerts to
            config: Optional configuration (uses defaults if not provided)
        """
        self._project_path = project_path.resolve()
        self._memory_manager = memory_manager
        self._channels = channels
        self._config = config or WatcherConfig()

        # Internal state
        self._observer: Observer | None = None
        self._event_queue: asyncio.Queue | None = None
        self._process_task: asyncio.Task | None = None
        self._running = False

        # Debounce tracking: file_path -> last_notification_time
        self._last_notified: dict[Path, datetime] = {}

        # Statistics
        self._stats = {
            "files_changed": 0,
            "notifications_sent": 0,
            "files_skipped_no_memories": 0,
            "files_debounced": 0,
            "errors": 0,
        }

    @property
    def is_running(self) -> bool:
        """Check if the watcher is currently running."""
        return self._running

    @property
    def statistics(self) -> dict[str, int]:
        """Get watcher statistics."""
        return self._stats.copy()

    async def start(self) -> None:
        """
        Start watching for file changes.

        This starts the watchdog observer in a background thread and
        the async processing task for handling events.

        Raises:
            RuntimeError: If already running or project path doesn't exist
        """
        if self._running:
            raise RuntimeError("FileWatcher is already running")

        if not self._project_path.exists():
            raise RuntimeError(f"Project path does not exist: {self._project_path}")

        if not self._project_path.is_dir():
            raise RuntimeError(f"Project path is not a directory: {self._project_path}")

        logger.info(f"Starting FileWatcher for: {self._project_path}")

        # Get the current event loop
        loop = asyncio.get_running_loop()

        # Create the event queue
        self._event_queue = asyncio.Queue(maxsize=self._config.max_queue_size)

        # Create and start the watchdog observer
        self._observer = Observer()
        handler = _FileChangeHandler(
            queue=self._event_queue,
            loop=loop,
            project_path=self._project_path,
            config=self._config,
        )

        self._observer.schedule(
            handler, str(self._project_path), recursive=self._config.recursive
        )
        self._observer.start()

        # Start the async processing task
        self._running = True
        self._process_task = asyncio.create_task(self._process_events())

        logger.info(
            f"FileWatcher started - watching {self._project_path} "
            f"with {len(self._channels)} channel(s)"
        )

    async def stop(self) -> None:
        """
        Stop watching for file changes.

        This gracefully shuts down the watchdog observer and processing task.
        """
        if not self._running:
            return

        logger.info("Stopping FileWatcher...")
        self._running = False

        # Stop the observer
        if self._observer:
            self._observer.stop()
            self._observer.join(timeout=5.0)
            self._observer = None

        # Cancel the processing task
        if self._process_task:
            self._process_task.cancel()
            try:
                await self._process_task
            except asyncio.CancelledError:
                pass
            self._process_task = None

        # Clear the queue
        self._event_queue = None

        logger.info(
            f"FileWatcher stopped - Stats: "
            f"changes={self._stats['files_changed']}, "
            f"notifications={self._stats['notifications_sent']}, "
            f"debounced={self._stats['files_debounced']}, "
            f"no_memories={self._stats['files_skipped_no_memories']}, "
            f"errors={self._stats['errors']}"
        )

    async def _process_events(self) -> None:
        """
        Main event processing loop.

        Reads events from the queue and processes them with debouncing.
        """
        while self._running:
            try:
                # Wait for next event with timeout
                try:
                    file_path = await asyncio.wait_for(
                        self._event_queue.get(), timeout=1.0
                    )
                except asyncio.TimeoutError:
                    continue

                self._stats["files_changed"] += 1

                # Check debounce
                if self._should_debounce(file_path):
                    self._stats["files_debounced"] += 1
                    logger.debug(f"Debounced: {file_path}")
                    continue

                # Process the change
                await self._handle_change(file_path)

            except asyncio.CancelledError:
                break
            except Exception as e:
                self._stats["errors"] += 1
                logger.error(f"Error processing event: {e}", exc_info=True)

    def _should_debounce(self, file_path: Path) -> bool:
        """
        Check if we should debounce this file change.

        Returns True if the same file was notified within debounce_seconds.
        """
        now = datetime.now(timezone.utc)
        last_time = self._last_notified.get(file_path)

        if last_time:
            elapsed = (now - last_time).total_seconds()
            if elapsed < self._config.debounce_seconds:
                return True

        return False

    async def _handle_change(self, file_path: Path) -> None:
        """
        Check for memories and notify if found.

        Args:
            file_path: The path to the changed file
        """
        try:
            # Query memories for this file
            result = await self._memory_manager.recall_for_file(
                file_path=str(file_path), project_path=str(self._project_path), limit=20
            )

            # Check if there are relevant memories
            total_memories = result.get("found", 0)

            if total_memories == 0:
                self._stats["files_skipped_no_memories"] += 1
                logger.debug(f"No memories for: {file_path}")
                return

            # Build flat list of all memories for notification
            all_memories: list[dict[str, Any]] = []
            for category in ["warnings", "decisions", "patterns", "learnings"]:
                memories_in_cat = result.get(category, [])
                for mem in memories_in_cat:
                    mem["category"] = category.rstrip("s")  # warnings -> warning
                    all_memories.append(mem)

            # Create notification
            now = datetime.now(timezone.utc)
            notification = WatcherNotification(
                file_path=file_path,
                memories=all_memories,
                timestamp=now,
                project_path=self._project_path,
            )

            # Update debounce tracking
            self._last_notified[file_path] = now

            # Send to all channels
            await self._notify_channels(notification)
            self._stats["notifications_sent"] += 1

            logger.info(
                f"Notified {len(self._channels)} channel(s) about {file_path.name}: "
                f"{notification.summary}"
            )

        except Exception as e:
            self._stats["errors"] += 1
            logger.error(f"Error handling change for {file_path}: {e}", exc_info=True)

    async def _notify_channels(self, notification: WatcherNotification) -> None:
        """
        Send notification to all registered channels.

        Errors in individual channels are logged but don't stop other channels.
        """
        tasks = []
        for channel in self._channels:
            tasks.append(self._notify_single_channel(channel, notification))

        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _notify_single_channel(
        self, channel: NotificationChannel, notification: WatcherNotification
    ) -> None:
        """Notify a single channel with error handling."""
        try:
            await channel.notify(notification)
        except Exception as e:
            logger.error(f"Channel {type(channel).__name__} failed: {e}", exc_info=True)

    def add_channel(self, channel: NotificationChannel) -> None:
        """
        Add a notification channel.

        Can be called while the watcher is running.
        """
        if channel not in self._channels:
            self._channels.append(channel)
            logger.info(f"Added channel: {type(channel).__name__}")

    def remove_channel(self, channel: NotificationChannel) -> None:
        """
        Remove a notification channel.

        Can be called while the watcher is running.
        """
        if channel in self._channels:
            self._channels.remove(channel)
            logger.info(f"Removed channel: {type(channel).__name__}")

    def clear_debounce_cache(self) -> None:
        """Clear the debounce tracking cache."""
        self._last_notified.clear()
        logger.debug("Cleared debounce cache")


# =============================================================================
# Simple Built-in Channel for Testing
# =============================================================================


class LoggingChannel:
    """
    Simple notification channel that logs to the module logger.

    Useful for testing and debugging.
    """

    def __init__(self, level: int = logging.INFO):
        self._level = level

    async def notify(self, notification: WatcherNotification) -> None:
        """Log the notification."""
        logger.log(
            self._level,
            f"[MEMORY ALERT] {notification.file_path.name}: {notification.summary}",
        )

        # Log details at debug level
        if logger.isEnabledFor(logging.DEBUG):
            for mem in notification.memories:
                logger.debug(
                    f"  - [{mem.get('category', '?')}] {mem.get('content', '')[:80]}..."
                )


class CallbackChannel:
    """
    Notification channel that calls a user-provided callback.

    Useful for integrating with other systems or for testing.

    Example:
        ```python
        async def my_handler(notification):
            print(f"File changed: {notification.file_path}")

        channel = CallbackChannel(my_handler)
        watcher = FileWatcher(..., channels=[channel])
        ```
    """

    def __init__(self, callback):
        """
        Initialize with a callback function.

        Args:
            callback: An async function that takes a WatcherNotification
        """
        self._callback = callback

    async def notify(self, notification: WatcherNotification) -> None:
        """Call the registered callback."""
        await self._callback(notification)


# =============================================================================
# Factory Function
# =============================================================================


def create_watcher(
    project_path: str | Path,
    memory_manager: Any,
    channels: list[NotificationChannel] | None = None,
    debounce_seconds: float = 1.0,
    skip_patterns: list[str] | None = None,
    watch_extensions: list[str] | None = None,
) -> FileWatcher:
    """
    Factory function to create a FileWatcher with common configuration.

    Args:
        project_path: The root directory to watch
        memory_manager: MemoryManager instance
        channels: Notification channels (defaults to LoggingChannel)
        debounce_seconds: Debounce interval
        skip_patterns: Additional patterns to skip (added to defaults)
        watch_extensions: File extensions to watch (empty = all)

    Returns:
        Configured FileWatcher instance (not started)

    Example:
        ```python
        watcher = create_watcher(
            project_path="/my/project",
            memory_manager=memory_manager,
            debounce_seconds=2.0
        )
        await watcher.start()
        ```
    """
    path = Path(project_path) if isinstance(project_path, str) else project_path

    # Build skip patterns
    patterns = DEFAULT_SKIP_PATTERNS.copy()
    if skip_patterns:
        patterns.extend(skip_patterns)

    # Build config
    config = WatcherConfig(
        debounce_seconds=debounce_seconds,
        skip_patterns=patterns,
        watch_extensions=watch_extensions or [],
    )

    # Default to logging channel if none provided
    if channels is None:
        channels = [LoggingChannel()]

    return FileWatcher(
        project_path=path,
        memory_manager=memory_manager,
        channels=channels,
        config=config,
    )
