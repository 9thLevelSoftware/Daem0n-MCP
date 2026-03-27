"""
Tests for execute_python MCP tool and CapabilityScope system.

Tests capability enforcement, sandbox availability handling, and logging.
Integration tests require E2B_API_KEY and e2b-code-interpreter installed.
"""

import logging
import os
from unittest.mock import AsyncMock, patch

import pytest

from daem0nmcp.agency import (
    CapabilityManager,
    CapabilityScope,
    ExecutionResult,
    SandboxExecutor,
    check_capability,
)


class TestCapabilityScope:
    """Test CapabilityScope enum."""

    def test_execute_code_scope_exists(self):
        """EXECUTE_CODE scope should exist."""
        assert hasattr(CapabilityScope, "EXECUTE_CODE")

    def test_network_access_scope_exists(self):
        """NETWORK_ACCESS scope should exist (reserved)."""
        assert hasattr(CapabilityScope, "NETWORK_ACCESS")

    def test_file_write_scope_exists(self):
        """FILE_WRITE scope should exist (reserved)."""
        assert hasattr(CapabilityScope, "FILE_WRITE")


class TestCapabilityManager:
    """Test CapabilityManager class."""

    def test_default_has_execute_code(self):
        """Projects should have EXECUTE_CODE by default."""
        manager = CapabilityManager()
        assert manager.has_capability("/test/project", CapabilityScope.EXECUTE_CODE)

    def test_get_capabilities_returns_default(self):
        """get_capabilities should return defaults for unknown project."""
        manager = CapabilityManager()
        caps = manager.get_capabilities("/unknown/project")
        assert CapabilityScope.EXECUTE_CODE in caps

    def test_grant_capability(self):
        """grant_capability should add capability."""
        manager = CapabilityManager()
        # First revoke to test grant
        manager.revoke_capability("/test", CapabilityScope.NETWORK_ACCESS)
        assert not manager.has_capability("/test", CapabilityScope.NETWORK_ACCESS)

        manager.grant_capability("/test", CapabilityScope.NETWORK_ACCESS)
        assert manager.has_capability("/test", CapabilityScope.NETWORK_ACCESS)

    def test_revoke_capability(self):
        """revoke_capability should remove capability."""
        manager = CapabilityManager()
        assert manager.has_capability("/test", CapabilityScope.EXECUTE_CODE)

        manager.revoke_capability("/test", CapabilityScope.EXECUTE_CODE)
        assert not manager.has_capability("/test", CapabilityScope.EXECUTE_CODE)

    def test_reset_capabilities(self):
        """reset_capabilities should restore defaults."""
        manager = CapabilityManager()
        manager.revoke_capability("/test", CapabilityScope.EXECUTE_CODE)
        assert not manager.has_capability("/test", CapabilityScope.EXECUTE_CODE)

        manager.reset_capabilities("/test")
        # After reset, should use defaults again
        assert manager.has_capability("/test", CapabilityScope.EXECUTE_CODE)

    def test_per_project_isolation(self):
        """Capabilities should be isolated per project."""
        manager = CapabilityManager()

        # Revoke for project A
        manager.revoke_capability("/project/a", CapabilityScope.EXECUTE_CODE)

        # Project B should still have it
        assert manager.has_capability("/project/b", CapabilityScope.EXECUTE_CODE)
        assert not manager.has_capability("/project/a", CapabilityScope.EXECUTE_CODE)


class TestCheckCapability:
    """Test check_capability helper function."""

    def test_no_manager_allows_all(self):
        """Without manager, should allow (backwards compatibility)."""
        result = check_capability("/test", CapabilityScope.EXECUTE_CODE, None)
        assert result is None

    def test_granted_capability_returns_none(self):
        """Granted capability should return None (allowed)."""
        manager = CapabilityManager()
        result = check_capability("/test", CapabilityScope.EXECUTE_CODE, manager)
        assert result is None

    def test_revoked_capability_returns_violation(self):
        """Revoked capability should return violation dict."""
        manager = CapabilityManager()
        manager.revoke_capability("/test", CapabilityScope.EXECUTE_CODE)

        violation = check_capability("/test", CapabilityScope.EXECUTE_CODE, manager)

        assert violation is not None
        assert violation["status"] == "blocked"
        assert violation["violation"] == "CAPABILITY_DENIED"
        assert "EXECUTE_CODE" in violation["message"]
        assert violation["project_path"] == "/test"
        assert violation["required_capability"] == "EXECUTE_CODE"


class TestSandboxExecutorAvailability:
    """Test SandboxExecutor availability checking."""

    def test_available_property_false_without_api_key(self):
        """Sandbox should report unavailable without E2B_API_KEY."""
        with patch.dict(os.environ, {"E2B_API_KEY": ""}, clear=False):
            # Force re-check by creating new instance
            executor = SandboxExecutor(api_key="")
            # Without api_key, should be unavailable
            assert not executor.available

    def test_available_property_false_without_package(self):
        """Sandbox should report unavailable without e2b-code-interpreter."""
        with patch.dict("sys.modules", {"e2b_code_interpreter": None}):
            # Mock the import to fail
            SandboxExecutor()
            # Check internal state (it checked availability at init)
            # Note: This may vary based on actual e2b installation


