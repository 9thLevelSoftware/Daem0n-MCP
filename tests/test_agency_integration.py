"""
Integration tests for Agency module: Sandbox execution and capability management.

Tests requirements:
- AGENCY-03: execute_python tool provides sandboxed code execution
- AGENCY-04: Sandbox isolation via Firecracker microVMs (E2B)
- AGENCY-05: Capability-scoped access enforces least-privilege

These tests verify sandbox execution and capability scoping work correctly.
"""

import inspect
import logging
import os
from unittest.mock import MagicMock, patch

import pytest

from daem0nmcp.agency import (
    CapabilityManager,
    CapabilityScope,
    ExecutionResult,
    SandboxExecutor,
    check_capability,
)

# ============================================================================
# AGENCY-03: execute_python tool provides sandboxed code execution
# ============================================================================


class TestAgency03SandboxedExecution:
    """
    AGENCY-03: execute_python tool provides sandboxed code execution.

    Tests verify:
    - SandboxExecutor exists and is importable
    - ExecutionResult has required fields
    - Graceful degradation when unavailable
    """

    def test_sandbox_executor_exists(self):
        """SandboxExecutor class exists and is importable."""
        # AGENCY-03: Verify SandboxExecutor available
        executor = SandboxExecutor()
        assert executor is not None

    def test_execution_result_structure(self):
        """ExecutionResult has required fields."""
        # AGENCY-03: Verify result dataclass structure
        result = ExecutionResult(
            success=True,
            output="hello",
            error=None,
            execution_time_ms=100,
            logs=["log1"],
        )
        assert result.success is True
        assert result.output == "hello"
        assert result.error is None
        assert result.execution_time_ms == 100
        assert result.logs == ["log1"]

    def test_execution_result_defaults(self):
        """ExecutionResult has sensible defaults."""
        # AGENCY-03: Verify defaults
        result = ExecutionResult(success=False, output="")
        assert result.error is None
        assert result.execution_time_ms == 0
        assert result.logs == []

    @pytest.mark.asyncio
    async def test_unavailable_returns_error(self):
        """When sandbox unavailable, returns error (not exception)."""
        # AGENCY-03: Verify graceful degradation
        executor = SandboxExecutor()
        executor._sandbox_available = False

        result = await executor.execute("print('test')")

        assert result.success is False
        assert "not available" in result.error.lower()

    def test_sandbox_available_property(self):
        """Sandbox has available property for runtime check."""
        # AGENCY-03: Verify availability check
        executor = SandboxExecutor()
        assert isinstance(executor.available, bool)

    @pytest.mark.asyncio
    async def test_execute_returns_execution_result(self):
        """execute() always returns ExecutionResult."""
        # AGENCY-03: Verify return type
        executor = SandboxExecutor()
        executor._sandbox_available = False

        result = await executor.execute("any code")
        assert isinstance(result, ExecutionResult)


# ============================================================================
# AGENCY-04: Sandbox isolation via Firecracker microVMs (E2B)
# ============================================================================


