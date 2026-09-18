"""
Daem0nMCP Models - Schema for AI memory, decision trees, and knowledge graphs.

Tables:
- memories: Stores decisions, patterns, warnings, learnings
- rules: Decision tree nodes / enforcement rules
- memory_relationships: Graph edges between memories for causal reasoning
"""

from datetime import datetime, timezone

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    Column,
    DateTime,
    Float,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    LargeBinary,
    PrimaryKeyConstraint,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase
from sqlalchemy.orm import relationship as orm_relationship


class Base(DeclarativeBase):
    pass


class Memory(Base):
    """
    A memory is any piece of information the AI should remember.

    Categories:
    - decision: An architectural or design choice (episodic - decays)
    - pattern: A recurring approach that should be followed (semantic - permanent)
    - warning: Something that went wrong / should be avoided (semantic - permanent)
    - learning: A lesson learned from experience (episodic - decays)

    Semantic memories (patterns, warnings) don't decay - they're project facts.
    Episodic memories (decisions, learnings) decay over time.
    """

    __tablename__ = "memories"

    id = Column(Integer, primary_key=True, index=True)

    # What type of memory
    category = Column(
        String, nullable=False, index=True
    )  # decision, pattern, warning, learning

    # The actual content
    content = Column(Text, nullable=False)

    # Why this decision was made / context
    rationale = Column(Text, nullable=True)

    # Structured context (files involved, alternatives, etc.)
    context = Column(JSON, default=dict)

    # Tags for retrieval
    tags = Column(JSON, default=list)

    # File path association - link memory to specific files
    file_path = Column(String, nullable=True, index=True)

    # Relative file path (for portability across machines)
    file_path_relative = Column(String, nullable=True, index=True)

    # Extracted keywords for semantic-ish search (computed from content + tags)
    keywords = Column(Text, nullable=True, index=True)

    # Permanent flag - semantic memories (patterns, warnings) don't decay
    # Auto-set based on category, but can be overridden
    is_permanent = Column(Boolean, default=False)

    # Vector embedding for semantic search (optional - requires sentence-transformers)
    # Stored as packed floats (bytes)
    vector_embedding = Column(LargeBinary, nullable=True)

    # Outcome tracking
    outcome = Column(Text, nullable=True)  # What actually happened
    worked = Column(Boolean, nullable=True)  # Did it work out?

    # Pinned memories are never pruned and have boosted relevance
    pinned = Column(Boolean, default=False)

    # Archived memories are hidden from normal recall but kept for history
    archived = Column(Boolean, default=False)

    # Recall count - tracks how often this memory is accessed (for saliency-based pruning)
    recall_count = Column(Integer, default=0)

    # Surprise score - measures information novelty (0.0-1.0)
    # High surprise = novel information, low = routine/expected
    surprise_score = Column(Float, nullable=True)

    # Importance score - EWC-inspired protection weight (0.0-1.0)
    # High importance = protected from decay/pruning
    # Based on: recall frequency, positive outcomes, user interactions
    importance_score = Column(Float, nullable=True)

    # Provenance tracking (Phase 22: LLM Compatibility)
    source_client = Column(String, nullable=True)  # e.g., "opencode", "claude-code"
    source_model = Column(
        String, nullable=True
    )  # e.g., "anthropic/claude-sonnet-4", "openai/gpt-5"

    # Timestamps
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = Column(
        DateTime,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )


class Fact(Base):
    """
    A verified, static fact (Engram-inspired).

    Facts are immutable knowledge that has been verified through:
    - Multiple successful outcomes
    - Explicit user verification
    - Promotion from stable patterns

    Uses content hash for O(1) lookup instead of semantic search.
    """

    __tablename__ = "facts"

    id = Column(Integer, primary_key=True, index=True)

    # Content hash for O(1) lookup (SHA256 of normalized content)
    content_hash = Column(String(64), unique=True, nullable=False, index=True)

    # The actual fact content
    content = Column(Text, nullable=False)

    # Category for organization (e.g., "language", "api", "convention")
    category = Column(String, nullable=True, index=True)

    # Original memory this fact was derived from
    source_memory_id = Column(
        Integer, ForeignKey("memories.id", ondelete="SET NULL"), nullable=True
    )

    # How many times this fact has been verified/confirmed
    verification_count = Column(Integer, default=0)

    # Whether this fact is fully verified
    is_verified = Column(Boolean, default=False)

    # Optional tags for filtering
    tags = Column(JSON, default=list)

    # Timestamps
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    verified_at = Column(DateTime, nullable=True)

    # ORM relationship
    source_memory = orm_relationship("Memory", backref="derived_facts")


class Rule(Base):
    """
    A rule is a decision tree node - when a trigger condition is met,
    it provides guidance on what to do.

    Example:
        trigger: "adding new API endpoint"
        must_do: ["Add rate limiting", "Add to OpenAPI spec"]
        must_not: ["Use synchronous database calls"]
        ask_first: ["Is this a breaking change?"]
    """

    __tablename__ = "rules"

    id = Column(Integer, primary_key=True, index=True)

    # What activates this rule (human-readable description)
    trigger = Column(Text, nullable=False)

    # Extracted keywords for matching
    trigger_keywords = Column(Text, nullable=True, index=True)

    # Things that MUST be done when this rule applies
    must_do = Column(JSON, default=list)

    # Things that MUST NOT be done
    must_not = Column(JSON, default=list)

    # Questions to ask/consider before proceeding
    ask_first = Column(JSON, default=list)

    # Warnings to display (from past experience)
    warnings = Column(JSON, default=list)

    # Higher priority rules are shown first
    priority = Column(Integer, default=0)

    # Can disable rules without deleting
    enabled = Column(Boolean, default=True)

    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))


# Note: ProjectState table was removed in v2.1 as it was unused.
# The briefing now computes statistics dynamically which is more accurate.


class MemoryVersion(Base):
    """
    Tracks historical versions of memories for temporal queries.

    Bi-Temporal Model:
    - transaction_time: When we learned/recorded this version (tracked via changed_at)
    - valid_time: When the fact was actually true in reality (tracked via valid_from/valid_to)

    This dual-time tracking enables:
    - Point-in-time queries: "What did we know at time T?"
    - As-of queries: "What was true at time T?"
    - Bitemporal queries: "What did we know at T1 about what was true at T2?"

    Captures snapshots when:
    - Memory content changes
    - Memory relationships change
    - Memory outcome is recorded

    Enables queries like:
    - "What did we believe about auth at time T?"
    - "How has this decision evolved?"
    - "When did this relationship change?"
    - "What facts were invalidated by new information?"
    """

    __tablename__ = "memory_versions"

    id = Column(Integer, primary_key=True, index=True)

    # Reference to the memory being versioned
    memory_id = Column(
        Integer,
        ForeignKey("memories.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    # Version sequence number (1, 2, 3...)
    version_number = Column(Integer, nullable=False)

    # Snapshot of memory state at this version
    content = Column(Text, nullable=False)
    rationale = Column(Text, nullable=True)
    context = Column(JSON, default=dict)
    tags = Column(JSON, default=list)

    # Outcome state at this version
    outcome = Column(Text, nullable=True)
    worked = Column(Boolean, nullable=True)

    # What triggered this version
    change_type = Column(
        String, nullable=False
    )  # created, content_updated, outcome_recorded, relationship_changed
    change_description = Column(Text, nullable=True)

    # Transaction time: when this version was recorded in the system
    changed_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))

    # Valid time: when this version's content became true in reality
    # NULL means "same as changed_at" (backwards compatible default)
    valid_from = Column(DateTime, nullable=True)

    # Valid time: when this version's content was superseded (NULL = still valid)
    valid_to = Column(DateTime, nullable=True)

    # Reference to the version that invalidated this one (for contradiction tracking)
    # Enables tracking causal chains of fact updates
    invalidated_by_version_id = Column(
        Integer, ForeignKey("memory_versions.id"), nullable=True
    )

    # Composite index for efficient version lookups
    __table_args__ = (
        Index("ix_memory_versions_memory_version", "memory_id", "version_number"),
    )

    # ORM relationship
    memory = orm_relationship("Memory", backref="versions")


