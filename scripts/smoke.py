"""Run against demo or real vLLM: python scripts/smoke.py --model qwen."""

import argparse
import json
import urllib.request
import uuid


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://localhost:8000")
    parser.add_argument("--model", default="demo-model")
    args = parser.parse_args()
    identifier = uuid.uuid4().hex
    body = json.dumps(
        {
            "model": args.model,
            "messages": [{"role": "user", "content": "Say hello."}],
            "max_tokens": 8,
            "stream": True,
        }
    ).encode()
    request = urllib.request.Request(
        args.base_url.rstrip("/") + "/v1/chat/completions",
        data=body,
        headers={"Content-Type": "application/json", "x-trace-id": identifier},
    )
    with urllib.request.urlopen(request, timeout=120) as response:
        assert response.headers["x-trace-id"] == identifier
        content = response.read()
        assert b"[DONE]" in content, "Expected completed SSE response"
    with urllib.request.urlopen(
        args.base_url.rstrip("/") + "/observer/metrics", timeout=10
    ) as response:
        assert b"observer_instance_info" in response.read()
    print(
        json.dumps(
            {
                "status": "passed",
                "trace_id": identifier,
                "note": "Check console engine_requests/stages and Langfuse separately",
            }
        )
    )


if __name__ == "__main__":
    main()
