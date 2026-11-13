# Windows native acceptance, 2026-11-12

Codex and agy passed all four real-source stages for both projects. Other clients
proved substantial capabilities but retain failed turns or an external quota
block. A passing individual capability below does not erase an unsuccessful
native turn.

The structured evidence is
[matrix.json](../../../.test_tmp/native-followup-20261112/windows/matrix.json).
It records every attempt, original and offline-revalidated verdicts, exact
checks, source revisions, native versions, available backend provenance paths,
explicit missing-attempt-digest statuses, and summary
hashes. Regenerate without model calls using
`python scripts/native_windows_matrix.py`. Raw events and native completion
receipts remain in the fixture evidence directories linked from that inventory.

## Scope and isolation

These are Windows 11 build 26200 native binaries. Linux and macOS results are
reported separately in [platform acceptance](platform-acceptance-2026-11-12.md).
The sources are immutable tracked blobs, independently exported for every pair:

| Project | Revision | Indexed files | Known real function / callers |
| --- | --- | ---: | --- |
| Daem0n-MCP | `4a96e8a8056c1b1d5cd1ca0f63c9f95f7e6929b5` | 225 Python | `lexical_fts_table_name` / `LexicalProjectionBuilder.rebuild`, `_drop_generation` |
| legion-cli | `581f1f5255f4ae3364750bab43eb34eb053e4a49` | 269 TS/TSX | `sha256utf8` / `fingerprintHash`, `fingerprintRoot` |

Final fixture roots are outside the repository, under
`D:/Daem0n-Native-Acceptance-20261112/windows/`; Claude uses the separate
`windows-claude-retry/` root. Each has its own Git root, backend database, native
configuration, and private home. Ancestor OpenCode plugin discovery made this
outside-repository boundary necessary; an independent Git root alone was
insufficient. Earlier provisional attempts beneath the repository remain
historical evidence.

Backend environments use Python 3.12.14 and immutable installed wheels. Completed
Python Codex/agy stages used freeze 01
(`38475f299dc66918a645760dfaea633b7e6883f1dc0901468a39dbc1b6155c65`).
Later stages and corrected TS indexing used freeze 02
(`6d9939417043b705631c03342095c5af7c2534c7cf40592b85a0a604b35b64da`).
Controlled rebind receipts retain configuration hashes and unchanged authority
roots. Most attempts also retained a backend package source digest. Six early
attempts did not: Codex/Daem0n `initial-1`, `initial-2`, and `mutate-1`;
agy/Daem0n `initial-1`; and Pi/legion `initial-1` and `index-continuation-1`.
Their matrix provenance is explicitly null with
`missing_attempt_digest`; retained fixture configuration still establishes the
frozen-runtime association, with a weaker per-attempt qualification. No digest
was reconstructed after execution. These receipts do not establish execution
against a subsequently rebuilt wheel.

No original user configuration or credential file was changed by these runs.
Selected existing API keys and Codex access material were supplied only in
process memory. Claude used the newly authenticated, isolated native account
profile with its normal native refresh behavior. No OAuth refresh store was
copied. Final hashes prove all 12 exported sources restored, all added probes
removed, and original user files unchanged:
[final-preservation.json](../../../.test_tmp/native-followup-20261112/windows/final-preservation.json).

## Native stage outcomes

Initial means preloaded instruction marker, guided automatic brief, explicit
brief, health, store/recall/outcome, index, unchanged rerun, known search and
actual callers. Add/edit appends a function and creates a second file; remove
restores source and removes that file. Restart verifies original memory record
and event replay plus persisted code without rebuilding.

| Native client | Source | Initial | Add/edit | Remove | Restart | Pair outcome |
| --- | --- | --- | --- | --- | --- | --- |
| Claude Code 2.1.295 | Daem0n | failed | failed | blocked | blocked | **blocked** |
| Claude Code 2.1.295 | legion | blocked | blocked | blocked | blocked | **blocked** |
| Codex 0.167.2 | Daem0n | passed | passed | passed | passed | **passed** |
| Codex 0.167.2 | legion | passed | passed | passed | passed | **passed** |
| agy 1.5.7 | Daem0n | passed | passed with recovered timeout | passed | passed | **passed** |
| agy 1.5.7 | legion | passed | passed | passed | passed | **passed** |
| OpenCode V1 1.18.21 | Daem0n | passed | passed | failed | passed | **failed** |
| OpenCode V1 1.18.21 | legion | passed | passed | failed | passed | **failed** |
| OMP 18.16.0 | Daem0n | passed | failed | failed, prompt assisted | passed, prompt assisted | **failed** |
| OMP 18.16.0 | legion | failed | failed | failed, prompt assisted | failed, prompt assisted | **failed** |
| Pi 1.0.0 | Daem0n | passed | passed | failed | failed | **failed** |
| Pi 1.0.0 | legion | failed; index continuation passed | passed | failed | passed | **failed** |

