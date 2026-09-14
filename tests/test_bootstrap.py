"""Exercise Python's real startup/import path without torch or accelerator hardware."""

import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

from vllm_observer import bootstrap

ROOT = Path(__file__).resolve().parents[1]


def run_python(code, *, enabled, extra_path=None, **extra_env):
    env = {**os.environ, **extra_env, "OBSERVER_ENABLED": enabled}
    env["PYTHONPATH"] = os.pathsep.join(
        str(path) for path in (ROOT / "bootstrap", ROOT / "src", extra_path) if path
    )
    return subprocess.run(
        [sys.executable, "-c", code],
        env=env,
        capture_output=True,
        text=True,
        timeout=20,
        check=True,
    )


@pytest.mark.parametrize("enabled", ["0", "1"])
def test_startup_never_eagerly_imports_device_modules_or_starts_threads(enabled):
    output = run_python(
        "import json, sys, threading; "
        "print(json.dumps({'vllm': any(k == 'vllm' or k.startswith('vllm.') "
        "for k in sys.modules), 'torch': 'torch' in sys.modules, "
        "'observer': 'vllm_observer.bootstrap' in sys.modules, "
        "'threads': len(threading.enumerate())}))",
        enabled=enabled,
    )
    assert json.loads(output.stdout) == {
        "vllm": False,
        "torch": False,
        "observer": enabled == "1",
        "threads": 1,
    }


def make_fake_vllm(path):
    modules = {
        "vllm/v1/engine/core.py": """
class EngineCore:
    def step(self):
        return {}, False
    def step_with_batch_queue(self):
        return {}, False
""",
        "vllm/v1/metrics/stats.py": """
class IterationStats:
    def update_from_finished_request(self, request_id):
        pass
""",
        "vllm/v1/engine/async_llm.py": """
from vllm.v1.metrics.stats import IterationStats
class AsyncLLM:
    async def generate(self, request_id):
        yield request_id
""",
        "vllm/entrypoints/launchers/app.py": """
from types import SimpleNamespace
class App:
    def __init__(self):
        self.user_middleware = []
    def add_middleware(self, cls):
        self.user_middleware.append(SimpleNamespace(cls=cls))
def build_app():
    return App()
""",
        "vllm/entrypoints/openai/api_server.py": """
from vllm.entrypoints.launchers.app import build_app
""",
    }
    for relative, source in modules.items():
        target = path / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        for parent in target.parents:
            if parent == path:
                break
            (parent / "__init__.py").touch()
        target.write_text(source, encoding="utf-8")


def test_real_delayed_imports_patch_once_and_automatically_add_middleware(tmp_path):
    make_fake_vllm(tmp_path)
    output = run_python(
        """
import json, threading
from vllm.v1.engine.core import EngineCore
from vllm.v1.engine.async_llm import AsyncLLM
from vllm.entrypoints.openai.api_server import build_app
from vllm_observer.bootstrap import install
first_step, first_generate = EngineCore.step, AsyncLLM.generate
install()
app = build_app()
print(json.dumps({
    'core': bool(getattr(EngineCore.step, '_observer_clock', False)),
    'engine': bool(getattr(AsyncLLM.generate, '_observer', False)),
    'same': first_step is EngineCore.step and first_generate is AsyncLLM.generate,
    'middleware': len(app.user_middleware),
    'threads': len(threading.enumerate()),
}))
""",
        enabled="1",
        extra_path=tmp_path,
        OBSERVER_LANGFUSE_ENABLED="1",
    )
    assert json.loads(output.stdout) == {
        "core": True,
        "engine": True,
        "same": True,
        "middleware": 1,
        "threads": 1,
    }


