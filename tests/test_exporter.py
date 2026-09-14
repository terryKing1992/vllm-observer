from vllm_observer.core import RequestTrace
from vllm_observer.exporter import AsyncExportFilter, LangfuseSink


def test_real_otlp_serialization_preserves_trace_and_aggregates(monkeypatch):
    import requests
    from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import (
        ExportTraceServiceRequest,
    )

    monkeypatch.setenv("LANGFUSE_BASE_URL", "http://localhost:9999")
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "test-public")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "test-secret")
    calls = []

    def post(self, url, **kwargs):
        payload = ExportTraceServiceRequest.FromString(kwargs["data"])
        calls.append((url, dict(self.headers), payload))
        response = requests.Response()
        response.status_code = 200
        response._content = b""
        return response

    monkeypatch.setattr(requests.Session, "post", post)
    sink = LangfuseSink()
    exporter = AsyncExportFilter(sink)
    event = RequestTrace("a" * 32, parent_span_id="b" * 16)
    event.add("decode", 0.6, 3)
    exporter.process(event.finish())
    assert exporter.close()
    assert exporter.failed == 0
    assert len(calls) == 1
    url, headers, payload = calls[0]
    assert url.endswith("/api/public/otel/v1/traces")
    assert headers["x-langfuse-ingestion-version"] == "4"
    span = payload.resource_spans[0].scope_spans[0].spans[0]
    assert span.trace_id.hex() == "a" * 32
    assert span.parent_span_id.hex() == "b" * 16
    attrs = {attr.key: attr.value for attr in span.attributes}
    assert attrs["observer.decode.count"].int_value == 3
    assert attrs["observer.decode.total_seconds"].double_value == 0.6
    sink.provider.shutdown()


def test_export_failure_is_counted_and_root_has_no_fake_parent(monkeypatch):
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    from opentelemetry.sdk.trace.export import SpanExportResult

    monkeypatch.setenv("LANGFUSE_BASE_URL", "http://localhost:9999")
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "test")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "test")
    captured = []

    def fail(self, spans):
        captured.extend(spans)
        return SpanExportResult.FAILURE

    monkeypatch.setattr(OTLPSpanExporter, "export", fail)
    sink = LangfuseSink()
    exporter = AsyncExportFilter(sink)
    exporter.process(RequestTrace("c" * 32).finish())
    assert exporter.close()
    assert exporter.failed == 1
    assert captured[0].parent is None
    assert captured[0].context.trace_id == int("c" * 32, 16)
    sink.provider.shutdown()
