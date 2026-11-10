"""Immutable workspace- and generation-scoped discovery projections."""

from __future__ import annotations

import json
import os
import re
import sqlite3
import time
import unicodedata
from collections.abc import Callable, Iterable, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from types import MappingProxyType

from .api.v7.public_ids import (
    PublicObjectIdRepository,
    PublicObjectKind,
    derive_public_object_id,
)
from .event_store import canonical_json_bytes, deterministic_id, sha256_json

_WORKSPACE_ID_RE = re.compile(r"^ws_[0-9a-f]{24}$")
_RECORD_ID_RE = re.compile(r"^mem_[0-9a-f]{64}$")
_CODE_ID_RE = re.compile(r"^code_[0-9a-f]{64}$")
_ENTITY_TYPE_RE = re.compile(r"^[a-z][a-z0-9_-]{0,79}$")
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")
_CODE_KINDS = frozenset(
    {"file", "module", "class", "function", "method", "variable", "symbol"}
)
_CODE_EDGE_KINDS = frozenset({"call", "import", "reference"})
_REQUIRED_TABLES = frozenset(
    {
        "discovery_projection_partitions",
        "discovery_entities",
        "discovery_entity_records",
        "discovery_communities",
        "discovery_community_members",
        "discovery_code_entities",
        "discovery_code_edges",
        "memory_records",
        "projection_manifests",
        "public_object_ids",
    }
)
_BUILDER_VERSION = "discovery-v1"
_MAX_ENTITIES = 100_000
_MAX_COMMUNITIES = 20_000
_MAX_MEMBERSHIPS = 1_000_000
_MAX_CODE_ENTITIES = 200_000
_MAX_CODE_EDGES = 200_000


class DiscoveryProjectionBuildError(RuntimeError):
    """Deterministic discovery build/import failure."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class CodeProjectionIntegrityError(RuntimeError):
    """A bounded code-generation integrity check failed closed."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class EntityRecordSeed:
    record_id: str
    mention_count: int = 1


@dataclass(frozen=True, slots=True)
class EntityProjectionSeed:
    name: str
    entity_type: str
    records: tuple[EntityRecordSeed, ...] = ()
    mention_count: int | None = None


@dataclass(frozen=True, slots=True)
class CommunityProjectionSeed:
    source_key: str
    label: str
    level: int
    member_record_ids: tuple[str, ...] = ()
    parent_source_key: str | None = None


@dataclass(frozen=True, slots=True)
class CodeEntityProjectionSeed:
    source_key: str
    kind: str
    qualified_name: str
    relative_file_path: str
    start_line: int
    end_line: int


@dataclass(frozen=True, slots=True)
class CodeEdgeProjectionSeed:
    source_key: str
    target_key: str
    kind: str
    source_line: int | None = None


@dataclass(frozen=True, slots=True)
class DiscoveryProjectionBuildResult:
    graph_generation: int | None = None
    code_generation: int | None = None
    entity_ids: tuple[str, ...] = ()
    community_ids: tuple[str, ...] = ()
    code_entity_ids: tuple[str, ...] = ()
    reused: bool = False


@dataclass(frozen=True, slots=True)
class _EntityRow:
    seed: EntityProjectionSeed
    normalized_name: str
    identity_hash: str
    mention_count: int


@dataclass(frozen=True, slots=True)
class _CommunityRow:
    seed: CommunityProjectionSeed
    identity_hash: str


@dataclass(frozen=True, slots=True)
class _CodeRow:
    seed: CodeEntityProjectionSeed
    normalized_name: str
    identity_hash: str


@dataclass(frozen=True, slots=True)
class _CodeEdgeRow:
    seed: CodeEdgeProjectionSeed
    identity_hash: str


def _safe_text(value: object, *, maximum: int) -> str:
    if not isinstance(value, str):
        raise DiscoveryProjectionBuildError("INVALID_DISCOVERY_SEED")
    normalized = unicodedata.normalize("NFC", value)
    try:
        normalized.encode("utf-8")
    except UnicodeEncodeError:
        raise DiscoveryProjectionBuildError("INVALID_DISCOVERY_SEED") from None
    if (
        not normalized
        or normalized != normalized.strip()
        or len(normalized) > maximum
        or _CONTROL_RE.search(normalized) is not None
    ):
        raise DiscoveryProjectionBuildError("INVALID_DISCOVERY_SEED")
    return normalized


def _normalized_name(value: object) -> tuple[str, str]:
    name = _safe_text(value, maximum=256)
    normalized = unicodedata.normalize("NFC", name.casefold())
    if not normalized or len(normalized) > 256:
        raise DiscoveryProjectionBuildError("INVALID_DISCOVERY_SEED")
    return name, normalized


def _workspace_id(value: object) -> str:
    if not isinstance(value, str) or _WORKSPACE_ID_RE.fullmatch(value) is None:
        raise DiscoveryProjectionBuildError("INVALID_WORKSPACE")
    return value


def _record_id(value: object) -> str:
    if not isinstance(value, str) or _RECORD_ID_RE.fullmatch(value) is None:
        raise DiscoveryProjectionBuildError("INVALID_DISCOVERY_SEED")
    return value


def _plain_int(value: object, *, minimum: int, maximum: int) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or value < minimum
        or value > maximum
    ):
        raise DiscoveryProjectionBuildError("INVALID_DISCOVERY_SEED")
    return value


def _relative_path(value: object) -> str:
    if not isinstance(value, str):
        raise DiscoveryProjectionBuildError("INVALID_RELATIVE_PATH")
    normalized = unicodedata.normalize("NFC", value.replace("\\", "/"))
    if (
        not normalized
        or len(normalized) > 1024
        or _CONTROL_RE.search(normalized) is not None
        or normalized.startswith(("/", "~"))
        or re.match(r"^[A-Za-z]:", normalized) is not None
    ):
        raise DiscoveryProjectionBuildError("INVALID_RELATIVE_PATH")
    parts = PurePosixPath(normalized).parts
    if (
        not parts
        or any(part in {"", ".", ".."} for part in parts)
        or PurePosixPath(*parts).as_posix() != normalized
    ):
        raise DiscoveryProjectionBuildError("INVALID_RELATIVE_PATH")
    return normalized


def _table_names(connection: sqlite3.Connection) -> frozenset[str]:
    return frozenset(
        str(row[0])
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    )


def _partition_digest(rows: Iterable[object]) -> str:
    return sha256_json(list(rows))


