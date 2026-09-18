"""Bounded, durable portable-workspace transfer support.

Format 2 separates a stable snapshot from its transport pages.  Export pages
are files produced from one SQLite backup (and, when requested, one completely
captured Qdrant generation) before a session is published.  Import pages are
only staging data until the caller validates every page and commits the
authoritative event replay.
"""

from __future__ import annotations

import hashlib
import hmac
import math
import os
import re
import secrets
import shutil
import sqlite3
import stat
import tempfile
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal, Protocol, cast

from ...event_store import (
    build_live_compatibility_claim_index,
    canonical_json_bytes,
    parse_canonical_json,
    sha256_json,
)
from ...retrieval.dense_generation_gc import (
    DEFAULT_READ_LEASE_US,
    DenseGenerationLifecycleError,
    DenseGenerationReadLease,
    acquire_active_generation_lease,
    enqueue_inactive_generation_gc,
    release_generation_lease,
    renew_generation_lease,
)
from ...retrieval.providers import (
    DENSE_BUILDER_VERSION,
    build_dense_point_payload,
    create_qdrant_client,
    dense_manifest_details,
    dense_query_encoder_matches_contract,
)
from ...retrieval.vector_validation import (
    VECTOR_ATTESTATION_FORMAT,
    cosine_vectors_match,
)

PAGE_BYTE_LIMIT = 850_000
MAX_PAGE_ITEMS = 4_096
MAX_SESSION_BYTES = 4 * 1024 * 1024 * 1024
MAX_LIVE_SESSIONS = 4
SESSION_TTL_US = 24 * 60 * 60 * 1_000_000
_ZERO_HASH = hashlib.sha256().hexdigest()
_EMPTY_MANIFEST_HASH = sha256_json({})
_VECTOR_SCHEMA_VERSION = 1
_VECTOR_PROVIDER_KEY = "qdrant"
_VECTOR_DISTANCE = "cosine"
_FINALIZATION_LEASE_US = 15 * 60 * 1_000_000
_BACKUP_PROGRESS_BYTES = 64 * 1024


def _portable_vector_attestations(
    encoder_contract: Mapping[str, object],
    *,
    model_id: str,
    dimension: int,
) -> tuple[str, str | None]:
    """Derive the dense vector-space identity from portable contract fields."""

    artifact_fingerprint = encoder_contract.get("artifact_fingerprint")
    if artifact_fingerprint is None:
        return VECTOR_ATTESTATION_FORMAT, None
    if (
        not isinstance(artifact_fingerprint, str)
        or len(artifact_fingerprint) != 64
        or set(artifact_fingerprint) - set("0123456789abcdef")
    ):
        raise ValueError("encoder artifact fingerprint is invalid")
    return (
        VECTOR_ATTESTATION_FORMAT,
        sha256_json(
            {
                "builder_version": DENSE_BUILDER_VERSION,
                "distance": _VECTOR_DISTANCE,
                "encoder_contract": dict(encoder_contract),
                "format": VECTOR_ATTESTATION_FORMAT,
                "model_id": model_id,
                "output_dimension": dimension,
                "provider_representation": "qdrant-cosine",
            }
        ),
    )


class _QdrantTransferClient(Protocol):
    """The bounded provider surface used by portable vector transfer."""

    def scroll(self, **kwargs: object) -> object: ...

    def create_collection(self, **kwargs: object) -> object: ...

    def upsert(self, **kwargs: object) -> object: ...

    def count(self, **kwargs: object) -> object: ...

    def delete_collection(self, collection_name: str) -> object: ...

    def collection_exists(self, collection_name: str) -> bool: ...


def _qdrant_transfer_client(config: object) -> _QdrantTransferClient:
    """Construct and structurally validate the portable-transfer client."""

    try:
        client = create_qdrant_client(
            qdrant_url=getattr(config, "qdrant_url", None),
            qdrant_api_key=getattr(config, "qdrant_api_key", None),
            qdrant_path=getattr(config, "qdrant_path", None),
            timeout_seconds=getattr(config, "qdrant_timeout_seconds", 10.0),
        )
    except Exception as exc:
        raise PortableTransferError("CAPABILITY_DEGRADED") from exc
    required = (
        "scroll",
        "create_collection",
        "upsert",
        "count",
        "delete_collection",
        "collection_exists",
    )
    if any(not callable(getattr(client, name, None)) for name in required):
        close = getattr(client, "close", None)
        if callable(close):
            close()
        raise PortableTransferError("CAPABILITY_DEGRADED")
    return cast(_QdrantTransferClient, client)


def _scroll_client(
    client: _QdrantTransferClient, **kwargs: object
) -> tuple[Sequence[object], object | None]:
    response = client.scroll(**kwargs)
    if not isinstance(response, Sequence) or len(response) != 2:
        raise PortableTransferError("CAPABILITY_DEGRADED")
    points, next_offset = response
    if isinstance(points, (str, bytes, Mapping)) or not isinstance(points, Sequence):
        raise PortableTransferError("CAPABILITY_DEGRADED")
    return cast(Sequence[object], points), next_offset


class PortableTransferError(RuntimeError):
    """A stable, sanitized transfer-layer failure."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def _now_us(value: datetime) -> int:
    if value.tzinfo is None:
        raise ValueError("clock must return an aware datetime")
    return int(value.astimezone(timezone.utc).timestamp() * 1_000_000)


def _root(storage: Path) -> Path:
    root = storage / "portable" / "v2"
    root.mkdir(parents=True, exist_ok=True)
    resolved = root.resolve()
    resolved.relative_to(storage.resolve())
    return resolved


def _session_dir(storage: Path, direction: str, session_id: str) -> Path:
    if direction not in {"export", "import"}:
        raise PortableTransferError("IMPORT_INVALID")
    prefix = "xpt_" if direction == "export" else "ipt_"
    if (
        not isinstance(session_id, str)
        or len(session_id) != 68
        or not session_id.startswith(prefix)
        or set(session_id[4:]) - set("0123456789abcdef")
    ):
        raise PortableTransferError("IMPORT_INVALID")
    result = (
        _root(storage)
        / ("exports" if direction == "export" else "imports")
        / session_id
    )
    result.resolve().relative_to(_root(storage))
    return result


def _is_link_or_reparse(path: Path) -> bool:
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return False
    return path.is_symlink() or bool(
        getattr(metadata, "st_file_attributes", 0)
        & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    )


def _remove_attempt_directory(
    storage: Path, session_id: str, attempt_relative_path: str
) -> None:
    """Remove only one validated owner path, rejecting links and reparses."""

    if re.fullmatch(r"attempts/[0-9a-f]{64}", attempt_relative_path) is None:
        raise PortableTransferError("IMPORT_INVALID")
    directory = _session_dir(storage, "import", session_id)
    attempt = directory / Path(attempt_relative_path)
    if not attempt.exists():
        return
    if (
        _is_link_or_reparse(attempt)
        or attempt.resolve().parent != (directory / "attempts").resolve()
    ):
        raise PortableTransferError("IMPORT_INVALID")
    if any(_is_link_or_reparse(path) for path in attempt.rglob("*")):
        raise PortableTransferError("IMPORT_INVALID")
    shutil.rmtree(attempt)
    if attempt.exists():
        raise PortableTransferError("TASK_REQUIRED")


def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=".partial-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except Exception:
        with suppress(OSError):
            os.unlink(temporary)
        raise


def _read_canonical(path: Path, expected_hash: str) -> dict[str, Any]:
    try:
        data = path.read_bytes()
        value = parse_canonical_json(data.decode("utf-8"))
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        raise PortableTransferError("IMPORT_INVALID") from exc
    if not isinstance(value, dict) or hashlib.sha256(data).hexdigest() != expected_hash:
        raise PortableTransferError("IMPORT_INVALID")
    return value


def _clean_expired(connection: sqlite3.Connection, storage: Path, now_us: int) -> None:
    rows = connection.execute(
        "SELECT session_id,direction FROM portable_transfer_sessions "
        "WHERE expires_at_us<=? AND status<>'expired'",
        (now_us,),
    ).fetchall()
    for row in rows:
        connection.execute(
            "UPDATE portable_transfer_sessions SET status='expired',updated_at_us=? "
            "WHERE session_id=?",
            (now_us, str(row[0])),
        )
        shutil.rmtree(
            _session_dir(storage, str(row[1]), str(row[0])),
            ignore_errors=True,
        )


def _directory_size(path: Path) -> int:
    total = 0
    if not path.exists():
        return 0
    for candidate in path.rglob("*"):
        if candidate.is_file():
            total += candidate.stat().st_size
    return total


def _sqlite_logical_size(connection: sqlite3.Connection) -> tuple[int, int]:
    """Return backup bytes and page size using SQLite's own page inventory."""

    page_count = int(connection.execute("PRAGMA page_count").fetchone()[0])
    page_size = int(connection.execute("PRAGMA page_size").fetchone()[0])
    if page_count < 0 or not 512 <= page_size <= 65_536:
        raise PortableTransferError("IMPORT_INVALID")
    return page_count * page_size, page_size


def _reconcile_storage(
    connection: sqlite3.Connection, storage: Path, now_us: int
) -> None:
    """Remove unowned files and fail live rows whose retained files vanished."""

    _clean_expired(connection, storage, now_us)
    known = {
        (str(row[0]), str(row[1]))
        for row in connection.execute(
            "SELECT session_id,direction FROM portable_transfer_sessions "
            "WHERE status NOT IN ('expired','failed') AND expires_at_us>?",
            (now_us,),
        ).fetchall()
    }
    root = _root(storage)
    for direction, dirname in (("export", "exports"), ("import", "imports")):
        parent = root / dirname
        if parent.exists():
            for candidate in parent.iterdir():
                if candidate.is_dir() and (candidate.name, direction) not in known:
                    shutil.rmtree(candidate, ignore_errors=True)
    for session_id, direction in known:
        directory = _session_dir(storage, direction, session_id)
        row = connection.execute(
            "SELECT status,updated_at_us FROM portable_transfer_sessions WHERE session_id=?",
            (session_id,),
        ).fetchone()
        if (
            row is not None
            and row[0] == "building"
            and now_us - int(row[1]) > _FINALIZATION_LEASE_US
        ):
            shutil.rmtree(directory, ignore_errors=True)
            connection.execute(
                "UPDATE portable_transfer_sessions SET status='failed',"
                "total_bytes=0,updated_at_us=? WHERE session_id=?",
                (now_us, session_id),
            )
        elif row is not None and row[0] != "building" and not directory.exists():
            connection.execute(
                "UPDATE portable_transfer_sessions SET status='failed',"
                "total_bytes=0,updated_at_us=? WHERE session_id=?",
                (now_us, session_id),
            )
        if direction == "import" and directory.exists():
            lease = connection.execute(
                "SELECT attempt_relative_path FROM "
                "portable_transfer_finalization_leases "
                "WHERE session_id=? AND expires_at_us>?",
                (session_id, now_us),
            ).fetchone()
            active_attempt = None if lease is None else str(lease[0])
            attempts = directory / "attempts"
            if attempts.exists():
                for candidate in attempts.iterdir():
                    relative = f"attempts/{candidate.name}"
                    if candidate.is_dir() and relative != active_attempt:
                        shutil.rmtree(candidate, ignore_errors=True)
            if active_attempt is None:
                _account_physical_files(
                    connection,
                    storage,
                    str(
                        connection.execute(
                            "SELECT workspace_id FROM portable_transfer_sessions "
                            "WHERE session_id=?",
                            (session_id,),
                        ).fetchone()[0]
                    ),
                    "import",
                    session_id,
                    now_us,
                )


def _retained_bytes(
    connection: sqlite3.Connection,
    workspace_id: str,
    now_us: int,
    *,
    exclude: str | None = None,
) -> int:
    sql = (
        "SELECT COALESCE(SUM(total_bytes),0) FROM portable_transfer_sessions "
        "WHERE workspace_id=? AND status NOT IN ('expired','failed') "
        "AND expires_at_us>?"
    )
    parameters: list[object] = [workspace_id, now_us]
    if exclude is not None:
        sql += " AND session_id<>?"
        parameters.append(exclude)
    return int(connection.execute(sql, parameters).fetchone()[0])


def _account_physical_files(
    connection: sqlite3.Connection,
    storage: Path,
    workspace_id: str,
    direction: str,
    session_id: str,
    now_us: int,
) -> int:
    physical = _directory_size(_session_dir(storage, direction, session_id))
    if (
        physical > MAX_SESSION_BYTES
        or _retained_bytes(connection, workspace_id, now_us, exclude=session_id)
        + physical
        > MAX_SESSION_BYTES
    ):
        raise PortableTransferError("TASK_REQUIRED")
    connection.execute(
        "UPDATE portable_transfer_sessions SET total_bytes=?,updated_at_us=? "
        "WHERE session_id=?",
        (physical, now_us, session_id),
    )
    return physical


def _ensure_physical_capacity(
    connection: sqlite3.Connection,
    storage: Path,
    workspace_id: str,
    direction: str,
    session_id: str,
    now_us: int,
    *,
    additional_bytes: int = 0,
) -> int:
    """Return remaining bytes after rejecting a write before it is attempted."""

    if additional_bytes < 0:
        raise PortableTransferError("IMPORT_INVALID")
    physical = _directory_size(_session_dir(storage, direction, session_id))
    retained = _retained_bytes(connection, workspace_id, now_us, exclude=session_id)
    wanted = physical + additional_bytes
    if wanted > MAX_SESSION_BYTES or retained + wanted > MAX_SESSION_BYTES:
        raise PortableTransferError("TASK_REQUIRED")
    return min(MAX_SESSION_BYTES - wanted, MAX_SESSION_BYTES - retained - wanted)


