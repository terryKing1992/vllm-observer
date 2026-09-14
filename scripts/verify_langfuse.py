"""Send one demo request through the middleware, then verify Langfuse ingestion.

Credentials are read exclusively from LANGFUSE_* environment variables.
"""

import asyncio
import base64
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import datetime, timedelta, timezone


def main():
    base = os.environ["LANGFUSE_BASE_URL"].rstrip("/")
    auth = base64.b64encode(
        f"{os.environ['LANGFUSE_PUBLIC_KEY']}:{os.environ['LANGFUSE_SECRET_KEY']}".encode()
    ).decode()
    os.environ["OBSERVER_PUSH_ENABLED"] = "0"
    os.environ["OBSERVER_LANGFUSE_ENABLED"] = "1"
    os.environ["OBSERVER_SERVICE"] = "observer-validation"
    os.environ["OBSERVER_MODEL"] = "demo-model"
    os.environ["OBSERVER_INSTANCE_ID"] = "windows-langfuse-check"
    from vllm_observer.demo import app

    identifier = uuid.uuid4().hex
    messages = []

    async def request():
        async def receive():
            return {"type": "http.request", "body": b"{}"}

        async def send(message):
            messages.append(message)

        await app(
            {
                "type": "http",
                "path": "/v1/chat/completions",
                "headers": [(b"x-trace-id", identifier.encode())],
            },
            receive,
            send,
        )

    try:
        asyncio.run(request())
        assert messages[-1]["body"] == b"data: [DONE]\n\n"
        assert (b"x-trace-id", identifier.encode()) in messages[0]["headers"]
        assert app.exporter.close(timeout=10), "Export worker did not finish"
        assert app.exporter.failed == 0, "Langfuse export failed"
    finally:
        app.exporter.close(timeout=10)
        app.exporter.sink.provider.shutdown()
    print(json.dumps({"export": "accepted", "trace_id": identifier}), flush=True)

    now = datetime.now(timezone.utc)
    query = urllib.parse.urlencode(
        {
            "traceId": identifier,
            "fields": "core,basic,metadata",
            "fromStartTime": (now - timedelta(minutes=5)).isoformat(),
            "toStartTime": (now + timedelta(minutes=5)).isoformat(),
            "limit": 10,
        }
    )
    url = base + "/api/public/v2/observations?" + query
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        req = urllib.request.Request(url, headers={"Authorization": "Basic " + auth})
        try:
            with urllib.request.urlopen(req, timeout=10) as response:
                result = json.load(response)
        except urllib.error.HTTPError as error:
            # Output only status, never credentials or request headers.
            raise RuntimeError(
                f"Langfuse read API returned HTTP {error.code}"
            ) from None
        rows = result.get("data", [])
        if rows:
            row = next(row for row in rows if row["traceId"] == identifier)
            assert row["type"].upper() == "GENERATION", row["type"]
            print(
                json.dumps(
                    {
                        "ingestion": "verified",
                        "trace_id": identifier,
                        "observation_id": row["id"],
                        "type": row["type"],
                        "project_id": row.get("projectId"),
                        "metadata": row.get("metadata"),
                    },
                    ensure_ascii=False,
                ),
                flush=True,
            )
            return
        time.sleep(2)
    raise RuntimeError(
        f"Export accepted but observation not visible within 120s: {identifier}"
    )


if __name__ == "__main__":
    main()
