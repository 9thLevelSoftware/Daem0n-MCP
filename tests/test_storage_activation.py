"""Dependency-free tests for the v7 database activation boundary."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path


class StorageActivationTests(unittest.TestCase):
    def _api(self):
        try:
            from daem0nmcp.storage_activation import (
                ActiveDatabasePointer,
                PointerValidationError,
                resolve_active_database,
                write_active_pointer,
            )
        except ImportError as exc:  # intentional RED before implementation
            self.fail(f"storage activation API is missing: {exc}")
        return (
            ActiveDatabasePointer,
            PointerValidationError,
            resolve_active_database,
            write_active_pointer,
        )

    def _lock_api(self):
        from daem0nmcp.storage_activation import (
            DatabaseFileLock,
            PointerValidationError,
        )

        return DatabaseFileLock, PointerValidationError

    def test_absent_pointer_resolves_existing_v6_without_writes(self):
        """Legacy storage must remain generation zero and strictly read-only."""
        _, _, resolve_active_database, _ = self._api()
        with tempfile.TemporaryDirectory() as raw:
            storage = Path(raw)
            (storage / "daem0nmcp.db").write_bytes(b"legacy")
            before = sorted(path.name for path in storage.iterdir())
            resolved = resolve_active_database(storage)
            self.assertEqual(resolved.format_version, 6)
            self.assertEqual(resolved.generation, 0)
            self.assertEqual(resolved.relative_path, "daem0nmcp.db")
            self.assertEqual(resolved.path, storage / "daem0nmcp.db")
            self.assertIsNone(resolved.previous_db)
            self.assertIsNone(resolved.migration_run_id)
            self.assertEqual(sorted(path.name for path in storage.iterdir()), before)

    def test_pointer_link_check_precedes_absence_fallback(self):
        """A dangling pointer symlink must never be interpreted as no pointer."""
        source = (
            Path(__file__).resolve().parents[1] / "daem0nmcp" / "storage_activation.py"
        ).read_text(encoding="utf-8")
        link_check = source.index("if pointer_path.is_symlink()")
        absent_fallback = source.index("if not pointer_path.exists()")
        self.assertLess(link_check, absent_fallback)

    def test_pointer_has_exact_canonical_bytes_and_round_trips(self):
        """Pointer replacement must publish the decision-complete wire format."""
        pointer_type, _, resolve_active_database, write_pointer = self._api()
        run_id = "mig_" + "a" * 64
        with tempfile.TemporaryDirectory() as raw:
            storage = Path(raw)
            target = storage / "migrations" / "v7" / run_id / "candidate.db"
            target.parent.mkdir(parents=True)
            target.write_bytes(b"candidate")
            (storage / "daem0nmcp.db").write_bytes(b"legacy")
            pointer = pointer_type(
                format_version=7,
                generation=2,
                active_db=f"migrations/v7/{run_id}/candidate.db",
                previous_db="daem0nmcp.db",
                migration_run_id=run_id,
            )
            write_pointer(storage, pointer)
            expected = (
                '{"active_db":"migrations/v7/'
                + run_id
                + '/candidate.db","format_version":7,"generation":2,'
                + '"migration_run_id":"'
                + run_id
                + '","previous_db":"daem0nmcp.db"}'
            ).encode("utf-8")
            self.assertEqual((storage / "active-db.json").read_bytes(), expected)
            resolved = resolve_active_database(storage)
            self.assertEqual(resolved.pointer, pointer)
            self.assertEqual(resolved.path, target)
            self.assertFalse((storage / "active-db.json.tmp").exists())

    def test_unsafe_or_noncanonical_pointer_fails_closed(self):
        """Unknown fields, traversal, absolute paths and symlinks never fall back."""
        _, pointer_error_type, resolve_active_database, _ = self._api()
        run_id = "mig_" + "b" * 64
        with tempfile.TemporaryDirectory() as raw:
            storage = Path(raw)
            (storage / "daem0nmcp.db").write_bytes(b"legacy")
            base = {
                "format_version": 7,
                "generation": 1,
                "active_db": "daem0nmcp.db",
                "previous_db": None,
                "migration_run_id": run_id,
            }
            invalid_values = [
                {**base, "unexpected": True},
                {**base, "active_db": "../outside.db"},
                {**base, "active_db": str(storage / "daem0nmcp.db")},
                {**base, "active_db": "migrations/stream:alternate/candidate.db"},
                {**base, "format_version": 8},
                {**base, "generation": True},
            ]
            for value in invalid_values:
                with self.subTest(value=value):
                    (storage / "active-db.json").write_text(
                        json.dumps(value, separators=(",", ":")), encoding="utf-8"
                    )
                    with self.assertRaises(pointer_error_type):
                        resolve_active_database(storage)
            link = storage / "linked.db"
            try:
                os.symlink(storage / "daem0nmcp.db", link)
            except (OSError, NotImplementedError):
                self.skipTest("symlinks unavailable in this test environment")
            (storage / "active-db.json").write_text(
                json.dumps({**base, "active_db": "linked.db"}, separators=(",", ":")),
                encoding="utf-8",
            )
            with self.assertRaises(pointer_error_type):
                resolve_active_database(storage)

    def test_fresh_pointer_requires_null_migration_fields(self):
        """Only generation-one daem0nmcp.db may represent a fresh v7 database."""
        pointer_type, pointer_error_type, _, write_pointer = self._api()
        with tempfile.TemporaryDirectory() as raw:
            storage = Path(raw)
            (storage / "daem0nmcp.db").write_bytes(b"fresh")
            fresh = pointer_type(7, 1, "daem0nmcp.db", None, None)
            write_pointer(storage, fresh)
            self.assertTrue((storage / "active-db.json").is_file())
            invalid = pointer_type(7, 2, "daem0nmcp.db", None, None)
            with self.assertRaises(pointer_error_type):
                write_pointer(storage, invalid)

    def test_pointer_temp_is_exclusively_owned_and_never_follows_links(self):
        """A stale/non-regular/link temp cannot redirect the pointer write."""
        pointer_type, pointer_error_type, _, write_pointer = self._api()
        with tempfile.TemporaryDirectory() as raw:
            storage = Path(raw)
            (storage / "daem0nmcp.db").write_bytes(b"fresh")
            temporary = storage / "active-db.json.tmp"
            temporary.mkdir()
            with self.assertRaises(pointer_error_type):
                write_pointer(storage, pointer_type(7, 1, "daem0nmcp.db", None, None))

        with tempfile.TemporaryDirectory() as raw:
            storage = Path(raw)
            (storage / "daem0nmcp.db").write_bytes(b"fresh")
            (storage / "active-db.json.tmp").write_bytes(b"unowned-stale-temp")
            with self.assertRaises(pointer_error_type):
                write_pointer(storage, pointer_type(7, 1, "daem0nmcp.db", None, None))

        with (
            tempfile.TemporaryDirectory() as raw,
            tempfile.TemporaryDirectory() as outside,
        ):
            storage = Path(raw)
            victim = Path(outside) / "victim.json"
            victim.write_bytes(b"do-not-touch")
            (storage / "daem0nmcp.db").write_bytes(b"fresh")
            try:
                os.symlink(victim, storage / "active-db.json.tmp")
            except (OSError, NotImplementedError):
                self.skipTest("symlinks unavailable in this test environment")
            with self.assertRaises(pointer_error_type):
                write_pointer(storage, pointer_type(7, 1, "daem0nmcp.db", None, None))
            self.assertEqual(b"do-not-touch", victim.read_bytes())

    def test_lock_rejects_non_regular_or_symlink_path_without_touching_target(self):
        """The advisory lock path is an untrusted filesystem boundary."""
        lock_type, pointer_error_type = self._lock_api()
        with tempfile.TemporaryDirectory() as raw:
            storage = Path(raw)
            (storage / ".migrate-v7.lock").mkdir()
            with self.assertRaises(pointer_error_type):
                lock_type(storage, "exclusive").acquire()

        with (
            tempfile.TemporaryDirectory() as raw,
            tempfile.TemporaryDirectory() as outside,
        ):
            storage = Path(raw)
            victim = Path(outside) / "victim.lock"
            victim.write_bytes(b"external")
            try:
                os.symlink(victim, storage / ".migrate-v7.lock")
            except (OSError, NotImplementedError):
                self.skipTest("symlinks unavailable in this test environment")
            with self.assertRaises(pointer_error_type):
                lock_type(storage, "exclusive").acquire()
            self.assertEqual(b"external", victim.read_bytes())

    def test_shared_lifetime_lock_excludes_migration_then_releases(self):
        """Many managers may coexist, while apply requires exclusive ownership."""
        from daem0nmcp.storage_activation import DatabaseInUseError

        lock_type, _ = self._lock_api()
        with tempfile.TemporaryDirectory() as raw:
            storage = Path(raw)
            first = lock_type(storage, "shared").acquire()
            second = lock_type(storage, "shared").acquire()
            try:
                with self.assertRaises(DatabaseInUseError):
                    lock_type(storage, "exclusive").acquire()
            finally:
                second.release()
                first.release()
            exclusive = lock_type(storage, "exclusive").acquire()
            exclusive.release()


if __name__ == "__main__":
    unittest.main()
