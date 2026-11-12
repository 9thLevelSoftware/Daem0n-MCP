"""Test OpenTelemetry tracing integration."""

import os
from types import SimpleNamespace
from unittest.mock import Mock

import pytest


def test_tracing_module_exists():
    """Verify tracing module can be imported."""
    from daem0nmcp import tracing

    assert tracing is not None


def test_tracing_disabled_by_default():
    """Tracing should be disabled when OTEL vars not set."""
    # Clear any OTEL env vars that might be set
    old_val = os.environ.pop("OTEL_EXPORTER_OTLP_ENDPOINT", None)
    try:
        # Need to reset the cached value
        import daem0nmcp.tracing
        from daem0nmcp.tracing import is_tracing_enabled

        daem0nmcp.tracing._TRACING_ENABLED = None
        assert not is_tracing_enabled()
    finally:
        if old_val:
            os.environ["OTEL_EXPORTER_OTLP_ENDPOINT"] = old_val


def test_get_service_name_default():
    """Service name should default to daem0nmcp."""
    from daem0nmcp.tracing import get_service_name

    old_val = os.environ.pop("OTEL_SERVICE_NAME", None)
    try:
        assert get_service_name() == "daem0nmcp"
    finally:
        if old_val:
            os.environ["OTEL_SERVICE_NAME"] = old_val


@pytest.fixture
def tracing_environment(monkeypatch):
    from daem0nmcp import tracing

    for name in tuple(os.environ):
        if name.startswith("OTEL_") or name == "DAEM0NMCP_OBSERVABILITY_ENABLED":
            monkeypatch.delenv(name)
    monkeypatch.setattr(tracing, "_TRACING_ENABLED", None)
    return tracing


@pytest.mark.parametrize("disabled", ["no-endpoint", "sdk", "profile"])
def test_disabled_tracing_does_not_import_optional_sdk(
    tracing_environment, monkeypatch, disabled
):
    import builtins

    if disabled != "no-endpoint":
        monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://127.0.0.1:4318")
    if disabled == "sdk":
        monkeypatch.setenv("OTEL_SDK_DISABLED", "TRUE")
    if disabled == "profile":
        monkeypatch.setenv("DAEM0NMCP_OBSERVABILITY_ENABLED", "false")
    original_import = builtins.__import__

    def guarded_import(name, *args, **kwargs):
        if name.startswith(("opentelemetry.sdk", "opentelemetry.exporter")):
            raise AssertionError("disabled tracing imported an optional dependency")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", guarded_import)
    assert tracing_environment.configure_tracing() is None
    assert not tracing_environment.is_tracing_enabled()


def test_preconfigured_tracing_provider_is_not_replaced_or_closed(
    tracing_environment, monkeypatch
):
    from opentelemetry import trace

    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://127.0.0.1:4318")
    provider = SimpleNamespace(shutdown=Mock())
    monkeypatch.setattr(trace, "get_tracer_provider", lambda: provider)
    install = Mock()
    monkeypatch.setattr(trace, "set_tracer_provider", install)
    owned = tracing_environment.configure_tracing()
    tracing_environment.shutdown_tracing(owned)
    assert owned is None
    assert tracing_environment.is_tracing_enabled()
    install.assert_not_called()
    provider.shutdown.assert_not_called()


@pytest.mark.parametrize(
    "failure",
    [
        "missing-dependencies",
        "auto-missing-dependencies",
        "invalid-protocol",
        "invalid-provider",
    ],
)
def test_tracing_configuration_failure_keeps_protocol_clean_and_core_available(
    tracing_environment, monkeypatch, caplog, capsys, failure
):
    from opentelemetry import trace

    from daem0nmcp.api.v7 import launcher
    from daem0nmcp.capabilities import CapabilityRegistry

    monkeypatch.setenv(
        "OTEL_EXPORTER_OTLP_ENDPOINT", "http://private-token@example.invalid:4318"
    )
    if failure == "invalid-provider":
        monkeypatch.setattr(
            trace,
            "get_tracer_provider",
            Mock(side_effect=RuntimeError("private-provider-secret")),
        )
    elif failure == "invalid-protocol":
        monkeypatch.setenv("OTEL_EXPORTER_OTLP_PROTOCOL", "invalid-secret-protocol")
    else:
        monkeypatch.setattr(
            CapabilityRegistry,
            "get",
            lambda _self, _name: {
                "status": "disabled"
                if failure == "auto-missing-dependencies"
                else "degraded",
                "remediation": {"action": "install_extra"},
            },
        )
    calls = []
    server = SimpleNamespace(run=lambda **kwargs: calls.append(kwargs))
    launcher.run_server(server, launcher.ServerOptions("stdio", "127.0.0.1", 8765))
    assert len(calls) == 1
    assert calls[0]["transport"] == "stdio"
    assert not tracing_environment.is_tracing_enabled()
    assert "OpenTelemetry tracing unavailable" in caplog.text
    assert "private-token" not in caplog.text
    assert "invalid-secret-protocol" not in caplog.text
    assert "private-provider-secret" not in caplog.text
    assert capsys.readouterr().out == ""


def test_owned_tracing_cleanup_does_not_mask_launch_failure(
    tracing_environment, monkeypatch, caplog
):
    from daem0nmcp.api.v7 import launcher

    provider = SimpleNamespace(
        force_flush=Mock(side_effect=RuntimeError("private-exporter-secret")),
        shutdown=Mock(side_effect=RuntimeError("private-exporter-secret")),
    )
    monkeypatch.setattr(tracing_environment, "configure_tracing", lambda: provider)

    def fail(**_kwargs):
        raise ValueError("original launch failure")

    with pytest.raises(ValueError, match="original launch failure"):
        launcher.run_server(
            SimpleNamespace(run=fail),
            launcher.ServerOptions("stdio", "127.0.0.1", 8765),
        )
    provider.force_flush.assert_called_once_with(timeout_millis=5000)
    provider.shutdown.assert_called_once_with()
    assert "private-exporter-secret" not in caplog.text


def test_http_security_failure_does_not_initialize_tracing(
    tracing_environment, monkeypatch
):
    from daem0nmcp.api.v7 import launcher

    initialize = Mock()
    monkeypatch.setattr(tracing_environment, "configure_tracing", initialize)

    def reject(_host, _auth):
        raise ValueError("remote server rejected")

    with pytest.raises(ValueError, match="remote server rejected"):
        launcher.run_server(
            SimpleNamespace(auth=None),
            launcher.ServerOptions("streamable-http", "0.0.0.0", 8765),
            validate_security=reject,
        )
    initialize.assert_not_called()
