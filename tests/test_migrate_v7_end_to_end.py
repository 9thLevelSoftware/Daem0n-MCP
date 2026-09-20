"""A migrated v6 workspace must be usable: brief, recall, outcome, export.

The fixture is built the way v6 built its own database -- the retained v6 ORM
tables plus the real migration ledger capped at ``_LAST_V6_SCHEMA_VERSION`` --
and seeded with rows shaped exactly as v6 ``remember`` wrote them, including
the host-absolute ``file_path``, the ``../outside`` relative form v6 writes for
a file above the project, unbounded tags and empty content.  The column
layout was compared against a database produced by running the v6 code at
commit 00809c6; it matches column for column.
"""

from __future__ import annotations

import json
import shutil
import sqlite3
import sys
from pathlib import Path
from typing import Any

import pytest
from fastmcp import Client
from sqlalchemy import create_engine

from daem0nmcp import models
from daem0nmcp.api.v7.production import create_v7_server
from daem0nmcp.config import Settings
from daem0nmcp.migrations.schema import _LAST_V6_SCHEMA_VERSION, run_migrations
from daem0nmcp.migrations.v7 import MigrationV7Error, MigrationV7Service
from daem0nmcp.storage_activation import resolve_active_database
from daem0nmcp.workspace import (
    WorkspaceRegistry,
    is_workspace_relative_path,
    relative_to_root,
)
from tests.api_v7.process_client import call, process_client

V6_TABLES = (
    "memories",
    "memory_versions",
    "facts",
    "memory_relationships",
    "rules",
    "context_triggers",
    "active_context",
)
PROFILE_ENVIRONMENT = {"DAEM0NMCP_PROFILE": "core"}
TIME = "2026-01-01 00:00:00"


def _v6_file_paths(file_path: Path, project_path: Path) -> tuple[str, str]:
    """Reproduce v6 ``_normalize_file_path`` (v6 memory.py:63-106) exactly."""

    resolved = file_path.resolve()
    absolute = str(resolved)
    root = project_path.resolve()
    try:
        relative = resolved.relative_to(root).as_posix()
    except ValueError:
        import os.path

        relative = os.path.relpath(resolved, start=root).replace("\\", "/")
    if sys.platform == "win32":
        absolute = absolute.lower()
        relative = relative.lower()
    return absolute, relative


