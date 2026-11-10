# Daem0n-MCP v7 completion ledger

Source: user-supplied v7 Completion and Release Plan, 2026-09-16.
Base: cc08b4f696b6226e88cca1f83104ce870dd62ae6; v6 baseline: 00809c67c03938014ac3ea470ef3600f7ccebabc.
Worktree: D:/Daem0n-MCP-v7-completion; branch: v7/completion.

## Contract
All 71 existing tools plus edit_preflight, memory_capture_list, memory_capture_promote, workspace_consolidation_preview must work through the production MCP server. Preserve event authority, opaque workspace IDs, relative public paths, exact authorization, idempotency, small core and optional profiles. Support desktop and one remote server, no replication. No release claim until every requirement has evidence from the final commit. Missing infrastructure means incomplete verification, never a passing skip.

## Work packages
P0 inventory, real stdio/HTTP harness, baseline, CI, frozen migration/relevance fixtures.
P1 bounded foreground execution (15s default, 1-60s), FastMCP 3.4.7/Python 3.10, dedicated durable admitted-ID task dispatcher, credential-free queue, scoped lifecycle, outbox recovery, replay safety, truthful cancellation.
P2 whole-store verify-v7, offline candidate projection repair, migration coverage/inspection, retained-generation rollback, recovery commands and failure injection.
P3 convergent projection slices/leases/dead letters, generation/config cached retrieval lifecycle, authorized bounded federation with origin identity, actual provider validation.
P4 code impact edges, entity backfill, atomic community generations/resolution, pinned no-redirect URL ingest, E2B isolation, preview-bound consolidation and recoverable managed-storage archive.
P5 versioned vector and actual legacy projection exports/imports, canonical validation and compatible event-only bundles.
P6 exact native-edit approval broker, one-use 120s receipts scoped to principal/MCP/host sessions and preimages, local protected IPC and remote authenticated HTTPS pairing; Claude/OpenCode V1/V2 adapters; reviewed capture/promotion.
P7 four production dreaming strategies, canonical writes/review candidates, bounded workspace lifecycle, idempotency and diagnostics.
P8 six authorized MCP dashboard shells, four JSON resource templates, registered metadata and packaged assets, real resource/client verification.
P9 TLS proxy/Host/Origin/JWT security, quotas, production attack-boundary tests, redacted diagnostics, real sandbox staging.
P10 wheel/sdist core and profile installations, accurate complete docs/examples, generated mappings, RC and final evidence.

## Dependency/interface review
| Packages | Interface and finding |
|---|---|
| P0/P1 | Harness consumes exact production server; keep baseline evidence separate from changed code. |
| P1/P2/P4/P6 | Admission, capture and consolidation metadata need migration coverage; additive schemas and explicit ownership required. |
| P1/P3/P7/P9 | Shared production lifecycle; serialize composition-root edits. |
| P3/P4/P7 | Discovery/projection generations support advanced tools and dreaming; stabilize before dependent wiring. |
| P4/P5 | Consolidation transforms identity; ordinary import preserves identity. |
| P6/P8/P9 | Same authenticated workspace boundary applies to bridge and resources. |
| P0-P10 | All package texts internally consistent; broad work packages must be decomposed into bounded reviewed changes. |

## Release gates (not yet accepted)
Real MCP calls for every tool/argument/profile; ritual through restart; local/remote native edit and capture workflows; dashboards/dreaming/tasks/previews. Persistence interruption, disk/lock/permission/corruption failures. Python 3.10-3.12 Windows/macOS/Linux. Actual local/remote Qdrant, embeddings/ONNX, Redis/Valkey, E2B and both clients. Full Python/JS/Ruff/packaging/blocking changed-surface type checks. Reference 8-core/32GB/SSD benchmark: 100k memories/1m events; warm 4-way lexical p95 <=500ms, hybrid <=2s; write visibility lexical <=5s, specialized <=60s, dense <=5min; migration <=30min excluding dense; Recall@10/nDCG@10 regression <=0.02; 8h mixed soak without resource accumulation. Publish cold start, initial indexing, disk and stress evidence.

## Progress
Accepted final-release requirements: **0 / 571**. The current inventory contains
75 tools and 10 resources. No package has final-commit acceptance. Counts include
surface requirements and explicit package scenarios; passing component tests
alone is not release acceptance. The dated entries below preserve development
history and can describe findings repaired by later entries.

