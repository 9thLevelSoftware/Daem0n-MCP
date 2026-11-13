"""Conservative lexical TypeScript relationships from an already parsed tree.

Only declared local runtime symbols can become resolved targets. Import facts
retain their module boundary, but cross-file edges await authenticated export
visibility/alias metadata. Arbitrary receivers and dynamic values are not inferred.
"""

from __future__ import annotations

import posixpath
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Any
from urllib.parse import quote

_FUNCTIONS = frozenset(
    {
        "function_declaration",
        "function_expression",
        "arrow_function",
        "generator_function_declaration",
        "generator_function",
        "method_definition",
    }
)
_ENTITIES = frozenset(
    {
        "function_declaration",
        "class_declaration",
        "method_definition",
        "interface_declaration",
    }
)
_SCOPES = _FUNCTIONS | {
    "program",
    "statement_block",
    "class_declaration",
    "class",
    "catch_clause",
    "for_statement",
    "for_in_statement",
    "switch_body",
    "interface_declaration",
}
_RUNTIME_DECLARATIONS = frozenset({"function_declaration", "class_declaration"})
_UNKNOWN_RUNTIME_DECLARATIONS = frozenset(
    {
        "enum_declaration",
        "internal_module",
        "abstract_class_declaration",
        "function_signature",
        "generator_function_declaration",
        "import_alias",
    }
)


def typescript_target(module: str, symbol: str) -> str | None:
    value = "ts://" + quote(module, safe="/") + "#" + quote(symbol, safe=".")
    return value if len(value) <= 256 else None


@dataclass
class _Scope:
    parent: _Scope | None
    owner: str | None
    function: bool = False
    bindings: dict[str, tuple[str, bool] | None] = field(default_factory=dict)

    def bind(self, name: str, target: tuple[str, bool] | None) -> None:
        if name in self.bindings and self.bindings[name] != target:
            self.bindings[name] = None
        else:
            self.bindings[name] = target

    def lookup(self, name: str) -> tuple[str, bool] | None:
        current: _Scope | None = self
        while current is not None:
            if name in current.bindings:
                return current.bindings[name]
            current = current.parent
        return None


