# Workspace access and admission bounds

## Operator configuration

Authentication identifies the caller. A workspace grant separately permits that
caller to use a configured workspace. Briefing, a workspace link, a preflight
token, or an edit-host pairing cannot create that grant.

Local stdio and unauthenticated loopback clients run as the local managed-storage
authority and can use its configured roots. An authenticated HTTP caller uses
the verified JWT subject, represented internally as `oauth-sub:<sub>`. It has no
workspace access until an operator grants it access.

Set `DAEM0NMCP_WORKSPACE_ACCESS_FILE` to a server-owned JSON file, or use
`v7-workspace-access.json` in the configured managed storage directory:

```json
{
  "schema_version": 1,
  "grants": {
    "oauth-sub:alice": ["ws_0123456789abcdef01234567"],
    "oauth-sub:bob": ["ws_fedcba9876543210fedcba98"]
  }
}
```

Use actual opaque IDs from the configured workspace registry. Wildcards and
filesystem paths are not grants. The policy is limited to 64 KiB, 128 principals,
and 128 distinct workspace IDs per principal. Duplicate JSON keys, unknown
fields, malformed grants, missing files, and unsafe file permissions deny remote
workspace access. The server does not disclose these private configuration
details in tool errors.

The policy file and its parent directory must be protected for the server user.
On POSIX use 0600 and 0700. On Windows use a protected DACL granting only the
current server user and SYSTEM; `chmod` alone is insufficient. The package's
`protected_files.write_new_owner_only_file` creates a new file with these
permissions on either platform. `protect_owner_only_file` and
`ensure_owner_only_directory` protect existing operator-managed paths. Use a
dedicated directory; do not change permissions on a shared project directory.
Symlink and reparse-point ancestry is rejected. Update a policy by writing a
protected candidate beside it and atomically replacing the file.

Grants are read again for each authorization. Revocation takes effect for new
calls, queued task execution, task lifecycle requests and waiting result reads,
and bridge routes. A denied caller receives no workspace data even if its MCP
session previously completed briefing. Linked recall checks every source and
rechecks access before returning its combined result. Regranting access does not
replace the existing requirement for briefing and exact-request preflight.

## Admission limits

Each application process admits at most 64 simultaneous tool/resource calls,
8 for one principal, and 16 for one workspace. Excess work is rejected with
`ADMISSION_LIMIT_REACHED`; retry after an active call completes. Cancellation
releases capacity. Accounting entries disappear when their count reaches zero.

The durable dispatcher admits at most 1,000 pending jobs across its authority,
100 per principal, and 250 per workspace. All counts and admission reservations
are checked in one SQLite write transaction. Excess work returns `TASK_QUEUE_FULL`
before capability consumption or operation side effects. An exact retry returns
its existing admission without consuming another slot. Active includes pending
authorization, queued, running, and cancellation-requested jobs. Terminal jobs do
not occupy admission slots. The SQLite authority directory/database and sidecars
use owner-only protections on Windows and POSIX.

These limits cover one application server and its embedded workers. They are
not a distributed rate-limit service. Configure the HTTPS reverse proxy's body,
connection, and time limits as described in [the HTTP boundary guide](p9-http-boundary.md).

## Development evidence and remaining gates

- Actual packaged HTTP MCP with ephemeral RSA JWKS and signed JWTs rejects
  missing, expired, and wrong-audience tokens; permits the granted workspace;
  denies another configured workspace; and observes grants/revocation within
  an existing session for tools and resources.
- `tests/test_workspace_access.py` covers malformed policies, exact roots,
  principal isolation, and briefing without access.
- `tests/api_v7/test_admission_limits.py` exercises all three concurrent limits
  and cancellation cleanup.
- Real authenticated Valkey tests exercise durable scoped quotas, exact retries,
  revocation across task status/result/list/cancel, and revoked queued execution.

This is development evidence, not remote deployment certification. Independent
security review, the real HTTPS proxy/client matrix, full enabled-profile paths,
performance, soak, and final-commit evidence remain release gates.
