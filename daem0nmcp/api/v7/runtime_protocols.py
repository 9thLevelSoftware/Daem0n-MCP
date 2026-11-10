"""Structural contracts for injected v7 storage and worker dependencies."""

from collections.abc import Callable
from contextlib import AbstractContextManager
from typing import Any, Protocol

from ...storage_activation import ResolvedActiveDatabase
from ...workspace import Workspace


class ActiveStorageResolver(Protocol):
    def locked_active(
        self, workspace: Workspace
    ) -> AbstractContextManager[ResolvedActiveDatabase]: ...


class WorkspaceResolver(Protocol):
    def resolve(self, workspace_id: str, /) -> Workspace: ...


class WorkerPool(Protocol):
    async def run(self, operation: Callable[[], Any]) -> Any: ...
    def shutdown(self) -> None: ...
