"""Tests for claim verification in Reflexion loop.

Tests verification of claims against stored knowledge:
- Memory recall verification
- GraphRAG entity verification
- Conflict detection via negation patterns
- Summary aggregation
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from daem0nmcp.reflexion.claims import Claim, ClaimType, VerificationLevel
from daem0nmcp.reflexion.verification import (
    VerificationEvidence,
    VerificationResult,
    summarize_verification,
    verify_claim,
    verify_claims,
)


@pytest.fixture
def mock_memory_manager():
    """Create a mock MemoryManager."""
    manager = AsyncMock()
    manager.recall = AsyncMock(return_value={"memories": []})
    return manager


@pytest.fixture
def mock_knowledge_graph():
    """Create a mock KnowledgeGraph."""
    kg = AsyncMock()
    kg.ensure_loaded = AsyncMock()
    kg._graph = MagicMock()
    kg._graph.nodes.return_value = []
    return kg


@pytest.fixture
def memory_reference_claim():
    """Create a memory reference claim for testing."""
    return Claim(
        text="We decided to use PostgreSQL",
        claim_type=ClaimType.MEMORY_REFERENCE,
        verification_level=VerificationLevel.MANDATORY,
        subject="PostgreSQL",
        predicate=None,
    )


@pytest.fixture
def factual_claim():
    """Create a factual assertion claim for testing."""
    return Claim(
        text="Python uses dynamic typing.",
        claim_type=ClaimType.FACTUAL_ASSERTION,
        verification_level=VerificationLevel.BEST_EFFORT,
        subject="Python",
        predicate="dynamic typing",
    )


@pytest.fixture
def skip_level_claim():
    """Create a skip-level claim (opinion) for testing."""
    return Claim(
        text="I think this approach is best",
        claim_type=ClaimType.FACTUAL_ASSERTION,
        verification_level=VerificationLevel.SKIP,
        subject="approach",
        predicate=None,
    )


class TestVerifyClaim:
    """Tests for verify_claim function."""

    @pytest.mark.asyncio
    async def test_verify_against_memory_found(
        self, mock_memory_manager, memory_reference_claim
    ):
        """Claim should be verified when supporting memory exists."""
        mock_memory_manager.recall.return_value = {
            "memories": [
                {
                    "id": 1,
                    "content": "We decided to use PostgreSQL for the database.",
                }
            ]
        }

        with (
            patch("daem0nmcp.reflexion.verification.encode_query") as mock_encode,
            patch(
                "daem0nmcp.reflexion.verification.encode_document"
            ) as mock_encode_doc,
        ):
            with patch("daem0nmcp.reflexion.verification.decode") as mock_decode:
                with patch(
                    "daem0nmcp.reflexion.verification.cosine_similarity"
                ) as mock_sim:
                    # Mock embedding operations
                    mock_encode.return_value = b"fake_embedding"
                    mock_encode_doc.return_value = b"fake_embedding"
                    mock_decode.return_value = [0.1] * 256
                    mock_sim.return_value = 0.85  # High similarity

                    result = await verify_claim(
                        claim=memory_reference_claim,
                        memory_manager=mock_memory_manager,
                    )

        assert result.status == "verified"
        assert result.confidence > 0.7
        assert len(result.evidence) >= 1
        assert result.evidence[0].source == "memory"

    @pytest.mark.asyncio
    async def test_verify_no_evidence_unverified(
        self, mock_memory_manager, memory_reference_claim
    ):
        """Claim should be unverified when no evidence found."""
        mock_memory_manager.recall.return_value = {"memories": []}

        result = await verify_claim(
            claim=memory_reference_claim,
            memory_manager=mock_memory_manager,
        )

        assert result.status == "unverified"
        assert result.confidence < 0.5

    @pytest.mark.asyncio
    async def test_verify_conflict_detected(self, mock_memory_manager):
        """Conflicting claim should be detected via negation."""
        claim = Claim(
            text="We decided NOT to use PostgreSQL",
            claim_type=ClaimType.MEMORY_REFERENCE,
            verification_level=VerificationLevel.MANDATORY,
            subject="PostgreSQL",
            predicate=None,
        )

        mock_memory_manager.recall.return_value = {
            "memories": [
                {
                    "id": 1,
                    "content": "We decided to use PostgreSQL for the database.",
                }
            ]
        }

        with (
            patch("daem0nmcp.reflexion.verification.encode_query") as mock_encode,
            patch(
                "daem0nmcp.reflexion.verification.encode_document"
            ) as mock_encode_doc,
        ):
            with patch("daem0nmcp.reflexion.verification.decode") as mock_decode:
                with patch(
                    "daem0nmcp.reflexion.verification.cosine_similarity"
                ) as mock_sim:
                    mock_encode.return_value = b"fake_embedding"
                    mock_encode_doc.return_value = b"fake_embedding"
                    mock_decode.return_value = [0.1] * 256
                    mock_sim.return_value = 0.8  # High similarity

                    result = await verify_claim(
                        claim=claim,
                        memory_manager=mock_memory_manager,
                    )

        assert result.status == "conflict"
        assert result.conflict_reason is not None
        assert "negation" in result.conflict_reason.lower()

    @pytest.mark.asyncio
    async def test_verify_with_knowledge_graph(
        self, mock_memory_manager, mock_knowledge_graph, memory_reference_claim
    ):
        """Verification should check GraphRAG entities."""
        mock_memory_manager.recall.return_value = {"memories": []}

        # Mock entity found in graph
        mock_knowledge_graph._graph.nodes.return_value = ["entity:1"]
        mock_knowledge_graph._graph.nodes.__getitem__ = MagicMock(
            return_value={"name": "PostgreSQL", "type": "technology"}
        )
        mock_knowledge_graph._graph.predecessors = MagicMock(return_value=["memory:1"])

        result = await verify_claim(
            claim=memory_reference_claim,
            memory_manager=mock_memory_manager,
            knowledge_graph=mock_knowledge_graph,
        )

        assert result.status == "verified"
        assert any(e.source == "entity" for e in result.evidence)

    @pytest.mark.asyncio
    async def test_verify_skip_level_auto_verified(
        self, mock_memory_manager, skip_level_claim
    ):
        """Skip-level claims should be auto-verified without checking."""
        result = await verify_claim(
            claim=skip_level_claim,
            memory_manager=mock_memory_manager,
        )

        assert result.status == "verified"
        assert result.confidence == 1.0
        # Memory manager should not have been called for skip-level
        mock_memory_manager.recall.assert_not_called()

    @pytest.mark.asyncio
    async def test_verify_with_as_of_time(
        self, mock_memory_manager, memory_reference_claim
    ):
        """Bi-temporal verification passes as_of_time to recall."""
        mock_memory_manager.recall.return_value = {"memories": []}

        await verify_claim(
            claim=memory_reference_claim,
            memory_manager=mock_memory_manager,
            as_of_time="2025-01-01T00:00:00Z",
        )

        # Verify as_of_time was parsed and passed to recall as datetime
        mock_memory_manager.recall.assert_called_once()
        call_kwargs = mock_memory_manager.recall.call_args.kwargs
        from datetime import datetime, timezone

        expected_dt = datetime(2025, 1, 1, 0, 0, tzinfo=timezone.utc)
        assert call_kwargs.get("as_of_time") == expected_dt

    @pytest.mark.asyncio
    async def test_verify_low_similarity_not_evidence(
        self, mock_memory_manager, memory_reference_claim
    ):
        """Low similarity memories should not count as evidence."""
        mock_memory_manager.recall.return_value = {
            "memories": [
                {
                    "id": 1,
                    "content": "Some unrelated content about databases.",
                }
            ]
        }

        with (
            patch("daem0nmcp.reflexion.verification.encode_query") as mock_encode,
            patch(
                "daem0nmcp.reflexion.verification.encode_document"
            ) as mock_encode_doc,
        ):
            with patch("daem0nmcp.reflexion.verification.decode") as mock_decode:
                with patch(
                    "daem0nmcp.reflexion.verification.cosine_similarity"
                ) as mock_sim:
                    mock_encode.return_value = b"fake_embedding"
                    mock_encode_doc.return_value = b"fake_embedding"
                    mock_decode.return_value = [0.1] * 256
                    mock_sim.return_value = 0.3  # Low similarity

                    result = await verify_claim(
                        claim=memory_reference_claim,
                        memory_manager=mock_memory_manager,
                    )

        # Should be unverified because similarity is below threshold
        assert result.status == "unverified"
        assert len(result.evidence) == 0


class TestVerifyClaims:
    """Tests for verify_claims function."""

    @pytest.mark.asyncio
    async def test_verify_multiple_claims(self, mock_memory_manager):
        """Multiple claims should all be verified."""
        claims = [
            Claim(
                text="We use Python",
                claim_type=ClaimType.FACTUAL_ASSERTION,
                verification_level=VerificationLevel.BEST_EFFORT,
                subject="Python",
                predicate="language",
            ),
            Claim(
                text="We decided on FastAPI",
                claim_type=ClaimType.MEMORY_REFERENCE,
                verification_level=VerificationLevel.MANDATORY,
                subject="FastAPI",
                predicate=None,
            ),
        ]

        mock_memory_manager.recall.return_value = {"memories": []}

        results = await verify_claims(
            claims=claims,
            memory_manager=mock_memory_manager,
        )

        assert len(results) == 2
        assert all(isinstance(r, VerificationResult) for r in results)

    @pytest.mark.asyncio
    async def test_verify_empty_claims_list(self, mock_memory_manager):
        """Empty claims list returns empty results."""
        results = await verify_claims(
            claims=[],
            memory_manager=mock_memory_manager,
        )

        assert len(results) == 0

    @pytest.mark.asyncio
    async def test_verify_claims_preserves_order(self, mock_memory_manager):
        """Results should be in same order as input claims."""
        claims = [
            Claim(
                text="Claim A",
                claim_type=ClaimType.MEMORY_REFERENCE,
                verification_level=VerificationLevel.MANDATORY,
                subject="A",
                predicate=None,
            ),
            Claim(
                text="Claim B",
                claim_type=ClaimType.FACTUAL_ASSERTION,
                verification_level=VerificationLevel.BEST_EFFORT,
                subject="B",
                predicate=None,
            ),
            Claim(
                text="Claim C",
                claim_type=ClaimType.OUTCOME_REFERENCE,
                verification_level=VerificationLevel.MANDATORY,
                subject="C",
                predicate=None,
            ),
        ]

        mock_memory_manager.recall.return_value = {"memories": []}

        results = await verify_claims(claims=claims, memory_manager=mock_memory_manager)

        assert results[0].claim_text == "Claim A"
        assert results[1].claim_text == "Claim B"
        assert results[2].claim_text == "Claim C"


class TestSummarizeVerification:
    """Tests for verification summary."""

    def test_summarize_mixed_results(self):
        """Summary should correctly count different statuses."""
        results = [
            VerificationResult("claim1", "memory_reference", "verified", 0.9, []),
            VerificationResult("claim2", "factual_assertion", "verified", 0.8, []),
            VerificationResult("claim3", "memory_reference", "unverified", 0.3, []),
            VerificationResult(
                "claim4", "memory_reference", "conflict", 0.9, [], "negation"
            ),
        ]

        summary = summarize_verification(results)

        assert summary["verified_count"] == 2
        assert summary["unverified_count"] == 1
        assert summary["conflict_count"] == 1
        assert 0.5 < summary["overall_confidence"] < 0.8
        assert len(summary["conflicts"]) == 1

    def test_summarize_empty_results(self):
        """Empty results should have default confidence."""
        summary = summarize_verification([])

        assert summary["verified_count"] == 0
        assert summary["unverified_count"] == 0
        assert summary["conflict_count"] == 0
        assert summary["overall_confidence"] == 0.5

    def test_summarize_all_verified(self):
        """All verified claims should have high confidence."""
        results = [
            VerificationResult("claim1", "memory_reference", "verified", 0.95, []),
            VerificationResult("claim2", "factual_assertion", "verified", 0.85, []),
        ]

        summary = summarize_verification(results)

        assert summary["verified_count"] == 2
        assert summary["unverified_count"] == 0
        assert summary["conflict_count"] == 0
        assert summary["overall_confidence"] == 0.9  # Average of 0.95 and 0.85
        assert summary["conflicts"] == []

    def test_summarize_all_conflicts(self):
        """All conflict claims should list all conflicts."""
        results = [
            VerificationResult(
                "claim1", "memory_reference", "conflict", 0.9, [], "negation detected"
            ),
            VerificationResult(
                "claim2",
                "outcome_reference",
                "conflict",
                0.85,
                [],
                "contradictory outcome",
            ),
        ]

        summary = summarize_verification(results)

        assert summary["conflict_count"] == 2
        assert len(summary["conflicts"]) == 2
        assert summary["conflicts"][0]["claim"] == "claim1"
        assert summary["conflicts"][0]["reason"] == "negation detected"


class TestVerificationEvidence:
    """Tests for VerificationEvidence dataclass."""

    def test_evidence_from_memory(self):
        """Memory evidence includes memory_id."""
        evidence = VerificationEvidence(
            source="memory",
            content="We decided to use SQLite.",
            similarity=0.85,
            memory_id=42,
        )

        assert evidence.source == "memory"
        assert evidence.memory_id == 42
        assert evidence.entity_id is None

    def test_evidence_from_entity(self):
        """Entity evidence includes entity_id."""
        evidence = VerificationEvidence(
            source="entity",
            content="Entity 'SQLite' found with 5 related memories",
            similarity=0.8,
            entity_id=17,
        )

        assert evidence.source == "entity"
        assert evidence.entity_id == 17
        assert evidence.memory_id is None


class TestVerificationResult:
    """Tests for VerificationResult dataclass."""

    def test_result_with_evidence(self):
        """Result should include evidence list."""
        evidence = [
            VerificationEvidence("memory", "content", 0.8, memory_id=1),
        ]
        result = VerificationResult(
            claim_text="We decided to use SQLite",
            claim_type="memory_reference",
            status="verified",
            confidence=0.85,
            evidence=evidence,
        )

        assert len(result.evidence) == 1
        assert result.conflict_reason is None

    def test_result_with_conflict(self):
        """Conflict result should include reason."""
        result = VerificationResult(
            claim_text="We decided NOT to use SQLite",
            claim_type="memory_reference",
            status="conflict",
            confidence=0.9,
            evidence=[],
            conflict_reason="negation pattern detected",
        )

        assert result.status == "conflict"
        assert result.conflict_reason == "negation pattern detected"


class TestVerificationIntegration:
    """Integration tests for the verification system."""

    @pytest.mark.asyncio
    async def test_memory_and_graph_verification(
        self, mock_memory_manager, mock_knowledge_graph
    ):
        """Both memory and graph verification should contribute evidence."""
        claim = Claim(
            text="We decided to use SQLite for storage",
            claim_type=ClaimType.MEMORY_REFERENCE,
            verification_level=VerificationLevel.MANDATORY,
            subject="SQLite",
            predicate=None,
        )

        # Mock memory with high similarity
        mock_memory_manager.recall.return_value = {
            "memories": [{"id": 1, "content": "SQLite was chosen for local storage."}]
        }

        # Mock entity in graph
        mock_knowledge_graph._graph.nodes.return_value = ["entity:10"]
        mock_knowledge_graph._graph.nodes.__getitem__ = MagicMock(
            return_value={"name": "SQLite", "type": "database"}
        )
        mock_knowledge_graph._graph.predecessors = MagicMock(return_value=["memory:1"])

        with (
            patch("daem0nmcp.reflexion.verification.encode_query") as mock_encode,
            patch(
                "daem0nmcp.reflexion.verification.encode_document"
            ) as mock_encode_doc,
        ):
            with patch("daem0nmcp.reflexion.verification.decode") as mock_decode:
                with patch(
                    "daem0nmcp.reflexion.verification.cosine_similarity"
                ) as mock_sim:
                    mock_encode.return_value = b"embedding"
                    mock_encode_doc.return_value = b"embedding"
                    mock_decode.return_value = [0.1] * 256
                    mock_sim.return_value = 0.85

                    result = await verify_claim(
                        claim=claim,
                        memory_manager=mock_memory_manager,
                        knowledge_graph=mock_knowledge_graph,
                    )

        assert result.status == "verified"
        # Should have evidence from both sources
        sources = {e.source for e in result.evidence}
        assert "memory" in sources
        assert "entity" in sources
