# AI-led v7 summoning acceptance, 2026-11-12

The restored `Summon_Daem0n.md` was exercised by actual Claude Code sessions in a
disposable project. The AI installed and configured Daem0n from the grimoire;
the harness did not preinstall the package, create its database, or write its
MCP registration, covenant, or hooks. Existing user configuration and databases
were not changed.

Base revision: `6fccf2b18a7f71dd076b843c640280bf2f07c449`.
Candidate branch: `codex/restore-v7-summoning`. This is functional setup evidence,
not a published release or proof of every optional deployment.

## Setup exercised

- Windows; Claude Code `2.1.319`, using `claude-haiku-5-5` through the real CLI.
- A new Python `3.11.15` virtual environment, the documented supported fallback.
- Editable source installation with `local,graph,apps,models-local`; dependency
  consistency and package version `7.0.0.dev0` verified.
- Separate target project and staged source checkout, isolated home and app-data
  directories, and a custom `CLAUDE_CONFIG_DIR` distinct from `~/.claude`.
- Synthetic preexisting MCP registration, project policy, settings, and hooks.
  One matcher deliberately contained an old Daem0n command alongside an unrelated
  command, testing preservation inside a shared hook entry.
- Fresh storage: the AI obtained the opaque workspace ID from the installed CLI
  and correctly skipped migration. Production startup initialized the store.

