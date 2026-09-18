"""Recoverable schema upgrades for an already active architecture-format 7 store."""

from __future__ import annotations

import hashlib
import os
import shutil
import sqlite3
import stat
import tempfile
import time
from collections import Counter
from collections.abc import Callable, Mapping
from decimal import Decimal, InvalidOperation
from functools import cache
from pathlib import Path
from typing import Any

from ..event_store import canonical_json_bytes, deterministic_id, parse_canonical_json
from ..schema_version import CURRENT_SCHEMA_VERSION, REQUIRED_V7_SCHEMA_VERSIONS
from ..storage_activation import (
    ActiveDatabasePointer,
    resolve_active_database,
    write_active_pointer,
)
from .schema import MIGRATIONS, run_migrations
from .v7 import (
    MigrationInterrupted,
    MigrationResult,
    MigrationV7Error,
    _integrity,
    _is_link_or_reparse,
    _physical_sha256,
    _readonly_connection,
    _sqlite_backup,
    _validated_migration_root,
    inventory_database,
)

_AUTHORITY_TABLES = (
    "memory_events",
    "governance_events",
    "workspace_link_events",
)

# The first production format-7 stores were created by SQLAlchemy before the
# additive migration ledger became the canonical fresh-schema constructor.
# SQLAlchemy supplied these values in Python and therefore omitted the SQL
# DEFAULT clauses.  Their absence makes direct writes fail closed on NOT NULL;
# it does not select a weaker value.  Defaults outside this exact historical
# set remain part of the physical contract.
_HISTORICAL_ORM_DEFAULT_OMISSIONS = {
    ("active_context_entries", "priority"): ("0",),
    ("background_jobs", "priority"): ("0",),
    ("background_jobs", "attempts"): ("0",),
    ("background_jobs", "max_attempts"): ("3",),
    ("enrichment_decisions", "evidence_json"): ("'[]'",),
    ("enrichment_decisions", "independent_source_count"): ("0",),
    ("memory_events", "event_schema_version"): ("1",),
    ("memory_fact_versions", "confidence"): ("1", ".", "0"),
    ("memory_fact_versions", "verification_count"): ("0",),
    ("memory_fact_versions", "is_verified"): ("0",),
    ("memory_fact_versions", "evidence_json"): ("'[]'",),
    ("memory_fact_versions", "metadata_json"): ("'{}'",),
    ("memory_records", "context_json"): ("'{}'",),
    ("memory_records", "tags_json"): ("'[]'",),
    ("memory_records", "is_permanent"): ("0",),
    ("memory_records", "pinned"): ("0",),
    ("memory_records", "archived"): ("0",),
    ("memory_records", "recall_count"): ("0",),
    ("memory_relationship_versions", "confidence"): ("1", ".", "0"),
    ("memory_relationship_versions", "metadata_json"): ("'{}'",),
    ("projection_manifests", "details_json"): ("'{}'",),
    ("v7_migration_checkpoints", "rows_imported"): ("0",),
    ("v7_migration_checkpoints", "completed"): ("0",),
    ("v7_migration_runs", "target_format_version"): ("7",),
}


def _sql_tokens(sql: str | None) -> tuple[str, ...]:
    """Tokenize stored SQLite DDL without depending on whitespace formatting."""

    if sql is None:
        return ()
    tokens: list[str] = []
    index = 0
    while index < len(sql):
        character = sql[index]
        if character.isspace():
            index += 1
            continue
        if sql.startswith("--", index):
            newline = sql.find("\n", index + 2)
            index = len(sql) if newline < 0 else newline + 1
            continue
        if sql.startswith("/*", index):
            end = sql.find("*/", index + 2)
            if end < 0:
                raise ValueError("unterminated SQL comment")
            index = end + 2
            continue
        if character in {"'", '"', "`", "["}:
            closing = "]" if character == "[" else character
            start = index
            index += 1
            while index < len(sql):
                if sql[index] != closing:
                    index += 1
                    continue
                if (
                    closing != "]"
                    and index + 1 < len(sql)
                    and sql[index + 1] == closing
                ):
                    index += 2
                    continue
                index += 1
                break
            else:
                raise ValueError("unterminated SQL quoted value")
            token = sql[start:index]
            tokens.append(token if character == "'" else token.lower())
            continue
        if character.isalnum() or character in {"_", "$"}:
            start = index
            index += 1
            while index < len(sql) and (
                sql[index].isalnum() or sql[index] in {"_", "$"}
            ):
                index += 1
            tokens.append(sql[start:index].lower())
            continue
        paired = sql[index : index + 2]
        if paired in {"<=", ">=", "<>", "!=", "==", "||", "->", "->>"}:
            tokens.append(paired)
            index += 2
            continue
        tokens.append(character)
        index += 1
    return tuple(tokens)


def _check_contract(sql: str | None) -> tuple[tuple[str, ...], ...]:
    tokens = _sql_tokens(sql)
    checks: list[tuple[str, ...]] = []
    index = 0
    while index < len(tokens):
        if (
            tokens[index] != "check"
            or index + 1 >= len(tokens)
            or tokens[index + 1] != "("
        ):
            index += 1
            continue
        depth = 1
        cursor = index + 2
        expression: list[str] = []
        while cursor < len(tokens) and depth:
            token = tokens[cursor]
            if token == "(":
                depth += 1
            elif token == ")":
                depth -= 1
                if depth == 0:
                    break
            expression.append(token)
            cursor += 1
        if depth:
            raise ValueError("unterminated SQLite CHECK constraint")
        checks.append(_strip_outer_parentheses(tuple(expression)))
        index = cursor + 1
    return tuple(sorted(checks))


def _default_contract(value: object) -> tuple[str, ...] | None:
    return None if value is None else _strip_outer_parentheses(_sql_tokens(str(value)))


