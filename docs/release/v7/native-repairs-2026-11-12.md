# Native workflow repairs — 2026-11-12

The subsequent [guidance, typed-tool, and readiness repair phase](native-guidance-repairs-2026-11-12.md)
retains this report as history and records the newer OMP, Mac V1, and V2 outcomes.

This follow-up implements the user's request to continue after the
[first acceptance report](native-integration-2026-11-12.md). Demonstrated code
defects were corrected and bounded native reruns completed; acceptance remains
incomplete where strict workflows still fail. Earlier results and raw attempts
remain historical evidence. No commit, push, account upgrade, purchase or change
of provider/model was performed.

## Demonstrated causes and changes

Discovery described a direct target-call schema requiring `workspace_id`, but
the gateway rejects that field inside `arguments`. Real FastMCP replay confirmed
that a payload valid against the advertised target schema was rejected by the
gateway. Discovery now retains that direct schema and adds a standalone
`gateway_arguments_schema`, the gateway tool name and explicit top-level scope
placement. References and nested user-data fields are preserved. The nine-tool
core surface and strict scope/argument validation remain unchanged.

The retained failed-turn sample contained 67 numeric-field failures: 48 search
limits and 19 impact depths, comprising 65 strings and two nulls. Previously
these returned only `Invalid value`. Bounded, sanitized field errors now explain
public JSON types and limits without echoing supplied values or contexts.
Input descriptions also clarify numeric types, cursor provenance and root/glob
semantics. Search/impact descriptions explain that indexes persist across
sessions; restart alone does not require rebuilding.

A real local API replay reproduced `EVENT_STREAM_CONFLICT` when broadening an
index from a source subdirectory to the workspace root. The index publication
transaction created an excluded SQLite journal, changing the diagnostic skipped
file count without changing source. Publication now compares eligible source
files and their byte preimages, preserving cancellation and source-change
rollback. Initial diagnostic counts remain reported. The original Mac failure
has no retained local database replay, so this independently reproduced cause
is not presented as proof of its exact historical cause.

Installed covenant text now reuses a supplied opaque workspace ID, explains
gateway scope/types and persistent indexes, and avoids invented or duplicate
memory writes solely for indexing or a new turn. OMP guidance identifies its
observed native MCP wrappers conditionally, without granting ordinary file
writes. Harness permission repairs are restricted to bounded read-only tool
discovery and exact stage-specific operations; unsupported calls remain denied.

Windows V1's npm `.cmd` launcher also mishandled a prompt containing angle
brackets, failing before any model or MCP event. The repair invokes the wrapper's
existing, hash-checked native executable directly. Pi similarly uses its
existing Node executable and CLI script as an argument vector. These changes do
not alter the client version, provider, model or grants; the failed zero-event
launch remains a separate receipt.

## Windows rename investigation

The previous `WinError 5` remains unexplained, not declared fixed. The exact
isolated test, 328 fresh diagnostic scenarios and the identity module (76 passed,
one skipped) did not reproduce it. A controlled handle without delete sharing
reproduced the exact error and validated owner inspection. A reviewed,
failure-only plugin instrumented the next full suite, with an eight-second
diagnostic bound and unchanged test exception. The original rename test passed
in that full run; no failure-time handle capture was generated. This clears
the observed rerun but does not establish or repair the intermittent cause.
It performs no retries, delays before rename, permission bypass or foreign
handle closure. [Diagnostic evidence and instructions](../../../.test_tmp/native-followup-20261112/repair-06/rename/README.md).

## Verification and native follow-up

- Gateway regressions were red before repair; the compatibility selection passed
  83 tests and 60 subtests, including real FastMCP dispatch, reference isolation,
  strict numeric rejection, sanitization and unchanged counsel/task contracts.
- Installer preservation selection passed 90 tests, with seven native-platform
  skips. The conditional instruction text does not change publication or grants.
- Two indexing regressions failed before repair. Six targeted cases passed after
  it, including real source-change and cancellation rollback. The original real
  parser replay also passed after correction.
- Independent source review found no material issue in these changes or in the
  bounded rename observer.
- During the full suite, the process-level validation test retained two exact
  expectations for the old generic error message. Those expectations now match
  the public schema guidance; error codes, field sanitization, input non-echo,
  size bounds and the valid-call control are unchanged. The affected test passed
  once after that correction, with independent review. The full run's original
  failure remains in its log.

