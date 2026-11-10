"""Background dreaming -- autonomous reasoning during idle periods.

The production runtime is :mod:`daem0nmcp.dreaming.v7_runtime`.  The legacy
managers stay importable from this package, but are loaded on first use so
that importing the runtime does not pull in their ORM dependencies.
"""

from __future__ import annotations

from importlib import import_module
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .persistence import (
        DreamResult,
        DreamSession,
        persist_dream_result,
        persist_session_summary,
    )
    from .scheduler import IdleDreamScheduler
    from .strategies import (
        CommunityRefresh,
        ConnectionDiscovery,
        DreamStrategy,
        FailedDecisionReview,
        PendingOutcomeResolver,
    )

_LAZY_EXPORTS = {
    "IdleDreamScheduler": "scheduler",
    "DreamSession": "persistence",
    "DreamResult": "persistence",
    "persist_dream_result": "persistence",
    "persist_session_summary": "persistence",
    "DreamStrategy": "strategies",
    "FailedDecisionReview": "strategies",
    "ConnectionDiscovery": "strategies",
    "CommunityRefresh": "strategies",
    "PendingOutcomeResolver": "strategies",
}


def __getattr__(name: str) -> Any:
    module = _LAZY_EXPORTS.get(name)
    if module is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(import_module(f"{__name__}.{module}"), name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted({*globals(), *_LAZY_EXPORTS})


__all__ = [
    "IdleDreamScheduler",
    "DreamSession",
    "DreamResult",
    "persist_dream_result",
    "persist_session_summary",
    "DreamStrategy",
    "FailedDecisionReview",
    "ConnectionDiscovery",
    "CommunityRefresh",
    "PendingOutcomeResolver",
]
