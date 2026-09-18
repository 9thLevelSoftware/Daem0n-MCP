from __future__ import annotations

import json
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from pydantic import ValidationError

from daem0nmcp.api.v7.models import RecordSummary
from daem0nmcp.workspace import Workspace

WORKSPACE_ID = "ws_0123456789abcdef01234567"
OTHER_WORKSPACE_ID = "ws_fedcba9876543210fedcba98"
NOW = datetime(2026, 8, 8, 12, 0, tzinfo=timezone.utc)


def _record(
    index: int,
    *,
    record_type: str = "warning",
    status: str = "current",
    updated_at: datetime | None = None,
) -> RecordSummary:
    updated = updated_at or NOW + timedelta(minutes=index)
    return RecordSummary(
        record_id=f"mem_{index:064x}",
        record_type=record_type,
        excerpt=f"bounded record {index}",
        tags=["resource"],
        relative_file_path="src/example.py",
        current_status=status,
        content_hash=f"{index + 1000:064x}",
        created_at=NOW - timedelta(days=1),
        updated_at=updated,
    )


class _Resolver:
    def __init__(self, events: list[tuple[object, ...]], *, fail: bool = False):
        self.events = events
        self.fail = fail

    def resolve(self, workspace_id: str) -> Workspace:
        self.events.append(("resolve", workspace_id))
        if self.fail:
            raise LookupError(r"unknown D:\private\project")
        return Workspace(workspace_id=workspace_id, root=Path(r"D:\private\project"))


class _Authorizer:
    def __init__(self, events: list[tuple[object, ...]], *, fail: bool = False):
        self.events = events
        self.fail = fail

    async def authorize(self, *, workspace: Workspace, resource_uri: str) -> None:
        self.events.append(("authorize", workspace.workspace_id, resource_uri))
        if self.fail:
            raise PermissionError("principal alice cannot access secret workspace")


class _Reader:
    def __init__(
        self,
        events: list[tuple[object, ...]],
        rows: list[object] | None = None,
        *,
        fail: bool = False,
    ) -> None:
        self.events = events
        self.rows = rows or []
        self.fail = fail

    async def __call__(self, workspace: Workspace, request: object) -> list[object]:
        self.events.append(
            (
                "read",
                workspace.workspace_id,
                request.kind,
                request.limit,
                request.order_by,
                request.include_archived,
                request.include_deleted,
                request.include_expired,
                request.enabled_only,
            )
        )
        if self.fail:
            raise RuntimeError(r"sqlite failed at D:\private\project\db.sqlite")
        return self.rows


def _dependencies(
    *,
    events: list[tuple[object, ...]],
    warning_rows: list[object] | None = None,
    failure_rows: list[object] | None = None,
    rule_rows: list[object] | None = None,
    active_rows: list[object] | None = None,
    resolver: _Resolver | None = None,
    authorizer: _Authorizer | None = None,
    warning_reader: _Reader | None = None,
):
    from daem0nmcp.api.v7.resources import ResourceDependencies

    return ResourceDependencies(
        workspace_resolver=resolver or _Resolver(events),
        communion_authorizer=authorizer or _Authorizer(events),
        warning_reader=warning_reader or _Reader(events, warning_rows),
        failure_reader=_Reader(events, failure_rows),
        rule_reader=_Reader(events, rule_rows),
        active_context_reader=_Reader(events, active_rows),
        clock=lambda: NOW,
    )