def _strip_outer_parentheses(tokens: tuple[str, ...]) -> tuple[str, ...]:
    """Remove parentheses only when they enclose the whole expression."""

    result = tokens
    while len(result) >= 2 and result[0] == "(" and result[-1] == ")":
        depth = 0
        encloses_all = True
        for index, token in enumerate(result):
            if token == "(":
                depth += 1
            elif token == ")":
                depth -= 1
                if depth < 0:
                    return result
                if depth == 0 and index != len(result) - 1:
                    encloses_all = False
                    break
        if not encloses_all or depth != 0:
            break
        result = result[1:-1]
    return result


def _sqlite_affinity(declaration: str) -> str:
    """Return SQLite's documented type affinity for a declaration."""

    declared = declaration.upper()
    if "INT" in declared:
        return "INTEGER"
    if any(marker in declared for marker in ("CHAR", "CLOB", "TEXT")):
        return "TEXT"
    if not declared or "BLOB" in declared:
        return "BLOB"
    if any(marker in declared for marker in ("REAL", "FLOA", "DOUB")):
        return "REAL"
    return "NUMERIC"


def _index_predicate(sql: str | None) -> tuple[str, ...]:
    tokens = _sql_tokens(sql)
    try:
        where = tokens.index("where")
    except ValueError:
        return ()
    return _strip_outer_parentheses(tokens[where + 1 :])


