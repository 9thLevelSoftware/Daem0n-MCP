from __future__ import annotations

import ast
import hashlib
import json
import sqlite3
import unittest
from pathlib import Path

WORKSPACE_A = "ws_0123456789abcdef01234567"
WORKSPACE_B = "ws_89abcdef0123456701234567"


def _migration_19() -> tuple[int, str, list[str]]:
    from daem0nmcp.migrations.schema import MIGRATIONS

    migration = next((item for item in MIGRATIONS if item[0] == 19), None)
    if migration is None:
        raise AssertionError("additive public-object-ID migration 19 is missing")
    return migration


def _connection() -> sqlite3.Connection:
    connection = sqlite3.connect(":memory:")
    connection.execute("PRAGMA foreign_keys=ON")
    for statement in _migration_19()[2]:
        connection.execute(statement)
    return connection


def _expected_id(
    prefix: str,
    workspace_id: str,
    kind: str,
    encoded_source_key: str,
    generation: int,
) -> str:
    # Independent hand-derived canonical JSON fixture for the public ID domain.
    value = [
        "daem0nmcp",
        "v7",
        "public-object-id",
        workspace_id,
        kind,
        encoded_source_key,
        generation,
    ]
    encoded = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    return f"{prefix}_{hashlib.sha256(encoded).hexdigest()}"


class Migration19Tests(unittest.TestCase):
    def test_migration_creates_integrity_protected_mapping_table(self) -> None:
        # Catches a migration that records the version without the mapping table.
        connection = _connection()
        try:
            columns = {
                row[1]
                for row in connection.execute("PRAGMA table_info(public_object_ids)")
            }
            triggers = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='trigger'"
                )
            }
            self.assertEqual(
                columns,
                {
                    "workspace_id",
                    "object_kind",
                    "source_key",
                    "projection_generation",
                    "public_id",
                    "created_at_us",
                },
            )
            self.assertTrue(
                {
                    "public_object_ids_no_update",
                    "public_object_ids_no_delete",
                }
                <= triggers
            )
        finally:
            connection.close()

    def test_database_constraints_reject_wrong_prefix_or_generation(self) -> None:
        # Catches rows that could make a kind or projection generation ambiguous.
        connection = _connection()
        try:
            connection.execute(
                "INSERT INTO public_object_ids VALUES (?,?,?,?,?,?)",
                (WORKSPACE_A, "rule", "i:1", 0, "rule_" + "a" * 64, 1),
            )
            invalid_rows = (
                (WORKSPACE_A, "rule", "i:2", 1, "rule_" + "b" * 64, 1),
                (WORKSPACE_A, "community", "i:2", 0, "com_" + "c" * 64, 1),
                (WORKSPACE_A, "rule", "i:3", 0, "ent_" + "d" * 64, 1),
                (WORKSPACE_A, "unknown", "i:4", 0, "rule_" + "e" * 64, 1),
            )
            for row in invalid_rows:
                with self.subTest(row=row), self.assertRaises(sqlite3.IntegrityError):
                    connection.execute(
                        "INSERT INTO public_object_ids VALUES (?,?,?,?,?,?)", row
                    )
        finally:
            connection.close()

    def test_mapping_rows_cannot_be_updated_or_deleted(self) -> None:
        # Catches identity reuse after callers have persisted opaque IDs.
        connection = _connection()
        try:
            connection.execute(
                "INSERT INTO public_object_ids VALUES (?,?,?,?,?,?)",
                (WORKSPACE_A, "rule", "i:1", 0, "rule_" + "a" * 64, 1),
            )
            with self.assertRaisesRegex(
                sqlite3.IntegrityError, "IMMUTABLE_PUBLIC_OBJECT_ID"
            ):
                connection.execute("UPDATE public_object_ids SET source_key='i:2'")
            with self.assertRaisesRegex(
                sqlite3.IntegrityError, "IMMUTABLE_PUBLIC_OBJECT_ID"
            ):
                connection.execute("DELETE FROM public_object_ids")
        finally:
            connection.close()

    def test_orm_metadata_declares_public_object_id_parity(self) -> None:
        # Catches fresh create_all databases silently omitting migration 19's table.
        models_path = Path(__file__).resolve().parents[2] / "daem0nmcp" / "models.py"
        tree = ast.parse(models_path.read_text(encoding="utf-8"))
        table_to_class: dict[str, str] = {}
        for node in tree.body:
            if not isinstance(node, ast.ClassDef):
                continue
            for statement in node.body:
                if (
                    isinstance(statement, ast.Assign)
                    and any(
                        isinstance(target, ast.Name) and target.id == "__tablename__"
                        for target in statement.targets
                    )
                    and isinstance(statement.value, ast.Constant)
                ):
                    table_to_class[str(statement.value.value)] = node.name
        self.assertEqual(table_to_class.get("public_object_ids"), "PublicObjectId")


class PublicObjectIdRepositoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.connection = _connection()

    def tearDown(self) -> None:
        self.connection.close()

    def _repository(self, **kwargs: object):
        from daem0nmcp.api.v7.public_ids import PublicObjectIdRepository

        return PublicObjectIdRepository(
            self.connection,
            clock_us=lambda: 1_700_000_000_000_000,
            **kwargs,
        )

    def test_stable_kinds_are_deterministic_and_bidirectional(self) -> None:
        # Catches accidental integer/public-row IDs and one-way-only mappings.
        repository = self._repository()
        cases = (
            ("rule", "rule", 7),
            ("trigger", "trg", 8),
            ("entity", "ent", 9),
            ("active_context", "act", 10),
        )
        for kind, prefix, source_key in cases:
            with self.subTest(kind=kind):
                public_id = repository.get_or_create(WORKSPACE_A, kind, source_key)
                self.assertEqual(
                    public_id,
                    _expected_id(
                        prefix,
                        WORKSPACE_A,
                        kind,
                        f"i:{source_key}",
                        0,
                    ),
                )
                self.assertRegex(public_id, rf"^{prefix}_[0-9a-f]{{64}}$")
                self.assertEqual(
                    repository.public_id_for_source(WORKSPACE_A, kind, source_key),
                    public_id,
                )
                resolved = repository.resolve_public_id(WORKSPACE_A, kind, public_id)
                self.assertEqual(resolved.source_key, source_key)
                self.assertIsNone(resolved.projection_generation)
                self.assertEqual(resolved.public_id, public_id)

        self.assertEqual(
            repository.get_or_create(WORKSPACE_A, "rule", 7),
            repository.get_or_create(WORKSPACE_A, "rule", 7),
        )
        self.assertEqual(
            4,
            self.connection.execute(
                "SELECT count(*) FROM public_object_ids"
            ).fetchone()[0],
        )

    def test_projection_kinds_bind_ids_to_manifest_generation(self) -> None:
        # Catches stale community/code identifiers selecting a rebuilt projection.
        from daem0nmcp.api.v7.public_ids import StaleProjectionId

        repository = self._repository()
        community_v1 = repository.get_or_create(
            WORKSPACE_A, "community", 11, projection_generation=1
        )
        community_v2 = repository.get_or_create(
            WORKSPACE_A, "community", 11, projection_generation=2
        )
        code_v2 = repository.get_or_create(
            WORKSPACE_A, "code", "entity-hash", projection_generation=2
        )

        self.assertNotEqual(community_v1, community_v2)
        self.assertRegex(community_v2, r"^com_[0-9a-f]{64}$")
        self.assertRegex(code_v2, r"^code_[0-9a-f]{64}$")
        current = repository.resolve_public_id(
            WORKSPACE_A,
            "community",
            community_v2,
            active_generation=2,
        )
        self.assertEqual(current.source_key, 11)
        self.assertEqual(current.projection_generation, 2)
        with self.assertRaises(StaleProjectionId) as raised:
            repository.resolve_public_id(
                WORKSPACE_A,
                "community",
                community_v1,
                active_generation=2,
            )
        self.assertEqual(raised.exception.code, "STALE_PROJECTION_ID")

    def test_unknown_and_cross_workspace_ids_are_indistinguishable(self) -> None:
        # Catches workspace enumeration through repository error distinctions.
        from daem0nmcp.api.v7.public_ids import PublicObjectIdNotFound

        repository = self._repository()
        public_id = repository.get_or_create(WORKSPACE_A, "rule", 12)
        failures = []
        for workspace_id, candidate in (
            (WORKSPACE_B, public_id),
            (WORKSPACE_B, "rule_" + "f" * 64),
            (WORKSPACE_A, "rule_" + "f" * 64),
        ):
            with self.assertRaises(PublicObjectIdNotFound) as raised:
                repository.resolve_public_id(workspace_id, "rule", candidate)
            failures.append((raised.exception.code, str(raised.exception)))
        self.assertEqual([("NOT_FOUND", "NOT_FOUND")] * 3, failures)

    def test_collision_fails_closed_without_creating_ambiguous_row(self) -> None:
        # Catches deterministic-ID collisions being mistaken for idempotent replay.
        from daem0nmcp.api.v7.public_ids import PublicObjectIdIntegrityError

        def constant_id(*_args):
            return "rule_" + "a" * 64

        repository = self._repository(id_factory=constant_id)
        repository.get_or_create(WORKSPACE_A, "rule", 1)
        with self.assertRaises(PublicObjectIdIntegrityError) as raised:
            repository.get_or_create(WORKSPACE_A, "rule", 2)
        self.assertEqual(raised.exception.code, "PUBLIC_ID_INTEGRITY_ERROR")
        self.assertEqual(
            1,
            self.connection.execute(
                "SELECT count(*) FROM public_object_ids"
            ).fetchone()[0],
        )

    def test_inputs_are_bounded_and_generation_rules_are_static(self) -> None:
        # Catches bool/huge IDs, uncontrolled source strings, and policy ambiguity.
        repository = self._repository()
        invalid = (
            ("bad", "rule", 1, None),
            (WORKSPACE_A, "missing", 1, None),
            (WORKSPACE_A, "rule", True, None),
            (WORKSPACE_A, "rule", 0, None),
            (WORKSPACE_A, "rule", 2**63, None),
            (WORKSPACE_A, "rule", "", None),
            (WORKSPACE_A, "rule", "x" * 513, None),
            (WORKSPACE_A, "rule", 1, 1),
            (WORKSPACE_A, "community", 1, None),
            (WORKSPACE_A, "community", 1, 0),
            (WORKSPACE_A, "code", "source", True),
        )
        for workspace_id, kind, source_key, generation in invalid:
            with (
                self.subTest(
                    workspace_id=workspace_id,
                    kind=kind,
                    source_key=source_key,
                    generation=generation,
                ),
                self.assertRaises(ValueError),
            ):
                repository.get_or_create(
                    workspace_id,
                    kind,
                    source_key,
                    projection_generation=generation,
                )


if __name__ == "__main__":
    unittest.main()
