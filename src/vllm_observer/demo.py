"""Windows/Linux demo: uvicorn vllm_observer.demo:app --port 8000."""

import asyncio
import json
import logging

from .core import current_request
from .middleware import ObserverMiddleware

logging.basicConfig(level=logging.INFO, format="%(message)s")


async def application(scope, receive, send):
    if scope["type"] == "lifespan":
        while True:
            message = await receive()
            if message["type"] == "lifespan.startup":
                await send({"type": "lifespan.startup.complete"})
            else:
                await send({"type": "lifespan.shutdown.complete"})
                return
    if scope["path"] == "/health":
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"OK"})
        return
    if scope["path"] != "/v1/chat/completions":
        await send({"type": "http.response.start", "status": 404, "headers": []})
        await send({"type": "http.response.body", "body": b"Not found"})
        return
    while True:
        message = await receive()
        if message["type"] == "http.disconnect":
            return
        if not message.get("more_body", False):
            break
    trace = current_request.get()
    generation = trace.begin_generation("demo-generation")
    generation["usage"] = {"input": 8, "output": 4}
    for name, duration in [("queue", 0.01), ("prefill", 0.02)]:
        start = trace.now_ns()
        await asyncio.sleep(duration)
        end = trace.now_ns()
        measured = (end - start) / 1e9
        trace.add(name, measured)
        trace.interval(generation, name, start, end, count=1,
                       total_seconds=measured, mean_seconds=measured,
                       timing_source="demo_measured_wall_interval")
    generation["completion_start_ns"] = trace.now_ns()
    trace.engine_requests = 1
    await send(
        {
            "type": "http.response.start",
            "status": 200,
            "headers": [(b"content-type", b"text/event-stream")],
        }
    )
    decode_start = trace.now_ns()
    for index in range(4):
        if index:
            await asyncio.sleep(0.01)
        data = json.dumps({"choices": [{"delta": {"content": str(index)}}]})
        await send(
            {
                "type": "http.response.body",
                "body": f"data: {data}\n\n".encode(),
                "more_body": True,
            }
        )
    decode_end = trace.now_ns()
    measured = (decode_end - decode_start) / 1e9
    trace.add("decode", measured, 3)
    trace.interval(generation, "decode", decode_start, decode_end, count=3,
                   total_seconds=measured, mean_seconds=measured / 3,
                   timing_source="demo_measured_wall_interval")
    generation["end_ns"] = trace.now_ns()
    await send({"type": "http.response.body", "body": b"data: [DONE]\n\n"})


app = ObserverMiddleware(application, instrument=False)
