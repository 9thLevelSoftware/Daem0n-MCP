"""Resolution rules for the workspace identity stored with the project (KD-3).

The identity file records the ID a workspace's rows were written under, so
memories follow a project that is moved or renamed and a re-clone works by
copying ``.daem0nmcp/`` into the new clone.  These tests pin the read-only
rules: what is adopted, what is blocked, which spellings are one folder, and
that nothing read from the file is ever handed to the filesystem (R-40).
"""

from __future__ import annotations

import builtins
import hashlib
import io
import json
import os
import sqlite3
import subprocess
import sys
from contextlib import closing
from pathlib import Path
from unittest import mock

import pytest

from daem0nmcp.storage_activation import (
    DEFAULT_DATABASE_NAME,
    LOCK_NAME,
    ActiveDatabasePointer,
    write_active_pointer,
)
from daem0nmcp.workspace import (
    WORKSPACE_IDENTITY_AMBIGUOUS,
    WORKSPACE_IDENTITY_DUPLICATE,
    WorkspaceIdentityError,
    WorkspaceRegistry,
    ensure_workspace_identity,
    path_hash_workspace_id,
    reset_workspace_identity_cache,
    stored_event_counts,
    workspace_identity,
    write_workspace_identity,
)

FOREIGN = "ws_" + "a" * 24
OTHER = "ws_" + "b" * 24


@pytest.fixture(autouse=True)
def _fresh_identity_cache():
    reset_workspace_identity_cache()
    yield
    reset_workspace_identity_cache()


def _identity_file(root: Path) -> Path:
    return root / ".daem0nmcp" / "workspace-id"


def _recorded(root: Path) -> dict:
    return json.loads(_identity_file(root).read_text(encoding="utf-8"))


def _write_raw(root: Path, payload: str) -> None:
    path = _identity_file(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(payload, encoding="utf-8")


def _seed_events(root: Path, *workspace_ids: str, table: str = "memory_events") -> None:
    """Record events under each ID, the way a stored workspace carries them."""

    if table not in {"memory_events", "governance_events"}:
        raise ValueError(table)
    storage = root / ".daem0nmcp" / "storage"
    storage.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(storage / DEFAULT_DATABASE_NAME)) as database:
        database.execute(
            f"CREATE TABLE IF NOT EXISTS {table} ("  # noqa: S608
            "workspace_id TEXT NOT NULL, event_id TEXT NOT NULL)"
        )
        database.executemany(
            f"INSERT INTO {table} VALUES (?, ?)",  # noqa: S608
            [
                (workspace_id, f"evt_{index}_{workspace_id}")
                for index, workspace_id in enumerate(workspace_ids)
            ],
        )
        database.commit()


def _database_digest(root: Path) -> str:
    raw = (root / ".daem0nmcp" / "storage" / DEFAULT_DATABASE_NAME).read_bytes()
    return hashlib.sha256(raw).hexdigest()


def _project(tmp_path: Path, name: str) -> Path:
    root = tmp_path / name
    root.mkdir()
    return root


# --- Adoption and blocking -------------------------------------------------


def test_a_new_root_is_seeded_with_the_path_hash(tmp_path):
    root = _project(tmp_path, "fresh")
    assert workspace_identity(root).workspace_id == path_hash_workspace_id(root)
    ensure_workspace_identity([root])
    assert _recorded(root)["id"] == path_hash_workspace_id(root)


@pytest.mark.parametrize("with_marker", [False, True])
def test_fresh_storage_directory_does_not_block_identity(tmp_path, with_marker):
    root = _project(tmp_path, "fresh-storage")
    storage = root / ".daem0nmcp" / "storage"
    storage.mkdir(parents=True)
    if with_marker:
        (storage / LOCK_NAME).write_bytes(b"")

    registry = WorkspaceRegistry([root], default_root=root)
    assert registry.default.identity_block is None
    assert registry.default.workspace_id == path_hash_workspace_id(root)
    assert not _identity_file(root).exists()
    assert not (storage / DEFAULT_DATABASE_NAME).exists()
    ensure_workspace_identity([root])
    assert _recorded(root)["id"] == registry.default.workspace_id


