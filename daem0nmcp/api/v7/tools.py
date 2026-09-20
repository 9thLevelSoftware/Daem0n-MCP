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
    ActiveContextId,
    ApiResponse,
    AwareDateTime,
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
    Preview,
    RecordId,
    RecordSummary,
    RecordType,
    RelationshipId,
    RelativePath,
    RetrievalData,
    RuleId,
    SelectionToken,
    Tag,
    ToolName,
    TriggerId,
    UniqueTags,
    UserJsonObject,
    UserText,
    UtcDateTime,
    VersionId,
    WireModel,
    WorkspaceId,
    contains_absolute_filesystem_path,
)
from .policy import V7_TOOL_LEVELS, V7ArgumentNormalizer
from .registry import PINNED_TOOL_NAMES, ManifestError, ToolSpec
from .resources import ActiveContextItem, RuleView

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
UserMediumText = Annotated[
    UserText,
    StringConstraints(strict=True, min_length=1, max_length=2000),
]
UserLongText = Annotated[
    UserText,
    StringConstraints(strict=True, min_length=1, max_length=100_000),
]
OptionalUserMediumText = UserMediumText | None
RecordVisibility = Literal["workspace", "private", "shared"]


def _record_visibility(context: dict[str, object]) -> dict[str, object]:
    if context.get("visibility", "workspace") not in get_args(RecordVisibility):
        raise ValueError("context.visibility must be workspace, private or shared")
    return context


