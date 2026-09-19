from __future__ import annotations

import asyncio
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from daem0nmcp.config import Settings


class ProductionCompositionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name).resolve()
        from daem0nmcp.database import DatabaseManager
        from daem0nmcp.workspace import WorkspaceRegistry

        self.workspace = WorkspaceRegistry([self.root], default_root=self.root).default

        storage = self.root / ".daem0nmcp" / "storage"
        database = DatabaseManager(str(storage))
        try:
            with patch("daem0nmcp.retrieval.runtime.schedule_projection_job_drain"):
                asyncio.run(database.init_db())
        finally:
            asyncio.run(database.close())
        self.settings = Settings(
            project_root=str(self.root),
            workspace_roots=[],
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_stdio_surface_is_exact_and_uses_one_runtime_authority(self) -> None:
        from daem0nmcp.api.v7.policy import V7_TOOL_LEVELS
        from daem0nmcp.api.v7.production import build_production_surface

        surface = build_production_surface(
            "stdio",
            settings=self.settings,
            environ={},
        )

        self.assertEqual(set(surface.handlers), set(V7_TOOL_LEVELS))
        self.assertEqual(len(surface.manifest.tools), 75)
        self.assertEqual(len(surface.manifest.resources), 10)
        self.assertEqual(
            surface.workspace_resolver.default.root,
            self.root,
        )
        self.assertIs(
            surface.gate,
            surface.middleware[0]._gate,
        )
        from daem0nmcp.api.v7.opaque_capabilities import (
            OpaqueCapabilityAuthority,
        )

        self.assertIsInstance(
            surface.gate.authority,
            OpaqueCapabilityAuthority,
        )
        self.assertEqual("stdio", surface.middleware[0]._transport_mode)

    def test_native_runtime_preloads_only_each_enabled_profile(
        self,
    ) -> None:
        from daem0nmcp.api.v7 import production

        imported: list[str] = []
        with patch.object(
            production.importlib,
            "import_module",
            side_effect=lambda name: imported.append(name),
        ):
            production._OptionalNativeRuntimeLifecycle(
                {"local": "disabled", "models-local": "disabled"}
            ).start()
            self.assertEqual([], imported)

            production._OptionalNativeRuntimeLifecycle(
                {"local": "ready", "models-local": "disabled"}
            ).start()
            self.assertEqual(["qdrant_client"], imported)
            production._OptionalNativeRuntimeLifecycle(
                {"local": "disabled", "models-local": "ready"}
            ).start()
            production._OptionalNativeRuntimeLifecycle({"graph": "ready"}).start()

        self.assertEqual(
            [
                "qdrant_client",
                "sentence_transformers",
                "onnx",
                "onnxruntime",
                "numpy",
                "networkx",
                "igraph",
                "leidenalg",
            ],
            imported,
        )

    def test_full_manifest_rejects_missing_or_unexpected_resources(self) -> None:
        from daem0nmcp.api.v7.production import build_production_surface
        from daem0nmcp.api.v7.registry import ManifestError

        manifest = build_production_surface(
            "stdio",
            settings=self.settings,
            environ={},
        ).manifest
        with self.assertRaisesRegex(ManifestError, "resource set"):
            replace(manifest, resources=manifest.resources[:-1])

        invented = replace(
            manifest.resources[0],
            uri_template=("memory://workspaces/{workspace_id}/invented"),
        )
        with self.assertRaisesRegex(ManifestError, "resource set"):
            replace(manifest, resources=manifest.resources + (invented,))

    def test_every_supported_operation_is_wired_not_placeholdered(self) -> None:
        from daem0nmcp.api.v7 import production
        from daem0nmcp.api.v7.pinned import PINNED_HANDLER_NAMES
        from daem0nmcp.api.v7.policy import V7_TOOL_LEVELS

        remaining_unimplemented: set[str] = set()
        expected = (
            set(V7_TOOL_LEVELS) - set(PINNED_HANDLER_NAMES) - remaining_unimplemented
        )
        observed: set[str] = set()
        original = production.build_v7_surface

        def capture(**kwargs):
            observed.update(kwargs["operations"])
            return original(**kwargs)

        with patch(
            "daem0nmcp.api.v7.production.build_v7_surface",
            side_effect=capture,
        ):
            production.build_production_surface(
                "stdio",
                settings=self.settings,
                environ={},
            )

        self.assertEqual(expected, observed)

    def test_briefing_zero_limits_skip_bounded_resource_reads(self) -> None:
        from daem0nmcp.api.v7.production import _briefing_reader
        from daem0nmcp.api.v7.resource_repository import (
            ResourceRepositoryReaders,
        )

        async def forbidden(*_args, **_kwargs):
            raise AssertionError("a zero-bound resource was read")

        async def empty(*_args, **_kwargs):
            return []

        reader = _briefing_reader(
            ResourceRepositoryReaders(
                warning_reader=forbidden,
                failure_reader=forbidden,
                rule_reader=empty,
                active_context_reader=empty,
            )
        )

        result = asyncio.run(
            reader(
                self.workspace,
                SimpleNamespace(
                    warning_limit=0,
                    failure_limit=0,
                    focus_areas=[],
                ),
            )
        )

        self.assertEqual([], result["warnings"])
        self.assertEqual([], result["failed_outcomes"])

    def test_briefing_uses_one_generation_snapshot_when_available(self) -> None:
        from daem0nmcp.api.v7.production import _briefing_reader
        from daem0nmcp.api.v7.resource_repository import (
            ResourceRepositoryReaders,
            ResourceRepositorySnapshot,
        )

        async def forbidden(*_args, **_kwargs):
            raise AssertionError("individual resource read mixed generations")

        observed: list[tuple[int, int]] = []

        async def snapshot(_workspace, **limits):
            observed.append((limits["warning_limit"], limits["failure_limit"]))
            return ResourceRepositorySnapshot([], [], [], [])

        reader = _briefing_reader(
            ResourceRepositoryReaders(
                warning_reader=forbidden,
                failure_reader=forbidden,
                rule_reader=forbidden,
                active_context_reader=forbidden,
                briefing_snapshot_reader=snapshot,
            )
        )

        asyncio.run(
            reader(
                self.workspace,
                SimpleNamespace(
                    warning_limit=7,
                    failure_limit=9,
                    focus_areas=[],
                ),
            )
        )

        self.assertEqual([(7, 9)], observed)

    def test_briefing_populates_every_category_and_uses_focus_areas(self) -> None:
        from daem0nmcp.api.v7.models import RecordSummary
        from daem0nmcp.api.v7.production import _briefing_reader
        from daem0nmcp.api.v7.resource_repository import (
            ResourceRepositoryReaders,
            ResourceRepositorySnapshot,
        )
        from daem0nmcp.api.v7.resources import ResourceRow
        from daem0nmcp.api.v7.tools import (
            GitChangeSummary,
            ProjectionManifest,
        )

        now = datetime(2026, 1, 1, tzinfo=timezone.utc)
        decision = RecordSummary(
            record_id="mem_" + "d" * 64,
            record_type="decision",
            excerpt="Use retrieval generation manifests for SQLite cutover.",
            tags=["retrieval", "sqlite"],
            relative_file_path=None,
            current_status="current",
            content_hash="d" * 64,
            created_at=now,
            updated_at=now,
        )
        failed = RecordSummary(
            record_id="mem_" + "f" * 64,
            record_type="learning",
            excerpt="The first migration attempt failed validation.",
            tags=["migration"],
            relative_file_path=None,
            current_status="current",
            content_hash="f" * 64,
            created_at=now,
            updated_at=now,
        )
        projection = ProjectionManifest(
            projection="lexical",
            generation=3,
            built_at=now,
            source_root_hash="a" * 64,
        )
        change = GitChangeSummary(
            relative_file_path="daem0nmcp/retrieval/service.py",
            status="modified",
        )

        async def forbidden(*_args, **_kwargs):
            raise AssertionError("snapshot-capable briefing split its reads")

        async def snapshot(_workspace, **_limits):
            return ResourceRepositorySnapshot(
                warnings=[],
                failures=[ResourceRow(item=failed, deleted=False)],
                rules=[],
                active_context=[],
                decisions=[ResourceRow(item=decision, deleted=False)],
                git_changes=[change],
                projection_freshness=[projection],
                workspace_statistics={"records": 42, "decisions": 7},
                stale_projection_count=1,
            )

        reader = _briefing_reader(
            ResourceRepositoryReaders(
                warning_reader=forbidden,
                failure_reader=forbidden,
                rule_reader=forbidden,
                active_context_reader=forbidden,
                briefing_snapshot_reader=snapshot,
            )
        )
        result = asyncio.run(
            reader(
                self.workspace,
                SimpleNamespace(
                    warning_limit=10,
                    failure_limit=10,
                    focus_areas=["retrieval"],
                ),
            )
        )

        self.assertEqual(result["recent_decisions"], [decision])
        self.assertEqual(result["git_changes"], [change])
        self.assertEqual(result["projection_freshness"], [projection])
        self.assertEqual(result["workspace_statistics"]["records"], 42)
        from daem0nmcp.api.v7.tools import SessionBriefData

        validated = SessionBriefData.model_validate(result)
        self.assertEqual(validated.failed_outcomes[0].worked, False)
        self.assertIn(
            "projection_rebuild",
            {step["tool"] for step in result["covenant_next_steps"]},
        )

    def test_preflight_uses_one_snapshot_and_filters_failures_by_target(self) -> None:
        from daem0nmcp.api.v7.models import RecordSummary
        from daem0nmcp.api.v7.production import _guidance_reader
        from daem0nmcp.api.v7.resource_repository import (
            ResourceRepositoryReaders,
            ResourceRepositorySnapshot,
        )
        from daem0nmcp.api.v7.resources import ResourceRow, RuleView
        from daem0nmcp.workspace import Workspace

        now = datetime(2026, 1, 1, tzinfo=timezone.utc)

        def record(suffix: str, excerpt: str, record_type: str) -> RecordSummary:
            return RecordSummary(
                record_id="mem_" + suffix * 64,
                record_type=record_type,
                excerpt=excerpt,
                tags=[],
                relative_file_path=None,
                current_status="current",
                content_hash=suffix * 64,
                created_at=now,
                updated_at=now,
            )

        relevant_failure = record(
            "a", "SQLite workspace import failed during migration.", "learning"
        )
        unrelated_warning = record(
            "b", "Avoid changing CSS colors without screenshots.", "warning"
        )
        relevant_rule = RuleView(
            rule_id="rule_" + "c" * 64,
            trigger="workspace import SQLite migration",
            must_do=["Validate the import bundle before activation."],
            must_not=[],
            ask_first=[],
            warnings=[],
            priority=90,
            enabled=True,
            created_at=now,
        )
        unrelated_rule = RuleView(
            rule_id="rule_" + "d" * 64,
            trigger="CSS theme",
            must_do=["Capture a screenshot."],
            must_not=[],
            ask_first=[],
            warnings=[],
            priority=80,
            enabled=True,
            created_at=now,
        )
        calls: list[object] = []

        async def forbidden(*_args, **_kwargs):
            raise AssertionError("preflight split its storage snapshot")

        async def snapshot(_workspace, **limits):
            calls.append(limits)
            return ResourceRepositorySnapshot(
                warnings=[ResourceRow(item=unrelated_warning, deleted=False)],
                failures=[ResourceRow(item=relevant_failure, deleted=False)],
                rules=[relevant_rule, unrelated_rule],
                active_context=[],
            )

        reader = _guidance_reader(
            ResourceRepositoryReaders(
                warning_reader=forbidden,
                failure_reader=forbidden,
                rule_reader=forbidden,
                active_context_reader=forbidden,
                briefing_snapshot_reader=snapshot,
            )
        )
        result = asyncio.run(
            reader(
                Workspace("ws_" + "a" * 24, self.root),
                "workspace_import",
                {"mode": "merge", "validate": True},
                "Import a SQLite migration bundle.",
            )
        )

        self.assertEqual(len(calls), 1)
        # Guidance never shows git changes, so it must not spawn git for them.
        self.assertIs(calls[0]["include_git_changes"], False)
        self.assertEqual(result["records"], [relevant_failure])
        self.assertEqual(result["rules"], [relevant_rule])
        self.assertIn(
            "Validate the import bundle before activation.",
            result["must_do"],
        )
        self.assertNotIn(unrelated_warning, result["records"])
        self.assertNotIn(unrelated_rule, result["rules"])

    def test_loopback_http_is_explicit_but_remote_http_fails_closed(self) -> None:
        from daem0nmcp.api.v7.production import build_production_surface
        from daem0nmcp.transport_security import TransportSecurityError

        loopback = build_production_surface(
            "streamable-http",
            host="127.0.0.1",
            settings=self.settings,
            environ={},
        )
        middleware = loopback.middleware[0]
        self.assertTrue(middleware._allow_unauthenticated_loopback)

        with self.assertRaisesRegex(
            TransportSecurityError,
            "REMOTE_BIND_REQUIRES_AUTH",
        ):
            build_production_surface(
                "streamable-http",
                host="0.0.0.0",
                settings=self.settings,
                environ={},
            )

    def test_remote_http_requires_capability_authority_as_well_as_jwt(self) -> None:
        from daem0nmcp.api.v7.production import (
            ProductionConfigurationError,
            build_production_surface,
        )

        with (
            patch(
                "daem0nmcp.api.v7.production.build_fastmcp_auth",
                return_value=object(),
            ),
            patch("daem0nmcp.api.v7.production.validate_transport_security"),
        ):
            with self.assertRaisesRegex(
                ProductionConfigurationError,
                "CAPABILITY_AUTHORITY_UNAVAILABLE",
            ):
                build_production_surface(
                    "streamable-http",
                    host="0.0.0.0",
                    settings=self.settings,
                    environ={},
                )

            surface = build_production_surface(
                "streamable-http",
                host="0.0.0.0",
                settings=self.settings,
                environ={"DAEM0NMCP_TOKEN_SECRET": "s" * 32},
            )
        self.assertFalse(surface.middleware[0]._allow_unauthenticated_loopback)

    def test_create_server_passes_reviewed_runtime_options(self) -> None:
        from daem0nmcp.api.v7.production import create_v7_server

        sentinel = object()
        observed: list[dict[str, object]] = []

        def build_server(_surface, **options):
            observed.append(options)
            return sentinel

        with (
            patch(
                "daem0nmcp.api.v7.production.V7Surface.build_server",
                new=build_server,
            ),
            patch(
                "daem0nmcp.api.v7.production.importlib.util.find_spec",
                return_value=None,
            ),
        ):
            built = create_v7_server(
                "stdio",
                settings=self.settings,
                environ={},
            )

        self.assertIs(sentinel, built)
        self.assertEqual(1, len(observed))
        self.assertIsNone(observed[0]["auth"])
        self.assertFalse(observed[0]["tasks_enabled"])
        self.assertEqual(15, observed[0]["sync_timeout_seconds"])
        self.assertTrue(callable(observed[0]["lifespan"]))

    def test_server_lifespan_closes_every_owned_runtime_service(self) -> None:
        from daem0nmcp.api.v7.production import create_v7_server

        closed: list[str] = []

        class Service:
            def __init__(self, name: str) -> None:
                self.name = name

            def close(self) -> None:
                closed.append(self.name)

            async def retrieve(self, *_args, **_kwargs):
                return None

        writer = Service("writer")
        recall = Service("recall")
        observed: list[dict[str, object]] = []

        def build_server(_surface, **options):
            observed.append(options)
            return SimpleNamespace()

        with (
            patch(
                "daem0nmcp.api.v7.production.SQLiteMemoryEventWriter",
                return_value=writer,
            ),
            patch(
                "daem0nmcp.api.v7.production.Task8RecallService",
                return_value=recall,
            ),
            patch(
                "daem0nmcp.api.v7.production.V7Surface.build_server",
                new=build_server,
            ),
            patch(
                "daem0nmcp.api.v7.production.importlib.util.find_spec",
                return_value=None,
            ),
        ):
            server = create_v7_server(
                "stdio",
                settings=self.settings,
                environ={},
            )

        lifespan = observed[0]["lifespan"]

        async def exercise() -> None:
            async with lifespan(server):
                self.assertEqual(closed, [])

        asyncio.run(exercise())
        self.assertEqual(closed, ["recall", "writer"])

    def test_unknown_transport_and_unsafe_task_backend_is_disabled(self) -> None:
        from daem0nmcp.api.v7.production import (
            build_production_surface,
            create_v7_server,
        )

        with self.assertRaises(ValueError):
            build_production_surface(
                "sse",
                settings=self.settings,
                environ={},
            )

        observed: list[bool] = []

        def build_server(_surface, **options):
            observed.append(bool(options["tasks_enabled"]))
            return object()

        with (
            patch(
                "daem0nmcp.api.v7.production.importlib.util.find_spec",
                return_value=object(),
            ),
            patch(
                "daem0nmcp.api.v7.production.V7Surface.build_server",
                new=build_server,
            ),
        ):
            create_v7_server(
                "stdio",
                settings=self.settings,
                environ={},
            )
        self.assertEqual([False], observed)


if __name__ == "__main__":
    unittest.main()
