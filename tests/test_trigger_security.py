"""Dependency-free security tests for stored context-trigger patterns."""

from __future__ import annotations

import ast
import asyncio
import importlib.util
import json
import os
import sys
import threading
import time
import types
import unittest
from contextlib import contextmanager
from pathlib import Path


class _CompiledPattern:
    def __init__(self, source: str) -> None:
        self.source = source


class _RecordingCompiler:
    def __init__(self) -> None:
        self.sources: list[str] = []

    def __call__(self, source: str) -> _CompiledPattern:
        self.sources.append(source)
        if source == "(invalid":
            raise ValueError("injected syntax failure")
        return _CompiledPattern(source)


def _faithful_search(compiled: _CompiledPattern, value: str, *, timeout: float) -> bool:
    """Small deterministic stand-in for the documented legacy patterns."""
    if timeout != 0.025:
        raise AssertionError(f"unexpected timeout: {timeout}")
    if compiled.source == "auth.*":
        return value.startswith("auth")
    if compiled.source == "UserService|AuthService":
        return value in {"UserService", "AuthService"}
    if compiled.source == ".*Repository$":
        return value.endswith("Repository")
    return compiled.source in value


class _RecordingLogger:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, object]]] = []

    def warning(self, message: str, *args: object, **kwargs: object) -> None:
        self.events.append((message, kwargs))


class _NoDatabase:
    """Fails loudly if validation reaches persistence."""

    def get_session(self):
        raise AssertionError("trigger validation unexpectedly touched the database")


@contextmanager
def _loaded_trigger_manager_module():
    """Load the real manager while replacing only unavailable DB dependencies."""
    root = Path(__file__).resolve().parents[1]
    module_name = "daem0nmcp._context_triggers_security_test"
    sqlalchemy_module = types.ModuleType("sqlalchemy")
    sqlalchemy_module.delete = lambda *args, **kwargs: None
    sqlalchemy_module.select = lambda *args, **kwargs: None
    database_module = types.ModuleType("daem0nmcp.database")
    database_module.DatabaseManager = object
    models_module = types.ModuleType("daem0nmcp.models")
    models_module.ContextTrigger = object
    stubs = {
        "sqlalchemy": sqlalchemy_module,
        "daem0nmcp.database": database_module,
        "daem0nmcp.models": models_module,
    }
    previous = {name: sys.modules.get(name) for name in stubs}
    sys.modules.update(stubs)
    spec = importlib.util.spec_from_file_location(
        module_name, root / "daem0nmcp" / "context_triggers.py"
    )
    if spec is None or spec.loader is None:
        raise AssertionError("could not load context trigger manager")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
        yield module
    finally:
        sys.modules.pop(module_name, None)
        for name, value in previous.items():
            if value is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = value


