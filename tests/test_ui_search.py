"""Tests for Search Results UI building."""

from daem0nmcp.ui.resources import _build_search_ui, _highlight_keywords


class TestBuildSearchUI:
    """Tests for _build_search_ui function."""

    def test_renders_cards_for_results(self):
        """Test that cards are rendered for each result."""
        data = {
            "topic": "python",
            "decisions": [
                {
                    "content": "Use Python 3.11",
                    "relevance": 0.95,
                    "created_at": "2026-01-28T00:00:00Z",
                    "tags": ["python"],
                },
                {
                    "content": "Use type hints",
                    "relevance": 0.88,
                    "created_at": "2026-01-27T00:00:00Z",
                    "tags": [],
                },
            ],
            "warnings": [
                {
                    "content": "Avoid Python 2",
                    "relevance": 0.75,
                    "created_at": "2026-01-26T00:00:00Z",
                    "tags": ["legacy"],
                },
            ],
            "patterns": [],
            "learnings": [],
        }

        html = _build_search_ui(data)

        # Count actual card divs (use class="daemon-card to distinguish from CSS references)
        assert html.count('class="daemon-card') == 3
        # Badge classes appear once in CSS, plus once per result card
        # 2 decisions + 1 CSS reference = 3 for decision
        # 1 warning + 1 CSS reference = 2 for warning
        assert html.count('class="daemon-badge daemon-badge--decision"') == 2
        assert html.count('class="daemon-badge daemon-badge--warning"') == 1

    def test_renders_empty_state(self):
        """Test that empty state is rendered when no results."""
        data = {
            "topic": "nonexistent",
            "decisions": [],
            "patterns": [],
            "warnings": [],
            "learnings": [],
        }

        html = _build_search_ui(data)

        assert "daemon-empty" in html
        assert "No results found" in html

    def test_has_filter_dropdown(self):
        """Test that filter dropdown is present."""
        data = {
            "topic": "test",
            "decisions": [{"content": "test", "relevance": 0.5}],
            "patterns": [],
            "warnings": [],
            "learnings": [],
        }

        html = _build_search_ui(data)

        assert "<select" in html
        assert 'id="category-filter"' in html
        assert ">All Categories</option>" in html
        assert ">Decisions</option>" in html
        assert ">Warnings</option>" in html
        assert ">Patterns</option>" in html
        assert ">Learnings</option>" in html

    def test_has_score_bars(self):
        """Test that score bars are rendered with correct widths."""
        data = {
            "topic": "test",
            "decisions": [
                {"content": "High score", "relevance": 0.95},
                {"content": "Low score", "relevance": 0.30},
            ],
            "patterns": [],
            "warnings": [],
            "learnings": [],
        }

        html = _build_search_ui(data)

        assert "daemon-score-bar" in html
        assert "daemon-score-bar__track" in html
        assert "daemon-score-bar__fill" in html
        # Check that width percentages are calculated
        assert "width: 95%" in html
        assert "width: 30%" in html

    def test_all_categories_rendered(self):
        """Test that all four categories are properly rendered."""
        data = {
            "topic": "comprehensive",
            "decisions": [{"content": "A decision", "relevance": 0.9}],
            "warnings": [{"content": "A warning", "relevance": 0.8}],
            "patterns": [{"content": "A pattern", "relevance": 0.7}],
            "learnings": [{"content": "A learning", "relevance": 0.6}],
        }

        html = _build_search_ui(data)

        assert "daemon-badge--decision" in html
        assert "daemon-badge--warning" in html
        assert "daemon-badge--pattern" in html
        assert "daemon-badge--learning" in html
        assert 'data-category="decision"' in html
        assert 'data-category="warning"' in html
        assert 'data-category="pattern"' in html
        assert 'data-category="learning"' in html

    def test_displays_topic_in_header(self):
        """Test that search topic is displayed."""
        data = {
            "topic": "authentication",
            "decisions": [],
            "patterns": [],
            "warnings": [],
            "learnings": [],
        }

        html = _build_search_ui(data)

        assert "authentication" in html

    def test_displays_result_count(self):
        """Test that result count is displayed."""
        data = {
            "topic": "test",
            "decisions": [{"content": "one", "relevance": 0.5}],
            "warnings": [{"content": "two", "relevance": 0.5}],
            "patterns": [],
            "learnings": [],
            "total_count": 10,
        }

        html = _build_search_ui(data)

        # Shows "2 of 10 results" since we have 2 actual results but total_count=10
        assert "2 of 10 results" in html


