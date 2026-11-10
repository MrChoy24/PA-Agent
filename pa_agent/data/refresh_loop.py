"""1 Hz data refresh loop running on a dedicated QThread."""
from __future__ import annotations

import logging
import threading
import time
from typing import TYPE_CHECKING

from pa_agent.data.base import DataSource, DataSourceTransientError, KlineBar
from pa_agent.data.snapshot import INDICATOR_WARMUP_BARS

if TYPE_CHECKING:
    from pa_agent.util.threading import CancelToken

logger = logging.getLogger(__name__)

from PyQt6.QtCore import QThread, pyqtSignal, QObject


class RefreshLoop(QThread):
    """Fetches the latest K-line snapshot every *interval_ms* milliseconds.

    Signals
    -------
    frame_ready(list[KlineBar])
        Emitted after each successful fetch with the raw bar list (newest-first).
    status_changed(str)
        Emitted with a human-readable status string (e.g. "数据延迟").
    """

    frame_ready = pyqtSignal(list)
    status_changed = pyqtSignal(str)

    # Backoff constants
    _MAX_BACKOFF_S = 10.0       # cap exponential backoff at 10 seconds
    _BACKOFF_BASE_S = 0.5      # initial backoff = 0.5s, doubles each failure

    def __init__(
        self,
        data_source: DataSource,
        n_bars: int,
        interval_ms: int = 1000,
        cancel_token: "CancelToken | None" = None,
        parent: "QObject | None" = None,
    ) -> None:
        super().__init__(parent)
        self._source = data_source
        self._n_bars = n_bars
        self._interval_ms = interval_ms
        self._cancel_token = cancel_token
        self._consecutive_failures = 0
        self._failure_threshold_s = 5.0
        self._in_flight = False  # guard against overlapping fetches
        # Interruptible wait: _stop_refresh_loop() joins this thread on the GUI
        # thread, so a plain time.sleep() here would freeze the UI for up to
        # _MAX_BACKOFF_S (plus the fetch itself) on every symbol/source switch.
        self._wake = threading.Event()

    def request_stop(self) -> None:
        """Wake the loop immediately so it can observe the cancel token."""
        self._wake.set()

    def _pause(self, seconds: float) -> None:
        """Sleep for *seconds*, but return early once ``request_stop()`` fires."""
        if seconds > 0:
            self._wake.wait(seconds)
        self._wake.clear()

    def run(self) -> None:  # noqa: C901
        """Main loop — runs on the worker thread."""
        failure_start: float | None = None

        while True:
            if self._cancel_token is not None and self._cancel_token.is_set():
                logger.debug("RefreshLoop cancelled")
                break

            # Skip this tick if a previous fetch is still in flight.
            # This prevents overlapping REST fetches that trip exchange
            # rate limits on the public kline endpoints.
            if self._in_flight:
                self._pause(0.5)
                continue

            t0 = time.monotonic()
            self._in_flight = True
            try:
                try:
                    bars = self._source.latest_snapshot(
                        self._n_bars + INDICATOR_WARMUP_BARS + 5
                    )
                    if self._consecutive_failures > 0:
                        # Clear any previous error message from the status bar.
                        self.status_changed.emit("")
                    self._consecutive_failures = 0
                    failure_start = None
                    if bars:
                        self.frame_ready.emit(bars)

                except DataSourceTransientError as exc:
                    logger.debug("RefreshLoop transient error: %s", exc)
                    self._consecutive_failures += 1
                    if failure_start is None:
                        failure_start = time.monotonic()
                    user_msg = str(exc).strip()
                    if user_msg:
                        self.status_changed.emit(user_msg)
                    elapsed = time.monotonic() - failure_start
                    if elapsed >= self._failure_threshold_s and not user_msg:
                        self.status_changed.emit("数据延迟")
                except Exception as exc:  # noqa: BLE001
                    logger.error("RefreshLoop unexpected error: %s", exc, exc_info=True)
            finally:
                self._in_flight = False

            # Exponential backoff on repeated failures to avoid hammering
            # the exchange REST endpoints
            if self._consecutive_failures > 0:
                backoff_s = min(
                    self._BACKOFF_BASE_S * (2 ** (self._consecutive_failures - 1)),
                    self._MAX_BACKOFF_S,
                )
                logger.debug(
                    "RefreshLoop backoff %.1fs after %d consecutive failure(s)",
                    backoff_s,
                    self._consecutive_failures,
                )
                self._pause(backoff_s)
                continue

            elapsed_ms = (time.monotonic() - t0) * 1000
            sleep_ms = max(0.0, self._interval_ms - elapsed_ms)
            self._pause(sleep_ms / 1000.0)

