# P6 remote workspace mapping independent re-review

Date: 2026-09-17

Status: **ACCEPTED (bounded)**. The repaired version-2 remote binding closes both
previous HIGH findings. The same-path replacement and recipient-redirection
probes now fail closed before a bearer-authenticated HTTP request. No remaining
material finding was identified in this repaired slice.

This accepts only the P6 remote workspace binding repair. It does not accept the
whole P6 package or the v7 release. External authenticated remote-client
certification is still unavailable, and OpenCode V2 still lacks the required
native execution hooks.

## Prior HIGH findings

### Closed — same-path checkout replacement

`daem0nmcp/edit_host.py:82-211`, `290-424`, and `758-800` bind the canonical
desktop root to a stable directory identity and recheck that identity when the
binding loads, when a hook resolves its server workspace, and immediately before
constructing the HTTP client. POSIX uses device and inode identity. Windows opens
the directory with `FILE_FLAG_OPEN_REPARSE_POINT`, rejects reparse points, and
records the volume serial plus the 128-bit file ID.

The independent replacement probe renamed the paired checkout, created a new
directory at the identical path, and attempted workspace resolution. It now
raises `ValueError: remote workspace binding does not match project root`.
Loading a fresh hook configuration for the replacement also fails. Restoring the
original directory restores the original identity and mapping. A Windows
junction alias raises `ProtectedPathError` before identity comparison.

### Closed — mutable project configuration redirecting the bearer

`daem0nmcp/edit_host.py:214-278`, `290-424`, `473-550`, and `688-800` bind the
credential ID, opaque server workspace, normalized HTTPS authority, normalized
Origin policy, canonical CA path, and SHA-256 of the bounded CA bytes in the
owner-only binding. The binding must be in the credential directory at the
deterministic root-derived name. Reprovisioning with a changed workspace or
recipient conflicts.

Runtime configuration may omit the legacy URL, CA, and Origin values and use the
protected values. If it supplies them, they must normalize to the same authority,
Origin, and canonical CA path, and the CA bytes must still match the protected
digest. The client SSL context is created from those already verified bytes via
`cadata`; it does not reopen the CA path during TLS construction.

During this re-review, an independent probe found that an alternate regular CA
file with byte-identical contents was initially accepted because the runtime
check compared only the digest. The coordinator repaired the check to require
both canonical path and digest and added a pre-network regression. The probe was
rerun and now raises `ValueError: remote edit bridge CA conflicts with binding`.

Claude and OpenCode remote project files contain credential and binding pointers
rather than the remote recipient values or secret. Changing the URL or Origin in
mutable hook configuration raises a binding conflict. Changing the CA path,
including to an equal-bytes copy, raises a binding conflict. The focused tests
also prove these failures occur before `RemoteBridgeHTTPSClient.call`.

## Security and regression review

- The Windows probe observed `windows-volume-file-id` with a 16-hex-digit volume
  value and 32-hex-digit file ID. Unsupported identity acquisition fails rather
  than falling back to path-only matching.
- Binding and credential files use the shared owner-only verifier. The Windows
  DACL regression rejects an added Users principal. Duplicate-key JSON, linked
  ancestry, non-regular files, oversized files, noncanonical binding paths, and
  changed binding or CA content fail closed.
- HTTPS URL and Origin normalization require a host-only `https` origin, reject
  credentials, paths, queries, fragments, and port zero, normalize IDNs and host
  case, and remove the default port. The independent probe normalized
  `HTTPS://Server.Example:443/` to `https://server.example`.
- Hook failures remain recoverable and fail closed as `EDIT_BRIDGE_UNAVAILABLE`.
  The actual HTTPS integration uses different desktop and server roots and
  completes deny, trusted structured-response staging, exact retry, edit, and
  candidate capture against a real TLS server.
- The repaired checks add bounded repeated filesystem reads between hook stages;
  no persistent directory handle is retained. Each short-lived hook revalidates
  the identity before its HTTP client is built. An arbitrary same-user process
  already has the credential owner's authority; no separate lower-privilege race
  capable of redirecting the protected bearer was found.

## Independent checks

- Python 3.12 focused remote mapping, hook, installer, transport, and real
  production-process suite: **67 passed in 22.86 seconds**.
- Core Python 3.10 rerun of the same focused suite: **67 passed in 23.73
  seconds**.
- A first cross-interpreter run executed both suites concurrently and produced
  two Windows named-pipe `FILE_FLAG_FIRST_PIPE_INSTANCE` collisions. The isolated
  Python 3.10 rerun passed; this was test-process contention, not a product
  assertion failure.
- Scoped Ruff over the reviewed production and test files: **passed**.
- Scoped mypy with imported modules skipped over `edit_host.py`, the Claude
  installer, and the OpenCode installer: **passed, no issues in 3 source files**.
- Protected-file checks on Windows: **3 passed, 2 skipped**. The skips are the
  unavailable unprivileged symlink creation case and the POSIX-only permission
  case. An independent Windows junction probe covered reparse rejection.
- The packaged and repository OpenCode TypeScript plugin copies remain
  byte-identical through the focused installer regression suite.

## Remaining external gates

- No external authenticated remote host and production client certificate were
  available, so released Claude and OpenCode V1 clients were not exercised
  against a genuinely separate remote deployment in this review.
- OpenCode V2 remains explicitly unsupported because its released interface does
  not expose the required trustworthy before/after execution hooks and raw MCP
  result. It cannot satisfy the required local or remote workflow at present.
- These gates remain P6/release-level work. This bounded acceptance makes no
  release claim.
