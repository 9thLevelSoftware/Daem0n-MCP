"""Repository binding capture and validity semantics."""

from __future__ import annotations

import hashlib
import os
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from types import SimpleNamespace

import pytest

from daem0nmcp import code_bindings, code_indexer
from daem0nmcp.api.v7 import discovery_operations
from daem0nmcp.code_bindings import (
    BindingEvaluator,
    CodeBindingError,
    CodeBindingReferenceError,
    CodeBindingUnavailableError,
    binding_refs_from_context,
    capture_bindings,
)
from daem0nmcp.workspace import WorkspaceRegistry


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "daem0nmcp.api.v7.resource_repository.read_git_output_sync", lambda *_: None
    )
    return WorkspaceRegistry([tmp_path], default_root=tmp_path).default


def context_for(workspace, refs):
    return {
        "code_bindings": [
            binding.to_json() for binding in capture_bindings(workspace, refs)
        ]
    }


def edit(path, text):
    previous = path.stat().st_mtime_ns
    path.write_text(text, encoding="utf-8")
    os.utime(path, ns=(previous + 1_000_000, previous + 1_000_000))


def require_parser():
    if not discovery_operations.default_code_indexer_factory().available:
        pytest.skip("tree-sitter parser unavailable")


def test_file_binding_detects_edit_and_deletion(workspace):
    path = workspace.root / "notes.md"
    path.write_bytes(b"original\r\n")
    context = context_for(workspace, [("notes.md", None)])
    assert (
        context["code_bindings"][0]["fingerprint"]
        == hashlib.sha256(b"original\n").hexdigest()
    )
    assert binding_refs_from_context(context) == (("notes.md", None),)
    evaluator = BindingEvaluator()
    assert evaluator.evaluate(workspace.root, context).applicability == "current"
    edit(path, "original\n")
    assert evaluator.evaluate(workspace.root, context).applicability == "current"
    edit(path, "modified\n")
    result = evaluator.evaluate(workspace.root, context)
    assert result.applicability == "needs_revalidation"
    assert result.changed == ("notes.md",)
    path.unlink()
    assert evaluator.evaluate(workspace.root, context).changed == ("notes.md",)


@pytest.mark.parametrize("qualified_name", [None, "settings.target"])
def test_binding_detects_content_changes_with_preserved_metadata(
    workspace, qualified_name
):
    if qualified_name is not None:
        require_parser()
    path = workspace.root / "settings.py"
    original = b"def target():\n    return 1\n"
    path.write_bytes(original)
    context = context_for(workspace, [("settings.py", qualified_name)])
    evaluator = BindingEvaluator()
    assert evaluator.evaluate(workspace.root, context).applicability == "current"
    metadata = path.stat()
    path.write_bytes(original.replace(b"1", b"9"))
    os.utime(path, ns=(metadata.st_atime_ns, metadata.st_mtime_ns))
    assert path.stat().st_size == metadata.st_size
    assert path.stat().st_mtime_ns == metadata.st_mtime_ns
    result = evaluator.evaluate(workspace.root, context)
    assert result.applicability == "needs_revalidation"
    label = "settings.py"
    if qualified_name is not None:
        label += "::" + qualified_name
    assert result.changed == (label,)
    path.write_bytes(original)
    os.utime(path, ns=(metadata.st_atime_ns, metadata.st_mtime_ns))
    assert evaluator.evaluate(workspace.root, context).applicability == "current"


def test_symbol_binding_distinguishes_sibling_and_bound_edit(workspace):
    require_parser()
    path = workspace.root / "sample.py"
    original = (
        "def target(value):\n    return value + 1\n\n"
        "def sibling(value):\n    return value * 2\n"
    )
    path.write_text(original, encoding="utf-8")
    context = context_for(workspace, [("sample.py", "target")])
    evaluator = BindingEvaluator()
    assert evaluator.evaluate(workspace.root, context).applicability == "current"
    edit(path, original.replace("* 2", "* 3"))
    assert evaluator.evaluate(workspace.root, context).applicability == "current"
    edit(path, original.replace("+ 1", "+ 2"))
    result = evaluator.evaluate(workspace.root, context)
    assert result.applicability == "needs_revalidation"
    assert result.changed == ("sample.py::target",)
    edit(path, "def sibling(value):\n    return value * 2\n")
    assert evaluator.evaluate(workspace.root, context).changed == ("sample.py::target",)


