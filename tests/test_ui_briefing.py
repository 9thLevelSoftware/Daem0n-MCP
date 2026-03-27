"""Tests for briefing dashboard UI rendering."""

import pytest

from daem0nmcp.ui.fallback import format_briefing_text
from daem0nmcp.ui.resources import _build_briefing_ui

# =============================================================================
# Test Data Fixtures
# =============================================================================


@pytest.fixture
def sample_briefing_data():
    """Complete briefing data for testing."""
    return {
        "status": "ready",
        "statistics": {
            "total_memories": 42,
            "by_category": {"decision": 15, "warning": 8, "pattern": 12, "learning": 7},
            "outcome_rates": {
                "success_rate": 0.73,
                "total_decisions": 15,
                "with_outcome": 11,
            },
        },
        "recent_decisions": [
            {
                "id": 1,
                "content": "Use PostgreSQL for the main database",
                "worked": True,
                "created_at": "2024-01-15T10:30:00Z",
            },
            {
                "id": 2,
                "content": "Try caching with Redis for session storage",
                "worked": False,
                "created_at": "2024-01-14T14:20:00Z",
            },
            {
                "id": 3,
                "content": "Implement JWT authentication",
                "worked": None,  # Pending
                "created_at": "2024-01-16T09:00:00Z",
            },
        ],
        "active_warnings": [
            {
                "id": 10,
                "content": "API rate limits may be hit during peak hours",
                "severity": "high",
            },
            {
                "id": 11,
                "content": "Consider adding request throttling",
                "severity": "medium",
            },
        ],
        "failed_approaches": [
            {"id": 20, "content": "SQLite doesn't scale for concurrent writes"}
        ],
        "git_changes": {
            "total": 5,
            "files": [
                {"status": "A", "path": "src/api/auth.py"},
                {"status": "M", "path": "src/config.py"},
                {"status": "D", "path": "src/old_handler.py"},
            ],
        },
        "focus_areas": [{"topic": "authentication"}, {"topic": "database design"}],
        "message": "3 active warnings require attention.",
    }


@pytest.fixture
def empty_briefing_data():
    """Minimal briefing data with empty lists."""
    return {
        "status": "ready",
        "statistics": {
            "total_memories": 0,
            "by_category": {},
            "outcome_rates": {"success_rate": 0},
        },
        "recent_decisions": [],
        "active_warnings": [],
        "failed_approaches": [],
        "git_changes": {},
        "focus_areas": None,
        "message": "",
    }


# =============================================================================
# Statistics Panel Tests
# =============================================================================


class TestStatisticsPanel:
    """Tests for the statistics summary panel."""

    def test_statistics_panel_rendered(self, sample_briefing_data):
        """Statistics panel shows memory counts."""
        html = _build_briefing_ui(sample_briefing_data)
        assert "daemon-stats" in html
        assert ">42<" in html  # total_memories in value div
        assert ">15<" in html  # decisions count
        # Check warnings count (8) appears - need to be careful as 8 appears in other places
        assert 'daemon-stat__value">8<' in html or ">8</div>" in html

    def test_statistics_success_rate_displayed(self, sample_briefing_data):
        """Success rate is displayed as percentage."""
        html = _build_briefing_ui(sample_briefing_data)
        assert "73%" in html  # success_rate 0.73 formatted

    def test_statistics_panel_empty_data(self, empty_briefing_data):
        """Statistics panel handles zero values."""
        html = _build_briefing_ui(empty_briefing_data)
        assert "daemon-stats" in html
        assert ">0<" in html  # total_memories = 0

    def test_statistics_success_class_applied(self, sample_briefing_data):
        """Success rate stat has success styling class."""
        html = _build_briefing_ui(sample_briefing_data)
        assert "daemon-stat--success" in html


# =============================================================================
# Accordion Section Tests
# =============================================================================


