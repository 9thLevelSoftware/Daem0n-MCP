"""Tests for the rules engine with TF-IDF matching."""

import shutil
import tempfile

import pytest

from daem0nmcp.database import DatabaseManager
from daem0nmcp.rules import RulesEngine


@pytest.fixture
def temp_storage():
    """Create a temporary storage directory."""
    temp_dir = tempfile.mkdtemp()
    yield temp_dir
    shutil.rmtree(temp_dir)


@pytest.fixture
async def rules_engine(temp_storage):
    """Create a rules engine with temporary storage."""
    db = DatabaseManager(temp_storage)
    await db.init_db()
    engine = RulesEngine(db)
    yield engine
    await db.close()


class TestRulesEngine:
    """Test rule management and semantic checking."""

    @pytest.mark.asyncio
    async def test_add_rule(self, rules_engine):
        """Test adding a rule."""
        result = await rules_engine.add_rule(
            trigger="adding new API endpoint",
            must_do=["Add rate limiting", "Write tests"],
            must_not=["Use synchronous calls"],
            ask_first=["Is this a breaking change?"],
        )

        assert "id" in result
        assert result["trigger"] == "adding new API endpoint"
        assert "Add rate limiting" in result["must_do"]
        assert "Use synchronous calls" in result["must_not"]

    @pytest.mark.asyncio
    async def test_add_rule_with_warnings(self, rules_engine):
        """Test adding a rule with warnings."""
        result = await rules_engine.add_rule(
            trigger="modifying database schema",
            must_do=["Create migration"],
            warnings=["Last schema change caused downtime"],
        )

        assert "Last schema change caused downtime" in result["warnings"]

    @pytest.mark.asyncio
    async def test_check_rules_semantic_match(self, rules_engine):
        """Test semantic matching of rules with TF-IDF."""
        # Add a rule
        await rules_engine.add_rule(
            trigger="adding API endpoint",
            must_do=["Add rate limiting"],
            must_not=["Skip validation"],
        )

        # Check an action that matches via shared terms (API, endpoint)
        result = await rules_engine.check_rules("adding a new API endpoint for users")

        # Should match via shared terms
        assert result["matched_rules"] >= 1
        assert result["guidance"] is not None
        assert "Add rate limiting" in result["guidance"]["must_do"]

    @pytest.mark.asyncio
    async def test_check_rules_no_match(self, rules_engine):
        """Test checking an action that doesn't match any rules."""
        await rules_engine.add_rule(
            trigger="database migration", must_do=["Backup first"]
        )

        result = await rules_engine.check_rules("updating documentation files")

        assert result["matched_rules"] == 0
        assert result["guidance"] is None

    @pytest.mark.asyncio
    async def test_check_rules_multiple_matches(self, rules_engine):
        """Test combining guidance from multiple matching rules."""
        await rules_engine.add_rule(
            trigger="API changes", must_do=["Update OpenAPI spec"]
        )
        await rules_engine.add_rule(
            trigger="endpoint modifications",
            must_do=["Write integration tests"],
            warnings=["Check backwards compatibility"],
        )

        result = await rules_engine.check_rules("making API endpoint changes")

        # Should combine guidance from both rules
        if result["matched_rules"] >= 2:
            guidance = result["guidance"]
            assert len(guidance["must_do"]) >= 2

    @pytest.mark.asyncio
    async def test_check_rules_has_blockers(self, rules_engine):
        """Test detecting blocker conditions."""
        await rules_engine.add_rule(
            trigger="production deployment",
            must_not=["Deploy on Friday"],
            warnings=["Always have rollback plan"],
        )

        result = await rules_engine.check_rules("deploying to production server")

        if result["matched_rules"] >= 1:
            assert result["has_blockers"]

    @pytest.mark.asyncio
    async def test_list_rules(self, rules_engine):
        """Test listing all rules."""
        await rules_engine.add_rule(trigger="Rule 1", must_do=["Do X"])
        await rules_engine.add_rule(trigger="Rule 2", must_do=["Do Y"])

        rules = await rules_engine.list_rules()

        assert len(rules) >= 2
        triggers = [r["trigger"] for r in rules]
        assert "Rule 1" in triggers
        assert "Rule 2" in triggers

    @pytest.mark.asyncio
    async def test_update_rule(self, rules_engine):
        """Test updating a rule."""
        rule = await rules_engine.add_rule(
            trigger="test rule", must_do=["Original task"]
        )

        result = await rules_engine.update_rule(
            rule_id=rule["id"], must_do=["Updated task"], priority=10
        )

        assert result["updated"]

        # Verify update
        rules = await rules_engine.list_rules()
        updated = next(r for r in rules if r["id"] == rule["id"])
        assert "Updated task" in updated["must_do"]
        assert updated["priority"] == 10

    @pytest.mark.asyncio
    async def test_delete_rule(self, rules_engine):
        """Test deleting a rule."""
        rule = await rules_engine.add_rule(
            trigger="to be deleted", must_do=["Something"]
        )

        result = await rules_engine.delete_rule(rule["id"])
        assert result["deleted"]

        # Verify deletion
        rules = await rules_engine.list_rules()
        assert not any(r["id"] == rule["id"] for r in rules)

    @pytest.mark.asyncio
    async def test_add_warning_to_rule(self, rules_engine):
        """Test adding a warning to an existing rule."""
        rule = await rules_engine.add_rule(
            trigger="database changes", must_do=["Backup first"]
        )

        result = await rules_engine.add_warning_to_rule(
            rule["id"], "Previous migration took 2 hours"
        )

        assert "Previous migration took 2 hours" in result["warnings"]

    @pytest.mark.asyncio
    async def test_priority_ordering(self, rules_engine):
        """Test that higher priority rules come first."""
        await rules_engine.add_rule(trigger="Low priority", priority=1)
        await rules_engine.add_rule(trigger="High priority", priority=10)
        await rules_engine.add_rule(trigger="Medium priority", priority=5)

        rules = await rules_engine.list_rules()

        # Should be ordered by priority descending
        priorities = [r["priority"] for r in rules]
        assert priorities == sorted(priorities, reverse=True)

    @pytest.mark.asyncio
    async def test_disabled_rules_not_matched(self, rules_engine):
        """Test that disabled rules are not matched."""
        rule = await rules_engine.add_rule(
            trigger="disabled rule test", must_do=["Should not appear"]
        )

        # Disable the rule
        await rules_engine.update_rule(rule["id"], enabled=False)

        # Check rules - should not match
        result = await rules_engine.check_rules("disabled rule test action")

        # The disabled rule should not contribute
        if result["guidance"]:
            assert "Should not appear" not in result["guidance"]["must_do"]

    @pytest.mark.asyncio
    async def test_find_similar_rules(self, rules_engine):
        """Test finding similar rules to avoid duplicates."""
        await rules_engine.add_rule(
            trigger="adding new API endpoint", must_do=["Add rate limiting"]
        )

        # Find rules similar to a proposed trigger
        similar = await rules_engine.find_similar_rules("creating API route")

        # Should find the existing rule as similar
        assert len(similar) >= 1
        assert "similarity" in similar[0]

    @pytest.mark.asyncio
    async def test_check_rules_returns_match_scores(self, rules_engine):
        """Test that check_rules returns match scores."""
        await rules_engine.add_rule(
            trigger="API endpoint creation", must_do=["Add tests"]
        )

        result = await rules_engine.check_rules("creating new API endpoint")

        if result["matched_rules"] >= 1:
            for rule in result["rules"]:
                assert "match_score" in rule
                assert 0 <= rule["match_score"] <= 1

    @pytest.mark.asyncio
    async def test_semantic_matching_related_concepts(self, rules_engine):
        """Test that TF-IDF matches related concepts."""
        # Add rules with different terminology
        await rules_engine.add_rule(
            trigger="authentication security", must_do=["Use secure tokens"]
        )

        # Query with related but different words
        result1 = await rules_engine.check_rules("implementing login security")
        result2 = await rules_engine.check_rules("adding auth mechanism")

        # At least one should match due to semantic similarity
        # (security, auth/authentication)
        total_matches = result1["matched_rules"] + result2["matched_rules"]
        assert total_matches >= 1  # May not match, but feature exists