Evidence is retained under `.test_tmp/native-followup-20261112/repair-06/`.
`freeze-06` and `freeze-07` were not selected for retained repair model-call
attempts. Freeze-07 was installed and configured during preparation, with those
receipts preserved before retargeting. Inspection of
the latter found two obsolete Python modules copied from the checkout's old
`build/lib` directory. No retained repair native model-call attempt used those
candidates.
The replacement was built from a new, explicitly enumerated source snapshot;
the old build directory and evidence remain intact. Every packaged member is
hash-bound to source, all 228 Python modules are present, and the declared
runtime assets are required. Missing or changed Python, TypeScript and UI
assets have rejection regressions. Historical wheel hashes identify the actual
old artifacts; their incomplete source inventories do not establish this new
whole-package attestation.

The first repaired native build is `freeze-08`, SHA-256
`1a7082b687ae6759934d6ee480a732722f6b9a8798b36d834f9f5d116a627609`.
[Source and complete wheel attestation](../../../.test_tmp/native-followup-20261112/backend/freeze-08/wheel-runtime-assets-attestation.json)
include all 245 packaged source files. Source-only UI build inputs and gitkeep
files are not declared runtime assets.
The freeze-08 results are retained separately from the final adapter follow-up.

The full repository run completed in 35m38s: 4,208 tests passed, 111 skipped,
2,114 subtests passed, one test failed on the obsolete error-message expectation
(subsequently corrected and passed), and four schema-equality subtests failed.
The latter exposed a real adapter defect: its synthetic callable signatures
retained validation constraints but dropped field descriptions. A bounded
adapter correction passed all 22 affected adapter methods, including all 77
schema comparisons and actual in-process FastMCP listing. A new wire test's
initial title expectation was corrected for FastMCP's intentional title pruning;
its four affected cases then passed. The original full run is not described as
a clean pass.

The [full log](../../../.test_tmp/native-followup-20261112/checks-freeze-07/pytest.log)
is paired with [error-message follow-up](../../../.test_tmp/native-followup-20261112/repair-06/process-error-guidance.log)
and [adapter follow-up](../../../.test_tmp/native-followup-20261112/repair-06/fastmcp-description-wire.log).
Final collection contains 4,346 tests versus 4,320 at the full run's start;
later added/changed harness and artifact cases were checked in focused runs,
not a second complete suite. The
[observed-remedy and stage-policy receipts](../../../.test_tmp/native-followup-20261112/platforms/focused-tool-output-receipts/manifest.json)
explicitly preserve actual tool-result output after the invocation, rather than
claiming original redirected stdout, JUnit or invocation-time source hashes.
The final changed harness lint/format checks passed. Independent review found no
material issue in the corrected adapter, exact-remedy validation, source guards,
baseline preservation or matrix selection logic.

The final adapter build is `freeze-09`, SHA-256
`a6c371a0c1a5b2f69f6e68b04d16caa5c77a2a5b1fc4689915b2e6fe6b571010`.
Its sole production change from freeze-08 carries field descriptions into
callable annotations, preserving constraints, validator order, defaults and
aliases. [Complete wheel attestation](../../../.test_tmp/native-followup-20261112/backend/freeze-09/wheel-attestation.json)
and independent review passed. Linux-platform mypy (49 source files), release
inventory (77 tools, 10 resources, 588 requirements) and changed-file Ruff passed.
Only relevant failed native stages were retried; retained freeze-08 passes
remain identified as freeze-08 evidence. The missing descriptions are a proven
adapter defect, not proof of the exact cause of every earlier native argument
failure.

Early strict failures remain separate from successful components. Windows V1's
freeze-08 TypeScript removal completed functional checks but sent numeric strings
and made unrequested calls. Its freeze-09 follow-up passed the entire turn using
actual JSON numbers, after a fresh native setup and 270-to-269-file removal.
The original failed turn remains retained. WSL OMP's Python restart completed persistence and
replay checks, but explicitly briefed before the requested first recall, so it
did not establish that turn's automatic briefing requirement. Valid explicit
briefing is not substituted for the requested automatic-brief evidence.

The Mac's previous home-directory acceptance runtime and tools are absent,
confirmed against absolute paths on the expected SSH host; the cause is unknown.
Canonical disposable source projects and their authority databases survive.
Their source and configuration hashes were checked before consistent SQLite
backups and isolated pinned-tool reprovisioning. V2's canonical bridge
credentials survived with identical hashes. V1's missing private credentials
required supported fresh automation installation and an explicitly new bridge
authority; credential continuity is not claimed for that reprovision. The new
bridge passed an actual native deny→preflight→edit→single-capture cycle. Its
credentials and hook runtime were subsequently preserved during the server-only
freeze-09 retarget.

## Selected primary workflow results

