from __future__ import annotations

import sqlite3
import threading
from dataclasses import replace
from pathlib import Path
from types import MappingProxyType

import pytest

from daem0nmcp.api.v7.application import AdmittedRequest
from daem0nmcp.api.v7.code_entity_operations import (
    CodeEntityOperationDependencies,
    build_code_entity_operations,
)
from daem0nmcp.api.v7.discovery_operations import (
    DiscoveryOperationDependencies,
    _code_index_sync,
    _resolve_code_edges,
    default_code_indexer_factory,
)
from daem0nmcp.api.v7.tools import TOOL_INPUT_MODELS
from daem0nmcp.discovery_projection import CodeEntityProjectionSeed
from tests.api_v7.process_client import initialize_workspaces


def _entities(relative: str, source: bytes) -> list[dict]:
    producer = default_code_indexer_factory()
    return list(
        producer.index_source_strict(
            Path("/fixture") / relative, Path("/fixture"), source
        )
    )


def _edges(files: dict[str, bytes]) -> set[tuple[str, str, str, str, str]]:
    seeds = {}
    relationships = {}
    for relative, source in files.items():
        for index, entity in enumerate(_entities(relative, source)):
            key = f"{relative}:{index}"
            seeds[key] = CodeEntityProjectionSeed(
                key,
                entity["entity_type"],
                entity["qualified_name"],
                relative,
                entity["line_start"],
                entity["line_end"],
            )
            relationships[key] = [
                (kind, str(item["name"]), item["line"])
                for kind in ("call", "reference", "import")
                for item in entity[kind + "s"]
            ]
    return {
        (
            seeds[edge.source_key].relative_file_path,
            seeds[edge.source_key].qualified_name,
            seeds[edge.target_key].relative_file_path,
            seeds[edge.target_key].qualified_name,
            edge.kind,
        )
        for edge in _resolve_code_edges(seeds, relationships)
    }


def test_real_typescript_local_callers_are_not_declarations_or_strings():
    source = b"""import { createHash } from "node:crypto";
export function sha256utf8(canonical: string): string {
  return createHash("sha256").update(canonical, "utf8").digest("hex");
}
export function fingerprintHash(value: string): string { return sha256utf8(value); }
export function fingerprintRoot(value: string): string { return sha256utf8(value); }
export function unrelated(): string { return "sha256utf8()"; }
"""
    calls = {
        edge[1:4:2] for edge in _edges({"fingerprint.ts": source}) if edge[-1] == "call"
    }
    assert calls == {
        ("fingerprint.fingerprintHash", "fingerprint.sha256utf8"),
        ("fingerprint.fingerprintRoot", "fingerprint.sha256utf8"),
    }


def test_typescript_lexical_shadowing_nested_owners_and_unknown_receivers():
    source = b"""export function target() { return 1; }
export function outer() { function nested() { return target(); } return nested(); }
export function parameter(target: () => number) { return target(); }
export function block() { { const target = () => 2; target(); } return target(); }
export function objectCall(obj: { target: () => number }) { return obj.target(); }
export class A { target() {} caller() { this.target(); } }
export class B { target() {} }
export function arrow() { return () => target(); }
"""
    calls = {
        (edge[1], edge[3])
        for edge in _edges({"scope.ts": source})
        if edge[-1] == "call"
    }
    assert calls == {
        ("scope.outer.nested", "scope.target"),
        ("scope.outer", "scope.outer.nested"),
        ("scope.block", "scope.target"),
    }


def test_tsx_calls_preserve_scope_without_treating_jsx_tag_as_call():
    source = b"""function target() { return 1; }
export function Component() { return <div>{target()}</div>; }
export function Intrinsic() { return <target />; }
"""
    edges = _edges({"view.tsx": source})
    assert any(
        edge[1] == "view.Component" and edge[3] == "view.target" and edge[-1] == "call"
        for edge in edges
    )
    assert not any(edge[1] == "view.Intrinsic" for edge in edges)


def test_relative_typescript_imports_do_not_authenticate_unverified_exports():
    files = {
        "src/dep.ts": b"export function target() { return 1; }\n",
        "other/dep.ts": b"export function target() { return 2; }\n",
        "src/client.ts": b"""import { target as chosen } from "./dep.js";
import * as ns from "./dep";
import { target as foreign } from "dep";
export function entry() { chosen(); ns.target(); foreign(); }
export function unused() { return 1; }
export function shadow(chosen: () => void) { chosen(); }
""",
    }
    edges = _edges(files)
    calls = {(edge[1], edge[2], edge[3]) for edge in edges if edge[-1] == "call"}
    assert calls == set()
    assert not any(edge[1] in {"client.unused", "client.shadow"} for edge in edges)


