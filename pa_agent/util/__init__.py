"""PA Agent utility package.

Submodules are imported lazily (PEP 562) so that headless consumers — the
server daemon, the trading package, the orchestrators — do not drag in PyQt6
just because they need ``timefmt`` or ``threading``.

``EventBus`` is the only Qt-backed member here; importing it explicitly
(``from pa_agent.util.event_bus import EventBus``) still works as before.
"""
from __future__ import annotations

import importlib
from typing import Any

_LAZY = frozenset(
    {
        "CancelToken",
        "EventBus",
        "OrchestratorEvent",
        "configure_logging",
        "update_api_key",
    }
)

__all__ = [
    "CancelToken",
    "OrchestratorEvent",
    "EventBus",
    "configure_logging",
    "update_api_key",
]


def __getattr__(name: str) -> Any:
    if name not in _LAZY:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    if name in ("CancelToken", "OrchestratorEvent"):
        mod = importlib.import_module("pa_agent.util.threading")
    elif name == "EventBus":
        mod = importlib.import_module("pa_agent.util.event_bus")
    else:
        mod = importlib.import_module("pa_agent.util.logging")
    value = getattr(mod, name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(__all__)
