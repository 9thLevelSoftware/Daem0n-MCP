"""Lazy capability reporting for optional Daem0n MCP dependency profiles."""

from __future__ import annotations

import os
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from importlib import metadata
from typing import Any

CAPABILITY_STATUSES = frozenset({"ready", "disabled", "degraded", "failed"})
CORE_DISTRIBUTIONS = (
    "fastmcp",
    "sqlalchemy",
    "aiosqlite",
    "greenlet",
    "pydantic-settings",
    "regex",
)


@dataclass(frozen=True)
class CapabilityProfile:
    """The configuration and imports required by one optional profile."""

    name: str
    distributions: tuple[str, ...]

    @property
    def environment_key(self) -> str:
        return "DAEM0NMCP_" + self.name.upper().replace("-", "_") + "_ENABLED"


PROFILES = {
    "local": CapabilityProfile("local", ("qdrant-client", "rank-bm25")),
    "graph": CapabilityProfile(
        "graph",
        (
            "networkx",
            "python-igraph",
            "leidenalg",
            "langgraph",
            "langgraph-checkpoint-sqlite",
        ),
    ),
    "apps": CapabilityProfile(
        "apps",
        (
            "httpx",
            "httpcore",
            "packaging",
            "beautifulsoup4",
            "watchdog",
            "plyer",
            "tree-sitter-language-pack",
        ),
    ),
    "models-local": CapabilityProfile(
        "models-local", ("sentence-transformers", "onnxruntime", "numpy", "llmlingua")
    ),
    "models-hosted": CapabilityProfile("models-hosted", ("tiktoken",)),
    "agency-e2b": CapabilityProfile("agency-e2b", ("e2b-code-interpreter",)),
    "observability": CapabilityProfile(
        "observability",
        (
            "opentelemetry-api",
            "opentelemetry-sdk",
            "opentelemetry-exporter-otlp",
        ),
    ),
}


class CapabilityUnavailableError(RuntimeError):
    """Raised only when code explicitly invokes an unavailable optional profile."""

    def __init__(self, capability: dict[str, Any]) -> None:
        self.capability = capability
        super().__init__(
            f"Capability '{capability['name']}' is {capability['status']}. "
            f"Remediation: {capability['remediation']}"
        )


def _distribution_available(distribution: str) -> bool:
    """Check distribution metadata without importing optional package parents."""
    try:
        metadata.version(distribution)
        return True
    except metadata.PackageNotFoundError:
        return False


class CapabilityRegistry:
    """Report optional profile availability without eagerly importing its code."""

    def __init__(
        self,
        environ: Mapping[str, str] | None = None,
        module_available: Callable[[str], bool] | None = None,
    ) -> None:
        self._environ = os.environ if environ is None else environ
        # `module_available` is retained as an injectable seam for callers and
        # tests; its argument is now an explicit distribution name.
        self._distribution_available = module_available or _distribution_available

    def get(self, name: str) -> dict[str, Any]:
        """Return a structured status and remediation for one capability profile."""
        if name == "core":
            return self._core()
        try:
            profile = PROFILES[name]
        except KeyError as error:
            raise KeyError(f"Unknown capability profile: {name}") from error

        configured = self._configured(profile)
        if configured is None:
            return self._failed(profile)
        if not configured:
            return self._disabled(profile)
        if name == "models-local" and sys.version_info < (3, 11):
            return self._python_version_unavailable(profile)

        missing = [
            distribution
            for distribution in profile.distributions
            if not self._distribution_available(distribution)
        ]
        if missing:
            return self._degraded(profile, missing)
        return self._ready(profile)

    def require(self, name: str) -> dict[str, Any]:
        """Return a ready capability or raise a remediation-bearing lazy error."""
        capability = self.get(name)
        if capability["status"] != "ready":
            raise CapabilityUnavailableError(capability)
        return capability

    def all(self) -> dict[str, dict[str, Any]]:
        """Return core and optional statuses when a caller requests health."""
        return {
            "core": self.get("core"),
            **{name: self.get(name) for name in PROFILES},
        }

    def _core(self) -> dict[str, Any]:
        missing = [
            distribution
            for distribution in CORE_DISTRIBUTIONS
            if not self._distribution_available(distribution)
        ]
        if not missing:
            return {
                "name": "core",
                "status": "ready",
                "remediation": {"action": "none"},
            }
        return {
            "name": "core",
            "status": "degraded",
            "remediation": {
                "action": "install_core",
                "command": "pip install daem0nmcp",
                "missing": missing,
            },
        }

    def _configured(self, profile: CapabilityProfile) -> bool | None:
        value = self._environ.get(profile.environment_key, "false").strip().lower()
        if value in {"1", "true", "yes", "on"}:
            return True
        if value in {"0", "false", "no", "off", ""}:
            return False
        return None

    @staticmethod
    def _ready(profile: CapabilityProfile) -> dict[str, Any]:
        return {
            "name": profile.name,
            "status": "ready",
            "remediation": {"action": "none"},
        }

    @staticmethod
    def _disabled(profile: CapabilityProfile) -> dict[str, Any]:
        return {
            "name": profile.name,
            "status": "disabled",
            "remediation": {
                "action": "enable_configuration",
                "environment": f"{profile.environment_key}=true",
            },
        }

    @staticmethod
    def _degraded(profile: CapabilityProfile, missing: list[str]) -> dict[str, Any]:
        return {
            "name": profile.name,
            "status": "degraded",
            "remediation": {
                "action": "install_extra",
                "command": f"pip install 'daem0nmcp[{profile.name}]'",
                "missing": missing,
            },
        }

    @staticmethod
    def _python_version_unavailable(profile: CapabilityProfile) -> dict[str, Any]:
        return {
            "name": profile.name,
            "status": "degraded",
            "remediation": {
                "action": "upgrade_python",
                "minimum_python": "3.11",
                "message": "models-local requires Python 3.11 or newer",
            },
        }

    def _failed(self, profile: CapabilityProfile) -> dict[str, Any]:
        return {
            "name": profile.name,
            "status": "failed",
            "remediation": {
                "action": "fix_configuration",
                "environment": profile.environment_key,
                "message": "Use a boolean value: true or false.",
            },
        }


def get_capabilities() -> dict[str, dict[str, Any]]:
    """Return optional profile statuses for health and diagnostics callers."""
    return CapabilityRegistry().all()
