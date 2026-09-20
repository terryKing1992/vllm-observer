"""Device-free checks for full-request stage instrumentation."""

import asyncio
import json
from types import SimpleNamespace

import pytest

from vllm_observer import stages
from vllm_observer.core import RequestTrace, current_request
from vllm_observer.engine_clock import CLOCK_ERROR, CLOCK_MONO, CLOCK_UNIX

EPOCH_NS = 1_700_000_000_000_000_000
SECOND = 1_000_000_000


@pytest.fixture(autouse=True)
def clean_registry():
    stages._REGISTRY.clear()
    yield
    stages._REGISTRY.clear()


class FakeServingModule:
    class OpenAIServingChat:
        async def render_chat_request(self, request):
            return ("conversation", ["engine-input"])


class FakeInputProcessorModule:
    class InputProcessor:
        def process_inputs(self, request_id, prompt, params, **kwargs):
            return SimpleNamespace(request_id=request_id, params=params)


class FakeEngine:
    async def _add_request(self, request, prompt, parent_req, index, queue):
        return "submitted"


def test_frontend_stages_record_wall_time(monkeypatch):
    stages.install_serving(FakeServingModule)
    stages.install_input_processor(FakeInputProcessorModule)
    stages.install_async_llm(FakeEngine)
    trace = RequestTrace("b" * 32, started_ns=EPOCH_NS)
    clock = [EPOCH_NS + SECOND]
    monkeypatch.setattr(trace, "now_ns", lambda: clock[0])
    token = current_request.set(trace)

    async def run():
        serving = FakeServingModule.OpenAIServingChat()
        processor = FakeInputProcessorModule.InputProcessor()
        assert await serving.render_chat_request("request") == (
            "conversation",
            ["engine-input"],
        )
        assert (
            processor.process_inputs("request", "prompt", "params").request_id
            == "request"
        )
        assert (
            await FakeEngine()._add_request("request", "prompt", None, 0, "queue")
            == "submitted"
        )

    try:
        asyncio.run(run())
    finally:
        current_request.reset(token)
    assert set(trace.stages) == {"render_input", "input_process", "submit_engine"}
    for stage in trace.stages.values():
        assert stage.count == 1
        assert stage.total_seconds >= 0.0


class FakeSchedulerModule:
    class Scheduler:
        def __init__(self):
            self.requests = {}

        def add_request(self, request):
            self.requests[request.request_id] = request

        def schedule(self, throttle_prefills=False):
            return SimpleNamespace(num_scheduled_tokens={"request-a": 3})


def test_scheduler_wraps_count_prefill_and_decode_steps():
    module = FakeSchedulerModule
    stages.install_scheduler(module)
    scheduler = module.Scheduler()
    request = SimpleNamespace(
        request_id="request-a",
        num_computed_tokens=0,
        num_prompt_tokens=10,
    )
    scheduler.add_request(request)
    scheduler.schedule()
    request.num_computed_tokens = 10
    scheduler.schedule()
    record = stages._REGISTRY["request-a"]
    assert record["waiting_enter"] is not None
    assert record["first_schedule"] is not None
    assert record["schedule_steps"] == 2
    assert record["prefill_chunks"] == 1
    assert record["decode_steps"] == 1
    assert record["schedule_seconds"] >= 0.0


class FakeFuture:
    def result(self):
        return "model-output"


class FakeExecutor:
    def execute_model(self, scheduler_output, non_block=True):
        return FakeFuture()


class FakeEngineCore:
    def __init__(self):
        self.model_executor = FakeExecutor()

    def step(self):
        output = SimpleNamespace(
            request_id="request-a",
            finished=True,
            trace_headers={"vendor": "keep"},
        )
        return ({0: SimpleNamespace(outputs=[output])}, True)


def test_engine_core_attaches_stages_and_times_executor_future():
    stages.install_engine_core(FakeEngineCore)
    engine = FakeEngineCore()
    engine.step()  # First step wraps the executor instance in place.
    stages._REGISTRY["request-a"] = stages._new_record()
    scheduler_output = SimpleNamespace(num_scheduled_tokens={"request-a": 2})
    future = engine.model_executor.execute_model(scheduler_output)
    assert future.result() == "model-output"
    outputs, executed = engine.step()
    assert executed is True
    assert "request-a" not in stages._REGISTRY
    final = outputs[0].outputs[0]
    assert final.trace_headers["vendor"] == "keep"
    payload = json.loads(final.trace_headers[stages.STAGES_HEADER])
    assert payload["execute_steps"] == 1
    assert payload["execute_seconds"] >= 0.0


def finished_raw(payload, *, with_clock=True):
    raw = SimpleNamespace(_observer_stages=payload)
    if with_clock:
        raw._observer_clock = {
            CLOCK_MONO: "1010.0",
            CLOCK_UNIX: str(EPOCH_NS + 10 * SECOND),
            CLOCK_ERROR: "40",
        }
    return raw


def engine_payload():
    return json.dumps(
        {
            "waiting_enter": 1001.5,
            "first_schedule": 1002.5,
            "schedule_steps": 3,
            "prefill_chunks": 2,
            "decode_steps": 1,
            "schedule_seconds": 0.003,
            "execute_seconds": 0.09,
            "execute_steps": 3,
        }
    )


def test_record_engine_stages_merges_aggregates_and_waiting_interval(monkeypatch):
    trace = RequestTrace("c" * 32, started_ns=EPOCH_NS)
    clock = [EPOCH_NS + SECOND]
    monkeypatch.setattr(trace, "now_ns", lambda: clock[0])
    generation = trace.begin_generation("request-a")
    clock[0] = EPOCH_NS + 12 * SECOND
    stages.record_engine_stages(trace, generation, finished_raw(engine_payload()))
    assert trace.stages["schedule_total"].count == 3
    assert trace.stages["execute_model"].total_seconds == pytest.approx(0.09)
    metadata = generation["metadata"]
    assert metadata["schedule_steps"] == 3
    assert metadata["prefill_chunks"] == 2
    assert metadata["decode_steps"] == 1
    waiting = trace.observations[1]
    assert waiting["name"] == "waiting_to_first_schedule"
    assert waiting["parent_id"] == generation["id"]
    assert waiting["start_ns"] == EPOCH_NS + int(1.5 * SECOND)
    assert waiting["end_ns"] == EPOCH_NS + int(2.5 * SECOND)
    assert waiting["metadata"]["calibration_error_ns"] == 40


def test_record_engine_stages_tolerates_missing_clock(monkeypatch):
    trace = RequestTrace("d" * 32, started_ns=EPOCH_NS)
    clock = [EPOCH_NS + SECOND]
    monkeypatch.setattr(trace, "now_ns", lambda: clock[0])
    generation = trace.begin_generation("request-a")
    stages.record_engine_stages(
        trace, generation, finished_raw(engine_payload(), with_clock=False)
    )
    assert "schedule_total" in trace.stages
    assert (
        generation["metadata"]["waiting_unavailable"]
        == "missing_clock_or_timestamps"
    )
