# Disposable end-to-end acceptance, 2026-11-11

This pass exercises production entry points and installed packages using
synthetic data in disposable workspaces. It is functional development evidence,
not final-release certification of every requirement in `requirements.json`.
Existing user databases and global client configurations were not changed.

Base: `1fb98bb087e94fad949942b9a7d3227c62d55bd3` (merged PR #74).
Branch: `codex/v7-end-to-end-acceptance`. The base's
[merged-main CI run](https://github.com/9thLevelSoftware/Daem0n-MCP/actions/runs/42962203152)
passed all 12 jobs, including Python 3.10–3.12 on Linux, Windows and macOS.
That run predates the fixes below; it is not CI evidence for this branch.

## Defects found and repaired

1. **Command reminders read an obsolete database generation.** A real
   CLI-installed Bash hook continued reading the legacy database after
   `active-db.json` selected another generation. The hook now uses the shared
   canonical pointer resolver. Invalid pointers and missing selected databases
   produce no stale-rule fallback. The actual hook scenario and nine focused
   tests passed; the canonical missing-target fixture also passed separately.
2. **Configured telemetry exported no spans.** A real loopback collector
   received positive-control SDK spans but none from the production server.
   The shared launcher now initializes the requested OTLP provider and flushes
   only the provider it owns. HTTP/protobuf and default gRPC collectors receive
   actual MCP spans. Disabled profiles stay disabled; missing dependencies and
   invalid provider/exporter configuration warn without breaking core startup
   or printing endpoint/header secrets. Provider ownership and cleanup have
   focused regression coverage: 29 tests and five subtests passed.
3. **Windows restart could stall loading NumPy from a worker.** The real
   five-source durable-consolidation scenario intermittently timed out at
   `session_brief` after restart. A stack capture taken after the stall showed
   the main thread starting a pool worker while the Qdrant worker loaded
   NumPy's native core. Windows now initializes NumPy on the main thread for
   ready dependent profiles before provider workers start. Other heavy auto
   imports remain lazy. The ordering regression failed before the fix; 22
   production-composition tests plus five subtests, the original process case,
   and three further restart runs passed with the original 30-second deadline.
   The exact Windows loader-lock mechanism is an inference from those stacks.
4. **Community and graph visualizations stayed empty with valid data.** Real
   Chromium rendering showed a community count of one alongside an empty-map
   fallback. The shipped D3 bundle exports `d3`, while both renderers looked
   for `D3`. Their references now use the actual bundle's global. Two new Node
   regressions failed before the fix and passed afterward; they load the
   shipped D3 bundle and check actual SVG cells/canvas drawing. Source browser
   checks now show the populated treemap and linked graph and exercise their
   controls through actual MCP calls.

All four production changes received independent correctness and simplicity
review. The default-core gateway also gained real stdio/HTTP regression tests
for discovery, writes, idempotency, exact counsel remedies, spent-token refusal
and persisted state after restart. No gateway production defect was found.

## Package and production-path evidence

Artifacts and raw logs remain under ignored `.test_tmp/acceptance-20261111-*`
directories. They are local evidence, not published packages. The runtime wheel
below includes all three Python fixes; the sdist was built in the same invocation.
Subsequent test-only corrections do not change packaged runtime code.
No final commit identity is implied by these development builds.

| Artifact | SHA-256 |
| --- | --- |
| `dist-final/daem0nmcp-7.0.0.dev0-py3-none-any.whl` | `351e6f42a9c1f65ef068a95f88564695b9b85a5c7e23b7ce72a8cf75f5cf43ed` |
| `dist-final/daem0nmcp-7.0.0.dev0.tar.gz` | `98fca909f369d6554ff45c4f6bd636d3f8246539ba9edccc85fdba373f6dbc28` |
| `dist-ui-fixed/daem0nmcp-7.0.0.dev0-py3-none-any.whl` | `375f83b1fe50f078bba43fa9dd87151425ba54985084aca03fd41303282254c7` |
| `dist-ui-fixed/daem0nmcp-7.0.0.dev0.tar.gz` | `a7ff7106798416619ec381ad667a7ed448e52eb8d1585f5b6185e71cb5f667e5` |

The artifact paths above abbreviate `.test_tmp/acceptance-20261111-core/`.
The rebuilt `dist-ui-fixed` wheel contains the renderer repair. Comparing every
ZIP member with the earlier wheel found changes only in the two renderer files
and `RECORD`; all Python runtime bytes are identical. Its private installation
was verified with `python -I`.

| Check | Observed result |
| --- | --- |
| Baseline wheel and sdist | Fresh private installs, dependency checks and isolated package-import provenance passed; packaged dashboard and OpenCode assets present. |
| Installed-wheel process/boundary batch | 100 passed, three Windows platform skips. Covers memory rituals, stateful tools, history, workspace identity/relocation/isolation, HTTP/auth boundaries, code/discovery/dreaming, migration/recovery and portability. Some cases are component/race checks rather than independent process scenarios. |
| Baseline sdist ritual | Both stdio and HTTP passed, including outcome recording and persistence after restart. |
| Rebuilt sdist | Python 3.12.14 private installation and dependency check passed; isolated imports came from site-packages. Both stdio/HTTP memory and restart rituals passed against the final `dist-ui-fixed` sdist. |
| New core gateway | Two source and two installed-wheel transport cases passed. |
| Python 3.10.21 wheel install | Fresh private `dev,observability` install and dependency check passed; isolated imports resolve to its site-packages. Both real memory/restart rituals and all three real OTLP collector cases passed. |
| E2B SDK without live credentials | Fresh Python 3.12.14 `dev,agency-e2b` wheel install passed dependency/provenance checks. All 18 controlled SDK transport/provider tests and three subtests passed with `e2b-code-interpreter` 2.10.0 and `e2b` 2.63.0. These do not establish live sandbox isolation. |
| POSIX-only safeguards | Four skipped Windows cases passed on Linux. Three required native `/tmp` fixtures because Windows DrvFS does not implement the tested POSIX permissions; those initial DrvFS failures were not repaired in product code. |
| Real models and retrieval | ModernBERT ONNX returned finite normalized 256-dimensional vectors. Local Qdrant projection, dense retrieval, actual ColBERT reranking and restart passed on stdio and HTTP. |
| Compression and token counting | Actual LLMLingua CPU compression reduced 5,045 tokens to 1,750 while retaining the forced marker; local tiktoken passed. The `models-hosted` profile advertises tiktoken, so this check does not require a hosted-model API key. |
| Public HTTPS ingestion | Both transports fetched the actual public HTTPS fixture and verified canonical provenance and idempotency. |
| Final-wheel tasks | Four authenticated Redis lifecycle/restart and five-source consolidation cases passed on stdio/HTTP in 50.43 seconds. Unauthorized Redis access was rejected. |
| Remaining Redis scenarios | All 14 dispatcher cases and five subtests passed in 29.17 seconds against the accepted private wheel: cancellation/restart, admission/claim recovery, quotas/revocation, lost wakeups, outage recovery, shutdown, database faults and authority isolation. Combined with the four process cases, all 18 Redis-gated cases are covered. |
| Final-wheel models | Actual HTTP ModernBERT → local Qdrant → ColBERT rerank and restart passed in 26.26 seconds. Isolated imports and installed `production.py` bytes were verified against the wheel. |
| Service cleanup | The owned Redis process was stopped only after verifying its PID, executable and data directory; its loopback port was closed. |

## Actual clients, hooks and dashboards

Five real CLI/install/emitted-hook scenarios passed. The hook/install/bridge
batch passed 67 tests, with its Windows symlink skip separately covered on
Linux. Documentation checks passed 13 tests and 41 subtests; 23 JavaScript
protocol/UI tests passed.

**Claude Code 2.1.319:** an authenticated installed CLI used an isolated MCP
configuration and disposable project to brief, inspect health, store, recall,
read/edit a file, obtain file guidance through the gateway and record an
outcome. Nine tool calls and four hook event pairs completed. Independent
SQLite inspection found exactly one synthetic record and one outcome.

**OpenCode 1.18.21:** isolated plugin/MCP installation passed. Its existing
OpenAI provider OAuth refresh returned HTTP 401 before any MCP call. An actual
assistant workflow therefore remains unverified until local provider login is
restored (`opencode providers login`). No credential was copied into the report.

Installed Claude/OpenCode hooks are intentionally remind-only. They do not
create native edit-bridge requests. Backend bridge subprocess acceptance does
not establish a native-client deny/preflight/retry or automatic capture flow;
the README and operations guide now state that boundary accurately.

**Dashboard browser:** Chromium 153.0.8010.12 loaded all six unmodified
production `ui://` shells inside a disposable local Apps test host. That host
forwarded data/actions to an actual stdio MCP session using synthetic records,
links and a rebuilt community. Infrastructure, search/filter/refresh,
briefing/context, Covenant refresh, community drill-down and graph controls
passed. The final community/graph source runs had no page or console errors;
screenshots show the populated visualizations. This is local-host acceptance,
not compatibility certification inside Claude Desktop or ChatGPT.

The rebuilt wheel passed the two affected browser scenarios using its private
Python installation for the actual MCP server. Five backend calls completed,
the treemap and nontransparent graph pixels were asserted, and page/console
error lists were empty. Source renderer checks passed 18 cases, including the
two actual-bundle regressions. The wheel result and screenshots are retained in
`acceptance-20261111-dashboard/packaged/`.

## Regression and bounded stability

The full Python 3.12 suite passed **3,957 tests and 2,097 subtests**, with 76
skips and 510 warnings, in 1,517.79 seconds. Command:
`python -u -X utf8 -m pytest tests/ -q --asyncio-mode=auto
--junitxml=.test_tmp/acceptance-20261111-core/final-suite.xml`.
The skip inventory is retained in `core/skip-summary.json`: 25 platform-only
cases, 18 optional SDK cases, 18 Redis cases, ten live E2B cases, three OTLP
collector cases and two public-ingestion cases. Explicit provider runs above
closed all 18 SDK cases, all 18 Redis cases, all three OTLP cases and both
public-ingestion cases. Four of the platform-only cases also passed in local
Linux fixtures. These separate runs do not change the full suite's skip count;
the live E2B and remaining platform cases were not accepted from skipped runs.

Full Ruff lint/format checks passed
(505 files), v7 mypy passed (48 source files with `--platform linux`), and
release inventory passed (77 tools, 10 resources, 587 requirements).

The first [PR #76 CI run](https://github.com/9thLevelSoftware/Daem0n-MCP/actions/runs/43138245898)
passed eight of nine platform jobs and all three static gates. Windows/Python
3.11 exposed a test-only floating-point boundary: subtracting monotonic clock
values yielded `299.9999999999998` for a correctly scheduled 300-second wait.
The backoff test now compares absolute deadlines, retaining the strict minimum
wait and existing exponential/reset checks. The affected test and file-level
Ruff checks passed; independent review approved the assertion repair. Runtime
code and the accepted packages are unchanged. New-head CI is tracked on the PR.
The completed Ubuntu 3.11 verbose log also verified that all 25 platform-only
tests skipped locally on Windows actually ran and passed.

The [second CI run](https://github.com/9thLevelSoftware/Daem0n-MCP/actions/runs/43142862389),
for `3fc261870c1ec7495496c71e2b544c6982d8f3af`, passed all 12 CI jobs,
including all nine OS/Python combinations. GitGuardian and Kilo also completed
successfully. Kilo's comment still contained a warning and suggestions, which
were checked separately from its green conclusion:

- The proposed offline SQLite URI substitution is unsafe for this live hook.
  Actual concurrent writers produced 65 readable rules with the current normal
  SQLite locking, but zero rules and a false `SQLITE_CORRUPT` read with the
  proposed immutable helper on both Windows and Linux. The Windows database
  remained healthy (`quick_check=ok`, 65 rows). Normal read-only SQLite may
  create temporary WAL coordination files; physically read-only storage with
  absent sidecars still cannot provide those files. This configuration is not
  claimed as supported by this acceptance pass. No unsafe fallback was added.
- Qdrant already declares NumPy as a dependency. Replacing the telemetry resource
  merge as suggested would lose supported service-name environment precedence.
  Neither proposal required a runtime change.
- The community view's missing-library fallback wording predates this change;
  the accepted packages contain the library and render populated views. The
  unrelated fallback text was left unchanged.
- The new OTLP test's parent control did inherit ambient SDK/exporter settings.
  `OTEL_SDK_DISABLED=true` reproduced a failed control span. The test now clears
  inherited `OTEL_*` values through `monkeypatch`, restoring them afterward.
  All three actual collector cases passed in 13.06 seconds with host SDK
  disablement and gzip compression both set. Explicit server overrides remain
  covered. This final test-only improvement passed file-level Ruff checks and
  independent review; new-head CI remains linked from PR #76.

The bounded HTTP mixed-workload run completed in 600.125 seconds with 1,604
recalls, ten writes, ten outcomes, ten resource reads, ten health checks and two
briefings. It used concurrency four and retained ten resource samples. Python
source stayed unchanged throughout. Warm sampled RSS was roughly 0.99–1.03 GB;
after shutdown, owned child-process, thread, handle and RSS counts were all zero.
This is a successful ten-minute functional/stability smoke, not evidence of
eight-hour leak freedom or reference-hardware performance certification.
Command: `python -m benchmarks.v7_soak <disposable-workspace> --hours 0.1667
--transport streamable-http --interval 1`.

## Remaining external and release boundaries

- Live E2B sandbox isolation/network behavior remains unverified: no configured
  `E2B_API_KEY` was available for staging acceptance. The SDK was installed and
  its controlled transport/provider tests passed separately.
- OpenCode needs provider reauthentication before its real assistant workflow
  can run. Installation success is not that workflow's acceptance.
- A local browser host can prove rendering and forwarding to a real MCP
  server; it does not prove compatibility with a third-party MCP Apps host.
- Local Qdrant and loopback Redis/OTLP checks do not establish remote-service
  deployment, internet TLS/proxy configuration or a production operating history.
- A bounded mixed-workload check is not the planned eight-hour soak, 100,000
  memory/one-million-event scale benchmark, relevance evaluation or RC release.
  This report does not mark the release requirement ledger accepted.

## Reproduction and retained evidence

Build with `uv build --python .venv/Scripts/python.exe --out-dir <owned-dir>`.
Install a built wheel/sdist in a fresh environment; verify imports with
`python -I` before running process tests with that interpreter. The production
process harness uses the same interpreter for its server subprocesses.

Relevant tests include `test_process_client.py`, `test_process_gateway.py`,
`test_process_external_certification.py`, `test_process_observability.py`,
`test_production.py`, `test_claude_hooks_pre_bash.py` and `test_tracing.py`.
Real external cases require their explicit opt-in environment and provider
configuration; skipped default runs do not certify those integrations.

| Local evidence prefix | Contents |
| --- | --- |
| `acceptance-20261111-core` | Package builds, clean installs, process JUnit/logs, full regression, static gates and bounded soak. |
| `acceptance-20261111-hooks` | Five-scenario JSON report, actual Claude/OpenCode results, hook checks and redacted client event summaries. |
| `acceptance-20261111-profiles` | Real-provider report, stalled-process stack capture, deterministic ordering regression, final-wheel model/task proof and owned Redis cleanup. |
| `acceptance-20261111-observability` | Before/after collector probes and focused telemetry results. |
| `acceptance-20261111-dashboard` | Local browser host, actual MCP-backed rendering/action checks and browser evidence. |

Disposable service configuration may contain a fixture password. It is excluded
from Git and must not be copied into PR descriptions or published evidence.
