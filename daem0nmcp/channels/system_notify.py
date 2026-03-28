"""
System Notification Channel - Desktop notifications via plyer.

Uses the plyer library to send cross-platform desktop notifications
when files with associated memories are modified.
"""

import logging

from daem0nmcp.watcher import WatcherNotification

logger = logging.getLogger(__name__)


class SystemNotifyChannel:
    """
    Notification channel that sends desktop system notifications.

    Uses plyer for cross-platform support (Windows, macOS, Linux).
    Notifications show the file name and a summary of associated memories.

    Example:
        ```python
        from daem0nmcp.channels import SystemNotifyChannel

        channel = SystemNotifyChannel(
            app_name="Daem0n-MCP",
            timeout=10  # Notification visible for 10 seconds
        )
        watcher = FileWatcher(..., channels=[channel])
        ```

    Note:
        On some platforms, you may need to grant notification permissions
        to the application. Check plyer documentation for platform-specific
        requirements.
    """

    def __init__(
        self,
        app_name: str = "Daem0n-MCP",
        timeout: int = 10,
        ticker: str | None = None,
    ):
        """
        Initialize the system notification channel.

        Args:
            app_name: Application name shown in notifications
            timeout: How long notification is visible (seconds, 0 = until dismissed)
            ticker: Optional ticker text for Android notifications
        """
        self._app_name = app_name
        self._timeout = timeout
        self._ticker = ticker or "Memory Alert"
        self._plyer_available = False
        self._notification = None

        # Try to import plyer
        try:
            from plyer import notification

            self._notification = notification
            self._plyer_available = True
            logger.debug("plyer notification module loaded")
        except ImportError:
            logger.warning(
                "plyer not available - system notifications disabled. "
                "Install with: pip install plyer"
            )
        except Exception as e:
            logger.warning(f"Could not initialize plyer: {e}")

    @property
    def is_available(self) -> bool:
        """Check if system notifications are available."""
        return self._plyer_available and self._notification is not None

    async def notify(self, notification: WatcherNotification) -> None:
        """
        Send a desktop notification about the file change.

        Args:
            notification: The watcher notification with file and memory info
        """
        if not self.is_available:
            logger.debug("System notifications not available, skipping")
            return

        if self._notification is None:
            return

        try:
            # Build notification title
            title = f"{self._app_name}: {notification.file_path.name}"

            # Build notification message
            message_parts = [notification.summary]

            # Add top memories (up to 2 for brevity)
            for mem in notification.memories[:2]:
                content = mem.get("content", "")
                if len(content) > 60:
                    content = content[:57] + "..."
                category = mem.get("category", "memory")
                message_parts.append(f"• [{category}] {content}")

            if len(notification.memories) > 2:
                remaining = len(notification.memories) - 2
                message_parts.append(f"... and {remaining} more")

            message = "\n".join(message_parts)

            # Send the notification
            # Note: plyer.notification.notify is synchronous but fast
            self._notification.notify(
                title=title,
                message=message,
                app_name=self._app_name,
                timeout=self._timeout,
                ticker=self._ticker,
            )

            logger.debug(f"System notification sent for: {notification.file_path.name}")

        except Exception as e:
            # Don't crash on notification failures - they're not critical
            logger.error(f"Failed to send system notification: {e}")
