# SPDX-License-Identifier: Apache-2.0
"""Unit tests for the polled sonication-progress fields on
:class:`SimulatedTxDevice.get_trigger` /
:meth:`SimulatedTxDevice.get_trigger_json`.

Added 2026-10-01 for the openlifu-operator-interface walking
skeleton's Running-state progress poll (PROJ-08 Task 1, OI #78).
Rather than subscribing to the ``signal_data_received`` STATUS
frames (which carry the same progress but via an async-emission
pipeline the walking skeleton deliberately doesn't consume), the
host reads ``train_count`` and ``trigger_status`` off a polled
:meth:`get_trigger` call.

Also exercises a pre-existing sim bug fix: on natural completion
of a sequence, the engine now flips the TxDevice's
``_trigger_running`` to False (previously only an explicit
``stop_trigger()`` did so, which only mattered for callers that
subscribed to STATUS frames rather than polling).
"""

from __future__ import annotations

import time

import pytest

pytest.importorskip("PyQt6")

from PyQt6.QtCore import QCoreApplication  # noqa: E402

from openlifu_sdk.ui.simulated_interface import (  # noqa: E402
    SimulatedLIFUInterface,
    SimulatedTxDevice,
)


@pytest.fixture(scope="module")
def qt_app():
    return QCoreApplication.instance() or QCoreApplication([])


# ------------------------------------------------------------------
# get_trigger / get_trigger_json: static state before an engine runs
# ------------------------------------------------------------------


def test_get_trigger_json_initial_state_has_train_count_zero():
    tx = SimulatedTxDevice()
    j = tx.get_trigger_json()
    assert j["TriggerStatus"] == "STOPPED"
    assert j["TrainCount"] == 0


def test_get_trigger_initial_state_mirrors_get_trigger_json():
    tx = SimulatedTxDevice()
    t = tx.get_trigger()
    assert t["trigger_status"] == "STOPPED"
    assert t["train_count"] == 0
    # pulse_train_count defaults to 1 per __init__.
    assert t["pulse_train_count"] == 1


def test_get_trigger_returns_snake_case_shape():
    """Snake-case keys matching the real ``LIFUTXDevice.get_trigger``
    shape plus the two new progress fields."""
    tx = SimulatedTxDevice()
    t = tx.get_trigger()
    for key in (
        "pulse_interval", "pulse_count", "pulse_width",
        "pulse_train_interval", "pulse_train_count",
        "mode", "profile_index", "profile_increment",
        "train_count", "trigger_status",
    ):
        assert key in t, f"missing key {key!r} in get_trigger() dict"


# ------------------------------------------------------------------
# Running sequence: train_count advances, trigger_status flips
# ------------------------------------------------------------------


def _short_sequence(iface: SimulatedLIFUInterface, train_count: int = 5):
    """Configure a very short sequence so the full sonication
    completes within a few hundred milliseconds."""
    iface.txdevice.set_trigger(
        pulse_interval=0.001,
        pulse_count=1,
        pulse_train_interval=0.02,  # 20 ms per train (clamped to 20ms min)
        pulse_train_count=train_count,
    )


def test_train_count_advances_during_running_sequence(qt_app):
    iface = SimulatedLIFUInterface()
    try:
        _short_sequence(iface, train_count=5)
        iface.start_sonication(turn_hv_on=False, wait_for_settle=False)

        # Poll trigger state until the sim reports STOPPED. Record
        # successive train_count values to confirm monotonic advance.
        values: list[int] = []
        deadline = time.monotonic() + 1.0
        while time.monotonic() < deadline:
            qt_app.processEvents()
            t = iface.txdevice.get_trigger()
            values.append(t["train_count"])
            if t["trigger_status"] == "STOPPED":
                break
            time.sleep(0.015)

        assert values[-1] == 5, (
            f"train_count should reach pulse_train_count on natural "
            f"completion; final value was {values[-1]}, full trace: {values}"
        )
        assert values == sorted(values), (
            f"train_count must be monotonically non-decreasing: {values}"
        )
        assert iface.txdevice.get_trigger()["trigger_status"] == "STOPPED"
    finally:
        iface.close()


