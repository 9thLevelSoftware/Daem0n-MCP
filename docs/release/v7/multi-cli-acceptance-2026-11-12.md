# Native CLI integration research and acceptance

Date: 2026-11-12. Base: merged PR #77,
`54c16139aa5b5557d9a2af385f17e49e952da0e1`.

This report distinguishes a documented configuration route, installer regression
tests, and a real model using Daem0n through its host's native MCP implementation.
Host support is specific to the versions below. This is an installation and core
memory workflow pass, not acceptance of every optional Daem0n feature or host UI.

## Research and implementation

The five installed products all provide native MCP. The new `install-client`
command writes only the selected project's native configuration and a short
managed covenant in its effective instruction file. It preserves unrelated
configuration, comments, instruction text, and permission settings. It never
changes provider credentials or grants project trust.

| Client | Researched installed version | Project MCP configuration | Important difference |
| --- | --- | --- | --- |
| Codex | 0.167.0 | `.codex/config.toml` | Project trust controls loading; nonempty overrides and configured instruction fallbacks have precedence. |
| Antigravity CLI (`agy`) | 1.4.7 | `.agents/mcp_config.json` | This is Antigravity CLI, not an assumed Gemini CLI alias; tools may be exposed through lazy discovery. |
| OpenCode | 1.18.21 | `opencode.json(c)` or `.opencode/opencode.json(c)` | V1 and V2 use different schemas; this installer targets V1 and requires the observed version. |
| Oh My Pi (`omp`) | 18.16.0 | `.omp/mcp.json` | Imports other hosts' MCP configurations; verify the selected source. Timeout units are milliseconds. |
| Earendil Works Pi (`pi`) | 1.0.0 | `.pi/mcp.json` | Distinct from OMP; project MCP loading requires trust. Timeout units are seconds. |

Detailed primary-source research:

- [Codex and Antigravity](../../research/cli-hosts-codex-agy-2026-11-12.md)
- [OpenCode V1/V2](../../research/cli-hosts-opencode-2026-11-12.md)
- [Oh My Pi and Pi](../../research/cli-hosts-omp-pi-2026-11-12.md)

The common launcher fixes the Python interpreter, ignores Python import
environment settings before startup (`-E`), and removes the current directory
from the module search path. This matters for a project containing a shadow
`daem0nmcp` package or `sitecustomize.py`. OMP discards empty environment values,
so an empty `PYTHONPATH` override by itself is insufficient.

The installer previews changes without creating storage, refuses malformed,
linked or conflicting targets, keeps private backups, and reports changed files
even if a later publication or cleanup step fails. Installation is idempotent.
It does not add native lifecycle hooks or legacy edit-bridge pairing for these
five clients. Guided memory calls such as `memory_recall` can automatically brief
an unbriefed session. Diagnostic and discovery calls do not trigger briefing;
proactive host reminders are a separate integration feature.

## Disposable setup

All acceptance fixtures are beneath the ignored local directory
`.test_tmp/multi-cli-20261112/acceptance/`. Each host receives its own project,
storage, and test configuration. Unrelated MCP entries, existing instruction
text, and JSONC comments act as preservation sentinels. Agy's final account-mode
run uses the normal shared OS keyring; its authentication is not isolated.

A new Python 3.12.14 environment installed the source with
`[local,graph,apps,models-local]`. Dependency installation and `pip check` passed.
An isolated interpreter provenance check resolved this staged source and
reported `7.0.0.dev0`. A first local embedding call returned 1,024 bytes using a
dedicated model cache. This preparation establishes the backend, not host
acceptance.

The live test requires the actual host's native MCP manager and a real model:

1. Load the installed project configuration and covenant, resolve the actual
   tool names, and use the correct opaque workspace ID.
2. Obtain the full brief and health result, then store and recall a synthetic
   decision using a stable idempotency key.