def test_typescript_interface_does_not_shadow_outer_runtime_function():
    source = b"""function target() { return 1; }
export function caller() { interface target { value: number } return target(); }
"""
    calls = {
        (edge[1], edge[3])
        for edge in _edges({"types.ts": source})
        if edge[-1] == "call"
    }
    assert calls == {("types.caller", "types.target")}


def test_typescript_unsupported_enum_shadows_outer_runtime_function():
    source = b"""function target() { return 1; }
export function caller() { enum target { Value } return target(); }
export function unaffected() { return target(); }
"""
    calls = {
        (edge[1], edge[3])
        for edge in _edges({"values.ts": source})
        if edge[-1] == "call"
    }
    assert calls == {("values.unaffected", "values.target")}


def test_named_function_expression_self_binding_does_not_target_outer_function():
    source = b"""function target() { return 1; }
const holder = function target() { function nested() { return target(); } return nested; };
export function unaffected() { return target(); }
"""
    calls = {
        (edge[1], edge[3])
        for edge in _edges({"expression.ts": source})
        if edge[-1] == "call"
    }
    assert calls == {("expression.unaffected", "expression.target")}


def test_typescript_export_alias_does_not_link_import_to_private_same_named_function():
    files = {
        "dep.ts": b"""function target() { return 1; }
function actual() { return 2; }
export { actual as target };
""",
        "client.ts": b"""import { target } from "./dep";
export function caller() { return target(); }
""",
    }
    assert not any(edge[1] == "client.caller" for edge in _edges(files))


@pytest.mark.parametrize(
    "declaration",
    [
        "const C = class target { method() { return target; } }; return new C();",
        "return class { value = target(); };",
    ],
)
def test_unindexed_class_expression_does_not_leak_self_or_field_calls(declaration):
    source = (
        "function target() { return 1; }\n"
        f"export function build() {{ {declaration} }}\n"
        "export function unaffected() { return target(); }\n"
    ).encode()
    edges = _edges({"class-expression.ts": source})
    assert not any(edge[1].startswith("class-expression.build") for edge in edges)
    assert any(
        edge[1] == "class-expression.unaffected"
        and edge[3] == "class-expression.target"
        and edge[-1] == "call"
        for edge in edges
    )


def _request(tool: str, **arguments) -> AdmittedRequest:
    model = TOOL_INPUT_MODELS[tool].model_validate(arguments)
    return AdmittedRequest(tool, MappingProxyType(model.model_dump(mode="python")))


@pytest.mark.asyncio
async def test_ordinary_index_recovers_imports_only_generation_and_persists_callers(
    tmp_path,
):
    project = tmp_path / "project"
    project.mkdir()
    source = project / "fingerprint.ts"
    source.write_text(
        "export function sha256utf8(value: string) { return value; }\n"
        "export function fingerprintHash(value: string) { return sha256utf8(value); }\n"
        "export function fingerprintRoot(value: string) { return sha256utf8(value); }\n",
        encoding="utf-8",
    )
    [workspace] = await initialize_workspaces((project,))
    request = _request(
        "code_index", workspace_id=workspace.workspace_id, patterns=["**/*.ts"]
    )

    class ImportsOnly:
        available = True

        def __init__(self):
            self.current = default_code_indexer_factory()

        def get_supported_extensions(self):
            return self.current.get_supported_extensions()

        def index_source_strict(self, *arguments):
            for entity in self.current.index_source_strict(*arguments):
                yield {**entity, "calls": [], "references": [], "imports": []}

    dependencies = DiscoveryOperationDependencies()
    impact_dependencies = CodeEntityOperationDependencies(operation_secret=b"t" * 32)
    try:
        old = _code_index_sync(
            replace(dependencies, code_indexer_factory=ImportsOnly),
            workspace,
            request,
            threading.Event(),
        )
        current = _code_index_sync(dependencies, workspace, request, threading.Event())
        unchanged = _code_index_sync(
            dependencies, workspace, request, threading.Event()
        )
        assert current.manifest.generation > old.manifest.generation
        assert unchanged.manifest == current.manifest
        with sqlite3.connect(project / ".daem0nmcp/storage/daem0nmcp.db") as connection:
            [entity_id] = connection.execute(
                "SELECT code_entity_id FROM discovery_code_entities WHERE workspace_id=? AND code_generation=? AND qualified_name='fingerprint.sha256utf8'",
                (workspace.workspace_id, current.manifest.generation),
            ).fetchone()
        result = await build_code_entity_operations(impact_dependencies)[
            "code_impact_analyze"
        ](
            workspace=workspace,
            request=_request(
                "code_impact_analyze",
                workspace_id=workspace.workspace_id,
                code_entity_id=entity_id,
                max_depth=2,
            ),
        )
        assert {item.qualified_name for item in result.affected} == {
            "fingerprint.fingerprintHash",
            "fingerprint.fingerprintRoot",
        }
    finally:
        dependencies.close()
        impact_dependencies.close()