class TestHighlightKeywords:
    """Tests for _highlight_keywords function."""

    def test_highlights_matching_words(self):
        """Test that query words are wrapped in <mark> tags."""
        text = "Using Python for data processing"
        query = "python data"

        result = _highlight_keywords(text, query)

        assert "<mark>python</mark>" in result
        assert "<mark>data</mark>" in result

    def test_case_insensitive(self):
        """Test that highlighting is case-insensitive."""
        text = "Python is great for PYTHON beginners"
        query = "python"

        result = _highlight_keywords(text, query)

        # Should highlight both occurrences (case preserved in highlight)
        assert result.count("<mark>") == 2

    def test_skips_short_words(self):
        """Test that words with 2 or fewer characters are skipped."""
        text = "Is it a test or not"
        query = "is it a test"

        result = _highlight_keywords(text, query)

        # Only "test" should be highlighted (is, it, a are too short)
        assert "<mark>test</mark>" in result
        assert "<mark>is</mark>" not in result
        assert "<mark>it</mark>" not in result
        assert "<mark>a</mark>" not in result

    def test_empty_query_returns_original(self):
        """Test that empty query returns original text."""
        text = "Some text here"

        assert _highlight_keywords(text, "") == text
        assert _highlight_keywords(text, None) == text

    def test_empty_text_returns_empty(self):
        """Test that empty text returns empty."""
        assert _highlight_keywords("", "query") == ""
        assert _highlight_keywords(None, "query") is None


class TestSearchUIIntegration:
    """Integration tests for search UI."""

    def test_complete_ui_has_required_elements(self):
        """Test that complete UI has all required structural elements."""
        data = {
            "topic": "xyz",
            "decisions": [
                {
                    "content": "Unique content here",
                    "relevance": 0.85,
                    "tags": ["sample"],
                }
            ],
            "patterns": [],
            "warnings": [],
            "learnings": [],
        }

        html = _build_search_ui(data)

        # Has proper HTML structure
        assert "<!DOCTYPE html>" in html
        assert "<html" in html
        assert "</html>" in html

        # Has CSS injected
        assert "--daemon-accent" in html  # CSS custom property

        # Has filter JavaScript
        assert "category-filter" in html
        assert "highlightKeywords" in html

        # Has content (topic is "xyz" so no highlighting occurs)
        assert "Unique content here" in html
        assert 'class="daemon-card' in html

    def test_handles_missing_fields_gracefully(self):
        """Test that missing fields don't cause errors."""
        # Minimal data
        data = {"topic": "minimal"}

        html = _build_search_ui(data)

        # Should render without error and show empty state
        assert "daemon-empty" in html
        assert "No results found" in html

    def test_handles_results_without_optional_fields(self):
        """Test results without tags, created_at work correctly."""
        data = {
            "topic": "sparse",
            "decisions": [
                {"content": "Just content", "relevance": 0.5}
                # No tags, no created_at
            ],
            "patterns": [],
            "warnings": [],
            "learnings": [],
        }

        html = _build_search_ui(data)

        assert "Just content" in html
        assert "daemon-card" in html


class TestScoreBreakdown:
    """Tests for score breakdown display in result cards."""

    def test_score_breakdown_rendered(self):
        """Test that score breakdown section is rendered with components."""
        data = {
            "topic": "test",
            "decisions": [
                {
                    "content": "Test decision",
                    "relevance": 0.77,
                    "semantic_match": 0.85,
                    "recency_weight": 0.9,
                }
            ],
            "patterns": [],
            "warnings": [],
            "learnings": [],
        }

        html = _build_search_ui(data)

        # Check score breakdown section exists
        assert "daemon-score-breakdown" in html
        assert "Score breakdown" in html  # Summary text

        # Check component labels
        assert "Semantic match:" in html
        assert "Recency weight:" in html
        assert "Final relevance:" in html

        # Check values are displayed as percentages
        assert "85%" in html  # semantic_match
        assert "90%" in html  # recency_weight
        assert "77%" in html  # relevance

    def test_score_breakdown_fallback_values(self):
        """Test that score breakdown uses fallback when semantic_match not provided."""
        data = {
            "topic": "test",
            "decisions": [
                {
                    "content": "No explicit semantic_match",
                    "relevance": 0.65,
                    # semantic_match and recency_weight not provided
                }
            ],
            "patterns": [],
            "warnings": [],
            "learnings": [],
        }

        html = _build_search_ui(data)

        # Should still have breakdown section
        assert "daemon-score-breakdown" in html
        # Semantic match falls back to relevance (65%)
        assert "65%" in html
        # Recency weight defaults to 100%
        assert "100%" in html