def _v6_database(root: Path, *, project_path: Path | None = None) -> Path:
    """Build and seed a format-6 store below ``root`` and return its storage."""

    project = project_path or root
    storage = root / ".daem0nmcp" / "storage"
    storage.mkdir(parents=True)
    database = storage / "daem0nmcp.db"
    engine = create_engine(f"sqlite:///{database}")
    models.Base.metadata.create_all(
        engine, tables=[models.Base.metadata.tables[name] for name in V6_TABLES]
    )
    engine.dispose()
    run_migrations(str(database), maximum_version=_LAST_V6_SCHEMA_VERSION)

    inside_absolute, inside_relative = _v6_file_paths(
        project / "src" / "db.py", project
    )
    route_absolute, route_relative = _v6_file_paths(project / "src" / "app.py", project)
    outside_absolute, outside_relative = _v6_file_paths(
        project.parent / "other" / "helper.py", project
    )
    memories = (
        (
            1,
            "decision",
            "Use postgres for the analytics warehouse",
            "SQLite could not keep up with the nightly rollups",
            '{"ticket": "ANA-12"}',
            '["database", "postgres"]',
            inside_absolute,
            inside_relative,
            0,
            0,
        ),
        (
            2,
            "warning",
            "Keep the /health endpoint unauthenticated; crash was in C:\\proj\\app.py",
            "Load balancer probes it",
            "{}",
            '["http"]',
            route_absolute,
            route_relative,
            1,
            0,
        ),
        (
            3,
            "pattern",
            "The shared helper lives outside the project",
            None,
            "{}",
            '["shared"]',
            outside_absolute,
            outside_relative,
            1,
            0,
        ),
        (
            4,
            "learning",
            "Migrated rows must stay readable",
            None,
            "{}",
            json.dumps(
                ["x" * 120, "dup", "dup", "ctrl\x01char", *[f"t{n}" for n in range(40)]]
            ),
            None,
            None,
            0,
            1,
        ),
        (5, "learning", "", None, "{}", "[]", None, None, 0, 0),
    )
    connection = sqlite3.connect(database)
    try:
        for row in memories:
            connection.execute(
                "INSERT INTO memories (id,category,content,rationale,context,tags,"
                "file_path,file_path_relative,keywords,is_permanent,vector_embedding,"
                "outcome,worked,pinned,archived,recall_count,surprise_score,"
                "importance_score,source_client,source_model,created_at,updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,'kw',?,NULL,NULL,NULL,?,0,0,NULL,NULL,"
                "'claude-code','sonnet',?,?)",
                (*row, TIME, TIME),
            )
        connection.execute(
            "UPDATE memories SET outcome='Cut the rollup to 4 minutes',worked=1 "
            "WHERE id=1"
        )
        connection.execute(
            "INSERT INTO memory_versions (id,memory_id,version_number,content,"
            "rationale,context,tags,outcome,worked,change_type,change_description,"
            "changed_at,valid_from,valid_to,invalidated_by_version_id) "
            "VALUES (1,1,1,'Use postgres',NULL,'{}','[\"database\"]',NULL,NULL,"
            "'created','initial',?,?,NULL,NULL)",
            (TIME, TIME),
        )
        connection.execute(
            "INSERT INTO facts (id,content_hash,content,category,source_memory_id,"
            "verification_count,is_verified,tags,created_at,verified_at) "
            "VALUES (1,'legacy-hash','Postgres is remote','database',1,2,1,"
            "'[\"db\"]',?,NULL)",
            (TIME,),
        )
        connection.execute(
            "INSERT INTO memory_relationships (id,source_id,target_id,relationship,"
            "description,confidence,created_at) VALUES "
            "(1,1,2,'related_to','both touch the API tier',1.0,?)",
            (TIME,),
        )
        connection.execute(
            "INSERT INTO rules (id,trigger,trigger_keywords,must_do,must_not,"
            "ask_first,warnings,priority,enabled,created_at) VALUES "
            "(1,'before editing the schema','schema','[\"write a migration\"]',"
            "'[\"drop tables\"]','[]','[]',5,1,?)",
            (TIME,),
        )
        connection.execute(
            "INSERT INTO context_triggers (id,project_path,trigger_type,pattern,"
            "recall_topic,recall_categories,is_active,priority,created_at,"
            "trigger_count,last_triggered) VALUES "
            "(1,?,'file_pattern','src/*.py','source conventions',"
            "'[\"decision\"]',1,3,?,0,NULL)",
            (str(project), TIME),
        )
        connection.execute(
            "INSERT INTO active_context (id,project_path,memory_id,priority,reason,"
            "added_at,expires_at) VALUES (1,?,1,4,'migration focus',?,NULL)",
            (str(project), TIME),
        )
        connection.commit()
    finally:
        connection.close()
    return storage


def _registry(root: Path) -> WorkspaceRegistry:
    return WorkspaceRegistry([root], default_root=root)


def _workspace_id(root: Path) -> str:
    return _registry(root).default.workspace_id


def _server(root: Path):
    return create_v7_server(
        "stdio",
        settings=Settings(
            project_root=str(root),
            workspace_roots=[str(root)],
            dream_enabled=False,
        ),
        environ=PROFILE_ENVIRONMENT,
    )


def _records(storage: Path) -> list[sqlite3.Row]:
    connection = sqlite3.connect(resolve_active_database(storage).path)
    connection.row_factory = sqlite3.Row
    try:
        return connection.execute(
            "SELECT * FROM memory_records ORDER BY created_at_us,record_id"
        ).fetchall()
    finally:
        connection.close()


async def _invoke(client: Client, workspace_id: str, tool: str, **arguments: Any):
    result = await client.call_tool(
        tool, {"workspace_id": workspace_id, **arguments}, raise_on_error=False
    )
    assert result.structured_content is not None, tool
    return result.structured_content


