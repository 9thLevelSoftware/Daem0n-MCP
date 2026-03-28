"""Deprecation warning utilities for legacy tools."""

import contextvars
import logging
from typing import Any

logger = logging.getLogger(__name__)

# When True, add_deprecation is a no-op. Set by workflow_call context manager
# so that workflow dispatchers can reuse legacy tool implementations without
# emitting spurious deprecation warnings or polluting response payloads.
_in_workflow = contextvars.ContextVar("_in_workflow", default=False)


class WorkflowCall:
    """Context manager that suppresses deprecation noise during workflow dispatch."""

    def __enter__(self):
        self._token = _in_workflow.set(True)
        return self

    def __exit__(self, *exc):
        _in_workflow.reset(self._token)
        return False


def add_deprecation(
    result: dict[str, Any],
    tool_name: str,
    replacement: str,
) -> dict[str, Any]:
    """
    Add structured deprecation warning to tool output.

    The _deprecation field is structured so LLM clients can detect
    and surface the deprecation to users.  Skipped when the call
    originates from a workflow dispatcher (see workflow_call).
    """
    if _in_workflow.get():
        return result

    result["_deprecation"] = {
        "deprecated": True,
        "tool": tool_name,
        "replacement": replacement,
        "message": f"'{tool_name}' is deprecated. Use '{replacement}' instead.",
    }
    logger.warning(
        f"Deprecated tool '{tool_name}' invoked. Use '{replacement}' instead."
    )
    return result
