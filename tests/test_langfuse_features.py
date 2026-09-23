"""Tests for the Langfuse export change: sampling, validation, MECE,
identity derivation, loud failure and instrumentation overhead."""
import json
import time

import pytest

from vllm_observer.core import RequestTrace, derive_trace_id
from vllm_observer.exporter import (
    AsyncExportFilter,
    LangfuseSink,
    _timeline_nodes,
    langfuse_base_url,
    langfuse_sample_rate,
)
from vllm_observer.stages import record_engine_stages


def make_sink(monkeypatch, calls):
    import requests

    monkeypatch.setenv("LANGFUSE_BASE_URL", "http://localhost:9999")
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "test-public")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "test-secret")

    def post(self, url, **kwargs):
        from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import (
            ExportTraceServiceRequest,
        )

        calls.append(ExportTraceServiceRequest.FromString(kwargs["data"]))
        response = requests.Response()
        response.status_code = 200
        response._content = b""
        return response

    monkeypatch.setattr(requests.Session, "post", post)
    return LangfuseSink()


def make_event(status="ok", total=1.0):
    event = RequestTrace("c" * 32)
    event.status = status
    generation = event.begin_generation("unit-request")
    generation["start_ns"] = event.started_ns + 1_000_000
    generation["end_ns"] = event.started_ns + 800_000_000
    snapshot = event.finish()
    snapshot["stages"]["http_total"]["total_seconds"] = total
    snapshot["model"] = "unit-model"
    return snapshot


# ---------------------------------------------------------------- identity
def test_derive_trace_id_is_deterministic_and_distinct():
    assert derive_trace_id("req-1") == derive_trace_id("req-1")
    assert derive_trace_id("req-1") != derive_trace_id("req-2")
    value = derive_trace_id("req-1")
    assert len(value) == 32
    int(value, 16)  # valid 128-bit id


def test_langfuse_base_url_accepts_host_alias(monkeypatch):
    monkeypatch.delenv("LANGFUSE_BASE_URL", raising=False)
    monkeypatch.setenv("LANGFUSE_HOST", "https://lf.example.com/")
    assert langfuse_base_url() == "https://lf.example.com"
    monkeypatch.delenv("LANGFUSE_HOST", raising=False)
    with pytest.raises(ValueError):
        langfuse_base_url()


def test_sample_rate_defaults_low_and_rejects_garbage(monkeypatch):
    monkeypatch.delenv("OBSERVER_LANGFUSE_SAMPLE_RATE", raising=False)
    assert langfuse_sample_rate() == 0.01
    monkeypatch.setenv("OBSERVER_LANGFUSE_SAMPLE_RATE", "not-a-number")
    assert langfuse_sample_rate() == 0.01
    monkeypatch.setenv("OBSERVER_LANGFUSE_SAMPLE_RATE", "5")
    assert langfuse_sample_rate() == 0.01


# ---------------------------------------------------------------- sampling
def test_normal_requests_are_dropped_at_zero_rate(monkeypatch):
    calls = []
    monkeypatch.setenv("OBSERVER_LANGFUSE_SAMPLE_RATE", "0")
    sink = make_sink(monkeypatch, calls)
    sink(make_event("ok"))
    assert calls == []


def test_error_requests_are_always_sampled(monkeypatch):
    calls = []
    monkeypatch.setenv("OBSERVER_LANGFUSE_SAMPLE_RATE", "0")
    sink = make_sink(monkeypatch, calls)
    sink(make_event("error"))
    assert len(calls) == 1
    tags = [
        item.string_value
        for scope in calls[0].resource_spans
        for ss in scope.scope_spans
        for span in ss.spans
        for kv in span.attributes
        if kv.key == "langfuse.trace.tags"
        for item in kv.value.array_value.values
    ]
    assert any("sampled=error" in tag for tag in tags)


def test_full_rate_exports_and_carries_sampled_tag(monkeypatch):
    calls = []
    monkeypatch.setenv("OBSERVER_LANGFUSE_SAMPLE_RATE", "1")
    sink = make_sink(monkeypatch, calls)
    sink(make_event("ok"))
    assert len(calls) == 1
    tags = [
        item.string_value
        for scope in calls[0].resource_spans
        for ss in scope.scope_spans
        for span in ss.spans
        for kv in span.attributes
        if kv.key == "langfuse.trace.tags"
        for item in kv.value.array_value.values
    ]
    assert any("sampled=normal" in tag for tag in tags)


