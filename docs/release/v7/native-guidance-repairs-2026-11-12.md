# Native guidance and typed-tool repairs — 2026-11-12

This renewed repair phase follows the [previous native workflow report](native-repairs-2026-11-12.md).
It targets the remaining OMP workflows, Mac OpenCode V1 removal/restart, and
WSL/Mac OpenCode V2 TypeScript restarts. Earlier attempts remain historical
evidence. No commit, push, purchase, account upgrade, or provider/model change
is part of this phase.

## Diagnoses and production changes

Retained OMP events show that the model issued the premature `session_brief`
calls itself. The Windows mutation attempt also supplied `{}` before the
approval adapter rejected its missing workspace. The wrapper did not remove
the arguments. Installed guidance and reminders now say explicitly that a
full brief is optional in guided mode, task call order takes precedence over
gratuitous rituals, and every scoped call requires the supplied opaque
workspace ID. Strict-mode ceremony remains required. Exact old generated
OMP/Pi reminders can be upgraded; customized files remain protected.

The WSL V2 retry omitted seven redundant default-valued fields from the exact
counsel remedy. Canonical validation produced the same request, but the
unchanged native acceptance check correctly rejected the raw mismatch.
Remedy presentation now omits optional defaults only when canonical JSON
validation proves equivalence. Required fields, workspace, capability tokens,
and idempotency keys remain intact. Unknown default factories prevent
compaction before validation, including alias cases. Issuance, expiry,
single-use enforcement, session/principal binding, and admission are unchanged.

The generic gateway permits arbitrary JSON inside `arguments`; the target
model correctly rejects string-valued numeric fields. Actual FastMCP probes
confirmed that directly listed tools expose integer constraints, while the
core gateway cannot express every target's fields. Inspection of the pinned
OpenCode paths did not find a numeric-to-string rewrite. Historical provider
generation causality remains unproven.

Installation now supports explicit `--tool-surface core|full`. Fresh omission
still selects core; omission on an existing installation preserves its
configuration. Explicit selection changes the owned tool-surface setting,
preserving transport, authentication, covenant mode, automation, bridge
credentials, comments, and unrelated configuration. The report exposes the
configured selection. The full profile uses existing typed tools and retains
the same server validation and authorization. A pass on full must not be
reported as a pass on the default core profile.

## Frozen implementation and unchanged acceptance

Freeze 10 is SHA-256
`5de43b4a7f3eb3ceff40fe254b10a0d5200fed63d78fd855aa0436731e5e08e7`.
Its isolated build contains 229 Python modules and 246 package files, with
every member attested against the selected source. The checkout revision is
`4a96e8a8056c1b1d5cd1ca0f63c9f95f7e6929b5` plus recorded uncommitted changes.
[Manifest](../../../.test_tmp/native-followup-20261112/backend/freeze-10/manifest.json)
and [wheel attestation](../../../.test_tmp/native-followup-20261112/backend/freeze-10/wheel-attestation.json).

The five acceptance scenario/driver scripts are bound to their pre-repair
hashes in the [contract baseline](../../../.test_tmp/native-followup-20261112/repair-10/baseline-contracts.json).
Native retries retain strict numeric validation, exact observed remedy replay,
first-recall automatic briefing, stage-specific allowed operations, persisted
index queries without restart indexing, and stale-result removal. The rebind
helper only adds the exact reviewed freeze-10 interpreter/wheel pair.
It also recognizes the exact root-owned prior freeze-09 interpreter when
retargeting Mac V1; foreign-root lookalikes remain rejected.

The first Windows OMP retry then demonstrated a native startup race. The
reviewed RPC driver now waits, within the original stage deadline and at most
120 seconds, for a fresh matching `get_state` reply containing the nine actual
Daem0n routes. It sends no model prompt or MCP operation during that wait.
Errors, process exits, malformed replies, and unexpected approvals fail closed.
Request IDs prevent stale replies from satisfying readiness. This is a host
orchestration correction, not an upstream OMP client patch.

This changes one of the five checkpointed script hashes. The
[startup diff audit](../../../.test_tmp/native-followup-20261112/repair-10/windows/driver-startup-diff-audit.json)
shows that only `run_stream` changed among existing functions; removing its
startup-only nodes restores the exact old function AST. All prompt, approval,
receipt validation, and stage-policy functions remain unchanged. POSIX runs
used the original driver; the Windows readiness retry uses the new recorded
driver. [Reviewed contract verification](../../../.test_tmp/native-followup-20261112/repair-10/readiness-contract-verification.json).

## Verification

- Guidance and installer compatibility selection: 264 passed, 14 skipped.
  Four new fixture failures caused by Windows newline conversion were corrected
  in the test fixture; the four affected checks then passed. The exact-byte
  production ownership guard was retained.
  [Combined run](../../../.test_tmp/native-followup-20261112/repair-10/omp-guidance/green.log)
  and [affected fixture rerun](../../../.test_tmp/native-followup-20261112/repair-10/omp-guidance/legacy-fixture-corrected.log).
- New tool-surface selection cases: nine passed after their red baseline;
  an additional enforcement-bridge/comment preservation case passed.
- Compact-remedy compatibility selection: 102 tests and 107 subtests passed.
  Independent review identified an alias/default-factory edge and a missing
  protected token field. Both were reproduced, fixed, and the three affected
  checks passed. The reviewer cleared the final changes.
  [Compatibility run](../../../.test_tmp/native-followup-20261112/repair-06/compact-remedy-green.log)
  and [edge regressions](../../../.test_tmp/native-followup-20261112/repair-06/compact-remedy-edge-green.log).