class TestRecordOutcomeButton:
    """Tests for Record Outcome button on decision cards."""

    def test_record_outcome_button_on_pending_decision(self):
        """Test that Record Outcome button appears on decisions without outcome."""
        data = {
            "topic": "test",
            "decisions": [
                {
                    "id": 123,
                    "content": "Use async for all API calls",
                    "relevance": 0.85,
                    "worked": None,  # No outcome recorded yet
                }
            ],
            "patterns": [],
            "warnings": [],
            "learnings": [],
        }

        html = _build_search_ui(data)

        # Button should be present
        assert 'data-action="record-outcome"' in html
        assert 'data-memory-id="123"' in html
        assert "Record Outcome" in html
        assert "daemon-card__actions" in html

    def test_record_outcome_button_hidden_for_completed(self):
        """Test that Record Outcome button is hidden when outcome already recorded."""
        data = {
            "topic": "test",
            "decisions": [
                {
                    "id": 456,
                    "content": "Use JWT for authentication",
                    "relevance": 0.90,
                    "worked": True,  # Outcome already recorded
                }
            ],
            "patterns": [],
            "warnings": [],
            "learnings": [],
        }

        html = _build_search_ui(data)

        # Button element should NOT be present (outcome already recorded)
        # Check for the actual button with data-memory-id attribute
        assert 'data-memory-id="456"' not in html
        assert "data-memory-id=" not in html  # No button with memory id at all
        # But the card should still exist
        assert "Use JWT for authentication" in html

    def test_record_outcome_button_hidden_for_failed(self):
        """Test that Record Outcome button is hidden when outcome is failure."""
        data = {
            "topic": "test",
            "decisions": [
                {
                    "id": 789,
                    "content": "Use global state",
                    "relevance": 0.70,
                    "worked": False,  # Outcome recorded as failure
                }
            ],
            "patterns": [],
            "warnings": [],
            "learnings": [],
        }

        html = _build_search_ui(data)

        # Button should NOT be present - check for actual button element
        assert 'data-memory-id="789"' not in html
        assert "data-memory-id=" not in html  # No button with memory id

    def test_record_outcome_button_only_on_decisions(self):
        """Test that Record Outcome button only appears on decision cards."""
        data = {
            "topic": "test",
            "decisions": [],
            "patterns": [
                {"id": 100, "content": "A pattern", "relevance": 0.8, "worked": None}
            ],
            "warnings": [
                {"id": 101, "content": "A warning", "relevance": 0.7, "worked": None}
            ],
            "learnings": [
                {"id": 102, "content": "A learning", "relevance": 0.6, "worked": None}
            ],
        }

        html = _build_search_ui(data)

        # Button should NOT appear on any of these (they're not decisions)
        # Check for actual button elements with memory ids
        assert 'data-memory-id="100"' not in html
        assert 'data-memory-id="101"' not in html
        assert 'data-memory-id="102"' not in html
        assert "data-memory-id=" not in html  # No buttons at all
        # But the cards should still render
        assert "A pattern" in html
        assert "A warning" in html
        assert "A learning" in html

    def test_record_outcome_button_requires_memory_id(self):
        """Test that Record Outcome button requires memory ID."""
        data = {
            "topic": "test",
            "decisions": [
                {
                    # No id field
                    "content": "Decision without ID",
                    "relevance": 0.85,
                    "worked": None,
                }
            ],
            "patterns": [],
            "warnings": [],
            "learnings": [],
        }

        html = _build_search_ui(data)

        # Button should NOT appear (no memory_id)
        assert "data-memory-id=" not in html
        # But the card should still render
        assert "Decision without ID" in html