def _page_descriptor(page: Mapping[str, Any]) -> dict[str, Any]:
    return {
        key: page[key]
        for key in ("page_index", "page_kind", "item_count", "byte_count", "page_hash")
    }


def _merkle_levels(pages: Sequence[Mapping[str, Any]]) -> list[list[str]]:
    if not pages:
        raise PortableTransferError("IMPORT_INVALID")
    levels = [[sha256_json(_page_descriptor(page)) for page in pages]]
    while len(levels[-1]) > 1:
        current = levels[-1]
        levels.append(
            [
                hashlib.sha256(
                    bytes.fromhex(current[index])
                    + bytes.fromhex(current[min(index + 1, len(current) - 1)])
                ).hexdigest()
                for index in range(0, len(current), 2)
            ]
        )
    return levels


def _merkle_proof(levels: Sequence[Sequence[str]], index: int) -> list[str]:
    proof: list[str] = []
    cursor = index
    for level in levels[:-1]:
        sibling = cursor - 1 if cursor % 2 else min(cursor + 1, len(level) - 1)
        proof.append(level[sibling])
        cursor //= 2
    return proof


def _verify_page_proof(
    descriptor: Mapping[str, Any], proof: Sequence[object], root: object
) -> bool:
    if not isinstance(root, str) or len(proof) > 32:
        return False
    digest = sha256_json(dict(descriptor))
    index = descriptor.get("page_index")
    if isinstance(index, bool) or not isinstance(index, int) or index < 0:
        return False
    for sibling in proof:
        if (
            not isinstance(sibling, str)
            or len(sibling) != 64
            or set(sibling) - set("0123456789abcdef")
        ):
            return False
        pair = (
            bytes.fromhex(sibling) + bytes.fromhex(digest)
            if index % 2
            else bytes.fromhex(digest) + bytes.fromhex(sibling)
        )
        digest = hashlib.sha256(pair).hexdigest()
        index //= 2
    return hmac.compare_digest(digest, root)


def _cursor(
    secret: bytes,
    session_id: str,
    page_index: int,
    expires_at_us: int,
    manifest_hash: str,
) -> str:
    message = f"{session_id}:{page_index}:{expires_at_us}:{manifest_hash}".encode()
    return "cur_" + hmac.new(secret, message, hashlib.sha256).hexdigest()


def _event_row(row: sqlite3.Row) -> dict[str, Any]:
    try:
        payload = parse_canonical_json(str(row[13]))
    except Exception as exc:
        raise PortableTransferError("IMPORT_INVALID") from exc
    return {
        "event_id": str(row[0]),
        "workspace_id": str(row[1]),
        "stream_id": str(row[2]),
        "stream_kind": str(row[3]),
        "stream_version": int(row[4]),
        "event_type": str(row[5]),
        "event_schema_version": int(row[6]),
        "occurred_at_us": int(row[7]),
        "recorded_at_us": int(row[8]),
        "actor_type": str(row[9]),
        "actor_id": row[10],
        "causation_event_id": row[11],
        "correlation_id": row[12],
        "payload": payload,
        "payload_hash": str(row[14]),
        "previous_event_hash": row[15],
        "event_hash": str(row[16]),
    }


_EVENT_SELECT = (
    "event_id,workspace_id,stream_id,stream_kind,stream_version,event_type,"
    "event_schema_version,occurred_at_us,recorded_at_us,actor_type,actor_id,"
    "causation_event_id,correlation_id,payload_json,payload_hash,"
    "previous_event_hash,event_hash"
)


def _legacy_rows(
    connection: sqlite3.Connection, workspace_id: str
) -> Iterator[dict[str, Any]]:
    claims = build_live_compatibility_claim_index(
        connection, workspace_id, {"memories"}
    )
    legacy_by_record = {
        record_id: legacy_id
        for (source_table, legacy_id), record_id in claims.items()
        if source_table == "memories"
    }
    cursor = connection.execute(
        "SELECT record_id,record_type,legacy_type,content,content_hash,rationale,"
        "context_json,tags_json,file_path_relative,keywords,is_permanent,pinned,"
        "archived,outcome,worked,recall_count,surprise_score,importance_score,"
        "source_client,source_model,stream_version,source_event_id,created_at_us,"
        "updated_at_us,deleted_at_us,state_hash FROM memory_records "
        "WHERE workspace_id=? ORDER BY record_id",
        (workspace_id,),
    )
    for row in cursor:
        try:
            context = parse_canonical_json(str(row[6]))
            tags = parse_canonical_json(str(row[7]))
        except Exception as exc:
            raise PortableTransferError("IMPORT_INVALID") from exc
        yield {
            "record_id": str(row[0]),
            "legacy_id": legacy_by_record.get(str(row[0])),
            "record": {
                "record_type": str(row[1]),
                "legacy_type": row[2],
                "content": str(row[3]),
                "content_hash": str(row[4]),
                "rationale": row[5],
                "context": context,
                "tags": tags,
                "file_path_relative": row[8],
                "keywords": row[9],
                "is_permanent": bool(row[10]),
                "pinned": bool(row[11]),
                "archived": bool(row[12]),
                "outcome": row[13],
                "worked": None if row[14] is None else bool(row[14]),
                "recall_count": int(row[15]),
                "surprise_score": row[16],
                "importance_score": row[17],
                "source_client": row[18],
                "source_model": row[19],
                "stream_version": int(row[20]),
                "source_event_id": str(row[21]),
                "created_at_us": int(row[22]),
                "updated_at_us": int(row[23]),
                "deleted_at_us": row[24],
                "state_hash": str(row[25]),
            },
        }


def _field(value: object, name: str) -> object:
    if isinstance(value, Mapping):
        return value.get(name)
    return getattr(value, name, None)


