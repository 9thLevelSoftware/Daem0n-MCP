"""The dreaming package must not load the legacy managers for format-7 code."""

from __future__ import annotations

import subprocess
import sys

import pytest

_LEGACY_MODULES = (
    "daem0nmcp.dreaming.persistence",
    "daem0nmcp.dreaming.scheduler",
    "daem0nmcp.dreaming.strategies",
)


def _run(code: str) -> str:
    completed = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    return completed.stdout.strip()


def test_runtime_import_does_not_load_the_legacy_dreaming_modules() -> None:
    loaded = _run(
        "import sys\n"
        "import daem0nmcp.dreaming.v7_runtime\n"
        f"print(sorted(set({_LEGACY_MODULES!r}) & set(sys.modules)))\n"
    )
    assert loaded == "[]"


def test_package_import_alone_does_not_load_the_legacy_dreaming_modules() -> None:
    loaded = _run(
        "import sys\n"
        "import daem0nmcp.dreaming\n"
        f"print(sorted(set({_LEGACY_MODULES!r}) & set(sys.modules)))\n"
    )
    assert loaded == "[]"


def test_legacy_names_still_import_from_the_package() -> None:
    import daem0nmcp.dreaming as dreaming
    from daem0nmcp.dreaming import (
        CommunityRefresh,
        ConnectionDiscovery,
        DreamResult,
        DreamSession,
        DreamStrategy,
        FailedDecisionReview,
        IdleDreamScheduler,
        PendingOutcomeResolver,
        persist_dream_result,
        persist_session_summary,
        persistence,
        scheduler,
        strategies,
    )

    assert IdleDreamScheduler is scheduler.IdleDreamScheduler
    assert DreamSession is persistence.DreamSession
    assert DreamResult is persistence.DreamResult
    assert persist_dream_result is persistence.persist_dream_result
    assert persist_session_summary is persistence.persist_session_summary
    for name, value in (
        ("DreamStrategy", DreamStrategy),
        ("FailedDecisionReview", FailedDecisionReview),
        ("ConnectionDiscovery", ConnectionDiscovery),
        ("CommunityRefresh", CommunityRefresh),
        ("PendingOutcomeResolver", PendingOutcomeResolver),
    ):
        assert value is getattr(strategies, name)
    assert set(dreaming.__all__) <= set(dir(dreaming))
    for name in dreaming.__all__:
        assert getattr(dreaming, name) is not None


def test_unknown_package_attribute_is_an_attribute_error() -> None:
    import daem0nmcp.dreaming as dreaming

    missing = "no_such_dreaming_name"
    with pytest.raises(AttributeError):
        getattr(dreaming, missing)
    assert not hasattr(dreaming, missing)
