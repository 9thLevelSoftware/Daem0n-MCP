"""
Claude Code PreToolUse hook for Bash - rule enforcement.

Checks the command against Daem0n rules. If a ``must_not`` rule matches,
blocks the command (exit 2). Warnings are output as context (exit 0).
"""

import sys

from ._client import (
    block,
    get_command_from_input,
    get_managers,
    get_project_path,
    run_async,
    succeed,
)


class PreBashResult:
    """Value object returned by ``async_main``."""

    __slots__ = ("blocked", "message")

    def __init__(self, blocked: bool, message: str):
        self.blocked = blocked
        self.message = message


async def async_main(project_path: str, command: str) -> PreBashResult:
    """Core async logic.  Returns a result instead of calling sys.exit."""
    db, _memory, rules = get_managers(project_path)
    await db.init_db()

    result = await rules.check_rules(f"executing bash: {command[:200]}")

    guidance = result.get("guidance")
    if not guidance:
        return PreBashResult(blocked=False, message="")

    must_not = guidance.get("must_not", [])
    warnings = guidance.get("warnings", [])

    if must_not:
        lines = ["[Daem0n blocks] Rule violation for bash command:", "MUST NOT:"]
        for item in must_not:
            lines.append(f"  - {item}")
        return PreBashResult(blocked=True, message="\n".join(lines))

    if warnings:
        lines = ["[Daem0n warns] Rule guidance for bash command:"]
        for w in warnings:
            lines.append(f"  - {w}")
        return PreBashResult(blocked=False, message="\n".join(lines))

    return PreBashResult(blocked=False, message="")


def main() -> None:
    project_path = get_project_path()
    if project_path is None:
        sys.exit(0)

    command = get_command_from_input()
    if not command:
        sys.exit(0)

    result = run_async(async_main(project_path, command))

    if result.blocked:
        block(result.message)

    if result.message:
        succeed(result.message)

    sys.exit(0)


if __name__ == "__main__":
    import warnings

    warnings.filterwarnings("ignore")

    from daem0nmcp.claude_hooks._client import run_hook_safely

    run_hook_safely(main, timeout_seconds=10)