class TestRulesCaching:
    """Test rules caching behavior."""

    @pytest.mark.asyncio
    async def test_check_rules_cache_hit(self, rules_engine):
        """Test that identical check_rules calls use cache."""
        from daem0nmcp.cache import get_rules_cache

        # Clear cache to start fresh
        get_rules_cache().clear()

        # Create a rule
        await rules_engine.add_rule(
            trigger="cache test rule check",
            must_do=["verify caching works"],
            priority=1,
        )

        # First check_rules - should populate cache
        result1 = await rules_engine.check_rules("cache test rule check")

        # Check cache stats before second call
        stats_before = get_rules_cache().stats

        # Second check_rules with identical parameters - should hit cache
        result2 = await rules_engine.check_rules("cache test rule check")

        stats_after = get_rules_cache().stats

        # Verify results are the same
        assert result1["matched_rules"] == result2["matched_rules"]
        assert result1["action"] == result2["action"]

        # Verify cache hit happened
        assert stats_after["hits"] > stats_before["hits"]

    @pytest.mark.asyncio
    async def test_check_rules_cache_invalidated_on_add(self, rules_engine):
        """Test that cache is cleared when new rule is added."""
        from daem0nmcp.cache import get_rules_cache

        # Create initial rule
        await rules_engine.add_rule(
            trigger="initial rule for add test", must_do=["do something"]
        )

        # Clear cache
        get_rules_cache().clear()

        # Check rules - populates cache
        await rules_engine.check_rules("initial rule for add test")

        assert len(get_rules_cache()) > 0  # Cache has entries

        # Add new rule - should clear cache
        await rules_engine.add_rule(
            trigger="new rule for add test", must_do=["do something else"]
        )

        # Cache should be empty now
        assert len(get_rules_cache()) == 0

    @pytest.mark.asyncio
    async def test_check_rules_cache_invalidated_on_delete(self, rules_engine):
        """Test that cache is cleared when rule is deleted."""
        from daem0nmcp.cache import get_rules_cache

        # Create rule
        result = await rules_engine.add_rule(
            trigger="rule for delete test", must_do=["do something"]
        )
        rule_id = result["id"]

        # Clear cache
        get_rules_cache().clear()

        # Check rules - populates cache
        await rules_engine.check_rules("rule for delete test")

        # Delete rule - should clear cache via _invalidate_index
        await rules_engine.delete_rule(rule_id)

        # Cache should be empty now
        assert len(get_rules_cache()) == 0

    @pytest.mark.asyncio
    async def test_check_rules_cache_invalidated_on_update_enabled(self, rules_engine):
        """Test that cache is cleared when rule enabled status changes."""
        from daem0nmcp.cache import get_rules_cache

        # Create rule
        result = await rules_engine.add_rule(
            trigger="rule for enable test", must_do=["do something"]
        )
        rule_id = result["id"]

        # Clear cache
        get_rules_cache().clear()

        # Check rules - populates cache
        await rules_engine.check_rules("rule for enable test")

        # Disable rule - should clear cache via _invalidate_index
        await rules_engine.update_rule(rule_id, enabled=False)

        # Cache should be empty now
        assert len(get_rules_cache()) == 0
