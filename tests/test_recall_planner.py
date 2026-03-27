# tests/test_recall_planner.py
"""Tests for TiMem-style recall planner."""

from daem0nmcp.recall_planner import (
    QueryComplexity,
    RecallPlanner,
    classify_query_complexity,
)


class TestClassifyQueryComplexity:
    """Test query complexity classification."""

    def test_simple_query(self):
        # Short, single-concept queries
        assert classify_query_complexity("auth") == QueryComplexity.SIMPLE
        assert classify_query_complexity("database") == QueryComplexity.SIMPLE
        assert classify_query_complexity("what is JWT?") == QueryComplexity.SIMPLE

    def test_medium_query(self):
        # Multi-word queries with some specificity
        result = classify_query_complexity("how does authentication work")
        assert result in [QueryComplexity.SIMPLE, QueryComplexity.MEDIUM]

        result = classify_query_complexity("JWT token validation in API")
        assert result in [QueryComplexity.MEDIUM, QueryComplexity.COMPLEX]

    def test_complex_query(self):
        # Long, multi-concept queries
        query = "trace the authentication flow from login through token refresh to session expiration"
        assert classify_query_complexity(query) == QueryComplexity.COMPLEX

        # Temporal queries
        query = "what decisions did we make about auth last week"
        assert classify_query_complexity(query) == QueryComplexity.COMPLEX


class TestRecallPlanner:
    """Test recall planner memory level selection."""

    def test_simple_query_uses_summaries(self):
        planner = RecallPlanner()
        plan = planner.plan_recall("auth", QueryComplexity.SIMPLE)

        assert plan.use_communities is True
        assert plan.max_raw_memories <= 5

    def test_complex_query_uses_raw_memories(self):
        planner = RecallPlanner()
        plan = planner.plan_recall(
            "trace auth flow through all components", QueryComplexity.COMPLEX
        )

        assert plan.use_communities is True
        assert plan.use_raw_memories is True
        assert plan.max_raw_memories >= 10

    def test_simple_plan_no_compression(self):
        """Simple queries don't need compression (use summaries)."""
        planner = RecallPlanner()
        plan = planner.plan_recall("what is Python?")

        assert plan.compress is False

    def test_complex_plan_has_compression(self):
        """Complex queries enable compression."""
        planner = RecallPlanner()
        plan = planner.plan_recall("trace the complete history of all decisions")

        assert plan.compress is True
        assert plan.compression_rate > 0
        assert plan.compression_rate < 1

    def test_medium_plan_moderate_compression(self):
        """Medium queries use moderate compression rate."""
        planner = RecallPlanner()
        # 4-8 words is MEDIUM
        plan = planner.plan_recall("find patterns in the codebase")

        assert plan.compress is True
        assert plan.compression_rate >= 0.3  # Not too aggressive
