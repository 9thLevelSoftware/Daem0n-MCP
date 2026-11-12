"""
OpenTelemetry tracing integration for Daem0n-MCP.

FastMCP 3.0 has native OpenTelemetry support. This module provides
configuration and helper utilities for Daem0n-specific tracing.

Install the observability extra and enable by setting:
    OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4317
    OTEL_SERVICE_NAME=daem0nmcp

The default OTLP protocol is grpc; set OTEL_EXPORTER_OTLP_PROTOCOL to
http/protobuf for an HTTP collector. Configuration failures warn on stderr
and leave the core server available.
"""

from __future__ import annotations

import logging
import os
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from opentelemetry.sdk.trace import TracerProvider

logger = logging.getLogger(__name__)

_TRACING_ENABLED: bool | None = None


def is_tracing_enabled() -> bool:
    """Return whether startup installed or found an active tracing provider."""
    return _TRACING_ENABLED is True


def get_service_name() -> str:
    """Get the service name for tracing."""
    return os.environ.get("OTEL_SERVICE_NAME", "daem0nmcp")


def configure_tracing() -> TracerProvider | None:
    """Configure requested OTLP export and return only a provider we own."""
    global _TRACING_ENABLED
    _TRACING_ENABLED = False
    endpoint = os.environ.get("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT") or os.environ.get(
        "OTEL_EXPORTER_OTLP_ENDPOINT"
    )
    if not endpoint or os.environ.get("OTEL_SDK_DISABLED", "").lower() == "true":
        return None

    from .capabilities import CapabilityRegistry

    capability = CapabilityRegistry().get("observability")
    if (
        capability["status"] == "disabled"
        and capability["remediation"]["action"] == "enable_configuration"
    ):
        return None
    try:
        from opentelemetry import trace
    except ImportError:
        logger.warning(
            'OpenTelemetry tracing unavailable; install "daem0nmcp[observability]".'
        )
        return None
    try:
        existing_provider = trace.get_tracer_provider()
    except Exception:
        logger.warning(
            "OpenTelemetry tracing unavailable; check the configured tracing provider."
        )
        return None
    if not isinstance(existing_provider, trace.ProxyTracerProvider):
        _TRACING_ENABLED = True
        return None
    protocol = (
        (
            os.environ.get("OTEL_EXPORTER_OTLP_TRACES_PROTOCOL")
            or os.environ.get("OTEL_EXPORTER_OTLP_PROTOCOL")
            or "grpc"
        )
        .strip()
        .lower()
    )
    if protocol not in {"grpc", "http/protobuf"}:
        logger.warning(
            "OpenTelemetry tracing unavailable; set OTEL_EXPORTER_OTLP_PROTOCOL "
            "or OTEL_EXPORTER_OTLP_TRACES_PROTOCOL to grpc or http/protobuf."
        )
        return None
    if capability["status"] != "ready":
        logger.warning(
            "OpenTelemetry tracing unavailable; enable the observability profile "
            'and install "daem0nmcp[observability]".'
        )
        return None

    provider = None
    try:
        from opentelemetry.sdk.resources import OTELResourceDetector, Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor

        if protocol == "http/protobuf":
            from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
                OTLPSpanExporter,
            )
        else:
            from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import (
                OTLPSpanExporter,
            )
        resource = Resource.create({"service.name": "daem0nmcp"}).merge(
            OTELResourceDetector().detect()
        )
        provider = TracerProvider(resource=resource, shutdown_on_exit=False)
        provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
        trace.set_tracer_provider(provider)
        if trace.get_tracer_provider() is not provider:
            shutdown_tracing(provider)
            return None
    except Exception:
        shutdown_tracing(provider)
        logger.warning(
            "OpenTelemetry tracing unavailable; check the observability "
            "installation and OTLP exporter configuration."
        )
        return None
    _TRACING_ENABLED = True
    logger.info("OpenTelemetry tracing enabled.")
    return provider


def shutdown_tracing(provider: TracerProvider | None) -> None:
    """Flush owned spans without allowing telemetry errors to mask shutdown."""
    global _TRACING_ENABLED
    if provider is None:
        return
    _TRACING_ENABLED = False
    try:
        if not provider.force_flush(timeout_millis=5000):
            logger.warning("OpenTelemetry trace flush timed out.")
    except Exception:
        logger.warning("OpenTelemetry trace flush failed.")
    try:
        provider.shutdown()
    except Exception:
        logger.warning("OpenTelemetry tracing shutdown failed.")
