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
            "observer_request_duration_seconds",
            "HTTP wall time",
            names,
            registry=self.registry,
            buckets=(0.1, 0.5, 1, 2, 5, 10, 30, 60, 120, 300),
        )
        self.stage_seconds = Counter(
            "observer_stage_seconds",
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
            event["stages"]["http_total"]["total_seconds"]
        )
        for name in ("queue", "prefill", "decode", "http_total", "http_first_body"):
            if name in event["stages"]:
                value = event["stages"][name]
                self.stage_seconds.labels(*self.labels, name).inc(
                    value["total_seconds"]
                )
                self.stage_count.labels(*self.labels, name).inc(value["count"])
        return event

    def render(self):
        return generate_latest(self.registry)
