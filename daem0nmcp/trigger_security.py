"""Deterministically bounded matching for persisted context-trigger patterns.

User regular expressions are compiled and searched only through the third-party
``regex`` distribution, whose search API supports an execution timeout.  File
globs use an owned dynamic-programming matcher so they never pass through the
standard-library ``re``/``fnmatch`` implementation.
"""

from __future__ import annotations

import asyncio
import hashlib
import importlib
import logging
import os
import threading
import time
from collections import OrderedDict
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from functools import partial
from typing import Any

MAX_PATTERN_CODEPOINTS = 256
MAX_CANDIDATE_CODEPOINTS = 256
MAX_CANDIDATES = 32
MAX_ACTIVE_TRIGGERS = 100
REGEX_TIMEOUT_SECONDS = 0.025
MAX_GLOB_SEGMENTS = 64
MAX_FILE_PATH_CODEPOINTS = 256
MAX_FILE_PATH_SEGMENTS = 64
DEFAULT_CACHE_ENTRIES = 128
DEFAULT_WARNING_KEYS = 256
DEFAULT_WARNING_INTERVAL_SECONDS = 60.0
DEFAULT_PATTERN_WORKERS = 4

logger = logging.getLogger(__name__)


class TriggerPatternError(ValueError):
    """A stable, serialization-ready trigger creation/evaluation failure."""

    def __init__(
        self, code: str, message: str, *, details: dict[str, object] | None = None
    ) -> None:
        self.code = code
        self.message = message
        self.details = details or {}
        super().__init__(message)

    def as_dict(self) -> dict[str, object]:
        result: dict[str, object] = {"code": self.code, "message": self.message}
        if self.details:
            result["details"] = dict(self.details)
        return result


@dataclass(frozen=True)
class PatternMatchResult:
    """Outcome of one stored regex trigger evaluation."""

    matched: bool
    timed_out: bool = False
    candidates_evaluated: int = 0


@dataclass(frozen=True)
class GlobMatchResult:
    """Outcome and deterministic work counters for a stored file glob."""

    matched: bool
    states_evaluated: int
    segment_evaluations: int


class BoundedPatternExecutor:
    """Run regex searches off-loop without admitting an unbounded work queue."""

    def __init__(self, *, max_workers: int = DEFAULT_PATTERN_WORKERS) -> None:
        if max_workers <= 0:
            raise ValueError("max_workers must be positive")
        self._pool = ThreadPoolExecutor(
            max_workers=max_workers,
            thread_name_prefix="daem0nmcp-pattern",
        )
        self._slots = threading.BoundedSemaphore(max_workers)
        self._state_lock = threading.Lock()
        self._in_flight = 0

    @property
    def in_flight(self) -> int:
        """Return the number of admitted running or queued searches."""
        with self._state_lock:
            return self._in_flight

    async def run(self, operation: Callable[[], Any]) -> Any:
        """Admit one operation or reject immediately when workers are full."""
        if not self._slots.acquire(blocking=False):
            raise TriggerPatternError(
                "TRIGGER_MATCHER_BUSY",
                "Trigger pattern workers are busy; retry the request.",
            )
        with self._state_lock:
            self._in_flight += 1

        try:
            work = self._pool.submit(operation)
        except BaseException:
            self._release_slot()
            raise

        # The concurrent future owns the slot until the actual worker stops.
        # Cancelling the awaiting coroutine therefore cannot over-admit work.
        work.add_done_callback(lambda _future: self._release_slot())
        return await asyncio.wrap_future(work)

    def shutdown(self) -> None:
        """Release worker resources for explicitly owned test/application pools."""
        self._pool.shutdown(wait=True, cancel_futures=True)

    def _release_slot(self) -> None:
        with self._state_lock:
            self._in_flight -= 1
        self._slots.release()


_DEFAULT_PATTERN_EXECUTOR = BoundedPatternExecutor()


