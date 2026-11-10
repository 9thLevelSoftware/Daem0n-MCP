"""The legacy retrieval tests need ``sentence_transformers`` and skip without it.

The ``local`` extra brings ``numpy`` (through ``qdrant-client``) but not
``sentence-transformers``, so a ``numpy`` guard alone lets these modules fail
at collection instead of skipping.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

LEGACY_RETRIEVAL_MODULES = (
    "test_auto_zoom_integration.py",
    "test_query_classifier.py",
    "test_retrieval_router.py",
)

# Runs each module's import the way collection does, in a process where
# ``numpy`` is importable (a stand-in when it is not installed) and
# ``sentence_transformers`` is not, whatever profile the suite itself runs in.
_IMPORT_WITHOUT_SENTENCE_TRANSFORMERS = """
import importlib.abc
import runpy
import sys
import types

import pytest


class BlockSentenceTransformers(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split(".")[0] == "sentence_transformers":
            raise ModuleNotFoundError(
                "No module named 'sentence_transformers'",
                name="sentence_transformers",
            )


sys.meta_path.insert(0, BlockSentenceTransformers())
try:
    import numpy
except ModuleNotFoundError:
    sys.modules["numpy"] = types.ModuleType("numpy")

for path in sys.argv[1:]:
    try:
        runpy.run_path(path)
    except pytest.skip.Exception as skipped:
        print(f"skipped: {skipped}")
    else:
        print("imported")
"""


def test_legacy_retrieval_modules_skip_without_sentence_transformers():
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            _IMPORT_WITHOUT_SENTENCE_TRANSFORMERS,
            *(str(ROOT / "tests" / name) for name in LEGACY_RETRIEVAL_MODULES),
        ],
        cwd=ROOT,
        env={**os.environ, "PYTHONPATH": str(ROOT)},
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    outcomes = result.stdout.splitlines()
    assert len(outcomes) == len(LEGACY_RETRIEVAL_MODULES), result.stdout
    for name, outcome in zip(LEGACY_RETRIEVAL_MODULES, outcomes, strict=True):
        assert outcome.startswith("skipped:"), (name, outcome)
        assert "sentence_transformers" in outcome, (name, outcome)
