#!/usr/bin/env python3
"""
Daem0n-MCP Upgrade Script — one-stop-shop for upgrading any installation.

Usage:
    python scripts/upgrade.py                     # dry-run (assess only)
    python scripts/upgrade.py --apply             # perform upgrade
    python scripts/upgrade.py --apply --yes       # skip confirmations
    python scripts/upgrade.py --rollback          # undo last upgrade
    python scripts/upgrade.py --project /path     # include specific project dir
    python scripts/upgrade.py --skip-embeddings   # skip vector re-encoding
    python scripts/upgrade.py --no-install        # skip pip install step

4-Phase Pipeline:
  Phase 1: ASSESS & BACKUP   (stdlib only)
  Phase 2: INSTALL            (subprocess only)
  Phase 3: MIGRATE            (imports daem0nmcp)
  Phase 4: VALIDATE & RE-ENABLE

No dependencies outside the Python standard library until Phase 3.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import time
from pathlib import Path
from typing import NamedTuple, Optional, List, Dict, Any


# Direct script execution sets sys.path[0] to ``scripts/``.  Make the adjacent
# source package available to dependency-free Phase 1 even before installation,
# including under Python's isolated ``-I`` mode.
_SOURCE_CHECKOUT_ROOT = Path(__file__).resolve().parents[1]
if (_SOURCE_CHECKOUT_ROOT / "daem0nmcp" / "storage_activation.py").is_file():
    source_root = str(_SOURCE_CHECKOUT_ROOT)
    if source_root not in sys.path:
        sys.path.insert(0, source_root)

from daem0nmcp.schema_version import CURRENT_SCHEMA_VERSION


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

CURRENT_EMBEDDING_DIM = 256
OLD_EMBEDDING_DIM = 384
BACKUP_DIR_NAME = ".daem0nmcp_upgrade_backup"
MANIFEST_FILE = "manifest.json"


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

class VersionFingerprint(NamedTuple):
    schema_version: int           # 0-current
    embedding_dim: Optional[int]  # 384, 256, or None
    memory_count: int
    vector_count: int
    has_qdrant: bool
    db_path: str
    format_version: int           # 0 (absent), 6, or 7
    hook_format: str              # "none" | "legacy_shell" | "module"
    estimated_version: str        # "pre-v4" | "v4.0" | "v5.0" | "v6.0" | "v6.6.6"


class ProjectState:
    """Mutable state for a single project through the upgrade pipeline."""

    def __init__(self, project_path: str, fingerprint: VersionFingerprint) -> None:
        self.project_path = project_path
        self.fingerprint = fingerprint
        self.backup_dir = ""
        self.actions_taken: List[str] = []
        self.errors: List[str] = []
        self.needs_schema_migration = False
        self.needs_embedding_migration = False
        self.needs_hook_install = False
        self.needs_fresh_start = False
        self.skipped = False

    def classify(self) -> None:
        fp = self.fingerprint
        if fp.format_version == 7 or fp.estimated_version == "v6.6.6":
            self.skipped = True
        elif fp.estimated_version == "pre-v4":
            self.needs_fresh_start = True
            self.needs_hook_install = True
        elif fp.estimated_version in ("v4.0", "v5.0"):
            self.needs_schema_migration = True
            self.needs_embedding_migration = True
            self.needs_hook_install = True
        elif fp.estimated_version == "v6.0":
            self.needs_schema_migration = True
            self.needs_hook_install = True
        elif fp.estimated_version == "v6.0-stale-embeddings":
            self.needs_embedding_migration = True
            self.needs_hook_install = True
        else:
            # Unknown but has DB — try schema + hooks
            self.needs_schema_migration = True
            self.needs_hook_install = True


# ===================================================================
# Phase 1: ASSESS & BACKUP  (stdlib only)
# ===================================================================

def discover_projects(explicit_dirs: List[str]) -> List[str]:
    """
    Discover project directories to upgrade.

    Auto-detects cwd if it looks like a project, scans one level of child
    directories, and merges with explicitly provided --project paths.
    """
    cwd = Path.cwd()
    explicit = {str(Path(p).resolve()) for p in explicit_dirs}
    auto_dirs: List[str] = []

    cwd_str = str(cwd.resolve())
    if cwd_str not in explicit:
        if (cwd / ".claude").exists() or (cwd / ".mcp.json").exists() or (cwd / ".daem0nmcp").exists():
            auto_dirs.append(cwd_str)

    if cwd.is_dir():
        for child in cwd.iterdir():
            if not child.is_dir():
                continue
            child_str = str(child.resolve())
            if child_str in explicit or child_str in [str(Path(a).resolve()) for a in auto_dirs]:
                continue
            if (child / ".claude").exists() or (child / ".mcp.json").exists() or (child / ".daem0nmcp").exists():
                auto_dirs.append(child_str)

    all_dirs = auto_dirs + [str(Path(p).resolve()) for p in explicit_dirs]
    # Deduplicate while preserving order
    seen: set[str] = set()
    result: List[str] = []
    for d in all_dirs:
        if d not in seen:
            seen.add(d)
            result.append(d)
    return result


_V6_ONLY_ERROR = (
    "Deprecated v6-only vector migration cannot run against format 7; use "
    "`python -m daem0nmcp.cli rebuild-projection --projection dense "
    "--workspace-id <workspace-id>` instead."
)


def _resolve_db_selection(project_path: str) -> tuple[str, int]:
    """
    Find the authoritative database and architecture format from a project path.

    Accepted paths are any of:
      - /path/to/project                 (.daem0nmcp/storage/daem0nmcp.db)
      - /path/to/project/.daem0nmcp      (storage/daem0nmcp.db)
      - /path/to/project/.daem0nmcp/storage  (daem0nmcp.db)
    """
    storage_candidates = [
        Path(project_path) / ".daem0nmcp" / "storage",
        Path(project_path) / "storage",
        Path(project_path),
    ]
    from daem0nmcp.storage_activation import (
        has_canonical_v7_state,
        resolve_active_database,
    )

    # The shared validator owns canonical active-db.json decoding, including
    # the pointer.get("format_version") relationship checks used by v7 activation.
    for storage in storage_candidates:
        pointer_path = storage / "active-db.json"
        if pointer_path.exists() or pointer_path.is_symlink():
            active = resolve_active_database(storage)
            return str(active.path), active.format_version
    for storage in storage_candidates:
        if (storage / "daem0nmcp.db").is_file():
            active = resolve_active_database(storage)
            format_version = (
                7 if has_canonical_v7_state(active.path) else active.format_version
            )
            return str(active.path), format_version
    return "", 0


def resolve_db_path(project_path: str) -> str:
    """Return the authoritative database path for compatibility callers."""

    return _resolve_db_selection(project_path)[0]


def _active_selection_for_database(db_path: str) -> tuple[Path, int]:
    """Return the governing storage directory and active architecture format."""

    from daem0nmcp.storage_activation import (
        has_canonical_v7_state,
        resolve_active_database,
    )

    target = Path(db_path).resolve()
    for storage in target.parents:
        pointer = storage / "active-db.json"
        if not pointer.exists() and not pointer.is_symlink():
            continue
        active = resolve_active_database(storage)
        if active.path.resolve() != target:
            raise RuntimeError(
                "Legacy vector migration requires the selected active database."
            )
        return storage, active.format_version
    return target.parent, 7 if has_canonical_v7_state(target) else 6


def _detect_embedding_dim(conn: sqlite3.Connection) -> Optional[int]:
    """Detect embedding dimension from the first non-null vector blob."""
    try:
        row = conn.execute(
            "SELECT vector_embedding FROM memories "
            "WHERE vector_embedding IS NOT NULL LIMIT 1"
        ).fetchone()
        if row and row[0]:
            blob = row[0]
            return len(blob) // 4  # float32 = 4 bytes each
    except sqlite3.OperationalError:
        pass
    return None


def _detect_hook_format() -> str:
    """Detect hook format from ~/.claude/settings.json."""
    settings_path = Path.home() / ".claude" / "settings.json"
    if not settings_path.exists():
        return "none"
    try:
        data = json.loads(settings_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return "none"

    hooks = data.get("hooks", {})
    for event_entries in hooks.values():
        if not isinstance(event_entries, list):
            continue
        for entry in event_entries:
            for hook in entry.get("hooks", []):
                cmd = hook.get("command", "")
                if "daem0nmcp.claude_hooks" in cmd:
                    return "module"
                if "daem0n_pre_edit_hook" in cmd or "daem0n_stop_hook" in cmd:
                    return "legacy_shell"
                if "daem0n_post_edit_hook" in cmd or "daem0n_prompt_hook" in cmd:
                    return "legacy_shell"
    return "none"


def _estimate_version(
    schema_version: int,
    embedding_dim: Optional[int],
    has_legacy_dir: bool,
) -> str:
    """Estimate the installed version from collected signals."""
    if has_legacy_dir and schema_version == 0:
        return "pre-v4"
    if schema_version == 0:
        return "pre-v4"
    if schema_version >= CURRENT_SCHEMA_VERSION and embedding_dim in (CURRENT_EMBEDDING_DIM, None):
        return "v6.6.6"
    if schema_version >= CURRENT_SCHEMA_VERSION and embedding_dim == OLD_EMBEDDING_DIM:
        # Schema is current but embeddings are old — needs embedding migration only
        return "v6.0-stale-embeddings"
    if embedding_dim == OLD_EMBEDDING_DIM and schema_version < CURRENT_SCHEMA_VERSION:
        return "v4.0"
    if schema_version < CURRENT_SCHEMA_VERSION and embedding_dim in (CURRENT_EMBEDDING_DIM, None):
        return "v6.0"
    # Default — something in between
    return "v5.0"


def detect_version(project_path: str) -> VersionFingerprint:
    """
    Core version detection using only stdlib (sqlite3).

    Reads schema version, embedding dimensions, counts, and hook format
    directly from the database without importing daem0nmcp.
    """
    db_path, format_version = _resolve_db_selection(project_path)
    has_legacy_dir = os.path.isdir(os.path.join(project_path, ".devilmcp"))
    storage_dir = os.path.dirname(db_path) if db_path else os.path.join(project_path, ".daem0nmcp", "storage")
    has_qdrant = os.path.isdir(os.path.join(storage_dir, "qdrant"))

    schema_version = 0
    embedding_dim: Optional[int] = None
    memory_count = 0
    vector_count = 0

    if db_path and os.path.exists(db_path):
        try:
            conn = sqlite3.connect(db_path)
            try:
                # Schema version
                try:
                    row = conn.execute("SELECT MAX(version) FROM schema_version").fetchone()
                    if row and row[0] is not None:
                        schema_version = int(row[0])
                except sqlite3.OperationalError:
                    pass  # table doesn't exist

                # Memory count
                try:
                    row = conn.execute("SELECT COUNT(*) FROM memories").fetchone()
                    if row:
                        memory_count = row[0]
                except sqlite3.OperationalError:
                    pass

                # Vector count and embedding dim
                try:
                    row = conn.execute(
                        "SELECT COUNT(*) FROM memories WHERE vector_embedding IS NOT NULL"
                    ).fetchone()
                    if row:
                        vector_count = row[0]
                    embedding_dim = _detect_embedding_dim(conn)
                except sqlite3.OperationalError:
                    pass
            finally:
                conn.close()
        except sqlite3.DatabaseError:
            pass  # corrupt or not a sqlite file

    hook_format = _detect_hook_format()
    estimated = (
        "v7"
        if format_version == 7
        else _estimate_version(schema_version, embedding_dim, has_legacy_dir)
    )

    return VersionFingerprint(
        schema_version=schema_version,
        embedding_dim=embedding_dim,
        memory_count=memory_count,
        vector_count=vector_count,
        has_qdrant=has_qdrant,
        db_path=db_path,
        format_version=format_version,
        hook_format=hook_format,
        estimated_version=estimated,
    )


def find_claude_settings_files(project_dirs: List[str]) -> List[Path]:
    """Find all Claude Code settings files that could contain hooks."""
    home = Path.home()
    candidates = [
        home / ".claude" / "settings.json",
        home / ".claude" / "settings.local.json",
    ]
    for proj in project_dirs:
        p = Path(proj).resolve()
        candidates.append(p / ".claude" / "settings.json")
        candidates.append(p / ".claude" / "settings.local.json")
    return [f for f in candidates if f.exists()]


def find_mcp_json_files(project_dirs: List[str]) -> List[Path]:
    """Find all .mcp.json files that could define MCP servers."""
    home = Path.home()
    candidates = [
        home / ".claude" / ".mcp.json",
        home / ".mcp.json",
    ]
    for proj in project_dirs:
        p = Path(proj).resolve()
        candidates.append(p / ".mcp.json")
    return [f for f in candidates if f.exists()]


def backup_project(state: ProjectState) -> bool:
    """
    Back up DB, qdrant dir, and settings files for a single project.

    Returns True on success, False on failure (with error recorded in state).
    """
    fp = state.fingerprint
    if not fp.db_path:
        state.actions_taken.append("No database found — nothing to back up")
        return True

    timestamp = time.strftime("%Y%m%d_%H%M%S")
    backup_dir = os.path.join(state.project_path, BACKUP_DIR_NAME, timestamp)
    os.makedirs(backup_dir, exist_ok=True)
    state.backup_dir = backup_dir

    manifest: Dict[str, Any] = {
        "timestamp": timestamp,
        "project_path": state.project_path,
        "fingerprint": state.fingerprint._asdict(),
        "files": {},
    }

    # Back up database via sqlite3 backup API (WAL-safe)
    try:
        db_backup_path = os.path.join(backup_dir, "daem0nmcp.db")
        src = sqlite3.connect(fp.db_path)
        dst = sqlite3.connect(db_backup_path)
        try:
            src.backup(dst)
        finally:
            dst.close()
            src.close()
        manifest["files"]["database"] = {
            "original": fp.db_path,
            "backup": db_backup_path,
        }
        state.actions_taken.append("Backed up database to {}".format(backup_dir))
    except Exception as exc:
        state.errors.append("Failed to back up database: {}".format(exc))
        return False

    # Back up qdrant directory
    storage_dir = os.path.dirname(fp.db_path)
    qdrant_dir = os.path.join(storage_dir, "qdrant")
    if os.path.isdir(qdrant_dir):
        qdrant_backup = os.path.join(backup_dir, "qdrant")
        try:
            shutil.copytree(qdrant_dir, qdrant_backup)
            manifest["files"]["qdrant"] = {
                "original": qdrant_dir,
                "backup": qdrant_backup,
            }
            state.actions_taken.append("Backed up qdrant directory")
        except Exception as exc:
            state.errors.append("Failed to back up qdrant: {}".format(exc))
            # Non-fatal — continue

    # Back up settings files (use counter to avoid collisions between
    # files with the same name from different directories)
    settings_files = find_claude_settings_files([state.project_path])
    for idx, sf in enumerate(settings_files):
        bak_name = "{:02d}_{}".format(idx, sf.name + ".upgrade_bak")
        bak_path = os.path.join(backup_dir, bak_name)
        try:
            shutil.copy2(str(sf), bak_path)
            manifest["files"]["settings_{:02d}_{}".format(idx, sf.name)] = {
                "original": str(sf),
                "backup": bak_path,
            }
        except Exception as exc:
            state.errors.append("Failed to back up {}: {}".format(sf, exc))

    # Back up MCP JSON files
    mcp_files = find_mcp_json_files([state.project_path])
    for idx, mf in enumerate(mcp_files):
        bak_name = "{:02d}_{}".format(idx, mf.name + ".upgrade_bak")
        bak_path = os.path.join(backup_dir, bak_name)
        try:
            shutil.copy2(str(mf), bak_path)
            manifest["files"]["mcp_{:02d}_{}".format(idx, mf.name)] = {
                "original": str(mf),
                "backup": bak_path,
            }
        except Exception as exc:
            state.errors.append("Failed to back up {}: {}".format(mf, exc))

    # Write manifest
    manifest_path = os.path.join(backup_dir, MANIFEST_FILE)
    with open(manifest_path, "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2)

    return True


def disable_hooks_and_mcp(project_dirs: List[str]) -> List[str]:
    """
    Disable daem0n hooks and MCP server entries for safe upgrade.

    Returns list of action descriptions.
    """
    actions: List[str] = []

    # Disable hooks in settings files
    for sf in find_claude_settings_files(project_dirs):
        try:
            data = json.loads(sf.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue

        hooks = data.get("hooks", {})
        changed = False
        for event in list(hooks.keys()):
            if not isinstance(hooks[event], list):
                continue
            original_len = len(hooks[event])
            hooks[event] = [
                e for e in hooks[event]
                if not _is_daem0n_hook_entry(e)
            ]
            if len(hooks[event]) < original_len:
                changed = True
            if not hooks[event]:
                del hooks[event]

        if changed:
            if hooks:
                data["hooks"] = hooks
            else:
                data.pop("hooks", None)
            sf.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
            actions.append("Disabled daem0n hooks in {}".format(sf))

    # Disable MCP server entries
    for mf in find_mcp_json_files(project_dirs):
        try:
            data = json.loads(mf.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue

        servers = data.get("mcpServers", {})
        daem0n_keys = [k for k in servers if "daem0n" in k.lower() and "_DISABLED" not in k]
        if daem0n_keys:
            for k in daem0n_keys:
                servers[k + "_DISABLED"] = servers.pop(k)
            data["mcpServers"] = servers
            mf.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
            actions.append("Disabled MCP server(s) {} in {}".format(
                ", ".join(daem0n_keys), mf
            ))

    return actions


def _is_daem0n_hook_entry(entry: dict) -> bool:
    """Check if a hook entry belongs to Daem0n (current or legacy)."""
    for hook in entry.get("hooks", []):
        cmd = hook.get("command", "")
        if "daem0nmcp.claude_hooks" in cmd:
            return True
        if any(name in cmd for name in (
            "daem0n_pre_edit_hook", "daem0n_stop_hook",
            "daem0n_post_edit_hook", "daem0n_prompt_hook",
        )):
            return True
    return False


# ===================================================================
# Phase 2: INSTALL  (subprocess only)
# ===================================================================

def detect_install_mode(script_dir: str) -> str:
    """
    Determine install mode based on environment.

    Returns "editable" if pyproject.toml is nearby (dev install),
    otherwise "pip" for standard package install.
    """
    # Check if pyproject.toml exists in the same repo as this script
    repo_root = os.path.dirname(script_dir)
    if os.path.exists(os.path.join(repo_root, "pyproject.toml")):
        return "editable"
    return "pip"


def install_package(mode: str, script_dir: str) -> tuple[bool, str]:
    """
    Install or upgrade the daem0nmcp package.

    Returns (success, output_text).
    """
    if mode == "editable":
        repo_root = os.path.dirname(script_dir)
        cmd = [sys.executable, "-m", "pip", "install", "-e", repo_root]
    else:
        cmd = [sys.executable, "-m", "pip", "install", "--upgrade", "daem0nmcp"]

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=300,
        )
        output = result.stdout + result.stderr
        if result.returncode != 0:
            return False, "pip install failed (exit {}):\n{}".format(
                result.returncode, output
            )
        return True, output
    except subprocess.TimeoutExpired:
        return False, "pip install timed out after 300 seconds"
    except Exception as exc:
        return False, "pip install error: {}".format(exc)


def verify_package_import() -> tuple[bool, str]:
    """
    Verify that daem0nmcp can be imported and report its version.

    Returns (success, version_or_error).
    """
    try:
        result = subprocess.run(
            [sys.executable, "-c",
             "import daem0nmcp; print(daem0nmcp.__version__)"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode == 0:
            version = result.stdout.strip()
            return True, version
        return False, "Import failed:\n{}".format(result.stderr)
    except Exception as exc:
        return False, "Import check error: {}".format(exc)


# ===================================================================
# Phase 3: MIGRATE  (now safe to import daem0nmcp)
# ===================================================================

def run_phase3(
    states: List[ProjectState],
    skip_embeddings: bool,
    auto_yes: bool,
) -> None:
    """
    Run schema migrations, embedding re-encoding, and hook installation.

    This function imports daem0nmcp — it must only be called after Phase 2
    has verified the package is importable.
    """
    from daem0nmcp.migrations.schema import run_migrations
    from daem0nmcp.claude_hooks.install import install_claude_hooks
    from daem0nmcp.workspace import WorkspaceRegistry

    for state in states:
        if state.skipped:
            continue

        fp = state.fingerprint

        # --- Schema migration ---
        if state.needs_schema_migration and fp.db_path:
            try:
                workspace_root = Path(state.project_path).resolve()
                workspace_id = WorkspaceRegistry(
                    [workspace_root], default_root=workspace_root
                ).default.workspace_id
                count, descriptions = run_migrations(
                    fp.db_path, workspace_id=workspace_id
                )
                if count > 0:
                    state.actions_taken.append(
                        "Applied {} schema migration(s): {}".format(
                            count, "; ".join(descriptions)
                        )
                    )
                else:
                    state.actions_taken.append("Schema already up to date")
            except Exception as exc:
                state.errors.append("Schema migration failed: {}".format(exc))
                continue  # Don't attempt embedding migration if schema failed

        # --- Embedding re-encoding ---
        if (
            state.needs_embedding_migration
            and fp.db_path
            and not skip_embeddings
            and fp.format_version != 6
        ):
            state.errors.append("Embedding migration refused: " + _V6_ONLY_ERROR)

        elif state.needs_embedding_migration and fp.db_path and not skip_embeddings:
            if fp.vector_count > 500 and not auto_yes:
                print("\n  Project: {}".format(state.project_path))
                print("  {} vectors need re-encoding (384->256 dim).".format(
                    fp.vector_count
                ))
                answer = input("  Proceed? [y/N] ").strip().lower()
                if answer not in ("y", "yes"):
                    state.actions_taken.append(
                        "Skipped embedding migration ({} vectors, user declined)".format(
                            fp.vector_count
                        )
                    )
                    continue

            try:
                migrated, failed = _migrate_embeddings(
                    fp.db_path, format_version=fp.format_version
                )
                state.actions_taken.append(
                    "Re-encoded embeddings: {} migrated, {} failed".format(
                        migrated, failed
                    )
                )
            except Exception as exc:
                state.errors.append("Embedding migration failed: {}".format(exc))

        elif state.needs_embedding_migration and skip_embeddings:
            state.actions_taken.append("Skipped embedding migration (--skip-embeddings)")

        # --- Fresh start for pre-v4 ---
        if state.needs_fresh_start:
            state.actions_taken.append(
                "Pre-v4 install detected — fresh database will be created on first run"
            )

    # --- Hook installation (global, only once) ---
    any_needs_hooks = any(s.needs_hook_install and not s.skipped for s in states)
    if any_needs_hooks:
        try:
            success, message = install_claude_hooks()
            for state in states:
                if state.needs_hook_install and not state.skipped:
                    if success:
                        state.actions_taken.append("Reinstalled hooks")
                    else:
                        state.errors.append("Hook installation failed: {}".format(message))
        except Exception as exc:
            for state in states:
                if state.needs_hook_install and not state.skipped:
                    state.errors.append("Hook installation error: {}".format(exc))


def _migrate_embeddings(
    db_path: str,
    batch_size: int = 100,
    *,
    format_version: int,
) -> tuple[int, int]:
    """
    Re-encode format-6 embeddings from 384-dim to 256-dim.

    Returns (migrated_count, failed_count).
    """
    from daem0nmcp.storage_activation import DatabaseFileLock

    storage, _ = _active_selection_for_database(db_path)
    with DatabaseFileLock(storage, "shared"):
        locked_storage, active_format = _active_selection_for_database(db_path)
        if locked_storage.resolve() != storage.resolve():
            raise RuntimeError("Active database storage changed during migration.")
        if format_version != active_format or active_format != 6:
            raise RuntimeError(_V6_ONLY_ERROR)
        return _migrate_embeddings_v6(db_path, batch_size)


def _migrate_embeddings_v6(db_path: str, batch_size: int) -> tuple[int, int]:
    """Perform the format-6 write while the caller holds the storage lock."""

    from daem0nmcp import vectors
    from daem0nmcp.qdrant_store import QdrantVectorStore

    conn = sqlite3.connect(db_path)
    try:
        cursor = conn.cursor()

        cursor.execute(
            "SELECT COUNT(*) FROM memories WHERE vector_embedding IS NOT NULL"
        )
        total = cursor.fetchone()[0]
        if total == 0:
            return 0, 0

        # Initialize Qdrant if available
        storage_dir = os.path.dirname(db_path)
        qdrant_path = os.path.join(storage_dir, "qdrant")
        qdrant = None
        if os.path.isdir(qdrant_path):
            try:
                qdrant = QdrantVectorStore(path=qdrant_path)
            except Exception:
                pass  # SQLite-only migration

        # Materialize all rows upfront to avoid cursor mutation during updates
        cursor.execute(
            "SELECT id, content, rationale, category, tags, file_path, worked, is_permanent "
            "FROM memories WHERE vector_embedding IS NOT NULL"
        )
        rows = cursor.fetchall()

        migrated = 0
        failed = 0
        batch_updates: list[tuple] = []
        batch_qdrant: list[dict] = []

        for row in rows:
            mem_id, content, rationale, category, tags, file_path, worked, is_permanent = row
            text = content or ""
            if rationale:
                text += " " + rationale

            try:
                embedding_bytes = vectors.encode_document(text)
                if embedding_bytes is None:
                    failed += 1
                    continue

                batch_updates.append((embedding_bytes, mem_id))

                if qdrant is not None:
                    embedding_list = vectors.decode(embedding_bytes)
                    if embedding_list:
                        batch_qdrant.append({
                            "id": mem_id,
                            "embedding": embedding_list,
                            "metadata": {
                                "category": category,
                                "tags": tags.split(",") if tags else [],
                                "file_path": file_path,
                                "worked": worked,
                                "is_permanent": is_permanent,
                            },
                        })

                migrated += 1

                if len(batch_updates) >= batch_size:
                    conn.executemany(
                        "UPDATE memories SET vector_embedding = ? WHERE id = ?",
                        batch_updates,
                    )
                    conn.commit()
                    if qdrant is not None:
                        for item in batch_qdrant:
                            qdrant.upsert_memory(
                                item["id"], item["embedding"], item["metadata"]
                            )
                    print("  Embedding progress: {}/{}".format(migrated, total))
                    batch_updates.clear()
                    batch_qdrant.clear()

            except Exception:
                failed += 1

        # Final batch
        if batch_updates:
            conn.executemany(
                "UPDATE memories SET vector_embedding = ? WHERE id = ?",
                batch_updates,
            )
            conn.commit()
            if qdrant is not None:
                for item in batch_qdrant:
                    qdrant.upsert_memory(
                        item["id"], item["embedding"], item["metadata"]
                    )

        return migrated, failed
    finally:
        conn.close()


# ===================================================================
# Phase 4: VALIDATE & RE-ENABLE
# ===================================================================

def validate_upgrade(states: List[ProjectState]) -> List[str]:
    """
    Re-read databases to verify schema version and embedding dimensions.

    Returns list of validation messages.
    """
    messages: List[str] = []
    for state in states:
        if state.skipped:
            messages.append("[{}] Skipped (already current)".format(state.project_path))
            continue
        if not state.fingerprint.db_path:
            messages.append("[{}] No database — OK".format(state.project_path))
            continue

        db_path = state.fingerprint.db_path
        if not os.path.exists(db_path):
            messages.append("[{}] Database not found after upgrade".format(state.project_path))
            continue

        try:
            conn = sqlite3.connect(db_path)
            try:
                # Check schema version
                try:
                    row = conn.execute(
                        "SELECT MAX(version) FROM schema_version"
                    ).fetchone()
                    sv = row[0] if row and row[0] is not None else 0
                except sqlite3.OperationalError:
                    sv = 0

                # Check embedding dim
                edim = _detect_embedding_dim(conn)
            finally:
                conn.close()

            ok = True
            parts = []

            if state.needs_schema_migration:
                if sv >= CURRENT_SCHEMA_VERSION:
                    parts.append("schema=v{} OK".format(sv))
                else:
                    parts.append("schema=v{} EXPECTED v{}".format(sv, CURRENT_SCHEMA_VERSION))
                    ok = False

            if state.needs_embedding_migration:
                if edim is None or edim == CURRENT_EMBEDDING_DIM:
                    parts.append("embedding_dim={} OK".format(edim))
                else:
                    parts.append("embedding_dim={} EXPECTED {}".format(edim, CURRENT_EMBEDDING_DIM))
                    ok = False

            status = "PASS" if ok else "FAIL"
            messages.append("[{}] {} — {}".format(
                state.project_path, status, ", ".join(parts) if parts else "no checks needed"
            ))

        except Exception as exc:
            messages.append("[{}] Validation error: {}".format(state.project_path, exc))

    return messages


def reenable_mcp(project_dirs: List[str]) -> List[str]:
    """
    Re-enable MCP server entries that were disabled during Phase 1.

    Renames `_DISABLED` keys back to their original names.
    Returns list of action descriptions.
    """
    actions: List[str] = []
    for mf in find_mcp_json_files(project_dirs):
        try:
            data = json.loads(mf.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue

        servers = data.get("mcpServers", {})
        disabled_keys = [k for k in servers if "_DISABLED" in k and "daem0n" in k.lower()]
        if disabled_keys:
            for k in disabled_keys:
                original_key = k.replace("_DISABLED", "")
                servers[original_key] = servers.pop(k)
            data["mcpServers"] = servers
            mf.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
            actions.append("Re-enabled MCP server(s) in {}".format(mf))

    return actions


def print_summary(
    states: List[ProjectState],
    validation_messages: List[str],
    phase2_output: str,
    mcp_actions: List[str],
) -> None:
    """Print a formatted summary report of all upgrade actions."""
    print("\n" + "=" * 60)
    print("  UPGRADE SUMMARY")
    print("=" * 60)

    for state in states:
        print("\n  Project: {}".format(state.project_path))
        fp = state.fingerprint
        print("    Detected version: {}".format(fp.estimated_version))
        print("    Schema: v{}  |  Embedding dim: {}  |  Memories: {}  |  Vectors: {}".format(
            fp.schema_version, fp.embedding_dim, fp.memory_count, fp.vector_count
        ))

        if state.skipped:
            print("    Status: Already up to date")
            continue

        if state.actions_taken:
            print("    Actions:")
            for action in state.actions_taken:
                print("      - {}".format(action))

        if state.errors:
            print("    ERRORS:")
            for err in state.errors:
                print("      ! {}".format(err))

    if phase2_output:
        print("\n  Package install: OK")

    if mcp_actions:
        print("\n  MCP servers:")
        for a in mcp_actions:
            print("    - {}".format(a))

    if validation_messages:
        print("\n  Validation:")
        for msg in validation_messages:
            print("    {}".format(msg))

    has_errors = any(s.errors for s in states)
    print("\n" + "=" * 60)
    if has_errors:
        print("  COMPLETED WITH ERRORS — check above for details")
    else:
        print("  COMPLETED SUCCESSFULLY")
    print("=" * 60 + "\n")


# ===================================================================
# Rollback
# ===================================================================

def rollback(project_dirs: List[str]) -> bool:
    """
    Undo the last upgrade by restoring files from backup manifests.

    Returns True if any rollback was performed.
    """
    rolled_back = False

    for project_path in project_dirs:
        backup_root = os.path.join(project_path, BACKUP_DIR_NAME)
        if not os.path.isdir(backup_root):
            print("  No backups found in {}".format(project_path))
            continue

        # Find the most recent backup (by timestamp directory name)
        timestamps = sorted(os.listdir(backup_root), reverse=True)
        if not timestamps:
            print("  Empty backup directory in {}".format(project_path))
            continue

        latest = timestamps[0]
        manifest_path = os.path.join(backup_root, latest, MANIFEST_FILE)
        if not os.path.exists(manifest_path):
            print("  No manifest found in {}".format(
                os.path.join(backup_root, latest)
            ))
            continue

        try:
            with open(manifest_path, "r", encoding="utf-8") as f:
                manifest = json.load(f)
        except (json.JSONDecodeError, OSError) as exc:
            print("  Cannot read manifest: {}".format(exc))
            continue

        print("\n  Rolling back {} (backup from {})".format(project_path, latest))
        files_section = manifest.get("files", {})

        for key, entry in files_section.items():
            original = entry.get("original", "")
            backup = entry.get("backup", "")
            if not original or not backup:
                continue
            if not os.path.exists(backup):
                print("    SKIP {} — backup file missing".format(key))
                continue

            try:
                if key == "database":
                    # Restore database via sqlite3 backup API
                    src = sqlite3.connect(backup)
                    dst = sqlite3.connect(original)
                    try:
                        src.backup(dst)
                    finally:
                        dst.close()
                        src.close()
                elif key == "qdrant":
                    # Restore qdrant directory
                    if os.path.isdir(original):
                        shutil.rmtree(original)
                    shutil.copytree(backup, original)
                else:
                    # Regular file copy
                    shutil.copy2(backup, original)
                print("    RESTORED {}".format(original))
                rolled_back = True
            except Exception as exc:
                print("    FAILED to restore {}: {}".format(original, exc))

    # Re-enable MCP servers that may have been disabled
    if rolled_back:
        mcp_actions = reenable_mcp(project_dirs)
        for a in mcp_actions:
            print("    {}".format(a))

    return rolled_back


# ===================================================================
# Dry-run report
# ===================================================================

def print_dry_run_report(states: List[ProjectState]) -> None:
    """Print assessment without making any changes."""
    print("\n" + "=" * 60)
    print("  UPGRADE ASSESSMENT (dry-run)")
    print("=" * 60)

    for state in states:
        fp = state.fingerprint
        print("\n  Project: {}".format(state.project_path))
        print("    DB path:          {}".format(fp.db_path or "(none)"))
        print("    Schema version:   {}".format(fp.schema_version))
        print("    Embedding dim:    {}".format(fp.embedding_dim))
        print("    Memories:         {}".format(fp.memory_count))
        print("    Vectors:          {}".format(fp.vector_count))
        print("    Qdrant:           {}".format("yes" if fp.has_qdrant else "no"))
        print("    Hook format:      {}".format(fp.hook_format))
        print("    Estimated ver:    {}".format(fp.estimated_version))

        if state.skipped:
            print("    --> Already current. No upgrade needed.")
        else:
            print("    --> Upgrade plan:")
            if state.needs_fresh_start:
                print("        - Fresh start (pre-v4 detected)")
            if state.needs_schema_migration:
                print("        - Schema migration (v{} -> v{})".format(
                    fp.schema_version, CURRENT_SCHEMA_VERSION
                ))
            if state.needs_embedding_migration:
                print("        - Embedding re-encoding ({} -> {} dim, {} vectors)".format(
                    fp.embedding_dim, CURRENT_EMBEDDING_DIM, fp.vector_count
                ))
            if state.needs_hook_install:
                print("        - Hook reinstallation")

    print("\n  Run with --apply to perform the upgrade.")
    print("=" * 60 + "\n")


# ===================================================================
# Main
# ===================================================================

def main() -> int:
    parser = argparse.ArgumentParser(
        description="Upgrade Daem0n-MCP installations to the current version."
    )
    parser.add_argument(
        "--apply", action="store_true",
        help="Perform the upgrade (default is dry-run assessment only)",
    )
    parser.add_argument(
        "--yes", "-y", action="store_true",
        help="Skip confirmation prompts",
    )
    parser.add_argument(
        "--rollback", action="store_true",
        help="Undo the last upgrade from backups",
    )
    parser.add_argument(
        "--project", action="append", default=[],
        help="Include specific project directory (repeatable)",
    )
    parser.add_argument(
        "--skip-embeddings", action="store_true",
        help="Skip vector embedding re-encoding",
    )
    parser.add_argument(
        "--no-install", action="store_true",
        help="Skip the pip install step",
    )

    args = parser.parse_args()
    script_dir = os.path.dirname(os.path.abspath(__file__))

    # --- Discover projects ---
    project_dirs = discover_projects(args.project)
    if not project_dirs:
        print("No projects found. Use --project /path or run from a project directory.")
        return 1

    print("Discovered {} project(s):".format(len(project_dirs)))
    for d in project_dirs:
        print("  - {}".format(d))

    # --- Rollback mode ---
    if args.rollback:
        print("\nRolling back last upgrade...")
        if rollback(project_dirs):
            print("\nRollback complete.")
            return 0
        else:
            print("\nNothing to roll back.")
            return 1

    # --- Phase 1: Assess ---
    print("\n--- Phase 1: Assessment ---")
    states: List[ProjectState] = []
    for d in project_dirs:
        fp = detect_version(d)
        state = ProjectState(d, fp)
        state.classify()
        states.append(state)

    # Dry-run: just report and exit
    if not args.apply:
        print_dry_run_report(states)
        return 0

    # Check if anything needs upgrading
    if all(s.skipped for s in states):
        print("\nAll projects are already at the current version. Nothing to do.")
        return 0

    # Confirm
    if not args.yes:
        print_dry_run_report(states)
        answer = input("Proceed with upgrade? [y/N] ").strip().lower()
        if answer not in ("y", "yes"):
            print("Aborted.")
            return 1

    # --- Phase 1 continued: Backup & safe mode ---
    print("\n--- Phase 1: Backup & Safe Mode ---")
    for state in states:
        if state.skipped:
            continue
        if not backup_project(state):
            print("  FATAL: Backup failed for {} — aborting.".format(state.project_path))
            return 1
        print("  Backed up: {}".format(state.project_path))

    disable_actions = disable_hooks_and_mcp(project_dirs)
    for a in disable_actions:
        print("  {}".format(a))

    # --- Phase 2: Install ---
    phase2_output = ""
    if not args.no_install:
        print("\n--- Phase 2: Install ---")
        mode = detect_install_mode(script_dir)
        print("  Install mode: {}".format(mode))

        success, output = install_package(mode, script_dir)
        if not success:
            print("  FATAL: {}".format(output))
            print("  Use --rollback to restore from backups.")
            return 1
        phase2_output = output
        print("  Package installed successfully.")

        ok, version_or_error = verify_package_import()
        if not ok:
            print("  FATAL: Package import verification failed: {}".format(version_or_error))
            print("  Use --rollback to restore from backups.")
            return 1
        print("  Verified: daem0nmcp v{}".format(version_or_error))
    else:
        print("\n--- Phase 2: Install (skipped via --no-install) ---")

    # --- Phase 3: Migrate ---
    print("\n--- Phase 3: Migrate ---")
    run_phase3(states, args.skip_embeddings, args.yes)
    for state in states:
        if state.errors:
            for err in state.errors:
                print("  ERROR [{}]: {}".format(state.project_path, err))

    # --- Phase 4: Validate & Re-enable ---
    print("\n--- Phase 4: Validate & Re-enable ---")
    validation_messages = validate_upgrade(states)
    mcp_actions = reenable_mcp(project_dirs)

    print_summary(states, validation_messages, phase2_output, mcp_actions)

    has_errors = any(s.errors for s in states)
    return 1 if has_errors else 0


if __name__ == "__main__":
    sys.exit(main())