class TestAgency04SandboxIsolation:
    """
    AGENCY-04: Sandbox isolation via rootless container or Wasm (E2B).

    Tests verify:
    - Sandbox implementation uses E2B Code Interpreter
    - Timeout enforcement
    - No host access
    """

    def test_sandbox_uses_e2b(self):
        """Sandbox implementation uses E2B Code Interpreter."""
        # AGENCY-04: Verify E2B integration
        import daem0nmcp.agency.sandbox as sandbox_module

        source = inspect.getsource(sandbox_module)
        assert "e2b_code_interpreter" in source
        assert "Sandbox" in source

    def test_sandbox_timeout_configurable(self):
        """Sandbox timeout is configurable."""
        # AGENCY-04: Verify timeout configuration
        executor = SandboxExecutor(timeout_seconds=60)
        assert executor.timeout_seconds == 60

        executor2 = SandboxExecutor(timeout_seconds=10)
        assert executor2.timeout_seconds == 10

    def test_default_timeout(self):
        """Default timeout is reasonable."""
        # AGENCY-04: Verify default timeout
        executor = SandboxExecutor()
        assert executor.timeout_seconds == 30

    @pytest.mark.asyncio
    async def test_timeout_passed_to_execution(self):
        """Timeout is passed to E2B execution."""
        # AGENCY-04: Verify timeout enforcement
        mock_execution = MagicMock()
        mock_execution.text = "ok"
        mock_execution.logs = []
        mock_execution.error = None

        mock_sandbox = MagicMock()
        mock_sandbox.run_code.return_value = mock_execution
        mock_sandbox.__enter__ = MagicMock(return_value=mock_sandbox)
        mock_sandbox.__exit__ = MagicMock(return_value=False)

        mock_sandbox_class = MagicMock(return_value=mock_sandbox)
        mock_module = MagicMock()
        mock_module.Sandbox = mock_sandbox_class

        with patch.dict("sys.modules", {"e2b_code_interpreter": mock_module}):
            executor = SandboxExecutor(api_key="test-key", timeout_seconds=45)
            executor._sandbox_available = True

            await executor.execute("print('test')")

        # Verify timeout was passed
        mock_sandbox.run_code.assert_called_once_with("print('test')", timeout=45)

    def test_api_key_required_for_availability(self):
        """Sandbox requires API key to be available."""
        # AGENCY-04: Verify API key requirement
        # Clear environment
        env_copy = os.environ.copy()
        env_copy.pop("E2B_API_KEY", None)

        with patch.dict(os.environ, env_copy, clear=True):
            executor = SandboxExecutor()
            assert not executor.available

    @pytest.mark.skipif(
        not os.environ.get("E2B_API_KEY"),
        reason="E2B_API_KEY not set",
    )
    @pytest.mark.asyncio
    async def test_sandbox_isolation_live(self):
        """Live test: sandbox cannot access host environment."""
        # AGENCY-04: Live isolation test
        os.environ["TEST_SECRET_VALUE"] = "should_not_be_visible"

        executor = SandboxExecutor()
        result = await executor.execute(
            "import os; print(os.environ.get('TEST_SECRET_VALUE', 'NOT_FOUND'))"
        )

        assert result.success
        assert "NOT_FOUND" in result.output


# ============================================================================
# AGENCY-05: Capability-scoped access enforces least-privilege
# ============================================================================


class TestAgency05CapabilityScopedAccess:
    """
    AGENCY-05: Capability-scoped access enforces least-privilege.

    Tests verify:
    - Default capabilities granted
    - Capabilities can be revoked
    - check_capability returns violation when denied
    - Per-project capability tracking
    """

    def test_default_capabilities_granted(self):
        """Default capabilities include EXECUTE_CODE."""
        # AGENCY-05: Verify default capabilities
        manager = CapabilityManager()
        assert manager.has_capability("/test", CapabilityScope.EXECUTE_CODE)

    def test_capability_can_be_revoked(self):
        """Capabilities can be revoked per project."""
        # AGENCY-05: Verify revocation
        manager = CapabilityManager()
        manager.revoke_capability("/test", CapabilityScope.EXECUTE_CODE)
        assert not manager.has_capability("/test", CapabilityScope.EXECUTE_CODE)

    def test_capability_can_be_granted(self):
        """Capabilities can be granted per project."""
        # AGENCY-05: Verify granting
        manager = CapabilityManager()
        manager.revoke_capability("/test", CapabilityScope.EXECUTE_CODE)
        assert not manager.has_capability("/test", CapabilityScope.EXECUTE_CODE)

        manager.grant_capability("/test", CapabilityScope.EXECUTE_CODE)
        assert manager.has_capability("/test", CapabilityScope.EXECUTE_CODE)

    def test_revoked_capability_blocks_check(self):
        """check_capability returns violation when capability revoked."""
        # AGENCY-05: Verify check_capability behavior
        manager = CapabilityManager()
        manager.revoke_capability("/test", CapabilityScope.EXECUTE_CODE)

        violation = check_capability("/test", CapabilityScope.EXECUTE_CODE, manager)

        assert violation is not None
        assert violation["violation"] == "CAPABILITY_DENIED"
        assert violation["required_capability"] == "EXECUTE_CODE"

    def test_granted_capability_passes_check(self):
        """check_capability returns None when capability granted."""
        # AGENCY-05: Verify successful check
        manager = CapabilityManager()

        violation = check_capability("/test", CapabilityScope.EXECUTE_CODE, manager)

        assert violation is None

    def test_different_projects_independent(self):
        """Capabilities are tracked per project."""
        # AGENCY-05: Verify project isolation
        manager = CapabilityManager()
        manager.revoke_capability("/project1", CapabilityScope.EXECUTE_CODE)

        # Project 1 blocked
        assert not manager.has_capability("/project1", CapabilityScope.EXECUTE_CODE)
        # Project 2 still has default
        assert manager.has_capability("/project2", CapabilityScope.EXECUTE_CODE)

    def test_capability_scope_enum_values(self):
        """CapabilityScope enum has expected values."""
        # AGENCY-05: Verify capability scopes
        assert CapabilityScope.EXECUTE_CODE is not None
        assert CapabilityScope.NETWORK_ACCESS is not None
        assert CapabilityScope.FILE_WRITE is not None

    def test_reset_capabilities(self):
        """reset_capabilities restores defaults."""
        # AGENCY-05: Verify reset
        manager = CapabilityManager()
        manager.revoke_capability("/test", CapabilityScope.EXECUTE_CODE)
        assert not manager.has_capability("/test", CapabilityScope.EXECUTE_CODE)

        manager.reset_capabilities("/test")
        assert manager.has_capability("/test", CapabilityScope.EXECUTE_CODE)

    def test_check_capability_without_manager(self):
        """check_capability allows when no manager provided."""
        # AGENCY-05: Backwards compatibility
        violation = check_capability("/test", CapabilityScope.EXECUTE_CODE, None)
        assert violation is None


