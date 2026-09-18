"""Tests for optional dependency capability reporting and core startup."""

import asyncio
import importlib.abc
import importlib.metadata
import importlib.util
import os
import subprocess
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python 3.10
    import tomli as tomllib


OPTIONAL_IMPORT_BLOCKER = """
import importlib.abc
import sys

BLOCKED = {
    "sentence_transformers", "onnxruntime", "numpy", "qdrant_client",
    "rank_bm25", "tree_sitter", "tree_sitter_language_pack", "networkx",
    "igraph", "leidenalg", "langgraph", "llmlingua", "tiktoken",
    "e2b_code_interpreter", "opentelemetry.sdk", "opentelemetry.exporter",
    "bs4", "watchdog", "plyer",
}

class BlockOptionalImports(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if any(fullname == item or fullname.startswith(item + ".") for item in BLOCKED):
            raise ModuleNotFoundError("optional dependency deliberately blocked: " + fullname)

sys.meta_path.insert(0, BlockOptionalImports())
"""

BASE_SERVER_MODULES = (
    "fastmcp",
    "sqlalchemy",
    "aiosqlite",
    "pydantic_settings",
    "regex",
)
BASE_SERVER_AVAILABLE = all(
    importlib.util.find_spec(module) for module in BASE_SERVER_MODULES
)


class TestCoreStartupWithoutOptionalDependencies(unittest.TestCase):
    def test_core_imports_when_optional_dependencies_are_blocked(self):
        """Core package imports must not import an optional capability subsystem."""
        command = (
            OPTIONAL_IMPORT_BLOCKER
            + "\nimport daem0nmcp\nimport daem0nmcp.vectors\nprint('core-ok')\n"
        )
        completed = subprocess.run(
            [sys.executable, "-c", command],
            check=False,
            capture_output=True,
            text=True,
        )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("core-ok", completed.stdout)

    @unittest.skipUnless(
        BASE_SERVER_AVAILABLE, "mandatory base dependencies are unavailable"
    )
    def test_server_reaches_health_registration_when_optional_imports_are_blocked(self):
        """A normal base install starts the server composition root without extras."""
        command = (
            OPTIONAL_IMPORT_BLOCKER
            + "\nimport asyncio\nfrom daem0nmcp.server import create_server\n"
            + "server = create_server('stdio')\n"
            + "assert 'system_health' in [tool.name for tool in asyncio.run(server.list_tools())]\n"
            + "print('server-ok')\n"
        )
        completed = subprocess.run(
            [sys.executable, "-c", command],
            check=False,
            capture_output=True,
            text=True,
        )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("server-ok", completed.stdout)


