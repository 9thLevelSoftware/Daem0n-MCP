---
description: Retrieve bounded memories from a Daem0n v7 workspace
---

Call `daem0nmcp_memory_recall(workspace_id="<workspace_id>", query="$ARGUMENTS", limit=10)` immediately.

Guided mode automatically briefs an unbriefed session on `memory_recall`, `memory_preflight`, `memory_store`, or `memory_record_outcome`, returning `meta.covenant.auto_brief`. Diagnostic and discovery calls do not trigger briefing. Call `session_brief` for the full brief.
For other capabilities, use
`daem0n_tools_search(query)`, then `daem0n_tool_call(workspace_id, tool, arguments)`.

Present the retrieved memories in a clear format:
- Show each record's content, type, and creation time
- Highlight any warnings or failed approaches
- Preserve evidence references and truncation indicators

For service diagnostics use
`daem0nmcp_system_health(workspace_id="<workspace_id>")`. Do not replace the
opaque workspace ID with a project path.
