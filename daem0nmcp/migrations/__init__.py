"""
Daem0nMCP Migrations Package.

This package contains:
- Schema migrations for SQLite database updates (from original migrations.py)
- Data migration scripts for upgrading storage backends

Available migrations:
- run_migrations: Run SQLite schema migrations
- migrate_vectors_to_qdrant: Migrate vector embeddings from SQLite to Qdrant
"""

# Re-export schema migration functions from the original migrations module
# The original migrations.py was renamed to schema.py to avoid module name conflicts
from .schema import (
    CURRENT_SCHEMA_VERSION,
    MIGRATIONS,
    backfill_retained_public_object_ids,
    migrate_and_backfill_vectors,
    run_migrations,
)
from .v7 import MigrationResult, MigrationV7Error, MigrationV7Service


async def migrate_vectors_to_qdrant(*args, **kwargs):
    """Lazy compatibility export; v7 offline migration must not import models."""
    from .migrate_vectors import migrate_vectors_to_qdrant as implementation

    return await implementation(*args, **kwargs)


__all__ = [
    "run_migrations",
    "migrate_and_backfill_vectors",
    "MIGRATIONS",
    "CURRENT_SCHEMA_VERSION",
    "backfill_retained_public_object_ids",
    "migrate_vectors_to_qdrant",
    "MigrationResult",
    "MigrationV7Error",
    "MigrationV7Service",
]
