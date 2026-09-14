"""Prometheus Remote Write 1.0: instance-initiated protobuf/Snappy POST."""

import asyncio
import logging
import math
import os
import time
import urllib.request

import cramjam
from google.protobuf import descriptor_pb2, descriptor_pool, message_factory
from prometheus_client import Counter, Gauge

logger = logging.getLogger("vllm_observer")


def _write_request_type():
    # Wire-compatible subset of prometheus/prompb/{remote,types}.proto.
    schema = descriptor_pb2.FileDescriptorProto(
        name="observer_remote.proto", syntax="proto3"
    )
    for name, fields in {
        "Label": [("name", 1, 9, False, None), ("value", 2, 9, False, None)],
        "Sample": [("value", 1, 1, False, None), ("timestamp", 2, 3, False, None)],
        "TimeSeries": [
            ("labels", 1, 11, True, ".Label"),
            ("samples", 2, 11, True, ".Sample"),
        ],
        "WriteRequest": [("timeseries", 1, 11, True, ".TimeSeries")],
    }.items():
        message = schema.message_type.add(name=name)
        for field_name, number, kind, repeated, type_name in fields:
            field = message.field.add(
                name=field_name, number=number, type=kind, label=3 if repeated else 1
            )
            if type_name:
                field.type_name = type_name
    pool = descriptor_pool.DescriptorPool()
    pool.Add(schema)
    return message_factory.GetMessageClass(pool.FindMessageTypeByName("WriteRequest"))


WriteRequest = _write_request_type()


def encode_registry(registry, identity, timestamp_ms):
    request = WriteRequest()
    for metric in registry.collect():
        for sample in metric.samples:
            labels = {**sample.labels, **identity, "__name__": sample.name}
            series = request.timeseries.add()
            for name, value in sorted(labels.items()):
                series.labels.add(name=name, value=str(value))
            series.samples.add(value=sample.value, timestamp=timestamp_ms)
    return bytes(cramjam.snappy.compress_raw(request.SerializeToString()))


class RemoteWriter:
    def __init__(self, metrics, url, *, interval=10.0, timeout=2.0):
        if not all(math.isfinite(x) and x > 0 for x in (interval, timeout)):
            raise ValueError(
                "Push interval and timeout must be positive finite seconds"
            )
        if not url.startswith(("http://", "https://")):
            raise ValueError("Remote Write URL must use HTTP(S)")
        self.metrics = metrics
        self.url = url
        self.interval = interval
        self.timeout = timeout
        self.identity = dict(zip(("service", "model", "instance_id"), metrics.labels))
        self.identity["job"] = "observer"
        self.last_timestamp = 0
        self.stop = asyncio.Event()
        self.task = None
        self.heartbeat = Gauge(
            "observer_heartbeat_timestamp_seconds",
            "Last attempted heartbeat; judge freshness in Prometheus",
            registry=metrics.registry,
        )
        self.alive = Gauge(
            "observer_instance_alive",
            "Instance lifecycle state",
            registry=metrics.registry,
        )
        self.healthy = Gauge(
            "observer_model_healthy",
            "Local application /health result",
            registry=metrics.registry,
        )
        self.failures = Counter(
            "observer_push_failures", "Failed metric pushes", registry=metrics.registry
        )

    @classmethod
    def from_env(cls, metrics):
        return cls(
            metrics,
            os.getenv(
                "OBSERVER_REMOTE_WRITE_URL", "http://localhost:9090/api/v1/write"
            ),
            interval=float(os.getenv("OBSERVER_PUSH_INTERVAL_SECONDS", "10")),
            timeout=float(os.getenv("OBSERVER_PUSH_TIMEOUT_SECONDS", "2")),
        )

    def push(self):
        try:
            self._push_snapshot()
        except Exception:
            self.failures.inc()
            logger.warning(
                "Metric push failed; will send a fresh snapshot next cycle",
                exc_info=True,
            )

    def _push_snapshot(self):
        self.heartbeat.set(time.time())
        self.last_timestamp = max(self.last_timestamp + 1, time.time_ns() // 1_000_000)
        body = encode_registry(
            self.metrics.registry, self.identity, self.last_timestamp
        )
        headers = {
            "Content-Type": "application/x-protobuf",
            "Content-Encoding": "snappy",
            "X-Prometheus-Remote-Write-Version": "0.1.0",
            "User-Agent": "vllm-observer/0.1.0",
        }
        token = os.getenv("OBSERVER_REMOTE_WRITE_TOKEN")
        if token:
            headers["Authorization"] = "Bearer " + token
        request = urllib.request.Request(
            self.url, data=body, headers=headers, method="POST"
        )
        with urllib.request.urlopen(request, timeout=self.timeout) as response:
            if not 200 <= response.status < 300:
                raise RuntimeError("Remote Write rejected the batch")

    def start(self, health_check):
        if self.task is None:
            self.alive.set(1)
            self.task = asyncio.create_task(self._run(health_check))

    async def _run(self, health_check):
        while not self.stop.is_set():
            try:
                healthy = await asyncio.wait_for(health_check(), self.timeout)
            except Exception:
                healthy = False
            self.healthy.set(int(healthy))
            await asyncio.to_thread(self.push)
            try:
                await asyncio.wait_for(self.stop.wait(), self.interval)
            except asyncio.TimeoutError:
                pass
        self.alive.set(0)
        self.healthy.set(0)
        await asyncio.to_thread(self.push)

    async def close(self):
        self.stop.set()
        if self.task is not None:
            await self.task
