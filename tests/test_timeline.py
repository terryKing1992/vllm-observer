"""Device-free checks for calibrated request timelines and optional wrappers."""

import asyncio
from types import SimpleNamespace

import pytest

from vllm_observer import adapter, engine_clock
from vllm_observer.core import RequestTrace, current_request
from vllm_observer.engine_clock import CLOCK_ERROR, CLOCK_MONO, CLOCK_UNIX

EPOCH_NS = 1_700_000_000_000_000_000
SECOND = 1_000_000_000


def raw_stats(*, wall_ns=EPOCH_NS + 10 * SECOND, tokens=5):
    return SimpleNamespace(
        queued_ts=1001.0,
        scheduled_ts=1002.0,
        first_token_ts=1003.0,
        last_token_ts=1007.0,
        num_generation_tokens=tokens,
        _observer_clock={
            CLOCK_MONO: "1010.0",
            CLOCK_UNIX: str(wall_ns),
            CLOCK_ERROR: "40",
        },
    )


def finished_stats(request_id="request-a", tokens=5):
    return SimpleNamespace(
        request_id=request_id,
        num_prompt_tokens=12,
        num_generation_tokens=tokens,
        queued_time=1.0,
        prefill_time=1.0,
        decode_time=4.0,
    )


def timeline(monkeypatch, trace_id="a" * 32):
    trace = RequestTrace(trace_id, started_ns=EPOCH_NS)
    clock = [EPOCH_NS + SECOND]
    monkeypatch.setattr(trace, "now_ns", lambda: clock[0])
    generation = trace.begin_generation("request-a")
    clock[0] = EPOCH_NS + 12 * SECOND
    return trace, generation


@pytest.mark.parametrize("method", ["step", "step_with_batch_queue"])
def test_engine_wrapper_calibrates_only_final_outputs_and_preserves_result(
    monkeypatch, method
):
    original_headers = {"traceparent": "upstream", "vendor": "keep"}
    final = SimpleNamespace(finished=True, trace_headers=original_headers)
    unfinished = SimpleNamespace(finished=False, trace_headers={"vendor": "partial"})
    partial_headers = unfinished.trace_headers
    result = ({0: SimpleNamespace(outputs=[unfinished, final])}, True)
    calls = []

    class Engine:
        def step(self, argument=None):
            calls.append(("step", argument))
            return result

        def step_with_batch_queue(self, argument=None):
            calls.append(("step_with_batch_queue", argument))
            return result

    readings = iter([1_010_000_000_000, 1_010_000_000_040])
    monkeypatch.setattr(engine_clock.time, "monotonic_ns", lambda: next(readings))
    monkeypatch.setattr(engine_clock.time, "time_ns", lambda: EPOCH_NS)
    engine_clock.install(Engine)
    wrapped = getattr(Engine, method)
    engine_clock.install(Engine)
    assert getattr(Engine, method) is wrapped
    assert getattr(Engine(), method)(argument="preserved") is result
    assert calls == [(method, "preserved")]
    assert unfinished.trace_headers is partial_headers
    assert unfinished.trace_headers == {"vendor": "partial"}
    assert original_headers == {"traceparent": "upstream", "vendor": "keep"}
    assert final.trace_headers["traceparent"] == "upstream"
    assert final.trace_headers["vendor"] == "keep"
    assert float(final.trace_headers[CLOCK_MONO]) == pytest.approx(1010.00000002)
    assert final.trace_headers[CLOCK_UNIX] == str(EPOCH_NS)
    assert final.trace_headers[CLOCK_ERROR] == "40"


@pytest.mark.parametrize("batches", [None, {}, {0: SimpleNamespace(outputs=[])}])
def test_no_finished_output_does_not_read_clocks(monkeypatch, batches):
    def unexpected():
        pytest.fail("Calibration must be skipped when no request finished")

    monkeypatch.setattr(engine_clock.time, "monotonic_ns", unexpected)
    engine_clock.stamp_outputs(batches)


