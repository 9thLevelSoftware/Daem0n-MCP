"""Typed, framework-neutral manifest for the complete v7 MCP tool surface.

The models in this module are the public wire contract.  Business handlers are
injected by the composition root so importing this module cannot register a
tool or open a storage dependency as a side effect.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from types import MappingProxyType
from typing import Annotated, Literal, get_args
from urllib.parse import urlsplit

from pydantic import (
    AfterValidator,
    BeforeValidator,
    Field,
    PlainSerializer,
    StringConstraints,
    model_validator,
)

from ...covenant import CovenantLevel
from .mapping import V6_TO_V7_MAPPINGS
from .models import (
    RECORD_CONTENT_NOTICE,
    ActiveContextId,
    ApiResponse,
    AwareDateTime,
    BoundedText,
    CapabilityState,
    CodeEntityId,
    CommunityId,
    ContentHash,
    ContextJsonObject,
    CountMap,
    Cursor,
    DestructiveMutationReceipt,
    EntityId,
    EventId,
    EvidenceRef,
    JsonObject,
    MutationReceipt,
    Page,
    PreflightGuidance,
    PreflightToken,
    Preview,
    RecordId,
    RecordSummary,
    RecordType,
    RelationshipId,
    RelativePath,
    RetrievalData,
    RuleId,
    RuleView,
    SelectionToken,
    Tag,
    ToolName,
    TriggerId,
    UniqueTags,
    UserJsonObject,
    UserMediumText,
    UserText,
    UtcDateTime,
    VersionId,
    WireModel,
    WorkspaceId,
    _unique_strings,
    contains_absolute_filesystem_path,
)
from .policy import V7_TOOL_LEVELS, V7ArgumentNormalizer
from .registry import PINNED_TOOL_NAMES, ManifestError, ToolSpec
from .resources import ActiveContextItem

NameText = Annotated[
    str,
    StringConstraints(strict=True, min_length=1, max_length=256),
]
MediumText = Annotated[
    str,
    StringConstraints(strict=True, min_length=1, max_length=2000),
]
# User-authored or user-derived text, which may mention paths (see UserText).
UserShortText = Annotated[
    UserText,
    StringConstraints(strict=True, min_length=1, max_length=80),
]
UserNameText = Annotated[
    UserText,
    StringConstraints(strict=True, min_length=1, max_length=256),
]
UserLongText = Annotated[
    UserText,
    StringConstraints(strict=True, min_length=1, max_length=100_000),
]
OptionalUserMediumText = UserMediumText | None
RecordVisibility = Literal["workspace", "private", "shared"]


def _record_visibility(context: dict[str, object]) -> dict[str, object]:
    if "code_bindings" in context:
        raise ValueError("context.code_bindings is server-managed")
    if context.get("visibility", "workspace") not in get_args(RecordVisibility):
        raise ValueError("context.visibility must be workspace, private or shared")
    return context


RecordContext = Annotated[ContextJsonObject, AfterValidator(_record_visibility)]
InformedByRecordIds = Annotated[list[RecordId], AfterValidator(_unique_strings)]
IdempotencyKey = Annotated[
    str,
    StringConstraints(
        strict=True,
        min_length=8,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._~-]*$",
    ),
]
EditRequestId = Annotated[
    str,
    StringConstraints(
        strict=True,
        min_length=68,
        max_length=68,
        pattern=r"^edt_[0-9a-f]{64}$",
    ),
]
EditReceiptToken = Annotated[
    str,
    StringConstraints(
        strict=True,
        min_length=64,
        max_length=4096,
        pattern=r"^edr_v1\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+$",
    ),
]
CaptureCandidateId = Annotated[
    str,
    StringConstraints(
        strict=True,
        min_length=68,
        max_length=68,
        pattern=r"^cap_[0-9a-f]{64}$",
    ),
]
EntityType = Annotated[
    str,
    StringConstraints(
        strict=True,
        min_length=1,
        max_length=80,
        pattern=r"^[a-z][a-z0-9_-]*$",
    ),
]
RelationshipType = Literal[
    "led_to",
    "supersedes",
    "depends_on",
    "conflicts_with",
    "related_to",
    "evidence_for",
    "derived_from",
    "invalidates",
]
RelationshipDirection = Literal["outgoing", "incoming", "both"]
TriggerType = Literal["file", "tag", "entity"]
CodeEntityKind = Literal[
    "file", "module", "class", "function", "method", "variable", "symbol"
]
TodoType = Literal["todo", "fixme", "hack", "xxx", "note"]
ProjectionName = Literal[
    "lexical", "dense", "graph", "temporal", "procedure", "outcome", "code"
]
RebuildableProjectionName = Literal[
    "lexical", "dense", "graph", "temporal", "procedure", "outcome"
]
WorkspaceRelationship = Literal["related"]
GitChangeStatus = Literal[
    "added", "modified", "deleted", "renamed", "untracked", "conflicted"
]
UpdateKind = Literal[
    "record", "rule", "trigger", "active_context", "projection", "workspace"
]


def _https_url(value: str) -> str:
    parsed = urlsplit(value)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
    ):
        raise ValueError("url must be an absolute public HTTPS URL without credentials")
    return value


HttpsUrl = Annotated[
    str,
    StringConstraints(strict=True, min_length=9, max_length=2048),
    AfterValidator(_https_url),
]


def _wire_string_set(value: object) -> object:
    """Accept the JSON array representation while retaining strict elements."""

    if isinstance(value, list):
        try:
            converted = set(value)
        except TypeError:
            return value
        if len(converted) != len(value):
            raise ValueError("set-valued arrays cannot contain duplicates")
        return converted
    return value


_SORTED_SET_SERIALIZER = PlainSerializer(
    lambda values: sorted(values),
    return_type=list[str],
    when_used="json",
)


RecordTypeSet = Annotated[
    set[RecordType],
    Field(max_length=6),
    BeforeValidator(_wire_string_set),
    _SORTED_SET_SERIALIZER,
]
TagSet = Annotated[
    set[Tag],
    Field(max_length=32),
    BeforeValidator(_wire_string_set),
    _SORTED_SET_SERIALIZER,
]
RecordIdSet100 = Annotated[
    set[RecordId],
    Field(max_length=100),
    BeforeValidator(_wire_string_set),
    _SORTED_SET_SERIALIZER,
]
RecordIdSet500 = Annotated[
    set[RecordId],
    Field(max_length=500),
    BeforeValidator(_wire_string_set),
    _SORTED_SET_SERIALIZER,
]
WorkspaceIdSet = Annotated[
    set[WorkspaceId],
    Field(max_length=32),
    BeforeValidator(_wire_string_set),
    _SORTED_SET_SERIALIZER,
]
CodeEntityKindSet = Annotated[
    set[CodeEntityKind],
    Field(max_length=7),
    BeforeValidator(_wire_string_set),
    _SORTED_SET_SERIALIZER,
]
TodoTypeSet = Annotated[
    set[TodoType],
    Field(max_length=5),
    BeforeValidator(_wire_string_set),
    _SORTED_SET_SERIALIZER,
]
RelationshipTypeSet = Annotated[
    set[RelationshipType],
    Field(max_length=8),
    BeforeValidator(_wire_string_set),
    _SORTED_SET_SERIALIZER,
]
TransportSet = Annotated[
    set[Literal["stdio", "streamable-http"]],
    Field(max_length=2),
    BeforeValidator(_wire_string_set),
    _SORTED_SET_SERIALIZER,
]


PROTECTED_TOOL_NAMES = tuple(
    sorted(
        name
        for name, level in V7_TOOL_LEVELS.items()
        if level in {CovenantLevel.COUNSEL, CovenantLevel.DESTRUCTIVE}
    )
)
ProtectedToolName = Literal[
    "active_context_add",
    "active_context_clear",
    "active_context_remove",
    "code_todos_scan_and_store",
    "community_rebuild",
    "context_trigger_create",
    "context_trigger_delete",
    "decision_debate",
    "document_ingest_url",
    "dream_duplicates_purge",
    "entity_backfill",
    "memory_archive_set",
    "memory_capture_promote",
    "memory_compact",
    "memory_duplicates_cleanup",
    "memory_link",
    "memory_pin_set",
    "memory_prune",
    "memory_store",
    "memory_store_batch",
    "memory_unlink",
    "rule_create",
    "rule_update",
    "sandbox_execute_python",
    "workspace_consolidate",
    "workspace_consolidate_and_archive_sources",
    "workspace_import",
    "workspace_link",
    "workspace_unlink",
]


class ProjectionManifest(WireModel):
    projection: ProjectionName
    generation: Annotated[int, Field(ge=1)]
    built_at: UtcDateTime
    source_root_hash: ContentHash


class DiagnosticSummary(WireModel):
    code: Annotated[
        str,
        StringConstraints(
            strict=True,
            min_length=2,
            max_length=80,
            pattern=r"^[A-Z][A-Z0-9_]*$",
        ),
    ]
    message: MediumText


class TriggerView(WireModel):
    trigger_id: TriggerId
    trigger_type: TriggerType
    pattern: UserMediumText
    recall_query: UserMediumText
    categories: RecordTypeSet | None = None
    enabled: bool
    updated_at: UtcDateTime


class GitChangeSummary(WireModel):
    relative_file_path: RelativePath
    status: GitChangeStatus


class OutcomeSummary(WireModel):
    record_id: RecordId
    worked: bool
    outcome_excerpt: Annotated[
        UserText,
        StringConstraints(strict=True, min_length=1, max_length=4000),
    ]
    happened_at: UtcDateTime


class CovenantNextStep(WireModel):
    tool: ToolName
    reason: MediumText


class SessionBriefData(WireModel):
    workspace_id: WorkspaceId
    briefed_at: UtcDateTime
    workspace_statistics: CountMap
    recent_decisions: list[RecordSummary] = Field(default_factory=list, max_length=50)
    warnings: list[RecordSummary] = Field(default_factory=list, max_length=50)
    failed_outcomes: list[OutcomeSummary] = Field(default_factory=list, max_length=50)
    applicable_rules: list[RuleView] = Field(default_factory=list, max_length=50)
    active_context: list[ActiveContextItem] = Field(default_factory=list, max_length=50)
    git_changes: list[GitChangeSummary] = Field(default_factory=list, max_length=200)
    projection_freshness: list[ProjectionManifest] = Field(
        default_factory=list, max_length=7
    )
    covenant_next_steps: list[CovenantNextStep] = Field(
        default_factory=list, max_length=10
    )
    withheld_untrusted: CountMap = Field(default_factory=dict)
    content_notice: BoundedText = RECORD_CONTENT_NOTICE


class PreflightData(WireModel):
    guidance: PreflightGuidance
    preflight_token: PreflightToken | None = None
    target_tool: ProtectedToolName
    expires_at: UtcDateTime | None = None

    @model_validator(mode="after")
    def validate_capability_expiry(self) -> PreflightData:
        if (self.preflight_token is None) != (self.expires_at is None):
            raise ValueError("preflight token and expiry must be returned together")
        return self


class MemoryStoreData(WireModel):
    record: RecordSummary
    event_id: EventId
    stream_version: Annotated[int, Field(ge=1)]
    idempotent_replay: bool


class EditPreflightData(WireModel):
    edit_request_id: EditRequestId
    edit_receipt: EditReceiptToken
    expires_at: UtcDateTime


class CaptureCandidateView(WireModel):
    candidate_id: CaptureCandidateId
    source_kind: Literal["native_edit", "tool_result", "dreaming", "system"]
    proposed_record: UserJsonObject
    provenance: JsonObject
    created_at: UtcDateTime


class MemoryCaptureListData(WireModel):
    items: list[CaptureCandidateView] = Field(default_factory=list, max_length=100)
    next_cursor: Cursor | None = None
    truncated: bool


class MemoryCapturePromoteData(WireModel):
    candidate_id: CaptureCandidateId
    record: RecordSummary
    event_id: EventId
    idempotent_replay: bool


class OutcomeData(WireModel):
    record_id: RecordId
    outcome_event_id: EventId
    stream_version: Annotated[int, Field(ge=1)]
    worked: bool
    idempotent_replay: bool


class DreamingStrategyStatus(WireModel):
    strategy: Literal[
        "failed_decision",
        "pending_outcome",
        "connection_discovery",
        "community_refresh",
    ]
    status: Literal["idle", "running", "disabled", "degraded"]
    pending_count: Annotated[int, Field(ge=0, le=10_000)] = 0
    work_remaining: bool = False
    last_success_at: UtcDateTime | None = None
    stable_error_code: (
        Annotated[
            str,
            StringConstraints(
                strict=True, min_length=1, max_length=64, pattern=r"^[A-Z0-9_]+$"
            ),
        ]
        | None
    ) = None
    yielded: bool = False


class DreamingStatus(WireModel):
    enabled: bool
    running: bool
    yielded: bool
    strategies: list[DreamingStrategyStatus] = Field(default_factory=list, max_length=4)


class RuntimeDiagnostic(WireModel):
    component: Literal[
        "lexical",
        "dense",
        "graph",
        "temporal",
        "procedure",
        "outcome",
        "code",
        "tasks",
        "edit_bridge",
        "migration",
    ]
    status: Literal["ready", "disabled", "degraded", "not_verified"]
    counts: CountMap = Field(default_factory=dict)
    stable_error_code: (
        Annotated[
            str,
            StringConstraints(
                strict=True, min_length=1, max_length=64, pattern=r"^[A-Z0-9_]+$"
            ),
        ]
        | None
    ) = None


class HealthData(WireModel):
    package_version: Annotated[
        str,
        StringConstraints(strict=True, min_length=1, max_length=64),
    ]
    api_version: Literal["7"] = "7"
    protocol_version: Annotated[
        str,
        StringConstraints(strict=True, min_length=1, max_length=32),
    ]
    storage_format_version: Annotated[int, Field(ge=1)] | None = None
    storage_schema_version: Annotated[int, Field(ge=1)] | None = None
    supported_transports: TransportSet = Field(min_length=1)
    task_support: CapabilityState
    auth_mode: Literal["process", "loopback", "jwt"]
    capability_states: list[CapabilityState] = Field(
        default_factory=list, max_length=64
    )
    dreaming: DreamingStatus | None = None
    runtime_diagnostics: list[RuntimeDiagnostic] = Field(
        default_factory=list, max_length=16
    )


class TriggerMatch(WireModel):
    trigger: TriggerView
    matched_value: UserNameText
    records: list[RecordSummary] = Field(default_factory=list, max_length=20)


class TriggerMatchData(WireModel):
    matches: list[TriggerMatch] = Field(default_factory=list, max_length=5)
    truncated: bool


class UpdateSummary(WireModel):
    event_id: EventId
    kind: UpdateKind
    object_id: RecordId | RuleId | TriggerId | ActiveContextId | WorkspaceId
    occurred_at: UtcDateTime
    summary: MediumText


class SessionUpdatesData(WireModel):
    changed: bool
    cursor: Cursor
    events: list[UpdateSummary] = Field(default_factory=list, max_length=200)


class CovenantStatusData(WireModel):
    briefed: bool
    briefed_at: UtcDateTime | None = None
    token_ttl_seconds: Annotated[int, Field(ge=1, le=3600)]
    next_step: CovenantNextStep | None = None


class ActiveContextPage(Page[ActiveContextItem]):
    """One page plus a token bound to the exact clearable entry snapshot."""

    selection_token: SelectionToken


class HierarchyLayer(WireModel):
    level: Annotated[int, Field(ge=0, le=32)]
    records: list[RecordSummary] = Field(default_factory=list, max_length=50)


class CommunitySummary(WireModel):
    community_id: CommunityId
    label: UserNameText
    level: Annotated[int, Field(ge=0, le=32)]
    member_count: Annotated[int, Field(ge=0, le=1_000_000)]
    parent_community_id: CommunityId | None = None
    manifest_generation: Annotated[int, Field(ge=1)]


class HierarchicalRecallData(WireModel):
    layers: list[HierarchyLayer] = Field(default_factory=list, max_length=32)
    communities: list[CommunitySummary] = Field(default_factory=list, max_length=50)
    evidence_refs: list[EvidenceRef] = Field(default_factory=list, max_length=200)


class HighlightSpan(WireModel):
    start: Annotated[int, Field(ge=0)]
    end: Annotated[int, Field(gt=0)]

    @model_validator(mode="after")
    def validate_span(self) -> HighlightSpan:
        if self.end <= self.start:
            raise ValueError("highlight end must follow start")
        return self


class TextSearchHit(WireModel):
    record: RecordSummary
    bounded_excerpt: Annotated[
        UserText,
        StringConstraints(strict=True, min_length=1, max_length=8000),
    ]
    highlights: list[HighlightSpan] = Field(default_factory=list, max_length=100)
    evidence_refs: list[EvidenceRef] = Field(default_factory=list, max_length=32)


class RuleCheckData(WireModel):
    matched_rules: list[RuleView] = Field(default_factory=list, max_length=50)
    must_do: list[UserMediumText] = Field(default_factory=list, max_length=50)
    must_not: list[UserMediumText] = Field(default_factory=list, max_length=50)
    ask_first: list[UserMediumText] = Field(default_factory=list, max_length=50)
    warnings: list[UserMediumText] = Field(default_factory=list, max_length=50)


class ContextCompressData(WireModel):
    text: Annotated[
        UserText,
        StringConstraints(strict=True, min_length=1, max_length=500_000),
    ]
    original_tokens: Annotated[int, Field(ge=0, le=1_000_000)]
    rendered_tokens: Annotated[int, Field(ge=0, le=1_000_000)]
    ratio: Annotated[float, Field(gt=0, le=1, allow_inf_nan=False)]
    provider: NameText


class MemoryStoreBatchData(WireModel):
    records: list[RecordSummary] = Field(min_length=1, max_length=100)
    event_ids: list[EventId] = Field(min_length=1, max_length=100)
    idempotent_replay: bool


class DocumentSource(WireModel):
    url: HttpsUrl
    topic: UserMediumText
    content_hash: ContentHash


class DocumentIngestData(WireModel):
    source: DocumentSource
    records: list[RecordSummary] = Field(default_factory=list, max_length=100)
    event_ids: list[EventId] = Field(default_factory=list, max_length=100)
    truncated: bool


class VerifiedClaim(WireModel):
    claim: UserMediumText
    status: Literal["supported", "contradicted", "unknown"]
    evidence_refs: list[EvidenceRef] = Field(default_factory=list, max_length=32)


class Contradiction(WireModel):
    claim: UserMediumText
    explanation: MediumText
    evidence_refs: list[EvidenceRef] = Field(min_length=1, max_length=32)


class MemoryVerifyData(WireModel):
    claims: list[VerifiedClaim] = Field(default_factory=list, max_length=100)
    evidence_refs: list[EvidenceRef] = Field(default_factory=list, max_length=200)
    contradictions: list[Contradiction] = Field(default_factory=list, max_length=100)
    overall_status: Literal["supported", "contradicted", "mixed", "unknown"]


class SandboxExecutionData(WireModel):
    success: bool
    stdout: Annotated[UserText, StringConstraints(strict=True, max_length=100_000)]
    stderr: Annotated[UserText, StringConstraints(strict=True, max_length=100_000)]
    exit_status: Annotated[int, Field(ge=-1, le=255)]
    execution_time_ms: Annotated[int, Field(ge=0, le=60_000)]
    # The same lines as stdout/stderr (see external_operations), so user text.
    sanitized_logs: list[
        Annotated[
            UserText, StringConstraints(strict=True, min_length=1, max_length=4096)
        ]
    ] = Field(default_factory=list, max_length=100)


class CodeIndexData(WireModel):
    manifest: ProjectionManifest
    files_seen: Annotated[int, Field(ge=0)]
    files_indexed: Annotated[int, Field(ge=0)]
    skipped: Annotated[int, Field(ge=0)]
    diagnostics: list[DiagnosticSummary] = Field(default_factory=list, max_length=100)


class CodeEntitySummary(WireModel):
    code_entity_id: CodeEntityId
    kind: CodeEntityKind
    qualified_name: UserNameText
    relative_file_path: RelativePath
    start_line: Annotated[int, Field(ge=1)]
    end_line: Annotated[int, Field(ge=1)]
    manifest_generation: Annotated[int, Field(ge=1)]

    @model_validator(mode="after")
    def validate_lines(self) -> CodeEntitySummary:
        if self.end_line < self.start_line:
            raise ValueError("end_line cannot precede start_line")
        return self


class CodeImpactPath(WireModel):
    entities: list[CodeEntityId] = Field(min_length=1, max_length=32)


class CodeImpactData(WireModel):
    subject: CodeEntitySummary
    affected: list[CodeEntitySummary] = Field(default_factory=list, max_length=500)
    paths: list[CodeImpactPath] = Field(default_factory=list, max_length=500)
    evidence_refs: list[EvidenceRef] = Field(default_factory=list, max_length=200)


class TodoFinding(WireModel):
    relative_file_path: RelativePath
    line: Annotated[int, Field(ge=1)]
    todo_type: TodoType
    text: UserMediumText


class CodeTodosStoreData(WireModel):
    findings: list[TodoFinding] = Field(default_factory=list, max_length=500)
    stored_records: list[RecordSummary] = Field(default_factory=list, max_length=500)
    event_ids: list[EventId] = Field(default_factory=list, max_length=500)


class RefactorProposalData(WireModel):
    proposal: Annotated[
        UserText,
        StringConstraints(strict=True, min_length=1, max_length=100_000),
    ]
    affected_entities: list[CodeEntitySummary] = Field(
        default_factory=list, max_length=500
    )
    warnings: list[MediumText] = Field(default_factory=list, max_length=50)
    evidence_refs: list[EvidenceRef] = Field(default_factory=list, max_length=200)


class RelationshipView(WireModel):
    relationship_id: RelationshipId
    source_record_id: RecordId
    target_record_id: RecordId
    relationship_type: RelationshipType
    description: OptionalUserMediumText = None
    confidence: Annotated[float, Field(ge=0, le=1, allow_inf_nan=False)]


class RelationshipPath(WireModel):
    record_ids: list[RecordId] = Field(min_length=1, max_length=33)
    relationship_ids: list[RelationshipId] = Field(default_factory=list, max_length=32)


class MemoryRelatedData(WireModel):
    root: RecordSummary
    records: list[RecordSummary] = Field(default_factory=list, max_length=500)
    relationships: list[RelationshipView] = Field(default_factory=list, max_length=1000)
    paths: list[RelationshipPath] = Field(default_factory=list, max_length=500)


class MemoryChainTraceData(WireModel):
    paths: list[RelationshipPath] = Field(default_factory=list, max_length=500)
    evidence_refs: list[EvidenceRef] = Field(default_factory=list, max_length=200)


class KnowledgeGraphNode(WireModel):
    record: RecordSummary
    label: UserNameText
    node_type: Literal["record", "entity", "community"]


class KnowledgeGraphEdge(WireModel):
    relationship: RelationshipView


class KnowledgeGraphData(WireModel):
    nodes: list[KnowledgeGraphNode] = Field(default_factory=list, max_length=500)
    edges: list[KnowledgeGraphEdge] = Field(default_factory=list, max_length=2000)
    manifest: ProjectionManifest


class KnowledgeGraphRenderData(WireModel):
    format: Literal["mermaid"] = "mermaid"
    text: Annotated[
        UserText,
        StringConstraints(strict=True, min_length=1, max_length=500_000),
    ]
    evidence_refs: list[EvidenceRef] = Field(default_factory=list, max_length=200)


class KnowledgeGraphStatsData(WireModel):
    node_count: Annotated[int, Field(ge=0)]
    edge_count: Annotated[int, Field(ge=0)]
    type_counts: CountMap
    manifest: ProjectionManifest


class CommunityDetail(WireModel):
    community: CommunitySummary
    members: Page[RecordSummary]


class CommunityRebuildData(WireModel):
    manifest: ProjectionManifest
    communities: list[CommunitySummary] = Field(default_factory=list, max_length=500)
    modularity: Annotated[float, Field(ge=-1, le=1, allow_inf_nan=False)]


class EntitySummary(WireModel):
    entity_id: EntityId
    name: UserNameText
    entity_type: EntityType
    mention_count: Annotated[int, Field(ge=0)]
    manifest_generation: Annotated[int, Field(ge=1)]


class EntityBackfillData(WireModel):
    manifest: ProjectionManifest
    scanned: Annotated[int, Field(ge=0)]
    extracted: Annotated[int, Field(ge=0)]
    skipped: Annotated[int, Field(ge=0)]


class EntityEvolutionItem(WireModel):
    happened_at: UtcDateTime
    summary: UserMediumText
    event_id: EventId


class EntityEvolutionData(WireModel):
    entity: EntitySummary
    timeline: list[EntityEvolutionItem] = Field(default_factory=list, max_length=500)
    evidence_refs: list[EvidenceRef] = Field(default_factory=list, max_length=200)


class MemoryVersionView(WireModel):
    version_id: VersionId
    record: RecordSummary
    event_id: EventId
    valid_from: UtcDateTime
    valid_to: UtcDateTime | None = None
    transaction_time: UtcDateTime


class MemoryAtTimeData(WireModel):
    record: RecordSummary
    version_id: VersionId
    evidence_refs: list[EvidenceRef] = Field(default_factory=list, max_length=32)


class MemoryCompactData(WireModel):
    summary_record: RecordSummary
    source_event_ids: list[EventId] = Field(default_factory=list, max_length=500)
    receipt: DestructiveMutationReceipt


class ProjectionRebuildData(WireModel):
    manifest: ProjectionManifest
    previous_manifest: ProjectionManifest | None = None
    counts: CountMap
    diagnostics: list[DiagnosticSummary] = Field(default_factory=list, max_length=100)


class ExportEvent(WireModel):
    event_id: EventId
    record_id: RecordId | None = None
    event_type: NameText
    happened_at: UtcDateTime
    content_hash: ContentHash
    # Only the envelope's ``data`` holds user text; the rest stays guarded.
    payload: UserJsonObject

    @model_validator(mode="after")
    def guard_envelope(self) -> ExportEvent:
        envelope = {k: v for k, v in self.payload.items() if k != "data"}
        if contains_absolute_filesystem_path(envelope):
            raise ValueError("absolute filesystem paths are forbidden on the v7 wire")
        return self


TransferSessionId = Annotated[
    str,
    StringConstraints(
        strict=True,
        min_length=68,
        max_length=68,
        pattern=r"^(?:xpt|ipt)_[0-9a-f]{64}$",
    ),
]


class PortableVectorPoint(WireModel):
    point_id: Annotated[
        str, StringConstraints(strict=True, min_length=1, max_length=256)
    ]
    payload: JsonObject
    vector: list[Annotated[float, Field(allow_inf_nan=False)]] = Field(
        min_length=1, max_length=4096
    )


class ExportBundle(WireModel):
    api_version: Literal["7"] = "7"
    bundle_version: Literal[1, 2] = 1
    workspace_id: WorkspaceId
    exported_at: UtcDateTime
    root_hash: ContentHash
    events: list[ExportEvent] = Field(default_factory=list, max_length=10_000)
    legacy_projection_included: bool
    vectors_included: bool
    export_session_id: TransferSessionId | None = None
    manifest_hash: ContentHash | None = None
    manifest: JsonObject | None = None
    page_index: Annotated[int, Field(strict=True, ge=0)] | None = None
    page_count: Annotated[int, Field(strict=True, ge=1, le=1_000_000)] | None = None
    page_kind: Literal["events", "legacy", "vectors"] | None = None
    page_hash: ContentHash | None = None
    page_descriptor: JsonObject | None = None
    page_proof: list[ContentHash] = Field(default_factory=list, max_length=32)
    next_cursor: Cursor | None = None
    complete: bool = True
    legacy_rows: list[UserJsonObject] = Field(default_factory=list, max_length=4096)
    vector_points: list[PortableVectorPoint] = Field(
        default_factory=list, max_length=4096
    )

    @model_validator(mode="after")
    def validate_portable_page(self) -> ExportBundle:
        paging = (
            self.export_session_id,
            self.manifest_hash,
            self.manifest,
            self.page_index,
            self.page_count,
            self.page_kind,
            self.page_hash,
            self.page_descriptor,
        )
        if self.bundle_version == 1:
            if (
                any(value is not None for value in paging)
                or self.legacy_rows
                or self.vector_points
                or self.page_proof
            ):
                raise ValueError("format-1 bundles cannot contain format-2 pages")
            return self
        if any(value is None for value in paging):
            raise ValueError("format-2 page metadata is incomplete")
        if self.page_descriptor is None:
            raise ValueError("format-2 page descriptor is missing")
        if len(self.events) > 4096:
            raise ValueError("format-2 event pages contain at most 4096 events")
        if self.page_index is not None and self.page_count is not None:
            if self.page_index >= self.page_count:
                raise ValueError("page_index must be below page_count")
            if self.complete != (self.page_index + 1 == self.page_count):
                raise ValueError("complete does not match page position")
        populated = {
            "events": bool(self.events),
            "legacy": bool(self.legacy_rows),
            "vectors": bool(self.vector_points),
        }
        if any(value for key, value in populated.items() if key != self.page_kind):
            raise ValueError("page payload does not match page_kind")
        legacy = (
            None if self.manifest is None else self.manifest.get("legacy_projection")
        )
        vectors = None if self.manifest is None else self.manifest.get("vectors")
        if self.legacy_projection_included != (legacy is not None):
            raise ValueError("legacy projection flag does not match the manifest")
        if self.vectors_included != (vectors is not None):
            raise ValueError("vector flag does not match the manifest")
        return self


class WorkspaceImportData(WireModel):
    root_hash: ContentHash
    imported: Annotated[int, Field(ge=0)]
    skipped: Annotated[int, Field(ge=0)]
    event_ids: list[EventId] = Field(default_factory=list, max_length=10_000)
    import_session_id: TransferSessionId | None = None
    status: Literal["staging", "ready", "succeeded"] = "succeeded"
    staged_pages: Annotated[int, Field(strict=True, ge=0)] = 0
    page_count: Annotated[int, Field(strict=True, ge=0)] = 0
    event_ids_truncated: bool = False
    diagnostics: list[DiagnosticSummary] = Field(default_factory=list, max_length=100)


class WorkspaceLinkView(WireModel):
    workspace_id: WorkspaceId
    linked_workspace_id: WorkspaceId
    relationship: WorkspaceRelationship
    label: OptionalUserMediumText = None


class WorkspaceConsolidateData(WireModel):
    sources: list[WorkspaceId] = Field(min_length=1, max_length=32)
    imported: Annotated[int, Field(ge=0)]
    event_ids: list[EventId] = Field(default_factory=list, max_length=10_000)


class WorkspaceConsolidateArchiveData(WorkspaceConsolidateData):
    archived: Annotated[int, Field(ge=0)]


class WorkspaceConsolidationPreviewData(WireModel):
    preview_id: Annotated[
        str, StringConstraints(strict=True, pattern=r"^cpr_[0-9a-f]{64}$")
    ]
    sources: list[WorkspaceId] = Field(min_length=1, max_length=32)
    selected: Annotated[int, Field(ge=1, le=10_000)]
    total_bytes: Annotated[int, Field(ge=1, le=67_108_864)]
    expires_at: UtcDateTime
    selection_token: SelectionToken


class DecisionSimulationData(WireModel):
    decision: RecordSummary
    then_context: list[RecordSummary] = Field(default_factory=list, max_length=100)
    current_context: list[RecordSummary] = Field(default_factory=list, max_length=100)
    differences: list[MediumText] = Field(default_factory=list, max_length=100)
    evidence_refs: list[EvidenceRef] = Field(default_factory=list, max_length=200)


class RuleEvolutionReport(WireModel):
    rule: RuleView
    summary: MediumText
    evidence_refs: list[EvidenceRef] = Field(default_factory=list, max_length=100)


class RuleEvolutionData(WireModel):
    reports: list[RuleEvolutionReport] = Field(default_factory=list, max_length=100)
    analyzed: Annotated[int, Field(ge=0, le=1000)]
    evidence_refs: list[EvidenceRef] = Field(default_factory=list, max_length=200)


class DebateRound(WireModel):
    round_number: Annotated[int, Field(ge=1, le=20)]
    advocate: UserMediumText
    challenger: UserMediumText


class DecisionDebateData(WireModel):
    rounds: list[DebateRound] = Field(min_length=1, max_length=20)
    synthesis: Annotated[
        UserText,
        StringConstraints(strict=True, min_length=1, max_length=50_000),
    ]
    consensus_record_id: RecordId
    event_ids: list[EventId] = Field(min_length=1, max_length=100)
    evidence_refs: list[EvidenceRef] = Field(default_factory=list, max_length=200)


class ToolDescriptor(WireModel):
    name: ToolName
    description: MediumText
    category: NameText
    covenant: Literal["exempt", "communion", "counsel", "destructive"]
    read_only: bool
    destructive: bool
    idempotent: bool
    open_world: bool
    listed: bool
    input_schema: JsonObject


class ToolsSearchInput(WireModel):
    query: UserShortText
    limit: Annotated[int, Field(ge=1, le=10)] = 5


class ToolsSearchData(WireModel):
    tools: list[ToolDescriptor] = Field(max_length=10)


class ToolCallInput(WireModel):
    workspace_id: WorkspaceId
    tool: ToolName
    arguments: UserJsonObject = Field(default_factory=dict)


class ToolCallData(WireModel):
    tool: ToolName
    data: UserJsonObject


class SessionBriefInput(WireModel):
    workspace_id: WorkspaceId
    focus_areas: list[UserShortText] = Field(default_factory=list, max_length=10)
    warning_limit: Annotated[int, Field(ge=0, le=50)] = 10
    failure_limit: Annotated[int, Field(ge=0, le=50)] = 10


class MemoryPreflightInput(WireModel):
    workspace_id: WorkspaceId
    target_tool: ProtectedToolName
    target_arguments: UserJsonObject
    description: (
        Annotated[
            UserText,
            StringConstraints(strict=True, min_length=1, max_length=1000),
        ]
        | None
    ) = None


class MemoryRecallInput(WireModel):
    workspace_id: WorkspaceId
    query: Annotated[
        UserText,
        StringConstraints(strict=True, min_length=1, max_length=2000),
    ]
    limit: Annotated[int, Field(ge=1, le=50)] = 10
    candidate_limit: Annotated[int, Field(ge=1, le=200)] = 50
    categories: RecordTypeSet | None = None
    tags: TagSet | None = None
    record_ids: RecordIdSet100 | None = None
    linked_workspace_ids: WorkspaceIdSet = Field(default_factory=set)
    as_of_valid_time: AwareDateTime | None = None
    as_of_transaction_time: AwareDateTime | None = None
    include_invalidated: bool = False
    include_archived: bool = False
    token_budget: Annotated[int, Field(ge=256, le=16_000)] = 2400
    rerank: bool = False
    intent: Literal["explore", "implement", "debug", "review"] | None = None

    @model_validator(mode="after")
    def validate_candidate_limit(self) -> MemoryRecallInput:
        if self.candidate_limit < self.limit:
            raise ValueError("candidate_limit cannot be smaller than limit")
        return self


class OutcomeVerification(WireModel):
    kind: Literal["test", "command", "review", "self_report"]
    command: UserMediumText | None = None
    exit_code: Annotated[int, Field(ge=-2_147_483_648, le=2_147_483_647)] | None = None

    @model_validator(mode="after")
    def validate_command_evidence(self) -> OutcomeVerification:
        if self.kind in {"review", "self_report"} and (
            self.command is not None or self.exit_code is not None
        ):
            raise ValueError(
                "review and self_report verification cannot carry command evidence"
            )
        return self


class CodeRef(WireModel):
    relative_file_path: RelativePath
    qualified_name: UserNameText | None = None


class MemoryStoreInput(WireModel):
    workspace_id: WorkspaceId
    record_type: RecordType
    content: UserLongText
    rationale: (
        Annotated[
            UserText,
            StringConstraints(strict=True, min_length=1, max_length=20_000),
        ]
        | None
    ) = None
    context: RecordContext = Field(default_factory=dict)
    tags: UniqueTags = Field(default_factory=list, max_length=32)
    relative_file_path: RelativePath | None = None
    happened_at: AwareDateTime | None = None
    procedure_steps: list[UserMediumText] = Field(default_factory=list, max_length=100)
    informed_by: InformedByRecordIds = Field(default_factory=list, max_length=32)
    code_refs: list[CodeRef] = Field(default_factory=list, max_length=16)
    idempotency_key: IdempotencyKey
    preflight_token: PreflightToken | None = None

    @model_validator(mode="after")
    def validate_procedure_steps(self) -> MemoryStoreInput:
        if self.record_type != "procedure" and self.procedure_steps:
            raise ValueError("procedure_steps are only valid for procedure records")
        return self


class EditPreflightInput(WireModel):
    workspace_id: WorkspaceId
    edit_request_id: EditRequestId
    description: UserMediumText


class MemoryCaptureListInput(WireModel):
    workspace_id: WorkspaceId
    cursor: Cursor | None = None
    limit: Annotated[int, Field(ge=1, le=100)] = 25


class MemoryCapturePromoteInput(WireModel):
    workspace_id: WorkspaceId
    candidate_id: CaptureCandidateId
    record_type: RecordType
    content: Annotated[
        UserText,
        StringConstraints(strict=True, min_length=1, max_length=8_000),
    ]
    rationale: OptionalUserMediumText = None
    context: RecordContext = Field(default_factory=dict)
    tags: UniqueTags = Field(default_factory=list, max_length=16)
    idempotency_key: IdempotencyKey
    preflight_token: PreflightToken | None = None


class MemoryRecordOutcomeInput(WireModel):
    workspace_id: WorkspaceId
    record_id: RecordId
    outcome_text: Annotated[
        UserText,
        StringConstraints(strict=True, min_length=1, max_length=50_000),
    ]
    worked: bool
    happened_at: AwareDateTime | None = None
    informed_by: InformedByRecordIds = Field(default_factory=list, max_length=32)
    verification: OutcomeVerification | None = None
    rebind_code: bool = False
    idempotency_key: IdempotencyKey


class SystemHealthInput(WireModel):
    workspace_id: WorkspaceId | None = None
    include_components: bool = True


class ActiveContextListInput(WireModel):
    workspace_id: WorkspaceId
    cursor: Cursor | None = None
    limit: Annotated[int, Field(ge=1, le=100)] = 50


class ContextTriggersMatchInput(WireModel):
    workspace_id: WorkspaceId
    relative_file_path: RelativePath | None = None
    tags: UniqueTags = Field(default_factory=list, max_length=32)
    entities: list[UserNameText] = Field(default_factory=list, max_length=32)
    limit: Annotated[int, Field(ge=1, le=20)] = 5


class SessionUpdatesGetInput(WireModel):
    workspace_id: WorkspaceId
    after_cursor: Cursor | None = None
    since: AwareDateTime | None = None
    wait_seconds: Annotated[int, Field(ge=0, le=30)] = 0


class CovenantStatusInput(WireModel):
    workspace_id: WorkspaceId


class MemoryRecallFileInput(WireModel):
    workspace_id: WorkspaceId
    relative_file_path: RelativePath
    cursor: Cursor | None = None
    limit: Annotated[int, Field(ge=1, le=100)] = 20


class MemoryRecallEntityInput(WireModel):
    workspace_id: WorkspaceId
    entity_id: EntityId | None = None
    entity_name: UserNameText | None = None
    entity_type: EntityType | None = None
    cursor: Cursor | None = None
    limit: Annotated[int, Field(ge=1, le=100)] = 20

    @model_validator(mode="after")
    def validate_selector(self) -> MemoryRecallEntityInput:
        if (self.entity_id is None) == (self.entity_name is None):
            raise ValueError("exactly one of entity_id or entity_name is required")
        return self


class MemoryRecallHierarchicalInput(WireModel):
    workspace_id: WorkspaceId
    query: Annotated[
        UserText,
        StringConstraints(strict=True, min_length=1, max_length=2000),
    ]
    include_members: bool = False
    limit: Annotated[int, Field(ge=1, le=50)] = 10


class MemorySearchTextInput(WireModel):
    workspace_id: WorkspaceId
    query: Annotated[
        UserText,
        StringConstraints(strict=True, min_length=1, max_length=2000),
    ]
    cursor: Cursor | None = None
    limit: Annotated[int, Field(ge=1, le=100)] = 20
    include_metadata: bool = False
    highlight: bool = False


class RuleCheckInput(WireModel):
    workspace_id: WorkspaceId
    proposed_action: UserMediumText
    context: ContextJsonObject = Field(default_factory=dict)


class ContextCompressInput(WireModel):
    workspace_id: WorkspaceId
    text: Annotated[
        UserText,
        StringConstraints(strict=True, min_length=1, max_length=500_000),
    ]
    rate: Annotated[float, Field(ge=0.1, le=1, allow_inf_nan=False)] | None = None
    content_type: UserShortText | None = None
    preserve_code: bool = True


class MemoryCreate(WireModel):
    record_type: RecordType
    content: UserLongText
    rationale: (
        Annotated[
            UserText,
            StringConstraints(strict=True, min_length=1, max_length=20_000),
        ]
        | None
    ) = None
    context: RecordContext = Field(default_factory=dict)
    tags: UniqueTags = Field(default_factory=list, max_length=32)
    relative_file_path: RelativePath | None = None
    happened_at: AwareDateTime | None = None
    procedure_steps: list[UserMediumText] = Field(default_factory=list, max_length=100)

    @model_validator(mode="after")
    def validate_procedure_steps(self) -> MemoryCreate:
        if self.record_type != "procedure" and self.procedure_steps:
            raise ValueError("procedure_steps are only valid for procedure records")
        return self


class MemoryStoreBatchInput(WireModel):
    workspace_id: WorkspaceId
    records: list[MemoryCreate] = Field(min_length=1, max_length=100)
    idempotency_key: IdempotencyKey
    preflight_token: PreflightToken | None = None


class MemoryLinkInput(WireModel):
    workspace_id: WorkspaceId
    source_record_id: RecordId
    target_record_id: RecordId
    relationship_type: RelationshipType
    description: OptionalUserMediumText = None
    confidence: Annotated[float, Field(ge=0, le=1, allow_inf_nan=False)] = 1.0
    idempotency_key: IdempotencyKey
    preflight_token: PreflightToken | None = None

    @model_validator(mode="after")
    def validate_endpoints(self) -> MemoryLinkInput:
        if self.source_record_id == self.target_record_id:
            raise ValueError("relationship endpoints must be distinct")
        return self


class MemoryUnlinkInput(WireModel):
    workspace_id: WorkspaceId
    relationship_id: RelationshipId
    preflight_token: PreflightToken | None = None


class MemoryPinSetInput(WireModel):
    workspace_id: WorkspaceId
    record_id: RecordId
    pinned: bool
    preflight_token: PreflightToken | None = None


class ActiveContextAddInput(WireModel):
    workspace_id: WorkspaceId
    record_id: RecordId
    reason: OptionalUserMediumText = None
    priority: Annotated[int, Field(ge=-100, le=100)] = 0
    expires_at: AwareDateTime | None = None
    preflight_token: PreflightToken | None = None


class ActiveContextRemoveInput(WireModel):
    workspace_id: WorkspaceId
    active_context_id: ActiveContextId
    preflight_token: PreflightToken | None = None


class ActiveContextClearInput(WireModel):
    workspace_id: WorkspaceId
    selection_token: SelectionToken
    preflight_token: PreflightToken | None = None


class DocumentIngestUrlInput(WireModel):
    workspace_id: WorkspaceId
    url: HttpsUrl
    topic: UserMediumText
    chunk_size: Annotated[int, Field(ge=256, le=16_000)] = 2000
    idempotency_key: IdempotencyKey
    preflight_token: PreflightToken | None = None


class MemoryVerifyInput(WireModel):
    workspace_id: WorkspaceId
    text: Annotated[
        UserText,
        StringConstraints(strict=True, min_length=1, max_length=100_000),
    ]
    categories: RecordTypeSet | None = None
    as_of_valid_time: AwareDateTime | None = None
    as_of_transaction_time: AwareDateTime | None = None


class SandboxExecutePythonInput(WireModel):
    workspace_id: WorkspaceId
    code: Annotated[
        UserText,
        StringConstraints(strict=True, min_length=1, max_length=100_000),
    ]
    timeout_seconds: Annotated[int, Field(ge=1, le=60)] = 30
    preflight_token: PreflightToken | None = None


class CodeIndexInput(WireModel):
    workspace_id: WorkspaceId
    relative_root: RelativePath = "."
    patterns: list[RelativePath] = Field(
        default_factory=lambda: ["**/*"], min_length=1, max_length=32
    )
    force: bool = False


class CodeSearchInput(WireModel):
    workspace_id: WorkspaceId
    query: Annotated[
        UserText,
        StringConstraints(strict=True, min_length=1, max_length=2000),
    ]
    cursor: Cursor | None = None
    limit: Annotated[int, Field(ge=1, le=100)] = 20
    entity_kinds: CodeEntityKindSet | None = None


class CodeImpactAnalyzeInput(WireModel):
    workspace_id: WorkspaceId
    code_entity_id: CodeEntityId | None = None
    qualified_name: UserNameText | None = None
    max_depth: Annotated[int, Field(ge=1, le=10)] = 3

    @model_validator(mode="after")
    def validate_selector(self) -> CodeImpactAnalyzeInput:
        if (self.code_entity_id is None) == (self.qualified_name is None):
            raise ValueError(
                "exactly one of code_entity_id or qualified_name is required"
            )
        return self


class CodeTodosScanInput(WireModel):
    workspace_id: WorkspaceId
    relative_root: RelativePath = "."
    types: TodoTypeSet | None = None
    cursor: Cursor | None = None
    limit: Annotated[int, Field(ge=1, le=500)] = 100


class CodeTodosScanAndStoreInput(WireModel):
    workspace_id: WorkspaceId
    relative_root: RelativePath = "."
    types: TodoTypeSet | None = None
    cursor: Cursor | None = None
    limit: Annotated[int, Field(ge=1, le=500)] = 100
    record_type: Literal["warning"] = "warning"
    idempotency_key: IdempotencyKey
    preflight_token: PreflightToken | None = None


class CodeRefactorProposeInput(WireModel):
    workspace_id: WorkspaceId
    relative_file_path: RelativePath
    objective: OptionalUserMediumText = None


class RuleCreateInput(WireModel):
    workspace_id: WorkspaceId
    trigger: UserMediumText
    must_do: list[UserMediumText] = Field(default_factory=list, max_length=50)
    must_not: list[UserMediumText] = Field(default_factory=list, max_length=50)
    ask_first: list[UserMediumText] = Field(default_factory=list, max_length=50)
    warnings: list[UserMediumText] = Field(default_factory=list, max_length=50)
    priority: Annotated[int, Field(ge=-1000, le=1000)] = 0
    idempotency_key: IdempotencyKey
    preflight_token: PreflightToken | None = None


class RulePatch(WireModel):
    trigger: UserMediumText | None = None
    must_do: list[UserMediumText] | None = Field(default=None, max_length=50)
    must_not: list[UserMediumText] | None = Field(default=None, max_length=50)
    ask_first: list[UserMediumText] | None = Field(default=None, max_length=50)
    warnings: list[UserMediumText] | None = Field(default=None, max_length=50)
    priority: Annotated[int, Field(ge=-1000, le=1000)] | None = None
    enabled: bool | None = None

    @model_validator(mode="after")
    def validate_non_empty(self) -> RulePatch:
        if not self.model_fields_set or not self.model_dump(exclude_none=True):
            raise ValueError("rule patch must contain at least one field")
        return self


class RuleUpdateInput(WireModel):
    workspace_id: WorkspaceId
    rule_id: RuleId
    patch: RulePatch
    preflight_token: PreflightToken | None = None


class RuleListInput(WireModel):
    workspace_id: WorkspaceId
    enabled_only: bool = True
    cursor: Cursor | None = None
    limit: Annotated[int, Field(ge=1, le=100)] = 50


class ContextTriggerCreateInput(WireModel):
    workspace_id: WorkspaceId
    trigger_type: TriggerType
    pattern: UserMediumText
    recall_query: UserMediumText
    categories: RecordTypeSet | None = None
    enabled: bool = True
    idempotency_key: IdempotencyKey
    preflight_token: PreflightToken | None = None


class ContextTriggerListInput(WireModel):
    workspace_id: WorkspaceId
    active_only: bool = True
    cursor: Cursor | None = None
    limit: Annotated[int, Field(ge=1, le=100)] = 50


class ContextTriggerDeleteInput(WireModel):
    workspace_id: WorkspaceId
    trigger_id: TriggerId
    preflight_token: PreflightToken | None = None


class MemoryRelatedInput(WireModel):
    workspace_id: WorkspaceId
    record_id: RecordId
    relationship_types: RelationshipTypeSet | None = None
    direction: RelationshipDirection = "both"
    max_depth: Annotated[int, Field(ge=1, le=10)] = 2


class MemoryChainTraceInput(WireModel):
    workspace_id: WorkspaceId
    start_record_id: RecordId
    end_record_id: RecordId
    max_depth: Annotated[int, Field(ge=1, le=10)] = 5


class KnowledgeGraphGetInput(WireModel):
    workspace_id: WorkspaceId
    record_ids: RecordIdSet500 | None = None
    query: (
        Annotated[
            UserText,
            StringConstraints(strict=True, min_length=1, max_length=2000),
        ]
        | None
    ) = None
    include_orphans: bool = False
    max_nodes: Annotated[int, Field(ge=1, le=500)] = 500


class KnowledgeGraphRenderInput(KnowledgeGraphGetInput):
    format: Literal["mermaid"] = "mermaid"


class KnowledgeGraphStatsInput(WireModel):
    workspace_id: WorkspaceId


class CommunityListInput(WireModel):
    workspace_id: WorkspaceId
    level: Annotated[int, Field(ge=0, le=32)] | None = None
    parent_community_id: CommunityId | None = None
    cursor: Cursor | None = None
    limit: Annotated[int, Field(ge=1, le=100)] = 50


class CommunityGetInput(WireModel):
    workspace_id: WorkspaceId
    community_id: CommunityId
    include_members: bool = True
    cursor: Cursor | None = None
    limit: Annotated[int, Field(ge=1, le=500)] = 100


class CommunityRebuildInput(WireModel):
    workspace_id: WorkspaceId
    min_community_size: Annotated[int, Field(ge=2, le=1000)] = 2
    resolution: Annotated[float, Field(gt=0, le=100, allow_inf_nan=False)] = 1.0
    idempotency_key: IdempotencyKey
    preflight_token: PreflightToken | None = None


class EntityListInput(WireModel):
    workspace_id: WorkspaceId
    entity_type: EntityType | None = None
    cursor: Cursor | None = None
    limit: Annotated[int, Field(ge=1, le=100)] = 20


class EntityBackfillInput(WireModel):
    workspace_id: WorkspaceId
    force: bool = False
    idempotency_key: IdempotencyKey
    preflight_token: PreflightToken | None = None


class EntityEvolutionTraceInput(WireModel):
    workspace_id: WorkspaceId
    entity_id: EntityId | None = None
    entity_name: UserNameText | None = None
    entity_type: EntityType | None = None
    include_invalidated: bool = False

    @model_validator(mode="after")
    def validate_selector(self) -> EntityEvolutionTraceInput:
        if (self.entity_id is None) == (self.entity_name is None):
            raise ValueError("exactly one of entity_id or entity_name is required")
        return self


class MemoryVersionsListInput(WireModel):
    workspace_id: WorkspaceId
    record_id: RecordId
    cursor: Cursor | None = None
    limit: Annotated[int, Field(ge=1, le=100)] = 20


class MemoryAtTimeGetInput(WireModel):
    workspace_id: WorkspaceId
    record_id: RecordId
    valid_time: AwareDateTime
    transaction_time: AwareDateTime | None = None


class MemoryPrunePreviewInput(WireModel):
    workspace_id: WorkspaceId
    older_than_days: Annotated[int, Field(ge=1, le=36_500)] = 90
    categories: RecordTypeSet | None = None
    min_recall_count: Annotated[int, Field(ge=0, le=1_000_000)] = 5
    protect_successful: bool = True


class MemoryPruneInput(MemoryPrunePreviewInput):
    selection_token: SelectionToken
    preflight_token: PreflightToken | None = None


class MemoryArchiveSetInput(WireModel):
    workspace_id: WorkspaceId
    record_id: RecordId
    archived: bool
    preflight_token: PreflightToken | None = None


class MemoryDuplicatesPreviewInput(WireModel):
    workspace_id: WorkspaceId
    merge_duplicates: bool = True


class MemoryDuplicatesCleanupInput(MemoryDuplicatesPreviewInput):
    selection_token: SelectionToken
    preflight_token: PreflightToken | None = None


class MemoryCompactionPreviewInput(WireModel):
    workspace_id: WorkspaceId
    summary: Annotated[
        UserText,
        StringConstraints(strict=True, min_length=1, max_length=50_000),
    ]
    limit: Annotated[int, Field(ge=1, le=100)] = 10
    query: (
        Annotated[
            UserText,
            StringConstraints(strict=True, min_length=1, max_length=2000),
        ]
        | None
    ) = None


class MemoryCompactInput(MemoryCompactionPreviewInput):
    selection_token: SelectionToken
    idempotency_key: IdempotencyKey
    preflight_token: PreflightToken | None = None


class ProjectionRebuildInput(WireModel):
    workspace_id: WorkspaceId
    projection: RebuildableProjectionName
    force: bool = False


class WorkspaceExportInput(WireModel):
    workspace_id: WorkspaceId
    include_legacy_projection: bool = True
    include_vectors: bool = False
    export_session_id: TransferSessionId | None = None
    page_index: Annotated[int, Field(strict=True, ge=0)] = 0
    cursor: Cursor | None = None
    page_byte_limit: Annotated[int, Field(strict=True, ge=65_536, le=850_000)] = 850_000

    @model_validator(mode="after")
    def validate_export_page_request(self) -> WorkspaceExportInput:
        if self.export_session_id is None:
            if self.page_index != 0 or self.cursor is not None:
                raise ValueError("a new export starts at page zero without a cursor")
        elif self.page_index > 0 and self.cursor is None:
            raise ValueError("subsequent export pages require a cursor")
        return self


class WorkspaceImportInput(WireModel):
    workspace_id: WorkspaceId
    bundle: ExportBundle | None = None
    import_session_id: TransferSessionId | None = None
    finalize: bool = True
    merge: bool = True
    idempotency_key: IdempotencyKey
    preflight_token: PreflightToken | None = None

    @model_validator(mode="after")
    def validate_import_action(self) -> WorkspaceImportInput:
        if self.bundle is None:
            if not self.finalize or self.import_session_id is None:
                raise ValueError("finalize without a page requires an import session")
            return self
        if self.bundle.bundle_version == 1 and self.import_session_id is not None:
            raise ValueError("format-1 import is a single atomic action")
        return self


class WorkspaceLinkInput(WireModel):
    workspace_id: WorkspaceId
    linked_workspace_id: WorkspaceId
    relationship: WorkspaceRelationship = "related"
    label: OptionalUserMediumText = None
    preflight_token: PreflightToken | None = None

    @model_validator(mode="after")
    def validate_link(self) -> WorkspaceLinkInput:
        if self.linked_workspace_id == self.workspace_id:
            raise ValueError("a workspace cannot link to itself")
        return self


class WorkspaceUnlinkInput(WireModel):
    workspace_id: WorkspaceId
    linked_workspace_id: WorkspaceId
    preflight_token: PreflightToken | None = None


class WorkspaceLinksListInput(WireModel):
    workspace_id: WorkspaceId
    cursor: Cursor | None = None
    limit: Annotated[int, Field(ge=1, le=100)] = 50


class WorkspaceConsolidationPreviewInput(WireModel):
    workspace_id: WorkspaceId
    source_workspace_ids: WorkspaceIdSet = Field(min_length=1)

    @model_validator(mode="after")
    def validate_sources(self) -> WorkspaceConsolidationPreviewInput:
        if self.workspace_id in self.source_workspace_ids:
            raise ValueError("target workspace cannot be a consolidation source")
        return self


class WorkspaceConsolidateInput(WorkspaceConsolidationPreviewInput):
    idempotency_key: IdempotencyKey
    selection_token: SelectionToken
    preflight_token: PreflightToken | None = None


class WorkspaceConsolidateAndArchiveSourcesInput(WorkspaceConsolidateInput):
    pass


class DreamDuplicatesPreviewInput(WireModel):
    workspace_id: WorkspaceId


class DreamDuplicatesPurgeInput(WireModel):
    workspace_id: WorkspaceId
    selection_token: SelectionToken
    preflight_token: PreflightToken | None = None


class DecisionSimulateInput(WireModel):
    workspace_id: WorkspaceId
    record_id: RecordId
    as_of_transaction_time: AwareDateTime | None = None


class RuleEvolutionAnalyzeInput(WireModel):
    workspace_id: WorkspaceId
    rule_id: RuleId | None = None


class DecisionDebateInput(WireModel):
    workspace_id: WorkspaceId
    topic: UserMediumText
    advocate_position: Annotated[
        UserText,
        StringConstraints(strict=True, min_length=1, max_length=20_000),
    ]
    challenger_position: Annotated[
        UserText,
        StringConstraints(strict=True, min_length=1, max_length=20_000),
    ]
    max_rounds: Annotated[int, Field(ge=1, le=20)] = 5
    idempotency_key: IdempotencyKey
    preflight_token: PreflightToken | None = None


SessionBriefOutput = ApiResponse[SessionBriefData]
MemoryPreflightOutput = ApiResponse[PreflightData]
MemoryRecallOutput = ApiResponse[RetrievalData]
MemoryStoreOutput = ApiResponse[MemoryStoreData]
MemoryRecordOutcomeOutput = ApiResponse[OutcomeData]
SystemHealthOutput = ApiResponse[HealthData]


_TOOL_MODELS: Mapping[str, tuple[type[WireModel], type[WireModel]]] = MappingProxyType(
    {
        "daem0n_tools_search": (ToolsSearchInput, ToolsSearchData),
        "daem0n_tool_call": (ToolCallInput, ToolCallData),
        "active_context_add": (ActiveContextAddInput, ActiveContextItem),
        "active_context_clear": (
            ActiveContextClearInput,
            DestructiveMutationReceipt,
        ),
        "active_context_list": (
            ActiveContextListInput,
            ActiveContextPage,
        ),
        "active_context_remove": (
            ActiveContextRemoveInput,
            MutationReceipt,
        ),
        "code_impact_analyze": (CodeImpactAnalyzeInput, CodeImpactData),
        "code_index": (CodeIndexInput, CodeIndexData),
        "code_refactor_propose": (
            CodeRefactorProposeInput,
            RefactorProposalData,
        ),
        "code_search": (CodeSearchInput, Page[CodeEntitySummary]),
        "code_todos_scan": (CodeTodosScanInput, Page[TodoFinding]),
        "code_todos_scan_and_store": (
            CodeTodosScanAndStoreInput,
            CodeTodosStoreData,
        ),
        "community_get": (CommunityGetInput, CommunityDetail),
        "community_list": (CommunityListInput, Page[CommunitySummary]),
        "community_rebuild": (CommunityRebuildInput, CommunityRebuildData),
        "context_compress": (ContextCompressInput, ContextCompressData),
        "context_trigger_create": (ContextTriggerCreateInput, TriggerView),
        "context_trigger_delete": (
            ContextTriggerDeleteInput,
            MutationReceipt,
        ),
        "context_trigger_list": (
            ContextTriggerListInput,
            Page[TriggerView],
        ),
        "context_triggers_match": (
            ContextTriggersMatchInput,
            TriggerMatchData,
        ),
        "covenant_status": (CovenantStatusInput, CovenantStatusData),
        "decision_debate": (DecisionDebateInput, DecisionDebateData),
        "decision_simulate": (DecisionSimulateInput, DecisionSimulationData),
        "document_ingest_url": (DocumentIngestUrlInput, DocumentIngestData),
        "edit_preflight": (EditPreflightInput, EditPreflightData),
        "dream_duplicates_preview": (DreamDuplicatesPreviewInput, Preview),
        "dream_duplicates_purge": (
            DreamDuplicatesPurgeInput,
            DestructiveMutationReceipt,
        ),
        "entity_backfill": (EntityBackfillInput, EntityBackfillData),
        "entity_evolution_trace": (
            EntityEvolutionTraceInput,
            EntityEvolutionData,
        ),
        "entity_list": (EntityListInput, Page[EntitySummary]),
        "knowledge_graph_get": (KnowledgeGraphGetInput, KnowledgeGraphData),
        "knowledge_graph_render": (
            KnowledgeGraphRenderInput,
            KnowledgeGraphRenderData,
        ),
        "knowledge_graph_stats": (
            KnowledgeGraphStatsInput,
            KnowledgeGraphStatsData,
        ),
        "memory_archive_set": (MemoryArchiveSetInput, MutationReceipt),
        "memory_at_time_get": (MemoryAtTimeGetInput, MemoryAtTimeData),
        "memory_capture_list": (MemoryCaptureListInput, MemoryCaptureListData),
        "memory_capture_promote": (
            MemoryCapturePromoteInput,
            MemoryCapturePromoteData,
        ),
        "memory_chain_trace": (MemoryChainTraceInput, MemoryChainTraceData),
        "memory_compact": (MemoryCompactInput, MemoryCompactData),
        "memory_compaction_preview": (
            MemoryCompactionPreviewInput,
            Preview,
        ),
        "memory_duplicates_cleanup": (
            MemoryDuplicatesCleanupInput,
            DestructiveMutationReceipt,
        ),
        "memory_duplicates_preview": (
            MemoryDuplicatesPreviewInput,
            Preview,
        ),
        "memory_link": (MemoryLinkInput, MutationReceipt),
        "memory_pin_set": (MemoryPinSetInput, MutationReceipt),
        "memory_preflight": (MemoryPreflightInput, PreflightData),
        "memory_prune": (MemoryPruneInput, DestructiveMutationReceipt),
        "memory_prune_preview": (MemoryPrunePreviewInput, Preview),
        "memory_recall": (MemoryRecallInput, RetrievalData),
        "memory_recall_entity": (
            MemoryRecallEntityInput,
            Page[RecordSummary],
        ),
        "memory_recall_file": (MemoryRecallFileInput, Page[RecordSummary]),
        "memory_recall_hierarchical": (
            MemoryRecallHierarchicalInput,
            HierarchicalRecallData,
        ),
        "memory_record_outcome": (MemoryRecordOutcomeInput, OutcomeData),
        "memory_related": (MemoryRelatedInput, MemoryRelatedData),
        "memory_search_text": (MemorySearchTextInput, Page[TextSearchHit]),
        "memory_store": (MemoryStoreInput, MemoryStoreData),
        "memory_store_batch": (MemoryStoreBatchInput, MemoryStoreBatchData),
        "memory_unlink": (MemoryUnlinkInput, MutationReceipt),
        "memory_verify": (MemoryVerifyInput, MemoryVerifyData),
        "memory_versions_list": (
            MemoryVersionsListInput,
            Page[MemoryVersionView],
        ),
        "projection_rebuild": (ProjectionRebuildInput, ProjectionRebuildData),
        "rule_check": (RuleCheckInput, RuleCheckData),
        "rule_create": (RuleCreateInput, RuleView),
        "rule_evolution_analyze": (
            RuleEvolutionAnalyzeInput,
            RuleEvolutionData,
        ),
        "rule_list": (RuleListInput, Page[RuleView]),
        "rule_update": (RuleUpdateInput, RuleView),
        "sandbox_execute_python": (
            SandboxExecutePythonInput,
            SandboxExecutionData,
        ),
        "session_brief": (SessionBriefInput, SessionBriefData),
        "session_updates_get": (SessionUpdatesGetInput, SessionUpdatesData),
        "system_health": (SystemHealthInput, HealthData),
        "workspace_consolidate": (
            WorkspaceConsolidateInput,
            WorkspaceConsolidateData,
        ),
        "workspace_consolidation_preview": (
            WorkspaceConsolidationPreviewInput,
            WorkspaceConsolidationPreviewData,
        ),
        "workspace_consolidate_and_archive_sources": (
            WorkspaceConsolidateAndArchiveSourcesInput,
            WorkspaceConsolidateArchiveData,
        ),
        "workspace_export": (WorkspaceExportInput, ExportBundle),
        "workspace_import": (WorkspaceImportInput, WorkspaceImportData),
        "workspace_link": (WorkspaceLinkInput, WorkspaceLinkView),
        "workspace_links_list": (
            WorkspaceLinksListInput,
            Page[WorkspaceLinkView],
        ),
        "workspace_unlink": (WorkspaceUnlinkInput, MutationReceipt),
    }
)

TOOL_INPUT_MODELS: Mapping[str, type[WireModel]] = MappingProxyType(
    {name: models[0] for name, models in _TOOL_MODELS.items()}
)
TOOL_DATA_MODELS: Mapping[str, type[WireModel]] = MappingProxyType(
    {name: models[1] for name, models in _TOOL_MODELS.items()}
)


def build_argument_normalizer() -> V7ArgumentNormalizer:
    """Build the Covenant normalizer from the same authoritative schemas."""

    return V7ArgumentNormalizer(TOOL_INPUT_MODELS)


_OPTIONAL_TOOLS = frozenset(
    {
        "memory_recall",
        "memory_recall_hierarchical",
        "context_compress",
        "memory_store_batch",
        "document_ingest_url",
        "memory_verify",
        "sandbox_execute_python",
        "code_index",
        "code_impact_analyze",
        "code_todos_scan",
        "code_todos_scan_and_store",
        "code_refactor_propose",
        "memory_related",
        "memory_chain_trace",
        "knowledge_graph_get",
        "knowledge_graph_render",
        "community_rebuild",
        "entity_backfill",
        "entity_evolution_trace",
        "memory_prune_preview",
        "memory_prune",
        "memory_duplicates_preview",
        "memory_duplicates_cleanup",
        "memory_compaction_preview",
        "memory_compact",
        "projection_rebuild",
        "workspace_export",
        "workspace_import",
        "workspace_consolidate",
        "workspace_consolidate_and_archive_sources",
        "workspace_consolidation_preview",
        "dream_duplicates_preview",
        "dream_duplicates_purge",
        "decision_simulate",
        "rule_evolution_analyze",
        "decision_debate",
    }
)


_READ_ONLY_TOOLS = frozenset(
    {
        "daem0n_tools_search",
        "session_brief",
        "memory_preflight",
        "memory_capture_list",
        "memory_recall",
        "system_health",
        "active_context_list",
        "context_triggers_match",
        "session_updates_get",
        "covenant_status",
        "memory_recall_file",
        "memory_recall_entity",
        "memory_recall_hierarchical",
        "memory_search_text",
        "rule_check",
        "context_compress",
        "memory_verify",
        "code_search",
        "code_impact_analyze",
        "code_todos_scan",
        "code_refactor_propose",
        "rule_list",
        "context_trigger_list",
        "memory_related",
        "memory_chain_trace",
        "knowledge_graph_get",
        "knowledge_graph_render",
        "knowledge_graph_stats",
        "community_list",
        "community_get",
        "entity_list",
        "entity_evolution_trace",
        "memory_versions_list",
        "memory_at_time_get",
        "memory_prune_preview",
        "memory_duplicates_preview",
        "memory_compaction_preview",
        "workspace_export",
        "workspace_links_list",
        "workspace_consolidation_preview",
        "dream_duplicates_preview",
        "decision_simulate",
        "rule_evolution_analyze",
    }
)


_DESTRUCTIVE_TOOLS = frozenset(
    {
        "memory_unlink",
        "active_context_remove",
        "active_context_clear",
        "sandbox_execute_python",
        "context_trigger_delete",
        "memory_prune",
        "memory_archive_set",
        "memory_duplicates_cleanup",
        "memory_compact",
        "workspace_import",
        "workspace_unlink",
        "workspace_consolidate_and_archive_sources",
        "dream_duplicates_purge",
    }
)


_OPEN_WORLD_TOOLS = frozenset({"document_ingest_url", "sandbox_execute_python"})


_CATEGORY_TOOLS: Mapping[str, frozenset[str]] = MappingProxyType(
    {
        "session": frozenset({"session_brief", "session_updates_get"}),
        "covenant": frozenset({"memory_preflight", "covenant_status"}),
        "retrieval": frozenset(
            {
                "memory_recall",
                "memory_recall_file",
                "memory_recall_entity",
                "memory_recall_hierarchical",
                "memory_search_text",
            }
        ),
        "memory": frozenset(
            {
                "memory_store",
                "memory_record_outcome",
                "memory_store_batch",
                "memory_link",
                "memory_unlink",
                "memory_pin_set",
                "memory_verify",
                "memory_related",
                "memory_chain_trace",
                "memory_versions_list",
                "memory_at_time_get",
                "memory_capture_list",
                "memory_capture_promote",
            }
        ),
        "edit": frozenset({"edit_preflight"}),
        "system": frozenset(
            {"system_health", "daem0n_tools_search", "daem0n_tool_call"}
        ),
        "context": frozenset(
            {
                "active_context_list",
                "context_triggers_match",
                "context_compress",
                "active_context_add",
                "active_context_remove",
                "active_context_clear",
            }
        ),
        "external": frozenset({"document_ingest_url"}),
        "sandbox": frozenset({"sandbox_execute_python"}),
        "code": frozenset(
            {
                "code_index",
                "code_search",
                "code_impact_analyze",
                "code_todos_scan",
                "code_todos_scan_and_store",
                "code_refactor_propose",
            }
        ),
        "rules": frozenset(
            {
                "rule_check",
                "rule_create",
                "rule_update",
                "rule_list",
                "context_trigger_create",
                "context_trigger_list",
                "context_trigger_delete",
            }
        ),
        "graph": frozenset(
            {
                "knowledge_graph_get",
                "knowledge_graph_render",
                "knowledge_graph_stats",
            }
        ),
        "communities": frozenset(
            {"community_list", "community_get", "community_rebuild"}
        ),
        "entities": frozenset(
            {"entity_list", "entity_backfill", "entity_evolution_trace"}
        ),
        "maintenance": frozenset(
            {
                "memory_prune_preview",
                "memory_prune",
                "memory_archive_set",
                "memory_duplicates_preview",
                "memory_duplicates_cleanup",
                "memory_compaction_preview",
                "memory_compact",
                "dream_duplicates_preview",
                "dream_duplicates_purge",
            }
        ),
        "projection": frozenset({"projection_rebuild"}),
        "workspace": frozenset(
            {
                "workspace_export",
                "workspace_import",
                "workspace_link",
                "workspace_unlink",
                "workspace_links_list",
                "workspace_consolidate",
                "workspace_consolidate_and_archive_sources",
                "workspace_consolidation_preview",
            }
        ),
        "cognitive": frozenset(
            {"decision_simulate", "rule_evolution_analyze", "decision_debate"}
        ),
    }
)


def _category_by_tool() -> Mapping[str, str]:
    categories: dict[str, str] = {}
    for category, names in _CATEGORY_TOOLS.items():
        for name in names:
            if name in categories:
                raise ManifestError(f"duplicate category for v7 tool: {name}")
            categories[name] = category
    return MappingProxyType(categories)


_TOOL_CATEGORIES = _category_by_tool()
_DASHBOARD_TOOL_URIS = {
    "memory_recall": "ui://daem0n/search",
    "session_brief": "ui://daem0n/briefing",
    "covenant_status": "ui://daem0n/covenant",
    "community_list": "ui://daem0n/community",
    "knowledge_graph_get": "ui://daem0n/graph",
}
V7_NATIVE_TOOL_NAMES = frozenset(
    {
        "daem0n_tools_search",
        "daem0n_tool_call",
        "edit_preflight",
        "memory_capture_list",
        "memory_capture_promote",
        "workspace_consolidation_preview",
    }
)
_MAPPED_TOOL_NAMES = (
    frozenset(name for mapping in V6_TO_V7_MAPPINGS for name in mapping.new_tools)
    | V7_NATIVE_TOOL_NAMES
)


# One entry per tool: (title, description).  Each description is derived from
# the handler and the input model: what the tool does, when to prefer it over
# its neighbours, and its side effects.  The same text is listed to clients
# and is what ``daem0n_tools_search`` ranks, so write it for a caller who has
# not seen the code, and keep it under 300 characters.
_TOOL_TEXT: Mapping[str, tuple[str, str]] = MappingProxyType(
    {
        "session_brief": (
            "Session Briefing",
            "Start or refresh the session: returns workspace statistics, recent "
            "decisions, warnings, failed approaches, applicable rules, active "
            "context and git changes, and marks the session briefed. Read-only; "
            "untrusted scanned or ingested text is withheld.",
        ),
        "session_updates_get": (
            "Get Session Updates",
            "List what changed in the workspace since a cursor or time: new "
            "records, outcomes, rule, trigger and active-context events. Can wait "
            "up to 30 seconds for the next change. Use it to poll for news instead "
            "of a new briefing.",
        ),
        "covenant_status": (
            "Covenant Status",
            "Report whether this session has been briefed, when, how long a "
            "preflight token lasts and the next step required before protected "
            "work. Read-only and never gated.",
        ),
        "memory_preflight": (
            "Preflight a Protected Call",
            "Plan a protected write in advance: returns guidance from past "
            "warnings, failures and rules, plus a single-use token bound to one "
            "exact target tool and its arguments. Pass the token as "
            "preflight_token on that call. Optional outside strict mode.",
        ),
        "memory_recall": (
            "Recall Memories",
            "Find the memories most relevant to a question: ranked decisions, "
            "warnings, learnings and what failed or worked before and why, with "
            "citations inside a token budget. Filter by type, tag or time, or "
            "include linked workspaces. The default way to look up history.",
        ),
        "memory_recall_entity": (
            "Recall Memories by Entity",
            "List the memories that mention one extracted entity, chosen by "
            "entity id or by name, in pages. Use it after entity_list when you "
            "want everything known about a named function, class, file or concept.",
        ),
        "memory_recall_file": (
            "Recall Memories for a File",
            "List the memories attached to one file, given its path relative to "
            "the workspace root, most recently updated first, in pages. Use it "
            "before editing a file to see what we know about it.",
        ),
        "memory_recall_hierarchical": (
            "Recall by Community",
            "Answer a broad question from the community hierarchy: finds the "
            "clusters of related memories whose label or members match the query "
            "and returns them layer by layer. Needs a built graph projection; "
            "use memory_recall for a specific question.",
        ),
        "memory_search_text": (
            "Search Memory Text",
            "Full-text keyword search over memory content, in pages, with "
            "optional highlight spans. Use it for an exact word or phrase; use "
            "memory_recall for ranked, budgeted answers. Archived records are "
            "left out.",
        ),
        "memory_store": (
            "Store a Memory",
            "Remember something: save one decision, pattern, warning, learning "
            "(a lesson), procedure or observation with its rationale, tags, "
            "file path and the records that informed it. Replay-safe by "
            "idempotency_key. A gated write: it may answer COUNSEL_REQUIRED "
            "with the exact retry.",
        ),
        "memory_store_batch": (
            "Store Memories in Bulk",
            "Save up to 100 memory records in one replay-safe write under a "
            "single idempotency_key. Use it instead of repeated memory_store "
            "calls when recording several items at once. A gated write.",
        ),
        "memory_record_outcome": (
            "Record an Outcome",
            "Record whether a stored decision or approach worked or failed, "
            "with what happened and optional test or command evidence. Failed "
            "outcomes are shown in later briefings. Replay-safe by "
            "idempotency_key.",
        ),
        "memory_link": (
            "Link Two Memories",
            "Connect two memory records with a typed relationship such as "
            "led_to, supersedes, depends_on, conflicts_with, related_to or "
            "evidence_for, with an optional note and confidence. A gated write.",
        ),
        "memory_unlink": (
            "Remove a Memory Link",
            "Remove one relationship between two memories by its "
            "relationship_id. The records themselves are kept. Destructive: "
            "gated at the highest level.",
        ),
        "memory_pin_set": (
            "Pin or Unpin a Memory",
            "Pin or unpin one memory record. A pinned record is never selected "
            "by memory_prune or memory_compact, so pin what must stop being "
            "pruned or summarized away. A gated write.",
        ),
        "memory_verify": (
            "Verify Claims Against Memory",
            "Check a statement or draft against stored memories: splits the "
            "text into claims and marks each supported, contradicted or unknown "
            "with the evidence. Use it to see whether text contradicts what is "
            "stored. Read-only.",
        ),
        "memory_related": (
            "List Related Memories",
            "Walk the relationships out from one memory record, up to a chosen "
            "depth, direction and set of relationship types, and return the "
            "connected records and paths. Read-only.",
        ),
        "memory_chain_trace": (
            "Trace a Memory Chain",
            "Find the relationship paths that lead from one memory record to "
            "another, up to a maximum depth. Use it to explain how an early "
            "decision led to a later result. Read-only.",
        ),
        "memory_versions_list": (
            "List Memory Versions",
            "Show the history of one memory record: every version in order, "
            "each with its event, validity period and the time it was written. "
            "Use it to see how a record changed over time.",
        ),
        "memory_at_time_get": (
            "Get a Memory at a Point in Time",
            "Return one memory record as it was at a past moment: the version "
            "valid at valid_time, optionally as it was known at "
            "transaction_time. Use it to see how a record looked last week or "
            "last month.",
        ),
        "memory_capture_list": (
            "List Capture Candidates",
            "List the pending capture candidates: edits reported by a paired "
            "editor bridge that are waiting for review before they become "
            "memories. Read-only; promote one with memory_capture_promote.",
        ),
        "memory_capture_promote": (
            "Promote a Capture Candidate",
            "Turn one reviewed capture candidate into a stored memory record "
            "with the type, content, rationale and tags you supply. Replay-safe "
            "by idempotency_key. A gated write.",
        ),
        "edit_preflight": (
            "Preflight a Native Edit",
            "Issue the one-use receipt for a native file edit that a paired "
            "editor bridge has asked to approve, identified by edit_request_id. "
            "Only useful when the host created that edit request; it is not "
            "needed for memory writes.",
        ),
        "system_health": (
            "System Health",
            "Check that the server is healthy: package, API and storage "
            "versions, supported transports, task support, authentication mode, "
            "the state of each optional capability and dreaming. Read-only and "
            "never gated.",
        ),
        "daem0n_tools_search": (
            "Search Tools",
            "Find tools by what they do. Returns up to 10 matches with each "
            "tool's description, gate level, safety hints, input schema and "
            "whether it is listed directly. Call an unlisted match through "
            "daem0n_tool_call.",
        ),
        "daem0n_tool_call": (
            "Call a Tool by Name",
            "Call any registered tool found with daem0n_tools_search, passing "
            "its name and arguments, and relay its result or guidance. It does "
            "whatever the target does, so it can be destructive or reach the "
            "network.",
        ),
        "active_context_list": (
            "List Active Context",
            "List the memories currently held in active context, the short "
            "list kept in focus for this workspace and shown in every briefing. "
            "Also returns the selection_token that active_context_clear needs.",
        ),
        "active_context_add": (
            "Add to Active Context",
            "Keep one memory record in focus: adds it to active context with an "
            "optional reason, priority and expiry so it appears in every "
            "session briefing until removed. A gated write.",
        ),
        "active_context_remove": (
            "Remove from Active Context",
            "Drop one entry from active context by its active_context_id. The "
            "underlying memory record is kept. Destructive: gated at the "
            "highest level.",
        ),
        "active_context_clear": (
            "Clear Active Context",
            "Empty active context in one step. Needs the selection_token from "
            "active_context_list, so only the entries you were shown are "
            "cleared. Memory records are kept. Destructive.",
        ),
        "context_triggers_match": (
            "Match Context Triggers",
            "Given a file path, tags or entity names, return the context "
            "triggers that fire and the memories each one recalls. Use it when "
            "a file is opened or edited to recall memories automatically.",
        ),
        "context_compress": (
            "Compress Text",
            "Shorten a long text so it fits the context: keeps the leading "
            "share of its words set by rate (half by default) and does not cut "
            "inside a fenced code block. Deterministic, stores nothing.",
        ),
        "document_ingest_url": (
            "Ingest a Document from a URL",
            "Fetch a web page or online documentation over HTTPS, split its "
            "text into chunks and remember each chunk as a learning tagged "
            "document. Reaches the network; website text is stored as "
            "untrusted. A gated write.",
        ),
        "sandbox_execute_python": (
            "Run Python in a Sandbox",
            "Run Python code in an isolated remote E2B sandbox and return its "
            "output, errors and exit status. Needs E2B_API_KEY; not idempotent, "
            "reaches the network and is gated as destructive.",
        ),
        "code_index": (
            "Index Source Code",
            "Parse the source files under a folder into the code index of "
            "files, classes, functions and their relationships that code_search "
            "and code_impact_analyze read. Run it after the code changes; force "
            "rebuilds the index even when nothing changed.",
        ),
        "code_search": (
            "Search Code Entities",
            "Find indexed code entities such as classes, functions and methods "
            "whose name or file path contains the query, in pages. Needs a "
            "code_index run first. Read-only.",
        ),
        "code_impact_analyze": (
            "Analyze Code Impact",
            "Show what breaks if you change a function, class, method or other "
            "code entity: lists everything that depends on it, such as what "
            "calls, imports or inherits it, directly or through others up to "
            "max_depth, with the paths. Needs a code_index run first.",
        ),
        "code_todos_scan": (
            "Scan for TODO Comments",
            "Find TODO, FIXME, HACK, XXX and NOTE comments in the files under "
            "a folder and list each with its file and line, in pages. Stores "
            "nothing; use code_todos_scan_and_store to keep them.",
        ),
        "code_todos_scan_and_store": (
            "Scan and Store TODO Comments",
            "Scan for TODO, FIXME, HACK, XXX and NOTE comments and save each "
            "finding as a warning memory tagged code-todo. Scanned text is "
            "treated as untrusted. A gated write; use code_todos_scan to look "
            "without saving.",
        ),
        "code_refactor_propose": (
            "Propose a Refactoring",
            "Return a brief static review of one file: its line count, "
            "TODO-style markers and over-long lines, with a general "
            "refactoring suggestion toward your objective. Reads the file only "
            "and edits nothing.",
        ),
        "rule_check": (
            "Check Rules for an Action",
            "Before acting, ask whether it is allowed and which rules apply: "
            "matches a proposed action against the enabled rules and returns "
            "what you must do, must not do, should ask first and should be "
            "warned about. Read-only.",
        ),
        "rule_create": (
            "Create a Rule",
            "Add a standing rule: a trigger describing a situation, plus the "
            "must-do, must-not, ask-first and warning guidance that rule_check "
            "returns when it matches. A gated write.",
        ),
        "rule_update": (
            "Update a Rule",
            "Change one rule's trigger, guidance lists or priority, or turn it "
            "off and on with enabled. Rules are never deleted; disable one "
            "instead. A gated write.",
        ),
        "rule_list": (
            "List Rules",
            "List the rules in priority order, in pages; only enabled rules "
            "unless enabled_only is false. Use rule_check to see which apply to "
            "a specific action.",
        ),
        "context_trigger_create": (
            "Create a Context Trigger",
            "Create a trigger that recalls memories automatically: a file "
            "glob, tag or entity pattern paired with the recall query to run "
            "when it matches. context_triggers_match evaluates them. A gated "
            "write.",
        ),
        "context_trigger_list": (
            "List Context Triggers",
            "List the context triggers with their pattern, recall query and "
            "state, in pages; only enabled ones unless active_only is false.",
        ),
        "context_trigger_delete": (
            "Delete a Context Trigger",
            "Delete one context trigger by its trigger_id so it no longer "
            "fires. Destructive: gated at the highest level.",
        ),
        "knowledge_graph_get": (
            "Get the Knowledge Graph",
            "Return the graph of memory records and the relationships between "
            "them as nodes and edges, for chosen records, a text query or the "
            "whole workspace, up to 500 nodes. Read-only.",
        ),
        "knowledge_graph_render": (
            "Render the Knowledge Graph",
            "Draw the same graph as knowledge_graph_get as Mermaid diagram "
            "text you can show to a person. Read-only.",
        ),
        "knowledge_graph_stats": (
            "Knowledge Graph Statistics",
            "Count the nodes and edges of the knowledge graph by type and "
            "report which projection generation they come from. A cheap way to "
            "see whether a graph exists. Read-only.",
        ),
        "community_list": (
            "List Communities",
            "List the communities, the clusters of related memories found in "
            "the graph, by level or under a parent community, in pages. "
            "Read-only; build them with community_rebuild.",
        ),
        "community_get": (
            "Get a Community",
            "Return one community with its label, level and, unless turned "
            "off, a page of its member memories. Read-only.",
        ),
        "community_rebuild": (
            "Rebuild Communities",
            "Recompute the graph projection and its communities with a chosen "
            "minimum size and resolution, returning the clusters and their "
            "modularity. Replaces the previous communities. A gated write.",
        ),
        "entity_list": (
            "List Entities",
            "List the entities extracted from memories, such as functions, "
            "classes, files, modules and concepts, with how often each is "
            "mentioned, optionally by entity type, in pages. Read-only.",
        ),
        "entity_backfill": (
            "Backfill Entities",
            "Rebuild the graph projection by extracting entities from the "
            "stored memories again, reporting how many were scanned and "
            "extracted. Use it when entity_list is empty or stale; force "
            "rebuilds even when current. A gated write.",
        ),
        "entity_evolution_trace": (
            "Trace an Entity's History",
            "Show the timeline of one entity, chosen by id or name: the "
            "memory events that mention it in order, so you can see how "
            "thinking about it evolved. Read-only.",
        ),
        "memory_prune_preview": (
            "Preview a Memory Prune",
            "Dry run of memory_prune: counts and samples the old records, by "
            "default decisions and learnings, that would be deleted for the "
            "given age, categories and filters, and returns the "
            "selection_token memory_prune requires. Changes nothing.",
        ),
        "memory_prune": (
            "Prune Old Memories",
            "Remove old, stale memories for good: deletes records, by default "
            "decisions and learnings, older than older_than_days that are "
            "unpinned, unarchived, have no outcome and informed no later work. "
            "Needs the selection_token from memory_prune_preview. Destructive.",
        ),
        "memory_archive_set": (
            "Archive or Restore a Memory",
            "Archive one memory record to hide it from recall and search "
            "without deleting it, or restore it by setting archived to false. "
            "Gated as destructive in both directions.",
        ),
        "memory_duplicates_preview": (
            "Preview Duplicate Memories",
            "Find duplicate memories: repeated records with the same type, file "
            "and text once case and spacing are ignored. Counts and samples "
            "them and returns the selection_token memory_duplicates_cleanup "
            "requires. Changes nothing.",
        ),
        "memory_duplicates_cleanup": (
            "Clean Up Duplicate Memories",
            "Get rid of duplicate memories: merges each group of repeated "
            "records into its newest record and deletes the others. Needs the "
            "selection_token from memory_duplicates_preview. Destructive.",
        ),
        "memory_compaction_preview": (
            "Preview a Memory Compaction",
            "Dry run of memory_compact: selects the oldest unpinned learnings "
            "and decisions with a recorded outcome, optionally those matching "
            "a query, that your summary would replace, and returns the "
            "selection_token memory_compact requires. Changes nothing.",
        ),
        "memory_compact": (
            "Compact Memories into a Summary",
            "Summarize many old memories into one: stores your summary as a "
            "new record, links it to the selected sources and archives them. "
            "Needs the selection_token from memory_compaction_preview. "
            "Destructive.",
        ),
        "dream_duplicates_preview": (
            "Preview Duplicate Dream Records",
            "Find repeated records written by background dreaming: several "
            "re-evaluations of the same decision or several dream summaries "
            "from one day. Returns the selection_token dream_duplicates_purge "
            "requires. Changes nothing.",
        ),
        "dream_duplicates_purge": (
            "Purge Duplicate Dream Records",
            "Delete the repeated dreaming records found by "
            "dream_duplicates_preview, keeping the newest of each group. Needs "
            "its selection_token. Destructive.",
        ),
        "projection_rebuild": (
            "Rebuild a Projection",
            "Rebuild one derived index from the event log: lexical (the text "
            "search index), dense (vectors), graph, temporal, procedure or "
            "outcome. Use it when a projection is stale or failed; stored "
            "memories are not changed.",
        ),
        "workspace_export": (
            "Export the Workspace",
            "Back up the workspace: export everything in it as a verifiable "
            "backup bundle of its events, page by page, optionally with "
            "vectors. Read-only; feed the pages to workspace_import to restore "
            "the same workspace.",
        ),
        "workspace_import": (
            "Import a Workspace Backup",
            "Restore or import a backup made by workspace_export into the same "
            "workspace, in one call or page by page and then finalize. Events "
            "already present are skipped; with merge false a non-empty "
            "workspace is refused. Destructive.",
        ),
        "workspace_link": (
            "Link Another Workspace",
            "Link another workspace to this one as related so that "
            "memory_recall can search its memories too when you list it in "
            "linked_workspace_ids. Nothing is copied. A gated write.",
        ),
        "workspace_unlink": (
            "Unlink a Workspace",
            "Remove the link to another workspace so its memories can no "
            "longer be included in recall here. Neither workspace's records "
            "change. Destructive: gated at the highest level.",
        ),
        "workspace_links_list": (
            "List Workspace Links",
            "List the other workspaces linked to this one, with each link's "
            "relationship and label, in pages. Read-only.",
        ),
        "workspace_consolidation_preview": (
            "Preview a Workspace Consolidation",
            "Dry run of merging other workspaces into this one: counts the "
            "live memories and bytes that would be copied from the source "
            "workspaces and returns the selection_token the consolidate tools "
            "require. Changes nothing.",
        ),
        "workspace_consolidate": (
            "Consolidate Workspaces",
            "Merge other projects into this one: copies the live memories of "
            "the source workspaces into this workspace and leaves the sources "
            "untouched. Needs the selection_token from "
            "workspace_consolidation_preview. A gated write.",
        ),
        "workspace_consolidate_and_archive_sources": (
            "Consolidate and Archive Sources",
            "Like workspace_consolidate, then archives the copied records in "
            "the source workspaces so they stop appearing there. Needs the "
            "selection_token from workspace_consolidation_preview. "
            "Destructive.",
        ),
        "decision_simulate": (
            "Simulate a Past Decision",
            "Replay a decision against today's knowledge: compares the "
            "memories relevant when it was made with those relevant now and "
            "lists evidence that is new, changed or gone. Use it to ask "
            "whether a decision would still be made. Read-only.",
        ),
        "rule_evolution_analyze": (
            "Analyze Rule Evolution",
            "Review how well rules are holding up: for one rule or all, "
            "reports its age, revision count and how many matching memories "
            "worked, failed or are unresolved. Read-only; change a rule with "
            "rule_update.",
        ),
        "decision_debate": (
            "Debate a Decision",
            "Weigh two opposing positions on a topic against stored memory "
            "evidence, round by round, and save the synthesis as a new "
            "decision record tagged decision-debate. Deterministic scoring, no "
            "model call. A gated write.",
        ),
    }
)


def _tags(name: str, category: str) -> tuple[str, ...]:
    return tuple(dict.fromkeys((category, *name.split("_"))))


_GATEWAY_CALL_TOOL = "daem0n_tool_call"
_GATEWAY_TOOLS = frozenset({"daem0n_tools_search", _GATEWAY_CALL_TOOL})


def _own_annotations(name: str) -> Mapping[str, bool]:
    return {
        "readOnlyHint": name in _READ_ONLY_TOOLS,
        "destructiveHint": name in _DESTRUCTIVE_TOOLS,
        "idempotentHint": name != "sandbox_execute_python",
        "openWorldHint": name in _OPEN_WORLD_TOOLS,
    }


def _annotations(name: str) -> Mapping[str, bool]:
    if name != _GATEWAY_CALL_TOOL:
        return _own_annotations(name)
    # The gateway performs whatever its target performs, so its hints are the
    # least favourable hint of any tool it can dispatch.  A host that decides
    # on these hints whether to ask the user must never be told less.
    targets = [
        _own_annotations(target)
        for target in _TOOL_MODELS
        if target not in _GATEWAY_TOOLS
    ]
    return {
        "readOnlyHint": all(target["readOnlyHint"] for target in targets),
        "destructiveHint": any(target["destructiveHint"] for target in targets),
        "idempotentHint": all(target["idempotentHint"] for target in targets),
        "openWorldHint": any(target["openWorldHint"] for target in targets),
    }


def build_tool_specs(
    handler_map: Mapping[str, Callable[..., object]],
) -> tuple[ToolSpec, ...]:
    """Bind the exact v7 handler set to immutable, fully typed tool specs."""

    handlers = dict(handler_map)
    expected = frozenset(V7_TOOL_LEVELS)
    model_names = frozenset(_TOOL_MODELS)
    category_names = frozenset(_TOOL_CATEGORIES)
    text_names = frozenset(_TOOL_TEXT)
    internal_gaps = {
        "models": (expected - model_names, model_names - expected),
        "categories": (expected - category_names, category_names - expected),
        "text": (expected - text_names, text_names - expected),
        "mapping": (expected - _MAPPED_TOOL_NAMES, _MAPPED_TOOL_NAMES - expected),
        "protected literal": (
            frozenset(PROTECTED_TOOL_NAMES) - frozenset(get_args(ProtectedToolName)),
            frozenset(get_args(ProtectedToolName)) - frozenset(PROTECTED_TOOL_NAMES),
        ),
    }
    drift = [
        f"{kind}: missing={sorted(missing)!r}, unexpected={sorted(unexpected)!r}"
        for kind, (missing, unexpected) in internal_gaps.items()
        if missing or unexpected
    ]
    if drift:
        raise ManifestError("v7 tool contract drift; " + "; ".join(drift))

    supplied = frozenset(handlers)
    missing_handlers = sorted(expected - supplied)
    unexpected_handlers = sorted(supplied - expected)
    if missing_handlers or unexpected_handlers:
        details = []
        if missing_handlers:
            details.append("missing handlers: " + ", ".join(missing_handlers))
        if unexpected_handlers:
            details.append("unexpected handlers: " + ", ".join(unexpected_handlers))
        raise ManifestError("; ".join(details))

    specs = []
    for name in sorted(expected):
        handler = handlers[name]
        if not callable(handler):
            raise ManifestError(f"handler is not callable: {name}")
        input_model, data_model = _TOOL_MODELS[name]
        category = _TOOL_CATEGORIES[name]
        title, description = _TOOL_TEXT[name]
        output_model = ApiResponse[data_model]  # type: ignore[valid-type]
        task_mode: Literal["forbidden", "optional"] = (
            "optional" if name in _OPTIONAL_TOOLS else "forbidden"
        )
        specs.append(
            ToolSpec(
                name=name,
                title=title,
                description=description,
                handler=handler,
                input_model=input_model,
                output_model=output_model,
                category=category,
                tags=_tags(name, category),
                covenant=V7_TOOL_LEVELS[name],
                task_mode=task_mode,
                annotations=_annotations(name),
                pinned=name in PINNED_TOOL_NAMES,
                _meta=(
                    {"ui": {"resourceUri": _DASHBOARD_TOOL_URIS[name]}}
                    if name in _DASHBOARD_TOOL_URIS
                    else {}
                ),
            )
        )
    return tuple(specs)


__all__ = [
    "ActiveContextPage",
    "CodeImpactAnalyzeInput",
    "CodeRef",
    "DecisionDebateInput",
    "DreamingStatus",
    "DreamingStrategyStatus",
    "EntityEvolutionTraceInput",
    "HealthData",
    "InformedByRecordIds",
    "MemoryPreflightInput",
    "MemoryPreflightOutput",
    "MemoryRecallEntityInput",
    "MemoryRecallInput",
    "MemoryRecallOutput",
    "MemoryRecordOutcomeInput",
    "MemoryRecordOutcomeOutput",
    "MemoryStoreData",
    "MemoryStoreInput",
    "MemoryStoreOutput",
    "OutcomeData",
    "OutcomeVerification",
    "PreflightData",
    "PROTECTED_TOOL_NAMES",
    "ProtectedToolName",
    "SessionBriefData",
    "SessionBriefInput",
    "SessionBriefOutput",
    "SystemHealthInput",
    "SystemHealthOutput",
    "ToolCallData",
    "ToolCallInput",
    "ToolDescriptor",
    "ToolsSearchData",
    "ToolsSearchInput",
    "TOOL_DATA_MODELS",
    "TOOL_INPUT_MODELS",
    "V7_NATIVE_TOOL_NAMES",
    "build_argument_normalizer",
    "build_tool_specs",
]
