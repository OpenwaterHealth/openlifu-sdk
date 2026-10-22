# SPDX-License-Identifier: Apache-2.0
"""Tests for the trigger-config application side of
:meth:`SimulatedTxDevice.load_preset`.

Mirrors the real firmware model: ``load_preset`` (OW_PRESET_LOAD)
programs the flash-baked preset, including its trigger + pulse
parameters, so a subsequent ``start_sonication`` runs the
correct sequence without a separate ``set_trigger`` call.

Added 2026-10-01 (companion to the OI #78 smoke test
revealing that loading a preset didn't update ``_sequence``,
so an immediate ``start_sonication`` ran the default 1-train
sequence instead of the operator's selected duration).
"""

from __future__ import annotations

import pytest

pytest.importorskip("PyQt6")

from sr003_presets import flash_entry  # noqa: E402 -- reason: after the PyQt6 importorskip

from openlifu_sdk.io.exceptions import LIFUDeviceError  # noqa: E402 -- reason: after the PyQt6 importorskip
from openlifu_sdk.ui.simulated_interface import SimulatedTxDevice  # noqa: E402 -- reason: after the PyQt6 importorskip


def _tx_with(**overrides):
    """A SimulatedTxDevice holding one compiled preset, plus that preset's
    (machine_config, regs_crc): the SR-003 CRCs, as a real image holds them."""
    mc, regs = flash_entry(**overrides)
    return SimulatedTxDevice(preset_flash=[(mc, regs)]), mc, regs


# ------------------------------------------------------------------
# Trigger config application
# ------------------------------------------------------------------


def test_load_preset_updates_sequence_from_machine_config():
    tx, mc, regs = _tx_with(
        pulse_interval_ms=25.0,
        pulse_count=50,
        pulse_train_interval_s=1.5,
        pulse_train_count_selections=[10, 20, 40],
    )

    # Pick the middle duration (index 1 -> 20 trains).
    tx.load_preset(0, regs, duration_index=1, settings_crc=mc["settings_crc"])

    assert tx._sequence["pulse_interval"] == pytest.approx(0.025)
    assert tx._sequence["pulse_count"] == 50
    assert tx._sequence["pulse_train_interval"] == pytest.approx(1.5)
    assert tx._sequence["pulse_train_count"] == 20


def test_load_preset_updates_pulse_duration_from_machine_config():
    # pulse_length_us is simulator-only (the image bakes the pulse into its
    # registers), so it rides on the flash entry next to the compiled preset.
    mc, regs = flash_entry()
    tx = SimulatedTxDevice(preset_flash=[(dict(mc, pulse_length_us=75.0), regs)])
    tx.load_preset(0, regs, duration_index=0, settings_crc=mc["settings_crc"])
    # pulse_length_us=75 -> _pulse["duration"] = 75e-6 s
    assert tx._pulse["duration"] == pytest.approx(75e-6)


def test_load_preset_reflects_through_get_trigger():
    tx, mc, regs = _tx_with(
        pulse_interval_ms=5.0,
        pulse_count=10,
        pulse_train_interval_s=0.1,
        pulse_train_count_selections=[2, 4, 8],
    )
    tx.load_preset(0, regs, duration_index=2, settings_crc=mc["settings_crc"])

    trig = tx.get_trigger()
    assert trig["pulse_interval"] == pytest.approx(0.005)
    assert trig["pulse_count"] == 10
    assert trig["pulse_train_interval"] == pytest.approx(0.1)
    assert trig["pulse_train_count"] == 8
    assert trig["train_count"] == 0
    assert trig["trigger_status"] == "STOPPED"


# ------------------------------------------------------------------
# Error paths
# ------------------------------------------------------------------


@pytest.mark.parametrize("bad_duration", [-1, 5])
def test_load_preset_rejects_bad_duration_index(bad_duration):
    tx, mc, regs = _tx_with(pulse_train_count_selections=[5, 10])
    with pytest.raises(LIFUDeviceError, match="duration index"):
        tx.load_preset(0, regs, duration_index=bad_duration, settings_crc=mc["settings_crc"])


def test_load_preset_crc_mismatch_leaves_sequence_unchanged():
    """SR-003 fault path: a CRC mismatch rejects the call and
    should not leak partial state into the sequence."""
    tx, mc, regs = _tx_with(
        pulse_interval_ms=10.0,
        pulse_count=5,
        pulse_train_interval_s=0.2,
        pulse_train_count_selections=[3],
    )
    sequence_before = dict(tx._sequence)
    pulse_before = dict(tx._pulse)
    for bad_regs, bad_settings in ((regs ^ 0xDEADBEEF, mc["settings_crc"]),
                                   (regs, mc["settings_crc"] ^ 0xDEADBEEF)):
        with pytest.raises(LIFUDeviceError):
            tx.load_preset(0, bad_regs, duration_index=0, settings_crc=bad_settings)
        assert tx._sequence == sequence_before
        assert tx._pulse == pulse_before
        assert tx.get_loaded_preset() == (None, None)


# ------------------------------------------------------------------
# Integration: start_sonication runs the loaded preset's sequence
# ------------------------------------------------------------------


def test_start_sonication_runs_preset_selected_sequence(qt_app):
    """End-to-end verification: load preset -> start_sonication ->
    engine ticks run the selected pulse_train_count, not the
    1-train default."""
    import time
    from openlifu_sdk.ui.simulated_interface import SimulatedDeviceInterface

    mc, regs = flash_entry(
        pulse_interval_ms=1.0,
        pulse_count=1,
        pulse_train_interval_s=0.02,
        pulse_train_count_selections=[5],
    )
    iface = SimulatedDeviceInterface(preset_flash=[(mc, regs)])
    try:
        iface.load_preset(0, mc["settings_crc"], regs,
                          frequency_hz=400_000.0, duration_index=0)
        iface.start_sonication(turn_hv_on=False, wait_for_settle=False)

        deadline = time.monotonic() + 1.0
        while time.monotonic() < deadline:
            qt_app.processEvents()
            if iface.txdevice.get_trigger()["trigger_status"] == "STOPPED":
                break
            time.sleep(0.02)

        final = iface.txdevice.get_trigger()
        assert final["train_count"] == 5
        assert final["pulse_train_count"] == 5
        assert final["trigger_status"] == "STOPPED"
    finally:
        iface.close()


# ------------------------------------------------------------------
# Fixtures
# ------------------------------------------------------------------


@pytest.fixture(scope="module")
def qt_app():
    from PyQt6.QtCore import QCoreApplication
    return QCoreApplication.instance() or QCoreApplication([])
