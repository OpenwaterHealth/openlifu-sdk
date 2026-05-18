"""Minimal :class:`QObject` scaffolding that wires a
:class:`~openlifu_sdk.LIFUInterface` to QML.

Owns:

- The shared connection state machine (DISCONNECTED, CONNECTED, READY,
  RUNNING) and the signals QML binds against.
- The TX/HV connect/disconnect slots invoked by OWSignal (and by the
  :class:`~openlifu_sdk.ui.simulated_interface.SimulatedLIFUInterface`).
- The :class:`~openlifu_sdk.ui.telemetry_thread.TelemetryPollThread`
  lifecycle.

Does **not** own (subclasses add these):

- Voltage / preset / solution business logic.
- Per-tab slots (firmware update, register I/O, run engine, etc.).
- Session/run logging.
- Pinmap / preset_settings path resolution (those are app-specific).

Subclass and override:

- :meth:`_make_interface` — return a :class:`LIFUInterface` (or fake).
- :meth:`poll_tx_tick`, :meth:`poll_hv_tick` — periodic device queries.
- :meth:`update_state` — recompute :attr:`state` (default: ``CONNECTED``
  iff both TX and HV are connected).
"""

from __future__ import annotations

import logging
from enum import IntEnum
from typing import Optional

from PyQt6.QtCore import QObject, pyqtProperty, pyqtSignal, pyqtSlot

from openlifu_sdk.ui.telemetry_thread import TelemetryPollThread

logger = logging.getLogger(__name__)


class ConnectorState(IntEnum):
    """Connection / sonication state exposed to QML as :attr:`state`."""

    DISCONNECTED = 0
    CONNECTED = 1
    READY = 2
    RUNNING = 3


class BaseConnector(QObject):
    """Cross-tab base class. Apps subclass this to add tab-specific slots."""

    # Mirror enum on the class for QML convenience / subclass reuse.
    DISCONNECTED = ConnectorState.DISCONNECTED
    CONNECTED = ConnectorState.CONNECTED
    READY = ConnectorState.READY
    RUNNING = ConnectorState.RUNNING

    # ---- Signals ---------------------------------------------------------

    signalConnected = pyqtSignal(str, str)       # (descriptor, port)
    signalDisconnected = pyqtSignal(str, str)
    connectionStatusChanged = pyqtSignal()       # tx/hv flag flipped
    stateChanged = pyqtSignal(int)               # ConnectorState value

    # ---- Construction ----------------------------------------------------

    def __init__(self, parent: Optional[QObject] = None, *, poll_interval_s: float = 1.0):
        super().__init__(parent)
        self._txConnected = False
        self._hvConnected = False
        self._state: int = int(ConnectorState.DISCONNECTED)
        self._monitoring_paused = False
        self._poll_interval_s = float(poll_interval_s)
        self._poll_thread: Optional[TelemetryPollThread] = None
        self.interface = None  # populated by start()

    # ---- Lifecycle -------------------------------------------------------

    def start(self) -> None:
        """Build the interface, wire its OWSignals, start polling.

        Idempotent; subsequent calls are no-ops.
        """
        if self.interface is not None:
            return
        self.interface = self._make_interface()
        self._wire_interface_signals()
        self._poll_thread = TelemetryPollThread(self, interval_s=self._poll_interval_s)
        self._poll_thread.start()

    def close(self) -> None:
        """Stop polling and close the interface. Safe to call multiple times."""
        if self._poll_thread is not None:
            self._poll_thread.stop()
            if not self._poll_thread.wait(5000):
                logger.warning("Telemetry poll thread did not exit within 5s")
            self._poll_thread = None
        if self.interface is not None:
            try:
                self.interface.close()
            except Exception as exc:
                logger.debug("Error closing interface: %s", exc)
            self.interface = None

    # ---- Seams (override in subclasses) ----------------------------------

    def _make_interface(self):
        """Return a :class:`LIFUInterface` instance.

        Default returns a stock :class:`openlifu_sdk.LIFUInterface`.
        Override to pass non-default constructor args (e.g. test mode),
        or to return a simulated interface.
        """
        from openlifu_sdk import LIFUInterface

        return LIFUInterface()

    def _wire_interface_signals(self) -> None:
        """Connect the interface's OWSignals to :meth:`on_connected` /
        :meth:`on_disconnected`.

        The default implementation wires the standard
        ``txdevice.signal_connected/disconnected`` and ``hvcontroller.``
        equivalents found on the real interface and the simulator.
        Override to wire additional signals (data received, errors, etc.).
        """
        iface = self.interface
        for sub, descriptor in (("txdevice", "TX"), ("hvcontroller", "HV")):
            sub_obj = getattr(iface, sub, None)
            if sub_obj is None:
                continue
            for sig_name in ("signal_connected",):
                sig = getattr(sub_obj, sig_name, None)
                if sig is not None and hasattr(sig, "connect"):
                    sig.connect(self.on_connected)
            for sig_name in ("signal_disconnected",):
                sig = getattr(sub_obj, sig_name, None)
                if sig is not None and hasattr(sig, "connect"):
                    sig.connect(self.on_disconnected)

    # ---- Poll hooks (override in subclasses) -----------------------------

    def poll_pre_tick(self) -> None:
        """Called once per poll cycle before TX/HV hooks. Default: no-op."""

    def poll_tx_tick(self) -> None:
        """Called while TX is connected and not RUNNING. Default: no-op."""

    def poll_hv_tick(self) -> None:
        """Called while HV is connected and not RUNNING. Default: no-op."""

    def poll_post_tick(self) -> None:
        """Called once per poll cycle after TX/HV hooks. Default: no-op."""

    # ---- Connection slots ------------------------------------------------

    @pyqtSlot(str, str)
    def on_connected(self, descriptor: str, port: str) -> None:
        if descriptor == "TX":
            self._txConnected = True
        elif descriptor == "HV":
            self._hvConnected = True
        self.signalConnected.emit(descriptor, port)
        self.connectionStatusChanged.emit()
        self.update_state()

    @pyqtSlot(str, str)
    def on_disconnected(self, descriptor: str, port: str) -> None:
        if descriptor == "TX":
            self._txConnected = False
        elif descriptor == "HV":
            self._hvConnected = False
        self.signalDisconnected.emit(descriptor, port)
        self.connectionStatusChanged.emit()
        self.update_state()

    # ---- State machine ---------------------------------------------------

    def update_state(self) -> None:
        """Recompute :attr:`state`. Default: CONNECTED iff TX **and** HV are up.

        Subclasses extend this to handle READY (preset loaded) and
        RUNNING (sonication active) transitions.
        """
        new_state = (
            int(ConnectorState.CONNECTED)
            if (self._txConnected and self._hvConnected)
            else int(ConnectorState.DISCONNECTED)
        )
        self._set_state(new_state)

    def _set_state(self, new_state: int) -> None:
        if new_state != self._state:
            self._state = int(new_state)
            self.stateChanged.emit(self._state)

    # ---- QML-visible properties ------------------------------------------

    @pyqtProperty(bool, notify=connectionStatusChanged)
    def txConnected(self) -> bool:
        return self._txConnected

    @pyqtProperty(bool, notify=connectionStatusChanged)
    def hvConnected(self) -> bool:
        return self._hvConnected

    @pyqtProperty(int, notify=stateChanged)
    def state(self) -> int:
        return self._state


__all__ = ["BaseConnector", "ConnectorState"]
