# Native platform acceptance follow-up

Date: 2026-11-12. This follow-up records actual native client preparation on the
paired Apple-silicon Mac and Ubuntu WSL. Preparation, login status and source
transfer are distinct from a model completing a Daem0n workflow.

## Reproducible preparation

`scripts/native_acceptance_platforms.py` runs Python over the existing paired SSH
connection or the Ubuntu WSL transport. Attempts have unique directories under
the ignored `.test_tmp/native-followup-20261112/platforms/` evidence root. An
existing attempt cannot be overwritten. The request records the operation and
script digest; stdout/stderr are redacted before persistence. Process-only
provider secrets can be supplied through the Python API's `sensitive_values`
argument, without writing the transmitted script or credential to disk.

All installed acceptance dependencies are beneath the remote user's
`.daem0nmcp-native-acceptance/20261112/` directory. Existing globally installed
clients and their configuration are retained. The helper invokes platform-native
executables, rather than Windows binaries mounted inside WSL.

| Lane | Actual operating system | Isolated Python | Client binaries prepared |
| --- | --- | --- | --- |
| macOS | macOS 27.0.1, arm64 | CPython 3.12.13 | Claude Code 2.1.274; Codex 0.167.0; agy 1.5.7; OpenCode 1.18.21; OMP 18.16.0; Pi 1.0.0 |
| Ubuntu WSL | Linux 6.18.33.2-microsoft-standard-WSL2, x86_64 | CPython 3.12.14 | Same six pinned client versions |

The Mac's existing uv catalog could not supply CPython 3.12.14. The failed
request is retained in `macos/python-preparation`; a supported Python 3.12
selection installed 3.12.13 in `macos/python-preparation-compatible-catalog`.
The original user Python was not upgraded.

The first private npm installation completed package extraction but npm deferred
dependency install scripts. The first client-preparation attempt also refused
to use a changed agy release manifest. The continuation ran the specifically
reviewed install scripts and used exact agy 1.5.7 artifact URLs and SHA-512
digests from the official manifest. OMP uses its exact 18.16.0 release artifact
and SHA-256 digest. All six private executables returned their expected versions
on both systems. This did not make a model request.

