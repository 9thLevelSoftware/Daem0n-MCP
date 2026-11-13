"""Stale setuptools output cannot enter a declared acceptance artifact."""

import hashlib
import zipfile

import pytest

from scripts.native_backend_artifact import (
    required_package_files,
    stage_sources,
    verify_wheel,
)


def test_runtime_requirements_follow_declared_package_data(tmp_path):
    (tmp_path / "pyproject.toml").write_text(
        "[tool.setuptools.package-data]\n"
        '"daem0nmcp.ui" = ["static/*.js"]\n'
        '"daem0nmcp.opencode_assets" = ["*.ts", "commands/*.md"]\n',
        encoding="utf-8",
    )
    paths = {
        "daem0nmcp/core.py",
        "daem0nmcp/ui/static/runtime.js",
        "daem0nmcp/opencode_assets/daem0n-v2.ts",
        "daem0nmcp/opencode_assets/commands/brief.md",
        "daem0nmcp/ui/build/bundle.mjs",
        "daem0nmcp/ui/static/.gitkeep",
    }
    assert required_package_files(tmp_path, dict.fromkeys(paths, "digest")) == {
        path for path in paths if not path.endswith((".mjs", ".gitkeep"))
    }


@pytest.mark.parametrize(
    "asset", ["opencode_assets/daem0n-v2.ts", "ui/static/runtime.js"]
)
def test_wheel_rejects_missing_required_runtime_asset(tmp_path, asset):
    wheel = tmp_path / "missing-asset.whl"
    with zipfile.ZipFile(wheel, "w"):
        pass
    with pytest.raises(ValueError, match="required runtime assets missing"):
        verify_wheel(
            wheel, {f"daem0nmcp/{asset}": hashlib.sha256(b"asset").hexdigest()}
        )


@pytest.mark.parametrize("defect", ["stale", "modified", "missing"])
def test_wheel_rejects_package_source_drift(tmp_path, defect):
    source = b"current_source = True\n"
    expected = {"daem0nmcp/current.py": hashlib.sha256(source).hexdigest()}
    wheel = tmp_path / "candidate.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        if defect != "missing":
            archive.writestr(
                "daem0nmcp/current.py", b"different" if defect == "modified" else source
            )
        if defect == "stale":
            archive.writestr("daem0nmcp/removed_legacy.py", b"stale = True\n")
    with pytest.raises(ValueError, match="Unattested|inventory differs"):
        verify_wheel(wheel, expected)


def test_isolated_staging_omits_build_cache_and_attests_current_wheel(tmp_path):
    root = tmp_path / "checkout"
    current = root / "daem0nmcp/current.py"
    current.parent.mkdir(parents=True)
    current.write_bytes(b"current = True\n")
    stale = root / "build/lib/daem0nmcp/stale.py"
    stale.parent.mkdir(parents=True)
    stale.write_bytes(b"stale = True\n")
    expected = {
        "daem0nmcp/current.py": hashlib.sha256(current.read_bytes()).hexdigest()
    }
    staged = tmp_path / "staged"
    stage_sources(root, staged, expected)
    assert not (staged / "build").exists()
    assert stale.read_bytes() == b"stale = True\n"
    wheel = tmp_path / "current.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.write(staged / "daem0nmcp/current.py", "daem0nmcp/current.py")
        archive.writestr("daem0nmcp.dist-info/METADATA", "Name: daem0nmcp\n")
    report = verify_wheel(wheel, expected)
    assert report["python_files"] == 1
    assert report["all_package_members_attested"] is True
