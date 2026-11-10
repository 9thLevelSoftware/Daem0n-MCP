"""Database paths must reach SQLite and SQLAlchemy as paths, never as URL text."""

from __future__ import annotations

import os
import sqlite3
import sys
from contextlib import closing
from pathlib import Path, PurePosixPath, PureWindowsPath

import pytest

from daem0nmcp.database import DatabaseManager
from tests.api_v7.process_client import (
    initialize_workspaces,
    process_client,
    succeed,
)
from tests.api_v7.test_process_client import _recall_until_visible
from tests.api_v7.test_process_surface import _store

# Every one of these is a legal directory name on Windows, macOS and Linux.
_PORTABLE_NAMES = (
    "hash#mark",
    "percent%41sign",
    "two words",
    "caf\u00e9-\u8a18\u61b6",
    "amp&equals=plus+",
)
_QUESTION_MARK = pytest.param(
    "question?mark",
    marks=pytest.mark.skipif(
        sys.platform == "win32", reason="Windows file names cannot contain '?'"
    ),
)


def _sqlite_file_uri(*args, **kwargs):
    from daem0nmcp.workspace import sqlite_file_uri

    return sqlite_file_uri(*args, **kwargs)


def _entries(directory: Path) -> set[str]:
    return {entry.name for entry in directory.iterdir()}


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        (PurePosixPath("/srv/data/daem0nmcp.db"), "file:///srv/data/daem0nmcp.db"),
        (PurePosixPath("/srv/a?b/x.db"), "file:///srv/a%3Fb/x.db"),
        (PurePosixPath("/srv/a#b/x.db"), "file:///srv/a%23b/x.db"),
        (PurePosixPath("/srv/a%41b/x.db"), "file:///srv/a%2541b/x.db"),
        (PurePosixPath("/srv/a b/x.db"), "file:///srv/a%20b/x.db"),
        (PurePosixPath("/srv/caf\u00e9/x.db"), "file:///srv/caf%C3%A9/x.db"),
        (
            PureWindowsPath(r"C:\Users\Jo Smith\a#b\x.db"),
            "file:///C:/Users/Jo%20Smith/a%23b/x.db",
        ),
        (PureWindowsPath("c:/data/100%/x.db"), "file:///c:/data/100%25/x.db"),
        # SQLite accepts only an empty authority, so the server of a UNC path
        # has to stay inside the path part.
        (
            PureWindowsPath(r"\\server\share\dir one\x.db"),
            "file:////server/share/dir%20one/x.db",
        ),
    ],
)
def test_uri_escapes_every_path_and_keeps_windows_forms(path, expected):
    """An unescaped '?', '#' or '%' would be read as URI syntax, not as a name."""

    assert _sqlite_file_uri(path) == expected
    assert _sqlite_file_uri(path, mode="ro") == expected + "?mode=ro"
    assert _sqlite_file_uri(path, mode="rw") == expected + "?mode=rw"


def test_uri_resolves_a_relative_path_against_the_working_directory(
    tmp_path, monkeypatch
):
    """A relative URI and its absolute form must name the same file."""

    monkeypatch.chdir(tmp_path)
    (tmp_path / "sub dir").mkdir()
    with closing(sqlite3.connect(tmp_path / "sub dir" / "x.db")) as connection:
        connection.execute("CREATE TABLE marker (value TEXT)")
        connection.commit()

    relative = _sqlite_file_uri(Path("sub dir") / "x.db", mode="ro")
    assert relative == _sqlite_file_uri(tmp_path / "sub dir" / "x.db", mode="ro")
    assert relative == _sqlite_file_uri(os.path.join("sub dir", "x.db"), mode="ro")
    with closing(sqlite3.connect(relative, uri=True)) as connection:
        assert connection.execute("SELECT count(*) FROM marker").fetchone() == (0,)
    with pytest.raises(ValueError):
        _sqlite_file_uri(PureWindowsPath("relative") / "x.db")


@pytest.mark.parametrize("name", [*_PORTABLE_NAMES, _QUESTION_MARK])
def test_uri_opens_exactly_the_named_file(tmp_path, name):
    """The read-only URI must reach the file whose name holds URI syntax."""

    directory = tmp_path / name
    directory.mkdir()
    database = directory / "x.db"
    with closing(sqlite3.connect(database)) as connection:
        connection.execute("CREATE TABLE marker (value TEXT)")
        connection.execute("INSERT INTO marker VALUES (?)", (name,))
        connection.commit()

    uri = _sqlite_file_uri(database, mode="ro")
    with closing(sqlite3.connect(uri, uri=True)) as connection:
        assert connection.execute("SELECT value FROM marker").fetchone() == (name,)
        with pytest.raises(sqlite3.OperationalError):
            connection.execute("INSERT INTO marker VALUES ('read-only')")
    assert _entries(tmp_path) == {name}
    assert _entries(directory) == {"x.db"}


@pytest.mark.parametrize("name", [*_PORTABLE_NAMES, _QUESTION_MARK])
async def test_database_start_stays_inside_a_workspace_with_url_syntax(tmp_path, name):
    """A '?' in the path used to create the database beside the workspace."""

    root = tmp_path / name
    storage = root / ".daem0nmcp" / "storage"
    storage.mkdir(parents=True)
    manager = DatabaseManager(str(storage))
    try:
        await manager.init_db()
    finally:
        await manager.close()

    assert _entries(tmp_path) == {name}
    assert _entries(root) == {".daem0nmcp"}
    database = storage / "daem0nmcp.db"
    with closing(sqlite3.connect(database)) as connection:
        tables = connection.execute(
            "SELECT count(*) FROM sqlite_master WHERE type='table' "
            "AND name IN ('memory_events','memory_records')"
        ).fetchone()[0]
    assert tables == 2


@pytest.mark.parametrize(
    "name",
    [
        "hash#mark percent%41sign caf\u00e9-\u8a18\u61b6 amp&equals=plus+",
        _QUESTION_MARK,
    ],
)
async def test_server_reads_and_writes_a_workspace_with_url_syntax(tmp_path, name):
    """A '#' in the path used to answer every tool with ACTIVE_V7_UNAVAILABLE."""

    root = tmp_path / name
    root.mkdir()
    (workspace,) = await initialize_workspaces((root,))
    scope = {"workspace_id": workspace.workspace_id}
    content = "Escape database paths before they become SQLite URIs."
    async with process_client(workspace.root, "stdio") as session:
        await succeed(session, "session_brief", scope)
        record = await _store(session, scope, content=content, key="uri-store-0001")
        health = await succeed(session, "system_health", scope)
        await _recall_until_visible(
            session, {**scope, "query": "SQLite URIs", "limit": 5}, record["record_id"]
        )

    assert health["storage_format_version"] == 7
    assert _entries(tmp_path) == {name}
    assert _entries(root) == {".daem0nmcp", "stdio-server.log"}