class MemoryRelationship(Base):
    """
    Explicit relationship edges between memories for graph traversal.

    Enables causal chain reasoning that similarity search alone cannot provide:
    - "What decisions led to this pattern?"
    - "What does this library choice depend on?"
    - "What approaches have been superseded?"

    Relationship types:
    - led_to: A caused or resulted in B (e.g., "database choice led to caching pattern")
    - supersedes: A replaces B (B is now outdated)
    - depends_on: A requires B to be valid
    - conflicts_with: A contradicts B
    - related_to: General association (weaker than above)

    Usage pattern (Vector-First, Graph-Second):
    1. Use semantic search to find candidate memories
    2. Expand via graph edges to get connected context
    3. Assembly full context for LLM including structural relationships
    """

    __tablename__ = "memory_relationships"

    id = Column(Integer, primary_key=True, index=True)

    # Source memory (the "from" node)
    source_id = Column(
        Integer,
        ForeignKey("memories.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    # Target memory (the "to" node)
    target_id = Column(
        Integer,
        ForeignKey("memories.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    # Relationship type
    relationship = Column(String, nullable=False, index=True)

    # Optional description/context for this edge
    description = Column(Text, nullable=True)

    # Confidence/strength (1.0 = certain, can decay over time)
    confidence = Column(Float, default=1.0)

    # Timestamps
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))

    # ORM relationships for easy navigation
    source = orm_relationship(
        "Memory", foreign_keys=[source_id], backref="outgoing_relationships"
    )
    target = orm_relationship(
        "Memory", foreign_keys=[target_id], backref="incoming_relationships"
    )


class SessionState(Base):
    """
    Tracks session state for enforcement.

    Sessions are identified by project + time bucket.
    Tracks what context checks were made and what decisions are pending outcomes.
    """

    __tablename__ = "session_state"

    id = Column(Integer, primary_key=True, index=True)
    session_id = Column(String, nullable=False, unique=True, index=True)
    project_path = Column(String, nullable=False)
    briefed = Column(Boolean, default=False)
    context_checks = Column(JSON, default=list)  # List of files/topics checked
    pending_decisions = Column(JSON, default=list)  # List of memory IDs
    last_activity = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))


class EnforcementBypassLog(Base):
    """
    Audit log for when enforcement is bypassed via --no-verify.

    Provides accountability even when developers skip enforcement.
    """

    __tablename__ = "enforcement_bypass_log"

    id = Column(Integer, primary_key=True, index=True)
    timestamp = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    pending_decisions = Column(JSON, default=list)  # List of skipped decision IDs
    staged_files_with_warnings = Column(JSON, default=list)  # List of risky files
    reason = Column(Text, nullable=True)  # Optional user-provided reason


class CodeEntity(Base):
    """
    A code element from an indexed project.

    Types: file, class, function, method, variable, import, module

    Used by Phase 2: Code Understanding layer to enable:
    - "What depends on X?"
    - Impact analysis for changes
    - Semantic code search
    """

    __tablename__ = "code_entities"

    id = Column(String, primary_key=True)  # hash of project+path+name+type
    project_path = Column(String, nullable=False, index=True)

    entity_type = Column(String, nullable=False)  # file, class, function, method
    name = Column(String, nullable=False)
    qualified_name = Column(String, nullable=True)  # e.g., "myapp.models.User.save"
    file_path = Column(String, nullable=False, index=True)
    line_start = Column(Integer, nullable=True)
    line_end = Column(Integer, nullable=True)

    signature = Column(Text, nullable=True)  # First line of definition
    docstring = Column(Text, nullable=True)

    # Structural relationships (for dependency tracking)
    calls = Column(JSON, default=list)  # Functions/methods this entity calls
    called_by = Column(JSON, default=list)  # Functions/methods that call this
    imports = Column(JSON, default=list)  # What this entity imports
    inherits = Column(JSON, default=list)  # Parent classes for class entities

    indexed_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))


class MemoryCodeRef(Base):
    """
    Links memories to code entities.

    Enables automatic symbol resolution:
    - When a memory mentions `UserService.authenticate`, link to that entity
    - When code changes, surface relevant memories

    Relationship types:
    - about: Memory discusses this entity
    - modifies: Memory describes changes to this entity
    - introduces: Memory introduces this entity
    - deprecates: Memory marks this entity as deprecated
    """

    __tablename__ = "memory_code_refs"

    id = Column(Integer, primary_key=True)
    memory_id = Column(
        Integer, ForeignKey("memories.id", ondelete="CASCADE"), index=True
    )
    code_entity_id = Column(String, index=True)

    # Snapshot (survives reindex - entity might be renamed/moved)
    entity_type = Column(String, nullable=True)
    entity_name = Column(String, nullable=True)
    file_path = Column(String, nullable=True)
    line_number = Column(Integer, nullable=True)

    relationship = Column(
        String, nullable=True
    )  # "about", "modifies", "introduces", "deprecates"
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))

    # ORM relationship
    memory = orm_relationship("Memory", backref="code_refs")


class ProjectLink(Base):
    """
    Links between related projects for cross-repo awareness.

    Enables reading memories from linked projects while maintaining
    strict write isolation (each project writes only to its own DB).

    Relationship types:
    - same-project: Full sharing (e.g., client/server monorepo split)
    - upstream: Dependency (your app depends on this library)
    - downstream: Dependent (this app depends on your library)
    - related: Loose association
    """

    __tablename__ = "project_links"

    id = Column(Integer, primary_key=True, index=True)

    # This project's path (where this link record is stored)
    source_path = Column(String, nullable=False, index=True)

    # The linked project's path
    linked_path = Column(String, nullable=False)

    # Type of relationship
    relationship = Column(
        String, default="related"
    )  # same-project, upstream, downstream, related

    # Optional label/description
    label = Column(String, nullable=True)

    # Timestamps
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))


class ActiveContextItem(Base):
    """
    Items in the active working context (always-hot memories).

    These memories are auto-injected into tool responses and briefings.
    Inspired by MemGPT's core memory concept.

    Use cases:
    - Critical decisions that must inform all work
    - Active warnings that should never be forgotten
    - Current focus areas

    Max items per project: 10 (prevents context bloat)
    """

    __tablename__ = "active_context"

    id = Column(Integer, primary_key=True, index=True)

    # Which project this belongs to
    project_path = Column(String, nullable=False, index=True)

    # The memory to keep in active context
    memory_id = Column(
        Integer, ForeignKey("memories.id", ondelete="CASCADE"), nullable=False
    )

    # Priority for ordering (higher = more important, shown first)
    priority = Column(Integer, default=0)

    # Why this was added to active context
    reason = Column(Text, nullable=True)

    # Timestamps
    added_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    expires_at = Column(DateTime, nullable=True)  # Optional auto-expiry

    # ORM relationship
    memory = orm_relationship("Memory")


class MemoryCommunity(Base):
    """
    A cluster of related memories with a generated summary.

    Inspired by GraphRAG's hierarchical community detection.

    Communities are auto-generated based on:
    - Tag co-occurrence (memories sharing tags cluster together)
    - Semantic similarity (similar content clusters together)

    Levels:
    - 0: Leaf communities (most specific)
    - 1+: Parent communities (aggregations)

    Use cases:
    - "Give me an overview of auth decisions" -> community summary
    - "Drill into JWT specifics" -> community members
    """

    __tablename__ = "memory_communities"

    id = Column(Integer, primary_key=True, index=True)

    # Which project this belongs to
    project_path = Column(String, nullable=False, index=True)

    # Human-readable name (auto-generated from dominant tags)
    name = Column(String, nullable=False)

    # AI-generated summary of community members
    summary = Column(Text, nullable=False)

    # Tags that define this community (union of member tags)
    tags = Column(JSON, default=list)

    # Member statistics
    member_count = Column(Integer, default=0)
    member_ids = Column(JSON, default=list)  # List of memory IDs

    # Hierarchy level (0 = leaf, higher = more abstract)
    level = Column(Integer, default=0)

    # Parent community (for hierarchy)
    parent_id = Column(
        Integer, ForeignKey("memory_communities.id", ondelete="SET NULL"), nullable=True
    )

    # Vector embedding for community summary (for semantic search)
    vector_embedding = Column(LargeBinary, nullable=True)

    # Timestamps
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = Column(
        DateTime,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    # ORM relationship for hierarchy
    parent = orm_relationship("MemoryCommunity", remote_side=[id], backref="children")


class ExtractedEntity(Base):
    """
    An entity extracted from memory content.

    Entity types:
    - function: Function or method names (e.g., authenticate_user)
    - class: Class names (e.g., UserService)
    - file: File paths (e.g., auth/service.py)
    - concept: Domain concepts (e.g., authentication, caching)
    - variable: Variable names mentioned
    - module: Module/package names

    Auto-extracted from memory content using pattern matching.
    Links to code_entities table when possible for richer context.
    """

    __tablename__ = "extracted_entities"

    id = Column(Integer, primary_key=True, index=True)
    project_path = Column(String, nullable=False, index=True)
    entity_type = Column(String, nullable=False, index=True)
    name = Column(String, nullable=False, index=True)
    qualified_name = Column(String, nullable=True, index=True)
    mention_count = Column(Integer, default=1)
    code_entity_id = Column(String, nullable=True, index=True)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = Column(
        DateTime,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )


class MemoryEntityRef(Base):
    """
    Links a memory to an extracted entity.

    Relationship types:
    - mentions: Memory mentions this entity
    - about: Memory is primarily about this entity
    - modifies: Memory describes changes to this entity
    - introduces: Memory introduces this entity
    - deprecates: Memory deprecates this entity
    """

    __tablename__ = "memory_entity_refs"

    id = Column(Integer, primary_key=True, index=True)
    memory_id = Column(
        Integer,
        ForeignKey("memories.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    entity_id = Column(
        Integer,
        ForeignKey("extracted_entities.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    relationship = Column(String, default="mentions")
    context_snippet = Column(Text, nullable=True)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))

    # ORM relationships
    memory = orm_relationship("Memory", backref="entity_refs")
    entity = orm_relationship("ExtractedEntity", backref="memory_refs")


class ContextTrigger(Base):
    """
    A trigger that auto-recalls memories when patterns match.

    Trigger types:
    - file_pattern: Glob pattern for file paths (e.g., "src/auth/**/*.py")
    - tag_match: Regex pattern for memory tags (e.g., "auth|security")
    - entity_match: Regex pattern for entity names (e.g., ".*Service$")

    When a trigger matches:
    1. Auto-recall memories for the specified topic
    2. Filter by recall_categories if specified
    3. Inject into tool response context

    Use cases:
    - Auto-surface auth decisions when editing auth files
    - Show database warnings when touching migration files
    - Recall API patterns when adding new endpoints
    """

    __tablename__ = "context_triggers"

    id = Column(Integer, primary_key=True, index=True)

    # Which project this trigger belongs to
    project_path = Column(String, nullable=False, index=True)

    # Type of trigger: file_pattern, tag_match, entity_match
    trigger_type = Column(String, nullable=False)

    # The pattern to match (glob for files, regex for tags/entities)
    pattern = Column(String, nullable=False)

    # Topic to recall when triggered
    recall_topic = Column(String, nullable=False)

    # Optional: limit to specific categories
    recall_categories = Column(JSON, default=list)

    # Enable/disable without deleting
    is_active = Column(Boolean, default=True)

    # Higher priority triggers are evaluated first
    priority = Column(Integer, default=0)

    # Timestamps
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))

    # Usage tracking
    trigger_count = Column(Integer, default=0)
    last_triggered = Column(DateTime, nullable=True)


