"""Guided admission composed from the exact-argument Covenant gate."""

from __future__ import annotations

import asyncio
import inspect
import re
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Literal, Protocol

from ...covenant import CovenantGate, CovenantLevel, InvocationScope
from ...workspace import Workspace
from .models import (
    CounselChallenge,
    CounselReason,
    CovenantBrief,
    CovenantNotice,
    InlineCounsel,
    PreflightGuidance,
)

RECOVERABLE_TOKEN_CODES = frozenset(
    {"TOKEN_EXPIRED", "TOKEN_ARGUMENT_MISMATCH", "TOKEN_REPLAYED"}
)
CounselMode = Literal["inline", "challenge", "off"]
_CONTENT_TOKEN = re.compile(r"[a-z0-9][a-z0-9_-]{3,79}")
_TOKEN_COMPONENT = re.compile(r"[_-]")
_COUNSEL_STOPWORDS = frozenset(
    {
        "memory",
        "memories",
        "store",
        "stored",
        "record",
        "records",
        "decision",
        "decisions",
        "pattern",
        "patterns",
        "learning",
        "warning",
        "warnings",
        "failed",
        "failure",
        "worked",
        "outcome",
        "this",
        "that",
        "with",
        "from",
        "into",
        "when",
        "then",
        "should",
        "would",
        "could",
        "have",
        "been",
        "will",
        "must",
        "because",
        "about",
        "using",
        "used",
    }
)


class ArgumentNormalizer(Protocol):
    def __call__(
        self,
        operation: str,
        arguments: Mapping[str, Any] | None,
        workspace: str,
    ) -> dict[str, Any]: ...


class PreflightService(Protocol):
    def guidance(
        self,
        workspace: Workspace,
        target_tool: str,
        normalized_arguments: Mapping[str, Any],
        description: str | None,
    ) -> object | Awaitable[object]: ...


@dataclass(frozen=True, slots=True)
class CeremonyOutcome:
    violation: Mapping[str, Any] | None
    notice: CovenantNotice | None
    challenge: CounselChallenge | None


def _content_tokens(value: object) -> frozenset[str]:
    tokens: set[str] = set()

    def collect(item: object) -> None:
        if isinstance(item, str):
            for match in _CONTENT_TOKEN.findall(item.casefold()):
                tokens.add(match)
                tokens.update(
                    part for part in _TOKEN_COMPONENT.split(match) if len(part) >= 4
                )
        elif isinstance(item, Mapping):
            for child in item.values():
                collect(child)
        elif isinstance(item, (list, tuple)):
            for child in item:
                collect(child)

    collect(value)
    return frozenset(tokens - _COUNSEL_STOPWORDS)


def counsel_reasons(
    level: CovenantLevel,
    guidance: PreflightGuidance,
    normalized_arguments: Mapping[str, Any],
) -> list[CounselReason]:
    reasons: list[CounselReason] = []

    def add(reason: CounselReason) -> None:
        if reason not in reasons:
            reasons.append(reason)

    if level is CovenantLevel.DESTRUCTIVE:
        add("DESTRUCTIVE_OPERATION")
    for rule in guidance.rules:
        if rule.must_not:
            add("RULE_MUST_NOT")
        if rule.ask_first:
            add("RULE_ASK_FIRST")
        if rule.warnings:
            add("RULE_WARNING")
    argument_tokens = _content_tokens(
        {
            key: value
            for key, value in normalized_arguments.items()
            if key not in {"idempotency_key", "workspace_id", "preflight_token"}
        }
    )
    path = normalized_arguments.get("relative_file_path")
    for record in guidance.records:
        related = isinstance(path, str) and path == record.relative_file_path
        if not related:
            record_tokens = _content_tokens(
                [record.excerpt, list(record.tags), record.relative_file_path]
            )
            related = len(argument_tokens & record_tokens) >= 2
        if related:
            add(
                "RELATED_WARNING"
                if record.record_type == "warning"
                else "RELATED_FAILURE"
            )
    return reasons


