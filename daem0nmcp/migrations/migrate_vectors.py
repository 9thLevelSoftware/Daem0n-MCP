"""
SQLite to Qdrant Vector Migration.

One-time migration script to transfer vector embeddings from SQLite's
vector_embedding column to Qdrant vector store.

Usage:
    python -m daem0nmcp.migrations.migrate_vectors [--project-path PATH]

The migration is idempotent - running it multiple times is safe and will
only migrate vectors that aren't already in Qdrant.
"""

import argparse
import asyncio
import logging
import sys
from collections.abc import Callable
from pathlib import Path

from sqlalchemy import select

from daem0nmcp import vectors
from daem0nmcp.config import Settings, settings
from daem0nmcp.database import DatabaseManager
from daem0nmcp.models import Memory
from daem0nmcp.qdrant_store import QdrantVectorStore

logger = logging.getLogger(__name__)


async def migrate_vectors_to_qdrant(
    db: DatabaseManager,
    qdrant: QdrantVectorStore,
    batch_size: int = 100,
    progress_callback: Callable[[int, int], None] | None = None,
) -> dict:
    """
    One-time migration of existing vectors from SQLite to Qdrant.

    Args:
        db: Initialized DatabaseManager instance.
        qdrant: Initialized QdrantVectorStore instance.
        batch_size: Number of memories to process before reporting progress.
        progress_callback: Optional callback(current, total) for progress reporting.

    Returns:
        Dictionary with migration statistics:
        - migrated: Number of memories successfully migrated
        - skipped: Number of memories skipped (already in Qdrant or no embedding)
        - failed: Number of memories that failed to migrate
        - total: Total memories processed
        - errors: List of error messages for failed migrations
    """
    result = {"migrated": 0, "skipped": 0, "failed": 0, "total": 0, "errors": []}

    # Ensure database is initialized
    await db.init_db()

    async with db.get_session() as session:
        # Query all memories with vector embeddings
        query = select(Memory).where(Memory.vector_embedding.isnot(None))
        query_result = await session.execute(query)
        memories = query_result.scalars().all()

        result["total"] = len(memories)

        if result["total"] == 0:
            logger.info("No memories with vector embeddings found in SQLite.")
            return result

        logger.info(f"Found {result['total']} memories with vectors to migrate.")

        # Check if Qdrant already has vectors (for logging purposes)
        try:
            qdrant_count = qdrant.get_count()
            if qdrant_count > 0:
                logger.info(
                    f"Qdrant already has {qdrant_count} vectors. Will skip existing."
                )
        except Exception as e:
            logger.debug(f"Could not check Qdrant count: {e}")

        for i, mem in enumerate(memories):
            try:
                # Decode the vector embedding from packed bytes
                embedding = vectors.decode(mem.vector_embedding)

                if not embedding:
                    logger.debug(f"Memory {mem.id}: No valid embedding, skipping")
                    result["skipped"] += 1
                    continue

                # Validate embedding dimensions
                if len(embedding) != settings.embedding_dimension:
                    error_msg = f"Memory {mem.id}: Invalid embedding dimension {len(embedding)}, expected {settings.embedding_dimension}"
                    logger.warning(error_msg)
                    result["failed"] += 1
                    result["errors"].append(error_msg)
                    continue

                # Prepare metadata payload
                metadata = {
                    "category": mem.category,
                    "tags": mem.tags or [],
                    "file_path": mem.file_path,
                    "worked": mem.worked,
                    "is_permanent": mem.is_permanent,
                }

                # Upsert to Qdrant (idempotent - safe to run multiple times)
                qdrant.upsert_memory(
                    memory_id=mem.id, embedding=embedding, metadata=metadata
                )
                result["migrated"] += 1

            except Exception as e:
                error_msg = f"Memory {mem.id}: {str(e)}"
                logger.warning(f"Failed to migrate memory: {error_msg}")
                result["errors"].append(error_msg)
                result["failed"] += 1

            # Progress reporting
            if progress_callback and (i + 1) % batch_size == 0:
                progress_callback(i + 1, result["total"])

        # Final progress report
        if progress_callback:
            progress_callback(result["total"], result["total"])

    return result