## Current local certification checkpoint - 2026-09-17

- This is evidence from the uncommitted `v7/completion` tree based on
  `cc08b4f`; it is not RC, final-commit, or release acceptance.
- Development artifacts in `.tmp/final-dist-final3` passed Twine and
  required-asset checks. The wheel is 1,081,772 bytes with SHA256
  `641D8E19905FBC2164803FCCCC780C8B7B6565AD12D80D110A0BB9E7F9D71CA5`; the
  source distribution is 1,422,044 bytes with SHA256
  `1DDD4206412DAF51E3724AA20308638A1B332166270826BB756827E83B8FBE2E`.
- Python 3.12 completed 2,937 tests with 26 skipped, 506 warnings, and 1,410
  subtests in 821.48 seconds (`.tmp/full-suite-schema32-final3.xml`). Ruff's
  formatting check covered 463 files and all lint checks passed; v7 mypy
  covered 46 modules; 23 JavaScript tests, inventory (75 tools, 10 resources,
  571 requirements), uv lock/diff checks, and the UI build passed.
- Exact-artifact certification passed clean Windows wheel installs on core
  Python 3.10/3.12, a clean Python 3.12 sdist stdio/HTTP ritual through restart,
  Linux Docker wheel stdio/HTTP rituals through restart on Python
  3.10/3.11/3.12, and the Python 3.11 `models-local` install and capability
  check. A real local Claude Code workflow also passed; remote bridge and host
  validation remains open.
- Independent review closed the final projection-quiescence HIGH with one
  focused regression and Ruff. The full suite above predates that final
  two-file fix and was not rerun afterward. The hashed `final3` development
  artifacts and their clean-install and transport certifications also predate
  the fix, are not artifacts of the current tree, and must be rebuilt and
  recertified after external blockers are resolved and a final commit exists.
- Scale evidence remains invalid/incomplete: the 100k/1m fixture is schema 30
  versus current schema 32 and now contains 100,003 memories/1,000,003 events.
  This is not an observed production defect; it needs an offline-migrated copy
  or reseed.
- Remaining gates are actual E2B service validation, authenticated remote
  Qdrant, real remote Claude/OpenCode bridges and hosts plus supported OpenCode
  V2 native hooks, macOS, frozen current-schema scale/dense/relevance/migration
  measurements, the eight-hour soak, and final commit/RC verification.

See [P10 final local certification checkpoint](p10-final-local-certification.md)
for artifact hashes, evidence paths, and the bounded remaining gates.

## Current integration checkpoint

- Latest full Python 3.12 diagnostic with schema 29 and authenticated Valkey:
  **2,798 passed, one failed, 21 skipped, 1,397 subtests passed**, 651 seconds.
  Evidence: `.tmp/full-suite-integrated-current.log`. The sole failure was a
  formatting-sensitive source assertion; it now checks the same call through
  the AST, and its 13 tests/11 subtests pass. Subsequent production fixes and
  new acceptance scenarios still require a stable full-suite rerun.
- P4 consolidation's prior high findings and real five-source durable restart
  flow passed independent review. The remaining scheduler thread/partial-commit
  finding passed independent re-review: wake-ups run on the owner event loop
  after worker completion, including archive failure/cancellation.
- P9 health now rechecks authorization after the complete public response is
  assembled, samples readiness once, and marks a stopping dispatcher unavailable.
  Root Python 3.10 health/pinned/consolidation/real task transport checks:
  **52 passed, 17 subtests passed**. Independent re-review accepted this slice.
- P7 root review reproduced source starvation, a foreground admission race,
  and missing work bounds. Independent re-review accepted the repairs and three
  subsequent lifecycle/graph fixes: 16 focused tests plus lock, shutdown,
  fairness, foreground, and 4,096-membership cross-page restart probes.
- P5 owner-fenced leases and bounded backup passed 101 focused tests, but final
  review found expired attempts can consume the quota needed for takeover.
  That repair passed re-review. A subsequent failed-provider-cleanup finding
  now has a root repair and 25 passing portable tests; independent re-review
  accepted this bounded recovery slice.
- P6 remote hooks now have an explicit protected desktop-root/server-workspace
  mapping. Different-root authenticated HTTPS hook integration passed;
  independent review accepted both HIGH repairs and a CA-path follow-up, with
  67 tests passing independently on Python 3.10 and 3.12. External certification is
  still open. OpenCode V2 lacks the required native execution hooks.
