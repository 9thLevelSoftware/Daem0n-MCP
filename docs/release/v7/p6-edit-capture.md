# P6 native edit approval and reviewed capture

## Host protocol

The native client adapter owns file inspection and native edit execution. It
normalizes the exact native tool arguments, workspace-relative affected paths,
and every file preimage before it asks the bridge to create a pending request.
The MCP model receives only the opaque `edit_request_id` and this remedy:

```json
{
  "tool": "edit_preflight",
  "arguments": {
    "workspace_id": "ws_...",
    "edit_request_id": "edt_...",
    "description": "Describe the exact planned native edit"
  }
}
```

`edit_preflight` is a distinct Communion operation. A `memory_preflight`
capability cannot authorize a native edit. The approval authority binds the
host principal, host session, actual MCP transport session, workspace, exact
argument hash, affected paths, and file preimages. Its receipt expires after
120 seconds and can be consumed once. Consumption occurs in an immediate
transaction before the adapter performs the unchanged retry. Any changed
argument, preimage, principal, session, workspace, expired receipt, or replay
is denied without consuming a still-valid exact receipt.

The adapter must stage the complete structured response returned by the real
`edit_preflight` MCP invocation. The bridge rejects a naked token, a response
for another operation, a failed response, or a response whose workspace or
request ID changed. Assistant-authored text is not accepted as an approval.

## Protected bridge

The bridge has five versioned POST endpoints:

- `/v1/sessions` opens an authenticated host session.
- `/v1/edits` commits an immutable native edit request and returns its remedy.
- `/v1/receipts/stage` accepts the actual structured MCP response.
- `/v1/receipts/consume` atomically consumes an unchanged retry.
- `/v1/captures` stages a bounded candidate from a trusted host producer.

Requests and responses are limited to 256 KiB. Local mode uses an owner-only
Unix socket or a Windows named pipe, a per-request HMAC proof, and a separate
bearer identity check. Windows credential and state paths use a protected DACL
containing only the current user and SYSTEM; POSIX paths use exact `0700`/`0600`
modes. Loads reject links, Windows reparse ancestry, changed owners, extra ACL
principals, and oversized or non-regular files. The accept loop never performs a blocking authentication
handshake. Four fixed workers and an eight-request queue bound admission; idle
reads and clients have enforced deadlines. Remote mode performs TLS handshakes
inside the same bounded worker pool, requires TLS 1.2 or newer, checks an
explicit Host allowlist, rejects unlisted Origin values, and requires the
native-host protocol context header. JSON with duplicate keys is rejected on
both transports. Transport failures raise the recoverable
`EDIT_BRIDGE_UNAVAILABLE` host error and deny the edit.

Credentials live in an owner-only file read by the MCP process and trusted
adapter. The command line, client settings, and MCP arguments contain only its
path. The Claude and OpenCode installers call
`provision_local_bridge_installation`, reuse the credential bound to the exact
managed-storage authority, and configure the project and bridge runtime paths.
The secret is never copied into prompts, tool arguments, settings, logs, or
environment variables. Manual deployments can configure production with:

```text
DAEM0NMCP_EDIT_BRIDGE_CREDENTIAL_FILE=<owner-only credential file>
DAEM0NMCP_EDIT_BRIDGE_MODE=local
DAEM0NMCP_EDIT_BRIDGE_RUNTIME_DIR=<owner-only runtime directory>
```

For an authenticated remote host, set mode to `remote-https` and also set the
remote host, port, certificate, and private-key file variables defined in
`edit_bridge_transport.py`. Configure the server Host and Origin JSON
allowlists explicitly. A remote client sets
`DAEM0NMCP_EDIT_HOST_WORKSPACE_BINDING_FILE` to the owner-only binding created
by `provision_remote_bridge_installation`. Project configuration contains only
that pointer and the protected credential path; it does not select the bearer
recipient.