Primary distribution sources are the
[Antigravity CLI installer](https://antigravity.google/cli/install.sh),
[OMP 18.16.0 release](https://github.com/can1357/oh-my-pi/releases/tag/v18.16.0),
and the exact npm versions recorded by the preparation helper. No global client
update, subscription change or purchase was performed.

Separate private prefixes also contain OpenCode V2 2.2.7 (`@opencode/cli`) and
Claude Code 2.1.295 for the newer hook-failure contract. Their version checks
passed on both systems; V1 and the prepared Claude 2.1.274 binaries were
preserved. V2 uses its own executable and configuration lane. The
[current OpenCode installation guide](https://opencode.ai/docs/) identifies the
separate V2 package; the exact package version is pinned in the helper.

## Source fixtures and transfer

`scripts/native_acceptance_fixtures.py` exports immutable Git blobs at each
repository's recorded HEAD: Python source from Daem0n and TypeScript/TSX `src`
files from legion-cli. It records the exact revision, every exported file hash,
the source checkout's separate dirty state, and known search/call-graph
subjects. Working-tree edits, host instructions, client configuration,
credentials, untracked files, caches and runtime data are excluded.

The original exporter smoke produced 225 Python and 269 TypeScript source files
from tracked working-tree bytes before the immutable-revision policy was
applied. Its dirty-source manifest and verified hash transfers remain historical
evidence in `transfer-fixture-smoke`. Those smoke trees are not final native
workflow evidence. The updated exporter regression verifies that uncommitted
source changes are excluded from a revision snapshot.

## Authentication readiness and preservation

Native `codex login status` reported ChatGPT authentication on both systems.
That establishes login presence, not valid provider quota or a successful model
turn. Native Claude status reported `loggedIn: false` on both. Native agy model
discovery in isolated account homes reported that sign-in was required. No
refresh credential was copied or exported.

After the user's isolated native sign-ins, agy model discovery succeeded on
both systems and advertised `gemini-3.8-flash-low`. Claude 2.1.295 status then
reported a Claude subscription login in the isolated WSL profile. The Mac SSH
process still reported no Claude login for both prepared Claude versions in the
same isolated profile; this is an authentication-context blocker pending
native Mac diagnosis, not a successful model call.

The prepared native Python environments have the base, apps and graph
dependencies installed. Their package freezes are retained in
`backend-dependency-preparation`; the final Daem0n wheel is installed only after
the coordinator freezes it. Optional local embedding and compression model
downloads are outside these code-indexing lanes.

The existing SSH and WSL process environments did not contain `E2B_API_KEY`.
Only presence was tested; credential values and unrelated files were not
searched. The receipts are in each platform's `optional-key-presence` attempt.

Initial isolated version probes preserved all monitored original profile hashes
on both systems. A later native Claude status command with the real WSL home
created `/home/dasbl/.claude.json`, which was absent immediately before that
command. The before/after receipt is in `linux/native-auth-status/stdout.log`.
The file was not removed or overwritten to hide this exception. The same native
status probe preserved the monitored original Mac files. Subsequent Claude
commands select an explicit isolated HOME and CLAUDE_CONFIG_DIR.

## Acceptance boundary

The final native matrix must use the coordinator's frozen backend and source
manifests. Installation preservation, instructions, core memory, restart,
real-source indexing, resource reads and the claimed automation mode each need
actual host-observed tool results. Prepared binaries and a login-status result
do not satisfy those gates. Missing account access remains a blocked lane;
unsupported client surfaces remain explicitly unsupported.

At this preparation checkpoint no native model workflow has been credited as
passed. Original Windows acceptance and existing CI results remain separately
identified evidence.

## Native execution results

The preparation checkpoint above is historical. Native execution subsequently
ran on **macOS 27.0.1 arm64** and **Ubuntu WSL**, Linux
`6.18.33.2-microsoft-standard-WSL2` x86_64. Python is isolated 3.12.13 on the Mac
and 3.12.14 in WSL. The native clients are Claude Code 2.1.295, Codex 0.167.0,
agy 1.5.7, OpenCode V1 1.18.21, OMP 18.16.0 and Pi 1.0.0. OpenCode V2 2.2.7 is
reported [separately](opencode-v2-2026-11-12.md).

The source inputs are immutable Git blobs, not dirty checkout bytes:

| Source | Recorded revision | Production source files |
| --- | --- | --- |
| Daem0n-MCP | `4a96e8a8056c1b1d5cd1ca0f63c9f95f7e6929b5` | 225 Python |
| legion-cli | `581f1f5255f4ae3364750bab43eb34eb053e4a49` | 269 TypeScript/TSX |

Each client/source pair has its own disposable Git root and storage. The native
stages test real symbols/callers, unchanged indexing, coordinator-owned source
edits/additions/removal, stale-result removal, and restart persistence. The TS
impact claim is limited to same-file calls; cross-file TS impact is not passed.
Source manifests retain every exported file hash. The harness requests disabled
optional local-model/dense features and installs the base/apps/graph profile.
This is configuration intent: native hosts can filter inherited environment
flags. Effective child capability status must be read from each retained native
health response; no blanket disabled/no-download result is claimed. Dense
qualification remains separate evidence.

All installed backend distributions are `7.0.0.dev0`, distinguished by immutable
wheel hashes. Earlier passes are retained rather than relabeled as final-wheel
executions:

| Backend | SHA-256 | Use |
| --- | --- | --- |
| Frozen 01 | `38475f299dc66918a645760dfaea633b7e6883f1dc0901468a39dbc1b6155c65` | Initial native core/Python stages |
| Frozen 02 | `6d9939417043b705631c03342095c5af7c2534c7cf40592b85a0a604b35b64da` | TS producer repair and affected index/remaining stages |
| Frozen 04 | `ab19bd46de7507ed5c3b67df400070f618e0512b17d0ceae73c02c06054768b9` | Native long-socket qualification; stale endpoint retained |
| Frozen 05 | `9b6ff0e18753e2075cc14eab2eeaa4dd2435ca8be06daace00906ce183618963` | Actual restart recovery, strict preload, reminders, resources and remaining V1 server stages |

Controlled rebinds retain before/after configuration and database-file hashes,
preserve authority/storage and credentials, and record the MCP and hook
interpreters separately. Existing V1 source fixtures retain their frozen-01
hook interpreter when only their MCP server is retargeted. The independent
V1 native edit-gate fixtures use frozen 05 for both paths.

### Automatic instructions and native automation

The [agy automatic-loading report](automatic-loading-2026-11-12.md) includes
independent native positive/negative first-response probes on both platforms:
exact random marker versus `null`, no filename/marker in either prompt, denied
native filesystem/command/network/MCP operations, and zero tool calls. Normal
guided memory use and restart are separate source-matrix executions.

All eight reminder probes passed on frozen 05. These are host-invoked callback
observations, not installed-mode assertions:

| Client, on both platforms | Observed native events | Scope of pass |
| --- | --- | --- |
| Codex | `SessionStart`, `PreToolUse`, `Stop` | Actual native edit matcher; edit approval withheld and target absent |
| agy | `SessionStart`, two `PreToolUse`, `Stop` | Actual `write_file` and `edit_file`; exact intended file contents |
| OMP | `before_agent_start` | Actual returned `daem0n-covenant-reminder` message |
| Pi | `before_agent_start` | Actual returned `daem0n-covenant-reminder` message |

The [Mac native reminder results](../../../.test_tmp/native-followup-20261112/platforms/macos/native-reminder-events05-01/stdout.log)
and [Ubuntu WSL native reminder results](../../../.test_tmp/native-followup-20261112/platforms/linux/native-reminder-events05-01/stdout.log)
include every check and the exact private settings/config restoration hashes.
WSL Codex uses its native private account directly; no refresh material is
extracted or copied. Mac Codex retains the earlier authorized access-only route.
All monitored original profile guards passed for these executions.

Mac Codex and OMP initially reported cleanup `closed: null` after `killpg(0)`
returned `EPERM`. Their raw summaries remain unchanged. A separate
[exact group inspection](../../../.test_tmp/native-followup-20261112/platforms/macos/native-reminder-exact-group-inspection-02/stdout.log)
found no remaining members of PGIDs 19029 and 19120, without sending signals.
This resolves remaining-process observation, not the cause of `EPERM` or an
immediate-shutdown guarantee. The other six reminder groups reported closed.

[OpenCode V1 native mediation](native-automation-acceptance-2026-11-12.md) passed
on both actual hosts: deny, real MCP preflight, identical successful native Edit,
one capture; missing interpreter denied native Write with unchanged target and
zero captures. V2 has independent evidence. Claude native MCP initialization
and SessionStart were observed, but real source/edit acceptance is **blocked**
by native weekly-quota responses, resetting November 14 at 11pm
America/New_York. No further Claude model retries were made after that blocker.

### Native MCP resources

All four Pi/OMP resource probes passed on frozen 05, separately from Windows:

| Native lane | Discovery | Same warnings URI refresh | Foreign scope | Other bounded resources |
| --- | --- | --- | --- | --- |
| macOS Pi | passed | Empty → exact warning store → populated | denied | failures, rules, active-context |
| macOS OMP | passed | Empty → exact warning store → populated | denied | failures, rules, active-context |
| Ubuntu WSL Pi | passed | Empty → exact warning store → populated | denied | failures, rules, active-context |
| Ubuntu WSL OMP | passed | Empty → exact warning store → populated | denied | failures, rules, active-context |

The [Mac native resource results](../../../.test_tmp/native-followup-20261112/platforms/macos/native-resources05-02/stdout.log)
and [Ubuntu WSL native resource results](../../../.test_tmp/native-followup-20261112/platforms/linux/native-resources05-02/stdout.log)
link the exact URI/order assessments and native receipts. Pi uses its installed
native resource helpers through Code Mode; OMP uses its native `mcp://` reads.
These passes do not claim MCP Apps rendering. Codex source runs separately
retain four native resource reads, foreign denial and reread; changed-content
refresh is not inferred from that repeated read. Other unexercised client
surfaces are not labeled unsupported without an actual capability result.

### Native Unix recovery and operational exceptions

Seven installed-wheel socket-path cases passed on each native platform, then
nineteen installed frozen-05 restart/unsafe-entry cases passed on each. The
[Mac actual restart proof](../../../.test_tmp/native-followup-20261112/platforms/macos/native05-same-endpoint-restart-proof/stdout.log)
and [Ubuntu WSL actual restart proof](../../../.test_tmp/native-followup-20261112/platforms/linux/native05-same-endpoint-restart-proof/stdout.log)
use the actual Claude native MCP manager, zero model calls, two sequential
start/stop cycles on the exact frozen-04 endpoint, unchanged credentials and
authenticated bridge responses of 201. The Mac's untouched stale endpoint
existed before the first start and was recovered through normal server startup.
Socket files still existed immediately after each SDK shutdown; immediate
removal is not passed. Next-start recovery is passed. No endpoint was manually
unlinked or rotated.

The initial WSL real-home Claude status command created `.claude.json`; that
documented preservation exception above remains visible. Mac Claude additionally
required a desktop Keychain context; a temporary, uniquely named launchd job
ran the reviewed fixed batch and was subsequently booted out successfully.
There was no Keychain reset/ACL change, credential export or persistent Login
Item. Both native Claude account contexts ultimately reached the same quota
blocker.

Mac disk pressure stopped the first frozen-05 upload before any wheel/runtime
change. The coordinator authorized seven reconstructible install caches, but
the UV cache contained internal symlinks and the conservative guard aborted
before deleting anything. Only the three authorized, link-free npm caches were
subsequently removed: `client-installation/.npm`,
`opencode-v2-preparation/.npm`, and `claude-enforcement-preparation/.npm`, all
under the owned test root's `private/` directory. All four UV caches, profiles,
runtimes, wheels, source snapshots and evidence were preserved. The
[cleanup manifest and receipt](../../../.test_tmp/native-followup-20261112/platforms/macos/authorized-npm-cache-cleanup-02/stdout.log)
record exactly 944,074,752 recovered free bytes, with manifest SHA-256
`08b738c269bf7fb37583bdcc9e631cf898c8567662c76a369b4c8f8e3053013b`.


## Final strict workflow matrix

Strict workflow status is separate from functional component coverage. A passed
component means actual native results cover that capability; it does not erase
unexpected errors in the selected turn. The [structured 24-row matrix](../../../.test_tmp/native-followup-20261112/platforms/final-platform-matrix.json)
contains every selected attempt ID, strict status, check predicates, hashes,
runtime provenance and the final source audit. Across the source workflows:
**12 passed, 6 failed, 6 blocked**. None is inferred from CI or another client.

| Platform | Client | Source | Strict workflow | Core | Context marker | Index/callers | Changes | Removal | Restart |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| macOS | Claude Code | Daem0n Python | blocked | blocked | blocked | blocked | blocked | blocked | blocked |
| macOS | Claude Code | legion TS/TSX | blocked | blocked | blocked | blocked | blocked | blocked | blocked |
| macOS | Codex | Daem0n Python | passed | passed | passed | passed | passed | passed | passed |
| macOS | Codex | legion TS/TSX | passed (repaired) | passed | passed | passed | passed | passed | passed |
| macOS | agy | Daem0n Python | passed | passed | passed | passed | passed | passed | passed |
| macOS | agy | legion TS/TSX | passed (repaired) | passed | passed | passed | passed | passed | passed |
| macOS | OpenCode V1 | Daem0n Python | failed | passed | passed | passed | passed | passed | passed |
| macOS | OpenCode V1 | legion TS/TSX | failed | passed | passed | passed | blocked | blocked | blocked |
| macOS | OMP | Daem0n Python | failed | passed | passed | failed | blocked | blocked | blocked |
| macOS | OMP | legion TS/TSX | blocked | blocked | blocked | blocked | blocked | blocked | blocked |
| macOS | Pi | Daem0n Python | passed | passed | passed | passed | passed | passed | passed |
| macOS | Pi | legion TS/TSX | passed | passed | passed | passed | passed | passed | passed |
| Ubuntu WSL | Claude Code | Daem0n Python | blocked | blocked | blocked | blocked | blocked | blocked | blocked |
| Ubuntu WSL | Claude Code | legion TS/TSX | blocked | blocked | blocked | blocked | blocked | blocked | blocked |
| Ubuntu WSL | Codex | Daem0n Python | passed | passed | passed | passed | passed | passed | passed |
| Ubuntu WSL | Codex | legion TS/TSX | passed | passed | passed | passed | passed | passed | passed |
| Ubuntu WSL | agy | Daem0n Python | passed | passed | passed | passed | passed | passed | passed |
| Ubuntu WSL | agy | legion TS/TSX | passed | passed | passed | passed | passed | passed | passed |
| Ubuntu WSL | OpenCode V1 | Daem0n Python | failed | passed | passed | passed | passed | passed | passed |
| Ubuntu WSL | OpenCode V1 | legion TS/TSX | passed | passed | passed | passed | passed | passed | passed |
| Ubuntu WSL | OMP | Daem0n Python | failed | passed | passed | passed | blocked | blocked | blocked |
| Ubuntu WSL | OMP | legion TS/TSX | blocked | blocked | blocked | blocked | blocked | blocked | blocked |
| Ubuntu WSL | Pi | Daem0n Python | failed | passed | passed | passed | passed | passed | passed |
| Ubuntu WSL | Pi | legion TS/TSX | passed | passed | passed | passed | passed | passed | passed |

Mac Codex/agy TS passes followed a targeted frozen-02 producer repair. Their
original initial turns failed only the expected-caller check, with no unrelated
unexpected MCP errors. The index-only follow-up and all remaining stages passed;
successful core writes were not replayed. These are repaired passes, not
first-attempt/seamless passes.

Both V1 Python workflows retain their failed initial caller-argument turns,
although all functional components are covered and the subsequent mutation,
removal and restart turns passed cleanly. WSL V1 TS has four genuinely clean
selected turns. Mac V1 TS initially used string-valued numeric arguments and an
invented cursor; that cursor's `CAPABILITY_DEGRADED` response is not evidence of
parser failure. Its one authorized index-only prompt repair completed real
indexing of 269 files, unchanged manifest, known symbol/callers and health, but
extra unexpected errors still failed the strict turn. Its dependent stages were
not run. No second prompt repair was attempted.

WSL Pi Python remains failed overall because its retained mutation turn included
an unexpected `INVALID_ARGUMENT`, despite passing functional mutation checks and
clean subsequent removal/restart. OMP source workflows stopped after the
coordinator-authorized final bounded-read policy correction still failed native
discovery/approval handling. Completed core components and WSL indexing of 225
files remain identifiable; TS source turns and dependent changes/restart were
not credited. Successful independent OMP reminders/resources do not convert its
source workflow into a pass.

## Final retained evidence and checks

The final public exports are local, complete and hash-verified:

| Native lane | Allowlisted files | Archive SHA-256 | Manifest/receipt |
| --- | --- | --- | --- |
| macOS | 671 | `8f436155d125afa2fb9eb59a8aa8435aae4d26c407453dc2aff4324acf8c3a6e` | [manifest](../../../.test_tmp/native-followup-20261112/platforms/macos/retained-snapshot-02/evidence-snapshot-manifest.json), [retention receipt](../../../.test_tmp/native-followup-20261112/platforms/macos/final-public-retention-receipt.json) |
| Ubuntu WSL | 630 | `d512848c0c691605292eba6c5cec53f9c348695709ea568224b2913cd6bd0f2f` | [manifest](../../../.test_tmp/native-followup-20261112/platforms/linux/retained-snapshot-02/evidence-snapshot-manifest.json), [retention receipt](../../../.test_tmp/native-followup-20261112/platforms/linux/final-public-retention-receipt.json) |

Every exported file and manifest hash was checked after transfer. Two existing
selected provider credentials were checked by exact literal matching against all
public files: **zero matches**, with no credential values printed or retained.
Account profiles, authentication files, bridge credentials, private directories
and databases were excluded. Initial snapshot-01 and historical failed attempts
remain distinct. The consolidated matrix records a derived OMP context-marker
classification correction backed by the original instruction-evidence artifact;
no native attempt was rewritten.

The [Mac final source audit](../../../.test_tmp/native-followup-20261112/platforms/macos/final-source-audit-and-matrix/stdout.log)
and [WSL final source audit](../../../.test_tmp/native-followup-20261112/platforms/linux/final-source-audit-and-matrix/stdout.log)
verify all 24 source snapshots match their frozen file hashes and that added
indexing probes are absent. The stopped WSL OMP mutation was restored through
the existing exact-preimage/source-ownership guard, without a new model or
indexing call; no stopped lane receives a stale-removal pass from that cleanup.
Expected native-mediation output fixtures remain retained as evidence.

Final read-only [Mac process inventory](../../../.test_tmp/native-followup-20261112/platforms/macos/final-owned-process-audit/stdout.log)
and [WSL process inventory](../../../.test_tmp/native-followup-20261112/platforms/linux/final-owned-process-audit/stdout.log)
checked 42 distinct historical owned group IDs per host and the private runtime
paths: zero remaining exact-group members or private-runtime processes. No
signals were sent. Earlier incomplete cleanup records remain unchanged with
separate resolution observations; the original `EPERM` cause is not explained.

Targeted harness checks passed: immutable Git export and redaction/isolation;
POSIX environment and mutation/preimage guards; exact agy output-artifact
confinement; native numeric JSON templates; Unix cleanup uncertainty retention;
Pi rebind approval positive/foreign-change rejection; and exact cleanup-summary
hash binding. Native path/symlink cases passed on both hosts (seven selected
cases each), then socket-path cases (seven each on frozen 04) and Unix restart
cases (nineteen each on frozen 05). Scoped harness Ruff checks passed.

One focused regression was added after final full-pytest collection:
`test_omp_bounded_recall_rejects_foreign_scope_extra_fields_and_unbounded_limits`
passed separately, checking the valid bounded own-workspace request plus foreign
scope, extra fields, oversized/empty queries and invalid limits. Full repository
pytest, package Ruff/mypy/release inventory and hosted CI are owned and reported
separately in the [consolidated integration report](native-integration-2026-11-12.md).


## Repair 08: later native evidence

This later repair preserves every earlier result and snapshot above. The
[structured repair results](../../../.test_tmp/native-followup-20261112/platforms/repair08-platform-results.json)
map all 22 new source turns to actual summaries, hashes, failed predicates,
client/backend versions and final source/authority guards: **16 passed, 6 failed**.
The table combines those turns with explicitly identified historical passes;
it does not claim a clean first attempt.

| Native host | Client / source | Initial | Add/edit | Remove | Restart | Latest strict workflow |
|---|---|---|---|---|---|---|
| Ubuntu WSL | OpenCode V1 / Python | Passed 08 | Passed historical | Passed historical | Passed historical | Passed after targeted repair |
| Ubuntu WSL | Pi / Python | Passed historical | Passed 08 | Passed 08 | Passed historical | Passed after targeted repair |
| macOS | OpenCode V1 / Python | Failed 08 | Passed historical | Passed historical | Passed historical | Failed |
| macOS | OpenCode V1 / TypeScript | Passed 08 | Failed 08 | Blocked | Blocked | Failed |
| Ubuntu WSL | OMP / Python | Passed 08 | Passed 08 | Passed 08 | Failed 08 | Failed |
| Ubuntu WSL | OMP / TypeScript | Passed 08 | Passed 08 | Passed 08 | Failed 08 | Failed |
| macOS | OMP / Python | Passed 08 | Passed 08 | Passed 08 | Failed 08 | Failed |
| macOS | OMP / TypeScript | Passed 08 | Passed 08 | Passed 08 | Failed 08 | Failed |

All four OMP restart failures have exactly one failed predicate:
`guided_auto_brief`. Each model called explicit `session_brief` before the
requested first recall, so the subsequent recall correctly lacked automatic
briefing. Same-record/event replay, persisted symbols/callers, stale-result
removal, scope and all other checks passed. The requested guided restart order
remains failed; neither the validator nor these original results was relaxed.

Mac OpenCode V1 continued to issue string-valued numeric arguments despite the
correct discovered schema and explicit type errors. Its Python initial turn
also lacked verified callers; the TypeScript mutation turn additionally used
unrequested operations and a fabricated cursor. The two irreversibly failed
turns were stopped through exact owned PID/group/UID checks, with separate
coordinator termination receipts. These are failures, not timeouts. The owned
TypeScript source mutation was restored; dependent source turns were blocked.
No paid retry followed either failure.

Both native runtimes imported the clean frozen 08 wheel
`1a7082b687ae6759934d6ee480a732722f6b9a8798b36d834f9f5d116a627609`:
245 package files matched wheel hashes with no extra package files. Python is
3.12.13 on macOS and 3.12.14 on WSL; native versions remain OpenCode V1
1.18.21, OMP 18.16.0 and Pi 1.0.0. These model calls used existing MiniMax
provider credentials in process memory only. Effective optional-model state is
available in each native health receipt; harness disable flags alone do not
establish that every optional local model was disabled.

Before Mac reprovisioning, independent absolute-path/host/user inspection found
the earlier private HOME runtimes/tools absent, with the cause unknown. The
canonical temporary source projects and databases survived. Public metadata
was restored from the verified historical snapshot; database contents and
credentials were not reconstructed from reports. Frozen source/config hashes
were checked and existing databases received consistent SQLite backups.
Post-install independent validation checked installer success, allowed semantic
configuration changes, unchanged logical database contents, Pi approval
preimages and storage authority before model calls. Those checks are accurately
labelled as post-install validation.

Mac V1's configured bridge credential files were absent. The authorized fresh
bridge installation therefore records new bridge credential locations/hashes
separately from the unchanged workspace/database storage authority. Its current
[existing-fixture enforcement probe](../../../.test_tmp/native-followup-20261112/platforms/macos/repair08-existing-v1-newbridge-gate/stdout.log)
passed: native Edit denied, actual MCP preflight, identical successful Edit and
one capture. The exact temporary target was removed after its expected
postimage was checked; frozen source and configuration hashes remained intact.
This proves the newly installed bridge independently of the failed indexing
workflows and the historical frozen 05 enforcement proof.

Raw public events, receipts, approval decisions, prompts and summaries are
retained locally in separate immutable repair snapshots:

- [macOS: 361 files](../../../.test_tmp/native-followup-20261112/platforms/macos/retained-repair08-01/EXPORT-MANIFEST.json), archive SHA256 `e5567b9b3525d7205635676e715da5ac8ed89ddd43ca7b794de9fc27bbd4f641`.
- [Ubuntu WSL: 350 files](../../../.test_tmp/native-followup-20261112/platforms/linux/retained-repair08-01/EXPORT-MANIFEST.json), archive SHA256 `5a0bdd0b557c06f5a395d9c8b7e5ce21f274cb9bc6c9f3ecd6112a4b111c8e8b`.

Every exported file hash was verified. Selected credential-literal scans found
zero matches; databases, private credentials and authentication profiles were
excluded. Final source/config/workspace/storage-authority guards passed for all
eight affected fixtures. Read-only process audits found zero members for the
11 recorded primary source groups on each host. The established gate helper
retains no native PID; its separate exact project/executable scan found zero
processes, and is reported as an observation rather than an invented PID proof.
Both primary provider leases were released after completion.

The focused stage-policy checks cover bounded discovery, exact stage writes,
unplanned gateway mutations, direct/gateway counsel retry and outcome proof,
immediate unsupported-call denial, unchanged global-eval denial, minimal index
setup, and create-only POSIX repair evidence/baseline routing. Eleven distinct
checks passed across the original focused run and affected new-test runs;
scoped Ruff checks passed. These helper checks occurred after full repository
suite collection and are reported separately. External quota/account blockers
and previously unsupported capabilities were not retried by this repair.


## Repair 09: targeted Mac V1 follow-up

The later [repair 09 results](../../../.test_tmp/native-followup-20261112/platforms/repair09-platform-results.json)
retain exact selected-summary hashes and explicitly carry forward successful
historical stages. No repair 08 attempt or classification above was overwritten.
Only the reviewed production description-preservation change was exercised;
OMP's unrelated guided-order failures were not rerun.

| Native host/client/source | Selected initial | Selected mutation | Selected removal | Restart | Latest strict workflow |
|---|---|---|---|---|---|
| macOS / OpenCode V1 / Python | Passed 09 | Passed historical | Passed historical | Passed historical | Passed after targeted repair |
| macOS / OpenCode V1 / TypeScript | Passed 08 | Passed 09 | Failed 09 | Blocked | Failed |

Before repeating the previously failed mutation, one actual native
`index_setup_clean` indexed the verified original 269-file source at generation
3. The same owned mutation then passed at 270 files, generation 4. This is an
observed clean-index-to-new-generation transition, not reuse of the earlier
failed mutation's already-indexed probe. The clean prerequisite is reported
separately from source workflow turns. Its focused test requires the original
file count and rejects the mutated count; scoped Ruff passed.

The Python initial replay passed all strict memory, automatic briefing,
instruction-marker, indexing and known-caller checks. TypeScript mutation also
passed strictly. TypeScript removal completed its functional indexing, known
callers and stale-result checks, but still issued string-valued numeric
arguments and an unrequested `code_todos_scan`. Its strict failure was preserved
and the exact owned process was boundedly stopped with a separate reason
receipt. Restart was blocked. This was the second supported production repair;
no third retry was attempted.

Both hosts received a fresh isolated frozen 09 runtime for the coordinator's
separate lanes; there were no WSL primary model calls here. All 245 installed
package files matched wheel SHA256
`a6c371a0c1a5b2f69f6e68b04d16caa5c77a2a5b1fc4689915b2e6fe6b571010`,
with no extra package files. Mac V1's consistent database backups preceded the
guarded server-only retarget. Covenant, sidecar, hook/plugin bytes, bridge
credential hashes and runtime paths remained unchanged: server 09, hooks 08.
Workspace and storage authority remained the same. No automation reinstall or
credential rotation occurred during this follow-up.

The [separate public snapshot](../../../.test_tmp/native-followup-20261112/platforms/macos/retained-repair09-01/EXPORT-MANIFEST.json)
contains 51 allowlisted files, all hash-verified, with zero selected credential
literal matches. Archive SHA256 is
`732897295be9517e35f48adad3a6e4cdc8b9a513c39d6fd5dc817e61011eb4e8`.
The [final audit](../../../.test_tmp/native-followup-20261112/platforms/macos/repair09-final-source-authority-process-audit/stdout.log)
verified restored frozen source, absent added probes, unchanged reviewed
configuration/workspace/storage authority/bridge credentials, and zero members
of all four exact owned native groups. The primary provider lease was released
for the separately reported V2 lane.


The later [focused pass receipts](../../../.test_tmp/native-followup-20261112/platforms/focused-tool-output-receipts/manifest.json)
retain the actual command and verbatim tool-result output for the 13-case
observed-remedy/stage-policy pass and the separate one-case clean-index pass.
They explicitly map the five requested POSIX/minimal/clean setup cases. These
are transcript-derived tool-output receipts, not newly generated stdout captures
or JUnit. Current source hashes are labelled as retention-time hashes; historical
invocation-time hashes were not captured. No test or model rerun was performed
to create these receipts, and they do not imply a single full-final-tree run.


## Repair 10: latest targeted POSIX follow-up

The additive [POSIX repair 10 report](posix-repair-10-2026-11-12.md) records all
four OMP restart passes, Mac V1 removal/restart passes, and its retained strict
setup failure from one extra scoped health call. The required setup qualification
keeps the overall Mac V1 row conservatively failed. Exact frozen profiles, raw
public evidence, unchanged acceptance contracts and final guards are linked there.


## Repair 11: resolved Mac V1 setup qualification

The additive [POSIX repair 11 report](posix-repair-11-2026-11-12.md) records the
original setup passing after coherent owned-plugin and Covenant refresh, plus
necessary strict cleanup. The setup prompt stayed byte-identical; all raw native
tool/error events were audited. Frozen 10 removal/restart passes are carried with
their original versions, while the earlier setup failure remains historical.