class TestAccordionSections:
    """Tests for accordion section rendering."""

    def test_accordion_structure_present(self, sample_briefing_data):
        """Accordion container is rendered."""
        html = _build_briefing_ui(sample_briefing_data)
        assert "daemon-accordion" in html

    def test_decisions_section_rendered(self, sample_briefing_data):
        """Recent decisions appear in accordion section."""
        html = _build_briefing_ui(sample_briefing_data)
        assert "Recent Decisions" in html
        assert "daemon-accordion__item" in html
        assert "(3)" in html  # count indicator

    def test_warnings_section_rendered(self, sample_briefing_data):
        """Active warnings appear in accordion section."""
        html = _build_briefing_ui(sample_briefing_data)
        assert "Active Warnings" in html
        assert "(2)" in html  # count indicator

    def test_git_changes_section_rendered(self, sample_briefing_data):
        """Git changes appear in accordion section."""
        html = _build_briefing_ui(sample_briefing_data)
        assert "Git Changes" in html
        assert "(5 files)" in html

    def test_focus_areas_section_rendered(self, sample_briefing_data):
        """Focus areas appear with quick-access buttons."""
        html = _build_briefing_ui(sample_briefing_data)
        assert "Focus Areas" in html
        assert "authentication" in html
        assert "database design" in html
        assert 'data-action="focus-area"' in html

    def test_empty_sections_not_rendered(self, empty_briefing_data):
        """Empty sections don't appear in accordion."""
        html = _build_briefing_ui(empty_briefing_data)
        assert "Recent Decisions" not in html
        assert "Active Warnings" not in html


# =============================================================================
# Decision Outcome Tests
# =============================================================================


class TestDecisionOutcomes:
    """Tests for decision outcome indicators."""

    def test_success_outcome_indicator(self, sample_briefing_data):
        """Successful decisions show success badge."""
        html = _build_briefing_ui(sample_briefing_data)
        assert "daemon-badge--success" in html
        assert ">Success<" in html

    def test_failed_outcome_indicator(self, sample_briefing_data):
        """Failed decisions show error badge."""
        html = _build_briefing_ui(sample_briefing_data)
        assert "daemon-badge--error" in html
        assert ">Failed<" in html

    def test_pending_outcome_indicator(self, sample_briefing_data):
        """Pending decisions show neutral badge."""
        html = _build_briefing_ui(sample_briefing_data)
        assert ">Pending<" in html

    def test_decision_content_displayed(self, sample_briefing_data):
        """Decision content is shown."""
        html = _build_briefing_ui(sample_briefing_data)
        assert "Use PostgreSQL" in html


# =============================================================================
# Warning Severity Tests
# =============================================================================


class TestWarningSeverity:
    """Tests for warning severity styling."""

    def test_high_severity_styling(self, sample_briefing_data):
        """High severity warnings get error styling."""
        html = _build_briefing_ui(sample_briefing_data)
        assert "warning-item--high" in html

    def test_medium_severity_styling(self, sample_briefing_data):
        """Medium severity warnings get warning styling."""
        html = _build_briefing_ui(sample_briefing_data)
        assert "warning-item--medium" in html

    def test_warning_content_displayed(self, sample_briefing_data):
        """Warning content is shown."""
        html = _build_briefing_ui(sample_briefing_data)
        assert "API rate limits" in html


# =============================================================================
# Git Changes Tests
# =============================================================================


class TestGitChanges:
    """Tests for git changes section."""

    def test_added_file_styling(self, sample_briefing_data):
        """Added files have success color class."""
        html = _build_briefing_ui(sample_briefing_data)
        assert "git-change--added" in html
        assert "A src/api/auth.py" in html

    def test_modified_file_styling(self, sample_briefing_data):
        """Modified files have warning color class."""
        html = _build_briefing_ui(sample_briefing_data)
        assert "git-change--modified" in html
        assert "M src/config.py" in html

    def test_deleted_file_styling(self, sample_briefing_data):
        """Deleted files have error color class."""
        html = _build_briefing_ui(sample_briefing_data)
        assert "git-change--deleted" in html
        assert "D src/old_handler.py" in html


# =============================================================================
# Briefing Message Tests
# =============================================================================


class TestBriefingMessage:
    """Tests for briefing message display."""

    def test_message_displayed(self, sample_briefing_data):
        """Briefing message appears in output."""
        html = _build_briefing_ui(sample_briefing_data)
        assert "briefing-message" in html
        assert "3 active warnings" in html

    def test_empty_message_not_rendered(self, empty_briefing_data):
        """Empty message doesn't create message block."""
        html = _build_briefing_ui(empty_briefing_data)
        # With empty message, briefing-message div should not be rendered
        # The class may still appear in CSS definitions but the div shouldn't
        assert 'class="briefing-message"' not in html


