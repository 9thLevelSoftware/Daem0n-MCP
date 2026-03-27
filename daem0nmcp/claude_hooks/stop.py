"""
Claude Code Stop hook - auto-capture decisions and outcome reminders.

Runs when Claude finishes responding (Stop / SubagentStop). Reads the
conversation transcript, extracts decisions via regex, auto-remembers
them, and reminds about recording outcomes. Never blocks.
"""

import json
import os
import re
import sys
from pathlib import Path

from ._client import get_managers, get_project_path, run_async, succeed

# ─── transcript analysis ───────────────────────────────────────────

COMPLETION_PATTERNS = [
    r"\ball\s+(?:tasks?|todos?|items?)\s+(?:are\s+)?(?:complete|done|finished)\b",
    r"\bcompleted?\s+all\s+(?:tasks?|todos?|items?)\b",
    r"\bmarking\s+.*\s+as\s+completed?\b",
    r"\btask\s+(?:is\s+)?(?:complete|done|finished)\b",
    r"\bimplementation\s+(?:is\s+)?(?:complete|done|finished)\b",
    r"\bsuccessfully\s+(?:implemented|completed|finished)\b",
    r"\bwork\s+(?:is\s+)?(?:complete|done|finished)\b",
    r"\bchanges?\s+(?:have\s+been\s+)?(?:committed|pushed)\b",
    r"\bpull\s+request\s+(?:created|opened)\b",
    r"\bfeature\s+(?:is\s+)?(?:complete|ready|done)\b",
    r"\bbug\s+(?:fix\s+)?(?:is\s+)?(?:complete|done|deployed)\b",
]

DAEM0N_OUTCOME_PATTERNS = [
    r"mcp__daem0nmcp__record_outcome",
    r"record_outcome",
    r"recorded?\s+(?:the\s+)?outcome",
    r"outcome\s+(?:has\s+been\s+)?recorded",
]

EXPLORATION_PATTERNS = [
    r"\bhere(?:'s|\s+is)\s+(?:the\s+)?(?:information|answer|explanation)\b",
    r"\bi\s+found\b",
    r"\blet\s+me\s+explain\b",
    r"\bthe\s+(?:code|file|function)\s+(?:is|does|works)\b",
    r"\bbased\s+on\s+my\s+(?:research|analysis|exploration)\b",
]

DECISION_PATTERNS = [
    (
        r"(?:i(?:'ll|'m going to| will| decided to))\s+(?:use|implement|add|create|choose)\s+(.{20,150})",
        "decision",
    ),
    (
        r"(?:chose|selected|picked|went with)\s+(.{20,100})\s+(?:because|since|for)",
        "decision",
    ),
    (
        r"(?:the (?:best|right|correct) (?:approach|solution|way) is)\s+(.{20,150})",
        "decision",
    ),
    (r"(?:pattern|approach|convention):\s*(.{20,150})", "pattern"),
    (r"(?:warning|caution|avoid|don't|do not):\s*(.{20,150})", "warning"),
    (
        r"(?:learned|discovered|found out|realized)\s+(?:that\s+)?(.{20,150})",
        "learning",
    ),
]

FILE_MENTION_PATTERN = (
    r"(?:in|to|from|at|file)\s+[`'\"]?([a-zA-Z0-9_/.\\\-]+\.[a-zA-Z0-9]+)[`'\"]?"
)


# ─── anti-loop state ───────────────────────────────────────────────


def _state_dir() -> Path:
    d = Path.home() / ".daem0nmcp" / "hook_state"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _state_file() -> Path:
    session_id = os.environ.get("CLAUDE_SESSION_ID", "default")
    safe = re.sub(r"[^\w\-]", "_", session_id)
    return _state_dir() / f"stop_{safe}.json"


