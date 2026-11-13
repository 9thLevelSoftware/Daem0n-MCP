# Native integration and acceptance — 2026-11-12

This is the integration follow-up to the historical
[multi-client acceptance](multi-cli-acceptance-2026-11-12.md). Implementation and
native acceptance are separate gates. The integration changes and bounded
acceptance work are recorded here; overall qualification remains incomplete
because of the failed workflows, unresolved repository test and external
blockers below. Component passes are not promoted to complete workflow passes.

The subsequent [native repair report](native-repairs-2026-11-12.md) records the
schema/indexing/launcher corrections and selected freeze-08/09 reruns. This
report's original results remain historical; the follow-up links the combined
matrix and the failures that remain.

## Implemented behavior

- `verify-v7` retains its aggregate fields and adds per-projection freshness,
  source cursors, queue state, expired leases and sanitized worker errors.
- Installation accepts explicit `--automation off|remind|enforce`. Omission
  preserves the prior installation behavior and existing automation. Unsupported
  enforcement requests fail with an actionable capability error.
- Claude and OpenCode adapters use the shared edit gate. Approval is bound to
  workspace, host/MCP sessions, the exact native call and file preimages. Native
  success evidence is required for capture; failed or denied calls do not
  produce successful captures. Coverage is limited to declared edit tools.
- Codex, agy, OMP and Pi install reminder adapters. These do not claim edit
  enforcement. The agy matcher includes the observed internal `write_file` and
  `edit_file` names as well as its documented model-facing names.
- OpenCode V2 has separate version-specific configuration and a plugin for its
  actual execution hooks. V1 remains separate.
- Real legion source exposed missing TypeScript call relationships. Ordinary
  reindexing now adds conservative same-file TS/TSX call/reference edges and
  preserves unchanged generations. Cross-file TS call/reference resolution is
  intentionally unsupported until export visibility and aliases can be verified.

See [automation usage](../../native-automation.md) and
[installation details](../../native-client-automation.md) for supported versions,
covered tools and mode selection.

The production changes are in `verification_v7.py`, `native_edit_gate.py`,
`native_reminders.py`, `client_install.py`, `cli.py`, `edit_host.py`,
`edit_bridge_transport.py`, the Claude hook modules, OpenCode installers/assets,
and `api/v7/discovery_operations.py` / `typescript_relationships.py`. The V1
repository plugin mirrors its packaged asset. Targeted tests cover projection
recovery/diagnostics, installer preservation, exact edit bindings, failure
settlement, remote entry points, socket paths, TypeScript scopes and acceptance
validators. The new `scripts/native_*` helpers retain reproducible fixture,
driver, scenario, platform and evidence-generation logic. The wheel input
manifests below identify the frozen package files by content hash.

## Priority acceptance

| Requirement | Outcome | Evidence |
| --- | --- | --- |
| Explain old Codex/agy rebuild notices | **Passed** | [Projection diagnosis and recovery](projection-recovery-2026-11-12.md): durable delayed jobs, interrupted dense lease, no old dead letters; both historical and current runtimes recover independent copies. The exact Codex interruption mechanism was not retained. |
| Recover disposable acceptance databases | **Passed** | Same report: consistent backups, supported scheduler drain, unchanged authority roots, zero latest local rebuild notices and actual dense readiness with two candidates in each store. |
| Prove agy automatic covenant loading | **Passed** | [Positive/negative first-response probe](automatic-loading-2026-11-12.md): fresh projects, unique sentinel, no filesystem tools, no prompt naming the instruction file or supplying its content. |
| Observe native edit mediation and reminders | **Passed for the bounded cases listed** | [Native automation evidence](native-automation-acceptance-2026-11-12.md): Windows adapter cases and actual V1 edit/fail-closed cycles on Mac/WSL. V2 has separate results; model-gated tools and quota-blocked Claude POSIX edits are not inferred from adapter tests. |

