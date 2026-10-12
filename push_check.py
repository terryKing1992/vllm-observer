import asyncio, json, logging, os, uuid
os.environ.update(
    OBSERVER_PUSH_ENABLED="1",
    OBSERVER_REMOTE_WRITE_URL="http://localhost:19090/api/v1/write",
    OBSERVER_LANGFUSE_ENABLED="0",
    OBSERVER_SERVICE="prometheus-check",
    OBSERVER_MODEL="demo-model",
)
from vllm_observer.demo import app

async def lifespan():
    started = asyncio.Event()
    async def receive():
        await started.wait()
        return {"type": "lifespan.shutdown"}
    async def send(m):
        if m["type"] == "lifespan.startup.complete":
            started.set()
    t = asyncio.create_task(app({"type": "lifespan"}, receive, send))
    await started.wait()
    return t

async def one():
    identifier = uuid.uuid4().hex
    messages = []
    async def receive():
        return {"type": "http.request", "body": b"{}"}
    async def send(m):
        messages.append(m)
    await app({"type": "http", "path": "/v1/chat/completions",
               "headers": [(b"x-trace-id", identifier.encode())]}, receive, send)

async def main():
    logging.getLogger("vllm_observer").setLevel(logging.ERROR)
    await lifespan()
    for _ in range(5):
        await one()
    await asyncio.sleep(12)
    print(json.dumps({"last_push_timestamp": app.writer.last_timestamp}))

asyncio.run(main())
