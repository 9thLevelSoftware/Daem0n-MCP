# P7 production dreaming implementation contract

This is an implementation handoff, not acceptance evidence.

## Runtime ownership

Add a v7 coordinator independent of legacy mutable managers. Production owns
one coordinator and its bounded worker pool. Register startup and close through
`_runtime_lifespan`; register a synchronous activity callback through
`V7InvocationMiddleware`. Activity is associated with the admitted workspace,
never inferred from the process default. No MCP credentials or user capabilities
are retained by idle work. The configured server grants a narrow internal policy
to analyze its registered workspaces; client access remains governed separately.

Use per-workspace idle clocks, at most one run per workspace, and a global bound
on concurrently executing analyses. Foreground activity signals cooperative
yield. Stop waits for owned work to quiesce before storage/retrieval close. Repeated
start is idempotent. Missing optional graph support produces a stable disabled
strategy status rather than repeated provider retries.

## Strategies and write policy

- Failed-decision review: canonical failed decisions older than the configured
  age; bounded related evidence, excluding self; classify successful alternatives,
  insufficient evidence, or confirmed failure. Persist a review candidate with
  source/evidence identities and hashes.
- Pending-outcome analysis: canonical pending decisions, existing age/cooldown and
  evidence threshold; distinguish insufficient, mixed, unanimous positive and
  unanimous negative evidence. Preserve `dream_pending_dry_run=True`. The explicit
  false setting authorizes only the supported unambiguous outcome action, recorded
  as an audited canonical event after source/evidence revalidation. Mixed evidence
  always requires review. Candidate wording must distinguish a proposed outcome
  from an outcome that actually committed.
- Connection discovery: bounded recent canonical records plus valid active entity
  memberships; detect currently unlinked pairs meeting the configured shared
  entity threshold. Persist proposed connections as reviewable candidates by
  default, with source record identities and shared entities. Do not silently
  append inferred relationships under the default policy.
- Community refresh: use the v7 generation builder and canonical source snapshot,
  existing staleness control, cancellation, and atomic activation. This is derived
  state. Never call legacy manager writes or pretend a missing graph provider ran.

Use `CaptureCandidateStore` for default inferred-memory proposals. The source kind
is `dreaming`; records use the canonical six record types. Bound content and
provenance; retain no complete transcripts. Stable idempotency derives from the
strategy, workspace, source versions and relevant evidence, excluding wall-clock
session IDs. Repeated analysis and process restart must not duplicate proposals or
semantic writes. Existing explicit settings remain effective. Additional automatic
actions, if supported, require a distinct explicit audited policy.

## Persistence and diagnostics

Persist per-strategy cooldown/progress/last-success/stable failure information in
workspace v7 storage using existing bounded metadata mechanisms where adequate.
Do not build a general workflow engine. Recheck the active storage generation at
write time. Interrupted work resumes from authoritative records without assuming
an in-memory completion flag committed. If a schema change is necessary, reserve
the next migration with the coordinator first (current schema is 27).

Expose bounded workspace-filtered health diagnostics: enabled/disabled/running,
last success, stable errors, pending work and yielded state. Global health must not
disclose another workspace's activity. Include component suppression behavior.

## Required evidence

Component tests cover all four strategies, dry-run and explicit action behavior,
event replay, candidate exclusion from recall, exact review promotion, cooldown,
deterministic retries, stale generations, foreground yielding, no overlap,
startup/restart/shutdown and workspace isolation. Real stdio and HTTP MCP tests
launch ordinary production startup with short configured idle intervals, observe
activity and resulting health/candidates/projections, restart and prove no duplicate
semantic writes. Use enabled real graph dependencies for community refresh and
verify missing-profile diagnostics separately. An injected strategy test alone
does not certify production integration.
