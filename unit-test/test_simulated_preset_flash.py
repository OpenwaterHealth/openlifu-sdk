# SPDX-License-Identifier: Apache-2.0
"""Unit tests for the simulated preset-integrity handshake
(SR-002 / SR-003).

Exercises :meth:`SimulatedTxDevice.get_preset` /
:meth:`SimulatedTxDevice.set_preset` and their exposure through
:class:`SimulatedLIFUInterface`. The real
:class:`~openlifu_sdk.io.LIFUTXDevice.TxDevice.get_preset` /
:meth:`~openlifu_sdk.io.LIFUTXDevice.TxDevice.set_preset` are
tested for their ``NotImplementedError`` contract only -- the
firmware side is PROJ-04 work.
"""

from __future__ import annotations

import pytest

pytest.importorskip("PyQt6")

from openlifu_sdk.io.LIFUTXDevice import TxDevice
from openlifu_sdk.ui.simulated_interface import (
    SimulatedLIFUInterface,
    SimulatedTxDevice,
)


# ------------------------------------------------------------------
# Test helpers
# ------------------------------------------------------------------


def _mc(preset_id: str, settings_crc: int, **extra) -> dict:
    """Minimal machine_config for tests -- ``id`` + ``settings_crc``
    are what ``SimulatedTxDevice.get_preset`` reads;
    ``pulse_train_count_selections`` (+ supporting trigger-config
    fields) are what ``set_preset`` reads to configure the
    sim's ``_sequence`` so a subsequent ``start_sonication`` runs
    the loaded preset. Overridable via ``**extra`` for tests that
    care about specific values."""
    base = {
        "id": preset_id,
        "settings_crc": settings_crc,
        "pulse_interval_ms": 10.0,
        "pulse_count": 1,
        "pulse_train_interval_s": 0.0,
        "pulse_train_count_selections": [1, 2, 3, 4, 5],
        "pulse_length_us": 100.0,
    }
    base.update(extra)
    return base


# ------------------------------------------------------------------
# SimulatedTxDevice: standalone flash
# ------------------------------------------------------------------


def test_get_preset_returns_id_and_settings_crc_from_machine_config():
    tx = SimulatedTxDevice(
        preset_flash=[
            (_mc("preset-A", 0x11111111), 0xAAAAAAAA),
            (_mc("preset-B", 0x22222222), 0xBBBBBBBB),
        ]
    )
    assert tx.get_preset(0) == ("preset-A", 0x11111111)
    assert tx.get_preset(1) == ("preset-B", 0x22222222)


def test_get_machine_config_returns_full_dict_and_crc():
    mc_a = _mc("preset-A", 0x11111111, voltage=40.0)
    tx = SimulatedTxDevice(preset_flash=[(mc_a, 0xAAAAAAAA)])
    machine_config, mc_crc = tx.get_machine_config(0)
    assert machine_config is mc_a
    assert mc_crc == 0xAAAAAAAA


def test_get_preset_default_flash_is_empty():
    tx = SimulatedTxDevice()
    with pytest.raises(IndexError):
        tx.get_preset(0)


@pytest.mark.parametrize("bad_index", [-1, 5])
def test_get_preset_rejects_out_of_range(bad_index):
    tx = SimulatedTxDevice(preset_flash=[(_mc("only", 0xDEADBEEF), 0xC0FFEE00)])
    with pytest.raises(IndexError):
        tx.get_preset(bad_index)


def test_set_preset_records_selection_on_matching_machine_config_crc():
    tx = SimulatedTxDevice(
        preset_flash=[
            (_mc("preset-A", 0x11111111), 0xAAAAAAAA),
            (_mc("preset-B", 0x22222222), 0xBBBBBBBB),
        ]
    )
    tx.set_preset(preset_index=1, sequence_duration_index=3, expected_crc=0xBBBBBBBB)
    assert tx.get_loaded_preset() == (1, 3)


def test_set_preset_rejects_crc_mismatch():
    tx = SimulatedTxDevice(
        preset_flash=[(_mc("preset-A", 0x11111111), 0xAAAAAAAA)]
    )
    with pytest.raises(ValueError, match="CRC mismatch"):
        tx.set_preset(preset_index=0, sequence_duration_index=0, expected_crc=0xDEADBEEF)
    assert tx.get_loaded_preset() == (None, None)


def test_set_preset_error_message_names_machine_config_crc():
    tx = SimulatedTxDevice(
        preset_flash=[(_mc("preset-A", 0x11111111), 0xAAAAAAAA)]
    )
    with pytest.raises(ValueError) as excinfo:
        tx.set_preset(preset_index=0, sequence_duration_index=0, expected_crc=0xDEADBEEF)
    # SR-003 fault path: caller sends machine_config_crc, not the
    # settings_crc; the error message should surface that distinction.
    assert "machine_config_crc" in str(excinfo.value)


def test_set_preset_rejects_out_of_range_index():
    tx = SimulatedTxDevice(preset_flash=[(_mc("only", 0x1), 0x2)])
    with pytest.raises(IndexError):
        tx.set_preset(preset_index=1, sequence_duration_index=0, expected_crc=0x2)


