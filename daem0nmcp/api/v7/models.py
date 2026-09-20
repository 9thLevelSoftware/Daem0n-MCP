"""Strict, framework-independent wire models for the v7 MCP API.

This module is the common serialization boundary shared by tools, resources,
and task results.  It intentionally imports neither FastMCP nor optional
retrieval/storage integrations.
"""

from __future__ import annotations

import json
import posixpath
import re
from collections.abc import Iterator, Mapping
from datetime import datetime, timezone
from functools import cache
from typing import (
    TYPE_CHECKING,
    Annotated,
    Any,
    Generic,
    Literal,
    TypeAlias,
    TypeVar,
    get_args,
)

from pydantic import (
    AfterValidator,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    StringConstraints,
    ValidationError,
    model_validator,
)
from typing_extensions import Self, TypeAliasType

from .errors import INTERNAL_ERROR_MESSAGE, ErrorCode

MAX_JSON_COLLECTION_ITEMS = 4096
MAX_JSON_KEY_CHARS = 256
MAX_JSON_STRING_CHARS = 1_048_576
MAX_CONTEXT_JSON_BYTES = 65_536
MAX_PAGE_ITEMS = 500

_RFC3339 = re.compile(
    r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T"
    r"[0-9]{2}:[0-9]{2}:[0-9]{2}"
    r"(?:\.[0-9]{1,9})?(?:Z|[+-][0-9]{2}:[0-9]{2})$"
)
_WINDOWS_DRIVE = re.compile(r"^[A-Za-z]:")
_WINDOWS_ABSOLUTE_PATH = re.compile(
    r"(?<![A-Za-z0-9])(?:[A-Za-z]:[\\/]|\\\\[^\s\\/]+[\\/])"
)
_POSIX_ABSOLUTE_PATH = re.compile(r"(?<![A-Za-z0-9:/])/(?!/)[A-Za-z0-9_.-]")
_FILE_URI = re.compile(r"(?i)\bfile:(?://)?/")
_CONTROL_CHAR = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def is_host_absolute_path(value: str) -> bool:
    """Return whether text contains an absolute host path or file URI.

    This is the only absolute-path predicate on the v7 wire.  ``WireModel``
    applies it to every string except ``UserText`` fields.
    """

    return bool(
        _WINDOWS_ABSOLUTE_PATH.search(value)
        or _POSIX_ABSOLUTE_PATH.search(value)
        or _FILE_URI.search(value)
    )


class _Exempt:
    """``Annotated`` marker for strings the substring path scan skips."""

    __slots__ = ("name",)

    def __init__(self, name: str) -> None:
        self.name = name

    def __repr__(self) -> str:
        return self.name


_USER_TEXT = _Exempt("UserText")
# RelativePath's own validator refuses absolute and escaping paths exactly;
# the prose scan would also refuse valid names such as ``src/__pycache__/x``.
_RELATIVE_PATH = _Exempt("RelativePath")

# Memory content, queries, rule text and server text quoting them may mention
# routes and file paths ("/health", "C:\proj\app.py").  Fields typed with it,
# alone or inside a list, set or dict, are exempt from the absolute-path rule in
# both directions, so anything accepted on input can always be read back.
UserText = Annotated[str, _USER_TEXT]


def _carries(annotation: object, marker: _Exempt) -> bool:
    return annotation is marker or any(
        _carries(argument, marker) for argument in get_args(annotation)
    )


def _fields_marked(model: type[BaseModel], marker: _Exempt) -> frozenset[str]:
    return frozenset(
        name
        for name, field in model.model_fields.items()
        if marker in field.metadata or _carries(field.annotation, marker)
    )


@cache
def _user_text_fields(model: type[BaseModel]) -> frozenset[str]:
    return _fields_marked(model, _USER_TEXT)


@cache
def _exempt_fields(model: type[BaseModel]) -> frozenset[str]:
    return _user_text_fields(model) | _fields_marked(model, _RELATIVE_PATH)


