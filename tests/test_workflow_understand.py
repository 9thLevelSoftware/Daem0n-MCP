"""Tests for understand workflow dispatcher."""

import pytest

from daem0nmcp.workflows.errors import InvalidActionError, MissingParamError


class TestUnderstandImport:
    """Verify understand module is importable."""

    def test_module_imports(self):
        from daem0nmcp.workflows import understand

        assert hasattr(understand, "dispatch")
        assert hasattr(understand, "VALID_ACTIONS")

    def test_valid_actions_contents(self):
        from daem0nmcp.workflows.understand import VALID_ACTIONS

        expected = {"index", "find", "impact", "todos", "refactor"}
        assert expected == VALID_ACTIONS


class TestUnderstandValidation:
    """Verify action validation without hitting the database."""

    @pytest.mark.asyncio
    async def test_invalid_action_raises(self):
        from daem0nmcp.workflows.understand import dispatch

        with pytest.raises(InvalidActionError):
            await dispatch(action="nonexistent", project_path="/tmp")

    @pytest.mark.asyncio
    async def test_find_requires_query(self):
        from daem0nmcp.workflows.understand import dispatch

        with pytest.raises(MissingParamError) as exc_info:
            await dispatch(action="find", project_path="/tmp")
        assert exc_info.value.param == "query"

    @pytest.mark.asyncio
    async def test_impact_requires_entity_name(self):
        from daem0nmcp.workflows.understand import dispatch

        with pytest.raises(MissingParamError) as exc_info:
            await dispatch(action="impact", project_path="/tmp")
        assert exc_info.value.param == "entity_name"

    @pytest.mark.asyncio
    async def test_refactor_requires_file_path(self):
        from daem0nmcp.workflows.understand import dispatch

        with pytest.raises(MissingParamError) as exc_info:
            await dispatch(action="refactor", project_path="/tmp")
        assert exc_info.value.param == "file_path"
