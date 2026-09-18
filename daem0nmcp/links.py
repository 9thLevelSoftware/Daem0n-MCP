# daem0nmcp/links.py
"""
Link Manager - Handles cross-project linking for multi-repo awareness.

Links enable reading memories from related projects while maintaining
strict write isolation (each project only writes to its own database).
"""

import logging
from typing import Any

from sqlalchemy import delete, select

from .database import DatabaseManager
from .models import ProjectLink
from .workspace import WorkspacePathError, WorkspaceRegistry, resolve_derived_path

logger = logging.getLogger(__name__)


class LinkManager:
    """
    Manages project links for cross-repo awareness.

    Usage:
        link_mgr = LinkManager(db_manager)
        await link_mgr.link_projects("/repos/backend", "/repos/client", "same-project")
        links = await link_mgr.list_linked_projects("/repos/backend")
    """

    def __init__(self, db: DatabaseManager, registry: WorkspaceRegistry | None = None):
        self.db = db
        if registry is None:
            # Import lazily to avoid a module cycle while still using the
            # process-wide immutable registry snapshot used by project contexts.
            from .context_manager import workspace_registry

            registry = workspace_registry
        self.registry = registry

    async def link_projects(
        self,
        source_path: str,
        linked_path: str,
        relationship: str = "related",
        label: str | None = None,
    ) -> dict[str, Any]:
        """
        Create a link between two projects.

        Args:
            source_path: The current project path (where link is stored)
            linked_path: The project to link to
            relationship: Type of relationship (same-project, upstream, downstream, related)
            label: Optional human-readable label

        Returns:
            Status dict with link details
        """
        source = self.registry.resolve(source_path)
        linked = self.registry.resolve(linked_path)
        source_root = str(source.root)
        linked_references = (linked.workspace_id, str(linked.root))

        async with self.db.get_session() as session:
            # Check if link already exists
            existing = await session.execute(
                select(ProjectLink).where(
                    ProjectLink.source_path == source_root,
                    ProjectLink.linked_path.in_(linked_references),
                )
            )
            if existing.scalar_one_or_none():
                return {
                    "status": "already_linked",
                    "source_path": source_root,
                    "linked_path": str(linked.root),
                    "workspace_id": linked.workspace_id,
                }

            # Create new link
            link = ProjectLink(
                source_path=source_root,
                linked_path=linked.workspace_id,
                relationship=relationship,
                label=label,
            )
            session.add(link)

            logger.info(
                f"Linked workspace {source.workspace_id} -> {linked.workspace_id} "
                f"({relationship})"
            )

            return {
                "status": "linked",
                "source_path": source_root,
                "linked_path": str(linked.root),
                "workspace_id": linked.workspace_id,
                "relationship": relationship,
                "label": label,
            }

    async def unlink_projects(
        self, source_path: str, linked_path: str
    ) -> dict[str, Any]:
        """
        Remove a link between two projects.

        Args:
            source_path: The current project path
            linked_path: The project to unlink

        Returns:
            Status dict
        """
        source = self.registry.resolve(source_path)
        linked = self.registry.resolve(linked_path)
        source_root = str(source.root)
        linked_references = (linked.workspace_id, str(linked.root))

        async with self.db.get_session() as session:
            result = await session.execute(
                delete(ProjectLink).where(
                    ProjectLink.source_path == source_root,
                    ProjectLink.linked_path.in_(linked_references),
                )
            )

            if result.rowcount > 0:
                logger.info(
                    f"Unlinked workspace {source.workspace_id} -> {linked.workspace_id}"
                )
                return {
                    "status": "unlinked",
                    "source_path": source_root,
                    "linked_path": str(linked.root),
                    "workspace_id": linked.workspace_id,
                }
            else:
                return {
                    "status": "not_found",
                    "source_path": source_root,
                    "linked_path": str(linked.root),
                    "workspace_id": linked.workspace_id,
                }

    async def list_linked_projects(self, source_path: str) -> list[dict[str, Any]]:
        """
        List all projects linked from the given source.

        Args:
            source_path: The project to list links for

        Returns:
            List of link dicts with linked_path, relationship, label
        """
        source = self.registry.resolve(source_path)
        source_root = str(source.root)
        async with self.db.get_session() as session:
            result = await session.execute(
                select(ProjectLink).where(ProjectLink.source_path == source_root)
            )
            links = result.scalars().all()

        # New rows contain workspace IDs. Legacy rows remain readable only while
        # their canonical path is explicitly registered in the current process.
        authorized_links = []
        for link in links:
            linked = self.registry.resolve(link.linked_path)
            authorized_links.append(
                {
                    "id": link.id,
                    "linked_path": str(linked.root),
                    "workspace_id": linked.workspace_id,
                    "relationship": link.relationship,
                    "label": link.label,
                    "created_at": link.created_at.isoformat()
                    if link.created_at
                    else None,
                }
            )
        return authorized_links

    async def get_linked_db_managers(self, source_path: str) -> list[tuple]:
        """
        Get DatabaseManager instances for all linked projects.

        Returns list of (linked_path, db_manager) tuples.
        Only returns managers for projects that exist and have .daem0n directories.

        Args:
            source_path: The current project path

        Returns:
            List of (path, DatabaseManager) tuples
        """
        links = await self.list_linked_projects(source_path)
        managers = []

        for link in links:
            linked = self.registry.resolve(link["workspace_id"])
            linked_path = str(linked.root)
            # Use correct storage path pattern: .daem0nmcp/storage
            storage_path = resolve_derived_path(linked.root, ".daem0nmcp", "storage")

            if storage_path.exists():
                try:
                    linked_db = DatabaseManager(str(storage_path))
                    await linked_db.init_db()
                    managers.append((linked_path, linked_db))
                except Exception as e:
                    logger.warning(f"Could not open linked project {linked_path}: {e}")

        return managers

    async def consolidate_linked_databases(
        self, target_path: str, archive_sources: bool = False
    ) -> dict[str, Any]:
        """
        Merge memories from all linked project databases into the target.

        This is useful when consolidating multiple child repos into a parent,
        or when switching from a multi-repo to a monorepo setup.

        Storage path pattern: .daem0nmcp/storage

        Args:
            target_path: The target project path (where memories will be merged to)
            archive_sources: If True, rename source .daem0nmcp dirs to .daem0nmcp.archived

        Returns:
            Dict with status, memories_merged count, and sources_processed list
        """
        from .memory import MemoryManager
        from .models import Memory

        target = self.registry.resolve(target_path)
        target_path = str(target.root)
        links = await self.list_linked_projects(target_path)
        if not links:
            return {
                "status": "no_links",
                "message": "No linked projects to consolidate",
            }

        target_mem = MemoryManager(self.db)
        memories_merged = 0
        sources_processed = []

        # Resolve every stored reference before opening any source database. A
        # single stale legacy row therefore fails closed without partial reads.
        authorized_sources = [
            self.registry.resolve(link["workspace_id"]) for link in links
        ]

        for source in authorized_sources:
            source = self.registry.resolve(source.workspace_id)
            source_path = str(source.root)
            # Use CORRECT storage path pattern: .daem0nmcp/storage
            source_storage = resolve_derived_path(source.root, ".daem0nmcp", "storage")

            if not source_storage.exists():
                logger.warning(f"No storage found at {source_storage}, skipping")
                continue

            try:
                from .database import DatabaseManager

                source_db = DatabaseManager(str(source_storage))
                await source_db.init_db()

                # Copy memories from source
                async with source_db.get_session() as session:
                    result = await session.execute(select(Memory))
                    source_memories = result.scalars().all()

                    for mem in source_memories:
                        # Add with source tracking in context
                        context = dict(mem.context) if mem.context else {}
                        context["_merged_from"] = source_path
                        context["_original_id"] = mem.id

                        await target_mem.remember(
                            category=mem.category,
                            content=mem.content,
                            rationale=mem.rationale,
                            context=context,
                            tags=list(mem.tags) if mem.tags else [],
                            file_path=mem.file_path,
                            project_path=target_path,
                        )
                        memories_merged += 1

                sources_processed.append(source_path)
                logger.info(
                    f"Merged {len(source_memories)} memories from {source_path}"
                )

                # Archive source if requested
                if archive_sources:
                    source = self.registry.resolve(source.workspace_id)
                    daem0nmcp_dir = resolve_derived_path(source.root, ".daem0nmcp")
                    archived_path = resolve_derived_path(
                        source.root, ".daem0nmcp.archived"
                    )
                    if daem0nmcp_dir.exists() and not archived_path.exists():
                        daem0nmcp_dir.rename(archived_path)
                        logger.info(f"Archived {daem0nmcp_dir} -> {archived_path}")

            except WorkspacePathError:
                raise
            except Exception as e:
                logger.error(f"Error consolidating from {source_path}: {e}")

        return {
            "status": "consolidated",
            "memories_merged": memories_merged,
            "sources_processed": sources_processed,
            "archived": archive_sources,
        }
