"""Rebuildable v7 retrieval contracts, projections, and ranking policy."""

from .composer import CompositionResult, EvidenceComposer, SelectedEvidence
from .dense_projection import (
    DenseProjectionBuilder,
    DenseProjectionBuildError,
    DenseProjectionBuildResult,
)
from .fusion import (
    DEFAULT_RRF_K,
    DEFAULT_RRF_WEIGHTS,
    MAX_RRF_K,
    weighted_reciprocal_rank_fusion,
)
from .planner import ProviderRequest, RetrievalPlan, RetrievalPlanner
from .policy import PolicyRecord, PolicyResult, apply_retrieval_policy
from .projections import (
    LexicalProjectionBuilder,
    ProjectionBuildError,
    ProjectionBuildResult,
)
from .providers import (
    DenseProvider,
    LexicalProvider,
    build_dense_point_payload,
    create_qdrant_client,
    dense_manifest_details,
    dense_point_id,
)
from .repository import (
    RetrievalRepositoryError,
    SQLiteRetrievalRepository,
    sqlite_read_connection_factory,
)
from .rerank import EmbeddingSimilarityReranker
from .runtime import (
    ConfiguredEmbeddingEncoder,
    CoreTokenizer,
    create_projection_builders,
    create_retrieval_service,
    drain_projection_jobs,
    normalize_legacy_category_filter,
    resolve_legacy_record_filter,
)
from .service import (
    AsyncEvidenceComposer,
    RetrievalClock,
    RetrievalRepository,
    RetrievalReranker,
    RetrievalService,
)
from .specialized import (
    MAX_GRAPH_BRANCHING,
    MAX_GRAPH_DEPTH,
    PROCEDURE_FTS_BUILD_CONFIG_HASH,
    GraphProvider,
    OutcomeProvider,
    ProcedureProvider,
    TemporalProvider,
    procedure_fts_table_name,
)
from .specialized_projection import (
    SpecializedProjectionBuilder,
    SpecializedProjectionBuildError,
    SpecializedProjectionBuildResult,
)
from .types import (
    MAX_TOKEN_BUDGET,
    Candidate,
    CitationEntry,
    ContextPackage,
    EvidenceItem,
    EvidenceRef,
    FusedCandidate,
    ProviderDiagnostic,
    ProviderResult,
    RetrievalProvider,
    RetrievalQuery,
    RetrievalResult,
)

__all__ = [
    "AsyncEvidenceComposer",
    "Candidate",
    "CitationEntry",
    "CompositionResult",
    "ConfiguredEmbeddingEncoder",
    "ContextPackage",
    "CoreTokenizer",
    "DEFAULT_RRF_K",
    "DEFAULT_RRF_WEIGHTS",
    "DenseProvider",
    "DenseProjectionBuildError",
    "DenseProjectionBuildResult",
    "DenseProjectionBuilder",
    "EvidenceComposer",
    "EvidenceItem",
    "EvidenceRef",
    "EmbeddingSimilarityReranker",
    "FusedCandidate",
    "GraphProvider",
    "LexicalProjectionBuilder",
    "LexicalProvider",
    "MAX_RRF_K",
    "MAX_GRAPH_BRANCHING",
    "MAX_GRAPH_DEPTH",
    "MAX_TOKEN_BUDGET",
    "PolicyRecord",
    "PolicyResult",
    "OutcomeProvider",
    "PROCEDURE_FTS_BUILD_CONFIG_HASH",
    "ProjectionBuildError",
    "ProjectionBuildResult",
    "ProviderDiagnostic",
    "ProviderRequest",
    "ProviderResult",
    "ProcedureProvider",
    "RetrievalClock",
    "RetrievalPlan",
    "RetrievalPlanner",
    "RetrievalProvider",
    "RetrievalQuery",
    "RetrievalRepository",
    "RetrievalResult",
    "RetrievalReranker",
    "RetrievalRepositoryError",
    "RetrievalService",
    "SQLiteRetrievalRepository",
    "SelectedEvidence",
    "SpecializedProjectionBuildError",
    "SpecializedProjectionBuildResult",
    "SpecializedProjectionBuilder",
    "TemporalProvider",
    "apply_retrieval_policy",
    "build_dense_point_payload",
    "create_projection_builders",
    "create_qdrant_client",
    "create_retrieval_service",
    "dense_manifest_details",
    "dense_point_id",
    "drain_projection_jobs",
    "normalize_legacy_category_filter",
    "procedure_fts_table_name",
    "resolve_legacy_record_filter",
    "sqlite_read_connection_factory",
    "weighted_reciprocal_rank_fusion",
]
