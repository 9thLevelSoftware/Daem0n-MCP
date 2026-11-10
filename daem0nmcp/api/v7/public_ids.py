"""Workspace-scoped opaque identifiers for retained legacy objects.

The repository is an internal adapter boundary: integer-backed legacy keys are
accepted here, but only opaque identifiers are suitable for v7 wire models.
Projection-owned identifiers additionally bind to the active generation so a
rebuild can never silently retarget an identifier.
"""

from __future__ import annotations

import re
import sqlite3
import time
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from types import MappingProxyType

from ...event_store import sha256_json

_MAX_SIGNED_INTEGER = 2**63 - 1
_MAX_SOURCE_KEY_CHARS = 512
_WORKSPACE_ID_RE = re.compile(r"^ws_[0-9a-f]{24}$")
_CONTROL_CHARACTER_RE = re.compile(r"[\x00-\x1f\x7f]")


class PublicObjectKind(str, Enum):
    """Legacy object domains that require an opaque v7 identity."""

    RULE = "rule"
    TRIGGER = "trigger"
    ENTITY = "entity"
    ACTIVE_CONTEXT = "active_context"
    COMMUNITY = "community"
    CODE = "code"


PUBLIC_OBJECT_PREFIXES = MappingProxyType(
    {
        PublicObjectKind.RULE.value: "rule",
        PublicObjectKind.TRIGGER.value: "trg",
        PublicObjectKind.ENTITY.value: "ent",
        PublicObjectKind.ACTIVE_CONTEXT.value: "act",
        PublicObjectKind.COMMUNITY.value: "com",
        PublicObjectKind.CODE.value: "code",
    }
)
STABLE_PUBLIC_OBJECT_KINDS = frozenset(
    {
        PublicObjectKind.RULE.value,
        PublicObjectKind.TRIGGER.value,
        PublicObjectKind.ENTITY.value,
        PublicObjectKind.ACTIVE_CONTEXT.value,
    }
)
GENERATION_BOUND_PUBLIC_OBJECT_KINDS = frozenset(
    {PublicObjectKind.COMMUNITY.value, PublicObjectKind.CODE.value}
)

PublicIdFactory = Callable[[str, str, str, str, int], str]
SourceKey = int | str


class PublicObjectIdError(RuntimeError):
    """Base class for invariant public-object-ID failures."""

    code = "PUBLIC_OBJECT_ID_ERROR"

    def __init__(self) -> None:
        super().__init__(self.code)


class PublicObjectIdNotFound(PublicObjectIdError):  # noqa: N818 -- retained public Python API name
    """Invariant lookup failure that does not reveal another workspace."""

    code = "NOT_FOUND"


class StaleProjectionId(PublicObjectIdError):  # noqa: N818 -- retained public Python API name
    """The identifier belongs to a superseded projection generation."""

    code = "STALE_PROJECTION_ID"


class PublicObjectIdIntegrityError(PublicObjectIdError):
    """A deterministic ID collision or stored mapping ambiguity was found."""

    code = "PUBLIC_ID_INTEGRITY_ERROR"


@dataclass(frozen=True, slots=True)
class ResolvedPublicObjectId:
    """Internal source selection; this value must never cross the wire boundary."""

    public_id: str
    kind: str
    source_key: SourceKey
    projection_generation: int | None


def _normalize_workspace_id(workspace_id: str) -> str:
    if (
        not isinstance(workspace_id, str)
        or _WORKSPACE_ID_RE.fullmatch(workspace_id) is None
    ):
        raise ValueError("workspace_id must be an opaque v7 workspace identifier")
    return workspace_id


def _normalize_kind(kind: str | PublicObjectKind) -> str:
    if isinstance(kind, PublicObjectKind):
        return kind.value
    if not isinstance(kind, str) or kind not in PUBLIC_OBJECT_PREFIXES:
        raise ValueError("unsupported public object kind")
    return kind


