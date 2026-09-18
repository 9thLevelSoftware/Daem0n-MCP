# P10 final local certification checkpoint

Date: 2026-09-17

## Status

This checkpoint records certification evidence from the uncommitted
`v7/completion` tree based on `cc08b4f`. It is not RC, final-commit, or release
acceptance. Accepted final-release requirements remain **0 / 571**.

The full Python suite below completed before the final two-file projection
quiescence fix. Independent review closed that final HIGH finding after its one
focused regression and Ruff passed. The full suite was not rerun after that
fix. The hashed `final3` development artifacts and their clean-install and
transport certifications also predate the fix, so they are not artifacts of
the current tree. They must be rebuilt and recertified after the external
blockers are resolved and a final commit exists.

## Distribution artifacts

The development artifacts are in `.tmp/final-dist-final3`:

| Artifact | Size (bytes) | SHA256 |
|---|---:|---|
| Wheel | 1,081,772 | `641D8E19905FBC2164803FCCCC780C8B7B6565AD12D80D110A0BB9E7F9D71CA5` |
| Source distribution | 1,422,044 | `1DDD4206412DAF51E3724AA20308638A1B332166270826BB756827E83B8FBE2E` |

Twine validation and required-asset checks passed.

## Completed checks

- The Python 3.12 suite completed with **2,937 passed, 26 skipped, 506
  warnings, and 1,410 subtests** in **821.48 seconds**. JUnit evidence is
  `.tmp/full-suite-schema32-final3.xml`.
- Ruff formatting covered 463 files and all lint checks passed. The v7 mypy
  check covered 46 modules. All 23 JavaScript tests passed. Inventory checks
  found 75 tools, 10 resources, and 571 requirements. The uv lock and diff
  checks and the UI build passed.
- Clean Windows core installations from the exact wheel passed on Python 3.10
  and 3.12, including installation, `pip check`, CLI help, and verification
  that imports came from `site-packages`.
- The exact source distribution passed a clean Python 3.12 fresh-bootstrap
  stdio and Streamable HTTP ritual through restart. Evidence is
  `.tmp/final-cert-sdist312b/transport-certification.log`.
- The exact wheel passed Linux Docker certification on Python 3.10, 3.11, and
  3.12, including `pip check` and fresh stdio and Streamable HTTP rituals
  through restart. Evidence is `.tmp/final-linux-cert/python-3.10.log`,
  `.tmp/final-linux-cert/python-3.11.log`, and
  `.tmp/final-linux-cert/python-3.12.log`.
- The exact wheel's `models-local` profile installed 115 packages on Python
  3.11. `pip check` passed; `onnxruntime` 1.30, `sentence-transformers` 5.7,
  `llmlingua` 0.2.2, and `numpy` 2.4.6 imported; and the capability registry
  reported ready. Evidence is `.tmp/final-cert-models311/README.txt`.
- A real local Claude Code workflow completed the enforced edit, capture,
  exact reviewed promotion, and recall path. This does not establish remote
  bridge or host certification.

## Incomplete scale evidence

The existing 100,000-memory/1,000,000-event fixture is schema 30 while the
current schema is 32, and it has been modified to 100,003 memories and
1,000,003 events. Any final measurement from that fixture is invalid and the
scale checkpoint remains incomplete. This is an evidence-fixture problem, not
an observed production defect. Certification requires an offline-migrated copy
or a fresh reseed before measurement.

## Remaining release gates

- Actual E2B key and service validation.
- Authenticated remote Qdrant validation; current evidence covers local
  authentication only.
- Real remote Claude and OpenCode bridge and host validation, plus supported
  OpenCode V2 native hooks.
- macOS certification.
- Frozen current-schema 100,000-memory/1,000,000-event performance, dense,
  relevance, and migration measurements.
- The eight-hour mixed soak.
- A final commit and RC built and verified from that commit.
