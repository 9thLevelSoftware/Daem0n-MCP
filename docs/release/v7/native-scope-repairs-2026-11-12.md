# Native scope repair round 11 — 2026-11-12

This new user-authorized round follows the [round 10 report](native-guidance-repairs-2026-11-12.md).
It targets Mac OpenCode V1's extra setup health call and the Mac/Ubuntu WSL
OpenCode V2 resource-discovery scope failures. Prior raw events, verdicts,
matrices, and backend versions remain historical evidence. No commit, push,
purchase, account upgrade, or provider/model change was made.

| Affected lane | Outcome in this round |
| --- | --- |
| Mac OpenCode V1 extra setup health call | **Passed** strict setup and necessary cleanup checks |
| Mac OpenCode V2 resource/workspace scope | **Failed** both native repair attempts |
| Ubuntu WSL OpenCode V2 resource/workspace scope | **Failed** both native repair attempts |
| V2 authenticated native-service readiness | **Passed** zero-model checks on both platforms; not a native workflow pass |

## Evidence and implementation

Offline replay of Mac V1's real trace reproduced the setup failure using its
unchanged validator. A minimal pair of actual health/index events still failed;
an index-only diagnostic control passed but was not counted as native
acceptance. No hook code invoked health automatically. The verified installed
plugin still contained older context, and the MiniMax model selected its
simplified branch. Both old and newer guidance mentioned health without making
its optional nature explicit. The model also described its action as a health
probe. This supports a guidance repair hypothesis, not proof of deterministic
model causality. [Diagnosis and replay](../../../.test_tmp/native-followup-20261112/repair-11/posix/diagnosis.json).

V2's three retained denied requests used `{}`. The pinned host supports a
server filter; omitting it enumerates all configured servers, including the
preserved disabled sentinel. The existing Daem0n-only grant correctly denies
that broader request before resource catalog execution. Actual effective
instructions lacked server-scope guidance.
[Red replay](../../../.test_tmp/native-followup-20261112/repair-11/v2/resource-scope-red-replay.json).

Production changes are limited to four package files and the identical V1
repository plugin mirror:

- `daem0nmcp/client_install.py`: optional diagnostics and task authority in the
  managed covenant, plus V2-specific native resource filter guidance.
- `daem0nmcp/api/v7/tools.py`: health remains discoverable as optional diagnostics,
  with no required startup ceremony.
- `daem0nmcp/opencode_assets/daem0n.ts` and `daem0n-v2.ts`: coherent task order,
  optional health/briefing, direct typed-tool preference, and V2 server scope
  in the context actually injected into the model.

Runtime hook logic, edit receipts, authentication, and server admission are
unchanged. Strict-mode ceremony remains explicit. No model-selected tool
argument is rewritten and no resource grant is broadened.

Acceptance changes are separate from the package: the new
`scripts/native_opencode_v2_readiness.py` owns an authenticated loopback service;
`scripts/native_opencode_v2_acceptance.py` can attach to it and collect its exact
actual event log. `tests/test_native_opencode_v2_readiness.py` exercises those
boundaries. `scripts/native_acceptance_rebind.py` and its tests add only the
exact freeze-11 interpreter/digest allowlist. The whole-file AST proof permits
only the reviewed startup, authentication, redaction, and collector changes;
all prompt, validator, and permission code remains identical to the retained
first-attempt driver.
[Final contract proof](../../../.test_tmp/native-followup-20261112/repair-11/final-freeze-contract-verification-03.json).

## Additional host verification

The new `scripts/native_opencode_v2_host_audit.py` supplements the unchanged
16 backend predicates. It audits observed before/after calls, including native
helpers. A successful outer execute event cannot erase an inner denial.
Pairing includes session, call ID, tool, exact JSON input, and the completed
child interval. Scoped resource metadata must have the pinned bounded shape
and Daem0n server attribution. Content reads and unrelated operations are not
allowed in these restart checks.

Expected COUNSEL errors are exempted only with an authoritative error envelope
and the exact typed same-session successful remedy retry. Unknown, denied,
malformed, or incomplete calls fail host qualification. This is an audit of
the declared observed event surface, not a JavaScript or shell sandbox.