def _normalize_source_key(source_key: SourceKey) -> tuple[SourceKey, str]:
    if isinstance(source_key, bool):
        raise ValueError("source_key must be a positive integer or bounded string")
    if isinstance(source_key, int):
        if source_key < 1 or source_key > _MAX_SIGNED_INTEGER:
            raise ValueError("integer source_key is outside the signed 64-bit range")
        return source_key, f"i:{source_key}"
    if not isinstance(source_key, str):
        raise ValueError("source_key must be a positive integer or bounded string")

    normalized = unicodedata.normalize(
        "NFC", source_key.replace("\r\n", "\n").replace("\r", "\n")
    )
    try:
        normalized.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ValueError("source_key must contain valid Unicode scalar values") from exc
    if (
        not normalized
        or len(normalized) > _MAX_SOURCE_KEY_CHARS
        or _CONTROL_CHARACTER_RE.search(normalized) is not None
    ):
        raise ValueError("string source_key must contain 1 to 512 safe characters")
    return normalized, f"s:{normalized}"


def _stored_generation(kind: str, projection_generation: int | None) -> int:
    if kind in STABLE_PUBLIC_OBJECT_KINDS:
        if projection_generation is not None:
            raise ValueError(
                "stable object kinds do not accept a projection generation"
            )
        return 0
    if (
        isinstance(projection_generation, bool)
        or not isinstance(projection_generation, int)
        or projection_generation < 1
        or projection_generation > _MAX_SIGNED_INTEGER
    ):
        raise ValueError("projection object kinds require a positive generation")
    return projection_generation


def _default_id_factory(
    prefix: str,
    workspace_id: str,
    kind: str,
    encoded_source_key: str,
    generation: int,
) -> str:
    digest = sha256_json(
        [
            "daem0nmcp",
            "v7",
            "public-object-id",
            workspace_id,
            kind,
            encoded_source_key,
            generation,
        ]
    )
    return f"{prefix}_{digest}"


def _validate_public_id(public_id: str, kind: str) -> str:
    prefix = PUBLIC_OBJECT_PREFIXES[kind]
    if (
        not isinstance(public_id, str)
        or re.fullmatch(rf"{re.escape(prefix)}_[0-9a-f]{{64}}", public_id) is None
    ):
        raise ValueError(f"public_id must use the canonical {prefix}_ prefix")
    return public_id


def derive_public_object_id(
    workspace_id: str,
    kind: str | PublicObjectKind,
    source_key: SourceKey,
    projection_generation: int | None = None,
) -> str:
    """Derive the canonical opaque identifier without persisting a mapping."""

    normalized_workspace = _normalize_workspace_id(workspace_id)
    normalized_kind = _normalize_kind(kind)
    _, encoded_source_key = _normalize_source_key(source_key)
    generation = _stored_generation(normalized_kind, projection_generation)
    return _default_id_factory(
        PUBLIC_OBJECT_PREFIXES[normalized_kind],
        normalized_workspace,
        normalized_kind,
        encoded_source_key,
        generation,
    )


