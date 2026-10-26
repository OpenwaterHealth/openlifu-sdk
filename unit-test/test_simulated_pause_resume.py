from __future__ import annotations

import time

import pytest

_qt = pytest.importorskip("openlifu_sdk.ui._qt")
QCoreApplication = _qt.import_qt_module("QtCore").QCoreApplication

from openlifu_sdk.io.LIFUInterface import LIFUInterfaceStatus  # noqa: E402
from openlifu_sdk.io.exceptions import LIFUDeviceError  # noqa: E402
from openlifu_sdk.ui.simulated_interface import SimulatedLIFUInterface  # noqa: E402


@pytest.fixture(scope="module")
def qt_app():
    return QCoreApplication.instance() or QCoreApplication([])


def _make_running_interface(timeout_s=1800.0):
    interface = SimulatedLIFUInterface(treatment_timeout_s=timeout_s)
    interface.txdevice.set_trigger(
        pulse_interval=0.001,
        pulse_count=1,
        pulse_train_interval=0.1,
        pulse_train_count=20,
    )
    interface.hvcontroller._hv_on = True
    interface.start_sonication(turn_hv_on=False, wait_for_settle=False)
    return interface


def _wait_until(qt_app, predicate, timeout=1.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        qt_app.processEvents()
        if predicate():
            return True
        time.sleep(0.005)
    return predicate()


def test_simulated_pause_resume_preserves_progress_and_hv(qt_app):
    interface = _make_running_interface()
    try:
        assert _wait_until(qt_app, lambda: interface.txdevice.get_trigger()["train_count"] >= 1)
        assert interface.pause_sonication()
        paused_count = interface.txdevice.get_trigger()["train_count"]
        assert interface.txdevice.get_trigger()["trigger_status"] == "PAUSED"
        assert interface.get_status() == LIFUInterfaceStatus.STATUS_PAUSED
        assert not interface.is_running()

        time.sleep(0.12)
        qt_app.processEvents()
        assert interface.txdevice.get_trigger()["train_count"] == paused_count
        assert interface.hvcontroller.get_hv_status()

        assert interface.resume_sonication()
        assert interface.txdevice.get_trigger()["trigger_status"] == "RUNNING"
        assert interface.get_status() == LIFUInterfaceStatus.STATUS_RUNNING
        assert _wait_until(
            qt_app,
            lambda: interface.txdevice.get_trigger()["train_count"] > paused_count,
        )
        assert interface.hvcontroller.get_hv_status()
    finally:
        interface.close()


def test_simulated_paused_run_delivers_every_train_once(qt_app):
    # SR-023 item 2/4: Resume delivers only the remaining trains, so a
    # paused run ends with the same count as an uninterrupted one.
    interface = _make_running_interface()
    try:
        assert _wait_until(qt_app, lambda: interface.txdevice.get_trigger()["train_count"] >= 3)
        interface.pause_sonication()
        time.sleep(0.15)
        interface.resume_sonication()

        assert _wait_until(
            qt_app,
            lambda: interface.txdevice.get_trigger()["trigger_status"] == "STOPPED",
            timeout=4.0,
        )
        assert interface.txdevice.get_trigger()["train_count"] == 20
    finally:
        interface.close()


def test_simulated_stop_from_paused(qt_app):
    interface = _make_running_interface()
    try:
        interface.pause_sonication()
        assert interface.stop_sonication()

        assert interface.txdevice.get_trigger()["trigger_status"] == "STOPPED"
        assert not interface.hvcontroller.get_hv_status()
        with pytest.raises(LIFUDeviceError):
            interface.resume_sonication()
    finally:
        interface.close()


def test_simulated_timeout_runs_while_paused_and_rejects_resume(qt_app):
    interface = _make_running_interface(timeout_s=0.12)
    try:
        assert interface.pause_sonication()
        paused_count = interface.txdevice.get_trigger()["train_count"]

        assert _wait_until(
            qt_app,
            lambda: interface.txdevice.get_trigger()["trigger_status"] == "TIMEOUT",
        )
        trigger = interface.txdevice.get_trigger()
        assert trigger["train_count"] == paused_count
        assert interface.get_status() == LIFUInterfaceStatus.STATUS_TIMEOUT
        assert not interface.is_running()
        with pytest.raises(LIFUDeviceError):
            interface.resume_sonication()
    finally:
        interface.close()


def test_simulated_pause_and_resume_reject_invalid_states(qt_app):
    interface = SimulatedLIFUInterface()
    try:
        with pytest.raises(LIFUDeviceError):
            interface.pause_sonication()
        with pytest.raises(LIFUDeviceError):
            interface.resume_sonication()
    finally:
        interface.close()