def test_ambiguous_alias_is_rejected_and_qualified_binding_is_isolated(workspace):
    require_parser()
    path = workspace.root / "dup.py"
    original = (
        "class A:\n    def target(self):\n        return 1\n\n"
        "class B:\n    def target(self):\n        return 2\n"
    )
    path.write_text(original, encoding="utf-8")
    with pytest.raises(CodeBindingReferenceError, match="ambiguous"):
        capture_bindings(workspace, [("dup.py", "target")])
    context = context_for(workspace, [("dup.py", "dup.A.target")])
    evaluator = BindingEvaluator()
    edit(path, original.replace("return 2", "return 3"))
    assert evaluator.evaluate(workspace.root, context).applicability == "current"
    edit(path, original.replace("return 1", "return 4"))
    result = evaluator.evaluate(workspace.root, context)
    assert result.applicability == "needs_revalidation"
    assert result.changed == ("dup.py::dup.A.target",)


def test_unique_alias_becoming_ambiguous_is_unverifiable(workspace):
    require_parser()
    path = workspace.root / "dup.py"
    original = "class A:\n    def target(self):\n        return 1\n"
    path.write_text(original, encoding="utf-8")
    context = context_for(workspace, [("dup.py", "target")])
    assert (
        BindingEvaluator().evaluate(workspace.root, context).applicability == "current"
    )
    edit(path, original + "\nclass B:\n    def target(self):\n        return 2\n")
    result = BindingEvaluator().evaluate(workspace.root, context)
    assert result.applicability == "unverifiable"
    assert result.changed == ()


@pytest.mark.parametrize("name", ["target", "sample.target"])
def test_same_identity_overload_spans_remain_grouped(workspace, name):
    require_parser()
    path = workspace.root / "sample.py"
    original = (
        "from typing import overload\n\n"
        "@overload\ndef target(value: int) -> int: ...\n\n"
        "@overload\ndef target(value: str) -> str: ...\n\n"
        "def target(value):\n    return value\n"
    )
    path.write_text(original, encoding="utf-8")
    context = context_for(workspace, [("sample.py", name)])
    evaluator = BindingEvaluator()
    assert evaluator.evaluate(workspace.root, context).applicability == "current"
    edit(path, original.replace("value: int", "value: float"))
    result = evaluator.evaluate(workspace.root, context)
    assert result.applicability == "needs_revalidation"
    assert result.changed == (f"sample.py::{name}",)


def test_exact_identity_takes_precedence_over_short_name_match(workspace):
    require_parser()
    path = workspace.root / "__init__.py"
    original = (
        "def target():\n    return 1\n\n"
        "class A:\n    def target(self):\n        return 2\n"
    )
    path.write_text(original, encoding="utf-8")
    context = context_for(workspace, [("__init__.py", "target")])
    evaluator = BindingEvaluator()
    edit(path, original.replace("return 2", "return 3"))
    assert evaluator.evaluate(workspace.root, context).applicability == "current"
    edit(path, original.replace("return 1", "return 4"))
    assert (
        evaluator.evaluate(workspace.root, context).applicability
        == "needs_revalidation"
    )


@pytest.mark.parametrize("nested_first", [False, True])
def test_symbol_snapshots_isolate_nested_workspace_identities(workspace, nested_first):
    require_parser()
    nested_root = workspace.root / "sub"
    nested_root.mkdir()
    nested = WorkspaceRegistry([nested_root], default_root=nested_root).default
    path = nested_root / "__init__.py"
    original = (
        "@route('original')\ndef handler():\n    return 1\n\n"
        "def sibling():\n    return 2\n"
    )
    path.write_text(original, encoding="utf-8")
    parent_bound = context_for(workspace, [("sub/__init__.py", "sub.handler")])
    nested_bound = context_for(nested, [("__init__.py", "handler")])
    parent_sibling = context_for(workspace, [("sub/__init__.py", "sub.sibling")])
    nested_sibling = context_for(nested, [("__init__.py", "sibling")])
    assert (
        parent_bound["code_bindings"][0]["fingerprint"]
        == nested_bound["code_bindings"][0]["fingerprint"]
    )
    evaluations = [
        (workspace.root, parent_bound, parent_sibling),
        (nested.root, nested_bound, nested_sibling),
    ]
    if nested_first:
        evaluations.reverse()
    evaluator = BindingEvaluator()
    with evaluator.read_batch():
        for root, bound, sibling in evaluations:
            assert evaluator.evaluate(root, bound).applicability == "current"
            assert evaluator.evaluate(root, sibling).applicability == "current"
        wrong_root_identity = {
            "code_bindings": [
                {
                    **parent_bound["code_bindings"][0],
                    "relative_file_path": "__init__.py",
                }
            ]
        }
        result = evaluator.evaluate(nested.root, wrong_root_identity)
        assert result.applicability == "needs_revalidation"
        assert result.changed == ("__init__.py::sub.handler",)
        for root, bound, sibling in evaluations:
            assert evaluator.evaluate(root, bound).applicability == "current"
            assert evaluator.evaluate(root, sibling).applicability == "current"
    edit(path, original.replace("return 2", "return 3"))
    with evaluator.read_batch():
        for root, bound, sibling in evaluations:
            assert evaluator.evaluate(root, bound).applicability == "current"
            result = evaluator.evaluate(root, sibling)
            assert result.applicability == "needs_revalidation"
            binding = sibling["code_bindings"][0]
            assert result.changed == (
                f"{binding['relative_file_path']}::{binding['qualified_name']}",
            )
    edit(path, original.replace("@route('original')", "@route('changed')"))
    with evaluator.read_batch():
        for root, bound, sibling in evaluations:
            result = evaluator.evaluate(root, bound)
            assert result.applicability == "needs_revalidation"
            binding = bound["code_bindings"][0]
            assert result.changed == (
                f"{binding['relative_file_path']}::{binding['qualified_name']}",
            )
            assert evaluator.evaluate(root, sibling).applicability == "current"
    edit(path, original)
    with evaluator.read_batch():
        for root, bound, sibling in evaluations:
            assert evaluator.evaluate(root, bound).applicability == "current"
            assert evaluator.evaluate(root, sibling).applicability == "current"