3. Record an outcome and use gateway discovery to archive a separate synthetic
   probe. Verify `COUNSEL_REQUIRED`, read the returned counsel, and replay the
   exact remedy in the same MCP session; restore that probe afterward.
4. Verify local semantic readiness, restart the host, recall the original
   decision, and repeat its write key without creating a duplicate.
5. Reinstall without changing the generated files or preservation sentinels;
   audit monitored original user files, credential handling and owned processes.
   Report exceptions rather than overwriting user files to restore a baseline.

A zero exit status, connected server list, or model's unsupported claim of
success is insufficient. The report uses tool events and returned record IDs.

## Results

The frozen installer passed independent review after correcting startup import
isolation, timeout units, instruction precedence, directory-swap safety, and
partial-write reporting. The installer used for those filesystem regressions had SHA-256
`a02ccb571deb43643ec48f995570b7a36e75e8b0219ef46bd563431096f00f7d`.

Live Pi testing then confirmed that scoped `system_health` is intentionally
exempt from briefing. A prose-only correction aligned the maintained guides,
generated covenant, server instructions, and active client mirrors with the four
guided memory-call triggers. Independent review approved that correction;
five affected tests and eight subtests passed. The resulting installer SHA-256 is
`758728f465f5606744b34df23a44760e7908e68a48550eaffc2e656ac73ed5a1`;
`fastmcp.py` is
`ab1d9b8234c658af3959ddd586041eaa57705f506a01a0b074560152ac97fbeb`.
No ceremony or diagnostic runtime behavior changed.

- Windows Python 3.12.14: all 33 affected installer cases passed across the first
  run and a one-case correction to a cleanup fault-injection fixture. Four
  POSIX-only cases were skipped on Windows.
- Ubuntu/WSL Python 3.12.15: all 40 selected installer cases passed, including
  publication and cleanup directory swaps, anchored parent creation, and three
  symlink cases skipped on Windows. Outside sentinel files were unchanged. This
  was an isolated source copy with the exact reviewed installer/test hashes,
  not native Linux client acceptance or a Linux run of every repository change.
- Focused CLI/document checks passed: 19 tests and 42 subtests, followed by the
  added human-readable partial-failure regression. The full suite includes
  the revised launcher examples.
- Ruff lint and formatting passed after import ordering/line wrapping fixes.
  The v7 type check passed on 48 source files. Release inventory passed with
  77 tools, 10 resources, and 588 atomic requirements.

### Native model workflows

All five installed Windows clients completed the core workflow through their
native MCP implementation. These are actual structured tool responses, not
model-written success summaries.

| Client | Native model used | Authentication route | Core workflow and restart |
| --- | --- | --- | --- |
| Codex 0.167.0 | `gpt-6.1-sol` | Documented experimental app-server external ChatGPT access-only flow, no refresh material supplied | Passed |
| agy 1.4.7 | `gemini-3.8-flash-low` | Normal consumer Google-account login through shared native keyring | Passed |
| OpenCode 1.18.21 | `minimax-coding-plan/MiniMax-M3` | Existing MiniMax API key in the test child environment | Passed |
| omp 18.16.0 | `minimax/MiniMax-M2.7` | Existing MiniMax API key in the test child environment | Passed across initial and continuation stages |
| Pi 1.0.0 | `minimax/MiniMax-M2.7` | Existing MiniMax API key in the test child environment | Passed |

Each passed health, full brief, store/recall, gateway use, exact same-session
counsel remedies, outcome recording, probe restoration, restart persistence,
same-key replay with the original record/event identity, automatic briefing on
a fresh protected recall, and a ready local dense provider with real candidates.
Final same-version installation preserved generated bytes and unrelated project
sentinels. The one-time covenant wording update is recorded separately.

Instruction use was observed. Agy's native events include an explicit read of
project `AGENTS.md`; that proves use, but does not independently establish
automatic preloading. Pi's native system context and the other clients' unique
pre-existing instruction sentinels provide their respective evidence. The
installer and grimoire still require checking effective instructions per host.

