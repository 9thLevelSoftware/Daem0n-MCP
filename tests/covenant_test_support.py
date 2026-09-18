"""Explicit scoped authorization helpers for legacy public-tool tests.

This module deliberately uses the production Covenant gate and decorated
entrypoints.  It provides no global state and installs ContextVars only for
the duration of one call.
"""

from __future__ import annotations

import importlib
import inspect
import secrets
import sys
import threading
import uuid
from collections.abc import Callable, Iterable, Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from daem0nmcp.covenant import (
    COVENANT_POLICY,
    LEGACY_ENTRYPOINTS,
    CapabilityAuthority,
    CovenantGate,
    CovenantLevel,
    CovenantStateStore,
    InvocationScope,
    _adapt_legacy_arguments,
    installed_invocation,
)
from daem0nmcp.workspace import WorkspaceRegistry

_REGISTRY_PATCH_LOCK = threading.Lock()
_REGISTRY_PATCHES: dict[Any, list[Any]] = {}


def _acquire_registry_patch(module: Any, registry: WorkspaceRegistry) -> None:
    with _REGISTRY_PATCH_LOCK:
        state = _REGISTRY_PATCHES.get(module)
        if state is None:
            _REGISTRY_PATCHES[module] = [module.workspace_registry, registry, 1]
            module.workspace_registry = registry
            return
        if state[1] is not registry:
            raise RuntimeError(
                "overlapping Covenant test scopes selected different workspaces"
            )
        state[2] += 1


def _release_registry_patch(module: Any) -> None:
    with _REGISTRY_PATCH_LOCK:
        state = _REGISTRY_PATCHES[module]
        state[2] -= 1
        if state[2] == 0:
            module.workspace_registry = state[0]
            del _REGISTRY_PATCHES[module]


class CovenantTestWorkspace(str):
    """A string-compatible path with one isolated test invocation scope."""

    def __new__(
        cls,
        project_path: str | Path,
        *,
        additional_roots: Iterable[str | Path] = (),
    ) -> CovenantTestWorkspace:
        canonical = str(Path(project_path).resolve())
        instance = super().__new__(cls, canonical)
        registry = WorkspaceRegistry(list(additional_roots), default_root=canonical)
        workspace = registry.resolve(canonical)
        instance._registry = registry
        instance.scope = InvocationScope(
            principal_id="pytest",
            transport_session_id=f"pytest-{uuid.uuid4().hex}",
            canonical_workspace=str(workspace.root),
        )
        instance.gate = CovenantGate(
            state_store=CovenantStateStore(),
            authority=CapabilityAuthority(
                secret=secrets.token_bytes(32), kid="pytest-ephemeral"
            ),
        )
        return instance

    @contextmanager
    def installed(self) -> Iterator[None]:
        """Install this workspace only for the lexical duration of a call."""
        patched_modules: list[Any] = []
        try:
            for module_name in (
                "daem0nmcp.context_manager",
                "daem0nmcp.server",
                "daem0nmcp.tools.code_tools",
                "daem0nmcp.tools.federation",
            ):
                module = sys.modules.get(module_name)
                if module is None or not hasattr(module, "workspace_registry"):
                    continue
                _acquire_registry_patch(module, self._registry)
                patched_modules.append(module)
            with installed_invocation(
                self.scope,
                self.gate,
                workspace_resolver=self._registry.resolve,
            ):
                yield
        finally:
            for module in reversed(patched_modules):
                _release_registry_patch(module)

    @staticmethod
    def _bound_arguments(
        func: Callable[..., Any], args: tuple[Any, ...], kwargs: Mapping[str, Any]
    ) -> tuple[str, dict[str, Any]]:
        entrypoint = func.__name__
        if entrypoint not in LEGACY_ENTRYPOINTS:
            raise ValueError(f"not a guarded legacy entrypoint: {entrypoint}")
        call_kwargs = dict(kwargs)
        call_kwargs.pop("preflight_token", None)
        signature = inspect.signature(func)
        bound = signature.bind(*args, **call_kwargs)
        bound.apply_defaults()
        original = inspect.unwrap(func)
        adapted = _adapt_legacy_arguments(
            entrypoint, bound.arguments, original.__globals__
        )
        return LEGACY_ENTRYPOINTS[entrypoint], adapted

    def adapt(
        self, func: Callable[..., Any], /, *args: Any, **kwargs: Any
    ) -> tuple[str, dict[str, Any]]:
        """Return the production operation and effective legacy arguments."""
        with self.installed():
            return self._bound_arguments(func, args, kwargs)

    def issue(self, func: Callable[..., Any], /, *args: Any, **kwargs: Any) -> str:
        """Issue one production capability for the exact effective call."""
        with self.installed():
            operation, adapted = self._bound_arguments(func, args, kwargs)
            return self.gate.issue_preflight(self.scope, operation, adapted)

    async def call(self, func: Callable[..., Any], /, *args: Any, **kwargs: Any) -> Any:
        """Invoke a real guarded leaf, issuing a token only when policy requires."""
        if "preflight_token" in kwargs:
            raise TypeError("use call_unsealed when supplying an explicit token")
        with self.installed():
            operation, adapted = self._bound_arguments(func, args, kwargs)
            level = COVENANT_POLICY.resolve(operation, adapted)
            call_kwargs = dict(kwargs)
            if level in {CovenantLevel.COUNSEL, CovenantLevel.DESTRUCTIVE}:
                call_kwargs["preflight_token"] = self.gate.issue_preflight(
                    self.scope, operation, adapted
                )
            return await func(*args, **call_kwargs)

    async def call_unsealed(
        self, func: Callable[..., Any], /, *args: Any, **kwargs: Any
    ) -> Any:
        """Invoke a guarded leaf with scope but without automatic authorization."""
        with self.installed():
            return await func(*args, **kwargs)

    async def brief(self) -> dict[str, Any]:
        """Run the real exempt briefing leaf so communion is recorded in scope."""
        server = importlib.import_module("daem0nmcp.server")
        result = await self.call_unsealed(server.get_briefing, project_path=self)
        if not self.gate.state_store.is_briefed(self.scope):
            raise AssertionError("briefing did not record communion for the test scope")
        return result


def covenant_workspace(project_path: str | Path) -> CovenantTestWorkspace:
    """Create one isolated, explicitly scoped test workspace."""
    return CovenantTestWorkspace(project_path)