def test_natural_completion_flips_trigger_status_to_stopped(qt_app):
    """Pre-2026-10-01 bug: natural completion of a sequence did
    not call ``_tx.stop_trigger()``, so a polled host would see
    ``TriggerStatus=RUNNING`` even after the sim finished. Fixed:
    the engine now flips the flag on natural completion."""
    iface = SimulatedLIFUInterface()
    try:
        _short_sequence(iface, train_count=3)
        iface.start_sonication(turn_hv_on=False, wait_for_settle=False)
        assert iface.txdevice.get_trigger()["trigger_status"] == "RUNNING"

        # Drive the sim engine to completion.
        deadline = time.monotonic() + 1.0
        while time.monotonic() < deadline:
            qt_app.processEvents()
            if iface.txdevice.get_trigger()["trigger_status"] == "STOPPED":
                break
            time.sleep(0.02)

        assert iface.txdevice.get_trigger()["trigger_status"] == "STOPPED"
        assert iface.txdevice.get_trigger()["train_count"] == 3
    finally:
        iface.close()


def test_operator_stop_flips_trigger_status_to_stopped(qt_app):
    """Explicit ``stop_sonication`` also flips trigger_status to
    STOPPED, matching the natural-completion edge so a polled
    host sees a single stop edge either way. ``train_count`` is
    left at whatever value the engine reached (so the host can
    display partial progress on a stopped-early treatment)."""
    iface = SimulatedLIFUInterface()
    try:
        # Long sequence so we can stop midway.
        iface.txdevice.set_trigger(
            pulse_interval=0.001,
            pulse_count=1,
            pulse_train_interval=0.05,
            pulse_train_count=100,
        )
        iface.start_sonication(turn_hv_on=False, wait_for_settle=False)

        # Let the engine run for a few ticks.
        deadline = time.monotonic() + 0.3
        while time.monotonic() < deadline:
            qt_app.processEvents()
            if iface.txdevice.get_trigger()["train_count"] >= 2:
                break
            time.sleep(0.02)

        mid_progress = iface.txdevice.get_trigger()["train_count"]
        assert 1 <= mid_progress < 100

        # Operator-initiated stop.
        iface.stop_sonication(turn_hv_off=False)
        qt_app.processEvents()

        final = iface.txdevice.get_trigger()
        assert final["trigger_status"] == "STOPPED"
        # train_count retained from where the engine got to -- not
        # reset to 0 and not advanced to pulse_train_count.
        assert mid_progress <= final["train_count"] < 100
    finally:
        iface.close()


# ------------------------------------------------------------------
# Fresh start resets train_count
# ------------------------------------------------------------------


def test_restart_resets_train_count(qt_app):
    """Running two sequences back-to-back: the second one starts
    counting from 0 again, not from where the first ended."""
    iface = SimulatedLIFUInterface()
    try:
        _short_sequence(iface, train_count=3)
        iface.start_sonication(turn_hv_on=False, wait_for_settle=False)
        deadline = time.monotonic() + 1.0
        while time.monotonic() < deadline:
            qt_app.processEvents()
            if iface.txdevice.get_trigger()["trigger_status"] == "STOPPED":
                break
            time.sleep(0.02)
        assert iface.txdevice.get_trigger()["train_count"] == 3

        # Second sonication.
        iface.start_sonication(turn_hv_on=False, wait_for_settle=False)
        qt_app.processEvents()
        # Immediately after start, counter is reset.
        assert iface.txdevice.get_trigger()["train_count"] == 0
        assert iface.txdevice.get_trigger()["trigger_status"] == "RUNNING"
    finally:
        iface.close()
