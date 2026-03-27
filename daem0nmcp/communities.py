"""
Community Manager - GraphRAG-style hierarchical clustering for Daem0n.

Supports two community detection modes:
1. Tag co-occurrence clustering (legacy, detect_communities)
2. Leiden algorithm on knowledge graph (detect_communities_from_graph)

The Leiden algorithm is preferred for proper graph-based community
detection that guarantees well-connected communities.
"""

import logging
from collections import defaultdict
from typing import Any

from sqlalchemy import delete, select

from .database import DatabaseManager
from .graph import KnowledgeGraph
from .graph.leiden import LeidenConfig, get_community_stats, run_leiden_on_networkx
from .graph.summarizer import CommunitySummarizer
from .models import Memory, MemoryCommunity

logger = logging.getLogger(__name__)


class CommunityManager:
    """
    Manages memory communities for hierarchical summarization.

    Uses tag co-occurrence for clustering:
    - Memories sharing 2+ tags cluster together
    - Dominant tags become community name
    - Summaries are generated from member content
    """

    def __init__(
        self,
        db_manager: DatabaseManager,
        summarizer: CommunitySummarizer | None = None,
    ):
        self.db = db_manager
        self.summarizer = summarizer or CommunitySummarizer()

    async def detect_communities(
        self,
        project_path: str,
        min_community_size: int = 2,
        min_shared_tags: int = 2,
    ) -> list[dict[str, Any]]:
        """
        Detect communities based on tag co-occurrence (legacy method).

        NOTE: For graph-based community detection using Leiden algorithm,
        use detect_communities_from_graph() instead. The Leiden algorithm
        provides true graph communities with guaranteed well-connectedness.

        Algorithm:
        1. Build tag co-occurrence matrix
        2. Find connected components (memories sharing tags)
        3. Merge small clusters into larger ones
        4. Generate community metadata

        Args:
            project_path: Project to analyze
            min_community_size: Minimum members for a community
            min_shared_tags: Minimum shared tags to cluster together

        Returns:
            List of detected community dicts (not yet persisted)
        """
        async with self.db.get_session() as session:
            # Get all non-archived memories with tags
            # Note: project_path is used when saving communities, not filtering memories.
            # Each project has its own .daem0nmcp database directory, so all memories
            # in this database already belong to this project.
            result = await session.execute(
                select(Memory).where(
                    Memory.tags.isnot(None),
                    (Memory.archived == False) | (Memory.archived.is_(None)),  # noqa: E712
                )
            )
            memories = result.scalars().all()

        if not memories:
            return []

        # Build tag -> memory_ids mapping
        tag_to_memories: dict[str, set[int]] = defaultdict(set)
        memory_tags: dict[int, set[str]] = {}

        for mem in memories:
            if mem.tags:
                tags = set(mem.tags) if isinstance(mem.tags, list) else set()
                memory_tags[mem.id] = tags
                for tag in tags:
                    tag_to_memories[tag].add(mem.id)

        # Find clusters using union-find on shared tags
        clusters = self._cluster_by_shared_tags(
            memory_tags, tag_to_memories, min_shared_tags
        )

        # Filter by minimum size and build community dicts
        communities = []
        for cluster_id, member_ids in clusters.items():
            if len(member_ids) < min_community_size:
                continue

            # Get dominant tags (appear in >50% of members)
            tag_counts: dict[str, int] = defaultdict(int)
            for mem_id in member_ids:
                for tag in memory_tags.get(mem_id, []):
                    tag_counts[tag] += 1

            threshold = len(member_ids) / 2
            dominant_tags = sorted(
                [t for t, c in tag_counts.items() if c >= threshold],
                key=lambda t: tag_counts[t],
                reverse=True,
            )

            # Generate name from top tags
            name = (
                " + ".join(dominant_tags[:3])
                if dominant_tags
                else f"Cluster {cluster_id}"
            )

            communities.append(
                {
                    "name": name,
                    "tags": dominant_tags,
                    "member_ids": list(member_ids),
                    "member_count": len(member_ids),
                    "level": 0,
                }
            )

        return communities

    def _cluster_by_shared_tags(
        self,
        memory_tags: dict[int, set[str]],
        tag_to_memories: dict[str, set[int]],
        min_shared: int,
    ) -> dict[int, set[int]]:
        """
        Cluster memories using union-find on shared tags.

        Two memories are in the same cluster if they share >= min_shared tags.
        """
        # Union-find data structure
        parent: dict[int, int] = {mid: mid for mid in memory_tags}

        def find(x: int) -> int:
            if parent[x] != x:
                parent[x] = find(parent[x])
            return parent[x]

        def union(x: int, y: int):
            px, py = find(x), find(y)
            if px != py:
                parent[px] = py

        # Union memories that share enough tags
        memory_ids = list(memory_tags.keys())
        for i, mid1 in enumerate(memory_ids):
            for mid2 in memory_ids[i + 1 :]:
                shared = memory_tags[mid1] & memory_tags[mid2]
                if len(shared) >= min_shared:
                    union(mid1, mid2)

        # Collect clusters
        clusters: dict[int, set[int]] = defaultdict(set)
        for mid in memory_ids:
            clusters[find(mid)].add(mid)

        return clusters

    async def _get_entity_names(self, entity_ids: list[int]) -> list[str]:
        """Get entity names for a list of entity IDs, ordered by mention count."""
        if not entity_ids:
            return []

        async with self.db.get_session() as session:
            from .models import ExtractedEntity

            result = await session.execute(
                select(ExtractedEntity)
                .where(ExtractedEntity.id.in_(entity_ids))
                .order_by(ExtractedEntity.mention_count.desc())
            )
            entities = result.scalars().all()
            return [e.name for e in entities]

    async def detect_communities_from_graph(
        self,
        project_path: str,
        knowledge_graph: KnowledgeGraph | None = None,
        resolution: float = 1.0,
        min_community_size: int = 2,
    ) -> list[dict[str, Any]]:
        """
        Detect communities using Leiden algorithm on the knowledge graph.

        This replaces the old tag-based clustering with true graph community detection.
        Leiden algorithm guarantees well-connected communities unlike union-find on tags.

        Args:
            project_path: Project to analyze
            knowledge_graph: Optional pre-loaded graph (will create if not provided)
            resolution: Leiden resolution parameter (>1 = smaller communities)
            min_community_size: Minimum members for a community

        Returns:
            List of detected community dicts (not yet persisted)
        """
        # Load or use provided graph
        if knowledge_graph is None:
            knowledge_graph = KnowledgeGraph(self.db)

        await knowledge_graph.ensure_loaded()

        # Get the internal NetworkX graph
        nx_graph = knowledge_graph._graph

        if nx_graph.number_of_nodes() == 0:
            logger.info("Knowledge graph is empty, no communities to detect")
            return []

        # Run Leiden
        config = LeidenConfig(resolution=resolution, seed=42)
        community_map = run_leiden_on_networkx(nx_graph, config)

        # Log stats
        stats = get_community_stats(community_map)
        logger.info(
            f"Leiden stats: {stats['num_communities']} raw communities, "
            f"sizes: {stats['sizes'][:5]}..."
        )

        # Group nodes by community
        communities_nodes: dict[int, list[str]] = defaultdict(list)
        for node_id, comm_id in community_map.items():
            communities_nodes[comm_id].append(node_id)

        # Convert to community dicts
        communities = []
        for comm_id, node_ids in communities_nodes.items():
            # Separate memory and entity nodes
            memory_ids = []
            entity_ids = []
            for node_id in node_ids:
                if node_id.startswith("memory:"):
                    try:
                        memory_ids.append(int(node_id.split(":")[1]))
                    except (ValueError, IndexError):
                        continue
                elif node_id.startswith("entity:"):
                    try:
                        entity_ids.append(int(node_id.split(":")[1]))
                    except (ValueError, IndexError):
                        continue

            # Skip if too small (based on memories, not total nodes)
            if len(memory_ids) < min_community_size:
                continue

            # Get entity names for community name
            entity_names = await self._get_entity_names(entity_ids)

            # Generate name from top entities
            if entity_names:
                name = " + ".join(entity_names[:3])
            else:
                name = f"Community {comm_id}"

            communities.append(
                {
                    "name": name,
                    "tags": entity_names[:10],  # Use entity names as tags
                    "member_ids": memory_ids,
                    "member_count": len(memory_ids),
                    "entity_ids": entity_ids,
                    "level": 0,
                    "leiden_community_id": comm_id,
                }
            )

        # Sort by size descending
        communities.sort(key=lambda c: c["member_count"], reverse=True)

        logger.info(f"Detected {len(communities)} communities from graph")
        return communities

    async def generate_community_summary(
        self,
        member_ids: list[int],
        community_name: str,
        entity_names: list[str] | None = None,
    ) -> str:
        """
        Generate a summary for a community from its members.

        Uses CommunitySummarizer for consistent summary generation.

        Args:
            member_ids: Memory IDs in this community
            community_name: Name of the community
            entity_names: Optional list of key entities in this community

        Returns:
            Generated summary text
        """
        async with self.db.get_session() as session:
            result = await session.execute(
                select(Memory).where(Memory.id.in_(member_ids))
            )
            memories = result.scalars().all()

        if not memories:
            return f"Empty community: {community_name}"

        # Convert to member dicts for summarizer
        members = [
            {
                "id": m.id,
                "category": m.category,
                "content": m.content,
                "rationale": m.rationale,
                "outcome": m.outcome,
                "worked": m.worked,
            }
            for m in memories
        ]

        return await self.summarizer.summarize_community(
            community_name,
            members,
            entity_names,
        )

    async def save_communities(
        self,
        project_path: str,
        communities: list[dict[str, Any]],
        replace_existing: bool = True,
    ) -> dict[str, Any]:
        """
        Persist detected communities to the database.

        Works with communities from both detect_communities() (tag-based)
        and detect_communities_from_graph() (Leiden-based).

        Args:
            project_path: Project these communities belong to
            communities: List of community dicts from detect methods
            replace_existing: If True, delete existing communities first

        Returns:
            Status with created count
        """
        async with self.db.get_session() as session:
            if replace_existing:
                await session.execute(
                    delete(MemoryCommunity).where(
                        MemoryCommunity.project_path == project_path
                    )
                )

            created_ids = []
            for comm in communities:
                # Generate summary
                summary = await self.generate_community_summary(
                    comm["member_ids"],
                    comm["name"],
                )

                # Get tags - handle both tag-based and entity-based communities
                tags = comm.get("tags", [])
                if not tags and comm.get("entity_ids"):
                    # Fall back to entity names if tags not provided
                    tags = await self._get_entity_names(comm["entity_ids"])

                community = MemoryCommunity(
                    project_path=project_path,
                    name=comm["name"],
                    summary=summary,
                    tags=tags,
                    member_count=comm["member_count"],
                    member_ids=comm["member_ids"],
                    level=comm.get("level", 0),
                )
                session.add(community)
                await session.flush()
                created_ids.append(community.id)

        return {
            "status": "saved",
            "created_count": len(created_ids),
            "community_ids": created_ids,
        }

    async def get_communities(
        self, project_path: str, level: int | None = None
    ) -> list[dict[str, Any]]:
        """
        Get all communities for a project.

        Args:
            project_path: Project to get communities for
            level: Optional filter by hierarchy level

        Returns:
            List of community dicts
        """
        async with self.db.get_session() as session:
            query = select(MemoryCommunity).where(
                MemoryCommunity.project_path == project_path
            )

            if level is not None:
                query = query.where(MemoryCommunity.level == level)

            query = query.order_by(MemoryCommunity.member_count.desc())

            result = await session.execute(query)
            communities = result.scalars().all()

            return [
                {
                    "id": c.id,
                    "name": c.name,
                    "summary": c.summary,
                    "tags": c.tags,
                    "member_count": c.member_count,
                    "member_ids": c.member_ids,
                    "level": c.level,
                    "parent_id": c.parent_id,
                    "created_at": c.created_at.isoformat() if c.created_at else None,
                }
                for c in communities
            ]

    async def get_community_members(self, community_id: int) -> dict[str, Any]:
        """
        Get full memory content for a community's members.

        Use this to "drill down" from a summary to specifics.
        """
        async with self.db.get_session() as session:
            community = await session.get(MemoryCommunity, community_id)
            if not community:
                return {"error": f"Community {community_id} not found"}

            member_ids = community.member_ids or []

            result = await session.execute(
                select(Memory).where(Memory.id.in_(member_ids))
            )
            memories = result.scalars().all()

            return {
                "community_id": community_id,
                "community_name": community.name,
                "community_summary": community.summary,
                "member_count": len(memories),
                "members": [
                    {
                        "id": m.id,
                        "category": m.category,
                        "content": m.content,
                        "rationale": m.rationale,
                        "tags": m.tags,
                        "outcome": m.outcome,
                        "worked": m.worked,
                    }
                    for m in memories
                ],
            }

    async def get_community_members_by_ids(
        self, community_ids: list[int]
    ) -> dict[str, Any]:
        """
        Get member memory IDs for multiple communities at once.

        Efficient bulk lookup for hierarchical retrieval.

        Args:
            community_ids: List of community IDs to retrieve

        Returns:
            Dict mapping community_id to list of member_ids
        """
        if not community_ids:
            return {"communities": {}}

        async with self.db.get_session() as session:
            result = await session.execute(
                select(MemoryCommunity).where(MemoryCommunity.id.in_(community_ids))
            )
            communities = result.scalars().all()

            return {
                "communities": {
                    c.id: {
                        "name": c.name,
                        "member_ids": c.member_ids or [],
                        "member_count": c.member_count,
                    }
                    for c in communities
                }
            }
