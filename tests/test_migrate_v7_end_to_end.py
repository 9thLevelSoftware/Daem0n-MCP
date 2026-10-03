"""A migrated v6 workspace must be usable: brief, recall, outcome, export.

The fixture is built the way v6 built its own database -- the retained v6 ORM
tables plus the real migration ledger capped at ``_LAST_V6_SCHEMA_VERSION`` --
and seeded with rows shaped exactly as v6 ``remember`` wrote them, including
the host-absolute ``file_path``, the ``../outside`` relative form v6 writes for
a file above the project, unbounded tags and empty content.

Its DDL is byte-identical to a database produced by running the v6 code at
commit 00809c6: the same twenty-three tables, and the same
``V6_SCHEMA_SHA256`` over their ``sqlite_master.sql``, which
``test_v6_fixture_uses_the_real_migration_ledger`` enforces so a v7-side edit
to a retained v6 model cannot quietly change what "a v6 database" means here.
"""

from __future__ import annotations

import hashlib
import json
import os
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
from daem0nmcp.migrations.schema import (
    _LAST_V6_SCHEMA_VERSION,
    MIGRATIONS,
    run_migrations,
)
from daem0nmcp.migrations.v7 import MigrationV7Error, MigrationV7Service
from daem0nmcp.storage_activation import resolve_active_database
from daem0nmcp.workspace import (
    WorkspaceRegistry,
    is_workspace_relative_path,
    relative_to_root,
)
from tests.api_v7.process_client import call, process_client

# Every table v6's own ``init_db`` created, so the migration's source
# inventory, its logical hash and the rollback byte-identity check run over
# the table set a real v6 store has.  The first seven carry rows.
V6_TABLES = (
    "memories",
    "memory_versions",
    "facts",
    "memory_relationships",
    "rules",
    "context_triggers",
    "active_context",
    "code_entities",
    "enforcement_bypass_log",
    "extracted_entities",
    "file_hashes",
    "memory_code_refs",
    "memory_communities",
    "memory_entity_refs",
    "project_links",
    "session_state",
)
# The v6 column layout, pinned so a v7-side edit to a retained model cannot
# quietly change what "a v6 database" means here.  Update it only when the v6
# format genuinely changes, and say so in the commit.
V6_SCHEMA_SHA256 = "07c197f506e5ba368e3ee71ef132f7bd636872fc9d89432262a20292e46b8b82"
PROFILE_ENVIRONMENT = {"DAEM0NMCP_PROFILE": "core"}
TIME = "2026-01-01 00:00:00"


def _v6_file_paths(file_path: Path, project_path: Path) -> tuple[str, str]:
    """Reproduce v6 ``_normalize_file_path`` (v6 memory.py:63-106) exactly.

    Including its inner fallback: across Windows drives ``os.path.relpath``
    raises too, and v6 then stored the resolved *absolute* path in the
    ``file_path_relative`` column.
    """

    resolved = file_path.resolve()
    absolute = str(resolved)
    root = project_path.resolve()
    try:
        relative = resolved.relative_to(root).as_posix()
    except ValueError:
        try:
            relative = os.path.relpath(resolved, start=root).replace("\\", "/")
        except ValueError:
            relative = resolved.as_posix()
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
            "INSERT INTO context_triggers (id,project_path,trigger_type,pattern,"
            "recall_topic,recall_categories,is_active,priority,created_at,"
            "trigger_count,last_triggered) VALUES "
            "(2,?,'entity_match','Postgres.*','storage entities',"
            "'[\"decision\"]',1,1,?,0,NULL)",
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


