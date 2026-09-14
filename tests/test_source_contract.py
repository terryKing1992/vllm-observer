"""Inspect adjacent source without importing device-dependent vLLM modules."""

import ast
from pathlib import Path

import pytest


def test_local_vllm_adapter_contract():
    project = Path(__file__).resolve().parents[1]
    roots = [parent / "vllm" / "vllm" / "v1" for parent in (project, project.parent)]
    root = next((path for path in roots if path.exists()), None)
    if root is None:
        pytest.skip("Local vLLM source is not present")
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


def test_local_vllm_engine_clock_contract():
    project = Path(__file__).resolve().parents[1]
    roots = [parent / "vllm" / "vllm" / "v1" for parent in (project, project.parent)]
    root = next((path for path in roots if path.exists()), None)
    if root is None:
        pytest.skip("Local vLLM source is not present")

    def source(relative):
        return ast.parse((root / relative).read_text(encoding="utf-8"))

    def class_named(tree, name):
        return next(
            node
            for node in tree.body
            if isinstance(node, ast.ClassDef) and node.name == name
        )

    core = class_named(source("engine/core.py"), "EngineCore")
    for name in ("step", "step_with_batch_queue"):
        method = next(
            node
            for node in core.body
            if isinstance(node, ast.FunctionDef) and node.name == name
        )
        returns = [
            node.value for node in ast.walk(method) if isinstance(node, ast.Return)
        ]
        assert returns
        assert all(
            isinstance(value, ast.Tuple) and len(value.elts) == 2 for value in returns
        )
        assert any(
            isinstance(value.elts[0], ast.Name)
            and value.elts[0].id == "engine_core_outputs"
            for value in returns
        )

    output = class_named(source("engine/__init__.py"), "EngineCoreOutput")
    fields = {node.target.id for node in output.body if isinstance(node, ast.AnnAssign)}
    assert {"trace_headers", "finish_reason"} <= fields
    assert any(
        isinstance(node, ast.FunctionDef) and node.name == "finished"
        for node in output.body
    )

    stats = source("metrics/stats.py")
    iteration = class_named(stats, "IterationStats")
    for name in ("update_from_output", "update_from_finished_request"):
        method = next(
            node
            for node in iteration.body
            if isinstance(node, ast.FunctionDef) and node.name == name
        )
        assert "req_stats" in [argument.arg for argument in method.args.args]
        if name == "update_from_output":
            assert method.args.args[1].arg == "output"
    request_stats = class_named(stats, "RequestStateStats")
    fields = {
        node.target.id for node in request_stats.body if isinstance(node, ast.AnnAssign)
    }
    assert {"queued_ts", "scheduled_ts", "first_token_ts", "last_token_ts"} <= fields