- Legacy pooled ONNX now uses the verified adapter; three root checks with the
  actual cached model passed. The original model cluster has no remaining ONNX
  output mismatch. Legacy format-6 text compression and v7 no-global-compression
  boundaries pass together (27 tests).
- Legacy Python recall compatibility now passes 141 owned tests plus 14 adjacent
  tests (one optional skip). Independent review found a cache publication race,
  unauthenticated compatibility metadata, and an admission-count mismatch;
  repairs pass 144 owned tests and independent re-review accepted all three
  repairs with retained adversarial probes. Six stale integration
  fixture suites pass all 81 tests after updating setup to current contracts.
- Repository Ruff lint and formatting now pass. The frozen retrieval fixture
  is excluded from formatting, and Git attributes retain the exact frozen CRLF
  bytes on all operating systems. Blocking v7 mypy passes all 46 modules.
- The synthetic 100,000-memory / one-million-event fixture is ready (2.50 GB;
  762.79-second seed, 7.19-second lexical build). Actual MCP recall exposed
  `POLICY_STATE_UNAVAILABLE` after successful lexical search. A revision-aware
  event-root cache restores successful recall (one development run p95 440 ms).
  Transactional lexical updates improved new-write visibility from 18.09 to
  5.40 seconds, still above target; rebuild contention is being repaired.
  A subsequent scale run exposed unbounded SQLite integrity scans in online
  health; bounded structural-readiness inspection is being implemented.
  No scale gate is accepted.
- Clean wheel core installs on Python 3.10/3.12 and a clean source-distribution
  install on Python 3.12 passed the actual stdio/HTTP ritual through restart.
  Nine optional extras resolve independently on Python 3.12; that is not runtime
  service certification. All 23 JavaScript tests pass.
- Actual process success coverage records 74/75 tools. Discovery/graph/apps
  scenarios pass both transports, as do six portability/pruning scenarios and
  two actual public-HTTPS ingestion/provenance/replay scenarios. Real E2B remains
  open. New temporal
  round-trip tests exposed JSON timestamps being treated as Python datetimes;
  schema rehydration fixes that production boundary. Further tool scenarios are
  being added. A purported TODO failure was invalid uppercase test input, not
  a production defect.
- Fresh-workspace initialization now passes the process ritual on Python 3.12.
  Independent review found a Python 3.10 Windows path race, a missing locked
  empty-directory recheck, and repeated-cancellation cleanup interruption;
  those repairs passed re-review. A new HIGH junction-swap race can redirect
  initialization outside the workspace and is being repaired. Bootstrap remains
  unaccepted. The short fresh-workspace
  soak passed 160 recalls, an authorized write/outcome, resources, and shutdown;
  it does not satisfy the eight-hour gate.
- Dense provider upload/validation now uses 128-point batches. Independent
  review passed 25 tests/11 subtests, including cross-batch identity failures
  and partial-upload cleanup. An actual configured ONNX/authenticated Qdrant
  257-record build passed (21.66 seconds); source changed during that run and
  it is integration evidence only. Large dense visibility remains unmeasured.
- Actual external E2B, remote Qdrant/TLS clients, the OS/Python matrix, clean
  artifact/profile certification, 100k/1m performance, and the eight-hour soak
  remain open. No release commit, RC, or final artifact has been produced.

## Integrated implementation progress
- P0 inventory/CI implemented and revised after review; currently 539 tracked atomic requirements, all awaiting final-commit acceptance evidence. Inventory must be regenerated as approved CLI/resources/tools land.
- P1 foreground slice and dedicated admitted-ID task dispatcher implemented;
  independent re-review remains open. FastMCP 3.4.7 exact pin, Python 3.10
  parse fix, all 35 optional tools have explicit policy, and Python 3.12
  cancellation is fixed. The protected SQLite/outbox authority queues only
  opaque IDs to authenticated loopback Valkey; real outage/restart/duplicate,
  isolation, and standard MCP lifecycle tests pass locally.
