"""Bounded background export; no network calls on the inference path."""

import base64
import contextvars
import copy
import json
import logging
import os
import queue
import random
import threading
import uuid
from datetime import datetime, timezone

logger = logging.getLogger("vllm_observer")

TIMELINE_MODULE = "vllm-observer.timeline"

# Exported span names are prefixed with the process/module that owns the
# stage so the Langfuse waterfall reads api-server.* vs engine-core.*
# directly. Root and generation keep their semantic names.
MODULE_BY_STAGE = {
    "api_prepare": "api-server",
    "render_input": "api-server",
    "sampling_params": "api-server",
    "input_process": "api-server",
    "submit_engine": "api-server",
    "output_processor_add": "api-server",
    "response_transfer": "api-server",
    "engine_handoff": "engine-core",
    "waiting_to_first_schedule": "engine-core",
    "prefill": "engine-core",
    "decode": "engine-core",
    "engine_finalize": "engine-core",
}


def module_prefixed(name):
    module = MODULE_BY_STAGE.get(name)
    return f"{module}.{name}" if module else name


def langfuse_sample_rate():
    """Head-sampling rate for normal requests (errors are always kept)."""
    raw = os.getenv("OBSERVER_LANGFUSE_SAMPLE_RATE", "0.01")
    try:
        rate = float(raw)
    except (TypeError, ValueError):
        logger.error(
            "Invalid OBSERVER_LANGFUSE_SAMPLE_RATE %r; falling back to 0.01", raw
        )
        return 0.01
    if not 0.0 <= rate <= 1.0:
        logger.error(
            "OBSERVER_LANGFUSE_SAMPLE_RATE %r out of range; falling back to 0.01", raw
        )
        return 0.01
    return rate


def tpot_threshold_seconds():
    """Slow-TPOT threshold in seconds. Config is milliseconds
    (OBSERVER_TPOT_SLOW_MS, default 100); legacy seconds variable kept
    as a fallback for existing deployments."""
    raw = os.getenv("OBSERVER_TPOT_SLOW_MS") or os.getenv(
        "OBSERVER_TPOT_SLOW_SECONDS", "100"
    )
    try:
        value = float(raw)
    except (TypeError, ValueError):
        logger.error("Invalid TPOT slow threshold %r; using 100ms", raw)
        return 0.1
    if value <= 0:
        return 0.1
    legacy_seconds = "OBSERVER_TPOT_SLOW_MS" not in os.environ and raw != "100"
    return value if legacy_seconds else value * 0.001


def to_milliseconds(key, value):
    """Rename a duration attribute key to milliseconds with converted value.

    Human-facing Langfuse attributes use ms exclusively; Prometheus metrics
    intentionally stay in seconds (naming convention).
    """
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return key, value
    if key.endswith("_seconds"):
        return key[: -len("_seconds")] + "_ms", value * 1000.0
    if key.endswith("_ns"):
        return key[: -len("_ns")] + "_ms", value / 1e6
    return key, value


def milliseconds_metadata(metadata):
    return dict(to_milliseconds(k, v) for k, v in metadata.items())


def langfuse_base_url():
    base = (
        os.getenv("LANGFUSE_BASE_URL") or os.getenv("LANGFUSE_HOST") or ""
    ).rstrip("/")
    if not base:
        raise ValueError(
            "Langfuse export enabled but LANGFUSE_BASE_URL/LANGFUSE_HOST is unset"
        )
    return base


def _segment(node_id, parent_id, name, start_ns, end_ns, **metadata):
    return {
        "id": node_id,
        "parent_id": parent_id,
        "name": name,
        "type": "span",
        "start_ns": start_ns,
        "end_ns": end_ns,
        "metadata": {
            "timing_source": "boundary_partition",
            "module": TIMELINE_MODULE,
            **metadata,
        },
    }


