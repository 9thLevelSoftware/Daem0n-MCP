"""Action-aware Sacred Covenant policy and scoped capability enforcement."""

from __future__ import annotations

import base64
import hashlib
import hmac
import inspect
import json
import math
import os
import re
import secrets
import threading
import time
import warnings
from collections import OrderedDict
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from enum import Enum
from functools import wraps
from pathlib import Path
from types import MappingProxyType
from typing import Any, Protocol

COUNSEL_TTL_SECONDS = 300
MAX_CAPABILITY_TOKEN_BYTES = 8192
MAX_ARGUMENT_NESTING = 64
MAX_CAPABILITIES_PER_SCOPE = 128
_TOKEN_FIELDS = frozenset(
    {
        "v",
        "kid",
        "principal",
        "session",
        "workspace",
        "operation",
        "args_sha256",
        "iat",
        "exp",
        "nonce",
    }
)
_EXCLUDED_ARGUMENTS = frozenset(
    {
        "project_path",
        "preflight_token",
        "target_operation",
        "target_args",
        "_client_meta",
        "pp",
    }
)
_PATH_ARGUMENTS = frozenset({"file_path", "path", "linked_path"})
_B64URL_RE = re.compile(r"^[A-Za-z0-9_-]+$")


class CovenantLevel(str, Enum):
    EXEMPT = "exempt"
    COMMUNION = "communion"
    COUNSEL = "counsel"
    DESTRUCTIVE = "destructive"


class UnknownCovenantOperation(ValueError):  # noqa: N818 -- retained public Python API name
    """Raised when a workflow/action is absent from the authoritative policy."""


class ArgumentNormalizationError(ValueError):
    """Raised when action arguments cannot be safely canonicalized."""


