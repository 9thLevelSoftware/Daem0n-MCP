"""Dependency-free architecture-format 7 schema compatibility floor."""

CURRENT_SCHEMA_VERSION = 32
REQUIRED_V7_SCHEMA_VERSIONS = frozenset(range(16, CURRENT_SCHEMA_VERSION + 1))


__all__ = ["CURRENT_SCHEMA_VERSION", "REQUIRED_V7_SCHEMA_VERSIONS"]