# ------------------------------------------------------- validation & MECE
def test_invalid_timestamp_nodes_are_dropped_not_exported(monkeypatch):
    calls = []
    monkeypatch.setenv("OBSERVER_LANGFUSE_SAMPLE_RATE", "1")
    sink = make_sink(monkeypatch, calls)
    event = make_event()
    event["observations"].append(
        {
            "id": "deadbeefdeadbeef",
            "parent_id": event["root_span_id"],
            "name": "backwards",
            "type": "span",
            "start_ns": event["started_ns"] + 900_000_000,
            "end_ns": event["started_ns"] + 100_000_000,
        }
    )
    sink(event)
    assert len(calls) == 1
    names = [
        span.name
        for scope in calls[0].resource_spans
        for ss in scope.scope_spans
        for span in ss.spans
    ]
    assert "backwards" not in names


def test_timeline_top_level_is_mece_partition_of_total():
    event = make_event()
    generation = next(n for n in event["observations"] if n["type"] == "generation")
    start = event["started_ns"]
    end = start + int(event["stages"]["http_total"]["total_seconds"] * 1e9)
    root = {
        "id": event["root_span_id"],
        "parent_id": None,
        "name": "serve-model-request",
        "type": "span",
        "start_ns": start,
        "end_ns": end,
    }
    nodes = _timeline_nodes(event, root, generation)
    top = [
        n
        for n in nodes
        if n["parent_id"] == root["id"]
    ]
    assert top, "expected top-level segments"
    cursor = start
    for node in sorted(top, key=lambda n: n["start_ns"]):
        assert node["start_ns"] == cursor
        cursor = node["end_ns"]
    assert cursor == end


# ---------------------------------------------- decode total is independent
def test_decode_total_survives_interval_bounding():
    class Trace:
        def __init__(self):
            self.stages = {}
            self.observations = []
            self.api_intervals = []

        def add(self, name, duration, count=1):
            agg = self.stages.setdefault(name, [0.0, 0])
            agg[0] += duration
            agg[1] += count

        def interval(self, *args, **kwargs):
            self.observations.append(kwargs)

        def now_ns(self):
            return 2_000_000_000_000

    class Raw:
        _observer_clock = {
            "x-observer-clock-monotonic": "100.0",
            "x-observer-clock-unix-ns": "1000000000000",
            "x-observer-clock-error-ns": "1000",
        }
        _observer_stages = json.dumps(
            {
                "waiting_enter": 99.9,
                "first_schedule": 99.95,
                "schedule_seconds": 0.01,
                "schedule_steps": 2,
                "execute_seconds": 5.0,
                "execute_steps": 40,
                "execute_intervals": [],  # bounded/discarded
                "contention_steps": 3,
                "contention_seconds": 0.07,
            }
        )

    generation = {
        "id": "g",
        "metadata": {},
        "start_ns": 0,
    }
    trace = Trace()
    record_engine_stages(trace, generation, Raw())
    assert trace.stages["execute_model"][0] == pytest.approx(5.0)
    waiting = next(o for o in trace.observations if True)
    assert waiting["contention_steps"] == 3
    assert waiting["contention_seconds"] == pytest.approx(0.07)


# --------------------------------------------------------- loud failure
def test_langfuse_unavailable_gauge_is_loud():
    from vllm_observer.metrics import MetricsFilter

    metrics = MetricsFilter()
    assert b"observer_langfuse_enabled_but_unavailable 0.0" in metrics.render()
    metrics.mark_langfuse_unavailable()
    body = metrics.render()
    assert b"observer_langfuse_enabled_but_unavailable 1.0" in body


def test_middleware_survives_broken_langfuse_config(monkeypatch):
    import logging

    from vllm_observer.middleware import ObserverMiddleware

    records = []

    class Capture(logging.Handler):
        def emit(self, record):
            records.append(record.getMessage())

    logger = logging.getLogger("vllm_observer")
    handler = Capture(level=logging.ERROR)
    logger.addHandler(handler)
    try:
        monkeypatch.setenv("OBSERVER_LANGFUSE_ENABLED", "1")
        monkeypatch.setenv("OBSERVER_PUSH_ENABLED", "0")
        monkeypatch.delenv("LANGFUSE_BASE_URL", raising=False)
        monkeypatch.delenv("LANGFUSE_HOST", raising=False)
        middleware = ObserverMiddleware(lambda *a: None, instrument=False)
    finally:
        logger.removeHandler(handler)
    assert b"observer_langfuse_enabled_but_unavailable 1.0" in middleware.metrics.render()
    assert any("unavailable" in message for message in records)