class TestSafeUserPattern(unittest.TestCase):
    def test_documented_patterns_keep_their_matching_semantics(self) -> None:
        """Replacing regex execution must not reject or reinterpret valid triggers."""
        from daem0nmcp.trigger_security import SafeUserPattern

        compiler = _RecordingCompiler()
        matcher = SafeUserPattern(compiler=compiler, search=_faithful_search)

        cases = (
            ("auth.*", ["database", "authentication"], True),
            ("UserService|AuthService", ["BillingService", "AuthService"], True),
            (".*Repository$", ["UserService", "OrderRepository"], True),
            (".*Repository$", ["RepositoryFactory"], False),
        )
        for trigger_id, (source, values, expected) in enumerate(cases, start=1):
            with self.subTest(source=source, values=values):
                result = matcher.matches(trigger_id, source, values)
                self.assertEqual(result.matched, expected)
                self.assertFalse(result.timed_out)

    def test_invalid_regex_has_a_stable_creation_error(self) -> None:
        """A compiler syntax failure must not leak dependency-specific exception text."""
        from daem0nmcp.trigger_security import SafeUserPattern, TriggerPatternError

        matcher = SafeUserPattern(
            compiler=_RecordingCompiler(), search=_faithful_search
        )

        with self.assertRaises(TriggerPatternError) as raised:
            matcher.validate("(invalid")

        self.assertEqual(
            raised.exception.as_dict(),
            {
                "code": "TRIGGER_PATTERN_INVALID",
                "message": "Trigger pattern is not a valid regular expression.",
            },
        )

    def test_pattern_over_256_code_points_is_rejected_before_compilation(self) -> None:
        """The 257th Unicode code point must never reach the regex compiler."""
        from daem0nmcp.trigger_security import SafeUserPattern, TriggerPatternError

        compiler = _RecordingCompiler()
        matcher = SafeUserPattern(compiler=compiler, search=_faithful_search)

        with self.assertRaises(TriggerPatternError) as raised:
            matcher.validate("💾" * 257)

        self.assertEqual(raised.exception.code, "TRIGGER_PATTERN_TOO_LONG")
        self.assertEqual(compiler.sources, [])

    def test_candidate_collection_over_32_is_rejected_without_truncation(self) -> None:
        """A 33-value request must fail as a whole instead of evaluating a prefix."""
        from daem0nmcp.trigger_security import SafeUserPattern, TriggerPatternError

        compiler = _RecordingCompiler()
        calls: list[str] = []

        def search(compiled: _CompiledPattern, value: str, *, timeout: float) -> bool:
            calls.append(value)
            return False

        matcher = SafeUserPattern(compiler=compiler, search=search)

        with self.assertRaises(TriggerPatternError) as raised:
            matcher.matches(1, "auth.*", ["tag"] * 33, field="tags")

        self.assertEqual(raised.exception.code, "TRIGGER_CANDIDATE_LIMIT")
        self.assertEqual(calls, [])
        self.assertEqual(compiler.sources, [])

    def test_candidate_over_256_code_points_is_rejected_without_matching(self) -> None:
        """An oversized value must fail before any persisted pattern executes."""
        from daem0nmcp.trigger_security import SafeUserPattern, TriggerPatternError

        compiler = _RecordingCompiler()
        calls: list[str] = []

        def search(compiled: _CompiledPattern, value: str, *, timeout: float) -> bool:
            calls.append(value)
            return False

        matcher = SafeUserPattern(compiler=compiler, search=search)

        with self.assertRaises(TriggerPatternError) as raised:
            matcher.matches(1, "auth.*", ["é" * 257], field="entities")

        self.assertEqual(raised.exception.code, "TRIGGER_VALUE_TOO_LONG")
        self.assertEqual(raised.exception.details["field"], "entities")
        self.assertEqual(calls, [])

    def test_lone_surrogates_are_rejected_before_compile_or_search(self) -> None:
        """Non-scalar Unicode must not reach regex or warning fingerprint code."""
        from daem0nmcp.trigger_security import SafeUserPattern, TriggerPatternError

        compiler = _RecordingCompiler()
        calls: list[str] = []

        def search(compiled: _CompiledPattern, value: str, *, timeout: float) -> bool:
            calls.append(value)
            return False

        matcher = SafeUserPattern(compiler=compiler, search=search)

        with self.assertRaises(TriggerPatternError) as pattern_error:
            matcher.validate("auth\ud800")
        with self.assertRaises(TriggerPatternError) as value_error:
            matcher.matches(1, "auth.*", ["tag\udfff"])

        self.assertEqual(
            pattern_error.exception.code, "TRIGGER_PATTERN_INVALID_UNICODE"
        )
        self.assertEqual(value_error.exception.code, "TRIGGER_VALUE_INVALID_UNICODE")
        self.assertEqual(compiler.sources, [])
        self.assertEqual(calls, [])

    def test_timeout_aborts_trigger_and_is_a_non_match(self) -> None:
        """A timed-out candidate must not let a later candidate trigger statistics."""
        from daem0nmcp.trigger_security import SafeUserPattern

        calls: list[str] = []

        def search(compiled: _CompiledPattern, value: str, *, timeout: float) -> bool:
            calls.append(value)
            if value == "slow":
                raise TimeoutError("simulated regex timeout")
            return value == "auth-match"

        matcher = SafeUserPattern(
            compiler=_RecordingCompiler(),
            search=search,
            logger=_RecordingLogger(),
        )

        result = matcher.matches(42, "auth.*", ["slow", "auth-match"])

        self.assertFalse(result.matched)
        self.assertTrue(result.timed_out)
        self.assertEqual(calls, ["slow"])

    def test_timeout_warning_is_rate_limited_by_trigger_and_source(self) -> None:
        """Repeated timeouts must not create an unbounded warning/log flood."""
        from daem0nmcp.trigger_security import SafeUserPattern

        now = [100.0]
        log = _RecordingLogger()

        def timeout_search(
            compiled: _CompiledPattern, value: str, *, timeout: float
        ) -> bool:
            raise TimeoutError("simulated regex timeout")

        matcher = SafeUserPattern(
            compiler=_RecordingCompiler(),
            search=timeout_search,
            clock=lambda: now[0],
            logger=log,
            warning_interval_seconds=60.0,
            max_warning_keys=2,
        )

        matcher.matches(1, "one", ["value"])
        now[0] += 10.0
        matcher.matches(1, "one", ["value"])
        now[0] += 51.0
        matcher.matches(1, "one", ["value"])
        matcher.matches(2, "two", ["value"])
        matcher.matches(3, "three", ["value"])

        self.assertEqual(len(log.events), 4)
        self.assertLessEqual(matcher.warning_key_count, 2)

    def test_cache_is_source_sensitive_invalidated_and_bounded(self) -> None:
        """A changed stored source must compile anew and cache growth must be finite."""
        from daem0nmcp.trigger_security import SafeUserPattern

        compiler = _RecordingCompiler()
        matcher = SafeUserPattern(
            compiler=compiler,
            search=_faithful_search,
            max_cache_entries=2,
        )

        matcher.matches(1, "auth.*", ["auth"])
        matcher.matches(1, "auth.*", ["auth"])
        matcher.matches(1, ".*Repository$", ["UserRepository"])
        matcher.matches(2, "UserService|AuthService", ["UserService"])
        matcher.matches(3, "auth.*", ["auth"])

        self.assertEqual(
            compiler.sources,
            [
                "auth.*",
                ".*Repository$",
                "UserService|AuthService",
                "auth.*",
            ],
        )
        self.assertLessEqual(matcher.cache_size, 2)

    def test_100_active_triggers_is_allowed_but_101_is_rejected(self) -> None:
        """Evaluation must reject trigger overflow rather than truncate priority order."""
        from daem0nmcp.trigger_security import (
            TriggerPatternError,
            validate_active_trigger_count,
        )

        validate_active_trigger_count(100)
        with self.assertRaises(TriggerPatternError) as raised:
            validate_active_trigger_count(101)

        self.assertEqual(raised.exception.code, "TRIGGER_ACTIVE_LIMIT")


