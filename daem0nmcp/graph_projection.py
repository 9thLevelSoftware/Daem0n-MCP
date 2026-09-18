"""Canonical, generation-scoped entity and community graph projection builds."""

from __future__ import annotations

import hashlib
import json
import math
import re
import sqlite3
import threading
import time
import unicodedata
from collections import Counter, defaultdict
from collections.abc import Callable, Mapping
from contextlib import AbstractContextManager, nullcontext
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

from .api.v7.public_ids import PublicObjectKind, derive_public_object_id
from .discovery_projection import (
    CommunityProjectionSeed,
    DiscoveryProjectionBuilder,
    DiscoveryProjectionBuildError,
    DiscoveryProjectionBuildResult,
    EntityProjectionSeed,
    EntityRecordSeed,
)
from .entity_extractor import EntityExtractor
from .event_store import canonical_json_bytes, sha256_json
from .retrieval.specialized_projection import (
    SpecializedProjectionBuilder,
    SpecializedProjectionBuildError,
    SpecializedProjectionBuildResult,
)

_MAX_RECORDS = 100_000
_MAX_CONTENT_BYTES = 32 * 1024 * 1024
_MAX_ENTITIES = 100_000
_MAX_ENTITY_MEMBERSHIPS = 250_000
_MAX_EDGES = 200_000
_FETCH_ROWS = 256
_BUILD_TIMEOUT_SECONDS = 30.0
_BUILDER_VERSION = "graph-discovery-v1"
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")