class FileHash(Base):
    """Tracks content hashes for indexed files."""

    __tablename__ = "file_hashes"

    id = Column(Integer, primary_key=True, index=True)
    project_path = Column(String, nullable=False, index=True)
    file_path = Column(String, nullable=False)  # Relative to project
    content_hash = Column(String(64), nullable=False)  # SHA256
    indexed_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))

    __table_args__ = (
        UniqueConstraint("project_path", "file_path", name="uix_file_project"),
    )


# Architecture-format 7 canonical log and rebuildable typed projections.  JSON is
# intentionally TEXT here: event_store owns canonical serialization, independent
# of SQLAlchemy dialect serializer behavior.


class MemoryEvent(Base):
    __tablename__ = "memory_events"

    event_id = Column(String(68), primary_key=True)
    workspace_id = Column(String, nullable=False)
    stream_id = Column(String, nullable=False)
    stream_kind = Column(String, nullable=False)
    stream_version = Column(Integer, nullable=False)
    event_type = Column(String(80), nullable=False)
    event_schema_version = Column(Integer, nullable=False, default=1)
    occurred_at_us = Column(Integer, nullable=False)
    recorded_at_us = Column(Integer, nullable=False)
    actor_type = Column(String, nullable=False)
    actor_id = Column(String, nullable=True)
    causation_event_id = Column(
        String(68), ForeignKey("memory_events.event_id", ondelete="RESTRICT")
    )
    correlation_id = Column(String, nullable=True)
    payload_json = Column(Text, nullable=False)
    payload_hash = Column(String(64), nullable=False)
    previous_event_hash = Column(String(64), nullable=True)
    event_hash = Column(String(64), nullable=False, unique=True)

    __table_args__ = (
        CheckConstraint(
            "length(event_id)=68 AND substr(event_id,1,4)='evt_' "
            "AND substr(event_id,5) NOT GLOB '*[^0-9a-f]*'",
            name="ck_memory_events_event_id",
        ),
        CheckConstraint(
            "substr(workspace_id,1,3)='ws_'", name="ck_memory_events_workspace"
        ),
        CheckConstraint(
            "stream_kind IN ('memory','fact','relationship')",
            name="ck_memory_events_stream_kind",
        ),
        CheckConstraint("stream_version >= 1", name="ck_memory_events_stream_version"),
        CheckConstraint(
            "length(event_type) BETWEEN 3 AND 80", name="ck_memory_events_event_type"
        ),
        CheckConstraint(
            "event_schema_version >= 1", name="ck_memory_events_schema_version"
        ),
        CheckConstraint(
            "actor_type IN ('user','client','system','migration','import')",
            name="ck_memory_events_actor_type",
        ),
        CheckConstraint(
            "json_valid(payload_json) AND json_type(payload_json)='object'",
            name="ck_memory_events_payload_json",
        ),
        CheckConstraint(
            "length(payload_hash)=64 AND payload_hash NOT GLOB '*[^0-9a-f]*'",
            name="ck_memory_events_payload_hash",
        ),
        CheckConstraint(
            "previous_event_hash IS NULL OR (length(previous_event_hash)=64 "
            "AND previous_event_hash NOT GLOB '*[^0-9a-f]*')",
            name="ck_memory_events_previous_hash",
        ),
        CheckConstraint(
            "length(event_hash)=64 AND event_hash NOT GLOB '*[^0-9a-f]*'",
            name="ck_memory_events_event_hash",
        ),
        UniqueConstraint(
            "workspace_id",
            "stream_id",
            "stream_version",
            name="uq_memory_events_stream_version",
        ),
        Index(
            "idx_memory_events_stream", "workspace_id", "stream_id", "stream_version"
        ),
        Index(
            "idx_memory_events_recorded", "workspace_id", "recorded_at_us", "event_id"
        ),
        Index("idx_memory_events_type", "workspace_id", "event_type", "recorded_at_us"),
    )


class MemoryRecord(Base):
    __tablename__ = "memory_records"

    record_id = Column(String(68), primary_key=True)
    workspace_id = Column(String, nullable=False)
    record_type = Column(String, nullable=False)
    legacy_type = Column(String, nullable=True)
    content = Column(Text, nullable=False)
    content_hash = Column(String(64), nullable=False)
    rationale = Column(Text, nullable=True)
    context_json = Column(Text, nullable=False, default="{}")
    tags_json = Column(Text, nullable=False, default="[]")
    file_path = Column(Text, nullable=True)
    file_path_relative = Column(Text, nullable=True)
    keywords = Column(Text, nullable=True)
    is_permanent = Column(Integer, nullable=False, default=0)
    pinned = Column(Integer, nullable=False, default=0)
    archived = Column(Integer, nullable=False, default=0)
    outcome = Column(Text, nullable=True)
    worked = Column(Integer, nullable=True)
    recall_count = Column(Integer, nullable=False, default=0)
    surprise_score = Column(Float, nullable=True)
    importance_score = Column(Float, nullable=True)
    source_client = Column(Text, nullable=True)
    source_model = Column(Text, nullable=True)
    stream_version = Column(Integer, nullable=False)
    source_event_id = Column(
        String(68),
        ForeignKey("memory_events.event_id", ondelete="RESTRICT"),
        nullable=False,
    )
    created_at_us = Column(Integer, nullable=False)
    updated_at_us = Column(Integer, nullable=False)
    deleted_at_us = Column(Integer, nullable=True)
    state_hash = Column(String(64), nullable=False)

    __table_args__ = (
        CheckConstraint(
            "length(record_id)=68 AND substr(record_id,1,4)='mem_' "
            "AND substr(record_id,5) NOT GLOB '*[^0-9a-f]*'",
            name="ck_memory_records_id",
        ),
        CheckConstraint(
            "substr(workspace_id,1,3)='ws_'", name="ck_memory_records_workspace"
        ),
        CheckConstraint(
            "record_type IN ('decision','pattern','warning','learning','procedure','observation','legacy')",
            name="ck_memory_records_type",
        ),
        CheckConstraint(
            "(record_type='legacy' AND legacy_type IS NOT NULL) OR "
            "(record_type<>'legacy' AND legacy_type IS NULL)",
            name="ck_memory_records_legacy_type",
        ),
        CheckConstraint(
            "length(content_hash)=64 AND content_hash NOT GLOB '*[^0-9a-f]*'",
            name="ck_memory_records_content_hash",
        ),
        CheckConstraint(
            "json_valid(context_json) AND json_type(context_json)='object'",
            name="ck_memory_records_context_json",
        ),
        CheckConstraint(
            "json_valid(tags_json) AND json_type(tags_json)='array'",
            name="ck_memory_records_tags_json",
        ),
        CheckConstraint("is_permanent IN (0,1)", name="ck_memory_records_permanent"),
        CheckConstraint("pinned IN (0,1)", name="ck_memory_records_pinned"),
        CheckConstraint("archived IN (0,1)", name="ck_memory_records_archived"),
        CheckConstraint(
            "worked IS NULL OR worked IN (0,1)", name="ck_memory_records_worked"
        ),
        CheckConstraint("recall_count >= 0", name="ck_memory_records_recall_count"),
        CheckConstraint(
            "surprise_score IS NULL OR surprise_score BETWEEN 0.0 AND 1.0",
            name="ck_memory_records_surprise",
        ),
        CheckConstraint(
            "importance_score IS NULL OR importance_score BETWEEN 0.0 AND 1.0",
            name="ck_memory_records_importance",
        ),
        CheckConstraint("stream_version >= 1", name="ck_memory_records_stream_version"),
        CheckConstraint(
            "length(state_hash)=64 AND state_hash NOT GLOB '*[^0-9a-f]*'",
            name="ck_memory_records_state_hash",
        ),
        UniqueConstraint(
            "workspace_id", "record_id", name="uq_memory_records_workspace_id"
        ),
        Index(
            "idx_memory_records_type",
            "workspace_id",
            "record_type",
            "archived",
            "deleted_at_us",
        ),
        Index("idx_memory_records_content_hash", "workspace_id", "content_hash"),
        Index("idx_memory_records_source_event", "source_event_id"),
    )


