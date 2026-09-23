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
import os
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
        # Degraded contention tracking: engine steps that ran while this
        # request was mid-prefill but did not schedule it (other requests'
        # work interleaved between its prefill chunks).
        "contention_steps": 0,
        "contention_seconds": 0.0,
        "prefill_done": False,
        # (monotonic_start, monotonic_end) pairs, bounded to avoid unbounded
        # memory for very long decode sequences.
        "execute_intervals": [],
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
            ended = time.perf_counter()
            trace.add(stage, ended - started)
            trace.api_interval(stage, started, ended, module=method.__module__)

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
            ended = time.perf_counter()
            trace.add(stage, ended - started)
            trace.api_interval(stage, started, ended, module=method.__module__)

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


def install_protocol(module):
    """Wrap protocol.to_sampling_params (sampling parameter construction)."""
    installed = []
    for cls_name in ("ChatCompletionRequest", "CompletionRequest"):
        cls = getattr(module, cls_name, None)
        method = getattr(cls, "to_sampling_params", None) if cls else None
        if method is None:
            continue
        if getattr(method, "_observer_stages", False):
            installed.append(method)
            continue
        wrapped = _timed_sync(method, "sampling_params", ("self",))
        setattr(cls, "to_sampling_params", wrapped)
        installed.append(wrapped)
    if not installed:
        raise RuntimeError(f"No sampling params hook found in {module.__name__}")


def install_async_llm(engine_cls):
    """Wrap AsyncLLM.add_request and _add_request (engine submission stages)."""
    # ``generate`` is an async generator spanning the whole request lifetime;
    # the submission boundary is the public ``add_request`` instead.
    add_request = getattr(engine_cls, "add_request", None)
    if (
        add_request is not None
        and inspect.iscoroutinefunction(add_request)
        and not getattr(add_request, "_observer_stages", False)
    ):
        try:
            engine_cls.add_request = _timed_async(
                add_request, "submit_engine", ("self",)
            )
        except RuntimeError:
            logger.warning("AsyncLLM.add_request contract changed; stage skipped")
    method = getattr(engine_cls, "_add_request", None)
    if method is None:
        logger.warning(
            "AsyncLLM._add_request unavailable; submit_engine stage disabled"
        )
        return
    if getattr(method, "_observer_stages", False):
        return
    engine_cls._add_request = _timed_async(method, "output_processor_add", ("request",))


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
                scheduled = (getattr(output, "num_scheduled_tokens", {}) or {}).get(
                    request_id, 0
                )
                if scheduled > 1:
                    record["prefill_chunks"] += 1
                else:
                    record["decode_steps"] += 1
                    record["prefill_done"] = True
        except Exception:
            logger.exception("Observer could not collect scheduler stage")
        try:
            scheduled_ids = set(getattr(output, "num_scheduled_tokens", {}) or {})
            if scheduled_ids:
                for request_id, record in _REGISTRY.items():
                    if (
                        record["first_schedule"] is not None
                        and not record["prefill_done"]
                        and request_id not in scheduled_ids
                    ):
                        record["contention_steps"] += 1
                        record["contention_seconds"] += duration
        except Exception:
            logger.exception("Observer could not collect prefill contention")
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
        started = time.monotonic()
        try:
            return self._inner.result(*args, **kwargs)
        finally:
            ended = time.monotonic()
            duration = ended - started
            if os.getenv("OBSERVER_DEBUG", "0") == "1":
                logger.warning(
                    "observer timed future ids=%s duration=%.6f registry=%d",
                    self._request_ids,
                    duration,
                    len(_REGISTRY),
                )
            for request_id in self._request_ids:
                record = _REGISTRY.get(request_id)
                if record is not None:
                    record["execute_seconds"] += duration
                    record["execute_steps"] += 1
                    intervals = record["execute_intervals"]
                    if len(intervals) < 512:
                        intervals.append((started, ended))

    def __getattr__(self, name):
        return getattr(self._inner, name)


def _wrap_executor(executor):
    if executor is None or getattr(executor, "_observer_stages", False):
        return
    try:
        execute = executor.execute_model

        @functools.wraps(execute)
        def timed_execute(scheduler_output, *args, **kwargs):
            try:
                request_ids = list(
                    getattr(scheduler_output, "num_scheduled_tokens", {}) or {}
                )
                if os.getenv("OBSERVER_DEBUG", "0") == "1":
                    logger.warning(
                        "observer execute called ids=%s type=%s",
                        request_ids,
                        type(scheduler_output).__name__,
                    )
                started = time.monotonic()
                future = execute(scheduler_output, *args, **kwargs)
                if request_ids and callable(getattr(future, "result", None)):
                    # vLLM >= 0.29 may never call result() on this future in
                    # its batch-queue path (outputs arrive via sample_tokens).
                    # A done-callback still captures real GPU completion.
                    try:
                        future.add_done_callback(
                            lambda _f, ids=tuple(request_ids), s=started: (
                                _record_execute(list(ids), s, time.monotonic())
                            )
                        )
                    except Exception:
                        logger.exception("Observer could not attach executor callback")
                    return future
                ended = time.monotonic()
                _record_execute(request_ids, started, ended)
                return future
            except Exception:
                logger.exception("Observer could not wrap executor call")
                return execute(scheduler_output, *args, **kwargs)

        executor.execute_model = timed_execute
        executor._observer_stages = True
        if os.getenv("OBSERVER_DEBUG", "0") == "1":
            logger.warning("observer wrapped %s.execute_model", type(executor).__name__)
    except Exception:
        logger.exception("Observer executor hook unavailable; serving continues")


def _record_execute(request_ids, started, ended):
    if os.getenv("OBSERVER_DEBUG", "0") == "1":
        logger.warning(
            "observer execute recorded ids=%s duration=%.6f registry=%d",
            request_ids,
            ended - started,
            len(_REGISTRY),
        )
    duration = ended - started
    for request_id in request_ids:
        record = _REGISTRY.get(request_id)
        if record is None:
            continue
        record["execute_seconds"] += duration
        record["execute_steps"] += 1
        intervals = record["execute_intervals"]
        if len(intervals) < 512:
            intervals.append((started, ended))


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
    metadata["contention_steps"] = stages.get("contention_steps", 0)
    metadata["contention_seconds"] = stages.get("contention_seconds", 0.0)
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
        module="vllm.v1.core.sched.scheduler",
        prefill_chunks=stages.get("prefill_chunks", 0),
        schedule_steps=stages.get("schedule_steps", 0),
        contention_steps=stages.get("contention_steps", 0),
        contention_seconds=stages.get("contention_seconds", 0.0),
    )
    intervals = stages.get("execute_intervals") or []
    if intervals and clock:
        try:
            mono = float(clock[CLOCK_MONO])
            wall = int(clock[CLOCK_UNIX])
            first_start = wall + int((intervals[0][0] - mono) * 1e9)
            last_end = wall + int((intervals[-1][1] - mono) * 1e9)
            if generation["start_ns"] <= first_start <= last_end <= trace.now_ns():
                trace.interval(
                    generation,
                    "execute_model",
                    first_start,
                    last_end,
                    timing_source="engine_monotonic_calibrated_to_wall",
                    calibration_error_ns=error_ns,
                    module="vllm.v1.executor.uniproc_executor",
                    aggregate=True,
                    step_count=stages.get("execute_steps", 0),
                    captured_steps=len(intervals),
                )
        except (KeyError, TypeError, ValueError):
            metadata["execute_interval_unavailable"] = "invalid_calibration_clock"
