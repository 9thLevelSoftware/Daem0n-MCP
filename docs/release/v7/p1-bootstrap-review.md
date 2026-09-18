# P1 fresh-workspace bootstrap final repair review

Date: 2026-09-17

## Decision

**ACCEPTED for the bounded cross-platform fresh-bootstrap repair.** The Windows
junction repair and the subsequent POSIX retained-directory publication repair
close the reviewed outside-write races. I found no remaining material defect in
this slice. This is not whole-P1 or release acceptance.

## Finding

### Resolved HIGH — POSIX pathname-based bootstrap writes escaped the retained directory

Affected components: `daem0nmcp/protected_files.py` (`DirectoryAncestryGuard`),
`daem0nmcp/api/v7/workspace_bootstrap.py`
(`bootstrap_registered_workspace`), and `daem0nmcp/database.py`
(`DatabaseManager.init_db`).

On Windows, the guard opens every directory without delete sharing, so its
retained handles prevent storage or ancestor rename until bootstrap closes. On
POSIX, an open directory descriptor does not prevent rename. The bootstrap
still opens SQLite and its activation files through stored pathnames. An
attacker can therefore rename the guarded storage directory after the last
pre-`init_db` verification, replace its pathname with a symlink to an outside
directory, and let the real initializer follow that new pathname.

The independent Linux/Docker probe wraps the real `_open_exclusive_manager`
only to place the swap at that deterministic boundary, then calls the real
`DatabaseManager.init_db`. SQLite creates and migrates an outside database.
`write_active_pointer` later rejects the linked storage, but that check occurs
after the outside write:

```text
{
  'bootstrap_error': 'WORKSPACE_BOOTSTRAP_FAILED',
  'bootstrap_cause': "PointerValidationError('UNSAFE_ACTIVE_POINTER: storage contains a link or reparse point')",
  'outside_database_created': True,
  'outside_pointer_created': False,
  'outside_entries': ['daem0nmcp.db']
}
```

This violates the required outside byte-invariance even though bootstrap
eventually reports failure. The same underlying gap also exists before the
initial `mkdir`: checks performed before a pathname operation cannot prevent a
POSIX ancestor swap during that operation.

The repair now builds and validates the fresh database in a private staging
directory, closes its manager, converts away from WAL, and publishes the closed
database plus canonical pointer relative to a duplicated descriptor for the
original guarded storage directory. Publication uses exclusive temporary files,
descriptor-relative hard links, fsync, and no pathname resolution through the
mutable workspace. A final ancestry check still reports the attack as
`WORKSPACE_STORAGE_UNSAFE`.

The same production exploit, moved to the new publication boundary, now leaves
the outside directory empty. The database and pointer are published only into
the original directory object, after which the changed pathname is detected:

```text
{
  'bootstrap_error': 'WORKSPACE_STORAGE_UNSAFE',
  'outside_database_created': False,
  'outside_pointer_created': False,
  'outside_entries': [],
  'original_storage_entries': ['.migrate-v7.lock', 'active-db.json', 'daem0nmcp.db']
}
```

## Accepted Windows repair

`guard_directory_ancestry` opens the workspace boundary and each descendant
with `FILE_FLAG_OPEN_REPARSE_POINT`, rejects reparse tags, records volume/file
identity, and retains the handles without delete sharing through manager close.
The separately retained `.migrate-v7.lock` descriptor prevents marker removal
or rename. Reverification rejects identity changes before manager construction,
after construction, and around initialization.

The original prepare-to-manager junction probe now fails before reaching its
outside target. A separate operating-system probe confirmed all of the
following fail while preparation remains open:

```text
('unlink-marker', False, 'PermissionError:32')
('rename-marker', False, 'PermissionError:32')
('rename-storage', False, 'PermissionError:5')
('rename-managed-ancestor', False, 'PermissionError:5')
('rename-workspace-boundary', False, 'PermissionError:5')
```

The final-close loop remains shielded through repeated cancellation. Normal
concurrent startup still publishes one generation, existing database/pointer
state remains unchanged, and the locked fresh predicate rejects foreign
entries.

## Independent verification

- Python 3.12:
  `pytest tests/api_v7/test_process_bootstrap.py -q` — **15 passed, 3 skipped**
  in 21.45 seconds, exit 0.
- Python 3.10:
  `pytest tests/api_v7/test_process_bootstrap.py -q` — **15 passed, 3 skipped**
  in 22.76 seconds, exit 0.
- Python 3.12:
  `pytest tests/test_workspace_security.py tests/test_storage_activation.py -q`
  — **16 passed, 5 skipped, 13 subtests passed**, exit 0.
- Ruff on the reviewed bootstrap, guard, activation, database, and test files:
  **passed**, exit 0.
- `.tmp/p1-windows-guard-probe.py`: marker and every guarded directory rename
  attempt was denied, exit 0.
- `.tmp/p1-posix-guard-probe.py`: WSL Ubuntu demonstrated that open POSIX guard
  descriptors allow the rename and detect it only after redirected writes,
  exit 0.
- Linux `python:3.12-slim` on a native container filesystem ran both POSIX race
  regressions, normal parallel bootstrap, and existing-state preservation:
  **4 passed**, exit 0.
- `.tmp/p1-posix-bootstrap-exploit.py`: Linux `python:3.12-slim` exercised the
  repaired production publication boundary; the outside directory remained
  empty and the changed pathname failed closed, exit 0.

Author evidence also records **24 passed, 6 skipped, 5 subtests passed** on
Python 3.10 and **92 passed, 8 skipped, 32 subtests passed** on Python 3.12.