@pytest.mark.parametrize("recorded", [False, True])
def test_invalid_pointer_defers_to_storage_without_writing_identity(tmp_path, recorded):
    root = _project(tmp_path, "invalid-pointer")
    if recorded:
        write_workspace_identity(root, workspace_id=FOREIGN)
    storage = root / ".daem0nmcp" / "storage"
    storage.mkdir(parents=True)
    pointer = storage / "active-db.json"
    pointer.write_bytes(b"invalid pointer")
    before = _identity_file(root).read_bytes() if recorded else None

    identity = workspace_identity(root)
    assert identity.blocked is None
    assert identity.workspace_id == (
        FOREIGN if recorded else path_hash_workspace_id(root)
    )
    ensure_workspace_identity([root])
    assert pointer.read_bytes() == b"invalid pointer"
    if recorded:
        assert _identity_file(root).read_bytes() == before
    else:
        assert not _identity_file(root).exists()
    assert not (storage / DEFAULT_DATABASE_NAME).exists()


def test_missing_pointer_target_does_not_seed_a_new_identity(tmp_path):
    root = _project(tmp_path, "missing-active")
    _seed_events(root, FOREIGN)
    storage = root / ".daem0nmcp" / "storage"
    write_active_pointer(
        storage, ActiveDatabasePointer(7, 1, DEFAULT_DATABASE_NAME, None, None)
    )
    (storage / DEFAULT_DATABASE_NAME).unlink()
    before = (storage / "active-db.json").read_bytes()

    ensure_workspace_identity([root])
    assert not _identity_file(root).exists()
    assert (storage / "active-db.json").read_bytes() == before
    assert not (storage / DEFAULT_DATABASE_NAME).exists()


@pytest.mark.parametrize("linked_component", ["managed", "storage"])
def test_empty_linked_storage_does_not_seed_identity_outside_root(
    tmp_path, linked_component
):
    root = _project(tmp_path, "linked-empty")
    outside = _project(tmp_path, "outside")
    managed = root / ".daem0nmcp"
    if linked_component == "managed":
        (outside / "storage").mkdir()
        link = managed
    else:
        managed.mkdir()
        link = managed / "storage"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except (OSError, NotImplementedError):
        if os.name != "nt":
            pytest.skip("this environment cannot create directory symlinks")
        result = subprocess.run(
            ["cmd.exe", "/d", "/c", "mklink", "/J", str(link), str(outside)],
            capture_output=True,
        )
        if result.returncode:
            pytest.skip("this environment cannot create directory junctions")
    before = sorted(path.relative_to(outside) for path in outside.rglob("*"))

    ensure_workspace_identity([root])
    assert workspace_identity(root).blocked == WORKSPACE_IDENTITY_AMBIGUOUS
    assert not _identity_file(root).exists()
    assert sorted(path.relative_to(outside) for path in outside.rglob("*")) == before


def test_a_root_with_no_file_adopts_the_single_stored_id(tmp_path):
    root = _project(tmp_path, "adopting")
    _seed_events(root, FOREIGN, FOREIGN)
    identity = workspace_identity(root)
    assert identity.workspace_id == FOREIGN
    assert identity.blocked is None
    ensure_workspace_identity([root])
    assert _recorded(root)["id"] == FOREIGN


def test_unavailable_identity_is_retried_after_a_transient_read_failure(
    tmp_path, monkeypatch
):
    from daem0nmcp import workspace as workspace_module

    root = _project(tmp_path, "temporarily-locked")
    _seed_events(root, FOREIGN)
    original = workspace_module._open_identity_reader
    unavailable = True

    def read(selected):
        if unavailable:
            raise sqlite3.OperationalError("database is locked")
        return original(selected)

    monkeypatch.setattr(workspace_module, "_open_identity_reader", read)
    first = workspace_identity(root)
    assert first.storage_unavailable
    assert first.workspace_id == path_hash_workspace_id(root)
    before = _database_digest(root)

    unavailable = False
    recovered = workspace_identity(root)
    assert not recovered.storage_unavailable
    assert recovered.workspace_id == FOREIGN
    assert _database_digest(root) == before
    assert not _identity_file(root).exists()


