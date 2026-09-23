"""Per-request aggregation and ordered, failure-isolated filters."""

import contextvars
import hashlib
import json
import logging
import sys
import time
import uuid
from dataclasses import asdict, dataclass, field
from typing import Protocol

logger = logging.getLogger("vllm_observer")
current_request = contextvars.ContextVar("observer_request", default=None)


def derive_trace_id(request_id: str) -> str:
    """Deterministically derive a 32-hex OTLP trace id from a request id.

    The API process and EngineCore process can therefore agree on the same
    trace identity without propagating OpenTelemetry context between them.
    """
    return hashlib.blake2b(
        str(request_id).encode("utf-8"), digest_size=16
    ).hexdigest()


@dataclass
class Aggregate:
    count: int = 0
    total_seconds: float = 0.0

    def add(self, duration: float, count: int = 1):
        self.count += count
        self.total_seconds += max(0.0, duration)

    def export(self):
        return {
            **asdict(self),
            "mean_seconds": self.total_seconds / self.count if self.count else 0.0,
        }


@dataclass
class RequestTrace:
    trace_id: str
    parent_span_id: str | None = None
    started_ns: int = field(default_factory=time.time_ns)
    started: float = field(default_factory=time.perf_counter)
    status: str = "ok"
    stages: dict[str, Aggregate] = field(default_factory=dict)
    engine_requests: int = 0
    root_span_id: str = field(default_factory=lambda: uuid.uuid4().hex[:16])
    observations: list[dict] = field(default_factory=list)
    route: str = "unknown"
    first_body_ns: int | None = None
    api_intervals: list = field(default_factory=list)
    request_payload: dict | None = None
    response_text: str | None = None
    user_id: str | None = None
    session_id: str | None = None

    def now_ns(self):
        return self.started_ns + int((time.perf_counter() - self.started) * 1e9)

    def begin_generation(self, request_id):
        node = {
            "id": uuid.uuid4().hex[:16],
            "parent_id": self.root_span_id,
            "name": "generate-response",
            "type": "generation",
            "start_ns": self.now_ns(),
            "end_ns": None,
            "metadata": {"request_id": request_id},
            "usage": {},
        }
        self.observations.append(node)
        return node

    def interval(self, parent, name, start_ns, end_ns, **metadata):
        if end_ns < start_ns:
            raise ValueError("Observation end precedes start")
        self.observations.append(
            {
                "id": uuid.uuid4().hex[:16],
                "parent_id": parent["id"],
                "name": name,
                "type": "span",
                "start_ns": start_ns,
                "end_ns": end_ns,
                "metadata": metadata,
            }
        )

    def add(self, stage: str, duration: float, count: int = 1):
        self.stages.setdefault(stage, Aggregate()).add(duration, count)

    def api_interval(self, name, started_perf, ended_perf, **metadata):
        """Record an API-process stage with real start/end wall timestamps."""
        start_ns = self.started_ns + int((started_perf - self.started) * 1e9)
        end_ns = self.started_ns + int((ended_perf - self.started) * 1e9)
        if end_ns >= start_ns:
            self.api_intervals.append(
                {
                    "id": uuid.uuid4().hex[:16],
                    "parent_id": self.root_span_id,
                    "name": name,
                    "type": "span",
                    "start_ns": start_ns,
                    "end_ns": end_ns,
                    "metadata": {"timing_source": "wall_measured", **metadata},
                }
            )

    def finish(self):
        self.add("http_total", time.perf_counter() - self.started)
        return {
            "trace_id": self.trace_id,
            "parent_span_id": self.parent_span_id,
            "started_ns": self.started_ns,
            "status": self.status,
            "engine_requests": self.engine_requests,
            "root_span_id": self.root_span_id,
            "route": self.route,
            "observations": self.observations,
            "stages": {k: v.export() for k, v in self.stages.items()},
            "request_payload": self.request_payload,
            "response_text": self.response_text,
            "first_body_ns": self.first_body_ns,
            "api_intervals": self.api_intervals,
            "user_id": self.user_id,
            "session_id": self.session_id,
        }


class Filter(Protocol):
    def process(self, event: dict) -> dict | None: ...


class FilterChain:
    def __init__(self, filters):
        self.filters = tuple(filters)

    def process(self, event):
        for item in self.filters:
            try:
                result = item.process(event)
                if result is None:
                    return
                event = result
            except Exception:
                logger.exception("Observer filter failed: %s", type(item).__name__)


class ConsoleFilter:
    def __init__(self):
        if not logger.handlers:
            handler = logging.StreamHandler(sys.stdout)
            handler.setFormatter(logging.Formatter("%(message)s"))
            logger.addHandler(handler)
        logger.setLevel(logging.INFO)
        logger.propagate = False

    def process(self, event):
        logger.info(json.dumps(event, ensure_ascii=False))
        return event
