"""Opt-in, delayed instrumentation without importing device-dependent modules.

Only the listed vLLM module loaders are wrapped. The original module executes
first; observer failures are isolated from its import and application startup.
"""

import functools
import importlib.abc
import logging
import os
import sys

logger = logging.getLogger("vllm_observer")

CORE = "vllm.v1.engine.core"
ENGINE = "vllm.v1.engine.async_llm"
STATS = "vllm.v1.metrics.stats"
APPS = {
    "vllm.entrypoints.launchers.app",
    "vllm.entrypoints.openai.api_server",
}
TARGETS = {CORE, ENGINE, STATS, *APPS}


class OptionalObserverMiddleware:
    """Keep ASGI startup usable when optional observer setup is unavailable."""

    _observer_middleware = True

    def __init__(self, app):
        self.app = app
        try:
            from .middleware import ObserverMiddleware

            # The delayed engine hook installs compatible instrumentation.
            self.app = ObserverMiddleware(app, instrument=False)
        except Exception:
            logger.warning(
                "Observer middleware unavailable; serving continues", exc_info=True
            )

    async def __call__(self, scope, receive, send):
        await self.app(scope, receive, send)


def _instrument_app(module):
    original = getattr(module, "build_app", None)
    if original is None or getattr(original, "_observer_bootstrap", False):
        return

    @functools.wraps(original)
    def build_app(*args, **kwargs):
        app = original(*args, **kwargs)
        if os.getenv("OBSERVER_HTTP_ENABLED", "1") != "1":
            return app
        try:
            configured = [item.cls for item in app.user_middleware]
            if not any(
                getattr(cls, "_observer_middleware", False)
                or (
                    cls.__module__ == "vllm_observer.middleware"
                    and cls.__name__ == "ObserverMiddleware"
                )
                for cls in configured
            ):
                app.add_middleware(OptionalObserverMiddleware)
        except Exception:
            logger.warning(
                "Observer ASGI hook unavailable; serving continues", exc_info=True
            )
        return app

    build_app._observer_bootstrap = True
    module.build_app = build_app


def _instrument(module):
    """Patch only classes already loaded; never import vLLM from a callback."""
    try:
        if module.__name__ in APPS:
            _instrument_app(module)
        if os.getenv("OBSERVER_ENGINE_ENABLED", "1") != "1":
            return
        if (
            module.__name__ == CORE
            and os.getenv("OBSERVER_LANGFUSE_ENABLED", "0") == "1"
        ):
            from .engine_clock import install as install_clock

            engine_cls = module.EngineCore
            if not all(
                callable(getattr(engine_cls, name, None))
                for name in ("step", "step_with_batch_queue")
            ):
                raise RuntimeError("Unsupported vLLM EngineCore step contract")
            install_clock(engine_cls)
        if module.__name__ in (ENGINE, STATS):
            engine_cls = getattr(sys.modules.get(ENGINE), "AsyncLLM", None)
            stats_cls = getattr(sys.modules.get(STATS), "IterationStats", None)
            if engine_cls is not None and stats_cls is not None:
                from .adapter import install as install_adapter

                install_adapter(engine_cls, stats_cls)
    except Exception:
        logger.warning(
            "Observer hook unavailable for %s; import continues",
            module.__name__,
            exc_info=True,
        )


class _ObserverLoader:
    def __init__(self, original):
        self.original = original

    def create_module(self, spec):
        create = getattr(self.original, "create_module", None)
        return create(spec) if create else None

    def exec_module(self, module):
        # Preserve failures from vLLM itself; isolate only our instrumentation.
        self.original.exec_module(module)
        _instrument(module)

    def __getattr__(self, name):
        return getattr(self.original, name)


class _ObserverFinder(importlib.abc.MetaPathFinder):
    _observer_finder = True

    def find_spec(self, fullname, path=None, target=None):
        if fullname not in TARGETS:
            return None
        for finder in tuple(sys.meta_path):
            if finder is self:
                continue
            find_spec = getattr(finder, "find_spec", None)
            spec = find_spec(fullname, path, target) if find_spec else None
            if spec is not None:
                if spec.loader is not None and hasattr(spec.loader, "exec_module"):
                    spec.loader = _ObserverLoader(spec.loader)
                return spec
        return None


def install():
    """Register once per Python process; disabled means no import hook at all."""
    if os.getenv("OBSERVER_ENABLED", "0") != "1":
        return False
    if not any(getattr(finder, "_observer_finder", False) for finder in sys.meta_path):
        sys.meta_path.insert(0, _ObserverFinder())
    # Useful when explicitly invoked after imports. Startup activation is preferred
    # because references copied with `from ... import ...` cannot be replaced later.
    for name in TARGETS:
        module = sys.modules.get(name)
        spec = getattr(module, "__spec__", None)
        if module is not None and not getattr(spec, "_initializing", False):
            _instrument(module)
    return True