class MemoryFactVersion(Base):
    __tablename__ = "memory_fact_versions"

    fact_version_id = Column(String(69), primary_key=True)
    fact_id = Column(String(69), nullable=False)
    workspace_id = Column(String, nullable=False)
    version = Column(Integer, nullable=False)
    subject_record_id = Column(
        String(68), ForeignKey("memory_records.record_id", ondelete="RESTRICT")
    )
    predicate = Column(String(120), nullable=False)
    object_kind = Column(String, nullable=False)
    object_json = Column(Text, nullable=False)
    legacy_type = Column(String, nullable=True)
    content_hash = Column(String(64), nullable=False)
    confidence = Column(Float, nullable=False, default=1.0)
    verification_count = Column(Integer, nullable=False, default=0)
    is_verified = Column(Integer, nullable=False, default=0)
    evidence_json = Column(Text, nullable=False, default="[]")
    metadata_json = Column(Text, nullable=False, default="{}")
    valid_from_us = Column(Integer, nullable=False)
    valid_to_us = Column(Integer, nullable=True)
    transaction_from_us = Column(Integer, nullable=False)
    transaction_to_us = Column(Integer, nullable=True)
    asserted_by_event_id = Column(
        String(68),
        ForeignKey("memory_events.event_id", ondelete="RESTRICT"),
        nullable=False,
    )
    retracted_by_event_id = Column(
        String(68), ForeignKey("memory_events.event_id", ondelete="RESTRICT")
    )

    __table_args__ = (
        CheckConstraint(
            "length(fact_version_id)=69 AND substr(fact_version_id,1,5)='fact_' "
            "AND substr(fact_version_id,6) NOT GLOB '*[^0-9a-f]*'",
            name="ck_fact_versions_id",
        ),
        CheckConstraint(
            "length(fact_id)=69 AND substr(fact_id,1,5)='fact_' "
            "AND substr(fact_id,6) NOT GLOB '*[^0-9a-f]*'",
            name="ck_fact_versions_fact_id",
        ),
        CheckConstraint(
            "substr(workspace_id,1,3)='ws_'", name="ck_fact_versions_workspace"
        ),
        CheckConstraint("version >= 1", name="ck_fact_versions_version"),
        CheckConstraint(
            "length(predicate) BETWEEN 1 AND 120", name="ck_fact_versions_predicate"
        ),
        CheckConstraint(
            "object_kind IN ('text','number','boolean','json','record_ref','legacy')",
            name="ck_fact_versions_object_kind",
        ),
        CheckConstraint("json_valid(object_json)", name="ck_fact_versions_object_json"),
        CheckConstraint(
            "(object_kind='legacy' AND legacy_type IS NOT NULL) OR (object_kind<>'legacy' AND legacy_type IS NULL)",
            name="ck_fact_versions_legacy_type",
        ),
        CheckConstraint(
            "length(content_hash)=64 AND content_hash NOT GLOB '*[^0-9a-f]*'",
            name="ck_fact_versions_content_hash",
        ),
        CheckConstraint(
            "confidence BETWEEN 0.0 AND 1.0", name="ck_fact_versions_confidence"
        ),
        CheckConstraint(
            "verification_count >= 0", name="ck_fact_versions_verification_count"
        ),
        CheckConstraint("is_verified IN (0,1)", name="ck_fact_versions_verified"),
        CheckConstraint(
            "json_valid(evidence_json) AND json_type(evidence_json)='array'",
            name="ck_fact_versions_evidence",
        ),
        CheckConstraint(
            "json_valid(metadata_json) AND json_type(metadata_json)='object'",
            name="ck_fact_versions_metadata",
        ),
        CheckConstraint(
            "valid_to_us IS NULL OR valid_to_us > valid_from_us",
            name="ck_fact_versions_valid_interval",
        ),
        CheckConstraint(
            "transaction_to_us IS NULL OR transaction_to_us > transaction_from_us",
            name="ck_fact_versions_transaction_interval",
        ),
        UniqueConstraint("fact_id", "version", name="uq_fact_versions_fact_version"),
        Index(
            "idx_fact_versions_valid",
            "workspace_id",
            "predicate",
            "valid_from_us",
            "valid_to_us",
        ),
        Index(
            "idx_fact_versions_transaction",
            "workspace_id",
            "transaction_from_us",
            "transaction_to_us",
        ),
        Index("idx_fact_versions_subject", "subject_record_id", "predicate"),
        Index(
            "uq_fact_versions_open_transaction",
            "fact_id",
            unique=True,
            sqlite_where=(transaction_to_us.is_(None)),
        ),
    )


class MemoryRelationshipVersion(Base):
    __tablename__ = "memory_relationship_versions"

    relationship_version_id = Column(String(68), primary_key=True)
    relationship_id = Column(String(68), nullable=False)
    workspace_id = Column(String, nullable=False)
    version = Column(Integer, nullable=False)
    source_record_id = Column(
        String(68),
        ForeignKey("memory_records.record_id", ondelete="RESTRICT"),
        nullable=False,
    )
    target_record_id = Column(
        String(68),
        ForeignKey("memory_records.record_id", ondelete="RESTRICT"),
        nullable=False,
    )
    relationship_type = Column(String, nullable=False)
    legacy_type = Column(String, nullable=True)
    description = Column(Text, nullable=True)
    confidence = Column(Float, nullable=False, default=1.0)
    metadata_json = Column(Text, nullable=False, default="{}")
    content_hash = Column(String(64), nullable=False)
    valid_from_us = Column(Integer, nullable=False)
    valid_to_us = Column(Integer, nullable=True)
    transaction_from_us = Column(Integer, nullable=False)
    transaction_to_us = Column(Integer, nullable=True)
    asserted_by_event_id = Column(
        String(68),
        ForeignKey("memory_events.event_id", ondelete="RESTRICT"),
        nullable=False,
    )
    retracted_by_event_id = Column(
        String(68), ForeignKey("memory_events.event_id", ondelete="RESTRICT")
    )

    __table_args__ = (
        CheckConstraint(
            "length(relationship_version_id)=68 AND "
            "substr(relationship_version_id,1,4)='rel_' AND "
            "substr(relationship_version_id,5) NOT GLOB '*[^0-9a-f]*'",
            name="ck_relationship_versions_id",
        ),
        CheckConstraint(
            "length(relationship_id)=68 AND substr(relationship_id,1,4)='rel_' "
            "AND substr(relationship_id,5) NOT GLOB '*[^0-9a-f]*'",
            name="ck_relationship_versions_relationship_id",
        ),
        CheckConstraint(
            "substr(workspace_id,1,3)='ws_'", name="ck_relationship_versions_workspace"
        ),
        CheckConstraint("version >= 1", name="ck_relationship_versions_version"),
        CheckConstraint(
            "relationship_type IN ('led_to','supersedes','depends_on',"
            "'conflicts_with','related_to','evidence_for','derived_from','invalidates','legacy')",
            name="ck_relationship_versions_type",
        ),
        CheckConstraint(
            "(relationship_type='legacy' AND legacy_type IS NOT NULL) OR (relationship_type<>'legacy' AND legacy_type IS NULL)",
            name="ck_relationship_versions_legacy_type",
        ),
        CheckConstraint(
            "confidence BETWEEN 0.0 AND 1.0", name="ck_relationship_versions_confidence"
        ),
        CheckConstraint(
            "json_valid(metadata_json) AND json_type(metadata_json)='object'",
            name="ck_relationship_versions_metadata",
        ),
        CheckConstraint(
            "length(content_hash)=64 AND content_hash NOT GLOB '*[^0-9a-f]*'",
            name="ck_relationship_versions_content_hash",
        ),
        CheckConstraint(
            "valid_to_us IS NULL OR valid_to_us > valid_from_us",
            name="ck_relationship_versions_valid_interval",
        ),
        CheckConstraint(
            "transaction_to_us IS NULL OR transaction_to_us > transaction_from_us",
            name="ck_relationship_versions_transaction_interval",
        ),
        UniqueConstraint(
            "relationship_id",
            "version",
            name="uq_relationship_versions_relationship_version",
        ),
        Index(
            "idx_relationship_versions_source",
            "workspace_id",
            "source_record_id",
            "relationship_type",
            "valid_to_us",
        ),
        Index(
            "idx_relationship_versions_target",
            "workspace_id",
            "target_record_id",
            "relationship_type",
            "valid_to_us",
        ),
        Index(
            "idx_relationship_versions_valid",
            "workspace_id",
            "valid_from_us",
            "valid_to_us",
        ),
        Index(
            "uq_relationship_versions_open_transaction",
            "relationship_id",
            unique=True,
            sqlite_where=(transaction_to_us.is_(None)),
        ),
    )


