"""
Database migrations for Daem0nMCP.

Handles schema updates for existing databases.
"""

import logging
import sqlite3
from pathlib import Path

logger = logging.getLogger(__name__)

# Migration definitions: (version, description, sql_statements)
MIGRATIONS: list[tuple[int, str, list[str]]] = [
    (
        1,
        "Add vector_embedding column",
        [
            """
        ALTER TABLE memories ADD COLUMN vector_embedding BLOB;
        """
        ],
    ),
    (
        2,
        "Create FTS5 virtual table for full-text search",
        [
            """
        CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts USING fts5(
            content,
            rationale,
            tags,
            content='memories',
            content_rowid='id'
        );
        """,
            """
        INSERT OR IGNORE INTO memories_fts(rowid, content, rationale, tags)
        SELECT
            id,
            content,
            COALESCE(rationale, ''),
            COALESCE((SELECT group_concat(value, ' ') FROM json_each(tags)), '')
        FROM memories;
        """,
            """
        CREATE TRIGGER IF NOT EXISTS memories_ai AFTER INSERT ON memories BEGIN
            INSERT INTO memories_fts(rowid, content, rationale, tags)
            SELECT new.id, new.content, COALESCE(new.rationale, ''),
                   COALESCE((SELECT group_concat(value, ' ') FROM json_each(new.tags)), '');
        END;
        """,
            """
        CREATE TRIGGER IF NOT EXISTS memories_ad AFTER DELETE ON memories BEGIN
            INSERT INTO memories_fts(memories_fts, rowid, content, rationale, tags)
            SELECT 'delete', old.id, old.content, COALESCE(old.rationale, ''),
                   COALESCE((SELECT group_concat(value, ' ') FROM json_each(old.tags)), '');
        END;
        """,
            """
        CREATE TRIGGER IF NOT EXISTS memories_au AFTER UPDATE ON memories BEGIN
            INSERT INTO memories_fts(memories_fts, rowid, content, rationale, tags)
            SELECT 'delete', old.id, old.content, COALESCE(old.rationale, ''),
                   COALESCE((SELECT group_concat(value, ' ') FROM json_each(old.tags)), '');
            INSERT INTO memories_fts(rowid, content, rationale, tags)
            SELECT new.id, new.content, COALESCE(new.rationale, ''),
                   COALESCE((SELECT group_concat(value, ' ') FROM json_each(new.tags)), '');
        END;
        """,
        ],
    ),
    (
        3,
        "Add pinned and archived columns to memories",
        [
            "ALTER TABLE memories ADD COLUMN pinned BOOLEAN DEFAULT 0;",
            "ALTER TABLE memories ADD COLUMN archived BOOLEAN DEFAULT 0;",
        ],
    ),
    (
        4,
        "Add file_path_relative column to memories",
        [
            "ALTER TABLE memories ADD COLUMN file_path_relative TEXT;",
            "CREATE INDEX IF NOT EXISTS idx_memories_file_path_relative ON memories(file_path_relative);",
        ],
    ),
    (
        5,
        "Track last_modified for index freshness",
        [
            """
        CREATE TABLE IF NOT EXISTS meta (
            key TEXT PRIMARY KEY,
            value TEXT
        );
        """,
            "INSERT OR IGNORE INTO meta(key, value) VALUES('memories_last_modified', CURRENT_TIMESTAMP);",
            "INSERT OR IGNORE INTO meta(key, value) VALUES('rules_last_modified', CURRENT_TIMESTAMP);",
            """
        CREATE TRIGGER IF NOT EXISTS memories_touch_ins AFTER INSERT ON memories BEGIN
            UPDATE meta SET value = CURRENT_TIMESTAMP WHERE key = 'memories_last_modified';
        END;
        """,
            """
        CREATE TRIGGER IF NOT EXISTS memories_touch_upd AFTER UPDATE ON memories BEGIN
            UPDATE meta SET value = CURRENT_TIMESTAMP WHERE key = 'memories_last_modified';
        END;
        """,
            """
        CREATE TRIGGER IF NOT EXISTS memories_touch_del AFTER DELETE ON memories BEGIN
            UPDATE meta SET value = CURRENT_TIMESTAMP WHERE key = 'memories_last_modified';
        END;
        """,
            """
        CREATE TRIGGER IF NOT EXISTS rules_touch_ins AFTER INSERT ON rules BEGIN
            UPDATE meta SET value = CURRENT_TIMESTAMP WHERE key = 'rules_last_modified';
        END;
        """,
            """
        CREATE TRIGGER IF NOT EXISTS rules_touch_upd AFTER UPDATE ON rules BEGIN
            UPDATE meta SET value = CURRENT_TIMESTAMP WHERE key = 'rules_last_modified';
        END;
        """,
            """
        CREATE TRIGGER IF NOT EXISTS rules_touch_del AFTER DELETE ON rules BEGIN
            UPDATE meta SET value = CURRENT_TIMESTAMP WHERE key = 'rules_last_modified';
        END;
        """,
        ],
    ),
    (
        6,
        "Add recall_count column for saliency-based pruning",
        ["ALTER TABLE memories ADD COLUMN recall_count INTEGER DEFAULT 0;"],
    ),
    (
        7,
        "Add memory_relationships table for graph edges",
        [
            """
        CREATE TABLE IF NOT EXISTS memory_relationships (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_id INTEGER NOT NULL,
            target_id INTEGER NOT NULL,
            relationship TEXT NOT NULL,
            description TEXT,
            confidence REAL DEFAULT 1.0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (source_id) REFERENCES memories(id) ON DELETE CASCADE,
            FOREIGN KEY (target_id) REFERENCES memories(id) ON DELETE CASCADE
        );
        """,
            "CREATE INDEX IF NOT EXISTS idx_relationships_source ON memory_relationships(source_id);",
            "CREATE INDEX IF NOT EXISTS idx_relationships_target ON memory_relationships(target_id);",
            "CREATE INDEX IF NOT EXISTS idx_relationships_type ON memory_relationships(relationship);",
        ],
    ),
    (
        8,
        "Add session_state and enforcement_bypass_log tables",
        [
            """
        CREATE TABLE IF NOT EXISTS session_state (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL UNIQUE,
            project_path TEXT NOT NULL,
            briefed INTEGER DEFAULT 0,
            context_checks TEXT DEFAULT '[]',
            pending_decisions TEXT DEFAULT '[]',
            last_activity TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        """,
            "CREATE INDEX IF NOT EXISTS idx_session_state_session_id ON session_state(session_id);",
            """
        CREATE TABLE IF NOT EXISTS enforcement_bypass_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            pending_decisions TEXT DEFAULT '[]',
            staged_files_with_warnings TEXT DEFAULT '[]',
            reason TEXT
        );
        """,
        ],
    ),
    (
        9,
        "Add code_entities and memory_code_refs tables for Phase 2",
        [
            """
        CREATE TABLE IF NOT EXISTS code_entities (
            id TEXT PRIMARY KEY,
            project_path TEXT NOT NULL,
            entity_type TEXT NOT NULL,
            name TEXT NOT NULL,
            qualified_name TEXT,
            file_path TEXT NOT NULL,
            line_start INTEGER,
            line_end INTEGER,
            signature TEXT,
            docstring TEXT,
            calls TEXT DEFAULT '[]',
            called_by TEXT DEFAULT '[]',
            imports TEXT DEFAULT '[]',
            inherits TEXT DEFAULT '[]',
            indexed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        """,
            "CREATE INDEX IF NOT EXISTS idx_code_entities_project ON code_entities(project_path);",
            "CREATE INDEX IF NOT EXISTS idx_code_entities_file ON code_entities(file_path);",
            "CREATE INDEX IF NOT EXISTS idx_code_entities_name ON code_entities(name);",
            "CREATE INDEX IF NOT EXISTS idx_code_entities_type ON code_entities(entity_type);",
            """
        CREATE TABLE IF NOT EXISTS memory_code_refs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            memory_id INTEGER,
            code_entity_id TEXT,
            entity_type TEXT,
            entity_name TEXT,
            file_path TEXT,
            line_number INTEGER,
            relationship TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (memory_id) REFERENCES memories(id) ON DELETE CASCADE
        );
        """,
            "CREATE INDEX IF NOT EXISTS idx_memory_code_refs_memory ON memory_code_refs(memory_id);",
            "CREATE INDEX IF NOT EXISTS idx_memory_code_refs_entity ON memory_code_refs(code_entity_id);",
        ],
    ),
    (
        10,
        "Add project_links table for cross-repo awareness",
        [
            """
        CREATE TABLE IF NOT EXISTS project_links (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_path TEXT NOT NULL,
            linked_path TEXT NOT NULL,
            relationship TEXT DEFAULT 'related',
            label TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        """,
            "CREATE INDEX IF NOT EXISTS idx_project_links_source ON project_links(source_path);",
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_project_links_unique ON project_links(source_path, linked_path);",
        ],
    ),
    (
        11,
        "Add file_hashes table for incremental indexing",
        [
            """
        CREATE TABLE IF NOT EXISTS file_hashes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            project_path TEXT NOT NULL,
            file_path TEXT NOT NULL,
            content_hash TEXT NOT NULL,
            indexed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(project_path, file_path)
        );
        """,
            "CREATE INDEX IF NOT EXISTS idx_file_hashes_project ON file_hashes(project_path);",
        ],
    ),
    (
        12,
        "Add surprise_score and importance_score columns to memories",
        [
            "ALTER TABLE memories ADD COLUMN surprise_score REAL;",
            "ALTER TABLE memories ADD COLUMN importance_score REAL;",
        ],
    ),
    (
        13,
        "Add facts table for static knowledge (Engram-inspired)",
        [
            """
        CREATE TABLE IF NOT EXISTS facts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            content_hash TEXT NOT NULL UNIQUE,
            content TEXT NOT NULL,
            category TEXT,
            source_memory_id INTEGER,
            verification_count INTEGER DEFAULT 0,
            is_verified BOOLEAN DEFAULT 0,
            tags TEXT DEFAULT '[]',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            verified_at TIMESTAMP,
            FOREIGN KEY (source_memory_id) REFERENCES memories(id) ON DELETE SET NULL
        );
        """,
            "CREATE INDEX IF NOT EXISTS idx_facts_content_hash ON facts(content_hash);",
            "CREATE INDEX IF NOT EXISTS idx_facts_category ON facts(category);",
        ],
    ),
    (
        14,
        "Add bi-temporal columns to memory_versions",
        [
            # Add valid_from column (when fact became true in reality)
            "ALTER TABLE memory_versions ADD COLUMN valid_from TIMESTAMP;",
            # Add valid_to column (when fact was superseded, NULL = still valid)
            "ALTER TABLE memory_versions ADD COLUMN valid_to TIMESTAMP;",
            # Add invalidated_by_version_id column (for contradiction tracking)
            "ALTER TABLE memory_versions ADD COLUMN invalidated_by_version_id INTEGER REFERENCES memory_versions(id);",
            # Backfill valid_from with changed_at for existing records (backwards compatible)
            "UPDATE memory_versions SET valid_from = changed_at WHERE valid_from IS NULL;",
            # Create temporal index for point-in-time queries (valid time dimension)
            "CREATE INDEX IF NOT EXISTS idx_memory_versions_temporal ON memory_versions(memory_id, valid_from);",
            # Create index for transaction time queries
            "CREATE INDEX IF NOT EXISTS idx_memory_versions_transaction ON memory_versions(memory_id, changed_at);",
        ],
    ),
    (
        15,
        "Add source_client and source_model columns to memories for LLM compatibility tracking",
        [
            "ALTER TABLE memories ADD COLUMN source_client TEXT;",
            "ALTER TABLE memories ADD COLUMN source_model TEXT;",
            "CREATE INDEX IF NOT EXISTS idx_memories_source_client ON memories(source_client);",
        ],
    ),
]


