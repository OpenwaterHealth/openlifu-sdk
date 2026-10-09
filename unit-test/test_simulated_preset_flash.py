# SPDX-License-Identifier: Apache-2.0
"""Unit tests for the simulated preset flash (SR-002 / SR-003).

Exercises :meth:`SimulatedTxDevice.get_preset` /
:meth:`SimulatedTxDevice.load_preset`, which mirror the real
``TxDevice`` OW_PRESET_GET / OW_PRESET_LOAD shapes, and the
end-to-end :meth:`SimulatedDeviceInterface.load_preset`.
"""

from __future__ import annotations

import pytest

pytest.importorskip("PyQt6")

from openlifu_sdk.io.exceptions import LIFUDeviceError
from openlifu_sdk.io.LIFUConfig import OW_BAD_CRC, OW_HV_PRESET_CRC
from openlifu_sdk.ui.simulated_interface import (
    SimulatedDeviceInterface,
    SimulatedLIFUInterface,
    SimulatedTxDevice,
)


# ------------------------------------------------------------------
# Test helpers
# ------------------------------------------------------------------


def _mc(preset_id: str, settings_crc: int, **extra) -> dict:
    """Minimal machine_config for tests -- ``id`` + ``settings_crc``
    are what ``get_preset`` reports; ``pulse_train_count_selections``
    (+ supporting trigger-config fields) are what ``load_preset``
    applies to the sim's ``_sequence``. Overridable via ``**extra``."""
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


def test_get_preset_matches_real_txdevice_shape():
    tx = SimulatedTxDevice(
        preset_flash=[
            (_mc("preset-A", 0x11111111, start_C=30.0, shutoff_C=60.0), 0xAAAAAAAA),
            (_mc("preset-B", 0x22222222), 0xBBBBBBBB),
        ]
    )
    a = tx.get_preset(0)
    assert (a["id"], a["settings_crc"], a["regs_crc"]) == ("preset-A", 0x11111111, 0xAAAAAAAA)
    assert (a["count"], a["index"]) == (2, 0)
    assert a["train_counts"] == [1, 2, 3, 4, 5]
    assert (a["start_c"], a["shutoff_c"]) == (30.0, 60.0)
    # Same keys TxDevice.get_preset returns from OW_PRESET_GET.
    assert set(a) == {"count", "index", "chip_count", "profile_count", "settings_crc",
                      "regs_crc", "baked_regs_crc", "id", "train_counts",
                      "start_c", "shutoff_c"}
    assert tx.get_preset(1)["id"] == "preset-B"


def test_default_flash_has_one_loadable_preset():
    tx = SimulatedTxDevice()
    preset = tx.get_preset(0)
    assert preset["count"] == 1
    assert tx.load_preset(0, preset["regs_crc"], 0)


def test_empty_flash_has_no_presets():
    tx = SimulatedTxDevice(preset_flash=[])
    with pytest.raises(LIFUDeviceError):
        tx.get_preset(0)


@pytest.mark.parametrize("bad_index", [-1, 5])
def test_get_preset_rejects_out_of_range(bad_index):
    tx = SimulatedTxDevice(preset_flash=[(_mc("only", 0xDEADBEEF), 0xC0FFEE00)])
    with pytest.raises(LIFUDeviceError):
        tx.get_preset(bad_index)


def test_load_preset_records_selection_on_matching_regs_crc():
    tx = SimulatedTxDevice(
        preset_flash=[
            (_mc("preset-A", 0x11111111), 0xAAAAAAAA),
            (_mc("preset-B", 0x22222222), 0xBBBBBBBB),
        ]
    )
    tx.load_preset(1, 0xBBBBBBBB, duration_index=3)
    assert tx.get_loaded_preset() == (1, 3)


def test_load_preset_rejects_crc_mismatch_with_ow_bad_crc():
    tx = SimulatedTxDevice(
        preset_flash=[(_mc("preset-A", 0x11111111), 0xAAAAAAAA)]
    )
    with pytest.raises(LIFUDeviceError) as excinfo:
        tx.load_preset(0, 0xDEADBEEF, duration_index=0)
    assert excinfo.value.device_error_code == OW_BAD_CRC
    assert tx.get_loaded_preset() == (None, None)


def test_load_preset_rejects_out_of_range_index():
    tx = SimulatedTxDevice(preset_flash=[(_mc("only", 0x1), 0x2)])
    with pytest.raises(LIFUDeviceError):
        tx.load_preset(1, 0x2, duration_index=0)


@pytest.mark.parametrize("bad_duration", [-1, 5])
def test_load_preset_rejects_bad_duration_index(bad_duration):
    tx = SimulatedTxDevice(preset_flash=[(_mc("only", 0x1), 0x2)])
    with pytest.raises(LIFUDeviceError, match="duration index"):
        tx.load_preset(0, 0x2, duration_index=bad_duration)
    assert tx.get_loaded_preset() == (None, None)


# ------------------------------------------------------------------
# SimulatedDeviceInterface: preset_flash seeds TX + HV; load_preset
# ------------------------------------------------------------------


def test_interface_constructor_forwards_preset_flash():
    interface = SimulatedDeviceInterface(
        preset_flash=[(_mc("diathermy", 0xAD813B92), 0xABBAABBA)]
    )
    assert interface.txdevice.get_preset(0)["id"] == "diathermy"
    assert interface.hvcontroller.get_preset(0)["settings_crc"] == 0xAD813B92


def test_lifu_interface_accepts_preset_flash():
    interface = SimulatedLIFUInterface(
        preset_flash=[(_mc("diathermy", 0xAD813B92), 0xABBAABBA)]
    )
    assert interface.txdevice.get_preset(0)["settings_crc"] == 0xAD813B92


def test_interface_load_preset_scales_hv_by_sensitivity_at_frequency():
    """End-to-end FDA load: TX loads the preset, HV voltage is the
    preset voltage scaled by ref / device sensitivity at the preset
    frequency (the sim's default module table: 2720 @ 400 kHz,
    2267 @ 410 kHz)."""
    mc = _mc("preset-A", 0x11111111, voltage=12.0, sensitivity=2720.0,
             voltage_range=[5.0, 20.0])
    interface = SimulatedDeviceInterface(preset_flash=[(mc, 0xAAAAAAAA)])

    interface.load_preset(0, 0x11111111, 0xAAAAAAAA, 400_000.0, duration_index=2)
    assert interface.txdevice.get_loaded_preset() == (0, 2)
    assert interface.hvcontroller.supply_voltage == pytest.approx(12.0)

    interface.load_preset(0, 0x11111111, 0xAAAAAAAA, 410_000.0)
    assert interface.hvcontroller.supply_voltage == pytest.approx(12.0 * 2720.0 / 2267.0)


def test_interface_load_preset_hv_settings_crc_mismatch():
    interface = SimulatedDeviceInterface(
        preset_flash=[(_mc("preset-A", 0x11111111), 0xAAAAAAAA)]
    )
    with pytest.raises(LIFUDeviceError) as excinfo:
        interface.load_preset(0, 0xDEADBEEF, 0xAAAAAAAA, 400_000.0)
    assert excinfo.value.device_error_code == OW_HV_PRESET_CRC


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