class TokenValidationError(ValueError):
    """Internal typed token rejection."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class CovenantStateCapacityError(RuntimeError):
    """Raised instead of evicting another live preflight grant."""

    code = "CAPABILITY_DEGRADED"


def _fixed(level: CovenantLevel, *operations: str) -> dict[str, CovenantLevel]:
    return dict.fromkeys(operations, level)


_FIXED_POLICY = {
    **_fixed(
        CovenantLevel.EXEMPT,
        "commune.briefing",
        "commune.health",
        "commune.covenant",
    ),
    **_fixed(
        CovenantLevel.COMMUNION,
        "commune.active_context",
        "commune.triggers",
        "commune.updates",
        "consult.preflight",
        "consult.recall",
        "consult.recall_file",
        "consult.recall_entity",
        "consult.recall_hierarchical",
        "consult.search",
        "consult.check_rules",
        "consult.compress",
        "reflect.outcome",
        "reflect.verify",
        "understand.index",
        "understand.find",
        "understand.impact",
        "understand.refactor",
        "govern.list_rules",
        "govern.list_triggers",
        "explore.related",
        "explore.chain",
        "explore.graph",
        "explore.stats",
        "explore.communities",
        "explore.community_detail",
        "explore.entities",
        "explore.evolution",
        "explore.versions",
        "explore.at_time",
        "maintain.rebuild_index",
        "maintain.export",
        "maintain.list_projects",
        "simulate_decision",
        "evolve_rule",
    ),
    **_fixed(
        CovenantLevel.COUNSEL,
        "inscribe.remember",
        "inscribe.remember_batch",
        "inscribe.link",
        "inscribe.pin",
        "inscribe.activate",
        "inscribe.ingest",
        "govern.add_rule",
        "govern.update_rule",
        "govern.add_trigger",
        "explore.rebuild_communities",
        "explore.backfill_entities",
        "maintain.link_project",
        "debate_internal",
    ),
    **_fixed(
        CovenantLevel.DESTRUCTIVE,
        "inscribe.unlink",
        "inscribe.deactivate",
        "inscribe.clear_active",
        "reflect.execute",
        "govern.remove_trigger",
        "maintain.archive",
        "maintain.import_data",
        "maintain.unlink_project",
    ),
}
_SENSITIVE_OPERATIONS = frozenset(
    {
        "understand.todos",
        "maintain.prune",
        "maintain.cleanup",
        "maintain.compact",
        "maintain.purge_dream_spam",
        "maintain.consolidate",
    }
)


class CovenantPolicy:
    """Single immutable action policy used by all authorization paths."""

    def __init__(self) -> None:
        self._fixed = MappingProxyType(dict(_FIXED_POLICY))
        self.operations = frozenset(self._fixed) | _SENSITIVE_OPERATIONS

    def resolve(
        self, operation: str, arguments: Mapping[str, Any] | None = None
    ) -> CovenantLevel:
        if operation in self._fixed:
            return self._fixed[operation]
        if operation not in _SENSITIVE_OPERATIONS:
            raise UnknownCovenantOperation(operation)
        args = arguments or {}
        if operation == "understand.todos":
            auto_remember = args.get("auto_remember", False)
            if type(auto_remember) is not bool:
                raise ArgumentNormalizationError("auto_remember must be boolean")
            return CovenantLevel.COUNSEL if auto_remember else CovenantLevel.COMMUNION
        if operation == "maintain.consolidate":
            archive_sources = args.get("archive_sources", False)
            if type(archive_sources) is not bool:
                raise ArgumentNormalizationError("archive_sources must be boolean")
            return (
                CovenantLevel.DESTRUCTIVE if archive_sources else CovenantLevel.COUNSEL
            )
        dry_run = args.get("dry_run", True)
        if type(dry_run) is not bool:
            raise ArgumentNormalizationError("dry_run must be boolean")
        return CovenantLevel.DESTRUCTIVE if not dry_run else CovenantLevel.COMMUNION


COVENANT_POLICY = CovenantPolicy()


def _schema(**defaults: Any) -> Mapping[str, Any]:
    return MappingProxyType(defaults)


# These schemas describe the exact parameters passed from each dispatcher to
# its selected handler. Irrelevant broad-wrapper parameters are rejected.
ACTION_ARGUMENT_DEFAULTS: Mapping[str, Mapping[str, Any]] = MappingProxyType(
    {
        "commune.briefing": _schema(focus_areas=None, visual=False),
        "commune.active_context": _schema(),
        "commune.triggers": _schema(file_path=None, tags=None, entities=None, limit=5),
        "commune.health": _schema(),
        "commune.covenant": _schema(visual=False),
        "commune.updates": _schema(since=None, interval_seconds=10),
        "consult.preflight": _schema(),
        "consult.recall": _schema(
            topic=None,
            categories=None,
            tags=None,
            file_path=None,
            offset=0,
            limit=10,
            since=None,
            until=None,
            include_linked=False,
            visual=False,
            condensed=False,
            as_of_time=None,
        ),
        "consult.recall_file": _schema(file_path=None, limit=10),
        "consult.recall_entity": _schema(entity_name=None, entity_type=None),
        "consult.recall_hierarchical": _schema(
            topic=None, include_members=False, limit=10
        ),
        "consult.search": _schema(
            query=None,
            limit=10,
            offset=0,
            include_meta=False,
            highlight=False,
            highlight_start="<b>",
            highlight_end="</b>",
        ),
        "consult.check_rules": _schema(action_desc=None, context=None),
        "consult.compress": _schema(
            compress_text=None, rate=None, content_type=None, preserve_code=True
        ),
        "inscribe.remember": _schema(
            category=None,
            content=None,
            rationale=None,
            context=None,
            tags=None,
            file_path=None,
            happened_at=None,
        ),
        "inscribe.remember_batch": _schema(memories=None),
        "inscribe.link": _schema(
            source_id=None,
            target_id=None,
            relationship=None,
            description=None,
        ),
        "inscribe.unlink": _schema(source_id=None, target_id=None, relationship=None),
        "inscribe.pin": _schema(memory_id=None, pinned=True),
        "inscribe.activate": _schema(
            memory_id=None, reason=None, priority=0, expires_in_hours=None
        ),
        "inscribe.deactivate": _schema(memory_id=None),
        "inscribe.clear_active": _schema(),
        "inscribe.ingest": _schema(url=None, topic=None, chunk_size=2000),
        "reflect.outcome": _schema(memory_id=None, outcome_text=None, worked=None),
        "reflect.verify": _schema(text=None, categories=None, as_of_time=None),
        "reflect.execute": _schema(code=None, timeout_seconds=None),
        "understand.index": _schema(path=None, patterns=None),
        "understand.find": _schema(query=None, limit=20),
        "understand.impact": _schema(entity_name=None),
        "understand.todos": _schema(path=None, auto_remember=False, types=None),
        "understand.refactor": _schema(file_path=None),
        "govern.add_rule": _schema(
            trigger=None,
            must_do=None,
            must_not=None,
            ask_first=None,
            warnings=None,
            priority=0,
        ),
        "govern.update_rule": _schema(
            rule_id=None,
            must_do=None,
            must_not=None,
            ask_first=None,
            warnings=None,
            priority=0,
            enabled=None,
        ),
        "govern.list_rules": _schema(enabled_only=True, limit=50),
        "govern.add_trigger": _schema(
            trigger_type=None,
            pattern=None,
            recall_topic=None,
            recall_categories=None,
            priority=0,
        ),
        "govern.list_triggers": _schema(active_only=True),
        "govern.remove_trigger": _schema(trigger_id=None),
        "explore.related": _schema(
            memory_id=None,
            relationship_types=None,
            direction="both",
            max_depth=2,
        ),
        "explore.chain": _schema(start_memory_id=None, end_memory_id=None, max_depth=2),
        "explore.graph": _schema(
            memory_ids=None,
            topic=None,
            format="json",
            visual=False,
            include_orphans=False,
        ),
        "explore.stats": _schema(),
        "explore.communities": _schema(
            level=None, parent_community_id=None, visual=False
        ),
        "explore.community_detail": _schema(community_id=None),
        "explore.rebuild_communities": _schema(min_community_size=2, resolution=1.0),
        "explore.entities": _schema(entity_type=None, limit=20),
        "explore.backfill_entities": _schema(),
        "explore.evolution": _schema(
            entity_name=None,
            entity_type=None,
            include_invalidated=True,
            entity_id=None,
        ),
        "explore.versions": _schema(memory_id=None, limit=20),
        "explore.at_time": _schema(memory_id=None, timestamp=None),
        "maintain.prune": _schema(
            older_than_days=90,
            categories=None,
            min_recall_count=5,
            protect_successful=True,
            dry_run=True,
        ),
        "maintain.archive": _schema(memory_id=None, archived=True),
        "maintain.cleanup": _schema(dry_run=True, merge_duplicates=True),
        "maintain.compact": _schema(summary=None, limit=10, topic=None, dry_run=True),
        "maintain.rebuild_index": _schema(),
        "maintain.export": _schema(include_vectors=False),
        "maintain.import_data": _schema(data=None, merge=True),
        "maintain.link_project": _schema(
            linked_path=None, relationship="related", label=None
        ),
        "maintain.unlink_project": _schema(linked_path=None),
        "maintain.list_projects": _schema(),
        "maintain.consolidate": _schema(archive_sources=False),
        "maintain.purge_dream_spam": _schema(dry_run=True),
        "simulate_decision": _schema(decision_id=None),
        "evolve_rule": _schema(rule_id=None),
        "debate_internal": _schema(
            topic=None,
            advocate_position=None,
            challenger_position=None,
        ),
    }
)

ACTION_REQUIRED_ARGUMENTS: Mapping[str, frozenset[str]] = MappingProxyType(
    {
        "simulate_decision": frozenset({"decision_id"}),
        "evolve_rule": frozenset(),
        "debate_internal": frozenset(
            {"topic", "advocate_position", "challenger_position"}
        ),
    }
)


def _normalize_json(value: Any, _depth: int = 0) -> Any:
    if _depth > MAX_ARGUMENT_NESTING:
        raise ArgumentNormalizationError(
            f"JSON arguments may not exceed {MAX_ARGUMENT_NESTING} nesting levels"
        )
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, str):
        try:
            value.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise ArgumentNormalizationError(
                "strings must contain valid Unicode scalar values"
            ) from exc
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ArgumentNormalizationError("non-finite numbers are not supported")
        return value
    if isinstance(value, list):
        return [_normalize_json(item, _depth + 1) for item in value]
    if isinstance(value, dict):
        if not all(isinstance(key, str) for key in value):
            raise ArgumentNormalizationError("object keys must be strings")
        return {
            _normalize_json(key, _depth + 1): _normalize_json(value[key], _depth + 1)
            for key in sorted(value)
        }
    raise ArgumentNormalizationError(
        f"unsupported argument type: {type(value).__name__}"
    )


def _normalize_path(value: str, workspace: str) -> str:
    try:
        root = Path(workspace).resolve()
        candidate = Path(value)
        if candidate.is_absolute():
            resolved = candidate.resolve()
        else:
            resolved = (root / candidate).resolve()
            resolved.relative_to(root)
    except (OSError, RuntimeError, ValueError) as exc:
        raise ArgumentNormalizationError(
            "path argument must remain inside the authorized workspace"
        ) from exc
    return os.path.normcase(str(resolved))


def normalize_operation_arguments(
    operation: str,
    arguments: Mapping[str, Any] | None,
    workspace: str,
) -> dict[str, Any]:
    """Apply action defaults and produce the exact JSON-safe handler arguments."""
    COVENANT_POLICY.resolve(operation, arguments)
    try:
        defaults = ACTION_ARGUMENT_DEFAULTS[operation]
    except KeyError as exc:
        raise UnknownCovenantOperation(operation) from exc
    supplied = dict(arguments or {})
    _, separator, action = operation.partition(".")
    if separator:
        supplied_action = supplied.pop("action", action)
        if supplied_action != action:
            raise ArgumentNormalizationError("action does not match target operation")
    for excluded in _EXCLUDED_ARGUMENTS:
        supplied.pop(excluded, None)
    if operation == "consult.preflight":
        supplied.pop("description", None)
    unknown = set(supplied) - set(defaults)
    if unknown:
        raise ArgumentNormalizationError(
            "arguments are not accepted by this action: " + ", ".join(sorted(unknown))
        )
    effective = {**defaults, **supplied}
    if separator:
        effective = {"action": action, **effective}
    for required_name in ACTION_REQUIRED_ARGUMENTS.get(operation, frozenset()):
        if effective.get(required_name) is None:
            raise ArgumentNormalizationError("required action argument is missing")
    normalized = _normalize_json(effective)
    for key in _PATH_ARGUMENTS:
        value = normalized.get(key)
        if isinstance(value, str) and value:
            normalized[key] = _normalize_path(value, workspace)
    return normalized


def canonical_json(value: Any) -> bytes:
    normalized = _normalize_json(value)
    return json.dumps(
        normalized,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def operation_args_digest(
    operation: str, arguments: Mapping[str, Any] | None, workspace: str
) -> str:
    normalized = normalize_operation_arguments(operation, arguments, workspace)
    return hashlib.sha256(canonical_json(normalized)).hexdigest()


@dataclass(frozen=True, slots=True)
class InvocationScope:
    principal_id: str
    transport_session_id: str
    canonical_workspace: str

    def __post_init__(self) -> None:
        if not self.principal_id or not self.transport_session_id:
            raise ValueError("principal and transport session must be non-empty")
        object.__setattr__(
            self,
            "canonical_workspace",
            os.path.normcase(str(Path(self.canonical_workspace).resolve())),
        )


@dataclass(slots=True)
class _ScopeState:
    briefed_at: int | None = None
    last_seen: int = 0
    issued: OrderedDict[str, tuple[str, str, int]] = field(default_factory=OrderedDict)
    consumed: OrderedDict[str, int] = field(default_factory=OrderedDict)


class CovenantStateStore:
    """Bounded in-memory authorization state keyed by full invocation scope."""

    def __init__(
        self,
        *,
        clock: Callable[[], int | float] = time.time,
        ttl_seconds: int = 3600,
        max_scopes: int = 1024,
        max_capabilities_per_scope: int = MAX_CAPABILITIES_PER_SCOPE,
    ) -> None:
        if max_scopes <= 0:
            raise ValueError("max_scopes must be positive")
        if max_capabilities_per_scope <= 0:
            raise ValueError("max_capabilities_per_scope must be positive")
        self._clock = clock
        self._ttl_seconds = ttl_seconds
        self._max_scopes = max_scopes
        self._max_capabilities_per_scope = max_capabilities_per_scope
        self._states: OrderedDict[InvocationScope, _ScopeState] = OrderedDict()
        self._lock = threading.Lock()

    def _now(self) -> int:
        return int(self._clock())

    def _prune_locked(self, now: int) -> None:
        stale = [
            scope
            for scope, state in self._states.items()
            if now - state.last_seen > self._ttl_seconds
        ]
        for scope in stale:
            self._states.pop(scope, None)

    def _state_locked(self, scope: InvocationScope, now: int) -> _ScopeState:
        self._prune_locked(now)
        state = self._states.get(scope)
        if state is None:
            if len(self._states) >= self._max_scopes:
                raise CovenantStateCapacityError(
                    "live Covenant scope capacity is unavailable"
                )
            state = _ScopeState(last_seen=now)
            self._states[scope] = state
        else:
            state.last_seen = now
            self._states.move_to_end(scope)
        for nonce, grant in tuple(state.issued.items()):
            if grant[2] <= now:
                state.issued.pop(nonce, None)
        for nonce, expiry in tuple(state.consumed.items()):
            if expiry <= now:
                state.consumed.pop(nonce, None)
        return state

    def mark_briefed(self, scope: InvocationScope) -> None:
        with self._lock:
            now = self._now()
            self._state_locked(scope, now).briefed_at = now

    def is_briefed(self, scope: InvocationScope) -> bool:
        with self._lock:
            now = self._now()
            self._prune_locked(now)
            state = self._states.get(scope)
            if state is None or state.briefed_at is None:
                return False
            state.last_seen = now
            self._states.move_to_end(scope)
            return True

    def record_issued(
        self,
        scope: InvocationScope,
        nonce: str,
        operation: str,
        args_sha256: str,
        expires_at: int,
    ) -> None:
        with self._lock:
            now = self._now()
            state = self._state_locked(scope, now)
            if (
                nonce not in state.issued
                and len(state.issued) >= self._max_capabilities_per_scope
            ):
                raise CovenantStateCapacityError(
                    "live preflight grant capacity is unavailable"
                )
            state.issued[nonce] = (operation, args_sha256, expires_at)

    def has_matching_grant(
        self, scope: InvocationScope, operation: str, args_sha256: str
    ) -> bool:
        with self._lock:
            now = self._now()
            state = self._states.get(scope)
            if state is None:
                return False
            state = self._state_locked(scope, now)
            return any(
                grant_operation == operation and grant_digest == args_sha256
                for grant_operation, grant_digest, _ in state.issued.values()
            )

    def consume_nonce(self, scope: InvocationScope, nonce: str, exp: int) -> bool:
        """Atomically consume one issued nonce; false covers replay/unknown nonce."""
        with self._lock:
            now = self._now()
            state = self._states.get(scope)
            if state is None:
                return False
            if now >= exp:
                state.issued.pop(nonce, None)
                raise TokenValidationError("TOKEN_EXPIRED")
            state = self._state_locked(scope, now)
            if nonce in state.consumed or nonce not in state.issued:
                return False
            state.issued.pop(nonce, None)
            state.consumed[nonce] = exp
            while len(state.consumed) > self._max_capabilities_per_scope:
                state.consumed.popitem(last=False)
            return True

    def is_nonce_active(
        self,
        scope: InvocationScope,
        nonce: str,
        operation: str,
        args_sha256: str,
        exp: int,
    ) -> bool:
        """Validate one issued nonce without consuming the capability."""

        with self._lock:
            now = self._now()
            if now >= exp:
                state = self._states.get(scope)
                if state is not None:
                    state.issued.pop(nonce, None)
                raise TokenValidationError("TOKEN_EXPIRED")
            state = self._states.get(scope)
            if state is None:
                return False
            state = self._state_locked(scope, now)
            return state.issued.get(nonce) == (
                operation,
                args_sha256,
                exp,
            )

    def status(self, scope: InvocationScope) -> dict[str, Any]:
        with self._lock:
            now = self._now()
            state = self._states.get(scope)
            if state is None:
                return {"briefed": False, "active_capabilities": 0}
            state = self._state_locked(scope, now)
            return {
                "briefed": state.briefed_at is not None,
                "briefed_at": state.briefed_at,
                "active_capabilities": len(state.issued),
            }


@dataclass(frozen=True, slots=True)
class _IssuedCapability:
    token: str
    nonce: str
    expires_at: int
    args_sha256: str


class CapabilityAuthority:
    """Issue and strictly verify versioned HMAC-SHA-256 capabilities."""

    def __init__(
        self,
        *,
        secret: bytes,
        kid: str,
        clock: Callable[[], int | float] = time.time,
        ttl_seconds: int = COUNSEL_TTL_SECONDS,
    ) -> None:
        if not isinstance(secret, bytes) or len(secret) < 32:
            raise ValueError("capability secret must contain at least 32 bytes")
        if not kid:
            raise ValueError("capability key id must be non-empty")
        self._secret = secret
        self.kid = kid
        self._clock = clock
        self._clock_lock = threading.Lock()
        self._max_observed_verify_time: int | None = None
        self.ttl_seconds = ttl_seconds

    @staticmethod
    def _encode(value: bytes) -> str:
        return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")

    @staticmethod
    def _decode(value: str) -> bytes:
        if not value or "=" in value or not _B64URL_RE.fullmatch(value):
            raise TokenValidationError("TOKEN_TAMPERED")
        try:
            decoded = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
        except Exception as exc:
            raise TokenValidationError("TOKEN_TAMPERED") from exc
        if CapabilityAuthority._encode(decoded) != value:
            raise TokenValidationError("TOKEN_TAMPERED")
        return decoded

    def _verification_time(self) -> int:
        """Return wall time while failing closed after an observed rollback."""
        now = int(self._clock())
        with self._clock_lock:
            if (
                self._max_observed_verify_time is not None
                and now < self._max_observed_verify_time
            ):
                raise TokenValidationError("TOKEN_EXPIRED")
            self._max_observed_verify_time = now
        return now

    def issue(
        self,
        scope: InvocationScope,
        operation: str,
        args_sha256: str,
    ) -> _IssuedCapability:
        now = int(self._clock())
        nonce = self._encode(secrets.token_bytes(16))
        payload = {
            "v": 1,
            "kid": self.kid,
            "principal": scope.principal_id,
            "session": scope.transport_session_id,
            "workspace": scope.canonical_workspace,
            "operation": operation,
            "args_sha256": args_sha256,
            "iat": now,
            "exp": now + self.ttl_seconds,
            "nonce": nonce,
        }
        payload_bytes = canonical_json(payload)
        signature = hmac.new(self._secret, payload_bytes, hashlib.sha256).digest()
        token = f"{self._encode(payload_bytes)}.{self._encode(signature)}"
        return _IssuedCapability(token, nonce, payload["exp"], args_sha256)

    @staticmethod
    def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise TokenValidationError("TOKEN_TAMPERED")
            result[key] = value
        return result

    def verify(self, token: str) -> dict[str, Any]:
        if not isinstance(token, str):
            raise TokenValidationError("TOKEN_TAMPERED")
        if token.lstrip().startswith("{"):
            raise TokenValidationError("TOKEN_LEGACY_UNSUPPORTED")
        try:
            token_bytes = token.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise TokenValidationError("TOKEN_TAMPERED") from exc
        if len(token_bytes) > MAX_CAPABILITY_TOKEN_BYTES:
            raise TokenValidationError("TOKEN_TAMPERED")
        try:
            payload_segment, signature_segment = token.split(".")
        except ValueError as exc:
            raise TokenValidationError("TOKEN_TAMPERED") from exc
        payload_bytes = self._decode(payload_segment)
        signature = self._decode(signature_segment)
        try:
            payload = json.loads(
                payload_bytes.decode("utf-8"), object_pairs_hook=self._unique_object
            )
        except (UnicodeDecodeError, ValueError, RecursionError) as exc:
            raise TokenValidationError("TOKEN_TAMPERED") from exc
        if not isinstance(payload, dict) or set(payload) != _TOKEN_FIELDS:
            raise TokenValidationError("TOKEN_TAMPERED")
        try:
            canonical_payload = canonical_json(payload)
        except (ValueError, RecursionError) as exc:
            raise TokenValidationError("TOKEN_TAMPERED") from exc
        if canonical_payload != payload_bytes:
            raise TokenValidationError("TOKEN_TAMPERED")
        if type(payload["v"]) is not int or payload["v"] != 1:
            raise TokenValidationError("TOKEN_TAMPERED")
        if payload["kid"] != self.kid:
            raise TokenValidationError("TOKEN_TAMPERED")
        for field_name in (
            "kid",
            "principal",
            "session",
            "workspace",
            "operation",
            "args_sha256",
            "nonce",
        ):
            if not isinstance(payload[field_name], str) or not payload[field_name]:
                raise TokenValidationError("TOKEN_TAMPERED")
        if not re.fullmatch(r"[0-9a-f]{64}", payload["args_sha256"]):
            raise TokenValidationError("TOKEN_TAMPERED")
        try:
            nonce_bytes = self._decode(payload["nonce"])
        except TokenValidationError as exc:
            raise TokenValidationError("TOKEN_TAMPERED") from exc
        if len(nonce_bytes) != 16:
            raise TokenValidationError("TOKEN_TAMPERED")
        if type(payload["iat"]) is not int or type(payload["exp"]) is not int:
            raise TokenValidationError("TOKEN_TAMPERED")
        if (
            payload["exp"] <= payload["iat"]
            or payload["exp"] - payload["iat"] != self.ttl_seconds
        ):
            raise TokenValidationError("TOKEN_TAMPERED")
        expected = hmac.new(self._secret, payload_bytes, hashlib.sha256).digest()
        if not hmac.compare_digest(signature, expected):
            raise TokenValidationError("TOKEN_TAMPERED")
        now = self._verification_time()
        if now < payload["iat"]:
            raise TokenValidationError("TOKEN_TAMPERED")
        if now >= payload["exp"]:
            raise TokenValidationError("TOKEN_EXPIRED")
        return payload


def _safe_remedy_args(arguments: Mapping[str, Any] | None) -> dict[str, Any]:
    return {
        key: value
        for key, value in (arguments or {}).items()
        if key not in _EXCLUDED_ARGUMENTS and key != "action"
    }


class CovenantViolation:
    """Stable, non-secret-bearing authorization failures."""

    @staticmethod
    def build(
        code: str,
        operation: str,
        workspace: str | None,
        arguments: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        response: dict[str, Any] = {
            "status": "blocked",
            "violation": code,
            "operation": operation,
        }
        if workspace:
            response["workspace"] = workspace
        if code == "COMMUNION_REQUIRED":
            response["message"] = (
                "The Sacred Covenant requires communion in this invocation scope."
            )
            response["remedy"] = {
                "tool": "commune",
                "args": {"action": "briefing", "project_path": workspace},
            }
        elif code in {"COUNSEL_REQUIRED", "TOKEN_MISSING"}:
            response["message"] = (
                "A bound preflight capability is required for this operation."
            )
            response["remedy"] = {
                "tool": "consult",
                "args": {
                    "action": "preflight",
                    "target_operation": operation,
                    "target_args": _safe_remedy_args(arguments),
                    "project_path": workspace,
                },
            }
        elif code == "IDENTITY_UNAVAILABLE":
            response["message"] = (
                "A server-authenticated invocation identity is unavailable."
            )
        elif code == "UNKNOWN_COVENANT_OPERATION":
            response["message"] = (
                "The workflow/action is not classified by the Covenant policy."
            )
        else:
            response["message"] = "The preflight capability was rejected."
        return response

    @staticmethod
    def communion_required(project_path: str) -> dict[str, Any]:
        return CovenantViolation.build("COMMUNION_REQUIRED", "unknown", project_path)

    @staticmethod
    def counsel_required(tool_name: str, project_path: str) -> dict[str, Any]:
        operation = LEGACY_OPERATION_MAP.get(tool_name, tool_name)
        return CovenantViolation.build("COUNSEL_REQUIRED", operation, project_path)

    @staticmethod
    def counsel_expired(
        tool_name: str, project_path: str, age_seconds: int
    ) -> dict[str, Any]:
        operation = LEGACY_OPERATION_MAP.get(tool_name, tool_name)
        return CovenantViolation.build("TOKEN_EXPIRED", operation, project_path)


class WorkspaceAccessDenied(PermissionError):  # noqa: N818 -- retained public Python API name
    code = "UNAUTHORIZED_WORKSPACE"


class IssuedCapability(Protocol):
    @property
    def token(self) -> str: ...
    @property
    def nonce(self) -> str: ...
    @property
    def expires_at(self) -> int: ...
    @property
    def args_sha256(self) -> str: ...


class CapabilityIssuer(Protocol):
    @property
    def ttl_seconds(self) -> int: ...
    def issue(
        self, scope: InvocationScope, operation: str, args_sha256: str
    ) -> IssuedCapability: ...
    def verify(self, token: str) -> dict[str, Any]: ...


class CovenantPolicyResolver(Protocol):
    @property
    def operations(self) -> frozenset[str]: ...
    def resolve(
        self, operation: str, arguments: Mapping[str, Any] | None = None
    ) -> CovenantLevel: ...


class CovenantGate:
    """Authoritative policy, communion, and one-use capability gate."""

    def __init__(
        self,
        *,
        state_store: CovenantStateStore,
        authority: CapabilityIssuer,
        policy: CovenantPolicyResolver = COVENANT_POLICY,
        argument_normalizer: Callable[
            [str, Mapping[str, Any] | None, str], dict[str, Any]
        ] = normalize_operation_arguments,
        workspace_authorizer: Callable[[InvocationScope], bool] | None = None,
    ) -> None:
        self.state_store = state_store
        self.authority = authority
        self.policy = policy
        self._argument_normalizer = argument_normalizer
        self._workspace_authorizer = workspace_authorizer

    def workspace_authorized(self, scope: InvocationScope) -> bool:
        if self._workspace_authorizer is None:
            return True
        try:
            return self._workspace_authorizer(scope) is True
        except Exception:
            return False

    def fingerprint(
        self,
        operation: str,
        arguments: Mapping[str, Any] | None,
        scope: InvocationScope,
    ) -> str:
        normalized = self._argument_normalizer(
            operation, arguments, scope.canonical_workspace
        )
        return hashlib.sha256(canonical_json(normalized)).hexdigest()

    def record_briefing(self, scope: InvocationScope) -> None:
        if not self.workspace_authorized(scope):
            raise WorkspaceAccessDenied("UNAUTHORIZED_WORKSPACE")
        self.state_store.mark_briefed(scope)

    def issue_preflight(
        self,
        scope: InvocationScope,
        target_operation: str,
        target_args: Mapping[str, Any] | None,
    ) -> str:
        if not self.workspace_authorized(scope):
            raise WorkspaceAccessDenied("UNAUTHORIZED_WORKSPACE")
        if not self.state_store.is_briefed(scope):
            raise PermissionError("COMMUNION_REQUIRED")
        level = self.policy.resolve(target_operation, target_args)
        if level not in {CovenantLevel.COUNSEL, CovenantLevel.DESTRUCTIVE}:
            raise ValueError("PREFLIGHT_TARGET_NOT_PROTECTED")
        digest = self.fingerprint(target_operation, target_args, scope)
        capability = self.authority.issue(scope, target_operation, digest)
        try:
            self.state_store.record_issued(
                scope,
                capability.nonce,
                target_operation,
                capability.args_sha256,
                capability.expires_at,
            )
        except Exception:
            discard = getattr(self.authority, "discard", None)
            if callable(discard):
                discard(capability.token)
            raise
        return capability.token

    def authorize(
        self,
        operation: str,
        arguments: Mapping[str, Any] | None,
        scope: InvocationScope | None,
        *,
        preflight_token: str | None = None,
        consume_capability: bool = True,
    ) -> dict[str, Any] | None:
        try:
            level = self.policy.resolve(operation, arguments)
        except (ArgumentNormalizationError, UnknownCovenantOperation):
            return CovenantViolation.build(
                "UNKNOWN_COVENANT_OPERATION"
                if operation not in self.policy.operations
                else "TOKEN_ARGUMENT_MISMATCH",
                operation,
                None,
            )
        if scope is not None and not self.workspace_authorized(scope):
            return CovenantViolation.build("UNAUTHORIZED_WORKSPACE", operation, None)
        if level is CovenantLevel.EXEMPT:
            return None
        if scope is None:
            return CovenantViolation.build("IDENTITY_UNAVAILABLE", operation, None)
        workspace = scope.canonical_workspace
        payload: dict[str, Any] | None = None
        if (
            level in {CovenantLevel.COUNSEL, CovenantLevel.DESTRUCTIVE}
            and preflight_token
        ):
            try:
                payload = self.authority.verify(preflight_token)
            except TokenValidationError as exc:
                return CovenantViolation.build(exc.code, operation, workspace)
        if not self.state_store.is_briefed(scope):
            return CovenantViolation.build(
                "COMMUNION_REQUIRED", operation, workspace, arguments
            )
        if level is CovenantLevel.COMMUNION:
            return None
        try:
            digest = self.fingerprint(operation, arguments, scope)
        except (ArgumentNormalizationError, UnknownCovenantOperation):
            return CovenantViolation.build(
                "TOKEN_ARGUMENT_MISMATCH", operation, workspace
            )
        if not preflight_token:
            code = (
                "TOKEN_MISSING"
                if self.state_store.has_matching_grant(scope, operation, digest)
                else "COUNSEL_REQUIRED"
            )
            return CovenantViolation.build(code, operation, workspace, arguments)
        assert payload is not None
        if (
            payload["principal"] != scope.principal_id
            or payload["session"] != scope.transport_session_id
            or os.path.normcase(payload["workspace"]) != workspace
        ):
            return CovenantViolation.build("TOKEN_SCOPE_MISMATCH", operation, workspace)
        if payload["operation"] != operation:
            return CovenantViolation.build(
                "TOKEN_OPERATION_MISMATCH", operation, workspace
            )
        if payload["args_sha256"] != digest:
            return CovenantViolation.build(
                "TOKEN_ARGUMENT_MISMATCH", operation, workspace
            )
        try:
            if consume_capability:
                admitted = self.state_store.consume_nonce(
                    scope, payload["nonce"], payload["exp"]
                )
            else:
                admitted = self.state_store.is_nonce_active(
                    scope,
                    payload["nonce"],
                    operation,
                    digest,
                    payload["exp"],
                )
        except TokenValidationError as exc:
            return CovenantViolation.build(exc.code, operation, workspace)
        if not admitted:
            return CovenantViolation.build("TOKEN_REPLAYED", operation, workspace)
        return None


def authority_from_environment(
    *,
    local_stdio: bool,
    environ: Mapping[str, str] | None = None,
    clock: Callable[[], int | float] = time.time,
) -> CapabilityAuthority | None:
    """Build configured remote authority or an ephemeral stdio authority."""
    env = os.environ if environ is None else environ
    configured = env.get("DAEM0NMCP_TOKEN_SECRET")
    if configured is not None:
        secret = configured.encode("utf-8")
        if len(secret) < 32:
            return None
        return CapabilityAuthority(
            secret=secret,
            kid=env.get("DAEM0NMCP_TOKEN_KEY_ID", "primary"),
            clock=clock,
        )
    if not local_stdio:
        return None
    return CapabilityAuthority(
        secret=secrets.token_bytes(32),
        kid="local-ephemeral",
        clock=clock,
    )


@dataclass(frozen=True, slots=True)
class _Admission:
    operation: str
    args_sha256: str


invocation_scope_var: ContextVar[InvocationScope | None] = ContextVar(
    "covenant_invocation_scope", default=None
)
covenant_gate_var: ContextVar[CovenantGate | None] = ContextVar(
    "covenant_gate", default=None
)
admitted_call_var: ContextVar[_Admission | None] = ContextVar(
    "covenant_admitted_call", default=None
)
workspace_resolver_var: ContextVar[Callable[[str | None], Any] | None] = ContextVar(
    "covenant_workspace_resolver", default=None
)


@contextmanager
def installed_invocation(
    scope: InvocationScope,
    gate: CovenantGate,
    *,
    workspace_resolver: Callable[[str | None], Any] | None = None,
) -> Iterator[None]:
    """Install an explicit local/test scope for direct Python invocation."""
    scope_token = invocation_scope_var.set(scope)
    gate_token = covenant_gate_var.set(gate)
    resolver_token = workspace_resolver_var.set(workspace_resolver)
    try:
        yield
    finally:
        workspace_resolver_var.reset(resolver_token)
        covenant_gate_var.reset(gate_token)
        invocation_scope_var.reset(scope_token)


def _resolve_requested_workspace(arguments: Mapping[str, Any]) -> str:
    selector = arguments.get("pp") or arguments.get("project_path")
    resolver = workspace_resolver_var.get()
    if resolver is None:
        from .context_manager import workspace_registry

        resolver = workspace_registry.resolve
    resolved = resolver(selector)
    root = getattr(resolved, "root", resolved)
    return os.path.normcase(str(Path(root).resolve()))


def _workspace_scope_violation(
    operation: str,
    arguments: Mapping[str, Any],
    scope: InvocationScope,
) -> dict[str, Any] | None:
    try:
        requested_workspace = _resolve_requested_workspace(arguments)
    except (OSError, RuntimeError, ValueError):
        return CovenantViolation.build(
            "TOKEN_SCOPE_MISMATCH", operation, scope.canonical_workspace
        )
    if requested_workspace != scope.canonical_workspace:
        return CovenantViolation.build(
            "TOKEN_SCOPE_MISMATCH", operation, scope.canonical_workspace
        )
    return None


def authorize_operation_call(
    operation: str,
    arguments: Mapping[str, Any],
    preflight_token: str | None = None,
) -> dict[str, Any] | None:
    """Apply the authoritative gate to a direct registered tool call."""
    direct_arguments = _select_operation_arguments(operation, arguments)
    gate = covenant_gate_var.get()
    scope = invocation_scope_var.get()
    if gate is None:
        try:
            level = COVENANT_POLICY.resolve(operation, direct_arguments)
        except (ArgumentNormalizationError, UnknownCovenantOperation):
            return CovenantViolation.build(
                "UNKNOWN_COVENANT_OPERATION"
                if operation not in COVENANT_POLICY.operations
                else "TOKEN_ARGUMENT_MISMATCH",
                operation,
                None,
            )
        if level is CovenantLevel.EXEMPT:
            return None
        return CovenantViolation.build("IDENTITY_UNAVAILABLE", operation, None)
    if scope is not None:
        scope_violation = _workspace_scope_violation(operation, direct_arguments, scope)
        if scope_violation is not None:
            return scope_violation
        admitted = admitted_call_var.get()
        try:
            fingerprint = gate.fingerprint(operation, direct_arguments, scope)
        except ArgumentNormalizationError:
            return CovenantViolation.build(
                "TOKEN_ARGUMENT_MISMATCH",
                operation,
                scope.canonical_workspace,
            )
        except UnknownCovenantOperation:
            return CovenantViolation.build(
                "UNKNOWN_COVENANT_OPERATION",
                operation,
                scope.canonical_workspace,
            )
        if (
            admitted is not None
            and admitted.operation == operation
            and admitted.args_sha256 == fingerprint
        ):
            return None
    return gate.authorize(
        operation,
        direct_arguments,
        scope,
        preflight_token=preflight_token,
    )


def _select_operation_arguments(
    operation: str, arguments: Mapping[str, Any]
) -> dict[str, Any]:
    """Select only fields belonging to one resolved action schema."""
    defaults = ACTION_ARGUMENT_DEFAULTS.get(operation, {})
    return {
        key: value
        for key, value in arguments.items()
        if key in defaults or key in _EXCLUDED_ARGUMENTS or key == "action"
    }


@contextmanager
def installed_operation_admission(
    operation: str, arguments: Mapping[str, Any]
) -> Iterator[None]:
    """Install exact nested admission for one already-authorized dispatch."""
    gate = covenant_gate_var.get()
    scope = invocation_scope_var.get()
    if gate is None or scope is None:
        yield
        return
    fingerprint = gate.fingerprint(
        operation,
        _select_operation_arguments(operation, arguments),
        scope,
    )
    admission_token = admitted_call_var.set(_Admission(operation, fingerprint))
    try:
        yield
    finally:
        admitted_call_var.reset(admission_token)


def authorize_workflow_call(
    workflow: str,
    action: str,
    arguments: Mapping[str, Any],
    preflight_token: str | None = None,
) -> dict[str, Any] | None:
    """Apply the same gate to direct consolidated wrapper calls."""
    return authorize_operation_call(f"{workflow}.{action}", arguments, preflight_token)


def authorize_legacy_call(
    tool_name: str,
    arguments: Mapping[str, Any],
    preflight_token: str | None = None,
) -> dict[str, Any] | None:
    """Route a legacy Python entry point through its canonical operation."""
    operation = LEGACY_OPERATION_MAP.get(tool_name)
    if operation is None:
        return CovenantViolation.build("UNKNOWN_COVENANT_OPERATION", tool_name, None)
    return authorize_operation_call(operation, arguments, preflight_token)


def record_current_briefing() -> bool:
    gate = covenant_gate_var.get()
    scope = invocation_scope_var.get()
    if gate is None or scope is None:
        return False
    gate.record_briefing(scope)
    return True


def issue_current_preflight(
    target_operation: str, target_args: Mapping[str, Any] | None
) -> str:
    gate = covenant_gate_var.get()
    scope = invocation_scope_var.get()
    if gate is None or scope is None:
        raise PermissionError("IDENTITY_UNAVAILABLE")
    return gate.issue_preflight(scope, target_operation, target_args)


def issue_current_preflight_response(
    target_operation: str,
    target_args: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Issue a capability or return one stable, non-secret violation."""
    scope = invocation_scope_var.get()
    workspace = scope.canonical_workspace if scope is not None else None
    try:
        token = issue_current_preflight(target_operation, target_args)
    except UnknownCovenantOperation:
        return CovenantViolation.build(
            "UNKNOWN_COVENANT_OPERATION", target_operation, workspace
        )
    except ArgumentNormalizationError:
        return CovenantViolation.build(
            "TOKEN_ARGUMENT_MISMATCH", target_operation, workspace
        )
    except PermissionError as exc:
        code = str(exc)
        if code not in {"COMMUNION_REQUIRED", "IDENTITY_UNAVAILABLE"}:
            code = "IDENTITY_UNAVAILABLE"
        return CovenantViolation.build(code, target_operation, workspace)
    except ValueError:
        return CovenantViolation.build(
            "PREFLIGHT_TARGET_NOT_PROTECTED", target_operation, workspace
        )
    return {
        "preflight_token": token,
        "target_operation": target_operation,
        "expires_in_seconds": COUNSEL_TTL_SECONDS,
    }