The [combined primary matrix](../../../.test_tmp/native-followup-20261112/repair-06/combined-primary-matrix.json)
contains 36 client/project/platform pairs: **24 passed, six failed, six blocked**.
It combines historical passes with selected repair turns; it is not a claim that
all 36 pairs were rerun on freeze-09. The previous matrix had 16 passed, 12 failed
and eight blocked. Inputs, selected summary hashes, exact versions and backend
provenance remain linked. All 64 selected local evidence references were checked
against their hashes and recorded strict verdicts during matrix construction.

| Native platform | Passed | Failed | Blocked |
|---|---:|---:|---:|
| Windows | 9 | 1 | 2 |
| Ubuntu WSL | 8 | 2 | 2 |
| macOS | 7 | 3 | 2 |

The six failed pairs are OMP/legion on Windows, both OMP source projects on each
POSIX host, and OpenCode V1/legion on macOS. OMP's five affected restart flows
explicitly briefed before the required first recall; persistence and exact
record/event replay passed. Windows OMP/legion also omitted the workspace ID on
a mutation-stage call and was denied. Mac V1/legion now passes initial indexing
and a fresh mutation (generation 3→4), but removal still sent numeric strings and
an unrequested operation. Its functional removal checks passed; the strict turn
failed and restart remains blocked. These outcomes were not normalized or
reclassified as passes.

Detailed evidence: [Windows freeze-08 repairs](windows-repair-2026-11-12.md),
[Windows freeze-09 follow-up](windows-schema-repair-2026-11-12.md), and
[POSIX repair appendices](platform-acceptance-2026-11-12.md). Source restoration,
authority and configuration preservation checks passed. Mac and WSL public
evidence snapshots retain raw host events and hash manifests while excluding
private databases and credentials. No primary acceptance processes remain.

## External and historical limits

OpenCode V2 is a separate compatibility lane. Its
[selected repair matrix](../../../.test_tmp/native-followup-20261112/repair-06/v2/final-status-matrix.json)
has **16 passed and two failed case rows**, including one necessary cleanup
qualification; these are not the same units as the 36 primary source-pair rows.
Twenty-three actual native turns and the original failures remain in history.
The coordinator checked all 18 selected verdicts/check sets against their
retained validation files. The [V2 report](opencode-v2-2026-11-12.md) preserves
the original capability qualifications and separate archive manifests.

Both remaining V2 failures are direct-mode TypeScript restarts. On Ubuntu WSL,
all native operations and persistence checks succeeded, but the retry omitted
seven fields from the exact counsel remedy; backend canonical replay success
does not satisfy that requested raw-argument proof. On macOS, `code_search`
again received a string limit, so the remaining persisted-code queries and
health call did not complete. Memory replay and automatic briefing passed.
No third attempt was made. The earlier false mode-label and expanded-remedy
validation predicates were corrected using separately retained offline
derivatives; native arguments and original host events were not rewritten.
All task-owned provider processes are closed. Where a Mac group-close probe
returned a permission error, the report retains that result and the subsequent
exact PID/group absence observation rather than inventing a successful signal.

Claude's six source-pair rows remain blocked by the retained weekly-quota
failure; no new quota retry or upgrade occurred. MCPJam rendering remains
blocked because the user has no existing account. Live E2B remains blocked by
missing usable credentials. Hosted embeddings remain unsupported because the
provider adapter is absent; successful token counting does not establish hosted
embedding support. Earlier projection recovery, agy automatic-loading controls,
resources and automation evidence remain in the historical report with their
original build identifiers. Hosted CI was not rerun and is not native evidence.

## Changed implementation

The repair's production changes are in
`daem0nmcp/api/v7/discovery_operations.py`, `gateway_operations.py`, `tools.py`,
`fastmcp.py`, and `daem0nmcp/client_install.py`. Targeted regressions accompany
them. Native driver, scenario, guarded repair and artifact-attestation helpers
under `scripts/native_*.py`, their tests, and these release reports also changed.
Earlier integration changes remain in the working tree. No commits or pushes
were made, and the unexplained intermittent rename cause is not declared fixed.
The [delivery inventory](../../../.test_tmp/native-followup-20261112/repair-06/final-delivery-verification.json)
records all 95 changed working-tree files, including prior integration work,
and verifies the unchanged Git head and final production snapshot.

Native reruns retain existing accounts/models, immutable source revisions and
old stage receipts. Only failed stages and necessary state preparation/cleanup
are rerun. A successful partial probe cannot silently clear a strict failed
whole turn. Source removal checks require actual indexed mutation preconditions.
V2 remains a separate lane. Claude quota, MCPJam account access, E2B credentials
and unsupported hosted embeddings remain explicit external gaps.
