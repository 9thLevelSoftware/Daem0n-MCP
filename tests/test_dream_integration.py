"""Integration tests for dream scheduler wiring into middleware, server, and briefing.

Covers:
- CovenantMiddleware dream scheduler hook (set, notify, on_call_tool)
- Scheduler lifecycle in server composition root
- Briefing dashboard dream_sessions section
- _build_briefing_message dream info inclusion
"""

import asyncio
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from daem0nmcp.dreaming.scheduler import IdleDreamScheduler
from daem0nmcp.transforms.covenant import (
    _FASTMCP_MIDDLEWARE_AVAILABLE,
    CovenantMiddleware,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_get_state(briefed=True, context_checks=None):
    """Return a get_state callback for CovenantMiddleware."""
    state = {
        "briefed": briefed,
        "context_checks": context_checks
        or [{"timestamp": datetime.now(timezone.utc).isoformat()}],
    }
    return lambda _path: state


def _make_mock_context(tool_name="remember", project_path="/test"):
    """Create a mock MiddlewareContext for on_call_tool."""
    context = MagicMock()
    context.message.name = tool_name
    context.message.arguments = {"project_path": project_path}
    return context


# ---------------------------------------------------------------------------
# CovenantMiddleware dream scheduler hook tests
# ---------------------------------------------------------------------------


class TestCovenantMiddlewareDreamSchedulerHook:
    def test_middleware_stores_dream_scheduler(self):
        """CovenantMiddleware should store the dream_scheduler argument."""
        mock_scheduler = MagicMock(spec=IdleDreamScheduler)
        mw = CovenantMiddleware(
            get_state=_make_get_state(),
            dream_scheduler=mock_scheduler,
        )
        assert mw._dream_scheduler is mock_scheduler

    def test_middleware_default_no_scheduler(self):
        """Without dream_scheduler arg, _dream_scheduler should be None."""
        mw = CovenantMiddleware(get_state=_make_get_state())
        assert mw._dream_scheduler is None

    def test_set_dream_scheduler(self):
        """set_dream_scheduler should update the stored scheduler."""
        mw = CovenantMiddleware(get_state=_make_get_state())
        assert mw._dream_scheduler is None

        mock_scheduler = MagicMock(spec=IdleDreamScheduler)
        mw.set_dream_scheduler(mock_scheduler)
        assert mw._dream_scheduler is mock_scheduler

        # Clear it
        mw.set_dream_scheduler(None)
        assert mw._dream_scheduler is None


@pytest.mark.skipif(
    not _FASTMCP_MIDDLEWARE_AVAILABLE,
    reason="FastMCP 3.0 middleware not available",
)
class TestMiddlewareOnCallToolNotify:
    @pytest.mark.asyncio
    async def test_on_call_tool_calls_notify(self):
        """on_call_tool should call notify_tool_call() on the scheduler."""
        mock_scheduler = MagicMock(spec=IdleDreamScheduler)
        mock_scheduler.notify_tool_call = MagicMock()

        mw = CovenantMiddleware(
            get_state=_make_get_state(),
            dream_scheduler=mock_scheduler,
        )

        context = _make_mock_context(tool_name="recall")
        call_next = AsyncMock(return_value=MagicMock())

        await mw.on_call_tool(context, call_next)

        mock_scheduler.notify_tool_call.assert_called_once()

    @pytest.mark.asyncio
    async def test_notify_before_covenant_check(self):
        """notify_tool_call should be called even if covenant blocks the tool."""
        mock_scheduler = MagicMock(spec=IdleDreamScheduler)
        mock_scheduler.notify_tool_call = MagicMock()

        # Not briefed -- covenant will block "remember"
        mw = CovenantMiddleware(
            get_state=_make_get_state(briefed=False, context_checks=[]),
            dream_scheduler=mock_scheduler,
        )

        context = _make_mock_context(tool_name="remember")
        call_next = AsyncMock()

        await mw.on_call_tool(context, call_next)

        # Scheduler should have been notified even though tool was blocked
        mock_scheduler.notify_tool_call.assert_called_once()

        # call_next should NOT have been called (blocked)
        call_next.assert_not_called()

    @pytest.mark.asyncio
    async def test_no_scheduler_no_error(self):
        """on_call_tool should work fine when no scheduler is set."""
        mw = CovenantMiddleware(get_state=_make_get_state())

        context = _make_mock_context(tool_name="recall")
        call_next = AsyncMock(return_value=MagicMock())

        # Should not raise
        await mw.on_call_tool(context, call_next)
        call_next.assert_called_once()


# ---------------------------------------------------------------------------
# Scheduler auto-start on notify_tool_call
# ---------------------------------------------------------------------------


class TestSchedulerAutoStart:
    @pytest.mark.asyncio
    async def test_notify_auto_starts_scheduler(self):
        """notify_tool_call should auto-start scheduler if enabled but not running."""
        scheduler = IdleDreamScheduler(idle_timeout=60.0, enabled=True)
        assert not scheduler.is_running
        assert scheduler._monitor_task is None

        # Call notify -- should trigger auto-start
        scheduler.notify_tool_call()

        # Give the loop a tick to process the created task
        await asyncio.sleep(0.1)

        assert scheduler.is_running

        await scheduler.stop()


# ---------------------------------------------------------------------------
# Briefing dream_sessions tests
# ---------------------------------------------------------------------------


class TestFetchDreamSessions:
    @pytest.mark.asyncio
    async def test_returns_grouped_results(self):
        """_fetch_dream_sessions should group memories by session_id."""
        from daem0nmcp.tools.briefing import _fetch_dream_sessions

        # Create mock memories with dream tags
        mem1 = MagicMock()
        mem1.context = {
            "dream_session_id": "sess-001",
            "decisions_reviewed": 3,
            "insights_generated": 2,
            "interrupted": False,
            "source_decision_id": 10,
            "re_evaluation_result": "revised",
        }
        mem1.tags = ["dream", "re-evaluation"]
        mem1.content = "Dream re-evaluation: decision #10 revised"
        mem1.created_at = datetime(2026, 1, 30, 12, 0, 0, tzinfo=timezone.utc)

        mem2 = MagicMock()
        mem2.context = {
            "dream_session_id": "sess-001",
            "source_decision_id": 20,
            "re_evaluation_result": "confirmed_failure",
        }
        mem2.tags = ["dream", "re-evaluation"]
        mem2.content = "Dream re-evaluation: decision #20 confirmed failure"
        mem2.created_at = datetime(2026, 1, 30, 12, 1, 0, tzinfo=timezone.utc)

        # Mock context with db session
        ctx = MagicMock()
        mock_session = AsyncMock()
        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = [mem1, mem2]
        mock_session.execute = AsyncMock(return_value=mock_result)

        async_cm = AsyncMock()
        async_cm.__aenter__ = AsyncMock(return_value=mock_session)
        async_cm.__aexit__ = AsyncMock(return_value=False)
        ctx.db_manager.get_session = MagicMock(return_value=async_cm)

        result = await _fetch_dream_sessions(ctx, limit=5)

        assert len(result) == 1
        assert result[0]["session_id"] == "sess-001"
        assert result[0]["decisions_reviewed"] == 3
        assert len(result[0]["insights"]) == 2

    @pytest.mark.asyncio
    async def test_empty_when_no_dreams(self):
        """_fetch_dream_sessions should return empty list with no dream memories."""
        from daem0nmcp.tools.briefing import _fetch_dream_sessions

        ctx = MagicMock()
        mock_session = AsyncMock()
        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = []
        mock_session.execute = AsyncMock(return_value=mock_result)

        async_cm = AsyncMock()
        async_cm.__aenter__ = AsyncMock(return_value=mock_session)
        async_cm.__aexit__ = AsyncMock(return_value=False)
        ctx.db_manager.get_session = MagicMock(return_value=async_cm)

        result = await _fetch_dream_sessions(ctx, limit=5)
        assert result == []

    @pytest.mark.asyncio
    async def test_exception_returns_empty(self):
        """_fetch_dream_sessions should return empty list on error."""
        from daem0nmcp.tools.briefing import _fetch_dream_sessions

        ctx = MagicMock()
        ctx.db_manager.get_session = MagicMock(side_effect=RuntimeError("db error"))

        result = await _fetch_dream_sessions(ctx, limit=5)
        assert result == []


# ---------------------------------------------------------------------------
# _build_briefing_message dream info tests
# ---------------------------------------------------------------------------


class TestBuildBriefingMessageDream:
    def test_includes_dream_info_when_sessions_have_insights(self):
        """[DREAM] should appear in message when dream_sessions have insights."""
        from daem0nmcp.tools.briefing import _build_briefing_message

        dream_sessions = [
            {
                "session_id": "s1",
                "decisions_reviewed": 3,
                "insights_generated": 2,
                "insights": [],
            }
        ]

        message = _build_briefing_message(
            stats={"total_memories": 42},
            bootstrap_result=None,
            failed_approaches=[],
            active_warnings=[],
            git_changes=None,
            dream_sessions=dream_sessions,
        )

        assert "[DREAM]" in message
        assert "3 decisions" in message
        assert "2 insight(s)" in message

    def test_no_dream_info_when_empty(self):
        """[DREAM] should NOT appear when dream_sessions is empty."""
        from daem0nmcp.tools.briefing import _build_briefing_message

        message = _build_briefing_message(
            stats={"total_memories": 10},
            bootstrap_result=None,
            failed_approaches=[],
            active_warnings=[],
            git_changes=None,
            dream_sessions=[],
        )

        assert "[DREAM]" not in message

    def test_no_dream_info_when_none(self):
        """[DREAM] should NOT appear when dream_sessions is None."""
        from daem0nmcp.tools.briefing import _build_briefing_message

        message = _build_briefing_message(
            stats={"total_memories": 10},
            bootstrap_result=None,
            failed_approaches=[],
            active_warnings=[],
            git_changes=None,
            dream_sessions=None,
        )

        assert "[DREAM]" not in message

    def test_no_dream_info_when_zero_insights(self):
        """[DREAM] should NOT appear when sessions exist but have 0 insights."""
        from daem0nmcp.tools.briefing import _build_briefing_message

        dream_sessions = [
            {
                "session_id": "s1",
                "decisions_reviewed": 2,
                "insights_generated": 0,
                "insights": [],
            }
        ]

        message = _build_briefing_message(
            stats={"total_memories": 10},
            bootstrap_result=None,
            failed_approaches=[],
            active_warnings=[],
            git_changes=None,
            dream_sessions=dream_sessions,
        )

        assert "[DREAM]" not in message


# ---------------------------------------------------------------------------
# get_briefing includes dream_sessions key
# ---------------------------------------------------------------------------


class TestBriefingIncludesDreamSessions:
    @pytest.mark.asyncio
    async def test_briefing_result_has_dream_sessions_key(self):
        """get_briefing result dict should include 'dream_sessions' key."""
        from daem0nmcp.tools.briefing import get_briefing

        with (
            patch("daem0nmcp.tools.briefing.get_project_context") as mock_get_ctx,
            patch("daem0nmcp.tools.briefing._fetch_recent_context") as mock_recent,
            patch("daem0nmcp.tools.briefing._get_git_changes", return_value=None),
            patch(
                "daem0nmcp.tools.briefing._get_linked_projects_summary",
                new_callable=AsyncMock,
                return_value=[],
            ),
            patch(
                "daem0nmcp.tools.briefing._fetch_dream_sessions",
                new_callable=AsyncMock,
                return_value=[],
            ),
        ):
            # Setup mock context
            mock_ctx = MagicMock()
            mock_ctx.project_path = "/test/project"
            mock_ctx.briefed = False
            mock_ctx.context_checks = []
            mock_ctx.memory_manager.get_statistics = AsyncMock(
                return_value={
                    "total_memories": 5,
                    "by_category": {},
                }
            )
            mock_ctx.db_manager = MagicMock()

            mock_get_ctx.return_value = mock_ctx

            mock_recent.return_value = {
                "last_memory_date": None,
                "recent_decisions": [],
                "total_decisions": 0,
                "active_warnings": [],
                "total_warnings": 0,
                "failed_approaches": [],
                "total_failed": 0,
                "top_rules": [],
                "drill_down": "",
            }

            # Mock active context manager
            with patch(
                "daem0nmcp.tools.briefing.ActiveContextManager", create=True
            ) as mock_acm_cls:
                mock_acm = MagicMock()
                mock_acm.get_active_context = AsyncMock(
                    return_value={"count": 0, "items": []}
                )
                mock_acm.cleanup_expired = AsyncMock()
                mock_acm_cls.return_value = mock_acm

                result = await get_briefing(project_path="/test/project")

            assert "dream_sessions" in result
            assert result["dream_sessions"] == []
