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

pytest.importorskip("openlifu_sdk.ui._qt")

from sr003_presets import flash_entry, two_module_flash_entry  # noqa: E402 -- reason: after the Qt importorskip

from openlifu_sdk.io.exceptions import LIFUDeviceError, LIFUError, LIFUSolutionError  # noqa: E402 -- reason: after the Qt importorskip
from openlifu_sdk.io.LIFUConfig import (  # noqa: E402 -- reason: after the Qt importorskip
    LIFU_ERR_MODULE_COUNT_MISMATCH,
    OW_HV_FDA_REFUSED,
    OW_HV_NO_PRESET,
    OW_HV_RANGE,
    OW_BAD_CRC,
    OW_HV_DATA_ERROR,
    OW_NO_PRESET,
    OW_TEMP_TOO_HIGH,
)
from openlifu_sdk.io.LIFUInterface import LIFUInterfaceStatus  # noqa: E402 -- reason: after the Qt importorskip
from openlifu_sdk.ui.simulated_interface import (  # noqa: E402 -- reason: after the Qt importorskip
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
    assert _code(excinfo) == OW_HV_DATA_ERROR
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


@pytest.mark.parametrize("interface_cls", [SimulatedDeviceInterface, SimulatedLIFUInterface])
def test_a_two_module_preset_loads_on_a_two_module_transmitter(interface_cls):
    mc, regs = two_module_flash_entry()
    assert isinstance(regs, list) and len(regs) == 2
    interface = interface_cls(num_modules=2, preset_flash=[(mc, regs)])
    try:
        interface.load_preset(0, mc["settings_crc"], regs, 400_000.0)
        assert interface.txdevice._loaded_modules == {0, 1}
        assert interface.get_status() == LIFUInterfaceStatus.STATUS_READY
        interface.unload_preset()
        assert interface.txdevice._loaded_modules == set()
    finally:
        interface.close()


@pytest.mark.parametrize("interface_cls", [SimulatedDeviceInterface, SimulatedLIFUInterface])
def test_a_preset_whose_module_count_differs_from_the_transmitter_is_refused(interface_cls):
    # The set-preset guard: the diathermy preset (64 elements, 1x) on a 2x transmitter, and the reverse.
    one, one_regs = flash_entry()
    two, two_regs = two_module_flash_entry()
    for modules, (mc, regs) in ((2, (one, one_regs)), (1, (two, two_regs))):
        interface = interface_cls(num_modules=modules, preset_flash=[(mc, regs)])
        try:
            with pytest.raises(LIFUError) as excinfo:
                interface.load_preset(0, mc["settings_crc"], regs, 400_000.0)
            assert excinfo.value.code == LIFU_ERR_MODULE_COUNT_MISMATCH
            assert interface.txdevice._loaded_modules == set()
            assert interface.get_status() == LIFUInterfaceStatus.STATUS_ERROR
        finally:
            interface.close()


def test_a_module_crc_from_another_preset_is_refused_by_that_module():
    mc, regs = two_module_flash_entry()
    interface = SimulatedDeviceInterface(num_modules=2, preset_flash=[(mc, regs)])
    try:
        with pytest.raises(LIFUDeviceError) as excinfo:
            interface.load_preset(0, mc["settings_crc"], [regs[0], regs[0]], 400_000.0)
        assert _code(excinfo) == OW_BAD_CRC
        assert interface.txdevice._loaded_modules == set()   # on both or on neither
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


# ---- two layers: the preset's own voltage_range vs the FDA image's hard stop ------
# Default calibration: 2720 @ 400 kHz, 2744 @ 395 kHz, 2267 @ 410 kHz, 2300 @ 405 kHz.
# A 40 V / 2720 preset therefore applies 40.0, 39.65, 47.99 and 47.30 V.

@pytest.mark.parametrize("interface_cls", [SimulatedDeviceInterface, SimulatedLIFUInterface])
def test_preset_37_42_loads_within_its_range(interface_cls):
    mc, regs = flash_entry(voltage_range=[37.0, 42.0])
    interface = interface_cls(preset_flash=[(mc, regs)])
    try:
        assert interface.hvcontroller.select_preset(0, mc["settings_crc"], 2720.0) == pytest.approx(40.0)
    finally:
        interface.close()


@pytest.mark.parametrize("interface_cls", [SimulatedDeviceInterface, SimulatedLIFUInterface])
def test_the_host_holds_a_load_to_the_presets_own_range(interface_cls):
    # Calibration 2507 at 400 kHz: 43.4 V, inside the FDA hard stop, outside this preset's [37, 42].
    mc, regs = flash_entry(voltage_range=[37.0, 42.0])
    interface = interface_cls(preset_flash=[(mc, regs)])
    try:
        interface.txdevice._user_configs[0]["module"]["sensitivity"] = [[400000, 2507]]
        with pytest.raises(LIFUSolutionError, match="voltage_range"):
            interface.load_preset(0, mc["settings_crc"], regs, 400_000.0,
                                  voltage_range=tuple(mc["voltage_range"]), turn_hv_on=True)
        assert interface.hvcontroller.get_hv_status() is False
        assert interface.txdevice.get_loaded_preset() == (None, None)
        # The consoles themselves never see the range: without it, the same load goes through.
        interface.load_preset(0, mc["settings_crc"], regs, 400_000.0)
        assert interface.hvcontroller.supply_voltage == pytest.approx(43.4, abs=0.01)
    finally:
        interface.close()


def test_preset_35_55_works_unless_the_fda_hard_stop_is_crossed():
    mc, regs = flash_entry(voltage_range=[35.0, 55.0])
    ruo = SimulatedLIFUInterface(preset_flash=[(mc, regs)])
    fda = SimulatedDeviceInterface(preset_flash=[(mc, regs)])
    try:
        # 47.99 V: within the preset's range, so a non-FDA console applies it ...
        assert ruo.hvcontroller.select_preset(0, mc["settings_crc"], 2267.0) == pytest.approx(47.99, abs=0.01)
        # ... but over the FDA image's 45 V hard stop.
        with pytest.raises(LIFUDeviceError) as excinfo:
            fda.hvcontroller.select_preset(0, mc["settings_crc"], 2267.0)
        assert _code(excinfo) == OW_HV_RANGE
        # Within both, both apply it.
        assert fda.hvcontroller.select_preset(0, mc["settings_crc"], 2744.0) == pytest.approx(39.65, abs=0.01)
    finally:
        ruo.close()
        fda.close()


def test_preset_data_cannot_widen_the_fda_hard_stop(iface):
    console = iface.hvcontroller.get_preset(0)
    iface.hvcontroller.configure_presets([dict(console, voltage_range=[5.0, 100.0], min_voltage=5.0, max_voltage=100.0)])
    with pytest.raises(LIFUDeviceError) as excinfo:
        iface.hvcontroller.select_preset(0, iface.preset[0], 2267.0)   # 47.99 V
    assert _code(excinfo) == OW_HV_RANGE
    assert (iface.hvcontroller.get_preset(0)["min_voltage"], iface.hvcontroller.get_preset(0)["max_voltage"]) == (35.0, 45.0)


def test_non_fda_console_has_no_fda_image_rules():
    mc, regs = flash_entry()   # no voltage_range: nothing to hold a select to
    interface = SimulatedLIFUInterface(preset_flash=[(mc, regs)])
    try:
        assert interface.hvcontroller.select_preset(0, mc["settings_crc"], 2267.0) == pytest.approx(47.99, abs=0.01)
        interface.hvcontroller.set_voltage(60.0)
        interface.hvcontroller.turn_hv_on()
        # Selection may change with HV on: not an FDA image.
        assert interface.hvcontroller.select_preset(0, mc["settings_crc"], 2720.0) == pytest.approx(40.0)
        interface.hvcontroller.deselect_preset()
    finally:
        interface.close()


@pytest.mark.parametrize("voltage, transmitter, accepted", [
    (35.0, 2720.0, True),                 # exactly 35.0 V in float32
    (35.0, 2720.0005, False),             # next float32 sensitivity up: 34.999996 V
    (45.0, 2720.0, True),                 # exactly 45.0 V
    (45.0, 2719.9995, False),             # next float32 sensitivity down: 45.000008 V
])
def test_fda_hard_stop_boundaries_in_float32(voltage, transmitter, accepted):
    # The console computes voltage x preset sensitivity / transmitter in float32 and compares
    # with 35.0f / 45.0f; the simulator does the same, so edge presets agree with hardware.
    mc, regs = flash_entry(voltage=voltage)
    interface = SimulatedDeviceInterface(preset_flash=[(mc, regs)])
    try:
        if accepted:
            assert interface.hvcontroller.select_preset(0, mc["settings_crc"], transmitter) == voltage
        else:
            with pytest.raises(LIFUDeviceError) as excinfo:
                interface.hvcontroller.select_preset(0, mc["settings_crc"], transmitter)
            assert _code(excinfo) == OW_HV_RANGE
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
