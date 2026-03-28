"""
Claim Verification for Reflexion Loop.

Verifies claims against stored knowledge:
1. Memory recall (MemoryManager.recall)
2. GraphRAG entity lookup (KnowledgeGraph)
3. Bi-temporal point-in-time queries

Per CONTEXT.md: Verification failures surface as "[unverified]" markers, not hard blocks.
"""

from __future__ import annotations

import contextlib
import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Any

from ..graph.contradiction import has_negation_mismatch
from ..vectors import cosine_similarity, decode, encode_document, encode_query
from .claims import VerificationLevel

if TYPE_CHECKING:
    from ..graph import KnowledgeGraph
    from ..memory import MemoryManager
    from .claims import Claim

logger = logging.getLogger(__name__)


@dataclass
class VerificationEvidence:
    """Evidence supporting or contradicting a claim."""

    source: str  # "memory", "entity", "temporal"
    content: str  # The actual evidence text
    similarity: float  # How similar to the claim
    memory_id: int | None = None
    entity_id: int | None = None


@dataclass
class VerificationResult:
    """Result of verifying a single claim."""

    claim_text: str
    claim_type: str
    status: str  # "verified", "unverified", "conflict"
    confidence: float  # 0.0-1.0 confidence in the verdict
    evidence: list[VerificationEvidence] = field(default_factory=list)
    conflict_reason: str | None = None  # If status is "conflict"


# Thresholds
SIMILARITY_THRESHOLD_SUPPORT = 0.7  # Similarity to consider as supporting evidence
SIMILARITY_THRESHOLD_CONFLICT = 0.75  # Higher bar for conflict detection


async def verify_claim(
    claim: Claim,
    memory_manager: MemoryManager,
    knowledge_graph: KnowledgeGraph | None = None,
    as_of_time: str | datetime | None = None,
    categories: list[str] | None = None,
) -> VerificationResult:
    """
    Verify a single claim against stored knowledge.

    Verification sources (in order):
    1. Memory recall - search for memories related to claim subject
    2. GraphRAG entities - look up entities mentioned in claim
    3. Bi-temporal - if as_of_time provided, check historical state

    Args:
        claim: The Claim object to verify
        memory_manager: MemoryManager instance for recall
        knowledge_graph: Optional KnowledgeGraph for entity lookup
        as_of_time: Optional ISO 8601 string or datetime for point-in-time verification
        categories: Optional list of categories to filter memory search

    Returns:
        VerificationResult with status, confidence, and evidence
    """
    # Get claim type value for result
    claim_type_str = (
        claim.claim_type.value
        if hasattr(claim.claim_type, "value")
        else str(claim.claim_type)
    )

    # Skip verification for skip-level claims (opinions, hypotheticals)
    if claim.verification_level == VerificationLevel.SKIP:
        return VerificationResult(
            claim_text=claim.text,
            claim_type=claim_type_str,
            status="verified",  # Auto-verified (skipped)
            confidence=1.0,
            evidence=[],
        )

    evidence_list: list[VerificationEvidence] = []
    has_support = False
    has_conflict = False
    conflict_reason = None

    # Parse as_of_time string to datetime if provided
    query_time: datetime | None = None
    if as_of_time is not None:
        if isinstance(as_of_time, str):
            try:
                # Parse ISO 8601 format
                query_time = datetime.fromisoformat(as_of_time.replace("Z", "+00:00"))
            except ValueError:
                logger.warning(f"Invalid as_of_time format: {as_of_time}, ignoring")
        elif isinstance(as_of_time, datetime):
            query_time = as_of_time

    # Encode claim for similarity comparison
    claim_embedding_bytes = encode_query(claim.text)
    claim_embedding = decode(claim_embedding_bytes) if claim_embedding_bytes else None

    # 1. Memory recall verification
    try:
        recall_result = await memory_manager.recall(
            topic=claim.subject or claim.text,
            categories=categories,
            limit=5,
            as_of_time=query_time,
        )

        # recall() returns categorized lists: decisions, patterns, warnings, learnings
        # Collect all memories across categories for verification
        memories = []
        for category in ("decisions", "patterns", "warnings", "learnings"):
            memories.extend(recall_result.get(category, []))
        # Fallback for empty-result shape which uses "memories" key
        if not memories:
            memories = recall_result.get("memories", [])
        for memory in memories:
            content = memory.get("content", "")

            # Calculate similarity if we have embeddings
            similarity = 0.0
            if claim_embedding is not None:
                memory_embedding_bytes = encode_document(content)
                if memory_embedding_bytes:
                    memory_embedding = decode(memory_embedding_bytes)
                    if memory_embedding is not None:
                        similarity = cosine_similarity(
                            claim_embedding, memory_embedding
                        )

            if similarity >= SIMILARITY_THRESHOLD_SUPPORT:
                # Check for negation mismatch (conflict)
                negation = has_negation_mismatch(claim.text, content)
                if negation:
                    has_conflict = True
                    conflict_reason = f"Memory contradicts claim: negation pattern {negation[0]} vs {negation[1]}"
                    evidence_list.append(
                        VerificationEvidence(
                            source="memory",
                            content=content[:200],  # Truncate for brevity
                            similarity=similarity,
                            memory_id=memory.get("id"),
                        )
                    )
                else:
                    has_support = True
                    evidence_list.append(
                        VerificationEvidence(
                            source="memory",
                            content=content[:200],
                            similarity=similarity,
                            memory_id=memory.get("id"),
                        )
                    )

    except Exception as e:
        logger.warning(f"Memory recall verification failed: {e}")

    # 2. GraphRAG entity verification (if available)
    if knowledge_graph is not None:
        try:
            await knowledge_graph.ensure_loaded()

            # Get subject for entity search
            search_subject = claim.subject or claim.text

            # Search for entities matching the claim subject
            entity_nodes = [
                node
                for node in knowledge_graph._graph.nodes()
                if node.startswith("entity:")
                and search_subject.lower()
                in knowledge_graph._graph.nodes[node].get("name", "").lower()
            ]

            for entity_node in entity_nodes[:3]:  # Limit to 3 entities
                entity_data = knowledge_graph._graph.nodes[entity_node]
                entity_name = entity_data.get("name", "")

                # Get related memories for this entity
                neighbors = list(knowledge_graph._graph.predecessors(entity_node))
                memory_neighbors = [n for n in neighbors if n.startswith("memory:")]

                if memory_neighbors:
                    has_support = True
                    # Extract entity ID safely
                    entity_id = None
                    with contextlib.suppress(ValueError, IndexError):
                        entity_id = (
                            int(entity_node.split(":")[1])
                            if ":" in entity_node
                            else None
                        )

                    evidence_list.append(
                        VerificationEvidence(
                            source="entity",
                            content=f"Entity '{entity_name}' found with {len(memory_neighbors)} related memories",
                            similarity=0.8,  # High confidence if entity exists
                            entity_id=entity_id,
                        )
                    )

        except Exception as e:
            logger.warning(f"GraphRAG verification failed: {e}")

    # Determine final status
    if has_conflict:
        status = "conflict"
        confidence = 0.9  # High confidence when we detect contradiction
    elif has_support:
        status = "verified"
        # Confidence based on best evidence similarity
        best_similarity = max((e.similarity for e in evidence_list), default=0.5)
        confidence = min(best_similarity, 0.95)  # Cap at 0.95
    else:
        status = "unverified"
        confidence = 0.3  # Low confidence when no evidence found

    return VerificationResult(
        claim_text=claim.text,
        claim_type=claim_type_str,
        status=status,
        confidence=confidence,
        evidence=evidence_list,
        conflict_reason=conflict_reason,
    )