def current_covenant_status() -> dict[str, Any]:
    gate = covenant_gate_var.get()
    scope = invocation_scope_var.get()
    if gate is None or scope is None:
        return {"briefed": False, "active_capabilities": 0}
    return gate.state_store.status(scope)


# Public deprecated Python entry points map to the same authoritative policy.
LEGACY_ENTRYPOINTS: Mapping[str, str] = MappingProxyType(
    {
        "remember": "inscribe.remember",
        "remember_batch": "inscribe.remember_batch",
        "recall": "consult.recall",
        "recall_visual": "consult.recall",
        "record_outcome": "reflect.outcome",
        "recall_for_file": "consult.recall_file",
        "recall_by_entity": "consult.recall_entity",
        "recall_hierarchical": "consult.recall_hierarchical",
        "search_memories": "consult.search",
        "find_related": "explore.related",
        "get_related_memories": "explore.related",
        "get_memory_versions": "explore.versions",
        "get_memory_at_time": "explore.at_time",
        "compact_memories": "maintain.compact",
        "cleanup_memories": "maintain.cleanup",
        "archive_memory": "maintain.archive",
        "pin_memory": "inscribe.pin",
        "add_rule": "govern.add_rule",
        "check_rules": "consult.check_rules",
        "list_rules": "govern.list_rules",
        "update_rule": "govern.update_rule",
        "get_briefing": "commune.briefing",
        "get_briefing_visual": "commune.briefing",
        "get_covenant_status": "commune.covenant",
        "get_covenant_status_visual": "commune.covenant",
        "context_check": "consult.preflight",
        "check_for_updates": "commune.updates",
        "health": "commune.health",
        "verify_facts": "reflect.verify",
        "scan_todos": "understand.todos",
        "index_project": "understand.index",
        "find_code": "understand.find",
        "analyze_impact": "understand.impact",
        "propose_refactor": "understand.refactor",
        "rebuild_index": "maintain.rebuild_index",
        "export_data": "maintain.export",
        "import_data": "maintain.import_data",
        "prune_memories": "maintain.prune",
        "link_memories": "inscribe.link",
        "unlink_memories": "inscribe.unlink",
        "trace_chain": "explore.related",
        "get_graph": "explore.graph",
        "get_graph_visual": "explore.graph",
        "get_graph_stats": "explore.stats",
        "rebuild_communities": "explore.rebuild_communities",
        "list_communities": "explore.communities",
        "list_communities_visual": "explore.communities",
        "get_community_details": "explore.community_detail",
        "set_active_context": "inscribe.activate",
        "get_active_context": "commune.active_context",
        "remove_from_active_context": "inscribe.deactivate",
        "clear_active_context": "inscribe.clear_active",
        "add_context_trigger": "govern.add_trigger",
        "list_context_triggers": "govern.list_triggers",
        "remove_context_trigger": "govern.remove_trigger",
        "check_context_triggers": "commune.triggers",
        "link_projects": "maintain.link_project",
        "unlink_projects": "maintain.unlink_project",
        "list_linked_projects": "maintain.list_projects",
        "consolidate_linked_databases": "maintain.consolidate",
        "compress_context": "consult.compress",
        "execute_python": "reflect.execute",
        "ingest_doc": "inscribe.ingest",
        "trace_causal_path": "explore.chain",
        "trace_evolution": "explore.evolution",
        "list_entities": "explore.entities",
        "backfill_entities": "explore.backfill_entities",
    }
)
# Compatibility alias; there is only one legacy classification inventory.
LEGACY_OPERATION_MAP = LEGACY_ENTRYPOINTS


