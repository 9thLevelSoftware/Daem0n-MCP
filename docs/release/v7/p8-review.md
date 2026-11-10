# P8 dashboard Git scoping and briefing statistics independent review

Date: 2026-09-17

## Decision

**ACCEPTED for the bounded Git-scoping, briefing-statistics, and briefing UI
repair.** No CRITICAL, HIGH, MEDIUM, or LOW finding remains in the reviewed
changes.

This is not full P8 or release acceptance. A supported real MCP Apps host must
still demonstrate discovery, render, interaction, and refresh, and the wider P8
surface requires its separate final review.

## Git scope and subprocess containment

Affected components: `daem0nmcp/api/v7/resource_repository.py` and
`tests/api_v7/test_git_briefing_deadline.py`.

The repository first obtains Git's repository-relative `--show-prefix`, rejects
an invalid/non-normalized prefix, and runs porcelain status with the fixed
pathspec `-- .` from the resolved workspace root. It then independently requires
every returned path to start with the exact slash-terminated prefix, strips the
prefix, and applies the normalized workspace-relative path validator before
exposing it. The slash termination prevents sibling prefix collisions such as
`nested` versus `nested-other`. Rename/copy secondary names are consumed but
never exposed. Malformed, non-UTF-8, oversized, or unsuccessful output fails
closed to an empty change list.

This fixes the observed nested-workspace leak: a parent repository may contain
staged or modified siblings, but only paths under the registered workspace are
returned and the public paths are relative to that workspace. Both the real Git
regression and a forged-porcelain defense-in-depth test cover the boundary.

The existing bounded subprocess implementation remains intact. Git output uses
a temporary file rather than inherited pipes, output is capped at 1 MiB, and
execution has a two-second deadline. POSIX launches a new session and terminates
the process group. Windows creates Git suspended, assigns it to a fully typed
KILL_ON_JOB_CLOSE Job Object, resumes the single primary thread through the
public Toolhelp/OpenThread/ResumeThread APIs, and reaps the suspended process if
containment setup fails. No command shell or user-controlled argument is used.

## Canonical statistics and briefing UI

Affected components: `daem0nmcp/api/v7/resource_repository.py`,
`daem0nmcp/ui/static/runtime.js`, and
`daem0nmcp/ui/static/renderers/briefing.js`.

The briefing snapshot now derives `patterns`, `learnings`,
`successful_outcomes`, and `failed_outcomes` in the same canonical SQLite read
snapshot as the existing record counts. Counts remain scoped by workspace,
exclude deleted and legacy rows, and coerce SQLite's empty `SUM` values to zero.
The public count map stays bounded and contains no path or record content.

The runtime projects the canonical category counts directly and computes the
success rate only from successful plus failed outcomes. No-outcome workspaces
produce `null`, which the renderer presents as an em dash instead of the
misleading `0%`/`NaN%`. The Git status mapping preserves added, modified,
deleted, renamed, untracked, and conflicted states; the renderer admits the
fixed `A/M/D/R/U/?` set and uses a neutral class for statuses without a
specialized color. Paths and labels are still inserted through text nodes.

The real production stdio and Streamable HTTP regression creates a workspace
nested inside a parent Git repository, verifies sibling paths do not appear,
verifies returned paths are workspace-relative, and exercises the zero-value
statistics through the standard `session_brief` protocol.

## Independent verification

- Python 3.12 Git deadline/scope, dashboard process, dashboard resource, and
  resource adapter suites: **19 passed** in 16.33 seconds.
- Dashboard protocol and renderer JavaScript suites: **23 passed** in 73.9 ms.
- The supplied production process log independently records **4 passed** for
  stdio/Streamable HTTP shell delivery and nested-workspace scope coverage.
- Source review covered prefix/pathspec behavior, forged porcelain entries,
  rename secondary fields, output bounds, process-tree cleanup, SQL scoping and
  null aggregates, numeric normalization, fixed status enums, and DOM-safe text
  rendering.

The reviewed local/process tests establish the repaired server and simulated UI
contract. They do not substitute for the outstanding supported-host visual and
interaction gate.