async def _succeed(client: Client, workspace_id: str, tool: str, **arguments: Any):
    response = await _invoke(client, workspace_id, tool, **arguments)
    assert response["ok"], (tool, response["error"])
    return response["data"]


def test_v6_fixture_uses_the_real_migration_ledger(tmp_path):
    """The fixture is a chain-built v6 store, not a hand-written table set."""

    storage = _v6_database(tmp_path / "project")
    connection = sqlite3.connect(storage / "daem0nmcp.db")
    try:
        versions = [
            int(row[0])
            for row in connection.execute("SELECT version FROM schema_version")
        ]
    finally:
        connection.close()
    assert max(versions) == _LAST_V6_SCHEMA_VERSION
    assert set(versions) >= {
        version
        for version, _, _ in __import__(
            "daem0nmcp.migrations.schema", fromlist=["MIGRATIONS"]
        ).MIGRATIONS
        if version <= _LAST_V6_SCHEMA_VERSION
    }


@pytest.mark.parametrize("transport", ["stdio"])
async def test_migrated_v6_workspace_is_usable_end_to_end(tmp_path, transport):
    """F-003/F-004/F-007/F-008/F-053: migrate, brief, recall, outcome, export."""

    root = tmp_path / "project"
    (root / "src").mkdir(parents=True)
    (tmp_path / "other").mkdir()
    storage = _v6_database(root)

    result = MigrationV7Service(_registry(root)).apply(root)
    assert result.status == "activated", result
    assert result.validation["rule_count"] == 1
    assert result.validation["trigger_count"] == 1
    # The one memory whose file lives above the project cannot keep a
    # workspace-relative link, and the migration says so instead of writing a
    # path that later fails every read.
    assert result.validation["dropped_file_link_count"] == 1
    assert any("file link" in warning for warning in result.warnings)

    rows = {row["content"]: row for row in _records(storage)}
    assert all(row["file_path"] is None for row in rows.values())
    assert rows["Use postgres for the analytics warehouse"]["file_path_relative"] == (
        "src/db.py"
    )
    # The helper lives above the project: no workspace-relative link exists.
    assert (
        rows["The shared helper lives outside the project"]["file_path_relative"]
        is None
    )
    # v6 tags had no length, uniqueness or count limit; the v7 wire has all three.
    tags = json.loads(rows["Migrated rows must stay readable"]["tags_json"])
    assert len(tags) == 32 and len(set(tags)) == 32
    assert all(len(tag) <= 80 for tag in tags)
    assert "<empty>" in rows, sorted(rows)

    workspace_id = _workspace_id(root)
    async with process_client(root, transport) as session:
        brief = await call(session, "session_brief", {"workspace_id": workspace_id})
        assert brief["ok"], brief["error"]
        assert brief["data"]["applicable_rules"], "the migrated v6 rule must brief"

        recall = await call(
            session,
            "memory_recall",
            {"workspace_id": workspace_id, "query": "postgres"},
        )
        assert recall["ok"], recall["error"]
        linked = [
            item["record"]
            for item in recall["data"]["items"]
            if item["record"]["relative_file_path"] == "src/db.py"
        ]
        assert linked, recall["data"]["items"]

        outcome = await call(
            session,
            "memory_record_outcome",
            {
                "workspace_id": workspace_id,
                "record_id": linked[0]["record_id"],
                "outcome_text": "still true after the migration",
                "worked": True,
                "idempotency_key": "migrated-outcome-0001",
            },
        )
        assert outcome["ok"], outcome["error"]

        pages: list[dict[str, Any]] = []
        arguments: dict[str, Any] = {}
        while len(pages) <= 32:
            page = await call(
                session,
                "workspace_export",
                {"workspace_id": workspace_id, **arguments},
            )
            assert page["ok"], page["error"]
            pages.append(page["data"])
            if page["data"]["complete"] or page["data"]["next_cursor"] is None:
                break
            arguments = {
                "export_session_id": page["data"]["export_session_id"],
                "cursor": page["data"]["next_cursor"],
                "page_index": page["data"]["page_index"] + 1,
            }
        assert pages and pages[-1]["complete"]
        # Exporting at all proves no host path reached a structured path field
        # (the guard refuses the whole page); assert the surviving links too.
        exported = [row for page in pages for row in page["legacy_rows"]]
        assert exported
        for row in exported:
            relative = row["record"]["file_path_relative"]
            assert relative is None or is_workspace_relative_path(relative), relative
            assert "file_path" not in row["record"]


