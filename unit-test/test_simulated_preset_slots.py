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
    OW_INVALID_PACKET,
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


def _slot_size(monkeypatch, size):
    """Shrink the slots, keeping the device's geometry: a blob is at most two."""
    monkeypatch.setattr(sim, "PRESET_SLOT_SIZE", size)
    monkeypatch.setattr(sim, "PRESET_BLOB_MAX", 2 * size)


def _short_entry(**overrides):
    """A preset whose blob is shorter than flash_entry()'s: fewer run lengths."""
    return flash_entry(pulse_train_count_selections=[1], **overrides)


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
    with pytest.raises(LIFUDeviceError, match="module 1's slot 0 is empty") as e:
        load_preset(tx, 0, mc)
    assert _code(e) == OW_PRESET_EMPTY
    state = tx.get_loaded_preset_state()
    assert state["loaded"] is False and state["missing_modules"] == [0, 1]  # module 0 rolled back


def test_each_module_keeps_its_own_part():
    """A module writing another preset leaves the other module's part
    as it was, and only that module's load is dropped."""
    tx = _dev(num_modules=2)
    mc, _ = two_module_flash_entry()
    other, _ = two_module_flash_entry(pulse_count=3)
    store_preset(tx, 0, mc)
    load_preset(tx, 0, mc)
    tx.write_preset(0, preset_blob(other, 1), 1)
    assert tx.get_preset(0)["settings_crc"] == mc["settings_crc"]  # module 0 answers
    state = tx.get_loaded_preset_state()
    assert state["loaded"] is False and state["missing_modules"] == [1]
    assert tx.get_loaded_preset_state(module=0)["loaded"] is True


def test_only_another_module_holding_a_preset_reads_as_empty():
    """Module 0 answers the preset queries."""
    tx = _dev(num_modules=2)
    mc, _ = two_module_flash_entry()
    tx.write_preset(0, preset_blob(mc, 1), 1)
    assert tx.get_preset_slot(0) == {"index": 0, "state": "empty"}


def test_a_stored_preset_loads_with_its_trigger_timing():
    tx = _dev()
    mc, _ = flash_entry()
    store_preset(tx, 0, mc)
    load_preset(tx, 0, mc, duration_index=1)
    seeded = SimulatedLIFUTxDevice(preset_flash=[flash_entry()])
    load_preset(seeded, 0, mc, duration_index=1)
    timing = ("pulse_interval", "pulse_count", "pulse_train_interval", "pulse_train_count")
    assert ({k: tx.get_trigger_json()[k] for k in timing}
            == {k: seeded.get_trigger_json()[k] for k in timing})


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


def test_store_and_delete_refused_while_the_trigger_runs():
    tx = _dev()
    mc, _ = flash_entry()
    store_preset(tx, 0, mc)
    tx.start_trigger()  # unlocked, nothing loaded: runs the host's config
    for call in (lambda: tx.write_preset(1, preset_blob(mc), 0), lambda: tx.delete_preset(0)):
        with pytest.raises(LIFUDeviceError) as e:
            call()
        assert _code(e) == OW_PRESET_BUSY
    assert tx.get_preset_slot(0)["state"] == "preset"


@pytest.mark.parametrize(("slot", "module"), [(PRESET_SLOTS, 0), (-1, 0), (0, 1), (0, -1)])
def test_a_slot_or_module_out_of_range_is_refused(slot, module):
    tx = _dev()
    mc, _ = flash_entry()
    for call in (lambda: tx.write_preset(slot, preset_blob(mc), module),
                 lambda: tx.delete_preset(slot, module)):
        with pytest.raises(LIFUDeviceError) as e:
            call()
        assert _code(e) == OW_INVALID_PACKET


@pytest.mark.parametrize(("damage", "match"), [
    (lambda b: b[:-1], "not a preset blob"),                     # truncated: does not parse
    (lambda b: b[:-1] + bytes([b[-1] ^ 0xFF]), "does not match"),  # parses; body CRC wrong
])
def test_damaged_blob_is_refused_and_the_slot_unchanged(damage, match):
    tx = _dev()
    mc, _ = flash_entry()
    with pytest.raises(LIFUDeviceError, match=match) as e:
        tx.write_preset(0, damage(preset_blob(mc)), 0)
    assert _code(e) == OW_BAD_CRC
    assert tx.get_preset_slot(0)["state"] == "empty"


