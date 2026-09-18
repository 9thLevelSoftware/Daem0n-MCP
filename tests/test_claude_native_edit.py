from __future__ import annotations

import hashlib

import pytest

from daem0nmcp.claude_hooks.native_edit import (
    NativeEditNormalizationError,
    configured_native_edit_tools,
    native_edit_relative_paths,
    native_edit_request,
)


def test_native_edit_normalizes_paths_and_hashes_preimage_at_host(tmp_path):
    target = tmp_path / "src" / "main.py"
    target.parent.mkdir()
    target.write_text("before", encoding="utf-8")

    request = native_edit_request(
        project_path=tmp_path,
        tool_name="Edit",
        tool_input={
            "file_path": str(target),
            "old_string": "before",
            "new_string": "after",
        },
        configured_tools=frozenset({"Edit"}),
    )

    assert request.arguments["file_path"] == "src/main.py"
    assert request.preimages[0].relative_file_path == "src/main.py"
    assert request.preimages[0].sha256 == hashlib.sha256(b"before").hexdigest()
    assert request.preimages[0].byte_count == len(b"before")


def test_native_edit_rejects_unconfigured_or_outside_paths(tmp_path):
    with pytest.raises(NativeEditNormalizationError):
        native_edit_request(
            project_path=tmp_path,
            tool_name="Bash",
            tool_input={"file_path": str(tmp_path / "x.py")},
            configured_tools=frozenset({"Edit"}),
        )
    with pytest.raises(NativeEditNormalizationError):
        native_edit_request(
            project_path=tmp_path,
            tool_name="Edit",
            tool_input={"file_path": str(tmp_path.parent / "outside.py")},
            configured_tools=frozenset({"Edit"}),
        )


def test_native_edit_records_missing_file_without_model_hash(tmp_path):
    request = native_edit_request(
        project_path=tmp_path,
        tool_name="Write",
        tool_input={"file_path": str(tmp_path / "new.py"), "content": "created"},
        configured_tools=frozenset({"Write"}),
    )
    assert request.preimages[0].state == "missing"
    assert request.preimages[0].sha256 is None


def test_notebook_edit_binds_notebook_preimage_and_post_edit_path(tmp_path):
    notebook = tmp_path / "analysis.ipynb"
    notebook.write_text('{"cells":[]}', encoding="utf-8")
    arguments = {
        "notebook_path": str(notebook),
        "new_source": "print(1)",
        "cell_type": "code",
        "edit_mode": "insert",
    }
    request = native_edit_request(
        project_path=tmp_path,
        tool_name="NotebookEdit",
        tool_input=arguments,
        configured_tools=configured_native_edit_tools({}),
    )
    assert request.preimages[0].relative_file_path == "analysis.ipynb"
    assert (
        request.preimages[0].sha256 == hashlib.sha256(notebook.read_bytes()).hexdigest()
    )
    notebook.write_text('{"cells":[{"source":"print(1)"}]}', encoding="utf-8")
    assert native_edit_relative_paths(
        project_path=tmp_path,
        tool_name="NotebookEdit",
        tool_input=arguments,
        configured_tools=configured_native_edit_tools({}),
    ) == ("analysis.ipynb",)


def test_configured_tools_are_explicit_when_present():
    assert configured_native_edit_tools(
        {"DAEM0NMCP_NATIVE_EDIT_TOOLS": "Edit, Write"}
    ) == {
        "Edit",
        "Write",
    }
    with pytest.raises(NativeEditNormalizationError):
        configured_native_edit_tools({"DAEM0NMCP_NATIVE_EDIT_TOOLS": ""})