async def test_every_migrated_row_reads_back_through_the_server(tmp_path):
    """A migrated row cannot deny a read, whatever v6 put in it."""

    root = tmp_path / "project"
    (root / "src").mkdir(parents=True)
    (tmp_path / "other").mkdir()
    _v6_database(root)
    MigrationV7Service(_registry(root)).apply(root)
    workspace_id = _workspace_id(root)

    async with Client(_server(root)) as client:
        await _succeed(client, workspace_id, "session_brief")
        for query in ("postgres", "health", "readable", "helper"):
            await _succeed(client, workspace_id, "memory_recall", query=query)
            await _succeed(client, workspace_id, "memory_search_text", query=query)
        for kind in ("warnings", "failures", "rules", "active-context"):
            await client.read_resource(f"memory://workspaces/{workspace_id}/{kind}")


async def test_a_database_migrated_by_the_pre_fix_code_still_reads(tmp_path):
    """The values the previous migration wrote must not brick a read."""

    root = tmp_path / "project"
    (root / "src").mkdir(parents=True)
    (tmp_path / "other").mkdir()
    storage = _v6_database(root)
    MigrationV7Service(_registry(root)).apply(root)
    workspace_id = _workspace_id(root)

    # Put the projection back into the shape the pre-fix migration left: the
    # host-absolute file_path, v6's ``../`` relative form, v6's unbounded tags
    # and empty content.  No re-migration is offered for such a store.
    absolute, relative = _v6_file_paths(tmp_path / "other" / "helper.py", root)
    connection = sqlite3.connect(resolve_active_database(storage).path)
    try:
        connection.execute(
            "UPDATE memory_records SET file_path=?,file_path_relative=?,tags_json=?,"
            "content=? WHERE record_type='pattern'",
            (
                absolute,
                relative,
                json.dumps(["y" * 120, "same", "same", *[f"u{n}" for n in range(40)]]),
                "",
            ),
        )
        connection.commit()
    finally:
        connection.close()

    async with Client(_server(root)) as client:
        brief = await _succeed(client, workspace_id, "session_brief")
        assert isinstance(brief["warnings"], list)
        recall = await _succeed(client, workspace_id, "memory_recall", query="helper")
        for item in recall["items"]:
            assert item["record"]["relative_file_path"] != relative
        for kind in ("warnings", "failures", "rules", "active-context"):
            await client.read_resource(f"memory://workspaces/{workspace_id}/{kind}")


async def test_a_moved_project_keeps_its_file_links(tmp_path):
    """F-022: v6 recorded one project root; file links resolve against it."""

    old_root = tmp_path / "old-name"
    (old_root / "src").mkdir(parents=True)
    new_root = tmp_path / "new-name"
    (tmp_path / "other").mkdir()
    storage = _v6_database(old_root, project_path=old_root)
    # v6 kept the database inside the project, so moving the directory moves
    # the store with it while every recorded path still names the old root.
    shutil.move(str(old_root), str(new_root))
    storage = new_root / ".daem0nmcp" / "storage"

    result = MigrationV7Service(_registry(new_root)).apply(new_root)
    assert result.status == "activated", result
    # The single recorded v6 project path is this workspace's own history.
    assert result.validation["trigger_count"] == 1
    assert result.validation["foreign_trigger_count"] == 0
    rows = {row["record_type"]: row for row in _records(storage)}
    assert rows["decision"]["file_path_relative"] == "src/db.py"

    workspace_id = _workspace_id(new_root)
    async with Client(_server(new_root)) as client:
        await _succeed(client, workspace_id, "session_brief")
        await _succeed(client, workspace_id, "memory_recall", query="postgres")


