# Repository Guidelines

## Project Structure & Module Organization
- `daem0nmcp/` contains the core Python package (server, memory, rules, indexing).
- `daem0nmcp/migrations/` holds database schema migrations.
- `daem0nmcp/channels/` provides notification channel implementations.
- `tests/` is the pytest suite (`test_*.py`, `test_*` functions).
- `docs/`, `scripts/`, and `hooks/` contain documentation, utilities, and git hook templates.
- Runtime data lives under `.daem0nmcp/` (e.g., `.daem0nmcp/storage/daem0nmcp.db`); do not commit it.

## Build, Test, and Development Commands
- `pip install -e ".[dev,apps,graph]"` installs the package in editable mode with the extras CI tests against.
- `python -m daem0nmcp.server` runs the MCP server directly.
- `python start_server.py --port 9876` starts the Windows HTTP launcher.
- `python -m daem0nmcp.cli <command>` runs CLI tasks (example: `python -m daem0nmcp.cli index`).

CI (`.github/workflows/ci.yml`) blocks merges on these gates; run them before pushing:
- Tests: `pytest tests/ -v --asyncio-mode=auto` (Ubuntu, Windows and macOS on Python 3.10-3.12).
- Lint and format: `ruff check daem0nmcp/ tests/` and `ruff format --check daem0nmcp/ tests/`, with ruff pinned to the `uv.lock` version (0.16.8).
- Type check: `mypy daem0nmcp/api/v7 --ignore-missing-imports --follow-imports=silent` (CI runs it on Linux; add `--platform linux` locally on Windows or macOS).
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

### 1. Start the scoped session

Immediately call:

```text
daem0nmcp_session_brief(workspace_id="<workspace_id>")
```

The server-issued session and authenticated transport identity establish the
scope. Headers, IP addresses, client information, and `_client_meta` are not
identity inputs.

### 2. Recall and preflight before protected work

Use bounded recall when you need relevant history:

```text
daem0nmcp_memory_recall(workspace_id="<workspace_id>", query="authentication", limit=10)
```

Before a protected tool, request counsel for that exact tool and its exact
arguments (excluding `workspace_id` and `preflight_token`):

```text
daem0nmcp_memory_preflight(
    workspace_id="<workspace_id>",
    target_tool="memory_store",
    target_arguments={
        "record_type": "decision",
        "content": "Use signed session cookies",
        "idempotency_key": "decision-auth-cookie-0001"
    },
    description="Record the authentication decision"
)
```

Respect warnings, failed approaches, and `must_not` guidance. Use the returned
`preflight_token` only with the exact protected request it authorizes.

### 3. Store durable decisions replay-safely

```text
daem0nmcp_memory_store(
    workspace_id="<workspace_id>",
    record_type="decision",
    content="Use signed session cookies",
    rationale="Avoid server-side session state",
    idempotency_key="decision-auth-cookie-0001",
    preflight_token="<token-from-memory_preflight>"
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
