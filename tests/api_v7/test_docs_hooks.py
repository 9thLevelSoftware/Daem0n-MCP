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
    event: dict[str, object],
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
    return subprocess.run(
        [sys.executable, str(ROOT / "hooks" / name)],
        cwd=workspace_root,
        env=environment,
        input=json.dumps(event),
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

    def test_root_hooks_are_stdlib_only_deprecation_stubs(self) -> None:
        for path in ROOT_HOOKS:
            text = _read(path)
            with self.subTest(path=path.name):
                imports = re.findall(r"^\s*(?:import|from)\s+(\S+)", text, re.M)
                self.assertEqual(["sys"], imports)
                self.assertIn("install-claude-hooks", text)
                self.assertIn("sys.exit(0)", text)
                self.assertNotIn("exit(2)", text)


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

    def test_pre_edit_reminds_without_blocking(self) -> None:
        from daem0nmcp.claude_hooks.pre_edit import reminder

        with tempfile.TemporaryDirectory() as tmp_dir:
            project = Path(tmp_dir)
            event = {
                "cwd": str(project),
                "tool_name": "Edit",
                "tool_input": {"file_path": str(project / "server.py")},
            }
            self.assertIsNone(reminder(event))
            (project / ".daem0nmcp").mkdir()
            output = json.loads(reminder(event) or "{}")["hookSpecificOutput"]

            self.assertNotIn("permissionDecision", output)
            self.assertIn("memory_preflight", output["additionalContext"])
            self.assertIn(
                'memory_recall_file(relative_file_path="server.py")',
                output["additionalContext"],
            )
            self.assertFalse((project / ".daem0nmcp" / "storage").exists())


class RootHookProcessTests(unittest.TestCase):
    def test_legacy_stubs_exit_zero_on_a_pre_tool_use_payload(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            workspace_root = Path(tmp_dir)
            (workspace_root / ".daem0nmcp").mkdir()
            before = _tree_snapshot(workspace_root)
            event = {
                "session_id": "s-1",
                "cwd": str(workspace_root),
                "hook_event_name": "PreToolUse",
                "tool_name": "Edit",
                "tool_input": {
                    "file_path": str(workspace_root / "service.py"),
                    "old_string": "a",
                    "new_string": "b",
                },
            }

            for path in ROOT_HOOKS:
                with self.subTest(path=path.name):
                    result = _run_root_hook(
                        path.name, workspace_root=workspace_root, event=event
                    )
                    self.assertEqual(0, result.returncode)
                    self.assertEqual("", result.stdout)
                    self.assertIn("deprecated", result.stderr)
                    self.assertIn("install-claude-hooks", result.stderr)
            self.assertEqual(before, _tree_snapshot(workspace_root))


if __name__ == "__main__":
    unittest.main()