async def verify_claims(
    claims: list[Claim],
    memory_manager: MemoryManager,
    knowledge_graph: KnowledgeGraph | None = None,
    as_of_time: str | None = None,
    categories: list[str] | None = None,
) -> list[VerificationResult]:
    """
    Verify multiple claims against stored knowledge.

    Args:
        claims: List of Claim objects to verify
        memory_manager: MemoryManager instance
        knowledge_graph: Optional KnowledgeGraph for entity lookup
        as_of_time: Optional ISO 8601 datetime for point-in-time verification
        categories: Optional categories filter

    Returns:
        List of VerificationResult objects, one per claim
    """
    results = []
    for claim in claims:
        result = await verify_claim(
            claim=claim,
            memory_manager=memory_manager,
            knowledge_graph=knowledge_graph,
            as_of_time=as_of_time,
            categories=categories,
        )
        results.append(result)

    logger.debug(f"Verified {len(claims)} claims")
    return results


def summarize_verification(results: list[VerificationResult]) -> dict[str, Any]:
    """
    Summarize verification results for quality scoring.

    Returns dict with:
    - verified_count: Number of verified claims
    - unverified_count: Number of unverified claims
    - conflict_count: Number of conflicting claims
    - overall_confidence: Average confidence across claims
    - conflicts: List of conflict details
    """
    verified = [r for r in results if r.status == "verified"]
    unverified = [r for r in results if r.status == "unverified"]
    conflicts = [r for r in results if r.status == "conflict"]

    avg_confidence = (
        sum(r.confidence for r in results) / len(results) if results else 0.5
    )

    return {
        "verified_count": len(verified),
        "unverified_count": len(unverified),
        "conflict_count": len(conflicts),
        "overall_confidence": round(avg_confidence, 3),
        "conflicts": [
            {"claim": c.claim_text, "reason": c.conflict_reason} for c in conflicts
        ],
    }


__all__ = [
    "verify_claim",
    "verify_claims",
    "summarize_verification",
    "VerificationResult",
    "VerificationEvidence",
    "SIMILARITY_THRESHOLD_SUPPORT",
    "SIMILARITY_THRESHOLD_CONFLICT",
]