def _quoted_identifier(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def _index_contract(
    connection: sqlite3.Connection, table: str, index_name: str
) -> tuple[object, ...]:
    listing = {
        str(row[1]): row
        for row in connection.execute(f"PRAGMA index_list({_quoted_identifier(table)})")
    }
    row = listing.get(index_name)
    if row is None:
        raise ValueError("required SQLite index is missing")
    keys = tuple(
        (
            int(item[1]),
            None if item[2] is None else str(item[2]),
            int(item[3]),
            None if item[4] is None else str(item[4]).lower(),
        )
        for item in connection.execute(
            f"PRAGMA index_xinfo({_quoted_identifier(index_name)})"
        )
        if int(item[5]) == 1
    )
    sql_row = connection.execute(
        "SELECT sql FROM sqlite_master WHERE type='index' AND name=?", (index_name,)
    ).fetchone()
    return (
        int(row[2]),
        str(row[3]),
        int(row[4]),
        keys,
        _index_predicate(None if sql_row is None else sql_row[0]),
    )


def _schema_contract(connection: sqlite3.Connection) -> dict[str, object]:
    objects = [
        (str(row[0]), str(row[1]), str(row[2]), row[3])
        for row in connection.execute(
            "SELECT type,name,tbl_name,sql FROM sqlite_master "
            "WHERE name NOT LIKE 'sqlite_%' ORDER BY type,name"
        )
    ]
    table_names = tuple(name for kind, name, _table, _sql in objects if kind == "table")
    table_contracts: dict[str, tuple[object, ...]] = {}
    for table in table_names:
        sql_row = next(
            sql
            for kind, name, _owner, sql in objects
            if kind == "table" and name == table
        )
        columns = tuple(
            (
                str(row[1]),
                _sqlite_affinity(str(row[2])),
                int(row[3]),
                _default_contract(row[4]),
                int(row[5]),
                int(row[6]),
            )
            for row in connection.execute(
                f"PRAGMA table_xinfo({_quoted_identifier(table)})"
            )
        )
        foreign_keys = tuple(
            tuple(row[1:])
            for row in connection.execute(
                f"PRAGMA foreign_key_list({_quoted_identifier(table)})"
            )
        )
        table_row = connection.execute(
            "SELECT type,ncol,wr,strict FROM pragma_table_list WHERE schema='main' AND name=?",
            (table,),
        ).fetchone()
        table_contracts[table] = (
            columns,
            foreign_keys,
            None if table_row is None else tuple(table_row),
            _check_contract(None if sql_row is None else str(sql_row)),
        )

    explicit_indexes: dict[str, tuple[str, tuple[object, ...]]] = {}
    automatic_indexes: Counter[tuple[str, tuple[object, ...]]] = Counter()
    for table in table_names:
        for row in connection.execute(
            f"PRAGMA index_list({_quoted_identifier(table)})"
        ):
            index_name = str(row[1])
            contract = _index_contract(connection, table, index_name)
            if index_name.startswith("sqlite_autoindex_"):
                automatic_indexes[(table, contract)] += 1
            else:
                explicit_indexes[index_name] = (table, contract)

    triggers = {
        name: (table, _sql_tokens(None if sql is None else str(sql)))
        for kind, name, table, sql in objects
        if kind == "trigger"
    }
    views = {
        name: _sql_tokens(None if sql is None else str(sql))
        for kind, name, _table, sql in objects
        if kind == "view"
    }
    return {
        "tables": table_contracts,
        "explicit_indexes": explicit_indexes,
        "automatic_indexes": automatic_indexes,
        "triggers": triggers,
        "views": views,
    }


@cache
def _reference_schema_contract(maximum: int) -> dict[str, object]:
    connection = sqlite3.connect(":memory:")
    try:
        connection.execute(
            "CREATE TABLE schema_version(version INTEGER PRIMARY KEY, "
            "applied_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)"
        )
        for version, _description, statements in MIGRATIONS:
            if version not in REQUIRED_V7_SCHEMA_VERSIONS or version > maximum:
                continue
            for statement in statements:
                connection.execute(statement)
            connection.execute(
                "INSERT INTO schema_version(version) VALUES (?)", (version,)
            )
        connection.commit()
        return _schema_contract(connection)
    finally:
        connection.close()


def _numeric_default(tokens: tuple[str, ...]) -> str | None:
    if len(tokens) == 1 and len(tokens[0]) >= 2 and tokens[0][0] == "'":
        candidate = tokens[0][1:-1].replace("''", "'")
    elif tokens and all(
        token.isdigit() or token in {"+", "-", ".", "e"} for token in tokens
    ):
        candidate = "".join(tokens)
    else:
        return None
    try:
        number = Decimal(candidate)
    except InvalidOperation:
        return None
    if not number.is_finite():
        return None
    return str(number.normalize())


def _defaults_match(
    expected: tuple[str, ...] | None,
    actual: tuple[str, ...] | None,
    affinity: str,
) -> bool:
    if expected == actual:
        return True
    if expected is None or actual is None:
        return False
    if affinity not in {"INTEGER", "REAL", "NUMERIC"}:
        return False
    expected_number = _numeric_default(expected)
    return expected_number is not None and expected_number == _numeric_default(actual)


def _table_contract_matches(
    table: str,
    expected: tuple[object, ...],
    actual: tuple[object, ...],
) -> bool:
    expected_columns, expected_foreign, expected_row, expected_checks = expected
    actual_columns, actual_foreign, actual_row, actual_checks = actual
    if not isinstance(expected_columns, tuple) or not isinstance(actual_columns, tuple):
        return False
    if len(expected_columns) != len(actual_columns):
        return False
    for expected_column, actual_column in zip(
        expected_columns, actual_columns, strict=True
    ):
        if not isinstance(expected_column, tuple) or not isinstance(
            actual_column, tuple
        ):
            return False
        if len(expected_column) != 6 or len(actual_column) != 6:
            return False
        expected_name = str(expected_column[0])
        actual_name = str(actual_column[0])
        if expected_name != actual_name or expected_column[1] != actual_column[1]:
            return False
        expected_not_null = int(expected_column[2])
        actual_not_null = int(actual_column[2])
        expected_primary_key = int(expected_column[4])
        actual_primary_key = int(actual_column[4])
        if expected_primary_key != actual_primary_key:
            return False
        if expected_not_null and not (actual_not_null or actual_primary_key):
            return False
        if not expected_not_null and actual_not_null and not expected_primary_key:
            return False
        expected_default = expected_column[3]
        actual_default = actual_column[3]
        if (
            actual_default is None
            and expected_default is not None
            and expected_not_null
            and _HISTORICAL_ORM_DEFAULT_OMISSIONS.get((table, expected_name))
            == expected_default
        ):
            pass
        elif not _defaults_match(
            expected_default if isinstance(expected_default, tuple) else None,
            actual_default if isinstance(actual_default, tuple) else None,
            str(expected_column[1]),
        ):
            return False
        if expected_column[5] != actual_column[5]:
            return False
    return (
        expected_foreign == actual_foreign
        and expected_row == actual_row
        and expected_checks == actual_checks
    )


def _validate_physical_schema(path: Path, maximum: int) -> None:
    try:
        expected = _reference_schema_contract(maximum)
        connection = _readonly_connection(path)
        try:
            actual = _schema_contract(connection)
        finally:
            connection.close()
    except MigrationV7Error:
        raise
    except (OSError, sqlite3.Error, TypeError, ValueError) as exc:
        raise MigrationV7Error(
            "V7_PHYSICAL_SCHEMA_INVALID", "physical schema cannot be inspected"
        ) from exc

    expected_tables = expected["tables"]
    actual_tables = actual["tables"]
    if not isinstance(expected_tables, dict) or not isinstance(actual_tables, dict):
        raise RuntimeError("physical schema contract is invalid")
    for name, contract in expected_tables.items():
        actual_contract = actual_tables.get(name)
        if (
            not isinstance(contract, tuple)
            or not isinstance(actual_contract, tuple)
            or not _table_contract_matches(name, contract, actual_contract)
        ):
            raise MigrationV7Error(
                "V7_PHYSICAL_SCHEMA_INVALID",
                f"required table contract differs: {name}",
            )

    expected_indexes = expected["explicit_indexes"]
    actual_indexes = actual["explicit_indexes"]
    if not isinstance(expected_indexes, dict) or not isinstance(actual_indexes, dict):
        raise RuntimeError("physical index contract is invalid")
    for name, contract in expected_indexes.items():
        if actual_indexes.get(name) != contract:
            raise MigrationV7Error(
                "V7_PHYSICAL_SCHEMA_INVALID",
                f"required index contract differs: {name}",
            )

    expected_automatic = expected["automatic_indexes"]
    actual_automatic = actual["automatic_indexes"]
    if not isinstance(expected_automatic, Counter) or not isinstance(
        actual_automatic, Counter
    ):
        raise RuntimeError("physical automatic-index contract is invalid")
    if expected_automatic - actual_automatic:
        raise MigrationV7Error(
            "V7_PHYSICAL_SCHEMA_INVALID", "required unique or primary index differs"
        )

    for category in ("triggers", "views"):
        expected_objects = expected[category]
        actual_objects = actual[category]
        if not isinstance(expected_objects, dict) or not isinstance(
            actual_objects, dict
        ):
            raise RuntimeError("physical schema object contract is invalid")
        for name, contract in expected_objects.items():
            if actual_objects.get(name) != contract:
                raise MigrationV7Error(
                    "V7_PHYSICAL_SCHEMA_INVALID",
                    f"required {category[:-1]} contract differs: {name}",
                )


def _fsync_file(path: Path) -> None:
    with path.open("r+b") as handle:
        handle.flush()
        os.fsync(handle.fileno())


def _fsync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _require_regular(path: Path, parent: Path) -> None:
    try:
        details = path.lstat()
        path.resolve(strict=True).relative_to(parent.resolve(strict=True))
    except (OSError, RuntimeError, ValueError) as exc:
        raise MigrationV7Error(
            "UNSAFE_MIGRATION_PATH", "schema-upgrade file is unavailable"
        ) from exc
    if _is_link_or_reparse(path) or not stat.S_ISREG(details.st_mode):
        raise MigrationV7Error(
            "UNSAFE_MIGRATION_PATH", "schema-upgrade file is not regular"
        )


def _schema_versions(path: Path) -> tuple[int, frozenset[int]]:
    try:
        connection = _readonly_connection(path)
        try:
            exists = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' "
                "AND name='schema_version'"
            ).fetchone()
            if exists is None:
                raise MigrationV7Error(
                    "UNSUPPORTED_V7_SCHEMA", "schema ledger is missing"
                )
            versions = frozenset(
                int(row[0])
                for row in connection.execute("SELECT version FROM schema_version")
            )
        finally:
            connection.close()
    except MigrationV7Error:
        raise
    except (OSError, sqlite3.Error, TypeError, ValueError) as exc:
        raise MigrationV7Error(
            "UNSUPPORTED_V7_SCHEMA", "schema ledger is unreadable"
        ) from exc
    maximum = max(versions, default=0)
    if maximum > CURRENT_SCHEMA_VERSION:
        raise MigrationV7Error(
            "FUTURE_V7_SCHEMA", "active schema is newer than this executable"
        )
    migration_versions = frozenset(
        version for version, _description, _sql in MIGRATIONS
    )
    if not migration_versions >= REQUIRED_V7_SCHEMA_VERSIONS:
        raise RuntimeError("v7 compatibility ledger differs from migration definitions")
    supported = frozenset(
        version for version in REQUIRED_V7_SCHEMA_VERSIONS if version <= maximum
    )
    if maximum not in REQUIRED_V7_SCHEMA_VERSIONS or not versions >= supported:
        raise MigrationV7Error(
            "UNSUPPORTED_V7_SCHEMA", "v7 schema ledger is incomplete"
        )
    _validate_physical_schema(path, maximum)
    return maximum, versions


