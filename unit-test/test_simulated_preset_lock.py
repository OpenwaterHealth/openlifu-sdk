# SPDX-License-Identifier: Apache-2.0
"""The simulator's preset lock and loaded-preset query (transmitter-fw#41, #44).

SimulatedTxDevice is an FDA image: always locked. SimulatedLIFUTxDevice is a
DEV image: full control until locked, raw writes leave nothing loaded, and
locked it refuses what an FDA image refuses.
"""

from __future__ import annotations

import pytest

pytest.importorskip("openlifu_sdk.ui._qt")

from sr003_presets import flash_entry, two_module_flash_entry  # noqa: E402 -- reason: after the Qt importorskip

from openlifu_sdk.io.exceptions import LIFUDeviceError  # noqa: E402 -- reason: after the Qt importorskip
from openlifu_sdk.io.LIFUConfig import (  # noqa: E402 -- reason: after the Qt importorskip
    OW_FDA_REFUSED,
    OW_INVALID_PACKET,
    OW_NO_PRESET,
    OW_PRESET_BUSY,
)
from openlifu_sdk.ui.simulated_interface import (  # noqa: E402 -- reason: after the Qt importorskip
    SimulatedLIFUInterface,
    SimulatedLIFUTxDevice,
    SimulatedTxDevice,
)


def _sim(cls, entry):
    mc, regs = entry
    tx = cls(num_modules=len(regs) if isinstance(regs, list) else 1, preset_flash=[(mc, regs)])
    return tx, mc, regs


def _load(tx, mc, regs, duration_index=0, modules=None):
    crcs = regs if isinstance(regs, list) else [regs]
    for module in (range(len(crcs)) if modules is None else modules):
        tx.load_preset(0, crcs[module], duration_index, settings_crc=mc["settings_crc"], module=module)


def _code(excinfo):
    return excinfo.value.device_error_code


def test_sim_fda_reports_the_load_and_is_always_locked():
    tx, mc, regs = _sim(SimulatedTxDevice, flash_entry())
    assert tx.is_preset_locked() is True
    assert tx.get_loaded_preset_state()["loaded"] is False

    _load(tx, mc, regs, duration_index=1)

    state = tx.get_loaded_preset_state()
    assert state["loaded"] and state["missing_modules"] == []
    assert (state["index"], state["duration_index"]) == (0, 1)
    assert state["train_count"] == mc["pulse_train_count_selections"][1]
    assert (state["settings_crc"], state["regs_crc"]) == (mc["settings_crc"], regs)
    assert tx.get_loaded_preset() == (0, 1)


def test_sim_one_module_without_the_load_reads_as_nothing_loaded():
    tx, mc, regs = _sim(SimulatedTxDevice, two_module_flash_entry())
    _load(tx, mc, regs, modules=[0])

    state = tx.get_loaded_preset_state()

    assert state["loaded"] is False
    assert state["missing_modules"] == [1]


def test_sim_dev_unlocked_raw_write_goes_through_and_unloads():
    tx, mc, regs = _sim(SimulatedLIFUTxDevice, flash_entry())
    assert tx.is_preset_locked() is False
    _load(tx, mc, regs)

    tx.set_trigger(pulse_train_count=99)

    assert tx.get_trigger()["pulse_train_count"] == 99
    assert tx.get_loaded_preset_state()["loaded"] is False


def test_sim_dev_unlocked_start_with_nothing_loaded_runs_the_host_config():
    tx, _, _ = _sim(SimulatedLIFUTxDevice, flash_entry())
    tx.start_trigger()
    assert tx.get_trigger()["trigger_status"] == "RUNNING"


def test_sim_dev_raw_write_while_a_loaded_preset_runs_is_refused():
    tx, mc, regs = _sim(SimulatedLIFUTxDevice, flash_entry())
    _load(tx, mc, regs)
    tx.start_trigger()

    with pytest.raises(LIFUDeviceError) as excinfo:
        tx.set_trigger(pulse_train_count=99)

    assert _code(excinfo) == OW_PRESET_BUSY
    assert tx.get_loaded_preset_state()["loaded"] is True


def test_sim_dev_locked_refuses_what_fda_refuses():
    tx, mc, regs = _sim(SimulatedLIFUTxDevice, flash_entry())
    tx.lock_presets()
    assert tx.is_preset_locked() is True

    for write in (lambda: tx.set_trigger(pulse_train_count=99),
                  lambda: tx.set_trigger_json({"pulse_train_count": 99}),
                  lambda: tx.set_solution(sequence={"pulse_train_count": 99})):
        with pytest.raises(LIFUDeviceError) as excinfo:
            write()
        assert _code(excinfo) == OW_INVALID_PACKET
    with pytest.raises(LIFUDeviceError) as excinfo:
        tx.write_config_json("{}")
    assert _code(excinfo) == OW_FDA_REFUSED

    # Presets still load and report; the trigger needs one.
    with pytest.raises(LIFUDeviceError) as excinfo:
        tx.start_trigger()
    assert _code(excinfo) == OW_NO_PRESET
    _load(tx, mc, regs)
    assert tx.get_loaded_preset_state()["loaded"] is True


def test_sim_lifu_interface_locks_on_request():
    mc, regs = flash_entry()
    iface = SimulatedLIFUInterface(preset_flash=[(mc, regs)], preset_lock=True)
    try:
        assert iface.txdevice.is_preset_locked() is True
    finally:
        iface.close()
    iface = SimulatedLIFUInterface(preset_flash=[(mc, regs)])
    try:
        assert iface.txdevice.is_preset_locked() is False
    finally:
        iface.close()


def test_sim_fda_lock_is_a_no_op_and_the_console_reports_float32():
    """The FDA component may send the lock; an FDA image is already locked.
    The simulated console reports each record's voltage as float32, as a real
    console does, so verify_console_preset's exact check passes."""
    from openlifu_sdk.io.LIFUTXPresets import verify_console_preset
    from openlifu_sdk.ui.simulated_interface import SimulatedDeviceInterface

    mc, regs = flash_entry(voltage=27.98984157172857)
    iface = SimulatedDeviceInterface(preset_flash=[(mc, regs)])
    assert iface.txdevice.lock_presets() is True and iface.txdevice.is_preset_locked() is True
    verify_console_preset(iface.hvcontroller, 0, mc)


def test_sim_loads_through_the_sdk_preset_helper_on_every_module():
    """LIFUTXPresets.load_preset passes module positionally, as
    TxDevice.load_preset takes it."""
    from openlifu_sdk.io.LIFUTXPresets import load_preset

    tx, mc, regs = _sim(SimulatedLIFUTxDevice, two_module_flash_entry())
    load_preset(tx, 0, mc, 1)
    assert tx.get_loaded_preset_state()["loaded"] is True
