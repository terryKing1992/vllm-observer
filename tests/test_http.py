"""Real localhost HTTP transport; no model or remote service required."""

import socket
import threading
import time
import urllib.request

import uvicorn

from vllm_observer.demo import application
from vllm_observer.middleware import ObserverMiddleware


def test_live_http_stream_and_metrics():
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    server = uvicorn.Server(
        uvicorn.Config(
            ObserverMiddleware(application, instrument=False), log_level="error"
        )
    )
    thread = threading.Thread(
        target=server.run, kwargs={"sockets": [sock]}, daemon=True
    )
    thread.start()
    try:
        deadline = time.monotonic() + 5
        while not server.started and thread.is_alive() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert server.started
        base = f"http://127.0.0.1:{port}"
        request = urllib.request.Request(
            base + "/v1/chat/completions",
            data=b"{}",
            headers={"Content-Type": "application/json", "x-trace-id": "d" * 32},
        )
        with urllib.request.urlopen(request, timeout=5) as response:
            assert response.headers["x-trace-id"] == "d" * 32
            assert response.read().endswith(b"data: [DONE]\n\n")
        with urllib.request.urlopen(base + "/observer/metrics", timeout=5) as response:
            assert b"observer_requests_total" in response.read()
        with urllib.request.urlopen(base + "/health", timeout=5) as response:
            assert response.status == 200
    finally:
        server.should_exit = True
        thread.join(5)
        sock.close()
    assert not thread.is_alive()
