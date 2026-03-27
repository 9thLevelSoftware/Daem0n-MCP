"""Tests for claim detection module.

Tests pattern-based extraction of verifiable claims from text.
"""

from daem0nmcp.reflexion.claims import (
    Claim,
    ClaimType,
    VerificationLevel,
    extract_claims,
    is_opinion,
)


class TestIsOpinion:
    """Tests for the is_opinion helper function."""

    def test_think_pattern(self) -> None:
        """'I think' is an opinion."""
        assert is_opinion("I think we should use SQLite")
        assert is_opinion("Think this might work")

    def test_believe_pattern(self) -> None:
        """'I believe' is an opinion."""
        assert is_opinion("I believe this approach is better")

    def test_maybe_probably_perhaps(self) -> None:
        """Uncertainty markers indicate opinions."""
        assert is_opinion("Maybe we should refactor")
        assert is_opinion("Probably the best option")
        assert is_opinion("Perhaps async would help")

    def test_might_could_hypothetical(self) -> None:
        """'might' and 'could' alone indicate opinions."""
        assert is_opinion("We might want to reconsider")
        assert is_opinion("This could be improved")

    def test_if_we_hypothetical(self) -> None:
        """Hypothetical conditionals are opinions."""
        assert is_opinion("If we used async, it would be faster")

    def test_in_my_opinion(self) -> None:
        """Explicit opinion phrase."""
        assert is_opinion("In my opinion, Rust is better")

    def test_factual_statement_not_opinion(self) -> None:
        """Factual statements are not opinions."""
        assert not is_opinion("We decided to use SQLite")
        assert not is_opinion("Python uses indentation for blocks")
        assert not is_opinion("The function returns a list")


class TestExtractMemoryReference:
    """Tests for memory reference claim extraction."""

    def test_extract_memory_reference_decided(self) -> None:
        """'We decided...' is a memory reference claim."""
        claims = extract_claims("We decided to use SQLite for storage.")
        assert len(claims) >= 1
        # Find the memory reference claim specifically
        memory_claims = [
            c for c in claims if c.claim_type == ClaimType.MEMORY_REFERENCE
        ]
        assert len(memory_claims) >= 1
        assert memory_claims[0].verification_level == VerificationLevel.MANDATORY
        assert (
            "use SQLite" in memory_claims[0].subject
            or "use SQLite" in memory_claims[0].text
        )

    def test_extract_previous_decision(self) -> None:
        """'Previously we...' is a memory reference claim."""
        claims = extract_claims("Previously we implemented caching.")
        assert len(claims) >= 1
        memory_claims = [
            c for c in claims if c.claim_type == ClaimType.MEMORY_REFERENCE
        ]
        assert len(memory_claims) >= 1
        assert memory_claims[0].verification_level == VerificationLevel.MANDATORY

    def test_extract_remember_pattern(self) -> None:
        """'I remember...' is a memory reference claim."""
        claims = extract_claims("I remember that we discussed async patterns.")
        assert len(claims) >= 1
        memory_claims = [
            c for c in claims if c.claim_type == ClaimType.MEMORY_REFERENCE
        ]
        assert len(memory_claims) >= 1
        assert memory_claims[0].verification_level == VerificationLevel.MANDATORY

    def test_extract_mentioned_pattern(self) -> None:
        """'You mentioned...' is a memory reference claim."""
        claims = extract_claims("You mentioned that testing is important.")
        assert len(claims) >= 1
        memory_claims = [
            c for c in claims if c.claim_type == ClaimType.MEMORY_REFERENCE
        ]
        assert len(memory_claims) >= 1
        assert memory_claims[0].verification_level == VerificationLevel.MANDATORY

    def test_we_agreed_pattern(self) -> None:
        """'We agreed...' is a memory reference claim."""
        claims = extract_claims("We agreed to follow TDD methodology.")
        assert len(claims) >= 1
        memory_claims = [
            c for c in claims if c.claim_type == ClaimType.MEMORY_REFERENCE
        ]
        assert len(memory_claims) >= 1

    def test_last_time_pattern(self) -> None:
        """'Last time we...' is a memory reference claim."""
        claims = extract_claims("Last time we fixed the memory leak issue.")
        assert len(claims) >= 1
        memory_claims = [
            c for c in claims if c.claim_type == ClaimType.MEMORY_REFERENCE
        ]
        assert len(memory_claims) >= 1