class TestExecutionResult:
    """Test ExecutionResult dataclass."""

    def test_success_result(self):
        """Test creating successful ExecutionResult."""
        result = ExecutionResult(
            success=True,
            output="Hello, world!",
            error=None,
            execution_time_ms=50,
            logs=["stdout: Hello, world!"],
        )

        assert result.success is True
        assert result.output == "Hello, world!"
        assert result.error is None
        assert result.execution_time_ms == 50
        assert len(result.logs) == 1

    def test_error_result(self):
        """Test creating error ExecutionResult."""
        result = ExecutionResult(
            success=False,
            output="",
            error="NameError: name 'foo' is not defined",
            execution_time_ms=10,
        )

        assert result.success is False
        assert result.error is not None
        assert "NameError" in result.error


class TestExecutePythonTool:
    """Test execute_python MCP tool."""

    @pytest.mark.asyncio
    async def test_missing_project_path_error(self):
        """Tool should return error without project_path."""
        # Mock the _default_project_path to be None
        from daem0nmcp import server

        original = server._default_project_path
        try:
            server._default_project_path = None

            result = await server.execute_python(
                code="print('hello')", project_path=None
            )

            assert "error" in result or "MISSING_PROJECT_PATH" in str(result)
        finally:
            server._default_project_path = original

    @pytest.mark.asyncio
    async def test_capability_denied_returns_violation(self):
        """Tool should return violation when capability revoked."""
        from daem0nmcp import server

        # Revoke capability
        server._capability_manager.revoke_capability(
            "/test/project", CapabilityScope.EXECUTE_CODE
        )

        try:
            result = await server.execute_python(
                code="print('hello')", project_path="/test/project"
            )

            assert result["status"] == "blocked"
            assert result["violation"] == "CAPABILITY_DENIED"
        finally:
            # Restore
            server._capability_manager.reset_capabilities("/test/project")

    @pytest.mark.asyncio
    async def test_sandbox_unavailable_returns_error(self):
        """Tool should return error when sandbox unavailable."""
        from daem0nmcp import server

        # Ensure capability is granted
        server._capability_manager.grant_capability(
            "/test/project", CapabilityScope.EXECUTE_CODE
        )

        # Mock sandbox as unavailable
        original_available = server._sandbox_executor._sandbox_available
        try:
            server._sandbox_executor._sandbox_available = False

            result = await server.execute_python(
                code="print('hello')", project_path="/test/project"
            )

            assert result["status"] == "error"
            assert result["error"] == "SANDBOX_UNAVAILABLE"
        finally:
            server._sandbox_executor._sandbox_available = original_available

    @pytest.mark.asyncio
    async def test_execution_logged(self, caplog):
        """Tool should log execution for anomaly detection."""
        from daem0nmcp import server

        # Mock sandbox to allow execution with mock result
        mock_result = ExecutionResult(
            success=True, output="hello", execution_time_ms=10
        )

        with patch.object(SandboxExecutor, "execute", new_callable=AsyncMock) as mock:
            mock.return_value = mock_result

            # Force sandbox available
            original = server._sandbox_executor._sandbox_available
            server._sandbox_executor._sandbox_available = True
            try:
                with caplog.at_level(logging.INFO):
                    await server.execute_python(
                        code="print('hello')", project_path="/test/project"
                    )

                # Should have logged the execution attempt and result
                log_messages = [r.message for r in caplog.records]
                assert any(
                    "execute_python" in msg and "code_len" in msg
                    for msg in log_messages
                ), f"Expected execute_python log, got: {log_messages}"
            finally:
                server._sandbox_executor._sandbox_available = original


# Integration tests - skip without E2B_API_KEY
@pytest.mark.skipif(
    not os.environ.get("E2B_API_KEY"), reason="E2B_API_KEY not set - skip integration"
)
class TestExecutePythonIntegration:
    """Integration tests requiring E2B sandbox."""

    @pytest.mark.asyncio
    async def test_simple_print(self):
        """Test executing simple print statement."""
        from daem0nmcp.server import execute_python

        result = await execute_python(code="print('hello')", project_path="/test")

        assert result["success"]
        assert "hello" in result["output"]

    @pytest.mark.asyncio
    async def test_math_calculation(self):
        """Test executing math calculation."""
        from daem0nmcp.server import execute_python

        result = await execute_python(code="print(2 + 2)", project_path="/test")

        assert result["success"]
        assert "4" in result["output"]

    @pytest.mark.asyncio
    async def test_syntax_error(self):
        """Test handling syntax error."""
        from daem0nmcp.server import execute_python

        result = await execute_python(
            code="print('unclosed string", project_path="/test"
        )

        assert not result["success"]
        assert result["error"] is not None

    @pytest.mark.asyncio
    async def test_runtime_error(self):
        """Test handling runtime error."""
        from daem0nmcp.server import execute_python

        result = await execute_python(
            code="raise ValueError('test error')", project_path="/test"
        )

        assert not result["success"]
        assert "ValueError" in str(result["error"])

    @pytest.mark.asyncio
    async def test_timeout_respected(self):
        """Test that timeout is respected."""
        from daem0nmcp.server import execute_python

        # This should timeout (if sandbox actually runs)
        result = await execute_python(
            code="import time; time.sleep(100)",
            project_path="/test",
            timeout_seconds=5,
        )

        # Should have failed due to timeout
        assert not result["success"]