def _v6_schema_digest(database: Path) -> str:
    """A digest of the v6 tables' DDL, in a stable order."""

    connection = sqlite3.connect(database)
    try:
        statements = [
            str(row[0])
            for row in connection.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND sql IS NOT NULL "
                "ORDER BY name"
            )
        ]
    finally:
        connection.close()
    return hashlib.sha256("\n".join(statements).encode("utf-8")).hexdigest()


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
    if result.structured_content is None:
        # A schema rejection by FastMCP never reaches the v7 router.
        assert result.is_error, tool
        return {"ok": False, "error": {"code": "SCHEMA_REJECTED"}}
    return result.structured_content


async def _succeed(client: Client, workspace_id: str, tool: str, **arguments: Any):
    response = await _invoke(client, workspace_id, tool, **arguments)
    assert response["ok"], (tool, response["error"])
    return response["data"]


async def _protected(
    client: Client, workspace_id: str, tool: str, arguments: dict[str, Any]
):
    """Call a covenant-protected tool with the preflight token it requires."""

    preflight = await _succeed(
        client,
        workspace_id,
        "memory_preflight",
        target_tool=tool,
        target_arguments=arguments,
        description=f"review before {tool}",
    )
    return await _succeed(
        client,
        workspace_id,
        tool,
        preflight_token=preflight["preflight_token"],
        **arguments,
    )


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
        version for version, _, _ in MIGRATIONS if version <= _LAST_V6_SCHEMA_VERSION
    }
    # R-25: the layout itself, so a v7-side edit to a retained v6 model cannot
    # change what this fixture means without failing here.
    assert _v6_schema_digest(storage / "daem0nmcp.db") == V6_SCHEMA_SHA256


async def test_migrated_v6_workspace_is_usable_end_to_end(tmp_path):
    """F-003/F-004/F-007/F-008/F-053: migrate, brief, recall, outcome, export."""

    root = tmp_path / "project"
    (root / "src").mkdir(parents=True)
    (tmp_path / "other").mkdir()
    storage = _v6_database(root)

    result = MigrationV7Service(_registry(root)).apply(root)
    assert result.status == "activated", result
    assert result.validation["rule_count"] == 1
    # Both v6 trigger types the fixture seeds, including ``entity_match``.
    assert result.validation["trigger_count"] == 2
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
    # PR 5 removed the control-character refusal; a migrated tag keeps its.
    assert "ctrl\x01char" in tags
    assert "<empty>" in rows, sorted(rows)

    workspace_id = _workspace_id(root)
    async with process_client(root, "stdio") as session:
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
        # And the event pages, where the migrated v6 row rides as `[name,
        # value]` pairs -- the place the host path used to reach the wire.
        assert any(page["page_kind"] == "events" for page in pages)
        for page in pages:
            assert str(root) not in json.dumps(page)


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


