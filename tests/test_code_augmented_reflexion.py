"""Tests for Code-Augmented Reflexion (Phase 14).

Comprehensive test coverage for all 5 REFL requirements:
- REFL-01: Actor generates verification code for code-verifiable claims
- REFL-02: Evaluator executes code in sandbox when budget allows
- REFL-03: Code results feed quality score (+0.1 pass, -0.15 assertion fail)
- REFL-04: Separate code budget (code_executions_used vs MAX_ITERATIONS)
- REFL-05: Failure classification (CodeFailureType, fixable/infra/verification sets)
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from daem0nmcp.agency.sandbox import StructuredExecutionResult
from daem0nmcp.reflexion.claims import (
    Claim,
    ClaimType,
    VerificationLevel,
    is_code_verifiable,
)
from daem0nmcp.reflexion.code_exec import (
    FIXABLE_FAILURES,
    INFRASTRUCTURE_FAILURES,
    VERIFICATION_FAILURES,
    CodeExecutionResult,
    CodeFailureType,
    classify_failure,
    execute_verification_code,
)
from daem0nmcp.reflexion.code_gen import generate_verification_code
from daem0nmcp.reflexion.graph import (
    build_reflexion_graph,
    run_reflexion,
)
from daem0nmcp.reflexion.nodes import (
    create_actor_node,
    create_evaluator_node,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_memory_manager():
    """Create a mock MemoryManager."""
    manager = AsyncMock()
    manager.recall = AsyncMock(return_value={"memories": []})
    return manager


@pytest.fixture
def mock_sandbox_executor():
    """Create a mock SandboxExecutor that is available."""
    executor = AsyncMock()
    executor.available = True
    return executor


@pytest.fixture
def unavailable_sandbox():
    """Create a mock SandboxExecutor that is NOT available."""
    executor = AsyncMock()
    executor.available = False
    return executor


def _make_claim(text, claim_type=ClaimType.FACTUAL_ASSERTION):
    """Helper to create a Claim for testing."""
    return Claim(
        text=text,
        claim_type=claim_type,
        verification_level=VerificationLevel.BEST_EFFORT,
        subject=text.split()[0] if text else None,
    )


# ===========================================================================
# REFL-01: Actor generates verification code
# ===========================================================================


class TestIsCodeVerifiable:
    """Tests for is_code_verifiable() heuristic."""

    def test_memory_refs_not_verifiable(self):
        """Memory reference claims are NOT code-verifiable."""
        claim = _make_claim("We decided to use PostgreSQL", ClaimType.MEMORY_REFERENCE)
        assert not is_code_verifiable(claim)

    def test_outcome_refs_not_verifiable(self):
        """Outcome reference claims are NOT code-verifiable."""
        claim = _make_claim("that approach worked", ClaimType.OUTCOME_REFERENCE)
        assert not is_code_verifiable(claim)

    def test_factual_returns_none_verifiable(self):
        """'returns None' pattern is code-verifiable."""
        claim = _make_claim("list.sort() returns None")
        assert is_code_verifiable(claim)

    def test_factual_numeric_verifiable(self):
        """Numeric assertion is code-verifiable."""
        claim = _make_claim("the sum of 1 to 100 is 5050")
        assert is_code_verifiable(claim)

    def test_generic_factual_not_verifiable(self):
        """Generic factual without code pattern is NOT code-verifiable."""
        claim = _make_claim("Python is a language")
        assert not is_code_verifiable(claim)


class TestCodeGeneration:
    """Tests for generate_verification_code()."""

    def test_no_verifiable_claims_returns_none(self):
        """No code-verifiable claims -> None."""
        claims = [_make_claim("Python is a language")]
        result = generate_verification_code(claims=claims, draft="Python is a language")
        assert result is None

    def test_template_generation_for_returns_pattern(self):
        """Template generates assertion for 'returns None' pattern."""
        claim = _make_claim("list.sort() returns None")
        # Ensure it's code-verifiable
        assert is_code_verifiable(claim)

        result = generate_verification_code(
            claims=[claim],
            draft="list.sort() returns None",
        )
        assert result is not None
        assert "assert" in result.lower() or "sort" in result.lower()

    def test_llm_func_called_when_provided(self):
        """LLM function is called for code generation."""
        llm_func = MagicMock(return_value="assert True\nprint('ALL_ASSERTIONS_PASSED')")
        claim = _make_claim("list.sort() returns None")

        result = generate_verification_code(
            claims=[claim],
            draft="list.sort() returns None",
            llm_func=llm_func,
        )
        assert result is not None
        llm_func.assert_called_once()

    def test_empty_claims_returns_none(self):
        """Empty claims list -> None."""
        result = generate_verification_code(claims=[], draft="test")
        assert result is None


class TestActorCodeGeneration:
    """Tests for Actor node generating verification_code."""

    def test_actor_returns_verification_code_key(self):
        """Actor always returns verification_code in its output dict."""
        actor = create_actor_node(llm_func=None)
        state = {"query": "Test query", "iteration": 0, "critique": ""}
        result = actor(state)

        assert "verification_code" in result
        assert "draft" in result
        assert "iteration" in result

    def test_actor_returns_none_when_no_code_verifiable_claims(self):
        """Actor returns verification_code=None when draft has no code-verifiable claims."""
        actor = create_actor_node(llm_func=None)
        # Default test draft is "[Draft iteration 1] Response to: Test" -- no code patterns
        state = {"query": "Test", "iteration": 0, "critique": ""}
        result = actor(state)
        assert result["verification_code"] is None

    def test_actor_generates_code_for_verifiable_draft(self):
        """Actor generates verification_code when draft has code-verifiable claims."""

        def llm_func(prompt):
            # Return text that extract_claims can parse AND is_code_verifiable matches
            return "The function returns None by default."

        actor = create_actor_node(llm_func=llm_func)
        state = {"query": "How does sort work?", "iteration": 0, "critique": ""}
        result = actor(state)

        # The draft contains "returns None" which is code-verifiable
        assert result["verification_code"] is not None

    def test_actor_graceful_on_code_gen_error(self):
        """Actor still returns draft if code generation fails."""
        # Patch at the source module so the local import picks up the mock
        with patch(
            "daem0nmcp.reflexion.code_gen.generate_verification_code",
            side_effect=RuntimeError("fail"),
        ):
            actor = create_actor_node(llm_func=None)
            state = {"query": "Test", "iteration": 0, "critique": ""}
            result = actor(state)

        # Even if code gen fails, actor still returns draft and verification_code=None
        assert "draft" in result
        assert "verification_code" in result
        assert result["verification_code"] is None


# ===========================================================================
# REFL-02: Evaluator executes code in sandbox
# ===========================================================================


class TestEvaluatorCodeExecution:
    """Tests for Evaluator executing verification code."""

    @pytest.mark.asyncio
    async def test_evaluator_executes_code_when_sandbox_available(
        self, mock_memory_manager
    ):
        """Evaluator calls execute_verification_code when sandbox_executor provided."""
        mock_exec = AsyncMock()
        mock_exec.available = True

        mock_code_result = CodeExecutionResult(
            failure_type=CodeFailureType.SUCCESS,
            output="ALL_ASSERTIONS_PASSED",
            assertions_passed=True,
            execution_time_ms=50,
        )

        evaluator = create_evaluator_node(
            memory_manager=mock_memory_manager,
            sandbox_executor=mock_exec,
        )
        state = {
            "draft": "Simple response.",
            "iteration": 1,
            "verification_code": "assert True\nprint('ALL_ASSERTIONS_PASSED')",
            "code_executions_used": 0,
            "max_code_executions": 2,
        }

        with patch("daem0nmcp.reflexion.nodes.verify_claims") as mock_verify:
            mock_verify.return_value = []
            with patch(
                "daem0nmcp.reflexion.nodes.summarize_verification"
            ) as mock_summary:
                mock_summary.return_value = {
                    "verified_count": 0,
                    "unverified_count": 0,
                    "conflict_count": 0,
                    "overall_confidence": 0.7,
                    "conflicts": [],
                }
                with patch(
                    "daem0nmcp.reflexion.code_exec.execute_verification_code",
                    return_value=mock_code_result,
                ) as mock_exec_fn:
                    result = await evaluator(state)

                    mock_exec_fn.assert_called_once()

        assert result["code_executions_used"] == 1
        assert len(result["code_verification_results"]) == 1

    @pytest.mark.asyncio
    async def test_evaluator_skips_code_without_sandbox(self, mock_memory_manager):
        """Evaluator skips code execution when sandbox_executor is None."""
        evaluator = create_evaluator_node(
            memory_manager=mock_memory_manager,
            sandbox_executor=None,  # No sandbox
        )
        state = {
            "draft": "Response.",
            "iteration": 1,
            "verification_code": "assert True",
            "code_executions_used": 0,
            "max_code_executions": 2,
        }

        with patch("daem0nmcp.reflexion.nodes.verify_claims") as mock_verify:
            mock_verify.return_value = []
            with patch(
                "daem0nmcp.reflexion.nodes.summarize_verification"
            ) as mock_summary:
                mock_summary.return_value = {
                    "verified_count": 0,
                    "unverified_count": 0,
                    "conflict_count": 0,
                    "overall_confidence": 0.7,
                    "conflicts": [],
                }
                result = await evaluator(state)

        # Code was not executed
        assert result["code_executions_used"] == 0
        assert result["code_verification_results"] == []

    @pytest.mark.asyncio
    async def test_evaluator_skips_code_when_no_verification_code(
        self, mock_memory_manager, mock_sandbox_executor
    ):
        """Evaluator skips code execution when verification_code is None."""
        evaluator = create_evaluator_node(
            memory_manager=mock_memory_manager,
            sandbox_executor=mock_sandbox_executor,
        )
        state = {
            "draft": "Response.",
            "iteration": 1,
            "verification_code": None,  # No code generated
            "code_executions_used": 0,
            "max_code_executions": 2,
        }

        with patch("daem0nmcp.reflexion.nodes.verify_claims") as mock_verify:
            mock_verify.return_value = []
            with patch(
                "daem0nmcp.reflexion.nodes.summarize_verification"
            ) as mock_summary:
                mock_summary.return_value = {
                    "verified_count": 0,
                    "unverified_count": 0,
                    "conflict_count": 0,
                    "overall_confidence": 0.7,
                    "conflicts": [],
                }
                result = await evaluator(state)

        assert result["code_executions_used"] == 0


# ===========================================================================
# REFL-03: Code results feed quality score
# ===========================================================================


class TestCodeQualityScoring:
    """Tests for code execution impact on quality_score."""

    @pytest.mark.asyncio
    async def test_passing_assertions_boost_score(self, mock_memory_manager):
        """Passing assertions boost quality_score by 0.1 (0.7 -> 0.8)."""
        mock_code_result = CodeExecutionResult(
            failure_type=CodeFailureType.SUCCESS,
            output="ALL_ASSERTIONS_PASSED",
            assertions_passed=True,
        )
        mock_exec = AsyncMock()
        mock_exec.available = True

        evaluator = create_evaluator_node(
            memory_manager=mock_memory_manager,
            sandbox_executor=mock_exec,
        )
        state = {
            "draft": "Response.",
            "iteration": 1,
            "verification_code": "assert True\nprint('ALL_ASSERTIONS_PASSED')",
            "code_executions_used": 0,
            "max_code_executions": 2,
        }

        with patch("daem0nmcp.reflexion.nodes.verify_claims") as mock_verify:
            mock_verify.return_value = []
            with patch(
                "daem0nmcp.reflexion.nodes.summarize_verification"
            ) as mock_summary:
                mock_summary.return_value = {
                    "verified_count": 0,
                    "unverified_count": 0,
                    "conflict_count": 0,
                    "overall_confidence": 0.7,
                    "conflicts": [],
                }
                with patch(
                    "daem0nmcp.reflexion.code_exec.execute_verification_code",
                    return_value=mock_code_result,
                ):
                    result = await evaluator(state)

        # 0.7 base + 0.1 bonus = 0.8
        assert result["quality_score"] == pytest.approx(0.8, abs=0.01)

    @pytest.mark.asyncio
    async def test_assertion_failure_penalizes_score(self, mock_memory_manager):
        """Assertion failure penalizes quality_score by 0.15 (0.7 -> 0.55)."""
        mock_code_result = CodeExecutionResult(
            failure_type=CodeFailureType.ASSERTION_FAILURE,
            output="",
            assertions_passed=False,
            error_message="AssertionError: expected True",
        )
        mock_exec = AsyncMock()
        mock_exec.available = True

        evaluator = create_evaluator_node(
            memory_manager=mock_memory_manager,
            sandbox_executor=mock_exec,
        )
        state = {
            "draft": "Response.",
            "iteration": 1,
            "verification_code": "assert False",
            "code_executions_used": 0,
            "max_code_executions": 2,
        }

        with patch("daem0nmcp.reflexion.nodes.verify_claims") as mock_verify:
            mock_verify.return_value = []
            with patch(
                "daem0nmcp.reflexion.nodes.summarize_verification"
            ) as mock_summary:
                mock_summary.return_value = {
                    "verified_count": 0,
                    "unverified_count": 0,
                    "conflict_count": 0,
                    "overall_confidence": 0.7,
                    "conflicts": [],
                }
                with patch(
                    "daem0nmcp.reflexion.code_exec.execute_verification_code",
                    return_value=mock_code_result,
                ):
                    result = await evaluator(state)

        # 0.7 base - 0.15 penalty = 0.55
        assert result["quality_score"] == pytest.approx(0.55, abs=0.01)
        assert "CODE VERIFICATION FAILED" in result["critique"]

    @pytest.mark.asyncio
    async def test_infrastructure_error_no_score_impact(self, mock_memory_manager):
        """Infrastructure errors (timeout, sandbox) have zero score impact."""
        mock_code_result = CodeExecutionResult(
            failure_type=CodeFailureType.TIMEOUT,
            output="",
            assertions_passed=False,
            error_message="Execution timed out",
        )
        mock_exec = AsyncMock()
        mock_exec.available = True

        evaluator = create_evaluator_node(
            memory_manager=mock_memory_manager,
            sandbox_executor=mock_exec,
        )
        state = {
            "draft": "Response.",
            "iteration": 1,
            "verification_code": "import time; time.sleep(999)",
            "code_executions_used": 0,
            "max_code_executions": 2,
        }

        with patch("daem0nmcp.reflexion.nodes.verify_claims") as mock_verify:
            mock_verify.return_value = []
            with patch(
                "daem0nmcp.reflexion.nodes.summarize_verification"
            ) as mock_summary:
                mock_summary.return_value = {
                    "verified_count": 0,
                    "unverified_count": 0,
                    "conflict_count": 0,
                    "overall_confidence": 0.7,
                    "conflicts": [],
                }
                with patch(
                    "daem0nmcp.reflexion.code_exec.execute_verification_code",
                    return_value=mock_code_result,
                ):
                    result = await evaluator(state)

        # 0.7 base, no adjustment for infrastructure error
        assert result["quality_score"] == pytest.approx(0.7, abs=0.01)

    @pytest.mark.asyncio
    async def test_syntax_error_no_score_impact(self, mock_memory_manager):
        """Syntax errors (fixable) have zero score impact."""
        mock_code_result = CodeExecutionResult(
            failure_type=CodeFailureType.SYNTAX_ERROR,
            output="",
            assertions_passed=False,
            error_message="SyntaxError: invalid syntax",
        )
        mock_exec = AsyncMock()
        mock_exec.available = True

        evaluator = create_evaluator_node(
            memory_manager=mock_memory_manager,
            sandbox_executor=mock_exec,
        )
        state = {
            "draft": "Response.",
            "iteration": 1,
            "verification_code": "assert(",
            "code_executions_used": 0,
            "max_code_executions": 2,
        }

        with patch("daem0nmcp.reflexion.nodes.verify_claims") as mock_verify:
            mock_verify.return_value = []
            with patch(
                "daem0nmcp.reflexion.nodes.summarize_verification"
            ) as mock_summary:
                mock_summary.return_value = {
                    "verified_count": 0,
                    "unverified_count": 0,
                    "conflict_count": 0,
                    "overall_confidence": 0.7,
                    "conflicts": [],
                }
                with patch(
                    "daem0nmcp.reflexion.code_exec.execute_verification_code",
                    return_value=mock_code_result,
                ):
                    result = await evaluator(state)

        # 0.7 base, no adjustment for syntax error
        assert result["quality_score"] == pytest.approx(0.7, abs=0.01)


# ===========================================================================
# REFL-04: Separate code budget
# ===========================================================================


class TestCodeExecutionBudget:
    """Tests for code execution budget control."""

    @pytest.mark.asyncio
    async def test_budget_exhaustion_falls_back_to_text_only(self, mock_memory_manager):
        """When budget exhausted, executor is NOT called -- text-only fallback."""
        mock_exec = AsyncMock()
        mock_exec.available = True

        evaluator = create_evaluator_node(
            memory_manager=mock_memory_manager,
            sandbox_executor=mock_exec,
        )
        state = {
            "draft": "Response.",
            "iteration": 1,
            "verification_code": "assert True",
            "code_executions_used": 2,  # Already used all budget
            "max_code_executions": 2,
        }

        with patch("daem0nmcp.reflexion.nodes.verify_claims") as mock_verify:
            mock_verify.return_value = []
            with patch(
                "daem0nmcp.reflexion.nodes.summarize_verification"
            ) as mock_summary:
                mock_summary.return_value = {
                    "verified_count": 0,
                    "unverified_count": 0,
                    "conflict_count": 0,
                    "overall_confidence": 0.7,
                    "conflicts": [],
                }
                with patch(
                    "daem0nmcp.reflexion.code_exec.execute_verification_code"
                ) as mock_exec_fn:
                    result = await evaluator(state)
                    # Executor NOT called because budget exhausted
                    mock_exec_fn.assert_not_called()

        # Budget stays at 2 (not incremented)
        assert result["code_executions_used"] == 2
        # Score is text-only (0.7 base, no code adjustment)
        assert result["quality_score"] == pytest.approx(0.7, abs=0.01)

    @pytest.mark.asyncio
    async def test_budget_increments_on_execution(self, mock_memory_manager):
        """code_executions_used increments by 1 after each execution."""
        mock_code_result = CodeExecutionResult(
            failure_type=CodeFailureType.SUCCESS,
            output="ALL_ASSERTIONS_PASSED",
            assertions_passed=True,
        )
        mock_exec = AsyncMock()
        mock_exec.available = True

        evaluator = create_evaluator_node(
            memory_manager=mock_memory_manager,
            sandbox_executor=mock_exec,
        )
        state = {
            "draft": "Response.",
            "iteration": 1,
            "verification_code": "assert True",
            "code_executions_used": 0,
            "max_code_executions": 2,
        }

        with patch("daem0nmcp.reflexion.nodes.verify_claims") as mock_verify:
            mock_verify.return_value = []
            with patch(
                "daem0nmcp.reflexion.nodes.summarize_verification"
            ) as mock_summary:
                mock_summary.return_value = {
                    "verified_count": 0,
                    "unverified_count": 0,
                    "conflict_count": 0,
                    "overall_confidence": 0.7,
                    "conflicts": [],
                }
                with patch(
                    "daem0nmcp.reflexion.code_exec.execute_verification_code",
                    return_value=mock_code_result,
                ):
                    result = await evaluator(state)

        assert result["code_executions_used"] == 1

    @pytest.mark.asyncio
    async def test_code_budget_separate_from_max_iterations(self, mock_memory_manager):
        """Code budget does not affect iteration-based loop control."""
        _mock_code_result = CodeExecutionResult(  # noqa: F841
            failure_type=CodeFailureType.SUCCESS,
            output="ALL_ASSERTIONS_PASSED",
            assertions_passed=True,
        )
        mock_exec = AsyncMock()
        mock_exec.available = True

        evaluator = create_evaluator_node(
            memory_manager=mock_memory_manager,
            sandbox_executor=mock_exec,
        )
        # Low quality at iteration 1 with budget exhausted
        state = {
            "draft": "Response.",
            "iteration": 1,
            "verification_code": "assert True",
            "code_executions_used": 2,  # Budget exhausted
            "max_code_executions": 2,
        }

        with patch("daem0nmcp.reflexion.nodes.verify_claims") as mock_verify:
            mock_verify.return_value = []
            with patch(
                "daem0nmcp.reflexion.nodes.summarize_verification"
            ) as mock_summary:
                mock_summary.return_value = {
                    "verified_count": 0,
                    "unverified_count": 0,
                    "conflict_count": 0,
                    "overall_confidence": 0.5,
                    "conflicts": [],
                }
                result = await evaluator(state)

        # Budget exhausted does NOT end the loop -- should_continue is based on quality + iteration
        assert result["should_continue"] is True  # Quality < 0.8 and iteration < 3
        assert result["quality_score"] < 0.8

    @pytest.mark.asyncio
    async def test_budget_exhaustion_does_not_end_loop(self, mock_memory_manager):
        """Budget exhaustion falls back to text-only, loop continues based on quality."""
        evaluator = create_evaluator_node(
            memory_manager=mock_memory_manager,
            sandbox_executor=AsyncMock(),
        )
        state = {
            "draft": "Response.",
            "iteration": 1,
            "verification_code": "assert True",
            "code_executions_used": 5,
            "max_code_executions": 2,
        }

        with patch("daem0nmcp.reflexion.nodes.verify_claims") as mock_verify:
            mock_verify.return_value = []
            with patch(
                "daem0nmcp.reflexion.nodes.summarize_verification"
            ) as mock_summary:
                mock_summary.return_value = {
                    "verified_count": 0,
                    "unverified_count": 0,
                    "conflict_count": 0,
                    "overall_confidence": 0.3,
                    "conflicts": [],
                }
                result = await evaluator(state)

        # Low quality + iteration < MAX -> should continue
        assert result["should_continue"] is True


# ===========================================================================
# REFL-05: Failure classification
# ===========================================================================


class TestFailureClassification:
    """Tests for classify_failure()."""

    def test_classify_success(self):
        """Successful execution -> SUCCESS."""
        result = StructuredExecutionResult(success=True, output="ok")
        assert classify_failure(result) == CodeFailureType.SUCCESS

    def test_classify_syntax_error(self):
        """SyntaxError -> SYNTAX_ERROR."""
        result = StructuredExecutionResult(
            success=False,
            output="",
            error_name="SyntaxError",
            error_value="invalid syntax",
        )
        assert classify_failure(result) == CodeFailureType.SYNTAX_ERROR

    def test_classify_import_error(self):
        """ImportError -> IMPORT_ERROR."""
        result = StructuredExecutionResult(
            success=False,
            output="",
            error_name="ImportError",
            error_value="No module named 'foo'",
        )
        assert classify_failure(result) == CodeFailureType.IMPORT_ERROR

    def test_classify_module_not_found(self):
        """ModuleNotFoundError -> IMPORT_ERROR."""
        result = StructuredExecutionResult(
            success=False,
            output="",
            error_name="ModuleNotFoundError",
            error_value="No module named 'bar'",
        )
        assert classify_failure(result) == CodeFailureType.IMPORT_ERROR

    def test_classify_assertion_error(self):
        """AssertionError -> ASSERTION_FAILURE."""
        result = StructuredExecutionResult(
            success=False,
            output="",
            error_name="AssertionError",
            error_value="expected True",
        )
        assert classify_failure(result) == CodeFailureType.ASSERTION_FAILURE

    def test_classify_timeout(self):
        """TimeoutError -> TIMEOUT."""
        result = StructuredExecutionResult(
            success=False, output="", error_name="TimeoutError", error_value=""
        )
        assert classify_failure(result) == CodeFailureType.TIMEOUT

    def test_classify_unknown_as_sandbox_error(self):
        """Unknown errors -> SANDBOX_ERROR."""
        result = StructuredExecutionResult(
            success=False,
            output="",
            error_name="RuntimeError",
            error_value="something broke",
        )
        assert classify_failure(result) == CodeFailureType.SANDBOX_ERROR

    def test_classify_timeout_from_value(self):
        """Timeout mentioned in error_value -> TIMEOUT."""
        result = StructuredExecutionResult(
            success=False,
            output="",
            error_name="Error",
            error_value="execution timed out",
        )
        assert classify_failure(result) == CodeFailureType.TIMEOUT

    def test_fixable_failures_set(self):
        """FIXABLE_FAILURES contains SYNTAX_ERROR and IMPORT_ERROR."""
        assert CodeFailureType.SYNTAX_ERROR in FIXABLE_FAILURES
        assert CodeFailureType.IMPORT_ERROR in FIXABLE_FAILURES
        assert len(FIXABLE_FAILURES) == 2

    def test_infrastructure_failures_set(self):
        """INFRASTRUCTURE_FAILURES contains TIMEOUT and SANDBOX_ERROR."""
        assert CodeFailureType.TIMEOUT in INFRASTRUCTURE_FAILURES
        assert CodeFailureType.SANDBOX_ERROR in INFRASTRUCTURE_FAILURES
        assert len(INFRASTRUCTURE_FAILURES) == 2

    def test_verification_failures_set(self):
        """VERIFICATION_FAILURES contains ASSERTION_FAILURE."""
        assert CodeFailureType.ASSERTION_FAILURE in VERIFICATION_FAILURES
        assert len(VERIFICATION_FAILURES) == 1


class TestCodeExecutionResult:
    """Tests for CodeExecutionResult dataclass."""

    def test_is_success(self):
        """is_success returns True for SUCCESS type."""
        result = CodeExecutionResult(
            failure_type=CodeFailureType.SUCCESS, assertions_passed=True
        )
        assert result.is_success

    def test_is_not_success(self):
        """is_success returns False for non-SUCCESS types."""
        result = CodeExecutionResult(failure_type=CodeFailureType.ASSERTION_FAILURE)
        assert not result.is_success

    def test_is_fixable(self):
        """is_fixable returns True for fixable failure types."""
        result = CodeExecutionResult(failure_type=CodeFailureType.SYNTAX_ERROR)
        assert result.is_fixable
        result = CodeExecutionResult(failure_type=CodeFailureType.IMPORT_ERROR)
        assert result.is_fixable

    def test_is_not_fixable(self):
        """is_fixable returns False for non-fixable types."""
        result = CodeExecutionResult(failure_type=CodeFailureType.ASSERTION_FAILURE)
        assert not result.is_fixable

    def test_is_infrastructure_failure(self):
        """is_infrastructure_failure returns True for infra types."""
        result = CodeExecutionResult(failure_type=CodeFailureType.TIMEOUT)
        assert result.is_infrastructure_failure
        result = CodeExecutionResult(failure_type=CodeFailureType.SANDBOX_ERROR)
        assert result.is_infrastructure_failure

    def test_is_verification_failure(self):
        """is_verification_failure returns True for ASSERTION_FAILURE."""
        result = CodeExecutionResult(failure_type=CodeFailureType.ASSERTION_FAILURE)
        assert result.is_verification_failure

    def test_to_dict(self):
        """to_dict serializes correctly."""
        result = CodeExecutionResult(
            failure_type=CodeFailureType.SUCCESS,
            output="ALL_ASSERTIONS_PASSED",
            assertions_passed=True,
            execution_time_ms=42,
        )
        d = result.to_dict()
        assert d["failure_type"] == "SUCCESS"
        assert d["output"] == "ALL_ASSERTIONS_PASSED"
        assert d["assertions_passed"] is True
        assert d["execution_time_ms"] == 42

    def test_to_dict_with_error(self):
        """to_dict includes error_message."""
        result = CodeExecutionResult(
            failure_type=CodeFailureType.ASSERTION_FAILURE,
            output="",
            error_message="AssertionError: bad",
            assertions_passed=False,
        )
        d = result.to_dict()
        assert d["failure_type"] == "ASSERTION_FAILURE"
        assert d["error_message"] == "AssertionError: bad"
        assert d["assertions_passed"] is False


class TestSandboxUnavailability:
    """Tests for graceful sandbox unavailability handling."""

    @pytest.mark.asyncio
    async def test_execute_verification_code_sandbox_unavailable(
        self, unavailable_sandbox
    ):
        """execute_verification_code returns SANDBOX_ERROR when sandbox unavailable."""
        result = await execute_verification_code(
            code="assert True",
            executor=unavailable_sandbox,
        )

        assert result.failure_type == CodeFailureType.SANDBOX_ERROR
        assert not result.is_success
        assert result.is_infrastructure_failure
        assert "not available" in result.error_message.lower()

    @pytest.mark.asyncio
    async def test_evaluator_no_crash_sandbox_unavailable(
        self, mock_memory_manager, unavailable_sandbox
    ):
        """Evaluator does NOT crash when sandbox is unavailable -- falls back silently."""
        evaluator = create_evaluator_node(
            memory_manager=mock_memory_manager,
            sandbox_executor=unavailable_sandbox,
        )
        state = {
            "draft": "Response.",
            "iteration": 1,
            "verification_code": "assert True",
            "code_executions_used": 0,
            "max_code_executions": 2,
        }

        with patch("daem0nmcp.reflexion.nodes.verify_claims") as mock_verify:
            mock_verify.return_value = []
            with patch(
                "daem0nmcp.reflexion.nodes.summarize_verification"
            ) as mock_summary:
                mock_summary.return_value = {
                    "verified_count": 0,
                    "unverified_count": 0,
                    "conflict_count": 0,
                    "overall_confidence": 0.7,
                    "conflicts": [],
                }
                # Should not crash
                result = await evaluator(state)

        # The evaluator should still produce valid output
        assert "quality_score" in result
        assert "critique" in result
        # Score should still be 0.7 (infrastructure error = no impact)
        assert result["quality_score"] == pytest.approx(0.7, abs=0.01)


# ===========================================================================
# State Extension Tests
# ===========================================================================


class TestStateExtension:
    """Tests for state field extensions."""

    def test_state_has_code_fields(self):
        """ReflexionState TypedDict has code-augmented fields."""
        from daem0nmcp.reflexion.state import ReflexionState

        annotations = ReflexionState.__annotations__
        assert "code_executions_used" in annotations
        assert "max_code_executions" in annotations
        assert "code_verification_results" in annotations
        assert "verification_code" in annotations

    def test_run_reflexion_accepts_sandbox_executor(self):
        """run_reflexion function accepts sandbox_executor parameter."""
        import inspect

        sig = inspect.signature(run_reflexion)
        assert "sandbox_executor" in sig.parameters

    def test_build_reflexion_graph_accepts_sandbox_executor(self):
        """build_reflexion_graph accepts sandbox_executor parameter."""
        import inspect

        sig = inspect.signature(build_reflexion_graph)
        assert "sandbox_executor" in sig.parameters

    def test_create_evaluator_node_accepts_sandbox_executor(self):
        """create_evaluator_node accepts sandbox_executor parameter."""
        import inspect

        sig = inspect.signature(create_evaluator_node)
        assert "sandbox_executor" in sig.parameters


# ===========================================================================
# Integration: code_verification_results accumulation
# ===========================================================================


class TestCodeVerificationResultsAccumulation:
    """Tests for code_verification_results accumulation across iterations."""

    @pytest.mark.asyncio
    async def test_code_results_returned_as_list(self, mock_memory_manager):
        """Evaluator returns code_verification_results as a list."""
        mock_code_result = CodeExecutionResult(
            failure_type=CodeFailureType.SUCCESS,
            output="ALL_ASSERTIONS_PASSED",
            assertions_passed=True,
            execution_time_ms=30,
        )

        evaluator = create_evaluator_node(
            memory_manager=mock_memory_manager,
            sandbox_executor=AsyncMock(),
        )
        state = {
            "draft": "Response.",
            "iteration": 1,
            "verification_code": "assert True",
            "code_executions_used": 0,
            "max_code_executions": 2,
        }

        with patch("daem0nmcp.reflexion.nodes.verify_claims") as mock_verify:
            mock_verify.return_value = []
            with patch(
                "daem0nmcp.reflexion.nodes.summarize_verification"
            ) as mock_summary:
                mock_summary.return_value = {
                    "verified_count": 0,
                    "unverified_count": 0,
                    "conflict_count": 0,
                    "overall_confidence": 0.7,
                    "conflicts": [],
                }
                with patch(
                    "daem0nmcp.reflexion.code_exec.execute_verification_code",
                    return_value=mock_code_result,
                ):
                    result = await evaluator(state)

        assert isinstance(result["code_verification_results"], list)
        assert len(result["code_verification_results"]) == 1
        assert result["code_verification_results"][0]["failure_type"] == "SUCCESS"

    @pytest.mark.asyncio
    async def test_empty_code_results_when_no_execution(self, mock_memory_manager):
        """Evaluator returns empty code_verification_results when no code executed."""
        evaluator = create_evaluator_node(
            memory_manager=mock_memory_manager,
            sandbox_executor=None,
        )
        state = {
            "draft": "Response.",
            "iteration": 1,
        }

        with patch("daem0nmcp.reflexion.nodes.verify_claims") as mock_verify:
            mock_verify.return_value = []
            with patch(
                "daem0nmcp.reflexion.nodes.summarize_verification"
            ) as mock_summary:
                mock_summary.return_value = {
                    "verified_count": 0,
                    "unverified_count": 0,
                    "conflict_count": 0,
                    "overall_confidence": 0.7,
                    "conflicts": [],
                }
                result = await evaluator(state)

        assert result["code_verification_results"] == []
