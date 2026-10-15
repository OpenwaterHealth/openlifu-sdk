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

import zlib

import pytest

pytest.importorskip("PyQt6")

from openlifu_sdk.io.exceptions import LIFUDeviceError  # noqa: E402
from openlifu_sdk.ui.simulated_interface import SimulatedTxDevice  # noqa: E402


def _machine_config_crc(mc: dict) -> int:
    """Replicates openlifu_operator_interface.preset_schema.machine_config_crc
    without introducing a cross-repo import."""
    import json
    canonical = json.dumps(mc, sort_keys=True, separators=(",", ":"),
                           ensure_ascii=True).encode("utf-8")
    return zlib.crc32(canonical)


def _build_mc(
    *,
    label: str = "example",
    pulse_interval_ms: float = 10.0,
    pulse_count: int = 100,
    pulse_train_interval_s: float = 0.5,
    pulse_train_count_selections=(5, 10, 20),
    pulse_length_us: float = 50.0,
) -> dict:
    """Build a minimal machine_config in the same shape the OI
    builds in preset_schema.to_machine_config."""
    return {
        "id": label,
        "voltage": 12.0,
        "sensitivity": 1.0,
        "voltage_range": [10.0, 15.0],
        "start_C": 25.0,
        "shutoff_C": 55.0,
        "pulse_length_us": pulse_length_us,
        "pulse_interval_ms": pulse_interval_ms,
        "pulse_count": pulse_count,
        "pulse_train_interval_s": pulse_train_interval_s,
        "pulse_train_count_selections": list(pulse_train_count_selections),
        "settings_crc": 0,
    }


def _tx_with_flash(mc_list):
    """Build a SimulatedTxDevice with a flash seeded by the given
    machine_configs (CRC computed on the fly)."""
    flash = [(mc, _machine_config_crc(mc)) for mc in mc_list]
    return SimulatedTxDevice(preset_flash=flash)


# ------------------------------------------------------------------
# Trigger config application
# ------------------------------------------------------------------


def test_load_preset_updates_sequence_from_machine_config():
    mc = _build_mc(
        pulse_interval_ms=25.0,
        pulse_count=50,
        pulse_train_interval_s=1.5,
        pulse_train_count_selections=[10, 20, 40],
    )
    tx = _tx_with_flash([mc])

    # Pick the middle duration (index 1 -> 20 trains).
    tx.load_preset(0, _machine_config_crc(mc), duration_index=1)

    assert tx._sequence["pulse_interval"] == pytest.approx(0.025)
    assert tx._sequence["pulse_count"] == 50
    assert tx._sequence["pulse_train_interval"] == pytest.approx(1.5)
    assert tx._sequence["pulse_train_count"] == 20


def test_load_preset_updates_pulse_duration_from_machine_config():
    mc = _build_mc(pulse_length_us=75.0)
    tx = _tx_with_flash([mc])
    tx.load_preset(0, _machine_config_crc(mc), duration_index=0)
    # pulse_length_us=75 -> _pulse["duration"] = 75e-6 s
    assert tx._pulse["duration"] == pytest.approx(75e-6)


def test_load_preset_reflects_through_get_trigger():
    mc = _build_mc(
        pulse_interval_ms=5.0,
        pulse_count=10,
        pulse_train_interval_s=0.1,
        pulse_train_count_selections=[2, 4, 8],
    )
    tx = _tx_with_flash([mc])
    tx.load_preset(0, _machine_config_crc(mc), duration_index=2)

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
    mc = _build_mc(pulse_train_count_selections=[5, 10])
    tx = _tx_with_flash([mc])
    with pytest.raises(LIFUDeviceError, match="duration index"):
        tx.load_preset(0, _machine_config_crc(mc), duration_index=bad_duration)


def test_load_preset_crc_mismatch_leaves_sequence_unchanged():
    """SR-003 fault path: a CRC mismatch rejects the call and
    should not leak partial state into the sequence."""
    mc = _build_mc(
        pulse_interval_ms=10.0,
        pulse_count=5,
        pulse_train_interval_s=0.2,
        pulse_train_count_selections=[3],
    )
    tx = _tx_with_flash([mc])
    sequence_before = dict(tx._sequence)
    pulse_before = dict(tx._pulse)
    with pytest.raises(LIFUDeviceError):
        tx.load_preset(0, 0xDEADBEEF, duration_index=0)
    assert tx._sequence == sequence_before
    assert tx._pulse == pulse_before


# ------------------------------------------------------------------
# Integration: start_sonication runs the loaded preset's sequence
# ------------------------------------------------------------------


def test_start_sonication_runs_preset_selected_sequence(qt_app):
    """End-to-end verification: load preset -> start_sonication ->
    engine ticks run the selected pulse_train_count, not the
    1-train default."""
    import time
    from openlifu_sdk.ui.simulated_interface import SimulatedDeviceInterface

    mc = _build_mc(
        pulse_interval_ms=1.0,
        pulse_count=1,
        pulse_train_interval_s=0.02,
        pulse_train_count_selections=[5],
    )
    flash = [(mc, _machine_config_crc(mc))]
    iface = SimulatedDeviceInterface(preset_flash=flash)
    try:
        iface.load_preset(0, mc["settings_crc"], _machine_config_crc(mc),
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
