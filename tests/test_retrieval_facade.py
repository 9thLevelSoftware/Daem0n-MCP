"""Public import contracts for the v7 retrieval subsystem."""

from __future__ import annotations

import unittest


class RetrievalFacadeTests(unittest.TestCase):
    def test_facade_exports_landed_provider_planner_and_service_apis(self):
        from daem0nmcp import retrieval
        from daem0nmcp.retrieval.fusion import MAX_RRF_K
        from daem0nmcp.retrieval.planner import (
            ProviderRequest,
            RetrievalPlan,
            RetrievalPlanner,
        )
        from daem0nmcp.retrieval.providers import (
            DenseProvider,
            build_dense_point_payload,
            create_qdrant_client,
            dense_manifest_details,
            dense_point_id,
        )
        from daem0nmcp.retrieval.repository import (
            RetrievalRepositoryError,
            SQLiteRetrievalRepository,
            sqlite_read_connection_factory,
        )
        from daem0nmcp.retrieval.rerank import EmbeddingSimilarityReranker
        from daem0nmcp.retrieval.runtime import (
            ConfiguredEmbeddingEncoder,
            CoreTokenizer,
            create_projection_builders,
            create_retrieval_service,
            drain_projection_jobs,
        )
        from daem0nmcp.retrieval.service import (
            AsyncEvidenceComposer,
            RetrievalClock,
            RetrievalRepository,
            RetrievalReranker,
            RetrievalService,
        )
        from daem0nmcp.retrieval.specialized_projection import (
            SpecializedProjectionBuilder,
            SpecializedProjectionBuildError,
            SpecializedProjectionBuildResult,
        )
        from daem0nmcp.retrieval.types import MAX_TOKEN_BUDGET

        expected = {
            "AsyncEvidenceComposer": AsyncEvidenceComposer,
            "DenseProvider": DenseProvider,
            "EmbeddingSimilarityReranker": EmbeddingSimilarityReranker,
            "MAX_RRF_K": MAX_RRF_K,
            "MAX_TOKEN_BUDGET": MAX_TOKEN_BUDGET,
            "ProviderRequest": ProviderRequest,
            "RetrievalClock": RetrievalClock,
            "RetrievalPlan": RetrievalPlan,
            "RetrievalPlanner": RetrievalPlanner,
            "RetrievalRepository": RetrievalRepository,
            "RetrievalReranker": RetrievalReranker,
            "RetrievalRepositoryError": RetrievalRepositoryError,
            "RetrievalService": RetrievalService,
            "SQLiteRetrievalRepository": SQLiteRetrievalRepository,
            "SpecializedProjectionBuildError": SpecializedProjectionBuildError,
            "SpecializedProjectionBuildResult": SpecializedProjectionBuildResult,
            "SpecializedProjectionBuilder": SpecializedProjectionBuilder,
            "ConfiguredEmbeddingEncoder": ConfiguredEmbeddingEncoder,
            "CoreTokenizer": CoreTokenizer,
            "build_dense_point_payload": build_dense_point_payload,
            "create_projection_builders": create_projection_builders,
            "create_qdrant_client": create_qdrant_client,
            "create_retrieval_service": create_retrieval_service,
            "dense_manifest_details": dense_manifest_details,
            "dense_point_id": dense_point_id,
            "drain_projection_jobs": drain_projection_jobs,
            "sqlite_read_connection_factory": sqlite_read_connection_factory,
        }
        for name, value in expected.items():
            with self.subTest(name=name):
                self.assertIs(value, getattr(retrieval, name, None))


if __name__ == "__main__":
    unittest.main()