class TestCapabilityRegistry(unittest.TestCase):
    def test_apps_profile_declares_the_validated_ingestion_stack(self):
        """Release metadata must install every validated pinned-HTTP dependency."""
        project_path = Path(__file__).resolve().parents[1] / "pyproject.toml"
        with project_path.open("rb") as project_file:
            project = tomllib.load(project_file)

        apps = project["project"]["optional-dependencies"]["apps"]
        self.assertIn("httpx>=0.28.1,<0.29", apps)
        self.assertIn("httpcore>=1.0.9,<1.1", apps)
        self.assertIn("packaging>=24.0", apps)
        self.assertIn("beautifulsoup4>=4.12.0", apps)

    def test_ci_installs_the_apps_profile_for_ingestion_security_tests(self):
        """The positive CI path must not silently skip pinned-ingestion tests."""
        workflow_path = (
            Path(__file__).resolve().parents[1] / ".github" / "workflows" / "ci.yml"
        )
        workflow = workflow_path.read_text(encoding="utf-8")

        self.assertIn('pip install -e ".[dev,apps,graph]"', workflow)

    def test_graph_and_observability_checks_do_not_import_optional_parents(self):
        """Capability inspection must use distribution metadata, never dotted imports."""
        from daem0nmcp.capabilities import PROFILES, CapabilityRegistry

        optional_roots = {"langgraph", "opentelemetry"}
        attempts: list[str] = []

        class RejectOptionalImports(importlib.abc.MetaPathFinder):
            def find_spec(self, fullname, path=None, target=None):
                if fullname.split(".")[0] in optional_roots:
                    attempts.append(fullname)
                    raise ModuleNotFoundError(fullname)
                return None

        blocker = RejectOptionalImports()
        sys.meta_path.insert(0, blocker)
        try:
            with patch("importlib.metadata.version", return_value="1.0"):
                for profile_name in ("graph", "observability"):
                    registry = CapabilityRegistry(
                        environ={PROFILES[profile_name].environment_key: "true"},
                    )
                    self.assertEqual(registry.get(profile_name)["status"], "ready")
        finally:
            sys.meta_path.remove(blocker)

        self.assertEqual(attempts, [])

    def test_require_raises_a_clear_lazy_capability_error(self):
        """Invoking a disabled feature must return its structured remediation."""
        from daem0nmcp.capabilities import (
            CapabilityRegistry,
            CapabilityUnavailableError,
        )

        registry = CapabilityRegistry(
            environ={"DAEM0NMCP_GRAPH_ENABLED": "false"},
        )

        with self.assertRaises(CapabilityUnavailableError) as raised:
            registry.require("graph")

        self.assertEqual(raised.exception.capability["status"], "disabled")
        self.assertEqual(
            raised.exception.capability["remediation"]["environment"],
            "DAEM0NMCP_GRAPH_ENABLED=true",
        )

    def test_missing_extra_is_degraded_with_install_remediation(self):
        """Removing an installed optional extra must degrade only its capability."""
        from daem0nmcp.capabilities import CapabilityRegistry

        registry = CapabilityRegistry(
            environ={"DAEM0NMCP_MODELS_LOCAL_ENABLED": "true"},
            module_available=lambda _: False,
        )

        capability = registry.get("models-local")

        self.assertEqual(capability["status"], "degraded")
        self.assertEqual(capability["remediation"]["action"], "install_extra")
        self.assertEqual(
            capability["remediation"]["command"],
            "pip install 'daem0nmcp[models-local]'",
        )
        self.assertIn("sentence-transformers", capability["remediation"]["missing"])

    def test_models_local_reports_python_floor(self):
        """models-local must provide remediation on unsupported Python versions."""
        from unittest.mock import patch

        from daem0nmcp.capabilities import CapabilityRegistry

        with patch("daem0nmcp.capabilities.sys.version_info", (3, 10, 21)):
            capability = CapabilityRegistry(
                environ={"DAEM0NMCP_MODELS_LOCAL_ENABLED": "true"},
                module_available=lambda _: True,
            ).get("models-local")

        self.assertEqual(capability["status"], "degraded")
        self.assertEqual(capability["remediation"]["action"], "upgrade_python")
        self.assertEqual(capability["remediation"]["minimum_python"], "3.11")

    def test_disabled_capability_does_not_probe_its_optional_modules(self):
        """An explicitly disabled profile must remain lazy and explain how to enable it."""
        from daem0nmcp.capabilities import CapabilityRegistry

        probes: list[str] = []
        registry = CapabilityRegistry(
            environ={"DAEM0NMCP_MODELS_LOCAL_ENABLED": "false"},
            module_available=probes.append,
        )

        capability = registry.get("models-local")

        self.assertEqual(capability["status"], "disabled")
        self.assertEqual(probes, [])
        self.assertEqual(capability["remediation"]["action"], "enable_configuration")
        self.assertEqual(
            capability["remediation"]["environment"],
            "DAEM0NMCP_MODELS_LOCAL_ENABLED=true",
        )

    def test_invalid_enablement_value_is_failed_with_structured_remediation(self):
        """An invalid profile setting is a configuration failure, not a missing extra."""
        from daem0nmcp.capabilities import CapabilityRegistry

        registry = CapabilityRegistry(
            environ={"DAEM0NMCP_MODELS_LOCAL_ENABLED": "sometimes"},
            module_available=lambda _: True,
        )

        capability = registry.get("models-local")

        self.assertEqual(capability["status"], "failed")
        self.assertEqual(capability["remediation"]["action"], "fix_configuration")
        self.assertEqual(
            capability["remediation"]["environment"], "DAEM0NMCP_MODELS_LOCAL_ENABLED"
        )

    def test_ready_profile_checks_only_the_requested_profile(self):
        """Capability inspection must not import or probe every optional subsystem."""
        from daem0nmcp.capabilities import CapabilityRegistry

        probes: list[str] = []
        registry = CapabilityRegistry(
            environ={"DAEM0NMCP_MODELS_LOCAL_ENABLED": "true"},
            module_available=lambda module: probes.append(module) or True,
        )

        capability = registry.get("models-local")

        self.assertEqual(capability["status"], "ready")
        self.assertEqual(
            set(probes),
            {"sentence-transformers", "onnxruntime", "numpy", "llmlingua"},
        )

    def test_profile_probes_cover_each_advertised_extra_dependency(self):
        """Every package advertised by a profile is included in its availability check."""
        from daem0nmcp.capabilities import PROFILES, CapabilityRegistry

        expected = {
            "graph": {
                "networkx",
                "python-igraph",
                "leidenalg",
                "langgraph",
                "langgraph-checkpoint-sqlite",
            },
            "observability": {
                "opentelemetry-api",
                "opentelemetry-sdk",
                "opentelemetry-exporter-otlp",
            },
            "apps": {
                "httpx",
                "httpcore",
                "packaging",
                "beautifulsoup4",
                "watchdog",
                "plyer",
                "tree-sitter-language-pack",
            },
        }
        for profile_name, modules in expected.items():
            probes: list[str] = []
            registry = CapabilityRegistry(
                environ={PROFILES[profile_name].environment_key: "true"},
                module_available=lambda module, probes=probes: (
                    probes.append(module) or True
                ),
            )

            self.assertEqual(registry.get(profile_name)["status"], "ready")
            self.assertEqual(set(probes), modules)


