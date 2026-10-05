"""Repository binding capture and validity semantics."""

from __future__ import annotations

import hashlib
import os
from types import SimpleNamespace

import pytest

from daem0nmcp.api.v7 import discovery_operations
from daem0nmcp.code_bindings import (
    BindingEvaluator,
    CodeBindingError,
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


def test_capture_rejects_workspace_escape(workspace):
    with pytest.raises(CodeBindingError):
        capture_bindings(workspace, [("../outside.py", None)])


def test_capture_rejects_oversized_file(workspace):
    path = workspace.root / "large.txt"
    with path.open("wb") as output:
        output.truncate(5 * 1024 * 1024 + 1)
    with pytest.raises(CodeBindingError, match="size limit"):
        capture_bindings(workspace, [("large.txt", None)])


def test_unavailable_parser_is_unverifiable(workspace, monkeypatch):
    require_parser()
    path = workspace.root / "sample.py"
    path.write_text("def target():\n    return 1\n", encoding="utf-8")
    context = context_for(workspace, [("sample.py", "sample.target")])
    evaluator = BindingEvaluator()
    assert evaluator.evaluate(workspace.root, context).applicability == "current"
    monkeypatch.setattr(
        discovery_operations,
        "default_code_indexer_factory",
        lambda: SimpleNamespace(available=False),
    )
    result = evaluator.evaluate(workspace.root, context)
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