@pytest.mark.parametrize(
    "stored_relative",
    [
        # v6 wrote this for a file above the project root...
        "../other/helper.py",
        # ...and this when `os.path.relpath` raised across Windows drives.
        "c:/other/helper.py",
        # A POSIX absolute path reaches the same column the same way.
        "/var/other/helper.py",
    ],
)
async def test_a_database_migrated_by_the_pre_fix_code_still_reads(
    tmp_path, stored_relative
):
    """PR 5 carry-over: the in-place repair, on values this test controls.

    Neither the ``file_path_relative`` normalization nor the tag/content
    clipping is reachable through a store this branch migrated -- the
    migration already clips and nulls -- so both are exercised here by
    writing the pre-fix projection back and reading it through the server.
    """

    root = tmp_path / "project"
    (root / "src").mkdir(parents=True)
    (tmp_path / "other").mkdir()
    storage = _v6_database(root)
    MigrationV7Service(_registry(root)).apply(root)
    workspace_id = _workspace_id(root)

    absolute, _relative = _v6_file_paths(tmp_path / "other" / "helper.py", root)
    overlong = "y" * 120
    tags = json.dumps([overlong, "same", "same", *[f"u{n}" for n in range(40)]])
    connection = sqlite3.connect(resolve_active_database(storage).path)
    try:
        # One warning row carrying every shape at once, so it is read back
        # through `session_brief` and the warnings resource -- the reader
        # whose failure PR 5 escalated, and one that selects by record type
        # rather than through the lexical index, so no re-indexing is
        # involved and the assertion cannot race a background rebuild.
        connection.execute(
            "UPDATE memory_records SET file_path=?,file_path_relative=?,tags_json=?,"
            "content='' WHERE record_type='warning'",
            (absolute, stored_relative, tags),
        )
        connection.commit()
        record_id = str(
            connection.execute(
                "SELECT record_id FROM memory_records WHERE record_type='warning'"
            ).fetchone()[0]
        )
    finally:
        connection.close()

    async with Client(_server(root)) as client:
        brief = await _succeed(client, workspace_id, "session_brief")
        summaries = {item["record_id"]: item for item in brief["warnings"]}
        assert record_id in summaries, brief["warnings"]
        record = summaries[record_id]
        # The stored value is not workspace-relative, so no link is emitted --
        # and above all the host path never reaches the wire.
        assert record["relative_file_path"] is None
        assert absolute not in json.dumps(brief)
        assert stored_relative not in json.dumps(brief)
        # v6 accepted empty content; the wire needs at least one character.
        assert record["excerpt"] == "<empty>"
        # v6 had no tag length, uniqueness or count limit; the wire has all three.
        assert len(record["tags"]) == 32
        assert len(set(record["tags"])) == 32
        assert all(len(tag) <= 80 for tag in record["tags"])
        assert overlong[:80] in record["tags"]

        for kind in ("warnings", "failures", "rules", "active-context"):
            document = await client.read_resource(
                f"memory://workspaces/{workspace_id}/{kind}"
            )
            assert absolute not in document[0].text


async def test_a_pre_fix_migrated_record_still_accepts_an_outcome(tmp_path):
    """R-6: the repair moves `content_hash`, and that is a decision."""

    root = tmp_path / "project"
    (root / "src").mkdir(parents=True)
    (tmp_path / "other").mkdir()
    storage = _v6_database(root)
    MigrationV7Service(_registry(root)).apply(root)
    workspace_id = _workspace_id(root)

    database = resolve_active_database(storage).path
    connection = sqlite3.connect(database)
    try:
        connection.execute(
            "UPDATE memory_records SET file_path_relative='../other/helper.py' "
            "WHERE record_type='decision'"
        )
        connection.commit()
        before = connection.execute(
            "SELECT record_id,content_hash FROM memory_records "
            "WHERE record_type='decision'"
        ).fetchone()
    finally:
        connection.close()

    async with Client(_server(root)) as client:
        await _succeed(client, workspace_id, "session_brief")
        outcome = await _succeed(
            client,
            workspace_id,
            "memory_record_outcome",
            record_id=before[0],
            outcome_text="still true after the repair",
            worked=True,
            idempotency_key="pre-fix-outcome-0001",
        )
        assert outcome["record_id"] == before[0]
        assert outcome["worked"] is True

    connection = sqlite3.connect(database)
    try:
        after = connection.execute(
            "SELECT content_hash,file_path_relative FROM memory_records "
            "WHERE record_id=?",
            (before[0],),
        ).fetchone()
    finally:
        connection.close()
    # Dropping the escaping link is part of the record state, so the next
    # event's content hash moves although no content changed.  Pinned here so
    # a client keying on `content_hash` is a known consequence, not a surprise.
    assert after[1] is None
    assert after[0] != before[1]


