# Daem0n-MCP v7 baseline

Baseline target: detached worktree `D:/Daem0n-MCP-v7-baseline`, commit
`cc08b4f696b6226e88cca1f83104ce870dd62ae6` (`feat: v7 core foundation with
opt-in profiles and lazy capabilities (#61)`). Captured 2026-09-16 (America/New_York).

## Environment and setup

- `uv 0.12.7`; `uv venv .venv --python 3.12` selected managed CPython 3.12.14.
  The host `python` and `py` launchers are unavailable; use
  `D:/Daem0n-MCP-v7-baseline/.venv/Scripts/python.exe`.
- `uv pip install -e ".[dev,apps]"` completed successfully (exit 0).
- Node `v24.19.0`, npm `11.17.0`; `npm install` completed (exit 0). npm reports
  one moderate audit vulnerability and a pending esbuild install script.
- Docker CLI `29.7.2` is installed, but `docker info` exits 1 because the
  Docker Desktop Linux engine pipe is unavailable. TCP probes to localhost
  Redis/Valkey 6379 and Qdrant 6333 are false.
- Checked environment variable names `REDIS_URL`, `REDIS_HOST`, `VALKEY_URL`,
  `QDRANT_URL`, `QDRANT_API_KEY`, `STAGING_API_KEY`, `STAGING_TOKEN`, and
  `OPENAI_API_KEY`; all are absent. Values were never printed.
- `mypy`, `pyright`, and `basedpyright` are not installed.
- `E2B_API_KEY` is absent (presence only checked; no value emitted).

## Baseline checks

- JavaScript renderer tests: `npm run test:ui` — **16 passed, 0 failed, exit 0**.
- Ruff: `.venv/Scripts/ruff.exe check .` — **exit 1, 602 findings** (462
  fixable); baseline code was not modified.
- Full Python suite with requested extras and `PYTHONPATH=.` reached collection
  but exits 2 with 16 collection errors. Missing optional modules are
  `numpy`, `rank_bm25`, `networkx`, and `langgraph`; these correspond to local
  and graph/model optional extras intentionally excluded from the requested
  dev/apps setup. The initial `uv run pytest -q` without `PYTHONPATH=.` exits 4
  at conftest import (`No module named 'tests'`).
- Follow-up installed bounded `numpy`, `rank-bm25`, `networkx`, `langgraph`,
  `langgraph-checkpoint-sqlite`, and `pytest-timeout` (all exit 0). A rerun
  with `python -X faulthandler -m pytest -q --timeout=180` reduced collection
  errors to three tests requiring `sentence_transformers`; installing that
  package would pull the large torch/model stack, so it was deliberately not
  downloaded. The run exits 2 at collection; see
  `D:/tmp/daem0n-v7-baseline-pytest-complete.log`.
- API-v7 subset was run separately with `PYTHONPATH=.`; progress reached 91%
  before a test timeout. A bounded rerun with `--timeout=60` exits 1 after
  `........F..`; faulthandler captures worker threads waiting in
  `concurrent.futures.thread` and the asyncio Windows IOCP selector. See
  `D:/tmp/daem0n-v7-baseline-api-v7-timeout.log`.
- A non-masking full-suite run used `--continue-on-collection-errors` and
  `--timeout=60`; it executed 346 tests successfully before one failure and a
  timeout in `tests/api_v7/test_tasks.py::SyncFallbackTests::test_repeated_cancellation_cannot_interrupt_child_drain`.
  The same API-v7 run identified the first failure as
  `test_caller_cancellation_is_never_translated_or_swallowed`: the caller is
  cancelled, but the child operation's `finally` has not run
  (`child_cancelled.is_set()` is false). See
  `D:/tmp/daem0n-v7-baseline-pytest-verbose20.log` and the isolated failure
  log `D:/tmp/daem0n-v7-baseline-api-v7-failure.log`.

Bulky command output is kept under `D:/tmp/daem0n-v7-baseline-*.log`; these
logs include `pytest`, npm install, Ruff, Docker, and marker evidence and are
not copied into the implementation tree.
