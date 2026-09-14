import asyncio
import logging
import os
import re
import time
import uuid

from .core import ConsoleFilter, FilterChain, RequestTrace, current_request
from .metrics import MetricsFilter

logger = logging.getLogger("vllm_observer")
TRACE_ID = re.compile(r"^[0-9a-f]{32}$")
TRACE_PARENT = re.compile(r"^00-([0-9a-f]{32})-([0-9a-f]{16})-[0-9a-f]{2}$")
PATHS = {"/v1/chat/completions", "/v1/completions", "/v1/responses"}


def get_trace_id(headers):
    values = {k.lower(): v.decode("latin1") for k, v in headers}
    parent = TRACE_PARENT.fullmatch(values.get(b"traceparent", ""))
    if parent and int(parent[1], 16) and int(parent[2], 16):
        return parent[1]
    candidate = values.get(b"x-trace-id", "")
    if TRACE_ID.fullmatch(candidate) and int(candidate, 16):
        return candidate
    return uuid.uuid4().hex


class ObserverMiddleware:
    def __init__(self, app, *, metrics=None, filters=None, instrument=True):
        self.app = app
        self.metrics = metrics or MetricsFilter()
        self.exporter = None
        # Validate optional hooks/config before creating any background exporter.
        if instrument and os.getenv("OBSERVER_ENGINE_ENABLED", "1") == "1":
            from .adapter import install

            install()
        self.writer = None
        if os.getenv("OBSERVER_PUSH_ENABLED", "1") == "1":
            from .remote_write import RemoteWriter

            self.writer = RemoteWriter.from_env(self.metrics)
        if filters is None:
            filters = [self.metrics, ConsoleFilter()]
            if os.getenv("OBSERVER_LANGFUSE_ENABLED", "0") == "1":
                from .exporter import AsyncExportFilter, LangfuseSink

                self.exporter = AsyncExportFilter(LangfuseSink())
                self.metrics.attach_exporter(self.exporter)
                filters.append(self.exporter)
        self.chain = FilterChain(filters)

    async def health_check(self):
        status = 503

        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(message):
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]

        await self.app(
            {
                "type": "http",
                "asgi": {"version": "3.0"},
                "http_version": "1.1",
                "method": "GET",
                "scheme": "http",
                "path": "/health",
                "raw_path": b"/health",
                "query_string": b"",
                "root_path": "",
                "headers": [],
                "client": ("127.0.0.1", 0),
                "server": ("localhost", 80),
            },
            receive,
            send,
        )
        return 200 <= status < 300

    async def __call__(self, scope, receive, send):
        if scope["type"] == "lifespan":

            async def lifecycle_send(message):
                if message["type"] == "lifespan.startup.complete" and self.writer:
                    self.writer.start(self.health_check)
                if message["type"] == "lifespan.shutdown.complete" and self.writer:
                    await self.writer.close()
                await send(message)

            try:
                await self.app(scope, receive, lifecycle_send)
            finally:
                if self.writer:
                    await self.writer.close()
                if self.exporter:
                    await asyncio.to_thread(self.exporter.close)
            return
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        if scope["path"] == "/observer/metrics":
            body = self.metrics.render()
            await send(
                {
                    "type": "http.response.start",
                    "status": 200,
                    "headers": [
                        (b"content-type", b"text/plain; version=0.0.4; charset=utf-8")
                    ],
                }
            )
            await send({"type": "http.response.body", "body": body})
            return
        if scope["path"] not in PATHS:
            return await self.app(scope, receive, send)
        trace = RequestTrace(get_trace_id(scope.get("headers", [])))
        trace.route = scope["path"]
        for key, value in scope.get("headers", []):
            if key.lower() == b"traceparent":
                parent = TRACE_PARENT.fullmatch(value.decode("latin1"))
                if parent and parent[1] == trace.trace_id and int(parent[2], 16):
                    trace.parent_span_id = parent[2]
        token = current_request.set(trace)
        self.metrics.inflight.inc()
        first_body = False
        complete = False

        async def traced_receive():
            message = await receive()
            if message["type"] == "http.disconnect":
                trace.status = "cancelled"
            return message

        async def traced_send(message):
            nonlocal first_body, complete
            if message["type"] == "http.response.start":
                if message["status"] >= 400:
                    trace.status = "error"
                message = dict(message)
                message["headers"] = [
                    (k, v)
                    for k, v in message.get("headers", [])
                    if k.lower() != b"x-trace-id"
                ]
                message["headers"].append((b"x-trace-id", trace.trace_id.encode()))
            if message["type"] == "http.response.body":
                if message.get("body") and not first_body:
                    trace.add("http_first_body", time.perf_counter() - trace.started)
                    first_body = True
                complete = not message.get("more_body", False)
            await send(message)

        try:
            await self.app(scope, traced_receive, traced_send)
            if not complete and trace.status == "ok":
                trace.status = "cancelled"
        except asyncio.CancelledError:
            trace.status = "cancelled"
            raise
        except Exception:
            trace.status = "error"
            raise
        finally:
            current_request.reset(token)
            self.metrics.inflight.dec()
            event = trace.finish()
            event.update(zip(("service", "model", "instance_id"), self.metrics.labels))
            self.chain.process(event)
