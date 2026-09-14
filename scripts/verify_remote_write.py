"""Verify POST ingestion and dashboard queries against a disposable Prometheus."""

import argparse
import json
import time
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

from vllm_observer.metrics import MetricsFilter
from vllm_observer.remote_write import RemoteWriter, encode_registry


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", required=True)
    args = parser.parse_args()
    base = args.base_url.rstrip("/")

    def query(expression):
        url = base + "/api/v1/query?" + urllib.parse.urlencode({"query": expression})
        with urllib.request.urlopen(url, timeout=5) as response:
            result = json.load(response)
        assert result["status"] == "success", result
        return result["data"]["result"]

    writer = RemoteWriter(MetricsFilter(), base + "/api/v1/write")
    identifier = "validation-" + uuid.uuid4().hex
    writer.identity["instance_id"] = identifier
    writer.alive.set(1)
    writer.healthy.set(1)
    writer.push()
    time.sleep(0.1)
    selector = '{instance_id="' + identifier + '"}'
    live = (
        "count((observer_instance_alive"
        + selector
        + " == 1) and (time() - observer_heartbeat_timestamp_seconds"
        + selector
        + " < 30)) or vector(0)"
    )
    assert float(query(live)[0]["value"][1]) == 1

    # Store a fresh sample carrying an OLD heartbeat: still excluded from online count.
    writer.heartbeat.set(time.time() - 60)
    payload = encode_registry(
        writer.metrics.registry, writer.identity, time.time_ns() // 1_000_000
    )
    with urllib.request.urlopen(
        urllib.request.Request(
            base + "/api/v1/write",
            data=payload,
            headers={
                "Content-Type": "application/x-protobuf",
                "Content-Encoding": "snappy",
                "X-Prometheus-Remote-Write-Version": "0.1.0",
                "User-Agent": "vllm-observer-test",
            },
        ),
        timeout=5,
    ) as response:
        assert response.status == 204
    time.sleep(0.1)
    assert float(query(live)[0]["value"][1]) == 0

    writer.alive.set(0)
    writer.push()
    time.sleep(0.1)
    assert float(query(live)[0]["value"][1]) == 0
    dashboard = json.loads(
        (
            Path(__file__).resolve().parents[1]
            / "deploy/grafana/dashboards/observer.json"
        ).read_text(encoding="utf-8")
    )
    for panel in dashboard["panels"]:
        for target in panel["targets"]:
            query(target["expr"])
    print(
        "PASS: Remote Write ingestion, heartbeat expiry, shutdown state, all dashboard PromQL"
    )


if __name__ == "__main__":
    main()
