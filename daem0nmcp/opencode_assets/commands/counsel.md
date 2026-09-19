---
description: Request an exact Daem0n v7 preflight capability
---

Parse `$ARGUMENTS` as the intended protected v7 tool and its arguments, then
call:

```text
daem0nmcp_memory_preflight(
    workspace_id="<workspace_id>",
    target_tool="<exact-protected-tool>",
    target_arguments={<exact arguments without workspace_id or preflight_token>},
    description="$ARGUMENTS"
)
```

After receiving the preflight results:
1. Report any WARNINGS the user must know about
2. Report any FAILED APPROACHES that are relevant
3. Report any must_not constraints that apply
4. Preserve the token only for the exact request it authorizes

Do not claim a generic edit authorization. The server binds the capability to
the workspace, principal, session, target tool, and canonical arguments.
