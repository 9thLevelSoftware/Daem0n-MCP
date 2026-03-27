"""Tests for commune workflow dispatcher."""

import pytest

from daem0nmcp.workflows.errors import InvalidActionError


class TestCommuneImport:
    """Verify commune module is importable."""

    def test_module_imports(self):
        from daem0nmcp.workflows import commune

        assert hasattr(commune, "dispatch")
        assert hasattr(commune, "VALID_ACTIONS")

    def test_valid_actions_contents(self):
        from daem0nmcp.workflows.commune import VALID_ACTIONS

        expected = {
            "briefing",
            "active_context",
            "triggers",
            "health",
            "covenant",
            "updates",
        }
        assert expected == VALID_ACTIONS


class TestCommuneValidation:
    """Verify action validation without hitting the database."""

    @pytest.mark.asyncio
    async def test_invalid_action_raises(self):
        from daem0nmcp.workflows.commune import dispatch

        with pytest.raises(InvalidActionError) as exc_info:
            await dispatch(action="nonexistent", project_path="/tmp")
        assert "nonexistent" in str(exc_info.value)