def guarded_strings(value: object, user_text: bool = False) -> Iterator[str]:
    """Yield every string, keys included, that the absolute-path rule covers.

    Nested models are walked with their own field annotations, so a model
    inside a ``UserText`` container is still checked.
    """

    if isinstance(value, BaseModel):
        exempt = _exempt_fields(type(value))
        for name, item in value.__dict__.items():
            yield from guarded_strings(item, name in exempt)
    elif isinstance(value, Mapping):
        for key, item in value.items():
            yield from guarded_strings(key, user_text)
            yield from guarded_strings(item, user_text)
    elif isinstance(value, (list, tuple, set, frozenset)):
        for item in value:
            yield from guarded_strings(item, user_text)
    elif isinstance(value, str) and not user_text:
        yield value


def contains_absolute_filesystem_path(value: object) -> bool:
    """Return whether a nested value has a guarded string with a host path."""

    return any(is_host_absolute_path(text) for text in guarded_strings(value))


def _relative_path(value: str) -> str:
    if value == ".":
        return value
    if (
        not value
        or "\\" in value
        or "\x00" in value
        or value.startswith(("/", "~"))
        or _WINDOWS_DRIVE.match(value) is not None
    ):
        raise ValueError("path must be a normalized workspace-relative POSIX path")
    components = value.split("/")
    if any(component in {"", ".", ".."} for component in components):
        raise ValueError("path must be a normalized workspace-relative POSIX path")
    if posixpath.normpath(value) != value:
        raise ValueError("path must be a normalized workspace-relative POSIX path")
    return value


def parse_wire_datetime(value: object) -> datetime:
    """Restore an aware schema timestamp from normalized MCP JSON arguments."""
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("timestamp must include an RFC 3339 offset")
        return value
    if not isinstance(value, str) or _RFC3339.fullmatch(value) is None:
        raise ValueError("timestamp must be an RFC 3339 date-time with an offset")
    try:
        parsed = datetime.fromisoformat(
            value[:-1] + "+00:00" if value.endswith("Z") else value
        )
    except ValueError as exc:
        raise ValueError("timestamp must be a valid RFC 3339 date-time") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("timestamp must include an RFC 3339 offset")
    return parsed


def _utc(value: datetime) -> datetime:
    if value.utcoffset() != timezone.utc.utcoffset(value):
        raise ValueError("timestamp must use a UTC offset")
    return value


def _sanitized(value: str) -> str:
    if _CONTROL_CHAR.search(value) is not None:
        raise ValueError("text contains a control character")
    return value


def _unique_strings(values: list[str]) -> list[str]:
    if len(values) != len(set(values)):
        raise ValueError("values must be unique")
    return values


def _context_size(value: dict[str, object]) -> dict[str, object]:
    encoded = json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    if len(encoded) > MAX_CONTEXT_JSON_BYTES:
        raise ValueError("JSON object exceeds the 64 KiB context limit")
    return value


def _error_code(value: object) -> object:
    if isinstance(value, ErrorCode):
        return value
    if isinstance(value, str):
        try:
            return ErrorCode(value)
        except ValueError as exc:
            raise ValueError("error code is not in the stable v7 registry") from exc
    return value


def _duplicate_safe_json(value: str | bytes | bytearray) -> object:
    if isinstance(value, (bytes, bytearray)):
        text = bytes(value).decode("utf-8", errors="strict")
    elif isinstance(value, str):
        text = value
    else:
        raise TypeError("JSON input must be str, bytes, or bytearray")

    def object_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, item in pairs:
            if key in result:
                raise ValueError(f"duplicate JSON key: {key}")
            result[key] = item
        return result

    def reject_constant(value_name: str) -> object:
        raise ValueError(f"non-finite JSON number: {value_name}")

    return json.loads(
        text,
        object_pairs_hook=object_pairs,
        parse_constant=reject_constant,
    )


