"""
Editor Poll Channel - Creates poll files for editor integration.

This channel writes notification state to a JSON file that editors
can poll periodically to show inline annotations or notifications.
"""

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from daem0nmcp.watcher import WatcherNotification

logger = logging.getLogger(__name__)


class EditorPollChannel:
    """
    Notification channel for editor integration via file polling.

    Writes a JSON file that editors can poll to discover which files
    have associated memories. The file contains a map of file paths
    to their memory summaries, allowing editors to show:
    - Inline annotations
    - Status bar indicators
    - File decorations
    - Hover information

    The poll file format:
    ```json
    {
        "version": 1,
        "updated_at": "2024-01-15T10:30:00Z",
        "project_path": "/path/to/project",
        "files": {
            "/path/to/file.py": {
                "summary": "ATTENTION NEEDED - 3 memories",
                "has_warnings": true,
                "has_failures": false,
                "memory_count": 3,
                "categories": {"warning": 1, "decision": 2},
                "last_change": "2024-01-15T10:30:00Z",
                "top_memories": [...]
            }
        },
        "stats": {
            "total_files": 1,
            "total_memories": 3,
            "files_with_warnings": 1
        }
    }
    ```

    Example:
        ```python
        from daem0nmcp.channels import EditorPollChannel

        channel = EditorPollChannel(
            poll_path=Path(".daem0nmcp/editor-poll.json"),
            max_entries=50
        )
        watcher = FileWatcher(..., channels=[channel])
        ```

    Editor plugins can watch this file for changes and update their UI.
    """

    CURRENT_VERSION = 1

    def __init__(
        self,
        poll_path: Path,
        max_entries: int = 50,
        entry_ttl_seconds: int = 3600,
        include_top_memories: int = 3,
    ):
        """
        Initialize the editor poll channel.

        Args:
            poll_path: Path to the poll JSON file
            max_entries: Maximum file entries to keep (oldest pruned)
            entry_ttl_seconds: Remove entries older than this (0 = never)
            include_top_memories: Number of top memories to include per file
        """
        self._poll_path = poll_path
        self._max_entries = max_entries
        self._entry_ttl_seconds = entry_ttl_seconds
        self._include_top_memories = include_top_memories
        self._project_path: Path | None = None

        # In-memory state (persisted to file)
        self._files: dict[str, dict[str, Any]] = {}
        self._last_updated: datetime | None = None

        # Ensure parent directory exists
        self._poll_path.parent.mkdir(parents=True, exist_ok=True)

        # Load existing state if available
        self._load_state()

        logger.debug(f"EditorPollChannel initialized: {poll_path}")

    @property
    def poll_path(self) -> Path:
        """Get the poll file path."""
        return self._poll_path

    async def notify(self, notification: WatcherNotification) -> None:
        """
        Update the poll file with new notification.

        Args:
            notification: The watcher notification with file and memory info
        """
        try:
            # Set project path on first notification
            if self._project_path is None:
                self._project_path = notification.project_path

            # Build file entry
            file_key = str(notification.file_path)
            entry = self._build_file_entry(notification)

            # Update state
            self._files[file_key] = entry
            self._last_updated = datetime.now(timezone.utc)

            # Prune old/excess entries
            self._prune_entries()

            # Save state
            self._save_state()

            logger.debug(f"Updated poll file for: {notification.file_path.name}")

        except Exception as e:
            logger.error(f"Failed to update poll file: {e}")

    def _build_file_entry(self, notification: WatcherNotification) -> dict:
        """Build a file entry dictionary."""
        # Count by category
        categories: dict[str, int] = {}
        has_warnings = False
        has_failures = False

        for mem in notification.memories:
            cat = mem.get("category", "unknown")
            categories[cat] = categories.get(cat, 0) + 1
            if cat == "warning":
                has_warnings = True
            if mem.get("worked") is False:
                has_failures = True

        entry = {
            "summary": notification.summary,
            "has_warnings": has_warnings,
            "has_failures": has_failures,
            "memory_count": len(notification.memories),
            "categories": categories,
            "last_change": notification.timestamp.isoformat(),
        }

        # Include top memories
        if self._include_top_memories > 0:
            top_memories = []
            for mem in notification.memories[: self._include_top_memories]:
                content = mem.get("content", "")
                if len(content) > 100:
                    content = content[:97] + "..."

                top_memories.append(
                    {
                        "id": mem.get("id"),
                        "category": mem.get("category"),
                        "content": content,
                        "worked": mem.get("worked"),
                    }
                )
            entry["top_memories"] = top_memories

        return entry

    def _prune_entries(self) -> None:
        """Remove old and excess entries."""
        now = datetime.now(timezone.utc)

        # Remove entries older than TTL
        if self._entry_ttl_seconds > 0:
            to_remove = []
            for file_path, entry in self._files.items():
                last_change = entry.get("last_change")
                if last_change:
                    try:
                        change_time = datetime.fromisoformat(
                            last_change.replace("Z", "+00:00")
                        )
                        age = (now - change_time).total_seconds()
                        if age > self._entry_ttl_seconds:
                            to_remove.append(file_path)
                    except (ValueError, TypeError):
                        pass

            for file_path in to_remove:
                del self._files[file_path]

        # Remove oldest entries if over limit
        if len(self._files) > self._max_entries:
            # Sort by last_change, oldest first
            sorted_files = sorted(
                self._files.items(),
                key=lambda x: x[1].get("last_change", ""),
            )
            excess = len(self._files) - self._max_entries
            for file_path, _ in sorted_files[:excess]:
                del self._files[file_path]

    def _build_stats(self) -> dict:
        """Build statistics summary."""
        total_memories = sum(
            entry.get("memory_count", 0) for entry in self._files.values()
        )
        files_with_warnings = sum(
            1 for entry in self._files.values() if entry.get("has_warnings")
        )
        files_with_failures = sum(
            1 for entry in self._files.values() if entry.get("has_failures")
        )

        return {
            "total_files": len(self._files),
            "total_memories": total_memories,
            "files_with_warnings": files_with_warnings,
            "files_with_failures": files_with_failures,
        }

    def _save_state(self) -> None:
        """Save state to poll file."""
        state = {
            "version": self.CURRENT_VERSION,
            "updated_at": self._last_updated.isoformat()
            if self._last_updated
            else None,
            "project_path": str(self._project_path) if self._project_path else None,
            "files": self._files,
            "stats": self._build_stats(),
        }

        with open(self._poll_path, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2)

    def _load_state(self) -> None:
        """Load state from poll file if it exists."""
        if not self._poll_path.exists():
            return

        try:
            with open(self._poll_path, encoding="utf-8") as f:
                state = json.load(f)

            # Check version compatibility
            if state.get("version", 0) != self.CURRENT_VERSION:
                logger.warning("Poll file version mismatch, starting fresh")
                return

            self._files = state.get("files", {})
            updated_at = state.get("updated_at")
            if updated_at:
                try:
                    self._last_updated = datetime.fromisoformat(
                        updated_at.replace("Z", "+00:00")
                    )
                except (ValueError, TypeError):
                    pass

            project_path = state.get("project_path")
            if project_path:
                self._project_path = Path(project_path)

            logger.debug(f"Loaded {len(self._files)} entries from poll file")

        except Exception as e:
            logger.warning(f"Failed to load poll file: {e}")
            self._files = {}

    def clear(self) -> None:
        """Clear all poll file data."""
        self._files = {}
        self._last_updated = None
        if self._poll_path.exists():
            self._poll_path.unlink()
        logger.info("Cleared poll file")

    def remove_file(self, file_path: Path) -> bool:
        """
        Remove a specific file entry from the poll.

        Args:
            file_path: The file to remove

        Returns:
            True if the file was removed, False if not found
        """
        key = str(file_path)
        if key in self._files:
            del self._files[key]
            self._save_state()
            return True
        return False

    def get_file_info(self, file_path: Path) -> dict | None:
        """
        Get the poll info for a specific file.

        Args:
            file_path: The file to look up

        Returns:
            File entry dictionary or None if not tracked
        """
        return self._files.get(str(file_path))

    def get_all_files(self) -> dict[str, dict]:
        """
        Get all tracked files.

        Returns:
            Dictionary mapping file paths to their entries
        """
        return self._files.copy()
