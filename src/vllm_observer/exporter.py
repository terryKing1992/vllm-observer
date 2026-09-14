"""Bounded background export; no network calls on the inference path."""

import base64
import contextvars
import copy
import json
import logging
import os
import queue
import threading
from datetime import datetime, timezone

logger = logging.getLogger("vllm_observer")


class AsyncExportFilter:
    def __init__(self, sink, capacity=1024):
        if capacity < 1:
            raise ValueError("Export queue capacity must be positive")
        self.sink = sink
        self.queue = queue.Queue(maxsize=capacity)
        self.dropped = 0
        self.failed = 0
        self.stopping = threading.Event()
        self.thread = threading.Thread(
            target=self._run, daemon=True, name="observer-export"
        )
        self.thread.start()

    def process(self, event):
        if self.stopping.is_set():
            self.dropped += 1
            return event
        try:
            self.queue.put_nowait(copy.deepcopy(event))
        except queue.Full:
            self.dropped += 1
            logger.warning("Observer export queue full; dropped=%d", self.dropped)
        return event

    def _run(self):
        while not self.stopping.is_set() or not self.queue.empty():
            try:
                event = self.queue.get(timeout=0.1)
            except queue.Empty:
                continue
            try:
                self.sink(event)
            except Exception:
                self.failed += 1
                logger.exception("Observer export failed")
            finally:
                self.queue.task_done()

    def close(self, timeout=5):
        self.stopping.set()
        self.thread.join(timeout)
        return not self.thread.is_alive()


class LangfuseSink:
    def __init__(self):
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
            OTLPSpanExporter,
        )
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import SpanProcessor, TracerProvider
        from opentelemetry.sdk.trace.export import SpanExportResult
        from opentelemetry.sdk.trace.id_generator import RandomIdGenerator

        desired_id = contextvars.ContextVar("observer_export_trace_id", default=None)
        self.desired_id = desired_id
        desired_span = contextvars.ContextVar("observer_export_span_id", default=None)
        self.desired_span = desired_span
        batch = contextvars.ContextVar("observer_export_batch", default=None)
        self.batch = batch

        class RequestIdGenerator(RandomIdGenerator):
            def generate_trace_id(self):
                return desired_id.get() or super().generate_trace_id()

            def generate_span_id(self):
                return desired_span.get() or super().generate_span_id()

        class ExportProcessor(SpanProcessor):
            def __init__(self, exporter):
                self.exporter = exporter

            def on_end(self, span):
                batch.get().append(span)

            def shutdown(self):
                self.exporter.shutdown()

        base = os.environ["LANGFUSE_BASE_URL"].rstrip("/")
        auth = base64.b64encode(
            f"{os.environ['LANGFUSE_PUBLIC_KEY']}:{os.environ['LANGFUSE_SECRET_KEY']}".encode()
        ).decode()
        self.provider = TracerProvider(
            id_generator=RequestIdGenerator(),
            resource=Resource.create(
                {"service.name": os.getenv("OBSERVER_SERVICE", "vllm")}
            ),
        )
        self.exporter = OTLPSpanExporter(
            endpoint=base + "/api/public/otel/v1/traces",
            timeout=2,
            headers={
                "Authorization": "Basic " + auth,
                "x-langfuse-ingestion-version": "4",
            },
        )
        self.provider.add_span_processor(ExportProcessor(self.exporter))
        self.success = SpanExportResult.SUCCESS
        self.tracer = self.provider.get_tracer("vllm-observer")

    def __call__(self, event):
        from opentelemetry import trace
        from opentelemetry.context import Context
        from opentelemetry.trace import NonRecordingSpan, SpanContext, TraceFlags

        context = Context()
        if event.get("parent_span_id"):
            parent = NonRecordingSpan(
                SpanContext(
                    int(event["trace_id"], 16),
                    int(event["parent_span_id"], 16),
                    True,
                    TraceFlags(1),
                )
            )
            context = trace.set_span_in_context(parent, context)
        start = event["started_ns"]
        end = start + int(event["stages"]["http_total"]["total_seconds"] * 1e9)
        root = {
            "id": event["root_span_id"],
            "parent_id": None,
            "name": "serve-model-request",
            "type": "span",
            "start_ns": start,
            "end_ns": end,
            "metadata": {"route": event.get("route", "unknown")},
        }
        nodes = [root, *event.get("observations", [])]
        contexts = {None: context}
        completed = []
        opened = []
        batch_token = self.batch.set(completed)
        trace_token = self.desired_id.set(int(event["trace_id"], 16))
        try:
            for node in nodes:
                node_end = node.get("end_ns") or end
                span_token = self.desired_span.set(int(node["id"], 16))
                try:
                    span = self.tracer.start_span(
                        node["name"],
                        context=contexts[node["parent_id"]],
                        start_time=node["start_ns"],
                    )
                finally:
                    self.desired_span.reset(span_token)
                contexts[node["id"]] = trace.set_span_in_context(span, Context())
                opened.append((span, node_end))
                span.set_attribute("langfuse.observation.type", node["type"])
                span.set_attribute("langfuse.trace.name", "serve-model-request")
                span.set_attribute(
                    "langfuse.environment",
                    os.getenv("OBSERVER_ENVIRONMENT", "production"),
                )
                status = node.get("metadata", {}).get("status", event["status"])
                span.set_attribute("observer.status", status)
                for name in ("service", "model", "instance_id"):
                    if name in event:
                        span.set_attribute(
                            "langfuse.observation.metadata." + name, event[name]
                        )
                for key, value in node.get("metadata", {}).items():
                    span.set_attribute("langfuse.observation.metadata." + key, value)
                if node["type"] == "generation":
                    span.set_attribute(
                        "langfuse.observation.model.name", event.get("model", "unknown")
                    )
                    span.set_attribute(
                        "langfuse.observation.usage_details",
                        json.dumps(node.get("usage", {})),
                    )
                    if node.get("completion_start_ns"):
                        span.set_attribute(
                            "langfuse.observation.completion_start_time",
                            datetime.fromtimestamp(
                                node["completion_start_ns"] / 1e9, timezone.utc
                            ).isoformat(),
                        )
                    span.set_attribute(
                        "langfuse.observation.input",
                        json.dumps(
                            {
                                "input_tokens": node.get("usage", {}).get("input"),
                                "content_captured": False,
                            }
                        ),
                    )
                    span.set_attribute(
                        "langfuse.observation.output",
                        json.dumps(
                            {
                                "output_tokens": node.get("usage", {}).get("output"),
                                "status": status,
                            }
                        ),
                    )
                if node is root:
                    span.set_attribute(
                        "langfuse.observation.input",
                        json.dumps(
                            {
                                "route": event.get("route"),
                                "model": event.get("model"),
                                "content_captured": False,
                            }
                        ),
                    )
                    span.set_attribute(
                        "langfuse.observation.output",
                        json.dumps(
                            {
                                "status": status,
                                "engine_requests": event["engine_requests"],
                            }
                        ),
                    )
                    for name, aggregate in event["stages"].items():
                        for key, value in aggregate.items():
                            span.set_attribute(f"observer.{name}.{key}", value)
                if status != "ok":
                    span.set_status(trace.Status(trace.StatusCode.ERROR, status))
            for span, node_end in reversed(opened):
                span.end(end_time=node_end)
            if self.exporter.export(tuple(completed)) != self.success:
                raise RuntimeError("Langfuse OTLP export failed")
        finally:
            self.desired_id.reset(trace_token)
            self.batch.reset(batch_token)