def _document_validation_error(
    model_name: str, raw_input: object, error: Exception
) -> ValidationError:
    return ValidationError.from_exception_data(
        model_name,
        [
            {
                "type": "value_error",
                "loc": (),
                "input": raw_input,
                "ctx": {"error": ValueError(str(error))},
            }
        ],
        hide_input=True,
    )


WorkspaceId = Annotated[
    str,
    StringConstraints(strict=True, pattern=r"^ws_[0-9a-f]{24}$"),
]
RecordId = Annotated[
    str,
    StringConstraints(strict=True, pattern=r"^mem_[0-9a-f]{64}$"),
]
EventId = Annotated[
    str,
    StringConstraints(strict=True, pattern=r"^evt_[0-9a-f]{64}$"),
]
FactId = Annotated[
    str,
    StringConstraints(strict=True, pattern=r"^fact_[0-9a-f]{64}$"),
]
RelationshipId = Annotated[
    str,
    StringConstraints(strict=True, pattern=r"^rel_[0-9a-f]{64}$"),
]
RuleId = Annotated[
    str,
    StringConstraints(strict=True, pattern=r"^rule_[0-9a-f]{64}$"),
]
TriggerId = Annotated[
    str,
    StringConstraints(strict=True, pattern=r"^trg_[0-9a-f]{64}$"),
]
EntityId = Annotated[
    str,
    StringConstraints(strict=True, pattern=r"^ent_[0-9a-f]{64}$"),
]
CommunityId = Annotated[
    str,
    StringConstraints(strict=True, pattern=r"^com_[0-9a-f]{64}$"),
]
CodeEntityId = Annotated[
    str,
    StringConstraints(strict=True, pattern=r"^code_[0-9a-f]{64}$"),
]
ActiveContextId = Annotated[
    str,
    StringConstraints(strict=True, pattern=r"^act_[0-9a-f]{64}$"),
]
VersionId = Annotated[
    str,
    StringConstraints(strict=True, pattern=r"^ver_[0-9a-f]{64}$"),
]
RequestId = Annotated[
    str,
    StringConstraints(
        strict=True,
        min_length=12,
        max_length=132,
        pattern=r"^req_[A-Za-z0-9_-]+$",
    ),
]
OperationId = Annotated[
    str,
    StringConstraints(
        strict=True,
        min_length=11,
        max_length=131,
        pattern=r"^op_[A-Za-z0-9_-]+$",
    ),
]
SelectionToken = Annotated[
    str,
    StringConstraints(
        strict=True,
        min_length=16,
        max_length=4096,
        pattern=r"^[A-Za-z0-9._~-]+$",
    ),
]
Cursor = Annotated[
    str,
    StringConstraints(strict=True, min_length=16, max_length=4096),
]
RelativePath = Annotated[
    str,
    StringConstraints(strict=True, min_length=1, max_length=1024),
    AfterValidator(_relative_path),
    _RELATIVE_PATH,
]
AwareDateTime = Annotated[datetime, BeforeValidator(parse_wire_datetime)]
UtcDateTime = Annotated[AwareDateTime, AfterValidator(_utc)]

JsonKey = Annotated[
    str,
    StringConstraints(strict=True, min_length=1, max_length=MAX_JSON_KEY_CHARS),
]
JsonString = Annotated[
    str,
    StringConstraints(strict=True, max_length=MAX_JSON_STRING_CHARS),
]
JsonBoolean = Annotated[bool, Field(strict=True)]
JsonInteger = Annotated[
    int,
    Field(strict=True, ge=-(2**63), le=2**63 - 1),
]
JsonNumber = Annotated[float, Field(strict=True, allow_inf_nan=False)]
if TYPE_CHECKING:
    JsonValue: TypeAlias = (
        None | bool | int | float | str | list["JsonValue"] | dict[str, "JsonValue"]
    )
    JsonObject: TypeAlias = dict[str, JsonValue]
