# SPDX-License-Identifier: Apache-2.0
"""The simulated FDA components refuse what FDA_MODE firmware refuses (#66).

Firmware behaviour mirrored here:
- TX ``presets_start_check`` (transmitter-fw ``presets.c``): no start with
  nothing loaded (OW_NO_PRESET) or above the loaded preset's start_C
  (OW_TEMP_TOO_HIGH).
- Console ``hv_presets.c``: no HV on with nothing selected (OW_HV_NO_PRESET);
  a select whose rescaled voltage is outside 35-45 V is refused, not
  clamped (OW_HV_RANGE); no select or deselect while HV is on
  (OW_HV_FDA_REFUSED); a non-positive sensitivity is refused (0xFE).

The RUO simulator (``SimulatedLIFUInterface``) has none of these gates, as
an RUO image has none.
"""

from __future__ import annotations

import pytest

pytest.importorskip("PyQt6")

from sr003_presets import flash_entry  # noqa: E402 -- reason: after the PyQt6 importorskip

from openlifu_sdk.io.exceptions import LIFUDeviceError, LIFUError, LIFUSolutionError  # noqa: E402 -- reason: after the PyQt6 importorskip
from openlifu_sdk.io.LIFUConfig import (  # noqa: E402 -- reason: after the PyQt6 importorskip
    LIFU_ERR_MODULE_COUNT_MISMATCH,
    OW_HV_FDA_REFUSED,
    OW_HV_NO_PRESET,
    OW_HV_RANGE,
    OW_INVALID_PACKET,
    OW_NO_PRESET,
    OW_TEMP_TOO_HIGH,
)
from openlifu_sdk.io.LIFUInterface import LIFUInterfaceStatus  # noqa: E402 -- reason: after the PyQt6 importorskip
from openlifu_sdk.ui.simulated_interface import (  # noqa: E402 -- reason: after the PyQt6 importorskip
    SimulatedDeviceInterface,
    SimulatedLIFUInterface,
)


@pytest.fixture
def iface():
    mc, regs = flash_entry()
    interface = SimulatedDeviceInterface(preset_flash=[(mc, regs)])
    interface.preset = (mc["settings_crc"], regs)
    yield interface
    interface.close()


def _load(interface, frequency_hz=400_000.0):
    settings_crc, regs = interface.preset
    return interface.load_preset(0, settings_crc, regs, frequency_hz)


def _code(excinfo):
    return excinfo.value.device_error_code


# ---- transmitter ---------------------------------------------------------------

def test_trigger_does_not_start_with_nothing_loaded(iface):
    with pytest.raises(LIFUDeviceError) as excinfo:
        iface.txdevice.start_trigger()
    assert _code(excinfo) == OW_NO_PRESET
    assert iface.txdevice.get_trigger()["trigger_status"] == "STOPPED"


def test_trigger_does_not_start_above_start_c(iface):
    _load(iface)
    iface.txdevice._modules[0].temp_c = 80.0   # preset start_C is 35
    with pytest.raises(LIFUDeviceError) as excinfo:
        iface.txdevice.start_trigger()
    assert _code(excinfo) == OW_TEMP_TOO_HIGH


def test_trigger_starts_once_loaded_and_cool(iface):
    _load(iface)
    iface.txdevice.start_trigger()
    assert iface.txdevice.get_trigger()["trigger_status"] == "RUNNING"


# ---- console -------------------------------------------------------------------

def test_hv_does_not_come_on_with_nothing_selected(iface):
    with pytest.raises(LIFUDeviceError) as excinfo:
        iface.hvcontroller.turn_hv_on()
    assert _code(excinfo) == OW_HV_NO_PRESET
    assert iface.hvcontroller.get_hv_status() is False


def test_select_returns_the_applied_voltage(iface):
    settings_crc, _ = iface.preset
    assert iface.hvcontroller.select_preset(0, settings_crc, 2720.0) == pytest.approx(40.0)
    # 40 V x 2720 / 2744 = 39.65 V, inside the window.
    assert iface.hvcontroller.select_preset(0, settings_crc, 2744.0) == pytest.approx(40.0 * 2720 / 2744)


@pytest.mark.parametrize("sensitivity", [2267.0, 3500.0])   # 47.99 V and 31.09 V
def test_select_outside_the_fda_window_is_refused_not_clamped(iface, sensitivity):
    settings_crc, _ = iface.preset
    with pytest.raises(LIFUDeviceError) as excinfo:
        iface.hvcontroller.select_preset(0, settings_crc, sensitivity)
    assert _code(excinfo) == OW_HV_RANGE
    assert iface.hvcontroller.get_preset(0)["selected"] is None
    assert iface.hvcontroller.supply_voltage is None


@pytest.mark.parametrize("sensitivity", [0.0, -2720.0, float("nan")])
def test_select_with_a_non_positive_sensitivity_is_refused(iface, sensitivity):
    settings_crc, _ = iface.preset
    with pytest.raises(LIFUDeviceError) as excinfo:
        iface.hvcontroller.select_preset(0, settings_crc, sensitivity)
    assert _code(excinfo) == OW_INVALID_PACKET
    assert iface.hvcontroller.get_preset(0)["selected"] is None


def test_selection_cannot_change_while_hv_is_on(iface):
    settings_crc, _ = iface.preset
    iface.hvcontroller.select_preset(0, settings_crc, 2720.0)
    iface.hvcontroller.turn_hv_on()
    for change in (lambda: iface.hvcontroller.select_preset(0, settings_crc, 2744.0),
                   iface.hvcontroller.deselect_preset):
        with pytest.raises(LIFUDeviceError) as excinfo:
            change()
        assert _code(excinfo) == OW_HV_FDA_REFUSED
    # The live setpoint is untouched.
    assert iface.hvcontroller.get_preset(0)["selected"] == 0
    assert iface.hvcontroller.supply_voltage == pytest.approx(40.0)