def verify_code_projection(
    connection: sqlite3.Connection,
    workspace_id: str,
    generation: int,
    *,
    expected_row_count: int | None = None,
    expected_root_hash: str,
    max_entities: int = _MAX_CODE_ENTITIES,
    max_edges: int = _MAX_CODE_EDGES,
) -> tuple[int, int]:
    """Recompute one immutable code generation and its public-ID bindings."""

    try:
        workspace_id = _workspace_id(workspace_id)
        generation = _plain_int(generation, minimum=1, maximum=2**63 - 1)
        max_entities = _plain_int(max_entities, minimum=1, maximum=2**31 - 1)
        max_edges = _plain_int(max_edges, minimum=1, maximum=2**31 - 1)
        if not isinstance(expected_root_hash, str) or not re.fullmatch(
            r"[0-9a-f]{64}", expected_root_hash
        ):
            raise CodeProjectionIntegrityError("CODE_PROJECTION_CORRUPT")
        partition_rows = connection.execute(
            "SELECT row_count,content_hash FROM discovery_projection_partitions "
            "WHERE workspace_id=? AND projection_name='code' AND generation=? "
            "AND partition_name='code' LIMIT 2",
            (workspace_id, generation),
        ).fetchall()
        if len(partition_rows) != 1:
            raise CodeProjectionIntegrityError("CODE_PROJECTION_CORRUPT")
        partition_count = _plain_int(
            partition_rows[0][0], minimum=0, maximum=max_entities
        )
        if expected_row_count is None:
            expected_row_count = partition_count
        else:
            expected_row_count = _plain_int(
                expected_row_count, minimum=0, maximum=max_entities
            )
        entity_rows = connection.execute(
            "SELECT code.code_entity_id,code.kind,code.qualified_name,"
            "code.normalized_name,code.relative_file_path,code.start_line,"
            "code.end_line,code.identity_hash,mapping.source_key,"
            "mapping.projection_generation FROM discovery_code_entities AS code "
            "LEFT JOIN public_object_ids AS mapping ON "
            "mapping.public_id=code.code_entity_id "
            "AND mapping.workspace_id=code.workspace_id "
            "AND mapping.object_kind='code' WHERE code.workspace_id=? "
            "AND code.code_generation=? ORDER BY code.identity_hash LIMIT ?",
            (workspace_id, generation, max_entities + 1),
        ).fetchall()
        if len(entity_rows) > max_entities:
            raise CodeProjectionIntegrityError("CODE_PROJECTION_TOO_LARGE")
        entity_payload: list[object] = []
        entity_ids: set[str] = set()
        for row in entity_rows:
            code_id, kind, qualified_name, normalized_name = row[:4]
            relative_path, start_line, end_line, identity_hash = row[4:8]
            source_key, binding_generation = row[8:10]
            try:
                validated_path = _relative_path(relative_path)
            except DiscoveryProjectionBuildError:
                raise CodeProjectionIntegrityError("CODE_PROJECTION_CORRUPT") from None
            if (
                not isinstance(source_key, str)
                or not source_key.startswith("s:")
                or binding_generation != generation
                or _CODE_ID_RE.fullmatch(str(code_id)) is None
                or kind not in _CODE_KINDS
                or not isinstance(qualified_name, str)
                or not qualified_name
                or len(qualified_name) > 256
                or unicodedata.normalize("NFC", qualified_name) != qualified_name
                or normalized_name
                != unicodedata.normalize("NFC", qualified_name.casefold())
                or isinstance(start_line, bool)
                or not isinstance(start_line, int)
                or start_line < 1
                or isinstance(end_line, bool)
                or not isinstance(end_line, int)
                or end_line < start_line
                or identity_hash
                != sha256_json(
                    [
                        "code",
                        kind,
                        normalized_name,
                        validated_path,
                        start_line,
                        end_line,
                    ]
                )
                or code_id
                != derive_public_object_id(
                    workspace_id,
                    PublicObjectKind.CODE,
                    source_key[2:],
                    generation,
                )
            ):
                raise CodeProjectionIntegrityError("CODE_PROJECTION_CORRUPT")
            entity_ids.add(str(code_id))
            entity_payload.append(
                {
                    "code_entity_id": code_id,
                    "end_line": end_line,
                    "identity_hash": identity_hash,
                    "kind": kind,
                    "normalized_name": normalized_name,
                    "qualified_name": qualified_name,
                    "relative_file_path": validated_path,
                    "start_line": start_line,
                }
            )
        edge_rows = connection.execute(
            "SELECT source_code_entity_id,target_code_entity_id,edge_kind,"
            "source_line,identity_hash FROM discovery_code_edges "
            "WHERE workspace_id=? AND code_generation=? "
            "ORDER BY identity_hash LIMIT ?",
            (workspace_id, generation, max_edges + 1),
        ).fetchall()
        if len(edge_rows) > max_edges:
            raise CodeProjectionIntegrityError("CODE_PROJECTION_TOO_LARGE")
        edge_payload: list[object] = []
        for row in edge_rows:
            source_id, target_id, kind, source_line, identity_hash = row
            if (
                source_id not in entity_ids
                or target_id not in entity_ids
                or source_id == target_id
                or kind not in _CODE_EDGE_KINDS
                or (
                    source_line is not None
                    and (
                        isinstance(source_line, bool)
                        or not isinstance(source_line, int)
                        or source_line < 1
                    )
                )
                or identity_hash
                != sha256_json(["code-edge", source_id, target_id, kind, source_line])
            ):
                raise CodeProjectionIntegrityError("CODE_PROJECTION_CORRUPT")
            edge_payload.append(
                {
                    "edge_kind": kind,
                    "identity_hash": identity_hash,
                    "source_code_entity_id": source_id,
                    "source_line": source_line,
                    "target_code_entity_id": target_id,
                }
            )
        entity_hash = _partition_digest(entity_payload)
        combined_hash = sha256_json(
            {"code": entity_hash, "code_edges": _partition_digest(edge_payload)}
        )
        partition_hash = partition_rows[0][1]
        if (
            len(entity_rows) != expected_row_count
            or partition_count != expected_row_count
            or partition_hash != expected_root_hash
            or combined_hash != expected_root_hash
        ):
            raise CodeProjectionIntegrityError("CODE_PROJECTION_CORRUPT")
        return len(entity_rows), len(edge_rows)
    except CodeProjectionIntegrityError:
        raise
    except (DiscoveryProjectionBuildError, sqlite3.Error, TypeError, ValueError):
        raise CodeProjectionIntegrityError("CODE_PROJECTION_CORRUPT") from None