The grimoire now explicitly covers host-owned approvals. Early harness attempts
stopped at shell allow-list restrictions and then Claude's protected configuration
file guard. The AI preserved its checkpoint and did not bypass the denial.
The harness subsequently used the supported SDK permission callback to approve
concrete writes within the disposable setup. It also performed the isolated
host's project-server trust action. No blanket permission-bypass mode was used.
See [Claude SDK approval handling](https://code.claude.com/docs/en/agent-sdk/user-input).

## Observed results

| Check | Result |
| --- | --- |
| AI-authored setup | Project stdio registration, matching project environment, scoped `CLAUDE.md` covenant and maintained hook definitions installed. |
| Configuration preservation | Other MCP server, policy text, user setting, unrelated hook, and unrelated command inside a mixed hook entry preserved; obsolete owned command removed. |
| Custom Claude directory | Hooks installed in the actual custom settings file; unused default-home settings unchanged. |
| Live MCP | Actual brief, health, memory store, matching recall, outcome recording and file-specific tool discovery passed. |
| Native edit/hooks | Real Claude Edit succeeded; hook events were observed in the client stream. Hooks remain reminders, not edit enforcement. |
| Local model | Cold isolated model cache downloaded `nomic-ai/modernbert-embed-base`; `encode_query` returned 1,024 bytes, representing 256 float32 values. |
| Restart and semantic retrieval | After the runtime fix below, the same stored record was recalled through a ready dense provider, generation 4, with one candidate returned. |
| Stable write replay | Repeating the original memory-store payload and key created no additional decision or event. |
| Repeat installation | AI reused equivalent configuration. MCP JSON, project settings, custom user settings and covenant file hashes were identical before and after. |
| Offline verification | `verify-v7` exited 0 with `status=verified`, schema 33, no replay differences, no foreign-key violations, valid pointer and no local rebuild required. |
| Independent data inspection | Exactly one verification decision, three events and two outcomes remained. |
| Isolation and cleanup | Real-user configuration and credential hashes unchanged; private credential copy removed; no owned acceptance processes remained. The separate pytest processes were excluded from cleanup. |

Initial dense degradation was not accepted as semantic success. The index
finished automatically after the startup defect was repaired; no forced index
rebuild or database reset was needed. The first successful MCP response in the
repaired restart arrived 13.876 seconds after the SDK host started. That interval
includes CLI startup and AI/API work, so it is not an import-time benchmark.

## Runtime defect found and repaired

The second process stalled with the main thread waiting in `Thread.start` while
a dense-retrieval worker loaded `scipy.special` through scikit-learn and
Transformers. A stack capture and process inventory were retained. The native
loader/thread-start deadlock mechanism is an inference from those observed
stacks, consistent with the previously repaired NumPy startup failure.

The existing Windows preload handled NumPy for auto-enabled profiles but only
checked module specifications for their heavier entry points. Windows now
imports all existing entry points for ready native profiles before workers are
assembled. Disabled/unavailable profiles remain untouched; POSIX auto profiles
retain lazy checks. Import failures still degrade only the affected profile with
sanitized diagnostics. Windows installations with those profiles pay their import
cost during startup.

Ordering and failure-isolation regressions failed against the old behavior and
passed after the fix. The actual stalled restart was then retried against the
same installed environment, model cache and stored data, with only the reviewed
`production.py` updated in the staged editable source. Persistence, semantic
retrieval and subsequent repeat setup passed. Independent review found no
remaining material issues in the fix or the installation instructions.

## Repository verification

- Focused production checks: 23 passed and four subtests passed.
- Documentation/protocol checks: 13 passed and 41 subtests passed.
- Ruff lint and formatting: passed for the full package and tests (505 files).
- Mypy: passed for all 48 v7 source files with Linux platform selection.
- Release inventory: passed, 77 tools, 10 resources, 587 atomic requirements.
- Full regression run: 3,957 passed, 2,096 subtests passed, 76 skipped,
  510 warnings and one failure in 1,742.33 seconds. The sole failure was the
  launch-document extractor described below; this original run was not all-green.
- After correcting that extractor, both affected checks passed in 12.56 seconds,
  including actual shadow-package execution. Scoped Ruff lint/format passed.

`test_every_registration_document_shows_a_server_command` detected that its
helper omitted the grimoire's Claude JSON `command`/`args` configuration: it
previously recognized only shell registrations and OpenCode command arrays.
The helper now extracts the Claude shape and passes it through the existing
subprocess isolation assertions. An unrelated, commented HTTP-removal example
is excluded from stdio parsing; malformed relevant JSON still fails. Independent
review approved the correction. The affected checks were rerun, not the entire
already-completed suite.

The first PR CI run also exposed a platform leak in the new assembly-ordering
test. Its Windows simulation changed shared `os.name`, so macOS `pathlib` tried
to construct Windows paths while resolving configured roots. The test now
replaces only the production module's `os` reference. Runtime code is unchanged;
the same import-order and caller-thread assertions remain. The affected test
passed with both subcases, and scoped Ruff/format checks passed. Fresh PR CI
verifies this correction across the platform matrix.

The next CI run passed that regression on Linux and macOS, but Ubuntu Python
3.12 returned an empty first recall in the existing migrated-workspace test.
Its earlier database assertions confirmed the migrated record and file link;
the failed assertion omitted the response's abstention and provider diagnostics.
That assertion now includes the full response without changing its requirement,
timeouts or retry behavior. The same test passed once on Windows Python 3.12
(9.37 seconds) and once in a fresh Ubuntu/WSL Python 3.12.15 environment with CI's
`dev,apps,graph` extras (5.82 seconds). The original CI cause remains undetermined;
this is a diagnostic improvement, not a claimed runtime repair. A subsequent CI
failure will retain the missing evidence.

## Evidence and limits

Raw, ignored evidence is under `.test_tmp/acceptance-20261112-summon/evidence/`:
`final-acceptance.json`, the stage event streams and summaries,
`state-after-rerun.json`, `offline-verify-v7-stdout.json`, and
`restart1-server-stack.txt`. The disposable harness is
`.test_tmp/acceptance-20261112-summon/run_acceptance.py`.

The tested source files have these SHA-256 values:

| File | SHA-256 |
| --- | --- |
| `Summon_Daem0n.md` | `67cf00bedd312c4c8ae3067a213afb473fb3771d19cb6bf38a53e0022c7a747f` |
| `README.md` | `28f5493f9d8f95cf6ea402fa9abdd5e13306d1060e7dde55b6663cd4d5d8c106` |
| `daem0nmcp/api/v7/production.py` | `12b234416e2d547d9f618730a852ca2598ad647e56a7549b3e90019c1e1e9e95` |

The staged-source override exercises the uncommitted candidate, so the upstream
clone branch was not executed. Host callback approval and project trust were
exercised; the interactive approval UI was not. This fresh project contained no
application source to index and no legacy database to migrate. Linux/macOS AI
installation, external-storage migration, OpenCode, E2B, remote hosting and
Redis-backed tasks are not claimed by this scenario. The grimoire refuses the
unsupported custom-storage migration selection before touching a derived store.