def _vector_snapshot(
    connection: sqlite3.Connection,
    workspace_id: str,
    config: object,
    checkpoint: Callable[[], None] | None = None,
    *,
    expected_lease: DenseGenerationReadLease | None = None,
    lease_checkpoint: Callable[[], None] | None = None,
) -> tuple[dict[str, Any], Iterator[dict[str, Any]], Callable[[], None]]:
    row = connection.execute(
        "SELECT generation,source_event_count,source_event_root_hash,row_count,"
        "details_json,projection_version,builder_version,manifest_id FROM projection_manifests WHERE workspace_id=? "
        "AND projection_name='dense' AND status='active' "
        "ORDER BY generation DESC LIMIT 1",
        (workspace_id,),
    ).fetchone()
    if row is None:
        raise PortableTransferError("CAPABILITY_DEGRADED")
    if expected_lease is not None and (
        int(row[0]) != expected_lease.generation
        or str(row[7]) != expected_lease.manifest_id
    ):
        raise PortableTransferError("CAPABILITY_DEGRADED")
    try:
        details = parse_canonical_json(str(row[4]))
    except Exception as exc:
        raise PortableTransferError("CAPABILITY_DEGRADED") from exc
    if not isinstance(details, Mapping):
        raise PortableTransferError("CAPABILITY_DEGRADED")
    required = {
        "collection_name",
        "collection_prefix",
        "provider_key",
        "model_id",
        "dimension",
        "distance",
        "schema_version",
        "build_config_hash",
        "builder_contract_hash",
        "encoder_contract",
    }
    if not required <= set(details):
        raise PortableTransferError("CAPABILITY_DEGRADED")
    try:
        generation = int(row[0])
        dimension = int(details["dimension"])
        storage_contract = dense_manifest_details(
            workspace_id=workspace_id,
            provider_key=str(details["provider_key"]),
            generation=generation,
            model_id=str(details["model_id"]),
            dimension=dimension,
            collection_prefix=str(details["collection_prefix"]),
        )
        encoder_contract = details["encoder_contract"]
        if not isinstance(encoder_contract, Mapping):
            raise ValueError("encoder contract is invalid")
        vector_format, vector_space_hash = _portable_vector_attestations(
            encoder_contract,
            model_id=str(details["model_id"]),
            dimension=dimension,
        )
        contract_hash = sha256_json(
            {
                "build_config_hash": details["build_config_hash"],
                "builder_version": str(row[6]),
                "encoder_contract": dict(encoder_contract),
                "projection": "dense",
            }
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise PortableTransferError("CAPABILITY_DEGRADED") from exc
    if (
        str(details["provider_key"]) != _VECTOR_PROVIDER_KEY
        or details["schema_version"] != _VECTOR_SCHEMA_VERSION
        or details["distance"] != _VECTOR_DISTANCE
        or int(row[5]) != 1
        or str(row[6]) != DENSE_BUILDER_VERSION
        or any(details.get(key) != value for key, value in storage_contract.items())
        or details.get("builder_contract_hash") != contract_hash
        or (
            ("vector_format" in details or "vector_space_hash" in details)
            and (
                details.get("vector_format") != vector_format
                or details.get("vector_space_hash") != vector_space_hash
            )
        )
    ):
        raise PortableTransferError("CAPABILITY_DEGRADED")
    client = _qdrant_transfer_client(config)
    close = getattr(client, "close", None)

    def close_client() -> None:
        if callable(close):
            close()

    collection = str(details["collection_name"])

    def points() -> Iterator[dict[str, Any]]:
        offset: object | None = None
        seen_offsets: set[str] = set()
        while True:
            if lease_checkpoint is not None:
                lease_checkpoint()
            if checkpoint is not None:
                checkpoint()
            batch, next_offset = _scroll_client(
                client,
                collection_name=collection,
                limit=256,
                offset=offset,
                with_payload=True,
                with_vectors=True,
            )
            if checkpoint is not None:
                checkpoint()
            if lease_checkpoint is not None:
                lease_checkpoint()
            for point in batch:
                if checkpoint is not None:
                    checkpoint()
                point_id = _field(point, "id")
                payload = _field(point, "payload")
                vector = _field(point, "vector")
                if (
                    not isinstance(point_id, (str, int))
                    or not isinstance(payload, Mapping)
                    or not isinstance(vector, Sequence)
                    or isinstance(vector, (str, bytes, Mapping))
                ):
                    raise PortableTransferError("CAPABILITY_DEGRADED")
                values = [float(item) for item in vector]
                if len(values) != dimension or not all(
                    math.isfinite(v) for v in values
                ):
                    raise PortableTransferError("CAPABILITY_DEGRADED")
                if (
                    payload.get("workspace_id") != workspace_id
                    or payload.get("projection_generation") != generation
                    or payload.get("model_id") != details["model_id"]
                ):
                    raise PortableTransferError("CAPABILITY_DEGRADED")
                reference = connection.execute(
                    "SELECT content_hash,model_id,dimension,state "
                    "FROM dense_projection_refs WHERE workspace_id=? "
                    "AND provider_key=? AND projection_generation=? "
                    "AND record_id=?",
                    (
                        workspace_id,
                        str(details["provider_key"]),
                        generation,
                        payload.get("record_id"),
                    ),
                ).fetchone()
                if reference is None or tuple(reference) != (
                    payload.get("content_hash"),
                    details["model_id"],
                    dimension,
                    "ready",
                ):
                    raise PortableTransferError("CAPABILITY_DEGRADED")
                yield {
                    "point_id": str(point_id),
                    "payload": dict(payload),
                    "vector": values,
                }
            if next_offset is None:
                return
            marker = repr(next_offset)
            if marker in seen_offsets:
                raise PortableTransferError("CAPABILITY_DEGRADED")
            seen_offsets.add(marker)
            offset = next_offset

    metadata = {
        "schema_version": _VECTOR_SCHEMA_VERSION,
        "provider_key": str(details["provider_key"]),
        "model_id": str(details["model_id"]),
        "dimension": dimension,
        "distance": str(details["distance"]),
        "projection_version": int(row[5]),
        "builder_version": str(row[6]),
        "build_config_hash": str(details["build_config_hash"]),
        "builder_contract_hash": str(details["builder_contract_hash"]),
        "encoder_contract": dict(encoder_contract),
        "vector_format": vector_format,
        "vector_space_hash": vector_space_hash,
        "generation": generation,
        "collection_name": collection,
        "source_event_count": int(row[1]),
        "source_event_root_hash": str(row[2]),
        "row_count": int(row[3]),
    }
    return metadata, points(), close_client


def _write_item_pages(
    directory: Path,
    kind: Literal["events", "legacy", "vectors"],
    items: Iterator[dict[str, Any]],
    start_index: int,
    page_byte_limit: int,
    *,
    ensure_page: bool = False,
    checkpoint: Callable[[], None] | None = None,
) -> tuple[list[dict[str, Any]], str, int, int]:
    page_rows: list[dict[str, Any]] = []
    page_bytes = 0
    pages: list[dict[str, Any]] = []
    section_digest = hashlib.sha256()
    count = 0
    total_bytes = 0

    def flush() -> None:
        nonlocal page_rows, page_bytes, total_bytes
        if not page_rows:
            return
        content = {"kind": kind, "items": page_rows}
        encoded = canonical_json_bytes(content)
        if len(encoded) > page_byte_limit:
            raise PortableTransferError("TASK_REQUIRED")
        page_hash = hashlib.sha256(encoded).hexdigest()
        index = start_index + len(pages)
        relative = f"pages/{index:08d}.json"
        _atomic_write(directory / relative, encoded)
        if checkpoint is not None:
            checkpoint()
        pages.append(
            {
                "page_index": index,
                "page_kind": kind,
                "item_count": len(page_rows),
                "byte_count": len(encoded),
                "page_hash": page_hash,
                "relative_path": relative,
            }
        )
        total_bytes += len(encoded)
        page_rows = []
        page_bytes = 0

    for item in items:
        if checkpoint is not None:
            checkpoint()
        encoded_item = canonical_json_bytes(item)
        if len(encoded_item) + 64 > page_byte_limit:
            raise PortableTransferError("TASK_REQUIRED")
        if page_rows and (
            len(page_rows) >= MAX_PAGE_ITEMS
            or page_bytes + len(encoded_item) + 64 > page_byte_limit
        ):
            flush()
        page_rows.append(item)
        page_bytes += len(encoded_item) + 1
        section_digest.update(encoded_item)
        count += 1
    flush()
    if ensure_page and not pages:
        content: dict[str, object] = {"kind": kind, "items": []}
        encoded = canonical_json_bytes(content)
        page_hash = hashlib.sha256(encoded).hexdigest()
        relative = f"pages/{start_index:08d}.json"
        _atomic_write(directory / relative, encoded)
        if checkpoint is not None:
            checkpoint()
        pages.append(
            {
                "page_index": start_index,
                "page_kind": kind,
                "item_count": 0,
                "byte_count": len(encoded),
                "page_hash": page_hash,
                "relative_path": relative,
            }
        )
        total_bytes += len(encoded)
    return pages, section_digest.hexdigest(), count, total_bytes


def create_export_session(
    connection: sqlite3.Connection,
    storage: Path,
    workspace_id: str,
    *,
    include_legacy_projection: bool,
    include_vectors: bool,
    config: object | None,
    clock: datetime,
    cursor_secret: bytes,
    public_event: Callable[[Mapping[str, Any]], Mapping[str, Any]],
    page_byte_limit: int = PAGE_BYTE_LIMIT,
    local_capability_ready: bool = False,
    cancelled: Callable[[], bool] | None = None,
) -> dict[str, Any]:
    """Freeze, page, checksum, and publish one format-2 export session."""

    now_us = _now_us(clock)
    # Validate unsafe event payloads before optional vector capability checks.
    # That preserves the path-security error without serializing raw paths.
    for row in connection.execute(
        f"SELECT {_EVENT_SELECT} FROM memory_events WHERE workspace_id=? "
        "ORDER BY event_id",
        (workspace_id,),
    ):
        public_event(_event_row(row))
    if include_vectors and (config is None or not local_capability_ready):
        raise PortableTransferError("CAPABILITY_DEGRADED")
    if (
        isinstance(page_byte_limit, bool)
        or not isinstance(page_byte_limit, int)
        or not 65_536 <= page_byte_limit <= PAGE_BYTE_LIMIT
    ):
        raise PortableTransferError("IMPORT_INVALID")
    session_id = "xpt_" + secrets.token_hex(32)
    directory = _session_dir(storage, "export", session_id)
    expires_at_us = now_us + SESSION_TTL_US
    connection.execute("BEGIN IMMEDIATE")
    try:
        _reconcile_storage(connection, storage, now_us)
        live = connection.execute(
            "SELECT COUNT(*) FROM portable_transfer_sessions WHERE workspace_id=? "
            "AND direction='export' AND status NOT IN ('expired','failed') "
            "AND expires_at_us>?",
            (workspace_id, now_us),
        ).fetchone()
        retained = _retained_bytes(connection, workspace_id, now_us)
        if int(live[0]) >= MAX_LIVE_SESSIONS or retained >= MAX_SESSION_BYTES:
            raise PortableTransferError("TASK_REQUIRED")
        # Reserving the remaining physical allowance serializes builders while
        # permitting up to four already-published resumable sessions.
        reservation = MAX_SESSION_BYTES - retained
        connection.execute(
            "INSERT INTO portable_transfer_sessions(session_id,workspace_id,"
            "direction,status,event_root_hash,manifest_json,manifest_hash,page_count,"
            "staged_page_count,total_bytes,created_at_us,updated_at_us,expires_at_us,"
            "completed_at_us) VALUES (?,?,'export','building',?, '{}',?,0,0,?,?,?,?,NULL)",
            (
                session_id,
                workspace_id,
                _ZERO_HASH,
                _EMPTY_MANIFEST_HASH,
                reservation,
                now_us,
                now_us,
                expires_at_us,
            ),
        )
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    snapshot_path = directory / "source.snapshot.db"
    snapshot: sqlite3.Connection | None = None
    lease_connection: sqlite3.Connection | None = None
    vector_lease: DenseGenerationReadLease | None = None
    vector_renew_at_us = 0

    def close_vector() -> None:
        return None

    def renew_vector_lease(*, force: bool = False) -> None:
        nonlocal vector_lease, vector_renew_at_us
        if vector_lease is None:
            return
        if lease_connection is None:
            raise PortableTransferError("CAPABILITY_DEGRADED")
        current_us = time.time_ns() // 1_000
        if not force and current_us < vector_renew_at_us:
            return
        try:
            vector_lease = renew_generation_lease(lease_connection, vector_lease)
        except DenseGenerationLifecycleError as exc:
            raise PortableTransferError("CAPABILITY_DEGRADED") from exc
        vector_renew_at_us = vector_lease.expires_at_us - DEFAULT_READ_LEASE_US // 3

    def checkpoint() -> None:
        if cancelled is not None and cancelled():
            raise PortableTransferError("CANCELLED")
        renew_vector_lease()
        if _directory_size(directory) > reservation:
            raise PortableTransferError("TASK_REQUIRED")

    try:
        if include_vectors:
            try:
                database_row = next(
                    (
                        row
                        for row in connection.execute("PRAGMA database_list")
                        if str(row[1]) == "main"
                    ),
                    None,
                )
                if database_row is None or not str(database_row[2]):
                    raise DenseGenerationLifecycleError(
                        "DENSE_GENERATION_STORAGE_UNAVAILABLE"
                    )
                lease_connection = sqlite3.connect(str(database_row[2]), timeout=5.0)
                lease_connection.execute("PRAGMA foreign_keys=ON")
                vector_lease = acquire_active_generation_lease(
                    lease_connection,
                    workspace_id=workspace_id,
                    provider_key=_VECTOR_PROVIDER_KEY,
                    owner_id=session_id,
                )
            except DenseGenerationLifecycleError as exc:
                raise PortableTransferError("CAPABILITY_DEGRADED") from exc
            vector_renew_at_us = vector_lease.expires_at_us - DEFAULT_READ_LEASE_US // 3
        checkpoint()
        source_bytes, page_size = _sqlite_logical_size(connection)
        # Keep one source page in reserve so a first progress callback can
        # abort a concurrently grown snapshot without crossing the allowance.
        if source_bytes > max(0, reservation - page_size):
            raise PortableTransferError("TASK_REQUIRED")
        directory.mkdir(parents=True, exist_ok=False)
        snapshot = sqlite3.connect(snapshot_path)
        snapshot.row_factory = sqlite3.Row

        def backup_progress(_status: int, _remaining: int, total: int) -> None:
            checkpoint()
            if total * page_size > max(0, reservation - page_size):
                raise PortableTransferError("TASK_REQUIRED")

        connection.backup(
            snapshot,
            pages=max(1, _BACKUP_PROGRESS_BYTES // page_size),
            progress=backup_progress,
            sleep=0.0,
        )
        renew_vector_lease(force=True)
        checkpoint()
        snapshot.execute("PRAGMA query_only=ON")
        events_cursor = snapshot.execute(
            f"SELECT {_EVENT_SELECT} FROM memory_events WHERE workspace_id=? "
            "ORDER BY event_id",
            (workspace_id,),
        )
        event_hash_digest = hashlib.sha256()

        def events() -> Iterator[dict[str, Any]]:
            for row in events_cursor:
                internal = _event_row(row)
                event_hash_digest.update(bytes.fromhex(internal["event_hash"]))
                yield dict(public_event(internal))

        pages, _events_payload_hash, event_count, total_bytes = _write_item_pages(
            directory,
            "events",
            events(),
            0,
            page_byte_limit,
            ensure_page=True,
            checkpoint=checkpoint,
        )
        event_root = event_hash_digest.hexdigest()
        legacy_meta: dict[str, Any] | None = None
        if include_legacy_projection:
            legacy_pages, legacy_hash, legacy_count, legacy_bytes = _write_item_pages(
                directory,
                "legacy",
                _legacy_rows(snapshot, workspace_id),
                len(pages),
                page_byte_limit,
                ensure_page=True,
                checkpoint=checkpoint,
            )
            pages.extend(legacy_pages)
            total_bytes += legacy_bytes
            legacy_meta = {
                "schema_version": 1,
                "row_count": legacy_count,
                "payload_hash": legacy_hash,
                "source_event_root_hash": event_root,
            }
        vector_meta: dict[str, Any] | None = None
        if include_vectors:
            assert config is not None
            renew_vector_lease(force=True)
            vector_meta, vector_items, close_vector = _vector_snapshot(
                snapshot,
                workspace_id,
                config,
                checkpoint,
                expected_lease=vector_lease,
                lease_checkpoint=lambda: renew_vector_lease(force=True),
            )
            vector_pages, vector_hash, vector_count, vector_bytes = _write_item_pages(
                directory,
                "vectors",
                vector_items,
                len(pages),
                page_byte_limit,
                ensure_page=True,
                checkpoint=checkpoint,
            )
            pages.extend(vector_pages)
            total_bytes += vector_bytes
            if vector_count != vector_meta["row_count"]:
                raise PortableTransferError("CAPABILITY_DEGRADED")
            vector_meta = {
                **vector_meta,
                "row_count": vector_count,
                "payload_hash": vector_hash,
            }
            if vector_meta["source_event_root_hash"] != event_root:
                raise PortableTransferError("CAPABILITY_DEGRADED")
        levels = _merkle_levels(pages)
        for page in pages:
            proof = canonical_json_bytes(
                {"proof": _merkle_proof(levels, int(page["page_index"]))}
            )
            _atomic_write(
                directory / f"proofs/{int(page['page_index']):08d}.json", proof
            )
            checkpoint()
        manifest = {
            "bundle_version": 2,
            "workspace_id": workspace_id,
            "event_root_hash": event_root,
            "event_count": event_count,
            "legacy_projection": legacy_meta,
            "vectors": vector_meta,
            "page_count": len(pages),
            "page_table_root": levels[-1][0],
        }
        manifest_hash = sha256_json(manifest)
        manifest_text = canonical_json_bytes(manifest).decode("utf-8")
        snapshot.close()
        snapshot = None
        snapshot_path.unlink(missing_ok=True)
        physical_bytes = _directory_size(directory)
        if physical_bytes > reservation:
            raise PortableTransferError("TASK_REQUIRED")
        checkpoint()
        connection.execute("BEGIN IMMEDIATE")
        updated = connection.execute(
            "UPDATE portable_transfer_sessions SET status='ready',event_root_hash=?,"
            "manifest_json=?,manifest_hash=?,page_count=?,staged_page_count=?,"
            "total_bytes=?,updated_at_us=? WHERE session_id=? AND status='building'",
            (
                event_root,
                manifest_text,
                manifest_hash,
                len(pages),
                len(pages),
                physical_bytes,
                now_us,
                session_id,
            ),
        )
        if updated.rowcount != 1:
            raise PortableTransferError("IMPORT_INVALID")
        connection.executemany(
            "INSERT INTO portable_transfer_pages(session_id,page_index,page_kind,"
            "item_count,byte_count,page_hash,relative_path,received_at_us) "
            "VALUES (?,?,?,?,?,?,?,?)",
            [
                (
                    session_id,
                    page["page_index"],
                    page["page_kind"],
                    page["item_count"],
                    page["byte_count"],
                    page["page_hash"],
                    page["relative_path"],
                    now_us,
                )
                for page in pages
            ],
        )
        connection.commit()
        return read_export_page(
            connection,
            storage,
            workspace_id,
            session_id=session_id,
            page_index=0,
            cursor=None,
            cursor_secret=cursor_secret,
            now=clock,
        )
    except Exception:
        if connection.in_transaction:
            connection.rollback()
        if snapshot is not None:
            with suppress(sqlite3.Error):
                snapshot.close()
            snapshot = None
        shutil.rmtree(directory, ignore_errors=True)
        with suppress(sqlite3.Error):
            connection.execute(
                "DELETE FROM portable_transfer_sessions WHERE session_id=?",
                (session_id,),
            )
            connection.commit()
        raise
    finally:
        try:
            close_vector()
        finally:
            if vector_lease is not None:
                with suppress(sqlite3.Error, DenseGenerationLifecycleError):
                    if lease_connection is not None:
                        release_generation_lease(lease_connection, vector_lease)
            if lease_connection is not None:
                with suppress(sqlite3.Error):
                    lease_connection.close()
            if snapshot is not None:
                with suppress(sqlite3.Error):
                    snapshot.close()


def read_export_page(
    connection: sqlite3.Connection,
    storage: Path,
    workspace_id: str,
    *,
    session_id: str,
    page_index: int,
    cursor: str | None,
    cursor_secret: bytes,
    now: datetime,
) -> dict[str, Any]:
    now_us = _now_us(now)
    row = connection.execute(
        "SELECT manifest_json,manifest_hash,page_count,expires_at_us,status "
        "FROM portable_transfer_sessions WHERE session_id=? AND workspace_id=? "
        "AND direction='export'",
        (session_id, workspace_id),
    ).fetchone()
    if row is None or row[4] != "ready" or int(row[3]) <= now_us:
        raise PortableTransferError("IMPORT_INVALID")
    page_count = int(row[2])
    if page_index < 0 or page_index >= page_count:
        raise PortableTransferError("IMPORT_INVALID")
    if page_index > 0:
        expected = _cursor(
            cursor_secret, session_id, page_index, int(row[3]), str(row[1])
        )
        if not isinstance(cursor, str) or not hmac.compare_digest(cursor, expected):
            raise PortableTransferError("UNAUTHORIZED_WORKSPACE")
    page = connection.execute(
        "SELECT page_kind,item_count,byte_count,page_hash,relative_path "
        "FROM portable_transfer_pages WHERE session_id=? AND page_index=?",
        (session_id, page_index),
    ).fetchone()
    if page is None:
        raise PortableTransferError("IMPORT_INVALID")
    directory = _session_dir(storage, "export", session_id)
    content = _read_canonical(directory / str(page[4]), str(page[3]))
    manifest = parse_canonical_json(str(row[0]))
    if not isinstance(manifest, dict) or sha256_json(manifest) != row[1]:
        raise PortableTransferError("IMPORT_INVALID")
    try:
        proof_value = _read_canonical(
            directory / f"proofs/{page_index:08d}.json",
            hashlib.sha256(
                (directory / f"proofs/{page_index:08d}.json").read_bytes()
            ).hexdigest(),
        )
        proof = proof_value["proof"]
    except (KeyError, OSError, TypeError) as exc:
        raise PortableTransferError("IMPORT_INVALID") from exc
    descriptor = {
        "page_index": page_index,
        "page_kind": str(page[0]),
        "item_count": int(page[1]),
        "byte_count": int(page[2]),
        "page_hash": str(page[3]),
    }
    if (
        manifest.get("page_count") != page_count
        or not isinstance(proof, list)
        or not _verify_page_proof(descriptor, proof, manifest.get("page_table_root"))
    ):
        raise PortableTransferError("IMPORT_INVALID")
    next_cursor = None
    if page_index + 1 < page_count:
        next_cursor = _cursor(
            cursor_secret, session_id, page_index + 1, int(row[3]), str(row[1])
        )
    result = {
        "bundle_version": 2,
        "workspace_id": workspace_id,
        "export_session_id": session_id,
        "manifest_hash": str(row[1]),
        "manifest": manifest,
        "page_descriptor": descriptor,
        "page_proof": proof,
        "page_index": page_index,
        "page_count": page_count,
        "page_kind": str(page[0]),
        "page_hash": str(page[3]),
        "next_cursor": next_cursor,
        "complete": page_index + 1 == page_count,
        "content": content,
    }
    if len(canonical_json_bytes(result)) > 1_048_576:
        raise PortableTransferError("TASK_REQUIRED")
    return result


def stage_import_page(
    connection: sqlite3.Connection,
    storage: Path,
    workspace_id: str,
    page: Mapping[str, Any],
    *,
    import_session_id: str | None,
    now: datetime,
) -> tuple[str, bool]:
    """Durably stage one verified page without changing canonical state."""

    now_us = _now_us(now)
    _reconcile_storage(connection, storage, now_us)
    try:
        manifest = page["manifest"]
        manifest_hash = str(page["manifest_hash"])
        page_index = int(page["page_index"])
        page_count = int(page["page_count"])
        page_kind = str(page["page_kind"])
        page_hash = str(page["page_hash"])
        descriptor = page["page_descriptor"]
        proof = page["page_proof"]
        content = page["content"]
    except (KeyError, TypeError, ValueError) as exc:
        raise PortableTransferError("IMPORT_INVALID") from exc
    if (
        not isinstance(manifest, Mapping)
        or sha256_json(manifest) != manifest_hash
        or manifest.get("bundle_version") != 2
        or manifest.get("workspace_id") != workspace_id
        or not isinstance(content, Mapping)
        or content.get("kind") != page_kind
        or page_kind not in {"events", "legacy", "vectors"}
        or not isinstance(descriptor, Mapping)
        or not isinstance(proof, list)
    ):
        raise PortableTransferError("IMPORT_INVALID")
    if (
        page_count != manifest.get("page_count")
        or page_count < 1
        or page_index < 0
        or page_index >= page_count
        or dict(descriptor)
        != {
            "page_index": page_index,
            "page_kind": page_kind,
            "item_count": descriptor.get("item_count"),
            "byte_count": descriptor.get("byte_count"),
            "page_hash": page_hash,
        }
        or not _verify_page_proof(descriptor, proof, manifest.get("page_table_root"))
    ):
        raise PortableTransferError("IMPORT_INVALID")
    encoded = canonical_json_bytes(dict(content))
    items = content.get("items")
    if (
        len(encoded) > PAGE_BYTE_LIMIT
        or not isinstance(items, list)
        or len(items) > MAX_PAGE_ITEMS
        or hashlib.sha256(encoded).hexdigest() != page_hash
        or descriptor.get("byte_count") != len(encoded)
        or descriptor.get("item_count") != len(items)
    ):
        raise PortableTransferError("IMPORT_INVALID")
    deterministic = hashlib.sha256(
        f"{workspace_id}:{manifest_hash}".encode()
    ).hexdigest()
    expected_session = "ipt_" + deterministic
    if import_session_id is not None and import_session_id != expected_session:
        raise PortableTransferError("IMPORT_INVALID")
    session_id = expected_session
    directory = _session_dir(storage, "import", session_id)
    relative = f"pages/{page_index:08d}.json"
    path = directory / relative
    existing_session = connection.execute(
        "SELECT manifest_hash,status,expires_at_us FROM portable_transfer_sessions "
        "WHERE session_id=?",
        (session_id,),
    ).fetchone()
    if existing_session is None:
        live = connection.execute(
            "SELECT COUNT(*),COALESCE(SUM(total_bytes),0) "
            "FROM portable_transfer_sessions "
            "WHERE workspace_id=? AND direction='import' "
            "AND status NOT IN ('expired','failed') AND expires_at_us>?",
            (workspace_id, now_us),
        ).fetchone()
        if live is not None and (
            int(live[0]) >= MAX_LIVE_SESSIONS or int(live[1]) >= MAX_SESSION_BYTES
        ):
            raise PortableTransferError("TASK_REQUIRED")
        directory.mkdir(parents=True, exist_ok=True)
        connection.execute(
            "INSERT INTO portable_transfer_sessions(session_id,workspace_id,"
            "direction,status,event_root_hash,manifest_json,manifest_hash,page_count,"
            "staged_page_count,total_bytes,created_at_us,updated_at_us,expires_at_us,"
            "completed_at_us) VALUES (?,?,'import','staging',?,?,?,?,0,0,?,?,?,NULL)",
            (
                session_id,
                workspace_id,
                str(manifest.get("event_root_hash")),
                canonical_json_bytes(dict(manifest)).decode("utf-8"),
                manifest_hash,
                page_count,
                now_us,
                now_us,
                now_us + SESSION_TTL_US,
            ),
        )
    elif (
        str(existing_session[0]) != manifest_hash
        or str(existing_session[1]) not in {"staging", "finalizing", "succeeded"}
        or int(existing_session[2]) <= now_us
    ):
        raise PortableTransferError("IMPORT_INVALID")
    present = connection.execute(
        "SELECT page_hash,byte_count FROM portable_transfer_pages "
        "WHERE session_id=? AND page_index=?",
        (session_id, page_index),
    ).fetchone()
    if present is not None:
        if present[0] != page_hash or int(present[1]) != len(encoded):
            raise PortableTransferError("IMPORT_INVALID")
        if (
            not path.exists()
            or hashlib.sha256(path.read_bytes()).hexdigest() != page_hash
        ):
            raise PortableTransferError("IMPORT_INVALID")
    else:
        total = connection.execute(
            "SELECT current.total_bytes,all_sessions.total_bytes "
            "FROM portable_transfer_sessions AS current JOIN ("
            "SELECT workspace_id,COALESCE(SUM(total_bytes),0) AS total_bytes "
            "FROM portable_transfer_sessions WHERE direction='import' "
            "AND status NOT IN ('expired','failed') AND expires_at_us>? "
            "GROUP BY workspace_id) AS all_sessions "
            "ON all_sessions.workspace_id=current.workspace_id "
            "WHERE current.session_id=?",
            (now_us, session_id),
        ).fetchone()
        if total is None or (
            int(total[0]) + len(encoded) > MAX_SESSION_BYTES
            or int(total[1]) + len(encoded) > MAX_SESSION_BYTES
        ):
            raise PortableTransferError("TASK_REQUIRED")
        _atomic_write(path, encoded)
        connection.execute(
            "INSERT INTO portable_transfer_pages(session_id,page_index,page_kind,"
            "item_count,byte_count,page_hash,relative_path,received_at_us) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (
                session_id,
                page_index,
                page_kind,
                len(items),
                len(encoded),
                page_hash,
                relative,
                now_us,
            ),
        )
        connection.execute(
            "UPDATE portable_transfer_sessions SET "
            "staged_page_count=staged_page_count+1,"
            "total_bytes=total_bytes+?,updated_at_us=? WHERE session_id=?",
            (len(encoded), now_us, session_id),
        )
        _account_physical_files(
            connection, storage, workspace_id, "import", session_id, now_us
        )
    staged = connection.execute(
        "SELECT staged_page_count,page_count FROM portable_transfer_sessions "
        "WHERE session_id=?",
        (session_id,),
    ).fetchone()
    return session_id, staged is not None and int(staged[0]) == int(staged[1])


@dataclass(frozen=True, slots=True)
class ImportFinalizationLease:
    session_id: str
    workspace_id: str
    owner_token: str
    attempt_relative_path: str
    expires_at_us: int


def claim_import_finalization(
    connection: sqlite3.Connection,
    storage: Path,
    workspace_id: str,
    session_id: str,
    *,
    now: datetime,
) -> ImportFinalizationLease:
    """Acquire a durable lease before validation or provider side effects."""

    now_us = _now_us(now)
    owner_token = secrets.token_hex(32)
    attempt_relative_path = f"attempts/{owner_token}"
    connection.execute("BEGIN IMMEDIATE")
    try:
        row = connection.execute(
            "SELECT status,updated_at_us,staged_page_count,page_count,total_bytes "
            "FROM portable_transfer_sessions WHERE session_id=? AND workspace_id=? "
            "AND direction='import'",
            (session_id, workspace_id),
        ).fetchone()
        if row is None or int(row[2]) != int(row[3]):
            raise PortableTransferError("IMPORT_INVALID")
        if row[0] == "succeeded":
            raise PortableTransferError("IMPORT_INVALID")
        existing = connection.execute(
            "SELECT owner_token,attempt_relative_path,expires_at_us FROM "
            "portable_transfer_finalization_leases WHERE session_id=?",
            (session_id,),
        ).fetchone()
        if existing is not None and int(existing[2]) > now_us:
            raise PortableTransferError("TASK_REQUIRED")
        if existing is not None:
            # Retire the expired owner in its own durable phase. If the later
            # capacity check fails, retries see staging state and exact bytes
            # instead of rolling the expired lease back forever.
            _remove_attempt_directory(storage, session_id, str(existing[1]))
            physical = _directory_size(_session_dir(storage, "import", session_id))
            deleted = connection.execute(
                "DELETE FROM portable_transfer_finalization_leases "
                "WHERE session_id=? AND owner_token=? AND expires_at_us<=?",
                (session_id, str(existing[0]), now_us),
            )
            if deleted.rowcount != 1:
                raise PortableTransferError("TASK_REQUIRED")
            connection.execute(
                "UPDATE portable_transfer_sessions SET status='staging',"
                "total_bytes=?,updated_at_us=? WHERE session_id=? "
                "AND status='finalizing'",
                (physical, now_us, session_id),
            )
            connection.commit()
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT status,updated_at_us,staged_page_count,page_count,total_bytes "
                "FROM portable_transfer_sessions WHERE session_id=? "
                "AND workspace_id=? AND direction='import'",
                (session_id, workspace_id),
            ).fetchone()
            replacement = connection.execute(
                "SELECT expires_at_us FROM portable_transfer_finalization_leases "
                "WHERE session_id=?",
                (session_id,),
            ).fetchone()
            if row is None or int(row[2]) != int(row[3]) or replacement is not None:
                raise PortableTransferError("TASK_REQUIRED")
        elif row[0] == "finalizing" and now_us - int(row[1]) <= _FINALIZATION_LEASE_US:
            # Fail closed for a pre-migration finalizer that has no token.
            raise PortableTransferError("TASK_REQUIRED")
        if row[0] not in {"staging", "finalizing"}:
            raise PortableTransferError("IMPORT_INVALID")
        retained = _retained_bytes(connection, workspace_id, now_us, exclude=session_id)
        reservation = MAX_SESSION_BYTES - retained
        physical = _directory_size(_session_dir(storage, "import", session_id))
        if reservation - physical < _BACKUP_PROGRESS_BYTES:
            raise PortableTransferError("TASK_REQUIRED")
        connection.execute(
            "UPDATE portable_transfer_sessions SET status='finalizing',total_bytes=?,"
            "updated_at_us=? "
            "WHERE session_id=?",
            (reservation, now_us, session_id),
        )
        expires_at_us = now_us + _FINALIZATION_LEASE_US
        connection.execute(
            "INSERT INTO portable_transfer_finalization_leases("
            "session_id,owner_token,attempt_relative_path,acquired_at_us,"
            "renewed_at_us,expires_at_us) VALUES (?,?,?,?,?,?)",
            (
                session_id,
                owner_token,
                attempt_relative_path,
                now_us,
                now_us,
                expires_at_us,
            ),
        )
        connection.commit()
        return ImportFinalizationLease(
            session_id,
            workspace_id,
            owner_token,
            attempt_relative_path,
            expires_at_us,
        )
    except Exception:
        connection.rollback()
        raise