def get_current_version(conn: sqlite3.Connection) -> int:
    """Get current schema version from database."""
    cursor = conn.cursor()

    # Check if version table exists
    cursor.execute("""
        SELECT name FROM sqlite_master
        WHERE type='table' AND name='schema_version'
    """)

    if not cursor.fetchone():
        # Create version table
        cursor.execute("""
            CREATE TABLE schema_version (
                version INTEGER PRIMARY KEY,
                applied_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.commit()
        return 0

    cursor.execute("SELECT MAX(version) FROM schema_version")
    result = cursor.fetchone()
    return result[0] if result[0] else 0


def check_column_exists(conn: sqlite3.Connection, table: str, column: str) -> bool:
    """Check if a column exists in a table."""
    cursor = conn.cursor()
    cursor.execute(f"PRAGMA table_info({table})")
    columns = [row[1] for row in cursor.fetchall()]
    return column in columns


def run_migrations(db_path: str) -> tuple[int, list[str]]:
    """
    Run all pending migrations on the database.

    Args:
        db_path: Path to the SQLite database

    Returns:
        Tuple of (migrations_run, list of descriptions)
    """
    if not Path(db_path).exists():
        return 0, ["Database does not exist yet - will be created fresh"]

    conn = sqlite3.Connection(db_path)
    applied = []

    try:
        current_version = get_current_version(conn)

        for version, description, statements in MIGRATIONS:
            if version <= current_version:
                continue

            logger.info(f"Applying migration {version}: {description}")

            try:
                conn.execute("BEGIN")
                for sql in statements:
                    sql = sql.strip()
                    if not sql:
                        continue

                    # Handle ALTER TABLE ADD COLUMN - check if column exists first
                    if "ALTER TABLE" in sql and "ADD COLUMN" in sql:
                        # Parse table and column names
                        parts = sql.split()
                        table_idx = parts.index("TABLE") + 1
                        column_idx = parts.index("COLUMN") + 1
                        table = parts[table_idx]
                        column = parts[column_idx]

                        if check_column_exists(conn, table, column):
                            logger.info(
                                f"  Column {column} already exists in {table}, skipping"
                            )
                            continue

                    try:
                        conn.execute(sql)
                    except sqlite3.OperationalError as e:
                        # Ignore "duplicate column" errors
                        if "duplicate column" in str(e).lower():
                            logger.info("  Column already exists, skipping")
                            continue
                        raise

                # Record migration
                conn.execute(
                    "INSERT INTO schema_version (version) VALUES (?)", (version,)
                )
                conn.commit()
                applied.append(f"v{version}: {description}")
            except Exception:
                conn.rollback()
                raise

    finally:
        conn.close()

    return len(applied), applied


def migrate_and_backfill_vectors(db_path: str) -> dict:
    """
    Run migrations and optionally backfill vector embeddings for existing memories.

    Args:
        db_path: Path to the SQLite database

    Returns:
        Migration report
    """
    from .. import vectors

    # First run schema migrations
    count, applied = run_migrations(db_path)

    result = {
        "schema_migrations": count,
        "applied": applied,
        "vectors_backfilled": 0,
        "vectors_available": vectors.is_available(),
    }

    if not vectors.is_available():
        result["message"] = (
            f"Schema updated ({count} migrations). "
            "Vector backfill skipped - install sentence-transformers for vectors."
        )
        return result

    # Backfill vectors for memories that don't have them
    conn = sqlite3.Connection(db_path)
    try:
        cursor = conn.cursor()

        # Find memories without vectors
        cursor.execute("""
            SELECT id, content, rationale
            FROM memories
            WHERE vector_embedding IS NULL
        """)

        memories = cursor.fetchall()

        if not memories:
            result["message"] = (
                f"Schema updated ({count} migrations). All memories already have vectors."
            )
            return result

        logger.info(f"Backfilling vectors for {len(memories)} memories...")

        for mem_id, content, rationale in memories:
            text = content
            if rationale:
                text += " " + rationale

            embedding = vectors.encode_document(text)
            if embedding:
                cursor.execute(
                    "UPDATE memories SET vector_embedding = ? WHERE id = ?",
                    (embedding, mem_id),
                )
                result["vectors_backfilled"] += 1

        conn.commit()

        result["message"] = (
            f"Schema updated ({count} migrations). "
            f"Backfilled vectors for {result['vectors_backfilled']} memories."
        )

    finally:
        conn.close()

    return result


# CLI entry point
def main():
    """Run migrations from command line."""
    from ..config import settings

    db_path = str(settings.get_storage_path())
    print(f"Migrating database: {db_path}")

    result = migrate_and_backfill_vectors(db_path)

    print("\nMigration complete:")
    print(f"  Schema migrations: {result['schema_migrations']}")
    for m in result.get("applied", []):
        print(f"    - {m}")
    print(f"  Vectors backfilled: {result['vectors_backfilled']}")
    print(f"  Vectors available: {result['vectors_available']}")
    print(f"\n{result['message']}")

    return 0


if __name__ == "__main__":
    import sys

    sys.exit(main())