else:
    JsonValue = TypeAliasType(
        "JsonValue",
        None
        | JsonBoolean
        | JsonInteger
        | JsonNumber
        | JsonString
        | Annotated[list["JsonValue"], Field(max_length=MAX_JSON_COLLECTION_ITEMS)]
        | Annotated[
            dict[JsonKey, "JsonValue"], Field(max_length=MAX_JSON_COLLECTION_ITEMS)
        ],
    )
    JsonObject = TypeAliasType(
        "JsonObject",
        Annotated[
            dict[JsonKey, JsonValue],
            Field(max_length=MAX_JSON_COLLECTION_ITEMS),
        ],
    )
UserJsonObject = Annotated[JsonObject, _USER_TEXT]
ContextJsonObject = Annotated[UserJsonObject, AfterValidator(_context_size)]

ErrorCodeValue = Annotated[ErrorCode, BeforeValidator(_error_code)]
UpperSnakeCode = Annotated[
    str,
    StringConstraints(
        strict=True,
        min_length=2,
        max_length=80,
        pattern=r"^[A-Z][A-Z0-9_]*$",
    ),
]
ToolName = Annotated[
    str,
    StringConstraints(
        strict=True,
        min_length=3,
        max_length=128,
        pattern=r"^[a-z][a-z0-9_]*$",
    ),
]
Tag = Annotated[
    UserText,
    StringConstraints(strict=True, min_length=1, max_length=80),
]
ProviderName = Annotated[
    str,
    StringConstraints(
        strict=True,
        min_length=1,
        max_length=80,
        pattern=r"^[a-z][a-z0-9_-]*$",
    ),
]
SanitizedMessage = Annotated[
    str,
    StringConstraints(strict=True, min_length=1, max_length=512),
    AfterValidator(_sanitized),
]
SanitizedDetail = Annotated[
    str,
    StringConstraints(strict=True, min_length=1, max_length=2000),
    AfterValidator(_sanitized),
]
ContentHash = Annotated[
    str,
    StringConstraints(strict=True, pattern=r"^[0-9a-f]{64}$"),
]
Citation = Annotated[
    str,
    StringConstraints(strict=True, pattern=r"^\[E[1-9][0-9]*\]$"),
]

RecordType = Literal[
    "decision", "pattern", "warning", "learning", "procedure", "observation"
]
RecordStatus = Literal["current", "superseded", "invalidated", "archived"]
CapabilityStatus = Literal["ready", "disabled", "degraded", "failed"]
ProviderStatus = Literal["ready", "degraded", "unavailable", "failed"]
EvidenceStatus = Literal["current", "superseded"]

PublicObjectId = (
    WorkspaceId
    | RecordId
    | EventId
    | FactId
    | RelationshipId
    | RuleId
    | TriggerId
    | EntityId
    | CommunityId
    | CodeEntityId
    | ActiveContextId
)

CountName = Annotated[
    str,
    StringConstraints(
        strict=True,
        min_length=1,
        max_length=80,
        pattern=r"^[a-z][a-z0-9_]*$",
    ),
]
CountValue = Annotated[int, Field(strict=True, ge=0, le=2**63 - 1)]
CountMap = TypeAliasType(
    "CountMap",
    Annotated[dict[CountName, CountValue], Field(max_length=64)],
)


class WireModel(BaseModel):
    """Base for every v7 object schema and duplicate-safe JSON boundary."""

    model_config = ConfigDict(
        extra="forbid",
        strict=True,
        validate_default=True,
        hide_input_in_errors=True,
    )

    @model_validator(mode="after")
    def reject_absolute_filesystem_paths(self) -> WireModel:
        if contains_absolute_filesystem_path(self):
            raise ValueError("absolute filesystem paths are forbidden on the v7 wire")
        return self

    @classmethod
    def model_validate_json(
        cls,
        json_data: str | bytes | bytearray,
        **kwargs: Any,
    ) -> Self:
        try:
            decoded = _duplicate_safe_json(json_data)
        except (TypeError, UnicodeDecodeError, ValueError) as exc:
            raise _document_validation_error(cls.__name__, json_data, exc) from None
        return cls.model_validate(decoded, **kwargs)