class ProjectionManifest(Base):
    __tablename__ = "projection_manifests"

    manifest_id = Column(String(68), primary_key=True)
    workspace_id = Column(String, nullable=False)
    projection_name = Column(String, nullable=False)
    generation = Column(Integer, nullable=False)
    projection_version = Column(Integer, nullable=False)
    status = Column(String, nullable=False)
    source_event_count = Column(Integer, nullable=False)
    source_event_root_hash = Column(String(64), nullable=False)
    cursor_recorded_at_us = Column(Integer, nullable=True)
    cursor_event_id = Column(
        String(68), ForeignKey("memory_events.event_id", ondelete="RESTRICT")
    )
    row_count = Column(Integer, nullable=False)
    builder_version = Column(String, nullable=False)
    details_json = Column(Text, nullable=False, default="{}")
    started_at_us = Column(Integer, nullable=False)
    completed_at_us = Column(Integer, nullable=True)
    activated_at_us = Column(Integer, nullable=True)

    __table_args__ = (
        CheckConstraint(
            "length(manifest_id)=68 AND substr(manifest_id,1,4)='prj_' "
            "AND substr(manifest_id,5) NOT GLOB '*[^0-9a-f]*'",
            name="ck_projection_manifests_id",
        ),
        CheckConstraint(
            "substr(workspace_id,1,3)='ws_'", name="ck_projection_manifests_workspace"
        ),
        CheckConstraint("generation >= 1", name="ck_projection_manifests_generation"),
        CheckConstraint(
            "projection_version >= 1", name="ck_projection_manifests_version"
        ),
        CheckConstraint(
            "status IN ('building','ready','active','rebuild_required','failed')",
            name="ck_projection_manifests_status",
        ),
        CheckConstraint(
            "source_event_count >= 0", name="ck_projection_manifests_event_count"
        ),
        CheckConstraint(
            "length(source_event_root_hash)=64 AND source_event_root_hash NOT GLOB '*[^0-9a-f]*'",
            name="ck_projection_manifests_root_hash",
        ),
        CheckConstraint("row_count >= 0", name="ck_projection_manifests_row_count"),
        CheckConstraint(
            "json_valid(details_json) AND json_type(details_json)='object'",
            name="ck_projection_manifests_details",
        ),
        UniqueConstraint(
            "workspace_id",
            "projection_name",
            "generation",
            name="uq_projection_manifests_generation",
        ),
        Index(
            "uq_projection_active",
            "workspace_id",
            "projection_name",
            unique=True,
            sqlite_where=(status == "active"),
        ),
        Index("idx_projection_status", "workspace_id", "status", "projection_name"),
    )


class RetrievalDocument(Base):
    __tablename__ = "retrieval_documents"

    document_rowid = Column(Integer, primary_key=True)
    workspace_id = Column(String, nullable=False)
    projection_generation = Column(Integer, nullable=False)
    record_id = Column(
        String(68),
        ForeignKey("memory_records.record_id", ondelete="RESTRICT"),
        nullable=False,
    )
    content = Column(Text, nullable=False)
    rationale = Column(Text, nullable=False, server_default="")
    tags_text = Column(Text, nullable=False, server_default="")
    category = Column(String, nullable=False)
    valid_from_us = Column(Integer, nullable=True)
    valid_to_us = Column(Integer, nullable=True)
    transaction_from_us = Column(Integer, nullable=False)
    transaction_to_us = Column(Integer, nullable=True)
    visibility = Column(String, nullable=False, server_default="workspace")
    archived = Column(Integer, nullable=False, server_default="0")
    content_hash = Column(String(64), nullable=False)
    source_event_id = Column(
        String(68),
        ForeignKey("memory_events.event_id", ondelete="RESTRICT"),
        nullable=False,
    )

    __table_args__ = (
        CheckConstraint(
            "substr(workspace_id,1,3)='ws_'", name="ck_retrieval_documents_workspace"
        ),
        CheckConstraint(
            "projection_generation >= 1", name="ck_retrieval_documents_generation"
        ),
        CheckConstraint(
            "visibility IN ('workspace','private','shared')",
            name="ck_retrieval_documents_visibility",
        ),
        CheckConstraint("archived IN (0,1)", name="ck_retrieval_documents_archived"),
        CheckConstraint(
            "length(content_hash)=64 AND content_hash NOT GLOB '*[^0-9a-f]*'",
            name="ck_retrieval_documents_content_hash",
        ),
        CheckConstraint(
            "valid_to_us IS NULL OR valid_from_us IS NULL OR valid_to_us > valid_from_us",
            name="ck_retrieval_documents_valid_interval",
        ),
        CheckConstraint(
            "transaction_to_us IS NULL OR transaction_to_us > transaction_from_us",
            name="ck_retrieval_documents_transaction_interval",
        ),
        UniqueConstraint(
            "workspace_id",
            "projection_generation",
            "record_id",
            name="uq_retrieval_documents_record",
        ),
        Index(
            "idx_retrieval_documents_generation",
            "workspace_id",
            "projection_generation",
            "archived",
            "category",
        ),
        Index(
            "idx_retrieval_documents_record",
            "workspace_id",
            "record_id",
            "projection_generation",
        ),
    )


class RecordProcedure(Base):
    __tablename__ = "record_procedures"

    workspace_id = Column(String, nullable=False)
    projection_generation = Column(Integer, nullable=False)
    record_id = Column(
        String(68),
        ForeignKey("memory_records.record_id", ondelete="RESTRICT"),
        nullable=False,
    )
    ordinal = Column(Integer, nullable=False)
    step_text = Column(Text, nullable=False)
    step_hash = Column(String(64), nullable=False)
    source_event_id = Column(
        String(68),
        ForeignKey("memory_events.event_id", ondelete="RESTRICT"),
        nullable=False,
    )

    __table_args__ = (
        PrimaryKeyConstraint(
            "workspace_id",
            "projection_generation",
            "record_id",
            "ordinal",
            name="pk_record_procedures",
        ),
        CheckConstraint(
            "substr(workspace_id,1,3)='ws_'", name="ck_record_procedures_workspace"
        ),
        CheckConstraint(
            "projection_generation >= 1", name="ck_record_procedures_generation"
        ),
        CheckConstraint("ordinal >= 0", name="ck_record_procedures_ordinal"),
        CheckConstraint(
            "length(step_hash)=64 AND step_hash NOT GLOB '*[^0-9a-f]*'",
            name="ck_record_procedures_step_hash",
        ),
        Index(
            "idx_record_procedures_record",
            "workspace_id",
            "record_id",
            "projection_generation",
        ),
        {"sqlite_with_rowid": False},
    )


class RecordOutcomeView(Base):
    __tablename__ = "record_outcome_view"

    workspace_id = Column(String, nullable=False)
    projection_generation = Column(Integer, nullable=False)
    record_id = Column(
        String(68),
        ForeignKey("memory_records.record_id", ondelete="RESTRICT"),
        nullable=False,
    )
    worked = Column(Integer, nullable=True)
    outcome_text = Column(Text, nullable=True)
    outcome_event_id = Column(
        String(68),
        ForeignKey("memory_events.event_id", ondelete="RESTRICT"),
        nullable=False,
    )
    transaction_at_us = Column(Integer, nullable=False)

    __table_args__ = (
        PrimaryKeyConstraint(
            "workspace_id",
            "projection_generation",
            "record_id",
            name="pk_record_outcome",
        ),
        CheckConstraint(
            "substr(workspace_id,1,3)='ws_'", name="ck_record_outcome_workspace"
        ),
        CheckConstraint(
            "projection_generation >= 1", name="ck_record_outcome_generation"
        ),
        CheckConstraint(
            "worked IS NULL OR worked IN (0,1)", name="ck_record_outcome_worked"
        ),
        Index(
            "idx_record_outcome_worked",
            "workspace_id",
            "projection_generation",
            "worked",
            "transaction_at_us",
        ),
        {"sqlite_with_rowid": False},
    )


