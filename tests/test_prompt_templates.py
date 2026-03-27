# tests/test_prompt_templates.py
"""Tests for structured prompt templates."""

from daem0nmcp.prompt_templates import PromptSection, PromptTemplate, render_prompt


class TestPromptTemplate:
    """Test prompt template structure."""

    def test_template_creation(self):
        template = PromptTemplate(
            name="briefing",
            sections=[
                PromptSection(name="role", content="You are a memory assistant."),
                PromptSection(name="context", content="Project: {project_name}"),
                PromptSection(name="task", content="Provide the briefing."),
            ],
        )
        assert template.name == "briefing"
        assert len(template.sections) == 3

    def test_render_with_variables(self):
        template = PromptTemplate(
            name="test",
            sections=[
                PromptSection(name="greeting", content="Hello, {name}!"),
            ],
        )
        result = render_prompt(template, {"name": "Alice"})
        assert "Hello, Alice!" in result

    def test_render_multiple_sections(self):
        template = PromptTemplate(
            name="test",
            sections=[
                PromptSection(name="intro", content="Intro text"),
                PromptSection(name="body", content="Body text"),
                PromptSection(name="footer", content="Footer text"),
            ],
        )
        result = render_prompt(template, {})
        assert "Intro text" in result
        assert "Body text" in result
        assert "Footer text" in result


class TestPromptSection:
    """Test individual prompt sections."""

    def test_section_with_optional_flag(self):
        section = PromptSection(
            name="optional", content="Optional content: {data}", optional=True
        )
        assert section.optional is True

    def test_section_weight(self):
        section = PromptSection(name="important", content="Critical info", weight=2.0)
        assert section.weight == 2.0
