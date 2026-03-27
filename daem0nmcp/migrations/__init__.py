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
# Export vector migration function
from .migrate_vectors import migrate_vectors_to_qdrant
from .schema import MIGRATIONS, migrate_and_backfill_vectors, run_migrations

__all__ = [
    "run_migrations",
    "migrate_and_backfill_vectors",
    "MIGRATIONS",
    "migrate_vectors_to_qdrant",
]