async def test_verify_v7_stays_valid_across_the_first_write(tmp_path):
    """Plan item 8: a just-activated migration verifies, and keeps verifying.

    The migration activates manifests for tables it fills directly and
    nothing rebuilds them, so before the exemption the first write moved
    every migrated store to `invalid` for good.
    """

    from daem0nmcp.verification_v7 import verify_v7

    root = tmp_path / "project"
    (root / "src").mkdir(parents=True)
    (tmp_path / "other").mkdir()
    storage = _v6_database(root)
    MigrationV7Service(_registry(root)).apply(root)
    workspace_id = _workspace_id(root)

    # Not vacuous: the migration really did record table-snapshot manifests.
    connection = sqlite3.connect(resolve_active_database(storage).path)
    try:
        activated = {
            str(row[0])
            for row in connection.execute(
                "SELECT projection_name FROM projection_manifests "
                "WHERE workspace_id=? AND status='active'",
                (workspace_id,),
            )
        }
    finally:
        connection.close()
    assert {
        "memory_records",
        "memory_fact_versions",
        "memory_relationship_versions",
    } <= activated

    assert verify_v7(storage, workspace_id)["status"] == "verified"

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
                "idempotency_key": "verify-after-write-0001",
            },
            description="write after migrating",
        )
        await _succeed(
            client,
            workspace_id,
            "memory_store",
            record_type="decision",
            content="written after the migration",
            idempotency_key="verify-after-write-0001",
            preflight_token=preflight["preflight_token"],
        )

    assert verify_v7(storage, workspace_id)["status"] == "verified"


async def test_reactivating_keeps_governance_authored_through_v7(tmp_path):
    """Reconcile must not read "not in v6" as "deleted in v6".

    `rule_create` and `context_trigger_create` also write the retained v6
    row, so a stream with no v6 row only appears once that compatibility
    write is gone -- this test removes it to pin the guard that makes
    reconcile speak only for what the migration itself created.
    """

    root = tmp_path / "project"
    (root / "src").mkdir(parents=True)
    (tmp_path / "other").mkdir()
    storage = _v6_database(root)
    service = MigrationV7Service(_registry(root))
    service.apply(root)
    workspace_id = _workspace_id(root)

    async with Client(_server(root)) as client:
        await _succeed(client, workspace_id, "session_brief")
        rule = await _protected(
            client,
            workspace_id,
            "rule_create",
            {
                "trigger": "before touching the cache",
                "must_do": ["flush it"],
                "idempotency_key": "v7-rule-0001",
            },
        )
        trigger = await _protected(
            client,
            workspace_id,
            "context_trigger_create",
            {
                "trigger_type": "tag",
                "pattern": "cache.*",
                "recall_query": "cache guidance",
                "idempotency_key": "v7-trigger-0001",
            },
        )
    rule_id = rule["rule_id"]
    trigger_id = trigger["trigger_id"]

    # Drop the retained v6 rows the compatibility write made, leaving two
    # streams that exist only in v7 -- the shape reconcile must not touch.
    # That shape is not reachable through the tools today, because
    # `rule_create`/`context_trigger_create` write the retained row and
    # `rule_update` writes it back; it becomes reachable once PR 10 removes
    # that compatibility write, which is what this guards.
    connection = sqlite3.connect(resolve_active_database(storage).path)
    try:
        connection.execute("DELETE FROM rules WHERE id>1")
        connection.execute("DELETE FROM context_triggers WHERE id>2")
        connection.commit()
    finally:
        connection.close()

    # The documented recovery path: roll back accepting the stranded writes,
    # then re-apply, which reactivates the retained candidate.
    service.rollback(root, discard_v7_writes=True)
    result = service.apply(root)
    assert result.action == "reactivate", result
    assert result.validation["rules_disabled"] == 0, result.validation
    assert result.validation["triggers_deleted"] == 0, result.validation

    connection = sqlite3.connect(resolve_active_database(storage).path)
    try:
        rule_row = connection.execute(
            "SELECT enabled FROM governance_rules WHERE rule_id=?", (rule_id,)
        ).fetchone()
        trigger_row = connection.execute(
            "SELECT deleted_at_us FROM governance_context_triggers WHERE trigger_id=?",
            (trigger_id,),
        ).fetchone()
        migrated_rule = connection.execute(
            "SELECT enabled FROM governance_rules WHERE rule_id<>?", (rule_id,)
        ).fetchone()
    finally:
        connection.close()
    assert rule_row is not None and rule_row[0] == 1, "a v7 rule was disabled"
    assert trigger_row is not None and trigger_row[0] is None, (
        "a v7 trigger was deleted"
    )
    assert migrated_rule is not None and migrated_rule[0] == 1


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
    assert result.validation["trigger_count"] == 2
    assert result.validation["foreign_trigger_count"] == 0
    rows = {row["record_type"]: row for row in _records(storage)}
    assert rows["decision"]["file_path_relative"] == "src/db.py"

    workspace_id = _workspace_id(new_root)
    async with Client(_server(new_root)) as client:
        await _succeed(client, workspace_id, "session_brief")
        await _succeed(client, workspace_id, "memory_recall", query="postgres")


