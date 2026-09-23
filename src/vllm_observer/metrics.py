import os
import socket

from prometheus_client import (
    CollectorRegistry,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
)


class MetricsFilter:
    def __init__(self):
        self.registry = CollectorRegistry()
        self.labels = (
            os.getenv("OBSERVER_SERVICE", "vllm"),
            os.getenv("OBSERVER_MODEL", "unknown"),
            os.getenv("OBSERVER_INSTANCE_ID", socket.gethostname()),
        )
        names = ["service", "model", "instance_id"]
        self.info = Gauge(
            "observer_instance_info",
            "Service replica identity",
            names,
            registry=self.registry,
        )
        self.info.labels(*self.labels).set(1)
        self.inflight = Gauge(
            "observer_requests_inflight",
            "Active HTTP requests",
            names,
            registry=self.registry,
        ).labels(*self.labels)
        self.requests = Counter(
            "observer_requests",
            "Completed HTTP requests",
            names + ["status"],
            registry=self.registry,
        )
        self.duration = Histogram(
            "observer_request_duration_ms",
            "HTTP wall time",
            names,
            registry=self.registry,
            buckets=(100, 500, 1000, 2000, 5000, 10000, 30000, 60000, 120000, 300000),
        )
        self.stage_ms = Counter(
            "observer_stage_ms",
            "Accumulated stage wall time",
            names + ["stage"],
            registry=self.registry,
        )
        self.stage_count = Counter(
            "observer_stage_observations",
            "Stage count; decode counts output tokens after first",
            names + ["stage"],
            registry=self.registry,
        )
        # Distribution (not just totals) per stage. Labels deliberately
        # exclude instance_id to keep series count independent of replicas.
        self.stage_duration = Histogram(
            "observer_stage_duration_ms",
            "Stage wall time distribution (per-request mean)",
            ["stage", "model"],
            registry=self.registry,
            buckets=(1, 5, 10, 50, 100, 250, 500, 1000, 2500, 5000, 10000, 30000),
        )
        # Time-per-output-token distribution and slow-request counter.
        self.tpot = Histogram(
            "observer_tpot_ms",
            "TPOT (decode total / output tokens after first)",
            ["model"],
            registry=self.registry,
            buckets=(5, 10, 25, 50, 100, 150, 250, 500, 1000, 2000),
        )
        self.slow_tpot = Counter(
            "observer_slow_tpot_requests",
            "Requests whose TPOT exceeded the slow threshold",
            ["model"],
            registry=self.registry,
        )
        self.langfuse_unavailable = Gauge(
            "observer_langfuse_enabled_but_unavailable",
            "1 when Langfuse export is enabled but unavailable (loud failure)",
            registry=self.registry,
        )

    def mark_langfuse_unavailable(self):
        self.langfuse_unavailable.set(1)

    def tpot_threshold(self):
        from .exporter import tpot_threshold_seconds

        return tpot_threshold_seconds()

    def attach_exporter(self, exporter):
        for name, getter in [
            ("queue_size", lambda: exporter.queue.qsize()),
            ("dropped_total", lambda: exporter.dropped),
            ("failed_total", lambda: exporter.failed),
        ]:
            Gauge(
                "observer_export_" + name,
                "Background exporter " + name,
                registry=self.registry,
            ).set_function(getter)

    def process(self, event):
        self.requests.labels(*self.labels, event["status"]).inc()
        self.duration.labels(*self.labels).observe(
            event["stages"]["http_total"]["total_seconds"] * 1000.0
        )
        decode = event["stages"].get("decode")
        if decode and decode.get("count"):
            tpot = decode["total_seconds"] / decode["count"]
            self.tpot.labels(self.labels[1]).observe(tpot * 1000.0)
            if tpot > self.tpot_threshold():
                self.slow_tpot.labels(self.labels[1]).inc()
        for name in (
            "queue",
            "prefill",
            "decode",
            "http_total",
            "http_first_body",
            "render_input",
            "input_process",
            "submit_engine",
            "schedule_total",
            "execute_model",
        ):
            if name in event["stages"]:
                value = event["stages"][name]
                self.stage_ms.labels(*self.labels, name).inc(
                    value["total_seconds"] * 1000.0
                )
                self.stage_count.labels(*self.labels, name).inc(value["count"])
                self.stage_duration.labels(name, self.labels[1]).observe(
                    value["mean_seconds"] * 1000.0
                )
        return event

    def render(self):
        return generate_latest(self.registry)