class TestBoundedFileGlob(unittest.TestCase):
    def test_legacy_glob_features_match_without_stdlib_regex(self) -> None:
        """Owned glob matching must retain *, ?, class, range, and negation behavior."""
        from daem0nmcp.trigger_security import bounded_glob_match

        cases = (
            ("src/auth/*.py", "src/auth/service.py", True),
            # Legacy fnmatch matched wildcards against the whole normalized
            # path whenever the pattern did not contain ``**``.
            ("src/auth/*.py", "src/auth/nested/service.py", True),
            ("*.py", "dir/nested/module.py", True),
            ("src/**/test_?.py", "src/auth/tests/test_a.py", True),
            ("**/*.py", "test.py", True),
            ("data/file[0-9].txt", "data/file7.txt", True),
            ("data/file[0-9].txt", "data/filex.txt", False),
            ("data/[!x]*.txt", "data/alpha.txt", True),
            ("data/[!x]*.txt", "data/xray.txt", False),
            (r"src\**\test_[ab].py", "src/core/test_b.py", True),
        )

        for pattern, path, expected in cases:
            with self.subTest(pattern=pattern, path=path):
                self.assertEqual(bounded_glob_match(pattern, path).matched, expected)

    def test_glob_case_behavior_tracks_the_host_platform(self) -> None:
        """The owned matcher must preserve fnmatch's platform case policy."""
        from daem0nmcp.trigger_security import bounded_glob_match

        expected = os.path.normcase("SRC/*.PY") == os.path.normcase("src/*.py")

        self.assertEqual(
            bounded_glob_match("SRC/*.PY", "src/module.py").matched,
            expected,
        )

    def test_glob_pattern_and_candidate_have_exact_size_bounds(self) -> None:
        """Oversized glob source or file context must be rejected, never truncated."""
        from daem0nmcp.trigger_security import TriggerPatternError, bounded_glob_match

        bounded_glob_match("a" * 256, "a" * 256)
        cases = (
            ("a" * 257, "a", "TRIGGER_GLOB_TOO_LONG"),
            ("a", "a" * 257, "TRIGGER_FILE_PATH_TOO_LONG"),
            ("/".join(["a"] * 65), "a", "TRIGGER_GLOB_SEGMENT_LIMIT"),
            ("a", "/".join(["a"] * 65), "TRIGGER_FILE_PATH_SEGMENT_LIMIT"),
        )
        for pattern, path, code in cases:
            with self.subTest(code=code):
                with self.assertRaises(TriggerPatternError) as raised:
                    bounded_glob_match(pattern, path)
                self.assertEqual(raised.exception.code, code)

    def test_glob_source_and_file_path_reject_lone_surrogates(self) -> None:
        """Glob DP must operate only on encodable Unicode scalar text."""
        from daem0nmcp.trigger_security import TriggerPatternError, bounded_glob_match

        cases = (
            ("src/\ud800/*.py", "src/main.py", "TRIGGER_PATTERN_INVALID_UNICODE"),
            ("src/*.py", "src/\udfff.py", "TRIGGER_FILE_PATH_INVALID_UNICODE"),
        )
        for pattern, path, code in cases:
            with self.subTest(code=code):
                with self.assertRaises(TriggerPatternError) as raised:
                    bounded_glob_match(pattern, path)
                self.assertEqual(raised.exception.code, code)

    def test_double_star_search_visits_each_path_state_at_most_once(self) -> None:
        """Repeated ** branches must stay within the finite path-state grid."""
        from daem0nmcp.trigger_security import bounded_glob_match

        pattern = "**/**/**/**/**/target[0-9].py"
        path = "/".join(["directory"] * 24 + ["targetx.py"])

        result = bounded_glob_match(pattern, path)

        self.assertFalse(result.matched)
        self.assertLessEqual(result.states_evaluated, 7 * 26)
        self.assertLessEqual(result.segment_evaluations, 26)