def renew_import_finalization(
    connection: sqlite3.Connection,
    workspace_id: str,
    lease: ImportFinalizationLease,
    *,
    now: datetime,
) -> ImportFinalizationLease:
    """Renew exactly one live owner lease using a compare-and-set update."""

    if lease.workspace_id != workspace_id:
        raise PortableTransferError("IMPORT_INVALID")
    now_us = _now_us(now)
    owns_transaction = not connection.in_transaction
    if owns_transaction:
        connection.execute("BEGIN IMMEDIATE")
    try:
        expires_at_us = now_us + _FINALIZATION_LEASE_US
        updated = connection.execute(
            "UPDATE portable_transfer_finalization_leases SET renewed_at_us=?,"
            "expires_at_us=? WHERE session_id=? AND owner_token=? "
            "AND expires_at_us>? AND EXISTS(SELECT 1 FROM portable_transfer_sessions "
            "WHERE session_id=? AND workspace_id=? AND direction='import' "
            "AND status='finalizing')",
            (
                now_us,
                expires_at_us,
                lease.session_id,
                lease.owner_token,
                now_us,
                lease.session_id,
                workspace_id,
            ),
        )
        if updated.rowcount != 1:
            raise PortableTransferError("TASK_REQUIRED")
        connection.execute(
            "UPDATE portable_transfer_sessions SET updated_at_us=? WHERE session_id=?",
            (now_us, lease.session_id),
        )
        if owns_transaction:
            connection.commit()
        return ImportFinalizationLease(
            lease.session_id,
            workspace_id,
            lease.owner_token,
            lease.attempt_relative_path,
            expires_at_us,
        )
    except Exception:
        if owns_transaction:
            connection.rollback()
        raise