def test_governance_events_alone_are_adopted(tmp_path):
    root = _project(tmp_path, "gov-only")
    _seed_events(root, FOREIGN, FOREIGN, table="governance_events")
    identity = workspace_identity(root)
    assert identity.workspace_id == FOREIGN
    assert identity.blocked is None
    ensure_workspace_identity([root])
    assert _recorded(root)["id"] == FOREIGN


def test_several_governance_ids_with_no_file_are_blocked(tmp_path):
    root = _project(tmp_path, "gov-mixed")
    _seed_events(root, FOREIGN, OTHER, table="governance_events")
    assert workspace_identity(root).blocked == WORKSPACE_IDENTITY_AMBIGUOUS


def test_a_root_with_no_file_and_several_stored_ids_is_blocked(tmp_path):
    root = _project(tmp_path, "mixed")
    _seed_events(root, FOREIGN, OTHER)
    identity = workspace_identity(root)
    assert identity.blocked == WORKSPACE_IDENTITY_AMBIGUOUS
    # Nothing is written for a blocked root, so the operator decides.
    ensure_workspace_identity([root])
    assert not _identity_file(root).exists()


def test_adopting_one_id_of_a_mixed_database_hides_the_rest(tmp_path):
    root = _project(tmp_path, "mixed-adopt")
    _seed_events(root, FOREIGN, OTHER, OTHER)
    assert stored_event_counts(root) == {FOREIGN: 1, OTHER: 2}
    write_workspace_identity(root, workspace_id=OTHER)
    identity = workspace_identity(root)
    assert identity.blocked is None
    assert identity.workspace_id == OTHER


def test_a_foreign_id_absent_from_a_non_empty_database_is_blocked(tmp_path):
    root = _project(tmp_path, "foreign")
    _seed_events(root, FOREIGN)
    write_workspace_identity(root, workspace_id=OTHER)
    assert workspace_identity(root).blocked == WORKSPACE_IDENTITY_AMBIGUOUS


def test_a_recorded_fork_hides_the_inherited_events(tmp_path):
    root = _project(tmp_path, "fork")
    _seed_events(root, FOREIGN, FOREIGN, FOREIGN)
    before = _database_digest(root)
    write_workspace_identity(
        root, workspace_id=path_hash_workspace_id(root), forked_from=FOREIGN
    )
    identity = workspace_identity(root)
    assert identity.blocked is None
    assert identity.workspace_id == path_hash_workspace_id(root)
    assert identity.forked_from == FOREIGN
    # The inherited events stay exactly where they are, never rewritten.
    assert _database_digest(root) == before
    assert stored_event_counts(root) == {FOREIGN: 3}


def test_a_third_id_besides_the_fork_source_still_blocks(tmp_path):
    root = _project(tmp_path, "fork-plus")
    _seed_events(root, FOREIGN, OTHER)
    write_workspace_identity(
        root, workspace_id=path_hash_workspace_id(root), forked_from=FOREIGN
    )
    assert workspace_identity(root).blocked == WORKSPACE_IDENTITY_AMBIGUOUS


def test_a_fork_stays_blocked_when_its_own_id_and_a_third_id_are_stored(tmp_path):
    root = _project(tmp_path, "fork-own-plus")
    own = path_hash_workspace_id(root)
    _seed_events(root, FOREIGN, OTHER, own)
    write_workspace_identity(root, workspace_id=own, forked_from=FOREIGN)
    assert workspace_identity(root).blocked == WORKSPACE_IDENTITY_AMBIGUOUS


def test_a_malformed_file_is_rejected_and_never_overwritten(tmp_path):
    root = _project(tmp_path, "malformed")
    _write_raw(root, '{"id": "not-a-workspace-id"}')
    identity = workspace_identity(root)
    assert identity.blocked == WORKSPACE_IDENTITY_AMBIGUOUS
    assert identity.malformed
    ensure_workspace_identity([root])
    assert _recorded(root) == {"id": "not-a-workspace-id"}