class DenseProjectionRef(Base):
    __tablename__ = "dense_projection_refs"

    workspace_id = Column(String, nullable=False)
    provider_key = Column(String, nullable=False)
    projection_generation = Column(Integer, nullable=False)
    record_id = Column(
        String(68),
        ForeignKey("memory_records.record_id", ondelete="RESTRICT"),
        nullable=False,
    )
    content_hash = Column(String(64), nullable=False)
    model_id = Column(String, nullable=False)
    dimension = Column(Integer, nullable=False)
    state = Column(String, nullable=False)
    updated_event_id = Column(
        String(68),
        ForeignKey("memory_events.event_id", ondelete="RESTRICT"),
        nullable=False,
    )
    failure_code = Column(String(80), nullable=True)
    updated_at_us = Column(Integer, nullable=False)
    vector_format = Column(Text, nullable=True)
    vector_sha256 = Column(Text, nullable=True)

    __table_args__ = (
        PrimaryKeyConstraint(
            "workspace_id",
            "provider_key",
            "projection_generation",
            "record_id",
            name="pk_dense_projection_refs",
        ),
        CheckConstraint(
            "substr(workspace_id,1,3)='ws_'", name="ck_dense_refs_workspace"
        ),
        CheckConstraint("projection_generation >= 1", name="ck_dense_refs_generation"),
        CheckConstraint(
            "length(content_hash)=64 AND content_hash NOT GLOB '*[^0-9a-f]*'",
            name="ck_dense_refs_content_hash",
        ),
        CheckConstraint("dimension > 0", name="ck_dense_refs_dimension"),
        CheckConstraint(
            "state IN ('pending','ready','failed','deleted')",
            name="ck_dense_refs_state",
        ),
        CheckConstraint(
            "failure_code IS NULL OR (length(failure_code) BETWEEN 1 AND 80 AND failure_code NOT GLOB '*[^A-Z0-9_]*')",
            name="ck_dense_refs_failure",
        ),
        CheckConstraint(
            "vector_format IS NULL OR vector_format='qdrant-cosine-f32-le-v1'",
            name="ck_dense_refs_vector_format",
        ),
        CheckConstraint(
            "vector_sha256 IS NULL OR (length(vector_sha256)=64 AND vector_sha256 NOT GLOB '*[^0-9a-f]*')",
            name="ck_dense_refs_vector_sha256",
        ),
        Index(
            "idx_dense_refs_state",
            "workspace_id",
            "provider_key",
            "projection_generation",
            "state",
        ),
        {"sqlite_with_rowid": False},
    )


class DenseGenerationReadLease(Base):
    __tablename__ = "dense_generation_read_leases"

    workspace_id = Column(String, nullable=False)
    projection_name = Column(
        String, nullable=False, default="dense", server_default="dense"
    )
    provider_key = Column(String, nullable=False)
    projection_generation = Column(Integer, nullable=False)
    owner_id = Column(String(128), nullable=False)
    fencing_token = Column(Integer, nullable=False)
    acquired_at_us = Column(Integer, nullable=False)
    renewed_at_us = Column(Integer, nullable=False)
    expires_at_us = Column(Integer, nullable=False)

    __table_args__ = (
        PrimaryKeyConstraint(
            "workspace_id",
            "provider_key",
            "projection_generation",
            "owner_id",
            name="pk_dense_generation_read_leases",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "projection_name", "projection_generation"],
            [
                "projection_manifests.workspace_id",
                "projection_manifests.projection_name",
                "projection_manifests.generation",
            ],
            name="fk_dense_generation_read_leases_manifest",
            ondelete="CASCADE",
        ),
        CheckConstraint(
            "length(workspace_id)=27 AND substr(workspace_id,1,3)='ws_' "
            "AND substr(workspace_id,4) NOT GLOB '*[^0-9a-f]*'",
            name="ck_dense_generation_leases_workspace",
        ),
        CheckConstraint(
            "projection_name='dense'",
            name="ck_dense_generation_leases_projection",
        ),
        CheckConstraint(
            "length(provider_key) BETWEEN 1 AND 64",
            name="ck_dense_generation_leases_provider",
        ),
        CheckConstraint(
            "typeof(projection_generation)='integer' AND projection_generation>=1",
            name="ck_dense_generation_leases_generation",
        ),
        CheckConstraint(
            "length(owner_id) BETWEEN 1 AND 128",
            name="ck_dense_generation_leases_owner",
        ),
        CheckConstraint(
            "typeof(fencing_token)='integer' AND fencing_token>=1",
            name="ck_dense_generation_leases_fence",
        ),
        CheckConstraint(
            "typeof(acquired_at_us)='integer' AND acquired_at_us>=0",
            name="ck_dense_generation_leases_acquired",
        ),
        CheckConstraint(
            "typeof(renewed_at_us)='integer' AND renewed_at_us>=acquired_at_us",
            name="ck_dense_generation_leases_renewed",
        ),
        CheckConstraint(
            "typeof(expires_at_us)='integer' AND expires_at_us>renewed_at_us",
            name="ck_dense_generation_leases_expires",
        ),
        Index(
            "idx_dense_generation_leases_expiry",
            "workspace_id",
            "provider_key",
            "projection_generation",
            "expires_at_us",
        ),
        {"sqlite_with_rowid": False},
    )


class DenseGenerationGCJob(Base):
    __tablename__ = "dense_generation_gc_jobs"

    workspace_id = Column(String, nullable=False)
    projection_name = Column(
        String, nullable=False, default="dense", server_default="dense"
    )
    provider_key = Column(String, nullable=False)
    projection_generation = Column(Integer, nullable=False)
    collection_name = Column(String(255), nullable=True)
    status = Column(String, nullable=False)
    attempts = Column(Integer, nullable=False, default=0, server_default="0")
    max_attempts = Column(Integer, nullable=False, default=3, server_default="3")
    available_at_us = Column(Integer, nullable=False)
    claim_owner = Column(String(128), nullable=True)
    claim_token = Column(String(128), nullable=True)
    claim_expires_at_us = Column(Integer, nullable=True)
    last_error_code = Column(String(64), nullable=True)
    created_at_us = Column(Integer, nullable=False)
    updated_at_us = Column(Integer, nullable=False)

    __table_args__ = (
        PrimaryKeyConstraint(
            "workspace_id",
            "provider_key",
            "projection_generation",
            name="pk_dense_generation_gc_jobs",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "projection_name", "projection_generation"],
            [
                "projection_manifests.workspace_id",
                "projection_manifests.projection_name",
                "projection_manifests.generation",
            ],
            name="fk_dense_generation_gc_manifest",
            ondelete="CASCADE",
        ),
        CheckConstraint(
            "length(workspace_id)=27 AND substr(workspace_id,1,3)='ws_' "
            "AND substr(workspace_id,4) NOT GLOB '*[^0-9a-f]*'",
            name="ck_dense_generation_gc_workspace",
        ),
        CheckConstraint(
            "projection_name='dense'", name="ck_dense_generation_gc_projection"
        ),
        CheckConstraint(
            "length(provider_key) BETWEEN 1 AND 64",
            name="ck_dense_generation_gc_provider",
        ),
        CheckConstraint(
            "typeof(projection_generation)='integer' AND projection_generation>=1",
            name="ck_dense_generation_gc_generation",
        ),
        CheckConstraint(
            "collection_name IS NULL OR length(collection_name) BETWEEN 1 AND 255",
            name="ck_dense_generation_gc_collection",
        ),
        CheckConstraint(
            "status IN ('queued','running','dead_letter')",
            name="ck_dense_generation_gc_status",
        ),
        CheckConstraint(
            "typeof(attempts)='integer' AND attempts>=0",
            name="ck_dense_generation_gc_attempts",
        ),
        CheckConstraint(
            "typeof(max_attempts)='integer' AND max_attempts>=1",
            name="ck_dense_generation_gc_max_attempts",
        ),
        CheckConstraint(
            "typeof(available_at_us)='integer' AND available_at_us>=0",
            name="ck_dense_generation_gc_available",
        ),
        CheckConstraint(
            "(status='running' AND claim_owner IS NOT NULL "
            "AND claim_token IS NOT NULL AND claim_expires_at_us IS NOT NULL) "
            "OR (status<>'running' AND claim_owner IS NULL "
            "AND claim_token IS NULL AND claim_expires_at_us IS NULL)",
            name="ck_dense_generation_gc_claim",
        ),
        CheckConstraint(
            "last_error_code IS NULL OR (length(last_error_code) BETWEEN 2 AND 64 "
            "AND last_error_code NOT GLOB '*[^A-Z0-9_]*')",
            name="ck_dense_generation_gc_error",
        ),
        Index(
            "idx_dense_generation_gc_ready",
            "status",
            "available_at_us",
            "workspace_id",
            "projection_generation",
        ),
        {"sqlite_with_rowid": False},
    )


