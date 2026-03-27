"""
UI-related tools for MCP Apps integration.

Provides tools for real-time update detection and UI refresh coordination.
"""

from datetime import datetime, timezone
from typing import Any

from ..database import DatabaseManager

__all__ = ["check_for_updates"]


async def check_for_updates(
    db: DatabaseManager,
    since: str | None = None,
    interval_seconds: int = 10,
) -> dict[str, Any]:
    """
    Check if database has changes since the given timestamp.

    Used by MCP hosts to implement polling-based real-time updates.
    The host calls this tool periodically and pushes 'data_updated'
    events to UI iframes via postMessage when changes are detected.

    Args:
        db: Database manager instance
        since: ISO 8601 timestamp to check changes from. If None,
               returns current state (always has_changes=True on first call).
        interval_seconds: Recommended polling interval in seconds (5-60).
                         Default 10s. Returned to help hosts configure polling.

    Returns:
        Dict with:
            - has_changes: bool - True if data changed since timestamp
            - last_update: str - ISO timestamp of most recent change
            - recommended_interval: int - Suggested polling interval
            - checked_at: str - ISO timestamp of this check

    Example:
        # First call (no timestamp)
        result = await check_for_updates(db)
        # {'has_changes': True, 'last_update': '2026-01-28T12:00:00Z', ...}

        # Subsequent calls (with timestamp)
        result = await check_for_updates(db, since=result['last_update'])
        # {'has_changes': False, ...} or {'has_changes': True, ...}
    """
    # Validate interval bounds (5-60 seconds per research)
    interval_seconds = max(5, min(60, interval_seconds))

    # Parse since timestamp if provided
    since_dt: datetime | None = None
    if since:
        try:
            since_dt = datetime.fromisoformat(since.replace("Z", "+00:00"))
        except ValueError:
            # Invalid timestamp treated as None (check everything)
            since_dt = None

    # Check for changes — first call (no timestamp) always reports changes
    if since_dt is None:
        has_changes = True
    else:
        has_changes = await db.has_changes_since(since_dt)

    # Get current last update time
    last_update_dt = await db.get_last_update_time()
    last_update = last_update_dt.isoformat() if last_update_dt else None

    # Current check timestamp
    checked_at = datetime.now(timezone.utc).isoformat()

    return {
        "has_changes": has_changes,
        "last_update": last_update,
        "recommended_interval": interval_seconds,
        "checked_at": checked_at,
    }