def test_only_the_reviewed_keys_are_read_from_the_file(tmp_path):
    root = _project(tmp_path, "extra-keys")
    _write_raw(
        root,
        json.dumps(
            {
                "id": FOREIGN,
                "root_key": "0" * 64,
                "root": "\\\\host\\share\\project",
                "storage_path": "/etc/passwd",
            }
        ),
    )
    identity = workspace_identity(root)
    assert identity.workspace_id == FOREIGN
    assert identity.blocked is None
    assert not identity.root_key_matches


# --- Moving, re-cloning and duplicates -------------------------------------


def test_a_renamed_folder_keeps_its_id_and_its_root_key(tmp_path):
    root = _project(tmp_path, "before")
    ensure_workspace_identity([root])
    original = _recorded(root)
    _seed_events(root, original["id"])
    moved = tmp_path / "after"
    root.rename(moved)
    identity = workspace_identity(moved)
    assert identity.workspace_id == original["id"]
    assert identity.blocked is None
    # A same-filesystem rename keeps the inode, so nothing needs rewriting.
    assert identity.root_key_matches
    ensure_workspace_identity([moved])
    assert _recorded(moved) == original


def test_a_re_clone_copy_works_in_its_own_process(tmp_path):
    import shutil

    original = _project(tmp_path, "clone-source")
    ensure_workspace_identity([original])
    identifier = _recorded(original)["id"]
    _seed_events(original, identifier, identifier)
    clone = _project(tmp_path, "clone-target")
    shutil.copytree(original / ".daem0nmcp", clone / ".daem0nmcp")

    # Only the new clone is registered: it is the ID's only holder here.
    identity = workspace_identity(clone)
    assert identity.workspace_id == identifier
    assert identity.blocked is None
    assert not identity.root_key_matches
    # The single holder's stale key is refreshed; the ID never changes.
    ensure_workspace_identity([clone])
    assert _recorded(clone)["id"] == identifier
    assert workspace_identity(clone).root_key_matches
    assert _recorded(original)["id"] == identifier


@pytest.mark.parametrize("reversed_order", [False, True])
def test_a_copy_registered_beside_the_original_is_blocked(tmp_path, reversed_order):
    import shutil

    original = _project(tmp_path, "origin")
    ensure_workspace_identity([original])
    identifier = _recorded(original)["id"]
    _seed_events(original, identifier)
    copy = _project(tmp_path, "copy")
    shutil.copytree(original / ".daem0nmcp", copy / ".daem0nmcp")

    roots = [copy, original] if reversed_order else [original, copy]
    registry = WorkspaceRegistry(roots)
    kept = registry.resolve(original)
    blocked = registry.resolve(copy)
    assert kept.workspace_id == identifier
    assert kept.identity_block is None
    assert blocked.identity_block == WORKSPACE_IDENTITY_DUPLICATE
    assert blocked.workspace_id == path_hash_workspace_id(copy)

    # Forking the copy settles it without touching the original.
    write_workspace_identity(
        copy, workspace_id=path_hash_workspace_id(copy), forked_from=identifier
    )
    registry = WorkspaceRegistry(roots)
    assert registry.resolve(original).identity_block is None
    forked = registry.resolve(copy)
    assert forked.identity_block is None
    assert forked.workspace_id == path_hash_workspace_id(copy)

    # Adopting the original's ID again restores the duplicate block.
    write_workspace_identity(copy, workspace_id=identifier)
    registry = WorkspaceRegistry(roots)
    assert registry.resolve(copy).identity_block == WORKSPACE_IDENTITY_DUPLICATE


def test_two_folders_that_both_mismatch_their_key_are_both_blocked(tmp_path):
    left = _project(tmp_path, "restored-a")
    right = _project(tmp_path, "restored-b")
    payload = json.dumps({"id": FOREIGN, "root_key": "0" * 64})
    _write_raw(left, payload)
    _write_raw(right, payload)
    registry = WorkspaceRegistry([left, right])
    assert registry.resolve(left).identity_block == WORKSPACE_IDENTITY_DUPLICATE
    assert registry.resolve(right).identity_block == WORKSPACE_IDENTITY_DUPLICATE
    # A blocked root is never written to, so a restored backup stays as it is.
    ensure_workspace_identity([left, right])
    assert _recorded(left) == json.loads(payload)