def release_import_finalization(
    connection: sqlite3.Connection,
    storage: Path,
    lease: ImportFinalizationLease,
    *,
    now: datetime,
) -> bool:
    """Release only the caller's lease and private attempt directory."""

    _remove_attempt_directory(storage, lease.session_id, lease.attempt_relative_path)
    physical = _directory_size(_session_dir(storage, "import", lease.session_id))
    if connection.in_transaction:
        connection.rollback()
    connection.execute("BEGIN IMMEDIATE")
    try:
        deleted = connection.execute(
            "DELETE FROM portable_transfer_finalization_leases "
            "WHERE session_id=? AND owner_token=?",
            (lease.session_id, lease.owner_token),
        )
        if deleted.rowcount == 1:
            connection.execute(
                "UPDATE portable_transfer_sessions SET status='staging',total_bytes=?,"
                "updated_at_us=? "
                "WHERE session_id=? AND status='finalizing'",
                (physical, _now_us(now), lease.session_id),
            )
        connection.commit()
        return deleted.rowcount == 1
    except Exception:
        connection.rollback()
        raise


def cleanup_import_attempt(
    connection: sqlite3.Connection,
    storage: Path,
    workspace_id: str,
    lease: ImportFinalizationLease,
    *,
    now: datetime,
) -> None:
    """Remove one fenced attempt's derived validation files."""

    _remove_attempt_directory(storage, lease.session_id, lease.attempt_relative_path)
    owns_transaction = not connection.in_transaction
    if owns_transaction:
        connection.execute("BEGIN IMMEDIATE")
    try:
        _account_physical_files(
            connection,
            storage,
            workspace_id,
            "import",
            lease.session_id,
            _now_us(now),
        )
        if owns_transaction:
            connection.commit()
    except Exception:
        if owns_transaction:
            connection.rollback()
        raise


@dataclass
class PreparedImport:
    session_id: str
    database_path: Path
    manifest: dict[str, Any]
    legacy_path: Path | None
    vectors_path: Path | None
    page_count: int
    lease: ImportFinalizationLease | None = None

    def iter_events(self) -> Iterator[dict[str, Any]]:
        connection = sqlite3.connect(self.database_path)
        connection.row_factory = sqlite3.Row
        try:
            while True:
                rows = connection.execute(
                    "SELECT event_json FROM events WHERE processed=0 AND "
                    "(previous_event_hash IS NULL OR EXISTS(SELECT 1 FROM events p "
                    "WHERE p.event_hash=events.previous_event_hash AND p.processed=1)) "
                    "AND (causation_event_id IS NULL OR EXISTS(SELECT 1 FROM events c "
                    "WHERE c.event_id=events.causation_event_id AND c.processed=1)) "
                    "AND (memory_ref_a IS NULL OR EXISTS(SELECT 1 FROM events a "
                    "WHERE a.stream_id=events.memory_ref_a AND a.processed=1)) "
                    "AND (memory_ref_b IS NULL OR EXISTS(SELECT 1 FROM events b "
                    "WHERE b.stream_id=events.memory_ref_b AND b.processed=1)) "
                    "ORDER BY recorded_at_us,event_id LIMIT 512"
                ).fetchall()
                if not rows:
                    remaining = connection.execute(
                        "SELECT COUNT(*) FROM events WHERE processed=0"
                    ).fetchone()[0]
                    if remaining:
                        raise PortableTransferError("IMPORT_INVALID")
                    return
                for row in rows:
                    event = parse_canonical_json(str(row[0]))
                    if not isinstance(event, dict):
                        raise PortableTransferError("IMPORT_INVALID")
                    yield event
                    connection.execute(
                        "UPDATE events SET processed=1 WHERE event_id=?",
                        (event["event_id"],),
                    )
                connection.commit()
        finally:
            connection.close()


@dataclass
class VectorCandidate:
    client: _QdrantTransferClient
    collection_name: str
    generation: int
    provider_key: str
    model_id: str
    dimension: int
    row_count: int
    source_event_count: int
    source_event_root_hash: str
    manifest_hash: str
    validation_path: Path
    encoder_contract: dict[str, Any]
    vector_format: str
    vector_space_hash: str | None
    attempt_id: str
    collection_prefix: str
    session_id: str | None = None
    owner_token: str | None = None
    artifact_collection_name: str | None = None

    def close(self) -> None:
        close = getattr(self.client, "close", None)
        if callable(close):
            close()
        self.validation_path.unlink(missing_ok=True)

    def discard(
        self,
        connection: sqlite3.Connection | None = None,
        workspace_id: str | None = None,
    ) -> None:
        try:
            active = None
            if connection is not None:
                active = _active_vector_collection(connection, self.collection_name)
            if active is None:
                self.client.delete_collection(self.collection_name)
                if (
                    connection is not None
                    and self.session_id is not None
                    and self.owner_token is not None
                    and self.artifact_collection_name is not None
                ):
                    connection.execute(
                        "DELETE FROM portable_transfer_attempt_artifacts "
                        "WHERE session_id=? AND owner_token=? "
                        "AND artifact_kind='qdrant_collection' AND artifact_name=?",
                        (
                            self.session_id,
                            self.owner_token,
                            self.artifact_collection_name,
                        ),
                    )
        finally:
            self.close()


def _active_vector_collection(
    connection: sqlite3.Connection, collection_name: str
) -> tuple[Any, ...] | None:
    # Provider collection names are global, even when this transfer is scoped.
    return connection.execute(
        "SELECT 1 FROM projection_manifests "
        "WHERE projection_name='dense' AND status='active' "
        "AND json_extract(details_json,'$.collection_name')=? LIMIT 1",
        (collection_name,),
    ).fetchone()


def _reclaim_stale_vector_artifacts(
    client: _QdrantTransferClient,
    connection: sqlite3.Connection,
    prepared: PreparedImport,
    workspace_id: str,
    checkpoint: Callable[[], None] | None,
) -> None:
    """Delete durable, inactive provider artifacts from prior lease owners."""

    current_owner = None if prepared.lease is None else prepared.lease.owner_token
    rows = connection.execute(
        "SELECT owner_token,artifact_name FROM portable_transfer_attempt_artifacts "
        "WHERE session_id=? AND artifact_kind='qdrant_collection' "
        "AND owner_token<>? ORDER BY owner_token,artifact_name",
        (prepared.session_id, current_owner or ""),
    ).fetchall()
    for owner_token, artifact_name in rows:
        if checkpoint is not None:
            checkpoint()
        active = _active_vector_collection(connection, str(artifact_name))
        if active is not None:
            continue
        if client.collection_exists(str(artifact_name)):
            client.delete_collection(str(artifact_name))
        if checkpoint is not None:
            checkpoint()
        connection.execute(
            "DELETE FROM portable_transfer_attempt_artifacts WHERE session_id=? "
            "AND owner_token=? AND artifact_kind='qdrant_collection' "
            "AND artifact_name=?",
            (prepared.session_id, str(owner_token), str(artifact_name)),
        )
    if rows:
        connection.commit()


def _vector_metadata_compatibility(
    metadata: Mapping[str, Any], manifest: Mapping[str, Any], config: object | None
) -> bool:
    required = {
        "schema_version",
        "provider_key",
        "model_id",
        "dimension",
        "distance",
        "projection_version",
        "builder_version",
        "build_config_hash",
        "builder_contract_hash",
        "encoder_contract",
        "generation",
        "collection_name",
        "source_event_count",
        "source_event_root_hash",
        "row_count",
        "payload_hash",
    }
    if not required <= set(metadata):
        raise PortableTransferError("IMPORT_INVALID")
    encoder_contract = metadata.get("encoder_contract")
    scalar_valid = (
        isinstance(metadata.get("schema_version"), int)
        and not isinstance(metadata.get("schema_version"), bool)
        and isinstance(metadata.get("projection_version"), int)
        and not isinstance(metadata.get("projection_version"), bool)
        and isinstance(metadata.get("generation"), int)
        and not isinstance(metadata.get("generation"), bool)
        and int(metadata["generation"]) > 0
        and isinstance(metadata.get("dimension"), int)
        and not isinstance(metadata.get("dimension"), bool)
        and int(metadata["dimension"]) > 0
        and isinstance(metadata.get("row_count"), int)
        and not isinstance(metadata.get("row_count"), bool)
        and int(metadata["row_count"]) >= 0
        and isinstance(encoder_contract, Mapping)
        and all(
            isinstance(metadata.get(field), str) and bool(metadata.get(field))
            for field in (
                "provider_key",
                "model_id",
                "distance",
                "builder_version",
                "collection_name",
            )
        )
        and all(
            isinstance(metadata.get(field), str)
            and len(str(metadata[field])) == 64
            and not (set(str(metadata[field])) - set("0123456789abcdef"))
            for field in ("build_config_hash", "builder_contract_hash", "payload_hash")
        )
    )
    if not scalar_valid:
        raise PortableTransferError("IMPORT_INVALID")
    assert isinstance(encoder_contract, Mapping)
    expected_contract_hash = sha256_json(
        {
            "build_config_hash": metadata["build_config_hash"],
            "builder_version": metadata["builder_version"],
            "encoder_contract": dict(encoder_contract),
            "projection": "dense",
        }
    )
    if expected_contract_hash != metadata["builder_contract_hash"]:
        raise PortableTransferError("IMPORT_INVALID")
    try:
        vector_format, vector_space_hash = _portable_vector_attestations(
            encoder_contract,
            model_id=str(metadata["model_id"]),
            dimension=int(metadata["dimension"]),
        )
    except (TypeError, ValueError) as exc:
        raise PortableTransferError("IMPORT_INVALID") from exc
    if ("vector_format" in metadata or "vector_space_hash" in metadata) and (
        metadata.get("vector_format") != vector_format
        or metadata.get("vector_space_hash") != vector_space_hash
    ):
        raise PortableTransferError("IMPORT_INVALID")
    if metadata.get("source_event_count") != manifest.get(
        "event_count"
    ) or metadata.get("source_event_root_hash") != manifest.get("event_root_hash"):
        raise PortableTransferError("IMPORT_INVALID")
    if config is None:
        return False
    if (
        metadata["schema_version"] != _VECTOR_SCHEMA_VERSION
        or metadata["projection_version"] != 1
        or metadata["provider_key"] != _VECTOR_PROVIDER_KEY
        or metadata["distance"] != _VECTOR_DISTANCE
        or metadata["builder_version"] != DENSE_BUILDER_VERSION
        or getattr(config, "embedding_model", None) != metadata["model_id"]
        or getattr(config, "embedding_dimension", None) != metadata["dimension"]
    ):
        return False
    try:
        from ...retrieval.runtime import _embedding_encoder

        query_prefix = _field(config, "embedding_query_prefix")
        if not isinstance(query_prefix, str):
            return False
        query_encoder = _embedding_encoder(config, "query")
        return dense_query_encoder_matches_contract(
            query_encoder=query_encoder,
            encoder_contract=encoder_contract,
            model_id=str(metadata["model_id"]),
            dimension=int(metadata["dimension"]),
            query_prefix=query_prefix,
        )
    except (AttributeError, TypeError, ValueError):
        return False


def _validate_uploaded_vector_point(
    connection: sqlite3.Connection,
    point: object,
) -> None:
    point_id = str(_field(point, "id"))
    row = connection.execute(
        "SELECT payload_json,vector_json,seen FROM portable_expected_vectors "
        "WHERE point_id=?",
        (point_id,),
    ).fetchone()
    if row is None or int(row[2]) != 0:
        raise PortableTransferError("CAPABILITY_DEGRADED")
    payload = _field(point, "payload")
    vector = _field(point, "vector")
    if (
        not isinstance(payload, Mapping)
        or canonical_json_bytes(dict(payload)).decode("utf-8") != str(row[0])
        or not isinstance(vector, Sequence)
        or isinstance(vector, (str, bytes, Mapping))
    ):
        raise PortableTransferError("CAPABILITY_DEGRADED")
    expected_raw = parse_canonical_json(str(row[1]))
    if not isinstance(expected_raw, list):
        raise PortableTransferError("CAPABILITY_DEGRADED")
    actual = [float(value) for value in vector]
    if not cosine_vectors_match(actual, expected_raw):
        raise PortableTransferError("CAPABILITY_DEGRADED")
    connection.execute(
        "UPDATE portable_expected_vectors SET seen=1 WHERE point_id=?", (point_id,)
    )


