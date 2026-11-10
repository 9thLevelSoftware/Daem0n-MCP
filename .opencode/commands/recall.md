---
description: Retrieve bounded memories from a Daem0n v7 workspace
---

Call `daem0nmcp_memory_recall(workspace_id="<workspace_id>", query="$ARGUMENTS", limit=10)` immediately.

The first Daem0n call briefs automatically in `meta.covenant.auto_brief`;
call `session_brief` for the full brief. For other capabilities, use
`daem0n_tools_search(query)`, then `daem0n_tool_call(workspace_id, tool, arguments)`.

Present the retrieved memories in a clear format:
- Show each record's content, type, and creation time
- Highlight any warnings or failed approaches
- Preserve evidence references and truncation indicators

For service diagnostics use
`daem0nmcp_system_health(workspace_id="<workspace_id>")`. Do not replace the
opaque workspace ID with a project path.