class TestExtractFactualAssertion:
    """Tests for factual assertion claim extraction."""

    def test_extract_factual_assertion_is(self) -> None:
        """'X is Y' is a factual assertion."""
        claims = extract_claims("Python is a programming language.")
        assert len(claims) >= 1
        factual_claims = [
            c for c in claims if c.claim_type == ClaimType.FACTUAL_ASSERTION
        ]
        assert len(factual_claims) >= 1
        assert factual_claims[0].verification_level == VerificationLevel.BEST_EFFORT

    def test_extract_uses_pattern(self) -> None:
        """'X uses Y' is a factual assertion."""
        claims = extract_claims("FastAPI uses Starlette under the hood.")
        assert len(claims) >= 1
        factual_claims = [
            c for c in claims if c.claim_type == ClaimType.FACTUAL_ASSERTION
        ]
        assert len(factual_claims) >= 1

    def test_extract_supports_pattern(self) -> None:
        """'X supports Y' is a factual assertion."""
        claims = extract_claims("The API supports JSON responses.")
        assert len(claims) >= 1
        factual_claims = [
            c for c in claims if c.claim_type == ClaimType.FACTUAL_ASSERTION
        ]
        assert len(factual_claims) >= 1

    def test_extract_returns_pattern(self) -> None:
        """'X returns Y' is a factual assertion."""
        claims = extract_claims("The function returns a dictionary.")
        assert len(claims) >= 1
        factual_claims = [
            c for c in claims if c.claim_type == ClaimType.FACTUAL_ASSERTION
        ]
        assert len(factual_claims) >= 1


class TestOutcomeReference:
    """Tests for outcome reference claim extraction."""

    def test_outcome_reference_worked(self) -> None:
        """'That approach worked' is an outcome reference."""
        claims = extract_claims("That approach worked well for us.")
        assert len(claims) >= 1
        outcome_claims = [
            c for c in claims if c.claim_type == ClaimType.OUTCOME_REFERENCE
        ]
        assert len(outcome_claims) >= 1
        assert outcome_claims[0].verification_level == VerificationLevel.MANDATORY

    def test_outcome_reference_failed(self) -> None:
        """'The X failed' is an outcome reference."""
        claims = extract_claims("The initial implementation failed.")
        assert len(claims) >= 1
        outcome_claims = [
            c for c in claims if c.claim_type == ClaimType.OUTCOME_REFERENCE
        ]
        assert len(outcome_claims) >= 1
        assert outcome_claims[0].verification_level == VerificationLevel.MANDATORY

    def test_outcome_succeeded(self) -> None:
        """'It succeeded' is an outcome reference."""
        claims = extract_claims("It succeeded after the refactor.")
        assert len(claims) >= 1
        outcome_claims = [
            c for c in claims if c.claim_type == ClaimType.OUTCOME_REFERENCE
        ]
        assert len(outcome_claims) >= 1


class TestSkipOpinions:
    """Tests that opinions are not extracted as claims."""

    def test_skip_opinions_think(self) -> None:
        """Opinions with 'I think' are skipped."""
        claims = extract_claims("I think we might want to refactor.")
        assert len(claims) == 0

    def test_skip_hypotheticals_if(self) -> None:
        """Hypotheticals with 'if we' are skipped."""
        claims = extract_claims("If we used async, performance would improve.")
        assert len(claims) == 0

    def test_skip_maybe(self) -> None:
        """Statements with 'maybe' are skipped."""
        claims = extract_claims("Maybe we should consider caching.")
        assert len(claims) == 0

    def test_skip_probably(self) -> None:
        """Statements with 'probably' are skipped."""
        claims = extract_claims("This is probably the best approach.")
        assert len(claims) == 0