# ============================================================================
# Security Logging Tests
# ============================================================================


class TestSecurityLogging:
    """Verify security logging for anomaly detection."""

    def test_sandbox_logs_unavailable(self, caplog):
        """SandboxExecutor logs when unavailable."""
        # Security logging for availability
        caplog.set_level(logging.WARNING)

        # Force unavailable by clearing API key
        env_copy = os.environ.copy()
        env_copy.pop("E2B_API_KEY", None)

        with patch.dict(os.environ, env_copy, clear=True):
            _ = SandboxExecutor()

        # Should log warning about unavailability (could be API key or package)
        assert any(
            "unavailable" in record.message.lower()
            or "not set" in record.message.lower()
            for record in caplog.records
        )

    @pytest.mark.asyncio
    async def test_sandbox_logs_execution(self, caplog):
        """SandboxExecutor logs execution attempts."""
        # Security logging for execution
        caplog.set_level(logging.INFO)

        mock_execution = MagicMock()
        mock_execution.text = "result"
        mock_execution.logs = []
        mock_execution.error = None

        mock_sandbox = MagicMock()
        mock_sandbox.run_code.return_value = mock_execution
        mock_sandbox.__enter__ = MagicMock(return_value=mock_sandbox)
        mock_sandbox.__exit__ = MagicMock(return_value=False)

        mock_sandbox_class = MagicMock(return_value=mock_sandbox)
        mock_module = MagicMock()
        mock_module.Sandbox = mock_sandbox_class

        with patch.dict("sys.modules", {"e2b_code_interpreter": mock_module}):
            executor = SandboxExecutor(api_key="test-key")
            executor._sandbox_available = True

            await executor.execute("print('test')")

        # Should log execution info
        assert any("execution" in record.message.lower() for record in caplog.records)

    def test_capability_logs_grant(self, caplog):
        """CapabilityManager logs capability grants."""
        caplog.set_level(logging.INFO)

        manager = CapabilityManager()
        manager.grant_capability("/test", CapabilityScope.NETWORK_ACCESS)

        assert any("granted" in record.message.lower() for record in caplog.records)

    def test_capability_logs_revoke(self, caplog):
        """CapabilityManager logs capability revocations."""
        caplog.set_level(logging.INFO)

        manager = CapabilityManager()
        manager.revoke_capability("/test", CapabilityScope.EXECUTE_CODE)

        assert any("revoked" in record.message.lower() for record in caplog.records)


# ============================================================================
# Edge Case Tests
# ============================================================================


class TestAgencyEdgeCases:
    """Edge case tests for robustness."""

    def test_capability_manager_get_capabilities(self):
        """CapabilityManager returns capability set."""
        manager = CapabilityManager()

        caps = manager.get_capabilities("/test")

        assert isinstance(caps, set)
        assert CapabilityScope.EXECUTE_CODE in caps

    def test_execution_result_with_all_fields(self):
        """ExecutionResult works with all fields populated."""
        result = ExecutionResult(
            success=True,
            output="Hello\nWorld",
            error=None,
            execution_time_ms=1500,
            logs=["Log 1", "Log 2", "Log 3"],
        )

        assert result.success
        assert "World" in result.output
        assert result.execution_time_ms == 1500
        assert len(result.logs) == 3