@pytest.mark.parametrize("identity", [None, "", "   ", 12])
def test_alias_without_usable_parser_identity_is_unverifiable(
    workspace, monkeypatch, identity
):
    require_parser()
    path = workspace.root / "sample.py"
    path.write_text("def target():\n    return 1\n", encoding="utf-8")
    context = context_for(workspace, [("sample.py", "target")])
    producer = discovery_operations.default_code_indexer_factory()

    class MissingIdentityProducer:
        available = True

        def get_supported_extensions(self):
            return producer.get_supported_extensions()

        def index_source_strict(self, *args):
            return [
                {**entity, "qualified_name": identity}
                for entity in producer.index_source_strict(*args)
            ]

    monkeypatch.setattr(
        discovery_operations, "default_code_indexer_factory", MissingIdentityProducer
    )
    with pytest.raises(CodeBindingUnavailableError, match="usable symbol identity"):
        capture_bindings(workspace, [("sample.py", "target")])
    result = BindingEvaluator().evaluate(workspace.root, context)
    assert result.applicability == "unverifiable"
    assert result.changed == ()


@pytest.mark.parametrize("fallback", [False, True])
@pytest.mark.parametrize("short_name", [False, True])
@pytest.mark.parametrize("kind", ["method", "function", "class"])
@pytest.mark.parametrize("mutation", ["add", "change", "remove", "multiline"])
def test_python_decorator_changes_revalidate_binding(
    workspace, monkeypatch, fallback, short_name, kind, mutation
):
    require_parser()
    if fallback:
        monkeypatch.delitem(code_indexer.ENTITY_QUERIES, "python")
    path = workspace.root / "sample.py"
    if kind == "method":
        prefix, indent = "class Service:\n", "    "
        definition = "def target(value):\n        return value\n"
        qualified_name = "sample.Service.target"
    elif kind == "function":
        prefix, indent = "", ""
        definition = "def target(value):\n    return value\n"
        qualified_name = "sample.target"
    else:
        prefix, indent = "", ""
        definition = "class target:\n    value = 1\n"
        qualified_name = "sample.target"
    before = "" if mutation == "add" else f"{indent}@route('old')\n"
    if mutation == "multiline":
        before = f"{indent}@route(\n{indent}    'old',\n{indent})\n"
    after = {
        "add": f"{indent}@route('new')\n",
        "change": f"{indent}@route('new')\n",
        "remove": "",
        "multiline": f"{indent}@route(\n{indent}    'new',\n{indent})\n",
    }[mutation]
    original = prefix + before + indent + definition
    path.write_text(original, encoding="utf-8")
    name = "target" if short_name else qualified_name
    context = context_for(workspace, [("sample.py", name)])
    evaluator = BindingEvaluator()
    assert evaluator.evaluate(workspace.root, context).applicability == "current"
    edit(path, prefix + after + indent + definition)
    result = evaluator.evaluate(workspace.root, context)
    assert result.applicability == "needs_revalidation"
    assert result.changed == (f"sample.py::{name}",)


@pytest.mark.parametrize("fallback", [False, True])
def test_python_sibling_decorator_does_not_revalidate_binding(
    workspace, monkeypatch, fallback
):
    require_parser()
    if fallback:
        monkeypatch.delitem(code_indexer.ENTITY_QUERIES, "python")
    path = workspace.root / "sample.py"
    original = (
        "class Service:\n"
        "    @staticmethod\n"
        "    def target(value):\n"
        "        return value\n"
        "    @route('old')\n"
        "    def sibling(value):\n"
        "        return value\n"
    )
    path.write_text(original, encoding="utf-8")
    context = context_for(workspace, [("sample.py", "sample.Service.target")])
    evaluator = BindingEvaluator()
    edit(path, original.replace("@route('old')", "@route('new')"))
    assert evaluator.evaluate(workspace.root, context).applicability == "current"
    edit(path, original.replace("@staticmethod", "@classmethod"))
    assert (
        evaluator.evaluate(workspace.root, context).applicability
        == "needs_revalidation"
    )