class GraphProjectionBuildError(RuntimeError):
    """Stable graph projection build failure."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class GraphProjectionBuildResult:
    generation: int
    source_event_root_hash: str
    built_at_us: int
    entities: tuple[EntityProjectionSeed, ...]
    communities: tuple[CommunityProjectionSeed, ...]
    entity_ids: tuple[str, ...]
    community_ids: tuple[str, ...]
    community_ids_by_source_key: Mapping[str, str]
    scanned: int
    extracted: int
    skipped: int
    modularity: float
    reused: bool


@dataclass(frozen=True, slots=True)
class _CanonicalGraphSnapshot:
    event_snapshot: tuple[int, str, tuple[int, str] | None]
    records: tuple[tuple[str, str], ...]
    edges: tuple[tuple[str, str], ...]


def _checkpoint(cancelled: threading.Event | None, deadline: float) -> None:
    if cancelled is not None and cancelled.is_set():
        raise GraphProjectionBuildError("CANCELLED")
    if time.monotonic() >= deadline:
        raise GraphProjectionBuildError("DEADLINE_EXCEEDED")


def _event_snapshot(
    connection: sqlite3.Connection,
    workspace_id: str,
    cancelled: threading.Event | None,
    deadline: float,
) -> tuple[int, str, tuple[int, str] | None]:
    digest = hashlib.sha256()
    count = 0
    rows = connection.execute(
        "SELECT event_hash FROM memory_events WHERE workspace_id=? ORDER BY event_id",
        (workspace_id,),
    )
    while True:
        _checkpoint(cancelled, deadline)
        page = rows.fetchmany(_FETCH_ROWS)
        if not page:
            break
        for row in page:
            try:
                digest.update(bytes.fromhex(str(row[0])))
            except ValueError:
                raise GraphProjectionBuildError(
                    "PROJECTION_VALIDATION_FAILED"
                ) from None
            count += 1
    row = connection.execute(
        "SELECT recorded_at_us,event_id FROM memory_events WHERE workspace_id=? "
        "ORDER BY recorded_at_us DESC,event_id DESC LIMIT 1",
        (workspace_id,),
    ).fetchone()
    cursor = None if row is None else (int(row[0]), str(row[1]))
    return count, digest.hexdigest(), cursor


def _canonical_snapshot(
    connection: sqlite3.Connection,
    workspace_id: str,
    now_us: int,
    cancelled: threading.Event | None,
    deadline: float,
) -> _CanonicalGraphSnapshot:
    if connection.in_transaction:
        raise GraphProjectionBuildError("PROJECTION_BUILD_FAILED")
    connection.execute("BEGIN")
    try:
        raw_records = connection.execute(
            "SELECT record.record_id,record.content,event.workspace_id,event.stream_id,"
            "event.payload_json FROM memory_records AS record "
            "JOIN memory_events AS event "
            "ON event.event_id=record.source_event_id WHERE record.workspace_id=? "
            "AND record.deleted_at_us IS NULL ORDER BY record.record_id LIMIT ?",
            (workspace_id, _MAX_RECORDS + 1),
        )
        records: list[tuple[str, str]] = []
        live_ids: set[str] = set()
        content_bytes = 0
        while True:
            _checkpoint(cancelled, deadline)
            page = raw_records.fetchmany(_FETCH_ROWS)
            if not page:
                break
            for row in page:
                if len(records) >= _MAX_RECORDS:
                    raise GraphProjectionBuildError("TASK_REQUIRED")
                payload = json.loads(str(row[4]))
                if canonical_json_bytes(payload).decode("utf-8") != row[4]:
                    raise ValueError
                source = payload.get("record")
                if (
                    not isinstance(source, dict)
                    or row[2] != workspace_id
                    or row[3] != row[0]
                    or source.get("content") != row[1]
                    or not isinstance(row[0], str)
                    or not isinstance(row[1], str)
                ):
                    raise ValueError
                encoded_size = len(row[1].encode("utf-8"))
                if content_bytes + encoded_size > _MAX_CONTENT_BYTES:
                    raise GraphProjectionBuildError("TASK_REQUIRED")
                content_bytes += encoded_size
                records.append((row[0], row[1]))
                live_ids.add(row[0])
        raw_edges = connection.execute(
            "SELECT source_record_id,target_record_id "
            "FROM memory_relationship_versions "
            "WHERE workspace_id=? AND transaction_to_us IS NULL AND valid_from_us<=? "
            "AND (valid_to_us IS NULL OR valid_to_us>?) UNION ALL "
            "SELECT subject_record_id,json_extract(object_json,'$') "
            "FROM memory_fact_versions WHERE workspace_id=? "
            "AND object_kind='record_ref' "
            "AND transaction_to_us IS NULL AND valid_from_us<=? "
            "AND (valid_to_us IS NULL OR valid_to_us>?) LIMIT ?",
            (
                workspace_id,
                now_us,
                now_us,
                workspace_id,
                now_us,
                now_us,
                _MAX_EDGES + 1,
            ),
        )
        edges: set[tuple[str, str]] = set()
        raw_edge_count = 0
        while True:
            _checkpoint(cancelled, deadline)
            page = raw_edges.fetchmany(_FETCH_ROWS)
            if not page:
                break
            for row in page:
                raw_edge_count += 1
                if raw_edge_count > _MAX_EDGES:
                    raise GraphProjectionBuildError("TASK_REQUIRED")
                source, target = row[0], row[1]
                if not isinstance(source, str) or not isinstance(target, str):
                    raise ValueError
                if source in live_ids and target in live_ids and source != target:
                    edge = (source, target) if source < target else (target, source)
                    edges.add(edge)
        snapshot = _CanonicalGraphSnapshot(
            _event_snapshot(connection, workspace_id, cancelled, deadline),
            tuple(records),
            tuple(sorted(edges)),
        )
        connection.rollback()
        return snapshot
    except GraphProjectionBuildError:
        connection.rollback()
        raise
    except (json.JSONDecodeError, sqlite3.Error, TypeError, ValueError, RecursionError):
        connection.rollback()
        raise GraphProjectionBuildError("PROJECTION_VALIDATION_FAILED") from None


def _entity_seeds(
    records: tuple[tuple[str, str], ...],
    extractor: EntityExtractor,
    cancelled: threading.Event | None,
    deadline: float,
) -> tuple[tuple[EntityProjectionSeed, ...], int, int]:
    indexed: dict[tuple[str, str], dict[str, Any]] = {}
    extracted = 0
    skipped = 0
    memberships = 0
    for record_id, content in records:
        _checkpoint(cancelled, deadline)
        found = extractor.extract_all(content)
        accepted_for_record = 0
        for item in found:
            name = item.get("name")
            entity_type = item.get("type")
            if not isinstance(name, str) or not isinstance(entity_type, str):
                raise GraphProjectionBuildError("PROJECTION_VALIDATION_FAILED")
            name = unicodedata.normalize("NFC", name)
            entity_type = unicodedata.normalize("NFC", entity_type)
            normalized = unicodedata.normalize("NFC", name.casefold())
            if (
                not name
                or name != name.strip()
                or len(name) > 256
                or len(normalized) > 256
                or _CONTROL_RE.search(name) is not None
                or not entity_type
                or len(entity_type) > 80
                or re.fullmatch(r"[a-z][a-z0-9_-]{0,79}", entity_type) is None
            ):
                skipped += 1
                continue
            try:
                name.encode("utf-8")
                entity_type.encode("utf-8")
            except UnicodeEncodeError:
                skipped += 1
                continue
            key = (entity_type, normalized)
            if key not in indexed and len(indexed) >= _MAX_ENTITIES:
                raise GraphProjectionBuildError("TASK_REQUIRED")
            memberships += 1
            if memberships > _MAX_ENTITY_MEMBERSHIPS:
                raise GraphProjectionBuildError("TASK_REQUIRED")
            entry = indexed.setdefault(key, {"name": name, "records": Counter()})
            entry["records"][record_id] += 1
            extracted += 1
            accepted_for_record += 1
        if accepted_for_record == 0:
            skipped += 1
    seeds = tuple(
        EntityProjectionSeed(
            name=str(value["name"]),
            entity_type=key[0],
            records=tuple(
                EntityRecordSeed(record_id, count)
                for record_id, count in sorted(value["records"].items())
            ),
        )
        for key, value in sorted(indexed.items())
    )
    return seeds, extracted, skipped


def _community_seeds(
    records: tuple[tuple[str, str], ...],
    edges: tuple[tuple[str, str], ...],
    entities: tuple[EntityProjectionSeed, ...],
    *,
    min_community_size: int,
    resolution: float,
    cancelled: threading.Event | None,
    deadline: float,
) -> tuple[tuple[CommunityProjectionSeed, ...], float]:
    _checkpoint(cancelled, deadline)
    try:
        from .graph.leiden import (
            LeidenConfig,
            LeidenExecutionError,
            run_leiden_bounded,
        )
    except ImportError:
        raise GraphProjectionBuildError("CAPABILITY_DEGRADED") from None
    try:
        result = run_leiden_bounded(
            tuple(record_id for record_id, _content in records),
            edges,
            LeidenConfig(resolution=resolution, seed=42, partition_type="modularity"),
            cancelled=cancelled,
            deadline=deadline,
        )
    except LeidenExecutionError as error:
        raise GraphProjectionBuildError(error.code) from None
    mapping = result.communities
    grouped: dict[int, list[str]] = defaultdict(list)
    for record_id, community in mapping.items():
        grouped[int(community)].append(str(record_id))
    selected = [
        tuple(sorted(members))
        for members in grouped.values()
        if len(members) >= min_community_size
    ]
    selected.sort()
    names_by_record: dict[str, Counter[str]] = defaultdict(Counter)
    for entity in entities:
        for record in entity.records:
            names_by_record[record.record_id][entity.name] += record.mention_count
    seeds: list[CommunityProjectionSeed] = []
    for ordinal, members in enumerate(selected, 1):
        _checkpoint(cancelled, deadline)
        names: Counter[str] = Counter()
        for record_id in members:
            names.update(names_by_record.get(record_id, ()))
        ranked = sorted(names.items(), key=lambda item: (-item[1], item[0].casefold()))
        label = ", ".join(name for name, _count in ranked[:3])
        if not label:
            label = f"Community {ordinal}"
        label = label[:256]
        source_key = "leiden:" + sha256_json(
            {
                "builder": _BUILDER_VERSION,
                "members": list(members),
                "min_community_size": min_community_size,
                "resolution": resolution,
            }
        )
        seeds.append(
            CommunityProjectionSeed(
                source_key=source_key,
                label=label,
                level=0,
                member_record_ids=members,
            )
        )
    modularity = result.modularity
    if not math.isfinite(modularity) or not -1.0 <= modularity <= 1.0:
        raise GraphProjectionBuildError("PROJECTION_VALIDATION_FAILED")
    return tuple(seeds), modularity


class GraphProjectionBuilder:
    """Build and atomically activate the complete graph discovery generation."""

    def __init__(
        self,
        connection: sqlite3.Connection,
        *,
        clock_us: Callable[[], int] | None = None,
        extractor_factory: Callable[[], EntityExtractor] = EntityExtractor,
        build_timeout_seconds: float = _BUILD_TIMEOUT_SECONDS,
    ) -> None:
        if not isinstance(connection, sqlite3.Connection):
            raise TypeError("connection must be sqlite3.Connection")
        if not callable(extractor_factory):
            raise TypeError("extractor_factory must be callable")
        if (
            isinstance(build_timeout_seconds, bool)
            or not isinstance(build_timeout_seconds, (int, float))
            or not math.isfinite(float(build_timeout_seconds))
            or float(build_timeout_seconds) <= 0
        ):
            raise TypeError("build_timeout_seconds must be positive and finite")
        self.connection = connection
        self._clock_us = clock_us or (lambda: time.time_ns() // 1_000)
        self._extractor_factory = extractor_factory
        self._build_timeout_seconds = float(build_timeout_seconds)

    def rebuild(
        self,
        workspace_id: str,
        *,
        min_community_size: int = 2,
        resolution: float = 1.0,
        force: bool = False,
        cancelled: threading.Event | None = None,
        before_publish: Callable[[], None] | None = None,
        publication_guard: Callable[[], AbstractContextManager[None]] | None = None,
        before_commit: Callable[[GraphProjectionBuildResult], None] | None = None,
    ) -> GraphProjectionBuildResult:
        if (
            isinstance(min_community_size, bool)
            or not isinstance(min_community_size, int)
            or not 2 <= min_community_size <= 1000
            or isinstance(resolution, bool)
            or not isinstance(resolution, (int, float))
            or not math.isfinite(float(resolution))
            or not 0 < float(resolution) <= 100
            or not isinstance(force, bool)
            or (publication_guard is not None and not callable(publication_guard))
            or (before_commit is not None and not callable(before_commit))
        ):
            raise GraphProjectionBuildError("INVALID_ARGUMENT")
        deadline = time.monotonic() + self._build_timeout_seconds
        now_us = self._clock_us()
        if isinstance(now_us, bool) or not isinstance(now_us, int):
            raise GraphProjectionBuildError("PROJECTION_BUILD_FAILED")
        _checkpoint(cancelled, deadline)
        snapshot = _canonical_snapshot(
            self.connection, workspace_id, now_us, cancelled, deadline
        )
        extractor = self._extractor_factory()
        entities, extracted, skipped = _entity_seeds(
            snapshot.records, extractor, cancelled, deadline
        )
        communities, modularity = _community_seeds(
            snapshot.records,
            snapshot.edges,
            entities,
            min_community_size=min_community_size,
            resolution=float(resolution),
            cancelled=cancelled,
            deadline=deadline,
        )
        _checkpoint(cancelled, deadline)
        discovery = DiscoveryProjectionBuilder(self.connection, clock_us=self._clock_us)
        specialized = SpecializedProjectionBuilder(
            self.connection, clock_us=self._clock_us
        )
        active = self.connection.execute(
            "SELECT generation FROM projection_manifests WHERE workspace_id=? "
            "AND projection_name='graph' AND status='active' LIMIT 2",
            (workspace_id,),
        ).fetchall()
        try:
            matches = (
                not force
                and len(active) == 1
                and specialized.active_is_current(workspace_id, "graph")
                and discovery.graph_matches(
                    workspace_id,
                    int(active[0][0]),
                    entities=entities,
                    communities=communities,
                )
            )
        except DiscoveryProjectionBuildError as error:
            code = (
                "TASK_REQUIRED"
                if error.code == "DISCOVERY_BUILD_TOO_LARGE"
                else "PROJECTION_VALIDATION_FAILED"
            )
            raise GraphProjectionBuildError(code) from None
        populated: DiscoveryProjectionBuildResult | None = None

        def populate(generation: int) -> None:
            nonlocal populated
            _checkpoint(cancelled, deadline)
            populated = discovery.populate_graph(
                workspace_id,
                entities=entities,
                communities=communities,
                generation=generation,
            )
            _checkpoint(cancelled, deadline)

        def activation_guard() -> None:
            _checkpoint(cancelled, deadline)
            if before_publish is not None:
                before_publish()
            _checkpoint(cancelled, deadline)

        def materialize_result(
            specialized_result: SpecializedProjectionBuildResult,
        ) -> GraphProjectionBuildResult:
            if populated is None:
                entity_ids = tuple(
                    str(row[0])
                    for row in self.connection.execute(
                        "SELECT entity_id FROM discovery_entities WHERE workspace_id=? "
                        "AND graph_generation=? ORDER BY identity_hash",
                        (workspace_id, specialized_result.generation),
                    )
                )
            else:
                entity_ids = populated.entity_ids
            community_map = {
                seed.source_key: derive_public_object_id(
                    workspace_id,
                    PublicObjectKind.COMMUNITY,
                    seed.source_key,
                    specialized_result.generation,
                )
                for seed in communities
            }
            community_ids = tuple(
                community_map[seed.source_key] for seed in communities
            )
            stored_community_ids = {
                str(row[0])
                for row in self.connection.execute(
                    "SELECT community_id FROM discovery_communities "
                    "WHERE workspace_id=? AND graph_generation=?",
                    (workspace_id, specialized_result.generation),
                )
            }
            if stored_community_ids != set(community_ids):
                raise GraphProjectionBuildError("PROJECTION_VALIDATION_FAILED")
            built_at = self.connection.execute(
                "SELECT activated_at_us FROM projection_manifests WHERE workspace_id=? "
                "AND projection_name='graph' AND generation=?",
                (workspace_id, specialized_result.generation),
            ).fetchone()
            if built_at is None or not isinstance(built_at[0], int):
                raise GraphProjectionBuildError("PROJECTION_ACTIVATION_FAILED")
            return GraphProjectionBuildResult(
                generation=specialized_result.generation,
                source_event_root_hash=specialized_result.source_event_root_hash,
                built_at_us=built_at[0],
                entities=entities,
                communities=communities,
                entity_ids=entity_ids,
                community_ids=community_ids,
                community_ids_by_source_key=MappingProxyType(community_map),
                scanned=len(snapshot.records),
                extracted=extracted,
                skipped=skipped,
                modularity=modularity,
                reused=specialized_result.reused,
            )

        def commit_callback(result: SpecializedProjectionBuildResult) -> None:
            _checkpoint(cancelled, deadline)
            if before_commit is not None:
                before_commit(materialize_result(result))
            _checkpoint(cancelled, deadline)

        try:
            guard = nullcontext() if publication_guard is None else publication_guard()
            with guard:
                specialized_result = specialized.rebuild(
                    workspace_id,
                    "graph",
                    force=force or not matches,
                    expected_snapshot=snapshot.event_snapshot,
                    candidate_populator=None if matches else populate,
                    before_activate=None if matches else activation_guard,
                    before_commit=(None if before_commit is None else commit_callback),
                )
        except GraphProjectionBuildError:
            raise
        except DiscoveryProjectionBuildError as error:
            code = (
                "TASK_REQUIRED"
                if error.code == "DISCOVERY_BUILD_TOO_LARGE"
                else "PROJECTION_VALIDATION_FAILED"
            )
            raise GraphProjectionBuildError(code) from None
        except SpecializedProjectionBuildError as error:
            raise GraphProjectionBuildError(error.code) from None
        return materialize_result(specialized_result)


__all__ = [
    "GraphProjectionBuildError",
    "GraphProjectionBuildResult",
    "GraphProjectionBuilder",
]
