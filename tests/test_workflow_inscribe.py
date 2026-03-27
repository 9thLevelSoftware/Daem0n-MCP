"""Tests for inscribe workflow dispatcher."""

import pytest

from daem0nmcp.workflows.errors import InvalidActionError, MissingParamError


class TestInscribeImport:
    """Verify inscribe module is importable."""

    def test_module_imports(self):
        from daem0nmcp.workflows import inscribe

        assert hasattr(inscribe, "dispatch")
        assert hasattr(inscribe, "VALID_ACTIONS")

    def test_valid_actions_contents(self):
        from daem0nmcp.workflows.inscribe import VALID_ACTIONS

        expected = {
            "remember",
            "remember_batch",
            "link",
            "unlink",
            "pin",
            "activate",
            "deactivate",
            "clear_active",
            "ingest",
        }
        assert expected == VALID_ACTIONS


class TestInscribeValidation:
    """Verify action validation without hitting the database."""

    @pytest.mark.asyncio
    async def test_invalid_action_raises(self):
        from daem0nmcp.workflows.inscribe import dispatch

        with pytest.raises(InvalidActionError):
            await dispatch(action="nonexistent", project_path="/tmp")

    @pytest.mark.asyncio
    async def test_remember_requires_category(self):
        from daem0nmcp.workflows.inscribe import dispatch

        with pytest.raises(MissingParamError) as exc_info:
            await dispatch(action="remember", project_path="/tmp")
        assert exc_info.value.param == "category"

    @pytest.mark.asyncio
    async def test_remember_requires_content(self):
        from daem0nmcp.workflows.inscribe import dispatch

        with pytest.raises(MissingParamError) as exc_info:
            await dispatch(action="remember", project_path="/tmp", category="decision")
        assert exc_info.value.param == "content"

    @pytest.mark.asyncio
    async def test_remember_batch_requires_memories(self):
        from daem0nmcp.workflows.inscribe import dispatch

        with pytest.raises(MissingParamError) as exc_info:
            await dispatch(action="remember_batch", project_path="/tmp")
        assert exc_info.value.param == "memories"

    @pytest.mark.asyncio
    async def test_link_requires_source_id(self):
        from daem0nmcp.workflows.inscribe import dispatch

        with pytest.raises(MissingParamError) as exc_info:
            await dispatch(action="link", project_path="/tmp")
        assert exc_info.value.param == "source_id"

    @pytest.mark.asyncio
    async def test_link_requires_target_id(self):
        from daem0nmcp.workflows.inscribe import dispatch

        with pytest.raises(MissingParamError) as exc_info:
            await dispatch(action="link", project_path="/tmp", source_id=1)
        assert exc_info.value.param == "target_id"

    @pytest.mark.asyncio
    async def test_link_requires_relationship(self):
        from daem0nmcp.workflows.inscribe import dispatch

        with pytest.raises(MissingParamError) as exc_info:
            await dispatch(
                action="link",
                project_path="/tmp",
                source_id=1,
                target_id=2,
            )
        assert exc_info.value.param == "relationship"

    @pytest.mark.asyncio
    async def test_unlink_requires_source_id(self):
        from daem0nmcp.workflows.inscribe import dispatch

        with pytest.raises(MissingParamError) as exc_info:
            await dispatch(action="unlink", project_path="/tmp")
        assert exc_info.value.param == "source_id"

    @pytest.mark.asyncio
    async def test_unlink_requires_target_id(self):
        from daem0nmcp.workflows.inscribe import dispatch

        with pytest.raises(MissingParamError) as exc_info:
            await dispatch(action="unlink", project_path="/tmp", source_id=1)
        assert exc_info.value.param == "target_id"

    @pytest.mark.asyncio
    async def test_pin_requires_memory_id(self):
        from daem0nmcp.workflows.inscribe import dispatch

        with pytest.raises(MissingParamError) as exc_info:
            await dispatch(action="pin", project_path="/tmp")
        assert exc_info.value.param == "memory_id"

    @pytest.mark.asyncio
    async def test_activate_requires_memory_id(self):
        from daem0nmcp.workflows.inscribe import dispatch

        with pytest.raises(MissingParamError) as exc_info:
            await dispatch(action="activate", project_path="/tmp")
        assert exc_info.value.param == "memory_id"

    @pytest.mark.asyncio
    async def test_deactivate_requires_memory_id(self):
        from daem0nmcp.workflows.inscribe import dispatch

        with pytest.raises(MissingParamError) as exc_info:
            await dispatch(action="deactivate", project_path="/tmp")
        assert exc_info.value.param == "memory_id"

    @pytest.mark.asyncio
    async def test_ingest_requires_url(self):
        from daem0nmcp.workflows.inscribe import dispatch

        with pytest.raises(MissingParamError) as exc_info:
            await dispatch(action="ingest", project_path="/tmp")
        assert exc_info.value.param == "url"

    @pytest.mark.asyncio
    async def test_ingest_requires_topic(self):
        from daem0nmcp.workflows.inscribe import dispatch

        with pytest.raises(MissingParamError) as exc_info:
            await dispatch(
                action="ingest",
                project_path="/tmp",
                url="https://example.com",
            )
        assert exc_info.value.param == "topic"
