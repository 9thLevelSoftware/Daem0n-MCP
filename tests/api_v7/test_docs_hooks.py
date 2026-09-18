"""Golden and behavioral tests for the maintained v7 protocol surfaces."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

PROTOCOL_FILES = (
    ROOT / "AGENTS.md",
    ROOT / "README.md",
    ROOT / "docs" / "multi-repo-setup.md",
    ROOT / ".claude" / "skills" / "summon_daem0n" / "SKILL.md",
    ROOT / ".claude" / "skills" / "daem0nmcp-protocol" / "SKILL.md",
    ROOT / ".opencode" / "plugins" / "daem0n.ts",
    ROOT / "daem0nmcp" / "claude_hooks" / "session_start.py",
    ROOT / "daem0nmcp" / "claude_hooks" / "pre_edit.py",
    ROOT / "daem0nmcp" / "claude_hooks" / "post_edit.py",
    ROOT / "daem0nmcp" / "claude_hooks" / "stop.py",
)

CUTOVER_FILES = (
    ROOT / "Summon_Daem0n.md",
    ROOT / "Summon_Daem0n_OpenCode.md",
    ROOT / "docs" / "index.html",
    ROOT / ".claude" / "skills" / "openspec-daem0n-bridge" / "SKILL.md",
    ROOT / "hooks" / "daem0n_prompt_hook.py",
    ROOT / "hooks" / "daem0n_pre_edit_hook.py",
    ROOT / "hooks" / "daem0n_post_edit_hook.py",
    ROOT / "hooks" / "daem0n_stop_hook.py",
    ROOT / "hooks" / "settings.json.example",
)

ROOT_HOOKS = tuple(path for path in CUTOVER_FILES if path.parent == ROOT / "hooks")

V7_RITUAL_TOOLS = (
    "session_brief",
    "memory_preflight",
    "memory_recall",
    "memory_store",
    "memory_record_outcome",
    "system_health",
)

RESOURCE_URIS = (
    "memory://workspaces/{workspace_id}/warnings",
    "memory://workspaces/{workspace_id}/failures",
    "memory://workspaces/{workspace_id}/rules",
    "memory://workspaces/{workspace_id}/active-context",
)

LEGACY_EXECUTABLE_CALL = re.compile(
    r"(?i)(?:(?:mcp__)?daem0nmcp__?|mcp__daem0nmcp__)?"
    r"(?:commune|consult|inscribe|reflect|understand|govern|explore|maintain)"
    r"\s*\("
)

LEGACY_MEMORY_CALL = re.compile(
    r"(?i)(?<![a-z0-9_])"
    r"(?:get_briefing|remember|record_outcome|recall_for_file|"
    r"check_context_triggers)\s*\("
)

RAW_SCOPE_EXAMPLE = re.compile(
    r"(?i)(?:--project-path\b|[\"']project_path[\"']\s*:|"
    r"\bproject_path\s*=)"
)

OBSOLETE_TRANSPORT_EXAMPLE = re.compile(
    r"(?i)(?:--transport\s+sse\b|text/event-stream|/sse(?:\b|/))"
)

DIRECT_MEMORY_CLI_WRITE = re.compile(
    r"(?i)(?:python(?:3)?\s+-m\s+daem0nmcp\.cli|\bdaem0nmcp)\s+"
    r"(?:remember|record-outcome|memory-store|memory-record-outcome)\b"
)


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _all_protocol_files() -> tuple[Path, ...]:
    commands = tuple(sorted((ROOT / ".opencode" / "commands").glob("*.md")))
    return PROTOCOL_FILES + CUTOVER_FILES + commands


def _tree_snapshot(root: Path) -> tuple[str, ...]:
    return tuple(sorted(path.relative_to(root).as_posix() for path in root.rglob("*")))


def _run_root_hook(
    name: str,
    *,
    workspace_root: Path,
    extra_environment: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    environment.update(
        {
            "CLAUDE_PROJECT_DIR": str(workspace_root),
            "HOME": str(workspace_root),
            "USERPROFILE": str(workspace_root),
            "PYTHONDONTWRITEBYTECODE": "1",
        }
    )
    if extra_environment:
        environment.update(extra_environment)
    return subprocess.run(
        [sys.executable, str(ROOT / "hooks" / name)],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        timeout=5,
        check=False,
    )


class ProtocolGoldenTests(unittest.TestCase):
    def test_maintained_surfaces_have_no_executable_v6_rituals(self) -> None:
        offenders: list[str] = []
        for path in _all_protocol_files():
            text = _read(path)
            if LEGACY_EXECUTABLE_CALL.search(text):
                offenders.append(str(path.relative_to(ROOT)))
            if "daem0n://" in text or OBSOLETE_TRANSPORT_EXAMPLE.search(text):
                offenders.append(str(path.relative_to(ROOT)))

        self.assertEqual(offenders, [])

    def test_cutover_surfaces_have_no_legacy_scope_or_write_examples(self) -> None:
        offenders: list[str] = []
        for path in CUTOVER_FILES:
            text = _read(path)
            checks = (
                LEGACY_MEMORY_CALL.search(text),
                RAW_SCOPE_EXAMPLE.search(text),
                DIRECT_MEMORY_CLI_WRITE.search(text),
                "_client_meta" in text,
            )
            if any(checks):
                offenders.append(str(path.relative_to(ROOT)))

        self.assertEqual(offenders, [])

    def test_cutover_docs_publish_exact_v7_ritual_and_transport(self) -> None:
        docs = (
            ROOT / "Summon_Daem0n.md",
            ROOT / "Summon_Daem0n_OpenCode.md",
            ROOT / "docs" / "index.html",
            ROOT / ".claude" / "skills" / "openspec-daem0n-bridge" / "SKILL.md",
        )
        for path in docs:
            text = _read(path)
            with self.subTest(path=path.relative_to(ROOT)):
                for tool_name in V7_RITUAL_TOOLS:
                    self.assertIn(tool_name, text)
                for uri in RESOURCE_URIS:
                    self.assertIn(uri, text)
                self.assertIn("workspace_id", text)
                self.assertIn("Streamable HTTP", text)
                self.assertIn("/mcp", text)
                self.assertIn("docs/v6-to-v7-tools.json", text)

    def test_primary_docs_publish_the_v7_protocol_contract(self) -> None:
        corpus = "\n".join(
            _read(path)
            for path in (
                ROOT / "AGENTS.md",
                ROOT / "README.md",
                ROOT / "docs" / "multi-repo-setup.md",
            )
        )

        for tool_name in V7_RITUAL_TOOLS:
            with self.subTest(tool_name=tool_name):
                self.assertIn(tool_name, corpus)
        for uri in RESOURCE_URIS:
            with self.subTest(uri=uri):
                self.assertIn(uri, corpus)

        self.assertIn("Streamable HTTP", corpus)
        self.assertIn("/mcp", corpus)
        self.assertIn("docs/v6-to-v7-tools.json", corpus)

    def test_host_integrations_use_v7_names_without_client_metadata(self) -> None:
        plugin = _read(ROOT / ".opencode" / "plugins" / "daem0n.ts")
        skills = "\n".join(
            _read(path)
            for path in (
                ROOT / ".claude" / "skills" / "summon_daem0n" / "SKILL.md",
                ROOT / ".claude" / "skills" / "daem0nmcp-protocol" / "SKILL.md",
            )
        )
        commands = "\n".join(
            _read(path)
            for path in sorted((ROOT / ".opencode" / "commands").glob("*.md"))
        )

        for tool_name in V7_RITUAL_TOOLS:
            with self.subTest(tool_name=tool_name):
                self.assertIn(tool_name, plugin + skills + commands)
        self.assertNotIn("_client_meta", plugin)
        self.assertIn("docs/v6-to-v7-tools.json", plugin + skills + commands)

    def test_hooks_do_not_open_the_mutable_v6_manager_path(self) -> None:
        hook_sources = "\n".join(
            _read(ROOT / "daem0nmcp" / "claude_hooks" / name)
            for name in ("session_start.py", "pre_edit.py", "post_edit.py", "stop.py")
        )

        self.assertNotIn("get_managers", hook_sources)
        self.assertNotIn("memory.remember", hook_sources)
        self.assertNotIn("INSERT INTO session_state", hook_sources)

    def test_root_hooks_are_read_only_v7_host_guidance(self) -> None:
        hook_sources = "\n".join(_read(path) for path in ROOT_HOOKS)

        for forbidden in (
            "subprocess",
            "daem0nmcp.cli",
            "os.system",
            "os.popen",
            "urllib.request",
            "sqlite3",
            ".write_text(",
            ".write_bytes(",
            ".mkdir(",
            ".touch(",
            ".unlink(",
            "open(",
        ):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, hook_sources)

        expected_calls = {
            "daem0n_prompt_hook.py": ("session_brief", "memory_recall"),
            "daem0n_pre_edit_hook.py": ("memory_recall", "memory_preflight"),
            "daem0n_post_edit_hook.py": ("memory_preflight", "memory_store"),
            "daem0n_stop_hook.py": (
                "memory_preflight",
                "memory_store",
                "memory_record_outcome",
            ),
            "settings.json.example": ("session_brief",),
        }
        for path in ROOT_HOOKS:
            text = _read(path)
            for tool_name in expected_calls[path.name]:
                with self.subTest(path=path.name, tool_name=tool_name):
                    self.assertIn(tool_name, text)


class HookNameTests(unittest.TestCase):
    def test_outcome_detection_accepts_bare_and_host_prefixed_names(self) -> None:
        from daem0nmcp.claude_hooks import stop

        for tool_name in (
            "memory_record_outcome",
            "daem0nmcp_memory_record_outcome",
            "mcp__daem0nmcp__memory_record_outcome",
        ):
            with self.subTest(tool_name=tool_name):
                self.assertTrue(
                    stop._is_v7_tool_call(tool_name, "memory_record_outcome")
                )
                self.assertTrue(stop._has_daem0n_outcome("", [tool_name]))

    def test_outcome_detection_rejects_legacy_or_lookalike_names(self) -> None:
        from daem0nmcp.claude_hooks import stop

        for tool_name in (
            "record_outcome",
            "daem0nmcp_record_outcome",
            "other_memory_record_outcome",
        ):
            with self.subTest(tool_name=tool_name):
                self.assertFalse(
                    stop._is_v7_tool_call(tool_name, "memory_record_outcome")
                )
                self.assertFalse(stop._has_daem0n_outcome("", [tool_name]))


class HookFailClosedTests(unittest.IsolatedAsyncioTestCase):
    async def test_stop_suggests_replay_safe_v7_calls_without_writing(self) -> None:
        from daem0nmcp.claude_hooks.stop import analyse_and_remember

        with tempfile.TemporaryDirectory() as tmp_dir:
            project = Path(tmp_dir)
            (project / ".daem0nmcp").mkdir()
            messages = [
                {"role": "user", "content": "Add caching"},
                {
                    "role": "assistant",
                    "content": (
                        "I will use Redis for caching because it provides durable "
                        "shared state. Implementation is complete and all tasks "
                        "are done."
                    ),
                },
            ]
            state = {"reminder_count": 0, "last_reminder_turn": -1}

            result = await analyse_and_remember(str(project), messages, state)

            self.assertIn("memory_store", result.message)
            self.assertIn("memory_record_outcome", result.message)
            self.assertRegex(
                result.message,
                r"workspace_id=[\"']ws_[a-f0-9]{24}[\"']",
            )
            self.assertIn("idempotency_key", result.message)
            self.assertIn("preflight_token", result.message)
            self.assertFalse((project / ".daem0nmcp" / "storage").exists())

    async def test_pre_edit_fails_closed_without_native_bridge(self) -> None:
        from daem0nmcp.claude_hooks.pre_edit import async_main

        with tempfile.TemporaryDirectory() as tmp_dir:
            project = Path(tmp_dir)
            (project / ".daem0nmcp").mkdir()
            result = await async_main(str(project), str(project / "server.py"))

            self.assertFalse(result.allowed)
            self.assertEqual("EDIT_BRIDGE_UNAVAILABLE", result.message)
            self.assertFalse((project / ".daem0nmcp" / "storage").exists())


class RootHookProcessTests(unittest.TestCase):
    def test_pre_edit_process_denies_without_mutating_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            workspace_root = Path(tmp_dir)
            (workspace_root / ".daem0nmcp").mkdir()
            before = _tree_snapshot(workspace_root)

            result = _run_root_hook(
                "daem0n_pre_edit_hook.py",
                workspace_root=workspace_root,
                extra_environment={
                    "TOOL_INPUT": json.dumps(
                        {"file_path": str(workspace_root / "service.py")}
                    )
                },
            )

            self.assertEqual(2, result.returncode)
            self.assertEqual("", result.stdout)
            self.assertIn("fails closed", result.stderr)
            self.assertIn("memory_recall", result.stderr)
            self.assertIn("memory_preflight", result.stderr)
            self.assertRegex(result.stderr, r"workspace_id=[\"']ws_[a-f0-9]{24}")
            self.assertIn("target_tool", result.stderr)
            self.assertIn("target_arguments", result.stderr)
            self.assertEqual(before, _tree_snapshot(workspace_root))

    def test_advisory_processes_emit_exact_v7_calls_without_writing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            workspace_root = Path(tmp_dir)
            (workspace_root / ".daem0nmcp").mkdir()
            transcript = workspace_root / "transcript.jsonl"
            transcript.write_text(
                json.dumps(
                    {
                        "role": "assistant",
                        "content": (
                            "I will use signed cookies because they avoid shared "
                            "server state. Implementation is complete."
                        ),
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            before = _tree_snapshot(workspace_root)
            tool_input = json.dumps(
                {
                    "file_path": str(workspace_root / "service.py"),
                    "new_string": "async def authenticate(token): pass",
                }
            )

            prompt = _run_root_hook(
                "daem0n_prompt_hook.py",
                workspace_root=workspace_root,
            )
            post_edit = _run_root_hook(
                "daem0n_post_edit_hook.py",
                workspace_root=workspace_root,
                extra_environment={"TOOL_INPUT": tool_input},
            )
            stop = _run_root_hook(
                "daem0n_stop_hook.py",
                workspace_root=workspace_root,
                extra_environment={
                    "CLAUDE_TRANSCRIPT_PATH": str(transcript),
                },
            )

            self.assertEqual(0, prompt.returncode)
            self.assertIn("session_brief", prompt.stdout)
            self.assertIn("memory_recall", prompt.stdout)
            self.assertEqual(0, post_edit.returncode)
            self.assertIn("memory_preflight", post_edit.stdout)
            self.assertIn("memory_store", post_edit.stdout)
            self.assertIn("idempotency_key", post_edit.stdout)
            self.assertEqual(0, stop.returncode)
            stop_payload = json.loads(stop.stdout)
            self.assertEqual("block", stop_payload["decision"])
            self.assertIn("memory_preflight", stop_payload["reason"])
            self.assertIn("memory_store", stop_payload["reason"])
            self.assertIn("memory_record_outcome", stop_payload["reason"])
            self.assertIn("idempotency_key", stop_payload["reason"])
            self.assertEqual(before, _tree_snapshot(workspace_root))


if __name__ == "__main__":
    unittest.main()