def _timeline_nodes(event, root, generation):
    """Build a strict MECE timeline.

    Top-level segments share boundaries and therefore always sum to the
    request total. Overlapping diagnostics (TTFT, execute_model, scheduler
    totals) stay metadata-only instead of becoming spans.
    """
    start, end = root["start_ns"], root["end_ns"]
    gen_start = generation["start_ns"] if generation else end
    gen_end = (generation.get("end_ns") or end) if generation else end
    engine = {
        node["name"]: node
        for node in event.get("observations", [])
        if node["name"] in ("queue", "waiting_to_first_schedule", "prefill", "decode")
    }
    waiting = engine.get("waiting_to_first_schedule") or engine.get("queue")
    prefill = engine.get("prefill")
    decode = engine.get("decode")

    def clamp(value, lo, hi):
        return max(lo, min(value, hi))

    api = _segment(
        uuid.uuid4().hex[:16],
        root["id"],
        module_prefixed("api_prepare"),
        start,
        gen_start,
        note="routing + render + sampling + submit; details nested",
    )
    transfer = _segment(
        uuid.uuid4().hex[:16],
        root["id"],
        module_prefixed("response_transfer"),
        gen_end,
        end,
        note="detokenize + SSE flush + response completion",
    )
    nodes = [root, api]
    if transfer["end_ns"] > transfer["start_ns"]:
        nodes.append(transfer)
    containers = [api, transfer]

    if generation is not None:
        generation = dict(generation)
        generation["parent_id"] = root["id"]
        nodes.append(generation)
        w_start = waiting["start_ns"] if waiting else gen_start
        p_start = prefill["start_ns"] if prefill else w_start
        d_start = decode["start_ns"] if decode else p_start
        d_end = decode["end_ns"] if decode else (prefill or {}).get("end_ns", d_start)
        boundaries = [
            (
                module_prefixed("engine_handoff"),
                gen_start,
                clamp(w_start, gen_start, gen_end),
            ),
            (
                module_prefixed("waiting_to_first_schedule"),
                clamp(w_start, gen_start, gen_end),
                clamp(p_start, gen_start, gen_end),
            ),
            (
                module_prefixed("prefill"),
                clamp(p_start, gen_start, gen_end),
                clamp(d_start, gen_start, gen_end),
            ),
            (
                module_prefixed("decode"),
                clamp(d_start, gen_start, gen_end),
                clamp(d_end, gen_start, gen_end),
            ),
            (
                module_prefixed("engine_finalize"),
                clamp(d_end, gen_start, gen_end),
                gen_end,
            ),
        ]
        seen = [gen_start]
        generation_segments = []
        for name, seg_start, seg_end in boundaries:
            # Never allow a segment to start before the previous boundary.
            seg_start = max(seg_start, seen[-1])
            seg_end = max(seg_start, min(seg_end, gen_end))
            seen.append(seg_end)
            if seg_end <= seg_start:
                continue
            extra = {}
            if name == module_prefixed("waiting_to_first_schedule") and waiting:
                extra = dict(waiting.get("metadata", {}))
                extra.pop("timing_source", None)
                queue_meta = (engine.get("queue") or {}).get("metadata", {})
                if "total_seconds" in queue_meta:
                    extra["queue_total_seconds"] = queue_meta["total_seconds"]
            if name == module_prefixed("prefill") and prefill:
                extra = {
                    k: v
                    for k, v in prefill.get("metadata", {}).items()
                    if k != "timing_source"
                }
            if name == module_prefixed("decode") and decode:
                extra = {
                    k: v
                    for k, v in decode.get("metadata", {}).items()
                    if k != "timing_source"
                }
            segment = _segment(
                uuid.uuid4().hex[:16],
                generation["id"],
                name,
                seg_start,
                seg_end,
                **extra,
            )
            generation_segments.append(segment)
            nodes.append(segment)
        containers.extend(generation_segments)

    # API detail intervals nest inside whichever timeline segment contains
    # them and are clipped to that segment, preserving the partition.
    assigned = {}
    for node in event.get("api_intervals", []):
        detail = dict(node)
        midpoint = (detail["start_ns"] + detail["end_ns"]) // 2
        container = next(
            (
                segment
                for segment in containers
                if segment["start_ns"] <= midpoint < segment["end_ns"]
            ),
            None,
        )
        if container is None:
            continue
        detail["start_ns"] = max(detail["start_ns"], container["start_ns"])
        detail["end_ns"] = min(detail["end_ns"], container["end_ns"])
        if detail["end_ns"] <= detail["start_ns"]:
            continue
        detail["parent_id"] = container["id"]
        assigned[detail["name"]] = detail
        detail["name"] = module_prefixed(detail["name"])
        nodes.append(detail)
    submit = assigned.get("submit_engine")
    for node in event.get("api_intervals", []):
        if node["name"] not in ("input_process", "output_processor_add"):
            continue
        detail = assigned.get(node["name"])
        if detail is None or submit is None:
            continue
        if not (
            submit["start_ns"] <= detail["start_ns"]
            and detail["end_ns"] <= submit["end_ns"]
        ):
            continue
        detail["parent_id"] = submit["id"]
    # Parents must be created before children for OTel context setup.
    # Parents must be created before children for OTel context setup;
    # on equal start timestamps the enclosing (longer) segment comes first.
    nodes[1:] = sorted(
        nodes[1:],
        key=lambda node: (node["start_ns"], -(node["end_ns"] - node["start_ns"])),
    )
    return nodes


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

        base = langfuse_base_url()
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
        # Head sampling: errors are always kept; normal requests are sampled.
        # The decision never affects Prometheus metrics (separate filter), so
        # monitoring stays unbiased; Langfuse is single-request drill-down only.
        if event["status"] == "ok":
            if random.random() >= langfuse_sample_rate():
                return
            sampled = "normal"
        else:
            sampled = "error"
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
        generation = next(
            (
                node
                for node in event.get("observations", [])
                if node["type"] == "generation"
            ),
            None,
        )
        nodes = _timeline_nodes(event, root, generation)
        # Timestamp validation: drop nodes with non-positive duration instead
        # of sending invalid spans; report via log so it stays observable.
        valid_nodes = []
        invalid_nodes = 0
        for node in nodes:
            if (node.get("end_ns") or end) > node["start_ns"]:
                valid_nodes.append(node)
            else:
                invalid_nodes += 1
        if invalid_nodes:
            logger.warning(
                "Langfuse timeline dropped %d invalid node(s) for trace %s",
                invalid_nodes,
                event["trace_id"],
            )
        nodes = valid_nodes
        # Carry the engine clock calibration residual as an explicit error
        # boundary on the trace so waterfall readers know its confidence.
        clock_residual_ns = max(
            (
                int(value)
                for node in event.get("observations", [])
                for value in [node.get("metadata", {}).get("calibration_error_ns")]
                if value is not None
            ),
            default=0,
        )
        request_payload = event.get("request_payload")
        response_text = event.get("response_text")
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
                for key, value in milliseconds_metadata(
                    node.get("metadata", {})
                ).items():
                    span.set_attribute("langfuse.observation.metadata." + key, value)
                # Expose the native trace ID in every node's Langfuse details.
                # Set it last so custom metadata cannot override the identity.
                span.set_attribute(
                    "langfuse.observation.metadata.traceId", event["trace_id"]
                )
                if node["type"] == "generation":
                    span.set_attribute(
                        "langfuse.observation.model.name", event.get("model", "unknown")
                    )
                    span.set_attribute(
                        "gen_ai.request.model", event.get("model", "unknown")
                    )
                    usage = node.get("usage") or {}
                    if "input" in usage:
                        span.set_attribute("gen_ai.usage.prompt_tokens", usage["input"])
                    if "output" in usage:
                        span.set_attribute(
                            "gen_ai.usage.completion_tokens", usage["output"]
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
                        json.dumps(request_payload, ensure_ascii=False)
                        if request_payload is not None
                        else json.dumps(
                            {"input_tokens": node.get("usage", {}).get("input")}
                        ),
                    )
                    span.set_attribute(
                        "langfuse.observation.output",
                        json.dumps(
                            {
                                "output_tokens": node.get("usage", {}).get("output"),
                                "content": response_text,
                                "status": status,
                            },
                            ensure_ascii=False,
                        ),
                    )
                    if isinstance(request_payload, dict):
                        parameters = {
                            key: request_payload.get(key)
                            for key in (
                                "temperature",
                                "top_p",
                                "top_k",
                                "max_tokens",
                                "stream",
                                "n",
                                "stop",
                            )
                            if key in request_payload
                        }
                        if parameters:
                            span.set_attribute(
                                "langfuse.observation.model.parameters",
                                json.dumps(parameters),
                            )
                if node is root:
                    if event.get("user_id"):
                        span.set_attribute("langfuse.user.id", event["user_id"])
                    if event.get("session_id"):
                        span.set_attribute("langfuse.session.id", event["session_id"])
                    span.set_attribute(
                        "langfuse.trace.tags",
                        [
                            value
                            for value in (
                                event.get("service"),
                                event.get("model"),
                                event.get("instance_id"),
                            )
                            if value
                        ]
                        + [f"sampled={sampled}"],
                    )
                    if clock_residual_ns:
                        span.set_attribute(
                            "observer.clock_calibration_error_ms",
                            clock_residual_ns / 1e6,
                        )
                    span.set_attribute(
                        "langfuse.observation.input",
                        json.dumps(request_payload or {}, ensure_ascii=False),
                    )
                    span.set_attribute(
                        "langfuse.observation.output",
                        json.dumps(
                            {
                                "status": status,
                                "engine_requests": event["engine_requests"],
                                "content": response_text,
                            },
                            ensure_ascii=False,
                        ),
                    )
                    for name, aggregate in event["stages"].items():
                        for key, value in aggregate.items():
                            key, value = to_milliseconds(key, value)
                            span.set_attribute(f"observer.{name}.{key}", value)
                    if event.get("first_body_ns"):
                        span.set_attribute(
                            "observer.ttft_ms",
                            (event["first_body_ns"] - start) / 1e6,
                        )
                    span.set_attribute(
                        "observer.timeline_partition",
                        "api_prepare + generate-response + response_transfer = total",
                    )
                    decode_agg = event["stages"].get("decode")
                    if decode_agg and decode_agg.get("count"):
                        tpot = decode_agg["total_seconds"] / decode_agg["count"]
                        span.set_attribute("observer.tpot_ms", tpot * 1000.0)
                        threshold = tpot_threshold_seconds()
                        if tpot > threshold:
                            span.set_attribute("observer.slow_tpot", True)
                            span.set_attribute(
                                "observer.tpot_threshold_ms", threshold * 1000.0
                            )
                            waiting_node = next(
                                (
                                    node
                                    for node in event.get("observations", [])
                                    if node.get("name")
                                    in ("waiting_to_first_schedule", "queue")
                                ),
                                None,
                            )
                            details = {}
                            if waiting_node is not None:
                                details["waiting"] = milliseconds_metadata(
                                    waiting_node.get("metadata", {})
                                )
                            for name in (
                                "queue",
                                "schedule_total",
                                "execute_model",
                                "prefill",
                            ):
                                if name in event["stages"]:
                                    details[name] = milliseconds_metadata(
                                        event["stages"][name]
                                    )
                            span.set_attribute(
                                "observer.slow_tpot_details",
                                json.dumps(details, ensure_ascii=False),
                            )
                if status != "ok":
                    span.set_status(trace.Status(trace.StatusCode.ERROR, status))
            for span, node_end in reversed(opened):
                span.end(end_time=node_end)
            if self.exporter.export(tuple(completed)) != self.success:
                raise RuntimeError("Langfuse OTLP export failed")
        finally:
            self.desired_id.reset(trace_token)
            self.batch.reset(batch_token)