def test_the_bounded_summary_clips_what_v6_had_no_limit_for():
    """The model is the one place every stored-record reader passes through."""

    from daem0nmcp.api.v7.models import RecordSummary

    base = {
        "record_id": "mem_" + "a" * 64,
        "record_type": "decision",
        "tags": ["x" * 120, "dup", "dup", *[f"t{n}" for n in range(40)]],
        "relative_file_path": None,
        "current_status": "current",
        "content_hash": "b" * 64,
        "created_at": "2026-01-02T00:00:00Z",
        "updated_at": "2026-01-01T00:00:00Z",
    }
    summary = RecordSummary.model_validate({**base, "excerpt": ""})
    # v6 accepted empty content; the wire needs at least one character.
    assert summary.excerpt == "<empty>"
    # v6 tags had no length, uniqueness or count limit.
    assert len(summary.tags) == 32
    assert len(set(summary.tags)) == 32
    assert all(len(tag) <= 80 for tag in summary.tags)
    # A valid time ahead of the transaction time is shown as the earlier one,
    # not refused: v6 kept the two independently.
    assert summary.created_at == summary.updated_at


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
    # The operator is told how much would be lost and where it stays.
    assert "1 event(s)" in str(refusal.value)
    assert "candidate.db" in str(refusal.value)
    assert "--discard-v7-writes" in str(refusal.value)
    assert resolve_active_database(storage).format_version == 7

    result = service.rollback(root, discard_v7_writes=True)
    assert result.status == "rolled_back"
    assert any(
        "1 event(s)" in warning and "candidate.db" in warning
        for warning in result.warnings
    ), result.warnings
    assert resolve_active_database(storage).format_version == 6
    assert (storage / "daem0nmcp.db").read_bytes() == source_bytes


@pytest.mark.parametrize(
    "table,insert",
    [
        (
            "workspace_link_events",
            "INSERT INTO workspace_link_events (event_id,workspace_id,"
            "linked_workspace_id,stream_version,event_type,occurred_at_us,"
            "recorded_at_us,event_hash) VALUES (?,?,?,1,'workspace.linked',"
            "?,?,?)",
        ),
        (
            "memory_capture_candidates",
            "INSERT INTO memory_capture_candidates (candidate_id,workspace_id,"
            "idempotency_key,candidate_hash,source_kind,proposed_record_json,"
            "provenance_json,created_at_us) VALUES (?,?,'capture-key-0001',?,"
            "'native_edit','{}','{}',?)",
        ),
    ],
)
def test_rollback_sees_every_stream_a_client_can_write(tmp_path, table, insert):
    """R-2/R-13/R-34: federation events and capture candidates count too."""

    root = tmp_path / "project"
    (root / "src").mkdir(parents=True)
    (tmp_path / "other").mkdir()
    storage = _v6_database(root)
    service = MigrationV7Service(_registry(root))
    service.apply(root)
    workspace_id = _workspace_id(root)

    digest = "a" * 64
    linked = "ws_" + "b" * 24
    connection = sqlite3.connect(resolve_active_database(storage).path)
    try:
        if table == "workspace_link_events":
            connection.execute(
                insert, ("evt_" + digest, workspace_id, linked, 1, 1, digest)
            )
        else:
            connection.execute(insert, ("cap_" + digest, workspace_id, digest, 1))
        connection.commit()
    finally:
        connection.close()

    with pytest.raises(MigrationV7Error) as refusal:
        service.rollback(root)
    assert refusal.value.code == "ROLLBACK_WOULD_HIDE_WRITES"