@dataclass(frozen=True, slots=True)
class LegacyArgumentAdapter:
    """Explicit translation from a deprecated leaf signature to policy args."""

    renames: Mapping[str, str] = field(default_factory=lambda: MappingProxyType({}))
    fixed: Mapping[str, Any] = field(default_factory=lambda: MappingProxyType({}))
    excluded: Mapping[str, str] = field(default_factory=lambda: MappingProxyType({}))
    use_scope_workspace: bool = False


def _legacy_adapter(
    *,
    renames: Mapping[str, str] | None = None,
    fixed: Mapping[str, Any] | None = None,
    excluded: Mapping[str, str] | None = None,
    use_scope_workspace: bool = False,
) -> LegacyArgumentAdapter:
    return LegacyArgumentAdapter(
        renames=MappingProxyType(dict(renames or {})),
        fixed=MappingProxyType(dict(fixed or {})),
        excluded=MappingProxyType(dict(excluded or {})),
        use_scope_workspace=use_scope_workspace,
    )


_DEFAULT_LEGACY_ADAPTER = _legacy_adapter()
_SPECIAL_LEGACY_ADAPTERS = {
    "recall": _legacy_adapter(fixed={"visual": False}),
    "recall_visual": _legacy_adapter(fixed={"visual": True}),
    "record_outcome": _legacy_adapter(renames={"outcome": "outcome_text"}),
    "find_related": _legacy_adapter(
        excluded={"limit": "semantic result limit has no graph-depth equivalent"}
    ),
    "check_rules": _legacy_adapter(renames={"action": "action_desc"}),
    "get_briefing": _legacy_adapter(fixed={"visual": False}),
    "get_briefing_visual": _legacy_adapter(fixed={"visual": True}),
    "get_covenant_status": _legacy_adapter(fixed={"visual": False}),
    "get_covenant_status_visual": _legacy_adapter(fixed={"visual": True}),
    "context_check": _legacy_adapter(
        excluded={
            "description": "advisory preflight declaration",
            "target_operation": "validated by capability issuance",
            "target_args": "validated by capability issuance",
        }
    ),
    "get_graph": _legacy_adapter(fixed={"visual": False, "include_orphans": False}),
    "get_graph_visual": _legacy_adapter(fixed={"visual": True, "format": "json"}),
    "list_communities": _legacy_adapter(
        fixed={"parent_community_id": None, "visual": False}
    ),
    "list_communities_visual": _legacy_adapter(fixed={"visual": True}),
    "compress_context": _legacy_adapter(
        renames={"context": "compress_text"},
        use_scope_workspace=True,
    ),
}
LEGACY_ARGUMENT_ADAPTERS: Mapping[str, LegacyArgumentAdapter] = MappingProxyType(
    {
        name: _SPECIAL_LEGACY_ADAPTERS.get(name, _DEFAULT_LEGACY_ADAPTER)
        for name in LEGACY_ENTRYPOINTS
    }
)