def _validate_uploaded_vector_collection(
    candidate: VectorCandidate,
    checkpoint: Callable[[], None] | None = None,
) -> None:
    connection = sqlite3.connect(candidate.validation_path)
    try:
        connection.execute("UPDATE portable_expected_vectors SET seen=0")
        offset: object | None = None
        seen_offsets: set[str] = set()
        count = 0
        while True:
            if checkpoint is not None:
                checkpoint()
            points, next_offset = _scroll_client(
                candidate.client,
                collection_name=candidate.collection_name,
                limit=256,
                offset=offset,
                with_payload=True,
                with_vectors=True,
            )
            if checkpoint is not None:
                checkpoint()
            for point in points:
                if checkpoint is not None:
                    checkpoint()
                _validate_uploaded_vector_point(connection, point)
                count += 1
            if next_offset is None:
                break
            marker = repr(next_offset)
            if marker in seen_offsets:
                raise PortableTransferError("CAPABILITY_DEGRADED")
            seen_offsets.add(marker)
            offset = next_offset
        remaining = connection.execute(
            "SELECT COUNT(*) FROM portable_expected_vectors WHERE seen=0"
        ).fetchone()
        if count != candidate.row_count or remaining is None or int(remaining[0]) != 0:
            raise PortableTransferError("CAPABILITY_DEGRADED")
        connection.commit()
    finally:
        connection.close()


def prepare_vector_candidate(
    prepared: PreparedImport,
    connection: sqlite3.Connection,
    workspace_id: str,
    config: object | None,
    *,
    local_capability_ready: bool = False,
    checkpoint: Callable[[], None] | None = None,
) -> tuple[VectorCandidate | None, str | None]:
    """Build and validate an isolated Qdrant collection before DB activation."""

    metadata = prepared.manifest.get("vectors")
    if metadata is None:
        return None, None
    if not isinstance(metadata, Mapping) or prepared.vectors_path is None:
        raise PortableTransferError("IMPORT_INVALID")
    compatible = _vector_metadata_compatibility(metadata, prepared.manifest, config)
    if not local_capability_ready or not compatible:
        return None, "VECTOR_REBUILD_REQUIRED"
    vector_format, vector_space_hash = _portable_vector_attestations(
        metadata["encoder_contract"],
        model_id=str(metadata["model_id"]),
        dimension=int(metadata["dimension"]),
    )
    try:
        if checkpoint is not None:
            checkpoint()
        client = _qdrant_transfer_client(config)
        if checkpoint is not None:
            checkpoint()
        _reclaim_stale_vector_artifacts(
            client, connection, prepared, workspace_id, checkpoint
        )
        generation_row = connection.execute(
            "SELECT COALESCE(MAX(generation),0)+1 FROM projection_manifests "
            "WHERE workspace_id=? AND projection_name='dense'",
            (workspace_id,),
        ).fetchone()
        generation = int(generation_row[0])
        prefix = str(getattr(config, "qdrant_collection_prefix", "daem0nmcp"))
        manifest_hash = sha256_json(prepared.manifest)
        attempt_id = (
            prepared.lease.owner_token[:16]
            if prepared.lease is not None
            else secrets.token_hex(8)
        )
        collection_name = f"{prefix}-{workspace_id}-portable-g{generation}-{manifest_hash[:8]}-{attempt_id}"
        if prepared.lease is not None:
            registered = connection.execute(
                "INSERT INTO portable_transfer_attempt_artifacts("
                "session_id,owner_token,artifact_kind,artifact_name,created_at_us) "
                "SELECT session_id,owner_token,'qdrant_collection',?,renewed_at_us "
                "FROM portable_transfer_finalization_leases WHERE session_id=? "
                "AND owner_token=?",
                (
                    collection_name,
                    prepared.session_id,
                    prepared.lease.owner_token,
                ),
            )
            if registered.rowcount != 1:
                raise PortableTransferError("TASK_REQUIRED")
            connection.commit()
        try:
            from qdrant_client import models as qdrant_models

            vector_config: object = qdrant_models.VectorParams(
                size=int(metadata["dimension"]),
                distance=qdrant_models.Distance.COSINE,
            )
            point_factory: Callable[..., object] = qdrant_models.PointStruct
        except ImportError:
            vector_config = {
                "size": int(metadata["dimension"]),
                "distance": "Cosine",
            }

            def point_factory(**values: object) -> object:
                return values

        if checkpoint is not None:
            checkpoint()
        client.create_collection(
            collection_name=collection_name,
            vectors_config=vector_config,
        )
        if checkpoint is not None:
            checkpoint()
        validation_path = prepared.vectors_path.with_name(
            f"validated-vectors-{attempt_id}.db"
        )
        validation_path.unlink(missing_ok=True)
        validation = sqlite3.connect(validation_path)
        validation.execute(
            "CREATE TABLE portable_expected_vectors("
            "point_id TEXT PRIMARY KEY,payload_json TEXT NOT NULL,"
            "vector_json TEXT NOT NULL,seen INTEGER NOT NULL DEFAULT 0)"
        )
        batch: list[object] = []
        count = 0
        with prepared.vectors_path.open("rb") as handle:
            for line in handle:
                if checkpoint is not None:
                    checkpoint()
                try:
                    point = parse_canonical_json(line.rstrip(b"\n").decode("utf-8"))
                except Exception as exc:
                    raise PortableTransferError("IMPORT_INVALID") from exc
                if not isinstance(point, Mapping):
                    raise PortableTransferError("IMPORT_INVALID")
                payload = point.get("payload")
                vector = point.get("vector")
                if (
                    not isinstance(payload, Mapping)
                    or payload.get("workspace_id") != workspace_id
                    or not isinstance(vector, list)
                    or len(vector) != int(metadata["dimension"])
                ):
                    raise PortableTransferError("IMPORT_INVALID")
                record_id = payload.get("record_id")
                content_hash = payload.get("content_hash")
                try:
                    expected_id, expected_source_payload = build_dense_point_payload(
                        workspace_id=workspace_id,
                        record_id=str(record_id),
                        content_hash=str(content_hash),
                        projection_generation=int(metadata["generation"]),
                        model_id=str(metadata["model_id"]),
                    )
                    rewritten_id, rewritten = build_dense_point_payload(
                        workspace_id=workspace_id,
                        record_id=str(record_id),
                        content_hash=str(content_hash),
                        projection_generation=generation,
                        model_id=str(metadata["model_id"]),
                    )
                    values = [float(value) for value in vector]
                except (TypeError, ValueError) as exc:
                    raise PortableTransferError("IMPORT_INVALID") from exc
                if (
                    str(point.get("point_id")) != expected_id
                    or expected_id != rewritten_id
                    or dict(payload) != expected_source_payload
                    or not all(math.isfinite(value) for value in values)
                ):
                    raise PortableTransferError("IMPORT_INVALID")
                try:
                    validation.execute(
                        "INSERT INTO portable_expected_vectors("
                        "point_id,payload_json,vector_json,seen) VALUES (?,?,?,0)",
                        (
                            expected_id,
                            canonical_json_bytes(rewritten).decode("utf-8"),
                            canonical_json_bytes(values).decode("utf-8"),
                        ),
                    )
                except sqlite3.IntegrityError as exc:
                    raise PortableTransferError("IMPORT_INVALID") from exc
                batch.append(
                    point_factory(
                        id=expected_id,
                        vector=values,
                        payload=rewritten,
                    )
                )
                count += 1
                if len(batch) >= 256:
                    if checkpoint is not None:
                        checkpoint()
                    client.upsert(
                        collection_name=collection_name,
                        points=batch,
                        wait=True,
                    )
                    if checkpoint is not None:
                        checkpoint()
                    batch = []
        if batch:
            if checkpoint is not None:
                checkpoint()
            client.upsert(collection_name=collection_name, points=batch, wait=True)
            if checkpoint is not None:
                checkpoint()
        validation.commit()
        validation.close()
        count_result = client.count(collection_name=collection_name, exact=True)
        if checkpoint is not None:
            checkpoint()
        actual_count = _field(count_result, "count")
        if actual_count != count or count != metadata.get("row_count"):
            raise PortableTransferError("CAPABILITY_DEGRADED")
        candidate = VectorCandidate(
            client=client,
            collection_name=collection_name,
            generation=generation,
            provider_key=str(metadata["provider_key"]),
            model_id=str(metadata["model_id"]),
            dimension=int(metadata["dimension"]),
            row_count=count,
            source_event_count=int(prepared.manifest["event_count"]),
            source_event_root_hash=str(prepared.manifest["event_root_hash"]),
            manifest_hash=manifest_hash,
            validation_path=validation_path,
            encoder_contract=dict(metadata["encoder_contract"]),
            vector_format=vector_format,
            vector_space_hash=vector_space_hash,
            attempt_id=attempt_id,
            collection_prefix=prefix,
            session_id=(prepared.session_id if prepared.lease is not None else None),
            owner_token=(
                prepared.lease.owner_token if prepared.lease is not None else None
            ),
            artifact_collection_name=(
                collection_name if prepared.lease is not None else None
            ),
        )
        _validate_uploaded_vector_collection(candidate, checkpoint)
        physical = _directory_size(prepared.vectors_path.parent)
        retained = connection.execute(
            "SELECT COALESCE(SUM(total_bytes),0) FROM portable_transfer_sessions "
            "WHERE workspace_id=? AND status NOT IN ('expired','failed') "
            "AND session_id<>?",
            (workspace_id, prepared.session_id),
        ).fetchone()
        if (
            physical > MAX_SESSION_BYTES
            or int(retained[0]) + physical > MAX_SESSION_BYTES
        ):
            raise PortableTransferError("TASK_REQUIRED")
        connection.execute(
            "UPDATE portable_transfer_sessions SET total_bytes=? WHERE session_id=?",
            (physical, prepared.session_id),
        )
        connection.commit()
        return candidate, None
    except Exception as exc:
        if "validation" in locals():
            with suppress(sqlite3.Error):
                validation.close()
        if "validation_path" in locals():
            with suppress(OSError):
                validation_path.unlink(missing_ok=True)
        if "client" in locals():
            try:
                if (
                    "collection_name" in locals()
                    and _active_vector_collection(connection, collection_name) is None
                ):
                    if client.collection_exists(collection_name):
                        client.delete_collection(collection_name)
                    # A failed or unacknowledged deletion must leave its durable
                    # recovery record intact for the next finalization owner.
                    if (
                        not client.collection_exists(collection_name)
                        and prepared.lease is not None
                    ):
                        connection.execute(
                            "DELETE FROM portable_transfer_attempt_artifacts "
                            "WHERE session_id=? AND owner_token=? "
                            "AND artifact_kind='qdrant_collection' AND artifact_name=?",
                            (
                                prepared.session_id,
                                prepared.lease.owner_token,
                                collection_name,
                            ),
                        )
                        connection.commit()
            except Exception:
                # Preserve the original stable error, including cancellation.
                # The artifact registry is the durable cleanup retry mechanism.
                pass
            finally:
                with suppress(Exception):
                    close = getattr(client, "close", None)
                    if callable(close):
                        close()
        if isinstance(exc, PortableTransferError):
            raise
        raise PortableTransferError("CAPABILITY_DEGRADED") from exc


