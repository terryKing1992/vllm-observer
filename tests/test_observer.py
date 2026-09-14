import asyncio
import threading
from types import SimpleNamespace

import pytest

from vllm_observer.adapter import install
from vllm_observer.core import FilterChain, RequestTrace, current_request
from vllm_observer.exporter import AsyncExportFilter
from vllm_observer.middleware import ObserverMiddleware, get_trace_id


class Capture:
    def __init__(self):
        self.events = []

    def process(self, event):
        self.events.append(event)
        return event


def test_repeated_decode_is_token_weighted():
    trace = RequestTrace("a" * 32)
    trace.add("decode", 0.3, 3)
    trace.add("decode", 0.2, 1)
    assert trace.finish()["stages"]["decode"] == {
        "count": 4,
        "total_seconds": 0.5,
        "mean_seconds": 0.125,
    }


def test_filter_failure_does_not_stop_following_sink():
    class Broken:
        def process(self, event):
            raise RuntimeError("offline")

    capture = Capture()
    FilterChain([Broken(), capture]).process({"status": "ok"})
    assert len(capture.events) == 1


def test_trace_header_validation():
    assert (
        get_trace_id([(b"traceparent", f"00-{'a' * 32}-{'b' * 16}-01".encode())])
        == "a" * 32
    )
    assert get_trace_id([(b"x-trace-id", b"0" * 32)]) != "0" * 32
    assert len(get_trace_id([(b"x-trace-id", b"arbitrary\ntext")])) == 32


@pytest.mark.parametrize("failure", [None, RuntimeError, asyncio.CancelledError])
def test_stream_lifecycle_and_cleanup(failure):
    capture = Capture()
    messages = []

    async def app(scope, receive, send):
        current_request.get().add("decode", 0.2, 2)
        await send({"type": "http.response.start", "status": 200})
        await send({"type": "http.response.body", "body": b"first", "more_body": True})
        if failure:
            raise failure()
        await send({"type": "http.response.body", "body": b"last"})

    middleware = ObserverMiddleware(app, filters=[capture], instrument=False)

    async def run():
        async def send(message):
            messages.append(message)

        async def receive():
            return {"type": "http.request", "body": b""}

        try:
            await middleware(
                {
                    "type": "http",
                    "path": "/v1/chat/completions",
                    "headers": [(b"x-trace-id", b"a" * 32)],
                },
                receive,
                send,
            )
        finally:
            assert current_request.get() is None

    if failure:
        with pytest.raises(failure):
            asyncio.run(run())
    else:
        asyncio.run(run())
    assert len(capture.events) == 1
    assert capture.events[0]["status"] == (
        "cancelled"
        if failure is asyncio.CancelledError
        else "error"
        if failure
        else "ok"
    )
    assert (b"x-trace-id", b"a" * 32) in messages[0]["headers"]
    assert b"observer_requests_inflight" in middleware.metrics.render()


def test_bounded_export_drops_without_blocking():
    entered, release = threading.Event(), threading.Event()

    def sink(event):
        entered.set()
        release.wait(3)

    exporter = AsyncExportFilter(sink, capacity=1)
    try:
        exporter.process({})
        assert entered.wait(1)
        exporter.process({})
        exporter.process({})
        assert exporter.dropped == 1
    finally:
        release.set()
        assert exporter.close()


def test_adapter_correlates_background_stats_and_is_idempotent():
    class Stats:
        def __init__(self):
            self.finished_requests = []

        def update_from_finished_request(self, request_id):
            self.finished_requests.append(
                SimpleNamespace(
                    request_id=request_id,
                    queued_time=0.1,
                    prefill_time=0.2,
                    decode_time=0.6,
                    num_generation_tokens=4,
                )
            )

    class Engine:
        async def generate(self, prompt, sampling_params, request_id):
            # Real output handler runs with a different context.
            token = current_request.set(None)
            try:
                Stats().update_from_finished_request(request_id)
            finally:
                current_request.reset(token)
            yield request_id

    install(Engine, Stats)
    wrapped = Engine.generate
    install(Engine, Stats)
    assert Engine.generate is wrapped

    async def run():
        async def request(identifier):
            trace = RequestTrace(identifier)
            token = current_request.set(trace)
            try:
                assert [x async for x in Engine().generate(None, None, identifier)] == [
                    identifier
                ]
            finally:
                current_request.reset(token)
            return trace.finish()

        return await asyncio.gather(request("a" * 32), request("b" * 32))

    events = asyncio.run(run())
    assert all(e["engine_requests"] == 1 for e in events)
    assert all(
        e["stages"]["decode"]["mean_seconds"] == pytest.approx(0.2) for e in events
    )