All five disposable databases passed SQLite integrity and the official offline
authority verification. That does not mean every derived projection was current:
Codex's final report has two local rebuild notices after its last outcome, and
agy's has five; Pi, OpenCode and OMP have zero. The aggregate reports do not
identify each notice's kind and reason. Dense readiness with two candidates was
observed on restart before the final outcome. Do not classify the notices by
guess or describe the whole projection inventory as healthy. No repair was used
to erase these notices.

Important unsuccessful attempts remain in the evidence:

- Codex's initial harness denied a native approval. A reviewed one-shot retry
  succeeded; one expired counsel token was replaced by a fresh server challenge.
- Pi's first harness disabled native MCP wrappers. Restoring those native
  facilities fixed discovery. A separate one-recall fresh process proved
  auto-brief after the first restart model chose an explicit full brief first.
- OpenCode's first provider returned HTTP 402. Its next model name was absent
  from the native effective catalog; diagnostic logging identified that error,
  and the suggested native `MiniMax-M3` worked. Malformed model gateway arguments
  were rejected before corrected exact remedies succeeded.
- OMP's first driver confused a presentation notification with an approval.
  Its corrected native RPC handling retained individual decisions. The initial
  15-minute stage ended before outcome recording; a continuation completed only
  the unfinished outcomes and planned restart. Sixteen individual approval
  decisions were audited; no blanket session grant was used.
- Agy's API-key route returned HTTP 402 `RESOURCE_EXHAUSTED` for depleted
  prepayment credits before any Daem0n call. Its later normal Google-account
  route passed. These are separate billing routes, not contradictory balances.
  Native `agy mcp list` reported no servers even though project initialization
  cached nine schemas and later native calls worked; that listing is not
  credited as project-discovery proof.

### Full suite and affected checks

The one full local run collected 4,130 tests and finished with **4,042 passed,
83 skipped, five failed**, plus 2,099 passing subtests, in 2,115.20 seconds. Its
five failures were addressed with focused verification:

- The launcher golden did not allow the new `-E -u -X utf8` prefix around the
  existing safe bootstrap. The expected shape was updated; the affected test
  passed without accepting arbitrary launch commands.
- Linked-project recall failed before lexical readiness; retained state showed
  the real five-second coalescing delay. The exact swallowed failure stack was
  unavailable.
  The fixture now quiesces its two databases, drains real projections and
  asserts lexical readiness before its unchanged recall assertions. The
  affected test passed; both databases close in `finally`.
- Three OpenCode template comparisons loaded constants while maintained prose
  was changing. All three passed against the final consistent snapshot.

A CLI test's environment cleanup was also corrected so a missing original
`DAEM0NMCP_PROJECT_ROOT` is restored properly. Its affected check passed.
The full run plus these focused resolutions is not a fresh final-tree full-suite
pass. Hosted CI on the eventual PR is a separate gate.

### Projection scheduling defect found during diagnosis

The linked-project investigation also retained a real procedure job in
`dead_letter` with `PROJECTION_BUILDER_UNAVAILABLE`. The lightweight commit
drain registered only a lexical builder, but its queue claim could take any due
job. If lexical work was delayed for coalescing, that partial worker could claim
a due procedure job and permanently reject it even though the full worker had
the required builder.

The correction restricts lexical-only claims inside the existing transaction,
before lease or attempt mutation. Optional jobs remain for the full worker;
genuine unavailable-builder handling in that full worker is preserved. The
base-profile runner factory uses the same lexical-only default. It does not
rewrite existing dead letters or user databases.

