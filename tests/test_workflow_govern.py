"""Tests for govern workflow dispatcher."""

import pytest

from daem0nmcp.workflows.errors import InvalidActionError, MissingParamError


class TestGovernImport:
    """Verify govern module is importable."""

    def test_module_imports(self):
        from daem0nmcp.workflows import govern

        assert hasattr(govern, "dispatch")
        assert hasattr(govern, "VALID_ACTIONS")

    def test_valid_actions_contents(self):
        from daem0nmcp.workflows.govern import VALID_ACTIONS

        expected = {
            "add_rule",
            "update_rule",
            "list_rules",
            "add_trigger",
            "list_triggers",
            "remove_trigger",
        }
        assert expected == VALID_ACTIONS


class TestGovernValidation:
    """Verify action validation without hitting the database."""

    @pytest.mark.asyncio
    async def test_invalid_action_raises(self):
        from daem0nmcp.workflows.govern import dispatch

        with pytest.raises(InvalidActionError):
            await dispatch(action="nonexistent", project_path="/tmp")

    @pytest.mark.asyncio
    async def test_add_rule_requires_trigger(self):
        from daem0nmcp.workflows.govern import dispatch

        with pytest.raises(MissingParamError) as exc_info:
            await dispatch(action="add_rule", project_path="/tmp")
        assert exc_info.value.param == "trigger"

    @pytest.mark.asyncio
    async def test_update_rule_requires_rule_id(self):
        from daem0nmcp.workflows.govern import dispatch

        with pytest.raises(MissingParamError) as exc_info:
            await dispatch(action="update_rule", project_path="/tmp")
        assert exc_info.value.param == "rule_id"

    @pytest.mark.asyncio
    async def test_add_trigger_requires_trigger_type(self):
        from daem0nmcp.workflows.govern import dispatch

        with pytest.raises(MissingParamError) as exc_info:
            await dispatch(action="add_trigger", project_path="/tmp")
        assert exc_info.value.param == "trigger_type"

    @pytest.mark.asyncio
    async def test_add_trigger_requires_pattern(self):
        from daem0nmcp.workflows.govern import dispatch

        with pytest.raises(MissingParamError) as exc_info:
            await dispatch(
                action="add_trigger",
                project_path="/tmp",
                trigger_type="file_pattern",
            )
        assert exc_info.value.param == "pattern"

    @pytest.mark.asyncio
    async def test_add_trigger_requires_recall_topic(self):
        from daem0nmcp.workflows.govern import dispatch

        with pytest.raises(MissingParamError) as exc_info:
            await dispatch(
                action="add_trigger",
                project_path="/tmp",
                trigger_type="file_pattern",
                pattern="*.py",
            )
        assert exc_info.value.param == "recall_topic"

    @pytest.mark.asyncio
    async def test_remove_trigger_requires_trigger_id(self):
        from daem0nmcp.workflows.govern import dispatch

        with pytest.raises(MissingParamError) as exc_info:
            await dispatch(action="remove_trigger", project_path="/tmp")
        assert exc_info.value.param == "trigger_id"
