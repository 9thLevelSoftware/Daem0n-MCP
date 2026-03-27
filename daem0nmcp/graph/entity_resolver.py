"""Entity resolution and canonicalization for knowledge graph."""

import logging
import re

from sqlalchemy import func, select

from ..database import DatabaseManager
from ..models import ExtractedEntity

logger = logging.getLogger(__name__)


class EntityResolver:
    """
    Canonicalizes entities to prevent duplicates and enable merging.

    Uses type+normalized_name as the uniqueness key.
    Maintains an in-memory cache for fast lookups during batch processing.
    """

    def __init__(self, db: DatabaseManager):
        self.db = db
        self._canonical_cache: dict[
            str, int
        ] = {}  # "project:type:normalized_name" -> entity_id
        self._loaded_projects: set = set()  # Track which projects have been loaded

    def normalize(self, name: str, entity_type: str) -> str:
        """
        Normalize entity name for comparison.

        Type-specific rules:
        - function: snake_case and camelCase to common form (lowercase with underscores)
        - class: lowercase for matching (preserve original for display)
        - file: normalize path separators to forward slash
        - module: lowercase, normalize dots
        - variable: lowercase
        - concept: lowercase, strip quotes
        """
        if not name:
            return ""

        # Start with basic normalization
        normalized = name.strip()

        if entity_type == "function":
            # Convert camelCase to snake_case, then lowercase
            normalized = re.sub(r"([a-z])([A-Z])", r"\1_\2", normalized)
            normalized = normalized.lower()
        elif entity_type == "class":
            # Just lowercase for matching
            normalized = normalized.lower()
        elif entity_type == "file":
            # Normalize path separators
            normalized = normalized.replace("\\", "/")
            # Remove leading ./ if present
            if normalized.startswith("./"):
                normalized = normalized[2:]
            normalized = normalized.lower()
        elif entity_type == "module" or entity_type == "variable":
            normalized = normalized.lower()
        elif entity_type == "concept":
            # Strip quotes and lowercase
            normalized = normalized.strip("'\"")
            normalized = normalized.lower()
        else:
            normalized = normalized.lower()

        return normalized

    def _cache_key(
        self, project_path: str, entity_type: str, normalized_name: str
    ) -> str:
        """Generate cache key from project, type, and normalized name."""
        return f"{project_path}:{entity_type}:{normalized_name}"

    async def ensure_cache_loaded(self, project_path: str):
        """Load existing entities into cache for fast lookup."""
        if project_path in self._loaded_projects:
            return

        async with self.db.get_session() as session:
            result = await session.execute(
                select(ExtractedEntity).where(
                    ExtractedEntity.project_path == project_path
                )
            )
            entities = result.scalars().all()

            count = 0
            for entity in entities:
                # Use qualified_name if set, otherwise normalize the name
                normalized = entity.qualified_name or self.normalize(
                    entity.name, entity.entity_type
                )
                key = self._cache_key(project_path, entity.entity_type, normalized)
                self._canonical_cache[key] = entity.id
                count += 1

        self._loaded_projects.add(project_path)
        logger.debug(f"Loaded {count} entities for {project_path} into resolver cache")

    async def resolve(
        self, name: str, entity_type: str, project_path: str, session=None
    ) -> tuple[int, bool]:
        """
        Resolve entity to canonical ID.

        Args:
            name: Original entity name
            entity_type: Type of entity
            project_path: Project this belongs to
            session: Optional existing session (for batch operations)

        Returns:
            (entity_id, is_new) tuple
        """
        normalized = self.normalize(name, entity_type)
        cache_key = self._cache_key(project_path, entity_type, normalized)

        # Check cache first
        if cache_key in self._canonical_cache:
            return self._canonical_cache[cache_key], False

        # Need to check/create in database
        async def do_resolve(sess):
            # Check for existing entity with same type and normalized name
            result = await sess.execute(
                select(ExtractedEntity).where(
                    ExtractedEntity.project_path == project_path,
                    ExtractedEntity.entity_type == entity_type,
                    func.lower(ExtractedEntity.name) == normalized,
                )
            )
            existing = result.scalar_one_or_none()

            if existing:
                self._canonical_cache[cache_key] = existing.id
                return existing.id, False

            # Also check qualified_name
            result = await sess.execute(
                select(ExtractedEntity).where(
                    ExtractedEntity.project_path == project_path,
                    ExtractedEntity.entity_type == entity_type,
                    ExtractedEntity.qualified_name == normalized,
                )
            )
            existing = result.scalar_one_or_none()

            if existing:
                self._canonical_cache[cache_key] = existing.id
                return existing.id, False

            # Create new entity
            new_entity = ExtractedEntity(
                project_path=project_path,
                entity_type=entity_type,
                name=name,  # Preserve original name
                qualified_name=normalized,  # Store normalized for matching
                mention_count=1,
            )
            sess.add(new_entity)
            await sess.flush()

            self._canonical_cache[cache_key] = new_entity.id
            logger.debug(
                f"Created new entity: {entity_type}:{name} (normalized: {normalized})"
            )
            return new_entity.id, True

        if session:
            return await do_resolve(session)
        else:
            async with self.db.get_session() as sess:
                return await do_resolve(sess)

    def clear_cache(self, project_path: str = None):
        """Clear the resolver cache (call after major changes).

        Args:
            project_path: If provided, only clear cache for this project.
                         If None, clear entire cache.
        """
        if project_path is not None:
            prefix = f"{project_path}:"
            keys_to_remove = [k for k in self._canonical_cache if k.startswith(prefix)]
            for k in keys_to_remove:
                del self._canonical_cache[k]
            self._loaded_projects.discard(project_path)
        else:
            self._canonical_cache.clear()
            self._loaded_projects.clear()
