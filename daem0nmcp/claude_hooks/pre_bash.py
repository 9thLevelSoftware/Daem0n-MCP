"""
Claude Code PreToolUse hook for Bash - rule guidance and enforcement.

Reads active Daem0n rules in read-only SQLite mode without mutating the
workspace database. In Claude Code stdin event mode, emits a non-blocking
``PreToolUse`` ``additionalContext`` reminder and exits 0.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from dataclasses import dataclass
from pathlib import Path

from ..similarity import TFIDFIndex
from ..storage_activation import PointerValidationError, resolve_active_database
from ._client import (
    block,
    find_project_root,
    get_command_from_input,
    get_project_path,
    read_hook_event,
    run_async,
    succeed,
)


class PreBashResult:
    """Value object returned by ``async_main``."""

    __slots__ = ("blocked", "message")

    def __init__(self, blocked: bool, message: str):
        self.blocked = blocked
        self.message = message


@dataclass(frozen=True, slots=True)
class _RuleCandidate:
    rule_id: str | int
    trigger: str
    must_not: tuple[str, ...]
    warnings: tuple[str, ...]
    priority: int


def _parse_str_list(raw: object) -> tuple[str, ...]:
    if isinstance(raw, list):
        return tuple(str(item) for item in raw if isinstance(item, str) and item)
    if not isinstance(raw, str) or not raw:
        return ()
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return ()
    if not isinstance(parsed, list):
        return ()
    return tuple(str(item) for item in parsed if isinstance(item, str) and item)


def _resolve_db_file(project_path: str) -> Path | None:
    storage_dir = Path(project_path) / ".daem0nmcp" / "storage"
    if not storage_dir.is_dir():
        return None
    try:
        return resolve_active_database(storage_dir).path
    except (OSError, PointerValidationError):
        return None


def _load_active_rules(project_path: str) -> list[_RuleCandidate]:
    db_file = _resolve_db_file(project_path)
    if db_file is None:
        return []
    conn: sqlite3.Connection | None = None
    try:
        conn = sqlite3.connect(
            f"{db_file.resolve().as_uri()}?mode=ro",
            uri=True,
            timeout=2.0,
        )
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=2000")
        conn.execute("PRAGMA query_only=ON")
        tables = {
            str(row[0])
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name IN ('governance_rules', 'rules')"
            )
        }
        rules: list[_RuleCandidate] = []
        if "governance_rules" in tables:
            rows = conn.execute(
                "SELECT rule_id, trigger, must_not_json, warnings_json, priority "
                "FROM governance_rules WHERE enabled = 1"
            ).fetchall()
            for idx, row in enumerate(rows):
                trigger = row["trigger"]
                if not isinstance(trigger, str) or not trigger.strip():
                    continue
                rules.append(
                    _RuleCandidate(
                        rule_id=str(row["rule_id"] or f"v7_{idx}"),
                        trigger=trigger,
                        must_not=_parse_str_list(row["must_not_json"]),
                        warnings=_parse_str_list(row["warnings_json"]),
                        priority=int(row["priority"] or 0),
                    )
                )
        if not rules and "rules" in tables:
            rows = conn.execute(
                "SELECT id, trigger, must_not, warnings, priority "
                "FROM rules WHERE enabled = 1"
            ).fetchall()
            for idx, row in enumerate(rows):
                trigger = row["trigger"]
                if not isinstance(trigger, str) or not trigger.strip():
                    continue
                rules.append(
                    _RuleCandidate(
                        rule_id=int(row["id"] if row["id"] is not None else idx + 1),
                        trigger=trigger,
                        must_not=_parse_str_list(row["must_not"]),
                        warnings=_parse_str_list(row["warnings"]),
                        priority=int(row["priority"] or 0),
                    )
                )
        return rules
    except (OSError, sqlite3.Error, TypeError, ValueError):
        return []
    finally:
        if conn is not None:
            conn.close()


def _evaluate_command(project_path: str, command: str) -> PreBashResult:
    candidates = _load_active_rules(project_path)
    if not candidates:
        return PreBashResult(blocked=False, message="")

    index = TFIDFIndex()
    by_id: dict[str | int, _RuleCandidate] = {}
    for candidate in candidates:
        by_id[candidate.rule_id] = candidate
        index.add_document(candidate.rule_id, candidate.trigger)

    matches = index.search(f"executing bash: {command[:200]}", top_k=10, threshold=0.15)
    if not matches:
        return PreBashResult(blocked=False, message="")

    score_map = dict(matches)
    sorted_matches = sorted(
        (
            (rule_id, score_map[rule_id], by_id[rule_id])
            for rule_id, _ in matches
            if rule_id in by_id
        ),
        key=lambda item: (item[2].priority, item[1]),
        reverse=True,
    )
    must_not: list[str] = []
    warnings: list[str] = []
    for _, _, rule in sorted_matches:
        for item in rule.must_not:
            if item not in must_not:
                must_not.append(item)
        for warning in rule.warnings:
            if warning not in warnings:
                warnings.append(warning)

    if must_not:
        lines = ["[Daem0n blocks] Rule violation for bash command:", "MUST NOT:"]
        for item in must_not:
            lines.append(f"  - {item}")
        return PreBashResult(blocked=True, message="\n".join(lines))

    if warnings:
        lines = ["[Daem0n warns] Rule guidance for bash command:"]
        for warning in warnings:
            lines.append(f"  - {warning}")
        return PreBashResult(blocked=False, message="\n".join(lines))

    return PreBashResult(blocked=False, message="")


async def async_main(project_path: str, command: str) -> PreBashResult:
    """Core async logic.  Returns a result instead of calling sys.exit."""
    return _evaluate_command(project_path, command)


def reminder(event: dict) -> str | None:
    """Return Claude Code PreToolUse hookSpecificOutput JSON for a Bash event."""
    project = find_project_root(event)
    if project is None:
        return None
    tool_input = event.get("tool_input")
    command = tool_input.get("command") if isinstance(tool_input, dict) else None
    if not isinstance(command, str) or not command.strip():
        return None
    result = _evaluate_command(str(project), command)
    if not result.message:
        return None
    return json.dumps(
        {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "additionalContext": result.message,
            }
        }
    )


def main() -> None:
    event = read_hook_event()
    if event:
        try:
            output = reminder(event)
        except Exception:
            output = None
        if output:
            print(output)
        sys.exit(0)

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
