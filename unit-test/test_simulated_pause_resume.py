# SPDX-License-Identifier: Apache-2.0
"""Simulator Pause/Resume and treatment timeout (SR-023, SR-024).

The simulated engine mirrors the transmitter firmware
(openlifu-transmitter-fw ``feature/trigger-pause-resume``): a pause
lands at the next pulse-train boundary, or not at all if that train
completes the sequence; Resume continues with the count retained and
withdraws a pause that has not landed yet. The treatment timeout is a
total-time clock from Start that keeps running while paused.

Tests that depend on where a train boundary falls call the engine's
train tick directly rather than waiting for its timer.
"""
from __future__ import annotations

import time

import pytest

_qt = pytest.importorskip("openlifu_sdk.ui._qt")
QCoreApplication = _qt.import_qt_module("QtCore").QCoreApplication

from sr003_presets import flash_entry  # noqa: E402 -- reason: after the Qt importorskip

from openlifu_sdk.io.exceptions import LIFUDeviceError  # noqa: E402 -- reason: after the Qt importorskip
from openlifu_sdk.io.LIFUConfig import OW_TEMP_TOO_HIGH  # noqa: E402 -- reason: after the Qt importorskip
from openlifu_sdk.io.LIFUInterface import LIFUInterfaceStatus  # noqa: E402 -- reason: after the Qt importorskip
from openlifu_sdk.ui.simulated_interface import (  # noqa: E402 -- reason: after the Qt importorskip
    SimulatedDeviceInterface,
    SimulatedLIFUInterface,
)

TRAINS = 20
TRAIN_PERIOD_S = 0.1


@pytest.fixture(scope="module")
def qt_app():
    return QCoreApplication.instance() or QCoreApplication([])