RecordContext = Annotated[ContextJsonObject, AfterValidator(_record_visibility)]
IdempotencyKey = Annotated[
    str,
    StringConstraints(
        strict=True,
        min_length=8,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._~-]*$",
    ),
]
PreflightToken = Annotated[
    str,
    StringConstraints(
        strict=True,
        min_length=16,
        max_length=8192,
        pattern=r"^[A-Za-z0-9._~-]+$",
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


class PreflightGuidance(WireModel):
    records: list[RecordSummary] = Field(default_factory=list, max_length=20)
    rules: list[RuleView] = Field(default_factory=list, max_length=20)
    must_do: list[UserMediumText] = Field(default_factory=list, max_length=50)
    must_not: list[UserMediumText] = Field(default_factory=list, max_length=50)
    ask_first: list[UserMediumText] = Field(default_factory=list, max_length=50)
    warnings: list[UserMediumText] = Field(default_factory=list, max_length=50)


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

    @model_validator(mode="after")
    def validate_candidate_limit(self) -> MemoryRecallInput:
        if self.candidate_limit < self.limit:
            raise ValueError("candidate_limit cannot be smaller than limit")
        return self


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
    idempotency_key: IdempotencyKey
    preflight_token: PreflightToken

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
    preflight_token: PreflightToken


class MemoryRecordOutcomeInput(WireModel):
    workspace_id: WorkspaceId
    record_id: RecordId
    outcome_text: Annotated[
        UserText,
        StringConstraints(strict=True, min_length=1, max_length=50_000),
    ]
    worked: bool
    happened_at: AwareDateTime | None = None
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
    preflight_token: PreflightToken


class MemoryLinkInput(WireModel):
    workspace_id: WorkspaceId
    source_record_id: RecordId
    target_record_id: RecordId
    relationship_type: RelationshipType
    description: OptionalUserMediumText = None
    confidence: Annotated[float, Field(ge=0, le=1, allow_inf_nan=False)] = 1.0
    idempotency_key: IdempotencyKey
    preflight_token: PreflightToken

    @model_validator(mode="after")
    def validate_endpoints(self) -> MemoryLinkInput:
        if self.source_record_id == self.target_record_id:
            raise ValueError("relationship endpoints must be distinct")
        return self


class MemoryUnlinkInput(WireModel):
    workspace_id: WorkspaceId
    relationship_id: RelationshipId
    preflight_token: PreflightToken


class MemoryPinSetInput(WireModel):
    workspace_id: WorkspaceId
    record_id: RecordId
    pinned: bool
    preflight_token: PreflightToken


class ActiveContextAddInput(WireModel):
    workspace_id: WorkspaceId
    record_id: RecordId
    reason: OptionalUserMediumText = None
    priority: Annotated[int, Field(ge=-100, le=100)] = 0
    expires_at: AwareDateTime | None = None
    preflight_token: PreflightToken


class ActiveContextRemoveInput(WireModel):
    workspace_id: WorkspaceId
    active_context_id: ActiveContextId
    preflight_token: PreflightToken


class ActiveContextClearInput(WireModel):
    workspace_id: WorkspaceId
    selection_token: SelectionToken
    preflight_token: PreflightToken


class DocumentIngestUrlInput(WireModel):
    workspace_id: WorkspaceId
    url: HttpsUrl
    topic: UserMediumText
    chunk_size: Annotated[int, Field(ge=256, le=16_000)] = 2000
    idempotency_key: IdempotencyKey
    preflight_token: PreflightToken


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
    preflight_token: PreflightToken


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
    preflight_token: PreflightToken


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
    preflight_token: PreflightToken


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
    preflight_token: PreflightToken


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
    preflight_token: PreflightToken


class ContextTriggerListInput(WireModel):
    workspace_id: WorkspaceId
    active_only: bool = True
    cursor: Cursor | None = None
    limit: Annotated[int, Field(ge=1, le=100)] = 50


class ContextTriggerDeleteInput(WireModel):
    workspace_id: WorkspaceId
    trigger_id: TriggerId
    preflight_token: PreflightToken


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
    preflight_token: PreflightToken


class EntityListInput(WireModel):
    workspace_id: WorkspaceId
    entity_type: EntityType | None = None
    cursor: Cursor | None = None
    limit: Annotated[int, Field(ge=1, le=100)] = 20


class EntityBackfillInput(WireModel):
    workspace_id: WorkspaceId
    force: bool = False
    idempotency_key: IdempotencyKey
    preflight_token: PreflightToken


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
    preflight_token: PreflightToken


class MemoryArchiveSetInput(WireModel):
    workspace_id: WorkspaceId
    record_id: RecordId
    archived: bool
    preflight_token: PreflightToken


class MemoryDuplicatesPreviewInput(WireModel):
    workspace_id: WorkspaceId
    merge_duplicates: bool = True


class MemoryDuplicatesCleanupInput(MemoryDuplicatesPreviewInput):
    selection_token: SelectionToken
    preflight_token: PreflightToken


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
    preflight_token: PreflightToken


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
    preflight_token: PreflightToken

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
    preflight_token: PreflightToken

    @model_validator(mode="after")
    def validate_link(self) -> WorkspaceLinkInput:
        if self.linked_workspace_id == self.workspace_id:
            raise ValueError("a workspace cannot link to itself")
        return self


class WorkspaceUnlinkInput(WireModel):
    workspace_id: WorkspaceId
    linked_workspace_id: WorkspaceId
    preflight_token: PreflightToken


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
    preflight_token: PreflightToken


class WorkspaceConsolidateAndArchiveSourcesInput(WorkspaceConsolidateInput):
    pass


class DreamDuplicatesPreviewInput(WireModel):
    workspace_id: WorkspaceId


class DreamDuplicatesPurgeInput(WireModel):
    workspace_id: WorkspaceId
    selection_token: SelectionToken
    preflight_token: PreflightToken


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
    preflight_token: PreflightToken


SessionBriefOutput = ApiResponse[SessionBriefData]
MemoryPreflightOutput = ApiResponse[PreflightData]
MemoryRecallOutput = ApiResponse[RetrievalData]
MemoryStoreOutput = ApiResponse[MemoryStoreData]
MemoryRecordOutcomeOutput = ApiResponse[OutcomeData]
SystemHealthOutput = ApiResponse[HealthData]


_TOOL_MODELS: Mapping[str, tuple[type[WireModel], type[WireModel]]] = MappingProxyType(
    {
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
        "system": frozenset({"system_health"}),
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


def _expanded_description(name: str, category: str) -> str:
    phrase = name.replace("_", " ")
    return f"{phrase.capitalize()} for the authorized workspace ({category})."


def _tags(name: str, category: str) -> tuple[str, ...]:
    return tuple(dict.fromkeys((category, *name.split("_"))))


def _annotations(name: str) -> Mapping[str, bool]:
    return {
        "readOnlyHint": name in _READ_ONLY_TOOLS,
        "destructiveHint": name in _DESTRUCTIVE_TOOLS,
        "idempotentHint": name != "sandbox_execute_python",
        "openWorldHint": name in _OPEN_WORLD_TOOLS,
    }


def build_tool_specs(
    handler_map: Mapping[str, Callable[..., object]],
) -> tuple[ToolSpec, ...]:
    """Bind the exact v7 handler set to immutable, fully typed tool specs."""

    handlers = dict(handler_map)
    expected = frozenset(V7_TOOL_LEVELS)
    model_names = frozenset(_TOOL_MODELS)
    category_names = frozenset(_TOOL_CATEGORIES)
    internal_gaps = {
        "models": (expected - model_names, model_names - expected),
        "categories": (expected - category_names, category_names - expected),
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
        output_model = ApiResponse[data_model]  # type: ignore[valid-type]
        task_mode: Literal["forbidden", "optional"] = (
            "optional" if name in _OPTIONAL_TOOLS else "forbidden"
        )
        specs.append(
            ToolSpec(
                name=name,
                description=_expanded_description(name, category),
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
    "DecisionDebateInput",
    "DreamingStatus",
    "DreamingStrategyStatus",
    "EntityEvolutionTraceInput",
    "HealthData",
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
    "PreflightData",
    "PROTECTED_TOOL_NAMES",
    "ProtectedToolName",
    "SessionBriefData",
    "SessionBriefInput",
    "SessionBriefOutput",
    "SystemHealthInput",
    "SystemHealthOutput",
    "TOOL_DATA_MODELS",
    "TOOL_INPUT_MODELS",
    "V7_NATIVE_TOOL_NAMES",
    "build_argument_normalizer",
    "build_tool_specs",
]