def _adapt_legacy_arguments(
    entrypoint: str,
    arguments: Mapping[str, Any],
    function_globals: Mapping[str, Any],
) -> dict[str, Any]:
    adapter = LEGACY_ARGUMENT_ADAPTERS[entrypoint]
    adapted = {}
    for name, value in arguments.items():
        if name == "project_path" or name in adapter.excluded:
            continue
        adapted[adapter.renames.get(name, name)] = value
    adapted.update(adapter.fixed)

    if adapter.use_scope_workspace:
        scope = invocation_scope_var.get()
        selector = scope.canonical_workspace if scope is not None else None
    else:
        selector = arguments.get("project_path")
        if not selector:
            selector = function_globals.get("_default_project_path")
    if selector:
        adapted["project_path"] = selector
    return adapted


def legacy_entrypoint(entrypoint: str) -> Callable[[Callable], Callable]:
    """Guard one deprecated public Python leaf before any handler code."""
    if entrypoint not in LEGACY_ENTRYPOINTS:
        raise ValueError(f"unknown legacy entrypoint: {entrypoint}")

    def decorator(func: Callable) -> Callable:
        signature = inspect.signature(func)

        @wraps(func)
        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            preflight_token = kwargs.pop("preflight_token", None)
            bound = signature.bind(*args, **kwargs)
            bound.apply_defaults()
            adapted = _adapt_legacy_arguments(
                entrypoint, bound.arguments, func.__globals__
            )
            violation = authorize_operation_call(
                LEGACY_ENTRYPOINTS[entrypoint],
                adapted,
                preflight_token,
            )
            if violation is not None:
                return violation
            return await func(*args, **kwargs)

        return wrapper

    return decorator


