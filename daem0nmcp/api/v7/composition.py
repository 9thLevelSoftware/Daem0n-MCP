"""One side-effect-free composition boundary for the complete MCP v7 surface."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from ...covenant import CovenantGate
from .application import V7ApplicationDependencies, V7ToolRouter
from .factory import build_v7_manifest, combine_handler_maps
from .fastmcp import build_fastmcp_server
from .middleware import (
    ResourceCommunionAuthorizer,
    TransportMode,
    V7InvocationMiddleware,
)
from .pinned import (
    PINNED_HANDLER_NAMES,
    PinnedDependencies,
    build_pinned_handlers,
)
from .registry import V7Manifest
from .resources import ResourceDependencies, ResourceHandlers, ResourceReader


@dataclass(frozen=True, slots=True)
class V7Surface:
    """Settled manifest and runtime boundaries for one transport profile."""

    gate: CovenantGate
    workspace_resolver: object
    handlers: Mapping[str, Callable[..., object]]
    resource_handlers: ResourceHandlers
    manifest: V7Manifest
    middleware: tuple[object, ...]

    def build_server(self, **options: Any) -> Any:
        """Create a fresh FastMCP instance from this immutable surface."""

        if "middleware" in options:
            raise ValueError("surface middleware cannot be replaced")
        return build_fastmcp_server(
            self.manifest,
            middleware=self.middleware,
            **options,
        )


def build_v7_surface(
    *,
    pinned_dependencies: PinnedDependencies,
    operations: Mapping[str, Callable[..., object]],
    warning_reader: ResourceReader,
    failure_reader: ResourceReader,
    rule_reader: ResourceReader,
    active_context_reader: ResourceReader,
    transport_mode: str,
    access_token_provider: Callable[[], object] | None = None,
    process_principal: str | None = None,
    session_id_factory: Callable[[], str] | None = None,
    allow_unauthenticated_loopback: bool = False,
    activity_callback: Callable[[Any, bool], None] | None = None,
) -> V7Surface:
    """Compose one exact surface without importing legacy decorator modules."""

    if not isinstance(pinned_dependencies, PinnedDependencies):
        raise ValueError("pinned dependencies are required")
    if transport_mode not in {"stdio", "streamable-http"}:
        raise ValueError("v7 supports stdio or streamable-http")
    selected_transport: TransportMode = (
        "stdio" if transport_mode == "stdio" else "streamable-http"
    )
    gate = pinned_dependencies.covenant_gate
    if not isinstance(gate, CovenantGate):
        raise ValueError("pinned handlers require the authoritative Covenant gate")
    resolver = pinned_dependencies.workspace_resolver
    if not callable(getattr(resolver, "resolve", None)):
        raise ValueError("an explicit workspace resolver is required")
    if not isinstance(operations, Mapping):
        raise ValueError("operation registry must be a mapping")
    pinned_bypasses = set(operations) & PINNED_HANDLER_NAMES
    if pinned_bypasses:
        raise ValueError("pinned tools cannot be replaced by generic operations")

    router = V7ToolRouter(
        V7ApplicationDependencies(
            workspace_resolver=resolver,
            covenant_gate=gate,
            scope_provider=pinned_dependencies.scope_provider,
            operations=operations,
            response_factory=pinned_dependencies.response_factory,
        )
    )
    handlers = combine_handler_maps(
        build_pinned_handlers(pinned_dependencies),
        router.handlers(exclude=PINNED_HANDLER_NAMES),
    )
    resource_handlers = ResourceHandlers(
        ResourceDependencies(
            workspace_resolver=resolver,
            communion_authorizer=ResourceCommunionAuthorizer(expected_gate=gate),
            warning_reader=warning_reader,
            failure_reader=failure_reader,
            rule_reader=rule_reader,
            active_context_reader=active_context_reader,
            clock=pinned_dependencies.clock,
        )
    )
    middleware = (
        V7InvocationMiddleware(
            gate=gate,
            workspace_resolver=resolver,
            transport_mode=selected_transport,
            access_token_provider=access_token_provider,
            process_principal=process_principal,
            session_id_factory=session_id_factory,
            allow_unauthenticated_loopback=allow_unauthenticated_loopback,
            activity_callback=activity_callback,
        ),
    )
    manifest = build_v7_manifest(handlers, resource_handlers)
    return V7Surface(
        gate=gate,
        workspace_resolver=resolver,
        handlers=handlers,
        resource_handlers=resource_handlers,
        manifest=manifest,
        middleware=middleware,
    )


__all__ = ["V7Surface", "build_v7_surface"]