def test_disabled_hooks_leave_imported_vllm_untouched(tmp_path):
    make_fake_vllm(tmp_path)
    output = run_python(
        """
import json
from vllm.v1.engine.core import EngineCore
from vllm.v1.engine.async_llm import AsyncLLM
from vllm.entrypoints.launchers.app import build_app
print(json.dumps([hasattr(EngineCore.step, '_observer_clock'),
                  hasattr(AsyncLLM.generate, '_observer'),
                  len(build_app().user_middleware)]))
""",
        enabled="0",
        extra_path=tmp_path,
        OBSERVER_LANGFUSE_ENABLED="1",
    )
    assert json.loads(output.stdout) == [False, False, 0]


def test_incompatible_engine_still_imports(tmp_path):
    make_fake_vllm(tmp_path)
    (tmp_path / "vllm/v1/engine/core.py").write_text(
        "class EngineCore:\n    pass\n",
        encoding="utf-8",
    )
    output = run_python(
        "from vllm.v1.engine.core import EngineCore; print('model can start')",
        enabled="1",
        extra_path=tmp_path,
        OBSERVER_LANGFUSE_ENABLED="1",
    )
    assert "model can start" in output.stdout
    assert "Unsupported vLLM EngineCore" in output.stderr


def test_original_vllm_import_error_is_preserved(tmp_path):
    make_fake_vllm(tmp_path)
    (tmp_path / "vllm/v1/engine/core.py").write_text(
        "raise RuntimeError('real engine startup failure')\n", encoding="utf-8"
    )
    with pytest.raises(subprocess.CalledProcessError) as failed:
        run_python(
            "from vllm.v1.engine.core import EngineCore",
            enabled="1",
            extra_path=tmp_path,
            OBSERVER_LANGFUSE_ENABLED="1",
        )
    assert "real engine startup failure" in failed.value.stderr


def test_engine_instrumentation_can_be_disabled_independently(tmp_path):
    make_fake_vllm(tmp_path)
    output = run_python(
        """
import json
from vllm.v1.engine.core import EngineCore
from vllm.v1.engine.async_llm import AsyncLLM
from vllm.entrypoints.launchers.app import build_app
print(json.dumps([hasattr(EngineCore.step, '_observer_clock'),
                  hasattr(AsyncLLM.generate, '_observer'),
                  len(build_app().user_middleware)]))
""",
        enabled="1",
        extra_path=tmp_path,
        OBSERVER_ENGINE_ENABLED="0",
        OBSERVER_LANGFUSE_ENABLED="1",
    )
    assert json.loads(output.stdout) == [False, False, 1]


def test_observer_dependency_failure_does_not_prevent_startup(tmp_path):
    observer = tmp_path / "vllm_observer"
    observer.mkdir()
    (observer / "__init__.py").write_text("raise ImportError('optional missing')")
    env = {**os.environ, "OBSERVER_ENABLED": "1"}
    env["PYTHONPATH"] = os.pathsep.join((str(ROOT / "bootstrap"), str(tmp_path)))
    result = subprocess.run(
        [sys.executable, "-c", "print('model can start')"],
        env=env,
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=20,
        check=True,
    )
    assert "model can start" in result.stdout
    assert "optional missing" in result.stderr


def test_middleware_initialization_failure_falls_back(monkeypatch):
    from vllm_observer import middleware

    def broken(*args, **kwargs):
        raise ImportError("optional dependency missing")

    monkeypatch.setattr(middleware, "ObserverMiddleware", broken)
    calls = []

    async def app(scope, receive, send):
        calls.append(scope)

    wrapped = bootstrap.OptionalObserverMiddleware(app)
    assert wrapped.app is app
    asyncio.run(wrapped({"type": "lifespan"}, None, None))
    assert calls == [{"type": "lifespan"}]


def test_already_configured_middleware_is_not_added_again():
    class Configured:
        _observer_middleware = True

    app = SimpleNamespace(user_middleware=[SimpleNamespace(cls=Configured)])
    module = ModuleType("fake_app")
    module.build_app = lambda: app
    bootstrap._instrument_app(module)
    assert module.build_app() is app
    assert len(app.user_middleware) == 1
