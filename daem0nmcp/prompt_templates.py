# daem0nmcp/prompt_templates.py
"""
Prompt Templates - Structured prompt management with section-wise optimization.

Inspired by AutoPDL's modular prompt optimization approach.
Each prompt is composed of sections that can be independently optimized.
"""

from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class PromptSection:
    """A section of a prompt template."""

    name: str  # e.g., "role", "context", "task", "constraints", "format"
    content: str  # Template content with {variable} placeholders
    optional: bool = False  # Can be omitted if variables missing
    weight: float = 1.0  # Importance weight for optimization


@dataclass
class PromptTemplate:
    """A structured prompt template."""

    name: str  # Template identifier
    sections: list[PromptSection]
    version: str = "1.0"
    description: str | None = None
    created_at: datetime = field(default_factory=datetime.now)


@dataclass
class PromptVariant:
    """A variant of a prompt for A/B testing."""

    template_name: str
    variant_id: str
    section_overrides: dict[str, str]  # section_name -> new content
    metrics: dict[str, float] = field(default_factory=dict)  # success_rate, etc.


def render_prompt(
    template: PromptTemplate, variables: dict[str, str], separator: str = "\n\n"
) -> str:
    """
    Render a prompt template with variables.

    Args:
        template: The prompt template
        variables: Variable values to substitute
        separator: Separator between sections

    Returns:
        Rendered prompt string
    """
    rendered_sections = []

    for section in template.sections:
        try:
            content = section.content.format(**variables)
            rendered_sections.append(content)
        except KeyError:
            if not section.optional:
                # Keep placeholder for required sections
                rendered_sections.append(section.content)
            # Skip optional sections with missing variables

    return separator.join(rendered_sections)


def create_section(name: str, content: str, **kwargs) -> PromptSection:
    """Convenience function to create a prompt section."""
    return PromptSection(name=name, content=content, **kwargs)


# Default templates
BRIEFING_TEMPLATE = PromptTemplate(
    name="briefing",
    description="Session start briefing template",
    sections=[
        PromptSection(
            name="role",
            content="You are Daem0n, an AI memory assistant for the project.",
        ),
        PromptSection(
            name="context",
            content="Project: {project_name}\nMemories: {memory_count}\nRules: {rule_count}",
        ),
        PromptSection(
            name="active_context",
            content="Active context items:\n{active_items}",
            optional=True,
        ),
        PromptSection(
            name="warnings", content="Recent warnings:\n{warnings}", optional=True
        ),
        PromptSection(
            name="task", content="Provide relevant context for the current session."
        ),
    ],
)

RECALL_TEMPLATE = PromptTemplate(
    name="recall",
    description="Memory recall response template",
    sections=[
        PromptSection(name="results", content="Found {count} relevant memories:"),
        PromptSection(name="memories", content="{memory_list}"),
        PromptSection(
            name="suggestions",
            content="Related topics: {related_topics}",
            optional=True,
        ),
    ],
)