class EnrichmentDecision(Base):
    __tablename__ = "enrichment_decisions"

    decision_id = Column(String(68), primary_key=True)
    workspace_id = Column(String, nullable=False)
    decision_kind = Column(String, nullable=False)
    status = Column(String, nullable=False)
    candidate_hash = Column(String(64), nullable=False)
    target_record_id = Column(
        String(68), ForeignKey("memory_records.record_id", ondelete="RESTRICT")
    )
    proposed_by_event_id = Column(
        String(68), ForeignKey("memory_events.event_id", ondelete="RESTRICT")
    )
    inverse_event_id = Column(
        String(68), ForeignKey("memory_events.event_id", ondelete="RESTRICT")
    )
    policy_version = Column(String, nullable=False)
    confidence = Column(Float, nullable=False)
    evidence_json = Column(Text, nullable=False, default="[]")
    has_unresolved_contradiction = Column(Integer, nullable=False)
    is_security_sensitive = Column(Integer, nullable=False)
    has_deterministic_source = Column(Integer, nullable=False)
    independent_source_count = Column(Integer, nullable=False, default=0)
    reason = Column(Text, nullable=True)
    created_at_us = Column(Integer, nullable=False)
    decided_at_us = Column(Integer, nullable=True)

    __table_args__ = (
        CheckConstraint(
            "length(decision_id)=68 AND substr(decision_id,1,4)='enr_' "
            "AND substr(decision_id,5) NOT GLOB '*[^0-9a-f]*'",
            name="ck_enrichment_decisions_id",
        ),
        CheckConstraint(
            "substr(workspace_id,1,3)='ws_'", name="ck_enrichment_decisions_workspace"
        ),
        CheckConstraint(
            "decision_kind IN ('promote','reject','supersede','rollback')",
            name="ck_enrichment_decisions_kind",
        ),
        CheckConstraint(
            "status IN ('proposed','accepted','rejected','superseded','rolled_back')",
            name="ck_enrichment_decisions_status",
        ),
        CheckConstraint(
            "length(candidate_hash)=64 AND candidate_hash NOT GLOB '*[^0-9a-f]*'",
            name="ck_enrichment_decisions_candidate_hash",
        ),
        CheckConstraint(
            "confidence BETWEEN 0.0 AND 1.0", name="ck_enrichment_decisions_confidence"
        ),
        CheckConstraint(
            "json_valid(evidence_json) AND json_type(evidence_json)='array'",
            name="ck_enrichment_decisions_evidence",
        ),
        CheckConstraint(
            "has_unresolved_contradiction IN (0,1)",
            name="ck_enrichment_decisions_contradiction",
        ),
        CheckConstraint(
            "is_security_sensitive IN (0,1)", name="ck_enrichment_decisions_security"
        ),
        CheckConstraint(
            "has_deterministic_source IN (0,1)", name="ck_enrichment_decisions_source"
        ),
        CheckConstraint(
            "independent_source_count >= 0", name="ck_enrichment_decisions_source_count"
        ),
        UniqueConstraint(
            "workspace_id",
            "decision_kind",
            "candidate_hash",
            "policy_version",
            name="uq_enrichment_decisions_candidate",
        ),
        Index("idx_enrichment_status", "workspace_id", "status", "created_at_us"),
        Index("idx_enrichment_target", "target_record_id", "status"),
    )


class BackgroundJob(Base):
    __tablename__ = "background_jobs"

    job_id = Column(String(68), primary_key=True)
    workspace_id = Column(String, nullable=False)
    job_type = Column(String, nullable=False)
    idempotency_key = Column(String, nullable=False)
    payload_json = Column(Text, nullable=False)
    payload_hash = Column(String(64), nullable=False)
    status = Column(String, nullable=False)
    priority = Column(Integer, nullable=False, default=0)
    attempts = Column(Integer, nullable=False, default=0)
    max_attempts = Column(Integer, nullable=False, default=3)
    available_at_us = Column(Integer, nullable=False)
    lease_owner = Column(String, nullable=True)
    lease_token = Column(String, nullable=True)
    lease_expires_at_us = Column(Integer, nullable=True)
    cancel_requested_at_us = Column(Integer, nullable=True)
    last_error_json = Column(Text, nullable=True)
    result_json = Column(Text, nullable=True)
    source_event_id = Column(
        String(68), ForeignKey("memory_events.event_id", ondelete="RESTRICT")
    )
    created_at_us = Column(Integer, nullable=False)
    updated_at_us = Column(Integer, nullable=False)
    started_at_us = Column(Integer, nullable=True)
    finished_at_us = Column(Integer, nullable=True)

    __table_args__ = (
        CheckConstraint(
            "length(job_id)=68 AND substr(job_id,1,4)='job_' "
            "AND substr(job_id,5) NOT GLOB '*[^0-9a-f]*'",
            name="ck_background_jobs_id",
        ),
        CheckConstraint(
            "substr(workspace_id,1,3)='ws_'", name="ck_background_jobs_workspace"
        ),
        CheckConstraint("json_valid(payload_json)", name="ck_background_jobs_payload"),
        CheckConstraint(
            "length(payload_hash)=64 AND payload_hash NOT GLOB '*[^0-9a-f]*'",
            name="ck_background_jobs_payload_hash",
        ),
        CheckConstraint(
            "status IN ('queued','running','succeeded','failed','cancelled','dead_letter')",
            name="ck_background_jobs_status",
        ),
        CheckConstraint("attempts >= 0", name="ck_background_jobs_attempts"),
        CheckConstraint("max_attempts >= 1", name="ck_background_jobs_max_attempts"),
        CheckConstraint(
            "last_error_json IS NULL OR json_valid(last_error_json)",
            name="ck_background_jobs_last_error",
        ),
        CheckConstraint(
            "result_json IS NULL OR json_valid(result_json)",
            name="ck_background_jobs_result",
        ),
        CheckConstraint(
            "(status='running' AND lease_owner IS NOT NULL AND lease_token IS NOT NULL AND lease_expires_at_us IS NOT NULL) OR (status<>'running' AND lease_owner IS NULL AND lease_token IS NULL AND lease_expires_at_us IS NULL)",
            name="ck_background_jobs_running_lease",
        ),
        UniqueConstraint(
            "workspace_id",
            "job_type",
            "idempotency_key",
            name="uq_background_jobs_idempotency",
        ),
        Index(
            "idx_background_jobs_claim",
            "status",
            "available_at_us",
            priority.desc(),
            "created_at_us",
        ),
        Index("idx_background_jobs_lease", "status", "lease_expires_at_us"),
    )


class V7MigrationRun(Base):
    __tablename__ = "v7_migration_runs"

    migration_run_id = Column(String(68), primary_key=True)
    workspace_id = Column(String, nullable=False)
    source_db_sha256 = Column(String(64), nullable=False)
    source_schema_version = Column(Integer, nullable=False)
    source_format_version = Column(Integer, nullable=False)
    target_format_version = Column(Integer, nullable=False, default=7)
    status = Column(String, nullable=False)
    snapshot_name = Column(Text, nullable=False)
    candidate_name = Column(Text, nullable=False)
    source_inventory_json = Column(Text, nullable=False)
    validation_json = Column(Text, nullable=True)
    last_error_json = Column(Text, nullable=True)
    created_at_us = Column(Integer, nullable=False)
    updated_at_us = Column(Integer, nullable=False)
    validated_at_us = Column(Integer, nullable=True)
    activated_at_us = Column(Integer, nullable=True)
    rolled_back_at_us = Column(Integer, nullable=True)

    __table_args__ = (
        CheckConstraint(
            "length(migration_run_id)=68 AND substr(migration_run_id,1,4)='mig_' "
            "AND substr(migration_run_id,5) NOT GLOB '*[^0-9a-f]*'",
            name="ck_v7_migration_runs_id",
        ),
        CheckConstraint(
            "substr(workspace_id,1,3)='ws_'", name="ck_v7_migration_runs_workspace"
        ),
        CheckConstraint(
            "length(source_db_sha256)=64 AND source_db_sha256 NOT GLOB '*[^0-9a-f]*'",
            name="ck_v7_migration_runs_source_hash",
        ),
        CheckConstraint(
            "target_format_version=7", name="ck_v7_migration_runs_target_format"
        ),
        CheckConstraint(
            "status IN ('snapshotted','importing','validating','ready','active','failed','rolled_back')",
            name="ck_v7_migration_runs_status",
        ),
        CheckConstraint(
            "json_valid(source_inventory_json) AND json_type(source_inventory_json)='object'",
            name="ck_v7_migration_runs_inventory",
        ),
        CheckConstraint(
            "validation_json IS NULL OR json_valid(validation_json)",
            name="ck_v7_migration_runs_validation",
        ),
        CheckConstraint(
            "last_error_json IS NULL OR json_valid(last_error_json)",
            name="ck_v7_migration_runs_error",
        ),
        UniqueConstraint(
            "workspace_id",
            "source_db_sha256",
            "target_format_version",
            name="uq_v7_migration_runs_source",
        ),
        Index(
            "idx_v7_migration_runs_status", "workspace_id", "status", "updated_at_us"
        ),
    )


