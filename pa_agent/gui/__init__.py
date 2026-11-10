"""PA Agent GUI package.

Imports are lazy (PEP 562) so that headless consumers — the daemon in
``pa_agent.daemon`` — can import leaf modules such as
``pa_agent.gui.order_opportunity`` (pure helpers) without dragging PyQt6 and
every widget into the process.  ``from pa_agent.gui import MainWindow`` keeps
working exactly as before.
"""
from __future__ import annotations

from typing import Any

__all__ = [
    "MainWindow",
    "SettingsDialog",
    "ChartWidget",
    "DecisionPanel",
    "ConversationWidget",
    "DebugWidget",
]

#: Public name -> submodule that defines it.
_LAZY: dict[str, str] = {
    "MainWindow": "pa_agent.gui.main_window",
    "SettingsDialog": "pa_agent.gui.settings_dialog",
    "ChartWidget": "pa_agent.gui.chart_widget",
    "DecisionPanel": "pa_agent.gui.decision_panel",
    "ConversationWidget": "pa_agent.gui.conversation_widget",
    "DebugWidget": "pa_agent.gui.debug_widget",
}


def __getattr__(name: str) -> Any:
    """Import GUI symbols on first access (PEP 562)."""
    module_path = _LAZY.get(name)
    if module_path is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

    import importlib

    value = getattr(importlib.import_module(module_path), name)
    globals()[name] = value  # cache so later lookups skip __getattr__
    return value


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(_LAZY))