The separate fresh Claude startup notice is also explained in the projection
report: local builders converged after the first brief; dense recorded an
unavailable-builder error. It is not the old Codex/agy backlog, and it is not
claimed to have dense readiness.

## Windows native workflow matrix

The [Windows report](windows-followup-2026-11-12.md) links all attempts, strict
stage outcomes and individual capability receipts. Each row below covers both
immutable source projects; successful edit-automation probes remain a separate
capability from these source workflows.

| Client | Daem0n source workflow | legion source workflow | Declared automation |
| --- | --- | --- | --- |
| Claude Code 2.1.295 | **Blocked** after functional initial/mutation evidence and strict failed turns | **Blocked** by native weekly quota | Enforce: bounded native Windows tool/failure probes passed before quota. |
| Codex 0.167.2 | **Passed** | **Passed** | Remind: actual Windows callbacks passed; enforcement unsupported. |
| agy 1.5.7 | **Passed**, with one explicitly recovered exact timeout | **Passed** | Remind: actual Windows callbacks and corrected edit aliases passed; enforcement unsupported. |
| OpenCode V1 1.18.21 | **Failed** strict removal turn; later restart passed | **Failed** strict removal turn; later restart passed | Enforce: actual Windows write/edit/failure probes passed; model-gated patch remains blocked. |
| OMP 18.16.0 | **Failed** strict workflow despite component and bounded prompt-assisted evidence | **Failed** strict workflow despite component and bounded prompt-assisted evidence | Remind: actual Windows turn callback passed; enforcement unsupported. |
| Pi 1.0.0 | **Failed** removal/restart constraints | **Failed** strict turns; corrected index and restart components passed | Remind: actual Windows turn callback passed; enforcement unsupported. |

Versions in the first column identify the source-workflow runs. The independent
Windows reminder probes used Codex 0.167.0 and agy 1.4.7 for lifecycle evidence;
the corrected agy edit matcher has its own freeze-03 receipt. Exact versions and
builds for each automation attempt are retained in the linked automation report.

The failed rows preserve invalid model arguments, denied unplanned calls and
unexpected rebuilds. Passing source-operation assertions are reported without
turning these into seamless workflow passes. Claude's quota was observed in
native responses on Windows, Ubuntu WSL and the Mac after authentication and
MCP readiness; no account upgrade or quota retry was attempted.

## macOS and Ubuntu WSL native workflows

The [platform report](platform-acceptance-2026-11-12.md) retains the attempts,
per-stage results and preservation exceptions. These runs used native binaries
on the Mac mini and Ubuntu WSL's Linux filesystem, with independent source
fixtures and storage. Each cell lists **Daem0n / legion** workflow outcomes.

| Client | macOS | Ubuntu WSL |
| --- | --- | --- |
| Claude Code 2.1.295 | **Blocked / Blocked**: weekly quota after authentication and MCP readiness | **Blocked / Blocked**: weekly quota |
| Codex 0.167.0 | **Passed / Passed** | **Passed / Passed** |
| agy 1.5.7 | **Passed / Passed** | **Passed / Passed** |
| OpenCode V1 1.18.21 | **Failed / Failed**: Python operations all covered, but initial invalid calls fail the workflow; TS follow-up had extra errors and blocked dependent stages | **Failed / Passed**: Python operations all covered, but initial invalid calls fail the workflow |
| OMP 18.16.0 | **Failed / Blocked**: source-workflow repair limit reached | **Failed / Blocked**: Python index components passed; later workflow failed and TS was not started |
| Pi 1.0.0 | **Passed / Passed** | **Failed / Passed**: Python mutation had an extra invalid call; removal and restart subsequently passed |

The [structured POSIX matrix](../../../.test_tmp/native-followup-20261112/platforms/final-platform-matrix.json)
contains 12 passed, six failed and six blocked source workflows. Combined with
Windows, the 36 client/platform/source pairs total **16 passed, 12 failed and
eight blocked**. OpenCode V2 is separate. All 24 POSIX source fixtures finish
with their original source hashes restored and added probes absent; stopped
lanes do not gain an inferred successful reindex from that cleanup.