class TestPagination:
    """Tests for pagination controls."""

    def test_pagination_controls_rendered(self):
        """Test that pagination controls are rendered when has_more or offset > 0."""
        data = {
            "topic": "test",
            "decisions": [{"content": "item", "relevance": 0.5}],
            "patterns": [],
            "warnings": [],
            "learnings": [],
            "offset": 10,
            "limit": 10,
            "total_count": 50,
            "has_more": True,
        }

        html = _build_search_ui(data)

        # Check pagination container
        assert "daemon-pagination" in html
        assert 'data-offset="10"' in html
        assert 'data-limit="10"' in html

        # Check info text shows correct range
        assert "Showing 11-" in html  # offset+1
        assert "of 50" in html  # total_count

        # Check buttons exist
        assert 'data-action="prev"' in html
        assert 'data-action="next"' in html

    def test_pagination_first_page(self):
        """Test that Previous is disabled on first page."""
        data = {
            "topic": "test",
            "decisions": [{"content": "item", "relevance": 0.5}],
            "patterns": [],
            "warnings": [],
            "learnings": [],
            "offset": 0,
            "limit": 10,
            "total_count": 20,
            "has_more": True,
        }

        html = _build_search_ui(data)

        # Pagination should be rendered (has_more is True)
        assert "daemon-pagination" in html

        # Find the Previous button - it should have disabled attribute
        # The pattern is: data-action="prev" ... disabled (or disabled before data-action)
        prev_button_start = html.find('data-action="prev"')
        assert prev_button_start > 0
        # Look backward for the button start and forward for the >
        button_start = html.rfind("<button", 0, prev_button_start)
        button_end = html.find(">", prev_button_start)
        prev_button = html[button_start : button_end + 1]
        assert "disabled" in prev_button

        # Next button should NOT be disabled
        next_button_start = html.find('data-action="next"')
        button_start = html.rfind("<button", 0, next_button_start)
        button_end = html.find(">", next_button_start)
        next_button = html[button_start : button_end + 1]
        # Check that disabled is not in the Next button specifically
        # (Need to be careful since "disabled" might appear elsewhere)
        assert next_button.count("disabled") == 0

    def test_pagination_last_page(self):
        """Test that Next is disabled on last page."""
        data = {
            "topic": "test",
            "decisions": [
                {"content": "item1", "relevance": 0.5},
                {"content": "item2", "relevance": 0.5},
            ],
            "patterns": [],
            "warnings": [],
            "learnings": [],
            "offset": 40,
            "limit": 10,
            "total_count": 42,
            "has_more": False,
        }

        html = _build_search_ui(data)

        # Pagination should be rendered (offset > 0)
        assert "daemon-pagination" in html

        # Previous button should NOT be disabled
        prev_button_start = html.find('data-action="prev"')
        button_start = html.rfind("<button", 0, prev_button_start)
        button_end = html.find(">", prev_button_start)
        prev_button = html[button_start : button_end + 1]
        assert prev_button.count("disabled") == 0

        # Next button should be disabled
        next_button_start = html.find('data-action="next"')
        button_start = html.rfind("<button", 0, next_button_start)
        button_end = html.find(">", next_button_start)
        next_button = html[button_start : button_end + 1]
        assert "disabled" in next_button

    def test_pagination_hidden_single_page(self):
        """Test that pagination is hidden when all results fit on single page."""
        data = {
            "topic": "test",
            "decisions": [
                {"content": "item1", "relevance": 0.5},
                {"content": "item2", "relevance": 0.5},
            ],
            "patterns": [],
            "warnings": [],
            "learnings": [],
            "offset": 0,
            "limit": 10,
            "total_count": 2,
            "has_more": False,
        }

        html = _build_search_ui(data)

        # Pagination container element should NOT be rendered (offset=0 and has_more=False)
        # Note: "daemon-pagination" class is in CSS, so check for actual element
        assert 'class="daemon-pagination"' not in html
        assert 'data-action="prev"' not in html
        assert 'data-action="next"' not in html


class TestRecallVisualFormat:
    """Tests for recall_visual tool output format."""

    def test_format_with_ui_hint_structure(self):
        """Test that format_with_ui_hint returns correct structure."""
        from daem0nmcp.ui.fallback import format_with_ui_hint

        data = {
            "decisions": [{"id": 1, "content": "test", "relevance": 0.8}],
            "patterns": [],
            "warnings": [],
            "learnings": [],
            "total_count": 1,
        }

        result = format_with_ui_hint(
            data=data, ui_resource="ui://daem0n/search", text="Test text"
        )

        # Should have original data plus ui_resource and text
        assert "ui_resource" in result
        assert result["ui_resource"] == "ui://daem0n/search"
        assert "text" in result
        assert result["text"] == "Test text"
        # Original data should be spread in
        assert "decisions" in result
        assert result["total_count"] == 1

    def test_format_search_results_text(self):
        """Test that format_search_results generates correct text fallback."""
        from daem0nmcp.ui.fallback import format_search_results

        results = [
            {"id": 1, "category": "decision", "content": "Use async", "score": 0.85},
            {
                "id": 2,
                "category": "warning",
                "content": "Watch for deadlocks",
                "score": 0.72,
            },
        ]

        text = format_search_results(
            query="concurrency", results=results, total_count=2
        )

        assert "Search Results for: concurrency" in text
        assert "Found 2 result(s)" in text
        assert "DECISION" in text
        assert "Use async" in text
        assert "WARNING" in text
        assert "Watch for deadlocks" in text
        assert "(score: 0.85)" in text
        assert "(score: 0.72)" in text

    def test_format_search_results_empty(self):
        """Test that format_search_results handles empty results."""
        from daem0nmcp.ui.fallback import format_search_results

        text = format_search_results(query="nonexistent", results=[], total_count=0)

        assert "No results found for: nonexistent" in text

    def test_format_search_results_truncates_long_content(self):
        """Test that format_search_results truncates long content."""
        from daem0nmcp.ui.fallback import format_search_results

        long_content = "x" * 300  # Longer than 200 char limit
        results = [
            {"id": 1, "category": "learning", "content": long_content, "score": 0.5}
        ]

        text = format_search_results(query="test", results=results)

        # Should be truncated with ellipsis
        assert "..." in text
        # Should not contain full 300 chars
        assert long_content not in text