class ApiWarning(WireModel):
    code: UpperSnakeCode
    message: SanitizedMessage


class FieldError(WireModel):
    field: Annotated[
        str,
        StringConstraints(
            strict=True,
            min_length=1,
            max_length=256,
            pattern=r"^[A-Za-z_][A-Za-z0-9_.\[\]-]*$",
        ),
    ]
    code: UpperSnakeCode
    message: SanitizedMessage


ApiFieldError = FieldError


class ErrorRemedy(WireModel):
    tool: ToolName
    # The caller's own validated arguments, echoed back for the retry.
    arguments: UserJsonObject = Field(default_factory=dict)


Remedy = ErrorRemedy


class ApiError(WireModel):
    code: ErrorCodeValue
    message: SanitizedMessage
    retryable: bool
    retry_after_ms: Annotated[int, Field(ge=0, le=86_400_000)] | None = None
    field_errors: list[FieldError] = Field(default_factory=list, max_length=50)
    remedy: ErrorRemedy | None = None
    correlation_id: RequestId

    @model_validator(mode="after")
    def protect_internal_details(self) -> ApiError:
        if self.code == ErrorCode.INTERNAL_ERROR and (
            self.message != INTERNAL_ERROR_MESSAGE
            or self.retryable
            or self.retry_after_ms is not None
            or self.field_errors
            or self.remedy is not None
        ):
            raise ValueError("INTERNAL_ERROR cannot carry caller-visible diagnostics")
        return self


class CapabilityState(WireModel):
    name: Annotated[
        str,
        StringConstraints(
            strict=True,
            min_length=1,
            max_length=80,
            pattern=r"^[a-z][a-z0-9_-]*$",
        ),
    ]
    status: CapabilityStatus
    reason_code: UpperSnakeCode | None = None
    remediation: SanitizedDetail | None = None


class ResponseMeta(WireModel):
    request_id: RequestId
    workspace_id: WorkspaceId | None = None
    started_at: UtcDateTime
    duration_ms: Annotated[int, Field(ge=0, le=86_400_000)]
    warnings: list[ApiWarning] = Field(default_factory=list, max_length=20)
    capability_states: list[CapabilityState] = Field(
        default_factory=list,
        max_length=64,
    )


def _api_response_schema(schema: dict[str, Any]) -> None:
    schema["type"] = "object"
    required = set(schema.get("required", []))
    required.update({"api_version", "ok", "data", "error", "meta"})
    schema["required"] = sorted(required)
    schema["allOf"] = [
        {
            "if": {
                "properties": {"ok": {"const": True}},
                "required": ["ok"],
            },
            "then": {
                "properties": {
                    "data": {"not": {"type": "null"}},
                    "error": {"type": "null"},
                },
                "required": ["data", "error"],
            },
            "else": {
                "properties": {
                    "data": {"type": "null"},
                    "error": {"not": {"type": "null"}},
                },
                "required": ["data", "error"],
            },
        }
    ]


T = TypeVar("T")


class ApiResponse(WireModel, Generic[T]):
    model_config = ConfigDict(json_schema_extra=_api_response_schema)

    api_version: Literal["7"] = "7"
    ok: bool
    data: T | None = None
    error: ApiError | None = None
    meta: ResponseMeta

    @model_validator(mode="after")
    def enforce_one_branch(self) -> ApiResponse[T]:
        valid_success = self.ok and self.data is not None and self.error is None
        valid_failure = not self.ok and self.data is None and self.error is not None
        if not (valid_success or valid_failure):
            raise ValueError("response must contain exactly one data or error branch")
        return self


class Page(WireModel, Generic[T]):
    items: list[T] = Field(default_factory=list, max_length=MAX_PAGE_ITEMS)
    next_cursor: Cursor | None = None
    truncated: bool