def test_export_failure_is_counted(monkeypatch):
    calls = []
    monkeypatch.setenv("OBSERVER_LANGFUSE_SAMPLE_RATE", "1")
    sink = make_sink(monkeypatch, calls)

    def boom(spans):
        raise RuntimeError("network down")

    sink.exporter.export = boom
    exporter = AsyncExportFilter(sink)
    assert exporter.process(make_event()) is not None
    assert exporter.close()
    assert exporter.failed == 1
    assert exporter.dropped == 0


# --------------------------------------------------- instrumentation overhead
def test_stage_wrapper_overhead_is_negligible():
    from vllm_observer import stages

    class Target:
        def work(self, request_id, prompt, params):
            return request_id

    raw = Target.work
    wrapped = stages._timed_sync(raw, "unit_stage", ("request_id",))
    target = Target()
    iterations = 20_000

    start = time.perf_counter()
    for i in range(iterations):
        raw(target, i, None, None)
    bare = (time.perf_counter() - start) / iterations

    start = time.perf_counter()
    for i in range(iterations):
        wrapped(target, i, None, None)
    observed = (time.perf_counter() - start) / iterations

    # Allow generous CI jitter but catch any accidental IO on the hot path.
    assert observed - bare < 50e-6


# ---------------------------------------------------------------- TPOT
def root_attributes(call):
    attrs = {}
    for scope in call.resource_spans:
        for ss in scope.scope_spans:
            for span in ss.spans:
                if span.name == "serve-model-request":
                    for kv in span.attributes:
                        if kv.key == "observer.slow_tpot_details":
                            attrs[kv.key] = json.loads(kv.value.string_value)
                        elif kv.value.WhichOneof("value") == "double_value":
                            attrs[kv.key] = kv.value.double_value
                        elif kv.value.WhichOneof("value") == "int_value":
                            attrs[kv.key] = kv.value.int_value
                        elif kv.value.WhichOneof("value") == "bool_value":
                            attrs[kv.key] = kv.value.bool_value
    return attrs


def test_tpot_reported_and_below_threshold_has_no_slow_flag(monkeypatch):
    calls = []
    monkeypatch.setenv("OBSERVER_LANGFUSE_SAMPLE_RATE", "1")
    sink = make_sink(monkeypatch, calls)
    event = make_event()
    event["stages"]["decode"] = {"count": 4, "total_seconds": 0.2, "mean_seconds": 0.05}
    sink(event)
    attrs = root_attributes(calls[0])
    assert attrs["observer.tpot_ms"] == pytest.approx(50.0)
    assert "observer.slow_tpot" not in attrs


def test_slow_tpot_attaches_queue_and_scheduler_context(monkeypatch):
    calls = []
    monkeypatch.setenv("OBSERVER_LANGFUSE_SAMPLE_RATE", "1")
    sink = make_sink(monkeypatch, calls)
    event = make_event()
    # 3 decode steps in 0.6s -> TPOT 0.2s > default 0.1 threshold.
    event["stages"]["decode"] = {"count": 3, "total_seconds": 0.6, "mean_seconds": 0.2}
    event["stages"]["schedule_total"] = {"count": 5, "total_seconds": 0.05, "mean_seconds": 0.01}
    event["stages"]["execute_model"] = {"count": 30, "total_seconds": 0.55, "mean_seconds": 0.018}
    event["observations"].append(
        {
            "id": "w1",
            "parent_id": event["root_span_id"],
            "name": "waiting_to_first_schedule",
            "type": "span",
            "start_ns": event["started_ns"],
            "end_ns": event["started_ns"] + 1_000_000,
            "metadata": {
                "contention_steps": 2,
                "contention_seconds": 0.03,
            },
        }
    )
    sink(event)
    attrs = root_attributes(calls[0])
    assert attrs["observer.tpot_ms"] == pytest.approx(200.0)
    assert attrs["observer.slow_tpot"] is True
    assert attrs["observer.tpot_threshold_ms"] == pytest.approx(100.0)
    details = attrs["observer.slow_tpot_details"]
    assert details["waiting"]["contention_steps"] == 2
    assert details["waiting"]["contention_ms"] == pytest.approx(30.0)
    assert details["schedule_total"]["total_ms"] == pytest.approx(50.0)
    assert details["execute_model"]["total_ms"] == pytest.approx(550.0)


def test_tpot_metrics_histogram_and_slow_counter(monkeypatch):
    from vllm_observer.metrics import MetricsFilter

    metrics = MetricsFilter()
    event = make_event()
    event["stages"]["decode"] = {"count": 3, "total_seconds": 0.6, "mean_seconds": 0.2}
    metrics.process(event)
    body = metrics.render()
    assert b"observer_tpot_ms_bucket" in body
    assert b'observer_slow_tpot_requests_total{model="unknown"} 1.0' in body
