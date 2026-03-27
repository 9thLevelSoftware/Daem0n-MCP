"""
Code Generation for Verifiable Claims.

Generates Python assertion code that can be executed in a sandbox
to verify factual claims extracted from the Actor's draft response.

The Actor generates verification code; the Evaluator executes it (Plan 02/03).
Code generation uses the LLM function if available, falling back to
template-based generation for common patterns.

Phase 14: Code-Augmented Reflexion
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .claims import Claim

from .claims import is_code_verifiable

logger = logging.getLogger(__name__)


def generate_verification_code(
    claims: list[Claim],
    draft: str,
    llm_func: Callable[[str], str] | None = None,
) -> str | None:
    """Generate Python assertion code for code-verifiable claims.

    Filters claims to only code-verifiable ones, then generates Python
    code that asserts the claim's truth. Uses llm_func if available
    for semantic understanding; falls back to template-based generation.

    Args:
        claims: All extracted claims from the draft
        draft: The full draft text (for LLM context)
        llm_func: Optional LLM function for generating verification code.
                   If None, uses template-based fallback.

    Returns:
        Python code string with assertions, or None if no code-verifiable claims found.
    """
    verifiable = [c for c in claims if is_code_verifiable(c)]

    if not verifiable:
        logger.debug("No code-verifiable claims found in draft")
        return None

    logger.info(f"Found {len(verifiable)} code-verifiable claims for verification")

    if llm_func is not None:
        return _generate_via_llm(verifiable, draft, llm_func)
    else:
        return _generate_via_templates(verifiable)


def _generate_via_llm(
    claims: list[Claim],
    draft: str,
    llm_func: Callable[[str], str],
) -> str | None:
    """Generate verification code using LLM."""
    claims_text = "\n".join(
        f"  {i + 1}. [{c.claim_type.value}] {c.text}" for i, c in enumerate(claims)
    )

    prompt = f"""Generate Python code that verifies the following claims using assertions.
Each assertion should test whether the claim is factually correct.
Use only Python standard library (no external packages).
If a claim cannot be verified with code, skip it with a comment.
End with: print('ALL_ASSERTIONS_PASSED')

Claims to verify:
{claims_text}

Context (the draft containing these claims):
{draft[:500]}

Rules:
- Use assert statements with descriptive messages
- Each assertion should be self-contained
- Handle potential exceptions with try/except
- If verification requires network or external data, skip with comment
- Keep code simple and readable

Output ONLY the Python code, no explanations."""

    try:
        code = llm_func(prompt)
        code = _strip_code_fences(code)
        if code and len(code.strip()) > 10:
            return code.strip()
        logger.warning("LLM returned empty or trivial verification code")
        return _generate_via_templates(claims)
    except Exception as e:
        logger.warning(f"LLM code generation failed: {e}, falling back to templates")
        return _generate_via_templates(claims)


def _generate_via_templates(claims: list[Claim]) -> str | None:
    """Generate verification code using pattern templates."""
    lines = ["# Auto-generated verification code (template-based)"]
    generated_any = False

    for claim in claims:
        assertion = _template_assertion(claim)
        if assertion:
            lines.append(f"# Verify: {claim.text}")
            lines.append(assertion)
            lines.append("")
            generated_any = True

    if not generated_any:
        logger.debug("No template-based assertions could be generated")
        return None

    lines.append("print('ALL_ASSERTIONS_PASSED')")
    return "\n".join(lines)


def _template_assertion(claim: Claim) -> str | None:
    """Generate a single assertion from a claim using pattern matching."""
    text = claim.text

    # Pattern: "X returns None/True/False"
    match = re.search(
        r"(\w+(?:\.\w+)*\(\))\s+returns?\s+(None|True|False)", text, re.IGNORECASE
    )
    if match:
        func_call = match.group(1)
        expected = match.group(2)
        return (
            f"try:\n"
            f"    result = {func_call}\n"
            f"    assert result is {expected}, "
            f'f"{func_call} returned {{result}}, expected {expected}"\n'
            f"except Exception as e:\n"
            f'    assert False, f"Could not verify: {{e}}"'
        )

    # Pattern: "returns ValueError/TypeError/etc"
    match = re.search(
        r"(\w+(?:\.\w+)*\(.*?\))\s+(?:raises?|throws?)\s+(\w+Error)",
        text,
        re.IGNORECASE,
    )
    if match:
        func_call = match.group(1)
        error_type = match.group(2)
        return (
            f"try:\n"
            f"    {func_call}\n"
            f'    assert False, "{func_call} should raise {error_type}"\n'
            f"except {error_type}:\n"
            f"    pass  # Expected\n"
            f"except Exception as e:\n"
            f'    assert False, f"Expected {error_type}, got {{type(e).__name__}}: {{e}}"'
        )

    # Pattern: numeric result "sum of X to Y is Z" or "result is N"
    match = re.search(
        r"(?:sum|total|result|answer|value)\s+(?:of\s+)?.*?(?:is|equals?|=)\s+(\d+(?:\.\d+)?)",
        text,
        re.IGNORECASE,
    )
    if match:
        expected_value = match.group(1)
        return (
            f"# Numeric claim: {text}\n"
            f"# Note: template cannot determine computation, marking for LLM verification\n"
            f"expected = {expected_value}\n"
            f"print(f'Numeric claim expects: {{expected}}')"
        )

    return None


def _strip_code_fences(text: str) -> str:
    """Strip markdown code fences from LLM output."""
    match = re.search(r"```(?:python)?\s*\n(.*?)```", text, re.DOTALL)
    if match:
        return match.group(1)
    return text


__all__ = [
    "generate_verification_code",
]