- Root production fixes: configurable DAEM0NMCP_SYNC_TIMEOUT_SECONDS (default15, range1..60), Windows Git timeout avoids inherited captured-pipe hang, HTTP JSON boundary preserves real disconnect semantics.
- Real stdio and HTTP process ritual through restart passed on Python3.10 (2 tests in13.20s); focused transport/resource/Git 28 tests plus26 subtests and deadline config6 passed. Evidence predates final release commit; not release accepted.
- P2 verifier/repair implemented and independent data-loss review running. Existing P2 migration/recovery matrix still outstanding.
- P3 projection/retrieval lifecycle implementation and production integration in progress; federation still outstanding.
- P8 static shell process delivery passed both transports. Root review identified missing dynamic v7 data/actions/metadata integration; implementation resumed, package unaccepted.
- Local Docker dependencies now available (see local-services.md); missing E2B/remote environment is not counted as passing verification.

## Independent review and integration follow-up
- P1 review found deadline bypass during admission, discarded committed
  receipts, unbounded export materialization, and Git descendant cleanup
  defects. All four corrections are implemented; P1 re-review and final process
  integration evidence remain open.
- Root corrected Windows Git containment to create suspended, assign a fully typed Job Object, then resume via documented thread APIs. Failure paths reap the parent; a non-destructive child-liveness regression and production tests pass (14 tests). POSIX implementation still needs platform execution.
- P2 independent review found stale-candidate data loss after interruption, shallow manifest/projection verification, inconsistent legacy mapping keys, migration-directory link traversal, and nonresumable early interruptions. Author is correcting all five with regression tests.
- P3 review found transient scheduler exceptions strand work and dense projection slices omit owned-resource cleanup. Corrections precede federation implementation.
- Expanded Python 3.12 all-extras integration: 420 passed, 466 subtests passed, one stdio recall timeout. Reproduction and stack capture show disabled dense profiles still load SentenceTransformer; capability gating is being corrected. Logs: .tmp/integrated-v7-py312.log and .tmp/stdio-stack.txt (development evidence).
- P8 dynamic review found a no-op refresh, false preflight display, dropped graph edges and incomplete host protocol. Production-loaded browser assets and meaningful message tests are being corrected.
- Inventory now includes all ten resources, packaged hook entry points, full commit IDs for accepted evidence, and explicit scenario gates in acceptance-scenarios.json. It still requires real execution evidence and frozen representative migration/relevance fixtures before P0 acceptance.

## Integration update — 2026-09-17

- The bounded P1 foreground slice passed independent re-review. Dispatcher
  review findings are repaired: ambiguous admissions fail closed, persisted
  cancellation and the claim/start race are covered, SQLite loops are
  supervised, listing scans authorized pages, and queue names use durable
  authority IDs. Follow-up review found shutdown-created false cancellation and
  post-publication broker loss; graceful close now requeues replay-safe work and
  bounded live reconciliation restores lost wake-ups without list growth. The
  final claim-to-active shutdown window now checks stop before opening the
  handler gate and settles replay-safe/unsafe work without starting effects.
  Real authenticated-Valkey stdio and HTTP task lifecycle plus restart passed;
  independent re-review remains open. The oversized export remedy and its
  operation-test reconciliation are assigned to P5.
- The async cursor portability slice passed independent P2 review. Whole-store verification still required correction for unknown manifest names, duplicate claims from non-active migration runs, and local staleness repair. Root implemented those corrections, orphan local generation recovery, and additional regressions. Current run: 41 tests passed; one subprocess schema-version mismatch occurred while migration 24 landed, then passed alone. A quiescent full re-review remains required.
- Full-suite development baseline: 261 failed, 2193 passed, 19 skipped, 145 errors. Dominant adapted-cursor errors, rules score NameError, URL transport attribute, optional-profile model loading, and resource cleanup faults have corrections. This is not a passing integrated suite; rerun after worker edits settle.
- The test temporary-directory override now retains standard-library keyword support and cleanup behavior. Database activation tests pass; hidden cleanup failures are no longer deliberately swallowed by the replacement class.
- Two missing P4 adapters are now registered: document_ingest_url and sandbox_execute_python. Root integration checks: 28 passed. Actual URL MCP verification and missing-credential remediation tests are in progress. Live E2B remains blocked by unavailable staging credentials.
- P3 lifecycle/federation focused runs passed (118 tests plus 17 subtests; 82 tests plus 30 subtests). Root review found federation silently omits linked sources when final result limit is smaller than source count and composes context more than once. Candidate gathering and global composition corrections are in progress; P3 is not accepted.
- P8 real MCPJam discovery exposed 71 tools and 10 resources. A real briefing returned three synthetic records, but the initial dashboard stayed empty because the sandbox proxy origin was rejected. Browser-referrer origin binding is patched; browser control became unavailable before visual confirmation. Root also fixed stale action arguments, unsupported server pagination, cross-tool response routing, UTF-8 payload bounds, protocol negotiation, and origin/citation display. JavaScript tests: 22 passed; Python dashboard tests: 6 passed. Browser and independent acceptance remain open.
- P5 implements a versioned paged snapshot and staged import contract with a 1 MiB page budget, migration 24, optional projection serialization/validation, and identity-preserving finalization. Implementation is in progress, not accepted.

