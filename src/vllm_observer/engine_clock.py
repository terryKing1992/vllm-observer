"""Calibrate final engine output timestamps in the originating process."""

import functools
import logging
import time

logger = logging.getLogger("vllm_observer")
CLOCK_MONO = "x-observer-clock-monotonic"
CLOCK_UNIX = "x-observer-clock-unix-ns"
CLOCK_ERROR = "x-observer-clock-error-ns"


def stamp_outputs(batches):
    # Only final outputs need calibration. Never compare frontend/core monotonic clocks.
    finished = [
        out
        for batch in (batches or {}).values()
        for out in batch.outputs
        if out.finished
    ]
    if not finished:
        return
    before = time.monotonic_ns()
    wall = time.time_ns()
    after = time.monotonic_ns()
    calibration = {
        CLOCK_MONO: str((before + after) / 2e9),
        CLOCK_UNIX: str(wall),
        CLOCK_ERROR: str(after - before),
    }
    for output in finished:
        output.trace_headers = {**(output.trace_headers or {}), **calibration}


def install(engine_cls):
    for name in ("step", "step_with_batch_queue"):
        original = getattr(engine_cls, name)
        if getattr(original, "_observer_clock", False):
            continue

        def wrap(method):
            @functools.wraps(method)
            def step(self, *args, **kwargs):
                result = method(self, *args, **kwargs)
                try:
                    stamp_outputs(result[0])
                except Exception:
                    logger.exception("Observer engine clock calibration failed")
                return result

            step._observer_clock = True
            return step

        setattr(engine_cls, name, wrap(original))
