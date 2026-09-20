"""Claude Code Stop hook: remind about v7 memory calls, never block.

The hook reads the stdin event (``cwd``, ``transcript_path``, ``session_id``),
analyzes the transcript, and never writes memory. Its replay-safe
``memory_store`` and ``memory_record_outcome`` suggestions are shown to the
user as a ``systemMessage``; it never emits ``decision`` and always exits 0.
"""

import contextlib
import hashlib
import json
import re
import sys
import time
from collections import deque
from pathlib import Path

from ..workspace import WorkspaceRegistry
from ._client import (
    find_project_root,
    read_hook_event,
    relative_project_path,
    run_async,
)

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
    (
        r"(?<![a-z0-9_])"
        r"(?:mcp__daem0nmcp__|daem0nmcp_)?memory_record_outcome"
        r"(?![a-z0-9_])"
    ),
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
        r"(?:i(?:'ll|'m going to| will| decided to))\s+"
        r"(?:use|implement|add|create|choose)\s+(.{20,150})",
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


def _state_file(session_id: str) -> Path:
    safe = re.sub(r"[^\w\-]", "_", session_id)
    return _state_dir() / f"stop_{safe}.json"


def _load_state(session_id: str) -> dict:
    f = _state_file(session_id)
    if f.exists():
        try:
            return json.loads(f.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass
    return {"reminder_count": 0, "last_reminder_turn": -1}


_STATE_MAX_AGE_SECONDS = 7 * 24 * 3600


def _save_state(session_id: str, state: dict) -> None:
    with contextlib.suppress(OSError):
        target = _state_file(session_id)
        target.write_text(json.dumps(state), encoding="utf-8")
        # One file per session: drop the ones no session has touched in a week.
        cutoff = time.time() - _STATE_MAX_AGE_SECONDS
        for old in target.parent.glob("stop_*.json"):
            with contextlib.suppress(OSError):
                if old.stat().st_mtime < cutoff:
                    old.unlink()


# ─── transcript reading ───────────────────────────────────────────


_TRANSCRIPT_TAIL_RECORDS = 50


def _read_transcript(path: object) -> tuple[list[dict], int]:
    """Return the last Claude Code JSONL records and the total record count.

    Only the tail is JSON-parsed (the analysis looks at the last 10 records);
    nested ``message`` objects are unwrapped.
    """
    if not isinstance(path, str) or not path or not Path(path).is_file():
        return [], 0
    tail: deque[str] = deque(maxlen=_TRANSCRIPT_TAIL_RECORDS)
    total = 0
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                if line.strip():
                    total += 1
                    tail.append(line)
    except OSError:
        return [], 0
    messages = []
    for line in tail:
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(record, dict) and isinstance(record.get("message"), dict):
            record = record["message"]
        if isinstance(record, dict):
            messages.append(record)
    return messages, total


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
    if any(_is_v7_tool_call(tool, "memory_record_outcome") for tool in tool_calls):
        return True
    return _matches_any(text, DAEM0N_OUTCOME_PATTERNS)


def _is_v7_tool_call(tool_name: str, canonical_name: str) -> bool:
    """Recognize the exact bare, OpenCode, and Claude Code tool forms."""
    normalized = tool_name.casefold()
    canonical = canonical_name.casefold()
    return normalized in {
        canonical,
        f"daem0nmcp_{canonical}",
        f"mcp__daem0nmcp__{canonical}",
    }


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


# ─── replay-safe v7 suggestions ───────────────────────────────────


def _workspace_id(project_path: str) -> str:
    return WorkspaceRegistry(default_root=project_path).default.workspace_id


def _relative_record_path(project_path: str, mentioned_path: str | None) -> str | None:
    if not mentioned_path:
        return None
    return relative_project_path(Path(project_path), mentioned_path)


def _memory_store_suggestion(
    decision: dict,
    project_path: str,
    workspace_id: str,
) -> str:
    content = decision["content"]
    relative_path = _relative_record_path(project_path, decision.get("file_path"))
    digest = hashlib.sha256(
        f"{workspace_id}\0{decision['category']}\0{content}".encode()
    ).hexdigest()[:24]
    target_arguments = {
        "record_type": decision["category"],
        "content": content,
        "rationale": "Decision extracted from the completed task",
        "idempotency_key": f"hook-stop-{digest}",
    }
    if relative_path is not None:
        target_arguments["relative_file_path"] = relative_path
    encoded = json.dumps(target_arguments, ensure_ascii=True, separators=(",", ":"))
    store_fields = [
        f'workspace_id="{workspace_id}"',
        f"record_type={json.dumps(target_arguments['record_type'])}",
        f"content={json.dumps(target_arguments['content'])}",
        f"rationale={json.dumps(target_arguments['rationale'])}",
    ]
    if relative_path is not None:
        store_fields.append(f"relative_file_path={json.dumps(relative_path)}")
    store_fields.extend(
        (
            f'idempotency_key="{target_arguments["idempotency_key"]}"',
            'preflight_token="<token-from-memory_preflight>"',
        )
    )
    return "\n".join(
        (
            (
                "  - mcp__daem0nmcp__memory_preflight("
                f'workspace_id="{workspace_id}", target_tool="memory_store", '
                f"target_arguments={encoded})"
            ),
            "    mcp__daem0nmcp__memory_store(" + ", ".join(store_fields) + ")",
        )
    )


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
    turn: int | None = None,
) -> StopResult:
    """
    Core logic extracted for in-process testing.

    * Analyses transcript content
    * Produces v7 calls without mutating memory
    * Returns result message (empty = nothing to say)
    * Updates *state* in-place for anti-loop tracking
    """
    current_turn = len(messages) if turn is None else turn

    # Anti-loop check
    reminded_recently = state.get("last_reminder_turn", -1) >= current_turn - 2
    if reminded_recently and state.get("reminder_count", 0) >= 2:
        return StopResult(message="")

    recent_content = _get_recent_assistant_content(messages)
    recent_tools = _get_recent_tool_calls(messages)

    if _matches_any(recent_content, EXPLORATION_PATTERNS):
        return StopResult(message="")

    if not _matches_any(recent_content, COMPLETION_PATTERNS):
        return StopResult(message="")

    if _has_daem0n_outcome(recent_content, recent_tools):
        return StopResult(message="")

    state["reminder_count"] = state.get("reminder_count", 0) + 1
    state["last_reminder_turn"] = current_turn

    workspace_id = _workspace_id(project_path)
    outcome_digest = hashlib.sha256(
        f"{workspace_id}\0{recent_content}".encode()
    ).hexdigest()[:24]
    outcome_key = f"hook-outcome-{outcome_digest}"
    extracted = _extract_decisions(recent_content)
    if extracted:
        suggestions = "\n".join(
            _memory_store_suggestion(decision, project_path, workspace_id)
            for decision in extracted[:3]
        )
        return StopResult(
            message=(
                "Daem0n: this task looks finished and no outcome was recorded. "
                "The hook wrote nothing. To keep these decisions, ask Claude "
                "to run:\n"
                f"{suggestions}\n"
                "Once the result is known, ask Claude to call "
                "mcp__daem0nmcp__memory_record_outcome("
                f'workspace_id="{workspace_id}", record_id="<mem_id>", '
                'outcome_text="<verified result>", worked=true, '
                f'idempotency_key="{outcome_key}").'
            )
        )

    return StopResult(
        message=(
            "Daem0n: this task looks finished and no outcome was recorded. "
            "The hook wrote nothing. To keep a durable decision, ask Claude to "
            "run memory_preflight and then memory_store for it. Once the result "
            "is known, ask Claude to call mcp__daem0nmcp__memory_record_outcome("
            f'workspace_id="{workspace_id}", record_id="<mem_id>", '
            'outcome_text="<verified result>", worked=true, '
            f'idempotency_key="{outcome_key}").'
        )
    )


# ─── main ─────────────────────────────────────────────────────────


def main() -> None:
    event = read_hook_event()
    project = find_project_root(event)
    if project is None:
        sys.exit(0)
    project_path = str(project)

    messages, turn = _read_transcript(event.get("transcript_path"))
    if not messages:
        sys.exit(0)

    session_id = event.get("session_id")
    if not isinstance(session_id, str) or not session_id:
        session_id = "default"
    state = _load_state(session_id)
    result = run_async(analyse_and_remember(project_path, messages, state, turn))
    _save_state(session_id, state)

    if result.message:
        # systemMessage is shown to the user; the hook never returns a
        # "decision", so it cannot keep the agent running.
        print(json.dumps({"systemMessage": result.message}))
    sys.exit(0)


if __name__ == "__main__":
    import warnings

    warnings.filterwarnings("ignore")

    from daem0nmcp.claude_hooks._client import run_hook_safely

    run_hook_safely(main, timeout_seconds=15)