## Latest independent review and provider integration

- P2 bounded verification/recovery is independently accepted after the final
  manifest, live migration-claim, and staleness corrections. Root's focused run
  passed 46 tests. This does not close the full migration/platform/failure matrix.
- P3 independent review found three HIGH issues: unlink after final federation
  authorization, specialized rebuilds holding the SQLite writer lock, and dense
  clients closing while timed-out workers remain active. Durable linked recall
  also lacked restored identity scope. Corrections are assigned; P3 is unaccepted.
- P4 pins E2B interpreter 2.10.0, whose sandbox creation contract includes the
  required lifecycle controls. The owned interpreter transport now limits raw
  response bytes before JSON parsing, rejects compression, and bounds cleanup
  under repeated cancellation. The actual SDK with synthetic transport passed
  30 tests plus three subtests; independent re-review remains open. Live staging
  remains unexecuted because its credentials are unavailable.
- The configured nomic ModernBERT quantized ONNX artifact ran locally with
  ONNX Runtime and returned finite 256-dimensional vectors. Its pooled output
  needs an owned adapter because its token output name differs from Optimum's
  expected output. The adapter truncates before normalization. Offline tests
  passed 19 cases in the all-profile Python 3.12 environment. Actual MCP dense
  retrieval, performance, quality, and final-commit certification remain open.
- P5 integration review rejected invented legacy identity mappings on ordinary
  v7 records. Portable compatibility mappings must derive from authoritative
  legacy-import events and pass whole-store verification after import.
- P6 broker, reviewed capture, and three public tools are in implementation.
  Client adapters and actual local/remote client flows remain open.
- Final-release acceptance remains 0 of 539 tracked requirements. These are
  development checkpoints; no final release commit or RC has been produced.

## Current development checkpoint

- P3 bounded lifecycle/federation/local-provider re-review passed: 190 tests and
  50 subtests. Ordinary production stdio and HTTP both returned dense evidence
  on 20 warm requests at concurrency four and survived restart. Tiny-fixture
  measurements are documented in real-provider-development.md, not certified
  against the 100,000-memory release target.
- P1 shutdown review found an additional claim-to-active race: a claimed worker
  can start after shutdown snapshots active children. Correction and independent
  re-review remain required.
- P5 ordinary production MCP/Qdrant export/import passed a two-page, one-record
  round trip. Independent review found vector paths bypass disabled local
  capability status; P5 remains unaccepted.
- P6 core broker/capture operations and client adapters are implemented in part.
  Bounded transport hardening, packaged OpenCode interfaces and actual client
  certification remain in progress.
- MCPJam currently discovers 74 production tools. Browser control is restored;
  briefing returned an internal server error, now under diagnosis. No dashboard
  rendering acceptance is claimed.
- Final-release acceptance remains 0 of 539 tracked requirements; the inventory
  will be regenerated for approved additions before final certification.

## Expanded inventory and HTTP boundary

- Regenerated inventory: 74 tools, 10 resources, 565 atomic requirements.
  Final-release acceptance is 0 of 565; no final commit or RC exists yet.
- Final P1 dispatcher shutdown repair passed independent review. The exact
  claim-to-active race, graceful reopening and lost broker notifications are
  covered. P1 platform/performance/final-commit gates remain open.
- Git workspace filtering and briefing statistics passed independent review;
  4 real stdio/HTTP dashboard-resource and nested-workspace tests passed.
  MCPJam rendered actual fixture records and its Check Context action returned
  real Covenant status. Browser control was lost again before the corrected
  counts and refresh could receive final visual verification.