class TestOptionalCapabilityGates(unittest.TestCase):
    @unittest.skipUnless(
        BASE_SERVER_AVAILABLE, "mandatory base dependencies are unavailable"
    )
    def test_models_local_does_not_initialize_qdrant_when_local_is_disabled(self):
        """The embedder profile must not activate the separate Qdrant store profile."""
        from daem0nmcp.memory import MemoryManager

        with (
            patch.dict(
                os.environ,
                {
                    "DAEM0NMCP_MODELS_LOCAL_ENABLED": "true",
                    "DAEM0NMCP_LOCAL_ENABLED": "false",
                },
                clear=False,
            ),
            patch("daem0nmcp.memory.vectors.is_available", return_value=True),
        ):
            manager = MemoryManager(
                SimpleNamespace(storage_path="unused", format_version=7)
            )

        self.assertIsNone(manager._qdrant)

    @unittest.skipUnless(
        BASE_SERVER_AVAILABLE, "mandatory base dependencies are unavailable"
    )
    def test_hierarchical_recall_requires_graph_before_importing_communities(self):
        """Graph-backed hierarchical recall reports structured remediation when disabled."""
        from daem0nmcp.capabilities import CapabilityUnavailableError
        from daem0nmcp.memory import MemoryManager

        with patch.dict(os.environ, {"DAEM0NMCP_GRAPH_ENABLED": "false"}, clear=False):
            manager = MemoryManager(
                SimpleNamespace(storage_path="unused", format_version=7)
            )
            with self.assertRaises(CapabilityUnavailableError) as raised:
                asyncio.run(manager.recall_hierarchical("topic"))

        self.assertEqual(raised.exception.capability["name"], "graph")


class TestVectorMath(unittest.TestCase):
    def test_cosine_similarity_rejects_unequal_dimensions(self):
        """Mismatched vector dimensions must never be silently truncated."""
        from daem0nmcp.vectors import cosine_similarity

        with self.assertRaises(ValueError):
            cosine_similarity([1.0, 0.0], [1.0])


if __name__ == "__main__":
    unittest.main()
