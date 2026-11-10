"""Deprecated format-6 embedding re-encoder.

Usage: python -m daem0nmcp.migrations.migrate_embedding_model [--project-path PATH]

Architecture format 7 stores retrieval vectors in rebuildable projections.  This
legacy writer therefore refuses every format-7 active database.

This script:
1. Loads all memories with vector_embedding IS NOT NULL from SQLite
2. Re-encodes each with vectors.encode_document(content + rationale)
3. Updates SQLite vector_embedding column
4. Upserts to Qdrant (collections already recreated with new dim on startup)
5. Reports progress and statistics
"""

import argparse
import logging
import os
import sqlite3
import sys
import time
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)


_V6_ONLY_ERROR = (
    "Deprecated v6-only vector migration cannot run against format 7; use "
    "`python -m daem0nmcp.cli rebuild-projection --projection dense "
    "--workspace-id <workspace-id>` instead."
)


def _resolve_db_selection(project_path: str) -> tuple[str, int, Path | None]:
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

    for storage in storage_candidates:
        pointer = storage / "active-db.json"
        if pointer.exists() or pointer.is_symlink():
            resolved = resolve_active_database(storage)
            return str(resolved.path), resolved.format_version, storage
    for storage in storage_candidates:
        path = storage / "daem0nmcp.db"
        if path.is_file():
            resolved = resolve_active_database(storage)
            format_version = (
                7 if has_canonical_v7_state(resolved.path) else resolved.format_version
            )
            return str(resolved.path), format_version, storage
    return "", 0, None


def _resolve_db_path(project_path: str) -> str:
    """Return the authoritative database path for compatibility callers."""

    return _resolve_db_selection(project_path)[0]


def main():
    parser = argparse.ArgumentParser(
        description="Re-encode memory embeddings for new model"
    )
    parser.add_argument(
        "--project-path",
        default=os.getcwd(),
        help="Project root, .daem0nmcp dir, or storage dir (default: cwd)",
    )
    parser.add_argument("--batch-size", type=int, default=100, help="Commit batch size")
    args = parser.parse_args()

    db_path, _, storage = _resolve_db_selection(args.project_path)
    if not db_path:
        logger.error(
            f"Database not found. Searched from: {args.project_path}\n"
            f"  Tried: <path>/.daem0nmcp/storage/daem0nmcp.db\n"
            f"         <path>/storage/daem0nmcp.db\n"
            f"         <path>/daem0nmcp.db"
        )
        sys.exit(1)

    from daem0nmcp.storage_activation import DatabaseFileLock

    assert storage is not None
    with DatabaseFileLock(storage, "shared"):
        db_path, format_version, locked_storage = _resolve_db_selection(
            args.project_path
        )
        if locked_storage is None or locked_storage.resolve() != storage.resolve():
            logger.error("Active database storage changed during migration.")
            sys.exit(1)
        if format_version != 6:
            logger.error(_V6_ONLY_ERROR)
            sys.exit(1)
        return _migrate_v6_database(db_path, args.batch_size)


def _migrate_v6_database(db_path: str, batch_size: int) -> None:
    """Perform the format-6 write while the caller holds the storage lock."""

    logger.info(f"Using database: {db_path}")

    # Import after arg parsing to avoid slow imports on --help
    from daem0nmcp import vectors
    from daem0nmcp.qdrant_store import QdrantVectorStore

    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()

    # Count total memories with embeddings
    cursor.execute("SELECT COUNT(*) FROM memories WHERE vector_embedding IS NOT NULL")
    total = cursor.fetchone()[0]
    logger.info(f"Found {total} memories with embeddings to re-encode")

    if total == 0:
        logger.info("Nothing to migrate.")
        conn.close()
        return

    # Initialize Qdrant (collections auto-recreated with new dimension)
    # Derive qdrant path from wherever we found the db
    storage_dir = os.path.dirname(db_path)
    qdrant_path = os.path.join(storage_dir, "qdrant")
    qdrant = None
    if os.path.exists(qdrant_path):
        try:
            qdrant = QdrantVectorStore(path=qdrant_path)
            logger.info("Qdrant store initialized for re-indexing")
        except Exception as e:
            logger.warning(f"Could not initialize Qdrant: {e}. SQLite-only migration.")

    # Process memories in batches
    cursor.execute(
        "SELECT id, content, rationale, category, tags, file_path, worked, is_permanent "
        "FROM memories WHERE vector_embedding IS NOT NULL"
    )

    migrated = 0
    failed = 0
    start_time = time.time()

    batch_updates = []
    batch_qdrant = []

    for row in cursor:
        mem_id, content, rationale, category, tags, file_path, worked, is_permanent = (
            row
        )
        text = content
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
                    batch_qdrant.append(
                        {
                            "id": mem_id,
                            "embedding": embedding_list,
                            "metadata": {
                                "category": category,
                                "tags": tags.split(",") if tags else [],
                                "file_path": file_path,
                                "worked": worked,
                                "is_permanent": is_permanent,
                            },
                        }
                    )

            migrated += 1

            # Batch commit
            if len(batch_updates) >= batch_size:
                conn.executemany(
                    "UPDATE memories SET vector_embedding = ? WHERE id = ?",
                    batch_updates,
                )
                conn.commit()

                for item in batch_qdrant:
                    qdrant.upsert_memory(
                        item["id"], item["embedding"], item["metadata"]
                    )

                logger.info(
                    f"Progress: {migrated}/{total} ({migrated * 100 // total}%)"
                )
                batch_updates.clear()
                batch_qdrant.clear()

        except Exception as e:
            logger.error(f"Memory {mem_id}: {e}")
            failed += 1

    # Final batch
    if batch_updates:
        conn.executemany(
            "UPDATE memories SET vector_embedding = ? WHERE id = ?",
            batch_updates,
        )
        conn.commit()
        for item in batch_qdrant:
            qdrant.upsert_memory(item["id"], item["embedding"], item["metadata"])

    elapsed = time.time() - start_time
    conn.close()

    logger.info(
        f"Migration complete: {migrated} migrated, {failed} failed, {elapsed:.1f}s elapsed"
    )


if __name__ == "__main__":
    main()
