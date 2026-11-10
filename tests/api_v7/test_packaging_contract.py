from __future__ import annotations

import re
import unittest
from pathlib import Path
from typing import Any

try:
    import tomllib
except ImportError:  # pragma: no cover - exercised on Python 3.10
    import tomli as tomllib


PROJECT_ROOT = Path(__file__).resolve().parents[2]
_REQUIREMENT_RE = re.compile(
    r"^(?P<name>[A-Za-z0-9_.-]+)"
    r"(?:\[(?P<extras>[^]]+)\])?"
    r"(?P<specifier>.*)$"
)


def _canonical_name(value: str) -> str:
    return re.sub(r"[-_.]+", "-", value).lower()


def _parse_requirement(value: str) -> tuple[str, tuple[str, ...], str]:
    requirement, _, _marker = value.strip().partition(";")
    match = _REQUIREMENT_RE.fullmatch(requirement.strip())
    if match is None:
        raise AssertionError(f"unsupported project requirement: {value!r}")
    extras = tuple(
        sorted(
            _canonical_name(extra.strip())
            for extra in (match.group("extras") or "").split(",")
            if extra.strip()
        )
    )
    return (
        _canonical_name(match.group("name")),
        extras,
        match.group("specifier").strip(),
    )


def _project_marker(value: str, extra: str | None = None) -> str | None:
    """Normalize PEP 508 markers to the form emitted in uv.lock metadata."""
    _requirement, separator, marker = value.strip().partition(";")
    normalized = re.sub(r"\bpython_version\b", "python_full_version", marker.strip())
    if extra:
        normalized = (
            f"{normalized} and extra == '{extra}'"
            if separator and normalized
            else f"extra == '{extra}'"
        )
    return normalized or None


def _locked_requirement(
    value: dict[str, Any],
) -> tuple[str, tuple[str, ...], str]:
    extras = value.get("extras", value.get("extra", ()))
    if isinstance(extras, str):
        extras = (extras,)
    return (
        _canonical_name(value["name"]),
        tuple(sorted(_canonical_name(extra) for extra in extras)),
        value.get("specifier", ""),
    )


class PackagingContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.project = tomllib.loads(
            (PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8")
        )["project"]
        cls.lock = tomllib.loads((PROJECT_ROOT / "uv.lock").read_text(encoding="utf-8"))

    def test_v7_manifest_pins_supported_fastmcp_profiles(self) -> None:
        self.assertEqual(self.project["version"], "7.0.0.dev0")
        self.assertIn("fastmcp==3.4.7", self.project["dependencies"])
        self.assertEqual(
            self.project["optional-dependencies"]["tasks"],
            ["fastmcp[tasks]==3.4.7"],
        )

    def test_lock_editable_root_matches_project_metadata(self) -> None:
        roots = [
            package
            for package in self.lock["package"]
            if package["name"] == self.project["name"]
            and package.get("source") == {"editable": "."}
        ]
        self.assertEqual(len(roots), 1)
        root = roots[0]

        self.assertEqual(root["version"], self.project["version"])

        expected_base = {
            _parse_requirement(requirement)[0]
            for requirement in self.project["dependencies"]
        }
        locked_base = {
            _canonical_name(requirement["name"]) for requirement in root["dependencies"]
        }
        self.assertEqual(locked_base, expected_base)

        project_extras = self.project["optional-dependencies"]
        locked_extras = root["optional-dependencies"]
        self.assertEqual(set(locked_extras), set(project_extras))
        for extra, requirements in project_extras.items():
            self.assertEqual(
                {_locked_requirement(value)[:2] for value in locked_extras[extra]},
                {_parse_requirement(value)[:2] for value in requirements},
                extra,
            )

        metadata = root["metadata"]
        self.assertEqual(
            set(metadata["provides-extras"]),
            set(project_extras),
        )
        expected_requires_dist = {
            (*_parse_requirement(requirement), _project_marker(requirement))
            for requirement in self.project["dependencies"]
        }
        expected_requires_dist.update(
            {
                (
                    *_parse_requirement(requirement),
                    _project_marker(requirement, extra),
                )
                for extra, requirements in project_extras.items()
                for requirement in requirements
            }
        )
        locked_requires_dist = {
            (*_locked_requirement(requirement), requirement.get("marker"))
            for requirement in metadata["requires-dist"]
        }
        self.assertEqual(locked_requires_dist, expected_requires_dist)


if __name__ == "__main__":
    unittest.main()