# --- One folder, several spellings (R-28) ----------------------------------


def _assert_one_workspace(primary: Path, alias: Path) -> None:
    registry = WorkspaceRegistry([primary, alias])
    first = registry.resolve(primary)
    second = registry.resolve(alias)
    assert first.identity_block is None, first
    assert second.identity_block is None, second
    assert first.workspace_id == second.workspace_id


def test_a_relative_spelling_of_the_same_folder_is_not_a_copy(tmp_path):
    root = _project(tmp_path, "spelled")
    (root / "sub").mkdir()
    ensure_workspace_identity([root])
    _seed_events(root, _recorded(root)["id"])
    _assert_one_workspace(root, root / "sub" / "..")


def test_a_case_variant_of_the_same_folder_is_not_a_copy(tmp_path):
    root = _project(tmp_path, "CaseProject")
    variant = tmp_path / "caseproject"
    if not variant.is_dir():
        pytest.skip("this filesystem is case-sensitive")
    ensure_workspace_identity([root])
    _seed_events(root, _recorded(root)["id"])
    _assert_one_workspace(root, variant)


def test_a_link_to_the_same_folder_is_not_a_copy(tmp_path):
    root = _project(tmp_path, "linked")
    link = tmp_path / "link-to-linked"
    try:
        os.symlink(root, link, target_is_directory=True)
    except (OSError, NotImplementedError, AttributeError):
        if sys.platform != "win32":
            pytest.skip("this environment cannot create directory links")
        completed = subprocess.run(  # noqa: S603
            ["cmd", "/c", "mklink", "/J", str(link), str(root)],
            capture_output=True,
        )
        if completed.returncode != 0 or not link.exists():
            pytest.skip("this environment cannot create directory junctions")
    ensure_workspace_identity([root])
    _seed_events(root, _recorded(root)["id"])
    _assert_one_workspace(root, link)


@pytest.mark.skipif(sys.platform != "win32", reason="subst is a Windows mapping")
def test_a_subst_drive_of_the_same_folder_is_not_a_copy(tmp_path):
    letter = next(
        (
            candidate
            for candidate in ("Y:", "W:", "V:")
            if not Path(candidate + "\\").exists()
        ),
        None,
    )
    if letter is None:
        pytest.skip("no free drive letter for subst")
    root = _project(tmp_path, "substituted")
    created = subprocess.run(  # noqa: S603
        ["subst", letter, str(root)], capture_output=True, shell=True
    )
    if created.returncode != 0:
        pytest.skip("subst is unavailable here")
    try:
        ensure_workspace_identity([root])
        _seed_events(root, _recorded(root)["id"])
        _assert_one_workspace(root, Path(letter + "\\"))
        first = _identity_file(root).read_bytes()
        ensure_workspace_identity([Path(letter + "\\")])
        assert _identity_file(root).read_bytes() == first
    finally:
        subprocess.run(["subst", letter, "/d"], capture_output=True, shell=True)  # noqa: S603


def test_alternating_spellings_never_rewrite_the_root_key(tmp_path):
    root = _project(tmp_path, "alternating")
    (root / "sub").mkdir()
    ensure_workspace_identity([root])
    first = _identity_file(root).read_bytes()
    # A second process starting at the other spelling must agree, not flip.
    ensure_workspace_identity([root / "sub" / ".."])
    assert _identity_file(root).read_bytes() == first
    ensure_workspace_identity([root])
    assert _identity_file(root).read_bytes() == first


# --- No filesystem access driven by the file (R-40) ------------------------


class _PathRecorder:
    """Refuse, and record, every filesystem call naming the forbidden host."""

    def __init__(self, *needles: str) -> None:
        self.needles = tuple(needle.lower() for needle in needles)
        self.calls: list[str] = []

    def guard(self, original):
        def wrapper(path, *args, **kwargs):
            try:
                text = os.fspath(path)
            except TypeError:
                text = ""
            if isinstance(text, bytes):
                text = text.decode("utf-8", "replace")
            if any(needle in text.lower() for needle in self.needles):
                self.calls.append(text)
                raise OSError("the identity file must never be dereferenced")
            return original(path, *args, **kwargs)

        return wrapper


