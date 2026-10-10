import runpy
from pathlib import Path

import pytest


def observations():
    rows = []
    timeline = {
        "serve-model-request": ("00:00:00", "00:00:01"),
        "api-server.api_prepare": ("00:00:00", "00:00:00.1"),
        "generate-response": ("00:00:00.1", "00:00:00.9"),
        "engine-core.engine_handoff": ("00:00:00.1", "00:00:00.15"),
        "engine-core.waiting_to_first_schedule": ("00:00:00.15", "00:00:00.2"),
        "engine-core.prefill": ("00:00:00.2", "00:00:00.4"),
        "engine-core.decode": ("00:00:00.4", "00:00:00.85"),
        "engine-core.engine_finalize": ("00:00:00.85", "00:00:00.9"),
        "api-server.response_transfer": ("00:00:00.9", "00:00:01"),
    }
    for name, parent in (
        ("serve-model-request", None),
        ("api-server.api_prepare", "serve-model-request"),
        ("generate-response", "serve-model-request"),
        ("engine-core.engine_handoff", "generate-response"),
        ("engine-core.waiting_to_first_schedule", "generate-response"),
        ("engine-core.prefill", "generate-response"),
        ("engine-core.decode", "generate-response"),
        ("engine-core.engine_finalize", "generate-response"),
        ("api-server.response_transfer", "serve-model-request"),
    ):
        start, end = timeline[name]
        rows.append(
            {
                "id": name,
                "name": name,
                "parentObservationId": parent,
                "type": "GENERATION" if name == "generate-response" else "SPAN",
                "traceId": "a" * 32,
                "startTime": f"2026-09-15T{start}Z",
                "endTime": f"2026-09-15T{end}Z",
                "model": "qwen-observed",
                "usageDetails": {"input": 20, "output": 16},
                "metadata": {
                    "traceId": "a" * 32,
                    "count": 15,
                    "total_ms": 150.0,
                    "mean_ms": 10.0,
                },
            }
        )
    return rows


def test_real_model_audit_uses_reported_tokens_and_checks_node_identity():
    script = Path(__file__).resolve().parents[1] / "scripts" / "verify_langfuse.py"
    audit = runpy.run_path(str(script))["audit_observations"]
    rows = observations()
    assert len(audit(rows, "a" * 32, "qwen-observed")) == len(rows)
    rows[-1]["metadata"]["traceId"] = "b" * 32
    with pytest.raises(AssertionError, match="node details traceId"):
        audit(rows, "a" * 32, "qwen-observed")


def test_real_model_audit_rejects_incorrect_decode_aggregation():
    script = Path(__file__).resolve().parents[1] / "scripts" / "verify_langfuse.py"
    audit = runpy.run_path(str(script))["audit_observations"]
    rows = observations()
    decode = next(row for row in rows if row["name"] == "engine-core.decode")
    decode["metadata"]["count"] = 3
    with pytest.raises(AssertionError):
        audit(rows, "a" * 32, "qwen-observed")