def _make_running_interface(timeout_s=1800.0):
    interface = SimulatedLIFUInterface(treatment_timeout_s=timeout_s)
    interface.txdevice.set_trigger(
        pulse_interval=0.001,
        pulse_count=1,
        pulse_train_interval=TRAIN_PERIOD_S,
        pulse_train_count=TRAINS,
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


def _trigger(interface):
    return interface.txdevice.get_trigger()


def _train_boundary(interface):
    interface._engine._on_train_tick()


def test_pause_lands_at_the_next_train_boundary(qt_app):
    # SR-023 items 1-3, 9: until the boundary the firmware still reports
    # RUNNING; then PAUSED with the count held and HV left on.
    interface = _make_running_interface()
    try:
        assert interface.pause_sonication()
        assert _trigger(interface)["trigger_status"] == "RUNNING"
        assert interface.get_status() == LIFUInterfaceStatus.STATUS_RUNNING

        _train_boundary(interface)
        paused_count = _trigger(interface)["train_count"]
        assert _trigger(interface)["trigger_status"] == "PAUSED"
        assert interface.get_status() == LIFUInterfaceStatus.STATUS_PAUSED
        assert not interface.is_running()
        assert interface.pause_sonication()  # no-op while paused

        time.sleep(3 * TRAIN_PERIOD_S)
        qt_app.processEvents()
        assert _trigger(interface)["train_count"] == paused_count
        assert interface.hvcontroller.get_hv_status()

        assert interface.resume_sonication()
        assert _trigger(interface)["trigger_status"] == "RUNNING"
        assert interface.get_status() == LIFUInterfaceStatus.STATUS_RUNNING
        assert _wait_until(qt_app, lambda: _trigger(interface)["train_count"] > paused_count)
        assert interface.hvcontroller.get_hv_status()
    finally:
        interface.close()


def test_resume_before_the_pause_lands_withdraws_it(qt_app):
    interface = _make_running_interface()
    try:
        interface.pause_sonication()
        assert interface.resume_sonication()
        _train_boundary(interface)
        assert _trigger(interface)["trigger_status"] == "RUNNING"
    finally:
        interface.close()


def test_pause_on_the_final_train_lets_the_sequence_complete(qt_app):
    interface = _make_running_interface()
    try:
        engine = interface._engine
        engine._train_curr = TRAINS - 1
        interface.pause_sonication()
        _train_boundary(interface)

        assert _trigger(interface)["trigger_status"] == "STOPPED"
        assert _trigger(interface)["train_count"] == TRAINS
        assert interface.get_status() == LIFUInterfaceStatus.STATUS_FINISHED
        with pytest.raises(LIFUDeviceError):
            interface.resume_sonication()
    finally:
        interface.close()


def test_paused_run_delivers_every_train_exactly_once(qt_app):
    # SR-023 items 2, 4: Resume delivers only the remaining trains. A
    # resume that restarted the sequence would reset the count (the
    # trace would fall) and deliver more than TRAINS trains of heating.
    interface = _make_running_interface()
    heat_steps = []
    module = interface.txdevice._modules[0]
    real_heat_step = module.heat_step
    module.heat_step = lambda *a: (heat_steps.append(1), real_heat_step(*a))[1]
    trace = []

    def poll():
        trace.append(_trigger(interface)["train_count"])
        return _trigger(interface)["trigger_status"]

    try:
        assert _wait_until(qt_app, lambda: _trigger(interface)["train_count"] >= 3)
        interface.pause_sonication()
        assert _wait_until(qt_app, lambda: poll() == "PAUSED")
        interface.resume_sonication()
        assert _wait_until(qt_app, lambda: poll() == "STOPPED", timeout=4.0)

        assert trace == sorted(trace)
        assert trace[-1] == TRAINS
        assert len(heat_steps) == TRAINS
    finally:
        interface.close()


def test_resume_waits_a_full_train_period_before_the_next_train(qt_app):
    # The firmware honours the inter-train gap owed when the pause landed.
    interface = _make_running_interface()
    try:
        interface.pause_sonication()
        _train_boundary(interface)
        paused_count = _trigger(interface)["train_count"]

        interface.resume_sonication()
        time.sleep(TRAIN_PERIOD_S / 2)
        qt_app.processEvents()
        assert _trigger(interface)["train_count"] == paused_count
        assert _wait_until(qt_app, lambda: _trigger(interface)["train_count"] == paused_count + 1)
    finally:
        interface.close()


def test_stop_from_paused(qt_app):
    interface = _make_running_interface()
    try:
        interface.pause_sonication()
        _train_boundary(interface)
        assert interface.stop_sonication()

        assert _trigger(interface)["trigger_status"] == "STOPPED"
        assert not interface.hvcontroller.get_hv_status()
        with pytest.raises(LIFUDeviceError):
            interface.resume_sonication()
    finally:
        interface.close()


def test_timeout_runs_while_paused_and_rejects_resume(qt_app):
    # SR-024 items 2-4.
    interface = _make_running_interface(timeout_s=0.12)
    try:
        interface.pause_sonication()
        _train_boundary(interface)
        paused_count = _trigger(interface)["train_count"]

        assert _wait_until(qt_app, lambda: _trigger(interface)["trigger_status"] == "TIMEOUT")
        assert _trigger(interface)["train_count"] == paused_count
        assert interface.get_status() == LIFUInterfaceStatus.STATUS_TIMEOUT
        assert not interface.is_running()
        with pytest.raises(LIFUDeviceError):
            interface.resume_sonication()
    finally:
        interface.close()


def test_timeout_fires_while_running(qt_app):
    interface = _make_running_interface(timeout_s=0.12)
    try:
        assert _wait_until(qt_app, lambda: _trigger(interface)["trigger_status"] == "TIMEOUT")
        assert _trigger(interface)["train_count"] < TRAINS
    finally:
        interface.close()


@pytest.mark.parametrize("timeout_s", [-1.0, float("nan"), float("inf"), 3_000_000.0])
def test_timeout_out_of_range_is_refused(qt_app, timeout_s):
    with pytest.raises(ValueError):
        SimulatedLIFUInterface(treatment_timeout_s=timeout_s)


def test_pause_and_resume_are_refused_with_no_sequence(qt_app):
    interface = SimulatedLIFUInterface()
    try:
        with pytest.raises(LIFUDeviceError):
            interface.pause_sonication()
        with pytest.raises(LIFUDeviceError):
            interface.resume_sonication()
    finally:
        interface.close()


def test_resume_ignores_start_c_but_not_shutoff_c(qt_app):
    # SR-023 item 8, on the FDA simulator with a loaded preset
    # (start_C 35, shutoff_C 75).
    mc, regs = flash_entry()
    interface = SimulatedDeviceInterface(preset_flash=[(mc, regs)])
    try:
        interface.load_preset(0, mc["settings_crc"], regs, 400_000.0, duration_index=1)
        interface.start_sonication(turn_hv_on=True, wait_for_settle=False)
        module = interface.txdevice._modules[0]

        interface.pause_sonication()
        _train_boundary(interface)
        module.temp_c = 50.0
        assert interface.resume_sonication()

        interface.pause_sonication()
        _train_boundary(interface)
        module.temp_c = 80.0
        with pytest.raises(LIFUDeviceError) as excinfo:
            interface.resume_sonication()
        assert excinfo.value.device_error_code == OW_TEMP_TOO_HIGH
        assert _trigger(interface)["trigger_status"] == "PAUSED"
    finally:
        interface.close()
