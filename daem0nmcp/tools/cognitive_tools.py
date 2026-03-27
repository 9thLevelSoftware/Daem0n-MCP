"""Cognitive tools: simulate_decision, evolve_rule, debate_internal."""

import logging
from typing import Any

try:
    from .. import __version__
    from ..context_manager import (
        _default_project_path,
        _missing_project_path_error,
        get_project_context,
    )
    from ..logging_config import with_request_id
    from ..mcp_instance import mcp
except ImportError:
    from daem0nmcp import __version__
    from daem0nmcp.context_manager import (
        _default_project_path,
        _missing_project_path_error,
        get_project_context,
    )
    from daem0nmcp.logging_config import with_request_id
    from daem0nmcp.mcp_instance import mcp

logger = logging.getLogger(__name__)


@mcp.tool(version=__version__)
@with_request_id
async def simulate_decision(
    decision_id: int,
    project_path: str | None = None,
) -> dict[str, Any]:
    """
    Temporal Scrying -- replay a past decision with current knowledge.

    Reconstructs the context that existed at the decision's moment of inscription,
    compares it with the daemon's present understanding, and reveals what is now
    known that was not known then.

    Args:
        decision_id: Memory ID of the decision to scry
        project_path: Project root
    """
    if not project_path and not _default_project_path:
        return _missing_project_path_error()

    ctx = await get_project_context(project_path)

    try:
        from ..cognitive.simulate import run_simulation
    except ImportError:
        from daem0nmcp.cognitive.simulate import run_simulation

    result = await run_simulation(decision_id, ctx)
    return result.to_dict()


@mcp.tool(version=__version__)
@with_request_id
async def evolve_rule(
    rule_id: int | None = None,
    project_path: str | None = None,
) -> dict[str, Any]:
    """
    Rule Entropy Analysis -- examine rules for signs of decay and drift.

    Cross-references rule triggers against the code index and outcome history
    to produce staleness scores and concrete evolution suggestions. The daemon
    proposes changes but never inscribes them without consent.

    Args:
        rule_id: Specific rule ID to analyze (omit for batch analysis of all rules)
        project_path: Project root
    """
    if not project_path and not _default_project_path:
        return _missing_project_path_error()

    ctx = await get_project_context(project_path)

    try:
        from ..cognitive.evolve import run_evolution
    except ImportError:
        from daem0nmcp.cognitive.evolve import run_evolution

    results = await run_evolution(rule_id, ctx)
    return {"reports": [r.to_dict() for r in results], "analyzed": len(results)}


@mcp.tool(version=__version__)
@with_request_id
async def debate_internal(
    topic: str,
    advocate_position: str,
    challenger_position: str,
    project_path: str | None = None,
) -> dict[str, Any]:
    """
    Adversarial Council -- convene an internal debate grounded in memory evidence.

    Runs a structured advocate/challenger/judge debate where each argument is
    supported only by recalled memory evidence. No external reasoning is invoked.
    Convergence detection halts deliberation when positions stabilize. The
    synthesis is inscribed as a consensus memory for future retrieval.

    Args:
        topic: The subject of deliberation
        advocate_position: The position the advocate will defend
        challenger_position: The position the challenger will defend
        project_path: Project root
    """
    if not project_path and not _default_project_path:
        return _missing_project_path_error()

    ctx = await get_project_context(project_path)

    try:
        from ..cognitive.debate import run_debate
    except ImportError:
        from daem0nmcp.cognitive.debate import run_debate

    result = await run_debate(topic, advocate_position, challenger_position, ctx)
    return result.to_dict()