def test_a_sibling_directory_is_not_inside_the_workspace(tmp_path):
    """R-25: containment is decided by path parts, never a string prefix."""

    inside = tmp_path / "proj"
    sibling = tmp_path / "proj2"
    (inside / "src").mkdir(parents=True)
    sibling.mkdir()
    (inside / "src" / "a.py").write_text("", encoding="utf-8")
    (sibling / "a.py").write_text("", encoding="utf-8")

    assert relative_to_root(inside / "src" / "a.py", inside) == "src/a.py"
    assert relative_to_root(sibling / "a.py", inside) is None
    assert relative_to_root(inside, inside) is None
    assert relative_to_root(tmp_path / "proj" / ".." / "proj2" / "a.py", inside) is None


async def test_rollback_refuses_to_hide_writes_made_since_activation(tmp_path):
    """F-024/F-068: rollback is loud about the v7 events it would strand."""

    root = tmp_path / "project"
    (root / "src").mkdir(parents=True)
    (tmp_path / "other").mkdir()
    storage = _v6_database(root)
    source_bytes = (storage / "daem0nmcp.db").read_bytes()
    service = MigrationV7Service(_registry(root))
    service.apply(root)
    workspace_id = _workspace_id(root)

    async with Client(_server(root)) as client:
        await _succeed(client, workspace_id, "session_brief")
        preflight = await _succeed(
            client,
            workspace_id,
            "memory_preflight",
            target_tool="memory_store",
            target_arguments={
                "record_type": "decision",
                "content": "written after the migration",
                "idempotency_key": "post-migration-0001",
            },
            description="write after migrating",
        )
        stored = await _succeed(
            client,
            workspace_id,
            "memory_store",
            record_type="decision",
            content="written after the migration",
            idempotency_key="post-migration-0001",
            preflight_token=preflight["preflight_token"],
        )
        assert stored["record"]["record_id"]

    with pytest.raises(MigrationV7Error) as refusal:
        service.rollback(root)
    assert refusal.value.code == "ROLLBACK_WOULD_HIDE_WRITES"
    assert resolve_active_database(storage).format_version == 7

    result = service.rollback(root, discard_v7_writes=True)
    assert result.status == "rolled_back"
    assert result.warnings and "unreachable" not in result.warnings[0]
    assert resolve_active_database(storage).format_version == 6
    assert (storage / "daem0nmcp.db").read_bytes() == source_bytes


async def test_an_un_migrated_v6_workspace_says_migration_is_required(tmp_path):
    """F-066/F-069/F-043: a non-retryable error naming the migrate command."""

    root = tmp_path / "project"
    (root / "src").mkdir(parents=True)
    (tmp_path / "other").mkdir()
    _v6_database(root)
    workspace_id = _workspace_id(root)

    async with Client(_server(root)) as client:
        response = await _invoke(client, workspace_id, "session_brief")
    assert response["ok"] is False
    assert response["error"]["code"] == "MIGRATION_REQUIRED"
    assert response["error"]["retryable"] is False
    assert "migrate-v7 --apply" in response["error"]["message"]


def test_a_v6_rule_edited_after_an_in_place_upgrade_reaches_v7(tmp_path):
    """F-023: a `must_not` added after an in-place upgrade must not vanish."""

    root = tmp_path / "project"
    (root / "src").mkdir(parents=True)
    (tmp_path / "other").mkdir()
    storage = _v6_database(root)
    database = storage / "daem0nmcp.db"
    # The pre-fix legacy CLI applied the whole ledger to the v6 file, which
    # backfilled governance from the rules table as it stood then.
    run_migrations(str(database), workspace_id=_workspace_id(root))
    connection = sqlite3.connect(database)
    try:
        connection.execute(
            "UPDATE rules SET must_not=? WHERE id=1",
            (json.dumps(["drop tables", "truncate"]),),
        )
        connection.execute("DELETE FROM context_triggers WHERE id=1")
        connection.commit()
    finally:
        connection.close()

    result = MigrationV7Service(_registry(root)).apply(root)
    assert result.status == "activated", result
    assert result.validation["rules_updated"] == 1
    assert result.validation["triggers_deleted"] == 1

    connection = sqlite3.connect(resolve_active_database(storage).path)
    try:
        must_not = connection.execute(
            "SELECT must_not_json FROM governance_rules"
        ).fetchone()[0]
        deleted = connection.execute(
            "SELECT count(*) FROM governance_context_triggers "
            "WHERE deleted_at_us IS NOT NULL"
        ).fetchone()[0]
    finally:
        connection.close()
    assert json.loads(must_not) == ["drop tables", "truncate"]
    assert deleted == 1