Mac Codex/legion and agy/legion passed **after the targeted TypeScript backend
repair**. Their original initial turns failed only the missing-caller assertion,
with no unexpected MCP errors; the freeze-02 index follow-up resolved that
assertion, and the later stages passed. Already-successful core writes were not
replayed. The original failed summaries remain retained; these are not
first-attempt seamless passes.

All eight independent POSIX reminder probes passed: Codex lifecycle and
`apply_patch` matcher, agy lifecycle and observed edit aliases, and OMP/Pi
`before_agent_start` callbacks. Codex's proposed edit was not approved, and its
target remained absent. These are reminder results, not enforcement claims.
OpenCode V1 separately passed native exact-edit capture and missing-interpreter
denial on both platforms. Claude POSIX edit mediation remains quota-blocked.

Mac and WSL agy also passed fresh positive/negative automatic-loading probes
with zero tools. Pi and OMP each passed native resource discovery, own-workspace
reads, exact warning refresh and foreign-workspace denial on both platforms.
The V2 mode-specific results remain in their separate matrix below.

## Evidence boundaries and provenance

Native fixtures export immutable tracked source from recorded Git revisions:
225 Daem0n Python files and 269 legion TypeScript/TSX files. Each client/project
pair has independent configuration and storage. Credentials, caches, runtime
data and existing client configuration are excluded. Only disposable copies
receive indexing mutations. Source manifests retain individual file hashes and
the original checkout's separate dirty state.

Five immutable wheel builds are retained. Successful unaffected checks are not
rerun solely to replace their provenance. Affected checks use the next build;
earlier failed attempts remain identifiable.

Six early Windows attempts lack a contemporaneous per-attempt backend digest.
The Windows report identifies each one and retains the available installation,
configuration and retarget receipts. Their digest fields remain null rather
than being reconstructed as if recorded at execution. An independent audit
verified all 51 retained Windows attempt-summary hashes and preserved the 12
strict workflow outcomes during this provenance correction.

| Build | SHA-256 | Change boundary |
| --- | --- | --- |
| freeze-01 | `38475f299dc66918a645760dfaea633b7e6883f1dc0901468a39dbc1b6155c65` | Projection diagnostics, automation adapters and separate V2 integration. |
| freeze-02 | `6d9939417043b705631c03342095c5af7c2534c7cf40592b85a0a604b35b64da` | Adds reviewed same-file TypeScript relationships. |
| freeze-03 | `007b32b84f1d53e56e6fa830c399bd262a0edbcad1fcad3c33313d47f8e64cea` | Adds observed agy hook aliases and renames a local TS variable for type checking; other executable behavior is unchanged. |
| freeze-04 | `ab19bd46de7507ed5c3b67df400070f618e0512b17d0ceae73c02c06054768b9` | Adds protected short Unix socket endpoints for long runtime paths and restores exact remote checkout binding across native hook entry points. |
| freeze-05 | `9b6ff0e18753e2075cc14eab2eeaa4dd2435ca8be06daace00906ce183618963` | Adds guarded stale Unix socket recovery, lifetime startup serialization, replacement-aware cleanup and failed-startup cleanup. |

Build manifests and exact package input hashes are under
`.test_tmp/native-followup-20261112/backend/freeze-*/`. The implementation is
uncommitted on base revision `4a96e8a8056c1b1d5cd1ca0f63c9f95f7e6929b5`.
Installed runtimes are separate environments; no running runtime is upgraded
in place. Linux results are explicitly **Ubuntu WSL acceptance**, not hosted CI
or bare-metal Linux qualification.