def _load_state() -> dict:
    f = _state_file()
    if f.exists():
        try:
            return json.loads(f.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass
    return {"reminder_count": 0, "last_reminder_turn": -1}


def _save_state(state: dict) -> None:
    try:
        _state_file().write_text(json.dumps(state), encoding="utf-8")
    except OSError:
        pass


# ─── transcript reading ───────────────────────────────────────────


def _read_transcript() -> list[dict]:
    path = os.environ.get("CLAUDE_TRANSCRIPT_PATH", "")
    if not path or not Path(path).exists():
        return []
    messages = []
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        messages.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue
    except OSError:
        pass
    return messages


def _get_recent_assistant_content(messages: list[dict], lookback: int = 5) -> str:
    parts: list[str] = []
    for msg in reversed(messages[-lookback:]):
        if msg.get("role") != "assistant":
            continue
        content = msg.get("content", "")
        if isinstance(content, str):
            parts.append(content)
        elif isinstance(content, list):
            for part in content:
                if isinstance(part, dict):
                    if part.get("type") == "text":
                        parts.append(part.get("text", ""))
                    elif part.get("type") == "tool_use":
                        parts.append(part.get("name", ""))
                elif isinstance(part, str):
                    parts.append(part)
    return " ".join(parts)


def _get_recent_tool_calls(messages: list[dict], lookback: int = 10) -> list[str]:
    tools: list[str] = []
    for msg in messages[-lookback:]:
        content = msg.get("content", [])
        if isinstance(content, list):
            for part in content:
                if isinstance(part, dict) and part.get("type") == "tool_use":
                    tools.append(part.get("name", ""))
    return tools


# ─── pattern helpers ───────────────────────────────────────────────


def _matches_any(text: str, patterns: list[str]) -> bool:
    text_lower = text.lower()
    return any(re.search(p, text_lower) for p in patterns)


def _has_daem0n_outcome(text: str, tool_calls: list[str]) -> bool:
    if any("record_outcome" in t.lower() for t in tool_calls):
        return True
    return _matches_any(text, DAEM0N_OUTCOME_PATTERNS)


def _extract_decisions(text: str) -> list[dict]:
    decisions: list[dict] = []
    seen: set[str] = set()
    for pattern, category in DECISION_PATTERNS:
        for match in re.finditer(pattern, text, re.IGNORECASE):
            content = re.sub(r"\s+", " ", match.group(1).strip()).rstrip(".,;:")
            if len(content) < 20 or content.lower() in seen:
                continue
            seen.add(content.lower())
            ctx = text[max(0, match.start() - 200) : match.end() + 200]
            file_match = re.search(FILE_MENTION_PATTERN, ctx)
            decisions.append(
                {
                    "category": category,
                    "content": content[:200],
                    "file_path": file_match.group(1) if file_match else None,
                }
            )
    return decisions[:5]


# ─── auto-remember via direct import ──────────────────────────────


async def _async_auto_remember(decisions: list[dict], project_path: str) -> list[int]:
    """Remember decisions directly using MemoryManager."""
    db, memory, _rules = get_managers(project_path)
    await db.init_db()
    ids: list[int] = []
    for d in decisions:
        result = await memory.remember(
            category=d["category"],
            content=d["content"],
            rationale="Auto-captured from conversation",
            file_path=d.get("file_path"),
            project_path=project_path,
        )
        mem_id = result.get("id")
        if mem_id:
            ids.append(mem_id)
    return ids


# ─── testable core logic ─────────────────────────────────────────


class StopResult:
    """Value object returned by ``analyse_and_remember``."""

    __slots__ = ("message",)

    def __init__(self, message: str):
        self.message = message


async def analyse_and_remember(
    project_path: str,
    messages: list[dict],
    state: dict,
) -> StopResult:
    """
    Core logic extracted for in-process testing.

    * Analyses transcript content
    * Auto-remembers decisions
    * Returns result message (empty = nothing to say)
    * Updates *state* in-place for anti-loop tracking
    """
    current_turn = len(messages)

    # Anti-loop check
    if state.get("last_reminder_turn", -1) >= current_turn - 2:
        if state.get("reminder_count", 0) >= 2:
            return StopResult(message="")

    recent_content = _get_recent_assistant_content(messages)
    recent_tools = _get_recent_tool_calls(messages)

    if _matches_any(recent_content, EXPLORATION_PATTERNS):
        return StopResult(message="")

    if not _matches_any(recent_content, COMPLETION_PATTERNS):
        return StopResult(message="")

    if _has_daem0n_outcome(recent_content, recent_tools):
        return StopResult(message="")

    # Auto-extract and remember decisions
    extracted = _extract_decisions(recent_content)
    if extracted:
        memory_ids = await _async_auto_remember(extracted, project_path)
        if memory_ids:
            summaries = "\n".join(f"  - {d['content'][:80]}" for d in extracted[:3])
            return StopResult(
                message=(
                    f"[Daem0n auto-captured] {len(memory_ids)} decision(s):\n"
                    f"{summaries}\n"
                    f"Memory IDs: {memory_ids}. Remember to record_outcome() when results are known."
                )
            )

    # Generic reminder
    state["reminder_count"] = state.get("reminder_count", 0) + 1
    state["last_reminder_turn"] = current_turn

    return StopResult(
        message=(
            "[Daem0n whispers] Task completion detected. "
            "If you made decisions worth tracking, consider: "
            "inscribe(action='remember', content='...') and later "
            "reflect(action='record_outcome', memory_id=<id>, outcome='...', worked=True/False)"
        )
    )


# ─── main ─────────────────────────────────────────────────────────


def main() -> None:
    project_path = get_project_path()
    if project_path is None:
        sys.exit(0)

    messages = _read_transcript()
    if not messages:
        sys.exit(0)

    state = _load_state()
    result = run_async(analyse_and_remember(project_path, messages, state))
    _save_state(state)

    if result.message:
        succeed(result.message)
    sys.exit(0)


if __name__ == "__main__":
    import warnings

    warnings.filterwarnings("ignore")

    from daem0nmcp.claude_hooks._client import run_hook_safely

    run_hook_safely(main, timeout_seconds=15)