def _default_compile(source: str) -> Any:
    try:
        regex_module = importlib.import_module("regex")
    except ModuleNotFoundError as error:
        raise TriggerPatternError(
            "TRIGGER_MATCHER_UNAVAILABLE",
            "The required regex trigger matcher is unavailable.",
        ) from error
    return regex_module.compile(source)


def _default_search(compiled: Any, value: str, *, timeout: float) -> bool:
    return compiled.search(value, timeout=timeout) is not None


class SafeUserPattern:
    """Compile, cache, and timeout stored user regex patterns behind one seam."""

    def __init__(
        self,
        *,
        compiler: Callable[[str], Any] | None = None,
        search: Callable[..., bool] | None = None,
        clock: Callable[[], float] | None = None,
        logger: Any | None = None,
        timeout_exceptions: tuple[type[BaseException], ...] = (TimeoutError,),
        timeout_seconds: float = REGEX_TIMEOUT_SECONDS,
        max_cache_entries: int = DEFAULT_CACHE_ENTRIES,
        warning_interval_seconds: float = DEFAULT_WARNING_INTERVAL_SECONDS,
        max_warning_keys: int = DEFAULT_WARNING_KEYS,
        executor: BoundedPatternExecutor | None = None,
    ) -> None:
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        if max_cache_entries <= 0:
            raise ValueError("max_cache_entries must be positive")
        if warning_interval_seconds < 0:
            raise ValueError("warning_interval_seconds cannot be negative")
        if max_warning_keys <= 0:
            raise ValueError("max_warning_keys must be positive")
        if not timeout_exceptions:
            raise ValueError("timeout_exceptions cannot be empty")

        self._compiler = compiler or _default_compile
        self._search = search or _default_search
        self._clock = clock or time.monotonic
        self._logger = logger or globals()["logger"]
        self._timeout_exceptions = timeout_exceptions
        self._timeout_seconds = timeout_seconds
        self._max_cache_entries = max_cache_entries
        self._warning_interval_seconds = warning_interval_seconds
        self._max_warning_keys = max_warning_keys
        self._executor = executor or _DEFAULT_PATTERN_EXECUTOR
        self._state_lock = threading.RLock()
        self._cache: OrderedDict[tuple[object, str], Any] = OrderedDict()
        self._warning_times: OrderedDict[tuple[object, str], float] = OrderedDict()

    @property
    def cache_size(self) -> int:
        """Current number of compiled entries, exposed for health diagnostics."""
        with self._state_lock:
            return len(self._cache)

    @property
    def warning_key_count(self) -> int:
        """Current number of timeout rate-limit keys."""
        with self._state_lock:
            return len(self._warning_times)

    def validate(self, source: str) -> None:
        """Validate and compile a regex source before it is persisted."""
        self._validate_source_shape(source)
        self._compile(source)

    def matches(
        self,
        trigger_id: object,
        source: str,
        values: Sequence[str],
        *,
        field: str = "values",
    ) -> PatternMatchResult:
        """Search bounded candidates, aborting the trigger on the first timeout."""
        self.validate_candidates(values, field=field)
        compiled = self._compiled(trigger_id, source)

        for index, value in enumerate(values, start=1):
            try:
                if bool(self._search(compiled, value, timeout=self._timeout_seconds)):
                    return PatternMatchResult(matched=True, candidates_evaluated=index)
            except self._timeout_exceptions:
                self._warn_timeout(trigger_id, source)
                return PatternMatchResult(
                    matched=False, timed_out=True, candidates_evaluated=index
                )
            except Exception as error:
                raise TriggerPatternError(
                    "TRIGGER_PATTERN_EVALUATION_FAILED",
                    "Trigger pattern evaluation failed safely.",
                ) from error

        return PatternMatchResult(matched=False, candidates_evaluated=len(values))

    async def matches_async(
        self,
        trigger_id: object,
        source: str,
        values: Sequence[str],
        *,
        field: str = "values",
    ) -> PatternMatchResult:
        """Run one bounded trigger search outside the caller's event loop."""
        self._validate_source_shape(source)
        self.validate_candidates(values, field=field)
        candidates = tuple(values)
        return await self._executor.run(
            partial(
                self.matches,
                trigger_id,
                source,
                candidates,
                field=field,
            )
        )

    @staticmethod
    def _validate_source_shape(source: str) -> None:
        if not isinstance(source, str):
            raise TriggerPatternError(
                "TRIGGER_PATTERN_INVALID_TYPE",
                "Trigger pattern must be a string.",
            )
        _validate_scalar_text(
            source,
            code="TRIGGER_PATTERN_INVALID_UNICODE",
            message="Trigger pattern must contain valid Unicode scalar text.",
        )
        if not source.strip():
            raise TriggerPatternError(
                "TRIGGER_PATTERN_EMPTY",
                "Trigger pattern must not be empty.",
            )
        if len(source) > MAX_PATTERN_CODEPOINTS:
            raise TriggerPatternError(
                "TRIGGER_PATTERN_TOO_LONG",
                "Trigger pattern exceeds 256 Unicode code points.",
                details={"limit": MAX_PATTERN_CODEPOINTS, "actual": len(source)},
            )

    @staticmethod
    def validate_candidates(values: Sequence[str], *, field: str = "values") -> None:
        """Validate an entire request collection before any pattern executes."""
        if isinstance(values, (str, bytes)) or not isinstance(values, Sequence):
            raise TriggerPatternError(
                "TRIGGER_CANDIDATES_INVALID",
                "Trigger candidate values must be a sequence of strings.",
                details={"field": field},
            )
        if len(values) > MAX_CANDIDATES:
            raise TriggerPatternError(
                "TRIGGER_CANDIDATE_LIMIT",
                "Trigger evaluation accepts at most 32 candidate values.",
                details={
                    "field": field,
                    "limit": MAX_CANDIDATES,
                    "actual": len(values),
                },
            )
        for index, value in enumerate(values):
            if not isinstance(value, str):
                raise TriggerPatternError(
                    "TRIGGER_VALUE_INVALID",
                    "Each trigger candidate value must be a string.",
                    details={"field": field, "index": index},
                )
            _validate_scalar_text(
                value,
                code="TRIGGER_VALUE_INVALID_UNICODE",
                message=(
                    "Trigger candidate value must contain valid Unicode scalar text."
                ),
                details={"field": field, "index": index},
            )
            if len(value) > MAX_CANDIDATE_CODEPOINTS:
                raise TriggerPatternError(
                    "TRIGGER_VALUE_TOO_LONG",
                    "Trigger candidate value exceeds 256 Unicode code points.",
                    details={
                        "field": field,
                        "index": index,
                        "limit": MAX_CANDIDATE_CODEPOINTS,
                        "actual": len(value),
                    },
                )

    @staticmethod
    def validate_candidate_total(count: int) -> None:
        """Enforce the aggregate regex-candidate cap for one invocation."""
        if count > MAX_CANDIDATES:
            raise TriggerPatternError(
                "TRIGGER_CANDIDATE_LIMIT",
                "Trigger evaluation accepts at most 32 candidate values.",
                details={
                    "field": "tags_and_entities",
                    "limit": MAX_CANDIDATES,
                    "actual": count,
                },
            )

    def _compile(self, source: str) -> Any:
        with self._state_lock:
            try:
                return self._compiler(source)
            except TriggerPatternError:
                raise
            except Exception as error:
                raise TriggerPatternError(
                    "TRIGGER_PATTERN_INVALID",
                    "Trigger pattern is not a valid regular expression.",
                ) from error

    def _compiled(self, trigger_id: object, source: str) -> Any:
        self._validate_source_shape(source)
        with self._state_lock:
            key = (trigger_id, source)
            try:
                compiled = self._cache.pop(key)
            except KeyError:
                stale_keys = [
                    cached for cached in self._cache if cached[0] == trigger_id
                ]
                for stale_key in stale_keys:
                    del self._cache[stale_key]
                compiled = self._compile(source)
            self._cache[key] = compiled
            while len(self._cache) > self._max_cache_entries:
                self._cache.popitem(last=False)
            return compiled

    def _warn_timeout(self, trigger_id: object, source: str) -> None:
        key = (trigger_id, source)
        now = self._clock()
        with self._state_lock:
            last_warning = self._warning_times.get(key)
            should_warn = (
                last_warning is None
                or now - last_warning >= self._warning_interval_seconds
            )
            if key in self._warning_times:
                self._warning_times.move_to_end(key)
            if should_warn:
                self._warning_times[key] = now
            while len(self._warning_times) > self._max_warning_keys:
                self._warning_times.popitem(last=False)
        if should_warn:
            source_digest = hashlib.sha256(source.encode("utf-8")).hexdigest()[:12]
            self._logger.warning(
                "Stored trigger regex timed out and was treated as a non-match.",
                extra={
                    "trigger_id": str(trigger_id),
                    "pattern_sha256": source_digest,
                },
            )


