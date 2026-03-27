"""Test OpenTelemetry tracing integration."""

import os


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
