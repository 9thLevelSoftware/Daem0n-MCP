"""Tests for consult workflow dispatcher."""

import pytest

from daem0nmcp.workflows.errors import InvalidActionError, MissingParamError


class TestConsultImport:
    """Verify consult module is importable."""

    def test_module_imports(self):
        from daem0nmcp.workflows import consult

        assert hasattr(consult, "dispatch")
        assert hasattr(consult, "VALID_ACTIONS")

    def test_valid_actions_contents(self):
        from daem0nmcp.workflows.consult import VALID_ACTIONS

        expected = {
            "preflight",
            "recall",
            "recall_file",
            "recall_entity",
            "recall_hierarchical",
            "search",
            "check_rules",
            "compress",
        }
        assert expected == VALID_ACTIONS


class TestConsultValidation:
    """Verify action validation without hitting the database."""

    @pytest.mark.asyncio
    async def test_invalid_action_raises(self):
        from daem0nmcp.workflows.consult import dispatch

        with pytest.raises(InvalidActionError):
            await dispatch(action="nonexistent", project_path="/tmp")

    @pytest.mark.asyncio
    async def test_preflight_requires_description(self):
        from daem0nmcp.workflows.consult import dispatch

        with pytest.raises(MissingParamError) as exc_info:
            await dispatch(action="preflight", project_path="/tmp")
        assert exc_info.value.param == "description"

    @pytest.mark.asyncio
    async def test_recall_requires_topic(self):
        from daem0nmcp.workflows.consult import dispatch

        with pytest.raises(MissingParamError) as exc_info:
            await dispatch(action="recall", project_path="/tmp")
        assert exc_info.value.param == "topic"

    @pytest.mark.asyncio
    async def test_recall_file_requires_file_path(self):
        from daem0nmcp.workflows.consult import dispatch

        with pytest.raises(MissingParamError) as exc_info:
            await dispatch(action="recall_file", project_path="/tmp")
        assert exc_info.value.param == "file_path"

    @pytest.mark.asyncio
    async def test_recall_entity_requires_entity_name(self):
        from daem0nmcp.workflows.consult import dispatch

        with pytest.raises(MissingParamError) as exc_info:
            await dispatch(action="recall_entity", project_path="/tmp")
        assert exc_info.value.param == "entity_name"

    @pytest.mark.asyncio
    async def test_recall_hierarchical_requires_topic(self):
        from daem0nmcp.workflows.consult import dispatch

        with pytest.raises(MissingParamError) as exc_info:
            await dispatch(action="recall_hierarchical", project_path="/tmp")
        assert exc_info.value.param == "topic"

    @pytest.mark.asyncio
    async def test_search_requires_query(self):
        from daem0nmcp.workflows.consult import dispatch

        with pytest.raises(MissingParamError) as exc_info:
            await dispatch(action="search", project_path="/tmp")
        assert exc_info.value.param == "query"

    @pytest.mark.asyncio
    async def test_check_rules_requires_action_desc(self):
        from daem0nmcp.workflows.consult import dispatch

        with pytest.raises(MissingParamError) as exc_info:
            await dispatch(action="check_rules", project_path="/tmp")
        assert exc_info.value.param == "action_desc"

    @pytest.mark.asyncio
    async def test_compress_requires_compress_text(self):
        from daem0nmcp.workflows.consult import dispatch

        with pytest.raises(MissingParamError) as exc_info:
            await dispatch(action="compress", project_path="/tmp")
        assert exc_info.value.param == "compress_text"