The immutable version-2 binding records the canonical, link-free desktop root,
its stable directory identity (POSIX device/inode or Windows volume/file ID),
the server's explicit opaque `ws_...` ID, credential ID, normalized HTTPS
authority, normalized Origin policy, canonical CA path, and SHA-256 digest of
the bounded CA bytes. Every load and client construction rechecks directory
identity and CA content before constructing a bearer-authenticated request.
Legacy URL, CA, or Origin environment values are accepted only when they
normalize to the protected values. Replacing a checkout at the same path,
changing CA bytes, redirecting the endpoint or Origin, changing credentials,
using a symlink or Windows reparse path, or omitting the binding denies the edit
with the recoverable `EDIT_BRIDGE_UNAVAILABLE` result before network access.
Reprovisioning an existing binding with any changed recipient or workspace
conflicts; an administrator must explicitly remove or select a new protected
credential/binding after reviewing the change. Each credential and canonical
root has one deterministic binding path, so mutable project settings cannot
select an alternate recipient for the same bearer and checkout.

The Claude and OpenCode installers accept the remote workspace ID, credential
file, HTTPS URL, CA file, and optional Origin as explicit remote pairing
arguments. OpenCode stores the hook-only paths in
`.opencode/daem0n-host.json`; it does not pass client bridge settings to the
MCP server process. The credential and binding remain outside the project; only
their paths enter model-visible configuration, and the binding contains no
credential secret.
When no credential file is configured, the public MCP surface remains
available but native client approval integration is disabled.

Short-lived hook processes share `EditHostStateStore`, an owner-only SQLite
file beside the credential. Its key hashes the actual native client session,
workspace, and credential. It stores opaque bridge IDs and hashes rather than
native arguments or the raw client session ID. Session creation is serialized
across processes. Pending edits transition atomically to consumed state using
the allowed retry's Claude `tool_use_id` or OpenCode call ID, remain available
to the matching post-edit hook, and are removed only after candidate capture.

## Capture review

Trusted producers stage only bounded proposed records and structured
provenance. Raw prompts, messages, transcripts, response bodies, and public
absolute paths are rejected. An idempotency key is bound to the exact proposal;
a changed replay conflicts. Pending candidates live only in
`memory_capture_candidates`, so ordinary recall, briefing, and rules cannot
observe them.

`memory_capture_list` lists pending candidates within the authenticated
workspace and session. `memory_capture_promote` requires the ordinary exact
`memory_preflight` Counsel capability. Promotion appends the canonical
`memory.created` event and marks the candidate promoted in the same database
transaction. A replay must match the candidate, final record, and promotion
idempotency key exactly.

## Acceptance evidence and open gate

The automated acceptance suite covers local IPC, authenticated HTTPS, exact
response staging, principal and MCP-session pairing, expiry, replay, changed
arguments and preimages, immutable database commitments, candidate exclusion,
atomic promotion, and a real production stdio MCP process sharing the exact
broker and candidate-store instances with its bridge.
It also runs the full native hook path over HTTPS with different desktop and
server roots: deny, trusted actual-MCP-response staging, exact retry, edit, and
capture all use the explicit server workspace binding.
Security regressions replace the checkout at the identical path and substitute
the endpoint, CA, and Origin in mutable client settings. Each case fails before
the HTTP client is called. A separate regression changes CA bytes after config
load and confirms client construction revalidates the protected digest.

Released local clients were also exercised with real paired credentials.
Claude Code `2.1.274` completed deny, exact edit preflight, unchanged retry,
edit, capture, exact reviewed promotion, and ordinary recall in
`.tmp/p6-live-claude-final-a0386712/live-full.log`. The candidate
`cap_5ed405e0c5c758eb0452a64767b4a26f4d78f0c85b9ef1af4f83cbf61d344bee`
promoted to canonical record
`mem_8602185df442f4806fedff67395b80273f05910c557d6fe502384a573dc52afb`,
and recall returned the exact reviewed learning. OpenCode `1.18.21` with a GPT
model completed the same path through its native `apply_patch` tool in
`.tmp/p6-live-gpt-fab4c478/live-gpt-fixed.log` and
`.tmp/p6-live-gpt-fab4c478/live-gpt-promote.log`. External remote HTTPS client
execution remains an open staging gate because no external authenticated host
and certificate were available; the bounded authenticated HTTPS protocol
remains covered by the automated transport tests.
