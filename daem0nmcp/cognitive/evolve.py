"""Rule Entropy Analysis -- the daemon examines its own rules for signs
of decay, drift, and obsolescence.

Given a rule (or all rules), this module:
1. Extracts meaningful terms from the rule's trigger text
2. Cross-references those terms against the code index to detect drift
3. Queries outcome history to find worked/failed ratio for matching decisions
4. Computes an age-based decay factor
5. Produces a composite staleness score from 0.0 (fresh) to 1.0 (fully stale)
6. Generates concrete evolution suggestions but NEVER auto-applies changes

Gracefully degrades when the code index is unavailable (code_drift_score
defaults to 0.0 with a note in suggestions).
"""

import logging
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from sqlalchemy import select

if TYPE_CHECKING:
    from ..context_manager import ProjectContext

try:
    from ..config import settings
    from ..models import Rule
except ImportError:
    from daem0nmcp.config import settings
    from daem0nmcp.models import Rule

try:
    from . import StalenessReport
except ImportError:
    from daem0nmcp.cognitive import StalenessReport

logger = logging.getLogger(__name__)

# Minimum term length for code drift analysis -- shorter tokens are noise
_MIN_TERM_LENGTH = 3


async def run_evolution(
    rule_id: int | None,
    ctx: "ProjectContext",
) -> list[StalenessReport]:
    """Examine one or all rules for signs of entropy and decay.

    If *rule_id* is provided, analyse that single rule.  If ``None``,
    analyse all enabled rules capped at
    ``settings.cognitive_evolve_max_rules``, sorted by staleness score
    descending so the most decayed rules surface first.

    Args:
        rule_id: Specific rule ID to analyse, or ``None`` for batch mode.
        ctx: The active ``ProjectContext`` providing database and
            memory manager access.

    Returns:
        A list of :class:`StalenessReport` objects sorted by
        ``staleness_score`` descending.

    Raises:
        ValueError: If *rule_id* is given but no such rule exists.
    """
    # ------------------------------------------------------------------
    # 1. Fetch the rule(s)
    # ------------------------------------------------------------------
    async with ctx.db_manager.get_session() as session:
        if rule_id is not None:
            result = await session.execute(select(Rule).where(Rule.id == rule_id))
            rule = result.scalar_one_or_none()
            if not rule:
                raise ValueError(
                    f"The daemon finds no rule inscribed with id {rule_id}"
                )
            rules = [rule]
        else:
            result = await session.execute(select(Rule))
            rules = list(result.scalars().all())

    # ------------------------------------------------------------------
    # 2. Analyse each rule
    # ------------------------------------------------------------------
    reports: list[StalenessReport] = []
    for rule_obj in rules:
        report = await _analyze_rule(rule_obj, ctx)
        reports.append(report)

    # ------------------------------------------------------------------
    # 3. Sort by staleness descending, cap to max_rules
    # ------------------------------------------------------------------
    reports.sort(key=lambda r: r.staleness_score, reverse=True)

    max_rules = settings.cognitive_evolve_max_rules
    if rule_id is None and len(reports) > max_rules:
        reports = reports[:max_rules]

    return reports


# ------------------------------------------------------------------
# Internal helpers
# ------------------------------------------------------------------


def _extract_terms(trigger: str) -> list[str]:
    """Extract meaningful terms from a rule trigger string.

    Splits on whitespace, filters out tokens shorter than
    ``_MIN_TERM_LENGTH``, and lowercases for consistent matching.
    """
    return [token.lower() for token in trigger.split() if len(token) > _MIN_TERM_LENGTH]


async def _code_drift_analysis(
    terms: list[str],
    ctx: "ProjectContext",
) -> tuple:
    """Compute code drift score by cross-referencing terms against the
    code index.

    Returns:
        A 4-tuple of (code_drift_score, referenced_entities,
        missing_entities, suggestion_notes).
    """
    referenced_entities: list[dict[str, Any]] = []
    missing_entities: list[str] = []
    suggestion_notes: list[str] = []

    if not terms:
        return 0.0, referenced_entities, missing_entities, suggestion_notes

    # Construct a CodeIndexManager on-the-fly (same pattern as code_tools.py)
    try:
        try:
            from ..code_indexer import CodeIndexManager, is_available
        except ImportError:
            from daem0nmcp.code_indexer import CodeIndexManager, is_available

        if not is_available():
            suggestion_notes.append("Code index unavailable -- drift analysis skipped")
            return 0.0, referenced_entities, missing_entities, suggestion_notes

        indexer = CodeIndexManager(db=ctx.db_manager, qdrant=None)

        for term in terms:
            results = await indexer.search_entities(
                term, project_path=ctx.project_path, limit=3
            )
            if results:
                referenced_entities.extend(results)
            else:
                missing_entities.append(term)

    except Exception as exc:
        logger.warning("Code drift analysis failed: %s", exc)
        suggestion_notes.append(
            f"Code index unavailable -- drift analysis skipped ({exc})"
        )
        return 0.0, referenced_entities, missing_entities, suggestion_notes

    all_terms_count = len(terms)
    code_drift_score = len(missing_entities) / max(all_terms_count, 1)
    return code_drift_score, referenced_entities, missing_entities, suggestion_notes


