"""Inspect adjacent source without importing device-dependent vLLM modules."""

import ast
from pathlib import Path

import pytest


def test_local_vllm_adapter_contract():
    root = Path(__file__).resolve().parents[2] / "vllm" / "vllm" / "v1"
    if not root.exists():
        pytest.skip("Adjacent vLLM source is not present")
    engine = ast.parse((root / "engine/async_llm.py").read_text(encoding="utf-8"))
    stats = ast.parse((root / "metrics/stats.py").read_text(encoding="utf-8"))
    processor = ast.parse(
        (root / "engine/output_processor.py").read_text(encoding="utf-8")
    )
    engine_cls = next(
        n for n in engine.body if isinstance(n, ast.ClassDef) and n.name == "AsyncLLM"
    )
    generate = next(
        n
        for n in engine_cls.body
        if isinstance(n, ast.AsyncFunctionDef) and n.name == "generate"
    )
    assert "request_id" in [a.arg for a in generate.args.args]
    assert any(isinstance(n, ast.Yield) for n in ast.walk(generate))
    finished = next(
        n
        for n in stats.body
        if isinstance(n, ast.ClassDef) and n.name == "FinishedRequestStats"
    )
    fields = {n.target.id for n in finished.body if isinstance(n, ast.AnnAssign)}
    assert {
        "request_id",
        "queued_time",
        "prefill_time",
        "decode_time",
        "num_generation_tokens",
    } <= fields
    calls = [
        n
        for n in ast.walk(processor)
        if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Attribute)
        and n.func.attr == "update_from_finished_request"
    ]
    assert calls
    assert any(
        k.arg == "request_id"
        and isinstance(k.value, ast.Attribute)
        and k.value.attr == "external_req_id"
        for k in calls[0].keywords
    )
