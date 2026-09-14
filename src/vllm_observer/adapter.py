"""Version-checked instrumentation; never imports torch or touches devices."""

import functools
import asyncio
import inspect
import logging
import math

from .core import current_request
from .engine_clock import CLOCK_MONO, CLOCK_UNIX, CLOCK_ERROR

logger = logging.getLogger("vllm_observer")


def record_timeline(trace, generation, stat, raw):
    for field, key in (("num_prompt_tokens", "input"), ("num_generation_tokens", "output")):
        value = getattr(stat, field, None)
        if value is not None:
            generation["usage"][key] = generation["usage"].get(key, 0) + value
    sequence = generation["metadata"].get("sequence_count", 0)
    generation["metadata"]["sequence_count"] = sequence + 1
    clock = getattr(raw, "_observer_clock", None)
    if not clock:
        generation["metadata"]["timeline_unavailable"] = "missing_engine_clock"
        return
    mono = float(clock[CLOCK_MONO])
    wall = int(clock[CLOCK_UNIX])
    edges = [getattr(raw, key) for key in
             ("queued_ts", "scheduled_ts", "first_token_ts", "last_token_ts")]
    if not all(math.isfinite(value) and value > 0 for value in edges):
        generation["metadata"]["timeline_unavailable"] = "incomplete_engine_timestamps"
        return
    stamps = [wall + int((edge - mono) * 1e9) for edge in edges]
    if stamps != sorted(stamps) or not generation["start_ns"] <= stamps[0] <= stamps[-1] <= trace.now_ns():
        generation["metadata"]["timeline_unavailable"] = "clock_skew_or_invalid_order"
        return
    generation.setdefault("completion_start_ns", stamps[2])
    count = max(0, stat.num_generation_tokens - 1)
    for i, (name, repetitions) in enumerate((("queue", 1), ("prefill", 1), ("decode", count))):
        duration = (stamps[i + 1] - stamps[i]) / 1e9
        trace.interval(generation, name, stamps[i], stamps[i + 1],
                       timing_source="engine_monotonic_calibrated_to_wall",
                       calibration_error_ns=int(clock[CLOCK_ERROR]), sequence_index=sequence,
                       count=repetitions, total_seconds=duration,
                       mean_seconds=duration / repetitions if repetitions else 0)


def install(engine_cls=None, stats_cls=None):
    if engine_cls is None:
        from vllm.v1.engine.async_llm import AsyncLLM
        from vllm.v1.metrics.stats import IterationStats

        engine_cls, stats_cls = AsyncLLM, IterationStats
    if getattr(engine_cls.generate, "_observer", False):
        return
    generate = engine_cls.generate
    update = stats_cls.update_from_finished_request
    update_signature = inspect.signature(update)
    signature = inspect.signature(generate)
    if "request_id" not in signature.parameters or not inspect.isasyncgenfunction(
        generate
    ):
        raise RuntimeError("Unsupported vLLM AsyncLLM.generate contract")
    if "request_id" not in inspect.signature(update).parameters:
        raise RuntimeError("Unsupported vLLM request statistics contract")
    active = {}

    @functools.wraps(generate)
    async def observed_generate(self, *args, **kwargs):
        trace = current_request.get()
        request_id = signature.bind(self, *args, **kwargs).arguments["request_id"]
        generation = None
        if trace is not None:
            generation = trace.begin_generation(request_id)
            active[request_id] = (trace, generation)
        iterator = generate(self, *args, **kwargs)
        try:
            async for output in iterator:
                yield output
        except (asyncio.CancelledError, GeneratorExit):
            if generation is not None:
                generation["metadata"]["status"] = "cancelled"
            raise
        except Exception:
            if generation is not None:
                generation["metadata"]["status"] = "error"
            raise
        finally:
            try:
                await iterator.aclose()
            finally:
                if generation is not None:
                    generation["end_ns"] = trace.now_ns()
                active.pop(request_id, None)

    @functools.wraps(update)
    def observed_update(self, *args, **kwargs):
        result = update(self, *args, **kwargs)
        try:
            stat = self.finished_requests[-1]
            entry = active.get(stat.request_id)
            if entry is not None:
                trace, generation = entry
                trace.engine_requests += 1
                trace.add("queue", stat.queued_time)
                trace.add("prefill", stat.prefill_time)
                trace.add(
                    "decode", stat.decode_time, max(0, stat.num_generation_tokens - 1)
                )
                raw = update_signature.bind(self, *args, **kwargs).arguments.get("req_stats")
                record_timeline(trace, generation, stat, raw)
        except Exception:
            logger.exception("Observer could not collect request statistics")
        return result

    observed_generate._observer = True
    engine_cls.generate = observed_generate
    stats_cls.update_from_finished_request = observed_update
    output_update = getattr(stats_cls, "update_from_output", None)
    if output_update is not None:
        output_signature = inspect.signature(output_update)

        @functools.wraps(output_update)
        def observed_output(self, *args, **kwargs):
            result = output_update(self, *args, **kwargs)
            try:
                output = args[0] if args else kwargs.get("output")
                if output.finished and output.trace_headers and CLOCK_MONO in output.trace_headers:
                    raw = output_signature.bind(self, *args, **kwargs).arguments["req_stats"]
                    raw._observer_clock = {key: output.trace_headers[key]
                                           for key in (CLOCK_MONO, CLOCK_UNIX, CLOCK_ERROR)}
            except Exception:
                logger.exception("Observer could not read engine clock")
            return result

        stats_cls.update_from_output = observed_output
