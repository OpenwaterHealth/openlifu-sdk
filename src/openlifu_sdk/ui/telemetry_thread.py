"""Background QThread that periodically calls polling hooks on a
:class:`~openlifu_sdk.ui.base_connector.BaseConnector`.

A single thread covers both TX and HV polling so we don't have to
manage two thread lifecycles. The thread itself is dumb: it sleeps,
checks if monitoring is paused, and calls four hooks on the connector
that subclasses override to do the actual SDK queries (and emit any
QML-facing signals).

Using QThread (not threading.Thread) keeps Qt's queued cross-thread
signal delivery working correctly so signals emitted from poll hooks
land on the main thread's event loop.
"""

from __future__ import annotations

import logging
import threading

from PyQt6.QtCore import QThread

logger = logging.getLogger(__name__)


class TelemetryPollThread(QThread):
    """Polls connector hooks once per ``interval_s`` seconds.

    The connector must implement four no-arg methods (all default to
    no-ops in :class:`~openlifu_sdk.ui.base_connector.BaseConnector`):

    - ``poll_tx_tick()`` — called when ``txConnected`` is True and
      ``state != RUNNING``. Typically queries TX temperature / module
      count and emits signals.
    - ``poll_hv_tick()`` — called when ``hvConnected`` is True and
      ``state != RUNNING``. Typically queries power status / VMONs.
    - ``poll_pre_tick()`` — called once per cycle before the TX/HV
      hooks, regardless of connection state. Useful for connect
      attempts on platforms that don't use OWSignal autoconnect.
    - ``poll_post_tick()`` — called once per cycle after TX/HV. Useful
      for failure-counter book-keeping.

    The thread exits when :meth:`stop` is called (and the connector is
    expected to ``wait()`` on it during shutdown).
    """

    def __init__(self, connector, interval_s: float = 1.0):
        super().__init__()
        self._connector = connector
        self._interval_s = float(interval_s)
        self._stop_event = threading.Event()

    def run(self):  # noqa: D401 - QThread override
        conn = self._connector
        while not self._stop_event.wait(timeout=self._interval_s):
            if getattr(conn, "_monitoring_paused", False):
                continue
            try:
                if hasattr(conn, "poll_pre_tick"):
                    conn.poll_pre_tick()
                running = getattr(conn, "_state", 0) == getattr(conn, "RUNNING", -1)
                if not running:
                    if getattr(conn, "_txConnected", False):
                        if hasattr(conn, "poll_tx_tick"):
                            conn.poll_tx_tick()
                    if getattr(conn, "_hvConnected", False):
                        if hasattr(conn, "poll_hv_tick"):
                            conn.poll_hv_tick()
                if hasattr(conn, "poll_post_tick"):
                    conn.poll_post_tick()
            except Exception as exc:
                logger.warning("Telemetry poll loop error: %s", exc)

    def stop(self):
        """Signal the run loop to exit on its next wake."""
        self._stop_event.set()


__all__ = ["TelemetryPollThread"]
