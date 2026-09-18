"""Build and atomically activate rebuildable retrieval projections."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import time
from collections.abc import Callable
from dataclasses import dataclass

from ..event_store import canonical_json_bytes, deterministic_id, sha256_json
from .lexical_config import (
    LEXICAL_TOKENIZER,
    lexical_build_config_hash,
    lexical_fts_table_name,
)

_WORKSPACE_ID = re.compile(r"^ws_[0-9a-f]{24}$")
_LEXICAL_BUILDER_VERSION = "retrieval-lexical-1"


class ProjectionBuildError(RuntimeError):
    """A staging projection could not be validated or activated."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(f"{code}: {message}")


@dataclass(frozen=True, slots=True)
class ProjectionBuildResult:
    """Sanitized result of a projection build or dry-run inventory."""

    projection_name: str
    generation: int
    status: str
    row_count: int
    source_event_count: int
    source_event_root_hash: str
    source_high_water_recorded_at_us: int | None
    source_high_water_event_id: str | None
    content_digest: str
    build_config_hash: str
    dry_run: bool = False
    capability_status: str = "ready"
    active_manifest_id: str | None = None
    active_generation: int | None = None
    active_status: str | None = None
    active_row_count: int = 0
    row_count_delta: int = 0
    active_content_digest: str | None = None
    content_digest_changed: bool = True
    staging_manifest_id: str | None = None
    storage_target: str | None = None


@dataclass(frozen=True, slots=True)
class _LexicalRecord:
    record_id: str
    content: str
    rationale: str
    tags_text: str
    category: str
    valid_from_us: int | None
    valid_to_us: int | None
    transaction_from_us: int
    transaction_to_us: int | None
    visibility: str
    archived: int
    content_hash: str
    source_event_id: str