def test_source_clock_produces_nested_real_intervals_and_decode_summary(monkeypatch):
    trace, generation = timeline(monkeypatch)
    adapter.record_timeline(trace, generation, finished_stats(), raw_stats())
    generation["end_ns"] = trace.now_ns()
    assert generation["parent_id"] == trace.root_span_id
    assert generation["usage"] == {"input": 12, "output": 5}
    assert generation["completion_start_ns"] == EPOCH_NS + 3 * SECOND
    spans = trace.observations[1:]
    assert [span["name"] for span in spans] == ["queue", "prefill", "decode"]
    assert [(span["start_ns"], span["end_ns"]) for span in spans] == [
        (EPOCH_NS + SECOND, EPOCH_NS + 2 * SECOND),
        (EPOCH_NS + 2 * SECOND, EPOCH_NS + 3 * SECOND),
        (EPOCH_NS + 3 * SECOND, EPOCH_NS + 7 * SECOND),
    ]
    for span in spans:
        assert span["parent_id"] == generation["id"]
        assert generation["start_ns"] <= span["start_ns"] <= span["end_ns"]
        assert span["end_ns"] <= generation["end_ns"]
        assert span["metadata"]["calibration_error_ns"] == 40
        assert span["metadata"]["sequence_index"] == 0
    assert spans[-1]["metadata"]["count"] == 4
    assert spans[-1]["metadata"]["total_seconds"] == 4.0
    assert spans[-1]["metadata"]["mean_seconds"] == 1.0


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        ("missing_clock", "missing_engine_clock"),
        ("past_clock", "clock_skew_or_invalid_order"),
        ("future_clock", "clock_skew_or_invalid_order"),
        ("unordered", "clock_skew_or_invalid_order"),
        ("missing_timestamp", "incomplete_engine_timestamps"),
        ("nan_timestamp", "incomplete_engine_timestamps"),
    ],
)
def test_unavailable_timeline_never_invents_stage_spans(monkeypatch, change, reason):
    trace, generation = timeline(monkeypatch)
    raw = raw_stats()
    if change == "missing_clock":
        del raw._observer_clock
    elif change == "past_clock":
        raw._observer_clock[CLOCK_UNIX] = str(EPOCH_NS)
    elif change == "future_clock":
        raw._observer_clock[CLOCK_UNIX] = str(EPOCH_NS + 100 * SECOND)
    elif change == "unordered":
        raw.first_token_ts = raw.queued_ts
    elif change == "missing_timestamp":
        raw.queued_ts = 0.0
    else:
        raw.last_token_ts = float("nan")
    adapter.record_timeline(trace, generation, finished_stats(), raw)
    assert trace.observations == [generation]
    assert generation["metadata"]["timeline_unavailable"] == reason
    assert generation["usage"] == {"input": 12, "output": 5}


def test_single_token_has_zero_decode_duration_and_no_division_by_zero(monkeypatch):
    trace, generation = timeline(monkeypatch)
    raw = raw_stats(tokens=1)
    raw.last_token_ts = raw.first_token_ts
    adapter.record_timeline(trace, generation, finished_stats(tokens=1), raw)
    decode = trace.observations[-1]
    assert decode["name"] == "decode"
    assert decode["start_ns"] == decode["end_ns"]
    assert decode["metadata"]["count"] == 0
    assert decode["metadata"]["mean_seconds"] == 0


def test_parallel_sequences_have_distinct_spans_and_accumulated_usage(monkeypatch):
    trace, generation = timeline(monkeypatch)
    adapter.record_timeline(trace, generation, finished_stats(), raw_stats())
    adapter.record_timeline(
        trace, generation, finished_stats(tokens=3), raw_stats(tokens=3)
    )
    spans = trace.observations[1:]
    assert generation["usage"] == {"input": 24, "output": 8}
    assert generation["metadata"]["sequence_count"] == 2
    assert len({span["id"] for span in spans}) == 6
    assert [span["metadata"]["sequence_index"] for span in spans] == [0] * 3 + [1] * 3
    assert {span["parent_id"] for span in spans} == {generation["id"]}