# =============================================================================
# Text Fallback Tests
# =============================================================================


class TestTextFallback:
    """Tests for text fallback formatter."""

    def test_text_fallback_has_header(self, sample_briefing_data):
        """Text output has session briefing header."""
        text = format_briefing_text(sample_briefing_data)
        assert "Session Briefing" in text
        assert "[READY]" in text

    def test_text_fallback_statistics(self, sample_briefing_data):
        """Text output includes statistics."""
        text = format_briefing_text(sample_briefing_data)
        assert "Total Memories: 42" in text
        assert "Decisions: 15" in text
        assert "Success Rate: 73%" in text

    def test_text_fallback_decisions(self, sample_briefing_data):
        """Text output includes recent decisions."""
        text = format_briefing_text(sample_briefing_data)
        assert "Recent Decisions" in text
        assert "[SUCCESS]" in text
        assert "[FAILED]" in text
        assert "[PENDING]" in text

    def test_text_fallback_warnings(self, sample_briefing_data):
        """Text output includes active warnings."""
        text = format_briefing_text(sample_briefing_data)
        assert "Active Warnings" in text
        assert "[HIGH]" in text
        assert "[MEDIUM]" in text

    def test_text_fallback_git_changes(self, sample_briefing_data):
        """Text output includes git changes."""
        text = format_briefing_text(sample_briefing_data)
        assert "Git Changes" in text
        assert "A src/api/auth.py" in text

    def test_text_fallback_empty_data(self, empty_briefing_data):
        """Text fallback handles empty data gracefully."""
        text = format_briefing_text(empty_briefing_data)
        assert "Session Briefing" in text
        assert "Total Memories: 0" in text
        # Empty lists shouldn't show sections
        assert "Recent Decisions" not in text


# =============================================================================
# Header and Status Tests
# =============================================================================


class TestHeaderAndStatus:
    """Tests for briefing header and status."""

    def test_title_rendered(self, sample_briefing_data):
        """Page title is rendered."""
        html = _build_briefing_ui(sample_briefing_data)
        assert "<title>Session Briefing</title>" in html

    def test_status_displayed(self, sample_briefing_data):
        """Status is displayed in header."""
        html = _build_briefing_ui(sample_briefing_data)
        assert "READY" in html

    def test_csp_header_present(self, sample_briefing_data):
        """CSP meta tag is present."""
        html = _build_briefing_ui(sample_briefing_data)
        assert "Content-Security-Policy" in html
        assert "default-src 'none'" in html


# =============================================================================
# Tool Integration Tests
# =============================================================================


class TestGetBriefingVisualTool:
    """Tests for get_briefing_visual tool output format."""

    def test_format_with_ui_hint_structure(self, sample_briefing_data):
        """Verify format_with_ui_hint output structure."""
        from daem0nmcp.ui.fallback import format_with_ui_hint

        result = format_with_ui_hint(
            data=sample_briefing_data,
            ui_resource="ui://daem0n/briefing",
            text="fallback text",
        )

        assert "ui_resource" in result
        assert "text" in result
        assert result["ui_resource"] == "ui://daem0n/briefing"
        assert result["text"] == "fallback text"

    def test_ui_hint_contains_briefing_data(self, sample_briefing_data):
        """UI hint result contains original briefing data."""
        from daem0nmcp.ui.fallback import format_with_ui_hint

        result = format_with_ui_hint(
            data=sample_briefing_data, ui_resource="ui://daem0n/briefing", text=""
        )

        assert result["status"] == "ready"
        assert result["statistics"]["total_memories"] == 42

    def test_text_fallback_integration(self, sample_briefing_data):
        """Text fallback works with format_with_ui_hint."""
        from daem0nmcp.ui.fallback import format_with_ui_hint

        text = format_briefing_text(sample_briefing_data)
        result = format_with_ui_hint(
            data=sample_briefing_data, ui_resource="ui://daem0n/briefing", text=text
        )

        assert "Session Briefing" in result["text"]
        assert "Total Memories: 42" in result["text"]