def validate_active_trigger_count(count: int) -> None:
    """Reject an active-trigger set that cannot be evaluated within policy."""
    if count > MAX_ACTIVE_TRIGGERS:
        raise TriggerPatternError(
            "TRIGGER_ACTIVE_LIMIT",
            "Trigger evaluation accepts at most 100 active triggers.",
            details={"limit": MAX_ACTIVE_TRIGGERS, "actual": count},
        )


def _validate_scalar_text(
    value: str,
    *,
    code: str,
    message: str,
    details: dict[str, object] | None = None,
) -> None:
    try:
        value.encode("utf-8", errors="strict")
    except UnicodeEncodeError as error:
        raise TriggerPatternError(code, message, details=details) from error


def validate_glob_pattern(pattern: str) -> tuple[str, tuple[str, ...]]:
    """Normalize and validate one persisted file glob."""
    return _normalize_glob_value(
        pattern,
        value_name="pattern",
        too_long_code="TRIGGER_GLOB_TOO_LONG",
        too_long_message="File trigger glob exceeds 256 Unicode code points.",
        segment_code="TRIGGER_GLOB_SEGMENT_LIMIT",
        segment_message="File trigger glob exceeds 64 path segments.",
    )


def validate_file_path(file_path: str) -> tuple[str, tuple[str, ...]]:
    """Normalize and validate one candidate file path."""
    return _normalize_glob_value(
        file_path,
        value_name="file_path",
        too_long_code="TRIGGER_FILE_PATH_TOO_LONG",
        too_long_message="Trigger file path exceeds 256 Unicode code points.",
        segment_code="TRIGGER_FILE_PATH_SEGMENT_LIMIT",
        segment_message="Trigger file path exceeds 64 path segments.",
    )