Independent review found and closed three initial audit gaps: malformed or
foreign metadata, children outside their outer execution interval, and an
unrelated denial disguised by an embedded COUNSEL envelope. The review cleared
the final production guidance and companion audit before native calls.

## Frozen build and checks

Freeze 11 wheel SHA-256:
`0d8d9087888b09860297619d25c74cb2a47f0d8c7fa8f5cc92d872393f42c687`.
All 229 Python modules and 246 package files are attested. At first native
release, the five existing acceptance script hashes and both previous selected
matrices remained unchanged.
[Freeze/contract verification](../../../.test_tmp/native-followup-20261112/repair-11/freeze-contract-verification.json).

- Six installed-instruction and actual Bun context callback cases were red
  before the guidance fix and passed after it, including both V1 model branches.
- Compatibility selection: 150 passed, one skipped, one initial failure.
  Restoring the health search phrase fixed that failure; the affected discovery
  check and description bound check passed. This yields 151 distinct passing
  compatibility cases, not an unqualified clean initial invocation.
- The companion audit initially passed 25 checks. Review then exposed 19 red
  negatives; 25 affected cases passed after repair, with 21 unaffected cases
  deselected. [Versioned test receipt](../../../.test_tmp/native-followup-20261112/repair-11/v2/host-audit-review-repair-verification.json).
- Two new exact freeze-11 rebind checks passed after their red baseline. A
  formatting issue in those new test parameters was corrected separately.
- Repository Ruff lint/format, Linux-targeted v7 mypy, and release inventory
  passed. Later rebind changes received scoped lint/format checks.
  [Results](../../../.test_tmp/native-followup-20261112/repair-11/static-results.json)
  and [executed commands](../../../.test_tmp/native-followup-20261112/repair-11/static-commands.json).

These checks supplement earlier full-suite evidence; no second complete pytest
run or hosted CI pass is claimed in this round.

The readiness helper covered 32 focused cases. Authentication-specific checks
then reproduced three failures and passed after the supported local-service
authentication fix. Scoped lint and format passed. These are harness checks,
separate from native model acceptance. The original unauthenticated preflights
both failed with local HTTP 401 before any model or native tool call; those
receipts remain preserved. Authenticated zero-model qualification subsequently
passed on Mac and Ubuntu WSL, with exact-project status, unchanged source and
authority roots, and a disabled unrelated server. Mac's cleanup receipt retained
a `PermissionError`/unknown result; a later exact process inspection found no
matching owned processes. That observation does not explain the original error.
[Authentication verification](../../../.test_tmp/native-followup-20261112/repair-11/v2/readiness-auth-targeted-verification.json).

## Native results

Mac V1 setup and necessary cleanup passed on the first bounded native attempt
each. Setup indexed 270 files at generation 8; cleanup restored 269 at generation
9. All seven native tool events were completed discovery/index calls, with no
extra health, briefing, memory, denied helper, or host error. Earlier successful
removal/restart checks were not repeated. The updated [primary matrix](../../../.test_tmp/native-followup-20261112/repair-11/combined-primary-matrix.json)
selects 30 passed workflows and six blocked Claude workflows across the six
primary clients, two projects, and three platforms. Only the two Mac V1 setup
checks were rerun in this round; earlier stages retain their exact historical
versions. All 79 referenced evidence files matched their recorded hashes.

Both first V2 attempts on freeze 11 failed whole-host acceptance. Mac passed
all 16 backend predicates and used the correct resource server filter, but
attempted two unavailable direct MCP tools inside Code Mode. Ubuntu WSL
subsequently attempted three unscoped resource inventories and 40 unrequested
resource reads. All 40 reads failed; no resource content was returned. These
failures remain failures regardless of successful individual backend calls.

The traces also demonstrate a native startup race: the first context supplied
only four native tools, the model executed a resource helper, and only the
next context supplied the 11 direct Daem0n tools. Inspection of the pinned
2.2.7 native request builder confirms that it builds the model request from
that context's tool definitions. The MCP startup wait is capped at two seconds;
the tool snapshot can precede connection completion. This establishes missing
definitions in the first host model request, although the exact provider wire
body was not captured. [Routing diagnosis](../../../.test_tmp/native-followup-20261112/repair-11/v2/routing-diagnosis.json).

