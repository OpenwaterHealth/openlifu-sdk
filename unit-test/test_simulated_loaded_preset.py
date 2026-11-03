# SPDX-License-Identifier: Apache-2.0
"""The simulator's loaded-preset query (OW_PRESET_LOADED; transmitter-fw#44),
with the same contract as TxDevice.get_loaded_preset_state.
"""

from __future__ import annotations

import pytest

pytest.importorskip("openlifu_sdk.ui._qt")

from sr003_presets import flash_entry, two_module_flash_entry  # noqa: E402 -- reason: after the Qt importorskip

from openlifu_sdk.ui.simulated_interface import (  # noqa: E402 -- reason: after the Qt importorskip
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


def test_sim_reports_the_load():
    tx, mc, regs = _sim(SimulatedTxDevice, flash_entry())
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


def test_sim_one_module_reports_its_own_load():
    tx, mc, regs = _sim(SimulatedTxDevice, two_module_flash_entry())
    _load(tx, mc, regs, duration_index=1, modules=[1])

    own = tx.get_loaded_preset_state(module=1)

    assert own["loaded"] and own["missing_modules"] == []
    assert (own["index"], own["duration_index"], own["regs_crc"]) == (0, 1, regs[1])
    assert tx.get_loaded_preset_state(module=0)["missing_modules"] == [0]
    assert tx.get_loaded_preset_state()["missing_modules"] == [0, 1]


def test_sim_modules_holding_different_loads_are_not_loaded():
    tx, mc, regs = _sim(SimulatedTxDevice, two_module_flash_entry())
    _load(tx, mc, regs, duration_index=0, modules=[0])
    _load(tx, mc, regs, duration_index=1, modules=[1])

    assert tx.get_loaded_preset_state()["missing_modules"] == [1]


def test_sim_console_reports_the_voltage_as_float32():
    """The simulated console reports each record's voltage as float32, as a
    real console does, so verify_console_preset's exact check passes."""
    from openlifu_sdk.io.LIFUTXPresets import verify_console_preset
    from openlifu_sdk.ui.simulated_interface import SimulatedDeviceInterface

    mc, regs = flash_entry(voltage=27.98984157172857)
    iface = SimulatedDeviceInterface(preset_flash=[(mc, regs)])
    verify_console_preset(iface.hvcontroller, 0, mc)


def test_sim_loads_through_the_sdk_preset_helper_on_every_module():
    """LIFUTXPresets.load_preset passes module positionally, as
    TxDevice.load_preset takes it."""
    from openlifu_sdk.io.LIFUTXPresets import load_preset

    tx, mc, regs = _sim(SimulatedLIFUTxDevice, two_module_flash_entry())
    load_preset(tx, 0, mc, 1)
    assert tx.get_loaded_preset_state()["loaded"] is True
