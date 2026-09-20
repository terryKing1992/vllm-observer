"""Full-request stage instrumentation for the API and EngineCore processes.

Coverage follows the documented request flow:

- ``render_input``      chat/completion template rendering + tokenize (API)
- ``input_process``     InputProcessor validation and request packaging (API)
- ``submit_engine``     output registration + ZMQ handoff (API)
- ``waiting_to_first_schedule`` engine-side waiting queue latency (EngineCore)
- ``schedule_total``    scheduler schedule() time per request (EngineCore)
- ``execute_model``     model forward + sampling time per request (EngineCore)
- metadata: prefill chunk count, decode step count (EngineCore)

Engine-side numbers cannot use the API process contextvar; they are collected
in a request-id keyed registry inside the EngineCore process, serialized onto
the final ``EngineCoreOutput.trace_headers`` and decoded back into the request
trace by the frontend (same transport as the calibration clock).
"""

import functools
import inspect
import json
import logging
import math
import time

from .core import current_request
from .engine_clock import CLOCK_ERROR, CLOCK_MONO, CLOCK_UNIX

logger = logging.getLogger("vllm_observer")
STAGES_HEADER = "x-observer-engine-stages"

# EngineCore process only; keyed by engine request id.
_REGISTRY: dict[str, dict] = {}


def _new_record() -> dict:
    return {
        "waiting_enter": None,
        "first_schedule": None,
        "schedule_steps": 0,
        "prefill_chunks": 0,
        "decode_steps": 0,
        "schedule_seconds": 0.0,
        "execute_seconds": 0.0,
        "execute_steps": 0,
    }


def _finite(value):
    try:
        return value is not None and math.isfinite(float(value))
    except (TypeError, ValueError):
        return False


# ---------------------------------------------------------------------------
# Frontend (API process): contextvar-friendly wall-clock stages.
# ---------------------------------------------------------------------------


def _timed_async(method, stage, contract):
    signature = inspect.signature(method)
    if not all(name in signature.parameters for name in contract):
        raise RuntimeError(f"Unsupported {method.__qualname__} contract")

    @functools.wraps(method)
    async def observed(self, *args, **kwargs):
        trace = current_request.get()
        if trace is None:
            return await method(self, *args, **kwargs)
        started = time.perf_counter()
        try:
            return await method(self, *args, **kwargs)
        finally:
            trace.add(stage, time.perf_counter() - started)

    observed._observer_stages = True
    return observed


def _timed_sync(method, stage, contract):
    signature = inspect.signature(method)
    if not all(name in signature.parameters for name in contract):
        raise RuntimeError(f"Unsupported {method.__qualname__} contract")

    @functools.wraps(method)
    def observed(self, *args, **kwargs):
        trace = current_request.get()
        if trace is None:
            return method(self, *args, **kwargs)
        started = time.perf_counter()
        try:
            return method(self, *args, **kwargs)
        finally:
            trace.add(stage, time.perf_counter() - started)

    observed._observer_stages = True
    return observed


def install_serving(module):
    """Wrap request rendering on OpenAI-compatible serving classes."""
    installed = []
    for cls_name, method_name in (
        ("OpenAIServingChat", "render_chat_request"),
        ("OpenAIServingCompletion", "render_completion_request"),
    ):
        cls = getattr(module, cls_name, None)
        method = getattr(cls, method_name, None) if cls is not None else None
        if method is None:
            continue
        if getattr(method, "_observer_stages", False):
            installed.append(method)
            continue
        wrapped = _timed_async(method, "render_input", ("request",))
        setattr(cls, method_name, wrapped)
        installed.append(wrapped)
    if not installed:
        raise RuntimeError(f"No serving render hook found in {module.__name__}")


def install_input_processor(module):
    """Wrap InputProcessor.process_inputs (validation + request packaging)."""
    processor = getattr(module, "InputProcessor", None)
    method = getattr(processor, "process_inputs", None)
    if method is None or getattr(method, "_observer_stages", False):
        return
    processor.process_inputs = _timed_sync(
        method, "input_process", ("request_id", "prompt", "params")
    )


def install_async_llm(engine_cls):
    """Wrap AsyncLLM._add_request (output registration + engine submission)."""
    method = getattr(engine_cls, "_add_request", None)
    if method is None:
        logger.warning(
            "AsyncLLM._add_request unavailable; submit_engine stage disabled"
        )
        return
    if getattr(method, "_observer_stages", False):
        return
    engine_cls._add_request = _timed_async(method, "submit_engine", ("request",))


# ---------------------------------------------------------------------------
# EngineCore process: request-id keyed counters attached on completion.
# ---------------------------------------------------------------------------


def install_scheduler(module):
    scheduler = getattr(module, "Scheduler", None)
    add_request = getattr(scheduler, "add_request", None)
    schedule = getattr(scheduler, "schedule", None)
    if add_request is None or schedule is None:
        raise RuntimeError("Unsupported vLLM Scheduler contract")
    if getattr(schedule, "_observer_stages", False):
        return

    @functools.wraps(add_request)
    def observed_add_request(self, request):
        result = add_request(self, request)
        try:
            record = _REGISTRY.setdefault(request.request_id, _new_record())
            if record["waiting_enter"] is None:
                record["waiting_enter"] = time.monotonic()
        except Exception:
            logger.exception("Observer could not mark waiting queue entry")
        return result

    @functools.wraps(schedule)
    def observed_schedule(self, *args, **kwargs):
        started = time.perf_counter()
        output = schedule(self, *args, **kwargs)
        duration = time.perf_counter() - started
        try:
            now = time.monotonic()
            for request_id in getattr(output, "num_scheduled_tokens", {}) or {}:
                record = _REGISTRY.get(request_id)
                if record is None:
                    continue
                if record["first_schedule"] is None:
                    record["first_schedule"] = now
                record["schedule_steps"] += 1
                record["schedule_seconds"] += duration
                request = (getattr(self, "requests", None) or {}).get(request_id)
                if request is not None and getattr(
                    request, "num_computed_tokens", 1
                ) < getattr(request, "num_prompt_tokens", 0):
                    record["prefill_chunks"] += 1
                else:
                    record["decode_steps"] += 1
        except Exception:
            logger.exception("Observer could not collect scheduler stage")
        return output

    observed_add_request._observer_stages = True
    observed_schedule._observer_stages = True
    scheduler.add_request = observed_add_request
    scheduler.schedule = observed_schedule


