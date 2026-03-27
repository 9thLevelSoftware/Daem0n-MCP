"""Tests for maintain workflow dispatcher."""

import pytest

from daem0nmcp.workflows.errors import InvalidActionError, MissingParamError


class TestMaintainImport:
    """Verify maintain module is importable."""

    def test_module_imports(self):
        from daem0nmcp.workflows import maintain

        assert hasattr(maintain, "dispatch")
        assert hasattr(maintain, "VALID_ACTIONS")

    def test_valid_actions_contents(self):
        from daem0nmcp.workflows.maintain import VALID_ACTIONS

        expected = {
            "prune",
            "archive",
            "cleanup",
            "compact",
            "rebuild_index",
            "export",
            "import_data",
            "link_project",
            "unlink_project",
            "list_projects",
            "consolidate",
            "purge_dream_spam",
        }
        assert expected == VALID_ACTIONS


class TestMaintainValidation:
    """Verify action validation without hitting the database."""

    @pytest.mark.asyncio
    async def test_invalid_action_raises(self):
        from daem0nmcp.workflows.maintain import dispatch

        with pytest.raises(InvalidActionError):
            await dispatch(action="nonexistent", project_path="/tmp")

    @pytest.mark.asyncio
    async def test_archive_requires_memory_id(self):
        from daem0nmcp.workflows.maintain import dispatch

        with pytest.raises(MissingParamError) as exc_info:
            await dispatch(action="archive", project_path="/tmp")
        assert exc_info.value.param == "memory_id"

    @pytest.mark.asyncio
    async def test_compact_requires_summary(self):
        from daem0nmcp.workflows.maintain import dispatch

        with pytest.raises(MissingParamError) as exc_info:
            await dispatch(action="compact", project_path="/tmp")
        assert exc_info.value.param == "summary"

    @pytest.mark.asyncio
    async def test_import_data_requires_data(self):
        from daem0nmcp.workflows.maintain import dispatch

        with pytest.raises(MissingParamError) as exc_info:
            await dispatch(action="import_data", project_path="/tmp")
        assert exc_info.value.param == "data"

    @pytest.mark.asyncio
    async def test_link_project_requires_linked_path(self):
        from daem0nmcp.workflows.maintain import dispatch

        with pytest.raises(MissingParamError) as exc_info:
            await dispatch(action="link_project", project_path="/tmp")
        assert exc_info.value.param == "linked_path"

    @pytest.mark.asyncio
    async def test_unlink_project_requires_linked_path(self):
        from daem0nmcp.workflows.maintain import dispatch

        with pytest.raises(MissingParamError) as exc_info:
            await dispatch(action="unlink_project", project_path="/tmp")
        assert exc_info.value.param == "linked_path"