class V7MigrationCheckpoint(Base):
    __tablename__ = "v7_migration_checkpoints"

    migration_run_id = Column(
        String(68),
        ForeignKey("v7_migration_runs.migration_run_id", ondelete="RESTRICT"),
        nullable=False,
    )
    source_table = Column(String, nullable=False)
    last_legacy_pk = Column(Text, nullable=True)
    rows_imported = Column(Integer, nullable=False, default=0)
    rolling_hash = Column(String(64), nullable=False)
    completed = Column(Integer, nullable=False, default=0)
    updated_at_us = Column(Integer, nullable=False)

    __table_args__ = (
        PrimaryKeyConstraint(
            "migration_run_id", "source_table", name="pk_v7_migration_checkpoints"
        ),
        CheckConstraint("rows_imported >= 0", name="ck_v7_checkpoints_rows"),
        CheckConstraint(
            "length(rolling_hash)=64 AND rolling_hash NOT GLOB '*[^0-9a-f]*'",
            name="ck_v7_checkpoints_hash",
        ),
        CheckConstraint("completed IN (0,1)", name="ck_v7_checkpoints_completed"),
    )


class LegacyIdMap(Base):
    __tablename__ = "legacy_id_map"

    migration_run_id = Column(
        String(68),
        ForeignKey("v7_migration_runs.migration_run_id", ondelete="RESTRICT"),
        nullable=False,
    )
    source_table = Column(String, nullable=False)
    legacy_id = Column(Text, nullable=False)
    workspace_id = Column(String, nullable=False)
    target_kind = Column(String, nullable=False)
    target_id = Column(String, nullable=False)
    source_row_hash = Column(String(64), nullable=False)
    imported_event_id = Column(
        String(68),
        ForeignKey("memory_events.event_id", ondelete="RESTRICT"),
        nullable=False,
    )

    __table_args__ = (
        PrimaryKeyConstraint(
            "migration_run_id", "source_table", "legacy_id", name="pk_legacy_id_map"
        ),
        CheckConstraint(
            "substr(workspace_id,1,3)='ws_'", name="ck_legacy_id_map_workspace"
        ),
        CheckConstraint(
            "target_kind IN ('memory','fact','relationship','placeholder')",
            name="ck_legacy_id_map_kind",
        ),
        CheckConstraint(
            "length(source_row_hash)=64 AND source_row_hash NOT GLOB '*[^0-9a-f]*'",
            name="ck_legacy_id_map_source_hash",
        ),
        UniqueConstraint(
            "migration_run_id",
            "target_kind",
            "target_id",
            name="uq_legacy_id_map_target",
        ),
        Index("idx_legacy_id_map_source", "workspace_id", "source_table", "legacy_id"),
    )


class PublicObjectId(Base):
    """Internal, immutable mapping from legacy keys to opaque v7 IDs."""

    __tablename__ = "public_object_ids"

    workspace_id = Column(String(27), nullable=False)
    object_kind = Column(String, nullable=False)
    source_key = Column(Text, nullable=False)
    projection_generation = Column(Integer, nullable=False)
    public_id = Column(String(69), nullable=False)
    created_at_us = Column(Integer, nullable=False)

    __table_args__ = (
        PrimaryKeyConstraint(
            "workspace_id",
            "object_kind",
            "source_key",
            "projection_generation",
            name="pk_public_object_ids",
        ),
        CheckConstraint(
            "length(workspace_id)=27 AND substr(workspace_id,1,3)='ws_' "
            "AND substr(workspace_id,4) NOT GLOB '*[^0-9a-f]*'",
            name="ck_public_object_ids_workspace",
        ),
        CheckConstraint(
            "object_kind IN "
            "('rule','trigger','entity','active_context','community','code')",
            name="ck_public_object_ids_kind",
        ),
        CheckConstraint(
            "((substr(source_key,1,2)='i:' AND length(source_key) BETWEEN 3 AND 21 "
            "AND substr(source_key,3) NOT GLOB '*[^0-9]*' "
            "AND CAST(substr(source_key,3) AS INTEGER) BETWEEN 1 AND 9223372036854775807 "
            "AND CAST(CAST(substr(source_key,3) AS INTEGER) AS TEXT)=substr(source_key,3)) "
            "OR (substr(source_key,1,2)='s:' AND length(source_key) BETWEEN 3 AND 514))",
            name="ck_public_object_ids_source",
        ),
        CheckConstraint(
            "((object_kind IN ('rule','trigger','entity','active_context') "
            "AND projection_generation=0) OR (object_kind IN ('community','code') "
            "AND projection_generation BETWEEN 1 AND 9223372036854775807))",
            name="ck_public_object_ids_generation",
        ),
        CheckConstraint(
            "(object_kind='rule' AND length(public_id)=69 "
            "AND substr(public_id,1,5)='rule_' "
            "AND substr(public_id,6) NOT GLOB '*[^0-9a-f]*') OR "
            "(object_kind='trigger' AND length(public_id)=68 "
            "AND substr(public_id,1,4)='trg_' "
            "AND substr(public_id,5) NOT GLOB '*[^0-9a-f]*') OR "
            "(object_kind='entity' AND length(public_id)=68 "
            "AND substr(public_id,1,4)='ent_' "
            "AND substr(public_id,5) NOT GLOB '*[^0-9a-f]*') OR "
            "(object_kind='active_context' AND length(public_id)=68 "
            "AND substr(public_id,1,4)='act_' "
            "AND substr(public_id,5) NOT GLOB '*[^0-9a-f]*') OR "
            "(object_kind='community' AND length(public_id)=68 "
            "AND substr(public_id,1,4)='com_' "
            "AND substr(public_id,5) NOT GLOB '*[^0-9a-f]*') OR "
            "(object_kind='code' AND length(public_id)=69 "
            "AND substr(public_id,1,5)='code_' "
            "AND substr(public_id,6) NOT GLOB '*[^0-9a-f]*')",
            name="ck_public_object_ids_public_id",
        ),
        CheckConstraint(
            "created_at_us BETWEEN 0 AND 9223372036854775807",
            name="ck_public_object_ids_created",
        ),
        UniqueConstraint("public_id", name="uq_public_object_ids_public"),
        Index(
            "idx_public_object_ids_reverse",
            "workspace_id",
            "object_kind",
            "public_id",
            "projection_generation",
        ),
        {"sqlite_with_rowid": False},
    )


class ActiveContextEntry(Base):
    """Canonical v7 active-context state bound to one workspace record."""

    __tablename__ = "active_context_entries"

    active_context_id = Column(String(68), primary_key=True)
    workspace_id = Column(String(27), nullable=False)
    record_id = Column(String(68), nullable=False)
    priority = Column(Integer, nullable=False, default=0)
    reason = Column(Text, nullable=True)
    added_at_us = Column(Integer, nullable=False)
    expires_at_us = Column(Integer, nullable=True)
    removed_at_us = Column(Integer, nullable=True)

    __table_args__ = (
        CheckConstraint(
            "length(active_context_id)=68 "
            "AND substr(active_context_id,1,4)='act_' "
            "AND substr(active_context_id,5) NOT GLOB '*[^0-9a-f]*'",
            name="ck_active_context_entries_id",
        ),
        CheckConstraint(
            "length(workspace_id)=27 AND substr(workspace_id,1,3)='ws_' "
            "AND substr(workspace_id,4) NOT GLOB '*[^0-9a-f]*'",
            name="ck_active_context_entries_workspace",
        ),
        CheckConstraint(
            "length(record_id)=68 AND substr(record_id,1,4)='mem_' "
            "AND substr(record_id,5) NOT GLOB '*[^0-9a-f]*'",
            name="ck_active_context_entries_record",
        ),
        CheckConstraint(
            "typeof(priority)='integer' AND priority BETWEEN -100 AND 100",
            name="ck_active_context_entries_priority",
        ),
        CheckConstraint(
            "reason IS NULL OR (typeof(reason)='text' "
            "AND length(reason) BETWEEN 1 AND 2000)",
            name="ck_active_context_entries_reason",
        ),
        CheckConstraint(
            "typeof(added_at_us)='integer' "
            "AND added_at_us BETWEEN 0 AND 9223372036854775807",
            name="ck_active_context_entries_added",
        ),
        CheckConstraint(
            "expires_at_us IS NULL OR (typeof(expires_at_us)='integer' "
            "AND expires_at_us BETWEEN 0 AND 9223372036854775807)",
            name="ck_active_context_entries_expires",
        ),
        CheckConstraint(
            "removed_at_us IS NULL OR (typeof(removed_at_us)='integer' "
            "AND removed_at_us BETWEEN added_at_us AND 9223372036854775807)",
            name="ck_active_context_entries_removed",
        ),
        UniqueConstraint(
            "workspace_id",
            "record_id",
            name="uq_active_context_entries_record",
        ),
        ForeignKeyConstraint(
            ("workspace_id", "record_id"),
            ("memory_records.workspace_id", "memory_records.record_id"),
            name="fk_active_context_entries_record",
            onupdate="RESTRICT",
            ondelete="RESTRICT",
        ),
        Index(
            "idx_active_context_entries_current",
            "workspace_id",
            "removed_at_us",
            priority.desc(),
            added_at_us.desc(),
            "active_context_id",
        ),
        Index(
            "idx_active_context_entries_expiry",
            "workspace_id",
            "expires_at_us",
            sqlite_where=removed_at_us.is_(None) & expires_at_us.is_not(None),
        ),
        {"sqlite_with_rowid": False},
    )
