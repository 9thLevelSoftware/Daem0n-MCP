"""Build-input and wheel attestation for isolated native acceptance snapshots."""

from __future__ import annotations

import hashlib
import shutil
import zipfile
from fnmatch import fnmatchcase
from pathlib import Path, PurePosixPath

import tomlkit

from daem0nmcp.protected_files import reject_linked_ancestry


def source_hashes(root: Path) -> dict[str, str]:
    root = root.resolve()
    selected = [root / name for name in ("pyproject.toml", "README.md", "LICENSE")]
    selected.extend(
        path
        for path in (root / "daem0nmcp").rglob("*")
        if path.is_file()
        and "__pycache__" not in path.parts
        and path.suffix not in {".pyc", ".pyo"}
    )
    result = {}
    for path in sorted(selected):
        reject_linked_ancestry(path)
        result[path.relative_to(root).as_posix()] = hashlib.sha256(
            path.read_bytes()
        ).hexdigest()
    return result


def stage_sources(root: Path, target: Path, expected: dict[str, str]) -> None:
    """Copy only declared inputs into a new tree with no setuptools build cache."""
    reject_linked_ancestry(target)
    target.mkdir(parents=True, exist_ok=False)
    for relative, digest in expected.items():
        name = PurePosixPath(relative)
        if name.is_absolute() or ".." in name.parts:
            raise ValueError("Invalid build input path")
        source = root / relative
        reject_linked_ancestry(source)
        if hashlib.sha256(source.read_bytes()).hexdigest() != digest:
            raise ValueError("Source changed during staging")
        destination = target / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
        if hashlib.sha256(destination.read_bytes()).hexdigest() != digest:
            raise ValueError("Staged source differs from declared input")


def required_package_files(root: Path, expected: dict[str, str]) -> set[str]:
    """Require Python plus the runtime assets declared by setuptools."""
    configuration = tomlkit.parse((root / "pyproject.toml").read_text("utf-8"))
    patterns = configuration["tool"]["setuptools"]["package-data"]
    required = {name for name in expected if name.endswith(".py")}
    for package, globs in patterns.items():
        prefix = package.replace(".", "/") + "/"
        for name in expected:
            if name.startswith(prefix) and any(
                fnmatchcase(name[len(prefix) :], pattern) for pattern in globs
            ):
                required.add(name)
    return required


def verify_wheel(
    wheel: Path, expected: dict[str, str], required: set[str] | None = None
) -> dict:
    """Reject stale, missing or modified package files, retaining a full inventory."""
    files = {}
    package_files = {}
    with zipfile.ZipFile(wheel) as archive:
        for entry in archive.infolist():
            if entry.is_dir():
                continue
            name = PurePosixPath(entry.filename)
            if name.is_absolute() or ".." in name.parts or entry.filename in files:
                raise ValueError("Unsafe or duplicate wheel member")
            digest = hashlib.sha256(archive.read(entry)).hexdigest()
            files[entry.filename] = digest
            if entry.filename.startswith("daem0nmcp/") or name.suffix == ".py":
                if expected.get(entry.filename) != digest:
                    raise ValueError(f"Unattested package member: {entry.filename}")
                package_files[entry.filename] = digest
    expected_python = {
        name
        for name in expected
        if name.startswith("daem0nmcp/") and name.endswith(".py")
    }
    actual_python = {name for name in package_files if name.endswith(".py")}
    if expected_python != actual_python:
        raise ValueError("Wheel Python file inventory differs from source inputs")
    required = (
        {name for name in expected if name.startswith("daem0nmcp/")}
        if required is None
        else required | expected_python
    )
    missing = required - set(package_files)
    if missing:
        raise ValueError(f"Wheel required runtime assets missing: {sorted(missing)}")
    return {
        "wheel_sha256": hashlib.sha256(wheel.read_bytes()).hexdigest(),
        "python_files": len(actual_python),
        "package_files": package_files,
        "all_wheel_members": files,
        "all_package_members_attested": True,
        "all_required_runtime_files_present": True,
        "required_package_files": sorted(required),
    }