class CovenantCeremony:
    def __init__(
        self,
        *,
        covenant_gate: CovenantGate,
        argument_normalizer: ArgumentNormalizer,
        preflight_service: PreflightService,
        brief_reader: Callable[[Workspace], Awaitable[Mapping[str, object]]],
    ) -> None:
        self._gate = covenant_gate
        self._normalizer = argument_normalizer
        self._preflight = preflight_service
        self._brief_reader = brief_reader

    async def admit(
        self,
        tool_name: str,
        arguments: Mapping[str, Any],
        workspace: Workspace,
        scope: InvocationScope | None,
        *,
        preflight_token: str | None,
        consume_capability: bool,
        counsel_mode: CounselMode,
    ) -> CeremonyOutcome:
        def authorize(token: str | None) -> Mapping[str, Any] | None:
            return self._gate.authorize(
                tool_name,
                arguments,
                scope,
                preflight_token=token,
                consume_capability=consume_capability,
            )

        def code(violation: Mapping[str, Any] | None) -> object:
            return violation.get("violation") if violation is not None else None

        token = preflight_token
        violation = authorize(token)
        notice: CovenantNotice | None = None
        if code(violation) in RECOVERABLE_TOKEN_CODES:
            token = None
            violation = authorize(None)
        if code(violation) == "COMMUNION_REQUIRED" and scope is not None:
            try:
                brief = CovenantBrief.model_validate(
                    await self._brief_reader(workspace)
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                return CeremonyOutcome(violation, notice, None)
            self._gate.record_briefing(scope)
            notice = CovenantNotice(auto_brief=brief)
            violation = authorize(token)
            if code(violation) in RECOVERABLE_TOKEN_CODES:
                token = None
                violation = authorize(None)
        if (
            code(violation) in {"COUNSEL_REQUIRED", "TOKEN_MISSING"}
            and scope is not None
            and counsel_mode != "off"
        ):
            call_args = {
                key: value
                for key, value in arguments.items()
                if key != "preflight_token"
            }
            try:
                level = self._gate.policy.resolve(tool_name, call_args)
                normalized = self._normalizer(
                    tool_name, call_args, scope.canonical_workspace
                )
                raw_guidance = self._preflight.guidance(
                    workspace, tool_name, normalized, None
                )
                if inspect.isawaitable(raw_guidance):
                    raw_guidance = await raw_guidance
                guidance = PreflightGuidance.model_validate(raw_guidance)
                new_token = self._gate.issue_preflight(scope, tool_name, call_args)
                expires_at = datetime.fromtimestamp(
                    self._gate.authority.verify(new_token)["exp"], timezone.utc
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                return CeremonyOutcome(violation, notice, None)
            reasons = counsel_reasons(level, guidance, normalized)
            if counsel_mode == "challenge" and not reasons:
                reasons = ["TASK_ADMISSION"]
            if not reasons and counsel_mode == "inline":
                violation = authorize(new_token)
                if violation is None:
                    notice = CovenantNotice(
                        auto_brief=notice.auto_brief if notice is not None else None,
                        counsel=InlineCounsel(
                            target_tool=tool_name, must_do=guidance.must_do[:20]
                        ),
                    )
                return CeremonyOutcome(violation, notice, None)
            return CeremonyOutcome(
                {
                    "status": "blocked",
                    "violation": "COUNSEL_REQUIRED",
                    "operation": tool_name,
                },
                notice,
                CounselChallenge(
                    guidance=guidance,
                    preflight_token=new_token,
                    expires_at=expires_at,
                    reasons=reasons,
                ),
            )
        return CeremonyOutcome(violation, notice, None)


__all__ = [
    "ArgumentNormalizer",
    "CeremonyOutcome",
    "CounselMode",
    "CovenantCeremony",
    "PreflightService",
    "RECOVERABLE_TOKEN_CODES",
    "counsel_reasons",
]