def _forbidden_host_patches(recorder: _PathRecorder):
    return (
        mock.patch("os.stat", recorder.guard(os.stat)),
        mock.patch("os.path.exists", recorder.guard(os.path.exists)),
        mock.patch.object(Path, "exists", recorder.guard(Path.exists)),
        mock.patch("builtins.open", recorder.guard(builtins.open)),
        mock.patch("io.open", recorder.guard(io.open)),
    )


@pytest.mark.parametrize("spelling", ["\\\\host\\share\\project", "//host/share/proj"])
def test_a_root_in_the_file_is_never_touched(tmp_path, spelling):
    root = _project(tmp_path, "unc")
    _write_raw(
        root,
        json.dumps({"id": FOREIGN, "root_key": "0" * 64, "root": spelling}),
    )
    _seed_events(root, FOREIGN)
    recorder = _PathRecorder("host\\share", "host/share")
    patches = _forbidden_host_patches(recorder)
    for patch in patches:
        patch.start()
    try:
        reset_workspace_identity_cache()
        identity = workspace_identity(root)
    finally:
        for patch in reversed(patches):
            patch.stop()
    assert identity.workspace_id == FOREIGN
    assert identity.blocked is None
    assert recorder.calls == []
    # The guard really fires, so an empty call list means the path was unused.
    with pytest.raises(OSError):
        recorder.guard(os.stat)(spelling)
    assert recorder.calls == [spelling]


def test_the_session_start_hook_never_touches_a_root_named_in_the_file(tmp_path):
    from daem0nmcp.claude_hooks import session_start

    root = _project(tmp_path, "hooked")
    _write_raw(
        root,
        json.dumps(
            {"id": FOREIGN, "root_key": "0" * 64, "root": "\\\\host\\share\\project"}
        ),
    )
    _seed_events(root, FOREIGN)
    recorder = _PathRecorder("host\\share", "host/share")
    patches = _forbidden_host_patches(recorder)
    for patch in patches:
        patch.start()
    try:
        reset_workspace_identity_cache()
        message = session_start._hook_message(
            session_start.workspace_identity(str(root))
        )
    finally:
        for patch in reversed(patches):
            patch.stop()
    assert FOREIGN in message
    assert recorder.calls == []


def test_the_pre_bash_hook_reads_no_identity_file(tmp_path):
    from daem0nmcp.claude_hooks import _client, pre_bash

    root = _project(tmp_path, "prebash")
    _write_raw(
        root,
        json.dumps(
            {"id": FOREIGN, "root_key": "0" * 64, "root": "\\\\host\\share\\project"}
        ),
    )
    recorder = _PathRecorder("host\\share", "host/share")
    patches = _forbidden_host_patches(recorder)
    for patch in patches:
        patch.start()
    try:
        result = _client.run_async(pre_bash.async_main(str(root), "ls"))
    finally:
        for patch in reversed(patches):
            patch.stop()
    assert result.blocked is False
    assert recorder.calls == []


# --- Persistence -----------------------------------------------------------


def test_concurrent_starts_write_the_same_value_and_a_gitignore(tmp_path):
    root = _project(tmp_path, "concurrent")
    ensure_workspace_identity([root])
    first = _identity_file(root).read_bytes()
    ensure_workspace_identity([root])
    assert _identity_file(root).read_bytes() == first
    assert (root / ".daem0nmcp" / ".gitignore").read_text(encoding="utf-8") == "*\n"


def test_an_existing_store_keeps_its_rows_when_the_file_is_written(tmp_path):
    root = _project(tmp_path, "pre-pr")
    identifier = path_hash_workspace_id(root)
    _seed_events(root, identifier, identifier)
    before = _database_digest(root)
    ensure_workspace_identity([root])
    assert _recorded(root)["id"] == identifier
    assert _database_digest(root) == before


