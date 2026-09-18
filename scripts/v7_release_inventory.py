#!/usr/bin/env python3
"""Generate and validate the evidence-driven v7 release inventory."""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import re
import sys
from copy import deepcopy
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
REQUIREMENTS_PATH = ROOT / "docs/release/v7/requirements.json"
INVENTORY_PATH = ROOT / "docs/release/v7/inventory.md"
MAPPING_PATH = ROOT / "docs/v6-to-v7-tools.json"
VALID_STATUSES = frozenset({"pending", "implemented", "accepted", "blocked"})


def _load_json(path: Path) -> dict[str, Any]:
    result = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(result, dict):
        raise ValueError(f"{path.relative_to(ROOT)} must contain an object")
    return result


def _v6_operations(mapping: dict[str, Any]) -> dict[str, list[str]]:
    result: dict[str, list[str]] = {}
    for item in mapping.get("mappings", []):
        if isinstance(item, dict) and isinstance(item.get("old_operation"), str):
            for name in item.get("new_tools", []):
                if isinstance(name, str):
                    result.setdefault(name, []).append(item["old_operation"])
    return result


def _cli_commands() -> list[str]:
    tree = ast.parse((ROOT / "daem0nmcp/cli.py").read_text(encoding="utf-8"))
    commands = set()
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "add_parser"
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and isinstance(node.args[0].value, str)
        ):
            commands.add(node.args[0].value)
    return sorted(commands)


def _operation_entry_points() -> dict[str, str]:
    """Extract literal production operation-map membership from builder modules."""
    operation_directory = ROOT / "daem0nmcp/api/v7"
    operation_modules = [
        operation_directory / "operations.py",
        *sorted(operation_directory.glob("*_operations.py")),
    ]
    entries: dict[str, str] = dict.fromkeys(
        (
            "session_brief",
            "memory_preflight",
            "memory_recall",
            "memory_store",
            "memory_record_outcome",
            "system_health",
        ),
        "daem0nmcp.api.v7.pinned:build_pinned_handlers",
    )
    for path in operation_modules:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        module = f"daem0nmcp.api.v7.{path.stem}"
        for function in tree.body:
            if not isinstance(
                function, (ast.FunctionDef, ast.AsyncFunctionDef)
            ) or not function.name.startswith("build_"):
                continue
            for node in ast.walk(function):
                if not (
                    isinstance(node, ast.Dict)
                    and all(
                        isinstance(key, ast.Constant) and isinstance(key.value, str)
                        for key in node.keys
                        if key is not None
                    )
                ):
                    continue
                for key in node.keys:
                    if key is not None and isinstance(key.value, str):
                        entries[key.value] = f"{module}:{function.name}"
    return entries


def collect(requirements: dict[str, Any]) -> dict[str, Any]:
    """Read manifest schemas and static entry-point inventories without a server."""
    from daem0nmcp.api.v7.dashboard_resources import DASHBOARD_RESOURCE_URIS
    from daem0nmcp.api.v7.policy import V7_TOOL_LEVELS
    from daem0nmcp.api.v7.resources import RESOURCE_URI_TEMPLATES
    from daem0nmcp.api.v7.tools import _TOOL_CATEGORIES, TOOL_INPUT_MODELS

    mapping = _load_json(MAPPING_PATH)
    parity = _v6_operations(mapping)
    operation_entries = _operation_entry_points()
    tools = []
    for name in sorted(V7_TOOL_LEVELS):
        schema = TOOL_INPUT_MODELS[name].model_json_schema()
        properties = schema.get("properties", {})
        required = set(schema.get("required", []))
        if not isinstance(properties, dict):
            raise ValueError(f"{name} input schema has no properties")
        tools.append(
            {
                "name": name,
                "category": _TOOL_CATEGORIES[name],
                "covenant": V7_TOOL_LEVELS[name].value,
                "options": [
                    {"name": option, "required": option in required}
                    for option in sorted(properties)
                ],
                "v6_parity": sorted(parity.get(name, [])),
                "owner": "api/v7/production",
                "operation_entry_point": (
                    "daem0nmcp.api.v7.production:create_v7_server -> "
                    + operation_entries.get(name, "unwired operation mapping")
                ),
            }
        )
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    optional = pyproject.split("[project.optional-dependencies]", 1)[1]
    optional = re.split(r"\n\[[^\]]+\]\n", optional, maxsplit=1)[0]
    profiles = sorted(re.findall(r"^([\w-]+)\s*=\s*\[", optional, re.MULTILINE))
    return {
        "requirements": requirements,
        "tools": tools,
        "resources": sorted(set(RESOURCE_URI_TEMPLATES) | DASHBOARD_RESOURCE_URIS),
        "profiles": profiles,
        "cli_commands": _cli_commands(),
        "hooks": sorted(
            {
                path.relative_to(ROOT).as_posix()
                for directory in (
                    ROOT / "hooks",
                    ROOT / "daem0nmcp/claude_hooks",
                    ROOT / ".opencode/plugins",
                )
                for path in directory.iterdir()
                if path.suffix in {".py", ".ts"} and path.name != "__init__.py"
            }
            | {"daem0nmcp/opencode_install.py"}
        ),
        "mapping_count": len(mapping.get("mappings", [])),
        "release_scenarios": _load_json(
            ROOT / "docs/release/v7/acceptance-scenarios.json"
        ),
    }


