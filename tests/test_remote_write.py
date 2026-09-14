import asyncio
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import cramjam
import pytest

from vllm_observer.metrics import MetricsFilter
from vllm_observer.middleware import ObserverMiddleware
from vllm_observer.remote_write import RemoteWriter, WriteRequest


def test_push_http_protocol_heartbeat_and_shutdown():
    batches = []
    statuses = [503, 204]

    class Receiver(BaseHTTPRequestHandler):
        def do_POST(self):
            assert self.path == "/api/v1/write"
            assert self.headers["Content-Encoding"] == "snappy"
            assert self.headers["Content-Type"] == "application/x-protobuf"
            assert self.headers["X-Prometheus-Remote-Write-Version"] == "0.1.0"
            raw = self.rfile.read(int(self.headers["Content-Length"]))
            batch = WriteRequest.FromString(bytes(cramjam.snappy.decompress_raw(raw)))
            batches.append(batch)
            self.send_response(statuses.pop(0) if statuses else 204)
            self.end_headers()

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Receiver)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    metrics = MetricsFilter()
    writer = RemoteWriter(
        metrics, f"http://127.0.0.1:{server.server_port}/api/v1/write", interval=0.01
    )

    async def run():
        async def health():
            return True

        writer.start(health)
        for _ in range(200):
            if len(batches) >= 2:
                break
            await asyncio.sleep(0.01)
        await writer.close()

    try:
        asyncio.run(run())
    finally:
        server.shutdown()
        server.server_close()
        thread.join(2)
    assert len(batches) >= 3
    values = []
    for batch in batches:
        mapping = {}
        for series in batch.timeseries:
            labels = {label.name: label.value for label in series.labels}
            assert list(labels) == sorted(labels)
            assert labels["instance_id"] == metrics.labels[2]
            assert "trace_id" not in labels
            mapping[labels["__name__"]] = series.samples[0]
        values.append(mapping)
    assert values[0]["observer_instance_alive"].value == 1
    assert values[-1]["observer_instance_alive"].value == 0
    assert values[-1]["observer_model_healthy"].value == 0
    assert values[-1]["observer_push_failures_total"].value == 1
    assert (
        values[-1]["observer_heartbeat_timestamp_seconds"].timestamp
        > values[0]["observer_heartbeat_timestamp_seconds"].timestamp
    )


def test_middleware_starts_push_only_after_application_startup(monkeypatch):
    monkeypatch.setenv("OBSERVER_PUSH_ENABLED", "1")
    observations = []

    async def app(scope, receive, send):
        if scope["type"] == "http":
            await send({"type": "http.response.start", "status": 503})
            await send({"type": "http.response.body", "body": b""})
        else:
            await send({"type": "lifespan.startup.complete"})
            await asyncio.sleep(0.03)
            await send({"type": "lifespan.shutdown.complete"})

    middleware = ObserverMiddleware(app, instrument=False)

    def push():
        observations.append(middleware.writer.alive._value.get())

    monkeypatch.setattr(middleware.writer, "push", push)
    assert observations == []

    async def run():
        async def receive():
            return {"type": "lifespan.startup"}

        async def send(message):
            pass

        await middleware({"type": "lifespan"}, receive, send)

    asyncio.run(run())
    assert observations == [1, 0]
    assert middleware.writer.healthy._value.get() == 0


@pytest.mark.parametrize("interval", [0, -1, float("nan"), float("inf")])
def test_invalid_push_interval_rejected(interval):
    with pytest.raises(ValueError):
        RemoteWriter(
            MetricsFilter(), "http://localhost/api/v1/write", interval=interval
        )
