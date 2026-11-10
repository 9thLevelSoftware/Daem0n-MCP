"""Tool descriptions and titles are hand-written and good enough to search."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastmcp import Client

from daem0nmcp.api.v7.factory import (
    build_inspectable_v7_server,
    build_tool_search_index,
    build_v7_manifest,
)
from daem0nmcp.api.v7.gateway_operations import GATEWAY_TOOL_NAMES
from daem0nmcp.api.v7.policy import V7_TOOL_LEVELS
from daem0nmcp.api.v7.production import build_production_surface
from daem0nmcp.api.v7.registry import ManifestError, ToolSpec, V7Manifest
from daem0nmcp.api.v7.tools import ToolDescriptor
from daem0nmcp.config import Settings
from daem0nmcp.covenant import CovenantLevel
from tests.api_v7.process_client import initialize_workspaces

MAX_DESCRIPTION_CHARACTERS = 300
MAX_TITLE_CHARACTERS = 48


async def _unused(**arguments: object) -> object:
    return arguments


class _Resources:
    async def warnings(self, workspace_id: str) -> object:
        return workspace_id

    async def failures(self, workspace_id: str) -> object:
        return workspace_id

    async def rules(self, workspace_id: str) -> object:
        return workspace_id

    async def active_context(self, workspace_id: str) -> object:
        return workspace_id


def _handlers() -> dict[str, object]:
    return dict.fromkeys(V7_TOOL_LEVELS, _unused)


def _manifest() -> V7Manifest:
    return build_v7_manifest(_handlers(), _Resources())


def _placeholder(name: str, category: str) -> str:
    """The text every tool carried before descriptions were written."""

    phrase = name.replace("_", " ")
    return f"{phrase.capitalize()} for the authorized workspace ({category})."


def _name_words(name: str) -> set[str]:
    return set(name.split("_"))


def test_every_tool_has_a_written_description() -> None:
    manifest = _manifest()
    assert len(manifest.tools) == 77
    descriptions: dict[str, str] = {}
    for spec in manifest.tools:
        description = spec.description
        assert description != _placeholder(spec.name, spec.category), spec.name
        assert "for the authorized workspace" not in description, spec.name
        assert 40 <= len(description) <= MAX_DESCRIPTION_CHARACTERS, (
            spec.name,
            len(description),
        )
        assert description == description.strip(), spec.name
        assert description.endswith("."), spec.name
        assert "\n" not in description and "  " not in description, spec.name
        # A description says more than the words of the tool's own name.
        words = {
            word.strip(".,;:()").casefold() for word in description.split()
        } - _name_words(spec.name)
        assert len(words) >= 8, spec.name
        # The search descriptor carries the same text on the guarded wire.
        ToolDescriptor(
            name=spec.name,
            description=description,
            category=spec.category,
            covenant=spec.covenant.value,
            read_only=spec.annotations["readOnlyHint"],
            destructive=spec.annotations["destructiveHint"],
            idempotent=spec.annotations["idempotentHint"],
            open_world=spec.annotations["openWorldHint"],
            listed=True,
            input_schema=spec.input_schema,
        )
        descriptions[spec.name] = description
    assert len(set(descriptions.values())) == len(descriptions)


def test_every_tool_has_a_short_unique_title() -> None:
    manifest = _manifest()
    titles: dict[str, str] = {}
    for spec in manifest.tools:
        title = spec.title
        assert isinstance(title, str) and title.strip() == title, spec.name
        assert 3 <= len(title) <= MAX_TITLE_CHARACTERS, (spec.name, title)
        assert "_" not in title and title != spec.name, spec.name
        assert not title.endswith("."), spec.name
        assert title[0].isupper(), spec.name
        titles[spec.name] = title
    assert len(set(titles.values())) == len(titles)


def test_a_full_manifest_refuses_a_tool_without_a_title() -> None:
    manifest = _manifest()
    tools = tuple(
        spec.replace(title="") if spec.name == "memory_store" else spec
        for spec in manifest.tools
    )
    with pytest.raises(ManifestError, match="title"):
        V7Manifest(tools=tools, resources=manifest.resources, policy=manifest.policy)


def test_a_title_is_bounded_single_line_text() -> None:
    def spec(title: str) -> ToolSpec:
        template = _manifest().tool_map["memory_store"]
        return ToolSpec(
            name="memory_store",
            title=title,
            description=template.description,
            handler=_unused,
            input_model=template.input_model,
            output_model=template.output_model,
            category="memory",
            tags=("memory",),
            covenant=CovenantLevel.COUNSEL,
            task_mode="forbidden",
            annotations=dict(template.annotations),
        )

    assert spec("Store Memory").title == "Store Memory"
    for bad in (" padded ", "two\nlines", "x" * 81):
        with pytest.raises(ManifestError, match="title"):
            spec(bad)


def test_the_inspectable_listing_carries_the_title_and_description() -> None:
    manifest = _manifest()
    inspectable = build_inspectable_v7_server(_handlers(), _Resources())
    assert {
        tool["name"]: (tool["title"], tool["description"])
        for tool in inspectable.list_tools()
    } == {spec.name: (spec.title, spec.description) for spec in manifest.tools}


async def test_tools_list_carries_the_title_and_description(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path.resolve()
    monkeypatch.setenv("DAEM0NMCP_PROJECT_ROOT", str(root))
    await initialize_workspaces((root,))
    surface = build_production_surface(
        "stdio",
        settings=Settings(
            project_root=str(root),
            workspace_roots=[str(root)],
            tool_surface="full",
            covenant_mode="guided",
            dream_enabled=False,
        ),
        environ={},
    )
    expected = {
        spec.name: (spec.title, spec.description) for spec in surface.manifest.tools
    }
    assert len(expected) == 77
    async with Client(surface.build_server()) as client:
        listed = {
            tool.name: (tool.title, tool.description)
            for tool in await client.list_tools()
        }
    assert listed == expected


# Realistic agent phrasings; the expected tool must be in the first three
# results the gateway returns (it never returns the gateway tools themselves).
SEARCH_CASES: tuple[tuple[str, frozenset[str]], ...] = (
    ("delete old memories", frozenset({"memory_prune", "memory_prune_preview"})),
    ("why did this fail before", frozenset({"memory_recall", "session_brief"})),
    ("import a backup", frozenset({"workspace_import"})),
    ("export everything to a backup file", frozenset({"workspace_export"})),
    (
        "find duplicate memories",
        frozenset({"memory_duplicates_preview", "memory_duplicates_cleanup"}),
    ),
    ("what changed since I last looked", frozenset({"session_updates_get"})),
    ("fetch a web page and remember it", frozenset({"document_ingest_url"})),
    ("run some python code", frozenset({"sandbox_execute_python"})),
    ("which rules apply to what I am about to do", frozenset({"rule_check"})),
    ("what do we know about this file", frozenset({"memory_recall_file"})),
    (
        "what breaks if I change this function",
        frozenset({"code_impact_analyze"}),
    ),
    (
        "how did this record look last month",
        frozenset({"memory_at_time_get", "memory_versions_list"}),
    ),
    ("connect two related memories", frozenset({"memory_link"})),
    (
        "merge another project's memories into this one",
        frozenset({"workspace_consolidate", "workspace_consolidation_preview"}),
    ),
    ("does this statement contradict what is stored", frozenset({"memory_verify"})),
    ("find TODO comments in the code", frozenset({"code_todos_scan"})),
    ("hide a record without deleting it", frozenset({"memory_archive_set"})),
    ("draw the knowledge graph as a diagram", frozenset({"knowledge_graph_render"})),
    ("is the server healthy", frozenset({"system_health"})),
    (
        "summarize many old memories into one",
        frozenset({"memory_compact", "memory_compaction_preview"}),
    ),
    ("keep a memory in focus for this session", frozenset({"active_context_add"})),
    ("search memories for an exact phrase", frozenset({"memory_search_text"})),
    ("record whether the approach worked", frozenset({"memory_record_outcome"})),
    ("save a decision and its rationale", frozenset({"memory_store"})),
    ("stop a memory from being pruned", frozenset({"memory_pin_set"})),
    ("shorten a long text to fit the context", frozenset({"context_compress"})),
    ("turn off a rule", frozenset({"rule_update"})),
    (
        "recall memories automatically when a file is edited",
        frozenset({"context_trigger_create", "context_triggers_match"}),
    ),
    ("search another workspace's memories too", frozenset({"workspace_link"})),
    ("rebuild the search index", frozenset({"projection_rebuild"})),
    ("remove stale records", frozenset({"memory_prune"})),
    ("what went wrong last time we tried this", frozenset({"memory_recall"})),
    ("restore from an export bundle", frozenset({"workspace_import"})),
    ("back up the workspace", frozenset({"workspace_export"})),
    ("get rid of repeated entries", frozenset({"memory_duplicates_cleanup"})),
    ("poll for new events", frozenset({"session_updates_get"})),
    (
        "read documentation from a website into memory",
        frozenset({"document_ingest_url"}),
    ),
    ("execute a script", frozenset({"sandbox_execute_python"})),
    ("history of a record", frozenset({"memory_versions_list"})),
    ("add a new rule", frozenset({"rule_create"})),
    ("show the graph", frozenset({"knowledge_graph_get"})),
    ("list clusters of memories", frozenset({"community_list"})),
    ("mark a decision as failed", frozenset({"memory_record_outcome"})),
    ("remember this lesson", frozenset({"memory_store"})),
    ("disable a trigger", frozenset({"context_trigger_delete"})),
    ("unarchive a memory", frozenset({"memory_archive_set"})),
    ("fact check a paragraph", frozenset({"memory_verify"})),
    ("search the codebase for a class", frozenset({"code_search"})),
    ("get a token before a write", frozenset({"memory_preflight"})),
    ("start the session", frozenset({"session_brief"})),
    ("copy memories from another workspace", frozenset({"workspace_consolidate"})),
    ("vector index is stale", frozenset({"projection_rebuild"})),
)


def _top_three(manifest: V7Manifest, query: str) -> list[str]:
    index = build_tool_search_index(manifest)
    return [
        match.name
        for match in index.search(query, top_k=5)
        if match.name not in GATEWAY_TOOL_NAMES
    ][:3]


@pytest.mark.parametrize(("query", "expected"), SEARCH_CASES)
def test_an_agent_query_finds_the_tool(query: str, expected: frozenset[str]) -> None:
    assert expected <= set(V7_TOOL_LEVELS)
    found = _top_three(_manifest(), query)
    assert expected & set(found), (query, found)


def test_the_generated_placeholders_could_not_answer_those_queries() -> None:
    manifest = _manifest()
    placeholder = V7Manifest(
        tools=tuple(
            spec.replace(description=_placeholder(spec.name, spec.category))
            for spec in manifest.tools
        ),
        resources=manifest.resources,
        policy=manifest.policy,
    )
    missed = [
        query
        for query, expected in SEARCH_CASES
        if not expected & set(_top_three(placeholder, query))
    ]
    # The names alone answer a few; the written text has to answer the rest.
    assert len(missed) >= len(SEARCH_CASES) // 2, missed