async def run_migration(project_path: str | None = None) -> dict:
    """
    Run the vector migration with proper initialization.

    Args:
        project_path: Path to project root. Uses current directory if not specified.

    Returns:
        Migration result dictionary.
    """
    # Resolve project path
    if project_path:
        project_dir = Path(project_path).resolve()
    else:
        project_dir = Path.cwd()

    logger.info(f"Running vector migration for project: {project_dir}")

    # Initialize settings with project path
    settings = Settings(project_root=str(project_dir))
    storage_path = settings.get_storage_path()
    qdrant_path = settings.get_qdrant_path()

    logger.info(f"SQLite storage path: {storage_path}")
    logger.info(f"Qdrant storage path: {qdrant_path}")

    # Initialize database and Qdrant
    db = DatabaseManager(storage_path=storage_path)
    qdrant = QdrantVectorStore(path=qdrant_path)

    def progress_reporter(current: int, total: int):
        percent = (current / total) * 100 if total > 0 else 0
        logger.info(f"Migration progress: {current}/{total} ({percent:.1f}%)")

    try:
        result = await migrate_vectors_to_qdrant(
            db=db, qdrant=qdrant, progress_callback=progress_reporter
        )
        return result
    finally:
        await db.close()
        qdrant.close()


def main():
    """CLI entry point for the migration script."""
    parser = argparse.ArgumentParser(
        description="Migrate vector embeddings from SQLite to Qdrant",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
    # Migrate vectors in current project
    python -m daem0nmcp.migrations.migrate_vectors

    # Migrate vectors for a specific project
    python -m daem0nmcp.migrations.migrate_vectors --project-path /path/to/project

    # Run with verbose logging
    python -m daem0nmcp.migrations.migrate_vectors --verbose
        """,
    )
    parser.add_argument(
        "--project-path",
        "-p",
        help="Path to project root (defaults to current directory)",
        default=None,
    )
    parser.add_argument(
        "--verbose", "-v", action="store_true", help="Enable verbose logging"
    )

    args = parser.parse_args()

    # Configure logging
    log_level = logging.DEBUG if args.verbose else logging.INFO
    logging.basicConfig(
        level=log_level, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
    )

    print("\n" + "=" * 60)
    print("Daem0nMCP Vector Migration: SQLite -> Qdrant")
    print("=" * 60 + "\n")

    try:
        result = asyncio.run(run_migration(args.project_path))

        print("\n" + "-" * 40)
        print("Migration Complete!")
        print("-" * 40)
        print(f"  Total memories processed: {result['total']}")
        print(f"  Successfully migrated:    {result['migrated']}")
        print(f"  Skipped (no embedding):   {result['skipped']}")
        print(f"  Failed:                   {result['failed']}")

        if result["errors"]:
            print("\nErrors encountered:")
            for error in result["errors"][:10]:  # Show first 10 errors
                print(f"  - {error}")
            if len(result["errors"]) > 10:
                print(f"  ... and {len(result['errors']) - 10} more errors")

        if result["failed"] == 0 and result["migrated"] > 0:
            print("\nMigration completed successfully!")
            return 0
        elif result["total"] == 0:
            print(
                "\nNo vectors to migrate (database may be empty or have no embeddings)."
            )
            return 0
        elif result["failed"] > 0:
            print("\nMigration completed with errors. Review the errors above.")
            return 1
        else:
            print("\nNo new vectors migrated (may already be in Qdrant).")
            return 0

    except KeyboardInterrupt:
        print("\nMigration cancelled by user.")
        return 130
    except RuntimeError as e:
        error_str = str(e)
        if "already accessed by another instance" in error_str:
            print("\nMigration failed: Qdrant storage is locked.")
            print("\nThis typically means the Daem0nMCP server is running.")
            print("To run the migration:")
            print("  1. Stop the Daem0nMCP MCP server (close Claude Desktop or IDE)")
            print("  2. Run this migration script again")
            print("  3. Restart the Daem0nMCP server")
            print("\nAlternatively, if you're using Qdrant server mode,")
            print("set DAEM0NMCP_QDRANT_URL to your Qdrant server address.")
        else:
            logger.exception("Migration failed with error")
            print(f"\nMigration failed: {e}")
        return 1
    except Exception as e:
        logger.exception("Migration failed with error")
        print(f"\nMigration failed: {e}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
