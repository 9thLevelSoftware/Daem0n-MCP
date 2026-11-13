# Native integration PR review index

This follow-up to PR #78 adds client automation and edit mediation, richer
projection diagnostics, indexing/schema repairs, native acceptance harnesses,
and the retained platform results. It does **not** establish that every client
or optional interface is ready. Publication follows the dated phase reports;
their statements that no commit or push had occurred describe those phases.

## Current acceptance selection

The [portable matrix summary](native-acceptance-summary-2026-11-12.json) contains
the selected rows, profiles, available versions, backend hashes, and source
matrix digests without accounts, databases, or raw native transcripts.

| Lane | Selected outcome | Qualification |
| --- | --- | --- |
| Primary clients, two projects, three platforms | 30 passed, 6 blocked | Mixed historical stages; not all rerun on freeze 11. Claude quota blocks six rows. |
| OpenCode V2 | 16 historical passed, 2 failed | All four latest-round model attempts failed overall. Historical host behavior was not reaudited with the new companion. |
| Mac V1 extra health call | Passed | Strict setup and cleanup both passed with the original prompts. |
| agy automatic instructions | Passed | Positive/negative first-response controls and separate normal/restart evidence. |
| Projection recovery | Passed with causal limits | Authority roots preserved; delayed agy work and an expired Codex lease recovered through supported paths. Exact historical Codex interruption mechanism is unknown. |
| MCP Apps / E2B | Blocked | Missing existing MCPJam account / usable E2B access. |
| Hosted embeddings | Unsupported | Provider adapter absent; token counting was evaluated separately. |

V2 attached sessions selected stale inherited `PWD` rather than the disposable
project's actual working directory. The canonical directory binding still needs
correction and native acceptance. WSL also performed unplanned session move and
rename operations. These are failures, not external blockers. The earlier
Windows rename `WinError 5` remains unexplained despite passing follow-ups.
Native edit mediation covers declared tools, not every filesystem-writing route.

## Verification already executed

- The full repository run completed with 4,208 passed, 111 skipped, 2,114 passing
  subtests, one failing test and four failing schema subtests. The five failures
  were subsequently corrected and verified with affected checks. This is not a
  fresh, clean full-suite result on the final tree.
- Later guidance, installer, adapter, receipt, native audit, readiness, and
  collector changes received focused regressions. Original failures and bounded
  reruns remain identified in the linked reports.
- Repository Ruff lint/format, Linux-targeted v7 mypy, and release inventory
  passed; later harness changes received scoped lint/format checks.
- Hosted CI on this PR is separate and was not inferred from local/native
  evidence. No successful checks or paid model turns were repeated just to
  prepare this PR.

## Reports and evidence availability

Start with [the latest repair report](native-scope-repairs-2026-11-12.md) and
[V2's detailed failure report](opencode-v2-repair-11-2026-11-12.md).
[Projection recovery](projection-recovery-2026-11-12.md),
[automatic loading](automatic-loading-2026-11-12.md),
[automation acceptance](native-automation-acceptance-2026-11-12.md), and
[platform acceptance](platform-acceptance-2026-11-12.md) retain earlier results.
The [repair history](native-repairs-2026-11-12.md) describes the full-suite
failures and their affected follow-ups.

Raw receipts, private profiles, database backups, and runtime directories stay
in the original acceptance workspace and are not committed. Phase-report links
into `.test_tmp` are **local evidence references**, not working GitHub links.
The portable JSON summary and these narrative reports are the public PR
evidence; their counts do not imply that a reviewer has the raw transcripts.

Automation is opt-in through explicit modes; omitted flags preserve existing
installation behavior. See [client automation](../../native-client-automation.md)
and [the edit gate](../../native-automation.md) for supported tool coverage,
preservation behavior, version requirements, and limitations.