- Rebind selection: three passed. Repository Ruff lint and format,
  Linux-targeted v7 mypy, and release inventory passed on the frozen source.
  [Static results](../../../.test_tmp/native-followup-20261112/repair-10/static-results.json).
  The [command receipt](../../../.test_tmp/native-followup-20261112/repair-10/static-commands.json)
  records the executed argv from coordinator history, including `--platform linux`.
- OMP readiness: 15 focused cases passed across the initial run and affected
  fixes, including actual retained pre-discovery/catalog state replay, stale
  IDs, malformed/error responses, timeout, and no prompt before readiness.
  Independent review cleared the barrier. The prior-interpreter allowlist
  also passed two focused root-owned/foreign-root cases. These later harness
  changes received scoped Ruff checks; production still matches freeze 10.

These targeted checks supplement the earlier full-suite run and its separately
documented affected-check repairs; they are not a second complete suite run or
a hosted CI result.

## Native outcomes

The [combined primary matrix](../../../.test_tmp/native-followup-20261112/repair-10/combined-primary-matrix.json)
now contains **29 passed, one failed, and six blocked** source/client/platform
rows. It preserves historical passes, versions, and backend hashes; it is not
a claim that all 36 workflows were rerun on freeze 10. Seventy-five selected
evidence-file hashes were verified. OpenCode V2 remains a separate matrix.

- OMP: all four Mac/Ubuntu WSL source-project restart checks passed on their
  first freeze-10 attempts, with core/reminder profiles. These include first
  recall automatic briefing and persisted code queries without indexing.
- Mac V1: full/enforce removal and restart passed. The actual setup index
  contained 270 source files at generation 6; removal restored the original
  269 files at generation 7. The separate setup turn made an unplanned health
  call, so it remains strictly failed. Its observed index supplies the removal
  precondition without converting the failed setup into a pass. The combined
  Mac V1 row remains conservatively failed for that qualification.
- Windows OMP: the first mutation attempt failed before any MCP receipt.
  Its native log says the turn started before MCP discovery finished; native
  catalog registration followed later. A separate zero-model stdio probe
  successfully initialized the same backend and listed its nine core tools.
  The reviewed native-RPC readiness barrier then allowed mutation, removal,
  and restart to pass with unchanged acceptance. Mutation indexed 270 files
  at generation 6; removal restored 269 files at generation 7. Restart recalled
  first and reused the original record/event, without indexing. All three
  selected attempts had no denied or unplanned calls. The initial failure is
  retained, not overwritten. [Windows report](windows-omp-repair-2026-11-12.md).
- WSL V2: the core-profile restart now passed exact counsel replay, original
  record/event replay, and automatic briefing, but later sent a string-valued
  search limit. The strict failure is retained. The subsequent full-profile
  run passed all 16 backend acceptance checks, as did Mac V2's full-profile
  run. Raw events confirm direct typed search/impact calls and integer numeric
  arguments, with the same prompts, accounts, models, and validators.
  However, both also attempted a native resource-discovery helper that the
  existing host permission denied. Their backend passes do not establish a
  clean native-host workflow; that separate failed qualification remains
  explicit. The pinned host supports `server: "daem0nmcp"`, but the actual
  requests used `{}`, which enumerates all configured servers, including a
  preserved disabled test entry. The existing permission correctly restricts
  discovery to Daem0n. That resource grant stayed Daem0n-only, and no
  preservation sentinel was removed. The full profiles separately added
  exact grants for `daem0nmcp_code_search` and `daem0nmcp_code_impact_analyze`,
  the typed equivalents of the already-authorized source reads; existing
  memory permissions were preserved. Backend repair success is therefore separate
  from the still-failed host request-scope qualification.
  [V2 report](opencode-v2-repair-10-2026-11-12.md) and
  [separate matrix](../../../.test_tmp/native-followup-20261112/repair-10/v2/final-status-matrix.json).
  That matrix has 18 selected backend-check passes, but its conservative
  overall selection remains 16 historical passes and two current host
  failures. Historical host behavior was not re-audited in this phase; it is
  not a claim of 18 clean native workflows.

The [POSIX report](posix-repair-10-2026-11-12.md) retains all seven primary
attempts, including the failed Mac V1 setup, and 227 verified public evidence
files. Sources were restored and exact owned process groups closed. Windows
retains its initial failed attempt and three passing replacements. All external
gaps below remain unchanged.

## Files changed in this phase

- `daem0nmcp/api/v7/remedies.py`, `pinned.py`, and `application.py`: compact exact
  remedies, without changing canonical capability admission.
- `daem0nmcp/client_install.py`, `cli.py`, `native_reminders.py`, and
  `api/v7/tools.py`: explicit tool-surface selection and guided-mode guidance.
- `scripts/native_acceptance_drivers.py` and `native_acceptance_rebind.py`:
  bounded native RPC readiness and exact frozen-runtime retargeting.
- `tests/api_v7/test_compact_counsel_remedies.py`,
  `tests/test_client_tool_surface.py`, `test_guided_native_instructions.py`,
  `test_native_omp_readiness.py`, and `test_native_acceptance_rebind.py`:
  focused regressions and preservation checks.
- `docs/native-client-automation.md` and the linked release reports: supported
  profile behavior, evidence, and remaining limitations.

All pre-existing uncommitted work remains intact. No commit or push was made.

## External gaps retained

Claude native workflows remain blocked by the observed account quota.
MCPJam rendering remains blocked without an existing account. E2B live checks
remain blocked without usable existing credentials. Hosted embeddings remain
unsupported because the provider adapter is absent; token counting is a
separate capability. The earlier intermittent Windows rename failure remains
unexplained despite passing reruns. None of these is converted to a pass by
this repair phase.
