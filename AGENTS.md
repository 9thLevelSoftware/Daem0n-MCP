# Repository Guidelines

## Project Structure & Module Organization
- `daem0nmcp/` contains the core Python package (server, memory, rules, indexing).
- `daem0nmcp/migrations/` holds database schema migrations.
- `daem0nmcp/channels/` provides notification channel implementations.
- `tests/` is the pytest suite (`test_*.py`, `test_*` functions).
- `docs/`, `scripts/`, and `hooks/` contain documentation, utilities, and git hook templates.
- Runtime data lives under `.daem0nmcp/` (e.g., `.daem0nmcp/storage/daem0nmcp.db`); do not commit it.

## Build, Test, and Development Commands
- `pip install -e ".[dev,apps,graph]"` installs the package in editable mode with the extras CI tests against; the `dev` extra pins the ruff and mypy versions CI uses. (The locked `cryptography` ships wheels only for 64-bit Windows, Apple-silicon macOS and Linux; elsewhere `uv sync --frozen` builds it from source and needs a Rust toolchain.)
- `python -m daem0nmcp.server` runs the MCP server directly.
- `python start_server.py --port 9876` starts the Windows HTTP launcher.
- `python -m daem0nmcp.cli <command>` runs CLI tasks (example: `python -m daem0nmcp.cli index`).

CI (`.github/workflows/ci.yml`) blocks merges on these gates; run them before pushing:
- Tests: `pytest tests/ -v --asyncio-mode=auto` (Ubuntu, Windows and macOS on Python 3.10-3.12).
- Lint and format: `ruff check daem0nmcp/ tests/` and `ruff format --check daem0nmcp/ tests/`, with ruff 0.16.8 (pinned in the `dev` extra).
- Type check (mypy 2.3.1, pinned in the `dev` extra): `mypy daem0nmcp/api/v7 --ignore-missing-imports --follow-imports=silent` (CI runs it on Linux; add `--platform linux` locally on Windows or macOS).
- Release inventory: `python scripts/v7_release_inventory.py --check`.

## Coding Style & Naming Conventions
- Use 4-space indentation and follow PEP 8 layout.
- `snake_case` for functions/variables, `PascalCase` for classes, `UPPER_SNAKE_CASE` for constants.
- Keep modules focused and add new features under `daem0nmcp/` with corresponding tests.
- Code must pass `ruff check` and `ruff format --check`.

## Testing Guidelines
- Tests use `pytest` with `pytest-asyncio`; stick to `test_*.py` and `test_*` names.
- Reuse fixtures from `tests/conftest.py` where possible.
- Add regression tests for bug fixes and new CLI or server behavior.

## Commit & Pull Request Guidelines
- Commit messages follow Conventional Commits (examples: `feat: add active context API`, `fix: handle missing vectors`).
- PRs should include a short summary, tests run, and any config or migration notes.
- Link relevant issues; include screenshots only if user-facing output changes.

## Configuration & Data
- Configuration is via `DAEM0NMCP_` environment variables (see `README.md` for options).
- Keep `.daem0nmcp/` and other local caches out of commits.

---

## The Daem0n's Covenant (v7 Protocol)

This project uses Daem0n for persistent AI memory. When the v7 tools are
available, follow this protocol. Every workspace-scoped call takes the opaque
`workspace_id`; never substitute a filesystem path.

### Tool Detection

The core ritual tools are `session_brief`, `memory_preflight`, `memory_recall`,
`memory_store`, `memory_record_outcome`, and `system_health`. Hosts may expose
the same tool in any of these exact forms:

- bare: `session_brief`
- OpenCode-style: `daem0nmcp_session_brief`
- Claude Code-style: `mcp__daem0nmcp__session_brief`

If none of those forms is available, proceed without Daem0n. Do not guess a
legacy tool name.

### 1. Brief automatically

The first Daem0n call in a session briefs automatically; the compact brief is returned in `meta.covenant.auto_brief`. Call `session_brief` for the full brief.

The server-issued session and authenticated transport identity establish the
scope. Headers, IP addresses and client information are not identity inputs.

### 2. Recall, direct writes, and advance planning

Use bounded recall when you need relevant history:

```text
daem0nmcp_memory_recall(workspace_id="<workspace_id>", query="authentication", limit=10)
```

Call `memory_store` (or any protected tool) directly. If it returns `COUNSEL_REQUIRED`, read `error.counsel` (guidance and reasons), then retry exactly `error.remedy`. `memory_preflight` remains available for planning a change in advance.

Respect warnings, failed approaches, and `must_not` guidance. Challenge tokens
remain exact-argument, single-use capabilities valid for 300 seconds and bound
to workspace, principal, session, and tool.

Use `daem0n_tools_search(query)`, then `daem0n_tool_call(workspace_id, tool, arguments)`.

Defaults are `DAEM0NMCP_COVENANT_MODE=guided` and `DAEM0NMCP_TOOL_SURFACE=core`.
Set `DAEM0NMCP_COVENANT_MODE=strict` for explicit `session_brief` then exact
`memory_preflight` and token-bearing writes; `DAEM0NMCP_TOOL_SURFACE=full`
lists all registered tools.

### 3. Store durable decisions replay-safely

```text
daem0nmcp_memory_store(
    workspace_id="<workspace_id>",
    record_type="decision",
    content="Use signed session cookies",
    rationale="Avoid server-side session state",
    idempotency_key="decision-auth-cookie-0001"
)
```

Keep the returned `record_id`. Every write needs a stable idempotency key;
retries must reuse the same key.

### 4. Record the verified outcome

```text
daem0nmcp_memory_record_outcome(
    workspace_id="<workspace_id>",
    record_id="<mem_id>",
    outcome_text="The implementation passed integration tests",
    worked=true,
    idempotency_key="outcome-auth-cookie-0001"
)
```

Failures are valuable: use `worked=false` and explain what failed.

### Resources and health

Read-only context is available at these bounded JSON resources:

- `memory://workspaces/{workspace_id}/warnings`
- `memory://workspaces/{workspace_id}/failures`
- `memory://workspaces/{workspace_id}/rules`
- `memory://workspaces/{workspace_id}/active-context`

Use `system_health(workspace_id="<workspace_id>")` for diagnostics. Supported
transports are stdio and Streamable HTTP at `/mcp`.

The generated v6-to-v7 migration reference is
[`docs/v6-to-v7-tools.json`](docs/v6-to-v7-tools.json). Treat it as the source
of truth for renamed or split tools; do not copy an old invocation into new
instructions.
