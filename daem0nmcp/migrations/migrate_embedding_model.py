"""
Re-encode all memory embeddings with the new embedding model.

Usage: python -m daem0nmcp.migrations.migrate_embedding_model [--project-path PATH]

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

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)


def _resolve_db_path(project_path: str) -> str:
    """
    Find daem0nmcp.db from a project path, accepting any of:
      - /path/to/project                 (.daem0nmcp/storage/daem0nmcp.db)
      - /path/to/project/.daem0nmcp      (storage/daem0nmcp.db)
      - /path/to/project/.daem0nmcp/storage  (daem0nmcp.db)
    """
    candidates = [
        os.path.join(project_path, ".daem0nmcp", "storage", "daem0nmcp.db"),
        os.path.join(project_path, "storage", "daem0nmcp.db"),
        os.path.join(project_path, "daem0nmcp.db"),
    ]
    for path in candidates:
        if os.path.exists(path):
            return path
    return ""


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

    db_path = _resolve_db_path(args.project_path)
    if not db_path:
        logger.error(
            f"Database not found. Searched from: {args.project_path}\n"
            f"  Tried: <path>/.daem0nmcp/storage/daem0nmcp.db\n"
            f"         <path>/storage/daem0nmcp.db\n"
            f"         <path>/daem0nmcp.db"
        )
        sys.exit(1)

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
            if len(batch_updates) >= args.batch_size:
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