The second repair used the supported native service API to wait for the same
project's MCP connection before attaching `run --server`. Production freeze 11,
the acceptance prompt, permissions, and backend predicates stayed unchanged.
Both second native attempts failed: the actual project observer log contained
setup only, while native stdout showed model turns. The wrapper rejected missing
model context with an `IndexError`; no native pass is inferred from it. WSL's
stdout showed a skill loaded from another project location. The pinned attached
client selects its session directory using `process.env.PWD ?? process.cwd()`.
The harness preserves inherited `PWD` even though it changes the child process's
working directory. That stale environment value explains why the attached
session did not use the warmed fixture. The explicit service URL and
authentication were correct; the project-directory binding was not.
[Pinned routing source](../../../.test_tmp/native-followup-20261112/repair-11/v2/run-server-routing-source.json).

WSL also completed two unplanned host metadata writes: `session_move` moved its
test session to `/mnt/d/Daem0n-MCP`, and `session_rename` changed the title. These
are explicit scope failures. Both stdout streams contained additional metadata
probing or errors; an empty fixture observer is not evidence that host calls
succeeded. No successful source-edit or Daem0n memory call was observed in those
stdout inventories.

Independent review verified all 80 collected evidence-file hashes. Separate
post-model read-only audits confirmed all 269 source files, owned configuration,
credential hashes, runtime paths, and authority roots remained unchanged on
each disposable fixture, with no matching owned native processes. These audits
were necessary because the wrapper's earlier exception skipped its own
postconditions. [Ubuntu WSL audit](../../../.test_tmp/native-followup-20261112/repair-11/v2/linux/post-ready-source-authority-process-audit/stdout.log)
and [Mac audit](../../../.test_tmp/native-followup-20261112/repair-11/v2/macos/post-ready-source-authority-process-audit/stdout.log).
The local production tree also still matches freeze 11 after the native attempts.
[Post-native contract proof](../../../.test_tmp/native-followup-20261112/repair-11/post-native-freeze-contract-verification.json).

There were two model repair attempts per V2 host in this round, both failed.
No third model attempt was run. Authenticated readiness alone does not qualify
native workspace selection, resource scope, or complete host observation.
Ordinary standalone startup remains unqualified by the readiness workaround.
The [V2 matrix](../../../.test_tmp/native-followup-20261112/repair-11/v2/final-status-matrix.json)
selects 16 historical passes and these two latest failures. Those earlier
passes were not rerun or reaudited with the new host auditor. All four model
attempts in this round failed overall; Mac's first backend-only pass remains
history and is not combined with a later readiness pass.
The next repair must bind the attached client's environment to the canonical
fixture directory and prove first-turn project/tool scope natively. That change
and a third model attempt were not made after this round's two failed repairs.
The [V2 detailed report](opencode-v2-repair-11-2026-11-12.md) retains each attempt,
the exact location reproductions, and component-level qualifications.
[Independent final evidence review](../../../.test_tmp/native-followup-20261112/repair-11/v2/final-evidence-review.json)
confirmed the selected counts and preserved historical rows.

## Fixture maintenance and remaining external gaps

V2's disposable fixtures carried stale plugin ownership hashes from previous
controlled interpreter retargeting. The installer correctly refused them.
Preparation required exact matches against both immutable prior receipts and
the frozen prior renderer, consistent backups, and exact interpreter retargeting.
The supported automation-omitted installer refreshed the covenant, while the
public frozen renderer refreshed the owned plugin and its managed hash.
Credentials, short runtime overrides, sidecar environment, permissions, source,
and authority records were preserved. This guarded test-fixture repair is not
claimed as an ordinary explicit-enforce installer upgrade pass.

Claude's retained quota failures, missing MCPJam account, and missing usable E2B
credentials remain blocked. Hosted embeddings remain unsupported because the
provider adapter is absent. The earlier intermittent Windows rename cause
remains unexplained despite passing reruns. None is inferred passed here.