def test_a_foreign_project_trigger_is_skipped_and_counted(tmp_path):
    """F-022: triggers from other project roots are reported, not dropped."""

    root = tmp_path / "project"
    (root / "src").mkdir(parents=True)
    (tmp_path / "other").mkdir()
    storage = _v6_database(root)
    connection = sqlite3.connect(storage / "daem0nmcp.db")
    try:
        connection.execute(
            "INSERT INTO context_triggers (id,project_path,trigger_type,pattern,"
            "recall_topic,recall_categories,is_active,priority,created_at,"
            "trigger_count,last_triggered) VALUES "
            "(2,?,'tag_match','auth.*','authentication','[\"warning\"]',1,1,?,0,NULL)",
            (str(tmp_path / "another-project"), TIME),
        )
        connection.commit()
    finally:
        connection.close()

    result = MigrationV7Service(_registry(root)).apply(root)
    assert result.validation["trigger_count"] == 1
    assert result.validation["foreign_trigger_count"] == 1
    assert any("another project path" in warning for warning in result.warnings)


async def test_export_pages_come_from_one_snapshot(tmp_path):
    """Writes between pages cannot change a page already in the session."""

    root = tmp_path / "project"
    (root / "src").mkdir(parents=True)
    (tmp_path / "other").mkdir()
    _v6_database(root)
    MigrationV7Service(_registry(root)).apply(root)
    workspace_id = _workspace_id(root)

    async with Client(_server(root)) as client:
        await _succeed(client, workspace_id, "session_brief")
        first = await _succeed(
            client, workspace_id, "workspace_export", page_byte_limit=65_536
        )
        assert first["page_count"] >= 2, "the fixture must page"
        preflight = await _succeed(
            client,
            workspace_id,
            "memory_preflight",
            target_tool="memory_store",
            target_arguments={
                "record_type": "decision",
                "content": "written between export pages",
                "idempotency_key": "between-pages-0001",
            },
            description="write between export pages",
        )
        await _succeed(
            client,
            workspace_id,
            "memory_store",
            record_type="decision",
            content="written between export pages",
            idempotency_key="between-pages-0001",
            preflight_token=preflight["preflight_token"],
        )
        page = first
        collected = [first]
        while not page["complete"] and page["next_cursor"] is not None:
            page = await _succeed(
                client,
                workspace_id,
                "workspace_export",
                export_session_id=page["export_session_id"],
                cursor=page["next_cursor"],
                page_index=page["page_index"] + 1,
            )
            collected.append(page)
        assert page["complete"]
        # Every page belongs to the session's manifest, taken before the write.
        assert {item["manifest_hash"] for item in collected} == {first["manifest_hash"]}
        assert all(item["root_hash"] == first["root_hash"] for item in collected), (
            "a later page must not observe an event written after the session"
        )


def test_cli_status_leaves_a_v6_database_at_its_last_v6_version(tmp_path):
    """F-023/A-005: a legacy command must not upgrade the migration source."""

    import subprocess

    root = tmp_path / "project"
    (root / "src").mkdir(parents=True)
    (tmp_path / "other").mkdir()
    storage = _v6_database(root)
    database = storage / "daem0nmcp.db"

    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "daem0nmcp.cli",
            "--project-path",
            str(root),
            "--json",
            "status",
        ],
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert completed.returncode == 0, completed.stderr[-2000:]
    connection = sqlite3.connect(database)
    try:
        version = connection.execute(
            "SELECT MAX(version) FROM schema_version"
        ).fetchone()[0]
    finally:
        connection.close()
    assert version == _LAST_V6_SCHEMA_VERSION
    assert resolve_active_database(storage).format_version == 6
    # The source is still migratable afterwards.
    assert MigrationV7Service(_registry(root)).apply(root).status == "activated"
