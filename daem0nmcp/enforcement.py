"""
Enforcement module for Daem0nMCP.

Provides session state tracking and pre-commit enforcement logic.
"""

import hashlib
import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select

from .config import settings
from .database import DatabaseManager
from .models import Memory, SessionState

logger = logging.getLogger(__name__)


def get_session_id(project_path: str) -> str:
    """
    Generate a session ID based on project path and current hour.

    Sessions are bucketed by hour to group related work together.
    """
    repo_hash = hashlib.md5(project_path.encode()).hexdigest()[:8]
    hour_bucket = datetime.now().strftime("%Y%m%d%H")
    return f"{repo_hash}-{hour_bucket}"


class SessionManager:
    """
    Manages session state for enforcement tracking.

    Tracks:
    - Whether get_briefing was called
    - What context checks were performed
    - What decisions are pending outcomes
    """

    def __init__(self, db_manager: DatabaseManager):
        self.db = db_manager

    async def get_session_state(self, project_path: str) -> dict[str, Any] | None:
        """Get current session state as a dictionary."""
        session_id = get_session_id(project_path)

        async with self.db.get_session() as session:
            result = await session.execute(
                select(SessionState).where(SessionState.session_id == session_id)
            )
            state = result.scalar_one_or_none()

            if state is None:
                return None

            return {
                "session_id": state.session_id,
                "project_path": state.project_path,
                "briefed": state.briefed,
                "context_checks": state.context_checks
                or [],  # JSON column returns list
                "pending_decisions": state.pending_decisions or [],
                "last_activity": state.last_activity.isoformat()
                if state.last_activity
                else None,
            }

    async def mark_briefed(self, project_path: str) -> None:
        """Mark the session as briefed (get_briefing was called)."""
        session_id = get_session_id(project_path)

        async with self.db.get_session() as session:
            result = await session.execute(
                select(SessionState).where(SessionState.session_id == session_id)
            )
            state = result.scalar_one_or_none()

            if state is None:
                state = SessionState(
                    session_id=session_id,
                    project_path=project_path,
                    briefed=True,
                    context_checks=[],
                    pending_decisions=[],
                )
                session.add(state)
            else:
                state.briefed = True
                state.last_activity = datetime.now(timezone.utc)

    async def add_context_check(
        self,
        project_path: str,
        topic_or_file: str,
        timestamp: datetime | None = None,
    ) -> None:
        """
        Record that a context check was performed.

        Args:
            project_path: Project root path
            topic_or_file: What was checked (topic or file path)
            timestamp: When the check was performed (defaults to now)
        """
        session_id = get_session_id(project_path)
        check_time = timestamp or datetime.now(timezone.utc)

        async with self.db.get_session() as session:
            result = await session.execute(
                select(SessionState).where(SessionState.session_id == session_id)
            )
            state = result.scalar_one_or_none()

            check_entry = {
                "topic": topic_or_file,
                "timestamp": check_time.isoformat(),
            }

            if state is None:
                state = SessionState(
                    session_id=session_id,
                    project_path=project_path,
                    context_checks=[check_entry],
                    pending_decisions=[],
                )
                session.add(state)
            else:
                checks = list(state.context_checks or [])
                # Remove old entry for same topic if exists
                checks = [
                    c
                    for c in checks
                    if not (isinstance(c, dict) and c.get("topic") == topic_or_file)
                ]
                checks.append(check_entry)
                # Keep only last 20 checks to prevent unbounded growth
                state.context_checks = checks[-20:]
                state.last_activity = datetime.now(timezone.utc)

    async def has_recent_context_check(
        self,
        project_path: str,
        max_age_seconds: int = 300,
    ) -> bool:
        """
        Check if any context_check was performed within the TTL.

        Args:
            project_path: Project root path
            max_age_seconds: Maximum age for a check to be considered valid

        Returns:
            True if a valid recent check exists
        """
        state = await self.get_session_state(project_path)
        if state is None:
            return False

        context_checks = state.get("context_checks", [])
        if not context_checks:
            return False

        now = datetime.now(timezone.utc)
        cutoff = now - timedelta(seconds=max_age_seconds)

        for check in context_checks:
            if isinstance(check, dict) and "timestamp" in check:
                check_time = datetime.fromisoformat(check["timestamp"])
                if check_time > cutoff:
                    return True
            elif isinstance(check, str):
                # Legacy format - treat as valid (backwards compatibility)
                return True

        return False

    async def add_pending_decision(self, project_path: str, memory_id: int) -> None:
        """Record that a decision was made but not yet outcome-recorded."""
        session_id = get_session_id(project_path)

        async with self.db.get_session() as session:
            result = await session.execute(
                select(SessionState).where(SessionState.session_id == session_id)
            )
            state = result.scalar_one_or_none()

            if state is None:
                state = SessionState(
                    session_id=session_id,
                    project_path=project_path,
                    pending_decisions=[memory_id],
                    context_checks=[],
                )
                session.add(state)
            else:
                pending = list(state.pending_decisions or [])
                if memory_id not in pending:
                    pending.append(memory_id)
                    state.pending_decisions = pending
                state.last_activity = datetime.now(timezone.utc)

    async def remove_pending_decision(self, project_path: str, memory_id: int) -> None:
        """Remove a decision from pending (outcome was recorded)."""
        session_id = get_session_id(project_path)

        async with self.db.get_session() as session:
            result = await session.execute(
                select(SessionState).where(SessionState.session_id == session_id)
            )
            state = result.scalar_one_or_none()

            if state is not None:
                pending = list(state.pending_decisions or [])
                if memory_id in pending:
                    pending.remove(memory_id)
                    state.pending_decisions = pending
                state.last_activity = datetime.now(timezone.utc)


