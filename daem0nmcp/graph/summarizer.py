"""Community summarization for GraphRAG hierarchical queries."""

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class SummaryConfig:
    """Configuration for community summarization."""

    max_content_length: int = 4000  # Max chars of member content to consider
    max_summary_length: int = 500  # Target summary length
    include_categories: bool = True  # Group by memory category
    include_stats: bool = True  # Include member count, etc.
    llm_enabled: bool = False  # Use LLM for summarization
    llm_temperature: float = 0.0  # Deterministic for consistency


class CommunitySummarizer:
    """
    Generates summaries for memory communities.

    Supports two modes:
    1. Extractive (default): Concatenates key information from members
    2. LLM-based (optional): Uses LLM for natural language summary

    Anti-hallucination measures:
    - Extractive prompts: "Summarize ONLY what is stated"
    - Source attribution: Include memory IDs in context
    - Temperature 0: Deterministic outputs
    """

    def __init__(
        self,
        config: SummaryConfig | None = None,
        llm_func: Callable[[str], Awaitable[str]] | None = None,
    ):
        """
        Initialize summarizer.

        Args:
            config: Summary configuration
            llm_func: Optional async function that takes prompt, returns summary
                      Signature: async def llm(prompt: str) -> str
        """
        self.config = config or SummaryConfig()
        self.llm_func = llm_func

    async def summarize_community(
        self,
        community_name: str,
        members: list[dict[str, Any]],
        entity_names: list[str] | None = None,
    ) -> str:
        """
        Generate summary for a community.

        Args:
            community_name: Name of the community
            members: List of memory dicts with content, category, etc.
            entity_names: Optional list of key entities in this community

        Returns:
            Generated summary text
        """
        if not members:
            return f"Empty community: {community_name}"

        if self.config.llm_enabled and self.llm_func:
            return await self._llm_summarize(community_name, members, entity_names)
        else:
            return self._extractive_summarize(community_name, members, entity_names)

    def _extractive_summarize(
        self,
        community_name: str,
        members: list[dict[str, Any]],
        entity_names: list[str] | None = None,
    ) -> str:
        """
        Generate summary by extracting key information from members.

        This is the default approach - no external dependencies.
        """
        parts = [f"Community: {community_name}"]

        if self.config.include_stats:
            parts.append(f"Contains {len(members)} memories.")

        if entity_names:
            parts.append(f"Key entities: {', '.join(entity_names[:5])}")

        if self.config.include_categories:
            # Group by category
            by_category: dict[str, list[str]] = {}
            for mem in members:
                cat = mem.get("category", "other")
                if cat not in by_category:
                    by_category[cat] = []
                content = mem.get("content", "")
                # Truncate long content
                if len(content) > 150:
                    content = content[:147] + "..."
                by_category[cat].append(content)

            for category, contents in by_category.items():
                parts.append(f"\n{category.title()}s ({len(contents)}):")
                # Show up to 3 items per category
                for content in contents[:3]:
                    parts.append(f"  - {content}")
                if len(contents) > 3:
                    parts.append(f"  ... and {len(contents) - 3} more")

        summary = "\n".join(parts)

        # Truncate if too long
        if len(summary) > self.config.max_summary_length:
            summary = summary[: self.config.max_summary_length - 3] + "..."

        return summary

    async def _llm_summarize(
        self,
        community_name: str,
        members: list[dict[str, Any]],
        entity_names: list[str] | None = None,
    ) -> str:
        """
        Generate summary using LLM.

        Uses extractive prompting to prevent hallucination.
        """
        # Build context from members
        context_parts = []
        total_len = 0

        for mem in members:
            content = mem.get("content", "")
            category = mem.get("category", "other")
            mem_id = mem.get("id", "?")

            entry = f"[{category}:{mem_id}] {content}"
            if total_len + len(entry) > self.config.max_content_length:
                break

            context_parts.append(entry)
            total_len += len(entry)

        context = "\n".join(context_parts)

        # Build extractive prompt
        entity_hint = ""
        if entity_names:
            entity_hint = (
                f"\nKey entities in this community: {', '.join(entity_names[:5])}"
            )

        prompt = f"""Summarize the following memories that form a community called "{community_name}".
{entity_hint}

IMPORTANT: Only include information that is explicitly stated in the memories below.
Do NOT add information that isn't present.
Do NOT make inferences beyond what is directly stated.

Memories:
{context}

Write a concise summary (2-4 sentences) covering:
1. The main topics or decisions in this community
2. Any patterns or recurring themes
3. Key outcomes if mentioned

Summary:"""

        try:
            summary = await self.llm_func(prompt)
            return summary.strip()
        except Exception as e:
            logger.warning(f"LLM summarization failed: {e}, falling back to extractive")
            return self._extractive_summarize(community_name, members, entity_names)

    async def summarize_hierarchy(
        self,
        communities: list[dict[str, Any]],
        fetch_members_func: Callable[[list[int]], Awaitable[list[dict[str, Any]]]],
    ) -> list[dict[str, Any]]:
        """
        Generate summaries for a hierarchy of communities (bottom-up).

        GraphRAG pattern: Leaf communities summarize from members,
        higher-level communities summarize from child summaries.

        Args:
            communities: List of community dicts with level, parent_id, member_ids
            fetch_members_func: Async function to fetch memory dicts for a community
                               Signature: async def fetch(member_ids: List[int]) -> List[Dict]

        Returns:
            Communities with summaries populated
        """
        # Group by level
        by_level: dict[int, list[dict[str, Any]]] = {}
        for c in communities:
            level = c.get("level", 0)
            if level not in by_level:
                by_level[level] = []
            by_level[level].append(c)

        # Track generated summaries by community ID
        summaries: dict[int, str] = {}

        # Process bottom-up (level 0 first)
        for level in sorted(by_level.keys()):
            for community in by_level[level]:
                comm_id = community.get("id") or community.get("leiden_community_id")

                if level == 0:
                    # Leaf: summarize from member memories
                    member_ids = community.get("member_ids", [])
                    members = await fetch_members_func(member_ids) if member_ids else []
                else:
                    # Higher level: summarize from child summaries
                    # Find children
                    children = [c for c in communities if c.get("parent_id") == comm_id]
                    # Create pseudo-members from child summaries
                    members = [
                        {
                            "id": child.get("id"),
                            "category": f"level-{child.get('level', 0)}-summary",
                            "content": summaries.get(
                                child.get("id"), child.get("summary", "")
                            ),
                        }
                        for child in children
                    ]

                entity_names = community.get("tags", [])
                summary = await self.summarize_community(
                    community.get("name", f"Community {comm_id}"),
                    members,
                    entity_names,
                )

                community["summary"] = summary
                if comm_id:
                    summaries[comm_id] = summary

        return communities