Preservation exceptions are explicit. An early native Claude status probe
created `/home/dasbl/.claude.json` in WSL where no file had existed; it was
retained, not removed to conceal the change. Subsequent runs used separate
profiles. Mac disk pressure was relieved by removing only three disposable npm
caches under the owned acceptance preparation directories; the four inspected
UV caches were retained. Account profiles, credentials, source snapshots,
databases and prior evidence were preserved. Some native process-group cleanup
calls returned permission errors after their root processes exited; separate
read-only checks of the exact recorded groups found no surviving members.
The original uncertain cleanup results remain unchanged.

Final POSIX exports retain 671 Mac and 630 WSL public evidence files locally,
including the complete agy preload requests and raw streams. The
[retention receipts](../../../.test_tmp/native-followup-20261112/platforms/final-public-retention-receipts.json)
record archive and individual-file verification. Private profiles, authentication
files, bridge credentials and databases are excluded from these public exports;
the original owned fixtures remain retained. The two selected provider
credential-literal checks found no matches in the exported files.

An [independent evidence audit](../../../.test_tmp/native-followup-20261112/final-evidence-audit.json)
verified all 69 selected POSIX stage summaries against the matrix, both complete
snapshot manifests, locally retained agy preload streams, and report links.
It found no blocking evidence inconsistencies. This validates the reporting,
including failed outcomes; it does not clear those failures.

## Independent optional lanes

| Lane | Outcome | Evidence and limitation |
| --- | --- | --- |
| OpenCode V2 | Reported separately | [V2 native acceptance](opencode-v2-2026-11-12.md), including Code Mode and direct-tool exposure. |
| Native MCP resources | **Passed for bounded non-Claude cases; Claude blocked by quota** | [Windows Codex/agy/V1 evidence](windows-followup-2026-11-12.md), [Pi/OMP native evidence](native-resources-2026-11-12.md), the platform report and the V2 report distinguish native discovery, reads, workspace isolation and explicit refresh. Agy's observed headless surface lists static resources and reads parameterized URIs but does not expose template listing. |
| MCP Apps rendering | **Blocked** | [Optional interfaces](optional-interfaces-2026-11-12.md): pinned MCPJam Inspector 3.21.0 requires an account; user has no existing account. No populated dashboard rendering or action pass is claimed. |
| Hosted embeddings | **Unsupported** | No provider adapter; implementation was explicitly outside scope. |
| Advertised token counting | **Passed** | `tiktoken` 0.14.0 fixed-sample encode/decode. This is not an embeddings call. |
| Live E2B | **Blocked** | Existing credentials absent in the inspected environments. No sandbox created; execution, isolation, limits and cleanup remain untested. |
| E2B disabled response | **Passed** | Actual covenant remedy followed by `CAPABILITY_DISABLED` / `E2B_API_KEY_MISSING`; not a live E2B acceptance substitute. |

OpenCode V2 2.2.7 has native evidence on all three operating environments.
Its POSIX matrix contains **46 capability rows: 32 passed, six failed, four
unsupported and four blocked**. These are not 46 complete client workflows:
the selected pairings are Code Mode with Python and direct-tool mode with
TypeScript on each host. Native edit-gate and resource cases passed; malformed
gateway arguments, an unrequested restart rebuild and failed TypeScript query
prerequisites remain visible in the source-workflow results. The model-selected
surface did not expose patch tools, so no native patch pass is claimed.

## Final repository verification

The freeze-03 tree passed Ruff lint and format (519 files), Linux-platform mypy
(49 files), and release inventory (77 tools, 10 resources, 588 atomic
requirements). Its full pytest run collected 4,255 items but was interrupted
when Mac acceptance demonstrated a Unix socket path limit defect. The final
suite follows the reviewed correction; the interrupted run is not a pass.
Freeze-04 passes Ruff lint and format (520 files), Linux-platform mypy (49 files)
and the same release inventory. Its full pytest suite was also interrupted:
native Mac shutdown left a confirmed stale Unix socket, requiring guarded
startup recovery before final qualification. No interrupted run is a pass. Logs are
retained under `.test_tmp/native-followup-20261112/checks-freeze-04/`; earlier
attempts keep their own directories.