class LexicalProjectionBuilder:
    """Build a new FTS5 generation from canonical v7 memory records."""

    def __init__(
        self,
        connection: sqlite3.Connection,
        *,
        clock_us: Callable[[], int] | None = None,
    ) -> None:
        self.connection = connection
        self._clock_us = clock_us or (lambda: time.time_ns() // 1_000)
        self._savepoint_number = 0

    def rebuild(
        self,
        workspace_id: str,
        *,
        dry_run: bool = False,
    ) -> ProjectionBuildResult:
        """Build, validate, and atomically switch one lexical generation."""

        if (
            not isinstance(workspace_id, str)
            or _WORKSPACE_ID.fullmatch(workspace_id) is None
        ):
            raise ProjectionBuildError(
                "INVALID_WORKSPACE_ID", "workspace identifier is invalid"
            )
        self._require_schema()
        owns_transaction = not self.connection.in_transaction
        self._savepoint_number += 1
        savepoint = f"retrieval_build_{self._savepoint_number}"
        try:
            if owns_transaction:
                self.connection.execute("BEGIN IMMEDIATE")
            else:
                self.connection.execute(f"SAVEPOINT {savepoint}")
            capability_status = "ready"
            try:
                self._probe_fts5()
            except sqlite3.Error:
                if not dry_run:
                    raise
                capability_status = "unavailable"
            records = self._records(workspace_id)
            source_event_count, source_root, cursor = self._event_snapshot(workspace_id)
            active = self._active_manifest(workspace_id)
            generation = (
                int(
                    self.connection.execute(
                        "SELECT COALESCE(MAX(generation),0) "
                        "FROM projection_manifests WHERE workspace_id=? "
                        "AND projection_name='lexical'",
                        (workspace_id,),
                    ).fetchone()[0]
                )
                + 1
            )
            fts_table = lexical_fts_table_name(workspace_id, generation)
            content_digest = sha256_json(
                [
                    {
                        "content_hash": record.content_hash,
                        "record_id": record.record_id,
                    }
                    for record in records
                ]
            )
            build_config_hash = lexical_build_config_hash()
            manifest_id = deterministic_id(
                "prj",
                "projection",
                workspace_id,
                "lexical",
                generation,
                source_root,
            )
            active_manifest_id = active[0] if active is not None else None
            active_generation = active[1] if active is not None else None
            active_status = active[2] if active is not None else None
            active_row_count = active[3] if active is not None else 0
            active_content_digest = active[4] if active is not None else None
            result = ProjectionBuildResult(
                projection_name="lexical",
                generation=generation,
                status=(
                    "unavailable"
                    if capability_status == "unavailable"
                    else "ready"
                    if dry_run
                    else "active"
                ),
                row_count=len(records),
                source_event_count=source_event_count,
                source_event_root_hash=source_root,
                source_high_water_recorded_at_us=(
                    cursor[0] if cursor is not None else None
                ),
                source_high_water_event_id=(cursor[1] if cursor is not None else None),
                content_digest=content_digest,
                build_config_hash=build_config_hash,
                dry_run=dry_run,
                capability_status=capability_status,
                active_manifest_id=active_manifest_id,
                active_generation=active_generation,
                active_status=active_status,
                active_row_count=active_row_count,
                row_count_delta=len(records) - active_row_count,
                active_content_digest=active_content_digest,
                content_digest_changed=active_content_digest != content_digest,
                staging_manifest_id=manifest_id,
                storage_target=fts_table,
            )
            if dry_run:
                if owns_transaction:
                    self.connection.rollback()
                else:
                    self.connection.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
                    self.connection.execute(f"RELEASE SAVEPOINT {savepoint}")
                return result

            now = self._clock_value()
            details = canonical_json_bytes(
                {
                    "build_config_hash": build_config_hash,
                    "content_digest": content_digest,
                    "fts_table": fts_table,
                    "projection": "lexical",
                }
            ).decode("utf-8")
            self.connection.execute(
                """
                INSERT INTO projection_manifests (
                    manifest_id, workspace_id, projection_name, generation,
                    projection_version, status, source_event_count,
                    source_event_root_hash, cursor_recorded_at_us,
                    cursor_event_id, row_count, builder_version, details_json,
                    started_at_us, completed_at_us, activated_at_us
                ) VALUES (?, ?, 'lexical', ?, 1, 'building', ?, ?, ?, ?, 0, ?, ?, ?, NULL, NULL)
                """,
                (
                    manifest_id,
                    workspace_id,
                    generation,
                    source_event_count,
                    source_root,
                    cursor[0] if cursor is not None else None,
                    cursor[1] if cursor is not None else None,
                    _LEXICAL_BUILDER_VERSION,
                    details,
                    now,
                ),
            )
            self.connection.executemany(
                """
                INSERT INTO retrieval_documents (
                    workspace_id, projection_generation, record_id, content,
                    rationale, tags_text, category, valid_from_us, valid_to_us,
                    transaction_from_us, transaction_to_us, visibility,
                    archived, content_hash, source_event_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    self._document_values(workspace_id, generation, record)
                    for record in records
                ],
            )
            self.connection.execute(
                f'CREATE VIRTUAL TABLE "{fts_table}" USING fts5('
                "content, rationale, tags_text, "
                f"tokenize='{LEXICAL_TOKENIZER}')"
            )
            self._populate_fts(fts_table, workspace_id, generation)
            self._validate_staging(
                fts_table,
                workspace_id,
                generation,
                expected_count=len(records),
                expected_digest=content_digest,
            )
            self.connection.execute(
                "UPDATE projection_manifests SET status='ready' "
                "WHERE workspace_id=? AND projection_name='lexical' "
                "AND status='active'",
                (workspace_id,),
            )
            changed = self.connection.execute(
                """
                UPDATE projection_manifests
                SET status='active', row_count=?, completed_at_us=?, activated_at_us=?
                WHERE manifest_id=? AND status='building'
                """,
                (len(records), now, now, manifest_id),
            ).rowcount
            if changed != 1:
                raise ProjectionBuildError(
                    "PROJECTION_ACTIVATION_FAILED",
                    "staging lexical manifest is unavailable",
                )
            if owns_transaction:
                self.connection.commit()
            else:
                self.connection.execute(f"RELEASE SAVEPOINT {savepoint}")
        except Exception as exc:
            if owns_transaction:
                self.connection.rollback()
            else:
                self.connection.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
                self.connection.execute(f"RELEASE SAVEPOINT {savepoint}")
            if isinstance(exc, ProjectionBuildError):
                raise
            if isinstance(exc, sqlite3.Error):
                raise ProjectionBuildError(
                    "LEXICAL_UNAVAILABLE", "FTS5 projection build is unavailable"
                ) from exc
            raise
        return result

    def apply_active_delta(self, workspace_id: str, record_id: str) -> bool:
        """Update one active lexical partition inside its canonical write.

        The active manifest deliberately remains marked stale by the event
        store.  This delta only makes the changed record discoverable while
        the durable full-generation rebuild catches up.
        """

        if (
            not isinstance(workspace_id, str)
            or _WORKSPACE_ID.fullmatch(workspace_id) is None
            or not isinstance(record_id, str)
            or not record_id.startswith("mem_")
            or len(record_id) != 68
        ):
            raise ProjectionBuildError(
                "INVALID_RECORD_ID", "lexical delta identity is invalid"
            )
        self._require_schema()
        row = self.connection.execute(
            "SELECT manifest_id,generation,details_json FROM projection_manifests "
            "WHERE workspace_id=? AND projection_name='lexical' "
            "AND status='active'",
            (workspace_id,),
        ).fetchone()
        if row is None:
            return False
        generation = int(row[1])
        try:
            details = json.loads(str(row[2]))
        except (TypeError, ValueError, json.JSONDecodeError, RecursionError):
            return False
        expected_table = lexical_fts_table_name(workspace_id, generation)
        if (
            not isinstance(details, dict)
            or details.get("projection") != "lexical"
            or details.get("build_config_hash") != lexical_build_config_hash()
            or details.get("fts_table") != expected_table
            or self.connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                (expected_table,),
            ).fetchone()
            is None
        ):
            return False

        record = self._record(workspace_id, record_id)
        self._savepoint_number += 1
        savepoint = f"retrieval_delta_{self._savepoint_number}"
        self.connection.execute(f"SAVEPOINT {savepoint}")
        try:
            existing = self.connection.execute(
                "SELECT document_rowid FROM retrieval_documents "
                "WHERE workspace_id=? AND projection_generation=? AND record_id=?",
                (workspace_id, generation, record_id),
            ).fetchone()
            document_rowid = None if existing is None else int(existing[0])
            if document_rowid is not None:
                self.connection.execute(
                    f'DELETE FROM "{expected_table}" WHERE rowid=?',
                    (document_rowid,),
                )
            if record is None:
                self.connection.execute(
                    "DELETE FROM retrieval_documents WHERE workspace_id=? "
                    "AND projection_generation=? AND record_id=?",
                    (workspace_id, generation, record_id),
                )
            else:
                values = self._document_values(workspace_id, generation, record)
                if document_rowid is None:
                    inserted = self.connection.execute(
                        """
                        INSERT INTO retrieval_documents (
                            workspace_id,projection_generation,record_id,content,
                            rationale,tags_text,category,valid_from_us,valid_to_us,
                            transaction_from_us,transaction_to_us,visibility,
                            archived,content_hash,source_event_id
                        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                        """,
                        values,
                    )
                    document_rowid = int(inserted.lastrowid)
                else:
                    self.connection.execute(
                        """
                        UPDATE retrieval_documents SET
                            content=?,rationale=?,tags_text=?,category=?,
                            valid_from_us=?,valid_to_us=?,transaction_from_us=?,
                            transaction_to_us=?,visibility=?,archived=?,
                            content_hash=?,source_event_id=?
                        WHERE document_rowid=?
                        """,
                        (*values[3:], document_rowid),
                    )
                self.connection.execute(
                    f'INSERT INTO "{expected_table}"('
                    "rowid,content,rationale,tags_text) VALUES (?,?,?,?)",
                    (
                        document_rowid,
                        record.content,
                        record.rationale,
                        record.tags_text,
                    ),
                )
                projected = self.connection.execute(
                    f'SELECT content,rationale,tags_text FROM "{expected_table}" '
                    "WHERE rowid=?",
                    (document_rowid,),
                ).fetchone()
                if projected is None or tuple(projected) != (
                    record.content,
                    record.rationale,
                    record.tags_text,
                ):
                    raise ProjectionBuildError(
                        "PROJECTION_VALIDATION_FAILED",
                        "lexical delta differs from canonical content",
                    )
            row_count = int(
                self.connection.execute(
                    "SELECT count(*) FROM retrieval_documents WHERE workspace_id=? "
                    "AND projection_generation=?",
                    (workspace_id, generation),
                ).fetchone()[0]
            )
            changed = self.connection.execute(
                "UPDATE projection_manifests SET row_count=? WHERE manifest_id=? "
                "AND status='active' AND generation=?",
                (row_count, str(row[0]), generation),
            ).rowcount
            if changed != 1:
                raise ProjectionBuildError(
                    "PROJECTION_ACTIVATION_FAILED",
                    "active lexical generation changed during delta",
                )
            self.connection.execute(f"RELEASE SAVEPOINT {savepoint}")
        except Exception as exc:
            self.connection.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
            self.connection.execute(f"RELEASE SAVEPOINT {savepoint}")
            if isinstance(exc, ProjectionBuildError):
                raise
            if isinstance(exc, sqlite3.Error):
                raise ProjectionBuildError(
                    "LEXICAL_UNAVAILABLE", "active lexical delta failed"
                ) from exc
            raise
        return True

    def active_is_current(self, workspace_id: str) -> bool:
        """Return whether the active partition exactly matches canonical state."""

        if (
            not isinstance(workspace_id, str)
            or _WORKSPACE_ID.fullmatch(workspace_id) is None
        ):
            raise ProjectionBuildError(
                "INVALID_WORKSPACE_ID", "workspace identifier is invalid"
            )
        self._require_schema()
        self._probe_fts5()
        row = self.connection.execute(
            "SELECT generation,row_count,source_event_count,"
            "source_event_root_hash,details_json FROM projection_manifests "
            "WHERE workspace_id=? AND projection_name='lexical' "
            "AND status='active' ORDER BY generation DESC LIMIT 1",
            (workspace_id,),
        ).fetchone()
        if row is None:
            return False
        generation = int(row[0])
        records = self._records(workspace_id)
        event_count, event_root, _ = self._event_snapshot(workspace_id)
        content_digest = sha256_json(
            [
                {
                    "content_hash": record.content_hash,
                    "record_id": record.record_id,
                }
                for record in records
            ]
        )
        fts_table = lexical_fts_table_name(workspace_id, generation)
        try:
            details = json.loads(str(row[4]))
        except (TypeError, ValueError, json.JSONDecodeError, RecursionError):
            return False
        if not isinstance(details, dict) or details != {
            "build_config_hash": lexical_build_config_hash(),
            "content_digest": content_digest,
            "fts_table": fts_table,
            "projection": "lexical",
        }:
            return False
        if (
            int(row[1]) != len(records)
            or int(row[2]) != event_count
            or str(row[3]) != event_root
        ):
            return False
        try:
            self._validate_staging(
                fts_table,
                workspace_id,
                generation,
                expected_count=len(records),
                expected_digest=content_digest,
            )
        except (ProjectionBuildError, sqlite3.Error):
            return False
        return True

    def _require_schema(self) -> None:
        names = {
            str(row[0])
            for row in self.connection.execute(
                "SELECT name FROM sqlite_master WHERE name IN "
                "('memory_events','memory_records','projection_manifests',"
                "'retrieval_documents')"
            )
        }
        if names != {
            "memory_events",
            "memory_records",
            "projection_manifests",
            "retrieval_documents",
        }:
            raise ProjectionBuildError(
                "LEXICAL_UNAVAILABLE", "FTS5 projection schema is unavailable"
            )

    def _probe_fts5(self) -> None:
        row = self.connection.execute("SELECT fts5_source_id()").fetchone()
        if row is None or not isinstance(row[0], str) or not row[0]:
            raise sqlite3.OperationalError("FTS5 capability is unavailable")

    def _active_manifest(
        self, workspace_id: str
    ) -> tuple[str, int, str, int, str | None] | None:
        row = self.connection.execute(
            "SELECT manifest_id,generation,status,row_count,details_json "
            "FROM projection_manifests WHERE workspace_id=? "
            "AND projection_name='lexical' AND status='active' "
            "ORDER BY generation DESC LIMIT 1",
            (workspace_id,),
        ).fetchone()
        if row is None:
            return None
        content_digest = None
        try:
            details = json.loads(str(row[4]))
            candidate = details.get("content_digest")
            if (
                isinstance(candidate, str)
                and len(candidate) == 64
                and not set(candidate).difference("0123456789abcdef")
            ):
                content_digest = candidate
        except (AttributeError, TypeError, ValueError, json.JSONDecodeError):
            pass
        return (
            str(row[0]),
            int(row[1]),
            str(row[2]),
            int(row[3]),
            content_digest,
        )

    def _populate_fts(
        self,
        fts_table: str,
        workspace_id: str,
        generation: int,
    ) -> None:
        self.connection.execute(
            f'INSERT INTO "{fts_table}"(rowid,content,rationale,tags_text) '
            "SELECT document_rowid,content,rationale,tags_text "
            "FROM retrieval_documents WHERE workspace_id=? "
            "AND projection_generation=? ORDER BY document_rowid",
            (workspace_id, generation),
        )

    def _validate_staging(
        self,
        fts_table: str,
        workspace_id: str,
        generation: int,
        *,
        expected_count: int,
        expected_digest: str,
    ) -> None:
        projected_rows = tuple(
            self.connection.execute(
                "SELECT document_rowid,record_id,content_hash "
                "FROM retrieval_documents WHERE workspace_id=? "
                "AND projection_generation=? ORDER BY record_id",
                (workspace_id, generation),
            )
        )
        projected_digest = sha256_json(
            [
                {"content_hash": str(row[2]), "record_id": str(row[1])}
                for row in projected_rows
            ]
        )
        projected_rowids = tuple(sorted(int(row[0]) for row in projected_rows))
        fts_rowids = tuple(
            int(row[0])
            for row in self.connection.execute(
                f'SELECT rowid FROM "{fts_table}" ORDER BY rowid'
            )
        )
        if (
            len(projected_rows) != expected_count
            or len(fts_rowids) != expected_count
            or projected_digest != expected_digest
            or fts_rowids != projected_rowids
        ):
            raise ProjectionBuildError(
                "PROJECTION_VALIDATION_FAILED",
                "lexical projection content or row identity differs",
            )

    def _records(self, workspace_id: str) -> tuple[_LexicalRecord, ...]:
        records: list[_LexicalRecord] = []
        rows = self.connection.execute(
            """
            SELECT record_id,content,rationale,tags_json,record_type,context_json,
                   archived,content_hash,source_event_id,created_at_us,
                   updated_at_us,deleted_at_us
            FROM memory_records
            WHERE workspace_id=? AND deleted_at_us IS NULL
            ORDER BY record_id
            """,
            (workspace_id,),
        )
        try:
            for row in rows:
                records.append(self._record_from_row(row))
        except (TypeError, ValueError, json.JSONDecodeError, RecursionError) as exc:
            raise ProjectionBuildError(
                "INVALID_RECORD_METADATA", "record projection metadata is invalid"
            ) from exc
        return tuple(records)

    def _record(self, workspace_id: str, record_id: str) -> _LexicalRecord | None:
        row = self.connection.execute(
            """
            SELECT record_id,content,rationale,tags_json,record_type,context_json,
                   archived,content_hash,source_event_id,created_at_us,
                   updated_at_us,deleted_at_us
            FROM memory_records WHERE workspace_id=? AND record_id=?
            """,
            (workspace_id, record_id),
        ).fetchone()
        if row is None or row[11] is not None:
            return None
        try:
            return self._record_from_row(row)
        except (TypeError, ValueError, json.JSONDecodeError, RecursionError) as exc:
            raise ProjectionBuildError(
                "INVALID_RECORD_METADATA", "record projection metadata is invalid"
            ) from exc

    @staticmethod
    def _record_from_row(row: sqlite3.Row | tuple[object, ...]) -> _LexicalRecord:
        tags = json.loads(str(row[3]))
        context = json.loads(str(row[5]))
        if not isinstance(tags, list) or not isinstance(context, dict):
            raise ValueError
        tag_values = [
            value
            if isinstance(value, str)
            else canonical_json_bytes(value).decode("utf-8")
            for value in tags
        ]
        visibility = context.get("visibility", "workspace")
        if visibility not in {"workspace", "private", "shared"}:
            raise ProjectionBuildError(
                "INVALID_RECORD_VISIBILITY",
                "record visibility is not a supported policy value",
            )
        return _LexicalRecord(
            record_id=str(row[0]),
            content=str(row[1]),
            rationale=str(row[2] or ""),
            tags_text="\n".join(tag_values),
            category=str(row[4]),
            valid_from_us=int(row[9]),
            valid_to_us=None,
            transaction_from_us=int(row[10]),
            transaction_to_us=None,
            visibility=str(visibility),
            archived=int(row[6]),
            content_hash=str(row[7]),
            source_event_id=str(row[8]),
        )

    @staticmethod
    def _document_values(
        workspace_id: str,
        generation: int,
        record: _LexicalRecord,
    ) -> tuple[object, ...]:
        return (
            workspace_id,
            generation,
            record.record_id,
            record.content,
            record.rationale,
            record.tags_text,
            record.category,
            record.valid_from_us,
            record.valid_to_us,
            record.transaction_from_us,
            record.transaction_to_us,
            record.visibility,
            record.archived,
            record.content_hash,
            record.source_event_id,
        )

    def _event_snapshot(
        self, workspace_id: str
    ) -> tuple[int, str, tuple[int, str] | None]:
        digest = hashlib.sha256()
        count = 0
        for row in self.connection.execute(
            "SELECT event_hash FROM memory_events WHERE workspace_id=? ORDER BY event_id",
            (workspace_id,),
        ):
            try:
                digest.update(bytes.fromhex(str(row[0])))
            except ValueError as exc:
                raise ProjectionBuildError(
                    "INVALID_EVENT_ROOT", "event hash is malformed"
                ) from exc
            count += 1
        cursor_row = self.connection.execute(
            "SELECT recorded_at_us,event_id FROM memory_events "
            "WHERE workspace_id=? ORDER BY recorded_at_us DESC,event_id DESC LIMIT 1",
            (workspace_id,),
        ).fetchone()
        cursor = (
            None if cursor_row is None else (int(cursor_row[0]), str(cursor_row[1]))
        )
        return count, digest.hexdigest(), cursor

    def _clock_value(self) -> int:
        value = self._clock_us()
        if isinstance(value, bool) or not isinstance(value, int):
            raise ProjectionBuildError(
                "INVALID_CLOCK", "projection clock must return integer microseconds"
            )
        return value


__all__ = [
    "LexicalProjectionBuilder",
    "ProjectionBuildError",
    "ProjectionBuildResult",
]