def activate_vector_candidate(
    candidate: VectorCandidate,
    connection: sqlite3.Connection,
    workspace_id: str,
    *,
    now_us: int,
    checkpoint: Callable[[], None] | None = None,
) -> None:
    """Publish a validated collection through the SQLite dense manifest."""

    _validate_uploaded_vector_collection(candidate, checkpoint)
    event_digest = hashlib.sha256()
    event_count = 0
    for row in connection.execute(
        "SELECT event_hash FROM memory_events WHERE workspace_id=? ORDER BY event_id",
        (workspace_id,),
    ):
        event_digest.update(bytes.fromhex(str(row[0])))
        event_count += 1
    if (
        event_count != candidate.source_event_count
        or event_digest.hexdigest() != candidate.source_event_root_hash
    ):
        raise PortableTransferError("VECTOR_REBUILD_REQUIRED")
    generation_row = connection.execute(
        "SELECT COALESCE(MAX(generation),0)+1 FROM projection_manifests "
        "WHERE workspace_id=? AND projection_name='dense'",
        (workspace_id,),
    ).fetchone()
    if generation_row is None or int(generation_row[0]) != candidate.generation:
        raise PortableTransferError("CAPABILITY_DEGRADED")
    target_storage = dense_manifest_details(
        workspace_id=workspace_id,
        provider_key=candidate.provider_key,
        generation=candidate.generation,
        model_id=candidate.model_id,
        dimension=candidate.dimension,
        collection_prefix=candidate.collection_prefix,
    )
    final_collection = str(target_storage["collection_name"])
    try:
        from qdrant_client import models as qdrant_models

        vector_config: object = qdrant_models.VectorParams(
            size=candidate.dimension, distance=qdrant_models.Distance.COSINE
        )
        point_factory: Callable[..., object] = qdrant_models.PointStruct
    except ImportError:
        vector_config = {"size": candidate.dimension, "distance": "Cosine"}

        def point_factory(**values: object) -> object:
            return values

    temporary_collection = candidate.collection_name
    exists = getattr(candidate.client, "collection_exists", None)
    final_exists = callable(exists) and bool(exists(final_collection))
    if final_exists:
        candidate.collection_name = final_collection
        try:
            _validate_uploaded_vector_collection(candidate, checkpoint)
        except PortableTransferError as exc:
            candidate.collection_name = temporary_collection
            raise PortableTransferError("VECTOR_REBUILD_REQUIRED") from exc
        candidate.client.delete_collection(temporary_collection)
    else:
        if checkpoint is not None:
            checkpoint()
        candidate.client.create_collection(
            collection_name=final_collection, vectors_config=vector_config
        )
        if checkpoint is not None:
            checkpoint()
    promotion_offset: object | None = None
    try:
        if not final_exists:
            while True:
                if checkpoint is not None:
                    checkpoint()
                points, promotion_offset = _scroll_client(
                    candidate.client,
                    collection_name=temporary_collection,
                    limit=256,
                    offset=promotion_offset,
                    with_payload=True,
                    with_vectors=True,
                )
                if checkpoint is not None:
                    checkpoint()
                batch = [
                    point_factory(
                        id=_field(point, "id"),
                        payload=_field(point, "payload"),
                        vector=_field(point, "vector"),
                    )
                    for point in points
                ]
                if batch:
                    candidate.client.upsert(
                        collection_name=final_collection, points=batch, wait=True
                    )
                    if checkpoint is not None:
                        checkpoint()
                if promotion_offset is None:
                    break
            candidate.client.delete_collection(temporary_collection)
            candidate.collection_name = final_collection
        if (
            candidate.session_id is not None
            and candidate.owner_token is not None
            and candidate.artifact_collection_name is not None
        ):
            connection.execute(
                "DELETE FROM portable_transfer_attempt_artifacts WHERE session_id=? "
                "AND owner_token=? AND artifact_kind='qdrant_collection' "
                "AND artifact_name=?",
                (
                    candidate.session_id,
                    candidate.owner_token,
                    candidate.artifact_collection_name,
                ),
            )
        _validate_uploaded_vector_collection(candidate, checkpoint)
    except Exception:
        if not final_exists:
            with suppress(Exception):
                candidate.client.delete_collection(final_collection)
        raise
    connection.execute(
        "CREATE TEMP TABLE IF NOT EXISTS portable_vector_refs("
        "workspace_id TEXT,provider_key TEXT,projection_generation INTEGER,"
        "record_id TEXT,content_hash TEXT,model_id TEXT,dimension INTEGER,"
        "updated_event_id TEXT,updated_at_us INTEGER)"
    )
    connection.execute("DELETE FROM portable_vector_refs")
    reference_count = 0
    references: list[tuple[Any, ...]] = []
    # The Qdrant payload is validated against canonical records after replay.
    reference_offset: object | None = None
    while True:
        if checkpoint is not None:
            checkpoint()
        points, reference_offset = _scroll_client(
            candidate.client,
            collection_name=candidate.collection_name,
            limit=256,
            offset=reference_offset,
            with_payload=True,
            with_vectors=False,
        )
        if checkpoint is not None:
            checkpoint()
        for point in points:
            payload = _field(point, "payload")
            if not isinstance(payload, Mapping):
                raise PortableTransferError("CAPABILITY_DEGRADED")
            record_id = payload.get("record_id")
            content_hash = payload.get("content_hash")
            row = connection.execute(
                "SELECT source_event_id FROM memory_records WHERE workspace_id=? "
                "AND record_id=? AND content_hash=? AND deleted_at_us IS NULL",
                (workspace_id, record_id, content_hash),
            ).fetchone()
            if row is None:
                raise PortableTransferError("IMPORT_INVALID")
            references.append(
                (
                    workspace_id,
                    candidate.provider_key,
                    candidate.generation,
                    record_id,
                    content_hash,
                    candidate.model_id,
                    candidate.dimension,
                    str(row[0]),
                    now_us,
                )
            )
            reference_count += 1
            if len(references) >= 256:
                connection.executemany(
                    "INSERT INTO portable_vector_refs VALUES (?,?,?,?,?,?,?,?,?)",
                    references,
                )
                references = []
        if reference_offset is None:
            break
    if reference_count != candidate.row_count:
        raise PortableTransferError("CAPABILITY_DEGRADED")
    if references:
        connection.executemany(
            "INSERT INTO portable_vector_refs VALUES (?,?,?,?,?,?,?,?,?)",
            references,
        )
        references = []
    missing_record = connection.execute(
        "SELECT 1 FROM memory_records r LEFT JOIN portable_vector_refs v "
        "ON v.workspace_id=r.workspace_id AND v.record_id=r.record_id "
        "WHERE r.workspace_id=? AND r.deleted_at_us IS NULL AND v.record_id IS NULL LIMIT 1",
        (workspace_id,),
    ).fetchone()
    live_count = int(
        connection.execute(
            "SELECT COUNT(*) FROM memory_records WHERE workspace_id=? "
            "AND deleted_at_us IS NULL",
            (workspace_id,),
        ).fetchone()[0]
    )
    if missing_record is not None or live_count != candidate.row_count:
        raise PortableTransferError("VECTOR_REBUILD_REQUIRED")
    storage_details = target_storage
    builder = {
        "build_config_hash": storage_details["build_config_hash"],
        "builder_version": DENSE_BUILDER_VERSION,
        "encoder_contract": candidate.encoder_contract,
        "projection": "dense",
    }
    details = {
        **storage_details,
        "builder_contract_hash": sha256_json(builder),
        "encoder_contract": candidate.encoder_contract,
        "projection": "dense",
        "vector_format": candidate.vector_format,
        "vector_space_hash": candidate.vector_space_hash,
        "portable_manifest_hash": candidate.manifest_hash,
    }
    manifest_id = (
        "prj_"
        + hashlib.sha256(
            f"portable:{workspace_id}:{candidate.generation}:{candidate.manifest_hash}".encode()
        ).hexdigest()
    )
    cursor = connection.execute(
        "SELECT recorded_at_us,event_id FROM memory_events WHERE workspace_id=? "
        "ORDER BY recorded_at_us DESC,event_id DESC LIMIT 1",
        (workspace_id,),
    ).fetchone()
    connection.execute(
        "UPDATE projection_manifests SET status='ready' WHERE workspace_id=? "
        "AND projection_name='dense' AND status='active'",
        (workspace_id,),
    )
    connection.execute(
        "INSERT INTO projection_manifests(manifest_id,workspace_id,projection_name,"
        "generation,projection_version,status,source_event_count,source_event_root_hash,"
        "cursor_recorded_at_us,cursor_event_id,row_count,builder_version,details_json,"
        "started_at_us,completed_at_us,activated_at_us) "
        "VALUES (?,?, 'dense',?,1,'active',"
        "?,?,?,?,?,?,?,?,?,?)",
        (
            manifest_id,
            workspace_id,
            candidate.generation,
            candidate.source_event_count,
            candidate.source_event_root_hash,
            None if cursor is None else int(cursor[0]),
            None if cursor is None else str(cursor[1]),
            candidate.row_count,
            DENSE_BUILDER_VERSION,
            canonical_json_bytes(details).decode("utf-8"),
            now_us,
            now_us,
            now_us,
        ),
    )
    connection.execute(
        "INSERT INTO dense_projection_refs(workspace_id,provider_key,"
        "projection_generation,record_id,content_hash,model_id,dimension,state,"
        "updated_event_id,failure_code,updated_at_us,vector_format,vector_sha256) "
        "SELECT workspace_id,provider_key,"
        "projection_generation,record_id,content_hash,model_id,dimension,'ready',"
        "updated_event_id,NULL,updated_at_us,NULL,NULL FROM portable_vector_refs"
    )
    enqueue_inactive_generation_gc(
        connection,
        workspace_id=workspace_id,
        provider_key=candidate.provider_key,
        now_us=now_us,
    )
    connection.execute("DELETE FROM portable_vector_refs")


def prepare_import(
    connection: sqlite3.Connection,
    storage: Path,
    workspace_id: str,
    session_id: str,
    *,
    lease: ImportFinalizationLease,
    decode_event: Callable[[Mapping[str, Any]], Mapping[str, Any]],
    clock: Callable[[], datetime],
    cancelled: Callable[[], bool] | None = None,
) -> PreparedImport:
    """Validate a complete session into bounded local staging indexes."""

    if lease.session_id != session_id or lease.workspace_id != workspace_id:
        raise PortableTransferError("IMPORT_INVALID")
    current_lease = lease

    def checkpoint(*, force_renew: bool = False) -> None:
        nonlocal current_lease
        if cancelled is not None and cancelled():
            raise PortableTransferError("CANCELLED")
        current = clock()
        current_us = _now_us(current)
        renew_at_us = current_lease.expires_at_us - _FINALIZATION_LEASE_US // 3
        if force_renew or current_us >= renew_at_us:
            current_lease = renew_import_finalization(
                connection, workspace_id, current_lease, now=current
            )

    checkpoint(force_renew=True)
    row = connection.execute(
        "SELECT manifest_json,manifest_hash,page_count,staged_page_count,status "
        "FROM portable_transfer_sessions WHERE session_id=? AND workspace_id=? "
        "AND direction='import'",
        (session_id, workspace_id),
    ).fetchone()
    if (
        row is None
        or int(row[2]) != int(row[3])
        or row[4]
        not in {
            "staging",
            "finalizing",
            "succeeded",
        }
    ):
        raise PortableTransferError("IMPORT_INVALID")
    manifest = parse_canonical_json(str(row[0]))
    if not isinstance(manifest, dict) or sha256_json(manifest) != row[1]:
        raise PortableTransferError("IMPORT_INVALID")
    directory = _session_dir(storage, "import", session_id)
    attempt_directory = directory / Path(current_lease.attempt_relative_path)
    attempt_directory.resolve().relative_to(directory.resolve())
    shutil.rmtree(attempt_directory, ignore_errors=True)
    attempt_directory.mkdir(parents=True, exist_ok=False)
    database_path = attempt_directory / "validated-events.db"
    available = _ensure_physical_capacity(
        connection,
        storage,
        workspace_id,
        "import",
        session_id,
        _now_us(clock()),
    )
    if available < _BACKUP_PROGRESS_BYTES:
        raise PortableTransferError("TASK_REQUIRED")
    staged = sqlite3.connect(database_path)
    page_size = int(staged.execute("PRAGMA page_size").fetchone()[0])
    staged.execute(f"PRAGMA max_page_count={max(1, available // page_size)}")
    staged.execute(
        "CREATE TABLE events(event_id TEXT PRIMARY KEY,event_hash TEXT UNIQUE,"
        "stream_id TEXT NOT NULL,stream_version INTEGER NOT NULL,recorded_at_us INTEGER NOT NULL,"
        "previous_event_hash TEXT,causation_event_id TEXT,memory_ref_a TEXT,memory_ref_b TEXT,"
        "event_json TEXT NOT NULL,"
        "processed INTEGER NOT NULL DEFAULT 0,UNIQUE(stream_id,stream_version))"
    )
    staged.execute(
        "CREATE INDEX ready_events ON events(processed,recorded_at_us,event_id)"
    )
    legacy_path: Path | None = None
    vectors_path: Path | None = None
    event_digest = hashlib.sha256()
    event_count = 0
    legacy_digest = hashlib.sha256()
    legacy_count = 0
    vector_digest = hashlib.sha256()
    vector_count = 0
    last_event_id: str | None = None
    try:
        pages = connection.execute(
            "SELECT page_index,page_kind,page_hash,relative_path "
            "FROM portable_transfer_pages "
            "WHERE session_id=? ORDER BY page_index",
            (session_id,),
        )
        actual_page_count = 0
        for expected_page_index, (page_index, kind, page_hash, relative) in enumerate(
            pages
        ):
            checkpoint()
            if int(page_index) != expected_page_index:
                raise PortableTransferError("IMPORT_INVALID")
            actual_page_count += 1
            content = _read_canonical(directory / str(relative), str(page_hash))
            items = content.get("items")
            if not isinstance(items, list):
                raise PortableTransferError("IMPORT_INVALID")
            if kind == "events":
                for public in items:
                    checkpoint()
                    if not isinstance(public, Mapping):
                        raise PortableTransferError("IMPORT_INVALID")
                    internal = dict(decode_event(public))
                    payload = internal.get("payload")
                    memory_refs: list[str] = []
                    if internal.get("stream_kind") == "fact" and isinstance(
                        payload, Mapping
                    ):
                        fact = payload.get("fact")
                        if isinstance(fact, Mapping) and isinstance(
                            fact.get("subject_record_id"), str
                        ):
                            memory_refs.append(str(fact["subject_record_id"]))
                    elif internal.get("stream_kind") == "relationship" and isinstance(
                        payload, Mapping
                    ):
                        relationship = payload.get("relationship")
                        if isinstance(relationship, Mapping):
                            memory_refs.extend(
                                str(value)
                                for value in (
                                    relationship.get("source_record_id"),
                                    relationship.get("target_record_id"),
                                )
                                if isinstance(value, str)
                            )
                    event_id = str(internal["event_id"])
                    if last_event_id is not None and event_id <= last_event_id:
                        raise PortableTransferError("IMPORT_INVALID")
                    last_event_id = event_id
                    event_digest.update(bytes.fromhex(str(internal["event_hash"])))
                    staged.execute(
                        "INSERT INTO events(event_id,event_hash,stream_id,"
                        "stream_version,recorded_at_us,previous_event_hash,causation_event_id,"
                        "memory_ref_a,memory_ref_b,event_json) VALUES (?,?,?,?,?,?,?,?,?,?)",
                        (
                            event_id,
                            str(internal["event_hash"]),
                            str(internal["stream_id"]),
                            int(internal["stream_version"]),
                            int(internal["recorded_at_us"]),
                            internal["previous_event_hash"],
                            internal["causation_event_id"],
                            memory_refs[0] if memory_refs else None,
                            memory_refs[1] if len(memory_refs) > 1 else None,
                            canonical_json_bytes(internal).decode("utf-8"),
                        ),
                    )
                    event_count += 1
            elif kind == "legacy":
                legacy_path = attempt_directory / "legacy.jsonl"
                with legacy_path.open("ab") as handle:
                    for item in items:
                        checkpoint()
                        encoded_item = canonical_json_bytes(item)
                        _ensure_physical_capacity(
                            connection,
                            storage,
                            workspace_id,
                            "import",
                            session_id,
                            _now_us(clock()),
                            additional_bytes=len(encoded_item) + 1,
                        )
                        legacy_digest.update(encoded_item)
                        legacy_count += 1
                        handle.write(encoded_item + b"\n")
            elif kind == "vectors":
                vectors_path = attempt_directory / "vectors.jsonl"
                with vectors_path.open("ab") as handle:
                    for item in items:
                        checkpoint()
                        encoded_item = canonical_json_bytes(item)
                        _ensure_physical_capacity(
                            connection,
                            storage,
                            workspace_id,
                            "import",
                            session_id,
                            _now_us(clock()),
                            additional_bytes=len(encoded_item) + 1,
                        )
                        vector_digest.update(encoded_item)
                        vector_count += 1
                        handle.write(encoded_item + b"\n")
            else:
                raise PortableTransferError("IMPORT_INVALID")
            _account_physical_files(
                connection,
                storage,
                workspace_id,
                "import",
                session_id,
                _now_us(clock()),
            )
            checkpoint()
        staged.commit()
        checkpoint(force_renew=True)
        _account_physical_files(
            connection,
            storage,
            workspace_id,
            "import",
            session_id,
            _now_us(clock()),
        )
        if actual_page_count != int(row[2]):
            raise PortableTransferError("IMPORT_INVALID")
        if event_count != int(
            manifest.get("event_count", -1)
        ) or event_digest.hexdigest() != manifest.get("event_root_hash"):
            raise PortableTransferError("IMPORT_INVALID")
        legacy_metadata = manifest.get("legacy_projection")
        if legacy_metadata is None:
            if legacy_count:
                raise PortableTransferError("IMPORT_INVALID")
        elif (
            not isinstance(legacy_metadata, Mapping)
            or legacy_count != legacy_metadata.get("row_count")
            or legacy_digest.hexdigest() != legacy_metadata.get("payload_hash")
        ):
            raise PortableTransferError("IMPORT_INVALID")
        vector_metadata = manifest.get("vectors")
        if vector_metadata is None:
            if vector_count:
                raise PortableTransferError("IMPORT_INVALID")
        elif (
            not isinstance(vector_metadata, Mapping)
            or vector_count != vector_metadata.get("row_count")
            or vector_digest.hexdigest() != vector_metadata.get("payload_hash")
        ):
            raise PortableTransferError("IMPORT_INVALID")
        previous_by_stream: dict[str, str | None] = {}
        version_by_stream: dict[str, int] = {}
        cursor = staged.execute(
            "SELECT stream_id,stream_version,previous_event_hash,event_hash "
            "FROM events ORDER BY stream_id,stream_version"
        )
        for stream_id, version, previous_hash, event_hash in cursor:
            checkpoint()
            wanted_version = version_by_stream.get(str(stream_id), 0) + 1
            if int(
                version
            ) != wanted_version or previous_hash != previous_by_stream.get(
                str(stream_id)
            ):
                raise PortableTransferError("IMPORT_INVALID")
            version_by_stream[str(stream_id)] = wanted_version
            previous_by_stream[str(stream_id)] = str(event_hash)
        missing = staged.execute(
            "SELECT 1 FROM events e LEFT JOIN events c "
            "ON c.event_id=e.causation_event_id "
            "WHERE e.causation_event_id IS NOT NULL AND c.event_id IS NULL LIMIT 1"
        ).fetchone()
        if missing is not None:
            raise PortableTransferError("IMPORT_INVALID")
        return PreparedImport(
            session_id,
            database_path,
            manifest,
            legacy_path,
            vectors_path,
            int(row[2]),
            current_lease,
        )
    except PortableTransferError:
        raise
    except sqlite3.Error as exc:
        if "full" in str(exc).casefold():
            raise PortableTransferError("TASK_REQUIRED") from exc
        raise PortableTransferError("IMPORT_INVALID") from exc
    except (ValueError, KeyError) as exc:
        staged.close()
        database_path.unlink(missing_ok=True)
        raise PortableTransferError("IMPORT_INVALID") from exc
    finally:
        with suppress(sqlite3.Error):
            staged.close()


