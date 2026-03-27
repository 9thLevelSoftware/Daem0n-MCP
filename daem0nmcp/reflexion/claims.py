"""Claim detection for the Reflexion loop.

Pattern-based extraction of verifiable claims from text.
Per CONTEXT.md: "Claims extracted via simple pattern matching (not LLM-based claim extraction)".

Claims are categorized by verification level:
- mandatory: Claims about stored memories, past decisions (MUST verify against memory store)
- best_effort: Factual assertions about code/behavior (check against indexed entities if available)
- skip: Opinions, hypotheticals, echoed user info (no verification needed)
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum


class ClaimType(str, Enum):
    """Type of claim detected."""

    MEMORY_REFERENCE = "memory_reference"  # References to stored memories/decisions
    FACTUAL_ASSERTION = "factual_assertion"  # Assertions about code/behavior
    OUTCOME_REFERENCE = "outcome_reference"  # References to past outcomes


class VerificationLevel(str, Enum):
    """How strictly the claim should be verified."""

    MANDATORY = "mandatory"  # Must verify against memory store
    BEST_EFFORT = "best_effort"  # Check if indexed; don't fail if not found
    SKIP = "skip"  # No verification needed


@dataclass
class Claim:
    """A verifiable claim extracted from text."""

    text: str  # The claim text as found
    claim_type: ClaimType  # Category of claim
    verification_level: VerificationLevel
    subject: str | None = None  # The subject being claimed about
    predicate: str | None = None  # What's being claimed about the subject
    source_span: tuple[int, int] = field(
        default_factory=lambda: (0, 0)
    )  # Character positions

    def __hash__(self) -> int:
        """Hash by normalized subject for deduplication."""
        return hash((self.subject or "").lower())

    def __eq__(self, other: object) -> bool:
        """Equal if same subject (case-insensitive)."""
        if not isinstance(other, Claim):
            return False
        return (self.subject or "").lower() == (other.subject or "").lower()


# Opinion/hypothetical markers - these indicate claims should be skipped
OPINION_PATTERNS = [
    r"\b(?:I\s+)?think\b",
    r"\b(?:I\s+)?believe\b",
    r"\bin\s+my\s+opinion\b",
    r"\bprobably\b",
    r"\bmaybe\b",
    r"\bperhaps\b",
    r"\bmight\b(?!\s+have\s+(?:decided|mentioned|said))",  # "might" but not "might have decided"
    r"\bcould\b(?!\s+have\s+(?:decided|mentioned|said))",  # "could" but not "could have decided"
    r"\bif\s+we\b",
    r"\bhypothetically\b",
    r"\bit's\s+possible\b",
    r"\bwould\s+be\b",
    r"\bseems?\s+like\b",
    r"\bi\s+guess\b",
    r"\bnot\s+sure\b",
]

# Memory reference patterns - mandatory verification
MEMORY_PATTERNS = [
    # "We decided..." patterns
    (r"\bwe\s+decided\s+(?:to\s+)?(.+?)(?:\.|,|$)", "decision"),
    (r"\bwe\s+agreed\s+(?:to\s+|on\s+)?(.+?)(?:\.|,|$)", "agreement"),
    (r"\bwe\s+chose\s+(?:to\s+)?(.+?)(?:\.|,|$)", "choice"),
    # "I remember..." patterns
    (r"\bi\s+remember\s+(?:that\s+)?(.+?)(?:\.|,|$)", "memory"),
    (r"\bas\s+i\s+recall\b[,]?\s*(.+?)(?:\.|,|$)", "memory"),
    # "You/We mentioned..." patterns
    (r"\byou\s+(?:mentioned|said)\s+(?:that\s+)?(.+?)(?:\.|,|$)", "user_statement"),
    (
        r"\bwe\s+(?:mentioned|discussed|talked\s+about)\s+(?:that\s+)?(.+?)(?:\.|,|$)",
        "discussion",
    ),
    # "Previously..." patterns
    (r"\bpreviously[,]?\s+(?:we\s+)?(.+?)(?:\.|,|$)", "previous"),
    (r"\bearlier[,]?\s+(?:we\s+)?(.+?)(?:\.|,|$)", "earlier"),
    (r"\blast\s+(?:time|session)[,]?\s+(?:we\s+)?(.+?)(?:\.|,|$)", "last_time"),
    (r"\bbefore[,]?\s+(?:we\s+)?(.+?)(?:\.|,|$)", "before"),
]

# Outcome reference patterns - mandatory verification
OUTCOME_PATTERNS = [
    (
        r"\bthat\s+(?:approach|method|solution)\s+(worked|failed|succeeded|broke)",
        "outcome",
    ),
    (r"\bit\s+(worked|failed|succeeded|broke)(?:\s+(?:well|badly|great))?", "outcome"),
    (r"\bthis\s+(worked|failed|succeeded|broke)", "outcome"),
    (r"\bwe\s+(?:tried|tested)\s+(?:that\s+)?and\s+it\s+(worked|failed)", "outcome"),
    (r"\bthe\s+result\s+was\s+(.+?)(?:\.|,|$)", "outcome"),
    # Generic "X failed/worked/succeeded" pattern
    (r"\bthe\s+(\w+(?:\s+\w+)?)\s+(worked|failed|succeeded|broke)", "outcome_subject"),
]

# Factual assertion patterns - best effort verification
FACTUAL_PATTERNS = [
    # "X is Y" patterns
    (r"(\w+(?:\s+\w+)?)\s+is\s+(?:a\s+)?(\w+(?:\s+\w+){0,3})(?:\.|,|$)", "identity"),
    # "X uses Y" patterns
    (r"(\w+(?:\s+\w+)?)\s+uses?\s+(\w+(?:\s+\w+){0,3})(?:\.|,|$)", "usage"),
    # "X supports Y" patterns
    (r"(\w+(?:\s+\w+)?)\s+supports?\s+(\w+(?:\s+\w+){0,3})(?:\.|,|$)", "support"),
    # "X returns Y" patterns
    (r"(\w+(?:\s+\w+)?)\s+returns?\s+(\w+(?:\s+\w+){0,3})(?:\.|,|$)", "return_value"),
    # "X has Y" patterns
    (r"(\w+(?:\s+\w+)?)\s+has\s+(?:a\s+)?(\w+(?:\s+\w+){0,3})(?:\.|,|$)", "property"),
    # "X requires Y" patterns
    (r"(\w+(?:\s+\w+)?)\s+requires?\s+(\w+(?:\s+\w+){0,3})(?:\.|,|$)", "requirement"),
]


# Patterns indicating a claim can be verified by running Python code
CODE_VERIFIABLE_PATTERNS = [
    # Numeric claims: "X is 42", "there are N items", "the result is 3.14"
    r"\b(?:is|are|equals?|returns?|gives?|produces?|yields?)\s+\d+(?:\.\d+)?",
    # Date/time claims: "released in 2023", "started on January", "6 months ago"
    r"\b(?:in|on|since|from|before|after)\s+(?:\d{4}|January|February|March|April|May|June|July|August|September|October|November|December)",
    # Code behavior claims: "returns None", "raises ValueError", "is O(n)"
    r"\b(?:returns?|raises?|throws?|produces?)\s+(?:None|True|False|[A-Z][a-zA-Z]*Error)",
    # Mathematical claims: "sum of", "average is", "maximum is"
    r"\b(?:sum|average|mean|median|maximum|minimum|count|total)\s+(?:of|is|are|equals?)",
    # Python-specific: "list.sort()", "len() returns", "range() produces"
    r"\b(?:list|dict|set|tuple|str|int|float|len|range|sorted|zip|map|filter)\b.*(?:returns?|is|produces?)",
]


def is_code_verifiable(claim: Claim) -> bool:
    """Check if a claim can be verified by executing Python code.

    Code-verifiable claims include:
    - Numeric assertions (e.g., "Python 3.12 was released in October 2023")
    - Date calculations (e.g., "The project started 6 months ago")
    - Code behavior claims (e.g., "list.sort() returns None")
    - Mathematical assertions (e.g., "the sum of 1 to 100 is 5050")

    Memory references and outcome references are NOT code-verifiable --
    they require memory store verification (existing flow).

    Args:
        claim: The Claim object to evaluate

    Returns:
        True if the claim is suitable for code verification
    """
    # Memory references and outcome references are verified against memory, not code
    if claim.claim_type in (ClaimType.MEMORY_REFERENCE, ClaimType.OUTCOME_REFERENCE):
        return False

    # Only factual assertions might be code-verifiable
    if claim.claim_type != ClaimType.FACTUAL_ASSERTION:
        return False

    # Check text against code-verifiable patterns
    text = claim.text.lower()
    for pattern in CODE_VERIFIABLE_PATTERNS:
        if re.search(pattern, text, re.IGNORECASE):
            return True

    return False


def is_opinion(text: str) -> bool:
    """Check if text contains opinion/hypothetical markers.

    Args:
        text: Text to check

    Returns:
        True if the text appears to be an opinion or hypothetical
    """
    text_lower = text.lower()
    for pattern in OPINION_PATTERNS:
        if re.search(pattern, text_lower, re.IGNORECASE):
            return True
    return False


def _extract_subject_predicate(
    match: re.Match, pattern_type: str
) -> tuple[str | None, str | None]:
    """Extract subject and predicate from a regex match.

    Args:
        match: The regex match object
        pattern_type: Type of pattern that matched

    Returns:
        Tuple of (subject, predicate)
    """
    groups = match.groups()

    if pattern_type in (
        "identity",
        "usage",
        "support",
        "return_value",
        "property",
        "requirement",
    ):
        # Two-group patterns: subject verb predicate
        if len(groups) >= 2:
            return groups[0].strip(), groups[1].strip()

    if pattern_type in (
        "decision",
        "agreement",
        "choice",
        "memory",
        "discussion",
        "previous",
        "earlier",
        "last_time",
        "before",
        "user_statement",
    ):
        # Single-group patterns: full claim is the subject
        if groups:
            return groups[0].strip(), None

    if pattern_type == "outcome":
        # Outcome patterns: the outcome word is the predicate
        if groups:
            return match.group(0).strip(), groups[0].strip()

    if pattern_type == "outcome_subject":
        # "the X failed" patterns: X is subject, outcome is predicate
        if len(groups) >= 2:
            return groups[0].strip(), groups[1].strip()

    return None, None


def extract_claims(text: str) -> list[Claim]:
    """Extract verifiable claims from text.

    Detects:
    1. Memory references (mandatory verification)
    2. Outcome references (mandatory verification)
    3. Factual assertions (best_effort verification)

    Skips opinions and hypotheticals.
    Deduplicates claims with the same subject.

    Args:
        text: Text to extract claims from

    Returns:
        List of Claim objects, deduplicated by subject
    """
    if not text or len(text) < 10:
        return []

    # Skip if the entire text appears to be an opinion
    if is_opinion(text):
        return []

    claims: list[Claim] = []
    seen_subjects: set[str] = set()

    # Check for memory references (mandatory verification)
    for pattern, pattern_type in MEMORY_PATTERNS:
        for match in re.finditer(pattern, text, re.IGNORECASE):
            claim_text = match.group(0).strip()

            # Skip if this specific claim is an opinion
            if is_opinion(claim_text):
                continue

            subject, predicate = _extract_subject_predicate(match, pattern_type)

            # Deduplicate by subject
            subject_key = (subject or claim_text).lower()
            if subject_key in seen_subjects:
                continue
            seen_subjects.add(subject_key)

            claims.append(
                Claim(
                    text=claim_text,
                    claim_type=ClaimType.MEMORY_REFERENCE,
                    verification_level=VerificationLevel.MANDATORY,
                    subject=subject,
                    predicate=predicate,
                    source_span=(match.start(), match.end()),
                )
            )

    # Check for outcome references (mandatory verification)
    for pattern, pattern_type in OUTCOME_PATTERNS:
        for match in re.finditer(pattern, text, re.IGNORECASE):
            claim_text = match.group(0).strip()

            if is_opinion(claim_text):
                continue

            subject, predicate = _extract_subject_predicate(match, pattern_type)

            subject_key = (subject or claim_text).lower()
            if subject_key in seen_subjects:
                continue
            seen_subjects.add(subject_key)

            claims.append(
                Claim(
                    text=claim_text,
                    claim_type=ClaimType.OUTCOME_REFERENCE,
                    verification_level=VerificationLevel.MANDATORY,
                    subject=subject,
                    predicate=predicate,
                    source_span=(match.start(), match.end()),
                )
            )

    # Check for factual assertions (best effort verification)
    for pattern, pattern_type in FACTUAL_PATTERNS:
        for match in re.finditer(pattern, text, re.IGNORECASE):
            claim_text = match.group(0).strip()

            if is_opinion(claim_text):
                continue

            subject, predicate = _extract_subject_predicate(match, pattern_type)

            # Skip very short subjects (likely false positives)
            if subject and len(subject) < 2:
                continue

            subject_key = (subject or claim_text).lower()
            if subject_key in seen_subjects:
                continue
            seen_subjects.add(subject_key)

            claims.append(
                Claim(
                    text=claim_text,
                    claim_type=ClaimType.FACTUAL_ASSERTION,
                    verification_level=VerificationLevel.BEST_EFFORT,
                    subject=subject,
                    predicate=predicate,
                    source_span=(match.start(), match.end()),
                )
            )

    return claims


__all__ = [
    "Claim",
    "ClaimType",
    "VerificationLevel",
    "extract_claims",
    "is_opinion",
    "is_code_verifiable",
    "CODE_VERIFIABLE_PATTERNS",
]