class RecordSummary(WireModel):
    record_id: RecordId
    record_type: RecordType
    excerpt: Annotated[
        UserText,
        StringConstraints(strict=True, min_length=1, max_length=4000),
    ]
    tags: Annotated[list[Tag], AfterValidator(_unique_strings)] = Field(
        default_factory=list,
        max_length=32,
    )
    relative_file_path: RelativePath | None = None
    current_status: RecordStatus
    content_hash: ContentHash
    created_at: AwareDateTime
    updated_at: AwareDateTime

    @model_validator(mode="after")
    def validate_timeline(self) -> RecordSummary:
        if self.updated_at < self.created_at:
            raise ValueError("updated_at cannot precede created_at")
        return self


class EvidenceRef(WireModel):
    origin_workspace_id: WorkspaceId | None = None
    record_id: RecordId
    event_id: EventId
    content_hash: ContentHash
    version_id: VersionId | None = None
    relation_path: list[RelationshipId] = Field(default_factory=list, max_length=32)
    provider: ProviderName


class EvidenceItem(WireModel):
    citation: Citation
    record: RecordSummary
    bounded_excerpt: Annotated[
        UserText,
        StringConstraints(strict=True, min_length=1, max_length=8000),
    ]
    channels: Annotated[list[ProviderName], AfterValidator(_unique_strings)] = Field(
        min_length=1,
        max_length=16,
    )
    score: Annotated[float, Field(ge=0, allow_inf_nan=False)]
    status: EvidenceStatus
    evidence_refs: list[EvidenceRef] = Field(min_length=1, max_length=32)

    @model_validator(mode="after")
    def validate_providers(self) -> EvidenceItem:
        channels = set(self.channels)
        if any(ref.provider not in channels for ref in self.evidence_refs):
            raise ValueError("every evidence provider must be a selected channel")
        return self


class CitationManifestEntry(WireModel):
    citation: Citation
    evidence_refs: list[EvidenceRef] = Field(min_length=1, max_length=32)
    channels: Annotated[list[ProviderName], AfterValidator(_unique_strings)] = Field(
        min_length=1,
        max_length=16,
    )

    @model_validator(mode="after")
    def validate_providers(self) -> CitationManifestEntry:
        channels = set(self.channels)
        if any(ref.provider not in channels for ref in self.evidence_refs):
            raise ValueError("every citation provider must be a selected channel")
        return self


class ProviderDiagnostic(WireModel):
    provider: ProviderName
    status: ProviderStatus
    manifest_generation: Annotated[int, Field(ge=1)] | None = None
    elapsed_ms: Annotated[float, Field(ge=0, allow_inf_nan=False)]
    reason: SanitizedMessage | None = None
    returned_count: Annotated[int, Field(ge=0, le=200)]

    @model_validator(mode="after")
    def validate_status(self) -> ProviderDiagnostic:
        if self.status != "ready" and self.reason is None:
            raise ValueError("a non-ready provider requires a reason")
        if self.status in {"unavailable", "failed"} and self.returned_count:
            raise ValueError("an unavailable provider cannot return evidence")
        return self


class TokenUsage(WireModel):
    budget: Annotated[int, Field(ge=0, le=16_000)]
    requested: Annotated[int, Field(ge=0, le=1_000_000)]
    selected: Annotated[int, Field(ge=0, le=1_000_000)]
    rendered: Annotated[int, Field(ge=0, le=1_000_000)]
    dropped: Annotated[int, Field(ge=0, le=1_000_000)]

    @model_validator(mode="after")
    def validate_counts(self) -> TokenUsage:
        if self.selected > self.requested:
            raise ValueError("selected tokens cannot exceed requested tokens")
        if self.rendered > self.budget:
            raise ValueError("rendered tokens cannot exceed the budget")
        return self