def _preserve(
    previous: dict[str, dict[str, Any]], requirement: dict[str, Any]
) -> dict[str, Any]:
    old = previous.get(requirement["id"], {})
    requirement["status"] = old.get("status", "pending")
    requirement["evidence"] = old.get("evidence", [])
    return requirement


def synchronize(requirements: dict[str, Any], data: dict[str, Any]) -> dict[str, Any]:
    """Materialize atomic requirements while preserving reviewed status/evidence."""
    synced = deepcopy(requirements)
    previous = {
        item.get("id"): item
        for item in synced.get("atomic_requirements", [])
        if isinstance(item, dict) and isinstance(item.get("id"), str)
    }
    atomic: list[dict[str, Any]] = []
    for tool in data["tools"]:
        name = tool["name"]
        atomic.append(
            _preserve(
                previous,
                {
                    "id": f"tool.{name}.execute",
                    "kind": "tool-execution",
                    "owner": tool["owner"],
                    "package": "daem0nmcp.api.v7",
                    "entry_point": tool["operation_entry_point"],
                    "acceptance": f"Enabled supported profile completes production stdio and HTTP calls of {name}; assert its documented success result and side effect. Disabled-profile remediation is tracked separately.",
                },
            )
        )
        for option in tool["options"]:
            atomic.append(
                _preserve(
                    previous,
                    {
                        "id": f"tool.{name}.argument.{option['name']}",
                        "kind": "tool-argument",
                        "owner": tool["owner"],
                        "package": "daem0nmcp.api.v7.models",
                        "entry_point": f"daem0nmcp.api.v7.tools:TOOL_INPUT_MODELS[{name!r}]",
                        "acceptance": f"Enabled supported profile exercises a {'required' if option['required'] else 'present and omitted optional'} {option['name']} branch for {name}, asserts the requested documented result or side effect, and rejects an invalid branch.",
                    },
                )
            )
    for uri in data["resources"]:
        atomic.append(
            _preserve(
                previous,
                {
                    "id": f"resource.{uri}",
                    "kind": "resource",
                    "owner": "api/v7/resources",
                    "package": "daem0nmcp.api.v7.resources",
                    "entry_point": (
                        "daem0nmcp.api.v7.dashboard_resources:build_dashboard_resource_specs"
                        if uri.startswith("ui://")
                        else "daem0nmcp.api.v7.resources:build_resource_specs"
                    ),
                    "acceptance": (
                        f"Stdio and HTTP discover/read of {uri} delivers a packaged shell; actual App host renders authorized bounded v7 tool data and executes its documented actions."
                        if uri.startswith("ui://")
                        else f"Authorized stdio and HTTP read of {uri} returns bounded JSON; unauthorized reads are denied."
                    ),
                },
            )
        )
    for command in data["cli_commands"]:
        atomic.append(
            _preserve(
                previous,
                {
                    "id": f"cli.{command}",
                    "kind": "cli",
                    "owner": "cli",
                    "package": "daem0nmcp.cli",
                    "entry_point": "daem0nmcp.cli:main",
                    "acceptance": f"python -m daem0nmcp.cli {command} parses and exercises its documented result contract.",
                },
            )
        )
    for profile in data["profiles"]:
        atomic.append(
            _preserve(
                previous,
                {
                    "id": f"profile.{profile}",
                    "kind": "dependency-profile",
                    "owner": "packaging",
                    "package": "pyproject.toml",
                    "entry_point": f"optional-dependency:{profile}",
                    "acceptance": f"Install profile {profile} and exercise its advertised capability.",
                },
            )
        )
    for hook in data["hooks"]:
        atomic.append(
            _preserve(
                previous,
                {
                    "id": f"hook.{hook}",
                    "kind": "hook",
                    "owner": "hooks",
                    "package": "hooks",
                    "entry_point": hook,
                    "acceptance": f"Hook {hook} runs with its documented input and redacted failure behavior.",
                },
            )
        )
    for name in synced["expected_surface"]["planned_tools"]:
        atomic.append(
            _preserve(
                previous,
                {
                    "id": f"planned-tool.{name}",
                    "kind": "planned-tool",
                    "owner": "api/v7",
                    "package": "daem0nmcp.api.v7",
                    "entry_point": "unwired",
                    "acceptance": f"{name} is registered and exercised through the production MCP server.",
                },
            )
        )
    for item in synced["release_requirements"]:
        atomic.append(
            _preserve(
                previous,
                {
                    "id": f"gate.{item['id']}",
                    "kind": "release-gate",
                    "owner": item["owner"],
                    "package": item["owner"],
                    "entry_point": "release ledger",
                    "acceptance": item["acceptance"],
                },
            )
        )
    for package, scenarios in data["release_scenarios"].items():
        for scenario, acceptance in scenarios.items():
            atomic.append(
                _preserve(
                    previous,
                    {
                        "id": f"scenario.{package}.{scenario}",
                        "kind": "release-scenario",
                        "owner": package,
                        "package": package,
                        "entry_point": "docs/release/v7/acceptance-scenarios.json",
                        "acceptance": acceptance,
                    },
                )
            )
    synced["atomic_requirements"] = atomic
    synced["observed_surface"] = {
        "registered_tools": len(data["tools"]),
        "resource_templates": len(data["resources"]),
        "cli_commands": len(data["cli_commands"]),
        "dependency_profiles": len(data["profiles"]),
        "hooks": len(data["hooks"]),
        "atomic_requirements": len(atomic),
    }
    return synced


