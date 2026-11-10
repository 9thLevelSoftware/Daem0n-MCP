"""Deprecated format-6 SQLite-to-Qdrant vector migration.

One-time migration script to transfer vector embeddings from SQLite's
vector_embedding column to Qdrant vector store.

Usage:
    python -m daem0nmcp.migrations.migrate_vectors [--project-path PATH]

The migration is idempotent for architecture format 6.  Architecture format 7
uses rebuildable dense projections and is refused before optional vector,
database, or Qdrant dependencies are imported.
"""

import argparse
import asyncio
import logging
import sys
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from daem0nmcp.database import DatabaseManager
    from daem0nmcp.qdrant_store import QdrantVectorStore

logger = logging.getLogger(__name__)

_V6_ONLY_ERROR = (
    "Deprecated v6-only vector migration cannot run against format 7; use "
    "`python -m daem0nmcp.cli rebuild-projection --projection dense "
    "--workspace-id <workspace-id>` instead."
)


def _require_v6(format_version: int) -> None:
    if format_version != 6:
        raise RuntimeError(_V6_ONLY_ERROR)


async def migrate_vectors_to_qdrant(
    db: "DatabaseManager",
    qdrant: "QdrantVectorStore",
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
    _require_v6(getattr(db, "format_version", 0))

    from sqlalchemy import select

    from daem0nmcp import vectors
    from daem0nmcp.config import settings
    from daem0nmcp.models import Memory

    result = {"migrated": 0, "skipped": 0, "failed": 0, "total": 0, "errors": []}

    # This deprecated copier must not publish v7 schema into the source before
    # reading it.  The dedicated initializer caps migrations at the last v6
    # schema and deliberately avoids current-model ``create_all``.
    await db.init_legacy_v6()

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
    from daem0nmcp.config import Settings
    from daem0nmcp.storage_activation import (
        DatabaseFileLock,
        has_canonical_v7_state,
        resolve_active_database,
    )

    # Resolve and authenticate the active database before constructing either
    # legacy database machinery or a Qdrant client.
    project_dir = Path(project_path).resolve() if project_path else Path.cwd()

    logger.info(f"Running vector migration for project: {project_dir}")

    # Initialize settings with project path
    project_settings = Settings(project_root=str(project_dir))
    storage_path = project_settings.get_storage_path()
    with DatabaseFileLock(storage_path, "shared"):
        active = resolve_active_database(storage_path)
        active_format = (
            7
            if active.pointer is None and has_canonical_v7_state(active.path)
            else active.format_version
        )
        _require_v6(active_format)

        from daem0nmcp.database import DatabaseManager

        db = DatabaseManager(storage_path=storage_path)
        try:
            # Recheck the manager's authoritative selection before even resolving
            # or constructing the legacy Qdrant target.
            _require_v6(db.format_version)
            qdrant_path = project_settings.get_qdrant_path()
            logger.info(f"SQLite storage path: {storage_path}")
            logger.info(f"Qdrant storage path: {qdrant_path}")

            from daem0nmcp.qdrant_store import QdrantVectorStore

            qdrant = QdrantVectorStore(path=qdrant_path)
            try:

                def progress_reporter(current: int, total: int):
                    percent = (current / total) * 100 if total > 0 else 0
                    logger.info(
                        f"Migration progress: {current}/{total} ({percent:.1f}%)"
                    )

                return await migrate_vectors_to_qdrant(
                    db=db, qdrant=qdrant, progress_callback=progress_reporter
                )
            finally:
                qdrant.close()
        finally:
            await db.close()


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
