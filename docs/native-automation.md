# Native client automation

Native MCP registration, instruction loading, reminders, and edit enforcement
are separate capabilities. The acceptance matrix records native-client evidence
for each installed version and platform; installing a hook is not acceptance.

## Installation

The multi-client installer accepts `--automation off|remind|enforce`:

```text
python -m daem0nmcp.cli --project-path PROJECT install-client codex --automation remind
python -m daem0nmcp.cli --project-path PROJECT install-client opencode --client-version 1.18.21 --automation enforce
python -m daem0nmcp.cli --project-path PROJECT install-claude-hooks --automation enforce --client-version 2.1.295
```

An omitted option preserves the previous installer behavior. Explicit modes
report their installed mode, events and covered tools. Explicit Claude modes
write the project's `.claude/settings.local.json`; the legacy omitted-option
Claude installer retains its existing user-hook location. Existing unrelated
commands and settings are preserved, including commands sharing a hook entry.

Claude enforcement requires an observed version 2.1.295 or later. Its edit
handlers use native `onFailure: "block"` with a bounded timeout and an exec-form
Python launcher that ignores inherited import settings. Older Claude versions
can install reminders, but are rejected for enforcement because failed or timed
out command hooks can allow the original tool call.

| Client | Reminder adapter | Enforcement adapter |
| --- | --- | --- |
| Claude Code | Session/edit hooks | `Edit`, `Write`, `NotebookEdit` |
| OpenCode V1 | System/session/edit plugin | `edit`, `write`, `apply_patch`; actual tool exposure depends on the selected model |
| Codex | Project command hooks | Unsupported until authoritative receipt/result capture is established |
| agy | Project command hooks | Unsupported: documented post-tool event omits the actual MCP result |
| OMP | Project `before_agent_start` extension | Unsupported until exact MCP response and call binding are established |
| Pi | Project `before_agent_start` extension | Unsupported until exact MCP response and call binding are established |
| OpenCode V2 >=2.2.7 | Version-specific session-context plugin | `edit`, `write`, `patch`; see the native acceptance matrix for actual host proof |

Codex/agy hooks provide context without overriding native approvals. OMP/Pi
extensions add a covenant reminder to each ordinary model turn; they do not
intercept edits. Host project trust still controls whether hooks/extensions load.
Unsupported enforcement is an actionable installer error before publication.

## Exact native edit approval

Enforcement uses the existing authenticated edit bridge and `edit_preflight`
operation. The production gate opens a host session, normalizes declared file
edits, records file preimages, and denies the first edit with an opaque request
and its remedy. The model calls `edit_preflight`, and the adapter stages only
the actual host-observed structured MCP response. The unchanged native retry
consumes that expiring, single-use receipt before the host executes it.

The host state also stores a SHA-256 digest of the original tool name and
arguments. Equivalent normalized paths do not make changed original arguments
eligible for an existing approval. Older pending host-state rows lacking this
digest cannot authorize a new enforcing retry. Only hashes and opaque IDs enter
the owner-protected host-state database.

Missing credentials, malformed native events, unavailable bridge transport,
unsupported edit arguments and failed approval consumption deny covered edits.
OpenCode's explicit installed mode is embedded in its plugin, so a missing or
malformed sidecar cannot silently convert enforcement into reminders. Reminder
mode retains its best-effort behavior when hook infrastructure is unavailable.

Post-edit capture requires the exact consumed native call and a successful
host completion event. Failed, skipped, denied or missing results cannot create
a successful capture. An observed failed or skipped exact call settles its
host-local binding so the unchanged edit can obtain a new request and approval;
the broker's previous receipt remains spent. Missing completion evidence keeps
the call in flight. The candidate remains subject to the existing reviewed
promotion protocol before ordinary memory recall can observe it.

This covers the declared native file-edit tools. It does not mediate arbitrary
shell writes or establish a filesystem sandbox.

On Unix, endpoints that exceed the socket path byte limit use a deterministic
short endpoint in an owner-only per-user temporary directory. The configured
runtime directory is still validated, and the endpoint digest includes both
its canonical path and the credential identity. Credentials remain in their
protected installation location. Existing short endpoints and Windows named
pipes retain their previous addresses.

Unix bridge startup serializes access to each endpoint with an owner-protected
lifetime lock. It reclaims an existing socket only after verifying its owner,
permissions and identity and confirming that no listener accepts connections.
Live sockets, replacements and uncertain probe results are preserved. This
allows a later native-client session to recover after an interrupted server;
immediate socket removal on every host shutdown is not assumed.

## Verification boundary

The focused Python suite exercises the production gate against a real stdio MCP
server and bridge: deny, real structured approval, exact retry, original-argument
mismatch, failed/skipped completion, changed post-event arguments, capture and
duplicate-capture rejection. Transport/broker suites retain expiry, replay,
preimage, workspace/session, concurrency and remote-binding coverage.

Native host/platform results are reported separately after execution. In
particular, the reminder adapters and OpenCode V2 configuration must not be
described as native-client certified from installer tests alone.