class PreCommitChecker:
    """
    Validates staged files before commit.

    Checks for:
    - Pending decisions without outcomes (blocks if >threshold hours old, warns if recent)
    - Files with failed approaches (worked=False)
    - Files with warning memories

    The threshold is configurable via DAEM0NMCP_PENDING_DECISION_THRESHOLD_HOURS
    environment variable (default: 24 hours).
    """

    @property
    def pending_threshold(self) -> timedelta:
        """Get the configurable pending decision threshold."""
        return timedelta(hours=settings.pending_decision_threshold_hours)

    def __init__(self, db_manager: DatabaseManager, memory_manager):
        """
        Initialize the pre-commit checker.

        Args:
            db_manager: Database manager instance
            memory_manager: Memory manager instance
        """
        self.db = db_manager
        self.memory = memory_manager

    async def check(self, staged_files: list[str], project_path: str) -> dict[str, Any]:
        """
        Check staged files for issues before commit.

        Args:
            staged_files: List of file paths being committed
            project_path: Project root path

        Returns:
            Dict with:
            - can_commit: bool - whether commit should proceed
            - blocks: List of blocking issues
            - warnings: List of non-blocking warnings
        """
        blocks = []
        warnings = []

        # Check for pending decisions without outcomes
        async with self.db.get_session() as session:
            result = await session.execute(
                select(Memory).where(
                    Memory.category == "decision",
                    Memory.outcome.is_(None),
                    Memory.worked.is_(None),
                )
            )
            pending_decisions = result.scalars().all()

        now = datetime.now(timezone.utc)
        for decision in pending_decisions:
            # Handle both naive and aware datetimes from DB
            decision_time = decision.created_at
            if decision_time.tzinfo is None:
                decision_time = decision_time.replace(tzinfo=timezone.utc)
            age = now - decision_time
            if age > self.pending_threshold:
                age_hours = int(age.total_seconds() / 3600)
                blocks.append(
                    {
                        "type": "PENDING_DECISION_OLD",
                        "memory_id": decision.id,
                        "content": decision.content,
                        "message": f"Decision #{decision.id} from {age_hours}h ago needs outcome: {decision.content[:80]}",
                    }
                )
            else:
                warnings.append(
                    {
                        "type": "PENDING_DECISION_RECENT",
                        "memory_id": decision.id,
                        "message": f"Decision #{decision.id} needs outcome: {decision.content[:80]}",
                    }
                )

        # Check each staged file for failed approaches and warnings
        for file_path in staged_files:
            try:
                file_memories = await self.memory.recall_for_file(
                    file_path=file_path, project_path=project_path
                )
            except Exception as e:
                logger.warning(f"Could not check memories for {file_path}: {e}")
                continue  # Skip this file but continue checking others

            # Check for failed approaches (worked=False)
            for category in ["decisions", "learnings", "warnings", "patterns"]:
                for mem in file_memories.get(category, []):
                    if mem.get("worked") is False:
                        blocks.append(
                            {
                                "type": "FAILED_APPROACH",
                                "memory_id": mem.get("id"),
                                "content": mem.get("content", ""),
                                "message": f"File {file_path} has failed approach: {mem.get('content', '')[:80]}",
                            }
                        )

            # Check for warning memories (not already failed)
            for mem in file_memories.get("warnings", []):
                if (
                    mem.get("worked") is not False
                ):  # Don't duplicate failed approach warnings
                    warnings.append(
                        {
                            "type": "FILE_WARNING",
                            "memory_id": mem.get("id"),
                            "message": f"Warning for {file_path}: {mem.get('content', '')[:80]}",
                        }
                    )

        return {"can_commit": len(blocks) == 0, "blocks": blocks, "warnings": warnings}