def _pointer_digest(storage: Path) -> str:
    pointer = storage / "active-db.json"
    try:
        payload = pointer.read_bytes() if pointer.is_file() else b""
    except OSError as exc:
        raise MigrationV7Error(
            "ACTIVE_POINTER_CHANGED", "active pointer cannot be read"
        ) from exc
    return hashlib.sha256(payload).hexdigest()


def _database_identity(inventory: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "logical_sha256": inventory["logical_sha256"],
        "tables": inventory["tables"],
        "table_hashes": inventory["table_hashes"],
        "max_schema_version": inventory["max_schema_version"],
    }


def _source_identity(
    storage: Path, active: Any, inventory: Mapping[str, Any]
) -> dict[str, Any]:
    return {
        "operation": "v7_schema_upgrade",
        "database": _database_identity(inventory),
        "active_pointer": {
            "sha256": _pointer_digest(storage),
            "generation": active.generation,
            "active_db": active.relative_path,
            "previous_db": active.previous_db,
            "migration_run_id": active.migration_run_id,
        },
        "target_schema_version": CURRENT_SCHEMA_VERSION,
    }


def _public_inventory(
    inventory: Mapping[str, Any], *, available_bytes: int
) -> dict[str, Any]:
    result = dict(inventory)
    result["available_bytes"] = available_bytes
    result["required_bytes_estimate"] = _required_bytes(inventory)
    result["schema_upgrade_required"] = (
        int(inventory["max_schema_version"]) < CURRENT_SCHEMA_VERSION
    )
    return result


def _required_bytes(inventory: Mapping[str, Any]) -> int:
    source_bytes = int(inventory["database_bytes"]) + int(inventory["wal_bytes"])
    # The source already exists. Free space must hold the retained snapshot,
    # an index-growing candidate, one whole-store replay database, and SQLite's
    # transactional scratch space.
    return source_bytes * 4 + 256 * 1024 * 1024


def _all_checks_ok(checks: Mapping[str, Mapping[str, Any]]) -> bool:
    return bool(checks) and all(bool(value.get("ok")) for value in checks.values())