- P9 Host/proxy/JSON boundary: 36 tests plus 27 subtests passed on Python 3.12;
  45 tests plus 27 subtests passed on Python 3.10. Explicit host authorities and
  proxy IP trust are documented in p9-http-boundary.md. Independent security
  review and actual remote TLS deployment remain open.
- P5 independent review found six material transfer/vector lifecycle defects;
  repair is in progress. P6 review found absent actual-response receipt staging
  and installer pairing gaps; Sol now owns their completion.
- P4 graph/entity operations and generation-scoped code impact are in progress.
  A graph worker native import hang is being corrected using captured stacks.

## Workspace grants, live clients, and advanced-operation reviews

- Inventory now contains 75 tools, 10 resources, and 570 atomic requirements.
  Final-release acceptance remains 0/570; no final release commit or RC exists.
- P9 server-managed workspace grants are enforced independently of JWT identity,
  briefing, links, edit-host pairing, and existing task admissions. Actual HTTP
  JWT tests cover revoked and ungranted workspaces, invalid/expired credentials,
  generic tools, resources, workspace linking, and linked recall. The focused
  federation/real-process suite passed 13 tests. An earlier Python 3.10 workspace
  access/middleware/admission run passed 24 tests and 3 subtests.
- Concurrent tool/resource admission is bounded globally and per principal and
  workspace. Durable quotas and revocation passed 16 tests and 8 subtests against
  authenticated Valkey. Protected file handling checks Windows owner/DACL as
  well as POSIX permissions. Independent P9 security review remains open.
- Live Claude Code completed denied edit, actual MCP preflight, receipt staging,
  unchanged retry, actual file modification, and candidate listing. OpenCode and
  reviewed promotion verification are ongoing. No remote-client acceptance is
  claimed.
- P4 graph review found four HIGH findings and one MEDIUM finding; repair is
  active. Code-impact review found two HIGH findings; repair is active. Passing
  normal-flow tests did not satisfy these independent acceptance reviews.
- P5's first six findings received repairs and its ordinary real Qdrant roundtrip
  passes. Coordinator re-review found unfenced expired import leases and backup
  quota/cancellation checks that run too late; see p5-rereview.md. P5 is not yet
  accepted.
- Consolidation preview/apply/archive handlers are now wired in production;
  their real-client and recovery verification is in progress.
- Shared wire-model/static-boundary fixes passed 58 tests and 61 subtests on
  Python 3.12 and 50 tests and 61 subtests on Python 3.10. Broader type checking
  still reports errors in operation dependencies and is not a passing gate.

## Current integration and independent acceptance checkpoints

- Code-impact resolver and integrity repairs passed independent bounded review,
  including real stdio/HTTP flows. Unknown receivers no longer create guessed
  dependencies; aliases, relative imports, case and shadowing have regressions.
- Graph's five review findings received repairs and coordinator re-review;
  20 tests passed independently. A native request cleanup regression also passed.
  End-to-end 100,000-memory and platform performance evidence remains open.
- Claude Code 2.1.274 and OpenCode 1.18.21 completed real local enforced edit,
  capture, exact reviewed promotion and recall. Independent P6 security review
  is active; remote execution remains unverified.
- P9 independent review found that standard MCP task lifecycle requests bypassed
  inflight quotas. The repair shares global, principal and workspace capacity with
  tools/resources; 62 tests and 94 subtests passed on Python 3.12 against Valkey.
  Re-review and a stable Python 3.10 process rerun remain open.
- Consolidation failed independent acceptance with authorization, interruption,
  generation-swap and bounded-processing findings. Repairs are active. P5 fenced
  transfer-lease and pre-backup quota repairs are also active.
- Production dreaming integration is active. Schema 28 holds bounded strategy
  metadata; schema 29 holds owner-fenced transfer leases. Neither package is yet
  accepted.
- One broader v7 run produced 502 passes, 16 skips and two stale tool-count
  assertions, now corrected to 75. A subsequent full Python run during shared
  edits produced 2,468 passes, 248 failures and 21 skips. At least 132 failures
  were migration-ledger/version mismatches during concurrent edits; other
  failures include disabled-profile fixtures, legacy compatibility and unfinished
  integration. These results are diagnostic, not a passing release gate. A
  quiescent rerun and resolution of remaining failures are required.
- CI now has a blocking v7 surface type check. Seven optional-vector client type
  errors remain assigned with P5; legacy repository type errors remain separately
  reported. Final-release acceptance remains **0/570** pending final-commit evidence.