COVENANT_EXEMPT_TOOLS = frozenset(
    name
    for name, operation in LEGACY_OPERATION_MAP.items()
    if COVENANT_POLICY.resolve(operation, {}) is CovenantLevel.EXEMPT
)
COMMUNION_REQUIRED_TOOLS = frozenset(
    name
    for name, operation in LEGACY_OPERATION_MAP.items()
    if COVENANT_POLICY.resolve(operation, {}) is not CovenantLevel.EXEMPT
)
COUNSEL_REQUIRED_TOOLS = frozenset(
    name
    for name, operation in LEGACY_OPERATION_MAP.items()
    if COVENANT_POLICY.resolve(operation, {})
    in {CovenantLevel.COUNSEL, CovenantLevel.DESTRUCTIVE}
)


class PreflightToken:
    """Retired legacy token API retained only as an explicit rejection seam."""

    @classmethod
    def issue(cls, *args: Any, **kwargs: Any) -> PreflightToken:
        raise RuntimeError("TOKEN_LEGACY_UNSUPPORTED")

    @classmethod
    def verify(cls, serialized: str, project_path: str) -> None:
        return None


class CovenantEnforcer:
    """Compatibility facade over the scoped gate; project state is never read."""

    def __init__(self, session_manager: Any = None) -> None:
        self._session_manager = session_manager

    async def check_communion(self, project_path: str) -> dict[str, Any] | None:
        gate = covenant_gate_var.get()
        scope = invocation_scope_var.get()
        if gate is None or scope is None:
            return CovenantViolation.build("IDENTITY_UNAVAILABLE", "unknown", None)
        if not gate.state_store.is_briefed(scope):
            return CovenantViolation.build(
                "COMMUNION_REQUIRED", "unknown", scope.canonical_workspace
            )
        return None

    async def check_counsel(
        self,
        tool_name: str,
        project_path: str,
        ttl_seconds: int = COUNSEL_TTL_SECONDS,
    ) -> dict[str, Any] | None:
        operation = LEGACY_OPERATION_MAP.get(tool_name)
        if operation is None:
            return CovenantViolation.build(
                "UNKNOWN_COVENANT_OPERATION", tool_name, None
            )
        return authorize_operation_call(operation, {"project_path": project_path})


_get_project_context_callback: Callable[[str], Any] | None = None


def set_context_callback(callback: Callable[[str], Any]) -> None:
    """Retain the old callback registration without using it for authorization."""
    global _get_project_context_callback
    _get_project_context_callback = callback


def _deprecated_decorator(name: str, func: Callable) -> Callable:
    warnings.warn(
        f"The {name} decorator is deprecated; use consolidated workflow tools.",
        DeprecationWarning,
        stacklevel=3,
    )

    @wraps(func)
    async def wrapper(*args: Any, **kwargs: Any) -> Any:
        operation = LEGACY_OPERATION_MAP.get(func.__name__)
        if operation is None:
            return CovenantViolation.build(
                "UNKNOWN_COVENANT_OPERATION", func.__name__, None
            )
        violation = authorize_operation_call(
            operation, kwargs, kwargs.get("preflight_token")
        )
        if violation is not None:
            return violation
        return await func(*args, **kwargs)

    return wrapper


def requires_communion(func: Callable) -> Callable:
    return _deprecated_decorator("requires_communion", func)


def requires_counsel(func: Callable) -> Callable:
    return _deprecated_decorator("requires_counsel", func)