class RetrievalData(WireModel):
    items: list[EvidenceItem] = Field(default_factory=list, max_length=50)
    rendered_context: (
        Annotated[
            UserText,
            StringConstraints(strict=True, min_length=1, max_length=500_000),
        ]
        | None
    ) = None
    citation_manifest: list[CitationManifestEntry] = Field(
        default_factory=list,
        max_length=50,
    )
    provider_diagnostics: list[ProviderDiagnostic] = Field(
        default_factory=list,
        max_length=32,
    )
    abstained: bool
    abstention_reason: UpperSnakeCode | None = None
    token_usage: TokenUsage

    @model_validator(mode="after")
    def validate_abstention(self) -> RetrievalData:
        if self.abstained:
            if (
                self.abstention_reason is None
                or self.items
                or self.rendered_context is not None
                or self.citation_manifest
            ):
                raise ValueError("an abstention cannot contain fabricated evidence")
            return self
        if self.abstention_reason is not None:
            raise ValueError("non-abstaining retrieval cannot contain a reason")
        if not self.items or self.rendered_context is None:
            raise ValueError(
                "retrieval requires selected evidence and rendered context"
            )
        item_citations = [item.citation for item in self.items]
        manifest_citations = [entry.citation for entry in self.citation_manifest]
        if item_citations != manifest_citations or len(item_citations) != len(
            set(item_citations)
        ):
            raise ValueError("selected evidence must match the citation manifest")
        return self


class MutationReceipt(WireModel):
    operation_id: OperationId
    affected_ids: list[PublicObjectId] = Field(default_factory=list, max_length=500)
    event_ids: list[EventId] = Field(default_factory=list, max_length=500)
    counts: CountMap = Field(default_factory=dict)
    idempotent_replay: bool


class DestructiveMutationReceipt(MutationReceipt):
    selected_count: Annotated[int, Field(ge=0)]
    changed_count: Annotated[int, Field(ge=0)]
    skipped_count: Annotated[int, Field(ge=0)]

    @model_validator(mode="after")
    def validate_bulk_counts(self) -> DestructiveMutationReceipt:
        if self.changed_count + self.skipped_count > self.selected_count:
            raise ValueError("bulk result counts exceed the selected count")
        return self


class Preview(WireModel):
    selection_token: SelectionToken
    counts: CountMap = Field(default_factory=dict)
    sample_ids: list[PublicObjectId] = Field(default_factory=list, max_length=20)
    expires_at: AwareDateTime


__all__ = [
    "ActiveContextId",
    "ApiError",
    "ApiFieldError",
    "ApiResponse",
    "ApiWarning",
    "AwareDateTime",
    "CapabilityState",
    "CapabilityStatus",
    "Citation",
    "CitationManifestEntry",
    "CodeEntityId",
    "CommunityId",
    "ContentHash",
    "ContextJsonObject",
    "CountMap",
    "Cursor",
    "DestructiveMutationReceipt",
    "EntityId",
    "ErrorRemedy",
    "EvidenceItem",
    "EvidenceRef",
    "EvidenceStatus",
    "EventId",
    "FactId",
    "FieldError",
    "guarded_strings",
    "is_host_absolute_path",
    "JsonObject",
    "JsonValue",
    "MAX_CONTEXT_JSON_BYTES",
    "MAX_JSON_COLLECTION_ITEMS",
    "MAX_JSON_KEY_CHARS",
    "MAX_JSON_STRING_CHARS",
    "MAX_PAGE_ITEMS",
    "MutationReceipt",
    "OperationId",
    "Page",
    "parse_wire_datetime",
    "Preview",
    "ProviderDiagnostic",
    "ProviderName",
    "ProviderStatus",
    "PublicObjectId",
    "RecordId",
    "RecordStatus",
    "RecordSummary",
    "RecordType",
    "RelationshipId",
    "RelativePath",
    "Remedy",
    "RequestId",
    "ResponseMeta",
    "RetrievalData",
    "RuleId",
    "SelectionToken",
    "Tag",
    "TokenUsage",
    "ToolName",
    "TriggerId",
    "UserJsonObject",
    "UserText",
    "UtcDateTime",
    "VersionId",
    "WireModel",
    "WorkspaceId",
]