class TestContextTriggerManagerWiring(unittest.IsolatedAsyncioTestCase):
    async def test_regex_search_yields_the_event_loop(self) -> None:
        """A bounded regex timeout must not synchronously stall async callers."""
        from daem0nmcp.trigger_security import SafeUserPattern

        def slow_timeout_search(
            compiled: _CompiledPattern, value: str, *, timeout: float
        ) -> bool:
            time.sleep(0.15)
            raise TimeoutError("simulated worker timeout")

        trigger = {
            "id": 81,
            "trigger_type": "tag_match",
            "pattern": "auth.*",
            "recall_topic": "topic",
            "recall_categories": [],
            "priority": 0,
        }
        with _loaded_trigger_manager_module() as module:
            manager = module.ContextTriggerManager(
                _NoDatabase(),
                pattern_matcher=SafeUserPattern(
                    compiler=_RecordingCompiler(),
                    search=slow_timeout_search,
                    logger=_RecordingLogger(),
                ),
            )

            async def list_triggers(*args, **kwargs):
                return [trigger]

            manager._list_active_triggers_for_evaluation = list_triggers
            started = time.perf_counter()
            evaluation = asyncio.create_task(
                manager.check_triggers("workspace", tags=["slow"])
            )
            await asyncio.sleep(0.02)
            heartbeat_elapsed = time.perf_counter() - started
            result = await evaluation

        self.assertLess(heartbeat_elapsed, 0.10)
        self.assertEqual(result, [])

    async def test_regex_worker_admission_is_bounded_and_fails_closed(self) -> None:
        """Worker saturation must reject before creating an unbounded queue."""
        from daem0nmcp.trigger_security import (
            BoundedPatternExecutor,
            SafeUserPattern,
            TriggerPatternError,
        )

        release = threading.Event()
        entered = threading.Event()

        def blocking_search(
            compiled: _CompiledPattern, value: str, *, timeout: float
        ) -> bool:
            entered.set()
            release.wait(timeout=1.0)
            return False

        executor = BoundedPatternExecutor(max_workers=1)
        matcher = SafeUserPattern(
            compiler=_RecordingCompiler(),
            search=blocking_search,
            executor=executor,
        )
        first = asyncio.create_task(matcher.matches_async(1, "one", ["value"]))
        try:
            for _ in range(100):
                if entered.is_set():
                    break
                await asyncio.sleep(0.001)
            self.assertTrue(entered.is_set())

            with self.assertRaises(TriggerPatternError) as raised:
                await matcher.matches_async(2, "two", ["value"])

            self.assertEqual(raised.exception.code, "TRIGGER_MATCHER_BUSY")
            self.assertEqual(executor.in_flight, 1)
        finally:
            release.set()
            await first
            executor.shutdown()

    async def test_creation_maps_regex_and_glob_failures_before_database_access(
        self,
    ) -> None:
        """Invalid persisted patterns must return stable envelopes without a DB write."""
        from daem0nmcp.trigger_security import SafeUserPattern

        compiler = _RecordingCompiler()
        pattern_matcher = SafeUserPattern(compiler=compiler, search=_faithful_search)
        cases = (
            ("tag_match", "   ", "TRIGGER_PATTERN_EMPTY"),
            ("tag_match", "auth\ud800", "TRIGGER_PATTERN_INVALID_UNICODE"),
            ("tag_match", "(invalid", "TRIGGER_PATTERN_INVALID"),
            ("entity_match", "x" * 257, "TRIGGER_PATTERN_TOO_LONG"),
            ("file_pattern", "x" * 257, "TRIGGER_GLOB_TOO_LONG"),
            (
                "file_pattern",
                "/".join(["s"] * 65),
                "TRIGGER_GLOB_SEGMENT_LIMIT",
            ),
            (
                "file_pattern",
                "src/\udfff/*.py",
                "TRIGGER_PATTERN_INVALID_UNICODE",
            ),
        )

        with _loaded_trigger_manager_module() as module:
            manager = module.ContextTriggerManager(
                _NoDatabase(), pattern_matcher=pattern_matcher
            )
            for trigger_type, pattern, code in cases:
                with self.subTest(code=code):
                    result = await manager.add_trigger(
                        project_path="workspace",
                        trigger_type=trigger_type,
                        pattern=pattern,
                        recall_topic="topic",
                    )
                    self.assertEqual(result["error"]["code"], code)

    async def test_candidate_overflow_rejects_before_query_match_or_stats(self) -> None:
        """Thirty-three candidates must stop evaluation before trigger retrieval."""
        from daem0nmcp.trigger_security import SafeUserPattern

        searches: list[str] = []

        def search(compiled: _CompiledPattern, value: str, *, timeout: float) -> bool:
            searches.append(value)
            return False

        with _loaded_trigger_manager_module() as module:
            manager = module.ContextTriggerManager(
                _NoDatabase(),
                pattern_matcher=SafeUserPattern(
                    compiler=_RecordingCompiler(), search=search
                ),
            )
            list_calls: list[bool] = []
            stats_calls: list[list[int]] = []

            async def list_triggers(*args, **kwargs):
                list_calls.append(True)
                return []

            async def update_stats(ids):
                stats_calls.append(ids)

            manager._list_active_triggers_for_evaluation = list_triggers
            manager._update_trigger_stats = update_stats

            result = await manager.check_triggers("workspace", tags=["tag"] * 33)

        self.assertEqual(result["error"]["code"], "TRIGGER_CANDIDATE_LIMIT")
        self.assertEqual(list_calls, [])
        self.assertEqual(searches, [])
        self.assertEqual(stats_calls, [])

    async def test_combined_tag_and_entity_candidates_share_the_32_value_cap(
        self,
    ) -> None:
        """The per-invocation cap applies across both regex candidate fields."""
        from daem0nmcp.trigger_security import SafeUserPattern

        with _loaded_trigger_manager_module() as module:
            manager = module.ContextTriggerManager(
                _NoDatabase(),
                pattern_matcher=SafeUserPattern(
                    compiler=_RecordingCompiler(), search=_faithful_search
                ),
            )
            query_calls: list[bool] = []

            async def list_triggers(*args, **kwargs):
                query_calls.append(True)
                return []

            manager._list_active_triggers_for_evaluation = list_triggers

            result = await manager.check_triggers(
                "workspace",
                tags=["tag"] * 20,
                entities=["entity"] * 13,
            )

        self.assertEqual(result["error"]["code"], "TRIGGER_CANDIDATE_LIMIT")
        self.assertEqual(query_calls, [])

    async def test_active_trigger_overflow_rejects_before_match_or_stats(self) -> None:
        """A 101-trigger set must be rejected intact instead of priority-truncated."""
        from daem0nmcp.trigger_security import SafeUserPattern

        searches: list[str] = []

        def search(compiled: _CompiledPattern, value: str, *, timeout: float) -> bool:
            searches.append(value)
            return False

        triggers = [
            {
                "id": index,
                "trigger_type": "tag_match",
                "pattern": "auth.*",
                "recall_topic": "topic",
                "recall_categories": [],
                "priority": 0,
            }
            for index in range(101)
        ]
        with _loaded_trigger_manager_module() as module:
            manager = module.ContextTriggerManager(
                _NoDatabase(),
                pattern_matcher=SafeUserPattern(
                    compiler=_RecordingCompiler(), search=search
                ),
            )
            stats_calls: list[list[int]] = []

            async def list_triggers(*args, **kwargs):
                return triggers

            async def update_stats(ids):
                stats_calls.append(ids)

            manager._list_active_triggers_for_evaluation = list_triggers
            manager._update_trigger_stats = update_stats

            result = await manager.check_triggers("workspace", tags=["auth"])

        self.assertEqual(result["error"]["code"], "TRIGGER_ACTIVE_LIMIT")
        self.assertEqual(searches, [])
        self.assertEqual(stats_calls, [])

    async def test_evaluation_requests_only_101_active_trigger_rows(self) -> None:
        """Overflow detection itself must not materialize an unbounded DB result."""
        from daem0nmcp.trigger_security import SafeUserPattern

        requested_limits: list[int] = []
        with _loaded_trigger_manager_module() as module:
            manager = module.ContextTriggerManager(
                _NoDatabase(),
                pattern_matcher=SafeUserPattern(
                    compiler=_RecordingCompiler(), search=_faithful_search
                ),
            )

            async def list_for_evaluation(project_path, *, limit):
                requested_limits.append(limit)
                return []

            manager._list_active_triggers_for_evaluation = list_for_evaluation

            result = await manager.check_triggers("workspace", tags=["auth"])

        self.assertEqual(result, [])
        self.assertEqual(requested_limits, [101])

    async def test_timeout_through_manager_never_updates_trigger_statistics(
        self,
    ) -> None:
        """A regex timeout is a clean non-match with no success-stat write."""
        from daem0nmcp.trigger_security import SafeUserPattern

        def timeout_search(
            compiled: _CompiledPattern, value: str, *, timeout: float
        ) -> bool:
            raise TimeoutError("simulated timeout")

        trigger = {
            "id": 73,
            "trigger_type": "tag_match",
            "pattern": "auth.*",
            "recall_topic": "topic",
            "recall_categories": [],
            "priority": 0,
        }
        with _loaded_trigger_manager_module() as module:
            manager = module.ContextTriggerManager(
                _NoDatabase(),
                pattern_matcher=SafeUserPattern(
                    compiler=_RecordingCompiler(),
                    search=timeout_search,
                    logger=_RecordingLogger(),
                ),
            )
            stats_calls: list[list[int]] = []

            async def list_triggers(*args, **kwargs):
                return [trigger]

            async def update_stats(ids):
                stats_calls.append(ids)

            manager._list_active_triggers_for_evaluation = list_triggers
            manager._update_trigger_stats = update_stats

            result = await manager.check_triggers("workspace", tags=["slow"])

        self.assertEqual(result, [])
        self.assertEqual(stats_calls, [])

    async def test_manager_cache_uses_real_trigger_id_and_source(self) -> None:
        """Manager wiring must reuse exact IDs and invalidate when stored source changes."""
        from daem0nmcp.trigger_security import SafeUserPattern

        compiler = _RecordingCompiler()
        trigger = {
            "id": 19,
            "trigger_type": "entity_match",
            "pattern": ".*Repository$",
            "recall_topic": "topic",
            "recall_categories": [],
            "priority": 0,
        }
        with _loaded_trigger_manager_module() as module:
            manager = module.ContextTriggerManager(
                _NoDatabase(),
                pattern_matcher=SafeUserPattern(
                    compiler=compiler, search=_faithful_search
                ),
            )

            async def list_triggers(*args, **kwargs):
                return [trigger]

            async def update_stats(ids):
                return None

            manager._list_active_triggers_for_evaluation = list_triggers
            manager._update_trigger_stats = update_stats

            await manager.check_triggers("workspace", entities=["NoMatch"])
            await manager.check_triggers("workspace", entities=["StillNoMatch"])
            trigger["pattern"] = "UserService|AuthService"
            await manager.check_triggers("workspace", entities=["BillingService"])

        self.assertEqual(compiler.sources, [".*Repository$", "UserService|AuthService"])

    async def test_get_triggered_context_propagates_error_without_memory_import(
        self,
    ) -> None:
        """Rejected evaluation must not begin recall or import MemoryManager."""
        from daem0nmcp.trigger_security import SafeUserPattern

        with _loaded_trigger_manager_module() as module:
            manager = module.ContextTriggerManager(
                _NoDatabase(),
                pattern_matcher=SafeUserPattern(
                    compiler=_RecordingCompiler(), search=_faithful_search
                ),
            )
            blocked_memory = types.ModuleType("daem0nmcp.memory")
            previous_memory = sys.modules.get("daem0nmcp.memory")
            sys.modules["daem0nmcp.memory"] = blocked_memory
            try:
                result = await manager.get_triggered_context(
                    "workspace", tags=["tag"] * 33
                )
            finally:
                if previous_memory is None:
                    sys.modules.pop("daem0nmcp.memory", None)
                else:
                    sys.modules["daem0nmcp.memory"] = previous_memory

        self.assertEqual(result["error"]["code"], "TRIGGER_CANDIDATE_LIMIT")
        self.assertEqual(result["triggers"], [])
        self.assertEqual(result["memories"], {})

    def test_manager_does_not_import_or_reference_stdlib_pattern_engines(self) -> None:
        """Persisted regex/glob execution must not regress to re or fnmatch."""
        root = Path(__file__).resolve().parents[1]
        imported: set[str] = set()
        referenced: set[str] = set()
        for filename in ("context_triggers.py", "trigger_security.py"):
            tree = ast.parse(
                (root / "daem0nmcp" / filename).read_text(encoding="utf-8")
            )
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    imported.update(alias.name.split(".")[0] for alias in node.names)
                elif isinstance(node, ast.ImportFrom) and node.module:
                    imported.add(node.module.split(".")[0])
                elif isinstance(node, ast.Name):
                    referenced.add(node.id)

        self.assertTrue({"re", "fnmatch"}.isdisjoint(imported))
        self.assertTrue({"re", "fnmatch"}.isdisjoint(referenced))


