"""
OpenTelemetry tracing integration for Daem0n-MCP.

FastMCP 3.0 has native OpenTelemetry support. This module provides
configuration and helper utilities for Daem0n-specific tracing.

Enable by setting:
    OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4317
    OTEL_SERVICE_NAME=daem0nmcp
"""

import logging
import os

logger = logging.getLogger(__name__)

_TRACING_ENABLED: bool | None = None


def is_tracing_enabled() -> bool:
    """Check if tracing is configured."""
    global _TRACING_ENABLED

    if _TRACING_ENABLED is not None:
        return _TRACING_ENABLED

    # Check for OTLP endpoint configuration
    endpoint = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT")
    _TRACING_ENABLED = endpoint is not None and len(endpoint) > 0

    if _TRACING_ENABLED:
        logger.info(f"OpenTelemetry tracing enabled: {endpoint}")

    return _TRACING_ENABLED


def get_service_name() -> str:
    """Get the service name for tracing."""
    return os.environ.get("OTEL_SERVICE_NAME", "daem0nmcp")
