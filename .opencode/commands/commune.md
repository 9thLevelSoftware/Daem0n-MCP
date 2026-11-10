---
description: Start the Daem0n v7 session for a registered workspace
---

The first Daem0n call establishes the scoped session and briefs automatically
in `meta.covenant.auto_brief`. For the full brief, call
`daem0nmcp_session_brief(workspace_id="$ARGUMENTS")`.
The argument must be the configured opaque `workspace_id`, not a path.

After receiving the briefing results:
1. Report the session status, active warnings, and recent activity summary
2. Acknowledge that the v7 scoped session is active

This slash-command filename is only a host shortcut. For v6 migration details,
use `docs/v6-to-v7-tools.json`.