class TestTriggeredContextResource(unittest.IsolatedAsyncioTestCase):
    async def test_resource_preserves_structured_trigger_rejection(self) -> None:
        """An empty rejected result must not be rewritten as a clean non-match."""
        root = Path(__file__).resolve().parents[1]
        module_name = "daem0nmcp.tools._resources_trigger_security_test"

        class _Mcp:
            @staticmethod
            def resource(*args, **kwargs):
                return lambda function: function

        async def get_project_context(project_path):
            return types.SimpleNamespace(db_manager=object())

        context_manager = types.ModuleType("daem0nmcp.context_manager")
        context_manager._default_project_path = None
        context_manager.get_project_context = get_project_context
        database = types.ModuleType("daem0nmcp.database")
        database.DatabaseManager = object
        mcp_instance = types.ModuleType("daem0nmcp.mcp_instance")
        mcp_instance.mcp = _Mcp()
        models = types.ModuleType("daem0nmcp.models")
        models.Memory = object
        models.Rule = object
        sqlalchemy = types.ModuleType("sqlalchemy")
        sqlalchemy.or_ = lambda *args, **kwargs: None
        sqlalchemy.select = lambda *args, **kwargs: None
        context_triggers = types.ModuleType("daem0nmcp.context_triggers")

        class _Manager:
            def __init__(self, db_manager):
                pass

            async def get_triggered_context(self, **kwargs):
                return {
                    "triggers": [],
                    "memories": {},
                    "total_triggers": 0,
                    "error": {
                        "code": "TRIGGER_FILE_PATH_TOO_LONG",
                        "message": "Trigger file path exceeds 256 Unicode code points.",
                    },
                    "message": "Trigger evaluation was rejected.",
                }

        context_triggers.ContextTriggerManager = _Manager
        stubs = {
            "daem0nmcp.context_manager": context_manager,
            "daem0nmcp.database": database,
            "daem0nmcp.mcp_instance": mcp_instance,
            "daem0nmcp.models": models,
            "daem0nmcp.context_triggers": context_triggers,
            "sqlalchemy": sqlalchemy,
        }
        previous = {name: sys.modules.get(name) for name in stubs}
        sys.modules.update(stubs)
        spec = importlib.util.spec_from_file_location(
            module_name, root / "daem0nmcp" / "tools" / "resources.py"
        )
        if spec is None or spec.loader is None:
            self.fail("could not load MCP resources module")
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        try:
            spec.loader.exec_module(module)
            payload = json.loads(
                await module.get_triggered_context_resource(
                    "x" * 257, project_path="workspace"
                )
            )
        finally:
            sys.modules.pop(module_name, None)
            for name, value in previous.items():
                if value is None:
                    sys.modules.pop(name, None)
                else:
                    sys.modules[name] = value

        self.assertEqual(payload["triggers_matched"], 0)
        self.assertEqual(payload["context"], [])
        self.assertEqual(payload["error"]["code"], "TRIGGER_FILE_PATH_TOO_LONG")
        self.assertEqual(payload["message"], "Trigger evaluation was rejected.")