@pytest.mark.parametrize("fallback", [False, True])
def test_typescript_decorator_owns_intervening_comment(
    workspace, monkeypatch, fallback
):
    require_parser()
    if fallback:
        monkeypatch.delitem(code_indexer.ENTITY_QUERIES, "typescript")
    path = workspace.root / "sample.ts"
    original = (
        "class Service {\n"
        "  @route('old')\n"
        "  // attached comment\n"
        "  target() { return 1; }\n"
        "  // ordinary comment containing @route('old')\n"
        "  sibling() { return 2; }\n"
        "}\n"
    )
    path.write_text(original, encoding="utf-8")
    bound = context_for(workspace, [("sample.ts", "sample.Service.target")])
    ordinary = context_for(workspace, [("sample.ts", "sample.Service.sibling")])
    evaluator = BindingEvaluator()
    edit(path, original.replace("  @route('old')", "  @route('new')"))
    assert (
        evaluator.evaluate(workspace.root, bound).applicability == "needs_revalidation"
    )
    assert evaluator.evaluate(workspace.root, ordinary).applicability == "current"
    edit(path, original.replace("attached comment", "changed attached comment"))
    assert (
        evaluator.evaluate(workspace.root, bound).applicability == "needs_revalidation"
    )
    edit(path, original.replace("ordinary comment", "changed ordinary comment"))
    assert evaluator.evaluate(workspace.root, bound).applicability == "current"
    assert evaluator.evaluate(workspace.root, ordinary).applicability == "current"


@pytest.mark.parametrize("extension", ["py", "ts"])
def test_query_and_fallback_bindings_are_interchangeable(
    workspace, monkeypatch, extension
):
    require_parser()
    path = workspace.root / f"sample.{extension}"
    if extension == "py":
        language = "python"
        original = (
            "class Service:\n"
            "    @route(\n"
            "        'old',\n"
            "    )\n"
            "    def target(self):\n"
            "        return 1\n"
        )
    else:
        language = "typescript"
        original = (
            "class Service {\n"
            "  @route('old')\n"
            "  // attached comment\n"
            "  target() { return 1; }\n"
            "}\n"
        )
    path.write_text(original, encoding="utf-8")
    refs = [(path.name, "sample.Service.target")]
    query_context = context_for(workspace, refs)
    monkeypatch.delitem(code_indexer.ENTITY_QUERIES, language)
    fallback_context = context_for(workspace, refs)
    assert fallback_context == query_context
    evaluator = BindingEvaluator()
    assert evaluator.evaluate(workspace.root, query_context).applicability == "current"
    edit(path, original.replace("'old'", "'new'"))
    for context in (query_context, fallback_context):
        assert (
            evaluator.evaluate(workspace.root, context).applicability
            == "needs_revalidation"
        )


@pytest.mark.parametrize(
    "bounds",
    [
        {"binding_line_start": None},
        {"binding_line_start": True},
        {"binding_line_start": "1"},
        {"binding_line_start": 1.0},
        {"binding_line_start": 0},
        {"binding_line_start": 3},
        {"line_start": True},
        {"line_end": True},
        {"line_end": 100},
    ],
)
def test_invalid_binding_span_is_unverifiable(workspace, monkeypatch, bounds):
    require_parser()
    path = workspace.root / "sample.py"
    path.write_text("@route('old')\ndef target():\n    return 1\n", encoding="utf-8")
    context = context_for(workspace, [("sample.py", "sample.target")])
    producer = discovery_operations.default_code_indexer_factory()

    class InvalidSpanProducer:
        available = True

        def get_supported_extensions(self):
            return producer.get_supported_extensions()

        def index_source_strict(self, *args):
            return [
                {**entity, **bounds} for entity in producer.index_source_strict(*args)
            ]

    monkeypatch.setattr(
        discovery_operations, "default_code_indexer_factory", InvalidSpanProducer
    )
    result = BindingEvaluator().evaluate(workspace.root, context)
    assert result.applicability == "unverifiable"
    assert result.changed == ()
    with pytest.raises(CodeBindingError):
        capture_bindings(workspace, [("sample.py", "sample.target")])


