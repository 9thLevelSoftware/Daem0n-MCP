"""Background dreaming -- autonomous reasoning during idle periods."""

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