async def _outcome_correlation_analysis(
    trigger: str,
    ctx: "ProjectContext",
) -> tuple:
    """Query outcome history for decisions matching the rule trigger.

    Returns:
        A 3-tuple of (outcome_correlation_score, outcome_summary,
        worked_count, failed_count).
    """
    worked = 0
    failed = 0
    unknown = 0

    try:
        recall_result = await ctx.memory_manager.recall(
            topic=trigger,
            project_path=ctx.project_path,
            limit=20,
        )

        # Flatten recall categories to a single list of memories
        memories: list[dict[str, Any]] = []
        for cat_key in ("decisions", "patterns", "warnings", "learnings"):
            cat_list = recall_result.get(cat_key)
            if isinstance(cat_list, list):
                memories.extend(cat_list)
        # Also check wrapper keys
        if "memories" in recall_result and isinstance(recall_result["memories"], list):
            memories = recall_result["memories"]

        for mem in memories:
            w = mem.get("worked")
            if w is True:
                worked += 1
            elif w is False:
                failed += 1
            else:
                unknown += 1
    except Exception as exc:
        logger.warning("Outcome correlation analysis failed: %s", exc)

    outcome_summary = {"worked": worked, "failed": failed, "unknown": unknown}

    total_decided = worked + failed
    if total_decided > 0:
        outcome_correlation_score = failed / total_decided
    elif worked + failed + unknown > 0:
        outcome_correlation_score = 0.5  # Unknown territory
    else:
        outcome_correlation_score = 0.5  # No decisions found

    return outcome_correlation_score, outcome_summary, worked, failed


async def _analyze_rule(
    rule: Rule,
    ctx: "ProjectContext",
) -> StalenessReport:
    """Analyse a single rule for staleness across three dimensions.

    Dimensions:
        1. **Code drift** -- how many trigger terms are still found in the
           codebase via the tree-sitter code index.
        2. **Outcome correlation** -- the worked/failed ratio of decisions
           that semantically match this rule's trigger.
        3. **Age factor** -- time-based decay weighted by
           ``settings.cognitive_staleness_age_weight``.

    Each dimension produces a score from 0.0 to 1.0; the composite
    staleness score is a weighted average clamped to [0.0, 1.0].

    On error for this rule, returns a StalenessReport with
    ``staleness_score=0.0`` and a suggestion noting the analysis error,
    ensuring one bad rule does not block analysis of others.
    """
    try:
        return await _analyze_rule_inner(rule, ctx)
    except Exception as exc:
        logger.error(
            "Rule entropy analysis failed for rule #%d: %s",
            rule.id,
            exc,
            exc_info=True,
        )
        return StalenessReport(
            rule_id=rule.id,
            rule_trigger=rule.trigger,
            staleness_score=0.0,
            code_drift_score=0.0,
            outcome_correlation_score=0.0,
            age_factor=0.0,
            referenced_entities=[],
            missing_entities=[],
            outcome_summary={"worked": 0, "failed": 0, "unknown": 0},
            evolution_suggestions=[
                {
                    "type": "analysis_error",
                    "reason": f"Rule analysis failed: {exc}",
                    "current": "N/A",
                    "proposed": "Investigate and retry",
                }
            ],
        )


async def _analyze_rule_inner(
    rule: Rule,
    ctx: "ProjectContext",
) -> StalenessReport:
    """Core per-rule analysis logic, separated for clean error handling."""

    terms = _extract_terms(rule.trigger)

    # ----- (a) Code drift score -----
    (
        code_drift_score,
        referenced_entities,
        missing_entities,
        drift_notes,
    ) = await _code_drift_analysis(terms, ctx)

    # ----- (b) Outcome correlation score -----
    (
        outcome_correlation_score,
        outcome_summary,
        worked,
        failed,
    ) = await _outcome_correlation_analysis(rule.trigger, ctx)

    # ----- (c) Age factor -----
    now = datetime.now(timezone.utc)
    rule_created = rule.created_at
    if rule_created.tzinfo is None:
        rule_created = rule_created.replace(tzinfo=timezone.utc)
    days_old = max(0, (now - rule_created).days)
    age_factor = min(1.0, days_old / 365.0) * settings.cognitive_staleness_age_weight

    # ----- (d) Composite staleness score -----
    staleness_score = (
        (code_drift_score * 0.4)
        + (outcome_correlation_score * 0.4)
        + (age_factor * 0.2)
    )
    staleness_score = max(0.0, min(1.0, staleness_score))

    # ----- (e) Evolution suggestions -----
    suggestions: list[dict[str, Any]] = []

    # Notes from drift analysis (e.g. code index unavailable)
    for note in drift_notes:
        suggestions.append(
            {
                "type": "info",
                "reason": note,
                "current": "N/A",
                "proposed": "N/A",
            }
        )

    if staleness_score >= 0.2:
        # Missing entities -> suggest trigger update
        for entity in missing_entities:
            suggestions.append(
                {
                    "type": "update_trigger",
                    "reason": f"Entity '{entity}' no longer found in codebase",
                    "current": rule.trigger,
                    "proposed": "Update trigger to reference current entity names",
                }
            )

        # High failure rate -> suggest warning
        if outcome_correlation_score > 0.7:
            suggestions.append(
                {
                    "type": "add_warning",
                    "reason": (f"Rule has {failed} failures vs {worked} successes"),
                    "current": "No staleness warning",
                    "proposed": "Add warning: this rule has low success correlation",
                }
            )

        # Age-based review
        if age_factor > 0.5:
            suggestions.append(
                {
                    "type": "review_needed",
                    "reason": f"Rule is {days_old} days old without review",
                    "current": "No review date",
                    "proposed": "Schedule rule review",
                }
            )

    # ----- (f) Return report -----
    return StalenessReport(
        rule_id=rule.id,
        rule_trigger=rule.trigger,
        staleness_score=staleness_score,
        code_drift_score=code_drift_score,
        outcome_correlation_score=outcome_correlation_score,
        age_factor=age_factor,
        referenced_entities=referenced_entities,
        missing_entities=missing_entities,
        outcome_summary=outcome_summary,
        evolution_suggestions=suggestions,
    )