def test_a_differently_spelled_project_root_env_var_is_one_workspace(tmp_path):
    root = _project(tmp_path, "environment")
    (root / "sub").mkdir()
    ensure_workspace_identity([root])
    _seed_events(root, _recorded(root)["id"])
    registry = WorkspaceRegistry.from_environment(
        {
            "DAEM0NMCP_PROJECT_ROOT": str(root / "sub" / ".."),
            "DAEM0NMCP_WORKSPACE_ROOTS": json.dumps([str(root)]),
        }
    )
    assert registry.default.identity_block is None
    assert registry.resolve(root).workspace_id == registry.default.workspace_id


# --- The offline command ---------------------------------------------------


def _run_cli(root: Path, *arguments: str) -> int:
    from daem0nmcp import cli

    previous = os.environ.get("DAEM0NMCP_PROJECT_ROOT")
    argv = sys.argv
    sys.argv = ["daem0nmcp", "--project-path", str(root), "--json", *arguments]
    try:
        with pytest.raises(SystemExit) as raised:
            cli.main()
        return int(raised.value.code)
    finally:
        sys.argv = argv
        if previous is None:
            os.environ.pop("DAEM0NMCP_PROJECT_ROOT", None)
        else:
            os.environ["DAEM0NMCP_PROJECT_ROOT"] = previous
        reset_workspace_identity_cache()


def test_adopt_reports_the_events_it_hides(tmp_path, capsys):
    root = _project(tmp_path, "cli-adopt")
    _seed_events(root, FOREIGN, OTHER, OTHER)
    assert _run_cli(root, "workspace-id") == 1
    blocked = json.loads(capsys.readouterr().out.strip())
    assert blocked["blocked"] == WORKSPACE_IDENTITY_AMBIGUOUS
    assert blocked["events_by_workspace_id"] == {FOREIGN: 1, OTHER: 2}

    assert _run_cli(root, "workspace-id", "--adopt", OTHER) == 0
    adopted = json.loads(capsys.readouterr().out.strip())
    assert adopted["workspace_id"] == OTHER
    assert adopted["hidden_event_count"] == 1


def test_new_on_a_nested_reclone_keeps_the_hidden_id(tmp_path):
    import shutil

    original = _project(tmp_path, "nested-origin")
    ensure_workspace_identity([original])
    identifier = _recorded(original)["id"]
    _seed_events(original, identifier)
    first = _project(tmp_path, "nested-first")
    shutil.copytree(original / ".daem0nmcp", first / ".daem0nmcp")
    assert _run_cli(first, "workspace-id", "--new") == 0
    assert _recorded(first)["forked_from"] == identifier
    second = _project(tmp_path, "nested-second")
    shutil.copytree(first / ".daem0nmcp", second / ".daem0nmcp")
    assert _run_cli(second, "workspace-id", "--new") == 0
    assert _recorded(second)["forked_from"] == identifier
    assert workspace_identity(second).blocked is None
    assert _run_cli(second, "workspace-id", "--new") == 0
    assert _recorded(second)["forked_from"] == identifier
    assert workspace_identity(second).blocked is None


def test_new_forks_from_the_id_that_blocked_the_folder(tmp_path):
    import shutil

    original = _project(tmp_path, "fork-origin")
    ensure_workspace_identity([original])
    identifier = _recorded(original)["id"]
    _seed_events(original, identifier)
    copy = _project(tmp_path, "fork-copy")
    shutil.copytree(original / ".daem0nmcp", copy / ".daem0nmcp")
    # Blocked as a duplicate while both are registered, the copy still knows
    # which ID it recorded, so the fork hides exactly those events.
    assert (
        WorkspaceRegistry([original, copy]).resolve(copy).identity_block
        == WORKSPACE_IDENTITY_DUPLICATE
    )
    assert _run_cli(copy, "workspace-id", "--new") == 0
    assert _recorded(copy) == {
        "forked_from": identifier,
        "id": path_hash_workspace_id(copy),
        "root_key": _recorded(copy)["root_key"],
    }
    registry = WorkspaceRegistry([original, copy])
    assert registry.resolve(copy).identity_block is None
    assert registry.resolve(original).workspace_id == identifier


