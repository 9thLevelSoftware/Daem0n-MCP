"""
Code Execution and Failure Classification for Reflexion Evaluator.

Wraps SandboxExecutor to provide:
1. Structured code execution results with failure classification
2. CodeFailureType enum for categorizing sandbox errors
3. Retry logic for fixable errors (SYNTAX_ERROR, IMPORT_ERROR)
4. Graceful fallback for infrastructure errors (TIMEOUT, SANDBOX_ERROR)

The classification determines Evaluator behavior:
- SUCCESS: Code assertions passed -> positive verification signal
- ASSERTION_FAILURE: Code assertions failed -> negative verification signal (claim is wrong)
- SYNTAX_ERROR: Generated code has syntax errors -> fixable, may retry
- IMPORT_ERROR: Code uses unavailable module -> fixable, may retry without import
- TIMEOUT: Code exceeded time limit -> infrastructure issue, fall back to text-only
- SANDBOX_ERROR: E2B infrastructure failure -> fall back to text-only

Phase 14: Code-Augmented Reflexion
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import Enum

from ..agency.sandbox import SandboxExecutor, StructuredExecutionResult

logger = logging.getLogger(__name__)


class CodeFailureType(str, Enum):
    """Classification of code execution outcomes.

    Each type triggers different behavior in the Evaluator:
    - SUCCESS: Positive signal for quality scoring
    - ASSERTION_FAILURE: Negative signal (claim is factually wrong)
    - SYNTAX_ERROR: Fixable error, may retry with corrected code
    - IMPORT_ERROR: Fixable error, may retry without the import
    - TIMEOUT: Infrastructure issue, fall back to text-only
    - SANDBOX_ERROR: Infrastructure issue, fall back to text-only
    """

    SUCCESS = "SUCCESS"
    SYNTAX_ERROR = "SYNTAX_ERROR"
    IMPORT_ERROR = "IMPORT_ERROR"
    ASSERTION_FAILURE = "ASSERTION_FAILURE"
    TIMEOUT = "TIMEOUT"
    SANDBOX_ERROR = "SANDBOX_ERROR"


# Failure types that indicate the CODE is wrong (potentially fixable)
FIXABLE_FAILURES = {CodeFailureType.SYNTAX_ERROR, CodeFailureType.IMPORT_ERROR}

# Failure types that indicate INFRASTRUCTURE problems (fall back to text-only)
INFRASTRUCTURE_FAILURES = {CodeFailureType.TIMEOUT, CodeFailureType.SANDBOX_ERROR}

# Failure types that indicate the CLAIM is wrong (verification signal)
VERIFICATION_FAILURES = {CodeFailureType.ASSERTION_FAILURE}


@dataclass
class CodeExecutionResult:
    """Result of executing verification code with classification.

    This is the structured result returned to the Evaluator node,
    containing both the execution output and the failure classification.
    """

    failure_type: CodeFailureType
    output: str = ""
    error_message: str | None = None
    error_traceback: str | None = None
    execution_time_ms: int = 0
    assertions_passed: bool = False

    @property
    def is_success(self) -> bool:
        """Whether the code executed successfully with all assertions passing."""
        return self.failure_type == CodeFailureType.SUCCESS

    @property
    def is_fixable(self) -> bool:
        """Whether the failure is potentially fixable by regenerating code."""
        return self.failure_type in FIXABLE_FAILURES

    @property
    def is_infrastructure_failure(self) -> bool:
        """Whether the failure is an infrastructure issue (should fall back)."""
        return self.failure_type in INFRASTRUCTURE_FAILURES

    @property
    def is_verification_failure(self) -> bool:
        """Whether the failure indicates the claim is wrong."""
        return self.failure_type in VERIFICATION_FAILURES

    def to_dict(self) -> dict:
        """Convert to dict for ReflexionState storage."""
        return {
            "failure_type": self.failure_type.value,
            "output": self.output,
            "error_message": self.error_message,
            "assertions_passed": self.assertions_passed,
            "execution_time_ms": self.execution_time_ms,
        }


def classify_failure(result: StructuredExecutionResult) -> CodeFailureType:
    """Classify a sandbox execution result into a CodeFailureType.

    Uses the structured error_name from E2B's ExecutionError to determine
    the failure category. The error_name contains the Python exception
    class name (e.g., "SyntaxError", "AssertionError").

    Args:
        result: StructuredExecutionResult from SandboxExecutor.execute_structured()

    Returns:
        CodeFailureType classification
    """
    if result.success:
        return CodeFailureType.SUCCESS

    error_name = (result.error_name or "").lower()
    error_value = (result.error_value or "").lower()

    # Syntax errors -- generated code has Python syntax issues
    if "syntax" in error_name or "syntaxerror" in error_name:
        return CodeFailureType.SYNTAX_ERROR

    # Import/module errors -- code uses unavailable packages
    if (
        "import" in error_name
        or "module" in error_name
        or "modulenotfound" in error_name
    ):
        return CodeFailureType.IMPORT_ERROR

    # Assertion errors -- claim verification failed (the claim is wrong)
    if "assertion" in error_name or "assertionerror" in error_name:
        return CodeFailureType.ASSERTION_FAILURE

    # Timeout errors -- execution exceeded time limit
    if "timeout" in error_name or "timedout" in error_name:
        return CodeFailureType.TIMEOUT

    # Check error_value as fallback for edge cases
    if "timeout" in error_value or "timed out" in error_value:
        return CodeFailureType.TIMEOUT

    if "syntax" in error_value:
        return CodeFailureType.SYNTAX_ERROR

    # Anything else is a sandbox/infrastructure error
    return CodeFailureType.SANDBOX_ERROR


async def execute_verification_code(
    code: str,
    executor: SandboxExecutor,
) -> CodeExecutionResult:
    """Execute verification code in the sandbox and classify the result.

    This is the main entry point for the Evaluator to run verification code.
    It handles sandbox availability checks, execution, and classification.

    Args:
        code: Python verification code to execute
        executor: SandboxExecutor instance

    Returns:
        CodeExecutionResult with classification and details
    """
    # Check sandbox availability first
    if not executor.available:
        logger.info(
            "Sandbox unavailable, returning SANDBOX_ERROR for graceful fallback"
        )
        return CodeExecutionResult(
            failure_type=CodeFailureType.SANDBOX_ERROR,
            error_message="Sandbox not available (E2B_API_KEY not set or e2b-code-interpreter not installed)",
        )

    # Execute in sandbox
    result = await executor.execute_structured(code)

    # Classify the result
    failure_type = classify_failure(result)

    # Check for assertion pass marker in output
    assertions_passed = (
        failure_type == CodeFailureType.SUCCESS
        and "ALL_ASSERTIONS_PASSED" in result.output
    )

    # Build error message
    error_message = None
    if not result.success:
        error_message = (
            f"{result.error_name}: {result.error_value}"
            if result.error_name
            else result.error_value
        )

    logger.info(
        f"Verification code execution: {failure_type.value}, "
        f"assertions_passed={assertions_passed}, "
        f"time={result.execution_time_ms}ms"
    )

    return CodeExecutionResult(
        failure_type=failure_type,
        output=result.output,
        error_message=error_message,
        error_traceback=result.error_traceback,
        execution_time_ms=result.execution_time_ms,
        assertions_passed=assertions_passed,
    )


__all__ = [
    "CodeFailureType",
    "CodeExecutionResult",
    "FIXABLE_FAILURES",
    "INFRASTRUCTURE_FAILURES",
    "VERIFICATION_FAILURES",
    "classify_failure",
    "execute_verification_code",
]
