# Baseline evidence

Captured 2026-09-16 from detached cc08b4f worktree `D:/Daem0n-MCP-v7-baseline`.

| Check | Result |
|---|---|
| Commit | `cc08b4f696b6226e88cca1f83104ce870dd62ae6` |
| Python | Host launchers absent; uv managed CPython 3.12.14 in `.venv` |
| uv / Node / npm | uv 0.12.7 / Node 24.19.0 / npm 11.17.0 |
| Docker | CLI 29.7.2; daemon unavailable (`docker info` exit 1) |
| Redis/Valkey | localhost:6379 unreachable |
| Qdrant | localhost:6333 unreachable |
| Checked credentials (including `E2B_API_KEY`) | all absent; values not emitted |
| JS renderer tests | 16 passed, exit 0 |
| Ruff | 602 findings, exit 1 |
| Python full suite after bounded extras | exit 2 at collection: only `sentence_transformers` remains (torch/model stack intentionally not downloaded) |
| API-v7 timeout rerun | exit 1 after `--timeout=60`; faulthandler captured worker/IOCP wait stacks |
| Full suite continue-on-collection-errors | 346 passed before 1 failure, then timeout in `SyncFallbackTests.test_repeated_cancellation_cannot_interrupt_child_drain` |
| Isolated first failure | `test_caller_cancellation_is_never_translated_or_swallowed`: child finally not set after caller cancellation |

Full command logs remain outside the tracked tree under `D:/tmp/daem0n-v7-baseline-*.log`.