def test_an_unreadable_store_is_not_seeded_as_a_new_root(tmp_path):
    root = _project(tmp_path, "unread")
    _seed_events(root, FOREIGN)
    (root / ".daem0nmcp" / "storage" / DEFAULT_DATABASE_NAME).write_bytes(
        b"not a SQLite database"
    )
    identity = workspace_identity(root)
    assert identity.blocked is None
    assert identity.storage_unavailable
    ensure_workspace_identity([root])
    assert not _identity_file(root).exists()
    assert workspace_identity(root).storage_unavailable


def test_a_linked_identity_file_is_malformed_and_never_followed(tmp_path):
    root = _project(tmp_path, "linked-id")
    _seed_events(root, FOREIGN)
    target = tmp_path / "outside-identity"
    target.write_text('{"id": "' + FOREIGN + '", "root_key": "' + "0" * 64 + '"}')
    path = _identity_file(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.symlink(target, path)
    except (OSError, NotImplementedError):
        pytest.skip("this environment cannot create file symlinks")
    identity = workspace_identity(root)
    assert identity.malformed
    assert identity.blocked == WORKSPACE_IDENTITY_AMBIGUOUS
    ensure_workspace_identity([root])
    assert path.is_symlink()
    recorder = _PathRecorder(str(target))
    patches = _forbidden_host_patches(recorder)
    for patch in patches:
        patch.start()
    try:
        reset_workspace_identity_cache()
        workspace_identity(root)
    finally:
        for patch in reversed(patches):
            patch.stop()
    assert recorder.calls == []


def test_an_oversized_identity_file_is_rejected_without_a_full_read(
    tmp_path, monkeypatch
):
    root = _project(tmp_path, "huge")
    _write_raw(root, '{"id": "' + FOREIGN + '"}' + ("x" * 5000))
    reads: list[int] = []
    real_read = os.read

    def capped(fd, n):
        reads.append(n)
        return real_read(fd, n)

    monkeypatch.setattr(os, "read", capped)
    identity = workspace_identity(root)
    assert identity.blocked == WORKSPACE_IDENTITY_AMBIGUOUS
    assert identity.malformed
    assert reads == []


def test_a_seed_does_not_clobber_a_published_identity(tmp_path):
    from daem0nmcp.workspace import _write_identity_file

    root = _project(tmp_path, "seed-keep")
    write_workspace_identity(root, workspace_id=OTHER)
    assert (
        _write_identity_file(root, path_hash_workspace_id(root), None, replace=False)
        is False
    )
    assert _recorded(root)["id"] == OTHER


def test_a_failed_seed_is_not_served(tmp_path, monkeypatch):
    from daem0nmcp import workspace as workspace_module

    root = _project(tmp_path, "seed-fail")
    monkeypatch.setattr(workspace_module, "_write_identity_file", lambda *a, **k: False)
    ensure_workspace_identity([root])
    assert not _identity_file(root).exists()
    assert workspace_identity(root).blocked == WORKSPACE_IDENTITY_AMBIGUOUS


def test_concurrent_starts_survive_a_lost_rename(tmp_path, monkeypatch):
    """Two starts publish the same value; the loser must not take the server down."""

    import threading

    from daem0nmcp import workspace as workspace_module

    root = _project(tmp_path, "racing")
    barrier = threading.Barrier(4)
    failures: list[BaseException] = []

    def start() -> None:
        try:
            barrier.wait(timeout=30)
            ensure_workspace_identity([root])
        except BaseException as error:  # noqa: BLE001 - reported, not raised
            failures.append(error)

    threads = [threading.Thread(target=start) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
    assert failures == []
    assert _recorded(root)["id"] == path_hash_workspace_id(root)

    # A rename that never lands is only a failure when the file still does not
    # carry the intended ID; an explicit command says so, a start does not.
    def refuse(*_args, **_kwargs):
        raise PermissionError(5, "Access is denied")

    monkeypatch.setattr(workspace_module.os, "replace", refuse)
    ensure_workspace_identity([root])
    with pytest.raises(WorkspaceIdentityError):
        write_workspace_identity(root, workspace_id=OTHER)
    assert _recorded(root)["id"] == path_hash_workspace_id(root)
    assert not list(_identity_file(root).parent.glob(".workspace-id-*.tmp"))