The selected models were Claude Haiku, Codex `gpt-6.1-sol`, agy
`gemini-3.8-flash-low`, OpenCode `minimax-coding-plan/MiniMax-M3`, and OMP/Pi
`MiniMax-M2.7`. Exact native arguments are retained per attempt.

Specific boundaries and failures:

- Claude's Python initial and mutation turns passed every functional assertion,
  including real callers and both mutation probes. Each also made an erroneous
  `daemon0nmcp` root call, which the backend rejected. Their strict failed
  verdicts remain. Its next native response was “weekly limit,” resetting
  November 14 at 11pm America/New_York, with zero tools. Removal, restart, and
  all legion operations are blocked by that account limit; no quota retry or
  purchase was attempted. `initial-1` also retains an earlier connecting/zero
  tool startup race. The persistent SDK connection/readiness barrier allowed
  the subsequent actual tool turn.
- Codex's first Python attempt found the instruction marker but began before
  its native MCP manager finished connecting. A native server readiness barrier
  fixed that harness race; `initial-2` and subsequent stages passed. No custom
  MCP connection substitutes for the client's MCP manager.
- agy's Python mutation `code_index` first returned `DEADLINE_EXCEEDED`
  (`req_cI3P5izznbyBLFdULZNldfed`). A later native completion in that same
  process succeeded with identical workspace, operation and arguments. All
  functional assertions passed. The original failed summary remains; its
  offline successor explicitly records the exact recovery mapping. No other
  error type receives this exception. Large agy outputs omitted from stream
  JSON were recovered from the native conversation/step output artifact with
  completion identity, source hash, and bounded-path provenance; raw events
  were retained unchanged.
- OpenCode's Python removal made 23 invalid argument calls and did not finish
  the caller assertion in that turn; both stale probes were empty. The later
  restart passed the known callers and all stale/persistence assertions.
  Legion removal passed every functional assertion but also made seven
  invalid argument calls. Both removal turns remain failed.
- Pi's first legion indexing hit an isolated-profile parser cache error;
  explicit existing private cache directories corrected that host setup.
  Index continuation then exposed the real missing TS call relationships.
  Freeze 02 supplied the reviewed TS relationship fix; its actual native
  continuation indexed 269 files, advanced generation, remained unchanged on
  rerun, and returned both expected callers. Earlier failures remain.
  Legion removal passed functional assertions but an unsolicited
  `session_brief` timed out. Python removal passed functional assertions but
  made an invalid argument call. Python restart replayed original memory and
  found the subject before rebuilding, then violated the requested no-rebuild
  stage; full restart-code acceptance remains failed.
- OMP's initial core and real indexing assertions passed for both projects.
  Legion's initial overall verdict also retains a denied unplanned eval
  import attempt. Later models omitted required workspace IDs or proposed
  unplanned writes. Those exact calls were denied. One authorized prompt
  repair supplied explicit JSON arguments: both projects then passed every
  removal functional assertion and every restart functional assertion.
  Python restart completed cleanly; other repaired turns proposed additional
  noncanonical or changed-key outcome writes and were denied, so their strict
  verdicts remain failed. No further prompt repairs chased a clean verdict.

The twelve capability maps in `matrix.json` distinguish these successful
assertions from strict pair outcomes. In particular, Claude has no demonstrated
removal/restart in this lane; OpenCode Python lacks a complete removal-stage
caller assertion; Pi Python lacks a compliant no-rebuild restart.

## Instructions, resources and automation

Each fixture received a unique marker in its actual host instruction file. The
initial model prompt did not name that file or supply the value. All exercised
initial pairs reported their exact marker before tools. Claude legion remains
unexercised. The stronger agy positive/negative control with zero filesystem
tools is reported in [automatic loading](automatic-loading-2026-11-12.md).

Codex's actual app-server inventory exposed all four bounded workspace resource
templates. Native reads passed for the own workspace, a foreign workspace was
denied, and repeated reads returned fresh response metadata for both sources.
See each successful Codex attempt's `native-resources.json`. Its earlier
connecting attempt's resource failure also remains. A separate new fixture then
passed an actual changed-content check on freeze 03: native `session_brief`,
same-session warnings read, one exact synthetic warning write, and the same URI
read again. The returned record
`mem_7cfdf67ffc9771ce1020b491aff0d9dd5e7b40aad5c0783ea8da2062d70b5be3`
was absent before and present after. There were exactly two native MCP tool
receipts (brief and store), no core/index repetitions, no errors, unchanged
original user hashes, and confirmed owned-process cleanup. The actual native
server inventory advertises `subscribe=false` and `listChanged=false`, so
subscriptions are **unsupported**, separately from the **passed** changed-content
read. See [resource change evidence](../../../.test_tmp/native-followup-20261112/windows/codex-resource-change.json)
and `scripts/native_codex_resources.py` for the reproducible native driver.
Other clients' resource surfaces are
reported separately and are not inferred from the backend's protocol support.