Focused socket/transport checks passed 21 tests with two native-POSIX-only skips;
one existing remote-hook test exposed incorrect local-ancestor discovery in a
remote checkout and passed after correction. The strengthened two-case test
then passed direct handling and all three actual packaged hook subprocess
entry points against a real HTTPS bridge, including final capture. Independent
review found no remaining material issues in these bounded fixes. Native
Unix bind/handshake evidence is recorded separately from Windows simulations.

Both native platforms passed all seven new Unix-path regressions. Claude's
actual MCP manager launched freeze-04 using the original too-long runtime path;
an authenticated bridge session returned HTTP-style status 201, with the
short endpoint under an owner-only directory and unchanged credentials. No model
call was needed. The immediate shutdown receipt recorded the socket still
present on both hosts. A later observation found WSL cleanup complete; the Mac
had a socket with no listener and no surviving server. The initial connection
success must not be read as a shutdown-cleanup pass.

Freeze-05 passed all 19 recovery regressions on each native POSIX platform. Its
actual Claude MCP-manager proof completed two start/stop cycles on the same
endpoint with unchanged credentials and an authenticated status-201 bridge
response each time. The Mac's first cycle recovered the untouched freeze-04
stale socket through supported startup. No manual unlink or endpoint rotation
was used. Immediate socket removal after native SDK shutdown is not claimed;
automatic recovery on the next start is verified. Receipts:
[macOS](../../../.test_tmp/native-followup-20261112/platforms/macos/native05-same-endpoint-restart-proof/stdout.log)
and [Ubuntu WSL](../../../.test_tmp/native-followup-20261112/platforms/linux/native05-same-endpoint-restart-proof/stdout.log).

Freeze-05 passes Ruff lint/format (526 files), Linux-platform mypy (49 files),
and release inventory (77 tools, 10 resources, 588 atomic requirements). Its
full pytest run completed with **4,178 passed, 111 skipped, six failed and 2,104
passing subtests** in 33m46s. Logs and the original nonzero result remain under
`.test_tmp/native-followup-20261112/checks-freeze-05/`.

Four failures came from stale acceptance-test fixtures: three configuration
doubles lacked the real configuration's explicit mode, and one native resource
fixture was corrected after the suite had collected it. The affected two
modules subsequently passed all ten tests; the negative workspace-binding test
also now reaches its intended rejection rather than failing on the incomplete
double. The retained `pytest-fixture-followup` log records this bounded rerun.
The dense-store test had completed its functional assertions and failed only
while deleting its temporary directory. Qdrant opens collection SQLite files
before acquiring its folder lock; failed constructors can remain in traceback
cycles. Explicitly closing the owned clients and collecting those cycles before
asserting fixture removal fixed this test-only cleanup: its one affected rerun
passed (`pytest-dense-cleanup-followup`). This does not establish immediate
cleanup of partial dependency objects in production.

**One failure remains unresolved:**
`tests/test_workspace_identity.py::test_a_renamed_folder_keeps_its_id_and_its_root_key`
received Windows `WinError 5` at the directory rename. Review confirmed the seed
helper closes SQLite and found no open identity-code handle in this sequence.
The log does not identify the blocking owner or establish a production defect.
No arbitrary retry, sleep, suppression or production change was used to turn
it green. The final repository suite therefore remains **failed**, with five
original failures resolved by the documented affected checks.

After the test-fixture changes, final Ruff lint and formatting passed again
(526 Python package/test files); native harness scripts also passed lint.
The [final source comparison](../../../.test_tmp/native-followup-20261112/checks-freeze-05/source-freeze-final.json)
confirms all 247 package inputs still match freeze-05 and the repository's V1
plugin matches the packaged asset byte for byte. No production change followed
the frozen native runs.

Earlier full-suite attempts were stopped when native acceptance demonstrated
changes requiring a new freeze; no interrupted run is reported as passed.
Hosted CI is separate and was not triggered by a push. No commit, push,
subscription upgrade, purchase or new service account was performed.
