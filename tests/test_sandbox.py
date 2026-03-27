"""Tests for SandboxExecutor - E2B-based sandboxed Python execution."""

import os
from unittest.mock import MagicMock, patch

import pytest

from daem0nmcp.agency import ExecutionResult, SandboxExecutor


class TestExecutionResult:
    """Tests for ExecutionResult dataclass."""

    def test_dataclass_fields(self):
        """Test ExecutionResult has all expected fields with defaults."""
        result = ExecutionResult(success=True, output="hello")
        assert result.success is True
        assert result.output == "hello"
        assert result.error is None
        assert result.execution_time_ms == 0
        assert result.logs == []

    def test_with_error(self):
        """Test ExecutionResult with error populated."""
        result = ExecutionResult(
            success=False,
            output="",
            error="SyntaxError: unexpected EOF",
            execution_time_ms=100,
        )
        assert not result.success
        assert result.error == "SyntaxError: unexpected EOF"
        assert result.execution_time_ms == 100

    def test_with_logs(self):
        """Test ExecutionResult with logs populated."""
        result = ExecutionResult(
            success=True,
            output="result",
            logs=["log line 1", "log line 2"],
        )
        assert result.logs == ["log line 1", "log line 2"]

    def test_all_fields(self):
        """Test ExecutionResult with all fields populated."""
        result = ExecutionResult(
            success=True,
            output="42",
            error=None,
            execution_time_ms=150,
            logs=["Computing..."],
        )
        assert result.success is True
        assert result.output == "42"
        assert result.error is None
        assert result.execution_time_ms == 150
        assert result.logs == ["Computing..."]


class TestSandboxExecutorAvailability:
    """Tests for SandboxExecutor availability checks."""

    def test_unavailable_without_api_key(self):
        """Test executor unavailable when E2B_API_KEY not set."""
        # Clear the environment variable
        env_copy = os.environ.copy()
        env_copy.pop("E2B_API_KEY", None)

        with patch.dict(os.environ, env_copy, clear=True):
            executor = SandboxExecutor()
            assert not executor.available

    def test_available_with_api_key(self):
        """Test executor available when API key provided and package installed."""
        # Mock the e2b_code_interpreter import
        mock_module = MagicMock()
        mock_module.Sandbox = MagicMock()

        with patch.dict("sys.modules", {"e2b_code_interpreter": mock_module}):
            executor = SandboxExecutor(api_key="test-api-key")
            assert executor.available

    def test_explicit_api_key(self):
        """Test explicit API key takes precedence over environment."""
        mock_module = MagicMock()
        mock_module.Sandbox = MagicMock()

        with patch.dict("sys.modules", {"e2b_code_interpreter": mock_module}):
            executor = SandboxExecutor(api_key="explicit-key")
            assert executor._api_key == "explicit-key"

    def test_repr(self):
        """Test string representation."""
        executor = SandboxExecutor(timeout_seconds=60)
        repr_str = repr(executor)
        assert "SandboxExecutor" in repr_str
        assert "timeout=60s" in repr_str
        assert "available=" in repr_str


