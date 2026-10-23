# SPDX-License-Identifier: Apache-2.0
"""Unit tests for :class:`openlifu_sdk.ui.base_connector.BaseConnector`'s
QML contract, on whichever Qt binding :mod:`openlifu_sdk.ui._qt` selects.

The PyQt6 CI job runs these on PyQt6; the PySide6 job sets
``OPENLIFU_QT_API=pyside6`` and runs them on PySide6 (#76).
"""

from __future__ import annotations

import threading
import time

import pytest

_qt = pytest.importorskip("openlifu_sdk.ui._qt")
QCoreApplication = _qt.import_qt_module("QtCore").QCoreApplication

from openlifu_sdk.ui.base_connector import BaseConnector, ConnectorState  # noqa: E402 -- reason: after the Qt importorskip
from openlifu_sdk.ui.telemetry_thread import TelemetryPollThread  # noqa: E402 -- reason: after the Qt importorskip


@pytest.fixture(scope="module")
def qt_app():
    return QCoreApplication.instance() or QCoreApplication([])


def _notify_signal_name(obj, prop: str) -> str:
    meta = obj.metaObject()
    meta_prop = meta.property(meta.indexOfProperty(prop))
    assert meta_prop.hasNotifySignal(), prop
    return bytes(meta_prop.notifySignal().name().data()).decode()


def test_qml_properties_are_readable_through_the_meta_object(qt_app):
    connector = BaseConnector()
    assert connector.property("txConnected") is False
    assert connector.property("hvConnected") is False
    assert connector.property("state") == int(ConnectorState.DISCONNECTED)
    assert _notify_signal_name(connector, "txConnected") == "connectionStatusChanged"
    assert _notify_signal_name(connector, "hvConnected") == "connectionStatusChanged"
    assert _notify_signal_name(connector, "state") == "stateChanged"


def test_connect_slots_driven_by_a_signal_update_properties_and_notify(qt_app):
    class _Source(_qt.QObject):
        connected = _qt.Signal(str, str)
        disconnected = _qt.Signal(str, str)

    source = _Source()
    connector = BaseConnector()
    source.connected.connect(connector.on_connected)
    source.disconnected.connect(connector.on_disconnected)
    status_changes, states = [], []
    connector.connectionStatusChanged.connect(lambda: status_changes.append(True))
    connector.stateChanged.connect(states.append)

    source.connected.emit("TX", "COM3")
    assert connector.property("txConnected") is True
    assert connector.property("state") == int(ConnectorState.DISCONNECTED)
    source.connected.emit("HV", "COM4")
    assert connector.property("hvConnected") is True
    assert connector.property("state") == int(ConnectorState.CONNECTED)
    source.disconnected.emit("TX", "COM3")
    assert connector.property("txConnected") is False

    assert len(status_changes) == 3
    assert states == [int(ConnectorState.CONNECTED), int(ConnectorState.DISCONNECTED)]


def test_poll_thread_signals_are_delivered_on_the_main_thread_loop(qt_app):
    """A signal emitted from the poll thread reaches a main-thread
    receiver only through the event loop, on the main thread."""

    class _Polling(BaseConnector):
        tick = _qt.Signal(int)

        def __init__(self):
            super().__init__()
            self._count = 0

        def poll_pre_tick(self):
            self._count += 1
            self.tick.emit(self._count)

    class _Sink(_qt.QObject):
        def __init__(self):
            super().__init__()
            self.received = []

        @_qt.Slot(int)
        def on_tick(self, n):
            self.received.append((n, threading.get_ident()))

    connector = _Polling()
    sink = _Sink()
    connector.tick.connect(sink.on_tick)
    thread = TelemetryPollThread(connector, interval_s=0.01)
    thread.start()
    try:
        deadline = time.monotonic() + 5.0
        while len(sink.received) < 2 and time.monotonic() < deadline:
            qt_app.processEvents()
            time.sleep(0.005)
    finally:
        thread.stop()
        stopped = thread.wait(2000)
    assert stopped, "poll thread did not stop"
    assert len(sink.received) >= 2, sink.received
    main = threading.get_ident()
    assert all(ident == main for _, ident in sink.received)
    assert [n for n, _ in sink.received[:2]] == [1, 2]
