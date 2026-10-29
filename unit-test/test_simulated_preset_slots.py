# SPDX-License-Identifier: Apache-2.0
"""The simulator's DEV preset slots (transmitter-fw#44, bare-metal DEV image).

SimulatedLIFUTxDevice stores presets the way the firmware does: each module
keeps its own part of each preset; a blob over PRESET_SLOT_SIZE takes the next
slot as well; the preset lock and a running trigger refuse stores and deletes.
LIFUTXPresets.store_preset / load_preset run against it unchanged.
"""

from __future__ import annotations

import pytest

pytest.importorskip("openlifu_sdk.ui._qt")

from sr003_presets import flash_entry, two_module_flash_entry  # noqa: E402 -- reason: after the Qt importorskip

import openlifu_sdk.ui.simulated_interface as sim  # noqa: E402 -- reason: after the Qt importorskip
from openlifu_sdk.io.exceptions import LIFUDeviceError  # noqa: E402 -- reason: after the Qt importorskip
from openlifu_sdk.io.LIFUConfig import (  # noqa: E402 -- reason: after the Qt importorskip
    OW_BAD_CRC,
    OW_FDA_REFUSED,
    OW_PRESET_BUSY,
    OW_PRESET_EMPTY,
    OW_PRESET_NO_ROOM,
    OW_PRESET_SLOT_TAIL,
)
from openlifu_sdk.io.LIFUTXDevice import PRESET_SLOTS  # noqa: E402 -- reason: after the Qt importorskip
from openlifu_sdk.io.LIFUTXPresets import (  # noqa: E402 -- reason: after the Qt importorskip
    delete_preset,
    load_preset,
    preset_blob,
    store_preset,
)
from openlifu_sdk.ui.simulated_interface import SimulatedLIFUTxDevice  # noqa: E402 -- reason: after the Qt importorskip


def _code(excinfo):
    return excinfo.value.device_error_code


def _dev(num_modules=1):
    """A DEV image with nothing stored."""
    return SimulatedLIFUTxDevice(num_modules=num_modules, preset_flash=[])


def test_fresh_dev_image_has_every_slot_empty():
    tx = _dev()
    assert tx.get_preset_capacity()["slots"] == tx.get_preset_capacity()["free"] == PRESET_SLOTS
    assert tx.get_preset_slot(0) == {"index": 0, "state": "empty"}
    with pytest.raises(LIFUDeviceError) as e:
        tx.get_preset(0)
    assert _code(e) == OW_PRESET_EMPTY


def test_store_then_load_a_one_module_preset():
    tx = _dev()
    mc, _ = flash_entry()
    got = store_preset(tx, 3, mc)  # writes, then verify_preset reads it back
    assert (got["id"], got["settings_crc"], got["count"]) == (mc["id"], mc["settings_crc"], 1)
    assert tx.get_preset_slot(3)["state"] == "preset"
    assert tx.get_preset_capacity()["free"] == PRESET_SLOTS - 1

    load_preset(tx, 3, mc, duration_index=1)
    state = tx.get_loaded_preset_state()
    assert (state["loaded"], state["index"], state["duration_index"]) == (True, 3, 1)
    assert state["train_count"] == mc["pulse_train_count_selections"][1]


def test_store_then_load_a_two_module_preset():
    tx = _dev(num_modules=2)
    mc, _ = two_module_flash_entry()
    store_preset(tx, 0, mc)
    load_preset(tx, 0, mc)
    assert tx.get_loaded_preset_state()["loaded"] is True


def test_a_preset_one_module_has_not_stored_does_not_load_there():
    tx = _dev(num_modules=2)
    mc, _ = two_module_flash_entry()
    tx.write_preset(0, preset_blob(mc, 0), 0)
    with pytest.raises(LIFUDeviceError):
        load_preset(tx, 0, mc)
    assert tx.get_loaded_preset_state()["loaded"] is False


def test_storing_replaces_the_slot_and_unloads():
    tx = _dev()
    first, _ = flash_entry()
    second, _ = flash_entry(pulse_count=3)
    store_preset(tx, 0, first)
    load_preset(tx, 0, first)
    store_preset(tx, 0, second)
    assert tx.get_preset(0)["settings_crc"] == second["settings_crc"]
    assert tx.get_loaded_preset_state()["loaded"] is False


def test_lock_refuses_store_and_delete_and_keeps_the_slot():
    tx = _dev()
    mc, _ = flash_entry()
    store_preset(tx, 0, mc)
    tx.lock_presets()
    for call in (lambda: tx.write_preset(1, preset_blob(mc), 0), lambda: tx.delete_preset(0)):
        with pytest.raises(LIFUDeviceError) as e:
            call()
        assert _code(e) == OW_FDA_REFUSED
    assert tx.get_preset_slot(1)["state"] == "empty"
    assert tx.get_preset(0)["settings_crc"] == mc["settings_crc"]
    load_preset(tx, 0, mc)  # loading a stored preset still works locked
    assert tx.get_loaded_preset_state()["loaded"] is True


def test_store_refused_while_the_trigger_runs():
    tx = _dev()
    mc, _ = flash_entry()
    tx.start_trigger()  # unlocked, nothing loaded: runs the host's config
    with pytest.raises(LIFUDeviceError) as e:
        tx.write_preset(0, preset_blob(mc), 0)
    assert _code(e) == OW_PRESET_BUSY


def test_damaged_blob_is_refused_and_the_slot_unchanged():
    tx = _dev()
    mc, _ = flash_entry()
    blob = bytearray(preset_blob(mc))
    blob[-1] ^= 0xFF
    with pytest.raises(LIFUDeviceError) as e:
        tx.write_preset(0, bytes(blob), 0)
    assert _code(e) == OW_BAD_CRC
    assert tx.get_preset_slot(0)["state"] == "empty"


def test_a_long_blob_takes_the_next_slot(monkeypatch):
    monkeypatch.setattr(sim, "PRESET_SLOT_SIZE", 64)  # every real blob is then "long"
    tx = _dev()
    mc, _ = flash_entry()
    other, _ = flash_entry(pulse_count=3)
    store_preset(tx, 4, mc)
    assert tx.get_preset_slot(5) == {"index": 5, "state": "tail"}

    with pytest.raises(LIFUDeviceError) as e:
        tx.write_preset(5, preset_blob(other), 0)
    assert _code(e) == OW_PRESET_SLOT_TAIL

    store_preset(tx, 7, other)
    with pytest.raises(LIFUDeviceError) as e:
        tx.write_preset(6, preset_blob(mc), 0)  # needs 7, which holds a preset
    assert _code(e) == OW_PRESET_NO_ROOM

    delete_preset(tx, 4)
    assert [tx.get_preset_slot(i)["state"] for i in (4, 5)] == ["empty", "empty"]


def test_delete_empties_the_slot_and_an_empty_delete_is_fine():
    tx = _dev()
    mc, _ = flash_entry()
    store_preset(tx, 2, mc)
    delete_preset(tx, 2)
    delete_preset(tx, 2)
    assert tx.get_preset_slot(2)["state"] == "empty"


def test_preset_flash_fills_the_first_slots():
    mc, regs = flash_entry()
    tx = SimulatedLIFUTxDevice(preset_flash=[(mc, regs)])
    assert tx.get_preset(0)["settings_crc"] == mc["settings_crc"]
    assert tx.get_preset_slot(1)["state"] == "empty"
    assert tx.get_preset_capacity()["free"] == PRESET_SLOTS - 1