def test_opencode_apply_patch_binds_raw_arguments_and_all_path_preimages(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "update.py").write_text("before\n", encoding="utf-8")
    (tmp_path / "src" / "delete.py").write_text("delete\n", encoding="utf-8")
    (tmp_path / "src" / "move.py").write_text("move\n", encoding="utf-8")
    patch_text = """*** Begin Patch
*** Update File: src\\update.py
@@
-before
+after
*** Add File: src/added.py
+added
*** Delete File: src/delete.py
*** Update File: src/move.py
*** Move to: src/moved.py
@@
 move
*** End Patch"""

    request = native_edit_request(
        project_path=tmp_path,
        tool_name="apply_patch",
        tool_input={"patchText": patch_text},
        configured_tools=configured_native_edit_tools({}),
    )

    assert request.arguments == {"patchText": patch_text}
    assert [item.relative_file_path for item in request.preimages] == [
        "src/added.py",
        "src/delete.py",
        "src/move.py",
        "src/moved.py",
        "src/update.py",
    ]
    assert [item.state for item in request.preimages] == [
        "missing",
        "file",
        "file",
        "missing",
        "file",
    ]


def test_opencode_apply_patch_post_paths_do_not_recheck_changed_state(tmp_path):
    patch_text = """*** Begin Patch
*** Add File: added.txt
+after
*** Delete File: old.txt
*** End Patch"""
    (tmp_path / "added.txt").write_text("after\n", encoding="utf-8")
    assert native_edit_relative_paths(
        project_path=tmp_path,
        tool_name="apply_patch",
        tool_input={"patchText": patch_text},
        configured_tools=configured_native_edit_tools({}),
    ) == ("added.txt", "old.txt")


@pytest.mark.parametrize(
    "patch_text",
    (
        """*** Begin Patch
*** Add File: same.txt
+one
*** Add File: same.txt
+two
*** End Patch""",
        """*** Begin Patch
*** Update File: source.txt
*** Move to: source.txt
@@
-old
+new
*** End Patch""",
        """*** Begin Patch
*** Update File: ../outside.txt
@@
-old
+new
*** End Patch""",
        "*** Begin Patch\r\n*** Add File: new.txt\r\n+new\r\n*** End Patch",
        """*** Begin Patch
*** Unsupported File: new.txt
+new
*** End Patch""",
    ),
)
def test_opencode_apply_patch_rejects_duplicate_rename_outside_and_ambiguous_syntax(
    tmp_path,
    patch_text,
):
    (tmp_path / "source.txt").write_text("old\n", encoding="utf-8")
    with pytest.raises(NativeEditNormalizationError):
        native_edit_request(
            project_path=tmp_path,
            tool_name="apply_patch",
            tool_input={"patchText": patch_text},
            configured_tools=configured_native_edit_tools({}),
        )


def test_opencode_apply_patch_enforces_add_and_update_preimage_state(tmp_path):
    (tmp_path / "exists.txt").write_text("existing", encoding="utf-8")
    for patch_text in (
        """*** Begin Patch
*** Add File: exists.txt
+replacement
*** End Patch""",
        """*** Begin Patch
*** Update File: missing.txt
@@
-old
+new
*** End Patch""",
        """*** Begin Patch
*** Delete File: missing.txt
*** End Patch""",
    ):
        with pytest.raises(NativeEditNormalizationError, match="preimage state"):
            native_edit_request(
                project_path=tmp_path,
                tool_name="apply_patch",
                tool_input={"patchText": patch_text},
                configured_tools=configured_native_edit_tools({}),
            )


def test_opencode_apply_patch_accepts_exact_end_of_file_marker(tmp_path):
    target = tmp_path / "target.txt"
    target.write_text("before", encoding="utf-8")
    patch_text = """*** Begin Patch
*** Update File: target.txt
@@
-before
+after
*** End of File
*** End Patch"""

    request = native_edit_request(
        project_path=tmp_path,
        tool_name="apply_patch",
        tool_input={"patchText": patch_text},
        configured_tools=configured_native_edit_tools({}),
    )

    assert request.arguments["patchText"] == patch_text
    assert request.preimages[0].relative_file_path == "target.txt"
