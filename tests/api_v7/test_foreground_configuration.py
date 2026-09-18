"""Configured production deadlines retain the public 1..60 second bound."""

from __future__ import annotations

from unittest.mock import patch

import pytest
from pydantic import ValidationError

from daem0nmcp.api.v7.production import create_v7_server
from daem0nmcp.config import Settings


def test_production_uses_environment_deadline(tmp_path, monkeypatch):
    monkeypatch.setenv("DAEM0NMCP_SYNC_TIMEOUT_SECONDS", "23")
    settings = Settings(project_root=str(tmp_path), _env_file=None)
    with patch("daem0nmcp.api.v7.production.V7Surface.build_server") as build:
        create_v7_server("stdio", settings=settings, environ={})
    assert build.call_args.kwargs["sync_timeout_seconds"] == 23


@pytest.mark.parametrize("value", [True, 0, 61, float("inf"), float("nan")])
def test_invalid_foreground_deadline_fails_configuration(value):
    with pytest.raises(ValidationError):
        Settings(sync_timeout_seconds=value, _env_file=None)