class DiscoveryProjectionBuilder:
    """Build immutable discovery rows on caller-selected SQLite storage."""

    def __init__(
        self,
        connection: sqlite3.Connection,
        *,
        clock_us=None,
    ) -> None:
        if not isinstance(connection, sqlite3.Connection):
            raise TypeError("connection must be sqlite3.Connection")
        if clock_us is not None and not callable(clock_us):
            raise TypeError("clock_us must be callable")
        self.connection = connection
        self._clock_us = clock_us or (lambda: time.time_ns() // 1_000)
        self._savepoint = 0

    def _require_schema(self) -> None:
        if not _table_names(self.connection).issuperset(_REQUIRED_TABLES):
            raise DiscoveryProjectionBuildError("DISCOVERY_SCHEMA_INCOMPLETE")
        foreign_keys = self.connection.execute("PRAGMA foreign_keys").fetchone()
        if foreign_keys is None or foreign_keys[0] != 1:
            raise DiscoveryProjectionBuildError("DISCOVERY_SCHEMA_INCOMPLETE")

    def _clock_value(self) -> int:
        try:
            value = self._clock_us()
        except Exception:
            raise DiscoveryProjectionBuildError("INVALID_CLOCK") from None
        if (
            isinstance(value, bool)
            or not isinstance(value, int)
            or not 0 <= value <= 2**63 - 1
        ):
            raise DiscoveryProjectionBuildError("INVALID_CLOCK")
        return value

    @contextmanager
    def _transaction(
        self,
        before_commit: Callable[[], None] | None = None,
    ):
        owns = not self.connection.in_transaction
        self._savepoint += 1
        savepoint = f"discovery_build_{self._savepoint}"
        try:
            if owns:
                self.connection.execute("BEGIN IMMEDIATE")
            else:
                self.connection.execute(f"SAVEPOINT {savepoint}")
            yield
            if before_commit is not None:
                before_commit()
            if owns:
                self.connection.commit()
            else:
                self.connection.execute(f"RELEASE SAVEPOINT {savepoint}")
        except Exception:
            if owns:
                if self.connection.in_transaction:
                    self.connection.rollback()
            else:
                self.connection.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
                self.connection.execute(f"RELEASE SAVEPOINT {savepoint}")
            raise

    def _active_graph_generation(self, workspace_id: str) -> int:
        rows = self.connection.execute(
            "SELECT generation FROM projection_manifests WHERE workspace_id=? "
            "AND projection_name='graph' AND status='active' LIMIT 2",
            (workspace_id,),
        ).fetchall()
        if len(rows) != 1:
            raise DiscoveryProjectionBuildError("GRAPH_PROJECTION_UNAVAILABLE")
        generation = rows[0][0]
        if isinstance(generation, bool) or not isinstance(generation, int):
            raise DiscoveryProjectionBuildError("GRAPH_PROJECTION_UNAVAILABLE")
        return generation

    def _candidate_graph_generation(self, workspace_id: str, generation: object) -> int:
        if (
            isinstance(generation, bool)
            or not isinstance(generation, int)
            or generation < 1
        ):
            raise DiscoveryProjectionBuildError("INVALID_DISCOVERY_SEED")
        rows = self.connection.execute(
            "SELECT status FROM projection_manifests WHERE workspace_id=? "
            "AND projection_name='graph' AND generation=? LIMIT 2",
            (workspace_id, generation),
        ).fetchall()
        if len(rows) != 1 or rows[0][0] != "building":
            raise DiscoveryProjectionBuildError("GRAPH_CANDIDATE_UNAVAILABLE")
        return generation

    def _validate_records(self, workspace_id: str, record_ids: set[str]) -> None:
        if not record_ids:
            return
        if len(record_ids) > _MAX_MEMBERSHIPS:
            raise DiscoveryProjectionBuildError("DISCOVERY_BUILD_TOO_LARGE")
        placeholders = ",".join("?" for _ in record_ids)
        rows = self.connection.execute(
            f"SELECT record_id FROM memory_records WHERE workspace_id=? "
            f"AND record_id IN ({placeholders}) AND deleted_at_us IS NULL",
            (workspace_id, *sorted(record_ids)),
        ).fetchall()
        if {str(row[0]) for row in rows} != record_ids or len(rows) != len(record_ids):
            raise DiscoveryProjectionBuildError("INVALID_DISCOVERY_SEED")

    @staticmethod
    def _entity_rows(
        seeds: Sequence[EntityProjectionSeed],
    ) -> tuple[_EntityRow, ...]:
        if not isinstance(seeds, Sequence) or len(seeds) > _MAX_ENTITIES:
            raise DiscoveryProjectionBuildError("DISCOVERY_BUILD_TOO_LARGE")
        result: list[_EntityRow] = []
        identities: set[str] = set()
        memberships = 0
        for seed in seeds:
            if not isinstance(seed, EntityProjectionSeed):
                raise DiscoveryProjectionBuildError("INVALID_DISCOVERY_SEED")
            name, normalized = _normalized_name(seed.name)
            entity_type = _safe_text(seed.entity_type, maximum=80)
            if _ENTITY_TYPE_RE.fullmatch(entity_type) is None:
                raise DiscoveryProjectionBuildError("INVALID_DISCOVERY_SEED")
            seen_records: set[str] = set()
            mentions = 0
            for record in seed.records:
                if not isinstance(record, EntityRecordSeed):
                    raise DiscoveryProjectionBuildError("INVALID_DISCOVERY_SEED")
                record_id = _record_id(record.record_id)
                if record_id in seen_records:
                    raise DiscoveryProjectionBuildError("INVALID_DISCOVERY_SEED")
                seen_records.add(record_id)
                mentions += _plain_int(
                    record.mention_count, minimum=1, maximum=1_000_000
                )
                memberships += 1
            declared = (
                mentions
                if seed.mention_count is None
                else _plain_int(seed.mention_count, minimum=0, maximum=1_000_000)
            )
            if declared < len(seen_records):
                raise DiscoveryProjectionBuildError("INVALID_DISCOVERY_SEED")
            identity = sha256_json(["entity", entity_type, normalized])
            if identity in identities:
                raise DiscoveryProjectionBuildError("INVALID_DISCOVERY_SEED")
            identities.add(identity)
            result.append(
                _EntityRow(
                    EntityProjectionSeed(
                        name=name,
                        entity_type=entity_type,
                        records=tuple(seed.records),
                        mention_count=declared,
                    ),
                    normalized,
                    identity,
                    declared,
                )
            )
        if memberships > _MAX_MEMBERSHIPS:
            raise DiscoveryProjectionBuildError("DISCOVERY_BUILD_TOO_LARGE")
        return tuple(sorted(result, key=lambda item: item.identity_hash))

    @staticmethod
    def _community_rows(
        seeds: Sequence[CommunityProjectionSeed],
    ) -> tuple[_CommunityRow, ...]:
        if not isinstance(seeds, Sequence) or len(seeds) > _MAX_COMMUNITIES:
            raise DiscoveryProjectionBuildError("DISCOVERY_BUILD_TOO_LARGE")
        indexed: dict[str, CommunityProjectionSeed] = {}
        memberships = 0
        for seed in seeds:
            if not isinstance(seed, CommunityProjectionSeed):
                raise DiscoveryProjectionBuildError("INVALID_DISCOVERY_SEED")
            source_key = _safe_text(seed.source_key, maximum=256)
            label, _ = _normalized_name(seed.label)
            level = _plain_int(seed.level, minimum=0, maximum=32)
            parent_key = (
                None
                if seed.parent_source_key is None
                else _safe_text(seed.parent_source_key, maximum=256)
            )
            members = tuple(_record_id(item) for item in seed.member_record_ids)
            if len(members) != len(set(members)):
                raise DiscoveryProjectionBuildError("INVALID_DISCOVERY_SEED")
            memberships += len(members)
            if source_key in indexed:
                raise DiscoveryProjectionBuildError("INVALID_DISCOVERY_SEED")
            indexed[source_key] = CommunityProjectionSeed(
                source_key=source_key,
                label=label,
                level=level,
                member_record_ids=members,
                parent_source_key=parent_key,
            )
        if memberships > _MAX_MEMBERSHIPS:
            raise DiscoveryProjectionBuildError("DISCOVERY_BUILD_TOO_LARGE")
        for source_key, seed in indexed.items():
            if seed.parent_source_key is None:
                continue
            parent_seed = indexed.get(seed.parent_source_key)
            if (
                parent_seed is None
                or parent_seed.source_key == source_key
                or parent_seed.level <= seed.level
            ):
                raise DiscoveryProjectionBuildError("INVALID_DISCOVERY_SEED")
            visited = {source_key}
            cursor = parent_seed
            while cursor.parent_source_key is not None:
                if cursor.parent_source_key in visited:
                    raise DiscoveryProjectionBuildError("INVALID_DISCOVERY_SEED")
                visited.add(cursor.parent_source_key)
                next_parent = indexed.get(cursor.parent_source_key)
                if next_parent is None or next_parent.level <= cursor.level:
                    raise DiscoveryProjectionBuildError("INVALID_DISCOVERY_SEED")
                cursor = next_parent
        result = [
            _CommunityRow(
                seed,
                sha256_json(
                    [
                        "community",
                        seed.source_key,
                        seed.label,
                        seed.level,
                        seed.parent_source_key,
                        sorted(seed.member_record_ids),
                    ]
                ),
            )
            for seed in indexed.values()
        ]
        return tuple(sorted(result, key=lambda item: item.seed.source_key))

    @staticmethod
    def _code_rows(
        seeds: Sequence[CodeEntityProjectionSeed],
    ) -> tuple[_CodeRow, ...]:
        if not isinstance(seeds, Sequence) or len(seeds) > _MAX_CODE_ENTITIES:
            raise DiscoveryProjectionBuildError("DISCOVERY_BUILD_TOO_LARGE")
        result: list[_CodeRow] = []
        source_keys: set[str] = set()
        identities: set[str] = set()
        for seed in seeds:
            if not isinstance(seed, CodeEntityProjectionSeed):
                raise DiscoveryProjectionBuildError("INVALID_DISCOVERY_SEED")
            source_key = _safe_text(seed.source_key, maximum=256)
            if source_key in source_keys or seed.kind not in _CODE_KINDS:
                raise DiscoveryProjectionBuildError("INVALID_DISCOVERY_SEED")
            source_keys.add(source_key)
            qualified, normalized = _normalized_name(seed.qualified_name)
            relative = _relative_path(seed.relative_file_path)
            start = _plain_int(seed.start_line, minimum=1, maximum=2**31 - 1)
            end = _plain_int(seed.end_line, minimum=start, maximum=2**31 - 1)
            identity = sha256_json(
                ["code", seed.kind, normalized, relative, start, end]
            )
            if identity in identities:
                raise DiscoveryProjectionBuildError("INVALID_DISCOVERY_SEED")
            identities.add(identity)
            result.append(
                _CodeRow(
                    CodeEntityProjectionSeed(
                        source_key,
                        seed.kind,
                        qualified,
                        relative,
                        start,
                        end,
                    ),
                    normalized,
                    identity,
                )
            )
        return tuple(sorted(result, key=lambda item: item.identity_hash))

    @staticmethod
    def _code_edge_rows(
        seeds: Sequence[CodeEdgeProjectionSeed],
        code_rows: tuple[_CodeRow, ...],
    ) -> tuple[_CodeEdgeRow, ...]:
        if not isinstance(seeds, Sequence) or len(seeds) > _MAX_CODE_EDGES:
            raise DiscoveryProjectionBuildError("DISCOVERY_BUILD_TOO_LARGE")
        source_keys = {row.seed.source_key for row in code_rows}
        result: list[_CodeEdgeRow] = []
        edge_keys: set[tuple[str, str, str]] = set()
        identities: set[str] = set()
        for seed in seeds:
            if not isinstance(seed, CodeEdgeProjectionSeed):
                raise DiscoveryProjectionBuildError("INVALID_DISCOVERY_SEED")
            source_key = _safe_text(seed.source_key, maximum=256)
            target_key = _safe_text(seed.target_key, maximum=256)
            if (
                source_key not in source_keys
                or target_key not in source_keys
                or source_key == target_key
                or seed.kind not in _CODE_EDGE_KINDS
            ):
                raise DiscoveryProjectionBuildError("INVALID_DISCOVERY_SEED")
            source_line = (
                None
                if seed.source_line is None
                else _plain_int(seed.source_line, minimum=1, maximum=2**31 - 1)
            )
            edge_key = (source_key, target_key, seed.kind)
            if edge_key in edge_keys:
                raise DiscoveryProjectionBuildError("INVALID_DISCOVERY_SEED")
            edge_keys.add(edge_key)
            identity = sha256_json(
                ["code-edge", source_key, target_key, seed.kind, source_line]
            )
            if identity in identities:
                raise DiscoveryProjectionBuildError("INVALID_DISCOVERY_SEED")
            identities.add(identity)
            result.append(
                _CodeEdgeRow(
                    CodeEdgeProjectionSeed(
                        source_key=source_key,
                        target_key=target_key,
                        kind=seed.kind,
                        source_line=source_line,
                    ),
                    identity,
                )
            )
        return tuple(sorted(result, key=lambda item: item.identity_hash))

    @staticmethod
    def _graph_payloads(
        entities: tuple[_EntityRow, ...],
        communities: tuple[_CommunityRow, ...],
        entity_ids: dict[str, str],
        community_ids: dict[str, str],
    ) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
        entity_payload = [
            {
                "entity_id": entity_ids[row.identity_hash],
                "entity_type": row.seed.entity_type,
                "identity_hash": row.identity_hash,
                "mention_count": row.mention_count,
                "name": row.seed.name,
                "normalized_name": row.normalized_name,
                "records": [
                    [record.record_id, record.mention_count]
                    for record in sorted(
                        row.seed.records, key=lambda item: item.record_id
                    )
                ],
            }
            for row in entities
        ]
        community_payload: list[dict[str, object]] = [
            {
                "community_id": community_ids[row.seed.source_key],
                "identity_hash": row.identity_hash,
                "label": row.seed.label,
                "level": row.seed.level,
                "members": sorted(row.seed.member_record_ids),
                "parent_community_id": (
                    None
                    if row.seed.parent_source_key is None
                    else community_ids[row.seed.parent_source_key]
                ),
            }
            for row in communities
        ]
        return entity_payload, community_payload

    def graph_matches(
        self,
        workspace_id: str,
        generation: int,
        *,
        entities: Sequence[EntityProjectionSeed],
        communities: Sequence[CommunityProjectionSeed],
    ) -> bool:
        """Return whether a complete retained graph partition matches the seeds."""

        workspace_id = _workspace_id(workspace_id)
        if (
            isinstance(generation, bool)
            or not isinstance(generation, int)
            or generation < 1
        ):
            return False
        self._require_schema()
        entity_rows = self._entity_rows(entities)
        community_rows = self._community_rows(communities)
        entity_ids = {
            row.identity_hash: derive_public_object_id(
                workspace_id,
                PublicObjectKind.ENTITY,
                row.identity_hash,
            )
            for row in entity_rows
        }
        community_ids = {
            row.seed.source_key: derive_public_object_id(
                workspace_id,
                PublicObjectKind.COMMUNITY,
                row.seed.source_key,
                generation,
            )
            for row in community_rows
        }
        entity_payload, community_payload = self._graph_payloads(
            entity_rows, community_rows, entity_ids, community_ids
        )
        expected = {
            "entities": (len(entity_rows), _partition_digest(entity_payload)),
            "communities": (len(community_rows), _partition_digest(community_payload)),
        }
        try:
            actual = {
                str(row[0]): (int(row[1]), str(row[2]))
                for row in self.connection.execute(
                    "SELECT partition_name,row_count,content_hash "
                    "FROM discovery_projection_partitions WHERE workspace_id=? "
                    "AND projection_name='graph' AND generation=?",
                    (workspace_id, generation),
                )
            }
        except (sqlite3.Error, TypeError, ValueError):
            return False
        return actual == expected

    def populate_graph(
        self,
        workspace_id: str,
        *,
        entities: Sequence[EntityProjectionSeed],
        communities: Sequence[CommunityProjectionSeed],
        generation: int | None = None,
    ) -> DiscoveryProjectionBuildResult:
        workspace_id = _workspace_id(workspace_id)
        self._require_schema()
        entity_rows = self._entity_rows(entities)
        community_rows = self._community_rows(communities)
        record_ids = {
            record.record_id for entity in entity_rows for record in entity.seed.records
        } | {
            record_id
            for community in community_rows
            for record_id in community.seed.member_record_ids
        }
        try:
            with self._transaction():
                generation = (
                    self._active_graph_generation(workspace_id)
                    if generation is None
                    else self._candidate_graph_generation(workspace_id, generation)
                )
                self._validate_records(workspace_id, record_ids)
                repository = PublicObjectIdRepository(
                    self.connection, clock_us=self._clock_value
                )
                entity_ids = {
                    row.identity_hash: repository.get_or_create(
                        workspace_id,
                        PublicObjectKind.ENTITY,
                        row.identity_hash,
                    )
                    for row in entity_rows
                }
                community_ids = {
                    row.seed.source_key: repository.get_or_create(
                        workspace_id,
                        PublicObjectKind.COMMUNITY,
                        row.seed.source_key,
                        generation,
                    )
                    for row in community_rows
                }
                entity_payload, community_payload = self._graph_payloads(
                    entity_rows, community_rows, entity_ids, community_ids
                )
                expected_partitions = {
                    "entities": (len(entity_rows), _partition_digest(entity_payload)),
                    "communities": (
                        len(community_rows),
                        _partition_digest(community_payload),
                    ),
                }
                existing = {
                    str(row[0]): (int(row[1]), str(row[2]))
                    for row in self.connection.execute(
                        "SELECT partition_name,row_count,content_hash "
                        "FROM discovery_projection_partitions "
                        "WHERE workspace_id=? AND projection_name='graph' "
                        "AND generation=?",
                        (workspace_id, generation),
                    )
                }
                if existing:
                    if existing != expected_partitions:
                        raise DiscoveryProjectionBuildError(
                            "DISCOVERY_PROJECTION_CONFLICT"
                        )
                    return DiscoveryProjectionBuildResult(
                        graph_generation=generation,
                        entity_ids=tuple(
                            entity_ids[row.identity_hash] for row in entity_rows
                        ),
                        community_ids=tuple(
                            community_ids[row.seed.source_key] for row in community_rows
                        ),
                        reused=True,
                    )
                now = self._clock_value()
                for entity_row in entity_rows:
                    entity_id = entity_ids[entity_row.identity_hash]
                    self.connection.execute(
                        "INSERT INTO discovery_entities("
                        "workspace_id,graph_generation,entity_id,name,"
                        "normalized_name,entity_type,mention_count,identity_hash) "
                        "VALUES (?,?,?,?,?,?,?,?)",
                        (
                            workspace_id,
                            generation,
                            entity_id,
                            entity_row.seed.name,
                            entity_row.normalized_name,
                            entity_row.seed.entity_type,
                            entity_row.mention_count,
                            entity_row.identity_hash,
                        ),
                    )
                    for record in sorted(
                        entity_row.seed.records, key=lambda item: item.record_id
                    ):
                        self.connection.execute(
                            "INSERT INTO discovery_entity_records VALUES (?,?,?,?,?)",
                            (
                                workspace_id,
                                generation,
                                entity_id,
                                record.record_id,
                                record.mention_count,
                            ),
                        )
                for community_row in community_rows:
                    self.connection.execute(
                        "INSERT INTO discovery_communities("
                        "workspace_id,graph_generation,community_id,label,level,"
                        "parent_community_id,member_count,identity_hash) "
                        "VALUES (?,?,?,?,?,?,?,?)",
                        (
                            workspace_id,
                            generation,
                            community_ids[community_row.seed.source_key],
                            community_row.seed.label,
                            community_row.seed.level,
                            (
                                None
                                if community_row.seed.parent_source_key is None
                                else community_ids[community_row.seed.parent_source_key]
                            ),
                            len(community_row.seed.member_record_ids),
                            community_row.identity_hash,
                        ),
                    )
                for community_row in community_rows:
                    for record_id in sorted(community_row.seed.member_record_ids):
                        self.connection.execute(
                            "INSERT INTO discovery_community_members VALUES (?,?,?,?)",
                            (
                                workspace_id,
                                generation,
                                community_ids[community_row.seed.source_key],
                                record_id,
                            ),
                        )
                for name, (row_count, digest) in expected_partitions.items():
                    self.connection.execute(
                        "INSERT INTO discovery_projection_partitions VALUES "
                        "(?,'graph',?,?,?,?,?,?)",
                        (
                            workspace_id,
                            generation,
                            name,
                            row_count,
                            digest,
                            _BUILDER_VERSION,
                            now,
                        ),
                    )
                return DiscoveryProjectionBuildResult(
                    graph_generation=generation,
                    entity_ids=tuple(
                        entity_ids[row.identity_hash] for row in entity_rows
                    ),
                    community_ids=tuple(
                        community_ids[row.seed.source_key] for row in community_rows
                    ),
                )
        except DiscoveryProjectionBuildError:
            raise
        except (sqlite3.Error, ValueError):
            raise DiscoveryProjectionBuildError("DISCOVERY_BUILD_FAILED") from None

    @staticmethod
    def _code_payload(
        rows: tuple[_CodeRow, ...], code_ids: dict[str, str]
    ) -> list[object]:
        return [
            {
                "code_entity_id": code_ids[row.seed.source_key],
                "end_line": row.seed.end_line,
                "identity_hash": row.identity_hash,
                "kind": row.seed.kind,
                "normalized_name": row.normalized_name,
                "qualified_name": row.seed.qualified_name,
                "relative_file_path": row.seed.relative_file_path,
                "start_line": row.seed.start_line,
            }
            for row in rows
        ]

    @staticmethod
    def _code_edge_payload(
        rows: tuple[_CodeEdgeRow, ...], code_ids: dict[str, str]
    ) -> list[dict[str, object]]:
        payload: list[dict[str, object]] = [
            {
                "edge_kind": row.seed.kind,
                "identity_hash": sha256_json(
                    [
                        "code-edge",
                        code_ids[row.seed.source_key],
                        code_ids[row.seed.target_key],
                        row.seed.kind,
                        row.seed.source_line,
                    ]
                ),
                "source_code_entity_id": code_ids[row.seed.source_key],
                "source_line": row.seed.source_line,
                "target_code_entity_id": code_ids[row.seed.target_key],
            }
            for row in rows
        ]
        payload.sort(key=lambda item: str(item["identity_hash"]))
        return payload

    def rebuild_code(
        self,
        workspace_id: str,
        *,
        entities: Sequence[CodeEntityProjectionSeed],
        edges: Sequence[CodeEdgeProjectionSeed] = (),
        force: bool = False,
        before_commit: Callable[[], None] | None = None,
    ) -> DiscoveryProjectionBuildResult:
        workspace_id = _workspace_id(workspace_id)
        if not isinstance(force, bool):
            raise DiscoveryProjectionBuildError("INVALID_DISCOVERY_SEED")
        if before_commit is not None and not callable(before_commit):
            raise TypeError("before_commit must be callable")
        self._require_schema()
        rows = self._code_rows(entities)
        edge_rows = self._code_edge_rows(edges, rows)
        try:
            with self._transaction(before_commit):
                active = self.connection.execute(
                    "SELECT generation,source_event_root_hash,row_count "
                    "FROM projection_manifests WHERE workspace_id=? "
                    "AND projection_name='code' AND status='active' LIMIT 2",
                    (workspace_id,),
                ).fetchall()
                next_generation = int(
                    self.connection.execute(
                        "SELECT COALESCE(MAX(generation),0)+1 "
                        "FROM projection_manifests WHERE workspace_id=? "
                        "AND projection_name='code'",
                        (workspace_id,),
                    ).fetchone()[0]
                )
                if active and not force:
                    if len(active) != 1:
                        raise DiscoveryProjectionBuildError(
                            "DISCOVERY_PROJECTION_CONFLICT"
                        )
                    generation = int(active[0][0])
                    expected_ids = {
                        row.seed.source_key: derive_public_object_id(
                            workspace_id,
                            PublicObjectKind.CODE,
                            row.seed.source_key,
                            generation,
                        )
                        for row in rows
                    }
                    entity_hash = _partition_digest(
                        self._code_payload(rows, expected_ids)
                    )
                    edge_hash = _partition_digest(
                        self._code_edge_payload(edge_rows, expected_ids)
                    )
                    content_hash = sha256_json(
                        {"code": entity_hash, "code_edges": edge_hash}
                    )
                    partition = self.connection.execute(
                        "SELECT row_count,content_hash FROM "
                        "discovery_projection_partitions WHERE workspace_id=? "
                        "AND projection_name='code' AND generation=? "
                        "AND partition_name='code'",
                        (workspace_id, generation),
                    ).fetchone()
                    if (
                        str(active[0][1]) == content_hash
                        and int(active[0][2]) == len(rows)
                        and partition is not None
                        and int(partition[0]) == len(rows)
                        and str(partition[1]) == content_hash
                    ):
                        existing_ids = tuple(
                            str(row[0])
                            for row in self.connection.execute(
                                "SELECT code_entity_id FROM discovery_code_entities "
                                "WHERE workspace_id=? AND code_generation=? "
                                "ORDER BY identity_hash",
                                (workspace_id, generation),
                            )
                        )
                        if existing_ids != tuple(
                            expected_ids[row.seed.source_key] for row in rows
                        ):
                            raise DiscoveryProjectionBuildError(
                                "DISCOVERY_PROJECTION_CONFLICT"
                            )
                        return DiscoveryProjectionBuildResult(
                            code_generation=generation,
                            code_entity_ids=existing_ids,
                            reused=True,
                        )
                generation = next_generation
                expected_ids = {
                    row.seed.source_key: derive_public_object_id(
                        workspace_id,
                        PublicObjectKind.CODE,
                        row.seed.source_key,
                        generation,
                    )
                    for row in rows
                }
                entity_hash = _partition_digest(self._code_payload(rows, expected_ids))
                edge_hash = _partition_digest(
                    self._code_edge_payload(edge_rows, expected_ids)
                )
                content_hash = sha256_json(
                    {"code": entity_hash, "code_edges": edge_hash}
                )
                repository = PublicObjectIdRepository(
                    self.connection, clock_us=self._clock_value
                )
                code_ids = {
                    row.seed.source_key: repository.get_or_create(
                        workspace_id,
                        PublicObjectKind.CODE,
                        row.seed.source_key,
                        generation,
                    )
                    for row in rows
                }
                if code_ids != expected_ids:
                    raise DiscoveryProjectionBuildError("DISCOVERY_PROJECTION_CONFLICT")
                now = self._clock_value()
                manifest_id = deterministic_id(
                    "prj",
                    "projection",
                    workspace_id,
                    "code",
                    generation,
                    content_hash,
                )
                self.connection.execute(
                    "INSERT INTO projection_manifests("
                    "manifest_id,workspace_id,projection_name,generation,"
                    "projection_version,status,source_event_count,"
                    "source_event_root_hash,row_count,builder_version,details_json,"
                    "started_at_us,completed_at_us,activated_at_us) "
                    "VALUES (?,?,'code',?,1,'building',0,?,0,?,?,?,NULL,NULL)",
                    (
                        manifest_id,
                        workspace_id,
                        generation,
                        content_hash,
                        _BUILDER_VERSION,
                        canonical_json_bytes(
                            {
                                "builder_version": _BUILDER_VERSION,
                                "content_digest": content_hash,
                            }
                        ).decode("utf-8"),
                        now,
                    ),
                )
                for row in rows:
                    self.connection.execute(
                        "INSERT INTO discovery_code_entities("
                        "workspace_id,code_generation,code_entity_id,kind,"
                        "qualified_name,normalized_name,relative_file_path,"
                        "start_line,end_line,identity_hash) "
                        "VALUES (?,?,?,?,?,?,?,?,?,?)",
                        (
                            workspace_id,
                            generation,
                            code_ids[row.seed.source_key],
                            row.seed.kind,
                            row.seed.qualified_name,
                            row.normalized_name,
                            row.seed.relative_file_path,
                            row.seed.start_line,
                            row.seed.end_line,
                            row.identity_hash,
                        ),
                    )
                for edge in edge_rows:
                    self.connection.execute(
                        "INSERT INTO discovery_code_edges("
                        "workspace_id,code_generation,source_code_entity_id,"
                        "target_code_entity_id,edge_kind,source_line,identity_hash) "
                        "VALUES (?,?,?,?,?,?,?)",
                        (
                            workspace_id,
                            generation,
                            code_ids[edge.seed.source_key],
                            code_ids[edge.seed.target_key],
                            edge.seed.kind,
                            edge.seed.source_line,
                            sha256_json(
                                [
                                    "code-edge",
                                    code_ids[edge.seed.source_key],
                                    code_ids[edge.seed.target_key],
                                    edge.seed.kind,
                                    edge.seed.source_line,
                                ]
                            ),
                        ),
                    )
                self.connection.execute(
                    "INSERT INTO discovery_projection_partitions VALUES "
                    "(?,'code',?,'code',?,?,?,?)",
                    (
                        workspace_id,
                        generation,
                        len(rows),
                        content_hash,
                        _BUILDER_VERSION,
                        now,
                    ),
                )
                self.connection.execute(
                    "UPDATE projection_manifests SET status='ready' "
                    "WHERE workspace_id=? AND projection_name='code' "
                    "AND status='active'",
                    (workspace_id,),
                )
                changed = self.connection.execute(
                    "UPDATE projection_manifests SET status='active',row_count=?,"
                    "completed_at_us=?,activated_at_us=? WHERE manifest_id=? "
                    "AND status='building'",
                    (len(rows), now, now, manifest_id),
                ).rowcount
                if changed != 1:
                    raise DiscoveryProjectionBuildError("DISCOVERY_PROJECTION_CONFLICT")
                return DiscoveryProjectionBuildResult(
                    code_generation=generation,
                    code_entity_ids=tuple(
                        code_ids[row.seed.source_key] for row in rows
                    ),
                )
        except DiscoveryProjectionBuildError:
            raise
        except (sqlite3.Error, ValueError):
            raise DiscoveryProjectionBuildError("DISCOVERY_BUILD_FAILED") from None

    @staticmethod
    def _legacy_root(workspace_root: Path) -> Path:
        if not isinstance(workspace_root, Path):
            raise DiscoveryProjectionBuildError("UNSAFE_LEGACY_PATH")
        try:
            root = workspace_root.resolve(strict=True)
        except (OSError, RuntimeError):
            raise DiscoveryProjectionBuildError("UNSAFE_LEGACY_PATH") from None
        if not root.is_dir() or root.is_symlink():
            raise DiscoveryProjectionBuildError("UNSAFE_LEGACY_PATH")
        return root

    @staticmethod
    def _legacy_project_matches(value: object, root: Path) -> bool:
        if not isinstance(value, str) or not value or _CONTROL_RE.search(value):
            raise DiscoveryProjectionBuildError("UNSAFE_LEGACY_PATH")
        try:
            candidate = Path(value).resolve(strict=False)
        except (OSError, RuntimeError, ValueError):
            raise DiscoveryProjectionBuildError("UNSAFE_LEGACY_PATH") from None
        return os.path.normcase(str(candidate)) == os.path.normcase(str(root))

    @staticmethod
    def _legacy_relative(value: object, root: Path) -> str:
        if not isinstance(value, str) or not value or _CONTROL_RE.search(value):
            raise DiscoveryProjectionBuildError("UNSAFE_LEGACY_PATH")
        try:
            candidate = Path(value)
            if not candidate.is_absolute():
                candidate = root.joinpath(
                    *PurePosixPath(value.replace("\\", "/")).parts
                )
            resolved = candidate.resolve(strict=False)
            relative = resolved.relative_to(root).as_posix()
        except (OSError, RuntimeError, ValueError):
            raise DiscoveryProjectionBuildError("UNSAFE_LEGACY_PATH") from None
        try:
            return _relative_path(relative)
        except DiscoveryProjectionBuildError:
            raise DiscoveryProjectionBuildError("UNSAFE_LEGACY_PATH") from None

    def _legacy_record_map(
        self,
        workspace_id: str,
        migration_run_id: str,
    ) -> MappingProxyType:
        rows = self.connection.execute(
            "SELECT legacy_id,target_id FROM legacy_id_map WHERE workspace_id=? "
            "AND migration_run_id=? AND source_table='memories' "
            "AND target_kind='memory' ORDER BY legacy_id",
            (workspace_id, migration_run_id),
        ).fetchall()
        mapping: dict[str, str] = {}
        for row in rows:
            legacy_id, target_id = str(row[0]), str(row[1])
            if legacy_id in mapping or _RECORD_ID_RE.fullmatch(target_id) is None:
                raise DiscoveryProjectionBuildError("INVALID_LEGACY_DISCOVERY")
            mapping[legacy_id] = target_id
        return MappingProxyType(mapping)

    @staticmethod
    def _legacy_member_ids(value: object) -> tuple[str, ...]:
        try:
            decoded = json.loads(str(value))
        except (TypeError, ValueError, RecursionError):
            raise DiscoveryProjectionBuildError("INVALID_LEGACY_DISCOVERY") from None
        if (
            not isinstance(decoded, list)
            or len(decoded) > _MAX_MEMBERSHIPS
            or any(
                isinstance(item, bool) or not isinstance(item, (int, str))
                for item in decoded
            )
        ):
            raise DiscoveryProjectionBuildError("INVALID_LEGACY_DISCOVERY")
        result = tuple(str(item) for item in decoded)
        if len(result) != len(set(result)):
            raise DiscoveryProjectionBuildError("INVALID_LEGACY_DISCOVERY")
        return result

    def import_legacy(
        self,
        workspace_id: str,
        *,
        workspace_root: Path,
        migration_run_id: str,
    ) -> DiscoveryProjectionBuildResult:
        """Import retained discovery rows through one exact legacy ID map."""

        workspace_id = _workspace_id(workspace_id)
        if (
            not isinstance(migration_run_id, str)
            or re.fullmatch(r"mig_[0-9a-f]{64}", migration_run_id) is None
        ):
            raise DiscoveryProjectionBuildError("INVALID_LEGACY_DISCOVERY")
        self._require_schema()
        root = self._legacy_root(workspace_root)
        tables = _table_names(self.connection)
        legacy_tables = {
            "extracted_entities",
            "memory_entity_refs",
            "memory_communities",
            "code_entities",
            "legacy_id_map",
        }
        if not legacy_tables <= tables:
            raise DiscoveryProjectionBuildError("INVALID_LEGACY_DISCOVERY")
        try:
            with self._transaction():
                record_map = self._legacy_record_map(workspace_id, migration_run_id)
                selected_entities: dict[int, sqlite3.Row] = {}
                self.connection.row_factory = sqlite3.Row
                for row in self.connection.execute(
                    "SELECT id,project_path,entity_type,name,qualified_name,"
                    "mention_count FROM extracted_entities ORDER BY id"
                ):
                    if self._legacy_project_matches(row["project_path"], root):
                        if isinstance(row["id"], bool) or not isinstance(
                            row["id"], int
                        ):
                            raise DiscoveryProjectionBuildError(
                                "INVALID_LEGACY_DISCOVERY"
                            )
                        selected_entities[int(row["id"])] = row
                refs: dict[int, list[str]] = {
                    entity_id: [] for entity_id in selected_entities
                }
                for row in self.connection.execute(
                    "SELECT memory_id,entity_id FROM memory_entity_refs ORDER BY id"
                ):
                    entity_id = row["entity_id"]
                    if entity_id not in selected_entities:
                        continue
                    target = record_map.get(str(row["memory_id"]))
                    if target is None:
                        raise DiscoveryProjectionBuildError("INVALID_LEGACY_DISCOVERY")
                    refs[int(entity_id)].append(target)
                entity_seeds: list[EntityProjectionSeed] = []
                for entity_id, row in selected_entities.items():
                    members = sorted(set(refs[entity_id]))
                    declared = row["mention_count"]
                    if (
                        isinstance(declared, bool)
                        or not isinstance(declared, int)
                        or declared < len(members)
                    ):
                        raise DiscoveryProjectionBuildError("INVALID_LEGACY_DISCOVERY")
                    entity_seeds.append(
                        EntityProjectionSeed(
                            name=str(row["qualified_name"] or row["name"]),
                            entity_type=str(row["entity_type"]),
                            records=tuple(EntityRecordSeed(item) for item in members),
                            mention_count=declared,
                        )
                    )
                community_rows: dict[int, sqlite3.Row] = {}
                for row in self.connection.execute(
                    "SELECT id,project_path,name,member_count,member_ids,level,"
                    "parent_id FROM memory_communities ORDER BY id"
                ):
                    if self._legacy_project_matches(row["project_path"], root):
                        if isinstance(row["id"], bool) or not isinstance(
                            row["id"], int
                        ):
                            raise DiscoveryProjectionBuildError(
                                "INVALID_LEGACY_DISCOVERY"
                            )
                        community_rows[int(row["id"])] = row
                community_seeds: list[CommunityProjectionSeed] = []
                for community_id, row in community_rows.items():
                    legacy_members = self._legacy_member_ids(row["member_ids"])
                    mapped: list[str] = []
                    for legacy_id in legacy_members:
                        target = record_map.get(legacy_id)
                        if target is None:
                            raise DiscoveryProjectionBuildError(
                                "INVALID_LEGACY_DISCOVERY"
                            )
                        mapped.append(target)
                    if row["member_count"] != len(mapped):
                        raise DiscoveryProjectionBuildError("INVALID_LEGACY_DISCOVERY")
                    parent_id = row["parent_id"]
                    if parent_id is not None and parent_id not in community_rows:
                        raise DiscoveryProjectionBuildError("INVALID_LEGACY_DISCOVERY")
                    community_seeds.append(
                        CommunityProjectionSeed(
                            source_key=f"legacy-community:{community_id}",
                            label=str(row["name"]),
                            level=row["level"],
                            member_record_ids=tuple(mapped),
                            parent_source_key=(
                                None
                                if parent_id is None
                                else f"legacy-community:{parent_id}"
                            ),
                        )
                    )
                code_seeds: list[CodeEntityProjectionSeed] = []
                kind_map = {"import": "symbol"}
                for row in self.connection.execute(
                    "SELECT id,project_path,entity_type,name,qualified_name,"
                    "file_path,line_start,line_end FROM code_entities ORDER BY id"
                ):
                    if not self._legacy_project_matches(row["project_path"], root):
                        continue
                    kind = kind_map.get(
                        str(row["entity_type"]), str(row["entity_type"])
                    )
                    if kind not in _CODE_KINDS:
                        kind = "symbol"
                    start = 1 if row["line_start"] is None else row["line_start"]
                    end = start if row["line_end"] is None else row["line_end"]
                    code_seeds.append(
                        CodeEntityProjectionSeed(
                            source_key=f"legacy-code:{row['id']}",
                            kind=kind,
                            qualified_name=str(row["qualified_name"] or row["name"]),
                            relative_file_path=self._legacy_relative(
                                row["file_path"], root
                            ),
                            start_line=start,
                            end_line=end,
                        )
                    )
                graph = self.populate_graph(
                    workspace_id,
                    entities=tuple(entity_seeds),
                    communities=tuple(community_seeds),
                )
                code = self.rebuild_code(
                    workspace_id,
                    entities=tuple(code_seeds),
                    force=False,
                )
                return DiscoveryProjectionBuildResult(
                    graph_generation=graph.graph_generation,
                    code_generation=code.code_generation,
                    entity_ids=graph.entity_ids,
                    community_ids=graph.community_ids,
                    code_entity_ids=code.code_entity_ids,
                    reused=graph.reused and code.reused,
                )
        except DiscoveryProjectionBuildError:
            raise
        except (sqlite3.Error, ValueError):
            raise DiscoveryProjectionBuildError("INVALID_LEGACY_DISCOVERY") from None


__all__ = [
    "CodeEdgeProjectionSeed",
    "CodeEntityProjectionSeed",
    "CommunityProjectionSeed",
    "DiscoveryProjectionBuildError",
    "DiscoveryProjectionBuildResult",
    "DiscoveryProjectionBuilder",
    "EntityProjectionSeed",
    "EntityRecordSeed",
]
