"""Regression coverage for the executable v7 release inventory."""

from __future__ import annotations

import json
import subprocess
import sys
from copy import deepcopy
from pathlib import Path

from scripts.v7_release_inventory import collect, synchronize, validate

ROOT = Path(__file__).resolve().parents[1]


def test_v7_release_inventory_is_current_and_checkable() -> None:
    result = subprocess.run(
        [sys.executable, "scripts/v7_release_inventory.py", "--check"],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "atomic requirements" in result.stdout


def test_fixture_checksum_tampering_is_rejected() -> None:
    requirements = json.loads(
        (ROOT / "docs/release/v7/requirements.json").read_text(encoding="utf-8")
    )
    tampered = deepcopy(requirements)
    fixture = next(iter(tampered["fixture_checksums"]))
    tampered["fixture_checksums"][fixture] = "0" * 64
    data = collect(tampered)
    data["requirements"] = synchronize(tampered, data)
    assert any("fixture checksum mismatch" in error for error in validate(data))


def test_accepted_status_requires_explicit_evidence_metadata() -> None:
    requirements = json.loads(
        (ROOT / "docs/release/v7/requirements.json").read_text(encoding="utf-8")
    )
    missing_evidence = deepcopy(requirements)
    missing_evidence["release_requirements"][0]["status"] = "accepted"
    data = collect(missing_evidence)
    data["requirements"] = synchronize(missing_evidence, data)
    assert any("accepted without evidence" in error for error in validate(data))

    evidenced = deepcopy(missing_evidence)
    evidenced["release_requirements"][0]["evidence"] = [
        {
            "kind": "test",
            "reference": "tests/test_v7_release_inventory.py",
            "commit": "a" * 40,
        }
    ]
    data = collect(evidenced)
    data["requirements"] = synchronize(evidenced, data)
    assert not any("P0" in error for error in validate(data))
