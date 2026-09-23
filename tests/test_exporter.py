from vllm_observer.core import RequestTrace
from vllm_observer.exporter import AsyncExportFilter, LangfuseSink


def test_real_otlp_serialization_preserves_trace_and_aggregates(monkeypatch):
    import requests
    from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import (
        ExportTraceServiceRequest,
    )

    monkeypatch.setenv("LANGFUSE_BASE_URL", "http://localhost:9999")
    monkeypatch.setenv("OBSERVER_LANGFUSE_SAMPLE_RATE", "1")
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
    generation = event.begin_generation("unit-request")
    generation["start_ns"] = event.started_ns + 1_000_000
    generation["end_ns"] = event.started_ns + 800_000_000
    generation["usage"] = {"input": 8, "output": 4}
    generation["metadata"]["traceId"] = "must-not-override-native-id"
    for name, start_ms, end_ms in (
        ("queue", 10, 40),
        ("prefill", 40, 100),
        ("decode", 100, 700),
    ):
        event.interval(
            generation,
            name,
            event.started_ns + start_ms * 1_000_000,
            event.started_ns + end_ms * 1_000_000,
        )
    event.add("decode", 0.6, 3)
    snapshot = event.finish()
    snapshot["stages"]["http_total"]["total_seconds"] = 1.0
    snapshot["model"] = "unit-model"
    exporter.process(snapshot)
    assert exporter.close()
    assert exporter.failed == 0
    assert len(calls) == 1
    url, headers, payload = calls[0]
    assert url.endswith("/api/public/otel/v1/traces")
    assert headers["x-langfuse-ingestion-version"] == "4"
    spans = {span.name: span for span in payload.resource_spans[0].scope_spans[0].spans}
    assert set(spans) == {
        "serve-model-request",
        "api_prepare",
        "response_transfer",
        "generate-response",
        "engine_handoff",
        "waiting_to_first_schedule",
        "prefill",
        "decode",
        "engine_finalize",
    }
    for node in spans.values():
        assert node.trace_id.hex() == event.trace_id
        node_attrs = {attr.key: attr.value for attr in node.attributes}
        assert (
            node_attrs["langfuse.observation.metadata.traceId"].string_value
            == event.trace_id
        )
    span = spans["serve-model-request"]
    assert span.trace_id.hex() == "a" * 32
    assert span.parent_span_id.hex() == "b" * 16
    attrs = {attr.key: attr.value for attr in span.attributes}
    assert attrs["observer.decode.count"].int_value == 3
    assert attrs["observer.decode.total_ms"].double_value == 600.0
    assert attrs["langfuse.observation.type"].string_value == "span"
    gen = spans["generate-response"]
    assert gen.parent_span_id == span.span_id
    gen_attrs = {attr.key: attr.value for attr in gen.attributes}
    assert gen_attrs["langfuse.observation.type"].string_value == "generation"
    assert gen_attrs["langfuse.observation.model.name"].string_value == "unit-model"
    for name in (
        "engine_handoff",
        "waiting_to_first_schedule",
        "prefill",
        "decode",
        "engine_finalize",
    ):
        child = spans[name]
        assert child.parent_span_id == gen.span_id
        assert gen.start_time_unix_nano <= child.start_time_unix_nano
        assert child.end_time_unix_nano <= gen.end_time_unix_nano
    assert (
        spans["decode"].end_time_unix_nano - spans["decode"].start_time_unix_nano
        == 600_000_000
    )
    # The timeline is a strict partition: children of each parent cover it
    # exactly once and sum to its duration.
    by_id = {span.span_id: span for span in spans.values()}
    children = {}
    for span in spans.values():
        if span.parent_span_id in by_id:
            children.setdefault(span.parent_span_id, []).append(span)
    for parent_id, kids in children.items():
        parent = by_id[parent_id]
        assert (
            sum(kid.end_time_unix_nano - kid.start_time_unix_nano for kid in kids)
            == parent.end_time_unix_nano - parent.start_time_unix_nano
        )
    sink.provider.shutdown()


def test_export_failure_is_counted_and_root_has_no_fake_parent(monkeypatch):
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    from opentelemetry.sdk.trace.export import SpanExportResult

    monkeypatch.setenv("LANGFUSE_BASE_URL", "http://localhost:9999")
    monkeypatch.setenv("OBSERVER_LANGFUSE_SAMPLE_RATE", "1")
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
    root = next(span for span in captured if span.parent is None)
    assert root.name == "serve-model-request"
    assert root.context.trace_id == int("c" * 32, 16)
    assert root.attributes["langfuse.observation.metadata.traceId"] == "c" * 32
    sink.provider.shutdown()
