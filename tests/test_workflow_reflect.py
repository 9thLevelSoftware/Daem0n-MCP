"""Tests for reflect workflow dispatcher."""

import pytest

from daem0nmcp.workflows.errors import InvalidActionError, MissingParamError


class TestReflectImport:
    """Verify reflect module is importable."""

    def test_module_imports(self):
        from daem0nmcp.workflows import reflect

        assert hasattr(reflect, "dispatch")
        assert hasattr(reflect, "VALID_ACTIONS")

    def test_valid_actions_contents(self):
        from daem0nmcp.workflows.reflect import VALID_ACTIONS

        expected = {"outcome", "verify", "execute"}
        assert expected == VALID_ACTIONS


class TestReflectValidation:
    """Verify action validation without hitting the database."""

    @pytest.mark.asyncio
    async def test_invalid_action_raises(self):
        from daem0nmcp.workflows.reflect import dispatch

        with pytest.raises(InvalidActionError):
            await dispatch(action="nonexistent", project_path="/tmp")

    @pytest.mark.asyncio
    async def test_outcome_requires_memory_id(self):
        from daem0nmcp.workflows.reflect import dispatch

        with pytest.raises(MissingParamError) as exc_info:
            await dispatch(action="outcome", project_path="/tmp")
        assert exc_info.value.param == "memory_id"

    @pytest.mark.asyncio
    async def test_outcome_requires_outcome_text(self):
        from daem0nmcp.workflows.reflect import dispatch

        with pytest.raises(MissingParamError) as exc_info:
            await dispatch(action="outcome", project_path="/tmp", memory_id=1)
        assert exc_info.value.param == "outcome_text"

    @pytest.mark.asyncio
    async def test_outcome_requires_worked(self):
        from daem0nmcp.workflows.reflect import dispatch

        with pytest.raises(MissingParamError) as exc_info:
            await dispatch(
                action="outcome",
                project_path="/tmp",
                memory_id=1,
                outcome_text="it worked",
            )
        assert exc_info.value.param == "worked"

    @pytest.mark.asyncio
    async def test_verify_requires_text(self):
        from daem0nmcp.workflows.reflect import dispatch

        with pytest.raises(MissingParamError) as exc_info:
            await dispatch(action="verify", project_path="/tmp")
        assert exc_info.value.param == "text"

    @pytest.mark.asyncio
    async def test_execute_requires_code(self):
        from daem0nmcp.workflows.reflect import dispatch

        with pytest.raises(MissingParamError) as exc_info:
            await dispatch(action="execute", project_path="/tmp")
        assert exc_info.value.param == "code"