def _normalize_glob_value(
    value: str,
    *,
    value_name: str,
    too_long_code: str,
    too_long_message: str,
    segment_code: str,
    segment_message: str,
) -> tuple[str, tuple[str, ...]]:
    if not isinstance(value, str):
        raise TriggerPatternError(
            "TRIGGER_GLOB_VALUE_INVALID",
            "File trigger glob inputs must be strings.",
            details={"field": value_name},
        )
    if value_name == "pattern":
        _validate_scalar_text(
            value,
            code="TRIGGER_PATTERN_INVALID_UNICODE",
            message="Trigger pattern must contain valid Unicode scalar text.",
        )
    else:
        _validate_scalar_text(
            value,
            code="TRIGGER_FILE_PATH_INVALID_UNICODE",
            message="Trigger file path must contain valid Unicode scalar text.",
        )
    if value_name == "pattern" and not value.strip():
        raise TriggerPatternError(
            "TRIGGER_PATTERN_EMPTY",
            "Trigger pattern must not be empty.",
        )
    if len(value) > MAX_PATTERN_CODEPOINTS:
        raise TriggerPatternError(
            too_long_code,
            too_long_message,
            details={"limit": MAX_PATTERN_CODEPOINTS, "actual": len(value)},
        )
    normalized = os.path.normcase(value.replace("\\", "/")).replace("\\", "/")
    segments = tuple(normalized.split("/"))
    segment_limit = (
        MAX_GLOB_SEGMENTS if value_name == "pattern" else MAX_FILE_PATH_SEGMENTS
    )
    if len(segments) > segment_limit:
        raise TriggerPatternError(
            segment_code,
            segment_message,
            details={"limit": segment_limit, "actual": len(segments)},
        )
    return normalized, segments