A deterministic test reproduced the original dead letter before the fix.
Focused job/runtime coverage then passed 49 tests across the initial 48 passing
cases and one corrected negative-fixture rerun. Coverage includes mixed due
work, delayed lexical work, untouched expired optional leases, payload
validation, full-worker unavailable errors, cancellation and lease handling.
Scoped Ruff and whitespace checks passed. Final source SHA-256 values are
`4eef50546c44efe27af4afd463b30591117f445ea25ff2a15f61f94a7aded0ef`
for `retrieval/jobs.py` and
`f1522e695245060f14eec42bb24146d4b5dece259f96f3148de6ffa436421461`
for `retrieval/runtime.py`. The negative fixture originally used invalid JSON
that the database correctly rejected before scheduling; the corrected fixture
uses valid JSON with an invalid field type. No production fix was changed to
accommodate that fixture.

The native host workflows used the staged runtime before this final scheduling
correction. Their installer and protocol evidence remains applicable, while
the scheduling change requires its own focused regression evidence; it is not
credited as a repeated five-host native run.
The staged Claude `session_start.py` also retains the earlier reminder wording;
only its prose differs. These five native registrations did not install or use
Claude hooks. The final Claude reminder was covered by its affected local test,
while the native clients used the final generated covenant/server instructions.

### Evidence and preservation

Local evidence is retained beneath the ignored acceptance directory above:

- `evidence/final-review/index.json` links per-host native events, validations,
  idempotence, offline verification and failed attempts.
- `evidence/final-review/native-core-receipts.jsonl` holds normalized actual v7
  envelopes: 115 rows representing 114 unique responses across the five hosts,
  reviewed against the original native events.
- `evidence/final-review/host-matrix.json` records the bounded per-host checks;
  `final-staged-source-manifest.json` identifies the native-test source snapshot.
- `evidence/final-review/frozen-evidence-manifest.json` freezes the five-host
  evidence files (SHA-256
  `075576cf974cfd587fa57091b89cf5d1b2968d71c90ba51f5d2867aacc2cc898`).
- The parent `.test_tmp/multi-cli-20261112/` retains `full-tests.log/xml`,
  `linked-recall-ready.log/xml`, `post-sync-templates.xml`, wording checks and
  installer Windows/Linux evidence. These private artifacts are not committed.

Seventeen of eighteen monitored original user files matched their baseline.
Antigravity's `app_storage.json` changed, with no added or removed top-level
keys. Only baseline hashes were retained, so changed values and attribution
cannot be established. A live UI or the isolated CLI version probe may have
caused it. The file was never restored or overwritten. Later agy live commands
each preserved their immediate pre-run monitored files, which does not erase
the earlier exception or prove shared keyring values unchanged.

Selected credential-literal scans found no matches in owned evidence, drivers
or profiles. No whole credential store was copied. Agy's normal keyring could
perform its own refresh; credentials were not extracted, copied or logged out.
The user's saved Gemini API key was retained. Completed native processes and
owned descendants were checked for closure.

## Boundaries

- Agy's Google AI credits apply to account-mode overages, enabled by **Use AI
  Credits** in `/settings` or `/config`; `/credits` shows balance and `/usage`
  shows quota. The final test enabled this only in private test settings. No
  credit debit or balance was measured and no global spending preference was
  changed. A Gemini API key is not required for ordinary installation.
  [Official credit guide](https://antigravity.google/docs/cli/credits),
  [authentication guide](https://antigravity.google/docs/install/).
- Codex acceptance uses the publicly documented experimental app-server
  external ChatGPT access-token flow, with in-memory authentication and refresh
  refused. This is a test harness choice, not an installer requirement or proof
  of an interactive TUI session.
- No expired OAuth credentials were refreshed from copied profiles. The agy
  account route deliberately uses its normal shared keyring; OpenCode, OMP and
  Pi use existing API-key provider routes.
- MCP Apps rendering, native MCP resource browsers, optional native lifecycle
  hooks, OpenCode V2, hosted embedding providers and E2B execution remain
  separate acceptance scopes. The synthetic projects contain no indexable
  source code; these runs do not certify code indexing.
- Windows native host results do not establish native Linux/macOS client
  acceptance. Linux filesystem regressions and CI matrix results are reported
  separately.
