"""Export inert, tracked real-source fixtures for native MCP code indexing.

Source bytes come from immutable Git objects at the recorded HEAD revision.
Uncommitted implementation changes are kept out of the indexing fixtures.
Client configuration, instructions, credentials and runtime state are excluded.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
from pathlib import Path, PurePosixPath

PROJECTS = {
    "daem0n": {
        "patterns": ["**/*.py"],
        "source_prefix": "daem0nmcp/",
        "suffix": ".py",
        "search": "lexical_fts_table_name",
        "subject_path": "daem0nmcp/retrieval/lexical_config.py",
        "expected_callers": ["LexicalProjectionBuilder.rebuild", "_drop_generation"],
        "mutation_path": "daem0nmcp/retrieval/lexical_config.py",
        "mutation_append": "\n\ndef native_acceptance_probe():\n    return 42\n",
        "mutation_search": "native_acceptance_probe",
        "added_path": "daem0nmcp/native_acceptance_added.py",
        "added_content": "def native_acceptance_added():\n    return 43\n",
        "added_search": "native_acceptance_added",
    },
    "legion": {
        "patterns": ["**/*.ts", "**/*.tsx"],
        "source_prefix": "packages/",
        "suffix": ".ts",
        "search": "sha256utf8",
        "subject_path": "packages/map/src/fingerprint.ts",
        "expected_callers": ["fingerprintHash", "fingerprintRoot"],
        "mutation_path": "packages/map/src/fingerprint.ts",
        "mutation_append": "\nexport function nativeAcceptanceProbe(): number { return 42; }\n",
        "mutation_search": "nativeAcceptanceProbe",
        "added_path": "packages/map/src/native-acceptance-added.ts",
        "added_content": "export function nativeAcceptanceAdded(): number { return 43; }\n",
        "added_search": "nativeAcceptanceAdded",
    },
}


def git(repository: Path, *arguments: str) -> str:
    return subprocess.check_output(
        ["git", "-C", str(repository), *arguments], text=True, encoding="utf-8"
    ).strip()


def git_blob(repository: Path, object_id: str) -> bytes:
    return subprocess.check_output(
        ["git", "-C", str(repository), "cat-file", "blob", object_id]
    )


def export_project(name: str, repository: Path, destination: Path) -> dict[str, object]:
    specification = PROJECTS[name]
    repository = repository.resolve(strict=True)
    destination.mkdir(parents=True, exist_ok=False)
    files: dict[str, str] = {}
    revision = git(repository, "rev-parse", "HEAD")
    entries = git(repository, "ls-tree", "-r", "-z", revision).split("\0")
    for entry in entries:
        if not entry:
            continue
        metadata, relative = entry.split("\t", 1)
        mode, kind, object_id = metadata.split()
        if kind != "blob" or mode not in {"100644", "100755"}:
            continue
        path = PurePosixPath(relative)
        if not relative or not relative.startswith(str(specification["source_prefix"])):
            continue
        if name == "daem0n" and path.suffix != ".py":
            continue
        if name == "legion" and (
            "src" not in path.parts or path.suffix not in {".ts", ".tsx"}
        ):
            continue
        if any(
            part in {"node_modules", "dist", "__pycache__", ".daem0nmcp"}
            for part in path.parts
        ):
            continue
        data = git_blob(repository, object_id)
        target = destination.joinpath(*path.parts)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        files[relative] = hashlib.sha256(data).hexdigest()
    subject = str(specification["subject_path"])
    if subject not in files:
        raise ValueError("known acceptance subject was not exported")
    manifest: dict[str, object] = {
        "project": name,
        "source_repository": str(repository),
        "source_revision": revision,
        "source_dirty": False,
        "source_worktree_dirty": bool(git(repository, "status", "--porcelain")),
        "source_policy": "Tracked blobs from immutable Git revision; working-tree edits and host configuration excluded",
        "files": files,
        "file_count": len(files),
        "scenario": specification,
    }
    (destination / "native-acceptance-source.json").write_text(
        json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
    )
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--daem0n", type=Path, required=True)
    parser.add_argument("--legion", type=Path, required=True)
    parser.add_argument("--destination", type=Path, required=True)
    args = parser.parse_args()
    for name, repository in (("daem0n", args.daem0n), ("legion", args.legion)):
        manifest = export_project(name, repository, args.destination / name)
        print(
            json.dumps(
                {
                    key: manifest[key]
                    for key in (
                        "project",
                        "source_revision",
                        "source_dirty",
                        "file_count",
                    )
                }
            )
        )


if __name__ == "__main__":
    main()