def validate_legacy_projection(
    prepared: PreparedImport,
    connection: sqlite3.Connection,
    workspace_id: str,
) -> None:
    metadata = prepared.manifest.get("legacy_projection")
    if metadata is None:
        if prepared.legacy_path is not None:
            raise PortableTransferError("IMPORT_INVALID")
        return
    if prepared.legacy_path is None:
        raise PortableTransferError("IMPORT_INVALID")
    current_rows = _legacy_rows(connection, workspace_id)
    with prepared.legacy_path.open("rb") as imported:
        for current in current_rows:
            line = imported.readline()
            if not line:
                raise PortableTransferError("IMPORT_INVALID")
            wanted = parse_canonical_json(line.rstrip(b"\n").decode("utf-8"))
            if not isinstance(wanted, Mapping):
                raise PortableTransferError("IMPORT_INVALID")
            if (
                wanted.get("record_id") != current["record_id"]
                or wanted.get("record") != current["record"]
            ):
                raise PortableTransferError("IMPORT_INVALID")
            wanted_legacy = wanted.get("legacy_id")
            current_legacy = current.get("legacy_id")
            if current_legacy != wanted_legacy:
                raise PortableTransferError("IMPORT_INVALID")
        if imported.read(1):
            raise PortableTransferError("IMPORT_INVALID")
    digest = hashlib.sha256()
    count = 0
    with prepared.legacy_path.open("rb") as imported:
        for expected in _legacy_rows(connection, workspace_id):
            line = imported.readline()
            if not line or line.rstrip(b"\n") != canonical_json_bytes(expected):
                raise PortableTransferError("IMPORT_INVALID")
            digest.update(canonical_json_bytes(expected))
            count += 1
        if imported.read(1):
            raise PortableTransferError("IMPORT_INVALID")
    if (
        count != metadata.get("row_count")
        or digest.hexdigest() != metadata.get("payload_hash")
        or metadata.get("source_event_root_hash")
        != prepared.manifest.get("event_root_hash")
    ):
        raise PortableTransferError("IMPORT_INVALID")


def reconstruct_legacy_mappings(
    connection: sqlite3.Connection,
    workspace_id: str,
    *,
    event_root_hash: str,
    manifest_hash: str | None,
    now_us: int,
) -> int:
    """Rebuild migration compatibility maps solely from verified events.

    Portable bundles intentionally do not carry database-internal migration
    rows.  The lossless ``legacy`` object on an authoritative migration event
    contains everything needed to restore the map without trusting optional
    projection payloads.
    """

    mapped: list[tuple[str, str, str, str, str, str, str]] = []
    run_counts: dict[str, int] = {}
    cursor = connection.execute(
        "SELECT event_id,stream_id,stream_kind,event_type,correlation_id,payload_json "
        "FROM memory_events WHERE workspace_id=? AND actor_type='migration' "
        "AND event_type IN ('legacy.memory_state_imported','legacy.placeholder_created',"
        "'fact.asserted','relationship.created') ORDER BY event_id",
        (workspace_id,),
    )
    for row in cursor:
        run_id = row[4]
        if (
            not isinstance(run_id, str)
            or len(run_id) != 68
            or not run_id.startswith("mig_")
            or set(run_id[4:]) - set("0123456789abcdef")
        ):
            raise PortableTransferError("IMPORT_INVALID")
        try:
            payload = parse_canonical_json(str(row[5]))
        except ValueError as exc:
            raise PortableTransferError("IMPORT_INVALID") from exc
        legacy = payload.get("legacy") if isinstance(payload, Mapping) else None
        if not isinstance(legacy, Mapping):
            raise PortableTransferError("IMPORT_INVALID")
        event_type = str(row[3])
        stream_kind = str(row[2])
        if event_type == "legacy.placeholder_created":
            legacy_id = legacy.get("id")
            if (
                stream_kind != "memory"
                or legacy.get("table") != "memories"
                or legacy.get("missing") is not True
                or isinstance(legacy_id, bool)
                or not isinstance(legacy_id, (int, str))
            ):
                raise PortableTransferError("IMPORT_INVALID")
            source_table = "memory_relationships.orphan"
            legacy_text = f"memories:{legacy_id}"
            target_kind = "placeholder"
        else:
            expected = {
                "legacy.memory_state_imported": ("memory", "memories", "memory"),
                "fact.asserted": ("fact", "facts", "fact"),
                "relationship.created": (
                    "relationship",
                    "memory_relationships",
                    "relationship",
                ),
            }.get(event_type)
            if expected is None or stream_kind != expected[0]:
                raise PortableTransferError("IMPORT_INVALID")
            columns = legacy.get("columns")
            if legacy.get("table") != expected[1] or not isinstance(columns, list):
                raise PortableTransferError("IMPORT_INVALID")
            identifiers = [
                item[1]
                for item in columns
                if isinstance(item, list) and len(item) == 2 and item[0] == "id"
            ]
            if (
                len(identifiers) != 1
                or isinstance(identifiers[0], bool)
                or not isinstance(identifiers[0], (int, str))
            ):
                raise PortableTransferError("IMPORT_INVALID")
            source_table = expected[1]
            legacy_text = str(identifiers[0])
            target_kind = expected[2]
        mapped.append(
            (
                run_id,
                source_table,
                legacy_text,
                target_kind,
                str(row[1]),
                sha256_json(legacy),
                str(row[0]),
            )
        )
        run_counts[run_id] = run_counts.get(run_id, 0) + 1

    for run_id, count in sorted(run_counts.items()):
        existing = connection.execute(
            "SELECT workspace_id FROM v7_migration_runs WHERE migration_run_id=?",
            (run_id,),
        ).fetchone()
        if existing is not None:
            if str(existing[0]) != workspace_id:
                raise PortableTransferError("IMPORT_INVALID")
            continue
        provenance = {
            "event_root_hash": event_root_hash,
            "manifest_hash": manifest_hash,
            "mapping_count": count,
            "portable_derived": True,
        }
        source_hash = sha256_json(
            {
                "event_root_hash": event_root_hash,
                "migration_run_id": run_id,
            }
        )
        connection.execute(
            "INSERT INTO v7_migration_runs(migration_run_id,workspace_id,"
            "source_db_sha256,source_schema_version,source_format_version,"
            "target_format_version,status,snapshot_name,candidate_name,"
            "source_inventory_json,validation_json,last_error_json,created_at_us,"
            "updated_at_us,validated_at_us,activated_at_us,rolled_back_at_us) "
            "VALUES (?,?,?,7,7,7,'ready','portable-event-replay',"
            "'portable-event-replay',?,?,NULL,?,?,?,NULL,NULL)",
            (
                run_id,
                workspace_id,
                source_hash,
                canonical_json_bytes(provenance).decode("utf-8"),
                canonical_json_bytes(
                    {"event_provenance_validated": True, "mapping_count": count}
                ).decode("utf-8"),
                now_us,
                now_us,
                now_us,
            ),
        )

    for mapping in mapped:
        existing = connection.execute(
            "SELECT workspace_id,target_kind,target_id,source_row_hash,imported_event_id "
            "FROM legacy_id_map WHERE migration_run_id=? AND source_table=? AND legacy_id=?",
            mapping[:3],
        ).fetchone()
        expected_mapping = (workspace_id, *mapping[3:])
        if existing is not None:
            if tuple(existing) != expected_mapping:
                raise PortableTransferError("IMPORT_INVALID")
            continue
        connection.execute(
            "INSERT INTO legacy_id_map(migration_run_id,source_table,legacy_id,"
            "workspace_id,target_kind,target_id,source_row_hash,imported_event_id) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (mapping[0], mapping[1], mapping[2], workspace_id, *mapping[3:]),
        )
    return len(mapped)


def complete_import_session(
    connection: sqlite3.Connection,
    workspace_id: str,
    lease: ImportFinalizationLease,
    *,
    now: datetime,
) -> None:
    """Fence final publication to the current, unexpired lease owner."""

    now_us = _now_us(now)
    updated = connection.execute(
        "UPDATE portable_transfer_sessions SET status='succeeded',updated_at_us=?,"
        "completed_at_us=? WHERE session_id=? AND direction='import' "
        "AND workspace_id=? AND status='finalizing' AND EXISTS(SELECT 1 FROM "
        "portable_transfer_finalization_leases AS lease WHERE lease.session_id="
        "portable_transfer_sessions.session_id AND lease.owner_token=? "
        "AND lease.expires_at_us>?)",
        (
            now_us,
            now_us,
            lease.session_id,
            workspace_id,
            lease.owner_token,
            now_us,
        ),
    )
    if updated.rowcount != 1:
        raise PortableTransferError("TASK_REQUIRED")
    deleted = connection.execute(
        "DELETE FROM portable_transfer_finalization_leases "
        "WHERE session_id=? AND owner_token=? AND expires_at_us>?",
        (lease.session_id, lease.owner_token, now_us),
    )
    if deleted.rowcount != 1:
        raise PortableTransferError("TASK_REQUIRED")


__all__ = [
    "MAX_PAGE_ITEMS",
    "PAGE_BYTE_LIMIT",
    "PortableTransferError",
    "PreparedImport",
    "ImportFinalizationLease",
    "VectorCandidate",
    "activate_vector_candidate",
    "claim_import_finalization",
    "cleanup_import_attempt",
    "complete_import_session",
    "create_export_session",
    "prepare_import",
    "prepare_vector_candidate",
    "read_export_page",
    "release_import_finalization",
    "renew_import_finalization",
    "reconstruct_legacy_mappings",
    "stage_import_page",
    "validate_legacy_projection",
]