def typescript_relationships(
    tree: Any, source: bytes, relative: str, *, limit: int
) -> tuple[list[dict[str, object]], ...]:
    """Return bounded call/import/reference facts with their exact owning entity."""
    nodes: list[Any] = []
    scope_for: dict[int, _Scope] = {}
    declarations: set[int] = set()
    module = PurePosixPath(relative).stem
    root = _Scope(None, None, True)
    imports: list[dict[str, object]] = []

    def text(node: Any) -> str:
        return source[node.start_byte : node.end_byte].decode("utf-8", errors="strict")

    def qualified(node: Any) -> str:
        parts = []
        current = node
        while current is not None:
            if current.type in _ENTITIES:
                name = current.child_by_field_name("name")
                if name is not None:
                    parts.append(text(name))
            current = current.parent
        return ".".join([module, *reversed(parts)])

    def pattern(node: Any) -> list[Any]:
        found = []
        pending = [node] if node is not None else []
        while pending:
            item = pending.pop()
            if item.type in {"identifier", "shorthand_property_identifier_pattern"}:
                found.append(item)
            elif item.type == "pair_pattern":
                value = item.child_by_field_name("value")
                if value is not None:
                    pending.append(value)
            elif item.type in {
                "required_parameter",
                "optional_parameter",
                "assignment_pattern",
            }:
                value = item.child_by_field_name("pattern") or item.child_by_field_name(
                    "left"
                )
                if value is not None:
                    pending.append(value)
            elif item.type in {
                "object_pattern",
                "array_pattern",
                "rest_pattern",
                "formal_parameters",
            }:
                pending.extend(item.named_children)
        return found

    def shadow(scope: _Scope, node: Any) -> None:
        for name in pattern(node):
            declarations.add(name.id)
            scope.bind(text(name), None)

    def import_bindings(node: Any, scope: _Scope) -> None:
        source_node = node.child_by_field_name("source")
        if source_node is None or text(node).lstrip().startswith("import type "):
            return
        raw = text(source_node)[1:-1]
        target: str | None = None
        if raw.startswith(("./", "../")) and "\\" not in raw:
            candidate = posixpath.normpath(
                posixpath.join(posixpath.dirname(relative), raw)
            )
            if not candidate.startswith(("../", "/")):
                target = candidate
        if target is not None and PurePosixPath(target).suffix in {
            ".ts",
            ".tsx",
            ".js",
            ".jsx",
            ".mjs",
            ".cjs",
        }:
            target = str(PurePosixPath(target).with_suffix(""))
        pending = list(node.named_children)
        while pending:
            item = pending.pop()
            if item.type == "import_specifier":
                for identifier in item.named_children:
                    declarations.add(identifier.id)
                if text(item).lstrip().startswith("type "):
                    continue
                original = item.child_by_field_name("name")
                alias = item.child_by_field_name("alias") or original
                if original is not None and alias is not None:
                    name = (
                        typescript_target(target, text(original))
                        if target is not None
                        else None
                    )
                    if name is not None:
                        scope.bind(text(alias), (name, False))
                        imports.append({"name": name, "line": node.start_point[0] + 1})
                    else:
                        scope.bind(text(alias), None)
            elif item.type == "namespace_import":
                alias = next(
                    (
                        child
                        for child in item.named_children
                        if child.type == "identifier"
                    ),
                    None,
                )
                name = typescript_target(target, "") if target is not None else None
                if alias is not None and name is not None:
                    declarations.add(alias.id)
                    scope.bind(text(alias), (name, True))
                    imports.append(
                        {
                            "name": name,
                            "line": node.start_point[0] + 1,
                            "namespace": True,
                        }
                    )
                elif alias is not None:
                    declarations.add(alias.id)
                    scope.bind(text(alias), None)
            elif item.type == "import_clause":
                # Default imports have no authenticated declaration-name mapping.
                for child in item.named_children:
                    if child.type == "identifier":
                        declarations.add(child.id)
                        scope.bind(text(child), None)
                    else:
                        pending.append(child)
            elif item.type != "string":
                pending.extend(item.named_children)

    pending = [(tree.root_node, root)]
    while pending:
        node, scope = pending.pop()
        nodes.append(node)
        if len(nodes) > 1_000_000:
            raise RuntimeError("TypeScript syntax tree exceeds its bound")
        name = node.child_by_field_name("name")
        if node.type in _RUNTIME_DECLARATIONS and name is not None:
            declarations.add(name.id)
            target = typescript_target(relative, qualified(node)[len(module) + 1 :])
            scope.bind(text(name), (target, False) if target is not None else None)
        elif node.type in _UNKNOWN_RUNTIME_DECLARATIONS and name is not None:
            # These declarations introduce values but their target semantics
            # are not indexed. They must still shadow an outer callable.
            declarations.add(name.id)
            scope.bind(text(name), None)
        elif node.type == "interface_declaration" and name is not None:
            # Interfaces occupy the type namespace, not the runtime namespace.
            declarations.add(name.id)
        if node.type in _SCOPES and node.type != "program":
            owner = qualified(node) if node.type in _ENTITIES else scope.owner
            if node.type in _FUNCTIONS and node.type not in _ENTITIES:
                owner = None  # Anonymous/arrow bodies are not indexed entities.
            if node.type == "class":
                # Unindexed class fields do not execute in the outer owner.
                owner = None
            scope = _Scope(scope, owner, node.type in _FUNCTIONS)
            if node.type == "class" and name is not None:
                declarations.add(name.id)
                scope.bind(text(name), None)
            if node.type in _FUNCTIONS:
                if (
                    node.type in {"function_expression", "generator_function"}
                    and name is not None
                ):
                    # A named expression's self binding exists only inside its
                    # function; its body is not a separately indexed entity.
                    declarations.add(name.id)
                    scope.bind(text(name), None)
                shadow(scope, node.child_by_field_name("parameters"))
                shadow(scope, node.child_by_field_name("parameter"))
            if node.type == "catch_clause":
                shadow(scope, node.child_by_field_name("parameter"))
        scope_for[node.id] = scope
        if node.type == "variable_declarator":
            destination = scope
            if node.parent is not None and node.parent.type == "variable_declaration":
                while destination.parent is not None and not destination.function:
                    destination = destination.parent
            shadow(destination, name)
        elif node.type == "import_statement":
            import_bindings(node, scope)
        pending.extend((child, scope) for child in reversed(node.named_children))

    # Reassignments invalidate a lexical binding; do not infer runtime values.
    for node in nodes:
        if node.type not in {
            "assignment_expression",
            "augmented_assignment_expression",
            "update_expression",
        }:
            continue
        left = node.child_by_field_name("left") or node.child_by_field_name("argument")
        for identifier in pattern(left):
            declarations.add(identifier.id)
            scope = scope_for[node.id]
            while text(identifier) not in scope.bindings and scope.parent is not None:
                scope = scope.parent
            scope.bind(text(identifier), None)

    def resolve(node: Any, scope: _Scope) -> str | None:
        if node is None:
            return None
        if node.type == "identifier":
            binding = scope.lookup(text(node))
            return binding[0] if binding is not None and not binding[1] else None
        if node.type == "member_expression":
            receiver = node.child_by_field_name("object")
            member = node.child_by_field_name("property")
            if (
                receiver is not None
                and receiver.type == "identifier"
                and member is not None
                and member.type == "property_identifier"
            ):
                binding = scope.lookup(text(receiver))
                if binding is not None and binding[1]:
                    target = binding[0] + quote(text(member), safe=".")
                    return target if len(target) <= 256 else None
        return None

    calls: list[dict[str, object]] = []
    references: list[dict[str, object]] = []
    for node in nodes:
        scope = scope_for[node.id]
        if scope.owner is None:
            continue
        name = None
        relationship_output = references
        if node.type in {"call_expression", "new_expression"}:
            name = resolve(
                node.child_by_field_name("function")
                or node.child_by_field_name("constructor"),
                scope,
            )
            relationship_output = calls
        elif node.type == "identifier" and node.id not in declarations:
            parent = node.parent
            if parent is not None and (
                parent.type
                in {
                    "labeled_statement",
                    "break_statement",
                    "continue_statement",
                    "export_specifier",
                }
                or parent.type.startswith("jsx_")
                or parent.child_by_field_name("key") == node
            ):
                continue
            name = resolve(node, scope)
        elif node.type == "member_expression":
            name = resolve(node, scope)
        if name is not None:
            relationship_output.append(
                {"name": name, "line": node.start_point[0] + 1, "owner": scope.owner}
            )
            if len(calls) + len(imports) + len(references) > limit:
                raise RuntimeError("TypeScript relationships exceed their bound")
    return calls, imports, references