class ResourceModelTests(unittest.TestCase):
    def test_documents_are_strict_json_objects_bounded_to_fifty_items(self) -> None:
        from daem0nmcp.api.v7.resources import (
            ActiveContextItem,
            RuleView,
            WarningResourceDocument,
        )

        schema = WarningResourceDocument.model_json_schema()
        self.assertEqual(schema["type"], "object")
        self.assertFalse(schema["additionalProperties"])
        self.assertEqual(schema["properties"]["items"]["maxItems"], 50)

        with self.assertRaises(ValidationError):
            WarningResourceDocument(
                workspace_id=WORKSPACE_ID,
                generated_at=NOW,
                items=[_record(index) for index in range(1, 52)],
                truncated=True,
            )
        with self.assertRaises(ValidationError):
            RuleView(
                rule_id=1,
                trigger="when changing storage",
                must_do=[],
                must_not=[],
                ask_first=[],
                warnings=[],
                priority=1,
                enabled=True,
                created_at=NOW,
            )
        with self.assertRaises(ValidationError):
            ActiveContextItem(
                active_context_id=f"act_{1:064x}",
                record=_record(1),
                priority=0,
                reason=None,
                added_at=NOW,
                expires_at=None,
                project_path=r"D:\private\project",
            )

    def test_manifest_specs_include_four_json_templates_and_six_static_shells(
        self,
    ) -> None:
        from daem0nmcp.api.v7.resources import (
            RESOURCE_URI_TEMPLATES,
            ResourceHandlers,
            build_resource_specs,
        )

        handlers = ResourceHandlers(_dependencies(events=[]))
        specs = build_resource_specs(handlers)

        expected = {
            f"memory://workspaces/{{workspace_id}}/{suffix}"
            for suffix in ("warnings", "failures", "rules", "active-context")
        }
        self.assertEqual(set(RESOURCE_URI_TEMPLATES), expected)
        workspace_specs = tuple(spec for spec in specs if spec.requires_workspace)
        dashboard_specs = tuple(spec for spec in specs if not spec.requires_workspace)
        self.assertEqual({spec.uri_template for spec in workspace_specs}, expected)
        self.assertEqual(len(workspace_specs), 4)
        self.assertTrue(
            all(spec.mime_type == "application/json" for spec in workspace_specs)
        )
        self.assertEqual(
            {spec.uri_template for spec in dashboard_specs},
            {
                "ui://daem0n/test",
                "ui://daem0n/search",
                "ui://daem0n/briefing",
                "ui://daem0n/covenant",
                "ui://daem0n/community",
                "ui://daem0n/graph",
            },
        )
        self.assertTrue(
            all(
                spec.mime_type == "text/html;profile=mcp-app"
                for spec in dashboard_specs
            )
        )
        self.assertTrue(all(spec.output_model is None for spec in dashboard_specs))
        self.assertTrue(all(spec.version == "7" for spec in specs))
        self.assertEqual(
            {spec.name for spec in workspace_specs},
            {
                "workspace_warnings",
                "workspace_failures",
                "workspace_rules",
                "workspace_active_context",
            },
        )