# ---- interface -----------------------------------------------------------------

@pytest.mark.parametrize("turn_hv_on, code", [(True, OW_HV_NO_PRESET), (False, OW_NO_PRESET)])
def test_start_sonication_with_nothing_loaded_is_refused_with_hv_off(iface, turn_hv_on, code):
    with pytest.raises(LIFUDeviceError) as excinfo:
        iface.start_sonication(turn_hv_on=turn_hv_on, wait_for_settle=False)
    assert _code(excinfo) == code
    assert iface.hvcontroller.get_hv_status() is False
    assert not iface.is_running()
    assert iface.get_status() != LIFUInterfaceStatus.STATUS_RUNNING


def test_a_trigger_refusal_turns_hv_back_off(iface):
    _load(iface)
    iface.txdevice._modules[0].temp_c = 80.0
    with pytest.raises(LIFUDeviceError) as excinfo:
        iface.start_sonication(turn_hv_on=True, wait_for_settle=False)
    assert _code(excinfo) == OW_TEMP_TOO_HIGH
    assert iface.hvcontroller.get_hv_status() is False


def test_a_load_out_of_the_hv_window_is_on_neither_device(iface):
    # 410 kHz: the default calibration has 2267 there, 47.99 V for this preset.
    with pytest.raises(LIFUDeviceError) as excinfo:
        _load(iface, 410_000.0)
    assert _code(excinfo) == OW_HV_RANGE
    assert iface.txdevice.get_loaded_preset() == (None, None)
    assert iface.get_status() == LIFUInterfaceStatus.STATUS_ERROR


def test_a_frequency_without_calibration_is_refused(iface):
    with pytest.raises(LIFUSolutionError):
        _load(iface, 412_345.0)
    assert iface.txdevice.get_loaded_preset() == (None, None)
    assert iface.hvcontroller.get_preset(0)["selected"] is None


def test_the_calibration_is_reread_on_every_load(iface):
    _load(iface)
    assert iface.hvcontroller.supply_voltage == pytest.approx(40.0)
    # The transmitter's stored calibration changes (another transmitter plugged in).
    iface.txdevice._user_configs[0]["module"]["sensitivity"] = [[400000, 2744]]
    _load(iface)
    assert iface.hvcontroller.supply_voltage == pytest.approx(40.0 * 2720 / 2744)


def test_fda_interface_refuses_a_multi_module_transmitter():
    with pytest.raises(LIFUError) as excinfo:
        SimulatedDeviceInterface(num_modules=2)
    assert excinfo.value.code == LIFU_ERR_MODULE_COUNT_MISMATCH


def test_research_interface_keeps_multi_module_but_not_its_preset_load():
    mc, regs = flash_entry()
    interface = SimulatedLIFUInterface(num_modules=2, preset_flash=[(mc, regs)])
    try:
        with pytest.raises(LIFUError) as excinfo:
            interface.load_preset(0, mc["settings_crc"], regs, 400_000.0)
        assert excinfo.value.code == LIFU_ERR_MODULE_COUNT_MISMATCH
        assert interface.txdevice.get_loaded_preset() == (None, None)
    finally:
        interface.close()


def test_research_simulator_preset_load_refusal_leaves_error_status():
    mc, regs = flash_entry()
    interface = SimulatedLIFUInterface(num_modules=2, preset_flash=[(mc, regs)])
    try:
        with pytest.raises(LIFUError):
            interface.load_preset(0, mc["settings_crc"], regs, 400_000.0)
        assert interface.get_status() == LIFUInterfaceStatus.STATUS_ERROR
    finally:
        interface.close()


def test_research_simulator_console_keeps_the_fda_select_rules():
    # An RUO console image has no preset table, so the simulator's preset select is the
    # FDA console's on both interfaces; HV on and set_voltage stay ungated (RUO).
    mc, regs = flash_entry()
    interface = SimulatedLIFUInterface(preset_flash=[(mc, regs)])
    try:
        with pytest.raises(LIFUDeviceError) as excinfo:
            interface.hvcontroller.select_preset(0, mc["settings_crc"], 2267.0)   # 47.99 V
        assert _code(excinfo) == OW_HV_RANGE
        interface.hvcontroller.set_voltage(60.0)
        interface.hvcontroller.turn_hv_on()
        with pytest.raises(LIFUDeviceError) as excinfo:
            interface.hvcontroller.select_preset(0, mc["settings_crc"], 2720.0)
        assert _code(excinfo) == OW_HV_FDA_REFUSED
        assert interface.hvcontroller.get_voltage() == pytest.approx(60.0)
    finally:
        interface.close()


def test_any_failure_starting_the_run_turns_hv_back_off(iface, monkeypatch):
    # Not only device refusals: an unexpected error building the run must not leave HV on.
    import openlifu_sdk.ui.simulated_interface as sim

    def broken(*args, **kwargs):
        raise TypeError("engine construction failed")

    _load(iface)
    monkeypatch.setattr(sim, "_SimulatedRunEngine", broken)
    with pytest.raises(TypeError):
        iface.start_sonication(turn_hv_on=True, wait_for_settle=False)
    assert iface.hvcontroller.get_hv_status() is False


def test_research_simulator_has_no_preset_gates():
    # SlicerOpenLIFU and the test app drive the RUO simulator with set_solution and no preset.
    interface = SimulatedLIFUInterface()
    try:
        interface.hvcontroller.turn_hv_on()
        interface.txdevice.start_trigger()
        assert interface.hvcontroller.get_hv_status() is True
        assert interface.txdevice.get_trigger()["trigger_status"] == "RUNNING"
    finally:
        interface.close()