def _canonical_value(value: Any) -> Any:
    """Convert verifier-only tuples to the canonical JSON value model."""

    if isinstance(value, tuple):
        return [_canonical_value(item) for item in value]
    if isinstance(value, list):
        return [_canonical_value(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _canonical_value(item) for key, item in value.items()}
    return value


def _verify_published_candidate(candidate: Path, workspace_id: str) -> dict[str, Any]:
    """Verify a published candidate without changing any database byte."""

    from ..verification_v7 import _verify_database

    _schema_versions(candidate)
    try:
        with tempfile.TemporaryDirectory(
            prefix="daem0nmcp-schema-verify-", dir=candidate.parent
        ) as raw:
            result = _verify_database(candidate, workspace_id, Path(raw) / "replay.db")
    except (OSError, sqlite3.Error, TypeError, ValueError) as exc:
        raise MigrationV7Error(
            "SCHEMA_UPGRADE_CANDIDATE_INVALID",
            "published candidate could not be verified",
        ) from exc
    if not _all_checks_ok(result["checks"]):
        raise MigrationV7Error(
            "SCHEMA_UPGRADE_CANDIDATE_INVALID",
            "published candidate did not pass whole-store verification",
        )
    return result


def _verify_and_repair_candidate(candidate: Path, workspace_id: str) -> dict[str, Any]:
    from ..verification_v7 import (
        _refresh_manifests,
        _replace_derived_tables,
        _verify_database,
    )

    repairable = {"canonical_replay", "projection_manifests"}
    _schema_versions(candidate)
    try:
        temporary_context = tempfile.TemporaryDirectory(
            prefix="daem0nmcp-schema-upgrade-", dir=candidate.parent
        )
    except OSError as exc:
        raise MigrationV7Error(
            "SCHEMA_UPGRADE_CANDIDATE_INVALID",
            "temporary verification storage is unavailable",
        ) from exc
    with temporary_context as raw:
        temporary = Path(raw)
        replay = temporary / "replay.db"
        try:
            first = _verify_database(candidate, workspace_id, replay)
        except (OSError, sqlite3.Error, TypeError, ValueError) as exc:
            raise MigrationV7Error(
                "SCHEMA_UPGRADE_CANDIDATE_INVALID",
                "candidate could not be verified",
            ) from exc
        failed = {
            name for name, value in first["checks"].items() if not bool(value.get("ok"))
        }
        if failed - repairable:
            raise MigrationV7Error(
                "SCHEMA_UPGRADE_AUTHORITY_INVALID",
                "candidate authoritative state did not verify",
            )
        if failed:
            _replace_derived_tables(candidate, replay)
            connection = sqlite3.connect(candidate)
            try:
                workspace_ids = {workspace_id}
                for table in _AUTHORITY_TABLES:
                    if (
                        connection.execute(
                            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                            (table,),
                        ).fetchone()
                        is None
                    ):
                        continue
                    workspace_ids.update(
                        str(row[0])
                        for row in connection.execute(
                            f'SELECT DISTINCT workspace_id FROM "{table}"'
                        )
                    )
            finally:
                connection.close()
            _refresh_manifests(candidate, sorted(workspace_ids))
        replay.unlink(missing_ok=True)
        final_replay = temporary / "final-replay.db"
        try:
            final = _verify_database(candidate, workspace_id, final_replay)
        except (OSError, sqlite3.Error, TypeError, ValueError) as exc:
            raise MigrationV7Error(
                "SCHEMA_UPGRADE_CANDIDATE_INVALID",
                "repaired candidate could not be verified",
            ) from exc
        if not _all_checks_ok(final["checks"]):
            raise MigrationV7Error(
                "SCHEMA_UPGRADE_CANDIDATE_INVALID",
                "upgraded candidate did not pass whole-store verification",
            )
        return {
            "authority": final["authority"],
            "authority_roots": _canonical_value(final["authority_roots"]),
            "checks": {
                name: bool(value.get("ok")) for name, value in final["checks"].items()
            },
        }


class V7SchemaUpgradeService:
    """Upgrade one active v7 database through a retained, verified candidate."""

    def __init__(
        self,
        *,
        fault_injector: Callable[[str, dict[str, Any]], None] | None = None,
        clock_us: Callable[[], int] | None = None,
    ) -> None:
        self._fault_injector = fault_injector
        self._clock_us = clock_us or (lambda: time.time_ns() // 1_000)

    def _fault(self, stage: str, **details: Any) -> None:
        if self._fault_injector is not None:
            self._fault_injector(stage, details)

    def inspect(
        self,
        storage: Path,
        workspace_id: str,
        active: Any,
    ) -> MigrationResult | None:
        try:
            return self._inspect(storage, workspace_id, active)
        except MigrationV7Error:
            raise
        except (OSError, sqlite3.Error, RuntimeError, TypeError, ValueError) as exc:
            raise MigrationV7Error(
                "SCHEMA_UPGRADE_INSPECTION_FAILED",
                "active v7 schema could not be inspected",
            ) from exc

    def _inspect(
        self,
        storage: Path,
        workspace_id: str,
        active: Any,
    ) -> MigrationResult | None:
        source_schema, _versions = _schema_versions(active.path)
        inventory = inventory_database(active.path)
        inventory["active_db"] = active.relative_path
        if source_schema == CURRENT_SCHEMA_VERSION:
            return MigrationResult(
                status="dry_run",
                action="already_active",
                workspace_id=workspace_id,
                source_format=7,
                migration_run_id=active.migration_run_id,
                active_generation=active.generation,
                inventory=inventory,
                validation={
                    "quick_check": inventory["quick_check"],
                    "foreign_key_violations": inventory["foreign_key_violations"],
                    "source_schema_version": source_schema,
                    "target_schema_version": CURRENT_SCHEMA_VERSION,
                },
            )
        available = shutil.disk_usage(storage).free
        action = (
            "reactivate"
            if self._rolled_back_candidate(storage, workspace_id, active)
            else "upgrade"
        )
        return MigrationResult(
            status="dry_run",
            action=action,
            workspace_id=workspace_id,
            source_format=7,
            migration_run_id=active.migration_run_id,
            active_generation=active.generation,
            inventory=_public_inventory(inventory, available_bytes=available),
            validation={
                "quick_check": inventory["quick_check"],
                "foreign_key_violations": inventory["foreign_key_violations"],
                "source_schema_version": source_schema,
                "target_schema_version": CURRENT_SCHEMA_VERSION,
            },
        )

    def apply(
        self,
        storage: Path,
        workspace_id: str,
        active: Any,
    ) -> MigrationResult | None:
        try:
            return self._apply(storage, workspace_id, active)
        except (MigrationV7Error, MigrationInterrupted):
            raise
        except (OSError, sqlite3.Error, RuntimeError, TypeError, ValueError) as exc:
            raise MigrationV7Error(
                "SCHEMA_UPGRADE_FAILED",
                "schema upgrade failed before activation; the source is retained",
            ) from exc

    def _apply(
        self,
        storage: Path,
        workspace_id: str,
        active: Any,
    ) -> MigrationResult | None:
        source_schema, _versions = _schema_versions(active.path)
        if source_schema == CURRENT_SCHEMA_VERSION:
            return self._recover_active_ready(storage, workspace_id, active)
        inventory = inventory_database(active.path)
        inventory["active_db"] = active.relative_path
        if inventory["quick_check"] != "ok" or inventory["foreign_key_violations"]:
            raise MigrationV7Error(
                "SOURCE_INTEGRITY_FAILED", "active v7 source failed SQLite integrity"
            )
        available = shutil.disk_usage(storage).free
        required = _required_bytes(inventory)
        if available < required:
            raise MigrationV7Error(
                "INSUFFICIENT_DISK_SPACE", "schema upgrade requires more free space"
            )
        rolled_back = self._rolled_back_candidate(storage, workspace_id, active)
        if rolled_back is not None:
            candidate, run_id, stored_identity = rolled_back
            if stored_identity.get("database") == _database_identity(inventory):
                return self._activate_candidate(
                    storage,
                    workspace_id,
                    active,
                    candidate,
                    run_id,
                    stored_identity,
                    action="reactivate",
                    source_schema=source_schema,
                    inventory=inventory,
                )
        identity = _source_identity(storage, active, inventory)
        run_id = deterministic_id(
            "mig",
            "v7-schema-upgrade",
            workspace_id,
            identity,
        )
        migration_root = _validated_migration_root(storage, create=True)
        if migration_root is None:  # pragma: no cover - create=True is exhaustive
            raise MigrationV7Error(
                "UNSAFE_MIGRATION_PATH", "schema-upgrade root is unavailable"
            )
        run_dir = migration_root / run_id
        resumed = self._resumable_candidate(run_dir, run_id, workspace_id, identity)
        if resumed is not None:
            return self._activate_candidate(
                storage,
                workspace_id,
                active,
                resumed,
                run_id,
                identity,
                action="resume",
                source_schema=source_schema,
                inventory=inventory,
            )
        if run_dir.exists():
            self._archive_incomplete(run_dir, migration_root, run_id)
        run_dir.mkdir(mode=0o700)
        if _is_link_or_reparse(run_dir) or not stat.S_ISDIR(run_dir.lstat().st_mode):
            raise MigrationV7Error(
                "UNSAFE_MIGRATION_PATH", "schema-upgrade run directory is unsafe"
            )
        self._fault("schema_upgrade_after_run_directory", migration_run_id=run_id)
        snapshot_partial = run_dir / "source.snapshot.db.partial"
        snapshot = run_dir / "source.snapshot.db"
        candidate_partial = run_dir / "candidate.db.partial"
        candidate = run_dir / "candidate.db"
        _sqlite_backup(active.path, snapshot_partial)
        _require_regular(snapshot_partial, run_dir)
        _integrity(snapshot_partial)
        _fsync_file(snapshot_partial)
        os.replace(snapshot_partial, snapshot)
        _fsync_directory(run_dir)
        self._fault("schema_upgrade_after_snapshot", migration_run_id=run_id)
        snapshot_inventory = inventory_database(snapshot)
        if _database_identity(snapshot_inventory) != _database_identity(inventory):
            raise MigrationV7Error(
                "SOURCE_CHANGED", "retained snapshot differs from active source"
            )
        _sqlite_backup(snapshot, candidate_partial)
        _require_regular(candidate_partial, run_dir)
        self._fault("schema_upgrade_after_candidate_copy", migration_run_id=run_id)
        applied_count, applied = run_migrations(
            str(candidate_partial), workspace_id=workspace_id
        )
        self._checkpoint(candidate_partial)
        candidate_inventory = inventory_database(candidate_partial)
        for table in _AUTHORITY_TABLES:
            if table in inventory["table_hashes"] and (
                candidate_inventory["table_hashes"].get(table)
                != inventory["table_hashes"][table]
            ):
                raise MigrationV7Error(
                    "SCHEMA_UPGRADE_AUTHORITY_CHANGED",
                    f"existing {table} rows changed during schema upgrade",
                )
        self._fault("schema_upgrade_after_schema", migration_run_id=run_id)
        validation = _verify_and_repair_candidate(candidate_partial, workspace_id)
        validation.update(
            {
                "operation": "v7_schema_upgrade",
                "source_schema_version": source_schema,
                "target_schema_version": CURRENT_SCHEMA_VERSION,
                "applied": applied,
            }
        )
        self._record_ready_run(
            candidate_partial,
            run_id,
            workspace_id,
            snapshot,
            source_schema,
            identity,
            validation,
        )
        # The run row is part of the final candidate and must itself verify.
        final_validation = _verify_published_candidate(candidate_partial, workspace_id)
        validation["checks"] = final_validation["checks"]
        self._checkpoint(candidate_partial)
        _fsync_file(snapshot)
        _fsync_file(candidate_partial)
        self._fault("schema_upgrade_after_verification", migration_run_id=run_id)
        os.replace(candidate_partial, candidate)
        _fsync_directory(run_dir)
        self._fault("schema_upgrade_after_candidate_publish", migration_run_id=run_id)
        result = self._activate_candidate(
            storage,
            workspace_id,
            active,
            candidate,
            run_id,
            identity,
            action="upgrade",
            source_schema=source_schema,
            inventory=inventory,
        )
        return MigrationResult(
            **{
                **result.as_dict(),
                "checkpoints": {
                    "schema_from": source_schema,
                    "schema_to": CURRENT_SCHEMA_VERSION,
                    "migrations_applied": applied_count,
                },
            }
        )

    def _checkpoint(self, path: Path) -> None:
        connection = sqlite3.connect(path)
        try:
            connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        finally:
            connection.close()

    def _record_ready_run(
        self,
        candidate: Path,
        run_id: str,
        workspace_id: str,
        snapshot: Path,
        source_schema: int,
        identity: Mapping[str, Any],
        validation: Mapping[str, Any],
    ) -> None:
        now = self._clock_us()
        connection = sqlite3.connect(candidate)
        try:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "INSERT INTO v7_migration_runs (migration_run_id,workspace_id,"
                "source_db_sha256,source_schema_version,source_format_version,"
                "target_format_version,status,snapshot_name,candidate_name,"
                "source_inventory_json,validation_json,created_at_us,updated_at_us,"
                "validated_at_us) VALUES (?,?,?,?,7,7,'ready','source.snapshot.db',"
                "'candidate.db',?,?,?,?,?)",
                (
                    run_id,
                    workspace_id,
                    _physical_sha256(snapshot),
                    source_schema,
                    canonical_json_bytes(dict(identity)).decode("utf-8"),
                    canonical_json_bytes(dict(validation)).decode("utf-8"),
                    now,
                    now,
                    now,
                ),
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _read_run(
        self, candidate: Path, run_id: str, workspace_id: str
    ) -> tuple[str, dict[str, Any]]:
        _require_regular(candidate, candidate.parent)
        connection = sqlite3.connect(f"file:{candidate.as_posix()}?mode=ro", uri=True)
        try:
            try:
                row = connection.execute(
                    "SELECT workspace_id,status,source_format_version,"
                    "source_inventory_json,validation_json FROM v7_migration_runs "
                    "WHERE migration_run_id=?",
                    (run_id,),
                ).fetchone()
            except sqlite3.Error as exc:
                raise MigrationV7Error(
                    "INVALID_SCHEMA_UPGRADE_CANDIDATE",
                    "upgrade run metadata is unreadable",
                ) from exc
        finally:
            connection.close()
        if row is None or row[0] != workspace_id or int(row[2]) != 7:
            raise MigrationV7Error(
                "INVALID_SCHEMA_UPGRADE_CANDIDATE", "upgrade run metadata differs"
            )
        try:
            identity = parse_canonical_json(str(row[3]))
            validation = parse_canonical_json(str(row[4]))
        except (TypeError, ValueError) as exc:
            raise MigrationV7Error(
                "INVALID_SCHEMA_UPGRADE_CANDIDATE", "upgrade metadata is invalid"
            ) from exc
        if (
            not isinstance(identity, dict)
            or identity.get("operation") != "v7_schema_upgrade"
            or not isinstance(validation, dict)
            or validation.get("operation") != "v7_schema_upgrade"
        ):
            raise MigrationV7Error(
                "INVALID_SCHEMA_UPGRADE_CANDIDATE", "upgrade metadata is unrecognized"
            )
        return str(row[1]), identity

    def _resumable_candidate(
        self,
        run_dir: Path,
        run_id: str,
        workspace_id: str,
        identity: Mapping[str, Any],
    ) -> Path | None:
        if not run_dir.exists():
            return None
        if _is_link_or_reparse(run_dir) or not run_dir.is_dir():
            raise MigrationV7Error(
                "UNSAFE_MIGRATION_PATH", "schema-upgrade run path is unsafe"
            )
        candidate = run_dir / "candidate.db"
        snapshot = run_dir / "source.snapshot.db"
        if not candidate.exists() and not snapshot.exists():
            return None
        if not candidate.is_file() or not snapshot.is_file():
            return None
        status, stored = self._read_run(candidate, run_id, workspace_id)
        if status != "ready" or stored != dict(identity):
            raise MigrationV7Error(
                "INVALID_SCHEMA_UPGRADE_CANDIDATE", "resumable candidate differs"
            )
        self._validate_snapshot(candidate, run_id, identity)
        _verify_published_candidate(candidate, workspace_id)
        return candidate

    def _snapshot_hash(self, candidate: Path, run_id: str) -> str:
        connection = sqlite3.connect(f"file:{candidate.as_posix()}?mode=ro", uri=True)
        try:
            try:
                row = connection.execute(
                    "SELECT source_db_sha256 FROM v7_migration_runs "
                    "WHERE migration_run_id=?",
                    (run_id,),
                ).fetchone()
            except sqlite3.Error as exc:
                raise MigrationV7Error(
                    "INVALID_SCHEMA_UPGRADE_CANDIDATE",
                    "snapshot metadata is unreadable",
                ) from exc
        finally:
            connection.close()
        if row is None:
            raise MigrationV7Error(
                "INVALID_SCHEMA_UPGRADE_CANDIDATE", "snapshot hash is missing"
            )
        return str(row[0])

    def _validate_snapshot(
        self,
        candidate: Path,
        run_id: str,
        identity: Mapping[str, Any],
    ) -> None:
        snapshot = candidate.parent / "source.snapshot.db"
        _require_regular(snapshot, candidate.parent)
        if _physical_sha256(snapshot) != self._snapshot_hash(candidate, run_id):
            raise MigrationV7Error(
                "INVALID_SCHEMA_UPGRADE_CANDIDATE",
                "retained snapshot fingerprint differs",
            )
        snapshot_schema, _versions = _schema_versions(snapshot)
        if snapshot_schema != identity.get("database", {}).get("max_schema_version"):
            raise MigrationV7Error(
                "INVALID_SCHEMA_UPGRADE_CANDIDATE",
                "retained snapshot schema differs from the recorded source",
            )
        if _database_identity(inventory_database(snapshot)) != identity.get("database"):
            raise MigrationV7Error(
                "INVALID_SCHEMA_UPGRADE_CANDIDATE",
                "retained snapshot content differs from the recorded source",
            )

    def _activate_candidate(
        self,
        storage: Path,
        workspace_id: str,
        active: Any,
        candidate: Path,
        run_id: str,
        identity: Mapping[str, Any],
        *,
        action: str,
        source_schema: int,
        inventory: Mapping[str, Any],
    ) -> MigrationResult:
        status, stored = self._read_run(candidate, run_id, workspace_id)
        if status not in {"ready", "rolled_back"} or stored != dict(identity):
            raise MigrationV7Error(
                "INVALID_SCHEMA_UPGRADE_CANDIDATE", "candidate is not activatable"
            )
        self._validate_snapshot(candidate, run_id, identity)
        _verify_published_candidate(candidate, workspace_id)
        current_active = resolve_active_database(storage)
        if current_active != active:
            raise MigrationV7Error(
                "ACTIVE_POINTER_CHANGED",
                "active database selection changed before schema-upgrade cutover",
            )
        current_inventory = inventory_database(active.path)
        if _database_identity(current_inventory) != identity.get("database"):
            raise MigrationV7Error(
                "SCHEMA_UPGRADE_SOURCE_CHANGED", "active source changed before cutover"
            )
        relative = candidate.relative_to(storage).as_posix()
        pointer = ActiveDatabasePointer(
            format_version=7,
            generation=active.generation + 1,
            active_db=relative,
            previous_db=active.relative_path,
            migration_run_id=run_id,
        )
        self._fault("schema_upgrade_before_pointer", migration_run_id=run_id)
        write_active_pointer(storage, pointer)
        self._fault("schema_upgrade_after_pointer", migration_run_id=run_id)
        connection = sqlite3.connect(candidate)
        try:
            connection.execute("BEGIN IMMEDIATE")
            changed = connection.execute(
                "UPDATE v7_migration_runs SET status='active',updated_at_us=?,"
                "activated_at_us=?,rolled_back_at_us=NULL,last_error_json=NULL "
                "WHERE migration_run_id=? AND status IN ('ready','rolled_back')",
                (self._clock_us(), self._clock_us(), run_id),
            ).rowcount
            if changed != 1:
                raise MigrationV7Error(
                    "ACTIVATION_STATE_INVALID", "schema-upgrade run is not activatable"
                )
            connection.commit()
        except Exception:
            connection.rollback()
            if active.pointer is None:
                pointer_path = storage / "active-db.json"
                if pointer_path.is_file() and not pointer_path.is_symlink():
                    pointer_path.unlink()
            else:
                write_active_pointer(storage, active.pointer)
            raise
        finally:
            connection.close()
        return MigrationResult(
            status="activated",
            action=action,
            workspace_id=workspace_id,
            source_format=7,
            migration_run_id=run_id,
            active_generation=pointer.generation,
            inventory=dict(inventory),
            validation={
                "source_schema_version": source_schema,
                "target_schema_version": CURRENT_SCHEMA_VERSION,
                "whole_store_verified": True,
            },
        )

    def _recover_active_ready(
        self, storage: Path, workspace_id: str, active: Any
    ) -> MigrationResult | None:
        run_id = active.migration_run_id
        if (
            run_id is None
            or active.relative_path != f"migrations/v7/{run_id}/candidate.db"
        ):
            return None
        try:
            status, identity = self._read_run(active.path, run_id, workspace_id)
        except MigrationV7Error:
            return None
        if status != "ready":
            return None
        _verify_published_candidate(active.path, workspace_id)
        connection = sqlite3.connect(active.path)
        try:
            now = self._clock_us()
            connection.execute(
                "UPDATE v7_migration_runs SET status='active',updated_at_us=?,"
                "activated_at_us=? WHERE migration_run_id=? AND status='ready'",
                (now, now, run_id),
            )
            connection.commit()
        finally:
            connection.close()
        database = identity.get("database", {})
        return MigrationResult(
            status="activated",
            action="resume",
            workspace_id=workspace_id,
            source_format=7,
            migration_run_id=run_id,
            active_generation=active.generation,
            inventory=dict(database) if isinstance(database, dict) else {},
            validation={"whole_store_verified": True},
        )

    def prepare_rollback(
        self, storage: Path, workspace_id: str, active: Any
    ) -> MigrationResult | None:
        """Complete an interrupted schema-upgrade boundary before rollback."""

        run_id = active.migration_run_id
        if run_id is None:
            return None
        candidate_relative = f"migrations/v7/{run_id}/candidate.db"
        if active.relative_path == candidate_relative:
            recovered = self._recover_active_ready(storage, workspace_id, active)
            if recovered is not None:
                # Activation metadata is now durable; the caller can perform
                # the requested rollback in the same exclusive lock scope.
                return None
        if active.previous_db != candidate_relative:
            return None
        candidate = storage.joinpath(*candidate_relative.split("/"))
        if not candidate.is_file():
            return None
        status, identity = self._read_run(candidate, run_id, workspace_id)
        if status == "active":
            if _database_identity(inventory_database(active.path)) != identity.get(
                "database"
            ):
                raise MigrationV7Error(
                    "ROLLBACK_STATE_INVALID",
                    "rolled-back schema source differs from recorded source",
                )
            connection = sqlite3.connect(candidate)
            try:
                connection.execute("BEGIN IMMEDIATE")
                now = self._clock_us()
                changed = connection.execute(
                    "UPDATE v7_migration_runs SET status='rolled_back',updated_at_us=?,"
                    "rolled_back_at_us=? WHERE migration_run_id=? AND status='active'",
                    (now, now, run_id),
                ).rowcount
                if changed != 1:
                    raise MigrationV7Error(
                        "ROLLBACK_STATE_INVALID",
                        "schema-upgrade rollback metadata changed concurrently",
                    )
                connection.commit()
            except Exception:
                connection.rollback()
                raise
            finally:
                connection.close()
            status = "rolled_back"
        if status != "rolled_back":
            return None
        return MigrationResult(
            status="already_rolled_back",
            action="already_rolled_back",
            workspace_id=workspace_id,
            source_format=7,
            migration_run_id=run_id,
            active_generation=active.generation,
        )

    def _rolled_back_candidate(
        self, storage: Path, workspace_id: str, active: Any
    ) -> tuple[Path, str, dict[str, Any]] | None:
        run_id = active.migration_run_id
        if run_id is None:
            return None
        expected = f"migrations/v7/{run_id}/candidate.db"
        if active.previous_db != expected:
            return None
        candidate = storage.joinpath(*expected.split("/"))
        if not candidate.is_file():
            return None
        status, identity = self._read_run(candidate, run_id, workspace_id)
        return (candidate, run_id, identity) if status == "rolled_back" else None

    def _archive_incomplete(self, run_dir: Path, root: Path, run_id: str) -> None:
        if _is_link_or_reparse(run_dir) or not run_dir.is_dir():
            raise MigrationV7Error(
                "UNSAFE_MIGRATION_PATH", "incomplete schema-upgrade run is unsafe"
            )
        for child in run_dir.iterdir():
            if _is_link_or_reparse(child):
                raise MigrationV7Error(
                    "UNSAFE_MIGRATION_PATH", "incomplete candidate contains a link"
                )
        suffix = 1
        while True:
            archived = root / f"schema-upgrade-incomplete-{run_id[4:20]}-{suffix}"
            if not archived.exists():
                os.replace(run_dir, archived)
                _fsync_directory(root)
                return
            suffix += 1


__all__ = ["V7SchemaUpgradeService"]
