"""A tiny event bus stand-in for headless runs.

``pa_agent.records.pending_writer`` only ever calls ``event_bus.emit(name,
payload)``; the Qt ``EventBus`` in ``pa_agent.util.event_bus`` exists purely to
fan out to widgets.  The daemon therefore needs nothing but a logger-backed
sink, which keeps PyQt6 out of the import graph.
"""
from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


class LoggingEventBus:
    """Minimal duck-typed replacement for :class:`pa_agent.util.event_bus.EventBus`."""

    def __init__(self, logger_: logging.Logger | None = None) -> None:
        self._logger = logger_ or logger

    def emit(self, name: str, payload: Any = None) -> None:
        try:
            if name in ("disk_error", "exception"):
                self._logger.error("event %s: %s", name, payload)
            else:
                self._logger.debug("event %s: %s", name, payload)
        except Exception:  # noqa: BLE001 - never let logging break the loop
            pass

    # Compatibility shims: some call sites check for these names.
    def emit_status(self, text: str) -> None:
        self.emit("status", {"text": text})

    def emit_exception(self, exc: BaseException) -> None:
        self.emit("exception", {"type": type(exc).__name__, "message": str(exc)})
