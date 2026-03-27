"""Tests for UI text fallback formatting."""

from datetime import datetime, timezone

from daem0nmcp.ui.fallback import (
    format_briefing,
    format_covenant_status,
    format_search_results,
    format_with_ui_hint,
)


class TestFormatWithUIHint:
    """Tests for format_with_ui_hint wrapper."""

    def test_adds_ui_resource_and_text(self):
        data = {"count": 5, "results": []}
        result = format_with_ui_hint(
            data=data, ui_resource="ui://daem0n/search", text="5 results found"
        )

        assert result["ui_resource"] == "ui://daem0n/search"
        assert result["text"] == "5 results found"
        assert result["count"] == 5
        assert result["results"] == []

    def test_preserves_all_data_fields(self):
        data = {"a": 1, "b": "two", "c": [1, 2, 3]}
        result = format_with_ui_hint(data, "ui://test", "test text")

        assert result["a"] == 1
        assert result["b"] == "two"
        assert result["c"] == [1, 2, 3]


class TestFormatSearchResults:
    """Tests for search results text formatting."""

    def test_empty_results(self):
        result = format_search_results("test query", [])
        assert "No results found" in result
        assert "test query" in result

    def test_formats_results_with_scores(self):
        results = [
            {"category": "decision", "content": "Use React", "score": 0.95},
            {"category": "warning", "content": "Avoid jQuery", "score": 0.82},
        ]
        result = format_search_results("framework", results)

        assert "framework" in result
        assert "DECISION" in result
        assert "WARNING" in result
        assert "0.95" in result
        assert "Use React" in result

    def test_truncates_long_content(self):
        long_content = "x" * 500
        results = [{"category": "test", "content": long_content, "score": 0.5}]
        result = format_search_results("query", results)

        assert "..." in result
        assert len(result) < 600  # Much shorter than 500 chars


class TestFormatBriefing:
    """Tests for briefing text formatting."""

    def test_includes_all_sections(self):
        result = format_briefing(
            project="test-project",
            stats={"memories": 100, "rules": 10},
            warnings=[{"content": "Watch out!"}],
            recent_decisions=[{"content": "Use TypeScript", "outcome": "success"}],
            focus_areas=["Performance", "Security"],
        )

        assert "test-project" in result
        assert "STATISTICS" in result
        assert "memories: 100" in result
        assert "ACTIVE WARNINGS" in result
        assert "Watch out!" in result
        assert "RECENT DECISIONS" in result
        assert "[+]" in result  # success marker
        assert "FOCUS AREAS" in result
        assert "Performance" in result

    def test_handles_empty_sections(self):
        result = format_briefing(
            project="empty",
            stats={},
            warnings=[],
            recent_decisions=[],
            focus_areas=[],
        )

        assert "empty" in result
        assert "STATISTICS" in result
        # Should not include empty sections
        assert "ACTIVE WARNINGS" not in result


class TestFormatCovenantStatus:
    """Tests for covenant status text formatting."""

    def test_valid_preflight(self):
        expires = datetime(2025, 1, 27, 12, 0, 0, tzinfo=timezone.utc)
        result = format_covenant_status(
            phase="exploration",
            is_briefed=True,
            context_checks=3,
            preflight_valid=True,
            preflight_expires=expires,
        )

        assert "exploration" in result
        assert "Briefed: Yes" in result
        assert "Context Checks: 3" in result
        assert "Preflight: Valid" in result
        assert "2025-01-27" in result

    def test_invalid_preflight(self):
        result = format_covenant_status(
            phase="briefing",
            is_briefed=False,
            context_checks=0,
            preflight_valid=False,
        )

        assert "Briefed: No" in result
        assert "Preflight: Invalid/Expired" in result
