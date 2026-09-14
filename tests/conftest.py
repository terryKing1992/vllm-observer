import pytest


@pytest.fixture(autouse=True)
def disable_external_push(monkeypatch):
    monkeypatch.setenv("OBSERVER_PUSH_ENABLED", "0")
