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
    Column,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
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
