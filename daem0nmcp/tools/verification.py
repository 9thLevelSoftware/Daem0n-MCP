"""Fact verification tools: verify_facts + _build_verification_message helper."""

import contextlib
import logging
from typing import Any

try:
    from .. import __version__
    from ..context_manager import (
        _default_project_path,
        _missing_project_path_error,
        get_project_context,
        hold_context,
    )
    from ..logging_config import with_request_id
    from ..mcp_instance import mcp
except ImportError:
    from daem0nmcp import __version__
    from daem0nmcp.context_manager import (
        _default_project_path,
        _missing_project_path_error,
        get_project_context,
        hold_context,
    )
    from daem0nmcp.logging_config import with_request_id
    from daem0nmcp.mcp_instance import mcp

from ._deprecation import add_deprecation

logger = logging.getLogger(__name__)


def _build_verification_message(summary: dict[str, Any]) -> str:
    """Build human-readable verification message."""
    parts = []

    if summary["verified_count"] > 0:
        parts.append(f"{summary['verified_count']} claim(s) verified")

    if summary["unverified_count"] > 0:
        parts.append(f"{summary['unverified_count']} claim(s) unverified [unverified]")

    if summary["conflict_count"] > 0:
        parts.append(
            f"{summary['conflict_count']} claim(s) CONFLICT with stored knowledge"
        )

    if not parts:
        return "No claims to verify"

    confidence_desc = (
        "high"
        if summary["overall_confidence"] >= 0.8
        else "medium"
        if summary["overall_confidence"] >= 0.5
        else "low"
    )

    return f"{'; '.join(parts)}. Overall confidence: {confidence_desc} ({summary['overall_confidence']:.1%})"


# ============================================================================
# Tool 11: VERIFY_FACTS - Verify factual claims against stored knowledge
# ============================================================================
@mcp.tool(version=__version__)
@with_request_id
async def verify_facts(
    text: str,
    categories: list[str] | None = None,
    as_of_time: str | None = None,
    project_path: str | None = None,
) -> dict[str, Any]:
    """
    [DEPRECATED] Use reflect(action='verify') instead.

    Verify factual claims in text against stored knowledge.

    Extracts claims from the provided text and verifies them against:
    1. Stored memories (via recall)
    2. GraphRAG entities (if available)
    3. Bi-temporal history (if as_of_time provided)

    Per CONTEXT.md: Verification failures surface as "[unverified]" markers,
    not hard blocks.

    Args:
        text: Text containing claims to verify
        categories: Optional list of memory categories to search
        as_of_time: Optional ISO 8601 datetime for point-in-time verification
        project_path: Project root

    Returns:
        Dict with:
        - claims: List of extracted claims
        - verified: List of verified claims with evidence
        - unverified: List of unverified claims
        - conflicts: List of conflicting claims with reasons
        - summary: Overall verification summary
    """
    # Require project_path for multi-project support
    if not project_path and not _default_project_path:
        return _missing_project_path_error()

    ctx = await get_project_context(project_path)

    # Hold context for entire Reflexion loop to prevent mid-loop eviction
    async with hold_context(ctx):
        # Import reflexion modules
        try:
            from ..reflexion.claims import extract_claims
            from ..reflexion.verification import summarize_verification, verify_claims
        except ImportError:
            from daem0nmcp.reflexion.claims import extract_claims
            from daem0nmcp.reflexion.verification import (
                summarize_verification,
                verify_claims,
            )

        # Extract claims from text
        claims = extract_claims(text)

        if not claims:
            return {
                "claims": [],
                "verified": [],
                "unverified": [],
                "conflicts": [],
                "summary": {
                    "verified_count": 0,
                    "unverified_count": 0,
                    "conflict_count": 0,
                    "overall_confidence": 1.0,
                    "message": "No verifiable claims found in text",
                },
            }

        # Get knowledge graph if available
        knowledge_graph = None
        with contextlib.suppress(Exception):
            knowledge_graph = await ctx.memory_manager.get_knowledge_graph()

        # Verify claims
        verification_results = await verify_claims(
            claims=claims,
            memory_manager=ctx.memory_manager,
            knowledge_graph=knowledge_graph,
            as_of_time=as_of_time,
            categories=categories,
        )

        # Categorize results
        verified = []
        unverified = []
        conflicts = []

        for result in verification_results:
            result_dict = {
                "claim": result.claim_text,
                "type": result.claim_type,
                "confidence": round(result.confidence, 3),
                "evidence": [
                    {"source": e.source, "content": e.content[:100]}
                    for e in result.evidence[:3]  # Limit evidence for readability
                ],
            }

            if result.status == "verified":
                verified.append(result_dict)
            elif result.status == "conflict":
                result_dict["conflict_reason"] = result.conflict_reason
                conflicts.append(result_dict)
            else:
                unverified.append(result_dict)

        # Build summary
        summary = summarize_verification(verification_results)
        summary["message"] = _build_verification_message(summary)

        result = {
            "claims": [
                {"text": c.text, "type": c.claim_type.value, "subject": c.subject}
                for c in claims
            ],
            "verified": verified,
            "unverified": unverified,
            "conflicts": conflicts,
            "summary": summary,
        }
        return add_deprecation(result, "verify_facts", "reflect(action='verify')")
