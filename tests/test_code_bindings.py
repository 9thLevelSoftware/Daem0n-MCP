"""Repository binding capture and validity semantics."""

from __future__ import annotations

import hashlib
import os
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
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
        assert result.applicability == "needs_revalidation"
        assert result.changed == ("notes.md",)


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
        if grow_past_file_limit:
            assert result.applicability == "needs_revalidation"
            assert result.changed == ("notes.md",)
        else:
            assert result.applicability == "unverifiable"
            assert result.changed == ()
        assert evaluator.evaluate(workspace.root, small).applicability == "unverifiable"
    assert (
        evaluator.evaluate(workspace.root, context).applicability
        == "needs_revalidation"
    )
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