def test_legacy_decorated_binding_requires_revalidation(workspace, monkeypatch):
    require_parser()
    path = workspace.root / "sample.py"
    path.write_text("@route('old')\ndef target():\n    return 1\n", encoding="utf-8")
    producer = discovery_operations.default_code_indexer_factory()

    class NavigationSpanProducer:
        available = True

        def get_supported_extensions(self):
            return producer.get_supported_extensions()

        def index_source_strict(self, *args):
            return [
                {
                    key: value
                    for key, value in entity.items()
                    if key != "binding_line_start"
                }
                for entity in producer.index_source_strict(*args)
            ]

    with monkeypatch.context() as patch:
        patch.setattr(
            discovery_operations, "default_code_indexer_factory", NavigationSpanProducer
        )
        context = context_for(workspace, [("sample.py", "sample.target")])
        assert (
            BindingEvaluator().evaluate(workspace.root, context).applicability
            == "current"
        )
    assert (
        BindingEvaluator().evaluate(workspace.root, context).applicability
        == "needs_revalidation"
    )


def test_capture_rejects_workspace_escape(workspace):
    with pytest.raises(CodeBindingError):
        capture_bindings(workspace, [("../outside.py", None)])


def test_capture_rejects_oversized_file(workspace):
    path = workspace.root / "large.txt"
    with path.open("wb") as output:
        output.truncate(5 * 1024 * 1024 + 1)
    with pytest.raises(CodeBindingReferenceError, match="size limit"):
        capture_bindings(workspace, [("large.txt", None)])


def test_oversized_newlines_do_not_prove_normalized_content_changed(
    workspace, monkeypatch
):
    monkeypatch.setattr(code_bindings, "_MAX_BINDING_FILE_BYTES", 8)
    path = workspace.root / "notes.txt"
    original = b"x\n" * 4
    path.write_bytes(original)
    context = context_for(workspace, [("notes.txt", None)])
    evaluator = BindingEvaluator()
    assert evaluator.evaluate(workspace.root, context).applicability == "current"
    expanded = original.replace(b"\n", b"\r\n")
    assert (
        hashlib.sha256(expanded.replace(b"\r\n", b"\n")).hexdigest()
        == (context["code_bindings"][0]["fingerprint"])
    )
    path.write_bytes(expanded)
    result = evaluator.evaluate(workspace.root, context)
    assert result.applicability == "unverifiable"
    assert result.changed == ()
    with pytest.raises(CodeBindingReferenceError, match="size limit"):
        capture_bindings(workspace, [("notes.txt", None)])
    path.write_bytes(original)
    assert evaluator.evaluate(workspace.root, context).applicability == "current"