class PublicObjectIdRepository:
    """Persist and resolve immutable opaque IDs on a caller-owned transaction."""

    def __init__(
        self,
        connection: sqlite3.Connection,
        *,
        clock_us: Callable[[], int] | None = None,
        id_factory: PublicIdFactory | None = None,
    ) -> None:
        if not callable(getattr(connection, "execute", None)):
            raise TypeError("connection must provide the SQLite execute interface")
        if clock_us is not None and not callable(clock_us):
            raise TypeError("clock_us must be callable")
        if id_factory is not None and not callable(id_factory):
            raise TypeError("id_factory must be callable")
        self._connection = connection
        self._clock_us = clock_us or (lambda: time.time_ns() // 1_000)
        self._id_factory = id_factory or _default_id_factory

    def _identity(
        self,
        workspace_id: str,
        kind: str | PublicObjectKind,
        source_key: SourceKey,
        projection_generation: int | None,
    ) -> tuple[str, str, SourceKey, str, int, str]:
        normalized_workspace = _normalize_workspace_id(workspace_id)
        normalized_kind = _normalize_kind(kind)
        normalized_source, encoded_source = _normalize_source_key(source_key)
        generation = _stored_generation(normalized_kind, projection_generation)
        public_id = self._id_factory(
            PUBLIC_OBJECT_PREFIXES[normalized_kind],
            normalized_workspace,
            normalized_kind,
            encoded_source,
            generation,
        )
        try:
            _validate_public_id(public_id, normalized_kind)
        except ValueError as exc:
            raise PublicObjectIdIntegrityError() from exc
        return (
            normalized_workspace,
            normalized_kind,
            normalized_source,
            encoded_source,
            generation,
            public_id,
        )

    def _source_rows(
        self,
        workspace_id: str,
        kind: str,
        encoded_source_key: str,
        generation: int,
    ) -> list[tuple[object, ...]]:
        return list(
            self._connection.execute(
                "SELECT source_key,projection_generation,public_id "
                "FROM public_object_ids WHERE workspace_id=? AND object_kind=? "
                "AND source_key=? AND projection_generation=? LIMIT 2",
                (workspace_id, kind, encoded_source_key, generation),
            )
        )

    def _verify_source_row(
        self,
        rows: list[tuple[object, ...]],
        *,
        encoded_source_key: str,
        generation: int,
        expected_public_id: str,
    ) -> str:
        if len(rows) != 1:
            raise PublicObjectIdIntegrityError()
        row_source_key, row_generation, row_public_id = rows[0]
        if (
            row_source_key != encoded_source_key
            or type(row_generation) is not int
            or row_generation != generation
            or row_public_id != expected_public_id
        ):
            raise PublicObjectIdIntegrityError()
        return expected_public_id

    def get_or_create(
        self,
        workspace_id: str,
        kind: str | PublicObjectKind,
        source_key: SourceKey,
        projection_generation: int | None = None,
    ) -> str:
        """Return one canonical ID, inserting its immutable mapping if absent."""

        (
            normalized_workspace,
            normalized_kind,
            _,
            encoded_source,
            generation,
            public_id,
        ) = self._identity(workspace_id, kind, source_key, projection_generation)
        rows = self._source_rows(
            normalized_workspace, normalized_kind, encoded_source, generation
        )
        if rows:
            return self._verify_source_row(
                rows,
                encoded_source_key=encoded_source,
                generation=generation,
                expected_public_id=public_id,
            )

        created_at_us = self._clock_us()
        if (
            isinstance(created_at_us, bool)
            or not isinstance(created_at_us, int)
            or created_at_us < 0
            or created_at_us > _MAX_SIGNED_INTEGER
        ):
            raise ValueError(
                "clock_us must return a non-negative signed 64-bit integer"
            )
        try:
            self._connection.execute(
                "INSERT INTO public_object_ids "
                "(workspace_id,object_kind,source_key,projection_generation,"
                "public_id,created_at_us) "
                "VALUES (?,?,?,?,?,?)",
                (
                    normalized_workspace,
                    normalized_kind,
                    encoded_source,
                    generation,
                    public_id,
                    created_at_us,
                ),
            )
        except sqlite3.IntegrityError as exc:
            # A concurrent identical insert is idempotent.  A different source
            # with the same public ID is a collision and must remain an error.
            rows = self._source_rows(
                normalized_workspace, normalized_kind, encoded_source, generation
            )
            if rows:
                return self._verify_source_row(
                    rows,
                    encoded_source_key=encoded_source,
                    generation=generation,
                    expected_public_id=public_id,
                )
            raise PublicObjectIdIntegrityError() from exc
        return public_id

    def public_id_for_source(
        self,
        workspace_id: str,
        kind: str | PublicObjectKind,
        source_key: SourceKey,
        projection_generation: int | None = None,
    ) -> str:
        """Resolve an existing source mapping without creating one."""

        (
            normalized_workspace,
            normalized_kind,
            _,
            encoded_source,
            generation,
            public_id,
        ) = self._identity(workspace_id, kind, source_key, projection_generation)
        rows = self._source_rows(
            normalized_workspace, normalized_kind, encoded_source, generation
        )
        if not rows:
            raise PublicObjectIdNotFound()
        return self._verify_source_row(
            rows,
            encoded_source_key=encoded_source,
            generation=generation,
            expected_public_id=public_id,
        )

    def resolve_public_id(
        self,
        workspace_id: str,
        kind: str | PublicObjectKind,
        public_id: str,
        active_generation: int | None = None,
    ) -> ResolvedPublicObjectId:
        """Resolve an opaque ID inside one workspace and active generation."""

        normalized_workspace = _normalize_workspace_id(workspace_id)
        normalized_kind = _normalize_kind(kind)
        _validate_public_id(public_id, normalized_kind)
        required_generation = _stored_generation(normalized_kind, active_generation)
        rows = list(
            self._connection.execute(
                "SELECT source_key,projection_generation,public_id "
                "FROM public_object_ids WHERE workspace_id=? AND object_kind=? "
                "AND public_id=? LIMIT 2",
                (normalized_workspace, normalized_kind, public_id),
            )
        )
        if not rows:
            raise PublicObjectIdNotFound()
        if len(rows) != 1:
            raise PublicObjectIdIntegrityError()

        encoded_source, stored_generation, stored_public_id = rows[0]
        if (
            not isinstance(encoded_source, str)
            or type(stored_generation) is not int
            or stored_public_id != public_id
        ):
            raise PublicObjectIdIntegrityError()
        try:
            decoded_source = _decode_source_key(encoded_source)
        except ValueError as exc:
            raise PublicObjectIdIntegrityError() from exc

        external_generation = (
            None if normalized_kind in STABLE_PUBLIC_OBJECT_KINDS else stored_generation
        )
        expected_public_id = self._identity(
            normalized_workspace,
            normalized_kind,
            decoded_source,
            external_generation,
        )[-1]
        if expected_public_id != public_id:
            raise PublicObjectIdIntegrityError()
        if (
            normalized_kind in GENERATION_BOUND_PUBLIC_OBJECT_KINDS
            and stored_generation != required_generation
        ):
            raise StaleProjectionId()
        if normalized_kind in STABLE_PUBLIC_OBJECT_KINDS and stored_generation != 0:
            raise PublicObjectIdIntegrityError()
        return ResolvedPublicObjectId(
            public_id=public_id,
            kind=normalized_kind,
            source_key=decoded_source,
            projection_generation=external_generation,
        )


def _decode_source_key(encoded_source_key: str) -> SourceKey:
    if encoded_source_key.startswith("i:"):
        raw_value = encoded_source_key[2:]
        if not raw_value or not raw_value.isascii() or not raw_value.isdecimal():
            raise ValueError("invalid stored integer source key")
        value = int(raw_value)
        normalized, encoded = _normalize_source_key(value)
    elif encoded_source_key.startswith("s:"):
        normalized, encoded = _normalize_source_key(encoded_source_key[2:])
    else:
        raise ValueError("invalid stored source key tag")
    if encoded != encoded_source_key:
        raise ValueError("stored source key is not canonical")
    return normalized


__all__ = [
    "GENERATION_BOUND_PUBLIC_OBJECT_KINDS",
    "PUBLIC_OBJECT_PREFIXES",
    "STABLE_PUBLIC_OBJECT_KINDS",
    "PublicObjectIdIntegrityError",
    "PublicObjectIdNotFound",
    "PublicObjectIdRepository",
    "PublicObjectKind",
    "ResolvedPublicObjectId",
    "StaleProjectionId",
    "derive_public_object_id",
]
