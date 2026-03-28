"""
Log File Notification Channel - Writes notifications to a log file.

This channel writes structured notifications to a log file that can be
read by external tools, scripts, or editor integrations.
"""

import contextlib
import json
import logging
from pathlib import Path
from typing import Any

from daem0nmcp.watcher import WatcherNotification

logger = logging.getLogger(__name__)


class LogFileChannel:
    """
    Notification channel that writes to a log file.

    Writes structured JSON-lines format to a log file that can be
    monitored by external tools, editor plugins, or scripts.

    Each line in the log file is a valid JSON object containing:
    - timestamp: ISO format timestamp
    - file_path: The modified file path
    - summary: Brief summary of memories
    - memory_count: Number of associated memories
    - categories: Dict of category counts
    - memories: List of memory records (optional, controlled by include_memories)

    Example:
        ```python
        from daem0nmcp.channels import LogFileChannel
        from pathlib import Path

        # Write to project-specific log
        channel = LogFileChannel(
            log_path=Path(".daem0nmcp/watcher.log"),
            max_size_mb=10,
            include_memories=True
        )
        watcher = FileWatcher(..., channels=[channel])
        ```

    The log file uses JSON-lines format (one JSON object per line),
    making it easy to parse with tools like `jq` or tail-f monitoring.
    """

    def __init__(
        self,
        log_path: Path,
        max_size_mb: float = 10.0,
        include_memories: bool = True,
        truncate_content: int = 200,
    ):
        """
        Initialize the log file channel.

        Args:
            log_path: Path to the log file
            max_size_mb: Maximum log file size before rotation (0 = no limit)
            include_memories: Include full memory records in log entries
            truncate_content: Truncate memory content to this many chars (0 = no truncation)
        """
        self._log_path = log_path
        self._max_size_bytes = int(max_size_mb * 1024 * 1024)
        self._include_memories = include_memories
        self._truncate_content = truncate_content

        # Ensure parent directory exists
        self._log_path.parent.mkdir(parents=True, exist_ok=True)

        logger.debug(f"LogFileChannel initialized: {log_path}")

    @property
    def log_path(self) -> Path:
        """Get the log file path."""
        return self._log_path

    async def notify(self, notification: WatcherNotification) -> None:
        """
        Write a notification entry to the log file.

        Args:
            notification: The watcher notification with file and memory info
        """
        try:
            # Check file size and rotate if needed
            self._maybe_rotate()

            # Build log entry
            entry = self._build_entry(notification)

            # Write to file (append mode)
            with open(self._log_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, default=str) + "\n")

            logger.debug(f"Logged notification for: {notification.file_path.name}")

        except Exception as e:
            logger.error(f"Failed to write to log file: {e}")

    def _build_entry(self, notification: WatcherNotification) -> dict[str, Any]:
        """Build a log entry dictionary."""
        # Count by category
        categories: dict[str, int] = {}
        for mem in notification.memories:
            cat = mem.get("category", "unknown")
            categories[cat] = categories.get(cat, 0) + 1

        entry = {
            "timestamp": notification.timestamp.isoformat(),
            "file_path": str(notification.file_path),
            "project_path": str(notification.project_path),
            "summary": notification.summary,
            "memory_count": len(notification.memories),
            "categories": categories,
        }

        # Optionally include memories
        if self._include_memories:
            memories = []
            for mem in notification.memories:
                mem_copy = dict(mem)

                # Truncate content if configured
                if self._truncate_content > 0:
                    content = mem_copy.get("content", "")
                    if len(content) > self._truncate_content:
                        mem_copy["content"] = content[: self._truncate_content] + "..."
                    rationale = mem_copy.get("rationale", "")
                    if rationale and len(rationale) > self._truncate_content:
                        mem_copy["rationale"] = (
                            rationale[: self._truncate_content] + "..."
                        )

                memories.append(mem_copy)

            entry["memories"] = memories

        return entry

    def _maybe_rotate(self) -> None:
        """Rotate log file if it exceeds max size."""
        if self._max_size_bytes <= 0:
            return

        if not self._log_path.exists():
            return

        try:
            size = self._log_path.stat().st_size
            if size >= self._max_size_bytes:
                # Simple rotation: rename current to .old and start fresh
                old_path = self._log_path.with_suffix(".log.old")
                if old_path.exists():
                    old_path.unlink()
                self._log_path.rename(old_path)
                logger.info(f"Rotated log file: {self._log_path}")
        except Exception as e:
            logger.warning(f"Log rotation failed: {e}")

    def clear(self) -> None:
        """Clear the log file."""
        try:
            if self._log_path.exists():
                self._log_path.unlink()
                logger.info(f"Cleared log file: {self._log_path}")
        except Exception as e:
            logger.error(f"Failed to clear log file: {e}")

    def read_recent(self, count: int = 10) -> list:
        """
        Read the most recent log entries.

        Args:
            count: Maximum number of entries to return

        Returns:
            List of log entry dictionaries, most recent first
        """
        entries: list[dict[str, Any]] = []

        if not self._log_path.exists():
            return entries

        try:
            with open(self._log_path, encoding="utf-8") as f:
                lines = f.readlines()

            # Parse last N lines
            for line in reversed(lines[-count:]):
                line = line.strip()
                if line:
                    with contextlib.suppress(json.JSONDecodeError):
                        entries.append(json.loads(line))

        except Exception as e:
            logger.error(f"Failed to read log file: {e}")

        return entries