def _validate_status(item: dict[str, Any]) -> str | None:
    if item.get("status") not in VALID_STATUSES:
        return f"{item.get('id')} has invalid status"
    if item.get("status") == "accepted":
        evidence = item.get("evidence")
        if not isinstance(evidence, list) or not evidence:
            return f"{item.get('id')} is accepted without evidence"
        for reference in evidence:
            if not isinstance(reference, dict) or not all(
                isinstance(reference.get(key), str) and reference[key]
                for key in ("kind", "reference", "commit")
            ):
                return f"{item.get('id')} has invalid accepted evidence"
            if re.fullmatch(r"[0-9a-f]{40}", reference["commit"]) is None:
                return f"{item.get('id')} accepted evidence requires a full commit SHA"
    return None


def validate(data: dict[str, Any]) -> list[str]:
    requirements = data["requirements"]
    errors = []
    if set(
        requirements.get("expected_surface", {}).get("dependency_profiles", [])
    ) != set(data["profiles"]):
        errors.append("dependency profile set differs from requirements.json")
    ids = {
        item.get("id")
        for item in requirements.get("release_requirements", [])
        if isinstance(item, dict)
    }
    if ids != {f"P{number}" for number in range(11)}:
        errors.append("release requirements must contain exactly P0 through P10")
    for item in [
        *requirements.get("release_requirements", []),
        *requirements.get("atomic_requirements", []),
    ]:
        if not isinstance(item, dict):
            errors.append("requirement entry is not an object")
        else:
            error = _validate_status(item)
            if error:
                errors.append(error)
    for relative, expected_hash in requirements.get("fixture_checksums", {}).items():
        path = ROOT / relative
        actual = (
            hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None
        )
        if actual != expected_hash:
            errors.append(f"fixture checksum mismatch: {relative}")
    if not requirements.get("migration_fixture_sources"):
        errors.append("migration fixture sources are missing")
    return errors


def _evidence(item: dict[str, Any]) -> str:
    evidence = item.get("evidence", [])
    return (
        "; ".join(reference["reference"] for reference in evidence)
        if evidence
        else "pending evidence"
    )