def test_a_long_blob_takes_the_next_slot(monkeypatch):
    tx = _dev()
    mc, _ = flash_entry()
    other, _ = flash_entry(pulse_count=3)
    _slot_size(monkeypatch, len(preset_blob(mc)) - 1)  # both blobs are then "long"
    store_preset(tx, 4, mc)
    assert tx.get_preset_slot(5) == {"index": 5, "state": "tail"}

    with pytest.raises(LIFUDeviceError) as e:
        tx.write_preset(5, preset_blob(other), 0)
    assert _code(e) == OW_PRESET_SLOT_TAIL

    store_preset(tx, 7, other)
    with pytest.raises(LIFUDeviceError) as e:
        tx.write_preset(6, preset_blob(mc), 0)  # needs 7, which holds a preset
    assert _code(e) == OW_PRESET_NO_ROOM

    with pytest.raises(LIFUDeviceError) as e:
        tx.delete_preset(5)
    assert _code(e) == OW_PRESET_SLOT_TAIL

    with pytest.raises(LIFUDeviceError) as e:
        tx.write_preset(PRESET_SLOTS - 1, preset_blob(mc), 0)  # no slot after the last
    assert _code(e) == OW_PRESET_NO_ROOM

    delete_preset(tx, 4)
    assert [tx.get_preset_slot(i)["state"] for i in (4, 5)] == ["empty", "empty"]


def test_a_long_part_keeps_its_tail_when_another_module_part_is_short(monkeypatch):
    """Whether a part takes two slots is each module's own."""
    tx = _dev(num_modules=2)
    long_mc, _ = two_module_flash_entry()
    short_mc, _ = two_module_flash_entry(pulse_train_count_selections=[1])
    _slot_size(monkeypatch, len(preset_blob(short_mc, 1)))
    assert len(preset_blob(long_mc, 0)) > sim.PRESET_SLOT_SIZE
    tx.write_preset(0, preset_blob(long_mc, 0), 0)    # module 0: two slots
    tx.write_preset(0, preset_blob(short_mc, 1), 1)   # module 1: one slot, another preset
    with pytest.raises(LIFUDeviceError) as e:
        tx.write_preset(1, preset_blob(short_mc, 0), 0)  # module 0's slot 1 is still its tail
    assert _code(e) == OW_PRESET_SLOT_TAIL
    tx.write_preset(1, preset_blob(short_mc, 1), 1)      # module 1's slot 1 is free


def test_replacing_a_long_preset_with_a_short_one_frees_the_tail(monkeypatch):
    tx = _dev()
    long_mc, _ = flash_entry()
    short_mc, _ = _short_entry()
    _slot_size(monkeypatch, len(preset_blob(short_mc)))
    store_preset(tx, 4, long_mc)
    assert tx.get_preset_slot(5)["state"] == "tail"
    store_preset(tx, 4, short_mc)
    assert [tx.get_preset_slot(i)["state"] for i in (4, 5)] == ["preset", "empty"]


def test_deleting_or_replacing_the_loaded_preset_unloads_it_and_others_do_not():
    tx = _dev()
    mc, _ = flash_entry()
    other, _ = flash_entry(pulse_count=3)
    store_preset(tx, 0, mc)
    store_preset(tx, 1, other)
    load_preset(tx, 0, mc)
    store_preset(tx, 2, other)   # another slot: the load stays
    delete_preset(tx, 1)
    assert tx.get_loaded_preset_state()["loaded"] is True
    delete_preset(tx, 0)         # the loaded slot: nothing loaded
    assert tx.get_loaded_preset_state()["loaded"] is False
    store_preset(tx, 0, mc)
    load_preset(tx, 0, mc)
    store_preset(tx, 0, other)   # over the loaded slot: nothing loaded
    assert tx.get_loaded_preset_state()["loaded"] is False


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


def test_the_simulated_console_reports_its_records_as_float32():
    """As a real console does, so verify_console_preset's exact check passes."""
    from openlifu_sdk.io.LIFUTXPresets import verify_console_preset
    from openlifu_sdk.ui.simulated_interface import SimulatedLIFUInterface

    mc, regs = flash_entry(voltage=27.98984157172857)
    iface = SimulatedLIFUInterface(preset_flash=[(mc, regs)])
    verify_console_preset(iface.hvcontroller, 0, mc)
