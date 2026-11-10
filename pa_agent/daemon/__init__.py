"""Headless daemon: 24/7 kline polling + two-stage AI analysis + notify + auto trade.

This package intentionally does **not** import PyQt6.  It reuses the same
engine modules as the desktop GUI (``pa_agent.data`` / ``pa_agent.ai`` /
``pa_agent.orchestrator`` / ``pa_agent.records`` / ``pa_agent.notify`` /
``pa_agent.trading``) so behaviour stays identical, only the presentation
layer is dropped.

Entry point::

    python -m pa_agent.daemon --once        # single pass, useful for smoke test
    python -m pa_agent.daemon               # run forever
    python -m pa_agent.daemon --health-check
"""
from __future__ import annotations

__all__ = ["DaemonConfig", "DaemonRunner", "main"]

_LAZY = {
    "DaemonConfig": "pa_agent.daemon.config",
    "DaemonRunner": "pa_agent.daemon.runner",
    "main": "pa_agent.daemon.__main__",
}


def __getattr__(name: str):
    module_name = _LAZY.get(name)
    if module_name is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    import importlib

    value = getattr(importlib.import_module(module_name), name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(__all__))
