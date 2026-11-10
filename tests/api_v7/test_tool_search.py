from __future__ import annotations

import unittest

from daem0nmcp.tool_search import ToolMetadata, ToolSearchIndex


class StableToolSearchTests(unittest.TestCase):
    def test_document_ids_are_stable_and_lookup_is_direct(self) -> None:
        first = ToolSearchIndex()
        second = ToolSearchIndex()
        tools = (
            ToolMetadata(
                name="memory_store",
                description="Store durable evidence-aware memory",
                category="memory",
                tags=["store", "event"],
            ),
            ToolMetadata(
                name="memory_recall",
                description="Retrieve cited memory evidence",
                category="memory",
                tags=["search", "evidence"],
            ),
            ToolMetadata(
                name="rule_list",
                description="List applicable project rules",
                category="rules",
            ),
        )
        for tool in tools:
            first.add_tool(tool)
        for tool in reversed(tools):
            second.add_tool(tool)

        self.assertEqual(first.document_ids, second.document_ids)
        self.assertEqual(
            [tool.name for tool in first.search("durable store")],
            [tool.name for tool in second.search("durable store")],
        )
        self.assertEqual(first.search("durable store")[0].name, "memory_store")

    def test_replacing_and_removing_a_name_cannot_leave_stale_documents(self) -> None:
        index = ToolSearchIndex()
        index.add_tool(ToolMetadata("memory_store", "old phrase"))
        index.add_tool(ToolMetadata("memory_store", "new durable phrase"))
        self.assertEqual(len(index), 1)
        self.assertEqual(index.search("new durable")[0].name, "memory_store")
        index.remove_tool("memory_store")
        self.assertEqual(index.search("new durable"), [])
        self.assertEqual(index.document_ids, {})

    def _index(self) -> ToolSearchIndex:
        index = ToolSearchIndex()
        for name, title, description in (
            (
                "memory_prune",
                "Prune Old Memories",
                "Deletes the decisions that are old and unused.",
            ),
            (
                "rule_list",
                "List Rules",
                "List the rules for the workspace in the order of priority.",
            ),
            (
                "memory_archive_set",
                "Archive a Memory",
                "Hide the record from the recall of the workspace.",
            ),
        ):
            index.add_tool(
                ToolMetadata(name=name, title=title, description=description)
            )
        return index

    def test_function_words_do_not_count_as_a_match(self) -> None:
        index = self._index()
        # "the", "of" and "for" are in every description; only "archive" counts.
        self.assertEqual(
            [tool.name for tool in index.search("the archive of the record for me")],
            ["memory_archive_set"],
        )
        self.assertEqual(
            [tool.name for tool in index.search("what is the priority for this")],
            ["rule_list"],
        )

    def test_a_query_of_only_function_words_still_searches(self) -> None:
        index = ToolSearchIndex()
        index.add_tool(ToolMetadata("which_tool", "Report which tool is in use."))
        self.assertEqual(index.search("which")[0].name, "which_tool")

    def test_word_endings_are_folded(self) -> None:
        index = self._index()
        for query in ("delete", "deleting decision", "pruned memory", "archived"):
            with self.subTest(query=query):
                self.assertTrue(index.search(query), query)
        self.assertEqual(index.search("deleted")[0].name, "memory_prune")
        self.assertEqual(index.search("hiding records")[0].name, "memory_archive_set")

    def test_a_rare_word_and_a_name_word_outrank_a_common_one(self) -> None:
        index = ToolSearchIndex()
        index.add_tool(ToolMetadata("alpha_tool", "Workspace memory workspace prune."))
        index.add_tool(ToolMetadata("beta_tool", "Workspace memory listing."))
        index.add_tool(ToolMetadata("prune_tool", "Workspace cleanup."))
        # "workspace" is everywhere; "prune" is rare and names prune_tool.
        self.assertEqual(
            [tool.name for tool in index.search("workspace prune")],
            ["prune_tool", "alpha_tool", "beta_tool"],
        )

    def test_the_title_is_searched(self) -> None:
        index = ToolSearchIndex()
        index.add_tool(ToolMetadata("t_one", "Does a thing.", title="Backup Export"))
        index.add_tool(ToolMetadata("t_two", "Does another thing."))
        self.assertEqual([t.name for t in index.search("backup")], ["t_one"])


if __name__ == "__main__":
    unittest.main()