def bounded_glob_match(pattern: str, file_path: str) -> GlobMatchResult:
    """Match a bounded file glob using visited DP states and no regex engine."""
    normalized_pattern, pattern_parts = validate_glob_pattern(pattern)
    normalized_path, path_parts = validate_file_path(file_path)

    # Preserve the prior fnmatch-on-the-whole-path behavior unless the source
    # contains ``**``. In that legacy mode ``*`` may consume path separators.
    if "**" not in normalized_pattern:
        return GlobMatchResult(
            matched=_segment_matches(normalized_pattern, normalized_path),
            states_evaluated=1,
            segment_evaluations=1,
        )

    states: list[tuple[int, int]] = [(0, 0)]
    visited: set[tuple[int, int]] = set()
    segment_cache: dict[tuple[str, str], bool] = {}

    while states:
        pattern_index, path_index = states.pop()
        state = (pattern_index, path_index)
        if state in visited:
            continue
        visited.add(state)

        if pattern_index == len(pattern_parts):
            if path_index == len(path_parts):
                return GlobMatchResult(
                    matched=True,
                    states_evaluated=len(visited),
                    segment_evaluations=len(segment_cache),
                )
            continue

        pattern_part = pattern_parts[pattern_index]
        if pattern_part == "**":
            if path_index < len(path_parts):
                states.append((pattern_index, path_index + 1))
            states.append((pattern_index + 1, path_index))
            continue

        if path_index >= len(path_parts):
            continue

        cache_key = (pattern_part, path_parts[path_index])
        if cache_key not in segment_cache:
            segment_cache[cache_key] = _segment_matches(*cache_key)
        if segment_cache[cache_key]:
            states.append((pattern_index + 1, path_index + 1))

    return GlobMatchResult(
        matched=False,
        states_evaluated=len(visited),
        segment_evaluations=len(segment_cache),
    )


def _segment_matches(pattern: str, value: str) -> bool:
    """Bounded glob matching inside one path segment."""
    states: list[tuple[int, int]] = [(0, 0)]
    visited: set[tuple[int, int]] = set()

    while states:
        pattern_index, value_index = states.pop()
        state = (pattern_index, value_index)
        if state in visited:
            continue
        visited.add(state)

        if pattern_index == len(pattern):
            if value_index == len(value):
                return True
            continue

        token = pattern[pattern_index]
        if token == "*":
            if value_index < len(value):
                states.append((pattern_index, value_index + 1))
            states.append((pattern_index + 1, value_index))
            continue

        if value_index >= len(value):
            continue

        if token == "?":
            states.append((pattern_index + 1, value_index + 1))
            continue

        if token == "[":
            class_end = _find_class_end(pattern, pattern_index)
            if class_end is not None:
                class_source = pattern[pattern_index + 1 : class_end]
                if _class_matches(class_source, value[value_index]):
                    states.append((class_end + 1, value_index + 1))
                continue

        if token == value[value_index]:
            states.append((pattern_index + 1, value_index + 1))

    return False


def _find_class_end(pattern: str, start: int) -> int | None:
    index = start + 1
    if index < len(pattern) and pattern[index] == "!":
        index += 1
    if index < len(pattern) and pattern[index] == "]":
        index += 1
    while index < len(pattern):
        if pattern[index] == "]":
            return index
        index += 1
    return None


def _class_matches(source: str, value: str) -> bool:
    negated = source.startswith("!")
    if negated:
        source = source[1:]

    matched = False
    index = 0
    while index < len(source):
        if index + 2 < len(source) and source[index + 1] == "-":
            lower = source[index]
            upper = source[index + 2]
            if lower <= upper and lower <= value <= upper:
                matched = True
            index += 3
            continue
        if source[index] == value:
            matched = True
        index += 1

    return not matched if negated else matched
