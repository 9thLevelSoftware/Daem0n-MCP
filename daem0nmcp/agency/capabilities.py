"""
CapabilityScope - Least-privilege access control for tool capabilities.

Defines capability scopes that can be granted or revoked per project/session.
Used to enforce fine-grained permissions on sensitive tools like execute_python.

Scopes:
- EXECUTE_CODE: Permission to run code in sandbox
- NETWORK_ACCESS: Permission for sandbox to access network (future)
- FILE_WRITE: Permission to write files to sandbox (future)
"""

import logging
from enum import Enum, auto

logger = logging.getLogger(__name__)


class CapabilityScope(Enum):
    """Available capability scopes for tool access."""

    EXECUTE_CODE = auto()  # Permission to run code in sandbox
    NETWORK_ACCESS = auto()  # Permission for network in sandbox (reserved)
    FILE_WRITE = auto()  # Permission to write files (reserved)


# Default capabilities granted to all projects
DEFAULT_CAPABILITIES: set[CapabilityScope] = {
    CapabilityScope.EXECUTE_CODE,  # Code execution allowed by default
}


class CapabilityManager:
    """
    Manages capability scopes per project.

    Provides least-privilege access control for sensitive operations.
    Capabilities can be granted or revoked per project.
    """

    def __init__(self) -> None:
        self._project_capabilities: dict[str, set[CapabilityScope]] = {}

    def get_capabilities(self, project_path: str) -> set[CapabilityScope]:
        """Get capabilities for a project (defaults if not set)."""
        return self._project_capabilities.get(project_path, DEFAULT_CAPABILITIES.copy())

    def has_capability(self, project_path: str, capability: CapabilityScope) -> bool:
        """Check if project has a specific capability."""
        caps = self.get_capabilities(project_path)
        return capability in caps

    def grant_capability(self, project_path: str, capability: CapabilityScope) -> None:
        """Grant a capability to a project."""
        if project_path not in self._project_capabilities:
            self._project_capabilities[project_path] = DEFAULT_CAPABILITIES.copy()
        self._project_capabilities[project_path].add(capability)
        logger.info(f"Granted {capability.name} to {project_path}")

    def revoke_capability(self, project_path: str, capability: CapabilityScope) -> None:
        """Revoke a capability from a project."""
        if project_path not in self._project_capabilities:
            self._project_capabilities[project_path] = DEFAULT_CAPABILITIES.copy()
        self._project_capabilities[project_path].discard(capability)
        logger.info(f"Revoked {capability.name} from {project_path}")

    def reset_capabilities(self, project_path: str) -> None:
        """Reset project to default capabilities."""
        self._project_capabilities.pop(project_path, None)


def check_capability(
    project_path: str,
    capability: CapabilityScope,
    manager: CapabilityManager | None = None,
) -> dict | None:
    """
    Check if project has required capability.

    Args:
        project_path: Project to check
        capability: Required capability
        manager: CapabilityManager instance (uses global if None)

    Returns:
        None if capability granted, or violation dict if denied
    """
    if manager is None:
        # Allow when no manager configured (backwards compatibility)
        return None

    if not manager.has_capability(project_path, capability):
        return {
            "status": "blocked",
            "violation": "CAPABILITY_DENIED",
            "message": (
                f"The capability '{capability.name}' is not granted for this project. "
                f"The daemon enforces least-privilege access."
            ),
            "project_path": project_path,
            "required_capability": capability.name,
        }
    return None