def test_set_preset_rejects_negative_duration_index():
    tx = SimulatedTxDevice(preset_flash=[(_mc("only", 0x1), 0x2)])
    with pytest.raises(ValueError, match="duration_index"):
        tx.set_preset(preset_index=0, sequence_duration_index=-1, expected_crc=0x2)


def test_set_preset_flash_clears_loaded_selection():
    tx = SimulatedTxDevice(
        preset_flash=[
            (_mc("A", 0xA1), 0xAA),
            (_mc("B", 0xB1), 0xBB),
        ]
    )
    tx.set_preset(preset_index=0, sequence_duration_index=2, expected_crc=0xAA)
    assert tx.get_loaded_preset() == (0, 2)
    tx.set_preset_flash([(_mc("C", 0xC1), 0xCC)])
    assert tx.get_loaded_preset() == (None, None)


# ------------------------------------------------------------------
# SimulatedLIFUInterface: preset_flash forwarded to txdevice
# ------------------------------------------------------------------


def test_interface_constructor_forwards_preset_flash():
    interface = SimulatedLIFUInterface(
        preset_flash=[(_mc("diathermy", 0xAD813B92), 0xABBAABBA)]
    )
    assert interface.txdevice.get_preset(0) == ("diathermy", 0xAD813B92)


def test_interface_without_preset_flash_has_empty_flash():
    interface = SimulatedLIFUInterface()
    with pytest.raises(IndexError):
        interface.txdevice.get_preset(0)


def test_interface_full_load_preset_round_trip():
    """End-to-end: seed flash via the interface, load a preset,
    read back the loaded selection."""
    interface = SimulatedLIFUInterface(
        preset_flash=[
            (_mc("preset-A", 0x11111111), 0xAAAAAAAA),
            (_mc("preset-B", 0x22222222), 0xBBBBBBBB),
        ]
    )
    interface.txdevice.set_preset(
        preset_index=1, sequence_duration_index=2, expected_crc=0xBBBBBBBB,
    )
    assert interface.txdevice.get_loaded_preset() == (1, 2)


# ------------------------------------------------------------------
# Real TxDevice: NotImplementedError contract until PROJ-04
# ------------------------------------------------------------------


def test_real_txdevice_get_preset_raises_not_implemented():
    tx = TxDevice(test_mode=True)
    try:
        with pytest.raises(NotImplementedError, match="PROJ-04"):
            tx.get_preset(0)
    finally:
        tx.stop()


def test_real_txdevice_set_preset_raises_not_implemented():
    tx = TxDevice(test_mode=True)
    try:
        with pytest.raises(NotImplementedError, match="PROJ-04"):
            tx.set_preset(preset_index=0, sequence_duration_index=0, expected_crc=0)
    finally:
        tx.stop()


# ------------------------------------------------------------------
# Version mutators (debug-UI hooks for firmware-compat simulation)
# ------------------------------------------------------------------


def test_txdevice_set_version_overrides_get_version():
    tx = SimulatedTxDevice()
    assert tx.get_version() == "sim-1.0.7"
    tx.set_version("sim-0.9.0")
    assert tx.get_version() == "sim-0.9.0"
    # module arg is honored on both getter/setter shapes even though
    # the impl currently ignores it (single string per device).
    tx.set_version("sim-2.0.0", module=1)
    assert tx.get_version(module=1) == "sim-2.0.0"


def test_hvcontroller_set_version_overrides_get_version():
    from openlifu_sdk.ui.simulated_interface import SimulatedHVController
    hv = SimulatedHVController()
    assert hv.get_version() == "sim-1.0.7"
    hv.set_version("sim-0.5.0")
    assert hv.get_version() == "sim-0.5.0"


def test_interface_version_overrides_visible_through_txdevice_and_hv():
    interface = SimulatedLIFUInterface()
    interface.txdevice.set_version("sim-0.9.0")
    interface.hvcontroller.set_version("sim-0.4.0")
    assert interface.txdevice.get_version() == "sim-0.9.0"
    assert interface.hvcontroller.get_version() == "sim-0.4.0"


# ------------------------------------------------------------------
# Simulated update_firmware (drives an app-side firmware-update flow)
# ------------------------------------------------------------------


def test_txdevice_update_firmware_flips_reported_version():
    tx = SimulatedTxDevice()
    tx.set_version("sim-0.0.1")
    assert tx.get_version() == "sim-0.0.1"
    new_version = tx.update_firmware(target_version="1.0.7")
    assert new_version == "1.0.7"
    assert tx.get_version() == "1.0.7"


def test_txdevice_update_firmware_default_target_is_sim_1_0_7():
    tx = SimulatedTxDevice()
    tx.set_version("sim-0.0.1")
    new_version = tx.update_firmware()
    assert new_version == "sim-1.0.7"


def test_hvcontroller_update_firmware_flips_reported_version():
    from openlifu_sdk.ui.simulated_interface import SimulatedHVController
    hv = SimulatedHVController()
    hv.set_version("sim-0.0.1")
    new_version = hv.update_firmware(target_version="1.0.7")
    assert new_version == "1.0.7"
    assert hv.get_version() == "1.0.7"
