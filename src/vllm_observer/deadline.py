"""Drop requests whose client has already timed out before they are batched.

vLLM aborts requests when it detects a client disconnect, but detection is
not instantaneous (and may never fire for some non-streaming clients). Under
heavy load, stale requests keep occupying the waiting queue and get scheduled
for prefill/decode compute that nobody will consume.

This guard runs inside the EngineCore process at the top of every
``Scheduler.schedule()`` call: any unfinished request older than
``OBSERVER_REQUEST_DEADLINE_SECONDS`` is finished with FINISHED_ABORTED via
the scheduler's own ``finish_requests`` path (queue removal, KV free, abort
output to the API process) -- exactly as a client disconnect would, just
earlier and unconditionally. ``0`` (default) disables the guard.
"""

import functools
import logging
import os
import time

logger = logging.getLogger("vllm_observer")

# Cumulative dropped-request count; readable for metrics/logging.
dropped_total = 0


def deadline_seconds() -> float:
    """Read the deadline TTL; non-positive values disable the guard."""
    try:
        return float(os.getenv("OBSERVER_REQUEST_DEADLINE_SECONDS", "0"))
    except ValueError:
        logger.warning(
            "Invalid OBSERVER_REQUEST_DEADLINE_SECONDS; deadline guard disabled"
        )
        return 0.0


def drop_expired(scheduler) -> list[str]:
    """Finish expired requests; returns the dropped request ids."""
    ttl = deadline_seconds()
    if ttl <= 0:
        return []
    try:
        from vllm.v1.request import RequestStatus
    except Exception:
        logger.exception("Observer deadline guard cannot import RequestStatus")
        return []
    now = time.time()
    expired = [
        request.request_id
        for request in list(getattr(scheduler, "requests", {}).values())
        if not request.is_finished()
        and (now - getattr(request, "arrival_time", now)) > ttl
    ]
    if not expired:
        return []
    global dropped_total
    try:
        scheduler.finish_requests(expired, RequestStatus.FINISHED_ABORTED)
    except Exception:
        logger.exception("Observer deadline guard failed to finish requests")
        return []
    dropped_total += len(expired)
    logger.warning(
        "Observer deadline guard dropped %d expired request(s): %s",
        len(expired),
        ", ".join(expired[:8]) + ("..." if len(expired) > 8 else ""),
    )
    return expired


def install(module):
    """Wrap Scheduler.schedule with the pre-scheduling deadline check."""
    scheduler = getattr(module, "Scheduler", None)
    schedule = getattr(scheduler, "schedule", None)
    if scheduler is None or schedule is None:
        raise RuntimeError("Unsupported vLLM Scheduler contract")
    if getattr(schedule, "_observer_deadline", False):
        return

    @functools.wraps(schedule)
    def guarded(self, *args, **kwargs):
        try:
            drop_expired(self)
        except Exception:
            # The guard must never break the scheduling loop.
            logger.exception("Observer deadline guard crashed; scheduling anyway")
        return schedule(self, *args, **kwargs)

    guarded._observer_deadline = True
    scheduler.schedule = guarded