class TestSandboxExecutorExecution:
    """Tests for SandboxExecutor execution."""

    @pytest.mark.asyncio
    async def test_execute_when_unavailable(self):
        """Test execute returns error when sandbox unavailable."""
        executor = SandboxExecutor()
        executor._sandbox_available = False

        result = await executor.execute("print('test')")

        assert not result.success
        assert "not available" in result.error

    @pytest.mark.asyncio
    async def test_execute_success(self):
        """Test successful execution with mocked E2B."""
        # Create mock execution result
        mock_execution = MagicMock()
        mock_execution.text = "Hello, World!"
        mock_execution.logs = []
        mock_execution.error = None

        # Create mock sandbox
        mock_sandbox = MagicMock()
        mock_sandbox.run_code.return_value = mock_execution
        mock_sandbox.__enter__ = MagicMock(return_value=mock_sandbox)
        mock_sandbox.__exit__ = MagicMock(return_value=False)

        # Create mock Sandbox class
        mock_sandbox_class = MagicMock(return_value=mock_sandbox)

        mock_module = MagicMock()
        mock_module.Sandbox = mock_sandbox_class

        with patch.dict("sys.modules", {"e2b_code_interpreter": mock_module}):
            executor = SandboxExecutor(api_key="test-key")
            executor._sandbox_available = True

            result = await executor.execute("print('Hello, World!')")

        assert result.success
        assert result.output == "Hello, World!"
        assert result.error is None

    @pytest.mark.asyncio
    async def test_execute_with_error(self):
        """Test execution that returns an error."""
        mock_execution = MagicMock()
        mock_execution.text = ""
        mock_execution.logs = []
        mock_execution.error = "NameError: name 'undefined' is not defined"

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

            result = await executor.execute("print(undefined)")

        assert not result.success
        assert "NameError" in result.error

    @pytest.mark.asyncio
    async def test_execute_with_exception(self):
        """Test execution that raises an exception."""
        mock_sandbox = MagicMock()
        mock_sandbox.run_code.side_effect = Exception("Connection failed")
        mock_sandbox.__enter__ = MagicMock(return_value=mock_sandbox)
        mock_sandbox.__exit__ = MagicMock(return_value=False)

        mock_sandbox_class = MagicMock(return_value=mock_sandbox)
        mock_module = MagicMock()
        mock_module.Sandbox = mock_sandbox_class

        with patch.dict("sys.modules", {"e2b_code_interpreter": mock_module}):
            executor = SandboxExecutor(api_key="test-key")
            executor._sandbox_available = True

            result = await executor.execute("print('test')")

        assert not result.success
        assert "Connection failed" in result.error
        assert result.execution_time_ms >= 0

    @pytest.mark.asyncio
    async def test_execute_with_logs(self):
        """Test execution with log output."""
        mock_log = MagicMock()
        mock_log.line = "Debug: processing"

        mock_execution = MagicMock()
        mock_execution.text = "done"
        mock_execution.logs = [mock_log]
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

            result = await executor.execute(
                "import logging; logging.debug('processing')"
            )

        assert result.success
        assert result.logs == ["Debug: processing"]


class TestSandboxExecutorTimeout:
    """Tests for SandboxExecutor timeout configuration."""

    def test_default_timeout(self):
        """Test default timeout is 30 seconds."""
        executor = SandboxExecutor()
        assert executor.timeout_seconds == 30

    def test_custom_timeout(self):
        """Test custom timeout configuration."""
        executor = SandboxExecutor(timeout_seconds=60)
        assert executor.timeout_seconds == 60

    @pytest.mark.asyncio
    async def test_timeout_passed_to_sandbox(self):
        """Test timeout is passed to sandbox.run_code()."""
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


# Integration tests - only run with real E2B_API_KEY
@pytest.mark.skipif(
    not os.environ.get("E2B_API_KEY"),
    reason="E2B_API_KEY not set - skipping integration tests",
)
class TestSandboxIntegration:
    """Integration tests requiring real E2B API key."""

    @pytest.mark.asyncio
    async def test_simple_execution(self):
        """Test simple print execution in real sandbox."""
        executor = SandboxExecutor(timeout_seconds=30)
        result = await executor.execute("print('hello from sandbox')")
        assert result.success
        assert "hello from sandbox" in result.output

    @pytest.mark.asyncio
    async def test_syntax_error(self):
        """Test syntax error handling in real sandbox."""
        executor = SandboxExecutor()
        result = await executor.execute("print('unclosed")
        assert not result.success
        assert result.error is not None

    @pytest.mark.asyncio
    async def test_computation(self):
        """Test computation in real sandbox."""
        executor = SandboxExecutor()
        result = await executor.execute("print(sum(range(100)))")
        assert result.success
        assert "4950" in result.output

    @pytest.mark.asyncio
    async def test_isolation_no_env_access(self):
        """Test sandbox cannot access host environment."""
        # Set a test env var locally
        os.environ["TEST_SECRET"] = "should_not_be_visible"

        executor = SandboxExecutor()
        result = await executor.execute(
            "import os; print(os.environ.get('TEST_SECRET', 'NOT_FOUND'))"
        )
        assert result.success
        assert "NOT_FOUND" in result.output  # Sandbox shouldn't see host env