A separate agy native resource attempt then **passed** static-resource discovery,
all four workspace JSON reads, foreign workspace denial, and an empty-to-populated
warnings reread around one exact warning write. The exact record is
`mem_0c06c7bbeed2cf52a1a49f5d7aff27ccb6a9f50353a2a13da98834e14bfda19e`.
Its stream omits resource calls as `unknown` steps, so verification binds the
native saved call arguments, matching native completion, and hashed output file
at the exact conversation/step. The original stream-only failure remains and
the correction used zero further model calls. See
[agy resource evidence](../../../.test_tmp/native-followup-20261112/windows/agy-resources.json).
The observed `list_resources` returns six static UI resource URIs; the native
catalog exposes no template-listing helper. Discovery of the four parameterized
workspace templates is **unsupported in that observed headless surface**;
reading those parameterized URIs is proven. Listing UI resources does not prove
UI rendering.

[Residual surface evidence](../../../.test_tmp/native-followup-20261112/windows/residual-resource-surfaces.json)
also establishes that Claude 2.1.295 has typed native list/read resource
contracts, and OpenCode V1 1.18.21's installed `SessionTools.resolve` registers
`list_mcp_resources`, `list_mcp_resource_templates`, and `read_mcp_resource`
when connected servers advertise resource support. Claude's actual resource
calls are **blocked by account quota**. Neither client is classified as wholly
unsupported from an absent CLI subcommand or a restricted tool selection.

OpenCode V1's subsequent actual resource probe **passed** static discovery,
all four template discoveries, workspace reads, changed-content reread, and
foreign workspace denial in one native session. The exact warning record is
`mem_adf728678333b52fb17fe2b439f633f78f656b7e9eca59913ed5aee4387376de`.
Discovery occurred before the explicit brief; every workspace JSON read followed
that brief. The first bounded attempt had incorrectly permitted the individual
helper names while globally denying generic `read`; installed native source
proves that those resource helpers map to `read` and were filtered before the
model saw them. It timed out after unplanned synthetic store probes; its
records and failed verdict remain. Exactly one corrected fresh-fixture attempt
allowed only `mcp:daem0nmcp:*` read patterns while retaining a default deny for
filesystem reads. It completed in 20 seconds with ten native tool completions.

The native V1 wrapper returns a generic foreign-read failure; its own retained
log records the matching foreign URI, Daem0n server name, and MCP error -32002
`Resource unavailable`. An offline assessment binds that native error line and
log hash to the exact native completion; the original string-matching failure
also remains, with zero further model calls. See
[V1 resource evidence](../../../.test_tmp/native-followup-20261112/windows/opencode-v1-resources.json)
and [permission diagnosis](../../../.test_tmp/native-followup-20261112/windows/v1-resource-permission-diagnosis.json).

Installed automation modes were enforce for Claude and OpenCode V1, remind for
Codex, agy, OMP and Pi. Installation preview/apply/repeat preservation is retained
in each `fixture.json`; mode configuration alone is not invocation proof.
Actual enforcement, reminder event evidence and unsupported tool surfaces belong
to [native automation acceptance](native-automation-acceptance-2026-11-12.md).
Pi's legion initial stream does independently contain native
`daem0n-covenant-reminder` custom message events. OpenCode final runs did not use
`--pure`, which disables external plugins. Mediation remains declared native
edit-tool coverage, not a shell sandbox.

## Harness verification

Portable helpers retain native execution identities, untouched v7 envelopes,
exact counsel remedies, source manifests, replay IDs, and original error
verdicts. Gateway data is unwrapped only when its executed tool matches the
response. OMP and Codex approvals validate exact observed one-call payloads;
no session-wide grant is issued. Native denials remain failed even if later
functional checks pass. Processes use owned Windows kill-on-close jobs or
bounded POSIX process groups; timeout cleanup does not target unrelated hosts.

The original 13 scenario cases passed in their planned focused checks. Seven
additional cases passed once as introduced: exact OMP frame/scope, index-only
prompt, gateway/component-health payloads, nullable covenant, denied-call
retention, explicit OMP prompt repair, and missing-outcome continuation. Final
scoped Ruff lint/format and repository whitespace checks passed. One further
targeted resource regression passed, rejecting a different record, foreign URI,
or changed write scope. The agy artifact regression also passed, rejecting model
prose or a proposed call without its matching native completion. Three V1
resource checks passed: exact write/record identity, URI-bound native error-log
evidence, and nonempty matching native session identity. The newest test fixture
initially omitted its native call ID; that fixture was corrected once and the
affected check passed. Full repository
tests, Linux mypy, release inventory, hosted CI, and later wheel qualifications
are reported by the coordinator separately.
