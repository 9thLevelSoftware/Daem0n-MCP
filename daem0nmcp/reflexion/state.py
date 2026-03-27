"""
State definitions for the Reflexion loop.

Uses TypedDict for LangGraph state schema. Annotated fields with operator.add
cause list accumulation; scalar fields overwrite.
"""

import operator
from typing import Annotated

from typing_extensions import TypedDict


class VerificationResult(TypedDict):
    """Result of verifying a single claim."""

    claim_text: str
    claim_type: str  # "memory_reference", "factual_assertion"
    status: str  # "verified", "unverified", "conflict"
    evidence: dict | None  # Supporting/contradicting evidence


class ReflexionState(TypedDict):
    """
    State for Actor-Evaluator-Reflector loop.

    Scalar fields (query, draft, critique, quality_score, iteration) overwrite.
    List fields with Annotated[..., operator.add] accumulate across iterations.

    Fields:
        query: The original query to respond to
        draft: The current draft response from Actor
        critique: Verbal gradient describing issues from Evaluator
        quality_score: 0.0-1.0, early exit if >= 0.8
        claims: Extracted claims (accumulated across iterations)
        verification_results: Verification results (accumulated)
        iteration: Current iteration (1, 2, 3), hard cap at 3
        should_continue: Set by evaluator based on score + iteration
        context_filter: Categories, tags to filter verification queries
        ritual_phase: Current ritual phase for tool visibility filtering (Phase 5 Agency)
    """

    # Input - the original query to respond to
    query: str

    # Actor output - the current draft response
    draft: str

    # Evaluator output
    critique: str  # Verbal gradient describing issues
    quality_score: float  # 0.0-1.0, early exit if >= 0.8

    # Claim verification
    claims: Annotated[list[dict], operator.add]  # Extracted claims
    verification_results: Annotated[list[VerificationResult], operator.add]

    # Loop control
    iteration: int  # Current iteration (1, 2, 3), hard cap at 3
    should_continue: bool  # Set by evaluator based on score + iteration

    # Optional context for claim verification
    context_filter: dict | None  # Categories, tags to filter verification queries

    # Ritual phase for tool visibility (Phase 5 Agency)
    # Values: "briefing" | "exploration" | "action" | "reflection"
    # Optional with default "briefing" - existing code that doesn't set it still works
    ritual_phase: str | None

    # Code-augmented verification (Phase 14)
    code_executions_used: int  # Counter: how many code executions consumed this run
    max_code_executions: int  # Budget: default 2, separate from MAX_ITERATIONS
    code_verification_results: Annotated[
        list[dict], operator.add
    ]  # Accumulated code execution results
    verification_code: str | None  # Latest generated verification code from Actor