@pytest.mark.parametrize("operation", ["resolve", "stat", "open"])
@pytest.mark.parametrize("error_type", [PermissionError, OSError])
def test_environmental_file_fault_is_unverifiable_and_restores_current(
    workspace, monkeypatch, operation, error_type
):
    path = workspace.root / "notes.txt"
    path.write_bytes(b"unchanged")
    changed = workspace.root / "changed.txt"
    changed.write_bytes(b"before")
    context = context_for(workspace, [("notes.txt", None)])
    mixed = context_for(workspace, [("notes.txt", None), ("changed.txt", None)])
    changed.write_bytes(b"after")
    evaluator = BindingEvaluator()
    assert evaluator.evaluate(workspace.root, context).applicability == "current"
    original = getattr(type(path), operation)

    def refuse(self, *args, **kwargs):
        if self == path:
            raise error_type("temporary filesystem fault")
        return original(self, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(type(path), operation, refuse)
        result = evaluator.evaluate(workspace.root, context)
        assert result.applicability == "unverifiable"
        assert result.changed == ()
        result = evaluator.evaluate(workspace.root, mixed)
        assert result.applicability == "needs_revalidation"
        assert result.changed == ("changed.txt",)
        if operation != "stat":
            with pytest.raises(CodeBindingUnavailableError):
                capture_bindings(workspace, [("notes.txt", None)])
        else:
            assert capture_bindings(workspace, [("notes.txt", None)])
    assert evaluator.evaluate(workspace.root, context).applicability == "current"


@pytest.mark.parametrize("operation", ["resolve", "stat"])
def test_batch_memo_does_not_hide_environmental_fault(
    workspace, monkeypatch, operation
):
    path = workspace.root / "notes.txt"
    path.write_bytes(b"notes")
    context = context_for(workspace, [("notes.txt", None)])
    evaluator = BindingEvaluator()
    original = getattr(type(path), operation)

    def refuse(self, *args, **kwargs):
        if self == path:
            raise PermissionError("temporary filesystem fault")
        return original(self, *args, **kwargs)

    with evaluator.read_batch(budget_bytes=5):
        assert evaluator.evaluate(workspace.root, context).applicability == "current"
        with monkeypatch.context() as patch:
            patch.setattr(type(path), operation, refuse)
            result = evaluator.evaluate(workspace.root, context)
            assert result.applicability == "unverifiable"
            assert result.changed == ()
        assert evaluator.evaluate(workspace.root, context).applicability == "current"


@pytest.mark.parametrize(
    "error_type", [FileNotFoundError, NotADirectoryError, IsADirectoryError]
)
@pytest.mark.parametrize("operation", ["resolve", "stat", "open"])
def test_definite_path_fault_remains_changed(
    workspace, monkeypatch, error_type, operation
):
    path = workspace.root / "notes.txt"
    path.write_bytes(b"notes")
    context = context_for(workspace, [("notes.txt", None)])
    original = getattr(type(path), operation)

    def refuse(self, *args, **kwargs):
        if self == path:
            raise error_type("definite path fault")
        return original(self, *args, **kwargs)

    monkeypatch.setattr(type(path), operation, refuse)
    result = BindingEvaluator().evaluate(workspace.root, context)
    assert result.applicability == "needs_revalidation"
    assert result.changed == ("notes.txt",)
    if operation != "stat":
        with pytest.raises(CodeBindingError) as caught:
            capture_bindings(workspace, [("notes.txt", None)])
        assert type(caught.value) is CodeBindingError
    else:
        assert capture_bindings(workspace, [("notes.txt", None)])


@pytest.mark.parametrize("mutation", ["deleted", "directory", "escape"])
def test_definite_filesystem_mutation_remains_changed(
    workspace, tmp_path_factory, mutation
):
    path = workspace.root / "notes.txt"
    path.write_bytes(b"notes")
    context = context_for(workspace, [("notes.txt", None)])
    path.unlink()
    if mutation == "directory":
        path.mkdir()
    elif mutation == "escape":
        outside = tmp_path_factory.mktemp("outside-binding") / "notes.txt"
        outside.write_bytes(b"notes")
        try:
            path.symlink_to(outside)
        except OSError:
            pytest.skip("symlink creation unavailable")
    result = BindingEvaluator().evaluate(workspace.root, context)
    assert result.applicability == "needs_revalidation"
    assert result.changed == ("notes.txt",)
    with pytest.raises(CodeBindingError) as caught:
        capture_bindings(workspace, [("notes.txt", None)])
    assert type(caught.value) is CodeBindingError


def test_unavailable_parser_is_unverifiable(workspace, monkeypatch):
    require_parser()
    path = workspace.root / "sample.py"
    path.write_text(
        "def target():\n    return 1\n\ndef sibling():\n    return 2\n",
        encoding="utf-8",
    )
    context = context_for(workspace, [("sample.py", "sample.target")])
    sibling = context_for(workspace, [("sample.py", "sample.sibling")])
    evaluator = BindingEvaluator()
    assert evaluator.evaluate(workspace.root, context).applicability == "current"
    monkeypatch.setattr(
        discovery_operations,
        "default_code_indexer_factory",
        lambda: SimpleNamespace(available=False),
    )
    for bound in (context, sibling):
        result = evaluator.evaluate(workspace.root, bound)
        assert result.applicability == "unverifiable"
        assert result.changed == ()
    with pytest.raises(CodeBindingError):
        capture_bindings(workspace, [("sample.py", "sample.target")])


def test_parse_failure_is_unverifiable(workspace):
    require_parser()
    path = workspace.root / "sample.py"
    path.write_text("def target():\n    return 1\n", encoding="utf-8")
    context = context_for(workspace, [("sample.py", "sample.target")])
    edit(path, "def target(:\n")
    assert (
        BindingEvaluator().evaluate(workspace.root, context).applicability
        == "unverifiable"
    )


@pytest.mark.parametrize(
    "raw", [None, {}, [], [None], [{"relative_file_path": "sample.py"}]]
)
def test_malformed_binding_context_is_rejected(workspace, raw):
    context = {"code_bindings": raw}
    with pytest.raises(CodeBindingError):
        binding_refs_from_context(context)
    with pytest.raises(CodeBindingError):
        BindingEvaluator().evaluate(workspace.root, context)


def test_missing_file_takes_precedence_over_unverifiable(workspace, monkeypatch):
    require_parser()
    (workspace.root / "sample.py").write_text(
        "def target():\n    return 1\n", encoding="utf-8"
    )
    (workspace.root / "notes.md").write_text("notes", encoding="utf-8")
    context = context_for(
        workspace, [("sample.py", "sample.target"), ("notes.md", None)]
    )
    (workspace.root / "notes.md").unlink()
    monkeypatch.setattr(
        discovery_operations,
        "default_code_indexer_factory",
        lambda: SimpleNamespace(available=False),
    )
    result = BindingEvaluator().evaluate(workspace.root, context)
    assert result.applicability == "needs_revalidation"
    assert result.changed == ("notes.md",)


def test_git_head_metadata(workspace, monkeypatch):
    path = workspace.root / "notes.md"
    path.write_text("notes", encoding="utf-8")
    head = "a" * 40
    monkeypatch.setattr(
        "daem0nmcp.api.v7.resource_repository.read_git_output_sync",
        lambda *_: head.encode() + b"\n",
    )
    assert capture_bindings(workspace, [("notes.md", None)])[0].head_commit == head
    monkeypatch.setattr(
        "daem0nmcp.api.v7.resource_repository.read_git_output_sync",
        lambda *_: b"invalid",
    )
    assert capture_bindings(workspace, [("notes.md", None)])[0].head_commit is None


def test_no_bindings_returns_none(workspace):
    assert BindingEvaluator().evaluate(workspace.root, {}) is None


@pytest.mark.parametrize("qualified_name", [None, "settings.target"])
def test_read_batch_refreshes_content_after_preserved_metadata_edit(
    workspace, qualified_name
):
    if qualified_name is not None:
        require_parser()
    path = workspace.root / "settings.py"
    original = b"def target():\n    return 1\n"
    path.write_bytes(original)
    context = context_for(workspace, [("settings.py", qualified_name)])
    evaluator = BindingEvaluator()
    with evaluator.read_batch():
        assert evaluator.evaluate(workspace.root, context).applicability == "current"
        metadata = path.stat()
        path.write_bytes(original.replace(b"1", b"9"))
        os.utime(path, ns=(metadata.st_atime_ns, metadata.st_mtime_ns))
        assert evaluator.evaluate(workspace.root, context).applicability == "current"
    label = "settings.py"
    if qualified_name is not None:
        label += "::" + qualified_name
    result = evaluator.evaluate(workspace.root, context)
    assert result.applicability == "needs_revalidation"
    assert result.changed == (label,)
    with evaluator.read_batch():
        assert (
            evaluator.evaluate(workspace.root, context).applicability
            == "needs_revalidation"
        )


@pytest.mark.parametrize("mutation", ["missing", "oversized"])
def test_read_batch_rechecks_file_constraints_before_memo(workspace, mutation):
    path = workspace.root / "notes.md"
    path.write_bytes(b"notes")
    context = context_for(workspace, [("notes.md", None)])
    evaluator = BindingEvaluator()
    with evaluator.read_batch(budget_bytes=5):
        assert evaluator.evaluate(workspace.root, context).applicability == "current"
        if mutation == "missing":
            path.unlink()
        else:
            with path.open("wb") as output:
                output.truncate(5 * 1024 * 1024 + 1)
        result = evaluator.evaluate(workspace.root, context)
        if mutation == "missing":
            assert result.applicability == "needs_revalidation"
            assert result.changed == ("notes.md",)
        else:
            assert result.applicability == "unverifiable"
            assert result.changed == ()


def test_zero_read_budget_preserves_missing_binding_precedence(workspace):
    (workspace.root / "notes.md").write_bytes(b"notes")
    (workspace.root / "gone.md").write_bytes(b"gone")
    context = context_for(workspace, [("notes.md", None)])
    mixed = context_for(workspace, [("notes.md", None), ("gone.md", None)])
    (workspace.root / "gone.md").unlink()
    evaluator = BindingEvaluator()
    with evaluator.read_batch(budget_bytes=0):
        result = evaluator.evaluate(workspace.root, context)
        assert result.applicability == "unverifiable"
        assert result.changed == ()
        result = evaluator.evaluate(workspace.root, mixed)
        assert result.applicability == "needs_revalidation"
        assert result.changed == ("gone.md",)


def test_exact_raw_byte_budget_allows_eof_and_free_memo_hits(workspace):
    source = b"notes\r\n"
    (workspace.root / "notes.md").write_bytes(source)
    (workspace.root / "small.md").write_bytes(b"x")
    (workspace.root / "empty.md").write_bytes(b"")
    context = context_for(workspace, [("notes.md", None)])
    small = context_for(workspace, [("small.md", None)])
    empty = context_for(workspace, [("empty.md", None)])
    evaluator = BindingEvaluator()
    with evaluator.read_batch(budget_bytes=len(source)):
        assert evaluator.evaluate(workspace.root, context).applicability == "current"
        assert evaluator.evaluate(workspace.root, context).applicability == "current"
        for refused in (small, empty):
            result = evaluator.evaluate(workspace.root, refused)
            assert result.applicability == "unverifiable"
            assert result.changed == ()
    with evaluator.read_batch(budget_bytes=1):
        assert evaluator.evaluate(workspace.root, empty).applicability == "current"
        assert evaluator.evaluate(workspace.root, small).applicability == "current"
        assert evaluator.evaluate(workspace.root, empty).applicability == "current"


def test_stat_budget_refusal_leaves_room_for_later_small_files(workspace):
    (workspace.root / "large.md").write_bytes(b"too large")
    (workspace.root / "small.md").write_bytes(b"ok")
    large = context_for(workspace, [("large.md", None)])
    small = context_for(workspace, [("small.md", None)])
    evaluator = BindingEvaluator()
    with evaluator.read_batch(budget_bytes=2):
        result = evaluator.evaluate(workspace.root, large)
        assert result.applicability == "unverifiable"
        assert result.changed == ()
        assert evaluator.evaluate(workspace.root, small).applicability == "current"


@pytest.mark.parametrize("grow_past_file_limit", [False, True])
def test_file_growth_during_read_never_verifies_partial_source(
    workspace, monkeypatch, grow_past_file_limit
):
    path = workspace.root / "notes.md"
    path.write_bytes(b"abc")
    (workspace.root / "small.md").write_bytes(b"x")
    context = context_for(workspace, [("notes.md", None)])
    small = context_for(workspace, [("small.md", None)])
    budget = 5 * 1024 * 1024 if grow_past_file_limit else 3
    original_open = type(path).open
    grown = False

    def grow_before_read(self, mode="r", *args, **kwargs):
        nonlocal grown
        if self == path and mode == "rb" and not grown:
            grown = True
            self.write_bytes(b"abc" + b"x" * (budget - 2))
        return original_open(self, mode, *args, **kwargs)

    monkeypatch.setattr(type(path), "open", grow_before_read)
    evaluator = BindingEvaluator()
    with evaluator.read_batch(budget_bytes=budget):
        result = evaluator.evaluate(workspace.root, context)
        assert result.applicability == "unverifiable"
        assert result.changed == ()
        assert evaluator.evaluate(workspace.root, small).applicability == "unverifiable"
    result = evaluator.evaluate(workspace.root, context)
    if grow_past_file_limit:
        assert result.applicability == "unverifiable"
        assert result.changed == ()
    else:
        assert result.applicability == "needs_revalidation"
        assert result.changed == ("notes.md",)
    with evaluator.read_batch(budget_bytes=1):
        assert evaluator.evaluate(workspace.root, small).applicability == "current"


@pytest.mark.parametrize("budget", [-1, True, False, 1.5, "1", None])
def test_read_batch_rejects_invalid_budgets(workspace, budget):
    evaluator = BindingEvaluator()
    with (
        pytest.raises(ValueError, match="nonnegative integer"),
        evaluator.read_batch(budget_bytes=budget),
    ):
        pass


def test_read_batch_rejects_nesting_and_resets_after_exception(workspace):
    path = workspace.root / "notes.md"
    path.write_bytes(b"old")
    context = context_for(workspace, [("notes.md", None)])
    evaluator = BindingEvaluator()
    with (
        pytest.raises(RuntimeError, match="abort batch"),
        evaluator.read_batch(budget_bytes=3),
    ):
        assert evaluator.evaluate(workspace.root, context).applicability == "current"
        with (
            pytest.raises(RuntimeError, match="already active"),
            evaluator.read_batch(),
        ):
            pass
        path.write_bytes(b"new")
        assert evaluator.evaluate(workspace.root, context).applicability == "current"
        raise RuntimeError("abort batch")
    assert (
        evaluator.evaluate(workspace.root, context).applicability
        == "needs_revalidation"
    )
    with evaluator.read_batch(budget_bytes=0):
        assert (
            evaluator.evaluate(workspace.root, context).applicability == "unverifiable"
        )


def test_read_batches_are_instance_local(workspace):
    (workspace.root / "notes.md").write_bytes(b"abc")
    context = context_for(workspace, [("notes.md", None)])
    first, second = BindingEvaluator(), BindingEvaluator()
    with first.read_batch(budget_bytes=0), second.read_batch(budget_bytes=3):
        assert first.evaluate(workspace.root, context).applicability == "unverifiable"
        assert second.evaluate(workspace.root, context).applicability == "current"


def test_concurrent_read_batches_keep_independent_budgets(workspace):
    (workspace.root / "notes.md").write_bytes(b"abc")
    context = context_for(workspace, [("notes.md", None)])
    evaluator = BindingEvaluator()
    barrier = Barrier(2)

    def evaluate_with_budget(budget):
        with evaluator.read_batch(budget_bytes=budget):
            barrier.wait(timeout=10)
            return evaluator.evaluate(workspace.root, context)

    with ThreadPoolExecutor(max_workers=2) as workers:
        results = list(workers.map(evaluate_with_budget, [3, 0]))
    assert [result.applicability for result in results] == ["current", "unverifiable"]
    assert all(result.changed == () for result in results)
