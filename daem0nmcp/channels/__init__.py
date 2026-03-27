"""
Notification Channels for Daem0n-MCP File Watcher.

This package provides various notification channel implementations
for the proactive file watcher system.

Available Channels:
- SystemNotifyChannel: Desktop notifications via plyer
- LogFileChannel: Writes to a log file for external consumption
- EditorPollChannel: Creates a poll file for editor integration
- LoggingChannel: Simple logging (re-exported from watcher)
- CallbackChannel: Custom callback (re-exported from watcher)

Usage:
    from daem0nmcp.channels import SystemNotifyChannel, LogFileChannel
    from daem0nmcp.watcher import FileWatcher

    watcher = FileWatcher(
        project_path=Path("/my/project"),
        memory_manager=memory_manager,
        channels=[
            SystemNotifyChannel(),
            LogFileChannel(Path("/tmp/daem0n.log"))
        ]
    )
"""

from daem0nmcp.watcher import (
    CallbackChannel,
    LoggingChannel,
    NotificationChannel,
    WatcherNotification,
)

from .editor_poll import EditorPollChannel
from .log_notify import LogFileChannel
from .system_notify import SystemNotifyChannel

__all__ = [
    # Protocol and data
    "NotificationChannel",
    "WatcherNotification",
    # Channel implementations
    "SystemNotifyChannel",
    "LogFileChannel",
    "EditorPollChannel",
    "LoggingChannel",
    "CallbackChannel",
]