def fake_stats_class():
    class Stats:
        def __init__(self):
            self.finished_requests = []

        def update_from_output(self, output, req_stats):
            req_stats.updated = True
            return "output-result"

        def update_from_finished_request(self, request_id, req_stats):
            self.finished_requests.append(
                finished_stats(request_id, req_stats.num_generation_tokens)
            )
            return "finished-result"

    return Stats


def test_concurrent_generations_keep_background_output_correlated(monkeypatch):
    Stats = fake_stats_class()
    traces = {key: RequestTrace(key * 32, started_ns=EPOCH_NS) for key in ("a", "b")}
    clocks = {key: EPOCH_NS + SECOND for key in traces}
    for key, trace in traces.items():
        monkeypatch.setattr(trace, "now_ns", lambda key=key: clocks[key])

    class Engine:
        async def generate(self, request_id):
            # Interleave generators, and emulate a background output-handler context.
            await asyncio.sleep(0)
            token = current_request.set(None)
            try:
                stats = Stats()
                raw = raw_stats(tokens=5 if request_id == "a" else 3)
                headers = raw._observer_clock
                del raw._observer_clock
                output = SimpleNamespace(finished=True, trace_headers=headers)
                clocks[request_id] = EPOCH_NS + 12 * SECOND
                assert stats.update_from_output(output=output, req_stats=raw) == (
                    "output-result"
                )
                assert raw.updated is True
                assert stats.update_from_finished_request(request_id, raw) == (
                    "finished-result"
                )
            finally:
                current_request.reset(token)
            yield output

    adapter.install(Engine, Stats)

    async def run_request(key):
        token = current_request.set(traces[key])
        try:
            outputs = [output async for output in Engine().generate(key)]
            assert len(outputs) == 1
        finally:
            current_request.reset(token)

    async def run():
        await asyncio.gather(*(run_request(key) for key in traces))

    asyncio.run(run())
    for key, trace in traces.items():
        assert trace.engine_requests == 1
        generation, *spans = trace.observations
        assert generation["metadata"]["request_id"] == key
        assert generation["usage"]["output"] == (5 if key == "a" else 3)
        assert generation["parent_id"] == trace.root_span_id
        assert len(spans) == 3
        assert {span["parent_id"] for span in spans} == {generation["id"]}
        assert generation["end_ns"] == EPOCH_NS + 12 * SECOND
    assert traces["a"].observations[0]["id"] != traces["b"].observations[0]["id"]


@pytest.mark.parametrize("ending", ["completed", "error", "cancelled", "closed"])
def test_generation_cleanup_drops_late_statistics(monkeypatch, ending):
    Stats = fake_stats_class()
    trace = RequestTrace("c" * 32, started_ns=EPOCH_NS)
    now = [EPOCH_NS + SECOND]
    monkeypatch.setattr(trace, "now_ns", lambda: now[0])
    closed = []

    class Engine:
        async def generate(self, request_id):
            try:
                now[0] = EPOCH_NS + 12 * SECOND
                Stats().update_from_finished_request(request_id, raw_stats())
                yield "first"
                if ending == "error":
                    raise RuntimeError("inference failed")
                if ending == "cancelled":
                    raise asyncio.CancelledError()
            finally:
                closed.append(True)

    adapter.install(Engine, Stats)

    async def run():
        token = current_request.set(trace)
        try:
            iterator = Engine().generate("cleanup")
            assert await anext(iterator) == "first"
            if ending == "closed":
                await iterator.aclose()
            else:
                exception = {
                    "completed": StopAsyncIteration,
                    "error": RuntimeError,
                    "cancelled": asyncio.CancelledError,
                }[ending]
                with pytest.raises(exception):
                    await anext(iterator)
            Stats().update_from_finished_request("cleanup", raw_stats())
        finally:
            current_request.reset(token)

    asyncio.run(run())
    assert closed == [True]
    assert trace.engine_requests == 1
    assert len(trace.observations) == 4
    generation = trace.observations[0]
    assert generation["end_ns"] == EPOCH_NS + 12 * SECOND
    if ending != "completed":
        assert generation["metadata"]["status"] == (
            "error" if ending == "error" else "cancelled"
        )
