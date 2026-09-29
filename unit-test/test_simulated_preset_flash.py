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
# SimulatedTxDevice: standalone flash
# ------------------------------------------------------------------


def test_get_preset_returns_flash_entry():
    tx = SimulatedTxDevice(
        preset_flash=[("preset-A", 0x11111111), ("preset-B", 0x22222222)]
    )
    assert tx.get_preset(0) == ("preset-A", 0x11111111)
    assert tx.get_preset(1) == ("preset-B", 0x22222222)


def test_get_preset_default_flash_is_empty():
    tx = SimulatedTxDevice()
    with pytest.raises(IndexError):
        tx.get_preset(0)


@pytest.mark.parametrize("bad_index", [-1, 5])
def test_get_preset_rejects_out_of_range(bad_index):
    tx = SimulatedTxDevice(preset_flash=[("only", 0xDEADBEEF)])
    with pytest.raises(IndexError):
        tx.get_preset(bad_index)


def test_set_preset_records_selection_on_matching_crc():
    tx = SimulatedTxDevice(
        preset_flash=[("preset-A", 0xA1A1A1A1), ("preset-B", 0xB2B2B2B2)]
    )
    tx.set_preset(preset_index=1, sequence_duration_index=3, expected_crc=0xB2B2B2B2)
    assert tx.get_loaded_preset() == (1, 3)


def test_set_preset_rejects_crc_mismatch():
    tx = SimulatedTxDevice(preset_flash=[("preset-A", 0xA1A1A1A1)])
    with pytest.raises(ValueError, match="CRC mismatch"):
        tx.set_preset(preset_index=0, sequence_duration_index=0, expected_crc=0xDEADBEEF)
    # Loaded selection unchanged.
    assert tx.get_loaded_preset() == (None, None)


def test_set_preset_rejects_out_of_range_index():
    tx = SimulatedTxDevice(preset_flash=[("only", 0x1)])
    with pytest.raises(IndexError):
        tx.set_preset(preset_index=1, sequence_duration_index=0, expected_crc=0x1)


def test_set_preset_rejects_negative_duration_index():
    tx = SimulatedTxDevice(preset_flash=[("only", 0x1)])
    with pytest.raises(ValueError, match="duration_index"):
        tx.set_preset(preset_index=0, sequence_duration_index=-1, expected_crc=0x1)


def test_set_preset_flash_clears_loaded_selection():
    tx = SimulatedTxDevice(preset_flash=[("A", 0xAA), ("B", 0xBB)])
    tx.set_preset(preset_index=0, sequence_duration_index=2, expected_crc=0xAA)
    assert tx.get_loaded_preset() == (0, 2)
    tx.set_preset_flash([("C", 0xCC)])
    assert tx.get_loaded_preset() == (None, None)


# ------------------------------------------------------------------
# SimulatedLIFUInterface: preset_flash forwarded to txdevice
# ------------------------------------------------------------------


def test_interface_constructor_forwards_preset_flash():
    interface = SimulatedLIFUInterface(
        preset_flash=[("diathermy", 0xAD813B92)]
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
        preset_flash=[("preset-A", 0xA1A1A1A1), ("preset-B", 0xB2B2B2B2)]
    )
    interface.txdevice.set_preset(
        preset_index=1, sequence_duration_index=2, expected_crc=0xB2B2B2B2,
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