class _TimedFuture:
    """Forwarding proxy timing the blocking ``result()`` of an executor future."""

    def __init__(self, inner, request_ids):
        self._inner = inner
        self._request_ids = request_ids

    def result(self, *args, **kwargs):
        started = time.perf_counter()
        try:
            return self._inner.result(*args, **kwargs)
        finally:
            duration = time.perf_counter() - started
            for request_id in self._request_ids:
                record = _REGISTRY.get(request_id)
                if record is not None:
                    record["execute_seconds"] += duration
                    record["execute_steps"] += 1

    def __getattr__(self, name):
        return getattr(self._inner, name)


def _wrap_executor(executor):
    if executor is None or getattr(executor, "_observer_stages", False):
        return
    try:
        execute = executor.execute_model

        @functools.wraps(execute)
        def timed_execute(scheduler_output, *args, **kwargs):
            future = execute(scheduler_output, *args, **kwargs)
            try:
                request_ids = list(
                    getattr(scheduler_output, "num_scheduled_tokens", {}) or {}
                )
                if request_ids and callable(getattr(future, "result", None)):
                    return _TimedFuture(future, request_ids)
            except Exception:
                logger.exception("Observer could not wrap executor future")
            return future

        executor.execute_model = timed_execute
        executor._observer_stages = True
    except Exception:
        logger.exception("Observer executor hook unavailable; serving continues")


def _finished_outputs(batches):
    for batch in (batches or {}).values():
        for output in getattr(batch, "outputs", []) or []:
            if getattr(output, "finished", False):
                yield output


def _attach_stages(output):
    request_id = getattr(output, "request_id", None)
    record = _REGISTRY.pop(request_id, None) if request_id else None
    if record is None:
        return
    try:
        payload = json.dumps(record, separators=(",", ":"))
    except Exception:
        logger.exception("Observer could not serialize engine stages")
        return
    headers = getattr(output, "trace_headers", None)
    output.trace_headers = {**(headers or {}), STAGES_HEADER: payload}


def install_engine_core(engine_cls):
    for name in ("step", "step_with_batch_queue"):
        original = getattr(engine_cls, name, None)
        if original is None or getattr(original, "_observer_stages", False):
            continue

        def wrap(method):
            @functools.wraps(method)
            def step(self, *args, **kwargs):
                _wrap_executor(getattr(self, "model_executor", None))
                result = method(self, *args, **kwargs)
                try:
                    batches = result[0] if isinstance(result, tuple) else result
                    for output in _finished_outputs(batches):
                        _attach_stages(output)
                except Exception:
                    logger.exception("Observer engine stage attach failed")
                return result

            step._observer_stages = True
            return step

        setattr(engine_cls, name, wrap(original))


# ---------------------------------------------------------------------------
# Frontend collection: decode the piggybacked payload into the request trace.
# ---------------------------------------------------------------------------


def record_engine_stages(trace, generation, raw):
    """Merge engine-side stage data carried by ``RequestStateStats``."""
    payload = getattr(raw, "_observer_stages", None)
    if not payload:
        return
    try:
        stages = json.loads(payload)
    except (TypeError, ValueError):
        logger.exception("Observer could not decode engine stages")
        return

    metadata = generation["metadata"]
    metadata["schedule_steps"] = stages.get("schedule_steps", 0)
    metadata["prefill_chunks"] = stages.get("prefill_chunks", 0)
    metadata["decode_steps"] = stages.get("decode_steps", 0)
    trace.add(
        "schedule_total",
        stages.get("schedule_seconds", 0.0),
        max(1, stages.get("schedule_steps", 0)),
    )
    trace.add(
        "execute_model",
        stages.get("execute_seconds", 0.0),
        max(1, stages.get("execute_steps", 0)),
    )

    clock = getattr(raw, "_observer_clock", None)
    waiting_enter = stages.get("waiting_enter")
    first_schedule = stages.get("first_schedule")
    if not clock or not all(
        _finite(value) for value in (waiting_enter, first_schedule)
    ):
        metadata["waiting_unavailable"] = "missing_clock_or_timestamps"
        return
    try:
        mono = float(clock[CLOCK_MONO])
        wall = int(clock[CLOCK_UNIX])
        error_ns = int(clock[CLOCK_ERROR])
    except (KeyError, TypeError, ValueError):
        metadata["waiting_unavailable"] = "invalid_calibration_clock"
        return
    start_ns = wall + int((float(waiting_enter) - mono) * 1e9)
    end_ns = wall + int((float(first_schedule) - mono) * 1e9)
    if not generation["start_ns"] <= start_ns <= end_ns <= trace.now_ns():
        metadata["waiting_unavailable"] = "clock_skew_or_invalid_order"
        return
    trace.interval(
        generation,
        "waiting_to_first_schedule",
        start_ns,
        end_ns,
        timing_source="engine_monotonic_calibrated_to_wall",
        calibration_error_ns=error_ns,
        prefill_chunks=stages.get("prefill_chunks", 0),
        schedule_steps=stages.get("schedule_steps", 0),
    )
