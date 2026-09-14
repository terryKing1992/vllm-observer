import asyncio

from prometheus_client.parser import text_string_to_metric_families

from vllm_observer.demo import application
from vllm_observer.middleware import ObserverMiddleware


def test_demo_stream_and_metrics_exposition():
    middleware = ObserverMiddleware(application, instrument=False)

    async def request(path):
        messages = []

        async def send(message):
            messages.append(message)

        async def receive():
            return {"type": "http.request", "body": b"{}"}

        await middleware({"type": "http", "path": path, "headers": []}, receive, send)
        return messages

    messages = asyncio.run(request("/v1/chat/completions"))
    assert messages[0]["status"] == 200
    assert messages[-1]["body"] == b"data: [DONE]\n\n"
    assert len(messages) == 6
    metrics = asyncio.run(request("/observer/metrics"))[-1]["body"].decode()
    samples = {
        sample.name: sample.value
        for family in text_string_to_metric_families(metrics)
        for sample in family.samples
    }
    assert samples["observer_requests_total"] == 1
    assert samples["observer_requests_inflight"] == 0
    assert samples["observer_instance_info"] == 1
