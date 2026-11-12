"""Real MCP requests export spans to a disposable local OTLP collector."""

from __future__ import annotations

import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from daem0nmcp.workspace import WorkspaceRegistry
from tests.api_v7.process_client import process_client, succeed


@contextmanager
def _collector(protocol):
    messages = []
    trace_pb = pytest.importorskip(
        "opentelemetry.proto.collector.trace.v1.trace_service_pb2"
    )
    if protocol == "grpc":
        grpc = pytest.importorskip("grpc")
        service_pb = pytest.importorskip(
            "opentelemetry.proto.collector.trace.v1.trace_service_pb2_grpc"
        )

        class Service(service_pb.TraceServiceServicer):
            def Export(self, request, _context):  # noqa: N802 - generated gRPC API
                messages.append(request)
                return trace_pb.ExportTraceServiceResponse()

        with ThreadPoolExecutor(max_workers=1) as executor:
            server = grpc.server(executor)
            service_pb.add_TraceServiceServicer_to_server(Service(), server)
            port = server.add_insecure_port("127.0.0.1:0")
            server.start()
            try:
                yield f"http://127.0.0.1:{port}", messages
            finally:
                server.stop(0).wait(5)
        return

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            raw = self.rfile.read(int(self.headers["Content-Length"]))
            messages.append(trace_pb.ExportTraceServiceRequest.FromString(raw))
            self.send_response(200)
            self.send_header("Content-Type", "application/x-protobuf")
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", messages
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        assert not thread.is_alive()


def _service_spans(messages, service):
    for message in tuple(messages):
        for resource in message.resource_spans:
            attributes = {
                item.key: item.value.string_value
                for item in resource.resource.attributes
            }
            if attributes.get("service.name") == service:
                for scope in resource.scope_spans:
                    for span in scope.spans:
                        yield attributes, span.name


@pytest.mark.parametrize(
    ("transport", "protocol"),
    [
        ("stdio", "http/protobuf"),
        ("streamable-http", "http/protobuf"),
        ("stdio", "grpc"),
    ],
)
async def test_production_otlp_export(transport, protocol, tmp_path):
    exporter_module = pytest.importorskip(
        "opentelemetry.exporter.otlp.proto."
        + ("http" if protocol == "http/protobuf" else "grpc")
        + ".trace_exporter"
    )
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor

    workspace = WorkspaceRegistry([tmp_path], default_root=tmp_path).default
    scope = {"workspace_id": workspace.workspace_id}
    service = f"daem0n-observability-{transport}"
    with _collector(protocol) as (endpoint, messages):
        control = TracerProvider(resource=Resource.create({"service.name": "control"}))
        control.add_span_processor(
            SimpleSpanProcessor(
                exporter_module.OTLPSpanExporter(
                    endpoint=endpoint + "/v1/traces"
                    if protocol == "http/protobuf"
                    else endpoint
                )
            )
        )
        try:
            with control.get_tracer("acceptance-control").start_as_current_span(
                "collector-control"
            ):
                pass
        finally:
            control.shutdown()
        assert list(_service_spans(messages, "control"))
        environment = {
            "OTEL_SERVICE_NAME": service,
            "OTEL_RESOURCE_ATTRIBUTES": "acceptance.run=isolated,service.name=ignored",
            "OTEL_BSP_SCHEDULE_DELAY": "100",
        }
        if protocol == "grpc":
            environment["OTEL_EXPORTER_OTLP_ENDPOINT"] = endpoint
        elif transport == "stdio":
            environment.update(
                OTEL_EXPORTER_OTLP_ENDPOINT=endpoint,
                OTEL_EXPORTER_OTLP_PROTOCOL="http/protobuf",
            )
        else:
            environment.update(
                OTEL_EXPORTER_OTLP_ENDPOINT="http://127.0.0.1:1",
                OTEL_EXPORTER_OTLP_PROTOCOL="grpc",
                OTEL_EXPORTER_OTLP_TRACES_ENDPOINT=endpoint + "/v1/traces",
                OTEL_EXPORTER_OTLP_TRACES_PROTOCOL="http/protobuf",
            )
        async with process_client(
            tmp_path, transport, environment_overrides=environment
        ) as session:
            await succeed(session, "session_brief", scope)
            await succeed(session, "system_health", scope)
            await succeed(
                session,
                "memory_recall",
                {**scope, "query": "private-observability-query"},
            )
            deadline = asyncio.get_running_loop().time() + 5
            while True:
                spans = list(_service_spans(messages, service))
                if "tools/call memory_recall" in {name for _, name in spans}:
                    break
                assert asyncio.get_running_loop().time() < deadline, spans
                await asyncio.sleep(0.05)
        names = {name for _, name in spans}
        assert {"tools/call session_brief", "tools/call system_health"} <= names
        assert all(
            attributes["acceptance.run"] == "isolated" for attributes, _ in spans
        )
        assert all("private-observability-query" not in str(item) for item in messages)