class TestEdgeCases:
    """Tests for edge cases and deduplication."""

    def test_short_text_no_claims(self) -> None:
        """Very short text returns no claims."""
        claims = extract_claims("Hi")
        assert len(claims) == 0

        claims = extract_claims("")
        assert len(claims) == 0

    def test_deduplicates_same_subject(self) -> None:
        """Multiple claims about the same subject are deduplicated."""
        text = "We decided to use SQLite. We also decided to use SQLite for caching."
        claims = extract_claims(text)
        # Should have only one claim about SQLite
        subjects = [c.subject.lower() if c.subject else c.text.lower() for c in claims]
        # Check for deduplication (subjects should have no duplicates)
        assert len(subjects) == len(set(subjects))

    def test_mixed_claims(self) -> None:
        """Text with multiple claim types extracts all."""
        text = (
            "We decided to use Python. "
            "FastAPI uses Starlette. "
            "That approach worked great."
        )
        claims = extract_claims(text)
        assert len(claims) >= 2

        # Check we have different claim types
        claim_types = {c.claim_type for c in claims}
        assert len(claim_types) >= 2

    def test_opinion_in_longer_text_skipped(self) -> None:
        """Full opinion text is skipped entirely."""
        # If the entire text is an opinion, skip all claims
        text = "I think Python uses indentation."
        claims = extract_claims(text)
        assert len(claims) == 0


class TestClaimDataclass:
    """Tests for the Claim dataclass."""

    def test_claim_hash_by_subject(self) -> None:
        """Claims hash by normalized subject."""
        c1 = Claim(
            text="We decided X",
            claim_type=ClaimType.MEMORY_REFERENCE,
            verification_level=VerificationLevel.MANDATORY,
            subject="SQLite",
        )
        c2 = Claim(
            text="We also decided X",
            claim_type=ClaimType.MEMORY_REFERENCE,
            verification_level=VerificationLevel.MANDATORY,
            subject="sqlite",
        )
        assert hash(c1) == hash(c2)

    def test_claim_equality_by_subject(self) -> None:
        """Claims are equal if same subject (case-insensitive)."""
        c1 = Claim(
            text="We decided X",
            claim_type=ClaimType.MEMORY_REFERENCE,
            verification_level=VerificationLevel.MANDATORY,
            subject="SQLite",
        )
        c2 = Claim(
            text="Different text",
            claim_type=ClaimType.FACTUAL_ASSERTION,
            verification_level=VerificationLevel.BEST_EFFORT,
            subject="sqlite",
        )
        assert c1 == c2

    def test_claim_source_span(self) -> None:
        """Claims include source span for text position."""
        claims = extract_claims("We decided to use Python for this project.")
        assert len(claims) >= 1
        # Source span should be set
        assert claims[0].source_span != (0, 0) or claims[0].source_span[1] > 0


class TestVerificationLevels:
    """Tests that verification levels are correctly assigned."""

    def test_memory_claims_are_mandatory(self) -> None:
        """Memory reference claims have mandatory verification."""
        claims = extract_claims("We decided to use async patterns.")
        memory_claims = [
            c for c in claims if c.claim_type == ClaimType.MEMORY_REFERENCE
        ]
        for claim in memory_claims:
            assert claim.verification_level == VerificationLevel.MANDATORY

    def test_outcome_claims_are_mandatory(self) -> None:
        """Outcome reference claims have mandatory verification."""
        claims = extract_claims("That solution worked perfectly.")
        outcome_claims = [
            c for c in claims if c.claim_type == ClaimType.OUTCOME_REFERENCE
        ]
        for claim in outcome_claims:
            assert claim.verification_level == VerificationLevel.MANDATORY

    def test_factual_claims_are_best_effort(self) -> None:
        """Factual assertion claims have best_effort verification."""
        claims = extract_claims("Python uses dynamic typing.")
        factual_claims = [
            c for c in claims if c.claim_type == ClaimType.FACTUAL_ASSERTION
        ]
        for claim in factual_claims:
            assert claim.verification_level == VerificationLevel.BEST_EFFORT
