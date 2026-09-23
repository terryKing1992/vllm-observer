"""Device-free checks for the pre-scheduling request deadline guard."""

import sys
import types
from types import SimpleNamespace

import pytest

from vllm_observer import deadline


class FakeRequestStatus:
    FINISHED_ABORTED = "aborted"


class FakeRequest:
    def __init__(self, request_id, arrival_time, finished=False):
        self.request_id = request_id
        self.arrival_time = arrival_time
        self._finished = finished

    def is_finished(self):
        return self._finished


class FakeScheduler:
    def __init__(self, requests):
        self.requests = requests
        self.finished = []

    def finish_requests(self, request_ids, status):
        self.finished.append((sorted(request_ids), status))
        for request_id in request_ids:
            self.requests[request_id]._finished = True
        return []

    def schedule(self, throttle_prefills=False):
        return SimpleNamespace(
            num_scheduled_tokens={
                rid: 1
                for rid, request in self.requests.items()
                if not request.is_finished()
            }
        )


class FakeSchedulerModule:
    Scheduler = FakeScheduler


@pytest.fixture(autouse=True)
def fake_vllm_request_module(monkeypatch):
    """Supply a stub vllm.v1.request so the lazy import stays device-free."""
    root = types.ModuleType("vllm")
    v1 = types.ModuleType("vllm.v1")
    request_module = types.ModuleType("vllm.v1.request")
    request_module.RequestStatus = FakeRequestStatus
    root.v1 = v1
    v1.request = request_module
    monkeypatch.setitem(sys.modules, "vllm", root)
    monkeypatch.setitem(sys.modules, "vllm.v1", v1)
    monkeypatch.setitem(sys.modules, "vllm.v1.request", request_module)


@pytest.fixture(autouse=True)
def reset_counter():
    deadline.dropped_total = 0
    yield
    deadline.dropped_total = 0


def test_expired_requests_are_dropped_before_scheduling(monkeypatch):
    monkeypatch.setenv("OBSERVER_REQUEST_DEADLINE_SECONDS", "30")
    monkeypatch.setattr(deadline.time, "time", lambda: 1000.0)
    deadline.install(FakeSchedulerModule)
    scheduler = FakeScheduler(
        {
            "fresh": FakeRequest("fresh", arrival_time=990.0),
            "stale-waiting": FakeRequest("stale-waiting", arrival_time=950.0),
            "already-done": FakeRequest("already-done", 950.0, finished=True),
        }
    )
    output = scheduler.schedule()
    assert scheduler.finished == [(["stale-waiting"], "aborted")]
    assert deadline.dropped_total == 1
    assert set(output.num_scheduled_tokens) == {"fresh"}


def test_guard_disabled_by_default(monkeypatch):
    monkeypatch.delenv("OBSERVER_REQUEST_DEADLINE_SECONDS", raising=False)
    scheduler = FakeScheduler({"stale": FakeRequest("stale", arrival_time=0.0)})
    assert deadline.drop_expired(scheduler) == []
    assert scheduler.finished == []


def test_invalid_ttl_disables_guard(monkeypatch):
    monkeypatch.setenv("OBSERVER_REQUEST_DEADLINE_SECONDS", "not-a-number")
    assert deadline.deadline_seconds() == 0.0


def test_finish_failure_is_isolated(monkeypatch):
    monkeypatch.setenv("OBSERVER_REQUEST_DEADLINE_SECONDS", "30")
    monkeypatch.setattr(deadline.time, "time", lambda: 1000.0)

    class Broken(FakeScheduler):
        def finish_requests(self, request_ids, status):
            raise RuntimeError("unsupported")

    scheduler = Broken({"stale": FakeRequest("stale", arrival_time=0.0)})
    assert deadline.drop_expired(scheduler) == []
    assert deadline.dropped_total == 0
