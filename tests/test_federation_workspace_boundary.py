"""Federation authorization tests through LinkManager."""

import contextlib
import datetime
import importlib.util
import shutil
import sys
import types
import unittest
import uuid
from pathlib import Path

from daem0nmcp.workspace import (
    WorkspaceAccessError,
    WorkspacePathError,
    WorkspaceRegistry,
)


class _Column:
    def __init__(self, name):
        self.name = name

    def __get__(self, instance, owner):
        if instance is None:
            return self
        return instance.__dict__.get(self.name)

    def __set__(self, instance, value):
        instance.__dict__[self.name] = value

    def __eq__(self, value):
        return lambda row: getattr(row, self.name) == value

    def in_(self, values):
        accepted = set(values)
        return lambda row: getattr(row, self.name) in accepted


class _ProjectLink:
    source_path = _Column("source_path")
    linked_path = _Column("linked_path")

    def __init__(self, source_path, linked_path, relationship="related", label=None):
        self.id = None
        self.source_path = source_path
        self.linked_path = linked_path
        self.relationship = relationship
        self.label = label
        self.created_at = datetime.datetime.now(datetime.timezone.utc)


class _Query:
    def __init__(self, operation, entity):
        self.operation = operation
        self.entity = entity
        self.predicates = []

    def where(self, *predicates):
        self.predicates.extend(predicates)
        return self


class _Result:
    def __init__(self, rows=None, rowcount=0):
        self._rows = list(rows or [])
        self.rowcount = rowcount

    def scalar_one_or_none(self):
        return self._rows[0] if self._rows else None

    def scalars(self):
        return self

    def all(self):
        return self._rows


class _Session:
    def __init__(self, database):
        self.database = database

    async def execute(self, query):
        matching = [
            link
            for link in self.database.links
            if all(predicate(link) for predicate in query.predicates)
        ]
        if query.operation == "delete":
            self.database.links = [
                link for link in self.database.links if link not in matching
            ]
            return _Result(rowcount=len(matching))
        return _Result(matching)

    def add(self, link):
        link.id = len(self.database.links) + 1
        self.database.links.append(link)


class _Database:
    def __init__(self):
        self.links = []
        self.session_count = 0

    @contextlib.asynccontextmanager
    async def get_session(self):
        self.session_count += 1
        yield _Session(self)


class _LinkedDatabase:
    constructed_paths = []

    def __init__(self, storage_path):
        self.storage_path = Path(storage_path)
        self.constructed_paths.append(self.storage_path)

    async def init_db(self):
        return None

    @contextlib.asynccontextmanager
    async def get_session(self):
        yield _SourceSession()


class _SourceSession:
    async def execute(self, query):
        return _Result([])


class _MemoryManager:
    def __init__(self, database):
        self.database = database

    async def remember(self, **kwargs):
        return None


def _fake_module(name, **attributes):
    module = types.ModuleType(name)
    for key, value in attributes.items():
        setattr(module, key, value)
    return module


@contextlib.contextmanager
def _loaded_links():
    sqlalchemy = _fake_module(
        "sqlalchemy",
        select=lambda entity: _Query("select", entity),
        delete=lambda entity: _Query("delete", entity),
    )
    database_module = _fake_module(
        "daem0nmcp.database", DatabaseManager=_LinkedDatabase
    )
    models_module = _fake_module(
        "daem0nmcp.models", ProjectLink=_ProjectLink, Memory=object
    )
    memory_module = _fake_module("daem0nmcp.memory", MemoryManager=_MemoryManager)
    fake_modules = {
        "sqlalchemy": sqlalchemy,
        "daem0nmcp.database": database_module,
        "daem0nmcp.models": models_module,
        "daem0nmcp.memory": memory_module,
    }
    originals = {name: sys.modules.get(name) for name in fake_modules}
    sys.modules.update(fake_modules)
    module_name = "daem0nmcp._federation_workspace_boundary_test"
    try:
        source = Path(__file__).resolve().parents[1] / "daem0nmcp" / "links.py"
        spec = importlib.util.spec_from_file_location(module_name, source)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        spec.loader.exec_module(module)
        yield module
    finally:
        sys.modules.pop(module_name, None)
        for name, original in originals.items():
            if original is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = original


class FederationWorkspaceBoundaryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        base = Path(__file__).resolve().parents[1] / ".test_tmp"
        base.mkdir(parents=True, exist_ok=True)
        self.temp_root = base / f"federation-security-{uuid.uuid4().hex}"
        self.temp_root.mkdir()
        self.primary = self.temp_root / "primary"
        self.secondary = self.temp_root / "secondary"
        self.primary.mkdir()
        self.secondary.mkdir()
        self.registry = WorkspaceRegistry([self.primary, self.secondary])
        _LinkedDatabase.constructed_paths.clear()

    def tearDown(self):
        shutil.rmtree(self.temp_root, ignore_errors=True)

    async def test_unregistered_link_target_fails_before_link_database_access(self):
        database = _Database()
        unknown = self.temp_root / "unknown"
        with _loaded_links() as links:
            manager = links.LinkManager(database, registry=self.registry)
            with self.assertRaises(WorkspaceAccessError):
                await manager.link_projects(str(self.primary), str(unknown))

        self.assertEqual(database.session_count, 0)
        self.assertEqual(database.links, [])

    async def test_registered_link_stores_stable_workspace_reference(self):
        database = _Database()
        secondary = self.registry.resolve(str(self.secondary))
        with _loaded_links() as links:
            manager = links.LinkManager(database, registry=self.registry)
            result = await manager.link_projects(
                str(self.primary), str(self.secondary), "same-project"
            )

        self.assertEqual(result["status"], "linked")
        self.assertEqual(database.links[0].linked_path, secondary.workspace_id)

    async def test_registered_legacy_stored_path_is_migration_readable(self):
        database = _Database()
        database.links.append(_ProjectLink(str(self.primary), str(self.secondary)))
        with _loaded_links() as links:
            manager = links.LinkManager(database, registry=self.registry)
            result = await manager.list_linked_projects(str(self.primary))

        self.assertEqual(result[0]["linked_path"], str(self.secondary.resolve()))
        self.assertTrue(result[0]["workspace_id"].startswith("ws_"))

    async def test_stale_stored_link_fails_before_source_database_or_archive(self):
        database = _Database()
        stale = self.temp_root / "stale"
        stale.mkdir()
        source_data = stale / ".daem0nmcp"
        source_data.mkdir()
        database.links.append(_ProjectLink(str(self.primary), str(stale)))

        with _loaded_links() as links:
            manager = links.LinkManager(database, registry=self.registry)
            with self.assertRaises(WorkspaceAccessError):
                await manager.consolidate_linked_databases(
                    str(self.primary), archive_sources=True
                )

        self.assertEqual(_LinkedDatabase.constructed_paths, [])
        self.assertTrue(source_data.exists())
        self.assertFalse((stale / ".daem0nmcp.archived").exists())

    async def test_two_registered_roots_can_open_linked_storage(self):
        storage = self.secondary / ".daem0nmcp" / "storage"
        storage.mkdir(parents=True)
        database = _Database()
        with _loaded_links() as links:
            manager = links.LinkManager(database, registry=self.registry)
            await manager.link_projects(str(self.primary), str(self.secondary))
            managers = await manager.get_linked_db_managers(str(self.primary))

        self.assertEqual(len(managers), 1)
        self.assertEqual(managers[0][0], str(self.secondary.resolve()))
        self.assertEqual(_LinkedDatabase.constructed_paths, [storage.resolve()])

    async def test_derived_storage_escape_fails_before_linked_database_access(self):
        class DerivedPathEscapeError(ValueError):
            pass

        storage = self.secondary / ".daem0nmcp" / "storage"
        storage.mkdir(parents=True)
        database = _Database()
        with _loaded_links() as links:
            manager = links.LinkManager(database, registry=self.registry)
            await manager.link_projects(str(self.primary), str(self.secondary))

            def reject_derived_path(*args, **kwargs):
                raise DerivedPathEscapeError("derived storage escaped")

            links.resolve_derived_path = reject_derived_path
            with self.assertRaises(DerivedPathEscapeError):
                await manager.get_linked_db_managers(str(self.primary))

        self.assertEqual(_LinkedDatabase.constructed_paths, [])

    async def test_registered_consolidation_can_archive_source(self):
        source_data = self.secondary / ".daem0nmcp"
        (source_data / "storage").mkdir(parents=True)
        database = _Database()
        with _loaded_links() as links:
            manager = links.LinkManager(database, registry=self.registry)
            await manager.link_projects(str(self.primary), str(self.secondary))
            result = await manager.consolidate_linked_databases(
                str(self.primary), archive_sources=True
            )

        self.assertEqual(result["status"], "consolidated")
        self.assertEqual(result["sources_processed"], [str(self.secondary.resolve())])
        self.assertFalse(source_data.exists())
        self.assertTrue((self.secondary / ".daem0nmcp.archived").exists())

    async def test_archive_path_escape_propagates_without_mutation(self):
        source_data = self.secondary / ".daem0nmcp"
        (source_data / "storage").mkdir(parents=True)
        database = _Database()
        with _loaded_links() as links:
            manager = links.LinkManager(database, registry=self.registry)
            await manager.link_projects(str(self.primary), str(self.secondary))
            real_resolver = links.resolve_derived_path

            def reject_archive_path(workspace_root, *relative_parts):
                if relative_parts in {
                    (".daem0nmcp",),
                    (".daem0nmcp.archived",),
                }:
                    raise WorkspacePathError()
                return real_resolver(workspace_root, *relative_parts)

            links.resolve_derived_path = reject_archive_path
            with self.assertRaises(WorkspacePathError):
                await manager.consolidate_linked_databases(
                    str(self.primary), archive_sources=True
                )

        self.assertTrue(source_data.exists())
        self.assertFalse((self.secondary / ".daem0nmcp.archived").exists())

    @unittest.skipUnless(hasattr(Path, "symlink_to"), "symlinks are unavailable")
    async def test_nested_storage_symlink_fails_before_linked_database_access(self):
        metadata = self.secondary / ".daem0nmcp"
        metadata.mkdir()
        outside = self.temp_root / "outside-storage"
        outside.mkdir()
        try:
            (metadata / "storage").symlink_to(outside, target_is_directory=True)
        except OSError as exc:
            self.skipTest(f"symlink creation is unavailable: {exc}")

        database = _Database()
        with _loaded_links() as links:
            manager = links.LinkManager(database, registry=self.registry)
            await manager.link_projects(str(self.primary), str(self.secondary))
            with self.assertRaises(ValueError):
                await manager.get_linked_db_managers(str(self.primary))

        self.assertEqual(_LinkedDatabase.constructed_paths, [])


if __name__ == "__main__":
    unittest.main()
