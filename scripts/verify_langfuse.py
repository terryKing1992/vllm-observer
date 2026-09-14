"""Send one demo trace and audit its timeline through the official Langfuse CLI.

Credentials are read only from LANGFUSE_* environment variables. Use --trace-id
to re-audit an existing demo trace without creating another request.
"""

import argparse
import asyncio
import json
import os
import shutil
import subprocess
import time
import uuid
from datetime import datetime, timedelta, timezone


def audit_observations(rows, identifier):
    expected = {
        "serve-model-request",
        "generate-response",
        "queue",
        "prefill",
        "decode",
    }
    assert len(rows) == 5 and {row["name"] for row in rows} == expected
    nodes = {row["name"]: row for row in rows}
    root, generation = nodes["serve-model-request"], nodes["generate-response"]
    assert root["type"] == "SPAN" and not root.get("parentObservationId")
    assert generation["type"] == "GENERATION"
    assert generation["parentObservationId"] == root["id"]
    assert generation["model"] == "demo-model"
    assert generation["usageDetails"]["input"] == 8
    assert generation["usageDetails"]["output"] == 4
    by_id = {row["id"]: row for row in rows}
    for row in rows:
        assert row["traceId"] == identifier
        start = datetime.fromisoformat(row["startTime"].replace("Z", "+00:00"))
        end = datetime.fromisoformat(row["endTime"].replace("Z", "+00:00"))
        assert end >= start
        parent = by_id.get(row.get("parentObservationId"))
        if parent:
            assert (
                datetime.fromisoformat(parent["startTime"].replace("Z", "+00:00"))
                <= start
            )
            assert end <= datetime.fromisoformat(
                parent["endTime"].replace("Z", "+00:00")
            )
    for name in ("queue", "prefill", "decode"):
        assert nodes[name]["type"] == "SPAN"
        assert nodes[name]["parentObservationId"] == generation["id"]
    decode = nodes["decode"]["metadata"]
    assert int(decode["count"]) == 3
    assert (
        abs(float(decode["total_seconds"]) / 3 - float(decode["mean_seconds"])) < 1e-8
    )
    return nodes


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--trace-id", help="Audit an existing demo trace only")
    args = parser.parse_args()
    base = os.environ["LANGFUSE_BASE_URL"].rstrip("/")
    os.environ["LANGFUSE_HOST"] = base
    identifier = args.trace_id or uuid.uuid4().hex
    if not args.trace_id:
        os.environ.update(
            OBSERVER_PUSH_ENABLED="0",
            OBSERVER_LANGFUSE_ENABLED="1",
            OBSERVER_SERVICE="observer-validation",
            OBSERVER_MODEL="demo-model",
            OBSERVER_INSTANCE_ID="windows-langfuse-check",
            OBSERVER_ENVIRONMENT="development",
        )
        from vllm_observer.demo import app

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

    npx = shutil.which("npx.cmd") or shutil.which("npx")
    if not npx:
        raise RuntimeError("Install Node.js to use the official langfuse-cli audit")
    now = datetime.now(timezone.utc)
    command = [
        npx,
        "--yes",
        "langfuse-cli@latest",
        "api",
        "observations",
        "list",
        "--trace-id",
        identifier,
        "--fields",
        "core,basic,metadata,model,usage,io,time",
        "--from-start-time",
        (now - timedelta(days=1)).isoformat(),
        "--to-start-time",
        (now + timedelta(minutes=5)).isoformat(),
        "--limit",
        "100",
        "--json",
    ]
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        result = subprocess.run(
            command, capture_output=True, text=True, encoding="utf-8", timeout=45
        )
        if result.returncode:
            raise RuntimeError(f"Langfuse CLI read failed (exit {result.returncode})")
        envelope = json.loads(result.stdout)
        body = envelope.get("body", envelope)
        rows = body.get("data", [])
        if len(rows) >= 5:
            nodes = audit_observations(rows, identifier)
            project = nodes["serve-model-request"]["projectId"]
            print(
                json.dumps(
                    {
                        "ingestion": "hierarchy_verified",
                        "trace_id": identifier,
                        "trace_url": f"{base}/project/{project}/traces/{identifier}",
                        "observations": [
                            {
                                "name": row["name"],
                                "id": row["id"],
                                "parent_id": row.get("parentObservationId"),
                                "type": row["type"],
                                "start": row["startTime"],
                                "end": row["endTime"],
                            }
                            for row in nodes.values()
                        ],
                    },
                    ensure_ascii=False,
                ),
                flush=True,
            )
            return
        time.sleep(2)
    raise RuntimeError(f"Trace hierarchy not visible within 120s: {identifier}")


if __name__ == "__main__":
    main()
