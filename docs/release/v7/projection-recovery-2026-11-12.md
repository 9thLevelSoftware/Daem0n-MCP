# Codex and agy projection recovery

Date: 2026-11-12. This follows the frozen five-client Windows acceptance.
The original evidence remains historical; the recovery receipts below are new.

## Diagnosis

The offline verifier's aggregate notices described derived-state lag after the
last `memory.outcome_recorded` event, not an authority or SQLite integrity error.

| Fixture | Before | Durable work | After recovery |
| --- | --- | --- | --- |
| Codex | Two latest local projections stale: outcome and procedure | Both queued with zero attempts; dense had a running job with an expired lease | Zero local rebuild notices; dense ready with two actual candidates |
| agy | Five latest local projections stale: lexical, graph, temporal, procedure and outcome | All six retrieval jobs queued with zero attempts and no recorded error | Zero local rebuild notices; dense ready with two actual candidates |

All affected jobs referenced the final outcome event. Their availability was
five seconds after that event, matching the event store's coalescing grace.
Agy's native driver waited for process exit before writing its restart receipt;
that receipt's retained file timestamp is `17:47:29.4628758 UTC`, 114 ms before
the jobs' `17:47:29.577236 UTC` availability. This establishes that its host
finished before the delayed work was due. Codex's retained state establishes an
interrupted dense worker and unfinished optional jobs. There is no retained
server shutdown trace establishing the exact interruption mechanism, so that
mechanism is not claimed.

Neither database contained a dead-lettered projection job. The separately
documented lexical-only worker defect is therefore not an explanation for these
notices. The staged and current production lifecycle code is identical; the
retrieval runtime/jobs delta is the prior lexical-only claim filter. The full
scheduled worker recovered independent copies using **both** runtime snapshots.
No additional scheduler behavior change was justified.

Shutdown intentionally quiesces admitted work rather than waiting for every
future coalescing deadline. Durable pending work resumes at startup; an expired
lease is reclaimed by the normal runner. Acceptance that requires a fully
settled projection inventory must observe health after its final write while
the host remains running, or explicitly record pending work and its later
restart convergence.

## Recovery and diagnostics

Before any recovery, SQLite's backup API produced consistent copies, and closed
Qdrant derived files, activation pointers, inventories, verification reports and
SHA-256 manifests were retained. Current-runtime copies converged first, then
the original **disposable acceptance** databases were recovered with
`schedule_projection_job_drain`, not SQL marker/job rewrites or offline authority
repair. The scheduled function selects the full optional runtime internally.
Both original stores remain `verified`, with
unchanged authority roots and zero local rebuild notices. Actual dense searches
returned `ready` and two candidates each. Staged-runtime comparison used a
second pair of copies and did not replace any original runtime or registration.

`verify-v7` now adds `checks.projection_manifests.projections`. Existing
aggregate fields retain their meaning. Each retrieval projection reports its
latest manifest, source count/cursor, sanitized reason codes, and durable queue
state: absent, queued, delayed, running, expired lease, succeeded or dead letter.
Unavailable-builder errors are identified from the worker's recorded error;
offline verification does not guess the next process's installed capabilities.
Code projection freshness remains independent of memory-event counts. Missing
optional projections are reported as not built and do not change authority
validity. Arbitrary marker/error text is not echoed as a diagnostic identifier.

The aggregate `rebuild_required` also counts historical/superseded manifests and
other projection kinds. It need not become zero when the latest five local
retrieval projections and dense are current; `local_rebuild_required` is the
bounded local freshness count used here.

## Verification and evidence

Focused verification covered offline authority/projection checks, durable job
claims and leases, and retrieval scheduling. Two new tests use real SQLite jobs
to prove delayed work survives shutdown and startup resumes it without another
write, and that startup reclaims an expired lease. Additional diagnostics tests
cover delayed/queued/running/expired/dead-letter states, unavailable builders,
code freshness, and suppression of arbitrary marker/error text.

- Initial focused run: 106 passed, 19 passing subtests, five new-fixture failures.
- Targeted corrections: all five failed cases passed. The fixtures now supply
  required lease owner/token fields, canonical error JSON, and explicitly close
  SQLite connections before Windows temporary-directory cleanup.
- Two subsequent code/sanitization checks passed, including the strengthened
  existing code-generation check.
- Scoped Ruff lint, format checks and whitespace checks passed.

Private evidence is under
`.test_tmp/native-followup-20261112/projections/`: `backups/`,
`*-before-{inventory,verify}.json`, `*-backup-manifest.json`,
`*-{copy,copy-staged,original}-recovery.json`, `*-original-dense.json`,
`runtime-comparison.json`, and focused test logs/XML. These synthetic data and
derived indexes remain ignored. This is backend recovery and regression
evidence, not a repeated native model workflow, hosted CI, or production gate.

## Separate fresh-profile startup notice

A later Claude automation fixture (`freeze01-core-4`) returned active=2 and
stale=4 in its first brief at 20:36:29.376 UTC. A consistent, read-only snapshot
under `projections/fresh-claude-startup/` explains that distinct notice. Procedure,
outcome and graph completed at 20:36:29.933, 20:36:30.968 and 20:36:32.348. Those
three projections were ordinary startup work still in progress at brief time.
Dense generation 1 instead has one dead-lettered job with the recorded code
`PROJECTION_BUILDER_UNAVAILABLE` at 20:36:27.889. The latest five local retrieval
projections are current and the authority ledgers are empty and valid.

The automation driver's owning source explicitly disabled `LOCAL_ENABLED` and
`MODELS_LOCAL_ENABLED` in its child environment; the automation owner confirmed
that environment was forwarded to the native SDK process. This explains the
intended capability profile. That attempt did not retain a complete child
environment or a native health response, so the unavailable capability is
independently proven by the durable worker error, not by a reconstructed live
capability inventory. No job, marker, or original database was modified for this
diagnostic. This unavailable dense builder is separate from the recovered old
Codex/agy delayed work and interrupted lease.