class TestCoreRegexMetadata(unittest.TestCase):
    def test_core_health_reports_regex_as_a_mandatory_distribution(self) -> None:
        """Missing regex must degrade core health with install remediation."""
        from daem0nmcp.capabilities import CapabilityRegistry

        registry = CapabilityRegistry(
            module_available=lambda distribution: distribution != "regex"
        )

        capability = registry.get("core")

        self.assertEqual(capability["status"], "degraded")
        self.assertEqual(capability["remediation"]["action"], "install_core")
        self.assertEqual(capability["remediation"]["missing"], ["regex"])

    def test_package_metadata_installs_regex_in_the_base_profile(self) -> None:
        """A normal installation must supply the timeout-capable regex engine."""
        root = Path(__file__).resolve().parents[1]
        source = (root / "pyproject.toml").read_text(encoding="utf-8")
        project_start = source.index("[project]")
        dependencies_start = source.index("dependencies = [", project_start)
        list_start = source.index("[", dependencies_start)
        list_end = source.index("]", list_start)
        dependencies = ast.literal_eval(source[list_start : list_end + 1])

        self.assertTrue(any(item.startswith("regex>=") for item in dependencies))


@unittest.skipUnless(
    importlib.util.find_spec("regex") is not None,
    "mandatory regex distribution is unavailable in this development environment",
)
class TestRealRegexIntegration(unittest.TestCase):
    def test_real_regex_package_preserves_documented_patterns(self) -> None:
        """The production compiler/search boundary must work with the real package."""
        from daem0nmcp.trigger_security import SafeUserPattern

        matcher = SafeUserPattern()

        self.assertTrue(matcher.matches(1, "auth.*", ["authentication"]).matched)
        self.assertTrue(
            matcher.matches(
                2, "UserService|AuthService", ["BillingService", "AuthService"]
            ).matched
        )
        self.assertTrue(matcher.matches(3, ".*Repository$", ["UserRepository"]).matched)


if __name__ == "__main__":
    unittest.main()
