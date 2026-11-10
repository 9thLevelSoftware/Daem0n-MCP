# P6 native edit and capture independent review

Status: **NEEDS WORK** for the P6 release gate. The exact local approval boundary and reviewed-capture implementation are materially sound, but required remote client scenarios remain uncertified and the required OpenCode V2 workflow is unavailable.

This review is bounded to the P6 implementation and evidence. It does not accept the full v7 release.

## Findings

### HIGH — required remote and OpenCode V2 workflows are not implemented or certified

**Affected components:** `docs/release/v7/acceptance-scenarios.json:55-64`, `docs/release/v7/p6-edit-capture.md:106-127`, `docs/release/v7/p6-opencode-compatibility.md`, and `daem0nmcp/opencode_install.py:21-29`.

The acceptance contract requires full authenticated remote workflows for Claude and OpenCode V1, plus a separately passing local and remote OpenCode V2 adapter. The supplied evidence covers real Claude 2.1.274 and OpenCode 1.18.21 V1 local runs. The report explicitly says external remote execution is still open, and the installer explicitly rejects `interface="v2"` because the released V2 package lacks execution hooks.

This is correctly disclosed and is preferable to an unsafe compatibility claim, but it means `scenario.P6.claude-remote`, the remote half of `scenario.P6.opencode-v1`, and `scenario.P6.opencode-v2` do not meet their stated acceptance criteria. The automated HTTPS tests validate the protocol implementation; they do not substitute for client-to-remote-host interoperability and credential-handling evidence.

**Remediation:** exercise Claude and OpenCode V1 end to end against an authenticated external HTTPS bridge with production certificate and credential handling. For V2, implement an adapter only when the exact supported OpenCode V2 contract exposes a trustworthy before/after execution boundary and raw MCP result, then certify the required local and remote workflows. Until then, keep V2 rejected and P6 pending rather than relabeling V1 as V2 support.

### Resolved during review — Claude `NotebookEdit` capture matcher

**Affected component:** `daem0nmcp/claude_hooks/install.py:62-100`.

The initial review found that the installed `PreToolUse` matcher sent `Edit|Write|NotebookEdit` through the fail-closed approval hook while the successful-edit `PostToolUse` matcher contained only `Edit|Write`. That would have allowed an approved `NotebookEdit` to execute without staging its review candidate.

Direct probe:

```text
pre  ['Edit|Write|NotebookEdit', 'Bash']
post ['mcp__.*__edit_preflight', 'Edit|Write|NotebookEdit']
```

The implementation now uses the identical configured edit matcher for pre and post hooks. Installer coverage asserts that equality and native-edit regressions exercise NotebookEdit preimage and post-path normalization. Independent focused rerun: **32 passed**. No material finding remains for this item.

## Security and correctness assessment

The local approval mechanism otherwise satisfies the material security properties inspected:

- Native arguments are canonicalized and bounded, referenced paths are normalized inside the workspace, apply-patch move sources and destinations are represented, and current file preimages are hashed before a pending edit is created.
- Pending rows persist commitments rather than raw arguments. Receipt issuance uses an immediate transaction and binds the workspace, host session, principal hash, first MCP transport-session hash, edit hash, description hash, issue time, and 120-second expiry.
- Staging accepts a complete successful v7 structured response, checks its workspace and exact response shape, then verifies the signed receipt and stored token hash. Assistant-rendered text alone is rejected.
- Consumption revalidates the live host credential/session, expiry, staged status, tool, argument hash, edit hash, and exact canonical preimages before a conditional one-use update. Changed and replayed retries fail closed.
- Local IPC uses the platform-authenticated connection plus a request HMAC and bearer identity, strict duplicate-key JSON, bounded bodies, a fixed worker pool/queue, and deadlines. HTTPS uses TLS 1.2+, bounded admission, exact single Host/context/authorization headers, Origin allowlisting, strict bodies, and the same authorization protocol on every route.
- Credential/state files use the protected-file helper; configured client settings contain credential paths rather than secrets. Searches across the supplied live logs found no bridge secret, bearer, authorization, credential ID, or bridge-environment marker.
- Pending candidates remain in `memory_capture_candidates`; ordinary recall reads canonical records. Promotion appends the canonical event and changes candidate status in one transaction, with exact idempotent replay checks.

The strict OpenCode `apply_patch` parser covers the native grammar exercised by the pinned 1.18.21 client, including add, update, delete, move, end-of-file marker, duplicate rejection, required source/destination state, Windows path normalization, and workspace escape rejection. Independent inspection of the pinned upstream V1 source also confirmed that its before hook receives the execution arguments and its after hook receives the raw MCP result before rendering.

## Verification

- Focused P6 suite: `106 passed, 2 skipped`; the skips are platform-specific protected-file cases.
- The real production stdio process test initially lost its subprocess during concurrent shared-worktree edits. A separate production startup probe initialized successfully, and an immediate isolated rerun of `tests/api_v7/test_process_edit_capture.py` passed (`1 passed`). I do not treat the transient first run as a P6 defect.
- Supplied real-client evidence was inspected at `.tmp/p6-live-claude-final-a0386712/live-full.log`, `.tmp/p6-live-gpt-fab4c478/live-gpt-fixed.log`, and `.tmp/p6-live-gpt-fab4c478/live-gpt-promote.log`. It shows the local denied edit, exact preflight, unchanged successful retry, candidate review/promotion, and canonical recall for both supported local clients.