async def test_an_un_migrated_v6_workspace_says_migration_is_required(tmp_path):
    """F-066/F-069/F-043: a non-retryable error naming the migrate command."""

    root = tmp_path / "project"
    (root / "src").mkdir(parents=True)
    (tmp_path / "other").mkdir()
    _v6_database(root)
    workspace_id = _workspace_id(root)

    async with Client(_server(root)) as client:
        for tool in ("session_brief", "memory_recall", "memory_store"):
            arguments: dict[str, Any] = {}
            if tool == "memory_recall":
                arguments = {"query": "anything"}
            elif tool == "memory_store":
                arguments = {
                    "record_type": "decision",
                    "content": "anything",
                    "idempotency_key": "v6-store-0001",
                }
            response = await _invoke(client, workspace_id, tool, **arguments)
            assert response["ok"] is False, tool
            # Store and recall are gated behind a briefing that cannot
            # succeed on a v6 store, so they answer COMMUNION_REQUIRED first.
            assert response["error"]["code"] in {
                "MIGRATION_REQUIRED",
                "COMMUNION_REQUIRED",
                "SCHEMA_REJECTED",
            }, (tool, response["error"])
        brief = await _invoke(client, workspace_id, "session_brief")
        assert brief["error"]["code"] == "MIGRATION_REQUIRED"
        assert brief["error"]["retryable"] is False
        assert "migrate-v7 --apply" in brief["error"]["message"]
        # The resource surface deliberately answers one invariant
        # (`RESOURCE_UNAVAILABLE`) for every failure, so a caller cannot
        # enumerate workspaces by reading its errors; `session_brief` is
        # where the reason is named.
        from mcp.shared.exceptions import McpError

        with pytest.raises(McpError):
            await client.read_resource(f"memory://workspaces/{workspace_id}/rules")

    # The transfer boundary raises it too.  It sits behind the covenant, so
    # reach it the way an operator would: brief a migrated store, roll it back
    # to v6 under the live session, then export.
    migrated = tmp_path / "migrated"
    (migrated / "src").mkdir(parents=True)
    migrated_storage = _v6_database(migrated)
    service = MigrationV7Service(_registry(migrated))
    service.apply(migrated)
    migrated_id = _workspace_id(migrated)
    async with Client(_server(migrated)) as client:
        await _succeed(client, migrated_id, "session_brief")
        service.rollback(migrated, discard_v7_writes=True)
        assert resolve_active_database(migrated_storage).format_version == 6
        exported = await _invoke(client, migrated_id, "workspace_export")
    assert exported["ok"] is False
    assert exported["error"]["code"] == "MIGRATION_REQUIRED", exported["error"]


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
            "(3,?,'tag_match','auth.*','authentication','[\"warning\"]',1,1,?,0,NULL)",
            (str(tmp_path / "another-project"), TIME),
        )
        connection.commit()
    finally:
        connection.close()

    result = MigrationV7Service(_registry(root)).apply(root)
    assert result.validation["trigger_count"] == 2
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
    # The source is still migratable afterwards, and the store the ORM's own
    # DDL helped build still satisfies the v7 physical-schema contract.
    from daem0nmcp.verification_v7 import verify_v7

    assert MigrationV7Service(_registry(root)).apply(root).status == "activated"
    assert verify_v7(storage, _workspace_id(root))["status"] == "verified"
