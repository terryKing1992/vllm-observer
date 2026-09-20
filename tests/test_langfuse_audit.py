import runpy
from pathlib import Path

import pytest


def observations():
    rows = []
    for name, parent in (
        ("serve-model-request", None),
        ("generate-response", "serve-model-request"),
        ("queue", "generate-response"),
        ("prefill", "generate-response"),
        ("decode", "generate-response"),
    ):
        rows.append(
            {
                "id": name,
                "name": name,
                "parentObservationId": parent,
                "type": "GENERATION" if name == "generate-response" else "SPAN",
                "traceId": "a" * 32,
                "startTime": "2026-09-15T00:00:00Z",
                "endTime": "2026-09-15T00:00:01Z",
                "model": "qwen-observed",
                "usageDetails": {"input": 20, "output": 16},
                "metadata": {
                    "traceId": "a" * 32,
                    "count": 15,
                    "total_seconds": 0.15,
                    "mean_seconds": 0.01,
                },
            }
        )
    return rows


def test_real_model_audit_uses_reported_tokens_and_checks_node_identity():
    script = Path(__file__).resolve().parents[1] / "scripts" / "verify_langfuse.py"
    audit = runpy.run_path(str(script))["audit_observations"]
    rows = observations()
    assert len(audit(rows, "a" * 32, "qwen-observed")) == 5
    rows[-1]["metadata"]["traceId"] = "b" * 32
    with pytest.raises(AssertionError, match="node details traceId"):
        audit(rows, "a" * 32, "qwen-observed")


def test_real_model_audit_rejects_incorrect_decode_aggregation():
    script = Path(__file__).resolve().parents[1] / "scripts" / "verify_langfuse.py"
    audit = runpy.run_path(str(script))["audit_observations"]
    rows = observations()
    rows[-1]["metadata"]["count"] = 3
    with pytest.raises(AssertionError):
        audit(rows, "a" * 32, "qwen-observed")