class ResourceHandlerTests(unittest.IsolatedAsyncioTestCase):
    async def test_warnings_resolve_authorize_then_read_and_return_newest_active(
        self,
    ) -> None:
        from daem0nmcp.api.v7.resources import ResourceHandlers, ResourceRow

        events: list[tuple[object, ...]] = []
        eligible = [_record(index) for index in range(1, 53)]
        rows = [
            *eligible,
            ResourceRow(item=_record(90), deleted=True),
            _record(91, status="archived"),
        ]
        handlers = ResourceHandlers(_dependencies(events=events, warning_rows=rows))

        document = await handlers.warnings(WORKSPACE_ID)

        self.assertEqual(
            events[:3],
            [
                ("resolve", WORKSPACE_ID),
                (
                    "authorize",
                    WORKSPACE_ID,
                    f"memory://workspaces/{WORKSPACE_ID}/warnings",
                ),
                (
                    "read",
                    WORKSPACE_ID,
                    "warnings",
                    51,
                    "updated_at_desc",
                    False,
                    False,
                    False,
                    None,
                ),
            ],
        )
        self.assertEqual(len(document.items), 50)
        self.assertTrue(document.truncated)
        self.assertEqual(document.items[0].record_id, f"mem_{52:064x}")
        self.assertEqual(document.items[-1].record_id, f"mem_{3:064x}")
        self.assertTrue(
            all(item.current_status != "archived" for item in document.items)
        )
        wire = document.model_dump_json()
        self.assertNotIn("private", wire)
        self.assertEqual(
            set(json.loads(wire)),
            {"api_version", "workspace_id", "generated_at", "items", "truncated"},
        )

    async def test_failures_use_their_own_exact_authorization_scope(self) -> None:
        from daem0nmcp.api.v7.resources import ResourceHandlers

        events: list[tuple[object, ...]] = []
        handlers = ResourceHandlers(
            _dependencies(
                events=events,
                failure_rows=[_record(1, record_type="decision")],
            )
        )

        document = await handlers.failures(WORKSPACE_ID)

        self.assertEqual(len(document.items), 1)
        self.assertEqual(
            events[1],
            (
                "authorize",
                WORKSPACE_ID,
                f"memory://workspaces/{WORKSPACE_ID}/failures",
            ),
        )
        self.assertEqual(events[2][2:5], ("failures", 51, "updated_at_desc"))

    async def test_rules_and_active_context_return_only_highest_priority_active_items(
        self,
    ) -> None:
        from daem0nmcp.api.v7.resources import (
            ActiveContextItem,
            ResourceHandlers,
            ResourceRow,
            RuleView,
        )

        def rule(index: int, priority: int, *, enabled: bool = True) -> RuleView:
            return RuleView(
                rule_id=f"rule_{index:064x}",
                trigger=f"rule trigger {index}",
                must_do=[f"do {index}"],
                must_not=[],
                ask_first=[],
                warnings=[],
                priority=priority,
                enabled=enabled,
                created_at=NOW + timedelta(minutes=index),
            )

        def active(
            index: int,
            priority: int,
            *,
            status: str = "current",
            expires_at: datetime | None = None,
        ) -> ActiveContextItem:
            return ActiveContextItem(
                active_context_id=f"act_{index:064x}",
                record=_record(index, status=status),
                priority=priority,
                reason=f"reason {index}",
                added_at=NOW + timedelta(minutes=index),
                expires_at=expires_at,
            )

        events: list[tuple[object, ...]] = []
        handlers = ResourceHandlers(
            _dependencies(
                events=events,
                rule_rows=[
                    rule(1, 1),
                    rule(2, 9),
                    rule(3, 100, enabled=False),
                    ResourceRow(item=rule(4, 200), deleted=True),
                ],
                active_rows=[
                    active(1, 1),
                    active(2, 9),
                    active(3, 80, status="archived"),
                    active(4, 90, expires_at=NOW),
                    ResourceRow(item=active(5, 100), deleted=True),
                ],
            )
        )

        rules = await handlers.rules(WORKSPACE_ID)
        active_context = await handlers.active_context(WORKSPACE_ID)

        self.assertEqual(
            [item.rule_id for item in rules.items],
            [f"rule_{2:064x}", f"rule_{1:064x}"],
        )
        self.assertEqual(
            [item.active_context_id for item in active_context.items],
            [f"act_{2:064x}", f"act_{1:064x}"],
        )
        rule_read = next(
            event for event in events if event[:3] == ("read", WORKSPACE_ID, "rules")
        )
        active_read = next(
            event
            for event in events
            if event[:3] == ("read", WORKSPACE_ID, "active_context")
        )
        self.assertEqual(rule_read[4:], ("priority_desc", False, False, False, True))
        self.assertEqual(active_read[4:], ("priority_desc", False, False, False, None))

    async def test_all_failures_collapse_to_one_path_free_resource_error(self) -> None:
        from daem0nmcp.api.v7.resources import (
            ResourceAccessError,
            ResourceHandlers,
        )

        errors: list[ResourceAccessError] = []

        unknown_events: list[tuple[object, ...]] = []
        unknown = ResourceHandlers(
            _dependencies(
                events=unknown_events,
                resolver=_Resolver(unknown_events, fail=True),
            )
        )
        with self.assertRaises(ResourceAccessError) as caught:
            await unknown.warnings(OTHER_WORKSPACE_ID)
        errors.append(caught.exception)
        self.assertEqual(unknown_events, [("resolve", OTHER_WORKSPACE_ID)])

        unauthorized_events: list[tuple[object, ...]] = []
        unauthorized = ResourceHandlers(
            _dependencies(
                events=unauthorized_events,
                authorizer=_Authorizer(unauthorized_events, fail=True),
            )
        )
        with self.assertRaises(ResourceAccessError) as caught:
            await unauthorized.warnings(WORKSPACE_ID)
        errors.append(caught.exception)
        self.assertEqual(
            [event[0] for event in unauthorized_events], ["resolve", "authorize"]
        )

        failed_read_events: list[tuple[object, ...]] = []
        failed_read = ResourceHandlers(
            _dependencies(
                events=failed_read_events,
                warning_reader=_Reader(failed_read_events, fail=True),
            )
        )
        with self.assertRaises(ResourceAccessError) as caught:
            await failed_read.warnings(WORKSPACE_ID)
        errors.append(caught.exception)
        self.assertEqual(
            [event[0] for event in failed_read_events],
            ["resolve", "authorize", "read"],
        )

        signatures = {
            (type(error), error.code, error.args, str(error)) for error in errors
        }
        self.assertEqual(len(signatures), 1)
        for error in errors:
            self.assertIsNone(error.__cause__)
            self.assertIsNone(error.__context__)
        rendered = " ".join(str(error) + repr(error) for error in errors).lower()
        for secret in ("private", "sqlite", "alice", "lookup", "permission"):
            self.assertNotIn(secret, rendered)

    async def test_invalid_or_mismatched_workspace_stops_before_authorization(
        self,
    ) -> None:
        from daem0nmcp.api.v7.resources import ResourceAccessError, ResourceHandlers

        events: list[tuple[object, ...]] = []
        handlers = ResourceHandlers(_dependencies(events=events))
        with self.assertRaises(ResourceAccessError):
            await handlers.warnings("not-a-workspace")
        self.assertEqual(events, [])

        class _MismatchedResolver(_Resolver):
            def resolve(self, workspace_id: str) -> Workspace:
                self.events.append(("resolve", workspace_id))
                return Workspace(
                    workspace_id=OTHER_WORKSPACE_ID,
                    root=Path(r"D:\private\other"),
                )

        events = []
        handlers = ResourceHandlers(
            _dependencies(events=events, resolver=_MismatchedResolver(events))
        )
        with self.assertRaises(ResourceAccessError):
            await handlers.warnings(WORKSPACE_ID)
        self.assertEqual(events, [("resolve", WORKSPACE_ID)])

    async def test_sync_injected_dependencies_are_supported_without_framework_imports(
        self,
    ) -> None:
        from daem0nmcp.api.v7.resources import ResourceDependencies, ResourceHandlers

        events: list[tuple[object, ...]] = []

        class _SyncAuthorizer:
            def authorize(self, *, workspace: Workspace, resource_uri: str) -> None:
                events.append(("authorize", workspace.workspace_id, resource_uri))

        def sync_reader(workspace: Workspace, request: object) -> list[RecordSummary]:
            events.append(("read", workspace.workspace_id, request.kind))
            return [_record(1)]

        dependencies = ResourceDependencies(
            workspace_resolver=_Resolver(events),
            communion_authorizer=_SyncAuthorizer(),
            warning_reader=sync_reader,
            failure_reader=sync_reader,
            rule_reader=lambda _workspace, _request: [],
            active_context_reader=lambda _workspace, _request: [],
            clock=lambda: NOW,
        )

        result = await ResourceHandlers(dependencies).warnings(WORKSPACE_ID)

        self.assertEqual(len(result.items), 1)
        self.assertEqual(
            [event[0] for event in events], ["resolve", "authorize", "read"]
        )


if __name__ == "__main__":
    unittest.main()