def render(data: dict[str, Any]) -> str:
    requirements = data["requirements"]
    atomic = {item["id"]: item for item in requirements["atomic_requirements"]}
    lines = [
        "# v7 release inventory",
        "",
        "Generated by `python scripts/v7_release_inventory.py --write`. Registration is not acceptance; only an `accepted` requirement with explicit evidence metadata can be accepted.",
        "",
        f"Production entry point: `{requirements['entry_point']}`. Registered tools: **{len(data['tools'])}**. Resource templates: **{len(data['resources'])}**. v6 mappings: **{data['mapping_count']}**. Atomic requirements: **{len(atomic)}**.",
        "",
        "## Release ledger",
        "",
        "| ID | Owner/package | Acceptance scenario | Evidence | Status |",
        "| --- | --- | --- | --- | --- |",
    ]
    for item in requirements["release_requirements"]:
        lines.append(
            f"| {item['id']} | {item['owner']} | {item['acceptance']} | {_evidence(item)} | {item['status']} |"
        )
    lines.extend(
        [
            "",
            "## Registered MCP tools",
            "",
            "| Tool | Owner/package | Operation entry point | Input branches (`*` required) | Production acceptance/evidence | Status |",
            "| --- | --- | --- | --- | --- |",
        ]
    )
    for tool in data["tools"]:
        execution = atomic[f"tool.{tool['name']}.execute"]
        options = (
            ", ".join(
                f"`{item['name']}`{'*' if item['required'] else ''} ([tool.{tool['name']}.argument.{item['name']}])"
                for item in tool["options"]
            )
            or "none"
        )
        lines.append(
            f"| `{tool['name']}` | {tool['owner']} / `daem0nmcp.api.v7` | `{tool['operation_entry_point']}` | {options} | {execution['acceptance']} — {_evidence(execution)} | {execution['status']} |"
        )
    lines.extend(
        [
            "",
            "## Resources, hooks, CLI, and profiles",
            "",
            "| Kind | Surface | Entry point | Acceptance/evidence | Status |",
            "| --- | --- | --- | --- | --- |",
        ]
    )
    for prefix, values in (
        ("resource", data["resources"]),
        ("hook", data["hooks"]),
        ("cli", data["cli_commands"]),
        ("profile", data["profiles"]),
    ):
        for value in values:
            item = atomic[f"{prefix}.{value}"]
            lines.append(
                f"| {item['kind']} | `{value}` | `{item['entry_point']}` | {item['acceptance']} — {_evidence(item)} | {item['status']} |"
            )
    lines.extend(
        [
            "",
            "## Planned tools and frozen fixtures",
            "",
            "| Surface | Entry point | Acceptance/evidence | Status |",
            "| --- | --- | --- | --- |",
        ]
    )
    for name in requirements["expected_surface"]["planned_tools"]:
        item = atomic[f"planned-tool.{name}"]
        lines.append(
            f"| `{name}` | {item['entry_point']} | {item['acceptance']} — {_evidence(item)} | {item['status']} |"
        )
    for relative, checksum in sorted(requirements["fixture_checksums"].items()):
        lines.append(
            f"| `{relative}` | SHA-256 `{checksum}` | frozen retrieval/relevance fixture | frozen |"
        )
    lines.extend(
        [
            "",
            "Migration fixture sources: "
            + ", ".join(
                f"`{source}`" for source in requirements["migration_fixture_sources"]
            )
            + ".",
            "",
        ]
    )
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--write", action="store_true")
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    requirements = _load_json(REQUIREMENTS_PATH)
    data = collect(requirements)
    requirements = synchronize(requirements, data)
    data["requirements"] = requirements
    errors = validate(data)
    rendered = render(data)
    serialized = json.dumps(requirements, indent=2, sort_keys=True) + "\n"
    if args.write:
        REQUIREMENTS_PATH.write_text(serialized, encoding="utf-8")
        INVENTORY_PATH.write_text(rendered, encoding="utf-8")
    if args.check:
        if REQUIREMENTS_PATH.read_text(encoding="utf-8") != serialized:
            errors.append("requirements.json is stale; run inventory --write")
        if (
            not INVENTORY_PATH.is_file()
            or INVENTORY_PATH.read_text(encoding="utf-8") != rendered
        ):
            errors.append("inventory.md is stale; run inventory --write")
    if errors:
        print(
            "v7 release inventory check failed:", *errors, sep="\n- ", file=sys.stderr
        )
        return 1
    print(
        f"v7 release inventory valid: {len(data['tools'])} tools, {len(data['resources'])} resources, {len(requirements['atomic_requirements'])} atomic requirements"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
